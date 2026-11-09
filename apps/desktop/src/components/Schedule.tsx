import { useEffect, useState } from "react";
import { api } from "../api";

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
type Simple = { mode: "daily" | "weekly"; hour: number; minute: number; day: number };

/** A cron schedule as daily or weekly, when it is one of those; anything else stays an advanced schedule. */
function simple(cron: string): Simple | null {
  const m = /^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-6])$/.exec(cron.trim());
  if (!m || +m[1] > 59 || +m[2] > 23) return null;
  return { mode: m[3] === "*" ? "daily" : "weekly", minute: +m[1], hour: +m[2], day: m[3] === "*" ? 0 : +m[3] };
}
const cronOf = (s: Simple) => `${s.minute} ${s.hour} * * ${s.mode === "daily" ? "*" : s.day}`;
const zones = (): string[] => {
  try { return (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? []; } catch { return []; }
};

/** Daily or weekly at a time, or an advanced cron schedule, in a timezone, with the next runs as the scheduler will
 * compute them (daylight-saving changes included). Switching to Advanced keeps the schedule; an advanced schedule
 * that is not daily or weekly is never simplified away. */
export function ScheduleEditor({ schedule, timezone, onChange, idPrefix }: {
  schedule: string; timezone: string; onChange: (schedule: string, timezone: string) => void; idPrefix: string;
}) {
  const parsed = simple(schedule);
  const [advanced, setAdvanced] = useState(!parsed);
  const [preview, setPreview] = useState<{ runs?: string[]; error?: string }>({});
  useEffect(() => {
    let live = true;
    const t = setTimeout(() => api.backups.schedulePreview(schedule, timezone)
      .then((r) => live && setPreview({ runs: r.runs }))
      .catch((e: Error) => live && setPreview({ error: e.message })), 300);
    return () => { live = false; clearTimeout(t); };
  }, [schedule, timezone]);
  const current = parsed ?? { mode: "daily", hour: 2, minute: 0, day: 0 } as Simple;
  const set = (change: Partial<Simple>) => onChange(cronOf({ ...current, ...change }), timezone);
  const time = `${String(current.hour).padStart(2, "0")}:${String(current.minute).padStart(2, "0")}`;
  const known = zones();
  return <fieldset className="field schedule-editor" style={{ gridColumn: "1 / -1" }}><legend>Schedule</legend>
    <div className="row" role="radiogroup" aria-label="How often">
      {(["daily", "weekly"] as const).map((mode) => <label key={mode} className="row" title={parsed ? undefined : "Write a daily or weekly schedule under Advanced first"}>
        <input type="radio" name={`${idPrefix}-mode`} checked={!advanced && current.mode === mode} disabled={!parsed}
          onChange={() => { setAdvanced(false); set({ mode }); }} />{mode === "daily" ? "Daily" : "Weekly"}</label>)}
      <label className="row"><input type="radio" name={`${idPrefix}-mode`} checked={advanced} onChange={() => setAdvanced(true)} />Advanced</label>
    </div>
    {!advanced ? <div className="row">
      {current.mode === "weekly" && <><label htmlFor={`${idPrefix}-day`}>Day</label>
        <select id={`${idPrefix}-day`} value={current.day} onChange={(e) => set({ day: +e.target.value })}>{DAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}</select></>}
      <label htmlFor={`${idPrefix}-time`}>Time</label>
      <input id={`${idPrefix}-time`} type="time" value={time} onChange={(e) => { if (e.target.value) { const [h, m] = e.target.value.split(":"); set({ hour: +h, minute: +m }); } }} />
    </div> : <div className="field"><label htmlFor={`${idPrefix}-cron`}>Cron schedule (five fields)</label>
      <input id={`${idPrefix}-cron`} required className="mono" value={schedule} onChange={(e) => onChange(e.target.value, timezone)} />
      <p className="hint">Minute, hour, day of month, month, day of week. Daily at 02:00 is <code>0 2 * * *</code>; Sundays at 03:30 is <code>30 3 * * 0</code>.
        {!parsed && " This schedule is not a simple daily or weekly one, so it stays here."}</p></div>}
    <div className="field"><label htmlFor={`${idPrefix}-timezone`}>Timezone</label>
      {known.length ? <select id={`${idPrefix}-timezone`} value={timezone} onChange={(e) => onChange(schedule, e.target.value)}>
        {!known.includes(timezone) && <option value={timezone}>{timezone}</option>}{known.map((z) => <option key={z} value={z}>{z}</option>)}</select>
        : <input id={`${idPrefix}-timezone`} required value={timezone} onChange={(e) => onChange(schedule, e.target.value)} />}</div>
    <div aria-live="polite" className="small">{preview.error ? <span className="error">{preview.error}</span>
      : preview.runs && <>Next runs: {preview.runs.slice(0, 3).map((r) => new Date(r).toLocaleString(undefined, { timeZone: timezone, weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })).join(" · ")}
        <span className="muted"> ({timezone})</span></>}</div>
  </fieldset>;
}
