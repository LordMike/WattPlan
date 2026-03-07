"""Export HA state, recorder history, and statistics for WattPlan forecasting."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import ClientSession


def _iso_utc(value: datetime) -> str:
    """Return UTC ISO timestamp for API requests."""
    return value.astimezone(UTC).isoformat()


async def _fetch_state(
    session: ClientSession, *, base_url: str, entity_id: str
) -> dict[str, Any]:
    """Fetch current state payload from HA REST API."""
    async with session.get(f"{base_url}/api/states/{entity_id}") as response:
        response.raise_for_status()
        return await response.json()


async def _fetch_history(
    session: ClientSession,
    *,
    base_url: str,
    entity_id: str,
    start_at: datetime,
    end_at: datetime,
) -> Any:
    """Fetch recorder history rows from HA REST API."""
    url = f"{base_url}/api/history/period/{quote(_iso_utc(start_at), safe='')}"
    params = {
        "end_time": _iso_utc(end_at),
        "filter_entity_id": entity_id,
        "minimal_response": "",
        "no_attributes": "",
    }
    async with session.get(url, params=params) as response:
        response.raise_for_status()
        return await response.json()


async def _fetch_statistics(
    session: ClientSession,
    *,
    base_url: str,
    token: str,
    entity_id: str,
    start_at: datetime,
    end_at: datetime,
    period: str,
) -> Any:
    """Fetch recorder statistics rows from HA websocket API."""
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    async with session.ws_connect(f"{ws_url}/api/websocket") as websocket:
        await websocket.receive_json()
        await websocket.send_json({"type": "auth", "access_token": token})
        auth_result = await websocket.receive_json()
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"Authentication failed: {auth_result}")

        await websocket.send_json(
            {
                "id": 1,
                "type": "recorder/statistics_during_period",
                "start_time": _iso_utc(start_at),
                "end_time": _iso_utc(end_at),
                "statistic_ids": [entity_id],
                "period": period,
                "types": ["sum", "mean", "state", "change"],
            }
        )
        message = await websocket.receive_json()
        if not message.get("success", False):
            raise RuntimeError(f"Statistics request failed: {message}")
        return message.get("result")


async def _async_main(args: argparse.Namespace) -> None:
    """Run export against one Home Assistant instance."""
    start_at = datetime.fromisoformat(args.start_at)
    end_at = datetime.fromisoformat(args.end_at)
    headers = {
        "Authorization": f"Bearer {args.token}",
        "Content-Type": "application/json",
    }

    async with ClientSession(headers=headers) as session:
        state = await _fetch_state(
            session, base_url=args.base_url, entity_id=args.entity_id
        )
        history = await _fetch_history(
            session,
            base_url=args.base_url,
            entity_id=args.entity_id,
            start_at=start_at,
            end_at=end_at,
        )
        statistics = await _fetch_statistics(
            session,
            base_url=args.base_url,
            token=args.token,
            entity_id=args.entity_id,
            start_at=start_at,
            end_at=end_at,
            period=args.period,
        )

    payload = {
        "entity_id": args.entity_id,
        "start_at": _iso_utc(start_at),
        "end_at": _iso_utc(end_at),
        "period": args.period,
        "state": state,
        "history": history,
        "statistics": statistics,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), "utf-8")


def main() -> None:
    """Parse arguments and run the export."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--start-at", required=True)
    parser.add_argument("--end-at", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--period",
        default="hour",
        choices=["5minute", "hour", "day", "week", "month", "year"],
    )
    args = parser.parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
