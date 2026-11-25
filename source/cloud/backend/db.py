"""Postgres schema and spoofed seed data.

Tables are created on startup to keep the take-home simple; production would use Alembic migrations.
Live-update triggers (Postgres LISTEN/NOTIFY) are in notify.sql, installed by migrate().
"""

import os
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://forge:forge@localhost:5432/forge")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Machine(Base):
    __tablename__ = "machines"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str]
    token: Mapped[str]  # per-machine API token used by its Pi agent
    bundle_version: Mapped[int] = mapped_column(default=1)


class CoreToolSlot(Base):
    __tablename__ = "core_tool_slots"
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.id"), primary_key=True)
    index: Mapped[int] = mapped_column(primary_key=True)
    tool_number: Mapped[int]


class Tool(Base):
    __tablename__ = "tools"
    tool_number: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str]
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Part(Base):
    __tablename__ = "parts"
    part_number: Mapped[str] = mapped_column(primary_key=True)
    material: Mapped[str]
    cycle_s: Mapped[int | None]  # expected cycle time for one part, from the CAM program


class PartFile(Base):
    """A file uploaded for a part. The bytes live in S3 under `key`; this row is the index."""

    __tablename__ = "part_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    part_number: Mapped[str] = mapped_column(ForeignKey("parts.part_number"))
    kind: Mapped[str]  # model (STEP) | drawing (PDF) | image | other
    key: Mapped[str] = mapped_column(unique=True)
    filename: Mapped[str]
    content_type: Mapped[str]
    size: Mapped[int]
    sha256: Mapped[str]
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PartTool(Base):
    __tablename__ = "part_tools"
    part_number: Mapped[str] = mapped_column(ForeignKey("parts.part_number"), primary_key=True)
    tool_number: Mapped[int] = mapped_column(primary_key=True)
    sequence: Mapped[int]  # order the program calls the tool


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.id"))
    order_number: Mapped[str]
    line_number: Mapped[int]
    part_number: Mapped[str] = mapped_column(ForeignKey("parts.part_number"))
    quantity: Mapped[int]
    machinist: Mapped[str]
    due_date: Mapped[date] = mapped_column(Date)
    queue_pos: Mapped[int]
    # queued -> done (the machinist completes it at the machine) | canceled (the office cancels it).
    # The first queued job is the one on the machine; the office can't move it, only cancel it.
    status: Mapped[str] = mapped_column(default="queued")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolTableSnapshot(Base):
    """Append-only history of every tool table a Pi reported. Latest seq = current state."""

    __tablename__ = "tool_table_snapshots"
    __table_args__ = (UniqueConstraint("machine_id", "seq"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.id"))
    seq: Mapped[int]
    tools: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MachineEvent(Base):
    """Machine telemetry, copied from the RabbitMQ stream by backend/telemetry.py (the activity feed)."""

    __tablename__ = "machine_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(unique=True)  # set by the Pi; makes redelivery harmless
    machine_id: Mapped[str] = mapped_column(index=True)
    # session_start | session_end | tool_change | cycle_start | cycle_stop | part_complete | quantity_reached
    # | alarm | alarm_clear | feed_hold | resume | job_complete
    type: Mapped[str]
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSONB)
    stream_offset: Mapped[int] = mapped_column(BigInteger, unique=True)  # where the consumer resumes
    # The part session it happened in. Set by the Pi; tool changes made with an empty queue arrive
    # without one and are attached to the next session when it starts. Not a foreign key: events are
    # stored even if their session_start was lost.
    session_id: Mapped[str | None] = mapped_column(index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PartSession(Base):
    """One job's time on one machine, from setup to Job complete or cancel.

    A projection of the event stream, kept by backend/sessions.py inside the telemetry consumer:
    it can be rebuilt by replaying the stream. Completion and cancel over REST also close it, so a
    lost session_end can't leave it open. Durations are seconds spent in each machine state.
    """

    __tablename__ = "part_sessions"
    id: Mapped[str] = mapped_column(primary_key=True)  # created by the Pi
    machine_id: Mapped[str] = mapped_column(index=True)
    job_id: Mapped[int] = mapped_column(index=True)
    part_number: Mapped[str] = mapped_column(index=True)
    order_ref: Mapped[str]  # "1616 - #1"
    quantity: Mapped[int]
    expected_cycle_s: Mapped[int | None]
    tool_seq: Mapped[int | None]  # the tool table it started with
    tools: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_cycle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None]  # completed | canceled
    # Live state: setup | cutting | idle | feed_hold | alarm, or ended. state_since is when it began.
    state: Mapped[str]
    state_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    alarm: Mapped[str | None]
    made: Mapped[int] = mapped_column(default=0)
    cycles: Mapped[int] = mapped_column(default=0)
    sum_cycle_s: Mapped[float] = mapped_column(Float, default=0)
    last_cycle_s: Mapped[float | None] = mapped_column(Float)
    avg_cycle_s: Mapped[float | None] = mapped_column(Float)
    min_cycle_s: Mapped[float | None] = mapped_column(Float)
    max_cycle_s: Mapped[float | None] = mapped_column(Float)
    setup_s: Mapped[float] = mapped_column(Float, default=0)
    cut_s: Mapped[float] = mapped_column(Float, default=0)
    idle_s: Mapped[float] = mapped_column(Float, default=0)
    hold_s: Mapped[float] = mapped_column(Float, default=0)
    alarm_s: Mapped[float] = mapped_column(Float, default=0)
    alarms: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MachineStatus(Base):
    __tablename__ = "machine_status"
    machine_id: Mapped[str] = mapped_column(ForeignKey("machines.id"), primary_key=True)
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seq: Mapped[int | None]
    agent_version: Mapped[str | None]


class MachinePresence(Base):
    """Every time a Pi went online or offline, so offline time can be worked out afterwards."""

    __tablename__ = "machine_presence"
    id: Mapped[int] = mapped_column(primary_key=True)
    machine_id: Mapped[str] = mapped_column(index=True)
    online: Mapped[bool]
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- Seed data: the machine and its tools, matching the production screenshot.
# Parts, files and jobs are loaded through the API by demo/load.py, like the real web system would.

SEED_MACHINE_ID = os.environ.get("SEED_MACHINE_ID", "haas-vf2-01")
SEED_MACHINE_TOKEN = os.environ.get("SEED_MACHINE_TOKEN", "dev-token")

CORE_TOOLS = {10: 99, 23: 100, 24: 159, 25: 15, 26: 16, 27: 17, 28: 18, 29: 19, 30: 20, 31: 90}
TOOLS = {
    15: "1/4 endmill 3FL", 16: "3/8 endmill 3FL", 17: "1/2 endmill 3FL", 18: "#7 drill",
    19: "1/4-20 tap", 20: "1/2 spot drill", 27: "3/16 ball mill", 29: "1/8 endmill 2FL",
    37: "5/16 drill", 58: "M6 tap", 77: "chamfer mill 90deg", 90: "2.5 face mill", 99: "Renishaw probe",
    100: "1/2 drill", 108: "3/4 endmill", 113: "3/8 ball mill", 120: "1/4 drill", 130: "#21 drill",
    150: "10-32 thread mill", 159: "1/8 spot drill", 283: "1/16 endmill", 295: "deburr tool",
    312: "1/4 chamfer", 321: "5/8 drill", 325: "3/8 drill", 327: "slitting saw", 350: "boring bar",
    366: "dovetail cutter",
}
UNMEASURED = {113, 283}  # new tools waiting at the presetter


def seed(session: Session) -> None:
    if session.scalar(select(Machine).limit(1)):
        return
    now = datetime.now().astimezone()
    session.add(Machine(id=SEED_MACHINE_ID, name="HAAS VF-2 #1", token=SEED_MACHINE_TOKEN))
    session.flush()
    session.add_all(CoreToolSlot(machine_id=SEED_MACHINE_ID, index=i, tool_number=t) for i, t in CORE_TOOLS.items())
    session.add_all(
        Tool(tool_number=n, description=d, measured_at=None if n in UNMEASURED else now) for n, d in TOOLS.items()
    )
    session.commit()


# Columns added after the first release, for databases created before them.
UPGRADES = [
    "ALTER TABLE parts ADD COLUMN IF NOT EXISTS cycle_s integer",
    "ALTER TABLE machine_events ADD COLUMN IF NOT EXISTS session_id varchar",
    "ALTER TABLE machine_events ADD COLUMN IF NOT EXISTS received_at timestamptz NOT NULL DEFAULT now()",
    "CREATE INDEX IF NOT EXISTS ix_machine_events_session_id ON machine_events (session_id)",
]
NOTIFY_SQL = Path(__file__).with_name("notify.sql")


def migrate() -> None:
    """Create tables, apply upgrades and install the NOTIFY triggers. Safe to run from several processes
    at once (the API and the telemetry consumer both start with it): an advisory lock serialises them."""
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(7151)"))
        Base.metadata.create_all(conn)
        for statement in UPGRADES:
            conn.execute(text(statement))
        # A multi-statement script with $$ function bodies: send it as one simple query, no parameters.
        conn.connection.cursor().execute(NOTIFY_SQL.read_text())


def init_db() -> None:
    migrate()
    with SessionLocal() as session:
        seed(session)
