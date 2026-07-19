# Pages

## / (Resource overview)

Entry: `src/gpu_broker/web/templates/dashboard.html`

Dependencies:
- `src/gpu_broker/web/templates/dashboard.html`
  - `src/gpu_broker/web/templates/base.html`
    - `src/gpu_broker/web/static/app.css`
    - `src/gpu_broker/web/static/vendor/phosphor/style.css`
    - `src/gpu_broker/web/static/assets/server-room-background.jpg`
    - `src/gpu_broker/web/static/app.js`
      - `src/gpu_broker/web/static/vendor/uPlot.iife.min.js` (loaded dynamically when GPU history is shown)
      - `src/gpu_broker/web/static/vendor/uPlot.min.css`
- `src/gpu_broker/api.py` (route `/`, overview payload)

Renders the dashboard hero, health summary, collapsed endpoint/server rows, GPU tiles, resource meters, SSH copying, and dialogs.

## /ui/gpus and /ui/gpus/{gpu_id}

Entry: `src/gpu_broker/web/templates/page.html`

Dependencies:
- `src/gpu_broker/web/templates/page.html`
  - `src/gpu_broker/web/templates/base.html`
    - `src/gpu_broker/web/static/app.css`
    - `src/gpu_broker/web/static/vendor/phosphor/style.css`
    - `src/gpu_broker/web/static/app.js`
- `src/gpu_broker/api.py` (GPU page and detail payload routes)

Renders searchable GPU cards/tables, state pills, metrics, ownership, and drawer detail.

## /ui/requests, /ui/reservations, /ui/maintenance, /ui/alerts, /ui/audit, /ui/doctor, /ui/identities

Entry: `src/gpu_broker/web/templates/page.html`

Dependencies:
- `src/gpu_broker/web/templates/page.html`
  - `src/gpu_broker/web/templates/base.html`
    - `src/gpu_broker/web/static/app.css`
    - `src/gpu_broker/web/static/vendor/phosphor/style.css`
    - `src/gpu_broker/web/static/app.js`
- `src/gpu_broker/api.py` (page payload and action routes)

Renders management tables, status pills, confirmations, forms, and dialogs using the shared shell.

## /ui/login

Entry: `src/gpu_broker/web/templates/login.html`

Dependencies:
- `src/gpu_broker/web/templates/login.html`
  - `src/gpu_broker/web/templates/base.html`
    - `src/gpu_broker/web/static/app.css`
- `desktop/GPU Broker.swift` (native pasteboard bridge)

Renders local-token login and the macOS clipboard affordance.

## /ui/identities (token-created state)

Entry: `src/gpu_broker/web/templates/token_created.html`

Dependencies:
- `src/gpu_broker/web/templates/token_created.html`
  - `src/gpu_broker/web/templates/base.html`
    - `src/gpu_broker/web/static/app.css`
    - `src/gpu_broker/web/static/app.js`
- `src/gpu_broker/api.py` (token-created route branch)

Renders the one-time secret disclosure panel.

## Native macOS target

Entry: `desktop/GPU Broker.swift`

Current dependency tree:
- `desktop/GPU Broker.swift`
  - starts `gpu-broker serve` on loopback
  - hosts `/` in `WKWebView`
  - implements the clipboard bridge for the login / SSH flow

Design target: replace the WKWebView presentation layer with a native SwiftUI/AppKit interface that calls the existing REST API. The loopback API and desktop lifecycle are not visual dependencies.
