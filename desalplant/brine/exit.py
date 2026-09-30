"""Sequenced exit from the brine valve emergency position.

When the recovery rate alarms, the brine valve is driven to its emergency
position.  Restoring the line used to take a manual click.  This module runs
the exit as a sequenced program: the alarm condition is confirmed cleared
before the valve is opened in stages, every stage is audit-logged with its
entry condition, the readings it saw and the verdict it reached, and a
deteriorating condition rolls the whole segment back to the emergency
position rather than leaving the valve half-open.

The exit strategy is fixed per feed water type: some waters are exited in
confirmed steps, others are run as a single segment in one breath.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from ..audit.recorder import Recorder
from ..clock import LogicalClock
from ..ro.recovery import Recovery
from .valve import Valve


class ExitMode(str, Enum):
    """How the emergency exit is sequenced."""

    STEPPED = "stepped"  # 逐步确认再走下一步
    SEGMENT = "segment"  # 并成一段一口气退完


class WaterQuality(str, Enum):
    """Feed water type; each fixes its own exit strategy."""

    SEAWATER = "seawater"
    BRACKISH = "brackish"
    FRESH = "fresh"

    def exit_mode(self) -> ExitMode:
        """Fixed exit strategy for this water type."""
        return _WATER_EXIT_MODE[self]


_WATER_EXIT_MODE = {
    WaterQuality.SEAWATER: ExitMode.STEPPED,
    WaterQuality.BRACKISH: ExitMode.STEPPED,
    WaterQuality.FRESH: ExitMode.SEGMENT,
}


class ExitState(str, Enum):
    """Lifecycle of an emergency exit."""

    IDLE = "idle"
    RUNNING = "running"
    ROLLED_BACK = "rolled_back"
    DONE = "done"


@dataclass(frozen=True)
class ExitReading:
    """Readings sampled at an exit step."""

    recovery_rate: float
    conductivity: float
    alarm_active: bool


@dataclass(frozen=True)
class ExitVerdict:
    """Whether a step's entry condition was satisfied."""

    passed: bool
    reason: str


@dataclass(frozen=True)
class ExitGate:
    """Entry condition for an exit step.

    The gate passes only when the recovery latch is down and the process
    readings sit inside their limits.
    """

    recovery_max: float
    conductivity_max: float

    def evaluate(self, reading: ExitReading) -> ExitVerdict:
        """Evaluate the entry condition against a reading."""
        if reading.alarm_active:
            return ExitVerdict(False, "recovery alarm still raised")
        if reading.recovery_rate > self.recovery_max:
            return ExitVerdict(
                False,
                f"recovery {reading.recovery_rate:g} above {self.recovery_max:g}",
            )
        if reading.conductivity > self.conductivity_max:
            return ExitVerdict(
                False,
                f"conductivity {reading.conductivity:g} above {self.conductivity_max:g}",
            )
        return ExitVerdict(True, "condition satisfied")


@dataclass(frozen=True)
class ExitStep:
    """One staged opening of the brine valve."""

    name: str
    dwell: int
    opening: float
    gate: ExitGate


