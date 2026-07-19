# Extractable Components

The current UI is template- and renderer-based rather than component-framework-based. These are the reusable visual patterns to carry forward into a native desktop design; do not copy their web implementation as the new runtime.

## WorkspaceSidebar

- Source: `src/gpu_broker/web/templates/base.html`
- Category: layout
- Description: Persistent navigation organized into workspace and management groups with a live-connection footer.
- Extractable props: `activeItem` (string), `isCollapsed` (boolean), `connectionState` (string)
- Hardcoded: GPU Broker brand mark, Chinese labels, Phosphor outline icon set, local-control disclosure.

## WorkspaceTopBar

- Source: `src/gpu_broker/web/templates/base.html`
- Category: layout
- Description: Compact global title, disclosure, sidebar control, and current-actor control.
- Extractable props: `actorName` (string), `isSidebarVisible` (boolean)
- Hardcoded: title and safety disclosure copy.

## AmbientApplicationBackground

- Source: `src/gpu_broker/web/templates/base.html`, `src/gpu_broker/web/static/app.css`
- Category: layout
- Description: Full-window photo-derived background behind a translucent work surface.
- Extractable props: none
- Hardcoded: server-room asset, blur/tint treatment.

## ResourceSummaryCard

- Source: `src/gpu_broker/web/templates/dashboard.html`, `src/gpu_broker/web/static/app.js`
- Category: basic
- Description: Overview card with metric, status, operational context, and status color.
- Extractable props: `metricValue` (string), `state` (string), `trend` (optional string)
- Hardcoded: icon name, card spacing, label type scale.

## EndpointRow

- Source: `src/gpu_broker/web/static/app.js`
- Category: basic
- Description: Expandable server row with SSH command, capacity counters, resource meters, and quick actions.
- Extractable props: `endpointId` (string), `sshCommand` (string), `gpuCounts` (object), `isExpanded` (boolean), `status` (string)
- Hardcoded: copy/expand affordances, metric order, localized labels.

## GPUStatusTile

- Source: `src/gpu_broker/web/static/app.js`
- Category: basic
- Description: GPU state tile with memory, utilization, ownership, and error/lease status.
- Extractable props: `gpuName` (string), `state` (string), `memoryPercent` (number), `utilizationPercent` (number), `owner` (optional string)
- Hardcoded: state vocabulary and health-color mapping.

## StatePill

- Source: `src/gpu_broker/web/static/app.js`, `src/gpu_broker/web/static/app.css`
- Category: basic
- Description: Reusable semantic availability, warning, alert, and managed-workload status marker.
- Extractable props: `state` (string), `label` (string)
- Hardcoded: semantic color palette and shape.

## DetailDrawer

- Source: `src/gpu_broker/web/templates/dashboard.html`, `src/gpu_broker/web/templates/page.html`
- Category: layout
- Description: In-context GPU detail / action panel with metrics, history, and ownership.
- Extractable props: `selectedGpu` (object), `isOpen` (boolean), `timeRange` (string)
- Hardcoded: metric order and action button styling.
