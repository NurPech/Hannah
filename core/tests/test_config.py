import pytest

from hannah import config


class TestLoad:
    def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            config.load(str(tmp_path / "does-not-exist.yaml"))

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
