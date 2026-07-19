from __future__ import annotations

import httpx

from gpu_broker.client import BrokerClient


def test_client_retries_a_transient_gateway_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    responses = iter(
        [
            httpx.Response(502, text="temporarily unavailable"),
            httpx.Response(200, json={"schema_version": "v1", "data": {}}),
        ]
    )
    calls = []

    def request(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((args, kwargs))
        return next(responses)

    monkeypatch.setattr("gpu_broker.client.httpx.request", request)
    monkeypatch.setattr("gpu_broker.client.time.sleep", lambda _seconds: None)

    assert BrokerClient("http://127.0.0.1:8787").get("/api/v1/snapshot") == {
        "schema_version": "v1",
        "data": {},
    }
    assert len(calls) == 2
