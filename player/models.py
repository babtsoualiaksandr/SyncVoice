import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from django.db import models
from django.urls import reverse
from django.utils import timezone

from .audio_paths import audio_upload_to

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


class AppSettings(models.Model):
    """Single row of user-editable settings (PBX access, controller, filters).

    The PBX password is not stored here: it lives in the OS credential store
    (Windows Credential Manager / macOS Keychain), see player/credentials.py.
    """

    pbx_url = models.URLField('адрес АТС', blank=True, help_text='Например, http://192.168.3.80')
    pbx_username = models.CharField('логин АТС', max_length=150, blank=True)
    pbx_verify_ssl = models.BooleanField(
        'проверять SSL-сертификат АТС', default=False,
        help_text='У АТС в локальной сети обычно самоподписанный сертификат — тогда выключено.',
    )
    controller = models.CharField('инициалы контролёра', max_length=16, blank=True, help_text='Например, ИБ')
    min_duration = models.PositiveIntegerField(
        'мин. длительность звонка, с', default=80,
        help_text='Короче не скачиваются (недозвоны, сбросы). Как «Длительность: между» в «Отчётах CDR».',
    )
    max_duration = models.PositiveIntegerField(
        'макс. длительность звонка, с', default=900,
        help_text='Длиннее не скачиваются. 0 — без ограничения.',
    )
    crm_url = models.URLField(
        'адрес CRM', blank=True,
        help_text='CRM с анкетами операторов, например http://192.168.12.230. SyncVoice сам находит анкету звонка.',
    )
    crm_username = models.CharField(
        'логин CRM (email)', max_length=150, blank=True,
        help_text='Отдельная учётная запись для SyncVoice. Через SyncVoice анкеты только просматриваются.',
    )
    survey_url = models.CharField(
        'ссылка на анкету оператора', max_length=500, blank=True,
        help_text='Адрес страницы CRM с ответами респондента. Подстановки: {phone} — 375291234567, '
                  '{phone_local} — 291234567, {date} — 2026-09-29, {date_ru} — 29.09.2026, '
                  '{time} — 14:01, {operator} — 301, {call_id}. Пусто — панели анкеты нет.',
    )
    worker_seen_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = 'настройки'
        verbose_name_plural = 'настройки'

    def __str__(self):
        return 'Настройки'

    @classmethod
    def load(cls) -> 'AppSettings':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def worker_alive(self) -> bool:
        return bool(self.worker_seen_at) and (timezone.now() - self.worker_seen_at).total_seconds() < 30


class Interviewer(models.Model):
    """Interviewer working on an extension. One extension can have several (shifts)."""

    extension = models.CharField('внутр. номер', max_length=16, db_index=True)
    name = models.CharField('имя', max_length=100)

    class Meta:
        ordering = ['extension', 'name']
        constraints = [
            models.UniqueConstraint(fields=['extension', 'name'], name='unique_interviewer'),
        ]
        verbose_name = 'интервьюер'
        verbose_name_plural = 'интервьюеры'

    def __str__(self):
        return f'{self.name} {self.extension}'  # format used in the report: «Марина 308»


