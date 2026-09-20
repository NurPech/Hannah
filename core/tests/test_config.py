import pytest

from hannah import config


class TestLoad:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert config.load(str(tmp_path / "does-not-exist.yaml")) == {}

    def test_valid_mapping_is_returned(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("mqtt:\n  host: localhost\n", encoding="utf-8")
        assert config.load(str(path)) == {"mqtt": {"host": "localhost"}}

    def test_non_mapping_content_raises_value_error(self, tmp_path):
        # Reproduces #324: pasting a shell command into the config file instead
        # of running it folds into a single plain YAML scalar (a string), not a dict.
        path = tmp_path / "config.yaml"
        path.write_text(
            "curl -fsSL https://example.com/config.example.yaml -o core-config.yaml\n"
            "nano core-config.yaml\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            config.load(str(path))

    def test_empty_file_raises_value_error(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("", encoding="utf-8")
        with pytest.raises(ValueError):
            config.load(str(path))

    def test_non_utf8_file_raises_value_error_not_unicode_decode_error(self, tmp_path):
        # Reproduces #325: an editor that saves the file in a non-UTF-8 encoding
        # (e.g. after ignoring an encoding warning) must not surface a raw
        # UnicodeDecodeError — it should look like any other config problem.
        path = tmp_path / "config.yaml"
        path.write_bytes("mqtt:\n  host: müller\n".encode("latin-1"))
        with pytest.raises(ValueError):
            config.load(str(path))


class TestEnvOverrides:
    def test_env_overrides_existing_value(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        path.write_text("mqtt:\n  host: localhost\n", encoding="utf-8")
        monkeypatch.setenv("HANNAH_CORE_MQTT__HOST", "192.168.1.5")
        assert config.load(str(path)) == {"mqtt": {"host": "192.168.1.5"}}

    def test_env_adds_key_not_present_in_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HANNAH_CORE_MQTT__HOST", "192.168.1.5")
        assert config.load(str(tmp_path / "missing.yaml")) == {"mqtt": {"host": "192.168.1.5"}}

    def test_double_underscore_separates_nesting_single_underscore_stays_in_key(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HANNAH_CORE_LLM__FALLBACK_RESPONSE", "Nope.")
        assert config.load(str(tmp_path / "missing.yaml")) == {
            "llm": {"fallback_response": "Nope."}
        }

    def test_values_are_type_coerced(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HANNAH_CORE_MQTT__PORT", "1883")
        monkeypatch.setenv("HANNAH_CORE_LLM__ENABLED", "true")
        monkeypatch.setenv("HANNAH_CORE_TTS__LENGTH_SCALE", "1.2")
        cfg = config.load(str(tmp_path / "missing.yaml"))
        assert cfg["mqtt"]["port"] == 1883
        assert cfg["llm"]["enabled"] is True
        assert cfg["tts"]["length_scale"] == 1.2

    def test_unrelated_env_vars_are_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOME_OTHER_TOOL_HOST", "not-hannah")
        assert config.load(str(tmp_path / "missing.yaml")) == {}

    def test_other_hannah_prefixed_vars_from_unrelated_tools_are_not_absorbed(
        self, tmp_path, monkeypatch
    ):
        # Reproduces a real CI failure: the project already has other HANNAH_*
        # variables in scope for unrelated purposes (asset/update-server tokens,
        # satellite NVS) — only HANNAH_CORE_* may be treated as config overrides.
        monkeypatch.setenv("HANNAH_ASSET_SERVER_BASE_URL", "https://hannah-asset.example.com")
        monkeypatch.setenv("HANNAH_UPDATE_UPLOAD_TOKEN", "secret")
        assert config.load(str(tmp_path / "missing.yaml")) == {}

    def test_env_override_does_not_clobber_sibling_keys(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        path.write_text("mqtt:\n  host: localhost\n  port: 1883\n", encoding="utf-8")
        monkeypatch.setenv("HANNAH_CORE_MQTT__HOST", "overridden")
        cfg = config.load(str(path))
        assert cfg["mqtt"] == {"host": "overridden", "port": 1883}
