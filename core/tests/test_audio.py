import numpy as np

from hannah.audio import resample_pcm


def _tone(freq_hz: float, rate: int, seconds: float = 1.0) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * freq_hz * t) * 12000).astype(np.int16).tobytes()


def _peak_hz(pcm: bytes, rate: int) -> float:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(samples))
    return float(np.argmax(spectrum) * rate / len(samples))


class TestResamplePcm:
    def test_same_rate_is_returned_unchanged(self):
        pcm = _tone(440, 16000)
        assert resample_pcm(pcm, 16000) is pcm

    def test_empty_input_is_returned_unchanged(self):
        assert resample_pcm(b"", 24000) == b""

    def test_azure_rate_is_brought_to_16k_without_changing_pitch(self):
        """#439: ein Ton muss nach dem Resampling dieselbe Frequenz haben — nur umetikettieren
        (oder die Bytes unverändert lassen) würde ihn um das 1,5-fache verschieben."""
        out = resample_pcm(_tone(440, 24000), 24000)

        assert abs(len(out) / 2 - 16000) < 200  # ~1 s bei 16 kHz
        assert abs(_peak_hz(out, 16000) - 440) < 5

    def test_piper_rate_is_brought_to_16k_without_changing_pitch(self):
        out = resample_pcm(_tone(440, 22050), 22050)

        assert abs(len(out) / 2 - 16000) < 200
        assert abs(_peak_hz(out, 16000) - 440) < 5

    def test_odd_trailing_byte_does_not_break(self):
        out = resample_pcm(_tone(440, 24000) + b"\x01", 24000)

        assert len(out) > 0
        assert len(out) % 2 == 0
