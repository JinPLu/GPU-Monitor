"""Shared REST client for CLI and MCP. It intentionally never opens SSH or SQLite."""

from __future__ import annotations

import os
import time
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
        # A loopback service can be briefly unavailable while it restarts. GET
        # requests are safe to retry, and this client gives every mutation an
        # idempotency key before retrying it, so a claim cannot be duplicated.
        retryable = method.upper() == "GET" or idempotency_key is not None
        attempts = 3 if retryable else 1
        response: httpx.Response | None = None
        last_transport_error: httpx.HTTPError | None = None
        for attempt in range(attempts):
            try:
                response = httpx.request(
                    method,
                    f"{self.url}{path}",
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=self.timeout_seconds,
                    # GPU Broker is a local control plane.  MCP processes are
                    # often launched with a minimal environment that omits
                    # NO_PROXY, so httpx would otherwise send loopback calls
                    # through an ambient HTTP proxy and surface its empty 502.
                    trust_env=False,
                )
            except httpx.HTTPError as exc:
                last_transport_error = exc
                if attempt + 1 == attempts:
                    raise BrokerClientError(f"broker request failed: {type(exc).__name__}") from exc
            else:
                if response.status_code not in {502, 503, 504} or attempt + 1 == attempts:
                    break
            time.sleep(0.1 * (attempt + 1))
        if response is None:
            assert last_transport_error is not None
            raise BrokerClientError(f"broker request failed: {type(last_transport_error).__name__}")
        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown")
            suffix = " after retry" if attempts > 1 else ""
            raise BrokerClientError(
                f"broker returned non-JSON HTTP {response.status_code}{suffix} ({content_type})"
            ) from exc
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

    def delete(self, path: str, *, idempotency_key: str) -> dict[str, Any]:
        return self.request("DELETE", path, idempotency_key=idempotency_key)
