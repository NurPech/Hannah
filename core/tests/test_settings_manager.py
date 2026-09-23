import os

import pytest

import hannah.utils.db as db_module
from hannah.settings_manager import (
    DEFAULT_AUTOMATION_WORDS,
    DEFAULT_NLU_SETTINGS,
    DEFAULT_PRESENCE_SETTINGS,
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

    def test_seeds_presence_when_empty(self, manager):
        """hannah#294: globale Presence-Fusion-Parameter (default_confidence,
        grace_period_seconds) laufen wie nlu/llm über SettingsManager."""
        manager.seed_defaults()

        assert manager.get_settings_dict("presence") == DEFAULT_PRESENCE_SETTINGS

    def test_does_not_overwrite_existing_presence_values(self, manager):
        cat_id = manager.ensure_category("presence")
        manager.create_setting(cat_id, "grace_period_seconds", 60)

        manager.seed_defaults()

        assert manager.get_settings_dict("presence") == {"grace_period_seconds": 60}

    def test_idempotent_on_repeated_calls(self, manager):
        manager.seed_defaults()
        manager.seed_defaults()

        assert manager.get_settings_dict("nlu") == DEFAULT_NLU_SETTINGS
        assert manager.get_settings_dict("automations") == DEFAULT_AUTOMATION_WORDS
        assert manager.get_settings_dict("voice_enrollment") == DEFAULT_VOICE_ENROLLMENT_SETTINGS
        assert manager.get_settings_dict("presence") == DEFAULT_PRESENCE_SETTINGS
        # + llm.system_prompt
        assert len(manager.get_settings()) == (
            len(DEFAULT_NLU_SETTINGS) + len(DEFAULT_AUTOMATION_WORDS)
            + len(DEFAULT_VOICE_ENROLLMENT_SETTINGS) + len(DEFAULT_PRESENCE_SETTINGS) + 1
        )


class TestRunDataMigrations:
    """#317: seed_defaults() only fills an empty category, so new default words never
    reach existing installs. run_data_migrations() adds them once — the marker in
    applied_migrations keeps a word the user deliberately removed afterwards from
    coming back on every start."""

    _NEW_BLIND_WORDS = {"rolladen": "blind", "rolllaeden": "blind", "rollaeden": "blind"}

    def _seed_custom_category_words(self, manager, words):
        cat_id = manager.ensure_category("nlu")
        manager.create_setting(cat_id, "category_words", words)

    def test_adds_missing_blind_words_to_existing_install(self, manager):
        self._seed_custom_category_words(manager, {"rollladen": "blind", "lampe": "light"})

        manager.run_data_migrations()

        words = manager.get_settings_dict("nlu")["category_words"]
        assert words == {"rollladen": "blind", "lampe": "light", **self._NEW_BLIND_WORDS}

    def test_does_not_overwrite_differently_mapped_word(self, manager):
        self._seed_custom_category_words(manager, {"rolladen": "light"})

        manager.run_data_migrations()

        assert manager.get_settings_dict("nlu")["category_words"]["rolladen"] == "light"

    def test_removed_word_does_not_come_back(self, manager):
        self._seed_custom_category_words(manager, {"rollladen": "blind"})
        manager.run_data_migrations()
        setting = next(s for s in manager.get_settings() if s["name"] == "category_words")
        manager.update_setting_value(setting["id"], {"rollladen": "blind"})

        manager.run_data_migrations()

        assert manager.get_settings_dict("nlu")["category_words"] == {"rollladen": "blind"}

    def test_fresh_install_keeps_seeded_defaults(self, manager):
        manager.seed_defaults()

        manager.run_data_migrations()

        assert manager.get_settings_dict("nlu") == DEFAULT_NLU_SETTINGS

    def test_without_stored_category_words_only_sets_marker(self, manager):
        manager.run_data_migrations()

        assert manager.get_settings_dict("nlu") == {}
        applied = {r[0] for r in db_module.get_db().execute('SELECT "name" FROM "applied_migrations"').fetchall()}
        assert "317_blind_category_words" in applied


class TestCleanupLegacySettings:
    """#257: iobroker.state_names used to be migrated into the DB (deploy/
    migrate_config_settings.py) but is a hardcoded, non-editable fallback in
    hannah.iobroker now — GetSettings/WebUI show every DB row unfiltered, so an
    already-migrated leftover would otherwise sit there forever looking editable
    while silently doing nothing."""

    def test_removes_leftover_iobroker_category_and_its_settings(self, manager):
        cat_id = manager.ensure_category("iobroker")
        manager.create_setting(cat_id, "state_names", {"current": "current"})

        manager.cleanup_legacy_settings()

        assert manager.get_category_id("iobroker") is None
        assert manager.get_settings_dict("iobroker") == {}

    def test_noop_when_no_iobroker_category_exists(self, manager):
        manager.seed_defaults()

        manager.cleanup_legacy_settings()

        assert manager.get_settings_dict("nlu") == DEFAULT_NLU_SETTINGS

    def test_idempotent_on_repeated_calls(self, manager):
        cat_id = manager.ensure_category("iobroker")
        manager.create_setting(cat_id, "state_names", {"current": "current"})

        manager.cleanup_legacy_settings()
        manager.cleanup_legacy_settings()

        assert manager.get_category_id("iobroker") is None
