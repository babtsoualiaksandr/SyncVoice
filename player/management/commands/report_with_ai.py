"""The controller's hand-made daily report with the AI's saved answers added (see player.ai_report)."""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from player import ai_report


class Command(BaseCommand):
    help = ('Добавить в отчёт контролёра (.xlsx) ответы ИИ рядом с его ответами и лист «Итог ИИ». '
            'Без новых запросов к Gemini.')

    def add_arguments(self, parser):
        parser.add_argument('report', help='Отчёт контролёра, например «Отчет записи 29.09.2026.xlsx»')
        parser.add_argument('--out', help='Куда записать (по умолчанию media/export/)')

    def handle(self, report, out, **options):
        source = Path(report)
        if not source.is_file():
            raise CommandError(f'Нет файла {source}')
        try:
            content, totals = ai_report.build(source)
        except ValueError as exc:
            raise CommandError(str(exc))
        path = Path(out) if out else Path(settings.MEDIA_ROOT) / 'export' / ai_report.output_filename(source.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        for title, value in ai_report.summary_lines(totals):
            if title:
                self.stdout.write(f'{title}: {value}')
        self.stdout.write(self.style.SUCCESS(f'Файл: {path}'))
