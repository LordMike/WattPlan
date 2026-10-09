"""Named configurations preserve existing tuning and isolate cached state."""

import pytest

from custom_components.wattplan.optimizer.models import encode_state_blob
from custom_components.wattplan.optimizer.planners.presets import (
    PRESETS, get_preset, lookahead_slots, run_preset,
)
from custom_components.wattplan.optimizer.planners.registry import BUILTINS, create_planner
from tests.optimizer.planner_conformance import conformance_params


def test_catalog_covers_all_algorithms_and_every_factory_accepts_its_settings():
    assert {preset.planner for preset in PRESETS.values()} == set(BUILTINS)
    for preset in PRESETS.values():
        assert callable(create_planner(preset.planner, dict(preset.options)))
    with pytest.raises(ValueError, match="Unknown planner preset"):
        get_preset("typo")


def test_highs_presets_apply_slots_and_discard_incompatible_state(monkeypatch):
    from custom_components.wattplan import optimizer

    seen = []
    legacy = encode_state_blob({"v": 1})

    def fake(params):
        seen.append((params.lookahead_slots, params.state))
        return {"state": legacy}

    monkeypatch.setattr(optimizer, "optimize", fake)
    params = conformance_params()[1].model_copy(update={"lookahead_slots": 22})
    first = run_preset(params)
    second = run_preset(params.model_copy(update={"state": first["state"]}), "highs-16")
    run_preset(params.model_copy(update={"state": second["state"]}), "highs-16")
    run_preset(params.model_copy(update={"state": second["state"]}), "highs-24")
    assert seen == [(22, None), (16, None), (16, legacy), (24, None)]
    assert second["diagnostics"]["planner"]["preset"] == "highs-16"
    assert second["diagnostics"]["planner"]["options"] == {"lookahead_slots": 16}


def test_effective_horizon_has_unambiguous_units():
    assert lookahead_slots("highs", 22, 96) == 22
    assert lookahead_slots("highs-16", 22, 96) == 16
    assert lookahead_slots("highs-24", 22, 8) == 8
    assert lookahead_slots("greedy-8", 22, 96) == 96


@pytest.mark.parametrize("value", [True, 1, 673, "16", 1.5])
def test_highs_rejects_invalid_preset_lookahead(value):
    with pytest.raises(ValueError, match="lookahead_slots"):
        create_planner("highs", {"lookahead_slots": value})
