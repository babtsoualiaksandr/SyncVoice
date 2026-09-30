import subprocess
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from player.transcription import available_engines, make_engine


class Command(BaseCommand):
    help = 'Замерить скорость распознавания каждым установленным движком на этом компьютере.'

    def add_arguments(self, parser):
        parser.add_argument('path', help='WAV-файл для замера (лучше 1–2 минуты)')
        parser.add_argument('--engine', help='Замерить только этот движок (в текущем процессе)')

    def handle(self, path, engine, **options):
        if engine:
            self.measure(engine, path)
            return

        engines = available_engines()
        if not engines:
            raise CommandError('Не установлен ни один движок распознавания.')
        self.stdout.write(f'Модель {settings.WHISPER_MODEL}, текущий движок: {settings.WHISPER_ENGINE}')
        # Each engine in its own process: torch and CTranslate2 in one process crash.
        for name in engines:
            subprocess.run([sys.executable, sys.argv[0], 'asr_benchmark', path, '--engine', name], check=False)
        self.stdout.write('\nБыстрее тот, у кого больше «× реального времени». '
                          'Выбор движка — переменная окружения WHISPER_ENGINE.')

    def measure(self, name, path):
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
