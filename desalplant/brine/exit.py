"""Automatic, auditable release of the brine valve emergency position.

When the recovery alarm raises, the brine valve is driven to its emergency
position. Release is no longer a manual one-shot: once the alarm condition has
genuinely cleared, the valve leaves the emergency position through a fixed
program.

Rules enforced here:

* ``trip`` drives the valve to emergency and is idempotent for the same alarm;
* the alarm condition (latch down *and* recovery back inside its band) is
  re-confirmed before the program starts and again before every step;
* the stepped program runs one step at a time, each confirmed from a fresh
  reading, so no step timing can be skipped;
* a failure on any later step is a partial failure: the whole segment rolls
  back to the emergency position rather than stopping half-open;
* repeated triggers for the same emergency are idempotent, the release runs at
  most once per emergency;
* every step's entry condition, the reading it was based on and the conclusion
  are appended to the audit record;
* whether release is stepped or one-shot is fixed by feedwater type, see
  :mod:`desalplant.brine.water_quality`.
"""

from dataclasses import dataclass
from enum import Enum

from ..audit.recorder import Recorder
from ..clock import LogicalClock
from ..errors import ExitNotArmedError, ExitStepFailedError
from ..ro.recovery import Recovery
from .limit import RecoveryLimit
from .valve import Valve
from .water_quality import ExitMode, WaterType, exit_mode

STEP_KIND = "recovery_exit_step"
ENTER_KIND = "recovery_exit_enter"
COMPLETE_KIND = "recovery_exit_complete"
ABORT_KIND = "recovery_exit_abort"
TRIGGER_KIND = "recovery_exit_trigger"
TRIP_KIND = "recovery_alarm_trip"


class ExitState(str, Enum):
    """Lifecycle of one emergency release."""

    EMERGENCY = "emergency"
    ARMED = "armed"
    STEPPING = "stepping"
    COMPLETED = "completed"
    ABORTED = "aborted"


@dataclass(frozen=True)
class RecoveryReading:
    """The observations an exit decision is based on."""

    rate: float
    latch_active: bool
    tick: int

    def condition_cleared(self, limit: RecoveryLimit) -> bool:
        """Whether the alarm condition has genuinely cleared.

        The latch has to be down and the measured recovery has to be back
        inside the admissible band; clearing the latch alone is not enough.
        """
        return (not self.latch_active) and limit.contains(self.rate)

    def basis(self, limit: RecoveryLimit) -> str:
        """Human-readable reading used in audit rows."""
        inside = "inside" if limit.contains(self.rate) else "outside"
        latch = "raised" if self.latch_active else "down"
        return (
            f"recovery={self.rate} band=[{limit.min},{limit.max}] {inside}, "
            f"latch={latch}, tick={self.tick}"
        )


@dataclass(frozen=True)
class ExitStep:
    """One ordered opening move toward the normal valve position."""

    name: str


STANDARD_STEPS: tuple[ExitStep, ...] = (
    ExitStep("crack"),
    ExitStep("throttle"),
    ExitStep("open"),
)


class ExitProcedure:
    """State machine for a single emergency release.

    The procedure is armed at most once per emergency and, once running, only
    advances one step per :meth:`advance` call from a fresh reading. A failed
    step aborts the whole procedure.
    """

    def __init__(self, valve: Valve, limit: RecoveryLimit, mode: ExitMode) -> None:
        self._valve = valve
        self._limit = limit
        self._mode = mode
        self._steps = STANDARD_STEPS if mode is ExitMode.STEPPED else STANDARD_STEPS[:1]
        self.state = ExitState.EMERGENCY
        self._index = 0
        self.failed_step: str | None = None
        self.failure_reason: str = ""

    @property
    def mode(self) -> ExitMode:
        """Release mode this procedure runs in."""
        return self._mode

    @property
    def index(self) -> int:
        """Number of confirmed steps taken."""
        return self._index

    @property
    def steps(self) -> tuple[ExitStep, ...]:
        """Programmed steps in order."""
        return self._steps

    @property
    def armed(self) -> bool:
        """Whether a release may currently run."""
        return self.state in (ExitState.ARMED, ExitState.STEPPING)

    @property
    def finished(self) -> bool:
        """Whether the valve has fully left the emergency position."""
        return self.state is ExitState.COMPLETED

    @property
    def aborted(self) -> bool:
        """Whether the procedure rolled back to the emergency position."""
        return self.state is ExitState.ABORTED

    def current_step(self) -> ExitStep | None:
        """The step about to run, or ``None`` past the end."""
        if self._index >= len(self._steps):
            return None
        return self._steps[self._index]

    def arm(self) -> None:
        """Move from the emergency position to the armed entry state."""
        if self.state is not ExitState.EMERGENCY:
            raise ExitNotArmedError("exit procedure is not waiting in emergency")
        self.state = ExitState.ARMED

    def start_stepping(self) -> ExitStep:
        """Leave the armed state and return the first step to run."""
        if self.state is not ExitState.ARMED:
            raise ExitNotArmedError("exit procedure is not armed")
        self.state = ExitState.STEPPING
        return self._steps[self._index]

    def advance(self) -> ExitStep:
        """Return the next step to run without skipping past it."""
        if self.state is not ExitState.STEPPING:
            raise ExitNotArmedError("exit procedure is not stepping")
        if self._index >= len(self._steps):
            raise ExitNotArmedError("exit procedure has no step left")
        return self._steps[self._index]

    def commit(self) -> None:
        """Mark the step just confirmed as done."""
        self._index += 1

    def complete(self) -> None:
        """Finish after the final step was confirmed."""
        if self.state is not ExitState.STEPPING or self._index != len(self._steps):
            raise ExitNotArmedError("exit procedure cannot finish off its last step")
        self.state = ExitState.COMPLETED

    def abort(self, step: str, reason: str) -> None:
        """Roll the whole procedure back to the emergency position."""
        self.failed_step = step
        self.failure_reason = reason
        self.state = ExitState.ABORTED
        self._valve.set_emergency()

    def reset(self) -> None:
        """Return to the entry state for a fresh emergency or retry."""
        self.state = ExitState.EMERGENCY
        self._index = 0
        self.failed_step = None
        self.failure_reason = ""


