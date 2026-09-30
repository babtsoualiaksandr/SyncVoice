"""Speech recognition.

Two engines: faster-whisper (CTranslate2, int8, no torch) and openai-whisper
(torch). Which one is faster depends on the machine — on an old Intel Mac
openai-whisper measured ~4.7x faster, since the macOS x86 CTranslate2 build
lacks MKL. settings.WHISPER_ENGINE picks one; if it isn't installed the
other is used. `manage.py asr_benchmark` measures both on the current machine.

The model is loaded once per process and reused; recognitions run one at a
time (the worker processes the queue sequentially, the lock guards against
accidental concurrent calls).
"""
import importlib.util
import logging
import threading

from django.conf import settings
from django.db import transaction

from .models import AudioFile, Segment

logger = logging.getLogger(__name__)

_engine = None
_lock = threading.Lock()


class FasterWhisperEngine:
    name = 'faster-whisper'

    def __init__(self, model_name):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model_name, device='cpu', compute_type='int8')

    def transcribe(self, path, language):
        segments, info = self.model.transcribe(
            str(path), language=language, vad_filter=True, beam_size=5,
        )
        segments = [(s.start, s.end, s.text) for s in segments]  # generator: decoding happens here
        return segments, info.language


class OpenAIWhisperEngine:
    name = 'openai-whisper'

    def __init__(self, model_name):
        import whisper

        self.model = whisper.load_model(model_name)

    def transcribe(self, path, language):
        result = self.model.transcribe(str(path), language=language, fp16=False)
        segments = [(s['start'], s['end'], s['text']) for s in result['segments']]
        return segments, result.get('language')


ENGINES = {
    FasterWhisperEngine.name: (FasterWhisperEngine, 'faster_whisper'),
    OpenAIWhisperEngine.name: (OpenAIWhisperEngine, 'whisper'),
}


def available_engines() -> list[str]:
    # find_spec checks without importing: loading both engines into one
    # process brings two OpenMP runtimes (torch + CTranslate2) and segfaults.
    return [name for name, (_, module) in ENGINES.items() if importlib.util.find_spec(module)]


def make_engine(name: str | None = None):
    """Engine `name` (default settings.WHISPER_ENGINE), or whichever is installed."""
    available = available_engines()
    if not available:
        raise RuntimeError('Не установлен ни faster-whisper, ни openai-whisper.')
    name = name or settings.WHISPER_ENGINE
    if name not in available:
        name = available[0]
    engine_cls, _ = ENGINES[name]
    logger.info('Loading %s model %r', name, settings.WHISPER_MODEL)
    return engine_cls(settings.WHISPER_MODEL)


def _get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def transcribe(audio: AudioFile) -> None:
    """Recognize speech in `audio` and replace its segments with the result."""
    with _lock:
        audio.status = AudioFile.Status.PROCESSING
        audio.error = ''
        audio.save(update_fields=['status', 'error'])
        try:
            raw_segments, language = _get_engine().transcribe(audio.file.path, settings.WHISPER_LANGUAGE)
        except Exception as exc:
            logger.exception('Transcription of %s failed', audio.pk)
            audio.status = AudioFile.Status.ERROR
            audio.error = str(exc)
            audio.save(update_fields=['status', 'error'])
            return

    segments = [
        Segment(audio=audio, index=i, start=start, end=end, text=text.strip())
        for i, (start, end, text) in enumerate(raw_segments)
    ]
    with transaction.atomic():
        audio.segments.all().delete()
        Segment.objects.bulk_create(segments)
        audio.language = language or ''
        audio.status = AudioFile.Status.DONE
        audio.save(update_fields=['language', 'status'])
