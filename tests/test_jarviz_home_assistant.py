"""Focused Phase 5 Home Assistant boundary tests."""
import json
from types import SimpleNamespace

import pytest

from api import jarviz_home_assistant as home


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return json.dumps(self.value).encode()


@pytest.fixture
def configured(monkeypatch):
    values = {"HOME_ASSISTANT_URL": "http://ha.local:8123/", "HOME_ASSISTANT_TOKEN": "secret-token"}
    monkeypatch.setattr("api.config._thread_local_env_value", lambda name: values.get(name, ""))


def test_state_reader_filters_entities_and_private_attributes(configured, monkeypatch):
    captured = {}
    states = [{"entity_id": "light.office", "state": "on", "last_changed": "now",
               "attributes": {"friendly_name": "Office", "access_token": "private"}},
              {"entity_id": "switch.desk", "state": "off", "attributes": {}}]

    def open_request(request, timeout):
        captured.update(url=request.full_url, headers=dict(request.header_items()), timeout=timeout)
        return _Response(states)

    monkeypatch.setattr(home, "urlopen", open_request)
    result = json.loads(home._handle_get_states({"domain": "light"}))
    assert result == {"states": [{"entity_id": "light.office", "state": "on",
                                  "attributes": {"friendly_name": "Office"},
                                  "last_changed": "now"}], "count": 1}
    assert captured["url"] == "http://ha.local:8123/api/states"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in json.dumps(result)


def test_service_call_is_narrow_and_targets_one_matching_entity(configured, monkeypatch):
    captured = {}

    def open_request(request, timeout):
        captured.update(url=request.full_url, method=request.method,
                        body=json.loads(request.data.decode()))
        return _Response([])

    monkeypatch.setattr(home, "urlopen", open_request)
    result = json.loads(home._handle_call_service({
        "domain": "climate", "service": "set_temperature", "entity_id": "climate.salon",
        "service_data": {"temperature": 24},
    }))
    assert result["ok"] is True
    assert captured == {"url": "http://ha.local:8123/api/services/climate/set_temperature",
                        "method": "POST", "body": {"temperature": 24, "entity_id": "climate.salon"}}


@pytest.mark.parametrize("args", [
    {"domain": "lock", "service": "unlock", "entity_id": "lock.front"},
    {"domain": "light", "service": "turn_on", "entity_id": "switch.desk"},
    {"domain": "light", "service": "turn_on", "entity_id": "light.office", "extra": True},
])
def test_service_call_rejects_unapproved_or_mismatched_controls(configured, monkeypatch, args):
    monkeypatch.setattr(home, "urlopen", lambda *_a, **_k: pytest.fail("invalid call reached network"))
    with pytest.raises(ValueError):
        home._handle_call_service(args)


def test_tool_surface_contains_only_home_assistant_operations():
    assert home.TOOL_NAMES == {"home_assistant_get_states", "home_assistant_call_service"}
    assert all(term not in " ".join(home.TOOL_NAMES) for term in ("terminal", "file", "email", "web"))


def test_runtime_registry_exposes_only_the_private_toolset(configured, monkeypatch):
    from toolsets import resolve_toolset

    monkeypatch.setattr(home, "_REGISTERED", False)
    home.ensure_registered()
    assert set(resolve_toolset(home.TOOLSET)) == set(home.TOOL_NAMES)
