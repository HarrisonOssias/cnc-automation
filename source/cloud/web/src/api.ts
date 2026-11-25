// Typed calls to the backend API (a separate server from this web app).

export const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export interface Machine {
  id: string;
  name: string;
  online: boolean;
  last_seen_at: string | null;
  agent_version: string | null;
}

export interface PartFile {
  id: number;
  kind: "model" | "drawing" | "image" | "preview" | "other"; // preview = rendered from the STEP by the backend
  filename: string;
  size: number;
  download_url: string; // redirects to a short-lived signed S3 link
}

export interface Part {
  part_number: string;
  material: string;
  tools: number[];
  files: PartFile[];
}

export interface QueuedJob {
  id: number;
  order_number: string;
  line_number: number;
  quantity: number;
  machinist: string;
  due_date: string;
  status: "queued" | "done" | "canceled"; // done = completed by the machinist at the machine
  finished_at: string | null;
  part: Part;
}

export interface MachineJobs {
  open: QueuedJob[]; // run order; the first is on the machine and can't be moved, only canceled
  recent: QueuedJob[];
}

// A machine event from the telemetry stream (sessions, tool changes, cycles, parts, alarms).
export interface MachineEvent {
  id: number;
  session_id: string | null; // the part session it happened in
  received_at: string; // stored in Postgres by the telemetry consumer
  notified_at?: string; // NOTIFY sent (live messages only)
  browser_at?: string; // arrived in this browser (live messages only)
  type:
    | "session_start" | "session_end" | "tool_change" | "cycle_start" | "cycle_stop" | "part_complete"
    | "quantity_reached" | "alarm" | "alarm_clear" | "feed_hold" | "resume" | "job_complete";
  at: string;
  data: {
    order?: string;
    part_number?: string;
    made?: number;
    quantity?: number;
    cycle_s?: number;
    code?: string;
    message?: string;
    reason?: string;
    seq?: number;
    changes?: { index: number; from: number | null; to: number | null }[];
  };
  stream_offset: number;
}

// A part session: one job's time on one machine, from setup to Job complete or cancel.
export interface PartSession {
  id: string;
  machine_id: string;
  job_id: number;
  part_number: string;
  order_ref: string;
  quantity: number;
  expected_cycle_s: number | null;
  started_at: string;
  first_cycle_at: string | null;
  ended_at: string | null;
  end_reason: "completed" | "canceled" | null;
  state: "setup" | "cutting" | "idle" | "feed_hold" | "alarm" | "ended";
  alarm: string | null;
  made: number;
  cycles: number;
  last_cycle_s: number | null;
  avg_cycle_s: number | null;
  min_cycle_s: number | null;
  max_cycle_s: number | null;
  setup_s: number;
  cut_s: number;
  idle_s: number;
  hold_s: number;
  alarm_s: number;
  alarms: number;
}

// The backend's guess for a part, from a STEP file name and the parts on record.
export interface PartSuggestion {
  match: boolean; // true: an existing part; false: a new part number taken from the file name
  score: number;
  reason: string;
  part_number: string;
  material: string;
  tools: number[];
}

export interface NewJob {
  order_number: string;
  line_number: number;
  part_number: string;
  quantity: number;
  machinist: string;
  due_date: string;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_URL}${path}`, init);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `${r.status} ${r.statusText}`);
  }
  return r.json();
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

export const fileUrl = (f: PartFile) => `${API_URL}${f.download_url}`;

export const api = {
  machines: () => call<Machine[]>("/machines"),
  parts: () => call<Part[]>("/parts"),
  suggestPart: (filename: string) => call<PartSuggestion>(`/parts/suggest?filename=${encodeURIComponent(filename)}`),
  jobs: (machineId: string) => call<MachineJobs>(`/machines/${machineId}/jobs`),
  activity: (machineId: string) => call<MachineEvent[]>(`/machines/${machineId}/activity`),

  savePart(partNumber: string, material: string, tools: string, files: File[]) {
    const form = new FormData();
    form.append("material", material);
    form.append("tools", tools);
    files.forEach((f) => form.append("files", f));
    return call<Part>(`/parts/${encodeURIComponent(partNumber)}`, { method: "PUT", body: form });
  },
  createJob: (machineId: string, job: NewJob) => call(`/machines/${machineId}/jobs`, json("POST", job)),
  reorder: (machineId: string, jobIds: number[]) => call(`/machines/${machineId}/queue`, json("PUT", { job_ids: jobIds })),
  cancel: (jobId: number) => call(`/jobs/${jobId}/cancel`, { method: "POST" }),
};
