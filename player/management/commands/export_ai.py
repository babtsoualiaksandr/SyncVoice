"""Export what the AI has already said about a day's calls (no new Gemini requests).

One JSON file for comparing the AI with the controller's report: call time and
extension, the AI's field suggestion and survey check, the controller's form in
SyncVoice and the transcript. Instead of the phone number — a short hash of it,
enough to match the report. Written into media/ (not in git): transcripts hold names.
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from player import analysis
from player.models import AudioFile
from player.views import parse_date


def phone_hash(phone: str) -> str:
    return hashlib.sha1(phone.encode()).hexdigest()[:10] if phone else ''


def _analysis(audio):
    item = getattr(audio, 'analysis', None)
    if not item:
        return None
    return {
        'status': item.status, 'error': item.error, 'model': item.model_name, 'prompt_version': item.prompt_version,
        'city': item.city, 'listen_city': item.listen_city, 'stations': item.stations, 'notes': item.notes,
    }


def _comparison(audio):
    item = getattr(audio, 'comparison', None)
    if not item:
        return None
    return {
        'status': item.status, 'error': item.error, 'model': item.model_name, 'prompt_version': item.prompt_version,
        'summary': item.summary, 'discrepancies': item.discrepancies,
    }


def _review(audio):
    item = getattr(audio, 'review', None)
    if not item:
        return None
    return {
        'completed': item.completed, 'city': item.city, 'listen_city': item.listen_city, 'stations': item.stations,
        'has_errors': item.has_errors, 'result': item.result, 'error_comment': item.error_comment, 'note': item.note,
    }


class Command(BaseCommand):
    help = 'Выгрузить сохранённые ответы ИИ за день в JSON — для сравнения с отчётом контролёра.'

    def add_arguments(self, parser):
        parser.add_argument('--day', required=True, help='День звонков, ГГГГ-ММ-ДД')
        parser.add_argument('--no-transcripts', action='store_true', help='Без расшифровок (файл меньше)')
        parser.add_argument('--out', help='Куда записать файл (по умолчанию media/export/)')

    def handle(self, day, no_transcripts, out, **options):
        if not (parsed := parse_date(day)):
            raise CommandError('Дата в формате ГГГГ-ММ-ДД')
        calls = (
            AudioFile.objects.filter(call_started_at__date=parsed)
            .select_related('analysis', 'comparison', 'review').order_by('call_started_at')
        )
        rows = []
        for audio in calls:
            rows.append({
                'id': audio.pk,
                'time': timezone.localtime(audio.call_started_at).strftime('%Y-%m-%d %H:%M:%S'),
                'extension': audio.operator,
                'phone_hash': phone_hash(audio.phone),
                'duration': round(audio.duration or 0),
                'status': audio.status,
                'analysis': _analysis(audio),
                'comparison': _comparison(audio),
                'review': _review(audio),
                **({} if no_transcripts else {'transcript': analysis.transcript_text(audio)}),
            })
        if not rows:
            raise CommandError(f'За {parsed:%d.%m.%Y} звонков в SyncVoice нет.')

        path = Path(out) if out else settings.MEDIA_ROOT / 'export' / f'ai-{parsed}-{datetime.now():%H%M}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'day': str(parsed), 'exported_at': timezone.localtime().strftime('%Y-%m-%d %H:%M'), 'calls': rows,
        }, ensure_ascii=False, indent=1), encoding='utf-8')
        with_ai = sum(1 for r in rows if r['comparison'] and r['comparison']['status'] == 'done')
        self.stdout.write(self.style.SUCCESS(
            f'Звонков: {len(rows)}, со сверкой ИИ: {with_ai}. Файл: {path}'))
