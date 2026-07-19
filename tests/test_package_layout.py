from __future__ import annotations

from importlib.resources import files

import pytest


@pytest.mark.parametrize(
    "resource",
    [
        "web/static/assets/server-room-background.jpg",
        "web/static/vendor/phosphor/style.css",
        "web/static/vendor/phosphor/Phosphor.woff2",
        "migrations/env.py",
        "migrations/script.py.mako",
        "migrations/versions/20260719_0003_endpoint_host_telemetry.py",
    ],
)
def test_runtime_package_resources_are_present(resource: str) -> None:
    assert files("gpu_broker").joinpath(resource).is_file(), resource
