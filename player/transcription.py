"""Speech recognition with Whisper.

The model is loaded once per process and reused. Whisper is CPU-heavy, so
recognitions run one at a time: a lock serializes them.
"""
import logging
import threading

from django.conf import settings
from django.db import close_old_connections, transaction

from .models import AudioFile, Segment

logger = logging.getLogger(__name__)

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        import whisper  # heavy import (torch), load only when needed

        logger.info('Loading Whisper model %r', settings.WHISPER_MODEL)
        _model = whisper.load_model(settings.WHISPER_MODEL)
    return _model


def transcribe(audio: AudioFile) -> None:
    """Recognize speech in `audio` and replace its segments with the result."""
    with _lock:
        audio.status = AudioFile.Status.PROCESSING
        audio.error = ''
        audio.save(update_fields=['status', 'error'])
        try:
            result = _get_model().transcribe(
                audio.file.path,
                language=settings.WHISPER_LANGUAGE,
                fp16=False,  # fp16 is not supported on CPU
            )
        except Exception as exc:
            logger.exception('Transcription of %s failed', audio.pk)
            audio.status = AudioFile.Status.ERROR
            audio.error = str(exc)
            audio.save(update_fields=['status', 'error'])
            return

    segments = [
        Segment(audio=audio, index=i, start=s['start'], end=s['end'], text=s['text'].strip())
        for i, s in enumerate(result['segments'])
    ]
    with transaction.atomic():
        audio.segments.all().delete()
        Segment.objects.bulk_create(segments)
        audio.language = result.get('language') or ''
        audio.status = AudioFile.Status.DONE
        audio.save(update_fields=['language', 'status'])


def transcribe_in_background(audio_id: int) -> None:
    """Run `transcribe` in a daemon thread so the request returns immediately.

    Fine for a single-process dev server; for production use a task queue
    (Celery, RQ, ...).
    """

    def run():
        close_old_connections()
        try:
            transcribe(AudioFile.objects.get(pk=audio_id))
        finally:
            close_old_connections()

    threading.Thread(target=run, daemon=True, name=f'transcribe-{audio_id}').start()
