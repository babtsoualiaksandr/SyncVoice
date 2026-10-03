"""Fill in respondents' names for calls already checked against the CRM (CRM only, no Gemini)."""
from django.core.management.base import BaseCommand, CommandError

from player import crm
from player.models import CallComparison
from player.views import parse_date


class Command(BaseCommand):
    help = ('Подтянуть из CRM имена респондентов для уже сверенных звонков — для списка звонков. '
            'Gemini не вызывается, имя в него не уходит.')

    def add_arguments(self, parser):
        parser.add_argument('--day', help='Только звонки этого дня, ГГГГ-ММ-ДД')

    def handle(self, day, **options):
        if not crm.configured():
            raise CommandError('Настройте CRM в «Настройках».')
        items = CallComparison.objects.filter(respondent_name='').exclude(survey_id=None).select_related('audio')
        if day:
            if not (parsed := parse_date(day)):
                raise CommandError('Дата в формате ГГГГ-ММ-ДД')
            items = items.filter(audio__call_started_at__date=parsed)
        items = list(items)
        client = crm.get_client()
        filled = 0
        for item in items:
            try:
                page = client.request('GET', crm.survey_path(item.survey_id))
            except crm.CrmError as exc:
                self.stdout.write(self.style.WARNING(f'#{item.audio_id}: {exc}'))
                continue
            if name := crm.respondent_name(page.text):
                CallComparison.objects.filter(pk=item.pk).update(respondent_name=name[:100])
                filled += 1
        self.stdout.write(self.style.SUCCESS(f'Имена заполнены: {filled} из {len(items)}.'))
