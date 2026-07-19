# GPU Broker — Native macOS Material Design System

## Product context

- Subject: a local shared-GPU control plane for infrastructure operators and research teams.
- Primary user: the person who needs to see which GPU capacity is safe to use, who owns it, and what needs attention before making a claim.
- Single job of the overview: make resource availability legible at a glance, then make the next safe action obvious.
- Platform: macOS desktop only. The target implementation is native SwiftUI/AppKit, not a browser page and not a WKWebView skin.
- Reference: Apple Home for macOS — the [Apple Home user guide](https://support.apple.com/zh-cn/guide/home/welcome/mac) shows a spatially grouped control surface with category chips, rooms/sections, cameras, scenes, and accessory tiles.

## Design decision

Create a new design style rather than preserve the present web-dashboard style. The visual reference is the translucent, calm, room-based material language of Apple Home, translated to an operations console.

### Signature

**Capacity weather** — a quiet, deliberately blurred live backdrop whose cool or warm cast reflects fleet health. It is visible only through translucent native materials; numbers and operational controls always sit on high-contrast frosted surfaces. This turns background from generic decoration into a non-blocking first signal of fleet state.

## Tokens

### Palette

| Token | Value | Use |
| --- | --- | --- |
| Fog blue | `#A8C0CD` | primary cool ambient field / selected navigation context |
| Harbor slate | `#3B5662` | sidebar selected state, strong header surfaces, primary dark text |
| Frost white | `#F7FBFC` at 78–92% | elevated glass cards and input material |
| Sea glass | `#DDF0EF` | quiet selected cards and safe secondary surfaces |
| Clear cyan | `#00B8D9` | primary interactive accent and safe control glyphs |
| Signal green | `#2BA56C` | available / healthy state |
| Amber | `#D79422` | capacity warning / stale data |
| Coral | `#D7645B` | error / conflict |
| Ink | `#233943` | primary text |

Do not introduce purple, neon, black-page, warm-cream, or generic gradient treatments. State colors are semantic support, never the only state signal.

### Typography

- Display: SF Pro Display / `system`, 28–32pt semibold for page title only.
- Body: SF Pro Text / `system`, 13–15pt with 19–22pt leading.
- Utility and data: SF Mono / `monospaced`, 11–13pt for SSH commands, GPU IDs, timestamps, and numeric meters.
- Use Chinese SF system fallback (PingFang SC) naturally; no decorative or web font pairings.
- Dense data gets tabular figures. Page titles use -0.02em visual tracking; utility text stays normal.

### Shape, spacing, material

- 8pt base grid. Typical horizontal rhythm: 16 / 20 / 24 / 32pt.
- 12pt radius for chips and compact controls; 16pt for cards; 20pt for major grouped surfaces.
- Native material: `.regularMaterial` for workspace panels, `.thickMaterial` only where text crosses the live ambient background, and no blanket blur on opaque content.
- Hairline separators use `#FFFFFF` at 22% over dark material or `#365B66` at 14% over light material.
- Shadows are subtle and low: 0 10 28 / 12% harbor slate; never hard drop shadows.

### Icons

- Use SF Symbols only, in one rounded-outline / hierarchical rendering mode.
- Standard icon size: 16pt in navigation, 18–20pt on cards, 22pt in primary status tiles.
- Icon color matches the semantic state only inside a soft, rounded 32pt tile; otherwise it inherits the text hierarchy.
- No mixed icon families, emoji, bespoke glyphs, or multi-colour illustration style.

### Motion

- One coherent 160–220ms ease-out transition for selection, panel expansion, and status refresh.
- Availability-state updates crossfade; never pulse constantly.
- Respect Reduce Motion. No decorative animated gradients or parallax.

## Application architecture and layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ macOS unified titlebar + clear page title + command controls             │
├──────────────┬───────────────────────────────────────────────────────────┤
│ frosted      │ Capacity weather ambience, intentionally blurred          │
│ sidebar      │                                                           │
│ GPU Broker   │ [fleet health chips]                                      │
│ Overview     │                                                           │
│ GPU status   │ 资源总览                                                   │
│ Claims       │ [availability scene] [active claims scene] [needs action] │
│ Schedule     │                                                           │
│              │ 服务器池                                                  │
│ Management   │ [ssh user@host] [GPU availability] [meters] [action]      │
│              │     └ expanded GPU accessory tiles                        │
└──────────────┴───────────────────────────────────────────────────────────┘
```

- Sidebar: 236pt, blue-gray translucent material; grouped navigation; a distinct selected capsule; the live-connection state sits at the foot.
- Title bar: one crisp title, not a glassy low-contrast label. Actor identity is a compact menu/control on the right.
- Main region: a soft, blurred capacity-weather field beneath two or three grouped content areas. Do not put a large opaque card across the entire window.
- Top state chips: compact, rounded, icon-led status chips in one line, like Apple Home categories. Use real fleet state: Available, Claimed, Running, Needs attention.
- Server pool: treat each endpoint as a “room” group. The primary identity is the exact `ssh -p <port> <user>@<host>` command in SF Mono; the endpoint ID is secondary metadata.
- Expanded GPU items: accessory-style cards. Each has one icon tile, GPU model/index, state wording, VRAM, utilization, ownership, and a chevron / more action.
- Do not duplicate CPU/memory labels in the left identity cell. Count headers fully describe the availability numbers. The resource column contains visual meters only.
- Column header must be opaque enough for readability: a harbor-slate 90% material with white text and a 1px low-contrast separator, never a blurred mist.

## Content vocabulary

Use concise, user-facing Chinese based on resource action, not implementation jargon:

| Purpose | Preferred wording |
| --- | --- |
| main page | 资源总览 |
| active fleet state | 可用、已认领、运行中、需处理 |
| server group | 服务器池 |
| server primary label | exact SSH command |
| server secondary label | 服务器名 · 在线 / 数据陈旧 / 连接错误 |
| table headers | 连接、GPU 可用性、资源、操作 |
| immediate claim | 认领 GPU |
| inspect detail | 查看详情 |
| no data | 暂无服务器。添加 SSH 连接以开始监控。 |
| bad state | 数据需要刷新。检查服务器连接或采集状态。 |

Keep a label’s meaning stable across summary, card, dialog, and confirmation message.

## Accessibility and clarity floor

- Text on material meets a 4.5:1 contrast target on its composed background.
- Every colored status also has a short text label and matching SF Symbol.
- Keyboard focus uses Clear Cyan with a 3pt visible outline.
- Status meters include textual percentage in the accessibility label, but visual density stays low.
- Do not convey operational actions only with hover states; critical controls stay visible.
- The title, table header, SSH command, and status wording must remain crisp even while the ambient background blurs.

## Native implementation boundary

The target native shell is a SwiftUI/AppKit split view with `NSVisualEffectView` / SwiftUI materials and SF Symbols. It consumes the existing loopback REST API. Once native parity is achieved, remove the browser-facing Jinja templates, JavaScript, CSS, static asset routing, and WKWebView hosting path; retain only REST/MCP/CLI contracts and the desktop process lifecycle.
