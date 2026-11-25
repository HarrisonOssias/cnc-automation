"""Pi agent: runs on each machine's Raspberry Pi.

- Reads the tool table (stubbed) and saves every change locally first.
- Computes the page with forge_core and pushes it to the kiosk over local SSE,
  so the screen keeps working when the network is down.
- Uploads tool tables to the backend over REST, retrying until confirmed.
- Fetches its bundle (jobs + config) over REST when MQTT says it changed,
  on every reconnect, and once a minute as a safety net.
- Publishes presence on MQTT (retained status + Last Will).
- Caches part images for the current and next job only, fetched from the backend API.
- Records the machinist's "Job complete" locally, moves the kiosk to the next job, and reports it upstream.
- Opens a part session when a job becomes the current job and closes it on Job complete or cancel.
  Every machine event carries the open session's id.
- Publishes machine events (sessions, tool changes, cycles, parts, alarms) on MQTT for the cloud's
  telemetry stream. Best effort, unlike the REST uploads: they're for live activity, not the record.
"""

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx
import paho.mqtt.client as mqtt
import sdnotify
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from forge_core import EMPTY, Alert, Bundle, Job, MachineState, PageState, Session, SyncStatus, compute_page
from forge_core.page import SLOT_COUNT, validate_tool_table

from agent.sim import MachineSim


def _release_version() -> str:
    """VERSION sits at the release root (the service's working directory, /opt/forge/current on a Pi).

    source/edge/build-release.sh writes it; without one (a dev checkout, Docker) the version is "dev".
    """
    path = Path("VERSION")
    return path.read_text().strip() if path.exists() else "dev"


AGENT_VERSION = _release_version()
SYNC_INTERVAL_S = 5  # retry cadence while uploads are pending or the backend is down
HEALTH_INTERVAL_S = 15  # how quickly the kiosk notices the backend went away
BUNDLE_REFRESH_S = 60  # safety-net bundle fetch, in case an MQTT notice was missed
ALERTS_KEPT = 5  # progress alerts kept for the kiosk (it shows each for a few seconds)

