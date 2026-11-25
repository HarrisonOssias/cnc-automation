"""Data shapes shared across the wire.

`Bundle` is what the backend sends a Pi (its jobs and config).
`PageState` is what the Pi agent pushes to the kiosk.
The kiosk mirrors these in source/edge/kiosk/src/types.ts.
"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel


class Job(BaseModel):
    id: int
    order_number: str
    line_number: int
    part_number: str
    quantity: int
    material: str
    machinist: str
    due_date: date
    required_tools: list[int]
    # The part's picture, if one was uploaded. The Pi caches it by sha256 for the current and next job.
    image_file_id: int | None = None
    image_sha256: str | None = None
    cycle_s: int | None = None  # expected cycle time for one part, from the CAM program


class Bundle(BaseModel):
    """Everything a Pi needs to render its page without the backend."""

    machine_id: str
    version: int
    core_tools: dict[int, int]  # index -> tool that must live there
    measured_tools: list[int]
    jobs: list[Job]  # queue order; the first is the active job


class SlotState(StrEnum):
    IN_USE = "in_use"  # green: holds a tool the active job needs
    IDLE = "idle"  # blue: holds a tool the job doesn't need
    EMPTY = "empty"  # purple
    CORE_OK = "core_ok"  # grey: core index holds its tool
    CORE_WRONG = "core_wrong"  # red: core index is missing its tool or has the wrong one


class Slot(BaseModel):
    index: int  # 1-based, as printed on the machine
    tool: int | None  # None when empty
    core: bool
    state: SlotState
    measured: bool
    in_use: bool
    swap_to: int | None = None  # tool the operator should load here


class SyncStatus(BaseModel):
    online: bool
    offline_since: datetime | None = None
    bundle_fetched_at: datetime | None = None
    pending_uploads: int = 0


class MachineState(StrEnum):
    SETUP = "setup"  # session open, no cycle run yet: tool swaps, probing, first-article
    CUTTING = "cutting"  # a cycle is running
    IDLE = "idle"  # between cycles (load/unload), or no job
    FEED_HOLD = "feed_hold"  # the operator paused the running cycle
    ALARM = "alarm"


class Session(BaseModel):
    """A part session: one job's time on this machine, from setup to Job complete (or cancel).

    The Pi opens it when the job becomes the current job and stamps its id on every machine event.
    """

    id: str
    job_id: int
    opened_at: datetime
    first_cycle_at: datetime | None = None
    made: int
    quantity: int
    last_cycle_s: float | None = None  # machine time; scaled by the simulator's speed in demos
    avg_cycle_s: float | None = None
    expected_cycle_s: int | None = None
    cycle_started_at: datetime | None = None  # set while a cycle runs (the kiosk animates from it)
    state: MachineState
    alarm: str | None = None  # "1010 Door open" while in alarm


class Alert(BaseModel):
    """A progress alert for the kiosk to pop up (part done, quantity reached, alarm, session closed)."""

    id: int
    at: datetime
    level: str  # info | done | alarm
    text: str


class SimStatus(BaseModel):
    """The demo machine simulator (only shown in the kiosk's ?demo=1 drawer)."""

    running: bool
    speed: float


class PageState(BaseModel):
    machine_id: str
    slots: list[Slot]
    job: Job | None
    up_next: list[Job]
    unplaced_tools: list[int]  # needed tools with no slot to go in
    sync: SyncStatus | None = None
    cached_images: list[str] = []  # sha256s the Pi can serve from /api/images/<sha256>, offline too
    session: Session | None = None  # the current job's part session
    alerts: list[Alert] = []  # newest last
    sim: SimStatus | None = None
