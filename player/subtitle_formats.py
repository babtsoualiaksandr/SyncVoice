"""Export segments as subtitle files: SRT, WebVTT and plain text."""
from django.utils import timezone


def _timestamp(seconds: float, ms_sep: str) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f'{h:02}:{m:02}:{s:02}{ms_sep}{ms:03}'


def to_srt(segments) -> str:
    blocks = [
        f'{n}\n{_timestamp(s.start, ",")} --> {_timestamp(s.end, ",")}\n{s.text}\n'
        for n, s in enumerate(segments, 1)
    ]
    return '\n'.join(blocks)


def to_vtt(segments) -> str:
    blocks = [
        f'{_timestamp(s.start, ".")} --> {_timestamp(s.end, ".")}\n{s.text}\n'
        for s in segments
    ]
    return 'WEBVTT\n\n' + '\n'.join(blocks)


def to_txt(audio, segments) -> str:
    lines = []
    if audio.call_id:
        started = timezone.localtime(audio.call_started_at)
        lines += [
            f'Телефон: {audio.phone_display}',
            f'Оператор: {audio.operator}',
            f'Дата и время: {started:%d.%m.%Y %H:%M:%S}',
            '',
        ]
    else:
        lines += [str(audio), '']
    lines += [f'[{_timestamp(s.start, ".")[:8]}] {s.text}' for s in segments]
    return '\n'.join(lines) + '\n'


FORMATS = {
    'srt': ('application/x-subrip', lambda audio, segs: to_srt(segs)),
    'vtt': ('text/vtt', lambda audio, segs: to_vtt(segs)),
    'txt': ('text/plain', to_txt),
}


def export_filename(audio, ext: str) -> str:
    if audio.call_id:
        started = timezone.localtime(audio.call_started_at)
        stem = f'{audio.phone}_{audio.operator}_{started:%Y-%m-%d_%H-%M-%S}'
    else:
        stem = audio.title or f'audio-{audio.pk}'
    return f'{stem}.{ext}'
