import threading
import webbrowser

from django.core.management.base import BaseCommand
from waitress import serve

from config.wsgi import application
from player.worker import run_forever


class Command(BaseCommand):
    help = 'Запустить SyncVoice: веб-интерфейс, фоновый обработчик и открыть браузер.'

    def add_arguments(self, parser):
        parser.add_argument('--port', type=int, default=8765)
        parser.add_argument('--no-browser', action='store_true')

    def handle(self, port, no_browser, **options):
        url = f'http://127.0.0.1:{port}/'
        threading.Thread(target=run_forever, daemon=True, name='worker').start()
        if not no_browser:
            threading.Timer(1.5, webbrowser.open, args=[url]).start()
        self.stdout.write(f'SyncVoice работает: {url}\nНе закрывайте это окно. Остановить — Ctrl+C.')
        try:
            serve(application, host='127.0.0.1', port=port, threads=8)
        except KeyboardInterrupt:
            pass
