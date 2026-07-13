"""Provider-local backend stability observer and drain guard.

The observer is intentionally conservative and read-only. It looks at local
backend status endpoints that operators already expose to their node process and
turns them into a coarse IICP state suitable for public health output. It never
loads/unloads models and never publishes raw model size, quantization or host
capacity details by default.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

OK = "ok"
DEGRADED = "degraded"
DRAINING = "draining"

REASON_OK = "ok"
REASON_BACKEND_COLD = "backend_cold"
REASON_BACKEND_LOADING = "backend_loading"
REASON_BACKEND_UNSTABLE = "backend_unstable"
REASON_OBSERVER_ERROR = "observer_error"


@dataclass(frozen=True)
class BackendStabilityObservation:
    """Coarse provider-local backend state.

    ``diagnostics`` is local/operator detail only. Use ``public_dict()`` for
    health endpoints or directory payloads so backend capacity details are not
    disclosed accidentally.
    """

    backend_state: str = OK
    reason_class: str = REASON_OK
    drain_until: float | None = None
    observed_at: float = field(default_factory=time.time)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def retry_after_s(self, now: float | None = None) -> int | None:
        if self.drain_until is None:
            return None
        remaining = self.drain_until - (time.time() if now is None else now)
        if remaining <= 0:
            return None
        return max(1, int(round(remaining)))

    def is_draining(self, now: float | None = None) -> bool:
        return self.retry_after_s(now) is not None and self.backend_state == DRAINING

    def public_dict(self, now: float | None = None) -> dict[str, Any]:
        """Redacted shape safe for /iicp/health and directory heartbeat."""
        body: dict[str, Any] = {
            "backend_state": self.backend_state,
            "reason_class": self.reason_class,
        }
        retry = self.retry_after_s(now)
        if retry is not None:
            body["retry_after_s"] = retry
            body["drain_until"] = int(self.drain_until or 0)
        return body


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _model_matches(candidate: str, expected_model: str | None) -> bool:
    if not expected_model:
        return True
    return candidate == expected_model or candidate.split(":", 1)[0] == expected_model.split(":", 1)[0]


def parse_ollama_ps(
    data: dict[str, Any],
    expected_model: str | None = None,
    *,
    now: float | None = None,
) -> BackendStabilityObservation:
    """Parse Ollama ``GET /api/ps`` without exposing model/capacity details.

    ``/api/ps`` lists models currently resident in memory. Absence of the
    expected model is treated as cold/degraded, not draining: automatically
    refusing all tasks just because a model is not resident would create a
    self-inflicted permanent drain for normal cold-start setups.
    """

    del now
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return BackendStabilityObservation(DEGRADED, REASON_OBSERVER_ERROR)
    names = [m.get("name") for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)]
    loaded_expected = any(_model_matches(name, expected_model) for name in names)
    if expected_model and not loaded_expected:
        return BackendStabilityObservation(
            DEGRADED,
            REASON_BACKEND_COLD,
            diagnostics={"loaded_model_count": len(names), "expected_model_loaded": False},
        )
    return BackendStabilityObservation(OK, REASON_OK, diagnostics={"loaded_model_count": len(names)})


def parse_lmstudio_models(
    data: dict[str, Any],
    expected_model: str | None = None,
    *,
    now: float | None = None,
    loading_retry_s: int = 30,
    unstable_retry_s: int = 60,
) -> BackendStabilityObservation:
    """Parse LM Studio ``GET /api/v1/models`` read-only model status.

    LM Studio reports ``loaded_instances`` for each model. Instance status/state
    values that indicate loading or failure become temporary drain signals.
    """

    now = time.time() if now is None else now
    raw_models = data.get("data", data.get("models")) if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return BackendStabilityObservation(DEGRADED, REASON_OBSERVER_ERROR, observed_at=now)

    saw_expected = not expected_model
    saw_loaded_expected = False
    saw_loading = False
    saw_unstable = False
    loaded_count = 0
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("model_key") or item.get("path") or "")
        expected = _model_matches(model_id, expected_model)
        saw_expected = saw_expected or expected
        instances = item.get("loaded_instances") or item.get("loadedInstances") or []
        if not isinstance(instances, list):
            instances = []
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            loaded_count += 1
            state = _norm(inst.get("state") or inst.get("status") or inst.get("load_status"))
            if expected:
                saw_loaded_expected = True
            if state in {"loading", "initializing", "starting", "warming", "warming_up"}:
                saw_loading = True
            if state in {"error", "failed", "crashed", "unhealthy", "oom", "out_of_memory"}:
                saw_unstable = True

    if saw_unstable:
        return BackendStabilityObservation(
            DRAINING,
            REASON_BACKEND_UNSTABLE,
            drain_until=now + unstable_retry_s,
            observed_at=now,
            diagnostics={"loaded_instance_count": loaded_count},
        )
    if saw_loading:
        return BackendStabilityObservation(
            DRAINING,
            REASON_BACKEND_LOADING,
            drain_until=now + loading_retry_s,
            observed_at=now,
            diagnostics={"loaded_instance_count": loaded_count},
        )
    if expected_model and (not saw_expected or not saw_loaded_expected):
        return BackendStabilityObservation(
            DEGRADED,
            REASON_BACKEND_COLD,
            observed_at=now,
            diagnostics={"loaded_instance_count": loaded_count, "expected_model_loaded": False},
        )
    return BackendStabilityObservation(
        OK,
        REASON_OK,
        observed_at=now,
        diagnostics={"loaded_instance_count": loaded_count},
    )


async def observe_backend_stability(
    client: httpx.AsyncClient,
    *,
    backend_url: str,
    backend: str | None = None,
    expected_model: str | None = None,
    api_key: str = "",
    timeout_s: float = 2.0,
) -> BackendStabilityObservation:
    """Read-only best-effort backend observation.

    The adapter only uses status/list endpoints. It intentionally avoids Ollama
    empty-prompt load/unload and LM Studio load/unload endpoints.
    """

    base = backend_url.rstrip("/")
    if not base:
        return BackendStabilityObservation(DEGRADED, REASON_OBSERVER_ERROR)
    root = base[:-3] if base.endswith("/v1") else base
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    flavor = _norm(backend)

    try:
        if flavor == "meshllm":
            ready = await client.get(f"{root}/readyz", headers=headers, timeout=timeout_s)
            if not ready.is_success:
                return BackendStabilityObservation(
                    DRAINING, REASON_BACKEND_LOADING,
                    drain_until=time.time() + 30,
                    diagnostics={"ready": False},
                )
            inventory = await client.get(f"{root}/v1/models", headers=headers, timeout=timeout_s)
            if not inventory.is_success:
                return BackendStabilityObservation(
                    DRAINING, REASON_BACKEND_LOADING,
                    drain_until=time.time() + 30,
                    diagnostics={"ready": False},
                )
            body = inventory.json()
            raw_models = body.get("data") if isinstance(body, dict) else None
            if not isinstance(raw_models, list):
                return BackendStabilityObservation(
                    DRAINING, REASON_BACKEND_LOADING,
                    drain_until=time.time() + 30,
                    diagnostics={"ready": False},
                )
            models = [item.get("id") for item in raw_models if isinstance(item, dict) and item.get("id")]
            if not models or (expected_model and not any(_model_matches(model, expected_model) for model in models)):
                return BackendStabilityObservation(
                    DRAINING, REASON_BACKEND_LOADING,
                    drain_until=time.time() + 30,
                    diagnostics={"selected_model_ready": False},
                )
            return BackendStabilityObservation(diagnostics={"ready": True})
        if flavor in {"ollama", ""}:
            resp = await client.get(f"{root}/api/ps", headers=headers, timeout=timeout_s)
            if resp.is_success:
                return parse_ollama_ps(resp.json(), expected_model)
            if flavor == "ollama":
                return BackendStabilityObservation(DEGRADED, REASON_OBSERVER_ERROR)

        if flavor in {"lmstudio", "lm_studio", "lm_studio_server", ""}:
            resp = await client.get(f"{root}/api/v1/models", headers=headers, timeout=timeout_s)
            if resp.is_success:
                return parse_lmstudio_models(resp.json(), expected_model)
            if flavor in {"lmstudio", "lm_studio", "lm_studio_server"}:
                return BackendStabilityObservation(DEGRADED, REASON_OBSERVER_ERROR)
    except Exception:  # noqa: BLE001 - observer must never break task serving/heartbeat
        return BackendStabilityObservation(DEGRADED, REASON_OBSERVER_ERROR)

    return BackendStabilityObservation(OK, REASON_OK)
