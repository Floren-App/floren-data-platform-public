# limoon. brand

**Limoon** is a play on the Dutch *limoen* (lime): said by an English speaker, *limoon* sounds the same. The mark is
a lime slice. The platform's green was already a lime.

## Name

- In running text: **Limoon**.
- As a logo, window title and on the opening screen: **limoon.**, lowercase. Styled wordmarks use
  `#6cb52e` for the period in both light and dark themes, including app navigation and the docs header.
- Technical names keep their `fdp` prefix (images `fdp-*`, the `fdp` SDK, `FDP_*` settings, the Terraform provider
  `floren-app/fdp`). The domain stays floren.app.

## Logos

All logos are SVG, in [`logos/`](logos/).

| File | Use |
|---|---|
| `limoon-slice.svg` | **The logo.** App icon, favicon, avatars, anywhere one mark stands alone. |
| `limoon-half.svg` | The alternative logo: half a slice, the cut rising 45° to the right. Where a whole slice is too heavy. |
| `limoon-slice-mono.svg`, `limoon-half-mono.svg` | One colour (`currentColor`), for places that take a single colour: editor icons, print, embossing. Not the main logo. |
| `limoon-wordmark.svg`, `limoon-wordmark-on-dark.svg` | The name alone, drawn as outlines (no font needed). |
| `limoon-lockup.svg`, `limoon-lockup-on-dark.svg` | Slice and name side by side: headers, README, slides. |
| `limoon-lockup-half.svg`, `limoon-lockup-half-on-dark.svg` | The same with the half slice. |
| `limoon-loading.svg` | The loading slice: its segments light up in turn. |

Only the loading slice moves. Use the `-on-dark` versions on dark backgrounds. Leave clear space around a logo of at
least a quarter of the slice's height, and don't recolour, stretch, rotate or outline the marks.

## Colours

See [`colors.css`](colors.css) and [`colors.json`](colors.json).

| Token | Value | Use |
|---|---|---|
| rind | `#4a8a1f` | The outside of the slice |
| pith | `#f2f9dc` | The pale lines and pith |
| flesh | `#a3d44a` | The segments |
| accent | `#6cb52e` | The wordmark's period and the app's controls |
| ink | `#1d2414` | The wordmark on light backgrounds |
| ink on dark | `#f2f2f2` | The wordmark on dark backgrounds |

The slice keeps the same colours in light and dark themes.

## Typeface

**Fredoka**, SemiBold (600) for the wordmark and Medium (500) where a softer weight fits. Files are in [`font/`](font/),
under the SIL Open Font License 1.1 ([`font/OFL.txt`](font/OFL.txt)). The app loads it from the `@fontsource/fredoka`
package, so nothing is fetched from the web. Body text in the app stays in the system font.

## Loading

In a page or panel the loading slice stands above the text, at 40 px; in tight spots (the sidebar, inline status)
it sits beside the text, at 18 px. The opening screen shows the slice and the wordmark while the app starts.

## Changing the brand

Every file here, and the copies in the app (`apps/desktop/public`), the docs (`docs/assets`) and the editor
extension, comes from [`build.py`](build.py), which draws the slice in the same way as
`apps/desktop/src/components/Brand.tsx`. Change the drawing there and in `Brand.tsx`, then run:

```sh
pip install fonttools   # for the wordmark outlines
python3 brand/build.py
```

The desktop app's icon set (`apps/desktop/src-tauri/icons`) is made from `limoon-slice.svg` with
`npx tauri icon` after rendering it to a 1024 px PNG.
