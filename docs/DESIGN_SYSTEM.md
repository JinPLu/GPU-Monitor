# GPU Broker — Native macOS Material Design System

## Product context

- Subject: a single-user local control plane for managing compute resources across several servers, projects, and Agents.
- Primary user: one person who needs to see available GPU/CPU capacity, decide which project or Agent receives it, and resolve blocked work.
- Not a multi-user product: actor IDs are local audit and Agent labels, not people, seats, teams, or accounts.
- Single job of the overview: show what compute is available and which project or Agent needs attention, then make the next safe action obvious.
- Primary desktop experience: native macOS SwiftUI/AppKit, not a browser page and not a WKWebView skin. The loopback Web/Jinja/JavaScript surface remains a supported Windows desktop compatibility path and must keep using the same REST/domain contracts.
- Reference: Apple Home for macOS for spatial grouping, Apple's official [Color](https://developer.apple.com/design/human-interface-guidelines/color), [Materials](https://developer.apple.com/design/human-interface-guidelines/materials), and [Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars) guidance, plus the concrete neutral/accent/spacing tokens summarized by [Open Design's Apple system](https://open-design.ai/zh/plugins/design-system-apple/).

## Design decision

Preserve the attractive native direction already established: a translucent, calm, room-based Apple Home style translated to an operations console. Product fixes should mature the interaction and copy without flattening the interface into a plain engineering table. The current composition is **Spatial Operations Desk**: an Apple-neutral graphite/fog field over the warm compute-studio image, with transparent material layers for navigation, collection, and detail.

### Signature

**Spatial Operations Desk** — the background keeps the source image's cool daylight and warm practical-light contrast, then softens it behind native material. Foreground panels use neutral fog-white or graphite glass so the image passes through without competing with data. Operational meaning comes from content density, semantic color, icons, and text. Do not use drifting orbs, parallax, animated gradients, or health-tinted haze as the primary state signal.

## Tokens

### Palette

| Token | Native source | Use |
| --- | --- | --- |
| Primary label | `NSColor.labelColor` | titles and primary text |
| Secondary label | `NSColor.secondaryLabelColor` | metadata and helper text |
| Ambient field | Apple fog `#F5F5F7` in light appearance; neutral graphite near `#1D1D1F` in dark appearance | unified app field behind the source image |
| Glass surface | native material plus a light neutral tint, not an opaque custom board | navigation, collections, detail, and command island |
| Interaction | `NSColor.controlAccentColor` | app icon, selected navigation, primary button, lease/GPU icons, meters, focus, and global tint |
| Healthy | `systemGreen` | available / healthy state |
| Attention | `systemOrange` | waiting / stale state |
| Destructive | `systemRed` | error, conflict, confirmed destructive action |

Use dynamic system colors for text, interaction, and semantic states so vibrancy, contrast, dark appearance, and the user's macOS accent preference remain coherent. System blue is the default Apple-like interaction reading, while `controlAccentColor` preserves platform behavior. Green, orange, and red are narrowed to semantic state only. Do not use `controlBackgroundColor` or opaque white fills as broad desk surfaces; light appearance should read as fog-white material, while dark appearance should read as neutral graphite material rather than blue-black or tinted “tech” panels. Color is semantic support, never the only state signal.

### Typography

- Display: SF Pro Display / `system`, 28–32pt semibold for page title or overview hero only.
- Body: SF Pro Text / `system`, 13–15pt with 19–22pt leading.
- Utility and data: SF Mono / `monospaced`, 11–13pt for SSH commands, GPU IDs, timestamps, and numeric meters.
- Use Chinese SF system fallback (PingFang SC) naturally; no decorative or web font pairings.
- Dense data gets tabular figures. Page titles use -0.02em visual tracking; utility text stays normal.

### Shape, spacing, material

- 8pt base grid. Typical horizontal rhythm: 16 / 20 / 24 / 32pt.
- 12pt radius for chips and compact controls; 16pt for cards; 20pt for major grouped surfaces.
- Native material: navigation, collections, and details live in place on same-hue transparent material, with three depth levels: navigation is the deepest translucent layer, collections sit on a denser translucent layer, and details or destructive confirmations use the clearest/highest-contrast glass layer. Do not create a single full-board foreground plate; use `.thickMaterial` only where contrast needs support, and no blanket blur on opaque content.
- Hairline separators are weaker than the glass fill and should read only as edge hints, not frames.
- Outer strokes and shadows are weak. Hierarchy comes from transparency, content density, and semantic color, not from heavy outlines, white fills, or hard drop shadows.
- The bottom command island floats inside the desk as the primary action surface. It should feel attached to the current page, not like a separate alert or another sidebar.

### Icons

- Use SF Symbols only, in one rounded-outline / hierarchical rendering mode.
- Standard icon size: 16pt in navigation, 18–20pt on cards, 22pt in primary status tiles.
- Icon color matches the semantic state only inside a soft, rounded 32pt tile; otherwise it inherits the text hierarchy.
- No mixed icon families, emoji, bespoke glyphs, or multi-colour illustration style.
- Custom button styles read SwiftUI's native `isEnabled` environment so disabled controls lose emphasis instead of retaining a bright interaction fill.

### Motion

- One coherent 160–220ms ease-out transition for selection, panel expansion, and status refresh.
- Availability-state updates crossfade; never pulse constantly.
- Collection rows and tiles may lift or tint subtly on hover. Respect Reduce Motion by disabling nonessential movement while keeping color/opacity feedback. No decorative animated gradients, drifting blobs, or parallax.

## Application architecture and layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ macOS unified titlebar + clear page title + command controls             │
├──────────────┬───────────────────────────────────────────────────────────┤
│ frosted      │ unified warm gray smoke-brown field with soft texture     │
│ sidebar      │                                                           │
│ GPU Broker   │ 总览 [summary] [attention] [freshness]                   │
│ 总览          │ 服务器 [server collection] → [server detail]             │
│ 服务器        │ 项目与 Agent [allocation collection] → [detail]          │
│ 项目与 Agent  │                                                           │
│ 本机设置      │                                      [command island]    │
└──────────────┴───────────────────────────────────────────────────────────┘
```

- Sidebar: 236–246pt regular system material; grouped navigation for real pages only; selection uses the macOS control accent tint; the live-connection state sits compactly at the foot without another floating card.
- Title bar: one crisp title, not a glassy low-contrast label. The local audit/Agent label is a compact control on the right; it must never read as a user switcher.
- Window: the initial size must fit within the visible screen area, accounting for menu bar and Dock. Prefer a polished first launch over a fixed oversized window.
- Main region: the neutral ambient background fills the whole window, and the main content shares the window boundary. Preserve the compute-studio image's blue daylight and amber practical light at restrained saturation, following Apple Home's use of real rooms behind material. There is no outer margin, 30pt full-board corner radius, full-board stroke, or large desk shadow. Background texture should pass through every foreground layer; do not place opaque white or `controlBackgroundColor` boards over it, and do not remove the atmospheric visual layer when fixing interactions.
- Overview: a first-screen summary with available, allocated, occupied, and needs-attention counts; include concrete attention rows when action is needed instead of a duplicate quick-action strip. Show at most eight exception rows on the overview, then provide one clearly labelled jump to the server list so an exception storm cannot bury server and project context.
- Command island: refresh, add server, and request GPU are the global command owners. Avoid repeating the same quick actions in both sidebar and overview.
- Top state chips or hero stats: compact, rounded, icon-led status items in one line. Use real fleet state: Available, Allocated, Occupied, Needs attention.
- Server list: use collection + detail. Treat each endpoint as a “room” group in the collection. The primary identity is the exact `ssh -p <port> <user>@<host>` command in SF Mono; the endpoint ID is secondary metadata. Selecting a server opens a clearer detail layer with metrics, GPU list, and removal state.
- Server detail: use a real detail sheet with endpoint identity, live metrics, GPU list, and a visible remove action. Removal must stay disabled when the connected service does not advertise deletion support.
- Project and Agent page: use collection + detail with a compact count strip. The project is the primary group, with Agent and task as alternate views. Show allocated, running, and queued resources in user language; reserve “lease” for technical detail. “归还” is confirmed and queued requests remain informational unless a dedicated cancel command exists.
- Expanded GPU items: accessory-style cards or stable numbered controls. Each has one icon tile or number, GPU model/index, state wording, VRAM, utilization, and ownership.
- Keep CPU, system memory, VRAM, and GPU utilization in the resource column. Each meter should pair the percentage bar with absolute values such as available CPU cores, available/total memory, or used/total VRAM; do not move those labels into the left identity cell.
- Dense headers and selected rows can increase opacity for readability, but must stay in the same warm field. Avoid white slabs, heavy borders, and isolated cool-gray panels.

## Content vocabulary

Use concise, user-facing Chinese based on resource action, not implementation jargon:

| Purpose | Preferred wording |
| --- | --- |
| main page | 总览 / 我的计算资源 |
| active fleet state | 可分配、已分配、运行中、需处理 |
| server group | 服务器 |
| server primary label | exact SSH command |
| server secondary label | 在线 / 数据陈旧 / 连接异常 |
| table headers | 连接、GPU 可用性、资源、操作 |
| project and Agent page | 项目与 Agent |
| immediate allocation request | 申请 GPU |
| inspect detail | 查看详情 |
| no data | 还没有服务器。添加第一台服务器开始监控。 |
| add server | 加入本机资源池 |
| remove server | 移除服务器 |
| old service | 当前本机服务版本不支持移除。更新本机服务后即可使用。 |
| local actor label | 本机操作标识 / 记录为（不是用户账号） |
| coordination boundary | 只协调资源，不执行任务。 |
| bad state | 当前无法读取 GPU 状态。检查服务器连接或采集状态。 |

Keep a label’s meaning stable across summary, card, dialog, and confirmation message.
Copy should sound like local desktop product UI, not an agent log. Prefer direct nouns and actions; avoid protocol names, nested error envelopes, and English backend reason codes in visible text unless they are the only exact identifier the operator needs.

## Accessibility and clarity floor

- Text on material meets a 4.5:1 contrast target on its composed background.
- Every colored status also has a short text label and matching SF Symbol.
- Keyboard focus uses Clear Cyan with a 3pt visible outline.
- Status meters include textual percentage in the accessibility label, but visual density stays low.
- Do not convey operational actions only with hover states; critical controls stay visible.
- The title, SSH command, status wording, and destructive action text must remain crisp even while the ambient background blurs.

## Native implementation boundary

The target native shell is a SwiftUI/AppKit split view with `NSVisualEffectView` / SwiftUI materials and SF Symbols. It consumes the existing loopback REST API. Native parity may retire duplicated macOS-only presentation code, but it must not remove the browser-facing Jinja templates, JavaScript, CSS, static asset routing, or loopback Web UI while the Windows launcher owns that compatibility surface.

## Beszel reference boundary

GPU Broker may borrow Beszel's system-first information architecture, dense sortable tables, field visibility controls, compact endpoint information bar, on-demand history, request-generation protection, ownership markers, and its visible-only chart discipline. Native trend charts use timestamped samples, explicit gaps, no decorative animation, and a bounded selected-sample inspector; hidden details do not fetch or redraw histories. It must not import Beszel's React/PocketBase runtime, authentication and registration model, target/claim/reaper control plane, browser storage, or arbitrary command adapters. The Broker REST API remains the presentation boundary and `service.py` remains the allocation truth.

The first native delivery presents current truth: resources, endpoint/GPU detail, attention, leases, requests, reservations, and current ownership arrangements. Time-series telemetry is an additive, capability-gated surface. A queue entry without a concrete GPU allocation belongs on an unassigned lane; it must never be drawn as historical ownership of a GPU.

## Adapter presentation boundary

Vendor adapter identities are diagnostic provenance, not an operator or Agent workflow. The native UI and MCP continue to expose unified resource monitoring, claim, queue, reservation, and scheduler concepts. Adding an adapter must not add a discovery/configuration round trip to a routine claim, create a second credential system, or permit the adapter to write lease/claim truth.
