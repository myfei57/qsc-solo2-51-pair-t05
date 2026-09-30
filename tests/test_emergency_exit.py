"""Sequenced exit from the brine valve emergency position.

Covers the exit-mode matrix, gate confirmation, gradual opening, rollback on
deterioration, idempotency per alarm and the audit trail.
"""

import pytest

from desalplant.audit.recorder import Recorder
from desalplant.brine.exit import (
    EmergencyExit,
    ExitGate,
    ExitMode,
    ExitPlan,
    ExitReading,
    ExitState,
    ExitStep,
    WaterQuality,
    default_plan,
)
from desalplant.brine.valve import Valve
from desalplant.clock import LogicalClock
from desalplant.ro.recovery import Recovery
from desalplant.store.store import Store


class FakeReader:
    """Controllable reading source for the exit tests."""

    def __init__(self) -> None:
        self.reading = ExitReading(
            recovery_rate=35.0, conductivity=200.0, alarm_active=False
        )

    def __call__(self) -> ExitReading:
        return self.reading


@pytest.fixture()
def clock():
    return LogicalClock()


@pytest.fixture()
def recorder(clock):
    return Recorder(Store(), clock)


@pytest.fixture()
def valve():
    return Valve()


@pytest.fixture()
def recovery():
    return Recovery()


def make_exit(valve, recovery, recorder, clock, water, reader):
    return EmergencyExit(
        valve=valve,
        recovery=recovery,
        recorder=recorder,
        clock=clock,
        plan=default_plan(water),
        reader=reader,
    )


# ------------------------------------------------------- water type fixes mode


def test_water_types_fix_their_exit_mode():
    assert WaterQuality.SEAWATER.exit_mode() is ExitMode.STEPPED
    assert WaterQuality.BRACKISH.exit_mode() is ExitMode.STEPPED
    assert WaterQuality.FRESH.exit_mode() is ExitMode.SEGMENT


def test_default_plans_match_their_mode():
    stepped = default_plan(WaterQuality.SEAWATER)
    segment = default_plan(WaterQuality.FRESH)
    assert stepped.mode is ExitMode.STEPPED
    assert segment.mode is ExitMode.SEGMENT
    assert len(stepped.steps) > len(segment.steps)


# ------------------------------------------------------------------ gate logic


def test_gate_passes_when_every_reading_sits_in_band():
    gate = ExitGate(recovery_max=40.0, conductivity_max=250.0)
    verdict = gate.evaluate(ExitReading(35.0, 200.0, False))
    assert verdict.passed is True


def test_gate_fails_while_the_alarm_is_still_raised():
    gate = ExitGate(recovery_max=40.0, conductivity_max=250.0)
    verdict = gate.evaluate(ExitReading(35.0, 200.0, True))
    assert verdict.passed is False
    assert "alarm" in verdict.reason


def test_gate_fails_when_recovery_is_too_high():
    gate = ExitGate(recovery_max=40.0, conductivity_max=250.0)
    verdict = gate.evaluate(ExitReading(45.0, 200.0, False))
    assert verdict.passed is False
    assert "recovery" in verdict.reason


def test_gate_fails_when_conductivity_is_too_high():
    gate = ExitGate(recovery_max=40.0, conductivity_max=250.0)
    verdict = gate.evaluate(ExitReading(35.0, 300.0, False))
    assert verdict.passed is False
    assert "conductivity" in verdict.reason


# ------------------------------------------------------------- plan validation


def test_plan_rejects_an_empty_step_list():
    with pytest.raises(ValueError):
        ExitPlan(WaterQuality.FRESH, ())


def test_plan_rejects_a_non_positive_dwell():
    with pytest.raises(ValueError):
        ExitPlan(
            WaterQuality.FRESH,
            (ExitStep("bad", 0, 0.5, ExitGate(40.0, 250.0)),),
        )


def test_plan_rejects_an_opening_outside_the_unit_interval():
    with pytest.raises(ValueError):
        ExitPlan(
            WaterQuality.FRESH,
            (ExitStep("bad", 2, 1.5, ExitGate(40.0, 250.0)),),
        )


# --------------------------------------------------------- stepped exit flow


