"""Shared REST client for CLI and MCP. It intentionally never opens SSH or SQLite."""

from __future__ import annotations

import os
from typing import Any

import httpx


class BrokerClientError(RuntimeError):
    pass


class BrokerClient:
    def __init__(self, url: str, actor: str = "agent", *, timeout_seconds: float = 20) -> None:
        if not url.startswith(("http://", "https://")):
            raise BrokerClientError("GPU_BROKER_URL must start with http:// or https://")
        self.url = url.rstrip("/")
        self.actor = actor or "agent"
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls, *, url: str | None = None, actor: str | None = None) -> "BrokerClient":
        return cls(
            url or os.environ.get("GPU_BROKER_URL", "http://127.0.0.1:8787"),
            actor or os.environ.get("GPU_BROKER_ACTOR", "agent"),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"X-GPU-Broker-Actor": self.actor}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = httpx.request(
                method,
                f"{self.url}{path}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise BrokerClientError(f"broker request failed: {type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrokerClientError(f"broker returned non-JSON HTTP {response.status_code}") from exc
        if response.is_error:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise BrokerClientError(
                f"broker HTTP {response.status_code}: {error.get('code', 'unknown')}: {error.get('message', 'request failed')}"
            )
        if not isinstance(payload, dict):
            raise BrokerClientError("broker returned an invalid JSON envelope")
        return payload

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.request("POST", path, json_body=body, idempotency_key=idempotency_key)
