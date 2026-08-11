# Contributing

ServerPilot is a local, loopback-only coordination service. Keep changes small, explicit, and easy to verify.

## Development setup

```bash
uv sync --extra dev --reinstall-package serverpilot
```

Run the focused tests first, then the full suite and Ruff:

```bash
uv run --reinstall-package serverpilot pytest
uv run --reinstall-package serverpilot ruff check .
```

Do not use a real GPU or SSH endpoint in tests. Use temporary SQLite databases, fake providers, and test inventory fixtures.

## Architecture boundaries

- `src/serverpilot/service.py` owns scheduling, leases, queueing, state, and audit rules.
- REST, CLI, and MCP are adapters; they must not access SQLite or SSH directly or duplicate domain rules.
- Collector probes are fixed, read-only operations. Never add arbitrary shell input, private-key handling, or remote lifecycle control.
- Changes to public behavior require tests, migrations when needed, and an update to the relevant documentation.

## Pull requests

Describe the user-visible result, the protected boundary, and the verification command. Keep generated files, local databases, app bundles, and credentials out of commits. The only local desktop bundle is the ignored repository-root `ServerPilot.app`; do not create copies under `dist`, `build`, or `~/Applications`. Desktop changes must pass `zsh desktop/build-macos-app.sh` and `zsh desktop/verify-macos-app.sh` on macOS.

## Repository hygiene

Keep the public tree small and source-focused:

- Commit product source, tests, migrations, docs, and small curated assets only.
- Do not commit `.superdesign/`, `.codegraph/`, `state/`, `dist/`, `build/`, root `ServerPilot*.app` entries, QA captures, or generated cache files.
- If a screenshot is needed for `README.md`, place one compressed, current image in `docs/assets/` and remove the obsolete image in the same change.
- Keep design rationale in `docs/DESIGN_SYSTEM.md`; do not commit tool-generated component/layout dumps.
