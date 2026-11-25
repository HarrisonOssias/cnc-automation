"""Part sessions in Postgres: a projection of the machine-event stream.

The Pi opens a session when a job becomes its current job and stamps the session id on every event.
The telemetry consumer calls `project()` for each new event, in the same transaction that stores the
event, so `part_sessions` always matches `machine_events`. Wipe both and replay the stream from offset
0 and you get the same rows back.

`apply_event` is the pure part (no I/O): it moves a session through its states and adds up the time
spent in each one. Time is credited to the state the machine was in *before* each event:

    session_start -> setup -> cycle_start -> cutting -> cycle_stop -> idle -> cycle_start -> ...
                                 feed_hold <-> resume          alarm -> alarm_clear -> idle (or setup)
    session_end (or a REST completion / cancel) -> ended
"""

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend.db import MachineEvent, PartSession

DURATION_FIELD = {"setup": "setup_s", "cutting": "cut_s", "idle": "idle_s", "feed_hold": "hold_s", "alarm": "alarm_s"}


def new_session(session_id: str, machine_id: str, at: datetime, data: dict) -> PartSession:
    return PartSession(
        id=session_id, machine_id=machine_id, job_id=data["job_id"], part_number=data.get("part_number", ""),
        order_ref=data.get("order", ""), quantity=data.get("quantity", 0),
        expected_cycle_s=data.get("expected_cycle_s"), tool_seq=data.get("tool_seq"), tools=data.get("tools"),
        started_at=at, state="setup", state_since=at, updated_at=at,
        made=0, cycles=0, sum_cycle_s=0.0, setup_s=0.0, cut_s=0.0, idle_s=0.0, hold_s=0.0, alarm_s=0.0, alarms=0,
    )


def _enter(row: PartSession, state: str, at: datetime) -> None:
    """Credit the time since the last change to the current state, then switch."""
    if row.state in DURATION_FIELD and at > row.state_since:
        field = DURATION_FIELD[row.state]
        setattr(row, field, round(getattr(row, field) + (at - row.state_since).total_seconds(), 1))
    if at > row.state_since:
        row.state_since = at
    row.state = state


def close(row: PartSession, at: datetime, reason: str) -> bool:
    """End the session. Returns False if it had already ended (a REST close and session_end both arrive)."""
    if row.ended_at is not None:
        return False
    _enter(row, "ended", at)
    row.ended_at, row.end_reason, row.alarm = at, reason, None
    row.updated_at = at
    return True


def _count_part(row: PartSession, data: dict) -> None:
    cycle_s = data.get("cycle_s")
    row.made = data.get("made", row.made + 1)
    if cycle_s is not None:
        row.cycles += 1
        row.sum_cycle_s = round(row.sum_cycle_s + cycle_s, 1)
        row.last_cycle_s = cycle_s
        row.avg_cycle_s = round(row.sum_cycle_s / row.cycles, 1)
        row.min_cycle_s = cycle_s if row.min_cycle_s is None else min(row.min_cycle_s, cycle_s)
        row.max_cycle_s = cycle_s if row.max_cycle_s is None else max(row.max_cycle_s, cycle_s)


def apply_event(row: PartSession, event_type: str, at: datetime, data: dict) -> None:
    """Move an open session along for one event (not session_start, which creates the row)."""
    if row.ended_at is not None:
        # Closed already, usually by the REST completion arriving before the consumer caught up.
        # Parts made before the close still count; nothing reopens the session or changes its timings.
        if at <= row.ended_at and event_type == "part_complete":
            _count_part(row, data)
        elif event_type == "session_end":
            row.made = data.get("made", row.made)  # the Pi's count is the one that matters
        return
    match event_type:
        case "cycle_start":
            _enter(row, "cutting", at)
            row.first_cycle_at = row.first_cycle_at or at
        case "cycle_stop":
            _enter(row, "idle", at)
        case "part_complete":
            _count_part(row, data)
        case "feed_hold":
            _enter(row, "feed_hold", at)
        case "resume":
            _enter(row, "cutting", at)
        case "alarm":
            _enter(row, "alarm", at)
            row.alarm = f"{data.get('code', '')} {data.get('message', '')}".strip()
            row.alarms += 1
        case "alarm_clear":
            _enter(row, "idle" if row.first_cycle_at else "setup", at)
            row.alarm = None
        case "session_end":
            row.made = data.get("made", row.made)
            close(row, at, data.get("reason", "completed"))
        case _:  # tool_change, quantity_reached, job_complete: no state change
            pass
    row.updated_at = max(row.updated_at, at)


# --- Database side -------------------------------------------------------------


def project(db: Session, event: MachineEvent) -> None:
    """Apply one newly stored event to its session. Called once per event (only when it was inserted)."""
    if not event.session_id:
        return
    if event.type == "session_start":
        if db.get(PartSession, event.session_id):
            return
        db.add(new_session(event.session_id, event.machine_id, event.at, event.data))
        # Tool changes made while the queue was empty belong to this session's setup: the untagged
        # ones since the machine's last event that had a session (the end of the previous one).
        previous = db.scalar(
            select(func.max(MachineEvent.at)).where(
                MachineEvent.machine_id == event.machine_id, MachineEvent.session_id.is_not(None),
                MachineEvent.session_id != event.session_id, MachineEvent.at < event.at,
            )
        )
        orphans = update(MachineEvent).where(
            MachineEvent.machine_id == event.machine_id, MachineEvent.session_id.is_(None),
            MachineEvent.type == "tool_change", MachineEvent.at <= event.at,
        )
        if previous is not None:
            orphans = orphans.where(MachineEvent.at > previous)
        db.execute(orphans.values(session_id=event.session_id))
        return
    row = db.get(PartSession, event.session_id, with_for_update=True)
    if row:
        apply_event(row, event.type, event.at, event.data)


def close_for_job(db: Session, job_id: int, at: datetime, reason: str) -> None:
    """Close the job's open session from the REST side (completion upload or office cancel)."""
    row = db.scalar(
        select(PartSession).where(PartSession.job_id == job_id, PartSession.ended_at.is_(None)).with_for_update()
    )
    if row:
        close(row, at, reason)
