import re
from pathlib import Path

from django import forms
from django.conf import settings as django_settings

from .models import CITIES, AppSettings, AudioFile, CallReview, Interviewer, RadioStation, parse_call_name


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """File field that accepts several files and returns a list."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data]
        return [single(data, initial)]


class AudioUploadForm(forms.Form):
    """Manual upload of one or many WAV files.

    Call details come from PBX-style file names. Files with other names need
    the call date (one for the whole batch), otherwise they would appear in
    no day and no report.
    """

    files = MultipleFileField(
        label='WAV-файлы',
        widget=MultipleFileInput(attrs={'accept': '.wav,audio/wav,audio/x-wav'}),
    )
    call_started_at = forms.DateTimeField(
        label='дата и время звонка', required=False,
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
        help_text='Для файлов, чьё имя не в формате АТС.',
    )
    operator = forms.CharField(
        label='внутр. номер интервьюера', required=False, max_length=16,
        widget=forms.TextInput(attrs={'placeholder': 'например, 308', 'inputmode': 'numeric'}),
        help_text='Для файлов, чьё имя не в формате АТС.',
    )
    phone = forms.CharField(
        label='телефон', required=False, max_length=32,
        widget=forms.TextInput(attrs={'placeholder': '375291234567', 'inputmode': 'numeric'}),
        help_text='Только если такой файл один.',
    )

    def clean_files(self):
        files = self.cleaned_data['files']
        bad = [f.name for f in files if Path(f.name).suffix.lower() != '.wav']
        if bad:
            raise forms.ValidationError(f'Поддерживаются только файлы .wav: {", ".join(bad)}')
        return files

    def clean_phone(self):
        return re.sub(r'\D', '', self.cleaned_data['phone'])

    def other_files(self):
        """Files whose names are not in PBX format."""
        return [f for f in self.cleaned_data.get('files', []) if not parse_call_name(f.name)]

    def clean(self):
        cleaned = super().clean()
        others = self.other_files()
        if others and not cleaned.get('call_started_at'):
            self.add_error(
                'call_started_at',
                'Имя не в формате АТС — укажите дату и время звонка, иначе запись не попадёт '
                f'ни в один день и отчёт: {", ".join(f.name for f in others)}',
            )
        if cleaned.get('phone') and len(others) > 1:
            self.add_error('phone', 'Телефон можно указать, только когда файл не из АТС один.')
        return cleaned


SURVEY_PLACEHOLDERS = {'phone', 'phone_local', 'date', 'date_ru', 'time', 'operator', 'call_id'}

_INTERVIEWER_LINE_RE = re.compile(r'^\s*(\d+)\s+(.+?)\s*$')


class SettingsForm(forms.ModelForm):
    # The PBX speaks plain HTTP: «192.168.3.80» must become http://, not Django's default https://.
    pbx_url = forms.URLField(
        label='адрес АТС', required=False, assume_scheme='http',
        help_text='Например, http://192.168.3.80',
    )
    pbx_password = forms.CharField(
        label='пароль АТС', required=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        help_text='Хранится в Диспетчере учётных данных Windows (на Mac — в Связке ключей). '
                  'Оставьте пустым, чтобы не менять.',
    )
    crm_url = forms.URLField(
        label='адрес CRM', required=False, assume_scheme='http',
        help_text='CRM с анкетами операторов, например http://192.168.12.230. SyncVoice сам находит анкету звонка.',
    )
    crm_password = forms.CharField(
        label='пароль CRM', required=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        help_text='Хранится там же, где пароль АТС. Оставьте пустым, чтобы не менять.',
    )
    interviewers = forms.CharField(
        label='интервьюеры', required=False,
        widget=forms.Textarea(attrs={'rows': 8, 'placeholder': '301 Виолетта\n303 Инна\n308 Марина'}),
        help_text='По одному на строку: внутренний номер и имя. У номера может быть несколько имён (смены). '
                  'Из АТС скачиваются звонки только этих номеров; если список пуст — всех.',
    )

    class Meta:
        model = AppSettings
        fields = ['pbx_url', 'pbx_username', 'pbx_password', 'pbx_verify_ssl', 'controller', 'min_duration',
                  'max_duration', 'crm_url', 'crm_username', 'crm_password', 'survey_url',
                  'ai_model', 'ai_compare_model', 'ai_fallback_models', 'ai_rules_analysis', 'ai_rules_compare']
        widgets = {
            'ai_rules_analysis': forms.Textarea(attrs={'rows': 5}),
            'ai_rules_compare': forms.Textarea(attrs={'rows': 5}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['interviewers'].initial = '\n'.join(
            f'{i.extension} {i.name}' for i in Interviewer.objects.all()
        )
        # Show what an empty model field means right now.
        self.fields['ai_model'].widget.attrs['placeholder'] = django_settings.GEMINI_MODEL
        self.fields['ai_compare_model'].widget.attrs['placeholder'] = (
            django_settings.GEMINI_COMPARE_MODEL or 'как для подсказок'
        )

    def clean_ai_model(self):
        return _model_names(self.cleaned_data['ai_model'], single=True)

    def clean_ai_compare_model(self):
        return _model_names(self.cleaned_data['ai_compare_model'], single=True)

    def clean_ai_fallback_models(self):
        return _model_names(self.cleaned_data['ai_fallback_models'])

    def clean_survey_url(self):
        url = self.cleaned_data['survey_url'].strip()
        if url and not re.match(r'^https?://', url):
            url = 'http://' + url
        unknown = set(re.findall(r'\{(\w+)\}', url)) - SURVEY_PLACEHOLDERS
        if unknown:
            raise forms.ValidationError(f'Неизвестные подстановки: {", ".join(sorted(unknown))}')
        return url

    def clean_interviewers(self):
        pairs = []
        for n, line in enumerate(self.cleaned_data['interviewers'].splitlines(), 1):
            if not line.strip():
                continue
            m = _INTERVIEWER_LINE_RE.match(line)
            if not m:
                raise forms.ValidationError(f'Строка {n}: нужно «номер имя», например «308 Марина».')
            pairs.append((m[1], m[2]))
        return pairs

    def save_interviewers(self):
        wanted = set(self.cleaned_data['interviewers'])
        existing = {(i.extension, i.name): i for i in Interviewer.objects.all()}
        for key, obj in existing.items():
            if key not in wanted:
                obj.delete()
        Interviewer.objects.bulk_create(
            [Interviewer(extension=e, name=n) for e, n in wanted if (e, n) not in existing]
        )


class ReviewForm(forms.ModelForm):
    class Meta:
        model = CallReview
        fields = [
            'review_date', 'interviewer', 'respondent_id', 'city', 'listen_city', 'stations',
            'has_errors', 'error_comment', 'result', 'note', 'fixed', 'controller',
        ]
        widgets = {
            'review_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'city': forms.TextInput(attrs={'list': 'cities'}),
            'listen_city': forms.TextInput(attrs={'list': 'cities'}),
            'stations': forms.TextInput(attrs={'list': 'stations'}),
            'error_comment': forms.Textarea(attrs={'rows': 3}),
            'note': forms.Textarea(attrs={'rows': 2}),
            'result': forms.RadioSelect,
        }


_FREQ_RE = re.compile(r'^\d{2,3}([.,]\d{1,2})?$')


class StationForm(forms.ModelForm):
    """One row of the station directory; frequencies are one field per city."""

    class Meta:
        model = RadioStation
        fields = ['name', 'report_name', 'aliases', 'note', 'active']
        widgets = {
            'aliases': forms.Textarea(attrs={'rows': 1}),
            'note': forms.Textarea(attrs={'rows': 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        freqs = self.instance.frequencies or {}
        for city in CITIES:
            self.fields[f'freq_{city}'] = forms.CharField(
                label=city, required=False, initial=freqs.get(city, ''),
                widget=forms.TextInput(attrs={'inputmode': 'decimal', 'size': 5}),
            )

    def freq_fields(self):
        return [self[f'freq_{city}'] for city in CITIES]

    def clean(self):
        cleaned = super().clean()
        freqs = {}
        for city in CITIES:
            value = (cleaned.get(f'freq_{city}') or '').strip()
            if not value:
                continue
            if not _FREQ_RE.match(value):
                self.add_error(f'freq_{city}', 'Частота вида 107.9')
                continue
            freqs[city] = value.replace(',', '.')
        cleaned['frequencies'] = freqs
        return cleaned

    def save(self, commit=True):
        self.instance.frequencies = self.cleaned_data['frequencies']
        return super().save(commit)


StationFormSet = forms.modelformset_factory(RadioStation, form=StationForm, extra=1, can_delete=True)


MODEL_NAME = re.compile(r'^[a-z0-9][a-z0-9.\-]*[a-z0-9]$')


def _model_names(value: str, single: bool = False) -> str:
    """Gemini model names, comma-separated; a stray dot or space at the end is dropped."""
    names = [n.strip().strip('.;').strip() for n in re.split(r'[,;\n]', value or '')]
    names = list(dict.fromkeys(n for n in names if n))
    if single and len(names) > 1:
        raise forms.ValidationError('Здесь одна модель; запасные — в поле «Запасные модели».')
    for name in names:
        if not MODEL_NAME.match(name.removeprefix('models/')):
            raise forms.ValidationError(f'«{name}» не похоже на имя модели Gemini, например gemini-3.5-flash-lite.')
    return ', '.join(names)


class ControllerReportForm(forms.Form):
    file = forms.FileField(label='Отчёт контролёра .xlsx', widget=forms.ClearableFileInput(attrs={'accept': '.xlsx'}))

    def clean_file(self):
        f = self.cleaned_data['file']
        if Path(f.name).suffix.lower() != '.xlsx':
            raise forms.ValidationError('Нужен файл .xlsx')
        return f


class StationImportForm(forms.Form):
    file = forms.FileField(label='Файл .xlsx', widget=forms.ClearableFileInput(attrs={'accept': '.xlsx'}))

    def clean_file(self):
        f = self.cleaned_data['file']
        if Path(f.name).suffix.lower() != '.xlsx':
            raise forms.ValidationError('Нужен файл .xlsx')
        return f
