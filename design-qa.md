# Design QA — Apple Home multi-cluster resource space

## Evidence

- Source visual truth: `qa-apple-home-reference.png` — Apple Home for macOS 26 ([official guide](https://support.apple.com/guide/home/welcome/mac)).
- Typography guidance: Apple HIG [Typography](https://developer.apple.com/design/human-interface-guidelines/typography) — system font families, semantic Regular/Medium/Semibold/Bold weights, and hierarchy through size, weight, and color.
- Data-display guidance: Apple HIG [Charts](https://developer.apple.com/design/human-interface-guidelines/charts) and [Charting data](https://developer.apple.com/design/human-interface-guidelines/charting-data) — keep data prominent, labels secondary, and use compact bar marks for glanceable comparison.
- Icon guidance: Apple HIG [Icons](https://developer.apple.com/design/human-interface-guidelines/icons), [SF Symbols](https://developer.apple.com/design/human-interface-guidelines/sf-symbols), and [Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars) — use familiar simplified metaphors, match symbol weight to adjacent text, optically align different shapes, and let the accent color communicate selection.
- Background source: `src/gpu_broker/web/static/assets/server-room-background.jpg` — Cookiecutter on [Pexels](https://www.pexels.com/photo/computer-server-in-data-center-room-17489163/).
- Browser-rendered implementation: `qa-apple-home-dashboard.jpg`.
- Full-view comparison: `qa-apple-home-comparison.jpg`.
- Expanded interaction capture: `qa-cluster-expanded.jpg`.
- Focused comparison: `qa-cluster-expanded-comparison.jpg`.
- Typography/data-bar before-and-after comparison: `qa-typography-comparison.jpg`.
- Earlier text-only aggregate: `qa-typography-before.jpg`.
- Iconography before-and-after comparison: `qa-iconography-comparison.jpg`.
- Earlier icon state: `qa-iconography-before.jpg`.
- App icon source visual truth: `desktop/assets/GPU Broker Icon.png`.
- Packaged app icon rendering: `qa-app-icon-rendered.png`, decoded from `dist/GPU Broker.app/Contents/Resources/GPU Broker.icns`.
- App icon full-view and focused small-size comparison: `qa-app-icon-comparison.jpg`.
- Earlier high-detail state: `qa-density-before-expanded-all.jpg`.
- Viewport: 1150 × 647 CSS pixels.
- State: light theme, actor `human`, five online clusters, 40 GPUs, two busy unmanaged GPUs, all clusters collapsed by default.

## Findings

- No open P0, P1, or P2 findings.
- P3: the Apple Home reference uses shorter accessory names and therefore fits more individual tiles. GPU Broker intentionally keeps model, memory, utilization, owner, and task details only in the expanded state because those facts affect safe resource decisions.
- P3: the Web UI uses the locally bundled Phosphor symbol font rather than Apple SF Symbols vectors. The selected metaphors, optical boxes, monochrome sidebar treatment, accent selection, and circular hierarchical content treatment follow the same principles without copying or redistributing Apple assets.
- P3: the command-line desktop build ships a flattened `.icns` rather than a layered Icon Composer asset. The finished continuous-corner silhouette, centered graphics-card metaphor, restrained depth, and small-size legibility preserve the requested macOS character within that packaging constraint.

## Fidelity surfaces

- Fonts and typography: computed browser styles use `-apple-system`/SF Pro Text with PingFang SC fallback; the display title uses the SF Pro Display stack at 29 px/700, cluster names use 13.5 px/600, operational text uses 10–14 px Regular/Medium/Semibold weights, and changing values use tabular numbers. Arbitrary intermediate weights were removed from the primary hierarchy.
- Spacing and layout: five 56 px cluster rows and 8 px gaps fit in the initial desktop viewport; single-GPU tiles add vertical space only after explicit expansion.
- Colors and tokens: the light photographic environment, dark glass navigation, white translucent cluster rows, and restrained blue/green/amber states remain aligned with the Apple Home source. Memory and utilization use separate solid-color data bars rather than decorative gradients.
- Image quality: the real server-room photograph is correctly cropped, softly blurred, and washed for contrast; no generated image, CSS drawing, placeholder art, or custom SVG replaces a source asset.
- Copy and content: “集群调度”, “展开 GPU”, capacity counts, utilization, and memory pressure describe the operational hierarchy directly. Compact bars preserve exact percentage labels, so the visualization never replaces the value. The coordination-only safety boundary remains visible.
- Icons: all visible interface icons use one local Phosphor Regular family. Sidebar glyphs share a 19 px optical box and 16 px symbol size; selected navigation relies on the system accent instead of fixed multicolor icons. Summary symbols use a 30 px circular hierarchical treatment with full-opacity glyphs over low-opacity semantic tints, while the active filter receives the accent fill. Complex or unclear metaphors were replaced with direct equivalents: graphics card, grid, users, warning, gear, user-plus, storage, check, waveform, and user.
- App icon: the 1024 px source and packaged `.icns` match visually in the combined comparison. The icon uses the same local Phosphor `graphics-card` asset as the product, a centered white silhouette, a calm blue field, a continuous macOS-style corner profile, and a subtle highlight/shadow hierarchy. The focused 128/64/32/16 px strip remains recognizable without text or fine decorative detail.
- Accessibility and responsiveness: cluster rows are semantic buttons with `aria-expanded`; state is communicated by text and icons as well as color; focus and reduced-motion rules remain; narrow layouts hide aggregate columns before controls collide.

## Comparison history

1. Initial density finding — P1.
   - Evidence: `qa-density-before-expanded-all.jpg` shows the detailed GPU state consuming the viewport, preventing simultaneous comparison of multiple clusters.
   - Fix: changed the default to collapsed cluster summaries, added total/available/busy/claimed/abnormal counts and aggregate memory/utilization, shortened KPI and section spacing, and moved per-GPU cards behind “展开 GPU”.
   - Post-fix evidence: `qa-apple-home-dashboard.jpg` shows all five clusters plus the start of the coordination section in one 1150 × 647 viewport.
2. First post-fix visual pass — P2.
   - Evidence: collapsed `.gpu-tiles` retained vertical margins, creating unnecessary gaps and partially hiding the fifth cluster.
   - Fix: apply GPU grid margins only to expanded clusters.
   - Post-fix evidence: `qa-apple-home-comparison.jpg` shows five evenly spaced cluster summaries fully visible.
3. Final focused pass.
   - Evidence: `qa-cluster-expanded-comparison.jpg` compares Apple Home room/accessory grouping with one expanded GPU cluster.
   - Result: the overview remains compact while the expanded state preserves readable individual GPU status and ownership. No actionable P0/P1/P2 differences remain.
4. Typography and data-display pass — P2.
   - Evidence: `qa-typography-before.jpg` and the user's review identified weak type hierarchy, very small aggregate labels, and text-only memory/utilization values that lacked glanceable comparison.
   - Fix: adopted the Apple system font stack with PingFang SC fallback, standardized the primary hierarchy to Regular/Medium/Semibold/Bold weights, raised small operational text, enabled tabular numbers, and added two labeled bar marks per cluster for memory and utilization.
   - Post-fix evidence: `qa-typography-comparison.jpg` shows stronger hierarchy and faster cross-cluster load comparison without increasing row height; `qa-apple-home-comparison.jpg` confirms that all five clusters remain visible.
5. Iconography pass — P2.
   - Evidence: `qa-iconography-before.jpg` used mixed metaphors such as CPU, circles-four, users-three, warning-circle, gear-six, hand-pointing, check-circle, activity, and user-focus. Their detail density and optical weight varied at small macOS sizes.
   - Fix: mapped actions and destinations to simpler familiar symbols, established shared sidebar optical boxes, converted summary icon containers to circular hierarchical treatments, and applied the accent fill only to the selected filter.
   - Post-fix evidence: `qa-iconography-comparison.jpg` shows more consistent weight, baseline, silhouette complexity, and selected-state emphasis while preserving the same density.
6. Desktop app icon and discovery pass.
   - Evidence: the application bundle previously had no `CFBundleIconFile`, no packaged icon resource, and was only available under `dist/GPU Broker.app`.
   - Fix: added a 1024 px source icon and complete `.icns`, packaged it through `desktop/build-macos-app.sh`, declared it in `Info.plist`, and created a guarded root-level `GPU Broker.app` symlink after each build. README now exposes that entry above the fold.
   - Post-fix evidence: `qa-app-icon-comparison.jpg` shows source-to-package fidelity and legibility down to 16 px; filesystem and metadata checks confirm the root entry resolves to an application bundle whose `CFBundleIconFile` is `GPU Broker`.

## Interaction checks

- Default view rendered five clusters and zero GPU tiles.
- Expanding `gpu-node-2` rendered eight GPU tiles, changed its control to “收起 GPU”, and preserved the other clusters as summaries.
- “展开全部” rendered all 40 GPU tiles and changed to “全部收起”.
- Opening a GPU tile displayed `GPU 0 · NVIDIA H100 80GB HBM3` in the detail drawer.
- Searching `10.40.1.225` reduced the overview to one matching cluster.
- Ten data bars rendered for five clusters; each exposes an exact text percentage and an accessible label such as “显存 9%”.
- Browser-computed typography confirmed the system/SF Pro stack, 29 px/700 title, and 13.5 px/600 cluster names.
- Five summary symbols and nine sidebar symbols rendered from the same local family; selecting “占用” changed its symbol to the accent treatment and returned the two expected clusters.
- Browser console errors: none.

## Automated verification

- `PYTHONPATH=src uv run --extra dev --no-editable pytest -q tests/test_api_gui_mcp.py` — 7 passed.
- `uv run --extra dev --no-editable ruff check .` — passed.
- `node --check src/gpu_broker/web/static/app.js` — passed.
- `zsh -n desktop/build-macos-app.sh` — passed.
- `zsh desktop/build-macos-app.sh` — passed; rebuilt `dist/GPU Broker.app`, packaged `Contents/Resources/GPU Broker.icns`, and refreshed root `GPU Broker.app`.
- Root-entry checks — passed: symlink target `dist/GPU Broker.app`, application bundle type `com.apple.application-bundle`, icon resource present, and `CFBundleIconFile=GPU Broker`.

final result: passed
