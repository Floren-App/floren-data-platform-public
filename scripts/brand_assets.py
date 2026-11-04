"""Writes the limoon. brand files from one drawing (the same geometry as apps/desktop/src/components/Brand.tsx).

    python3 scripts/brand_assets.py

The colour slice is the logo; the one-colour slice is for icons that take the text colour; the half slice is cut
along a line rising 45° to the right. The loading slice lights its segments one after another.
"""
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RIND, PITH, FLESH_COLOUR = '#4a8a1f', '#f2f9dc', '#a3d44a'
SEGMENTS, SPOKE, FLESH = 8, 3.4, 24
HALF_START, HALF_SPOKES = 45, (90, 135, 180)


def polar(r, degrees):
    a = (degrees - 90) * math.pi / 180
    return f'{32 + r * math.cos(a):.2f} {32 + r * math.sin(a):.2f}'


def wedge(start, end):
    return f'M 32 32 L {polar(FLESH, start)} A {FLESH} {FLESH} 0 0 1 {polar(FLESH, end)} Z'


def disc(r, half):
    if half:
        return f'M {polar(r, HALF_START)} A {r} {r} 0 0 1 {polar(r, HALF_START + 180)} Z'
    return f'M {32 - r} 32 A {r} {r} 0 1 0 {32 + r} 32 A {r} {r} 0 1 0 {32 - r} 32 Z'


def parts(half):
    step = 360 / SEGMENTS
    start, count = (HALF_START, 4) if half else (0, SEGMENTS)
    segments = [wedge(start + i * step, start + (i + 1) * step) for i in range(count)]
    angles = HALF_SPOKES if half else [i * step for i in range(SEGMENTS)]
    spokes = ''.join(f'<line x1="32" y1="32" x2="{polar(FLESH + 1, a).split()[0]}" y2="{polar(FLESH + 1, a).split()[1]}"/>' for a in angles)
    return segments, spokes


def colour(half=False, loading=False):
    segments, spokes = parts(half)
    style = ('<style>.s{animation:t .96s linear infinite}@keyframes t{from{opacity:1}to{opacity:.3}}'
             '@media (prefers-reduced-motion:reduce){.s{animation:none}}</style>') if loading else ''
    flesh = ''.join(f'<path class="s" d="{d}" fill="{FLESH_COLOUR}"'
                    + (f' style="animation-delay:{(i - len(segments)) * 0.12:.2f}s"' if loading else '') + '/>'
                    for i, d in enumerate(segments))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{style}'
            f'<path d="{disc(30.5, half)}" fill="{RIND}"/><path d="{disc(27.5, half)}" fill="{PITH}"/>{flesh}'
            f'<g stroke="{PITH}" stroke-width="{SPOKE}" stroke-linecap="round">{spokes}</g>'
            f'<circle cx="32" cy="32" r="{SPOKE}" fill="{PITH}"/></svg>\n')


def mono(half=False, colour='currentColor'):
    _, spokes = parts(half)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><mask id="m">'
            f'<path d="{disc(30.5, half)}" fill="white"/><path d="{disc(27.5, half)}" fill="black"/>'
            f'<path d="{disc(FLESH, half)}" fill="white"/><g stroke="black" stroke-width="{SPOKE}" stroke-linecap="round">{spokes}</g>'
            f'<circle cx="32" cy="32" r="{SPOKE}" fill="black"/></mask>'
            f'<path d="{disc(30.5, half)}" fill="{colour}" mask="url(#m)"/></svg>\n')


def main():
    files = {
        'apps/desktop/public/limoon.svg': colour(),
        'apps/desktop/public/limoon-half.svg': colour(half=True),
        'apps/desktop/public/limoon-mono.svg': mono(),
        'apps/desktop/public/limoon-loading.svg': colour(loading=True),
        'docs/assets/limoon.svg': colour(),
        # VS Code tints activity-bar icons itself; any single colour works.
        'extensions/vscode-fdp/media/icon.svg': mono(colour='#c5c5c5'),
    }
    for path, svg in files.items():
        (ROOT / path).write_text(svg)
        print('wrote', path)


if __name__ == '__main__':
    main()
