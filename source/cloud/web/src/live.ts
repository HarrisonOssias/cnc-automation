// Live updates from the backend: one EventSource on /live instead of polling.
// The backend LISTENs to Postgres NOTIFY (triggers on events, sessions, jobs, presence, files) and
// forwards each change here. Every (re)connect starts with a full snapshot.

import { useEffect, useRef, useState } from "react";
import { API_URL, api, type Machine, type MachineEvent, type MachineJobs, type PartSession } from "./api";

export interface LiveState {
  connected: boolean;
  machines: Machine[];
  jobs: Record<string, MachineJobs>;
  events: Record<string, MachineEvent[]>; // newest first
  sessions: Record<string, PartSession>; // by session id; open ones, plus any that closed while watching
}

type Message =
  | { kind: "snapshot"; machines: Machine[]; jobs: Record<string, MachineJobs>; events: Record<string, MachineEvent[]>; sessions: PartSession[] }
  | { kind: "event"; machine_id: string; event?: MachineEvent; truncated?: boolean }
  | { kind: "session"; machine_id: string; session: PartSession }
  | { kind: "jobs"; machine_id: string }
  | { kind: "status"; machine_id: string; online: boolean; agent_version: string | null; last_seen_at: string | null }
  | { kind: "files"; part_number: string }
  | { kind: "resync" };

const EVENTS_KEPT = 50;
const EMPTY: LiveState = { connected: false, machines: [], jobs: {}, events: {}, sessions: {} };

export function useLive(): LiveState {
  const [state, setState] = useState<LiveState>(EMPTY);
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    let source: EventSource;
    let closed = false;
    const refetchJobs = (machineId: string) =>
      api.jobs(machineId).then((jobs) => setState((s) => ({ ...s, jobs: { ...s.jobs, [machineId]: jobs } }))).catch(() => {});

    const handle = (msg: Message) => {
      switch (msg.kind) {
        case "snapshot":
          setState({
            connected: true, machines: msg.machines, jobs: msg.jobs, events: msg.events,
            sessions: Object.fromEntries(msg.sessions.map((s) => [s.id, s])),
          });
          break;
        case "event":
          if (!msg.event) return; // too big to send; it shows up on the next snapshot
          setState((s) => {
            const list = s.events[msg.machine_id] ?? [];
            if (list.some((e) => e.stream_offset === msg.event!.stream_offset)) return s;
            const event = { ...msg.event!, browser_at: new Date().toISOString() };
            return { ...s, events: { ...s.events, [msg.machine_id]: [event, ...list].slice(0, EVENTS_KEPT) } };
          });
          break;
        case "session":
          setState((s) => ({ ...s, sessions: { ...s.sessions, [msg.session.id]: msg.session } }));
          break;
        case "jobs":
          refetchJobs(msg.machine_id);
          break;
        case "status":
          setState((s) => ({
            ...s,
            machines: s.machines.map((m) => m.id === msg.machine_id
              ? { ...m, online: msg.online, agent_version: msg.agent_version, last_seen_at: msg.last_seen_at }
              : m),
          }));
          break;
        case "files": // a new file or rendered preview: the queues show part files
          stateRef.current.machines.forEach((m) => refetchJobs(m.id));
          break;
        case "resync":
          source.close();
          connect();
          break;
      }
    };

    const connect = () => {
      if (closed) return;
      source = new EventSource(`${API_URL}/live`); // reconnects by itself after a network error
      source.onmessage = (e) => handle(JSON.parse(e.data));
      source.onerror = () => setState((s) => ({ ...s, connected: false }));
    };
    connect();
    return () => {
      closed = true;
      source.close();
    };
  }, []);

  return state;
}

/** The open session for a job, if the machine is working on it. */
export function sessionForJob(live: LiveState, jobId: number): PartSession | undefined {
  return Object.values(live.sessions).find((s) => s.job_id === jobId && s.ended_at === null);
}
