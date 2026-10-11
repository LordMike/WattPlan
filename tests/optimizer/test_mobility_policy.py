"""Mobility service first, then bounded joint economic policy search."""
from itertools import product

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.physics import EPSILON, transition_at
from custom_components.wattplan.optimizer.planners import mobility_policy
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.presets import PRESETS, run_preset
from custom_components.wattplan.optimizer.planners.registry import create_planner


def request(*, prices=(5., .1, 0.), usage=None, pv=None, initial=0.,
            connected=None, draws=None, departures=None, house=False, ingress=3):
    prices=tuple(prices)+(0.,)*max(4-len(prices),0)
    horizon=len(prices)
    def profile(values,default):
        row=list(values) if values is not None else [default]*horizon
        return row+[default]*max(horizon-len(row),0)
    vehicle=dict(name='vehicle',initial_kwh=initial,minimum_kwh=0.,capacity_kwh=3.,
        charge_curve_kwh=[1.],discharge_curve_kwh=[1.],charge_efficiency=1.,
        discharge_efficiency=1.,can_charge_from=ingress,
        connected_slots=profile(connected,True if connected is None else connected[-1]),
        external_energy_kwh=profile(draws,0.),
        departure_requirements=departures if departures is not None else [dict(timeslot=1,minimum_kwh=1.)])
    batteries=[vehicle]
    if house:
        batteries.insert(0,dict(name='house',initial_kwh=0.,minimum_kwh=0.,capacity_kwh=2.,
            charge_curve_kwh=[1.],discharge_curve_kwh=[1.],charge_efficiency=1.,
            discharge_efficiency=1.,can_charge_from=3))
    return OptimizationParams(plan_start='2026-01-01T00:00:00+00:00',slot_minutes=60,
        usage_kwh=profile(usage,0.),solar_input_kwh=profile(pv,0.),
        grid_import_price_per_kwh=list(prices),grid_export_price_per_kwh=[0.]*horizon,
        battery_entities=batteries,comfort_entities=[],optional_entities=[])


def policies(result):
    return [[action['state'] for action in entity['schedule']]
        for entity in result['entities'] if entity['type']=='battery']


def independent_score(params,rows):
    """Direct physical full schedule, independent from beam/evaluator recursion."""
    levels=[b.initial_kwh for b in params.battery_entities]
    miss=cost=0.
    for slot in range(len(params.usage_kwh)):
        step=transition_at(params.battery_entities,levels,[row[slot] for row in rows],
            slot=slot,usage=params.usage_kwh[slot],pv=params.solar_input_kwh[slot])
        before=step.levels_before_external or step.levels
        miss+=sum(step.external_shortfall or ())
        miss+=sum(max(r.minimum_kwh-before[i],0.) for i,b in enumerate(params.battery_entities)
            for r in b.departure_requirements if r.timeslot==slot)
        cost+=step.grid_import*params.grid_import_price_per_kwh[slot]-step.grid_export*params.grid_export_price_per_kwh[slot]
        levels=step.levels
    return miss,0.,cost


def exhaustive_score(params):
    horizon=len(params.usage_kwh)
    choices=[tuple(product(('self_consume','preserve','grid_charge') if b.can_charge_from&1
        else ('self_consume','preserve'),repeat=horizon)) for b in params.battery_entities]
    return min(independent_score(params,rows) for rows in product(*choices))


def test_baseline_first_available_versus_economic_available_cheapest_slot():
    params=request(connected=(True,True,False),draws=(0.,0.,1.))
    snapshot=params.model_dump()
    baseline=mobility_policy.baseline_factory()(params)
    result=mobility_policy.factory(beam_width=100,max_expansions=1000)(params)
    assert policies(baseline)[0][0]=='grid_charge'
    assert policies(result)[0][0]!='grid_charge'
    assert policies(result)[0][1]=='grid_charge'
    assert baseline['projections']['projected_cost']==pytest.approx(5.)
    assert result['projections']['projected_cost']==pytest.approx(.1)
    assert independent_score(params,policies(result))==pytest.approx(exhaustive_score(params))
    assert evaluate_schedule(params,policies(result)).score==pytest.approx((0.,0.,.1))
    assert params.model_dump()==snapshot


