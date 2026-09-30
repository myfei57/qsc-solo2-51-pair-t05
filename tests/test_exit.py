"""Audited automatic release of the brine valve emergency position."""

import pytest

from desalplant.audit.recorder import Recorder
from desalplant.brine.exit import (
    ABORT_KIND,
    COMPLETE_KIND,
    ENTER_KIND,
    STEP_KIND,
    EmergencyExit,
    ExitState,
)
from desalplant.brine.limit import RecoveryLimit
from desalplant.brine.valve import Valve
from desalplant.brine.water_quality import ExitMode, WaterType, exit_mode
from desalplant.clock import LogicalClock
from desalplant.errors import ExitNotArmedError
from desalplant.ro.recovery import Recovery
from desalplant.store.store import Store

BAND = RecoveryLimit(min=30.0, max=55.0)


def build(water, rate=42.0):
    clock = LogicalClock()
    recorder = Recorder(Store(), clock)
    recovery = Recovery()
    recovery.update_rate(rate)
    valve = Valve()
    exit_ = EmergencyExit(recovery, valve, BAND, water=water, recorder=recorder, clock=clock)
    return exit_, recovery, valve, recorder, clock


def alarmed(exit_, recovery):
    """Trip the alarm and confirm the valve went to emergency."""
    recovery.raise_alarm()
    assert exit_.trip() is True
    assert exit_.state is ExitState.EMERGENCY


def settle(recovery, rate=42.0):
    """Operator/latch confirms the alarm condition genuinely cleared."""
    recovery.clear_alarm()
    recovery.update_rate(rate)


# --------------------------------------------------------------- water policy


def test_exit_mode_is_fixed_by_water_type():
    assert exit_mode(WaterType.SEAWATER) is ExitMode.STEPPED
    assert exit_mode(WaterType.BRACKISH) is ExitMode.ONE_SHOT


# ------------------------------------------------------------- entry guard ---


def test_trigger_is_refused_while_the_latch_stays_raised():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    recovery.update_rate(42.0)  # rate healthy but latch never cleared

    assert exit_.trigger() is ExitState.EMERGENCY
    assert valve.emergency is True
    assert exit_.state is ExitState.EMERGENCY


def test_trigger_is_refused_while_the_rate_is_outside_the_band():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    settle(recovery, rate=60.0)  # latch down, recovery still too high

    assert exit_.trigger() is ExitState.EMERGENCY
    assert valve.emergency is True


# ----------------------------------------------------- seawater stepped exit


def test_seawater_leaves_emergency_one_confirmed_step_at_a_time():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    settle(recovery)

    assert exit_.trigger() is ExitState.ARMED
    # armed but no step taken yet: valve still in emergency
    assert valve.emergency is True

    first = exit_.advance()
    assert first is ExitState.STEPPING
    assert exit_.procedure.index == 1
    assert exit_.procedure.current_step().name == "throttle"

    second = exit_.advance()
    assert second is ExitState.STEPPING
    assert exit_.procedure.index == 2

    third = exit_.advance()
    assert third is ExitState.COMPLETED
    assert valve.emergency is False
    assert valve.open is True


def test_step_ticks_advance_one_at_a_time_and_cannot_be_skipped():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    settle(recovery)
    start = clock.tick
    exit_.trigger()
    exit_.advance()
    assert clock.tick == start + 1
    exit_.advance()
    assert clock.tick == start + 2
    exit_.advance()
    assert clock.tick == start + 3


def test_advance_before_trigger_is_refused():
    exit_, recovery, valve, *_ = build(WaterType.SEAWATER)
    with pytest.raises(ExitNotArmedError):
        exit_.advance()


# ----------------------------------------------------- brackish one-shot exit


def test_brackish_releases_in_one_move_after_condition_confirmation():
    exit_, recovery, valve, recorder, clock = build(WaterType.BRACKISH)
    alarmed(exit_, recovery)
    settle(recovery)

    assert exit_.trigger() is ExitState.COMPLETED
    assert valve.emergency is False
    assert exit_.procedure.steps[0].name == "crack"
    assert len(exit_.procedure.steps) == 1


