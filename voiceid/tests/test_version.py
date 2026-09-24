import app


def test_reads_stamped_version(tmp_path, monkeypatch):
    f = tmp_path / "VERSION"
    f.write_text("0.88.0\n", encoding="utf-8")
    monkeypatch.setattr(app, "_VERSION_FILE", str(f))
    assert app.get_version() == "0.88.0"


def test_missing_file_falls_back_to_dev(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_VERSION_FILE", str(tmp_path / "VERSION"))
    assert app.get_version() == "dev"
