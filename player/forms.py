import re
from pathlib import Path

from django import forms

from .models import CITIES, AppSettings, AudioFile, CallReview, Interviewer, RadioStation, parse_call_name


class AudioUploadForm(forms.ModelForm):
    """Manual upload. Call details come from a PBX-style file name; for other
    names the call date must be entered, or the call would appear in no day
    and no report."""

    class Meta:
        model = AudioFile
        fields = ['file', 'call_started_at', 'operator', 'phone', 'title']
        widgets = {
            'file': forms.ClearableFileInput(attrs={'accept': '.wav,audio/wav,audio/x-wav'}),
            'call_started_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
            'operator': forms.TextInput(attrs={'placeholder': 'например, 308', 'inputmode': 'numeric'}),
            'phone': forms.TextInput(attrs={'placeholder': '375291234567', 'inputmode': 'numeric'}),
            'title': forms.TextInput(attrs={'placeholder': 'Необязательно — возьмём из имени файла'}),
        }
        labels = {'operator': 'внутр. номер интервьюера'}

    def clean_file(self):
        f = self.cleaned_data['file']
        if Path(f.name).suffix.lower() != '.wav':
            raise forms.ValidationError('Поддерживаются только файлы .wav')
        return f

    def clean_phone(self):
        return re.sub(r'\D', '', self.cleaned_data['phone'])

    def clean(self):
        cleaned = super().clean()
        f = cleaned.get('file')
        if f and not parse_call_name(f.name) and not cleaned.get('call_started_at'):
            self.add_error(
                'call_started_at',
                'Имя файла не в формате АТС — укажите дату и время звонка, '
                'иначе запись не попадёт ни в один день и отчёт.',
            )
        return cleaned


_INTERVIEWER_LINE_RE = re.compile(r'^\s*(\d+)\s+(.+?)\s*$')


class SettingsForm(forms.ModelForm):
    pbx_password = forms.CharField(
        label='пароль АТС', required=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
        help_text='Хранится в Диспетчере учётных данных Windows (на Mac — в Связке ключей). '
                  'Оставьте пустым, чтобы не менять.',
    )
    interviewers = forms.CharField(
        label='интервьюеры', required=False,
        widget=forms.Textarea(attrs={'rows': 8, 'placeholder': '301 Виолетта\n303 Инна\n308 Марина'}),
        help_text='По одному на строку: внутренний номер и имя. У номера может быть несколько имён (смены). '
                  'Из АТС скачиваются звонки только этих номеров; если список пуст — всех.',
    )

    class Meta:
        model = AppSettings
        fields = ['pbx_url', 'pbx_username', 'pbx_password', 'pbx_verify_ssl', 'controller', 'min_duration']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['interviewers'].initial = '\n'.join(
            f'{i.extension} {i.name}' for i in Interviewer.objects.all()
        )

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


class StationImportForm(forms.Form):
    file = forms.FileField(label='Файл .xlsx', widget=forms.ClearableFileInput(attrs={'accept': '.xlsx'}))

    def clean_file(self):
        f = self.cleaned_data['file']
        if Path(f.name).suffix.lower() != '.xlsx':
            raise forms.ValidationError('Нужен файл .xlsx')
        return f
