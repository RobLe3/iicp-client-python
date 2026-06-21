from __future__ import annotations

import time

import httpx
import respx

from iicp_client.backend_stability import (
    DEGRADED,
    DRAINING,
    OK,
    REASON_BACKEND_COLD,
    REASON_BACKEND_LOADING,
    REASON_BACKEND_UNSTABLE,
    observe_backend_stability,
    parse_lmstudio_models,
    parse_ollama_ps,
)


def test_ollama_ps_loaded_model_is_ok_and_redacted():
    obs = parse_ollama_ps({"models": [{"name": "qwen2.5:0.5b", "size_vram": 123456789}]}, "qwen2.5:0.5b")
    assert obs.backend_state == OK
    public = obs.public_dict()
    assert public == {"backend_state": "ok", "reason_class": "ok"}
    assert "size_vram" not in public


def test_ollama_ps_missing_expected_model_is_cold_not_draining():
    obs = parse_ollama_ps({"models": []}, "qwen2.5:0.5b")
    assert obs.backend_state == DEGRADED
    assert obs.reason_class == REASON_BACKEND_COLD
    assert not obs.is_draining()


def test_lmstudio_loading_instance_drains_temporarily_and_redacts_detail():
    now = 1_000.0
    obs = parse_lmstudio_models(
        {
            "data": [
                {
                    "id": "qwen2.5-coder",
                    "loaded_instances": [
                        {"instance_id": "abc", "state": "loading", "model_size_bytes": 9_999_999}
                    ],
                }
            ]
        },
        "qwen2.5-coder",
        now=now,
        loading_retry_s=17,
    )
    assert obs.backend_state == DRAINING
    assert obs.reason_class == REASON_BACKEND_LOADING
    assert obs.retry_after_s(now) == 17
    public = obs.public_dict(now)
    assert public["retry_after_s"] == 17
    assert public["drain_until"] == 1017
    assert "model_size_bytes" not in public
    assert "loaded_instances" not in public


def test_lmstudio_failed_instance_drains_as_unstable():
    now = time.time()
    obs = parse_lmstudio_models(
        {"data": [{"id": "m", "loaded_instances": [{"status": "failed"}]}]},
        "m",
        now=now,
        unstable_retry_s=45,
    )
    assert obs.backend_state == DRAINING
    assert obs.reason_class == REASON_BACKEND_UNSTABLE
    assert obs.retry_after_s(now) == 45


@respx.mock
async def test_observer_uses_read_only_ollama_ps():
    route = respx.get("http://localhost:11434/api/ps").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen2.5:0.5b"}]})
    )
    async with httpx.AsyncClient() as client:
        obs = await observe_backend_stability(
            client,
            backend_url="http://localhost:11434/v1",
            backend="ollama",
            expected_model="qwen2.5:0.5b",
        )
    assert obs.backend_state == OK
    assert route.called


@respx.mock
async def test_observer_uses_read_only_lmstudio_models():
    route = respx.get("http://localhost:1234/api/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "qwen", "loaded_instances": [{"state": "loading"}]}]},
        )
    )
    async with httpx.AsyncClient() as client:
        obs = await observe_backend_stability(
            client,
            backend_url="http://localhost:1234/v1",
            backend="lmstudio",
            expected_model="qwen",
        )
    assert obs.backend_state == DRAINING
    assert route.called