@pytest.mark.parametrize('params',[
    request(),
    request(connected=(True,False,True),draws=(0.,.5,0.),departures=[dict(timeslot=2,minimum_kwh=.5)]),
    request(prices=(.2,.8,.3),usage=(.5,.7,.2),pv=(1.,0.,0.),house=True),
    request(prices=(5.,.1,0.),usage=(1.,0.,0.),initial=1.,house=True,
        departures=[dict(timeslot=2,minimum_kwh=1.)]),
    request(prices=(-.1,.4,.3),pv=(.3,0.,0.),initial=.2),
])
def test_unpruned_tiny_fleet_matches_independent_causal_exhaustive_score(params):
    result=mobility_policy.factory(beam_width=10000,max_expansions=100000,max_joint_actions=100)(params)
    assert independent_score(params,policies(result))==pytest.approx(exhaustive_score(params))
    replay=evaluate_schedule(params,policies(result))
    assert result['projections']['projected_cost']==pytest.approx(replay.tariff_cost)
    for i,entity in enumerate(result['entities']):
        assert [a['level'] for a in entity['schedule']]==pytest.approx([s.levels[i] for s in replay.steps])


def test_vehicle_can_cover_house_then_recharge_before_departure():
    params=request(prices=(5.,.1,.5,0.),usage=(1.,0.,0.),initial=1.,house=True,
        departures=[dict(timeslot=2,minimum_kwh=1.)])
    result=mobility_policy.factory(beam_width=100,max_expansions=10000)(params)
    assert independent_score(params,policies(result))==pytest.approx((0.,0.,.1))
    replay=evaluate_schedule(params,policies(result))
    assert replay.steps[0].discharge[1]==pytest.approx(1.)
    assert replay.steps[1].grid_charge[1]==pytest.approx(1.)


def test_repeated_departures_trip_draws_and_away_mask():
    params=request(prices=(.2,5.,.1,5.),connected=(True,False,True,False),
        draws=(0.,1.,0.,1.),departures=[dict(timeslot=0,minimum_kwh=1.),dict(timeslot=2,minimum_kwh=1.)])
    for factory in (mobility_policy.baseline_factory,mobility_policy.factory):
        result=factory()(params)
        replay=evaluate_schedule(params,policies(result))
        assert replay.score[0]==pytest.approx(0.)
        assert replay.steps[1].grid_charge==[0.]
        assert replay.steps[3].discharge==[0.]
        assert replay.steps[-1].levels==pytest.approx([0.])


def test_current_trip_draw_above_departure_requirement_is_not_ignored():
    params=request(initial=1.,draws=(2.,0.,0.),departures=[dict(timeslot=0,minimum_kwh=1.)])
    result=mobility_policy.baseline_factory()(params)
    assert evaluate_schedule(params,policies(result)).score[0]==pytest.approx(0.)
    assert policies(result)[0][0]=='grid_charge'


def test_impossible_service_is_reported_without_manufactured_energy():
    params=request(connected=(False,False,False),draws=(1.,0.,0.),
        departures=[dict(timeslot=0,minimum_kwh=2.)])
    result=mobility_policy.factory()(params)
    replay=evaluate_schedule(params,policies(result))
    assert replay.score[0]==pytest.approx(3.)
    assert all(step.levels==[0.] for step in replay.steps)
    assert result['diagnostics']['mobility_service_seed_retained'] is True


def test_shared_pv_competition_cannot_allocate_the_same_energy_twice():
    params=request(pv=(1.,0.,0.),house=True,ingress=2,
        departures=[dict(timeslot=0,minimum_kwh=1.)])
    result=mobility_policy.factory()(params)
    replay=evaluate_schedule(params,policies(result))
    assert replay.score[0]>=1.-EPSILON
    assert sum(replay.steps[0].pv_charge)<=1.+EPSILON
    assert 'grid_charge' not in policies(result)[1]


def test_bound_keeps_full_service_seed_and_reports_expansion_limit():
    params=request(prices=(.1,)*40,connected=(True,)*40,draws=(0.,)*39+(2.,),
        departures=[dict(timeslot=38,minimum_kwh=2.)],house=True)
    result=mobility_policy.factory(beam_width=1,max_expansions=1,max_joint_actions=1)(params)
    baseline=evaluate_schedule(params,policies(mobility_policy.baseline_factory()(params)))
    evaluation=evaluate_schedule(params,policies(result))
    assert evaluation.score<=baseline.score
    assert all(len(row)==40 for row in policies(result))
    assert result['diagnostics']['expansions']==1
    assert result['diagnostics']['termination_reason']=='evaluation_limit'
    assert result['diagnostics']['tail_transitions']==39


