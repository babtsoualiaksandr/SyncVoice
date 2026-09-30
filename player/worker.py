"""Background worker, one job at a time: PBX downloads, then AI suggestions
(seconds each), then speech recognition (minutes each)."""
import logging
import threading
import time

from django.db import close_old_connections
from django.utils import timezone

from . import analysis
from .models import AppSettings, AudioFile, CallAnalysis, PbxSync
from .sync import run_sync
from .transcription import transcribe

logger = logging.getLogger(__name__)

IDLE_SLEEP = 3
ANALYSIS_BACKOFF = 60  # seconds to wait after a temporary Gemini failure
_analysis_paused_until = 0.0


def recover_interrupted() -> None:
    """Requeue jobs left half-done by a previous run (app closed mid-way)."""
    AudioFile.objects.filter(status=AudioFile.Status.PROCESSING).update(status=AudioFile.Status.PENDING)
    PbxSync.objects.filter(status=PbxSync.Status.RUNNING).update(status=PbxSync.Status.PENDING)


def heartbeat() -> None:
    AppSettings.load()  # make sure the row exists
    AppSettings.objects.filter(pk=1).update(worker_seen_at=timezone.now())


def run_once() -> bool:
    """Do one unit of work. Returns False when there was nothing to do."""
    heartbeat()
    sync = PbxSync.objects.filter(status=PbxSync.Status.PENDING).order_by('created_at').first()
    if sync:
        logger.info('Downloading recordings for %s', sync.day)
        run_sync(sync)
        return True
    if run_analysis_step():
        return True
    audio = (
        AudioFile.objects.filter(status=AudioFile.Status.PENDING)
        .order_by('call_started_at', 'created_at').first()
    )
    if audio:
        logger.info('Transcribing #%s %s', audio.pk, audio)
        # Keep the heartbeat fresh while a long recognition runs.
        stop = threading.Event()
        beat = threading.Thread(target=_beat_until, args=(stop,), daemon=True)
        beat.start()
        try:
            transcribe(audio)
        finally:
            stop.set()
        audio.refresh_from_db()
        if audio.status == AudioFile.Status.DONE and analysis.enabled():
            analysis.queue(audio)
        return True
    return False


def run_analysis_step() -> bool:
    """Analyse one queued call with Gemini. False when there was nothing to do."""
    global _analysis_paused_until
    if not analysis.enabled() or time.monotonic() < _analysis_paused_until:
        return False
    item = (
        CallAnalysis.objects.filter(status=CallAnalysis.Status.PENDING)
        .select_related('audio').order_by('updated_at').first()
    )
    if not item:
        return False
    logger.info('Analysing #%s with Gemini', item.audio_id)
    try:
        analysis.run_analysis(item)
    except analysis.RetryLater as exc:
        logger.warning('Gemini unavailable, retrying in %ss: %s', ANALYSIS_BACKOFF, exc)
        item.error = str(exc)
        item.save(update_fields=['error', 'updated_at'])
        _analysis_paused_until = time.monotonic() + ANALYSIS_BACKOFF
        return False  # let transcription continue meanwhile
    return True


def _beat_until(stop: threading.Event) -> None:
    while not stop.wait(10):
        close_old_connections()
        heartbeat()
    close_old_connections()


def run_forever(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    recover_interrupted()
    logger.info('Worker started')
    while not stop.is_set():
        close_old_connections()
        try:
            busy = run_once()
        except Exception:
            logger.exception('Worker step failed')
            busy = False
        if not busy:
            stop.wait(IDLE_SLEEP)
