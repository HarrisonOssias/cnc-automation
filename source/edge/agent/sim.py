"""Demo machine simulator: stands in for the HAAS control running the current part session.

Off by default. When running, it loops: cycle start -> cut for the part's cycle time (with jitter)
-> cycle stop (one part made) -> load/unload -> next cycle. It stops by itself when the job's
quantity is made, on an alarm, or when the session ends: the next job needs its setup first. Feed hold pauses the cycle in progress.

`speed` compresses time for demos: at 20x a 240 s cycle takes 12 s on the wall clock, and is still
reported as 240 s of machine time, so cycle times stay believable in the office's history.

Every step goes through the Agent's control methods (cycle_start, cycle_stop, ...), the same ones
the CLI uses, so the simulator produces exactly the events a real control would.
"""

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

from forge_core import SimStatus

if TYPE_CHECKING:
    from agent.agent import Agent

log = logging.getLogger("agent.sim")

DEFAULT_CYCLE_S = 120  # parts without a cycle time on record
LOAD_UNLOAD_S = 20  # machine time between cycles
JITTER = 0.15
ALARMS = {"1010": "Door open", "108": "Servo overload", "1068": "Low coolant"}


class MachineSim:
    def __init__(self, agent: "Agent", rng: random.Random | None = None):
        self.agent = agent
        self.rng = rng or random.Random()
        self.running = False
        self.speed = 1.0
        self._task: asyncio.Task | None = None
        self._changed = asyncio.Event()  # hold, resume, alarm, stop, "finish now"
        self._finish_now = False
        self._session_id: str | None = None  # the session this run is for
        agent.sim = self

    def status(self) -> SimStatus:
        return SimStatus(running=self.running, speed=self.speed)

    # Controls (kiosk demo drawer and CLI)
    def run(self, speed: float = 1.0) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        session = self.agent.store.open_session()
        if session is None:
            raise ValueError("no job on the machine")
        if self.agent.alarm:
            raise ValueError(f"the machine is in alarm ({self.agent.alarm}); clear it first")
        self.speed = speed
        self._session_id = session.session_id
        self.running = True
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._loop())
        self._poke()

    def stop(self) -> None:
        """Stop after the current part (like an optional stop at the end of the program)."""
        self.running = False
        self._poke()

    def finish_part(self) -> int:
        """Finish one part now: ends the running cycle, or runs a whole cycle instantly.

        Either way the part reports a full cycle time, as if the machine had cut it, so a demo
        button press doesn't show up as a short outlier in the history.
        """
        self._check_room_for_a_part()
        if self.agent.cycle_started_at:
            if self._task and not self._task.done():
                self._finish_now = True
                self._poke()
                return self.agent.store.open_session().made + 1
            return self.agent.cycle_stop(self._planned_cycle_s())
        self.agent.cycle_start()
        return self.agent.cycle_stop(self._planned_cycle_s())

    def hold(self) -> None:
        self.agent.feed_hold()
        self._poke()

    def resume(self) -> None:
        self.agent.resume()
        self._poke()

    def alarm(self, code: str) -> None:
        self.running = False
        self.agent.raise_alarm(code, ALARMS.get(code, "Alarm"))
        self._poke()

    def clear_alarm(self) -> None:
        self.agent.clear_alarm()
        self._poke()

    # The machine
    async def _loop(self) -> None:
        try:
            while self.running and self._ready_for_a_cycle():
                await self._one_cycle()
                if self.running and self._ready_for_a_cycle():
                    await asyncio.sleep(LOAD_UNLOAD_S / self.speed)
        except Exception:
            log.exception("simulator stopped")
        finally:
            self.running = False
            self.agent.broadcast()

    async def _one_cycle(self) -> None:
        planned = self._planned_cycle_s()
        self.agent.cycle_start()
        self._cycle_wall_start = time.monotonic()
        remaining = planned / self.speed  # wall seconds left to cut
        while remaining > 0:
            if self.agent.alarm or not self.agent.cycle_started_at:  # alarm, or the session ended
                return
            self._changed.clear()
            if self.agent.hold_since:  # feed hold: wait for resume (or an alarm)
                await self._changed.wait()
                continue
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except TimeoutError:
                break
            remaining -= time.monotonic() - t0
            if self._finish_now:
                self._finish_now = False
                break
        if self.agent.cycle_started_at and not self.agent.alarm:
            self.agent.cycle_stop(planned)

    def _ready_for_a_cycle(self) -> bool:
        session = self.agent.store.open_session()
        job = self.agent.current_job()
        return bool(
            session and job and session.session_id == self._session_id
            and not self.agent.alarm and session.made < job.quantity
        )

    def _check_room_for_a_part(self) -> None:
        session, job = self.agent.store.open_session(), self.agent.current_job()
        if session is None or job is None:
            raise ValueError("no job on the machine")
        if session.made >= job.quantity:
            raise ValueError(f"all {job.quantity} are made: tap Job complete")

    def _planned_cycle_s(self) -> float:
        job = self.agent.current_job()
        expected = (job.cycle_s if job else None) or DEFAULT_CYCLE_S
        return round(expected * self.rng.uniform(1 - JITTER, 1 + JITTER), 1)

    def poke(self) -> None:
        """Wake the loop; the agent calls this when a cycle ends underneath it (session closed)."""
        self._changed.set()

    def _poke(self) -> None:
        self._changed.set()
