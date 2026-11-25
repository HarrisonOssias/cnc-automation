import { useEffect, useState, type DragEvent, type FormEvent } from "react";
import {
  api, fileUrl, type Machine, type MachineEvent, type MachineJobs, type Part, type PartFile, type PartSession,
  type QueuedJob,
} from "./api";
import { sessionForJob, useLive, type LiveState } from "./live";

type View = "new" | "queue";

export function App() {
  const [view, setView] = useState<View>("queue");
  const live = useLive();
  const machines = live.machines;

  return (
    <div className="layout">
      <nav className="sidebar">
        <div className="brand">Jobshop office<small>demo web system</small></div>
        <button className={view === "new" ? "active" : ""} onClick={() => setView("new")}>New job</button>
        <button className={view === "queue" ? "active" : ""} onClick={() => setView("queue")}>Job queue</button>
        <div className={`live-status ${live.connected ? "on" : "off"}`}>
          {live.connected ? "Live updates on" : "Reconnecting…"}
        </div>
      </nav>
      <main className="content">
        {!live.connected && (
          <div className="error">Can't reach the backend's live updates. Retrying; the page fills in once it's back.</div>
        )}
        {view === "new" ? (
          <NewJob machines={machines} onCreated={() => setView("queue")} />
        ) : (
          machines.map((m) => <MachineQueue key={m.id} machine={m} live={live} />)
        )}
      </main>
    </div>
  );
}

// --- New job: upload part files + fill the part and job forms -------------------

const today = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD in local time