log = logging.getLogger("agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@dataclass(frozen=True)
class Config:
    machine_id: str = os.environ.get("MACHINE_ID", "haas-vf2-01")
    machine_token: str = os.environ.get("MACHINE_TOKEN", "dev-token")
    backend_url: str = os.environ.get("BACKEND_URL", "http://localhost:8000")
    mqtt_host: str = os.environ.get("MQTT_HOST", "localhost")
    mqtt_port: int = int(os.environ.get("MQTT_PORT", "1883"))
    mqtt_user: str = os.environ.get("MQTT_USER", "forge")
    mqtt_password: str = os.environ.get("MQTT_PASSWORD", "forge")
    # systemd's StateDirectory= sets STATE_DIRECTORY (/var/lib/forge on the Pi).
    data_dir: Path = Path(os.environ.get("DATA_DIR") or os.environ.get("STATE_DIRECTORY") or "data")
    # Default: the kiosk build next to this package (source/edge/kiosk/dist) in a dev checkout.
    kiosk_dir: Path = Path(os.environ.get("KIOSK_DIR") or Path(__file__).resolve().parents[1] / "kiosk" / "dist")


# --- Machine input ----------------------------------------------------------


class ToolTableSource(Protocol):
    """Where tool tables come from. The real one would query the HAAS control."""

    def read(self) -> list[int]: ...


# The production screenshot from the spec: 31 slots, -1 = empty.
SCREENSHOT_TABLE = [37, 27, -1, 77, 108, 312, 120, 130, -1, 99, 327, 350, -1, 150, 321, 325,
                    366, 58, 295, -1, -1, -1, 100, 159, 15, 16, 17, 18, 19, 20, 90]


class StubHaasSource:
    """Stand-in for the HAAS read. Later changes ("tool change events") come from the CLI."""

    def read(self) -> list[int]:
        raw = os.environ.get("HAAS_STUB_TABLE")
        return json.loads(raw) if raw else list(SCREENSHOT_TABLE)


# --- Local storage (SQLite): the cache, never the source of truth -----------


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending (
                seq INTEGER PRIMARY KEY, tools TEXT NOT NULL, captured_at TEXT NOT NULL);
            -- Jobs the machinist completed here. Kept until the backend has it AND a newer bundle
            -- no longer lists the job, so the kiosk never shows a finished job again.
            CREATE TABLE IF NOT EXISTS completions (
                job_id INTEGER PRIMARY KEY, completed_at TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0);
            -- Part sessions: one per job run on this machine. At most one is open (closed_at IS NULL).
            -- Kept here so a restart resumes the same session and the part count survives.
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, opened_at TEXT NOT NULL,
                first_cycle_at TEXT, made INTEGER NOT NULL DEFAULT 0, cycles INTEGER NOT NULL DEFAULT 0,
                sum_cycle_s REAL NOT NULL DEFAULT 0, last_cycle_s REAL, closed_at TEXT, reason TEXT);
            """
        )

    def _get(self, key: str):
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))

    def tool_table(self) -> list[int] | None:
        return self._get("tool_table")

    def last_seq(self) -> int:
        return self._get("seq") or 0

    def record_tool_table(self, tools: list[int]) -> int:
        """Save the table and queue it for upload, in one transaction."""
        with self.db:
            seq = self.last_seq() + 1
            self._put("tool_table", tools)
            self._put("seq", seq)
            self.db.execute(
                "INSERT INTO pending VALUES (?, ?, ?)", (seq, json.dumps(tools), datetime.now(UTC).isoformat())
            )
        return seq

    def pending(self) -> list[tuple[int, list[int], str]]:
        rows = self.db.execute("SELECT seq, tools, captured_at FROM pending ORDER BY seq").fetchall()
        return [(seq, json.loads(tools), at) for seq, tools, at in rows]

    def ack(self, seq: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM pending WHERE seq = ?", (seq,))

    def record_completion(self, job_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO completions (job_id, completed_at) VALUES (?, ?)",
                (job_id, datetime.now(UTC).isoformat()),
            )

    def completions(self) -> list[tuple[int, str, bool]]:
        rows = self.db.execute("SELECT job_id, completed_at, acked FROM completions ORDER BY completed_at")
        return [(job_id, at, bool(acked)) for job_id, at, acked in rows]

    def ack_completion(self, job_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE completions SET acked = 1 WHERE job_id = ?", (job_id,))

    def prune_completions(self, open_job_ids: set[int]) -> None:
        with self.db:
            for job_id, _, acked in self.completions():
                if acked and job_id not in open_job_ids:
                    self.db.execute("DELETE FROM completions WHERE job_id = ?", (job_id,))

    # Part sessions
    def open_session(self) -> "StoredSession | None":
        row = self.db.execute(
            "SELECT session_id, job_id, opened_at, first_cycle_at, made, cycles, sum_cycle_s, last_cycle_s"
            " FROM sessions WHERE closed_at IS NULL"
        ).fetchone()
        return StoredSession(*row) if row else None

    def start_session(self, job_id: int) -> "StoredSession":
        session = StoredSession(uuid.uuid4().hex, job_id, datetime.now(UTC).isoformat())
        with self.db:
            self.db.execute(
                "INSERT INTO sessions (session_id, job_id, opened_at) VALUES (?, ?, ?)",
                (session.session_id, job_id, session.opened_at),
            )
        return session

    def close_session(self, session_id: str, reason: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sessions SET closed_at = ?, reason = ? WHERE session_id = ? AND closed_at IS NULL",
                (datetime.now(UTC).isoformat(), reason, session_id),
            )

    def mark_first_cycle(self, session_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sessions SET first_cycle_at = ? WHERE session_id = ? AND first_cycle_at IS NULL",
                (datetime.now(UTC).isoformat(), session_id),
            )

    def record_part(self, session_id: str, cycle_s: float) -> int:
        """Count one finished part; returns how many the session has made."""
        with self.db:
            self.db.execute(
                "UPDATE sessions SET made = made + 1, cycles = cycles + 1, sum_cycle_s = sum_cycle_s + ?,"
                " last_cycle_s = ? WHERE session_id = ?",
                (cycle_s, cycle_s, session_id),
            )
        return self.db.execute("SELECT made FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]

    def bundle(self) -> tuple[Bundle | None, datetime | None]:
        raw = self._get("bundle")
        if not raw:
            return None, None
        return Bundle.model_validate(raw["bundle"]), datetime.fromisoformat(raw["fetched_at"])

    def save_bundle(self, bundle: Bundle) -> None:
        with self.db:
            self._put("bundle", {"bundle": bundle.model_dump(mode="json"), "fetched_at": datetime.now(UTC).isoformat()})


@dataclass(frozen=True)
class StoredSession:
    session_id: str
    job_id: int
    opened_at: str
    first_cycle_at: str | None = None
    made: int = 0
    cycles: int = 0
    sum_cycle_s: float = 0.0
    last_cycle_s: float | None = None


class ImageCache:
    """Part pictures on local disk, named by sha256 (e.g. <sha256>.png)."""

    def __init__(self, path: Path):
        self.dir = path
        self.dir.mkdir(parents=True, exist_ok=True)

    def find(self, sha256: str) -> Path | None:
        return next((p for p in self.dir.glob(f"{sha256}.*") if p.suffix != ".part"), None)

    def cached(self) -> list[str]:
        return sorted(p.name.split(".")[0] for p in self.dir.iterdir() if p.suffix != ".part")

    def keep_only(self, wanted: set[str]) -> None:
        for p in self.dir.iterdir():
            if p.name.split(".")[0] not in wanted or p.suffix == ".part":
                p.unlink()

    def save(self, sha256: str, data: bytes, content_type: str) -> None:
        tmp = self.dir / f"{sha256}.part"
        tmp.write_bytes(data)
        tmp.replace(self.dir / f"{sha256}{mimetypes.guess_extension(content_type) or '.img'}")


# --- The agent --------------------------------------------------------------


@dataclass
class Agent:
    cfg: Config
    store: Store
    source: ToolTableSource
    images: ImageCache
    online: bool = False
    offline_since: datetime | None = field(default_factory=lambda: datetime.now(UTC))
    bundle_stale: bool = True
    last_bundle_check: float = 0.0
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    # What the control is doing right now (stubbed: the CLI and the demo simulator drive it).
    # In memory only: after a restart a real agent would read it back from the control.
    cycle_started_at: datetime | None = None
    hold_since: datetime | None = None
    held_s: float = 0.0  # time the running cycle spent in feed hold
    alarm: str | None = None
    alerts: deque = field(default_factory=lambda: deque(maxlen=ALERTS_KEPT))
    _alert_id: int = 0
    sim: object | None = None  # MachineSim, attached by agent.sim; reported on the page for the demo drawer
    _last_sent: str = ""

    def __post_init__(self):
        self.http = httpx.AsyncClient(
            base_url=self.cfg.backend_url, timeout=5, headers={"X-Machine-Token": self.cfg.machine_token}
        )
        self.mqtt = self._make_mqtt()
        self.notify = sdnotify.SystemdNotifier()  # no-op outside systemd

    # Tool table changes (from the machine, or the CLI in this take-home)
    def record_tool_table(self, tools: list[int]) -> int:
        validate_tool_table(tools)
        previous = self.store.tool_table()
        seq = self.store.record_tool_table(tools)
        log.info("tool table seq %d recorded", seq)
        self.broadcast()  # the screen updates first, before any network call
        self.wake.set()
        if previous:
            changes = [
                {"index": i, "from": None if a == EMPTY else a, "to": None if b == EMPTY else b}
                for i, (a, b) in enumerate(zip(previous, tools), start=1)
                if a != b
            ]
            self.publish_event("tool_change", seq=seq, changes=changes)
        return seq

    # Job completion (the machinist taps "Job complete" on the kiosk)
    def open_jobs(self) -> list:
        """The cached queue minus jobs completed here that the backend hasn't caught up on yet."""
        bundle, _ = self.store.bundle()
        done = {job_id for job_id, _, _ in self.store.completions()}
        return [j for j in (bundle.jobs if bundle else []) if j.id not in done]

    def complete_job(self, job_id: int) -> None:
        jobs = self.open_jobs()
        if not jobs or jobs[0].id != job_id:  # e.g. a double tap after the screen already moved on
            raise ValueError("that job is not the current job")
        self.publish_event("job_complete", **_job_ref(jobs[0]))
        self.store.record_completion(job_id)
        log.info("job %d completed by the machinist", job_id)
        self.sync_session(completed=job_id)  # closes this session, opens the next job's
        self.broadcast()  # the next job shows right away, online or not
        self.wake.set()

    # Part sessions
    def current_job(self) -> Job | None:
        jobs = self.open_jobs()
        return jobs[0] if jobs else None

    def sync_session(self, completed: int | None = None) -> None:
        """Make the open session match the current job: close it if its job left, open one for a new job.

        Called after every bundle fetch and completion. Without a bundle yet we can't tell, so do nothing.
        """
        bundle, _ = self.store.bundle()
        if bundle is None:
            return
        job = self.current_job()
        session = self.store.open_session()
        if session and (job is None or session.job_id != job.id):
            reason = "completed" if session.job_id == completed else "canceled"
            self._end_cycle_quietly(reason)
            self.publish_event("session_end", reason=reason, made=session.made, job_id=session.job_id)
            self.store.close_session(session.session_id, reason)
            log.info("session %s closed (%s, %d made)", session.session_id[:8], reason, session.made)
            if reason == "completed":
                self.alert("done", f"Session closed: {session.made} made in {_duration(session.opened_at)}")
            session = None
        if job and session is None:
            session = self.store.start_session(job.id)
            log.info("session %s opened for job %d", session.session_id[:8], job.id)
            self.publish_event(
                "session_start", **_job_ref(job), quantity=job.quantity, expected_cycle_s=job.cycle_s,
                tool_seq=self.store.last_seq(), tools=self.store.tool_table(),
            )

    # The control: cycles, parts, feed hold, alarms (stubbed: the CLI and the demo simulator)
    def cycle_start(self) -> None:
        session = self.store.open_session()
        if session is None:
            raise ValueError("no job on the machine")
        if self.alarm:
            raise ValueError(f"the machine is in alarm ({self.alarm}); clear it first")
        if self.cycle_started_at:
            raise ValueError("a cycle is already running")
        self.cycle_started_at, self.hold_since, self.held_s = datetime.now(UTC), None, 0.0
        self.store.mark_first_cycle(session.session_id)
        job = self.current_job()
        self.publish_event("cycle_start", **_job_ref(job), program=f"O{job.order_number:0>5}")
        self.broadcast()

    def cycle_stop(self, machine_s: float | None = None) -> int:
        """The cycle ended normally (M30): one more part made. Returns the session's part count.

        machine_s is the cycle time to report; the simulator passes scaled machine time. Without it,
        the cycle's own time is used (wall clock minus feed hold).
        """
        session = self.store.open_session()
        if session is None or not self.cycle_started_at:
            raise ValueError("no cycle is running")
        job = self.current_job()
        cycle_s = round(machine_s if machine_s is not None else self._cycle_elapsed(), 1)
        self.cycle_started_at = self.hold_since = None
        self.publish_event("cycle_stop", **_job_ref(job), duration_s=cycle_s)
        made = self.store.record_part(session.session_id, cycle_s)
        self.publish_event("part_complete", job_id=job.id, made=made, quantity=job.quantity, cycle_s=cycle_s)
        if made == job.quantity:
            self.publish_event("quantity_reached", job_id=job.id)
            self.alert("done", f"All {job.quantity} made: tap Job complete")
        else:
            self.alert("info", f"Part {made} of {job.quantity} done")
        self.broadcast()
        return made

    def feed_hold(self) -> None:
        if not self.cycle_started_at:
            raise ValueError("no cycle is running")
        if self.hold_since is None:
            self.hold_since = datetime.now(UTC)
            self.publish_event("feed_hold")
            self.broadcast()

    def resume(self) -> None:
        if self.hold_since is not None:
            self.held_s += (datetime.now(UTC) - self.hold_since).total_seconds()
            self.hold_since = None
            self.publish_event("resume")
            self.broadcast()

    def raise_alarm(self, code: str, message: str) -> None:
        """An alarm stops the machine; a running cycle is lost (no part counted)."""
        self._end_cycle_quietly("alarm")
        self.alarm = f"{code} {message}"
        self.publish_event("alarm", code=code, message=message)
        self.alert("alarm", f"Alarm {self.alarm}")
        self.broadcast()

    def clear_alarm(self) -> None:
        if self.alarm:
            self.alarm = None
            self.publish_event("alarm_clear")
            self.broadcast()

    def _cycle_elapsed(self) -> float:
        if not self.cycle_started_at:
            return 0.0
        now = datetime.now(UTC)
        held = self.held_s + ((now - self.hold_since).total_seconds() if self.hold_since else 0)
        return max(0.0, (now - self.cycle_started_at).total_seconds() - held)

    def _end_cycle_quietly(self, reason: str) -> None:
        """Abort a running cycle without counting a part (alarm, or the session ended mid-cycle)."""
        if self.cycle_started_at:
            self.publish_event("cycle_stop", duration_s=round(self._cycle_elapsed(), 1), aborted=reason)
            self.cycle_started_at = self.hold_since = None
            if self.sim:
                self.sim.poke()

    def alert(self, level: str, text: str) -> None:
        self._alert_id += 1
        self.alerts.append(Alert(id=self._alert_id, at=datetime.now(UTC), level=level, text=text))

    def machine_state(self, session: StoredSession | None) -> MachineState:
        if self.alarm:
            return MachineState.ALARM
        if self.cycle_started_at:
            return MachineState.FEED_HOLD if self.hold_since else MachineState.CUTTING
        if session and not session.first_cycle_at:
            return MachineState.SETUP
        return MachineState.IDLE

    def session_view(self, job: Job | None) -> Session | None:
        stored = self.store.open_session()
        if not job or not stored or stored.job_id != job.id:
            return None
        return Session(
            id=stored.session_id, job_id=job.id, opened_at=stored.opened_at, first_cycle_at=stored.first_cycle_at,
            made=stored.made, quantity=job.quantity, last_cycle_s=stored.last_cycle_s,
            avg_cycle_s=round(stored.sum_cycle_s / stored.cycles, 1) if stored.cycles else None,
            expected_cycle_s=job.cycle_s, cycle_started_at=self.cycle_started_at,
            state=self.machine_state(stored), alarm=self.alarm,
        )

    # Page state for the kiosk
    def page(self) -> PageState:
        tools = self.store.tool_table() or [EMPTY] * SLOT_COUNT
        bundle, fetched_at = self.store.bundle()
        bundle = bundle or Bundle(machine_id=self.cfg.machine_id, version=0, core_tools={}, measured_tools=[], jobs=[])
        page = compute_page(tools, bundle.model_copy(update={"jobs": self.open_jobs()}))
        page.sync = SyncStatus(
            online=self.online,
            offline_since=self.offline_since,
            bundle_fetched_at=fetched_at,
            pending_uploads=len(self.store.pending()) + sum(not acked for _, _, acked in self.store.completions()),
        )
        page.cached_images = self.images.cached()
        page.session = self.session_view(page.job)
        page.alerts = list(self.alerts)
        page.sim = self.sim.status() if self.sim else None
        return page

    def broadcast(self) -> None:
        data = self.page().model_dump_json()
        if data == self._last_sent:
            return
        self._last_sent = data
        for q in self.subscribers:
            if q.full():
                q.get_nowait()  # a slow client only needs the latest state
            q.put_nowait(data)

    # Sync with the backend
    async def sync_loop(self) -> None:
        self.notify.notify("READY=1")
        while True:
            # Clear before the pass, not after: a notice that arrives mid-pass then wakes the next pass
            # at once, instead of waiting out the idle timeout.
            self.wake.clear()
            try:
                called = await self.flush_pending()
                called = await self.flush_completions() or called
                if self.bundle_stale or time.monotonic() - self.last_bundle_check > BUNDLE_REFRESH_S:
                    await self.fetch_bundle()
                    called = True
                called = await self.sync_images() or called
                if not called:
                    (await self.http.get("/healthz")).raise_for_status()
                self.set_online(True)
            except httpx.HTTPError as e:
                log.warning("backend unreachable: %s", e)
                self.set_online(False)
            self.broadcast()
            self.notify.notify("WATCHDOG=1")
            busy = self.store.pending() or any(not acked for _, _, acked in self.store.completions())
            idle = SYNC_INTERVAL_S if (not self.online or busy) else HEALTH_INTERVAL_S
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=idle)
            except TimeoutError:
                pass

    async def flush_pending(self) -> bool:
        sent = False
        for seq, tools, captured_at in self.store.pending():
            r = await self.http.post(
                f"/machines/{self.cfg.machine_id}/tool-tables",
                json={"seq": seq, "tools": tools, "captured_at": captured_at},
            )
            if r.status_code == 422:  # the backend will never accept it; don't block the queue
                log.error("backend rejected tool table seq %d: %s", seq, r.text)
            else:
                r.raise_for_status()
            self.store.ack(seq)
            sent = True
        if sent:
            self.publish_status(online=True)
        return sent

    async def flush_completions(self) -> bool:
        sent = False
        for job_id, completed_at, acked in self.store.completions():
            if acked:
                continue
            r = await self.http.post(
                f"/machines/{self.cfg.machine_id}/jobs/{job_id}/complete", json={"completed_at": completed_at}
            )
            if r.status_code != 404:  # 404: the job is gone from this machine; nothing left to report
                r.raise_for_status()
            self.store.ack_completion(job_id)
            self.bundle_stale = True  # pick up the queue the backend now has
            sent = True
        return sent

    async def fetch_bundle(self) -> None:
        # Cleared before the request: a bundle_changed that arrives while it's in flight marks it stale
        # again, since this response may predate that change.
        self.bundle_stale = False
        try:
            r = await self.http.get(f"/machines/{self.cfg.machine_id}/bundle")
            r.raise_for_status()
        except httpx.HTTPError:
            self.bundle_stale = True
            raise
        bundle = Bundle.model_validate(r.json())
        self.store.save_bundle(bundle)
        self.store.prune_completions({j.id for j in bundle.jobs})
        self.sync_session()
        self.last_bundle_check = time.monotonic()
        log.info("bundle v%d fetched (%d jobs)", bundle.version, len(bundle.jobs))

    async def sync_images(self) -> bool:
        """Keep pictures for the current and next job only; fetch any new one from the backend."""
        upcoming = self.open_jobs()[:2]
        wanted = {j.image_sha256: j.image_file_id for j in upcoming if j.image_sha256 and j.image_file_id}
        self.images.keep_only(set(wanted))
        called = False
        for sha256, file_id in wanted.items():
            if self.images.find(sha256):
                continue
            r = await self.http.get(f"/machines/{self.cfg.machine_id}/files/{file_id}")
            called = True
            if r.status_code == 404:  # the job moved on since the bundle was fetched
                continue
            r.raise_for_status()
            if hashlib.sha256(r.content).hexdigest() != sha256:
                log.warning("image %s failed its checksum; will retry", file_id)
                continue
            self.images.save(sha256, r.content, r.headers.get("content-type", ""))
            log.info("cached part image %s", sha256[:12])
        return called

    def set_online(self, online: bool) -> None:
        if online != self.online:
            log.info("backend is %s", "reachable" if online else "unreachable")
            self.offline_since = None if online else datetime.now(UTC)
        self.online = online

    def mark_bundle_stale(self) -> None:
        self.bundle_stale = True
        self.wake.set()

    # MQTT: presence out, bundle_changed notices in
    def _topic(self, name: str) -> str:
        return f"forge/machines/{self.cfg.machine_id}/{name}"

    def _make_mqtt(self) -> mqtt.Client:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"agent-{self.cfg.machine_id}", clean_session=False
        )
        client.username_pw_set(self.cfg.mqtt_user, self.cfg.mqtt_password)
        # If the Pi drops off, the broker tells everyone it's offline.
        client.will_set(self._topic("status"), json.dumps({"online": False}), qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        return client

    def start_mqtt(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.mqtt.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=30)
        self.mqtt.loop_start()  # paho runs in its own thread and reconnects on its own

    def publish_event(self, event_type: str, **data) -> None:
        """One machine event onto MQTT forge/machines/<id>/events, which RabbitMQ copies into its stream.

        QoS 1 with a persistent session: paho holds it while disconnected and sends it on reconnect.
        """
        session = self.store.open_session()
        event = {
            "event_id": uuid.uuid4().hex,
            "machine_id": self.cfg.machine_id,
            # null only for tool changes with an empty queue; the cloud attaches those to the next session
            "session_id": session.session_id if session else None,
            "type": event_type,
            "at": datetime.now(UTC).isoformat(),
            "data": data,
        }
        self.mqtt.publish(self._topic("events"), json.dumps(event), qos=1)

    def publish_status(self, online: bool) -> None:
        payload = {"online": online, "agent_version": AGENT_VERSION, "last_seq": self.store.last_seq()}
        self.mqtt.publish(self._topic("status"), json.dumps(payload), qos=1, retain=True)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.warning("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected")
        client.subscribe(self._topic("cmd"), qos=1)
        # paho calls this from its own thread; SQLite and agent state live on the event loop.
        self.loop.call_soon_threadsafe(self._after_connect)

    def _after_connect(self) -> None:
        self.publish_status(online=True)
        self.mark_bundle_stale()  # catch up on anything missed while disconnected

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        try:
            cmd = json.loads(msg.payload)
        except ValueError:
            return
        if cmd.get("type") == "bundle_changed":
            log.info("bundle_changed v%s", cmd.get("version"))
            self.loop.call_soon_threadsafe(self.mark_bundle_stale)


def _job_ref(job) -> dict:
    return {"job_id": job.id, "order": f"{job.order_number} - #{job.line_number}", "part_number": job.part_number}


def _duration(since_iso: str) -> str:
    minutes = int((datetime.now(UTC) - datetime.fromisoformat(since_iso)).total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60:02d} m" if minutes >= 60 else f"{minutes} min"


# --- Local HTTP API, served on the Pi for the kiosk and the CLI --------------

cfg = Config()
agent = Agent(cfg, Store(cfg.data_dir / "agent.db"), StubHaasSource(), ImageCache(cfg.data_dir / "images"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if agent.store.tool_table() is None:
        agent.record_tool_table(agent.source.read())
    agent.start_mqtt(asyncio.get_running_loop())
    MachineSim(agent)
    agent.sync_session()  # from the cached bundle: resumes the open session, works offline
    task = asyncio.create_task(agent.sync_loop())
    yield
    task.cancel()
    agent.publish_status(online=False)
    agent.mqtt.loop_stop()


app = FastAPI(title="Forge Pi agent", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    # update.sh checks the version here before it commits to a new release.
    return {"ok": True, "version": AGENT_VERSION}


@app.get("/api/page", response_model=PageState)
async def get_page():
    return agent.page()


@app.get("/api/events")
async def events(request: Request):
    """SSE stream: the full page state on connect and on every change."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    agent.subscribers.add(queue)

    async def stream():
        try:
            yield f"data: {agent.page().model_dump_json()}\n\n"
            while not await request.is_disconnected():
                try:
                    yield f"data: {await asyncio.wait_for(queue.get(), timeout=15)}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            agent.subscribers.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/images/{sha256}")
async def get_image(sha256: str):
    """A cached part picture; works offline."""
    path = re.fullmatch(r"[0-9a-f]{64}", sha256) and agent.images.find(sha256)
    if not path:
        raise HTTPException(404, "image not cached")
    return FileResponse(path, headers={"Cache-Control": "max-age=86400, immutable"})


@app.post("/api/jobs/{job_id}/complete")
async def complete_job(job_id: int):
    """The kiosk's "Job complete" button. Saved locally first, so it works offline."""
    try:
        agent.complete_job(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return {"completed": job_id}


@app.post("/api/cycle/{state}")
async def cycle(state: str):
    """Stand-in for the control's cycle start/stop signal (python -m agent cycle start|stop).

    A normal stop (M30) counts one part for the session.
    """
    if state not in ("start", "stop"):
        raise HTTPException(404, "state must be start or stop")
    if state == "start":
        _control(agent.cycle_start)
        return {"cycle": state}
    return {"cycle": state, "made": _control(agent.cycle_stop)}


# --- Demo machine simulator (the kiosk's ?demo=1 drawer and `python -m agent sim ...`) ---


class SimRunIn(BaseModel):
    speed: float = 20


class AlarmIn(BaseModel):
    code: str = "1010"


@app.post("/api/sim/run")
async def sim_run(body: SimRunIn):
    _control(agent.sim.run, body.speed)
    return agent.sim.status()


@app.post("/api/sim/stop")
async def sim_stop():
    agent.sim.stop()
    return agent.sim.status()


@app.post("/api/sim/part")
async def sim_part():
    return {"made": _control(agent.sim.finish_part)}


@app.post("/api/sim/hold")
async def sim_hold():
    _control(agent.sim.hold)
    return {"feed_hold": True}


@app.post("/api/sim/resume")
async def sim_resume():
    agent.sim.resume()
    return {"feed_hold": False}


@app.post("/api/sim/alarm")
async def sim_alarm(body: AlarmIn):
    agent.sim.alarm(body.code)
    return {"alarm": agent.alarm}


@app.post("/api/sim/alarm/clear")
async def sim_alarm_clear():
    agent.sim.clear_alarm()
    return {"alarm": None}


def _control(action, *args):
    """Run a control action; a state that doesn't allow it (no job, in alarm, ...) is a 409."""
    try:
        return action(*args)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


class ToolTableIn(BaseModel):
    tools: list[int]


class SlotIn(BaseModel):
    tool: int  # -1 to empty the slot


@app.put("/api/tool-table")
async def put_tool_table(body: ToolTableIn):
    return {"seq": _record(body.tools)}


@app.put("/api/slots/{index}")
async def put_slot(index: int, body: SlotIn):
    if not 1 <= index <= SLOT_COUNT:
        raise HTTPException(404, f"index must be 1-{SLOT_COUNT}")
    tools = list(agent.store.tool_table() or [EMPTY] * SLOT_COUNT)
    tools[index - 1] = body.tool
    return {"seq": _record(tools)}


def _record(tools: Sequence[int]) -> int:
    try:
        return agent.record_tool_table(list(tools))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


if cfg.kiosk_dir.is_dir():
    app.mount("/", StaticFiles(directory=cfg.kiosk_dir, html=True), name="kiosk")
