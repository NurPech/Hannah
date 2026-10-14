from hannah.stt import _AzureSTT


def test_azure_uses_interactive_mode_for_commands():
    """Der Modus 'conversation' transkribiert Hintergrundgespräche hinter den Befehl (#432)."""
    stt = _AzureSTT({"azure_key": "k", "azure_region": "germanywestcentral", "language": "de"})

    assert "/speech/recognition/interactive/" in stt._url
    assert "/conversation/" not in stt._url
