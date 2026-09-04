# DESIGN.md — Funnel Copilot Design System

Single source of truth for the demo UI. Agents implementing any frontend change
read this first and treat it as the contract. The palette and typography are
drawn from contemporary hearing-care brand aesthetics in general — not
derived from, or cloned from, any single company's live site — and are
brand-INSPIRED, never brand-identifying.

## 0. Brand stance (non-negotiable)

- Visual language may resemble the broader hearing-care category: palette
  family, typographic pairing, flat/bordered component feel. It must NOT
  replicate any real company's trade dress: no logo or wordmark asset from
  any real brand, no cloned hero layouts, no copied imagery.
- The footer always carries: "Independent portfolio demo · synthetic data ·
  not affiliated with any hearing-care company." The header keeps the
  synthetic-data badge.
- Typography uses **Inter** (UI) and **Fraunces** (display), both self-hosted
  (vendored woff2), system-stack fallbacks always present.

## 1. Color tokens — UI

| Token | Value | Use |
|---|---|---|
| --ink | #201F1C | primary text (warm near-black) |
| --ink-soft | #55534E | secondary text |
| --ink-muted | #8A877F | tertiary/labels, timestamps, badges |
| --surface | #FFFFFF | cards, panels |
| --surface-alt | #F5F5F3 | page background (warm off-white) |
| --surface-ink | #17231C | dark SQL blocks (green-cast near-black) |
| --border | #E4E3DE | hairline borders |
| --border-strong | #C9C7BF | inputs, emphasized dividers |
| --action | #CFFB5E | primary CTA fill (signature lime) — ALWAYS with --ink text |
| --action-hover | #C2F13F | CTA hover |
| --accent | #154734 | deep green: links, active states, focus, selected segments |
| --accent-soft | #E7F0EA | subtle green tint: chips, selected backgrounds, hovers |
| --accent-mid | #468254 | secondary green: icons, citation chips, small accents |
| --danger | #B3261E | errors only |
| --warn-bg / --warn-border / --warn-ink | #FDF3D7 / #E9C46A / #7A5A00 | retry badges, warnings |

Rules: lime (--action) is a FILL color only — never lime text, never lime thin
strokes on white (fails contrast). Links and interactive text use --accent.
Focus-visible: 2px --accent ring with 2px offset, everywhere.

## 1b. Color tokens — DARK theme

Implemented as `[data-theme="dark"]` overriding the same custom properties —
components never branch on theme, only tokens change. Toggle in the header
(sun/moon ghost icon button); default follows `prefers-color-scheme`; explicit
choice persisted in localStorage (`fcTheme`). `prefers-reduced-motion` and all
component rules apply identically in both themes.

| Token | Dark value | Note |
|---|---|---|
| --ink | #ECEFEA | primary text |
| --ink-soft | #B7BDB4 | secondary |
| --ink-muted | #868D83 | tertiary |
| --surface | #17231C | cards, panels |
| --surface-alt | #101815 | page background (deep green-black) |
| --surface-ink | #0C1310 | SQL blocks (darker than cards) |
| --border | #26332B | hairlines |
| --border-strong | #3A4A40 | inputs |
| --action | #CFFB5E | UNCHANGED — lime fill, text on it stays #201F1C |
| --action-hover | #DCFC82 | |
| --accent | #7FC79A | links/active/focus — the light-on-dark green (deep #154734 is illegible on dark) |
| --accent-soft | #1E2F26 | chip/selected fills |
| --accent-mid | #5FA878 | icons, small accents |
| --danger | #E5736C | |
| --warn-bg / --warn-border / --warn-ink | #33290F / #8A6D1F / #E9C46A | |

Dark rules: user chat bubble fill = --accent-soft with --ink text (NOT deep
green fill); segmented selected = --accent-soft fill + --accent text + 1px
--accent border; charts keep §2 palette but grid/axis/labels re-read tokens at
render (re-init on theme switch). Focus ring uses --accent in both themes.

## 2. Color — charts (validated, dataviz method)

Categorical, FIXED order, never cycled, assigned to entities not ranks:

1. #2F9E63 (green — brand anchor, first series always)
2. #3D72C0 (blue)
3. #CE7A1A (amber)
4. #8B55C9 (plum)
5. #C24E2A (rust)

