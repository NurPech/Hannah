from hannah import __version__ as version_mod


def test_reads_stamped_version(tmp_path, monkeypatch):
    f = tmp_path / "VERSION"
    f.write_text("0.88.0\n", encoding="utf-8")
    monkeypatch.setattr(version_mod, "_VERSION_FILE", f)
    assert version_mod._read_version() == "0.88.0"


def test_missing_file_falls_back_to_dev(tmp_path, monkeypatch):
    monkeypatch.setattr(version_mod, "_VERSION_FILE", tmp_path / "VERSION")
    assert version_mod._read_version() == "dev"


def test_empty_file_falls_back_to_dev(tmp_path, monkeypatch):
    f = tmp_path / "VERSION"
    f.write_text("\n", encoding="utf-8")
    monkeypatch.setattr(version_mod, "_VERSION_FILE", f)
    assert version_mod._read_version() == "dev"
