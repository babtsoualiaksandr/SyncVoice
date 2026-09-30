from django.core.management.base import BaseCommand

from player.worker import run_forever


class Command(BaseCommand):
    help = 'Фоновый обработчик: скачивание записей из АТС и распознавание речи.'

    def handle(self, **options):
        self.stdout.write('Обработчик запущен. Остановить — Ctrl+C.')
        try:
            run_forever()
        except KeyboardInterrupt:
            self.stdout.write('Остановлен.')
