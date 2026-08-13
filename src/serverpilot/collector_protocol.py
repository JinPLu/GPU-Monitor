"""Shared constants for the sealed remote collector protocol.

The broker always invokes this exact entry point.  A target administrator may
implement the command locally (for example to reach a containerized
``nvidia-smi``), but cannot influence its path or arguments through endpoint
configuration.
"""

from __future__ import annotations


SERVER_SCRIPT_SCHEMA_VERSION = 2
SERVER_SCRIPT_ENTRYPOINT = "serverpilot-collect"
SERVER_SCRIPT_REMOTE_COMMAND = (
    f"{SERVER_SCRIPT_ENTRYPOINT} --schema-version {SERVER_SCRIPT_SCHEMA_VERSION}"
)
