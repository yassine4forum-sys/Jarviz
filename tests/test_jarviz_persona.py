"""Durable, profile-scoped JarViz persona behavior."""
import pytest


def test_defaults_and_partial_update_persist(tmp_path, monkeypatch):
    from api import config, profiles
    from api.jarviz_persona import PersonaStore

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    profiles.set_request_profile("alpha")
    try:
        store = PersonaStore(tmp_path)
        defaults = store.get()
        assert defaults == {
            "name": "JarViz", "tone": "warm and direct", "verbosity": "balanced",
            "languages": ["en", "fr", "darija"], "voice": "Kore",
            "announce_task_start": True, "announce_task_completion": True,
            "announce_blockers": True, "speak_technical_logs": False,
        }
        updated = store.update({"name": "JARVIZ", "verbosity": "concise", "announce_task_start": False})
        assert updated["name"] == "JARVIZ" and updated["verbosity"] == "concise"
        assert updated["announce_task_start"] is False
        assert PersonaStore(tmp_path).get()["verbosity"] == "concise"
    finally:
        profiles.clear_request_profile()


def test_persona_isolated_by_active_profile(tmp_path, monkeypatch):
    from api import config, profiles
    from api.jarviz_persona import PersonaStore

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    store = PersonaStore(tmp_path)
    try:
        profiles.set_request_profile("alpha")
        store.update({"tone": "quiet"})
        profiles.set_request_profile("beta")
        assert store.get()["tone"] == "warm and direct"
        store.update({"tone": "playful"})
        profiles.set_request_profile("alpha")
        assert store.get()["tone"] == "quiet"
    finally:
        profiles.clear_request_profile()


@pytest.mark.parametrize("changes", [
    {"verbosity": "tiny"}, {"languages": ["es"]}, {"languages": ["en", "en"]},
    {"voice": "../../secret"}, {"announce_blockers": 1}, {"unknown": True}, {},
])
def test_invalid_persona_updates_are_rejected(tmp_path, changes):
    from api.jarviz_persona import PersonaStore

    with pytest.raises(ValueError):
        PersonaStore(tmp_path).update(changes, "alpha")


def test_instruction_supports_english_french_and_darija():
    from api.jarviz_persona import DEFAULT_PERSONA, persona_instruction

    instruction = persona_instruction(DEFAULT_PERSONA)
    assert "English" in instruction and "French" in instruction and "Moroccan Darija" in instruction
    assert "Arabic or Latin characters" in instruction
    assert "update_persona" in instruction