Validated on light surface AND dark surface #14201A: lightness band, chroma
floor, CVD separation, normal-vision floor, 3:1 contrast — ALL PASS both modes
(validate_palette.js, 2026-09-03). Same series colors in both themes; only
grid/axis/label tokens change. CVD tritan for green↔blue sits in the 6–8 band —
legal because every multi-series chart carries a legend + tooltips (secondary
encoding), which stays mandatory.
Sequential (magnitude): single green ramp #E7F0EA → #9CCDB0 → #468254 → #154734.
Chart rules (binding): one axis only, never dual-axis; thin marks with 4px
rounded data-ends and 2px gaps between adjacent fills; grid/axis lines in
--border, axis labels in --ink-muted; values/labels/legends wear TEXT tokens,
never series color; legend present for >=2 series, none for single series
(title names it); tooltips on all marks; numbers use tabular-nums.

## 3. Typography

| Role | Font | Weight / size |
|---|---|---|
| Display (welcome title, hero/stat numbers, card titles, section headers) | **Fraunces** (fallback Georgia, serif) | 600; welcome 28px, card title 15px, stat value 34px |
| UI & body (everything else) | **Inter** (fallback -apple-system, Segoe UI, Roboto, sans-serif) | 400/500/700; base 15px, chat 14.5px, small 12px, micro 11px |

Line-height 1.45 body, 1.2 display. Letter-spacing normal; micro-labels may use
+0.02em. Numbers everywhere: font-variant-numeric: tabular-nums.

## 4. Space, radius, elevation

- Spacing on an 8px grid: 4 / 8 / 12 / 16 / 24 / 32. Panels pad 16, cards pad 16,
  dense rows 8. One rhythm — no ad-hoc values.
- Radius: buttons & chips 8px; inputs 10px; cards & panels 16px; pills 999px.
- Elevation: FLAT by default — hairline --border instead of shadows (buttons
  carry no shadow). Exactly one soft shadow token for floating layers
  (drawer, toasts): 0 8px 24px rgba(32,31,28,.10).

## 5. Components

- **Button primary**: --action fill, --ink text, 700, radius 8, padding 10px 20px,
  no shadow; hover --action-hover; active translateY(1px).
- **Button secondary**: --surface fill, 1px --border-strong, --ink text; hover
  --accent-soft.
- **Ghost/icon buttons** (card controls): transparent, --ink-muted icon, hover
  --accent-soft circle.
- **Chips / suggestion cards**: --accent-soft fill, --accent text, radius 8;
  hover deepen. Welcome suggestion cards: --surface + border, Fraunces title line.
- **Segmented control (tiers)** & toggles: selected segment --accent fill with
  white text (NOT lime — text contrast), unselected --surface.
- **Chat bubbles**: user = --accent fill, white text; agent = --surface with
  border (not grey fill), radius 14/2.
- **Trace steps**: --ink-muted, done state stays; context step icon 🧠; secs
  badges --ink-muted micro.
- **SQL blocks**: --surface-ink bg; highlight.js theme re-tuned: keywords #CFFB5E,
  strings #9CCDB0, numbers #E9C46A, comments #7E8B83.
- **Tables**: header --surface-alt, hairline borders, row hover --accent-soft at 40%.
- **Cards (dashboard)**: --surface, border, radius 16, title in Fraunces,
  controls as ghost icons top-right; "Details" summary in --accent.
- **Catalog drawer**: --surface, soft shadow. Layer headers = small pill chips
  (BRONZE/SILVER/GOLD) tinted per layer (bronze #A9743A-soft, silver --ink-muted-
  soft, gold --accent-soft with --accent text). Each table = its own boxed card:
  --surface-alt fill, 1px --border, radius 8, 8px 10px padding, 6px vertical gap;
  monospace table name in --ink 13px; COMMENT text (when present) below in
  --ink-muted 12px, Inter. Hover: --accent-soft.
