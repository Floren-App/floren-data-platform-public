"""Writes every limoon. brand file from one drawing (the same geometry as apps/desktop/src/components/Brand.tsx).

    python3 brand/build.py

It writes brand/logos (all SVG) and brand/colors.*, and copies the files the app, the docs and the editor extension
use. The wordmark is drawn from the Fredoka font as outlines, so it needs fontTools (`pip install fonttools`);
without it the committed wordmark and lockups are left as they are.

The colour slice is the logo; the half slice, cut along a line rising 45° to the right, is the alternative logo; the
one-colour versions are for places that take a single colour. The loading slice lights its segments one after
another.
"""
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / 'brand'
RIND, PITH, FLESH_COLOUR = '#4a8a1f', '#f2f9dc', '#a3d44a'
ACCENT, INK, INK_ON_DARK = '#6cb52e', '#1d2414', '#f2f2f2'
COLOURS = {
    'rind': (RIND, 'The outside of the slice'),
    'pith': (PITH, 'The pale lines and pith'),
    'flesh': (FLESH_COLOUR, 'The segments'),
    'accent': (ACCENT, "The wordmark's period and the app's controls"),
    'ink': (INK, 'The wordmark on light backgrounds'),
    'ink-on-dark': (INK_ON_DARK, 'The wordmark on dark backgrounds'),
}
SEGMENTS, SPOKE, FLESH = 8, 3.4, 24
HALF_START, HALF_SPOKES = 45, (90, 135, 180)


def polar(r, degrees):
    a = (degrees - 90) * math.pi / 180
    return f'{32 + r * math.cos(a):.2f} {32 + r * math.sin(a):.2f}'


def wedge(start, end):
    return f'M 32 32 L {polar(FLESH, start)} A {FLESH} {FLESH} 0 0 1 {polar(FLESH, end)} Z'


# The half slice: the rind only on the curved outside; along the cut a band of pith, so the flesh and its lines stop
# short of the edge. The values set each layer in from the cut.
HALF_INSET = {30.5: 0, 27.5: 0, FLESH: 3}
# Moves the half slice back towards the middle of its square (it lies on one side of the centre).
HALF_SHIFT = 'translate(-4.6 -4.6)'


def disc(r, half):
    if half:
        t = math.degrees(math.acos(HALF_INSET.get(r, 0) / r))
        n = HALF_START + 90
        return f'M {polar(r, n - t)} A {r} {r} 0 0 1 {polar(r, n + t)} Z'
    return f'M {32 - r} 32 A {r} {r} 0 1 0 {32 + r} 32 A {r} {r} 0 1 0 {32 - r} 32 Z'


def parts(half):
    step = 360 / SEGMENTS
    start, count = (HALF_START, 4) if half else (0, SEGMENTS)
    segments = [wedge(start + i * step, start + (i + 1) * step) for i in range(count)]
    angles = HALF_SPOKES if half else [i * step for i in range(SEGMENTS)]
    spokes = ''.join(f'<line x1="32" y1="32" x2="{polar(FLESH + 1, a).split()[0]}" y2="{polar(FLESH + 1, a).split()[1]}"/>' for a in angles)
    return segments, spokes


def colour(half=False, loading=False, ident='f'):
    segments, spokes = parts(half)
    # Only the loading slice moves; the class is unique to it, so a page with several marks keeps the others still.
    style = ('<style>.limoon-tick{animation:limoon-tick .96s linear infinite}@keyframes limoon-tick{from{opacity:1}to{opacity:.3}}'
             '@media (prefers-reduced-motion:reduce){.limoon-tick{animation:none}}</style>') if loading else ''
    flesh = ''.join(f'<path d="{d}" fill="{FLESH_COLOUR}"'
                    + (f' class="limoon-tick" style="animation-delay:{(i - len(segments)) * 0.12:.2f}s"' if loading else '') + '/>'
                    for i, d in enumerate(segments))
    # The half slice keeps its segments and lines inside the flesh, so nothing reaches past its edge.
    clip = f'<clipPath id="{ident}"><path d="{disc(FLESH, half)}"/></clipPath>' if half else ''
    inside = f' clip-path="url(#{ident})"' if half else ''
    centre = '' if half else f'<path d="{disc(SPOKE, half)}" fill="{PITH}"/>'
    shift = f' transform="{HALF_SHIFT}"' if half else ''
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{style}<g{shift}>{clip}'
            f'<path d="{disc(30.5, half)}" fill="{RIND}"/><path d="{disc(27.5, half)}" fill="{PITH}"/>'
            f'<g{inside}>{flesh}<g stroke="{PITH}" stroke-width="{SPOKE}" stroke-linecap="butt">{spokes}</g></g>'
            f'{centre}</g></svg>\n')


def mono(half=False, colour='currentColor', ident='m'):
    _, spokes = parts(half)
    clip = f'<clipPath id="{ident}c"><path d="{disc(FLESH, half)}"/></clipPath>' if half else ''
    inside = f' clip-path="url(#{ident}c)"' if half else ''
    centre = '' if half else f'<path d="{disc(SPOKE, half)}" fill="black"/>'
    shift = f' transform="{HALF_SHIFT}"' if half else ''
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><g{shift}>{clip}<mask id="{ident}">'
            f'<path d="{disc(30.5, half)}" fill="white"/><path d="{disc(27.5, half)}" fill="black"/>'
            f'<path d="{disc(FLESH, half)}" fill="white"/><g stroke="black" stroke-width="{SPOKE}" stroke-linecap="butt"{inside}>{spokes}</g>'
            f'{centre}</mask><path d="{disc(30.5, half)}" fill="{colour}" mask="url(#{ident})"/></g></svg>\n')


