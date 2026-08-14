"""Release metadata remains synchronized across shipped runtime surfaces."""

from __future__ import annotations

import plistlib
import tomllib
from pathlib import Path

from serverpilot import __version__
from serverpilot.keepalive_protocol import KEEPALIVE_IMPLEMENTATION_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_release_versions_are_synchronized() -> None:
    expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    info = plistlib.loads((ROOT / "desktop" / "Info.plist").read_bytes())

    assert project["project"]["version"] == expected
    assert __version__ == expected
    assert KEEPALIVE_IMPLEMENTATION_VERSION == expected
    assert info["CFBundleShortVersionString"] == expected
    assert str(info["CFBundleVersion"]).isdigit()
