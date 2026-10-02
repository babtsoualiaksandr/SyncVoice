import time

from django.core.management.base import BaseCommand, CommandError

from player import analysis, compare
from player.views import parse_date
from player.models import AudioFile, CallAnalysis, CallComparison

RETRY_DELAYS = (5, 15, 30)  # seconds, for rate limits / overloaded model


class Command(BaseCommand):
    help = ('Заполнить через Gemini город и радиостанции для уже распознанных звонков; '
            'с --compare — ещё и сверить анкету оператора с разговором.')

    def add_arguments(self, parser):
        parser.add_argument('--day', help='Только звонки этого дня, ГГГГ-ММ-ДД')
        parser.add_argument('--force', action='store_true', help='Заново, даже если ответ уже есть')
        parser.add_argument('--compare', action='store_true', dest='with_compare',
                            help='Также сверить анкету оператора из CRM с разговором')
        parser.add_argument('--now', action='store_true',
                            help='Выполнить сразу здесь, а не поставить в очередь фоновому обработчику')

    def handle(self, day, force, with_compare, now, **options):
        if not analysis.enabled():
            raise CommandError('Не задан GEMINI_API_KEY в файле .env')
        if with_compare and not compare.enabled():
            raise CommandError('Для --compare настройте CRM в «Настройках».')
        calls = AudioFile.objects.filter(status=AudioFile.Status.DONE).order_by('call_started_at')
        if day:
            if not (parsed := parse_date(day)):
                raise CommandError('Дата в формате ГГГГ-ММ-ДД')
            calls = calls.filter(call_started_at__date=parsed)

        self.tokens = [0, 0]
        jobs = [(
            'подсказка', analysis.queue, analysis.run_analysis, 'analysis',
            calls if force else calls.exclude(analysis__status=CallAnalysis.Status.DONE),
        )]
        if with_compare:
            jobs.append((
                'сверка', compare.queue, compare.run_comparison, 'comparison',
                calls if force else calls.exclude(comparison__status=CallComparison.Status.DONE),
            ))
        for title, queue, run, attr, todo in jobs:
            todo = list(todo)
            for audio in todo:
                queue(audio)
                if now:
                    self._run(audio, title, run, attr)
            if not now:
                self.stdout.write(f'{title.capitalize()}: в очереди {len(todo)}. Их обработает запущенный SyncVoice.')

        if now:
            self.stdout.write(f'Всего токенов: {self.tokens[0]} на входе, {self.tokens[1]} на выходе.')

    def _run(self, audio, title, run, attr):
        item = getattr(audio, attr)
        label = f'#{audio.pk} {audio.phone_display or audio} — {title}'
        for delay in (*RETRY_DELAYS, None):
            try:
                run(item)
                break
            except analysis.RetryLater as exc:
                if delay is None:
                    self.stdout.write(self.style.WARNING(
                        f'{label}: {exc} — оставлен в очереди, его обработает запущенный SyncVoice'))
                    return
                self.stdout.write(f'{label}: {exc} — повтор через {delay} с')
                time.sleep(delay)
        item.refresh_from_db()
        if item.status == 'pending':
            return
        if item.status != 'done':
            self.stdout.write(self.style.ERROR(f'{label}: {item.error}'))
            return
        self.tokens[0] += item.input_tokens
        self.tokens[1] += item.output_tokens
        if attr == 'analysis':
            self.stdout.write(self.style.SUCCESS(
                f'{label}: город «{item.city}», слушания «{item.listen_city}», «{item.stations}» '
                f'({item.input_tokens}+{item.output_tokens} токенов)'
            ))
            if item.notes:
                self.stdout.write(f'    {item.notes}')
        else:
            errors = sum(d.get('severity') == 'ошибка' for d in item.discrepancies)
            self.stdout.write(self.style.SUCCESS(
                f'{label}: пунктов {len(item.discrepancies)}, из них ошибок {errors} '
                f'({item.input_tokens}+{item.output_tokens} токенов)'
            ))
            if item.summary:
                self.stdout.write(f'    {item.summary}')
