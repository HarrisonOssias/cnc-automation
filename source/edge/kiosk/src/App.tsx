import { useEffect, useRef, useState } from "react";
import { Toasts } from "./alerts";
import { DemoDrawer, demoMode, loadTool } from "./demo";
import { SessionProgress, SetupChecklist, StateStrip, timeScale } from "./session";
import { clock, useNow } from "./time";
import type { Job, PageState, Slot } from "./types";

/** Initial fetch, then the agent pushes the full page state over SSE on every change. */
function usePageState() {
  const [page, setPage] = useState<PageState | null>(null);
  const [agentUp, setAgentUp] = useState(true);

  useEffect(() => {
    fetch("/api/page")
      .then((r) => r.json())
      .then(setPage)
      .catch(() => setAgentUp(false));
    const events = new EventSource("/api/events"); // reconnects on its own
    events.onmessage = (e) => {
      setPage(JSON.parse(e.data));
      setAgentUp(true);
    };
    events.onerror = () => setAgentUp(false);
    return () => events.close();
  }, []);

  return { page, agentUp };
}

export function App() {
  const { page, agentUp } = usePageState();
  const moving = page?.session?.state === "cutting" || page?.session?.state === "setup";
  const now = useNow(moving ? 250 : 5000);
  if (!page) return <div className="loading">Waiting for the machine agent…</div>;

  const session = page.session;
  const scale = timeScale(page);
  const load = demoMode ? (index: number, tool: number) => loadTool(page.slots, index, tool) : undefined;
  const row = (s: Slot | undefined, key: number) => <SlotRow key={key} slot={s} onLoad={load} />;

  const byIndex = new Map(page.slots.map((s) => [s.index, s]));
  const core = page.slots.filter((s) => s.core);
  // Other tools keep their position: 1-12 in the first column, 13-24 in the second.
  const column = (from: number) => Array.from({ length: 12 }, (_, i) => byIndex.get(from + i));

  return (
    <div className="screen">
      <Banner page={page} agentUp={agentUp} />
      <main className="columns">
        <section className="col">
          {column(1).map((s, i) => row(s && !s.core ? s : undefined, i))}
        </section>
        <section className="col">
          {column(13).map((s, i) => row(s && !s.core ? s : undefined, i))}
        </section>
        <section className="col">
          {core.map((s) => row(s, s.index))}
        </section>
        <section className="col parts">
          {session && <StateStrip session={session} now={now} scale={scale} />}
          {page.job ? (
            <JobCard job={page.job} unplaced={page.unplaced_tools} cachedImages={page.cached_images}>
              {session && (
                <>
                  <SessionProgress session={session} now={now} scale={scale} />
                  <SetupChecklist session={session} slots={page.slots} unplaced={page.unplaced_tools} />
                </>
              )}
            </JobCard>
          ) : (
            <p className="muted">No job assigned</p>
          )}
          {page.up_next.length > 0 && <h2>Up next</h2>}
          {page.up_next.map((job) => <JobCard key={job.id} job={job} compact />)}
        </section>
        <Toasts alerts={page.alerts} />
      </main>
      {demoMode && <DemoDrawer page={page} />}
    </div>
  );
}

/** Counts changes to a value after the first render; used to flash a slot once when it changes. */
function useChangeCount(signature: string): number {
  const previous = useRef(signature);
  const [count, setCount] = useState(0);
  useEffect(() => {
    if (previous.current !== signature) {
      previous.current = signature;
      setCount((c) => c + 1);
    }
  }, [signature]);
  return count;
}

