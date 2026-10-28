import { ReactNode, useEffect, useRef, useState } from "react";

/** A trigger button with a keyboard-navigable menu (arrows, Home/End, Escape, click outside). */
export function MenuButton({ label, icon, id, menuLabel, className = "icon-btn", children }: {
  label: string; icon: ReactNode; id: string; menuLabel: string; className?: string;
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    anchor.current?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus();
    const dismiss = (event: PointerEvent) => { if (!anchor.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", escape); };
  }, [open]);
  // Close when focus moves elsewhere (Tab). WebKit (the desktop app on macOS) does not focus buttons on click,
  // so a click on a menu item blurs with no new target: ignore that, or the menu closes before the click lands.
  // Clicks outside close it through the pointerdown listener above.
  return <div className={`menu-anchor ${id}-anchor`} ref={anchor} onBlur={(e) => { if (e.relatedTarget && !e.currentTarget.contains(e.relatedTarget)) setOpen(false); }}>
    <button ref={trigger} className={className} aria-label={label} title={label} aria-haspopup="menu" aria-expanded={open}
      aria-controls={open ? id : undefined} onClick={() => setOpen(!open)}>{icon}</button>
    {open && <div id={id} className="menu" role="menu" aria-label={menuLabel} onKeyDown={(e) => {
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      const items = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'));
      const index = items.indexOf(document.activeElement as HTMLButtonElement);
      const next = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : (index + (e.key === "ArrowUp" ? -1 : 1) + items.length) % items.length;
      items[next]?.focus();
    }}>{children(() => setOpen(false))}</div>}
  </div>;
}
