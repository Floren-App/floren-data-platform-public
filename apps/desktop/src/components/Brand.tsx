import "@fontsource/fredoka/500.css";
import "@fontsource/fredoka/600.css";

/** limoon: a play on the Dutch "limoen" (lime), pronounced the same by an English speaker. The mark is a lime slice:
 * a rind, its pith and eight segments. */
const SEGMENTS = 8;

function polar(r: number, degrees: number) {
  const a = (degrees - 90) * Math.PI / 180;
  return `${(32 + r * Math.cos(a)).toFixed(2)} ${(32 + r * Math.sin(a)).toFixed(2)}`;
}

/** One segment of the slice: a wedge between two radii, with a small gap to the next. */
function segment(i: number) {
  const gap = 4, from = i * 360 / SEGMENTS + gap / 2, to = (i + 1) * 360 / SEGMENTS - gap / 2, inner = 3.2, outer = 24;
  return `M ${polar(inner, from)} L ${polar(outer, from)} A ${outer} ${outer} 0 0 1 ${polar(outer, to)} L ${polar(inner, to)} Z`;
}

/** The lime slice. With `loading`, its segments light up one after another. */
export function LimeSlice({ size = 32, loading = false, title }: { size?: number; loading?: boolean; title?: string }) {
  return <svg className={`lime-slice${loading ? " loading" : ""}`} width={size} height={size} viewBox="0 0 64 64"
    role={title ? "img" : undefined} aria-label={title} aria-hidden={title ? undefined : true}>
    <circle cx="32" cy="32" r="30.5" className="lime-rind" />
    <circle cx="32" cy="32" r="27.5" className="lime-pith" />
    {Array.from({ length: SEGMENTS }, (_, i) => <path key={i} d={segment(i)} className="lime-segment"
      style={loading ? { animationDelay: `${(i - SEGMENTS) * 0.12}s` } : undefined} />)}
    <circle cx="32" cy="32" r="2.4" className="lime-pith" />
  </svg>;
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
