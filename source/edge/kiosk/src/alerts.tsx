import { useEffect, useRef, useState } from "react";
import type { Alert } from "./types";

const SHOW_MS = { info: 4000, done: 7000, alarm: 7000 } as const;

/** Progress alerts from the agent ("Part 15 of 30 done", alarms), big enough to read from the machine.
 * Only alerts that arrive while the screen is open pop up; an alarm also stays on the state strip. */
export function Toasts({ alerts }: { alerts: Alert[] }) {
  const lastSeen = useRef<number | null>(null);
  const [shown, setShown] = useState<Alert[]>([]);

  useEffect(() => {
    const newest = alerts.reduce((max, a) => Math.max(max, a.id), 0);
    if (lastSeen.current === null) { // first page after loading: don't replay old alerts
      lastSeen.current = newest;
      return;
    }
    if (newest < lastSeen.current) lastSeen.current = 0; // ids went backwards: the agent restarted
    const fresh = alerts.filter((a) => a.id > lastSeen.current!);
    if (fresh.length === 0) return;
    lastSeen.current = newest;
    setShown((s) => [...s, ...fresh].slice(-3));
    for (const a of fresh) setTimeout(() => setShown((s) => s.filter((x) => x.id !== a.id)), SHOW_MS[a.level]);
  }, [alerts]);

  return (
    <div className="toasts" aria-live="polite">
      {shown.map((a) => <div key={a.id} className={`toast ${a.level}`}>{a.text}</div>)}
    </div>
  );
}
