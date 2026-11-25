import { useEffect } from "react";
import { clock, clockDuration, roughDuration, secondsBetween } from "./time";
import type { MachineState, PageState, Session, Slot } from "./types";

const STATE_LABEL: Record<MachineState, string> = {
  setup: "Setup", cutting: "Cutting", idle: "Idle", feed_hold: "Feed hold", alarm: "Alarm",
};

/** Machine time runs faster than the wall clock while the demo simulator is compressing time. */
export const timeScale = (page: PageState) => (page.sim?.running ? page.sim.speed : 1);

function cycleSeconds(session: Session, now: number, scale: number): number | null {
  return session.cycle_started_at ? secondsBetween(session.cycle_started_at, now) * scale : null;
}

/** The machine's state as a stack-light-coloured strip, across the top of the parts column. */
export function StateStrip({ session, now, scale }: { session: Session; now: number; scale: number }) {
  const cycle = cycleSeconds(session, now, scale);
  let detail = "";
  if (session.state === "alarm") detail = session.alarm ?? "";
  else if (session.state === "feed_hold") detail = "Cycle paused";
  else if (session.state === "cutting" && cycle !== null) {
    detail = `Cycle ${clockDuration(cycle)}${session.expected_cycle_s ? ` of ${clockDuration(session.expected_cycle_s)}` : ""}`;
  } else if (session.state === "setup") detail = `for ${roughDuration(secondsBetween(session.opened_at, now))}`;
  else if (session.made >= session.quantity) detail = "All parts made";
  else detail = "Between parts";
  return (
    <div className={`state-strip ${session.state}`} role="status">
      <span className="state-label">{STATE_LABEL[session.state]}</span>
      <span className="state-detail">{detail}</span>
    </div>
  );
}

/** Parts made against the quantity, the cycle in progress, and cycle times. On the current job card. */
export function SessionProgress({ session, now, scale }: { session: Session; now: number; scale: number }) {
  const { made, quantity } = session;
  const cycle = cycleSeconds(session, now, scale);
  const sweep = cycle !== null && session.expected_cycle_s ? Math.min(1, cycle / session.expected_cycle_s) : null;
  const perPart = session.avg_cycle_s ?? session.expected_cycle_s;
  const left = perPart && made < quantity ? (quantity - made) * perPart : null;
  const setup = session.first_cycle_at
    ? `setup took ${roughDuration((Date.parse(session.first_cycle_at) - Date.parse(session.opened_at)) / 1000)}`
    : "in setup";

  return (
    <div className="session">
      <div className="made">
        <span className="made-count">{made}</span> of {quantity} made
        {left !== null && <span className="time-left">about {roughDuration(left)} left</span>}
      </div>
      <Tally made={made} quantity={quantity} sweep={session.state === "cutting" || session.state === "feed_hold" ? sweep : null} />
      <dl className="cycle-stats">
        <div><dt>Last cycle</dt><dd>{session.last_cycle_s ? clockDuration(session.last_cycle_s) : "–"}</dd></div>
        <div><dt>Average</dt><dd>{session.avg_cycle_s ? clockDuration(session.avg_cycle_s) : "–"}</dd></div>
        <div><dt>Expected</dt><dd>{session.expected_cycle_s ? clockDuration(session.expected_cycle_s) : "–"}</dd></div>
      </dl>
      <div className="session-meta">Session started {clock(session.opened_at)}, {setup}</div>
    </div>
  );
}

/** One cell per part (like a tally sheet) up to 40 parts, a plain bar beyond that.
 * The thin line underneath fills as the part being cut now gets through its expected cycle. */
function Tally({ made, quantity, sweep }: { made: number; quantity: number; sweep: number | null }) {
  const label = `${made} of ${quantity} parts made`;
  return (
    <div className="tally-wrap">
      {quantity <= 40 ? (
        <div className="tally" role="img" aria-label={label} style={{ gridTemplateColumns: `repeat(${quantity}, 1fr)` }}>
          {Array.from({ length: quantity }, (_, i) => <span key={i} className={i < made ? "cell made" : "cell"} />)}
        </div>
      ) : (
        <div className="tally bar" role="img" aria-label={label}>
          <span className="fill" style={{ width: `${(100 * made) / quantity}%` }} />
        </div>
      )}
      <div className="sweep" aria-hidden="true">
        {sweep !== null && made < quantity && <span style={{ width: `${100 * sweep}%` }} />}
      </div>
    </div>
  );
}

interface Swap { tool: number; index: number | null; core: boolean }

function currentSwaps(slots: Slot[], unplaced: number[]): Swap[] {
  return [
    ...slots.filter((s) => s.swap_to !== null).map((s) => ({ tool: s.swap_to!, index: s.index, core: s.core })),
    ...unplaced.map((tool) => ({ tool, index: null, core: false })),
  ];
}

// The tools each session started out needing, so the checklist can tick items off as they're loaded.
// Browser storage is a convenience that survives a kiosk reload; without it, memory is used.
const swapLists = new Map<string, number[]>();

function loadSwapList(key: string): number[] {
  try {
    const raw = localStorage.getItem(key);
    if (raw) return JSON.parse(raw);
  } catch { /* storage blocked */ }
  return swapLists.get(key) ?? [];
}

function saveSwapList(key: string, tools: number[]): void {
  swapLists.set(key, tools);
  try {
    for (const k of Object.keys(localStorage)) if (k.startsWith("forge.swaps.") && k !== key) localStorage.removeItem(k);
    localStorage.setItem(key, JSON.stringify(tools));
  } catch { /* storage blocked */ }
}

function useSessionSwaps(sessionId: string, tools: number[]): number[] {
  const key = `forge.swaps.${sessionId}`;
  const known = loadSwapList(key);
  // A tool that needs loading later (one was taken out again) joins the list.
  const merged = [...known, ...tools.filter((t) => !known.includes(t))];
  useEffect(() => {
    if (merged.length !== known.length) saveSwapList(key, merged);
  });
  return merged;
}

/** Setup: which tools to load where, ticking off as the machinist swaps them. */
export function SetupChecklist({ session, slots, unplaced }: { session: Session; slots: Slot[]; unplaced: number[] }) {
  const swaps = currentSwaps(slots, unplaced);
  const initial = useSessionSwaps(session.id, swaps.map((s) => s.tool));
  const pending = new Set(swaps.map((s) => s.tool));
  const done = initial.filter((t) => !pending.has(t));
  if (swaps.length === 0 && (session.state !== "setup" || initial.length === 0)) return null;

  const where = (tool: number) => slots.find((s) => s.tool === tool)?.index;
  return (
    <section className={swaps.length === 0 ? "checklist ready" : "checklist"} aria-label="Tool setup">
      <h3>{swaps.length === 0 ? "Tools ready" : `${swaps.length} tool swap${swaps.length > 1 ? "s" : ""} to go`}</h3>
      <ul>
        {swaps.map((s) => (
          <li key={s.tool} className={s.core ? "todo core" : s.index === null ? "todo stuck" : "todo"}>
            <span className="box" aria-hidden="true" />
            {s.index === null
              ? `No free index for T${s.tool}`
              : s.core ? `Put T${s.tool} back in index ${s.index}` : `Load T${s.tool} in index ${s.index}`}
          </li>
        ))}
        {done.map((tool) => (
          <li key={tool} className="done">
            <span className="box" aria-hidden="true">✓</span>T{tool} in index {where(tool) ?? "?"}
          </li>
        ))}
      </ul>
    </section>
  );
}
