from hannah_telegram import config


class TestLoad:
    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = config.load(str(tmp_path / "does-not-exist.yaml"))
        assert cfg.telegram_token == ""
        assert cfg.webui_url == ""
        assert cfg.grpc.host == "127.0.0.1"
        assert cfg.grpc.port == 50051

    def test_valid_mapping_is_returned(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "telegram_token: abc123\nwebui_url: https://hannah.example.com\n"
            "grpc:\n  host: hannah-core\n  port: 12345\n",
            encoding="utf-8",
        )
        cfg = config.load(str(path))
        assert cfg.telegram_token == "abc123"
        assert cfg.webui_url == "https://hannah.example.com"
        assert cfg.grpc.host == "hannah-core"
        assert cfg.grpc.port == 12345


class TestEnvOverrides:
    def test_env_overrides_existing_value(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        path.write_text("telegram_token: from-file\n", encoding="utf-8")
        monkeypatch.setenv("HANNAH_TELEGRAM_TELEGRAM_TOKEN", "from-env")
        assert config.load(str(path)).telegram_token == "from-env"

    def test_env_sets_value_without_any_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HANNAH_TELEGRAM_TELEGRAM_TOKEN", "from-env")
        cfg = config.load(str(tmp_path / "missing.yaml"))
        assert cfg.telegram_token == "from-env"

    def test_double_underscore_addresses_nested_grpc_section(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HANNAH_TELEGRAM_GRPC__HOST", "hannah-core")
        monkeypatch.setenv("HANNAH_TELEGRAM_GRPC__PORT", "12345")
        cfg = config.load(str(tmp_path / "missing.yaml"))
        assert cfg.grpc.host == "hannah-core"
        assert cfg.grpc.port == 12345

    def test_port_is_coerced_to_int(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HANNAH_TELEGRAM_GRPC__PORT", "9999")
        cfg = config.load(str(tmp_path / "missing.yaml"))
        assert cfg.grpc.port == 9999
        assert isinstance(cfg.grpc.port, int)

    def test_other_hannah_prefixed_vars_from_unrelated_tools_are_not_absorbed(
        self, tmp_path, monkeypatch
    ):
        # Same collision Core hit in CI (#327): other HANNAH_* variables exist in the
        # project for unrelated purposes and must not affect this component's config.
        monkeypatch.setenv("HANNAH_ASSET_SERVER_BASE_URL", "https://hannah-asset.example.com")
        monkeypatch.setenv("HANNAH_CORE_MQTT__HOST", "192.168.1.5")
        cfg = config.load(str(tmp_path / "missing.yaml"))
        assert cfg.telegram_token == ""
        assert cfg.webui_url == ""

    def test_env_override_does_not_affect_sibling_fields(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        path.write_text("telegram_token: from-file\nwebui_url: https://x.example.com\n", encoding="utf-8")
        monkeypatch.setenv("HANNAH_TELEGRAM_TELEGRAM_TOKEN", "overridden")
        cfg = config.load(str(path))
        assert cfg.telegram_token == "overridden"
        assert cfg.webui_url == "https://x.example.com"