# --------------------------------------------------------------- rollback ---


def test_deterioration_midway_rolls_the_whole_segment_back():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    settle(recovery)
    exit_.trigger()
    exit_.advance()  # crack confirmed

    # water turns bad again before the throttle step
    recovery.raise_alarm()
    state = exit_.advance()
    assert state is ExitState.ABORTED
    assert valve.emergency is True  # whole segment rolled back, never half-open
    assert valve.open is False
    assert exit_.procedure.failed_step == "throttle"


def test_rollback_is_sticky_until_an_explicit_retry():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    settle(recovery)
    exit_.trigger()
    exit_.advance()
    recovery.raise_alarm()
    exit_.advance()  # rolls back

    # water healthy again; plain duplicate trigger must not auto-restart
    settle(recovery)
    assert exit_.trigger() is ExitState.ABORTED
    assert valve.emergency is True

    assert exit_.retry() is ExitState.ARMED
    assert exit_.advance() is ExitState.STEPPING
    assert exit_.advance() is ExitState.STEPPING
    assert exit_.advance() is ExitState.COMPLETED
    assert valve.emergency is False


def test_retry_is_refused_while_the_condition_is_still_bad():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    settle(recovery)
    exit_.trigger()
    recovery.raise_alarm()
    exit_.advance()  # rolls back at crack
    assert exit_.retry() is ExitState.ABORTED
    assert valve.emergency is True


# --------------------------------------------------------------- idempotency


def test_duplicate_triggers_for_one_alarm_release_emergency_once():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    settle(recovery)

    first = exit_.trigger()
    second = exit_.trigger()  # concurrent duplicate while armed
    assert first is second is ExitState.ARMED
    enter_rows = [e for e in recorder.events() if e.kind == ENTER_KIND]
    assert len(enter_rows) == 1

    exit_.advance()
    exit_.trigger()  # duplicate while stepping must not restart at step zero
    assert exit_.procedure.index == 1


def test_trigger_during_stepping_never_re_arms_a_second_release():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    settle(recovery)
    exit_.trigger()
    exit_.advance()  # crack confirmed, now stepping toward throttle

    # a concurrent duplicate trigger mid-segment must not begin a new procedure
    assert exit_.trigger() is ExitState.STEPPING
    assert exit_.procedure.index == 1
    enter_rows = [e for e in recorder.events() if e.kind == ENTER_KIND]
    assert len(enter_rows) == 1


def test_completed_release_ignores_further_triggers_until_a_new_alarm():
    exit_, recovery, valve, recorder, clock = build(WaterType.BRACKISH)
    alarmed(exit_, recovery)
    settle(recovery)
    exit_.trigger()
    assert exit_.trigger() is ExitState.COMPLETED
    assert valve.emergency is False

    # alarm raises again: a fresh emergency may be released once
    recovery.raise_alarm()
    assert exit_.trip() is True
    assert valve.emergency is True
    settle(recovery)
    assert exit_.trigger() is ExitState.COMPLETED


def test_trip_is_idempotent_while_the_same_alarm_persists():
    exit_, recovery, valve, recorder, clock = build(WaterType.BRACKISH)
    recovery.raise_alarm()
    assert exit_.trip() is True
    assert exit_.trip() is False
    trip_rows = [e for e in recorder.events() if e.kind == "recovery_alarm_trip"]
    assert len(trip_rows) == 1


# ------------------------------------------------------------------- audit ---


def test_every_step_records_entry_condition_reading_and_conclusion():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    settle(recovery, rate=40.0)
    exit_.trigger()
    exit_.advance()
    exit_.advance()
    exit_.advance()

    kinds = [e.kind for e in recorder.events()]
    assert ENTER_KIND in kinds
    assert kinds.count(STEP_KIND) == 2
    assert COMPLETE_KIND in kinds

    enter = recorder.latest(ENTER_KIND)
    assert "recovery=40.0" in enter.detail
    assert "band=[30.0,55.0] inside" in enter.detail
    assert "latch=down" in enter.detail
    assert "stepped exit armed" in enter.detail

    complete = recorder.latest(COMPLETE_KIND)
    assert "step open confirmed" in complete.detail
    assert "emergency released" in complete.detail


