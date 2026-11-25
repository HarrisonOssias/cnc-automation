"""Backend API: the system of record for every machine.

- Pi agents save tool tables and fetch their bundle over REST (token per machine).
- Forge's web system (spoofed here) assigns jobs and edits config over REST.
- Any change to what a machine needs bumps its bundle version and sends a
  `bundle_changed` notice on that machine's MQTT `cmd` topic.
- Pi presence comes in on MQTT `status` topics (retained, with a Last Will).
- Part files (STEP, PDF, images) go to S3 (MinIO locally); Postgres keeps the index.
- The office gets live updates from `/live` (SSE), fed by Postgres LISTEN/NOTIFY (backend/live.py).
"""

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import paho.mqtt.client as mqtt
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from backend import live, storage
from backend.preview import render_step_png
from backend.sessions import close_for_job
from backend.suggest import suggest_part
from backend.db import (
    CoreToolSlot,
    JobRow,
    Machine,
    MachineEvent,
    MachinePresence,
    MachineStatus,
    Part,
    PartFile,
    PartSession,
    PartTool,
    SessionLocal,
    Tool,
    ToolTableSnapshot,
    init_db,
)
from forge_core import Bundle, Job, PageState, compute_page
from forge_core.page import validate_tool_table

log = logging.getLogger("backend")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "forge")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "forge")
RELEASES_DIR = Path(os.environ.get("RELEASES_DIR", "releases"))  # written by source/edge/build-release.sh
WEB_ORIGINS = os.environ.get("WEB_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")


# --- MQTT: presence in, bundle_changed notices out --------------------------


def on_status(client, userdata, msg: mqtt.MQTTMessage) -> None:
    machine_id = msg.topic.split("/")[2]
    try:
        status = json.loads(msg.payload)
    except ValueError:
        log.warning("bad status payload on %s", msg.topic)
        return
    with SessionLocal() as session:
        if not session.get(Machine, machine_id):
            return
        row = session.get(MachineStatus, machine_id) or MachineStatus(machine_id=machine_id, online=False)
        online, now = bool(status.get("online")), datetime.now().astimezone()
        if online != row.online:  # keep every flip, so offline time can be worked out later
            session.add(MachinePresence(machine_id=machine_id, online=online, at=now))
        row.online = online
        row.last_seen_at = now
        row.last_seq = status.get("last_seq", row.last_seq)
        row.agent_version = status.get("agent_version", row.agent_version)
        session.merge(row)
        session.commit()
    log.info("machine %s is %s", machine_id, "online" if status.get("online") else "offline")


mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="forge-backend")
mqttc.username_pw_set(MQTT_USER, MQTT_PASSWORD)
def on_connect(client, *_) -> None:
    client.subscribe("forge/machines/+/status", qos=1)  # live changes from every Pi
    # RabbitMQ doesn't send retained messages to wildcard subscriptions, so also subscribe to each
    # machine's exact topic: that delivers its last known status as soon as the backend (re)connects.
    with SessionLocal() as session:
        for machine_id in session.scalars(select(Machine.id)):
            client.subscribe(f"forge/machines/{machine_id}/status", qos=1)


mqttc.on_connect = on_connect
mqttc.on_message = on_status