class EmergencyExit:
    """Coordinates the recovery latch, valve and the audited exit procedure.

    One instance covers the running line. Both ``trip`` and ``trigger`` may be
    called any number of times for the same alarm: ``trip`` only acts when the
    latch is freshly raised and ``trigger`` starts at most one release, so
    concurrent duplicate alarms never release the emergency twice.
    """

    def __init__(
        self,
        recovery: Recovery,
        valve: Valve,
        limit: RecoveryLimit,
        water: WaterType = WaterType.BRACKISH,
        recorder: Recorder | None = None,
        clock: LogicalClock | None = None,
    ) -> None:
        self._recovery = recovery
        self._valve = valve
        self._limit = limit
        self._water = water
        self._recorder = recorder
        self._clock = clock
        self.procedure: ExitProcedure | None = None

    @property
    def mode(self) -> ExitMode:
        """Exit mode fixed for the configured feedwater."""
        return exit_mode(self._water)

    @property
    def state(self) -> ExitState:
        """Current procedure state; ``EMERGENCY`` before the first alarm."""
        return self.procedure.state if self.procedure is not None else ExitState.EMERGENCY

    def read(self) -> RecoveryReading:
        """Take a fresh reading of the latch and the recovery rate."""
        tick = self._clock.tick if self._clock is not None else 0
        return RecoveryReading(
            rate=self._recovery.rate(),
            latch_active=self._recovery.active(),
            tick=tick,
        )

    # ------------------------------------------------------------- alarm side

    def trip(self, reading: RecoveryReading | None = None) -> bool:
        """React to a raised recovery alarm: drive the valve to emergency.

        Idempotent while the latch stays raised and the valve is already in its
        emergency position. A fresh raise after a completed release starts a
        new procedure book so the next recovery can be released once.
        """
        if not self._recovery.active():
            return False
        if self.procedure is not None and self.procedure.finished:
            # A new alarm after a finished release: forget the old procedure.
            self.procedure.reset()
            self.procedure = None
        if self._valve.emergency and self.procedure is not None:
            return False
        self._valve.set_emergency()
        self.procedure = ExitProcedure(self._valve, self._limit, self.mode)
        reading = reading or self.read()
        self._log(TRIP_KIND, "recovery alarm tripped, valve to emergency; " + reading.basis(self._limit))
        return True

    # -------------------------------------------------------------- exit side

    def trigger(self) -> ExitState:
        """Attempt to release the emergency position for the current alarm.

        Returns the procedure state immediately after this call: ``ARMED`` when
        a stepped release started, ``COMPLETED`` for a finished one-shot
        release, and the existing state for duplicate triggers of the same
        emergency (including a held rollback).
        """
        procedure = self.procedure
        if procedure is not None:
            if procedure.aborted:
                # A rollback stays latched pending an explicit retry().
                return ExitState.ABORTED
            if procedure.armed or procedure.finished:
                # Same emergency seen again: never start a second release.
                return procedure.state
            if procedure.state is ExitState.STEPPING:
                # A release is already walking its steps; trigger never re-arms.
                return ExitState.STEPPING
            if not self._valve.emergency:
                return procedure.state
        elif not self._valve.emergency:
            # No emergency to release; nothing to do and nothing to log.
            return ExitState.EMERGENCY

        reading = self.read()
        if not reading.condition_cleared(self._limit):
            self._log(TRIGGER_KIND, "exit refused: " + reading.basis(self._limit))
            return ExitState.EMERGENCY

        return self._begin(reading)

    def advance(self) -> ExitState:
        """Run the next step of an armed or in-progress stepped release.

        Each call re-confirms the alarm condition from a fresh reading and
        moves exactly one step. A deteriorated condition or an unconfirmed step
        rolls the entire release back to the emergency position.
        """
        procedure = self._require_procedure()
        if procedure.finished or procedure.aborted:
            return procedure.state

        reading = self.read()
        if not reading.condition_cleared(self._limit):
            return self._rollback(
                procedure,
                procedure.current_step().name if procedure.current_step() else "entry",
                "condition deteriorated before step",
                reading,
            )

        if procedure.state is ExitState.ARMED:
            step = procedure.start_stepping()
        else:
            step = procedure.advance()
        return self._take_step(procedure, step, reading)

    def retry(self) -> ExitState:
        """Attempt a release again after a rollback, once conditions hold."""
        if self.procedure is None or not self.procedure.aborted:
            return self.trigger()
        reading = self.read()
        if not reading.condition_cleared(self._limit):
            self._log(TRIGGER_KIND, "retry refused: " + reading.basis(self._limit))
            return ExitState.ABORTED
        self.procedure.reset()
        return self._begin(reading)

    # ------------------------------------------------------------- internals

    def _begin(self, reading: RecoveryReading) -> ExitState:
        """Verify the cleared condition and arm (or finish) a fresh procedure."""
        if not reading.condition_cleared(self._limit):
            self._log(TRIGGER_KIND, "exit refused: " + reading.basis(self._limit))
            return ExitState.EMERGENCY

        procedure = self.procedure
        if procedure is None or procedure.state is not ExitState.EMERGENCY:
            procedure = ExitProcedure(self._valve, self._limit, self.mode)
            self.procedure = procedure
        self._log(
            ENTER_KIND,
            f"alarm condition confirmed clear, {self.mode.value} exit armed; "
            + reading.basis(self._limit),
        )
        procedure.arm()

        if self.mode is ExitMode.ONE_SHOT:
            return self._take_step(procedure, procedure.start_stepping(), reading)
        return ExitState.ARMED

    def _take_step(
        self,
        procedure: ExitProcedure,
        step: ExitStep,
        reading: RecoveryReading,
    ) -> ExitState:
        """Execute one step, confirm it, and either complete or keep stepping."""
        self._tick()
        try:
            self._valve.release_emergency()
            confirmed = self._confirm_step(step)
        except Exception as exc:  # actuator refused to move
            self._rollback(procedure, step.name, f"actuator failed: {exc}", reading)
            raise ExitStepFailedError(f"exit step {step.name} failed: {exc}") from exc

        if not confirmed:
            return self._rollback(procedure, step.name, "step not confirmed at the valve", reading)

        procedure.commit()
        if procedure.index == len(procedure.steps):
            procedure.complete()
            if self.mode is ExitMode.ONE_SHOT:
                conclusion = "one-shot move confirmed, emergency released in a single segment; "
            else:
                conclusion = "step " + step.name + " confirmed, emergency released; "
            self._log(COMPLETE_KIND, conclusion + reading.basis(self._limit))
            return ExitState.COMPLETED

        self._log(
            STEP_KIND,
            "step " + step.name + " confirmed, holding for re-check; "
            + reading.basis(self._limit),
        )
        return ExitState.STEPPING

    def _confirm_step(self, step: ExitStep) -> bool:
        """Whether the valve really responded to the step command."""
        if self._valve.emergency:
            return False
        if step.name == "open":
            self._valve.open_valve()
        return not self._valve.emergency

    def _rollback(
        self,
        procedure: ExitProcedure,
        step: str,
        reason: str,
        reading: RecoveryReading,
    ) -> ExitState:
        """Roll the whole segment back to the emergency position."""
        procedure.abort(step, reason)
        self._log(
            ABORT_KIND,
            f"partial failure at step {step}: {reason}; rolled back to emergency; "
            + reading.basis(self._limit),
        )
        return ExitState.ABORTED

    def _require_procedure(self) -> ExitProcedure:
        if self.procedure is None:
            raise ExitNotArmedError("no emergency exit has been armed")
        return self.procedure

    def _tick(self) -> None:
        if self._clock is not None:
            self._clock.advance()

    def _log(self, kind: str, detail: str) -> None:
        if self._recorder is not None:
            self._recorder.log(kind, detail)
