import os

import pytest

import hannah.utils.db as db_module
from hannah.settings_manager import (
    DEFAULT_AUTOMATION_WORDS,
    DEFAULT_NLU_SETTINGS,
    DEFAULT_VOICE_ENROLLMENT_SETTINGS,
    SettingsManager,
)


@pytest.fixture
def manager(tmp_path):
    db_module.DB_PATH = os.path.join(str(tmp_path), "h.db")
    db_module.init_db()
    return SettingsManager(db_module.get_db)


class TestScalarJsonFieldRoundtrip:
    """Regression for #113: BaseModel.create()/update() only re-encoded __json_fields__
    when the value was a list/dict, so a plain string (like llm.system_prompt) got written
    to the DB unencoded and crashed the next read with JSONDecodeError."""

    def test_string_value_survives_create_and_read(self, manager):
        cat_id = manager.ensure_category("llm")
        text = 'Du bist Hannah.\nZeile zwei mit "Anführungszeichen".'
        created = manager.create_setting(cat_id, "system_prompt", text)
        assert created is not None

        settings = manager.get_settings()
        stored = next(s for s in settings if s["name"] == "system_prompt")
        assert stored["value"] == text

    def test_string_value_survives_update_and_read(self, manager):
        cat_id = manager.ensure_category("llm")
        created = manager.create_setting(cat_id, "system_prompt", "initial")

        ok = manager.update_setting_value(created["id"], "updated\nwith a newline")
        assert ok is True

        settings = manager.get_settings()
        stored = next(s for s in settings if s["id"] == created["id"])
        assert stored["value"] == "updated\nwith a newline"

    def test_list_value_still_works(self, manager):
        """Guards against regressing the list/dict case the isinstance check used to cover."""
        cat_id = manager.ensure_category("ble")
        created = manager.create_setting(cat_id, "tags", ["a", "b"])

        settings = manager.get_settings()
        stored = next(s for s in settings if s["id"] == created["id"])
        assert stored["value"] == ["a", "b"]


class TestSeedDefaults:
    """#114: a fresh install with an empty DB used to lose nlu.py's turn_on_words/
    turn_off_words/query_words (no code-level fallback, unlike category_words) once
    config.example.yaml's examples were trimmed. seed_defaults() restores working
    defaults for a fresh DB without ever touching a DB that already has data (migrated
    or admin-edited). #115 extends this to llm.system_prompt="" (a safe no-op default,
    see llm.py's `if system_prompt:` guard). iobroker.state_names used to be seeded here
    too, but #257 made it a hardcoded fallback in hannah.iobroker instead — no longer a
    DB setting, so it's not seeded here anymore."""

    def test_seeds_nlu_when_empty(self, manager):
        manager.seed_defaults()

        nlu = manager.get_settings_dict("nlu")
        assert nlu == DEFAULT_NLU_SETTINGS

    def test_seeds_llm_system_prompt_when_empty(self, manager):
        manager.seed_defaults()

        assert manager.get_settings_dict("llm") == {"system_prompt": ""}

    def test_does_not_overwrite_existing_nlu_values(self, manager):
        cat_id = manager.ensure_category("nlu")
        manager.create_setting(cat_id, "turn_on_words", ["custom_on"])

        manager.seed_defaults()

        nlu = manager.get_settings_dict("nlu")
        assert nlu == {"turn_on_words": ["custom_on"]}

    def test_does_not_overwrite_existing_llm_system_prompt(self, manager):
        cat_id = manager.ensure_category("llm")
        manager.create_setting(cat_id, "system_prompt", "Du bist Hannah.")

        manager.seed_defaults()

        assert manager.get_settings_dict("llm") == {"system_prompt": "Du bist Hannah."}

    def test_seeds_voice_enrollment_when_empty(self, manager):
        """hannah#8: Fragen-Pool/Ziel-Sprechzeit/Max-Fragenanzahl für den Voice-
        Enrollment-Dialog laufen wie nlu/llm über SettingsManager statt als Code-
        Konstanten, damit sie über die bestehende Settings-Admin-API editierbar sind."""
        manager.seed_defaults()

        assert manager.get_settings_dict("voice_enrollment") == DEFAULT_VOICE_ENROLLMENT_SETTINGS

    def test_does_not_overwrite_existing_voice_enrollment_values(self, manager):
        cat_id = manager.ensure_category("voice_enrollment")
        manager.create_setting(cat_id, "target_speech_s", 5.0)

        manager.seed_defaults()

        assert manager.get_settings_dict("voice_enrollment") == {"target_speech_s": 5.0}

    def test_idempotent_on_repeated_calls(self, manager):
        manager.seed_defaults()
        manager.seed_defaults()

        assert manager.get_settings_dict("nlu") == DEFAULT_NLU_SETTINGS
        assert manager.get_settings_dict("automations") == DEFAULT_AUTOMATION_WORDS
        assert manager.get_settings_dict("voice_enrollment") == DEFAULT_VOICE_ENROLLMENT_SETTINGS
        # + llm.system_prompt
        assert len(manager.get_settings()) == (
            len(DEFAULT_NLU_SETTINGS) + len(DEFAULT_AUTOMATION_WORDS)
            + len(DEFAULT_VOICE_ENROLLMENT_SETTINGS) + 1
        )
