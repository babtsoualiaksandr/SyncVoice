from pathlib import Path

from django import forms

from .models import AudioFile


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