function SlotRow({ slot, onLoad }: { slot?: Slot; onLoad?: (index: number, tool: number) => void }) {
  const changes = useChangeCount(slot ? `${slot.tool}|${slot.state}|${slot.swap_to}|${slot.in_use}` : "");
  if (!slot) return <div className="slot spacer" />;
  const swapClass = `pill ${slot.core ? "pill-core-swap" : "pill-swap"}`;
  const pill = slot.swap_to
    ? onLoad
      ? <button className={`${swapClass} tappable`} onClick={() => onLoad(slot.index, slot.swap_to!)}
          aria-label={`Load T${slot.swap_to} into index ${slot.index}`}>-&gt; T{slot.swap_to}</button>
      : <span className={swapClass}>-&gt; T{slot.swap_to}</span>
    : slot.in_use
      ? <span className={`pill ${slot.core ? "pill-core" : "pill-use"}`}>IN USE</span>
      : null;
  return (
    <div className="slot">
      {changes > 0 && <span key={changes} className="flash" aria-hidden="true" />}
      <div className={`tool ${slot.state}`}>
        <span className="index">{slot.index}</span>
        <span className="number">T{slot.tool ?? 0}</span>
        <span className={`check ${slot.measured ? "measured" : ""}`}>{slot.measured ? "✓" : ""}</span>
      </div>
      {pill}
    </div>
  );
}

function JobCard({ job, unplaced = [], compact = false, cachedImages = [], children }: {
  job: Job; unplaced?: number[]; compact?: boolean; cachedImages?: string[]; children?: React.ReactNode;
}) {
  const image = job.image_sha256 && cachedImages.includes(job.image_sha256) ? `/api/images/${job.image_sha256}` : null;
  return (
    <article className={`job ${compact ? "compact" : ""}`}>
      <div className="job-head">
        <span className="order">{job.order_number} - #{job.line_number}</span>
        <DueBadge due={job.due_date} />
      </div>
      <div className="job-info">
        <div>{job.quantity} x {job.material}</div>
        <div className="part-number">{job.part_number}</div>
        {!compact && <div className="machinist">{job.machinist}</div>}
      </div>
      {children}
      {!compact && (
        <div className="part-image">
          {image ? <img src={image} alt={job.part_number} /> : <span>No picture for {job.part_number}</span>}
        </div>
      )}
      {unplaced.length > 0 && (
        <div className="warning">No free slot for {unplaced.map((t) => `T${t}`).join(", ")}</div>
      )}
      {!compact && <CompleteButton key={job.id} jobId={job.id} />}
    </article>
  );
}

/** The machinist's "this job is finished" signal. Two taps, so a bump can't complete a job. */
function CompleteButton({ jobId }: { jobId: number }) {
  const [armed, setArmed] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!armed) return;
    const timer = setTimeout(() => setArmed(false), 4000);
    return () => clearTimeout(timer);
  }, [armed]);

  async function tap() {
    if (!armed) return setArmed(true);
    setArmed(false);
    const r = await fetch(`/api/jobs/${jobId}/complete`, { method: "POST" }).catch(() => null);
    setFailed(!r?.ok);
  }

  return (
    <>
      <button className={`complete ${armed ? "armed" : ""}`} onClick={tap}>
        {armed ? "Tap again to confirm" : "Job complete"}
      </button>
      {failed && <div className="warning">Couldn't record that — try again</div>}
    </>
  );
}

function DueBadge({ due }: { due: string }) {
  const days = Math.round((startOfToday() - Date.parse(`${due}T00:00:00`)) / 86_400_000);
  if (days > 0) return <span className="badge late">MFG {days} DAY{days > 1 ? "S" : ""} LATE</span>;
  if (days === 0) return <span className="badge today">DUE TODAY</span>;
  return <span className="badge">DUE IN {-days} DAY{days < -1 ? "S" : ""}</span>;
}

function Banner({ page, agentUp }: { page: PageState; agentUp: boolean }) {
  if (!agentUp) return <div className="banner">Machine agent not responding — showing last known state</div>;
  const sync = page.sync;
  if (!sync || sync.online) return null;
  const since = sync.offline_since ? ` since ${clock(sync.offline_since)}` : "";
  const asOf = sync.bundle_fetched_at ? ` — jobs as of ${clock(sync.bundle_fetched_at)}` : " — no job data yet";
  return <div className="banner">Offline{since}{asOf}</div>;
}

const startOfToday = () => new Date().setHours(0, 0, 0, 0);
