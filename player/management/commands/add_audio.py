from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from player.models import AudioFile, parse_call_name
from player.transcription import transcribe
from player.views import wav_duration


class Command(BaseCommand):
    help = 'Добавить WAV-файлы в плеер и распознать в них речь.'

    def add_arguments(self, parser):
        parser.add_argument('paths', nargs='+', type=Path)
        parser.add_argument('--no-transcribe', action='store_true', help='Только добавить, без распознавания')

    def handle(self, paths, no_transcribe, **options):
        for path in paths:
            if not path.is_file() or path.suffix.lower() != '.wav':
                raise CommandError(f'Не WAV-файл: {path}')

        for path in paths:
            info = parse_call_name(path.name)
            if info and AudioFile.objects.filter(call_id=info['call_id']).exists():
                self.stdout.write(self.style.WARNING(f'Пропускаю {path.name}: звонок {info["call_id"]} уже добавлен'))
                continue

            with path.open('rb') as f:
                audio = AudioFile(title=path.stem)
                audio.file.save(path.name, File(f), save=False)
            audio.duration = wav_duration(audio.file.path)
            audio.save()
            self.stdout.write(f'#{audio.pk} {audio.title}')
            if audio.call_id:
                started = timezone.localtime(audio.call_started_at)
                self.stdout.write(
                    f'  телефон {audio.phone_display}, оператор {audio.operator}, '
                    f'{started:%d.%m.%Y %H:%M:%S}'
                )

            if not no_transcribe:
                self.stdout.write('  распознаю…')
                transcribe(audio)
                audio.refresh_from_db()
                if audio.status == AudioFile.Status.DONE:
                    self.stdout.write(self.style.SUCCESS(
                        f'  готово: {audio.segments.count()} фраз, язык {audio.language}'
                    ))
                else:
                    self.stdout.write(self.style.ERROR(f'  ошибка: {audio.error}'))
