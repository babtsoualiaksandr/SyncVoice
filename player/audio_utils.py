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


def extract_wav(path, search_bytes: int = 64 * 1024) -> bool:
    """True if `path` holds a WAV. Some PBX pages emit HTML before the file;
    if a RIFF/WAVE header follows within the first `search_bytes`, the junk
    before it is cut off in place."""
    with open(path, 'rb') as f:
        head = f.read(search_bytes)
    start = head.find(b'RIFF')
    while start != -1 and head[start + 8:start + 12] != b'WAVE':
        start = head.find(b'RIFF', start + 1)
    if start == -1:
        return False
    if start > 0:
        with open(path, 'rb') as src:
            src.seek(start)
            data = src.read()
        with open(path, 'wb') as dst:
            dst.write(data)
    return True