def notify_bundle_changed(machine: Machine) -> None:
    payload = json.dumps({"type": "bundle_changed", "version": machine.bundle_version})
    mqttc.publish(f"forge/machines/{machine.id}/cmd", payload, qos=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    storage.ensure_bucket()
    threading.Thread(target=backfill_previews, daemon=True).start()  # STEP files uploaded before previews existed
    mqttc.connect_async(MQTT_HOST, MQTT_PORT)
    mqttc.loop_start()  # background thread; reconnects on its own
    listener = asyncio.create_task(live.listen())  # Postgres NOTIFY -> /live streams
    yield
    listener.cancel()
    mqttc.loop_stop()


app = FastAPI(title="Forge HAAS Talker backend", lifespan=lifespan)
# The office web app is served from its own origin (a separate server), so allow it explicitly.
app.add_middleware(CORSMiddleware, allow_origins=WEB_ORIGINS, allow_methods=["*"], allow_headers=["*"])


# --- Helpers ----------------------------------------------------------------


def db():
    with SessionLocal() as session:
        yield session


DB = Annotated[Session, Depends(db)]


def get_machine(session: Session, machine_id: str) -> Machine:
    machine = session.get(Machine, machine_id)
    if not machine:
        raise HTTPException(404, f"unknown machine {machine_id}")
    return machine


def agent_machine(machine_id: str, session: DB, x_machine_token: Annotated[str, Header()]) -> Machine:
    """Endpoints the Pi calls require that machine's token."""
    machine = get_machine(session, machine_id)
    if x_machine_token != machine.token:
        raise HTTPException(401, "bad machine token")
    return machine


def bump_and_notify(session: Session, machine: Machine) -> None:
    machine.bundle_version += 1
    session.commit()
    notify_bundle_changed(machine)


def part_tools(session: Session, part_number: str) -> list[int]:
    return list(
        session.scalars(
            select(PartTool.tool_number).where(PartTool.part_number == part_number).order_by(PartTool.sequence)
        )
    )


def part_files(session: Session, part_number: str) -> list[PartFile]:
    return list(
        session.scalars(select(PartFile).where(PartFile.part_number == part_number).order_by(PartFile.id))
    )


def part_image(session: Session, part_number: str) -> PartFile | None:
    """The part's picture: the latest uploaded photo, else the latest preview rendered from its STEP."""
    files = part_files(session, part_number)
    for kind in ("image", "preview"):
        matches = [f for f in files if f.kind == kind]
        if matches:
            return matches[-1]
    return None


def build_bundle(session: Session, machine: Machine) -> Bundle:
    core = session.execute(
        select(CoreToolSlot.index, CoreToolSlot.tool_number).where(CoreToolSlot.machine_id == machine.id)
    ).all()
    measured = session.scalars(select(Tool.tool_number).where(Tool.measured_at.is_not(None))).all()
    rows = session.execute(
        select(JobRow, Part.material, Part.cycle_s)
        .join(Part)
        .where(JobRow.machine_id == machine.id, JobRow.status == "queued")
        .order_by(JobRow.queue_pos, JobRow.id)
    ).all()
    jobs = []
    for job, material, cycle_s in rows:
        image = part_image(session, job.part_number)
        jobs.append(
            Job(
                id=job.id, order_number=job.order_number, line_number=job.line_number,
                part_number=job.part_number, quantity=job.quantity, material=material,
                machinist=job.machinist, due_date=job.due_date,
                required_tools=part_tools(session, job.part_number),
                image_file_id=image.id if image else None,
                image_sha256=image.sha256 if image else None,
                cycle_s=cycle_s,
            )
        )
    return Bundle(
        machine_id=machine.id,
        version=machine.bundle_version,
        core_tools=dict(core),
        measured_tools=list(measured),
        jobs=jobs,
    )


# --- Called by Pi agents ----------------------------------------------------


class ToolTableIn(BaseModel):
    seq: int
    tools: list[int]
    captured_at: datetime


@app.post("/machines/{machine_id}/tool-tables")
def save_tool_table(body: ToolTableIn, session: DB, machine: Annotated[Machine, Depends(agent_machine)]):
    """Idempotent on (machine, seq): a retried upload is stored once."""
    try:
        validate_tool_table(body.tools)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    result = session.execute(
        insert(ToolTableSnapshot)
        .values(machine_id=machine.id, seq=body.seq, tools=body.tools, captured_at=body.captured_at)
        .on_conflict_do_nothing(index_elements=["machine_id", "seq"])
    )
    session.commit()
    return {"seq": body.seq, "stored": result.rowcount == 1}


@app.get("/machines/{machine_id}/bundle", response_model=Bundle)
def get_bundle(session: DB, machine: Annotated[Machine, Depends(agent_machine)]):
    return build_bundle(session, machine)


@app.get("/machines/{machine_id}/release")
def get_release(machine: Annotated[Machine, Depends(agent_machine)]):
    """The Pi software release this machine should run (polled by source/edge/pi/update.sh).

    Today every machine gets the latest build; a per-machine pin or a canary group would go here.
    """
    manifest = RELEASES_DIR / "latest.json"
    if not manifest.exists():
        raise HTTPException(404, "no release published yet")
    release = json.loads(manifest.read_text())
    return {"version": release["version"], "url": f"/releases/files/{release['file']}", "sha256": release["sha256"]}


class CompletionIn(BaseModel):
    completed_at: datetime  # when the machinist tapped "Job complete" (may be earlier if the Pi was offline)


@app.post("/machines/{machine_id}/jobs/{job_id}/complete")
def complete_job(job_id: int, body: CompletionIn, session: DB, machine: Annotated[Machine, Depends(agent_machine)]):
    """The machinist finished this job at the machine. Idempotent: a retried upload changes nothing.

    A job the office canceled meanwhile stays canceled; the Pi drops its completion either way.
    """
    job = session.get(JobRow, job_id)
    if not job or job.machine_id != machine.id:
        raise HTTPException(404, "unknown job on this machine")
    if job.status == "queued":
        job.status, job.finished_at = "done", body.completed_at
        close_for_job(session, job.id, body.completed_at, "completed")  # in case session_end was lost
        bump_and_notify(session, machine)
    return {"id": job.id, "status": job.status}


@app.get("/machines/{machine_id}/files/{file_id}")
def get_file_for_machine(file_id: int, session: DB, machine: Annotated[Machine, Depends(agent_machine)]):
    """A part file, streamed from S3, for a part that's queued on this machine (the Pi caches images)."""
    f = session.get(PartFile, file_id)
    queued = f and session.scalar(
        select(JobRow.id)
        .where(JobRow.machine_id == machine.id, JobRow.status == "queued", JobRow.part_number == f.part_number)
        .limit(1)
    )
    if not queued:
        raise HTTPException(404, "no such file queued on this machine")
    return Response(storage.get(f.key), media_type=f.content_type)


# --- Read-only views --------------------------------------------------------


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/machines")
def list_machines(session: DB):
    rows = session.execute(select(Machine, MachineStatus).outerjoin(MachineStatus)).all()
    return [
        {
            "id": m.id,
            "name": m.name,
            "bundle_version": m.bundle_version,
            "online": bool(s and s.online),
            "last_seen_at": s.last_seen_at if s else None,
            "last_seq": s.last_seq if s else None,
            "agent_version": s.agent_version if s else None,  # what each Pi is running after OTA
        }
        for m, s in rows
    ]


@app.get("/machines/{machine_id}/page", response_model=PageState)
def get_page(machine_id: str, session: DB):
    """The same page the kiosk shows, computed from the latest saved tool table (for debugging/dashboards)."""
    machine = get_machine(session, machine_id)
    latest = session.scalar(
        select(ToolTableSnapshot)
        .where(ToolTableSnapshot.machine_id == machine_id)
        .order_by(ToolTableSnapshot.seq.desc())
        .limit(1)
    )
    if not latest:
        raise HTTPException(404, "no tool table received yet")
    return compute_page(latest.tools, build_bundle(session, machine))


# --- Called by Forge's web system (spoofed) ---------------------------------


@app.put("/machines/{machine_id}/core-tools")
def set_core_tools(machine_id: str, core_tools: dict[int, int], session: DB):
    machine = get_machine(session, machine_id)
    session.execute(delete(CoreToolSlot).where(CoreToolSlot.machine_id == machine_id))
    session.add_all(CoreToolSlot(machine_id=machine_id, index=i, tool_number=t) for i, t in core_tools.items())
    bump_and_notify(session, machine)
    return {"bundle_version": machine.bundle_version}


def open_jobs(session: Session, machine_id: str) -> list[JobRow]:
    """Queued jobs in run order. The first one is on the machine right now."""
    return list(
        session.scalars(
            select(JobRow)
            .where(JobRow.machine_id == machine_id, JobRow.status == "queued")
            .order_by(JobRow.queue_pos, JobRow.id)
        )
    )


def job_out(session: Session, j: JobRow) -> dict:
    return {
        "id": j.id, "order_number": j.order_number, "line_number": j.line_number, "quantity": j.quantity,
        "machinist": j.machinist, "due_date": j.due_date, "status": j.status, "finished_at": j.finished_at,
        "part": part_out(session, session.get(Part, j.part_number)),
    }


class JobIn(BaseModel):
    order_number: str
    line_number: int
    part_number: str
    quantity: int
    machinist: str
    due_date: date


@app.post("/machines/{machine_id}/jobs", status_code=201)
def create_job(machine_id: str, body: JobIn, session: DB):
    """New jobs go to the end of the queue, so they never replace the job on the machine."""
    machine = get_machine(session, machine_id)
    if not session.get(Part, body.part_number):
        raise HTTPException(422, f"unknown part {body.part_number}")
    last = session.scalar(select(func.max(JobRow.queue_pos)).where(JobRow.machine_id == machine_id))
    job = JobRow(machine_id=machine_id, **body.model_dump(), queue_pos=(last or 0) + 1)
    session.add(job)
    bump_and_notify(session, machine)
    return {"id": job.id, "bundle_version": machine.bundle_version}


@app.get("/machines/{machine_id}/jobs")
def list_jobs(machine_id: str, session: DB):
    """Office view: the open queue in run order (first = on the machine) and the last few finished jobs."""
    get_machine(session, machine_id)
    recent = session.scalars(
        select(JobRow)
        .where(JobRow.machine_id == machine_id, JobRow.status != "queued")
        .order_by(JobRow.finished_at.desc())
        .limit(5)
    )
    return {
        "open": [job_out(session, j) for j in open_jobs(session, machine_id)],
        "recent": [job_out(session, j) for j in recent],
    }


@app.get("/machines/{machine_id}/activity")
def machine_activity(machine_id: str, session: DB, limit: int = 20):
    """Latest machine events, as stored by the telemetry stream consumer (backend/telemetry.py)."""
    events = session.scalars(
        select(MachineEvent).where(MachineEvent.machine_id == machine_id).order_by(MachineEvent.at.desc()).limit(limit)
    )
    return [event_out(e) for e in events]


def event_out(e: MachineEvent) -> dict:
    return {
        "id": e.id, "type": e.type, "at": e.at, "data": e.data, "session_id": e.session_id,
        "stream_offset": e.stream_offset, "received_at": e.received_at,
    }


def session_out(row: PartSession) -> dict:
    """The same shape the NOTIFY trigger sends (the row without its tool table)."""
    return {c.name: getattr(row, c.name) for c in PartSession.__table__.columns if c.name != "tools"}


# --- Live updates for the office (SSE, fed by Postgres LISTEN/NOTIFY) -------


def live_snapshot(machine_id: str | None) -> dict:
    """Everything the office shows, for a stream to start from (and restart from after a resync)."""
    with SessionLocal() as session:
        machines = list_machines(session)
        ids = [m["id"] for m in machines if machine_id in (None, m["id"])]
        open_sessions = session.scalars(
            select(PartSession).where(PartSession.machine_id.in_(ids), PartSession.ended_at.is_(None))
        )
        return {
            "kind": "snapshot",
            "machines": machines,
            "jobs": {i: list_jobs(i, session) for i in ids},
            "events": {i: machine_activity(i, session, limit=30) for i in ids},
            "sessions": [session_out(s) for s in open_sessions],
        }


@app.get("/live")
async def live_updates(request: Request, machine_id: str | None = None):
    """Server-sent events: a snapshot, then every change Postgres NOTIFYs (events, sessions, queues,
    presence, files). `machine_id` limits it to one machine. Messages are JSON with a `kind`."""

    async def stream():
        queue = live.hub.subscribe()  # before the snapshot, so nothing slips between the two
        try:
            snapshot = await run_in_threadpool(live_snapshot, machine_id)
            yield f"data: {json.dumps(jsonable_encoder(snapshot))}\n\n"
            while not await request.is_disconnected():
                try:
                    target, payload = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if machine_id is None or target in (None, machine_id):
                    yield f"data: {payload}\n\n"
        finally:
            live.hub.unsubscribe(queue)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


class QueueIn(BaseModel):
    job_ids: list[int]  # the machine's open jobs, in the new run order


@app.put("/machines/{machine_id}/queue")
def reorder_queue(machine_id: str, body: QueueIn, session: DB):
    """Reorder what comes next. The job on the machine stays first until it's completed or canceled."""
    machine = get_machine(session, machine_id)
    jobs = open_jobs(session, machine_id)
    if sorted(body.job_ids) != sorted(j.id for j in jobs):
        raise HTTPException(422, "job_ids must list every open job on this machine exactly once")
    if jobs and body.job_ids[0] != jobs[0].id:
        raise HTTPException(
            409, f"job {jobs[0].order_number} - #{jobs[0].line_number} is on the machine; "
            "it stays first until the machinist completes it or you cancel it",
        )
    by_id = {j.id: j for j in jobs}
    for pos, job_id in enumerate(body.job_ids, start=1):
        by_id[job_id].queue_pos = pos
    bump_and_notify(session, machine)
    return {"bundle_version": machine.bundle_version}


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, session: DB):
    """The office's only way to take a job off the machine. Completion comes from the machinist."""
    job = session.get(JobRow, job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    if job.status != "queued":
        raise HTTPException(409, f"job is already {job.status}")
    job.status, job.finished_at = "canceled", datetime.now().astimezone()
    close_for_job(session, job.id, job.finished_at, "canceled")
    bump_and_notify(session, get_machine(session, job.machine_id))
    return job_out(session, job)


# --- Parts and their files (S3) ---------------------------------------------


def file_kind(filename: str, content_type: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in (".step", ".stp"):
        return "model"
    if ext == ".pdf":
        return "drawing"
    return "image" if content_type.startswith("image/") else "other"


def file_out(f: PartFile) -> dict:
    return {
        "id": f.id, "kind": f.kind, "filename": f.filename, "content_type": f.content_type,
        "size": f.size, "sha256": f.sha256, "uploaded_at": f.uploaded_at,
        "download_url": f"/files/{f.id}/download",
    }


def part_out(session: Session, part: Part) -> dict:
    return {
        "part_number": part.part_number,
        "material": part.material,
        "cycle_s": part.cycle_s,
        "tools": part_tools(session, part.part_number),
        "files": [file_out(f) for f in part_files(session, part.part_number)],
    }


@app.get("/parts")
def list_parts(session: DB):
    return [part_out(session, p) for p in session.scalars(select(Part).order_by(Part.part_number))]


@app.get("/parts/suggest")
def suggest_from_file(filename: str, session: DB):
    """Auto-fill for the office app: guess the part from a STEP file name and the parts on record."""
    parts = [
        {**part_out(session, p), "files": [f.filename for f in part_files(session, p.part_number)]}
        for p in session.scalars(select(Part))
    ]
    return suggest_part(filename, parts)


@app.put("/parts/{part_number}")
def upsert_part(
    part_number: str,
    session: DB,
    background: BackgroundTasks,
    material: Annotated[str, Form()],
    tools: Annotated[str, Form(description="Tools the CAM program calls, e.g. 'T99, T120, T130'")] = "",
    files: Annotated[list[UploadFile], File()] = [],
    cycle_s: Annotated[int | None, Form(description="Expected cycle time for one part, in seconds")] = None,
):
    """Create or update a part, like an RFQ upload: STEP model, PDF drawing, images, plus its tool list."""
    part = session.get(Part, part_number) or Part(part_number=part_number, material=material)
    part.material = material
    if cycle_s is not None:
        part.cycle_s = cycle_s
    session.add(part)
    session.flush()

    tool_numbers = list(dict.fromkeys(int(t) for t in re.findall(r"\d+", tools)))
    if tool_numbers:
        session.execute(delete(PartTool).where(PartTool.part_number == part_number))
        session.add_all(PartTool(part_number=part_number, tool_number=t, sequence=i) for i, t in enumerate(tool_numbers))

    known = {f.sha256 for f in part_files(session, part_number)}
    new_models: list[PartFile] = []
    for upload in files:
        data = upload.file.read()
        sha256 = hashlib.sha256(data).hexdigest()
        if sha256 in known:
            continue  # same file uploaded again
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(upload.filename or "file").name)
        content_type = upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        key = f"parts/{part_number}/{sha256[:12]}-{filename}"
        storage.put(key, data, content_type)
        row = PartFile(
            part_number=part_number, kind=file_kind(filename, content_type), key=key, filename=filename,
            content_type=content_type, size=len(data), sha256=sha256,
        )
        session.add(row)
        if row.kind == "model":
            new_models.append(row)
        known.add(sha256)
    session.commit()
    for model in new_models:  # render after responding; machines are notified when the picture is ready
        background.add_task(make_preview, model.id)
    notify_machines_with(session, part_number)  # they need the new tools or picture
    return part_out(session, part)


def notify_machines_with(session: Session, part_number: str) -> None:
    machine_ids = session.scalars(
        select(JobRow.machine_id).where(JobRow.part_number == part_number, JobRow.status == "queued").distinct()
    )
    for machine_id in list(machine_ids):
        bump_and_notify(session, get_machine(session, machine_id))


# --- Previews rendered from STEP files --------------------------------------


def preview_filename(model_filename: str) -> str:
    return f"{Path(model_filename).stem}-preview.png"


def make_preview(model_file_id: int) -> None:
    """Render a STEP file to a PNG, store it in S3 as the part's `preview`, and tell the machines."""
    with SessionLocal() as session:
        model = session.get(PartFile, model_file_id)
        if not model:
            return
        name = preview_filename(model.filename)
        if any(f.filename == name for f in part_files(session, model.part_number)):
            return
        try:
            png = render_step_png(storage.get(model.key))
        except Exception as e:  # a STEP with no usable geometry shouldn't break uploads
            log.warning("could not render a preview of %s: %s", model.filename, e)
            return
        sha256 = hashlib.sha256(png).hexdigest()
        key = f"parts/{model.part_number}/{sha256[:12]}-{name}"
        storage.put(key, png, "image/png")
        session.add(
            PartFile(
                part_number=model.part_number, kind="preview", key=key, filename=name,
                content_type="image/png", size=len(png), sha256=sha256,
            )
        )
        session.commit()
        log.info("rendered %s for %s", name, model.part_number)
        notify_machines_with(session, model.part_number)


def backfill_previews() -> None:
    with SessionLocal() as session:
        models = session.scalars(select(PartFile).where(PartFile.kind == "model")).all()
        have = {(f.part_number, f.filename) for f in session.scalars(select(PartFile).where(PartFile.kind == "preview"))}
    for model in models:
        if (model.part_number, preview_filename(model.filename)) not in have:
            make_preview(model.id)


@app.get("/files/{file_id}/download")
def download_file(file_id: int, session: DB):
    """Redirect to a short-lived signed S3 link, the same way the real site would serve files."""
    f = session.get(PartFile, file_id)
    if not f:
        raise HTTPException(404, "unknown file")
    return RedirectResponse(storage.download_url(f.key, f.filename), status_code=307)


# Release tarballs for Pi OTA updates. Integrity comes from the sha256 in the authenticated manifest.
app.mount("/releases/files", StaticFiles(directory=RELEASES_DIR, check_dir=False), name="releases")