@pytest.mark.parametrize('options',[dict(beam_width=True),dict(max_expansions=0),
    dict(max_joint_actions=1.5),dict(max_seconds=float('nan'))])
def test_invalid_search_options_rejected(options):
    with pytest.raises(ValueError):
        mobility_policy.factory(**options)


def test_mobility_presets_registry_and_old_family_rejection():
    params=request()
    for name in ('mobility-baseline','mobility-tariff-seeds-2','mobility-fleet-beam-4-exp1500-joint27','mobility-fleet-beam-8-exp6000-joint27'):
        assert name in PRESETS
        assert run_preset(params,name)['entities']
    with pytest.raises(ValueError,match='mobility'):
        create_planner('self-consumption')(params)
    assert create_planner('mobility-fleet-beam').capabilities.mobility is True


def test_two_seed_variant_delays_to_cheaper_connected_slot_with_service_intact():
    params=request(connected=(True,True,False),draws=(0.,0.,1.))
    result=mobility_policy.tariff_factory()(params)
    assert policies(result)[0][:2]==['preserve','grid_charge']
    assert evaluate_schedule(params,policies(result)).score==pytest.approx((0.,0.,.1))
    assert result['diagnostics']['evaluations']==2
    assert result['diagnostics']['tariff_seed_accepted'] is True


def test_two_seed_optimistic_delay_cannot_worsen_service_against_baseline():
    params=request(prices=(1.,.1,.1),departures=[dict(timeslot=1,minimum_kwh=1.5)])
    params.battery_entities[0].charge_curve_kwh=[1.,0.]
    result=mobility_policy.tariff_factory()(params)
    baseline=evaluate_schedule(params,policies(mobility_policy.baseline_factory()(params)))
    assert evaluate_schedule(params,policies(result)).score<=baseline.score


def test_variable_efficiencies_curves_and_preferences_replay_complete_horizon():
    params=request(initial=.3,usage=(.4,.2,.7),pv=(.3,0.,.5),house=True)
    params.throughput_cost_per_kwh=.07
    params.mode_switch_cost=.11
    for battery in params.battery_entities:
        battery.charge_curve_kwh=[.8,.4]
        battery.discharge_curve_kwh=[.3,.7]
        battery.charge_efficiency=.9
        battery.discharge_efficiency=.8
    result=mobility_policy.factory(beam_width=50,max_expansions=10000)(params)
    replay=evaluate_schedule(params,policies(result))
    baseline=evaluate_schedule(params,policies(mobility_policy.baseline_factory()(params)))
    assert replay.score<=baseline.score
    assert result['diagnostics']['evaluation']['score']==pytest.approx(replay.score)


def test_ordinary_house_target_is_after_mobility_and_before_tariff():
    params=request(house=True)
    payload=params.model_dump()
    payload['battery_entities'][0]['target']=dict(timeslot=1,soc_kwh=1.,mode='at_least')
    params=OptimizationParams(**payload)
    result=mobility_policy.factory(beam_width=1000,max_expansions=10000)(params)
    replay=evaluate_schedule(params,policies(result))
    assert replay.score[:2]==pytest.approx((0.,0.))
    assert replay.score[2]==pytest.approx(.2)


def test_one_comfort_wrapper_and_complete_result(monkeypatch):
    from custom_components.wattplan.optimizer.planners import comfort
    calls=[]
    original=comfort.prepare_comfort
    def prepare(params):
        calls.append(params)
        return original(params)
    monkeypatch.setattr(comfort,'prepare_comfort',prepare)
    result=mobility_policy.factory()(request())
    assert len(calls)==1
    assert len(result['energy_flows']['per_slot'])==4
    assert result['projections']['service_feasible'] is True


def test_baseline_mobile_identity_survives_no_visible_trip_horizon():
    params=request(prices=(.1,)*36,usage=(.5,)*36,initial=2.,house=True,departures=[])
    params.battery_entities[0].initial_kwh=1.
    # Explicit always-connected/zero-draw metadata still declares mobile control.
    result=mobility_policy.baseline_factory()(params)
    assert policies(result)[1]==['preserve']*36
    assert policies(result)[0]==['self_consume']*36
    assert result['projections']['final_soc_kwh']['vehicle']==pytest.approx(2.)