def test_rollback_records_the_failed_step_basis_and_conclusion():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    settle(recovery)
    exit_.trigger()
    exit_.advance()
    recovery.raise_alarm()
    exit_.advance()

    abort = recorder.latest(ABORT_KIND)
    assert "partial failure at step throttle" in abort.detail
    assert "rolled back to emergency" in abort.detail
    assert "latch=raised" in abort.detail


def test_refused_trigger_records_the_reading_it_was_based_on():
    exit_, recovery, valve, recorder, clock = build(WaterType.SEAWATER)
    alarmed(exit_, recovery)
    recovery.clear_alarm()
    recovery.update_rate(58.0)
    exit_.trigger()
    refusal = recorder.latest("recovery_exit_trigger")
    assert "exit refused" in refusal.detail
    assert "recovery=58.0" in refusal.detail
    assert "outside" in refusal.detail


# --------------------------------------------------------- console / wiring


def _console(water):
    from desalplant.config import Config
    from desalplant.console.plant import Plant
    from desalplant.console.router import Console
    import tempfile

    store = Store(str(tempfile.mkdtemp()) + "/plant.json")
    clock = LogicalClock()
    recorder = Recorder(store, clock)
    plant = Plant(store, clock, recorder, water=water)
    return Console(plant), plant, recorder


def test_console_seawater_exit_arms_then_opens_one_step_per_post():
    console, plant, recorder = _console(WaterType.SEAWATER)
    plant.trip_recovery_alarm(rate=60.0)
    assert plant.valve.emergency is True

    import json
    from desalplant.console.http import Request

    def post(path, body=None):
        text = "" if body is None else json.dumps(body)
        return console.handle(Request.parse("POST", path, text))

    plant.recovery.clear_alarm()
    plant.recovery.update_rate(42.0)
    armed = post("/api/ops/recovery/exit").body
    assert armed["status"] == "armed"
    assert armed["mode"] == "stepped"
    assert armed["valve_emergency"] is True

    # crack, throttle then open: one confirmed step per post
    assert post("/api/ops/recovery/exit").body["status"] == "stepping"
    assert post("/api/ops/recovery/exit").body["status"] == "stepping"
    done = post("/api/ops/recovery/exit").body
    assert done["status"] == "completed"
    assert done["valve_emergency"] is False
    # further posts after completion do not move anything
    assert post("/api/ops/recovery/exit").body["status"] == "completed"


def test_console_brackish_exit_completes_in_one_post():
    console, plant, recorder = _console(WaterType.BRACKISH)
    plant.trip_recovery_alarm(rate=60.0)
    plant.recovery.clear_alarm()
    plant.recovery.update_rate(42.0)

    import json
    from desalplant.console.http import Request

    text = json.dumps({})
    body = console.handle(Request.parse("POST", "/api/ops/recovery/exit", text)).body
    assert body["status"] == "completed"
    assert body["mode"] == "one_shot"
    assert plant.valve.emergency is False


def test_console_duplicate_exit_posts_are_idempotent():
    console, plant, recorder = _console(WaterType.SEAWATER)
    plant.trip_recovery_alarm(rate=60.0)
    plant.recovery.clear_alarm()
    plant.recovery.update_rate(42.0)

    import json
    from desalplant.console.http import Request

    def exit_once():
        return console.handle(Request.parse("POST", "/api/ops/recovery/exit", json.dumps({}))).body

    first = exit_once()
    assert first["status"] == "armed"
    # concurrent duplicate triggers must both observe the single armed release
    duplicate = plant.emergency_exit.trigger()
    assert duplicate.value == "armed"
    enters = [e for e in recorder.events() if e.kind == ENTER_KIND]
    assert len(enters) == 1