class PbxSync(models.Model):
    """A request to download one day's recordings from the PBX (run by the worker)."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'В очереди'
        RUNNING = 'running', 'Скачивается'
        DONE = 'done', 'Готово'
        ERROR = 'error', 'Ошибка'

    day = models.DateField('день звонков')
    status = models.CharField('статус', max_length=16, choices=Status, default=Status.PENDING)
    found = models.PositiveIntegerField('найдено', default=0)
    downloaded = models.PositiveIntegerField('скачано', default=0)
    skipped = models.PositiveIntegerField('уже были', default=0)
    failed = models.PositiveIntegerField('не скачано', default=0)
    error = models.TextField('ошибка', blank=True)
    created_at = models.DateTimeField('создано', auto_now_add=True)
    finished_at = models.DateTimeField('завершено', null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'загрузка из АТС'
        verbose_name_plural = 'загрузки из АТС'

    def __str__(self):
        return f'{self.day:%d.%m.%Y} — {self.get_status_display()}'


class AudioFile(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'В очереди'
        PROCESSING = 'processing', 'Распознаётся'
        DONE = 'done', 'Готово'
        ERROR = 'error', 'Ошибка'

    title = models.CharField('название', max_length=255, blank=True)
    file = models.FileField('файл', upload_to=audio_upload_to, max_length=255)
    duration = models.FloatField('длительность, с', null=True, blank=True)
    phone = models.CharField('номер телефона', max_length=32, blank=True, db_index=True)
    operator = models.CharField('номер оператора', max_length=16, blank=True, db_index=True)
    call_started_at = models.DateTimeField('дата и время звонка', null=True, blank=True)
    call_id = models.CharField('ID звонка', max_length=64, blank=True, db_index=True)
    language = models.CharField('язык', max_length=16, blank=True)
    status = models.CharField('статус', max_length=16, choices=Status, default=Status.PENDING)
    error = models.TextField('ошибка', blank=True)
    sync = models.ForeignKey(
        PbxSync, verbose_name='загрузка из АТС', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='audio_files',
    )
    created_at = models.DateTimeField('загружен', auto_now_add=True)

    class Meta:
        ordering = ['-call_started_at', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['call_id'], condition=~models.Q(call_id=''), name='unique_call_id',
            ),
        ]
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

    def survey_url(self) -> str | None:
        """Link to this respondent's answers in the operators' CRM, or None.

        With the CRM configured it is SyncVoice's own /crm/open/ link, which
        finds the survey and redirects to it; otherwise the template link.
        """
        settings = AppSettings.load()
        if settings.crm_url and settings.crm_username and self.phone:
            return reverse('player:crm_open', args=[self.pk])
        template = settings.survey_url.strip()
        if not template or not self.phone:
            return None
        started = timezone.localtime(self.call_started_at) if self.call_started_at else None
        values = {
            'phone': self.phone,
            'phone_local': self.phone[3:] if self.phone.startswith('375') else self.phone,
            'date': f'{started:%Y-%m-%d}' if started else '',
            'date_ru': f'{started:%d.%m.%Y}' if started else '',
            'time': f'{started:%H:%M}' if started else '',
            'operator': self.operator,
            'call_id': self.call_id,
        }
        return re.sub(
            r'\{(\w+)\}',
            lambda m: quote(values[m[1]], safe='') if m[1] in values else m[0],
            template,
        )

    def default_interviewer(self) -> str:
        """Interviewer last chosen for this extension, else the first one known, else the bare number."""
        last = (
            CallReview.objects.filter(audio__operator=self.operator).exclude(interviewer='')
            .order_by('-updated_at').values_list('interviewer', flat=True).first()
        )
        if last:
            return last
        first = Interviewer.objects.filter(extension=self.operator).first()
        return str(first) if first else self.operator


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


class CallReview(models.Model):
    """The controller's assessment of a call — one row of the daily Excel report."""

    class Result(models.TextChoices):
        OK = 'ок', 'ок'
        ERROR = 'ошибка', 'ошибка'
        REJECT = 'брак', 'брак'

    audio = models.OneToOneField(AudioFile, on_delete=models.CASCADE, related_name='review')
    review_date = models.DateField('дата контроля', default=timezone.localdate)
    interviewer = models.CharField('интервьюер', max_length=120, blank=True)
    respondent_id = models.CharField('ID респондента', max_length=64, blank=True)
    city = models.CharField('город проживания', max_length=100, blank=True)
    listen_city = models.CharField('город слушания', max_length=100, blank=True)
    stations = models.CharField('радиостанции / не слушал', max_length=255, blank=True)
    has_errors = models.BooleanField('ошибки', default=False)
    error_comment = models.TextField('комментарий к ошибкам', blank=True)
    result = models.CharField('результат', max_length=16, choices=Result, blank=True)
    note = models.TextField('примечание', blank=True)
    fixed = models.CharField('исправлено', max_length=100, blank=True)
    controller = models.CharField('контроль', max_length=16, blank=True)
    completed = models.BooleanField('проверено', default=False)
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'контроль звонка'
        verbose_name_plural = 'контроль звонков'

    def __str__(self):
        return f'{self.audio} — {self.result or "не проверен"}'


class CallAnalysis(models.Model):
    """Fields suggested by an LLM from the transcript, for the controller to check."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'В очереди'
        DONE = 'done', 'Готово'
        ERROR = 'error', 'Ошибка'

    audio = models.OneToOneField(AudioFile, on_delete=models.CASCADE, related_name='analysis')
    status = models.CharField('статус', max_length=16, choices=Status, default=Status.PENDING)
    city = models.CharField('город проживания', max_length=100, blank=True)
    listen_city = models.CharField('город слушания', max_length=100, blank=True)
    stations = models.CharField('радиостанции / не слушал', max_length=255, blank=True)
    notes = models.TextField('пояснение', blank=True)
    model_name = models.CharField('модель', max_length=64, blank=True)
    input_tokens = models.PositiveIntegerField('токенов на входе', default=0)
    output_tokens = models.PositiveIntegerField('токенов на выходе', default=0)
    error = models.TextField('ошибка', blank=True)
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'анализ ИИ'
        verbose_name_plural = 'анализ ИИ'

    def __str__(self):
        return f'{self.audio} — {self.get_status_display()}'

    FIELDS = ('city', 'listen_city', 'stations')


CITIES = ['Минск', 'Брест', 'Витебск', 'Гомель', 'Гродно', 'Могилев']


class RadioStation(models.Model):
    """Station directory: used in the Gemini prompt and as suggestions in the review form."""

    name = models.CharField('название', max_length=120, unique=True)
    report_name = models.CharField(
        'в отчёте', max_length=120, blank=True,
        help_text='Как писать в колонку «Радиостанции» отчёта. Пусто — как название.',
    )
    aliases = models.TextField(
        'другие названия', blank=True,
        help_text='Через «;»: английское, прежнее, как говорят респонденты.',
    )
    frequencies = models.JSONField('частоты', default=dict, blank=True)  # {'Минск': '107.9', ...}
    note = models.TextField('примечание', blank=True)
    active = models.BooleanField('использовать', default=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'радиостанция'
        verbose_name_plural = 'радиостанции'

    def __str__(self):
        return self.name

    @property
    def answer_name(self) -> str:
        return self.report_name or self.name

    def alias_list(self) -> list[str]:
        return [a.strip() for a in self.aliases.split(';') if a.strip()]
