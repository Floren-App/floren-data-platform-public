import { useEffect, useState } from "react";

/** A counter for views that show permission-dependent or pending state; add it to a load effect's dependencies.
 *
 * It ticks when the page becomes visible again, and while `pending` (an access request awaiting a decision, a
 * backup running) every `every` ms, backing off to `max`. It never ticks while the page is hidden, and stops when
 * nothing is pending or the view unmounts. Engine metadata stays on demand: do not drive storage browsing with it.
 */
export function useFreshness(pending: boolean, { every = 10_000, max = 60_000 }: { every?: number; max?: number } = {}): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const visible = () => { if (!document.hidden) setTick((t) => t + 1); };
    document.addEventListener("visibilitychange", visible);
    return () => document.removeEventListener("visibilitychange", visible);
  }, []);
  useEffect(() => {
    if (!pending) return;
    let delay = every, timer: ReturnType<typeof setTimeout>;
    const next = () => {
      timer = setTimeout(() => {
        if (!document.hidden) setTick((t) => t + 1);
        delay = Math.min(Math.round(delay * 1.5), max);
        next();
      }, delay);
    };
    next();
    return () => clearTimeout(timer);
  }, [pending, every, max]);
  return tick;
}

/** When a view's data was last loaded, kept apart from a failed refresh so old data never looks current. */
export interface Freshness { at: Date | null; refreshing: boolean; error: string }

export function freshnessText({ at, refreshing, error }: Freshness): string {
  if (refreshing) return "Updating…";
  const when = at ? `Updated ${at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}` : "";
  return error ? `${when ? when + " · " : ""}could not update: ${error}` : when;
}
