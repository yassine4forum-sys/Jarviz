"""Narrow Home Assistant tools registered only for JarViz smart-home runs."""
from __future__ import annotations

import json
import re
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

TOOLSET = "jarviz-smart-home"
TOOL_NAMES = frozenset({"home_assistant_get_states", "home_assistant_call_service"})
_ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_SERVICE = re.compile(r"^[a-z0-9_]{1,64}$")
_CONTROL_DOMAINS = frozenset({
    "light", "switch", "climate", "fan", "cover", "media_player", "scene",
    "vacuum", "humidifier", "water_heater", "input_boolean",
})
_SAFE_ATTRIBUTES = frozenset({
    "friendly_name", "device_class", "unit_of_measurement", "temperature",
    "current_temperature", "target_temp_high", "target_temp_low", "hvac_mode",
    "hvac_action", "fan_mode", "percentage", "brightness", "color_temp_kelvin",
    "position", "media_title", "media_artist", "volume_level",
})
_REGISTER_LOCK = threading.Lock()
_REGISTERED = False

GET_STATES_SCHEMA = {
    "description": "Read Home Assistant entity states for smart-home status questions.",
    "parameters": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Optional entity domain such as light or climate."},
            "entity_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
        },
    },
}
CALL_SERVICE_SCHEMA = {
    "description": "Call an allowed Home Assistant smart-home service on one explicit entity.",
    "parameters": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "enum": sorted(_CONTROL_DOMAINS)},
            "service": {"type": "string", "description": "Service such as turn_on, turn_off, or set_temperature."},
            "entity_id": {"type": "string"},
            "service_data": {"type": "object", "description": "Optional Home Assistant service parameters."},
        },
        "required": ["domain", "service", "entity_id"],
    },
}


def _config():
    from api.config import _thread_local_env_value

    base_url = _thread_local_env_value("HOME_ASSISTANT_URL").strip().rstrip("/")
    token = _thread_local_env_value("HOME_ASSISTANT_TOKEN").strip()
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeError("Home Assistant URL is not configured")
    if not token:
        raise RuntimeError("Home Assistant token is not configured")
    return base_url, token


def _available():
    try:
        _config()
        return True
    except Exception:
        return False


def _request(method, path, payload=None):
    base_url, token = _config()
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        base_url + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=12) as response:
            raw = response.read(1_000_001)
    except HTTPError as exc:
        raise RuntimeError(f"Home Assistant rejected the request ({exc.code})") from None
    except (URLError, TimeoutError):
        raise RuntimeError("Home Assistant is unavailable") from None
    if len(raw) > 1_000_000:
        raise RuntimeError("Home Assistant response is too large")
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError("Home Assistant returned an invalid response") from None


def _compact_state(item):
    attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    return {
        "entity_id": item.get("entity_id"),
        "state": item.get("state"),
        "attributes": {key: attributes[key] for key in _SAFE_ATTRIBUTES if key in attributes},
        "last_changed": item.get("last_changed"),
    }


def _handle_get_states(args, **_kwargs):
    if not isinstance(args, dict) or set(args) - {"domain", "entity_ids"}:
        raise ValueError("invalid state query")
    domain = args.get("domain")
    if domain is not None and (not isinstance(domain, str) or not _SERVICE.fullmatch(domain)):
        raise ValueError("invalid entity domain")
    entity_ids = args.get("entity_ids") or []
    if (not isinstance(entity_ids, list) or len(entity_ids) > 50
            or any(not isinstance(value, str) or not _ENTITY.fullmatch(value) for value in entity_ids)):
        raise ValueError("invalid entity ids")
    requested = set(entity_ids)
    states = _request("GET", "/api/states")
    if not isinstance(states, list):
        raise RuntimeError("Home Assistant returned invalid states")
    result = [
        _compact_state(item) for item in states
        if isinstance(item, dict)
        and (not domain or str(item.get("entity_id", "")).startswith(domain + "."))
        and (not requested or item.get("entity_id") in requested)
    ][:100]
    return json.dumps({"states": result, "count": len(result)}, ensure_ascii=False)


def _validate_service_data(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 30:
        raise ValueError("invalid service data")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded) > 16_000:
        raise ValueError("service data is too large")
    return value


def _handle_call_service(args, **_kwargs):
    if not isinstance(args, dict) or set(args) - {"domain", "service", "entity_id", "service_data"}:
        raise ValueError("invalid service call")
    domain, service, entity_id = args.get("domain"), args.get("service"), args.get("entity_id")
    if domain not in _CONTROL_DOMAINS or not isinstance(service, str) or not _SERVICE.fullmatch(service):
        raise ValueError("service is not allowed")
    if not isinstance(entity_id, str) or not _ENTITY.fullmatch(entity_id) or not entity_id.startswith(domain + "."):
        raise ValueError("entity does not match service domain")
    payload = {**_validate_service_data(args.get("service_data")), "entity_id": entity_id}
    response = _request("POST", f"/api/services/{domain}/{service}", payload)
    states = response if isinstance(response, list) else []
    return json.dumps({
        "ok": True, "domain": domain, "service": service, "entity_id": entity_id,
        "states": [_compact_state(item) for item in states if isinstance(item, dict)][:20],
    }, ensure_ascii=False)


def ensure_registered():
    """Register once in Hermes' runtime registry without modifying Agent files."""
    global _REGISTERED
    if _REGISTERED:
        return
    with _REGISTER_LOCK:
        if _REGISTERED:
            return
        from tools.registry import registry

        registry.register("home_assistant_get_states", TOOLSET, GET_STATES_SCHEMA,
                          _handle_get_states, check_fn=_available, emoji="home")
        registry.register("home_assistant_call_service", TOOLSET, CALL_SERVICE_SCHEMA,
                          _handle_call_service, check_fn=_available, emoji="home")
        _REGISTERED = True
