"""Background worker: PBX downloads first, then speech recognition, one job at a time."""
import logging
import threading
import time

from django.db import close_old_connections
from django.utils import timezone

from .models import AppSettings, AudioFile, PbxSync
from .sync import run_sync
from .transcription import transcribe

logger = logging.getLogger(__name__)

IDLE_SLEEP = 3


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
        return True
    return False


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
