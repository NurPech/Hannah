from app import _load_config


class TestLoadConfig:
    def test_missing_path_returns_empty_dict(self):
        assert _load_config("") == {}

    def test_nonexistent_file_returns_empty_dict(self, tmp_path):
        assert _load_config(str(tmp_path / "does-not-exist.yaml")) == {}

    def test_valid_mapping_is_returned(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("recognition:\n  unknown_threshold: 0.5\n", encoding="utf-8")
        assert _load_config(str(path)) == {"recognition": {"unknown_threshold": 0.5}}


class TestEnvOverrides:
    def test_env_overrides_existing_value(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        path.write_text("recognition:\n  unknown_threshold: 0.5\n", encoding="utf-8")
        monkeypatch.setenv("HANNAH_VOICEID_RECOGNITION__UNKNOWN_THRESHOLD", "0.8")
        assert _load_config(str(path)) == {"recognition": {"unknown_threshold": 0.8}}

    def test_env_sets_value_without_any_file(self, monkeypatch):
        monkeypatch.setenv("HANNAH_VOICEID_RECOGNITION__UNKNOWN_THRESHOLD", "0.8")
        assert _load_config("") == {"recognition": {"unknown_threshold": 0.8}}

    def test_values_are_type_coerced(self, monkeypatch):
        monkeypatch.setenv("HANNAH_VOICEID_SERVER__PORT", "9090")
        cfg = _load_config("")
        assert cfg["server"]["port"] == 9090
        assert isinstance(cfg["server"]["port"], int)

    def test_other_hannah_prefixed_vars_from_unrelated_tools_are_not_absorbed(self, monkeypatch):
        # Same collision Core hit in CI (#327): other HANNAH_* variables exist in the
        # project for unrelated purposes and must not affect this component's config.
        monkeypatch.setenv("HANNAH_ASSET_SERVER_BASE_URL", "https://hannah-asset.example.com")
        monkeypatch.setenv("HANNAH_CORE_MQTT__HOST", "192.168.1.5")
        assert _load_config("") == {}

    def test_env_override_does_not_clobber_sibling_keys(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        path.write_text(
            "recognition:\n  unknown_threshold: 0.5\n  uncertain_threshold: 0.6\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HANNAH_VOICEID_RECOGNITION__UNKNOWN_THRESHOLD", "0.9")
        cfg = _load_config(str(path))
        assert cfg["recognition"] == {"unknown_threshold": 0.9, "uncertain_threshold": 0.6}
