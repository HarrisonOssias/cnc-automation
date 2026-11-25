// Mirrors source/shared/forge_core/models.py (PageState and friends). Keep the two in sync.

export type SlotState = "in_use" | "idle" | "empty" | "core_ok" | "core_wrong";

export interface Slot {
  index: number;
  tool: number | null;
  core: boolean;
  state: SlotState;
  measured: boolean;
  in_use: boolean;
  swap_to: number | null;
}

export interface Job {
  id: number;
  order_number: string;
  line_number: number;
  part_number: string;
  quantity: number;
  material: string;
  machinist: string;
  due_date: string; // YYYY-MM-DD
  required_tools: number[];
  image_file_id: number | null;
  image_sha256: string | null;
  cycle_s: number | null; // expected cycle time for one part
}

export interface SyncStatus {
  online: boolean;
  offline_since: string | null;
  bundle_fetched_at: string | null;
  pending_uploads: number;
}

export type MachineState = "setup" | "cutting" | "idle" | "feed_hold" | "alarm";

/** A part session: one job's time on this machine, from setup to Job complete (or cancel). */
export interface Session {
  id: string;
  job_id: number;
  opened_at: string;
  first_cycle_at: string | null;
  made: number;
  quantity: number;
  last_cycle_s: number | null; // machine time (scaled by the simulator's speed in demos)
  avg_cycle_s: number | null;
  expected_cycle_s: number | null;
  cycle_started_at: string | null; // set while a cycle runs
  state: MachineState;
  alarm: string | null; // "1010 Door open" while in alarm
}

export interface Alert {
  id: number;
  at: string;
  level: "info" | "done" | "alarm";
  text: string;
}

export interface SimStatus {
  running: boolean;
  speed: number;
}

export interface PageState {
  machine_id: string;
  slots: Slot[];
  job: Job | null;
  up_next: Job[];
  unplaced_tools: number[];
  sync: SyncStatus | null;
  cached_images: string[]; // sha256s served by the agent at /api/images/<sha256>
  session: Session | null; // the current job's part session
  alerts: Alert[]; // newest last
  sim: SimStatus | null;
}
