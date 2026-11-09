import { createContext, useCallback, useContext, useEffect, useId, useRef, useState } from "react";
import { Modal } from "./components/Roles";

/** Unsaved work in the open page. Only the open page is mounted, so going to another page, closing it or switching
 * group would drop what was typed: the shell asks first. Secret fields are never kept anywhere to restore them. */
interface Entry { label: string; save?: () => Promise<unknown> }
type Registry = { set: (id: string, entry: Entry | null) => void };
const UnsavedContext = createContext<Registry | null>(null);

/** Marks a form as having unsaved changes while `dirty`. With `save`, leaving can save it first. */
export function useUnsaved(dirty: boolean, label: string, save?: () => Promise<unknown>) {
  const registry = useContext(UnsavedContext), id = useId();
  const latest = useRef(save);
  latest.current = save;
  const canSave = !!save;
  useEffect(() => {
    if (!registry) return;
    registry.set(id, dirty ? { label, save: canSave ? () => latest.current!() : undefined } : null);
    return () => registry.set(id, null);
  }, [registry, id, dirty, label, canSave]);
}

/** The shell's side: the provider value, a confirmation to await before leaving, and its dialog. */
export function useUnsavedGuard() {
  const entries = useRef(new Map<string, Entry>());
  const [asking, setAsking] = useState<{ resolve: (leave: boolean) => void } | null>(null);
  const [saving, setSaving] = useState(false), [error, setError] = useState("");
  const registry = useRef<Registry>({ set: (id, entry) => { if (entry) entries.current.set(id, entry); else entries.current.delete(id); } }).current;
  useEffect(() => {
    // Closing or reloading the window: the browser asks; there is nothing to save to from here.
    const unload = (event: BeforeUnloadEvent) => { if (entries.current.size) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", unload);
    return () => window.removeEventListener("beforeunload", unload);
  }, []);
  /** Resolves true when it is fine to leave the open page: nothing unsaved, saved, or discarded. */
  const confirmLeave = useCallback((): Promise<boolean> => entries.current.size === 0 ? Promise.resolve(true)
    : new Promise((resolve) => { setError(""); setAsking({ resolve }); }), []);
  const finish = (leave: boolean) => { asking?.resolve(leave); setAsking(null); };
  const pending = [...entries.current.values()];
  const allSave = pending.length > 0 && pending.every((e) => e.save);
  const dialog = asking && <Modal title="Unsaved changes" onClose={() => finish(false)}>
    <p>{pending.map((e) => e.label).join(", ")} {pending.length === 1 ? "has" : "have"} changes that are not saved yet.</p>
    {error && <p className="error" role="alert">Saving failed, so you stay on this page: {error}</p>}
    <div className="form-actions">
      {allSave && <button className="btn" disabled={saving} onClick={() => {
        setSaving(true);
        Promise.all(pending.map((e) => e.save!())).then(() => finish(true), (e: Error) => setError(e.message)).finally(() => setSaving(false));
      }}>{saving ? "Saving…" : "Save and continue"}</button>}
      <button className="btn secondary" disabled={saving} onClick={() => finish(true)}>Discard changes</button>
      <button className="btn secondary" disabled={saving} onClick={() => finish(false)}>Stay</button>
    </div>
  </Modal>;
  return { Provider: UnsavedContext.Provider, registry, confirmLeave, dialog };
}
