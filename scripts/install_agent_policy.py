#!/usr/bin/env python3
"""Render or install the shared GPU Broker policy for supported agent hosts.

Codex and Claude Code own global Markdown files. Cursor owns User Rules in its
settings UI, so this tool prints the Cursor block instead of editing a guessed
file. No target is changed unless --install is supplied.
"""

from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "docs" / "AGENT_MCP_policy.en.md"
MARKERS = {
    "codex": ("<!-- GPU_BROKER_GLOBAL_START -->", "<!-- GPU_BROKER_GLOBAL_END -->"),
    "claude": ("<!-- GPU_BROKER_GLOBAL_START -->", "<!-- GPU_BROKER_GLOBAL_END -->"),
    "cursor": ("<!-- GPU_BROKER_GLOBAL_START -->", "<!-- GPU_BROKER_GLOBAL_END -->"),
}


def destination(platform: str) -> Path | None:
    if platform == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "AGENTS.md"
    if platform == "claude":
        return Path.home() / ".claude" / "CLAUDE.md"
    return None


def render(platform: str, body: str) -> str:
    start, end = MARKERS[platform]
    return f"{start}\n{body.rstrip()}\n{end}\n"


def merge(existing: str, block: str) -> str:
    start, end = MARKERS["codex"]
    start_count = existing.count(start)
    end_count = existing.count(end)
    if start_count != end_count:
        raise ValueError("existing GPU Broker policy markers are incomplete")
    if start_count > 1:
        raise ValueError("existing GPU Broker policy markers are duplicated")
    if start_count == 1:
        begin = existing.find(start)
        finish = existing.find(end)
        if finish < begin:
            raise ValueError("existing GPU Broker policy markers are malformed")
        finish += len(end)
        return existing[:begin].rstrip() + "\n\n" + block.rstrip() + "\n" + existing[finish:].lstrip()
    if not existing.strip():
        return block
    return existing.rstrip() + "\n\n" + block


def install(platform: str, body: str) -> Path:
    path = destination(platform)
    if path is None:
        raise ValueError("Cursor User Rules must be pasted through Cursor settings; use --print cursor")
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlink: {path}")

    mode: int | None = None
    if path.exists():
        path_stat = path.stat()
        if not stat.S_ISREG(path_stat.st_mode):
            raise ValueError(f"refusing to replace non-regular file: {path}")
        mode = stat.S_IMODE(path_stat.st_mode)
        existing = path.read_text(encoding="utf-8")
    else:
        existing = ""
    updated = merge(existing, render(platform, body))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            handle.write(updated)
            temporary = Path(handle.name)
        if mode is not None:
            temporary.chmod(mode)
        if path.is_symlink():
            raise ValueError(f"refusing to replace symlink: {path}")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=["codex", "claude", "cursor", "all"])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--print", dest="print_only", action="store_true", help="print the rendered policy"
    )
    action.add_argument("--install", action="store_true", help="edit Codex or Claude global Markdown")
    args = parser.parse_args(argv)
    body = POLICY.read_text(encoding="utf-8")
    platforms = ["codex", "claude", "cursor"] if args.platform == "all" else [args.platform]
    if args.install and args.platform == "cursor":
        parser.error("[cursor] install is manual; use --print cursor and paste into Cursor User Rules")
    for platform in platforms:
        if args.print_only:
            if args.platform == "all":
                print(f"[{platform}] rendered policy")
            print(render(platform, body), end="")
        elif platform == "cursor":
            print("[cursor] not installed; use --print cursor and paste into Cursor User Rules")
        else:
            try:
                path = install(platform, body)
            except ValueError as error:
                parser.error(f"[{platform}] {error}")
            print(f"[{platform}] installed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
