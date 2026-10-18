"""Portable JSON, CSV and HTML reports, with coverage and terminal reserves."""

import csv
import json
import math
import statistics
from html import escape
from pathlib import Path


def latency_summary(cycles: list[dict], key: str) -> dict:
    values = sorted(row[key] for row in cycles)
    return {
        "count": len(values),
        "median": statistics.median(values) if values else None,
        "p95": values[max(0, math.ceil(len(values) * 0.95) - 1)] if values else None,
        "max": max(values) if values else None,
    }


def comparisons(runs: list[dict]) -> list[dict]:
    """Compare completed service-feasible runs against the original reference."""
    if not runs or runs[0]["status"] != "complete":
        return []
    baseline = runs[0]
    complete = [run for run in runs[1:] if run["status"] == "complete"]
    if not baseline["totals"].get("service_feasible", True):
        # Never silently promote an alternative into the configured control.
        return []
    result = []
    for run in complete:
        if not run["totals"].get("service_feasible", True):
            continue
        deltas = [
            {
                "segment": segment["segment"],
                "final_soc_delta_kwh": {
                    name: level - base["final_soc_kwh"][name]
                    for name, level in segment["final_soc_kwh"].items()
                },
            }
            for segment, base in zip(run["segments"], baseline["segments"], strict=True)
        ]
        result.append(
            {
                "run": run["run"],
                "reference": baseline["run"],
                "net_cost_delta": run["totals"]["net_cost"]
                - baseline["totals"]["net_cost"],
                "segments": deltas,
                "equal_terminal_reserves": all(
                    abs(value) <= 1e-6
                    for segment in deltas
                    for value in segment["final_soc_delta_kwh"].values()
                ),
            }
        )
    return result


def _csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        if not rows:
            return
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(
            {
                key: json.dumps(value, sort_keys=True)
                if isinstance(value, (dict, list))
                else value
                for key, value in row.items()
            }
            for row in rows
        )


def _plot(run: dict, key: str) -> str:
    rows = run["intervals"]
    if not rows:
        return ""
    values, total = [], 0.0
    for row in rows:
        if key == "net_cost":
            total += row[key]
            values.append(total)
        else:
            values.append(sum(row["soc_kwh"].values()))
    low, high = min(0, min(values)), max(values)
    span = max(high - low, 1e-9)
    points = " ".join(
        f"{30 + i * 710 / max(len(values) - 1, 1):.2f},{170 - (value - low) * 140 / span:.2f}"
        for i, value in enumerate(values)
    )
    label = (
        "Cumulative net cost"
        if key == "net_cost"
        else "Stored energy (kWh; resets at segment boundaries)"
    )
    return (
        f"<figure><figcaption>{escape(label)} · {low:.4g} … {high:.4g}</figcaption>"
        f'<svg viewBox="0 0 770 190" role="img" aria-label="{escape(label)}">'
        '<path d="M30 20V170H750" fill="none" stroke="#999"/>'
        f'<polyline points="{points}" fill="none" stroke="#087f8c" stroke-width="2"/></svg>'
        "<small>Executed interval index (chronological, uniform energy within each measured interval).</small></figure>"
    )


def write_reports(output: Path, summary: dict, runs: list[dict]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    compact = [
        {key: value for key, value in run.items() if key not in {"cycles", "intervals"}}
        for run in runs
    ]
    summary = {**summary, "runs": compact, "comparisons": comparisons(runs)}
    excluded = [run["run"] for run in runs if run["status"] == "complete"
                and not run["totals"].get("service_feasible", True)]
    if excluded:
        summary["comparison_exclusions"] = {name: "mobility_service_infeasible" for name in excluded}
        complete = [run for run in runs if run["status"] == "complete"]
        if complete and complete[0]["run"] in excluded:
            summary["comparison_omission_reason"] = "reference_mobility_service_infeasible"
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    sections = []
    for i, run in enumerate(runs):
        _csv(output / f"{i + 1:02d}-cycles.csv", run["cycles"])
        _csv(output / f"{i + 1:02d}-intervals.csv", run["intervals"])
        (output / f"{i + 1:02d}-run.json").write_text(
            json.dumps(run, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        totals = run.get("totals") or {}
        rows = "".join(
            f"<tr><th>{escape(key)}</th><td>{value:.6g}</td></tr>"
            for key, value in totals.items()
        )
        reserves = "".join(
            f"<tr><td>{s['segment']}</td><td>{escape(s['start'])}</td>"
            f"<td>{escape(s.get('end', 'failed'))}</td><td>{escape(json.dumps(s['initial_soc_kwh']))}</td>"
            f"<td>{escape(json.dumps(s.get('final_soc_kwh')))}</td></tr>"
            for s in run["segments"]
        )
        sections.append(
            f"<section><h2>{escape(run['run'])} · {escape(run['status'])}</h2>"
            + ("<p>Mobility service failed; trajectory costs are diagnostic and excluded from comparisons.</p>"
               if run["run"] in excluded else "")
            +
            f"<p>Whole-run wall-clock limit: {escape(str(run.get('run_timeout_seconds', 'unrecorded')))} seconds; "
            f"observed run time: {escape(str(run.get('run_wall_seconds', 'unrecorded')))} seconds.</p>"
            f"<p>{escape(run.get('failure') or '')}</p><table>{rows}</table>"
            "<h3>Stored energy by segment</h3><table><tr><th>Segment</th><th>Start</th><th>End</th>"
            f"<th>Initial kWh</th><th>Final kWh</th></tr>{reserves}</table>"
            + _plot(run, "net_cost")
            + _plot(run, "soc")
            + "</section>"
        )
    warnings = " ".join(summary.get("warnings", []))
    coverage = summary.get("coverage", {})
    inferred = coverage.get("inferred_range", {})
    range_text = (
        (
            f"Inferred range: {inferred.get('start')} to {inferred.get('end')} (end exclusive); "
            f"{inferred.get('hours')} hours / {inferred.get('slots')} slots. "
            f"Executed duration: {coverage.get('covered_seconds')} seconds. "
            f"Explicit repairs: {json.dumps(coverage.get('patches', []))}"
        )
        if inferred
        else ""
    )
    html = (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>WattPlan simulation</title>'
        "<style>body{font:16px system-ui;max-width:1050px;margin:3rem auto;padding:0 1rem;color:#172d35}"
        "table{border-collapse:collapse}td,th{padding:.4rem .8rem;border:1px solid #ccd;text-align:left}"
        "svg{width:100%;max-width:770px}section{border-top:2px solid #ccd;margin-top:2rem}small{color:#456}</style>"
        "<h1>WattPlan simulation</h1><p>Measured load and PV determine execution. Each planner sees its recorded forecasts. "
        "Optional suggestions are advisory; comfort loads are unsupported. Compute latency is measured, not applied as an actuator delay.</p>"
        "<p>Compare costs together with initial/final stored energy and coverage. Stored energy has no assumed terminal monetary value. "
        "Failed runs have no scored totals. Loss and throughput use AC-side accounting.</p>"
        f'<p>{escape(range_text)}</p><p>{escape(warnings)}</p><p>Full provenance, coverage and comparisons: <a href="summary.json">summary.json</a>.</p>'
        + "".join(sections)
        + "</html>"
    )
    (output / "report.html").write_text(html, encoding="utf-8")
