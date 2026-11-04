import { useId } from "react";
import "@fontsource/fredoka/500.css";
import "@fontsource/fredoka/600.css";

/** limoon: a play on the Dutch "limoen" (lime), pronounced the same by an English speaker. The mark is a lime slice:
 * a rind, its pith and eight segments, divided by spokes of even width so they still read from a distance. */
const SEGMENTS = 8;
const SPOKE = 3.4;   // width of the dividing lines, in the 64-unit drawing
const FLESH = 24;

function polar(r: number, degrees: number) {
  const a = (degrees - 90) * Math.PI / 180;
  return `${(32 + r * Math.cos(a)).toFixed(2)} ${(32 + r * Math.sin(a)).toFixed(2)}`;
}

/** One segment: a wedge of the flesh. The spokes drawn over it make the gaps. */
function wedge(from: number, to: number) {
  return `M 32 32 L ${polar(FLESH, from)} A ${FLESH} ${FLESH} 0 0 1 ${polar(FLESH, to)} Z`;
}

/** Half a slice, cut along a line rising 45° to the right (from 225° to 45°); the flesh lies below the cut. Its outline
 * is closed: the rind and pith also run along the cut (`inset` from it), so nothing reaches past the edge. */
const HALF = { start: 45, spokes: [90, 135, 180], segments: 4, inset: { 30.5: 0, 27.5: 3, [FLESH]: 6.5 } as Record<number, number> };

/** The part of a circle beyond a chord `d` from the centre, on the flesh side of the cut. */
function halfDisc(r: number, d: number) {
  const t = Math.acos(d / r) * 180 / Math.PI, n = HALF.start + 90;
  return `M ${polar(r, n - t)} A ${r} ${r} 0 0 1 ${polar(r, n + t)} Z`;
}

/** The lime slice, in colour (the logo) or in one colour (`mono`, for icons that take the text colour). `half` draws
 * the half slice. With `loading`, its segments light up one after another. */
export function LimeSlice({ size = 32, loading = false, title, half = false, mono = false }: {
  size?: number; loading?: boolean; title?: string; half?: boolean; mono?: boolean;
}) {
  const mask = useId(), clip = useId();
  const count = half ? HALF.segments : SEGMENTS, step = 360 / SEGMENTS;
  const start = half ? HALF.start : 0;
  const segments = Array.from({ length: count }, (_, i) => wedge(start + i * step, start + (i + 1) * step));
  const spokes = half ? HALF.spokes : Array.from({ length: SEGMENTS }, (_, i) => i * step);
  // The outline of the rind, pith and flesh: whole circles, or halves below the cut.
  const disc = (r: number) => half
    ? halfDisc(r, HALF.inset[r] ?? 0)
    : `M ${32 - r} 32 A ${r} ${r} 0 1 0 ${32 + r} 32 A ${r} ${r} 0 1 0 ${32 - r} 32 Z`;
  // The half slice keeps its segments and lines inside the flesh; the whole slice needs no clip.
  const inside = half ? `url(#${CSS.escape(clip)})` : undefined;
  const lines = spokes.map((a) => <line key={a} x1="32" y1="32" x2={polar(FLESH + 1, a).split(" ")[0]} y2={polar(FLESH + 1, a).split(" ")[1]} />);
  const label = { role: title ? "img" : undefined, "aria-label": title, "aria-hidden": title ? undefined : true } as const;
  // The half slice lies on one side of the centre; this moves it back to the middle of its square.
  const shift = half ? "translate(-4.6 -4.6)" : undefined;
  if (mono) return <svg className="lime-slice mono" width={size} height={size} viewBox="0 0 64 64" {...label}><g transform={shift}>
    <mask id={mask}>
      <path d={disc(30.5)} fill="white" /><path d={disc(27.5)} fill="black" /><path d={disc(FLESH)} fill="white" />
      <g stroke="black" strokeWidth={SPOKE} strokeLinecap="butt" clipPath={inside}>{lines}</g>
      {!half && <path d={disc(SPOKE)} fill="black" />}
    </mask>
    {half && <clipPath id={clip}><path d={disc(FLESH)} /></clipPath>}
    <path d={disc(30.5)} fill="currentColor" mask={`url(#${CSS.escape(mask)})`} />
  </g></svg>;
  return <svg className={`lime-slice${loading ? " loading" : ""}`} width={size} height={size} viewBox="0 0 64 64" {...label}><g transform={shift}>
    <path d={disc(30.5)} className="lime-rind" />
    <path d={disc(27.5)} className="lime-pith" />
    {half && <clipPath id={clip}><path d={disc(FLESH)} /></clipPath>}
    <g clipPath={inside}>
      {segments.map((d, i) => <path key={i} d={d} className="lime-segment"
        style={loading ? { animationDelay: `${(i - count) * 0.12}s` } : undefined} />)}
      <g className="lime-spokes" strokeWidth={SPOKE} strokeLinecap="butt">{lines}</g>
    </g>
    {!half && <path d={disc(SPOKE)} className="lime-pith" />}
  </g></svg>;
}

/** The name, lowercase with its period. */
export function Wordmark({ size = 28 }: { size?: number }) {
  return <span className="wordmark" style={{ fontSize: size }}>limoon<span className="wordmark-dot">.</span></span>;
}

/** The opening screen: the slice and the name, while the app starts. */
export function Splash({ status }: { status?: string }) {
  return <div className="splash" role="status" aria-label={status ?? "Starting limoon"}>
    <LimeSlice size={96} loading />
    <Wordmark size={52} />
    {status && <p className="muted">{status}</p>}
  </div>;
}
