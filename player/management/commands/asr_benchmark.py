import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from player.transcription import available_engines, make_engine


class Command(BaseCommand):
    help = 'Замерить скорость распознавания каждым установленным движком на этом компьютере.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='WAV-файл для замера (лучше 1–2 минуты)')

    def handle(self, path, **options):
        engines = available_engines()
        if not engines:
            raise CommandError('Не установлен ни один движок распознавания.')
        self.stdout.write(f'Модель {settings.WHISPER_MODEL}, текущий движок: {settings.WHISPER_ENGINE}')
        for name in engines:
            self.stdout.write(f'\n{name}: загружаю модель…')
            engine = make_engine(name)
            started = time.monotonic()
            segments, _ = engine.transcribe(path, settings.WHISPER_LANGUAGE)
            elapsed = time.monotonic() - started
            audio_seconds = segments[-1][1] if segments else 0
            self.stdout.write(self.style.SUCCESS(
                f'{name}: {elapsed:.0f} с на ~{audio_seconds:.0f} с речи '
                f'({audio_seconds / elapsed:.2f}× реального времени), {len(segments)} фраз'
            ))
        self.stdout.write('\nБыстрее тот, у кого больше «× реального времени». '
                          'Выбор движка — переменная окружения WHISPER_ENGINE.')
