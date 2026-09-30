import wave


def wav_duration(path) -> float | None:
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, OSError):
        return None  # non-PCM WAV; the browser will report the duration itself


def is_wav(path) -> bool:
    """True if the file starts with a RIFF/WAVE header (not, say, an HTML login page)."""
    with open(path, 'rb') as f:
        head = f.read(12)
    return head[:4] == b'RIFF' and head[8:12] == b'WAVE'