def test_stepped_exit_confirms_each_step_and_opens_gradually(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.SEAWATER, reader)
    recovery.raise_alarm()
    valve.set_emergency()

    assert exit.trigger(recovery.alarm_id) is True
    assert exit.state is ExitState.RUNNING
    assert valve.emergency is True  # still in emergency until the first step opens

    # dwell through the first step (crack, dwell=3)
    assert exit.advance() is ExitState.RUNNING
    assert valve.opening == 0.0  # dwell not finished
    assert exit.advance() is ExitState.RUNNING
    assert valve.opening == 0.0
    assert exit.advance() is ExitState.RUNNING
    assert valve.opening == pytest.approx(0.20)  # crack opened
    assert valve.emergency is False

    # the exit is not done yet
    assert exit.state is ExitState.RUNNING
    assert exit.step_index == 1


def test_stepped_exit_runs_to_completion(valve, recovery, recorder, clock):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.SEAWATER, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)

    for _ in range(5 * 3):  # five steps, three ticks each
        exit.advance()

    assert exit.state is ExitState.DONE
    assert valve.opening == pytest.approx(1.0)
    assert valve.emergency is False


def test_stepped_exit_holds_the_dwell_when_the_gate_fails(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.SEAWATER, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)

    # condition deteriorates: the alarm is still raised
    reader.reading = ExitReading(35.0, 200.0, True)
    assert exit.advance() is ExitState.ROLLED_BACK
    assert valve.emergency is True
    assert valve.opening == 0.0


# --------------------------------------------------------- segment exit flow


def test_segment_exit_confirms_once_then_runs_in_one_breath(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.FRESH, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)

    # first advance confirms the entry gate
    assert exit.advance() is ExitState.RUNNING
    assert valve.opening == 0.0  # dwell not finished
    # remaining dwell of the first step (prime, dwell=2)
    assert exit.advance() is ExitState.RUNNING
    assert valve.opening == pytest.approx(0.30)

    # the segment runs on dwell timers without re-confirmation
    for _ in range(2 * 2):  # two more steps, two ticks each
        exit.advance()

    assert exit.state is ExitState.DONE
    assert valve.opening == pytest.approx(1.0)


def test_segment_exit_rolls_back_when_the_alarm_re_raises(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.FRESH, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)

    # confirm the entry gate
    assert exit.advance() is ExitState.RUNNING
    # alarm comes back mid-segment
    reader.reading = ExitReading(35.0, 200.0, True)
    assert exit.advance() is ExitState.ROLLED_BACK
    assert valve.emergency is True


def test_segment_exit_rolls_back_when_the_entry_gate_fails(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.FRESH, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)

    reader.reading = ExitReading(35.0, 200.0, True)
    assert exit.advance() is ExitState.ROLLED_BACK
    assert valve.emergency is True


# ------------------------------------------------------------- rollback scope


def test_rollback_mid_segment_returns_the_valve_to_emergency(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.SEAWATER, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)

    # open the first step
    for _ in range(3):
        exit.advance()
    assert valve.opening == pytest.approx(0.20)
    assert valve.emergency is False

    # condition deteriorates at the second step
    reader.reading = ExitReading(50.0, 200.0, False)
    assert exit.advance() is ExitState.ROLLED_BACK
    assert valve.emergency is True
    assert valve.opening == 0.0


def test_a_rolled_back_exit_does_not_resume_on_advance(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.SEAWATER, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)

    reader.reading = ExitReading(35.0, 200.0, True)
    assert exit.advance() is ExitState.ROLLED_BACK
    # further advances are no-ops
    assert exit.advance() is ExitState.ROLLED_BACK
    assert exit.state is ExitState.ROLLED_BACK


# --------------------------------------------------------------- idempotency


def test_duplicate_trigger_for_the_same_alarm_is_a_no_op(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.SEAWATER, reader)
    recovery.raise_alarm()
    valve.set_emergency()

    assert exit.trigger(recovery.alarm_id) is True
    # a concurrent duplicate trigger for the same alarm does not re-exit
    assert exit.trigger(recovery.alarm_id) is False
    assert exit.state is ExitState.RUNNING


def test_trigger_for_a_new_alarm_starts_a_fresh_exit(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.SEAWATER, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)
    for _ in range(5 * 3):
        exit.advance()
    assert exit.state is ExitState.DONE

    # a new alarm event (different id) starts a fresh exit
    recovery.raise_alarm()
    valve.set_emergency()
    assert exit.trigger(recovery.alarm_id) is True
    assert exit.state is ExitState.RUNNING


def test_trigger_after_completion_for_the_same_alarm_is_a_no_op(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.SEAWATER, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)
    for _ in range(5 * 3):
        exit.advance()
    assert exit.state is ExitState.DONE
    assert exit.trigger(recovery.alarm_id) is False


