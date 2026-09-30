import re
from pathlib import Path

from django import forms

from .models import AppSettings, AudioFile, CallReview, Interviewer


class AudioUploadForm(forms.ModelForm):
    class Meta:
        model = AudioFile
        fields = ['file', 'title']
        widgets = {
            'file': forms.ClearableFileInput(attrs={'accept': '.wav,audio/wav,audio/x-wav'}),
            'title': forms.TextInput(attrs={'placeholder': 'Необязательно — возьмём из имени файла'}),
        }

    def clean_file(self):
        f = self.cleaned_data['file']
        if Path(f.name).suffix.lower() != '.wav':
            raise forms.ValidationError('Поддерживаются только файлы .wav')
        return f


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
