# GPU Broker — Native macOS Material Design System

## Product context

- Subject: a local shared-GPU control plane for infrastructure operators and research teams.
- Primary user: the person who needs to see which GPU capacity is safe to use, who owns it, and what needs attention before making a claim.
- Single job of the overview: make resource availability legible at a glance, then make the next safe action obvious.
- Platform: macOS desktop only. The target implementation is native SwiftUI/AppKit, not a browser page and not a WKWebView skin.
- Reference: Apple Home for macOS for spatial grouping, Apple's official [Color](https://developer.apple.com/design/human-interface-guidelines/color), [Materials](https://developer.apple.com/design/human-interface-guidelines/materials), and [Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars) guidance, plus the concrete neutral/accent/spacing tokens summarized by [Open Design's Apple system](https://open-design.ai/zh/plugins/design-system-apple/).

## Design decision

Preserve the attractive native direction already established: a translucent, calm, room-based Apple Home style translated to an operations console. Product fixes should mature the interaction and copy without flattening the interface into a plain engineering table. The current composition is **Spatial Operations Desk**: one warm gray smoke-brown field, with transparent same-hue glass layers for navigation, collection, and detail.

### Signature

**Spatial Operations Desk** — the background is first unified into a warm gray smoke-brown color field. Any source texture is desaturated, softened, and color-graded into that field before UI is placed on top. Foreground panels are same-hue transparent glass, so the background texture passes through instead of being covered by white boards. Operational meaning comes from content density, semantic color, icons, and text. Do not use drifting orbs, parallax, animated gradients, or health-tinted haze as the primary state signal.

## Tokens

### Palette

| Token | Native source | Use |
| --- | --- | --- |
| Primary label | `NSColor.labelColor` | titles and primary text |
| Secondary label | `NSColor.secondaryLabelColor` | metadata and helper text |
| Warm field | smoke-brown neutral derived from the background | unified app field and glass tint |
| Glass surface | same-hue translucent material, not `controlBackgroundColor` | navigation, collections, detail, and command island |
| Interaction | adaptive smoke rose | deep in light appearance and brighter in dark appearance; app icon, selected navigation, primary button, lease/GPU icons, and global tint |
| Healthy | `systemGreen` | available / healthy state |
| Attention | `systemOrange` | waiting / stale state |
| Destructive | `systemRed` | error, conflict, confirmed destructive action |

Do not hard-code a blue-gray brand palette. Use dynamic system colors for text and semantic states so vibrancy, contrast, and dark appearance remain coherent. The app's interaction tint is smoke rose, not system blue: it stays deep in light appearance and becomes brighter in dark appearance, and appears in the app icon, selected state, primary button, lease/GPU icons, and global tint. Green, orange, and red are retained but narrowed to semantic state only. Do not use `controlBackgroundColor` or white adaptive control fills as broad card/desk surfaces; they create a white-board stack and break the unified glass field. Color is semantic support, never the only state signal.

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
│ GPU Broker   │ 资源总览 [summary] [attention] [freshness]               │
│ 资源总览      │ 服务器池 [server collection] → [server detail]           │
│ 服务器池      │ 租约 [lease collection] → [lease detail]                 │
│ 租约          │                                                           │
│ 本地设置      │                                      [command island]    │
└──────────────┴───────────────────────────────────────────────────────────┘
```

- Sidebar: 236–246pt regular system material; grouped navigation for real pages only; selection uses the deep smoke rose interaction tint; the live-connection state sits compactly at the foot without another floating card.
- Title bar: one crisp title, not a glassy low-contrast label. Actor identity is a compact menu/control on the right.
- Window: the initial size must fit within the visible screen area, accounting for menu bar and Dock. Prefer a polished first launch over a fixed oversized window.
- Main region: the warm gray smoke-brown background fills the whole window, and the main content shares the window boundary. There is no outer margin, 30pt full-board corner radius, full-board stroke, or large desk shadow. Background texture should pass through every foreground layer; do not place white or `controlBackgroundColor` boards over it, and do not remove the atmospheric visual layer when fixing interactions.
- Overview: a first-screen summary with available, claimed, occupied, and needs-attention counts; include concrete attention rows when action is needed instead of a duplicate quick-action strip.
- Command island: refresh, add server, and claim GPU are the global command owners. Avoid repeating the same quick actions in both sidebar and overview.
- Top state chips or hero stats: compact, rounded, icon-led status items in one line. Use real fleet state: Available, Claimed, Occupied, Needs attention.
- Server pool: use collection + detail. Treat each endpoint as a “room” group in the collection. The primary identity is the exact `ssh -p <port> <user>@<host>` command in SF Mono; the endpoint ID is secondary metadata. Selecting a server opens a clearer detail layer with metrics, GPU list, and removal state.
- Server detail: use a real detail sheet with endpoint identity, live metrics, GPU list, and a visible remove action. Removal must stay disabled when the connected service does not advertise deletion support.
- Lease page: use collection + detail with a compact count strip and Apple Home-style active lease tiles. The project is the lease title, and the task is the subtitle. Owner, GPU count, localized expiry, and server scope are metadata. Hide the empty queue section; “归还” is confirmed and queued requests remain informational unless a dedicated cancel command exists.
- Expanded GPU items: accessory-style cards or stable numbered controls. Each has one icon tile or number, GPU model/index, state wording, VRAM, utilization, and ownership.
- Keep CPU, system memory, VRAM, and GPU utilization in the resource column. Each meter should pair the percentage bar with absolute values such as available CPU cores, available/total memory, or used/total VRAM; do not move those labels into the left identity cell.
- Dense headers and selected rows can increase opacity for readability, but must stay in the same warm field. Avoid white slabs, heavy borders, and isolated cool-gray panels.

## Content vocabulary

Use concise, user-facing Chinese based on resource action, not implementation jargon:

| Purpose | Preferred wording |
| --- | --- |
| main page | 资源总览 |
| active fleet state | 可用、已认领、占用中、需处理 |
| server group | 服务器池 |
| server primary label | exact SSH command |
| server secondary label | 在线 / 数据陈旧 / 连接异常 |
| table headers | 连接、GPU 可用性、资源、操作 |
| immediate claim | 认领 GPU |
| inspect detail | 查看详情 |
| no data | 暂无服务器。添加 SSH 连接以开始监控。 |
| add server | 加入本机资源池 |
| remove server | 移除服务器 |
| old service | 当前本机服务版本不支持移除。更新本机服务后即可使用。 |
| coordination boundary | 这里只负责分配 GPU，不代表可以启动或停止远端任务。 |
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

The target native shell is a SwiftUI/AppKit split view with `NSVisualEffectView` / SwiftUI materials and SF Symbols. It consumes the existing loopback REST API. Once native parity is achieved, remove the browser-facing Jinja templates, JavaScript, CSS, static asset routing, and WKWebView hosting path; retain only REST/MCP/CLI contracts and the desktop process lifecycle.
