import { useState } from "react";
import type { PageState, Slot } from "./types";

/** Demo controls only appear with ?demo=1, so the production URL never shows them. */
export const demoMode = new URLSearchParams(window.location.search).has("demo");

const SPEEDS = [1, 20, 60];

async function post(path: string, body?: unknown): Promise<string> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  }).catch(() => null);
  if (!r) return "The machine agent isn't responding";
  if (r.ok) return "";
  const detail = (await r.json().catch(() => ({}))).detail;
  return typeof detail === "string" ? detail : `${r.status} ${r.statusText}`;
}

/** Load a tool into an index, as the machinist would. A tool that's in another index moves out of it,
 * since a tool can only be in one place; whatever was in the target index goes back to the crib. */
export async function loadTool(slots: Slot[], index: number, tool: number): Promise<string> {
  const table = slots
    .slice()
    .sort((a, b) => a.index - b.index)
    .map((s) => (s.index === index ? tool : s.tool === tool || s.tool === null ? -1 : s.tool));
  const r = await fetch("/api/tool-table", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ tools: table }),
  }).catch(() => null);
  return r?.ok ? "" : "Couldn't load that tool";
}

/** Stand-in for the machine's control panel, for demos: run the job, finish parts, hold, raise alarms.
 * Docked along the bottom of the screen, so it never covers the production page. */
export function DemoDrawer({ page }: { page: PageState }) {
  const [open, setOpen] = useState(true);
  const [problem, setProblem] = useState("");
  const session = page.session;
  const sim = page.sim;
  const run = async (path: string, body?: unknown) => setProblem(await post(path, body));

  if (!open) {
    return <button className="demo-tab" onClick={() => setOpen(true)}>Demo controls</button>;
  }
  const running = sim?.running ?? false;
  const cutting = session?.state === "cutting" || session?.state === "feed_hold";
  return (
    <aside className="demo" aria-label="Demo controls">
      <div className="demo-title">
        <h2>Demo controls</h2>
        <span className="demo-status">
          {!session ? "No job on the machine" : running ? `Running at ${sim!.speed}×` : "Machine stopped"}
        </span>
      </div>
      <div className="demo-group" role="group" aria-label="Run the machine">
        {SPEEDS.map((speed) => (
          <button key={speed} disabled={!session} aria-pressed={running && sim?.speed === speed}
            onClick={() => run("/api/sim/run", { speed })}>
            Run {speed}×
          </button>
        ))}
        <button disabled={!running} onClick={() => run("/api/sim/stop")}>Stop after this part</button>
      </div>
      <div className="demo-group">
        <button disabled={!session} onClick={() => run("/api/sim/part")}>Finish a part</button>
        {session?.state === "feed_hold"
          ? <button onClick={() => run("/api/sim/resume")}>Resume</button>
          : <button disabled={!cutting} onClick={() => run("/api/sim/hold")}>Feed hold</button>}
      </div>
      <div className="demo-group">
        {session?.alarm ? (
          <button onClick={() => run("/api/sim/alarm/clear")}>Clear alarm</button>
        ) : (
          <>
            <button disabled={!session} onClick={() => run("/api/sim/alarm", { code: "1010" })}>Alarm: door open</button>
            <button disabled={!session} onClick={() => run("/api/sim/alarm", { code: "108" })}>Alarm: servo overload</button>
          </>
        )}
      </div>
      {problem ? <p className="demo-problem" role="alert">{problem}</p>
        : <p className="demo-hint">Tap an orange or red pill to load that tool.</p>}
      <button className="demo-close" onClick={() => setOpen(false)} aria-label="Hide demo controls">Hide</button>
    </aside>
  );
}