@dataclass(frozen=True)
class ExitPlan:
    """Ordered exit program for a water quality type."""

    water: WaterQuality
    steps: tuple[ExitStep, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("exit plan must have at least one step")
        for step in self.steps:
            if step.dwell < 1:
                raise ValueError("exit step dwell must be positive")
            if not 0.0 <= step.opening <= 1.0:
                raise ValueError("exit step opening must sit between 0 and 1")

    @property
    def mode(self) -> ExitMode:
        """Fixed exit strategy for the plan's water type."""
        return self.water.exit_mode()

    def names(self) -> tuple[str, ...]:
        """Step names in program order."""
        return tuple(step.name for step in self.steps)


Reader = Callable[[], ExitReading]


def default_plan(water: WaterQuality) -> ExitPlan:
    """Build the standard exit program for a water quality type."""
    if water is WaterQuality.FRESH:
        return ExitPlan(
            water,
            (
                ExitStep("prime", 2, 0.30, ExitGate(40.0, 250.0)),
                ExitStep("open", 2, 0.60, ExitGate(40.0, 250.0)),
                ExitStep("full", 2, 1.00, ExitGate(40.0, 250.0)),
            ),
        )
    return ExitPlan(
        water,
        (
            ExitStep("crack", 3, 0.20, ExitGate(40.0, 250.0)),
            ExitStep("quarter", 3, 0.40, ExitGate(40.0, 250.0)),
            ExitStep("half", 3, 0.60, ExitGate(40.0, 250.0)),
            ExitStep("three_quarter", 3, 0.80, ExitGate(40.0, 250.0)),
            ExitStep("full", 3, 1.00, ExitGate(40.0, 250.0)),
        ),
    )


class EmergencyExit:
    """Runs the sequenced exit from the emergency position.

    The exit is idempotent per alarm: a duplicate trigger for an alarm that
    already has an exit running (or already completed) does not start a second
    one.  A failed step rolls the whole segment back to the emergency
    position rather than leaving the valve half-open.
    """

    def __init__(
        self,
        valve: Valve,
        recovery: Recovery,
        recorder: Recorder,
        clock: LogicalClock,
        plan: ExitPlan,
        reader: Reader,
    ) -> None:
        self.valve = valve
        self.recovery = recovery
        self.recorder = recorder
        self.clock = clock
        self.plan = plan
        self.reader = reader
        self._state = ExitState.IDLE
        self._alarm_id = ""
        self._step_index = 0
        self._dwell_left = 0
        self._confirmed = False

    @property
    def state(self) -> ExitState:
        """Current lifecycle state."""
        return self._state

    @property
    def step_index(self) -> int:
        """Index of the step currently being held."""
        return self._step_index

    @property
    def alarm_id(self) -> str:
        """Identifier of the alarm being exited."""
        return self._alarm_id

    def trigger(self, alarm_id: str) -> bool:
        """Begin exiting for ``alarm_id``.

        Returns True when a new exit was started, False for a duplicate that
        is already being exited or was already exited.
        """
        if self._state in (ExitState.RUNNING, ExitState.DONE) and self._alarm_id == alarm_id:
            return False
        self._alarm_id = alarm_id
        self._step_index = 0
        self._confirmed = False
        self._state = ExitState.RUNNING
        self._enter_step(0)
        return True

    def advance(self) -> ExitState:
        """Advance the exit by one tick.

        Returns the resulting state.  A failed gate rolls the whole segment
        back to the emergency position.
        """
        if self._state is not ExitState.RUNNING:
            return self._state

        reading = self.reader()
        step = self.plan.steps[self._step_index]

        if self.plan.mode is ExitMode.STEPPED:
            verdict = step.gate.evaluate(reading)
            self._log_verdict(step, reading, verdict)
            if not verdict.passed:
                self._rollback(step, verdict.reason)
                return self._state
            self._dwell_left -= 1
        else:  # SEGMENT: confirm once, then run the segment in one breath
            if not self._confirmed:
                verdict = step.gate.evaluate(reading)
                self._log_verdict(step, reading, verdict)
                if not verdict.passed:
                    self._rollback(step, verdict.reason)
                    return self._state
                self._confirmed = True
            elif reading.alarm_active:
                reason = "recovery alarm re-raised during the segment"
                self._rollback(step, reason)
                return self._state
            self._dwell_left -= 1

        if self._dwell_left > 0:
            return self._state

        self._open_to(step)
        self._step_index += 1
        if self._step_index >= len(self.plan.steps):
            self._complete()
        else:
            self._enter_step(self._step_index)
        return self._state

    # ------------------------------------------------------------- internals

    def _enter_step(self, index: int) -> None:
        step = self.plan.steps[index]
        self._dwell_left = step.dwell
        self.recorder.log(
            "exit_step_enter",
            f"alarm={self._alarm_id} step={step.name} opening={step.opening:g} "
            f"recovery_max={step.gate.recovery_max:g} "
            f"conductivity_max={step.gate.conductivity_max:g}",
        )

    def _log_verdict(
        self, step: ExitStep, reading: ExitReading, verdict: ExitVerdict
    ) -> None:
        self.recorder.log(
            "exit_step_verdict",
            f"alarm={self._alarm_id} step={step.name} "
            f"recovery={reading.recovery_rate:g} conductivity={reading.conductivity:g} "
            f"alarm_active={reading.alarm_active} "
            f"verdict={'pass' if verdict.passed else 'fail'} reason={verdict.reason}",
        )

    def _open_to(self, step: ExitStep) -> None:
        self.valve.release_emergency()
        self.valve.set_opening(step.opening)
        self.recorder.log(
            "exit_step_open",
            f"alarm={self._alarm_id} step={step.name} opening={step.opening:g}",
        )

    def _rollback(self, step: ExitStep, reason: str) -> None:
        self.valve.set_emergency()
        self.valve.set_opening(0.0)
        self._state = ExitState.ROLLED_BACK
        self.recorder.log(
            "exit_rollback",
            f"alarm={self._alarm_id} step={step.name} reason={reason}; "
            "segment rolled back to emergency",
        )

    def _complete(self) -> None:
        self.valve.release_emergency()
        self.valve.set_opening(1.0)
        self._state = ExitState.DONE
        self.recorder.log(
            "exit_complete",
            f"alarm={self._alarm_id} water={self.plan.water.value} "
            f"mode={self.plan.mode.value}",
        )
