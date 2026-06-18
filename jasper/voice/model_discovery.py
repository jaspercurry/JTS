"""Manual discovery of provider voice models for the /voice wizard.

Discovery is deliberately operator-triggered. The wizard reads this
module's cache during normal page renders, but provider network calls
only happen when a user presses "Refresh available models".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from typing import TYPE_CHECKING, Any

# httpx is imported lazily inside the fetch helpers — this module is
# imported at the top of the /voice wizard (for its cache readers),
# which is socket-activated and must stay light (same documented
# convention as jasper/transit/providers/nyc_bus.py). Page renders only
# read the JSON cache; httpx loads when an operator presses "Refresh
# available models".
if TYPE_CHECKING:
    import httpx


DEFAULT_CACHE_PATH = "/var/lib/jasper/voice_model_discovery.json"
DISCOVERY_TIMEOUT_SEC = 8.0
DISCOVERY_CONNECT_TIMEOUT_SEC = 3.0


def _discovery_timeout() -> httpx.Timeout:
    import httpx  # lazy — see import comment at top of module

    return httpx.Timeout(
        timeout=DISCOVERY_TIMEOUT_SEC, connect=DISCOVERY_CONNECT_TIMEOUT_SEC,
    )


class ModelDiscoveryError(RuntimeError):
    """Raised when a provider model-list request fails safely."""


@dataclass(frozen=True)
class DiscoverySnapshot:
    provider_id: str
    fetched_at: str = ""
    models: tuple[str, ...] = ()
    last_error: str = ""
    last_error_at: str = ""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dedupe_preserving_order(values: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = value.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return tuple(out)


def _safe_get_json(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    import httpx  # lazy — see import comment at top of module

    try:
        response = client.get(url, headers=headers, params=params)
    except httpx.TimeoutException as e:
        raise ModelDiscoveryError("models endpoint timed out") from e
    except httpx.HTTPError as e:
        # httpx error strings can include full URLs. Keep surfaced errors
        # deliberately generic so provider request details never leak keys.
        raise ModelDiscoveryError(
            f"models endpoint request failed ({e.__class__.__name__})",
        ) from e
    if response.status_code >= 400:
        raise ModelDiscoveryError(
            f"models endpoint returned HTTP {response.status_code}",
        )
    try:
        data = response.json()
    except ValueError as e:
        raise ModelDiscoveryError("models endpoint returned invalid JSON") from e
    if not isinstance(data, dict):
        raise ModelDiscoveryError("models endpoint returned unexpected JSON")
    return data


def _openai_voice_model_ids(data: dict[str, Any]) -> tuple[str, ...]:
    ids: list[str] = []
    items = data.get("data", [])
    if not isinstance(items, list):
        return ()
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        lower = model_id.lower()
        if (
            "realtime" in lower
            and "whisper" not in lower
            and "translat" not in lower
            and "transcrib" not in lower
        ):
            ids.append(model_id)
    return _dedupe_preserving_order(ids)


def _gemini_model_id(item: dict[str, Any]) -> str:
    raw = str(item.get("name") or item.get("baseModelId") or "").strip()
    return raw.removeprefix("models/")


def _gemini_live_model_ids(data: dict[str, Any]) -> tuple[str, ...]:
    ids: list[str] = []
    items = data.get("models", [])
    if not isinstance(items, list):
        return ()
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = _gemini_model_id(item)
        lower_id = model_id.lower()
        raw_methods = (
            item.get("supportedGenerationMethods")
            or item.get("supported_actions")
            or item.get("supportedActions")
            or []
        )
        if not isinstance(raw_methods, list):
            raw_methods = []
        methods = {
            str(method).lower()
            for method in raw_methods
        }
        if (
            "bidigeneratecontent" in methods
            or "bidi_generate_content" in methods
            or "live" in lower_id
            or "native-audio" in lower_id
        ):
            ids.append(model_id)
    return _dedupe_preserving_order(ids)


def _grok_voice_model_ids(data: dict[str, Any]) -> tuple[str, ...]:
    ids: list[str] = []
    items = data.get("data", [])
    if not isinstance(items, list):
        return ()
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if model_id.lower().startswith("grok-voice-"):
            ids.append(model_id)
    return _dedupe_preserving_order(ids)


def fetch_provider_model_ids(
    provider_id: str,
    api_key: str,
    *,
    http: httpx.Client | None = None,
) -> tuple[str, ...]:
    """Fetch voice-capable model IDs for one provider.

    The filters are intentionally conservative. Provider model-list
    endpoints are useful discovery hints, not proof that a model is
    tested on this speaker.
    """
    api_key = api_key.strip()
    if not api_key:
        raise ModelDiscoveryError("missing API key")

    import httpx  # lazy — see import comment at top of module

    own_client = http is None
    client = http or httpx.Client(timeout=_discovery_timeout())
    try:
        if provider_id == "openai":
            data = _safe_get_json(
                client,
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            return _openai_voice_model_ids(data)
        if provider_id == "gemini":
            models: list[str] = []
            page_token = ""
            while True:
                params = {"pageSize": "1000"}
                if page_token:
                    params["pageToken"] = page_token
                data = _safe_get_json(
                    client,
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": api_key},
                    params=params,
                )
                models.extend(_gemini_live_model_ids(data))
                page_token = str(data.get("nextPageToken") or "")
                if not page_token:
                    break
            return _dedupe_preserving_order(models)
        if provider_id == "grok":
            data = _safe_get_json(
                client,
                "https://api.x.ai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            return _grok_voice_model_ids(data)
    finally:
        if own_client:
            client.close()
    raise ModelDiscoveryError(f"unsupported provider {provider_id!r}")


def load_cache(path: str = DEFAULT_CACHE_PATH) -> dict[str, DiscoverySnapshot]:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        return {}
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return {}
    out: dict[str, DiscoverySnapshot] = {}
    for provider_id, raw in providers.items():
        if not isinstance(provider_id, str) or not isinstance(raw, dict):
            continue
        raw_models = raw.get("models", [])
        if not isinstance(raw_models, list):
            raw_models = []
        models = tuple(
            str(model).strip()
            for model in raw_models
            if str(model).strip()
        )
        out[provider_id] = DiscoverySnapshot(
            provider_id=provider_id,
            fetched_at=str(raw.get("fetched_at") or ""),
            models=_dedupe_preserving_order(list(models)),
            last_error=str(raw.get("last_error") or ""),
            last_error_at=str(raw.get("last_error_at") or ""),
        )
    return out


def _write_cache(path: str, snapshots: dict[str, DiscoverySnapshot]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    payload = {
        "version": 1,
        "providers": {
            provider_id: {
                "fetched_at": snapshot.fetched_at,
                "models": list(snapshot.models),
                "last_error": snapshot.last_error,
                "last_error_at": snapshot.last_error_at,
            }
            for provider_id, snapshot in sorted(snapshots.items())
        },
    }
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def refresh_provider_cache(
    provider_id: str,
    api_key: str,
    *,
    path: str = DEFAULT_CACHE_PATH,
    http: httpx.Client | None = None,
    now: str | None = None,
) -> DiscoverySnapshot:
    snapshots = load_cache(path)
    timestamp = now or _utc_now()
    try:
        models = fetch_provider_model_ids(provider_id, api_key, http=http)
        if not models:
            raise ModelDiscoveryError(
                "models endpoint returned no voice-capable models",
            )
    except ModelDiscoveryError as e:
        previous = snapshots.get(
            provider_id,
            DiscoverySnapshot(provider_id=provider_id),
        )
        failed = DiscoverySnapshot(
            provider_id=provider_id,
            fetched_at=previous.fetched_at,
            models=previous.models,
            last_error=str(e),
            last_error_at=timestamp,
        )
        snapshots[provider_id] = failed
        _write_cache(path, snapshots)
        raise
    snapshot = DiscoverySnapshot(
        provider_id=provider_id,
        fetched_at=timestamp,
        models=models,
    )
    snapshots[provider_id] = snapshot
    _write_cache(path, snapshots)
    return snapshot