def wordmark_paths():
    """The letters "limoon" and the period as SVG path data in font units (baseline at y=0), with their bounds."""
    try:
        from fontTools.ttLib import TTFont
        from fontTools.pens.svgPathPen import SVGPathPen
        from fontTools.pens.transformPen import TransformPen
        from fontTools.pens.boundsPen import BoundsPen
    except ImportError:
        return None
    font = TTFont(BRAND / 'font' / 'Fredoka-SemiBold.woff')
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    x, letters, dot, bounds = 0, [], '', BoundsPen(glyphs)
    for ch in 'limoon.':
        glyph = glyphs[cmap[ord(ch)]]
        pen = SVGPathPen(glyphs)
        glyph.draw(TransformPen(pen, (1, 0, 0, -1, x, 0)))
        glyph.draw(TransformPen(bounds, (1, 0, 0, -1, x, 0)))
        if ch == '.':
            dot = pen.getCommands()
        else:
            letters.append(pen.getCommands())
        x += glyph.width
    return ' '.join(letters), dot, bounds.bounds


def wordmark(ink, paths):
    letters, dot, (x0, y0, x1, y1) = paths
    pad = 20
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0 - pad:.0f} {y0 - pad:.0f} {x1 - x0 + 2 * pad:.0f} {y1 - y0 + 2 * pad:.0f}">'
            f'<title>limoon.</title><path d="{letters}" fill="{ink}"/><path d="{dot}" fill="{ACCENT}"/></svg>\n')


def lockup(mark, ink, paths):
    """The mark beside the wordmark, centred on the letters' height."""
    letters, dot, (x0, y0, x1, y1) = paths
    size = (0 - y0) * 1.3                      # the mark is a little taller than the letters
    gap = size * 0.28
    top = (y0 + 0) / 2 - size / 2              # centred on the letters, from their top to the baseline
    inner = re.sub(r'^<svg[^>]*>|</svg>\s*$', '', mark.strip())
    scale = size / 64
    width = size + gap + (x1 - x0)
    pad = 20
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-pad} {top - pad:.0f} {width + 2 * pad:.0f} {size + 2 * pad:.0f}">'
            f'<title>limoon.</title><g transform="scale({scale:.4f}) translate(0 {top / scale:.2f})">{inner}</g>'
            f'<g transform="translate({size + gap - x0:.1f} 0)"><path d="{letters}" fill="{ink}"/><path d="{dot}" fill="{ACCENT}"/></g></svg>\n')


def tokens():
    css = ':root {\n' + ''.join(f'  --limoon-{name}: {value}; /* {why} */\n' for name, (value, why) in COLOURS.items()) + '}\n'
    data = {name: {'value': value, 'use': why} for name, (value, why) in COLOURS.items()}
    return css, json.dumps(data, indent=2) + '\n'


def main():
    css, data = tokens()
    logos = {
        'limoon-slice.svg': colour(),
        'limoon-slice-mono.svg': mono(ident='slice'),
        'limoon-half.svg': colour(half=True, ident='half'),
        'limoon-half-mono.svg': mono(half=True, ident='halfmono'),
        'limoon-loading.svg': colour(loading=True),
    }
    paths = wordmark_paths()
    if paths:
        logos.update({
            'limoon-wordmark.svg': wordmark(INK, paths),
            'limoon-wordmark-on-dark.svg': wordmark(INK_ON_DARK, paths),
            'limoon-lockup.svg': lockup(colour(), INK, paths),
            'limoon-lockup-on-dark.svg': lockup(colour(), INK_ON_DARK, paths),
            'limoon-lockup-half.svg': lockup(colour(half=True, ident='lockhalf'), INK, paths),
            'limoon-lockup-half-on-dark.svg': lockup(colour(half=True, ident='lockhalfdark'), INK_ON_DARK, paths),
        })
    else:
        print('fontTools is not installed: the wordmark and lockups are left as they are')
    for name, svg in logos.items():
        (BRAND / 'logos' / name).write_text(svg)
    (BRAND / 'colors.css').write_text(css)
    (BRAND / 'colors.json').write_text(data)
    # Copies where the app, the docs and the editor extension pick them up.
    copies = {
        'apps/desktop/public/limoon.svg': colour(),
        'apps/desktop/public/limoon-half.svg': colour(half=True),
        'apps/desktop/public/limoon-mono.svg': mono(),
        'apps/desktop/public/limoon-loading.svg': colour(loading=True),
        'docs/assets/limoon.svg': colour(),
        # VS Code tints activity-bar icons itself; any single colour works.
        'extensions/vscode-fdp/media/icon.svg': mono(colour='#c5c5c5'),
    }
    lockups = ['limoon-lockup.svg', 'limoon-lockup-on-dark.svg']
    for name in lockups:
        if (BRAND / 'logos' / name).exists():
            copies['docs/assets/' + name] = (BRAND / 'logos' / name).read_text()
    for path, svg in copies.items():
        (ROOT / path).write_text(svg)
    print(f'wrote {len(logos)} logos, colours and {len(copies)} copies')


if __name__ == '__main__':
    main()