function NewJob({ machines, onCreated }: { machines: Machine[]; onCreated: () => void }) {
  const [files, setFiles] = useState<File[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const [form, setForm] = useState({
    part_number: "", material: "", tools: "", machine_id: "", order_number: "", line_number: "1",
    quantity: "", machinist: "", due_date: today(),
  });
  const [problem, setProblem] = useState("");
  const [saving, setSaving] = useState(false);
  const [autofillNote, setAutofillNote] = useState("");
  const stepFile = files.find((f) => kindOf(f.name) === "model");

  useEffect(() => { api.parts().then(setParts).catch(() => {}); }, []);
  const existing = parts.find((p) => p.part_number === form.part_number.trim());
  const machineId = form.machine_id || machines[0]?.id || "";

  const set = (field: keyof typeof form) => (e: { target: { value: string } }) => {
    setProblem("");
    const value = e.target.value;
    setForm((f) => {
      const next = { ...f, [field]: value };
      const match = field === "part_number" && parts.find((p) => p.part_number === value.trim());
      if (match) return { ...next, material: match.material, tools: match.tools.map((t) => `T${t}`).join(", ") };
      return next;
    });
  };

  async function autofill() {
    if (!stepFile) return;
    setProblem("");
    try {
      const s = await api.suggestPart(stepFile.name);
      setForm((f) => ({
        ...f,
        part_number: s.part_number,
        material: s.material || f.material,
        tools: s.tools.length ? s.tools.map((t) => `T${t}`).join(", ") : f.tools,
      }));
      setAutofillNote(s.match
        ? `Matched ${s.part_number}: ${s.reason}.`
        : `New part: ${s.reason}. Add the material and the tools from the CAM program.`);
    } catch (err) {
      setProblem((err as Error).message);
    }
  }

  const addFiles = (list: FileList | null) => list && setFiles((f) => [...f, ...Array.from(list)]);
  const onDrop = (e: DragEvent) => { e.preventDefault(); addFiles(e.dataTransfer.files); };

  async function submit(e: FormEvent) {
    e.preventDefault();
    const missing = (["part_number", "material", "order_number", "quantity", "machinist", "due_date"] as const)
      .filter((k) => !form[k].trim());
    if (missing.length) return setProblem(`Fill in: ${missing.join(", ").replaceAll("_", " ")}`);
    if (!existing && !form.tools.trim()) return setProblem("A new part needs its tool list (from the CAM program)");
    if (!machineId) return setProblem("No machine to assign the job to");
    setSaving(true);
    try {
      await api.savePart(form.part_number.trim(), form.material.trim(), form.tools, files);
      await api.createJob(machineId, {
        order_number: form.order_number.trim(), line_number: Number(form.line_number), part_number: form.part_number.trim(),
        quantity: Number(form.quantity), machinist: form.machinist.trim(), due_date: form.due_date,
      });
      onCreated();
    } catch (err) {
      setProblem((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={submit}>
      <h1>New job</h1>
      <p className="lead">Upload the part's STEP model, drawing and a picture, then queue it on a machine.</p>

      <label className="dropzone" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
        <input type="file" multiple accept=".step,.stp,.pdf,image/*" onChange={(e) => addFiles(e.target.files)} hidden />
        Drag STEP, PDF, or image files here or <span className="link">browse</span>
      </label>
      {files.length > 0 && (
        <ul className="files">
          {files.map((f, i) => (
            <li key={i}>
              <span className={`kind ${kindOf(f.name)}`}>{kindOf(f.name)}</span> {f.name}
              <button type="button" className="ghost" onClick={() => setFiles(files.filter((_, j) => j !== i))}>remove</button>
            </li>
          ))}
        </ul>
      )}

      <div className="panels">
        <fieldset>
          <legend>Part</legend>
          <button type="button" className="autofill" disabled={!stepFile} onClick={autofill}
            title={stepFile ? `Fill in from ${stepFile.name}` : "Add a STEP file first"}>
            Auto-fill from STEP
          </button>
          {autofillNote && <p className="hint">{autofillNote}</p>}
          <Field label="Part number"><input value={form.part_number} onChange={set("part_number")} list="parts" placeholder="BRKT-303" /></Field>
          <datalist id="parts">{parts.map((p) => <option key={p.part_number} value={p.part_number} />)}</datalist>
          {existing && <p className="hint">Existing part: {existing.files.length} file(s) on record. New files are added.</p>}
          <Field label="Material"><input value={form.material} onChange={set("material")} placeholder="303 SUS" /></Field>
          <Field label="Tools (from the CAM program)"><input value={form.tools} onChange={set("tools")} placeholder="T99, T120, T130" /></Field>
        </fieldset>
        <fieldset>
          <legend>Job</legend>
          <Field label="Machine">
            <select value={machineId} onChange={set("machine_id")}>
              {machines.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          </Field>
          <div className="row">
            <Field label="Order #"><input value={form.order_number} onChange={set("order_number")} placeholder="1616" /></Field>
            <Field label="Line #"><input type="number" min="1" value={form.line_number} onChange={set("line_number")} /></Field>
            <Field label="Quantity"><input type="number" min="1" value={form.quantity} onChange={set("quantity")} /></Field>
          </div>
          <div className="row">
            <Field label="Machinist"><input value={form.machinist} onChange={set("machinist")} /></Field>
            <Field label="Due date"><input type="date" value={form.due_date} onChange={set("due_date")} /></Field>
          </div>
        </fieldset>
      </div>

      {problem && <div className="error">{problem}</div>}
      <button className="primary" disabled={saving}>{saving ? "Uploading…" : "Queue job"}</button>
    </form>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="field"><span>{label}</span>{children}</label>;
}

function kindOf(name: string) {
  const ext = name.toLowerCase().split(".").pop() ?? "";
  if (ext === "step" || ext === "stp") return "model";
  if (ext === "pdf") return "drawing";
  return ["png", "jpg", "jpeg", "webp", "gif", "svg"].includes(ext) ? "image" : "other";
}

// --- Job queue: one machine's open jobs, in run order --------------------------

const NO_JOBS: MachineJobs = { open: [], recent: [] };

function MachineQueue({ machine, live }: { machine: Machine; live: LiveState }) {
  const jobs = live.jobs[machine.id] ?? NO_JOBS;
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");

  // The queue updates itself: Postgres NOTIFYs the backend when the job rows change.
  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    setProblem("");
    try { await action(); } catch (e) { setProblem((e as Error).message); } finally { setBusy(false); }
  }
  // Only the jobs after the running one can be reordered; the running job stays first.
  const move = (i: number, by: number) => {
    const ids = jobs.open.map((j) => j.id);
    [ids[i], ids[i + by]] = [ids[i + by], ids[i]];
    act(() => api.reorder(machine.id, ids));
  };
  const cancel = (job: QueuedJob, running: boolean) => {
    const what = `${job.order_number} - #${job.line_number}`;
    const ok = window.confirm(running
      ? `Cancel ${what}? It's on the machine now; the kiosk will switch to the next job.`
      : `Cancel ${what}?`);
    if (ok) act(() => api.cancel(job.id));
  };

  return (
    <section className="machine">
      <header>
        <h1>{machine.name}</h1>
        <span className={`status ${machine.online ? "on" : "off"}`}>{machine.online ? "Pi online" : "Pi offline"}</span>
        {machine.agent_version && <span className="muted">agent {machine.agent_version}</span>}
      </header>
      {problem && <div className="error">{problem}</div>}
      {jobs.open.length === 0 && <p className="muted">No jobs queued. Add one under New job.</p>}
      <ol className="queue">
        {jobs.open.map((job, i) => {
          const running = i === 0;
          const image = job.part.files.filter((f) => f.kind === "image").at(-1)
            ?? job.part.files.filter((f) => f.kind === "preview").at(-1);
          return (
            <li key={job.id} className={running ? "running" : ""}>
              <div className="thumb">{image ? <img src={fileUrl(image)} alt={job.part.part_number} /> : "no image"}</div>
              <div className="info">
                <div className="title">
                  {job.order_number} - #{job.line_number} <span className="muted">{job.part.part_number}</span>
                  {running && <span className="tag">On the machine</span>}
                  {i === 1 && <span className="tag next">Up next</span>}
                </div>
                <div>{job.quantity} x {job.part.material} · {job.machinist} · due {job.due_date}</div>
                <div className="muted">Tools: {job.part.tools.map((t) => `T${t}`).join(", ")}</div>
                <div className="links">{job.part.files.map((f) => <FileLink key={f.id} file={f} />)}</div>
                {running && <SessionLine session={sessionForJob(live, job.id)} />}
                {running && <div className="hint">Locked until the machinist completes it at the machine, or you cancel it.</div>}
              </div>
              <div className="actions">
                {!running && (
                  <>
                    <button disabled={busy || i === 1} onClick={() => move(i, -1)} aria-label="Move up">↑</button>
                    <button disabled={busy || i === jobs.open.length - 1} onClick={() => move(i, 1)} aria-label="Move down">↓</button>
                  </>
                )}
                <button className="danger" disabled={busy} onClick={() => cancel(job, running)}>Cancel</button>
              </div>
            </li>
          );
        })}
      </ol>
      <Activity events={live.events[machine.id] ?? []} />
      {jobs.recent.length > 0 && (
        <>
          <h2>Recently finished</h2>
          <ul className="recent">
            {jobs.recent.map((job) => (
              <li key={job.id}>
                <span className={`tag ${job.status}`}>{job.status === "done" ? "Completed at machine" : "Canceled"}</span>
                {job.order_number} - #{job.line_number} <span className="muted">{job.part.part_number} · {job.machinist}</span>
                <span className="muted when">{job.finished_at && new Date(job.finished_at).toLocaleString()}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

const STATE_LABELS: Record<PartSession["state"], string> = {
  setup: "Setup", cutting: "Cutting", idle: "Idle", feed_hold: "Feed hold", alarm: "Alarm", ended: "Ended",
};

const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

/** The running job's part session, as the machine reports it (pushed live, never polled). */
function SessionLine({ session }: { session?: PartSession }) {
  if (!session) return <div className="muted">Waiting for the machine to start a session</div>;
  return (
    <div className="session-line">
      <span className={`state state-${session.state}`}>{STATE_LABELS[session.state]}</span>
      <strong>{session.made} of {session.quantity} made</strong>
      {session.alarm && <span className="alarm-text">{session.alarm}</span>}
      {session.avg_cycle_s !== null && (
        <span className="muted">
          avg cycle {mmss(session.avg_cycle_s)}{session.expected_cycle_s ? ` (expected ${mmss(session.expected_cycle_s)})` : ""}
        </span>
      )}
    </div>
  );
}

/** Live machine events: Pi -> MQTT -> RabbitMQ stream -> telemetry consumer -> Postgres -> NOTIFY -> here. */
function Activity({ events }: { events: MachineEvent[] }) {
  if (events.length === 0) return null;
  return (
    <>
      <h2>Machine activity</h2>
      <ul className="recent activity">
        {events.slice(0, 8).map((e) => (
          <li key={e.stream_offset}>
            <span className={`tag ev-${e.type}`}>{EVENT_LABELS[e.type]}</span>
            {describe(e)}
            <span className="muted when">{new Date(e.at).toLocaleTimeString()}</span>
          </li>
        ))}
      </ul>
    </>
  );
}

const EVENT_LABELS: Record<MachineEvent["type"], string> = {
  session_start: "Session start", session_end: "Session end", tool_change: "Tool change",
  cycle_start: "Cycle start", cycle_stop: "Cycle stop", part_complete: "Part done", quantity_reached: "Quantity made",
  alarm: "Alarm", alarm_clear: "Alarm cleared", feed_hold: "Feed hold", resume: "Resume", job_complete: "Job complete",
};

function describe(e: MachineEvent): string {
  if (e.type === "tool_change") {
    const slot = (t: number | null) => (t ? `T${t}` : "empty");
    return (e.data.changes ?? []).map((c) => `index ${c.index}: ${slot(c.from)} → ${slot(c.to)}`).join(", ");
  }
  if (e.type === "part_complete") return `${e.data.made} of ${e.data.quantity} · ${e.data.cycle_s} s`;
  if (e.type === "alarm") return `${e.data.code} ${e.data.message}`;
  if (e.type === "session_end") return `${e.data.reason}, ${e.data.made} made`;
  return e.data.order ? `${e.data.order} ${e.data.part_number ?? ""}` : "";
}

function FileLink({ file }: { file: PartFile }) {
  return (
    <a href={fileUrl(file)} target="_blank" rel="noreferrer">
      <span className={`kind ${file.kind}`}>{file.kind}</span> {file.filename} <span className="muted">({Math.ceil(file.size / 1024)} KB)</span>
    </a>
  );
}