- **Stat tile**: Fraunces 34px value in --ink, label --ink-muted.
- **Brand lockup (header)**: sound-bars mark + "Funnel Copilot" in
  Fraunces 600 20px + a lighter "Demo" word in --ink-soft. The synthetic-data
  badge stays but compact (micro text, single pill). The mark is OUR OWN inline
  SVG — 5 vertical rounded bars of varying height (a generic audio/equalizer
  glyph, drawn from scratch; never any real brand's icon asset), bars in
  --accent with the center bar --action-darkened #9CC23F in light /
  --action in dark. Size 22×22 in header.
- **Thinking animation**: the SAME sound-bars mark, animated — while an answer
  is streaming (from submit until done/error) the 5 bars oscillate like an
  equalizer (staggered scaleY keyframes, transform-origin bottom, ~0.9s loop,
  ease-in-out). Also rendered inline as the pending indicator in the chat
  bubble placeholder. Idle = static. prefers-reduced-motion: bars static, a
  simple opacity pulse instead.
- **Icon set**: NO emoji anywhere in UI chrome. One consistent inline-SVG set:
  24×24 viewBox, stroke currentColor 1.6px, round caps/joins, no fills (except
  the brand mark). Needed glyphs: plan/list, database (SQL), play/run, chart,
  memory/brain-circuit, book/catalog, download, csv/table, close ×, chevron
  down/right, sun, moon, grip-vertical (splitter), dot/status, check, alert.
  Icons inherit text color of their context (--ink-muted default, --accent on
  hover/active).
- **Splitter (chat ↔ dashboard)**: 10px hit-area vertical handle between the
  two panels with a grip-vertical icon at its center, cursor col-resize;
  drag adjusts the chat panel flex-basis live (pointer events + requestAnimation-
  Frame), clamped 320px–60% of viewport; double-click resets to default; width
  persisted in localStorage (fcSplit). Handle idle = transparent with --border
  center line; hover/drag = --accent line. Hidden on mobile (stacked layout).
  Charts resize on drag end (and throttled during drag).
- **Connection indicator**: header, next to the tier segmented control. An 8px
  status dot + micro label. Polls GET /health every 30s (and once on load):
  ok + driver databricks → dot #2F9E63, label "Databricks"; ok + duckdb → dot
  --ink-muted, label "DuckDB local"; fetch fails/timeout → dot --danger, label
  "Offline", retry sooner (10s). Dot gets a soft 2s pulse only when state
  CHANGES. Tooltip (title) carries driver + LLM + tier detail.
- **Trace panel (Claude-style)**: while streaming, steps append live as rows
  (unchanged behavior). When the answer completes, the trace COLLAPSES to a
  single summary line inside a bordered rounded container: "<n> steps ·
  <total>s" with a chevron — clicking expands the full step list; each step is
  its own hairline-separated row (icon + label + secs badge right-aligned,
  chevron for steps that carry payloads like SQL). Collapsed by default for
  every finished answer, expanded state remembered per message while the page
  lives. Keyboard accessible (button + aria-expanded).

## 6. Motion

120-180ms ease-out for hovers/toggles; existing step-in animation kept at 180ms;
drawer slides 200ms; theme switch: none (instant token swap); trace collapse/
expand 180ms; equalizer bars ~0.9s loop ONLY while streaming; status dot pulse
2s ONLY on state change. Nothing else animates. No parallax, no bounce, no
confetti. prefers-reduced-motion: reduce → transitions off, equalizer becomes
an opacity pulse, dot pulse off.

## 7. Accessibility

WCAG AA minimum: --ink on --surface 15.8:1; --accent on white 9.6:1; lime is
never a text color; all interactive elements have :focus-visible rings and
>=32px hit targets; charts follow §2 (legend + text tokens + tooltips);
language toggle preserved (EN/TR full coverage).

## 8. Do / Don't

DO keep the layout structure and every feature exactly as-is — this system
restyles, it does not redesign. DO keep it calm: max one lime element in view
per region (the primary action). DON'T add gradients, glassmorphism, or
decorative icons beyond the §5 icon set. DON'T introduce new hues outside
§1/§1b/§2. DON'T let any agent "improve" spacing off-grid. Dark mode exists
ONLY as the §1b token swap — never per-component dark overrides. DON'T use any
third party's icon/logo asset — every glyph is drawn inline in this codebase.