# ---------------------------------------------------------------- audit trail


def test_every_step_is_audit_logged_with_entry_reading_and_verdict(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.FRESH, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)
    for _ in range(3 * 2):
        exit.advance()

    kinds = [event.kind for event in recorder.events()]
    # newest-first; the exit must have entered, verdicted, opened and completed
    assert "exit_step_enter" in kinds
    assert "exit_step_verdict" in kinds
    assert "exit_step_open" in kinds
    assert "exit_complete" in kinds

    # the verdict carries the basis readings and the conclusion
    verdict_events = [e for e in recorder.events() if e.kind == "exit_step_verdict"]
    assert verdict_events
    detail = verdict_events[-1].detail  # oldest verdict
    assert "recovery=" in detail
    assert "conductivity=" in detail
    assert "alarm_active=" in detail
    assert "verdict=pass" in detail


def test_rollback_is_audit_logged_with_the_reason(valve, recovery, recorder, clock):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.SEAWATER, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)

    reader.reading = ExitReading(35.0, 200.0, True)
    exit.advance()

    rollbacks = [e for e in recorder.events() if e.kind == "exit_rollback"]
    assert len(rollbacks) == 1
    assert "emergency" in rollbacks[0].detail
    assert "alarm" in rollbacks[0].detail


def test_audit_events_are_stamped_with_the_clock_tick(
    valve, recovery, recorder, clock
):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.FRESH, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)
    clock.advance(5)
    exit.advance()

    events = recorder.events()
    assert all(event.tick >= 0 for event in events)
    assert any(event.tick == 5 for event in events)


# ------------------------------------------------------------- timing cannot skip


def test_the_dwell_cannot_be_skipped(valve, recovery, recorder, clock):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.FRESH, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)

    # prime step has dwell=2; one advance is not enough to open
    exit.advance()
    assert valve.opening == 0.0
    assert exit.step_index == 0
    # the second advance finishes the dwell and opens
    exit.advance()
    assert valve.opening == pytest.approx(0.30)
    assert exit.step_index == 1


def test_advance_before_trigger_is_a_no_op(valve, recovery, recorder, clock):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.FRESH, reader)
    assert exit.advance() is ExitState.IDLE
    assert valve.opening == 0.0


def test_trigger_records_the_water_type_and_mode(valve, recovery, recorder, clock):
    reader = FakeReader()
    exit = make_exit(valve, recovery, recorder, clock, WaterQuality.FRESH, reader)
    recovery.raise_alarm()
    valve.set_emergency()
    exit.trigger(recovery.alarm_id)
    for _ in range(3 * 2):
        exit.advance()

    complete = recorder.latest("exit_complete")
    assert complete is not None
    assert "fresh" in complete.detail
    assert "segment" in complete.detail


# ------------------------------------------------------------- console wiring


def test_console_trigger_and_advance_run_the_exit(console, plant):
    from support import call, ok

    # put the line into the emergency state, then let the water quality recover
    plant.recovery.raise_alarm()
    plant.valve.set_emergency()
    assert plant.valve.emergency is True
    plant.recovery.clear_alarm()

    body = ok(console, "POST", "/api/exit/trigger", {})
    assert body["started"] is True
    assert body["state"] == "running"

    # advance through the whole segment (fresh water = 3 steps, 2 ticks each)
    for _ in range(3 * 2):
        body = ok(console, "POST", "/api/exit/advance", {})

    assert body["state"] == "done"
    assert body["valve_emergency"] is False
    assert body["valve_opening"] == pytest.approx(1.0)


def test_console_trigger_is_idempotent_for_the_same_alarm(console, plant):
    from support import ok

    plant.recovery.raise_alarm()
    plant.valve.set_emergency()
    plant.recovery.clear_alarm()

    first = ok(console, "POST", "/api/exit/trigger", {})
    assert first["started"] is True
    second = ok(console, "POST", "/api/exit/trigger", {})
    assert second["started"] is False


def test_console_reports_the_water_quality_and_exit_state(console, plant):
    from support import ok

    body = ok(console, "GET", "/api/status")
    assert body["water_quality"] == "fresh"
    assert body["emergency_exit_state"] == "idle"

    plant.recovery.raise_alarm()
    plant.valve.set_emergency()
    plant.recovery.clear_alarm()
    ok(console, "POST", "/api/exit/trigger", {})
    body = ok(console, "GET", "/api/status")
    assert body["emergency_exit_state"] == "running"
