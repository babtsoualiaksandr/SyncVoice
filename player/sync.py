"""Download a day's recordings from the PBX into AudioFile rows (run by the worker)."""
import logging
import tempfile
from pathlib import Path

from django.core.files import File
from django.utils import timezone

from .audio_utils import wav_duration
from .credentials import get_password
from .models import AppSettings, AudioFile, Interviewer, PbxSync, parse_call_name
from .pbx import FreePbxClient, PbxError

logger = logging.getLogger(__name__)


def make_client(settings: AppSettings | None = None) -> FreePbxClient:
    settings = settings or AppSettings.load()
    return FreePbxClient(
        settings.pbx_url, settings.pbx_username,
        get_password(settings.pbx_username) or '', settings.pbx_verify_ssl,
    )


def run_sync(sync: PbxSync, client: FreePbxClient | None = None) -> None:
    settings = AppSettings.load()
    sync.status = PbxSync.Status.RUNNING
    sync.error = ''
    sync.save(update_fields=['status', 'error'])
    try:
        client = client or make_client(settings)
        recordings = client.list_recordings(sync.day)

        extensions = set(Interviewer.objects.values_list('extension', flat=True))
        wanted = []
        for rec in recordings:
            info = parse_call_name(rec.filename)
            operator = info['operator'] if info else rec.src
            if extensions and operator not in extensions:
                continue
            if rec.billsec < settings.min_duration:
                continue
            wanted.append((rec, info))
        sync.found = len(wanted)
        sync.save(update_fields=['found'])

        with tempfile.TemporaryDirectory() as tmp_dir:
            for rec, info in wanted:
                call_id = info['call_id'] if info else rec.uniqueid
                if AudioFile.objects.filter(call_id=call_id).exists():
                    sync.skipped += 1
                    sync.save(update_fields=['skipped'])
                    continue
                tmp_path = Path(tmp_dir) / rec.filename
                client.download(rec, tmp_path)
                audio = AudioFile(sync=sync, call_id='' if info else rec.uniqueid)
                with tmp_path.open('rb') as f:
                    audio.file.save(rec.filename, File(f), save=False)
                audio.duration = wav_duration(audio.file.path)
                audio.save()
                tmp_path.unlink()
                sync.downloaded += 1
                sync.save(update_fields=['downloaded'])
    except PbxError as exc:
        _finish(sync, PbxSync.Status.ERROR, str(exc))
    except Exception as exc:
        logger.exception('PBX sync %s failed', sync.pk)
        _finish(sync, PbxSync.Status.ERROR, f'Непредвиденная ошибка: {exc}')
    else:
        _finish(sync, PbxSync.Status.DONE)


def _finish(sync: PbxSync, status: str, error: str = '') -> None:
    sync.status = status
    sync.error = error
    sync.finished_at = timezone.now()
    sync.save(update_fields=['status', 'error', 'finished_at'])
