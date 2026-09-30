import time

from django.core.management.base import BaseCommand, CommandError

from player import analysis
from player.views import parse_date
from player.models import AudioFile, CallAnalysis

RETRY_DELAYS = (5, 15, 30)  # seconds, for rate limits / overloaded model


class Command(BaseCommand):
    help = 'Заполнить через Gemini город и радиостанции для уже распознанных звонков.'

    def add_arguments(self, parser):
        parser.add_argument('--day', help='Только звонки этого дня, ГГГГ-ММ-ДД')
        parser.add_argument('--force', action='store_true', help='Заново, даже если анализ уже есть')
        parser.add_argument('--now', action='store_true',
                            help='Выполнить сразу здесь, а не поставить в очередь фоновому обработчику')

    def handle(self, day, force, now, **options):
        if not analysis.enabled():
            raise CommandError('Не задан GEMINI_API_KEY в файле .env')
        calls = AudioFile.objects.filter(status=AudioFile.Status.DONE).order_by('call_started_at')
        if day:
            if not (parsed := parse_date(day)):
                raise CommandError('Дата в формате ГГГГ-ММ-ДД')
            calls = calls.filter(call_started_at__date=parsed)
        if not force:
            calls = calls.exclude(analysis__status=CallAnalysis.Status.DONE)

        total_in = total_out = 0
        for audio in calls:
            analysis.queue(audio)
            if not now:
                continue
            item = audio.analysis
            label = f'#{audio.pk} {audio.phone_display or audio}'
            for delay in (*RETRY_DELAYS, None):
                try:
                    analysis.run_analysis(item)
                    break
                except analysis.RetryLater as exc:
                    if delay is None:
                        self.stdout.write(self.style.WARNING(
                            f'{label}: {exc} — оставлен в очереди, его обработает запущенный SyncVoice'))
                        break
                    self.stdout.write(f'{label}: {exc} — повтор через {delay} с')
                    time.sleep(delay)
            item.refresh_from_db()
            if item.status == CallAnalysis.Status.PENDING:
                continue
            if item.status == CallAnalysis.Status.DONE:
                total_in += item.input_tokens
                total_out += item.output_tokens
                self.stdout.write(self.style.SUCCESS(
                    f'{label}: город «{item.city}», слушания «{item.listen_city}», «{item.stations}» '
                    f'({item.input_tokens}+{item.output_tokens} токенов)'
                ))
                if item.notes:
                    self.stdout.write(f'    {item.notes}')
            else:
                self.stdout.write(self.style.ERROR(f'{label}: {item.error}'))

        if now:
            self.stdout.write(f'Всего токенов: {total_in} на входе, {total_out} на выходе.')
        else:
            self.stdout.write(f'В очереди: {calls.count()}. Их обработает запущенный SyncVoice.')
