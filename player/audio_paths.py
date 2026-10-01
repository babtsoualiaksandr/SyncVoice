"""Where call recordings live: media/audio/<year>/<month>/<day>/<file>, by call date."""
import os
from pathlib import Path, PurePosixPath

from django.utils import timezone


def call_day(instance, filename: str):
    """The call's local date: from the record, else from a PBX-style file name, else today."""
    from .models import parse_call_name

    if getattr(instance, 'call_started_at', None):
        return timezone.localtime(instance.call_started_at).date()
    info = parse_call_name(filename)
    if info:
        return timezone.localtime(info['call_started_at']).date()
    return timezone.localdate()


def audio_upload_to(instance, filename: str) -> str:
    """upload_to for AudioFile.file."""
    day = call_day(instance, filename)
    return f'audio/{day:%Y/%m/%d}/{PurePosixPath(filename).name}'


def relocate(audio_model, media_root) -> int:
    """Move files stored the old way (media/audio/<file>) into the day folders.

    Returns how many files were moved. Missing files are left alone; a name
    taken in the target folder gets a numeric suffix.
    """
    media_root = Path(media_root)
    moved = 0
    for audio in audio_model.objects.exclude(file=''):
        old_name = audio.file.name
        new_name = audio_upload_to(audio, old_name)
        if new_name == old_name:
            continue
        old_path, new_path = media_root / old_name, media_root / new_name
        if not old_path.exists():
            continue
        new_path.parent.mkdir(parents=True, exist_ok=True)
        stem, suffix, n = new_path.stem, new_path.suffix, 1
        while new_path.exists():
            new_path = new_path.with_name(f'{stem}_{n}{suffix}')
            n += 1
        os.replace(old_path, new_path)
        audio_model.objects.filter(pk=audio.pk).update(file=new_path.relative_to(media_root).as_posix())
        moved += 1
    return moved
