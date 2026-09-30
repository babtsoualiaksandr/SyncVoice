import re
from datetime import datetime
from pathlib import Path

from django.db import models
from django.urls import reverse
from django.utils import timezone

# Call recording names from the PBX:
# out-<phone>-<operator>-<YYYYMMDD>-<HHMMSS>-<call id>.wav
# When the name is taken, Django storage inserts "_<random>" before the first
# dot, e.g. "...-1790415184_kRlAzSK.21439.wav", so allow that inside the call id.
CALL_NAME_RE = re.compile(
    r'^(?P<direction>[a-z]+)-(?P<phone>\d+)-(?P<operator>\d+)-'
    r'(?P<date>\d{8})-(?P<time>\d{6})-(?P<call_sec>\d+)(?:_[A-Za-z0-9]+)?\.(?P<call_seq>\d+)'
)


def parse_call_name(name: str) -> dict | None:
    """Extract call details from a recording file name, or None if it doesn't match."""
    m = CALL_NAME_RE.match(Path(name).name)
    if not m:
        return None
    try:
        started = datetime.strptime(m['date'] + m['time'], '%Y%m%d%H%M%S')
    except ValueError:
        return None
    return {
        'phone': m['phone'],
        'operator': m['operator'],
        'call_started_at': timezone.make_aware(started),
        'call_id': f"{m['call_sec']}.{m['call_seq']}",
    }


class AudioFile(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'В очереди'
        PROCESSING = 'processing', 'Распознаётся'
        DONE = 'done', 'Готово'
        ERROR = 'error', 'Ошибка'

    title = models.CharField('название', max_length=255, blank=True)
    file = models.FileField('файл', upload_to='audio/')
    duration = models.FloatField('длительность, с', null=True, blank=True)
    phone = models.CharField('номер телефона', max_length=32, blank=True, db_index=True)
    operator = models.CharField('номер оператора', max_length=16, blank=True, db_index=True)
    call_started_at = models.DateTimeField('дата и время звонка', null=True, blank=True)
    call_id = models.CharField('ID звонка', max_length=64, blank=True, db_index=True)
    language = models.CharField('язык', max_length=16, blank=True)
    status = models.CharField('статус', max_length=16, choices=Status, default=Status.PENDING)
    error = models.TextField('ошибка', blank=True)
    created_at = models.DateTimeField('загружен', auto_now_add=True)

    class Meta:
        ordering = ['-call_started_at', '-created_at']
        verbose_name = 'аудиофайл'
        verbose_name_plural = 'аудиофайлы'

    def __str__(self):
        return self.title or Path(self.file.name).name

    def save(self, *args, **kwargs):
        if self.file:
            if not self.title:
                self.title = Path(self.file.name).stem
            if not self.call_id and (info := parse_call_name(self.file.name)):
                for field, value in info.items():
                    setattr(self, field, value)
        super().save(*args, **kwargs)

    @property
    def phone_display(self):
        """+375 (29) 000-01-01 for Belarusian numbers, +<digits> otherwise."""
        p = self.phone
        if len(p) == 12 and p.startswith('375'):
            return f'+375 ({p[3:5]}) {p[5:8]}-{p[8:10]}-{p[10:]}'
        return f'+{p}' if p else ''

    def get_absolute_url(self):
        return reverse('player:detail', args=[self.pk])


class Segment(models.Model):
    """One subtitle line: a piece of recognized text with its time range."""

    audio = models.ForeignKey(AudioFile, on_delete=models.CASCADE, related_name='segments')
    index = models.PositiveIntegerField()
    start = models.FloatField('начало, с')
    end = models.FloatField('конец, с')
    text = models.TextField('текст')

    class Meta:
        ordering = ['audio', 'index']
        constraints = [
            models.UniqueConstraint(fields=['audio', 'index'], name='unique_segment_index'),
        ]

    def __str__(self):
        return f'{self.start:.1f}–{self.end:.1f}: {self.text[:50]}'
