"""Collect calls for reviewing the Gemini prompts: transcript, operator's survey,
AI answers and the controller's own review side by side, in one Markdown file.

The file holds conversation transcripts (respondents say their names), so it is
written into media/ (not in git) — don't publish it.
"""
import json
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from player import analysis, compare, crm
from player.models import AudioFile, CallReview
from player.views import parse_date


RETRY_DELAYS = (5, 15, 30)  # seconds, for an overloaded model (503) or rate limits (429)


def _retry(fn, log):
    """Call fn(); on a temporary Gemini failure wait and try again, then give up."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return fn()
        except analysis.RetryLater as exc:
            if delay is None:
                raise
            log(f'    {exc} — повтор через {delay} с')
            time.sleep(delay)


def _same(ai: str, controller: str) -> bool:
    """Equal up to case, spaces and the order of comma-separated stations."""
    def norm(value):
        return sorted(p.strip().casefold() for p in (value or '').split(',') if p.strip())
    return norm(ai) == norm(controller)


def _mark(ai: str, controller: str) -> str:
    if not ai and not controller:
        return ''
    return '✓' if _same(ai, controller) else '✗'


class Command(BaseCommand):
    help = ('Выгрузить звонки для разбора промптов: расшифровка, анкета оператора, ответы ИИ '
            'и форма контролёра — в один файл Markdown.')

    def add_arguments(self, parser):
        parser.add_argument('--day', help='Только звонки этого дня, ГГГГ-ММ-ДД')
        parser.add_argument('--ids', help='Номера звонков через запятую (как в адресе /audio/<номер>/)')
        parser.add_argument('--limit', type=int, default=15, help='Сколько звонков (по умолчанию 15)')
        parser.add_argument('--all', action='store_true',
                            help='Брать и звонки, которые контролёр ещё не проверил')
        parser.add_argument('--rerun', action='store_true',
                            help='Заново спросить Gemini с текущими промптами (тратит токены), '
                                 'показать рядом с сохранённым ответом')
        parser.add_argument('--model', help='С --rerun: другая модель Gemini для подсказки и сверки, '
                                            'например gemini-3.8-flash')
        parser.add_argument('--no-crm', action='store_true', help='Не запрашивать анкеты в CRM')
        parser.add_argument('--out', help='Куда записать файл (по умолчанию media/prompt_review/)')

    def handle(self, day, ids, limit, all, rerun, model, no_crm, out, **options):
        calls = (
            AudioFile.objects.filter(status=AudioFile.Status.DONE)
            .select_related('review', 'analysis', 'comparison').order_by('call_started_at')
        )
        if ids:
            try:
                calls = calls.filter(pk__in=[int(x) for x in ids.split(',') if x.strip()])
            except ValueError:
                raise CommandError('--ids: номера через запятую, например 12,15,40')
        if day:
            if not (parsed := parse_date(day)):
                raise CommandError('Дата в формате ГГГГ-ММ-ДД')
            calls = calls.filter(call_started_at__date=parsed)
        if not all and not ids:
            calls = calls.filter(review__completed=True)
        calls = list(calls[:limit])
        if not calls:
            raise CommandError('Нет подходящих звонков. Проверенных контролёром нет? Добавьте --all.')
        if rerun and not analysis.enabled():
            raise CommandError('Для --rerun нужен GEMINI_API_KEY в .env')
        if model and not rerun:
            raise CommandError('--model работает только вместе с --rerun')
        self.model = model  # this run only: suggestions and the survey check both use it
        self.versions = {
            'analysis': analysis.prompt_version(analysis.system_prompt()),
            'compare': analysis.prompt_version(compare.system_prompt()),
        }
        use_crm = not no_crm and crm.configured()
        crm_client = crm.get_client() if use_crm else None
        gemini = analysis.make_client() if rerun else None

        lines = [
            f'# Разбор промптов SyncVoice — {timezone.localtime():%d.%m.%Y %H:%M}',
            '',
            f'Модель: {analysis.model(model)}, сверка: {compare.model_name(model)}. '
            f'Версия промпта: подсказка {self.versions["analysis"]}, сверка {self.versions["compare"]}. '
            f'Звонков: {len(calls)}. '
            f'{"Gemini опрошен заново с текущими промптами. " if rerun else ""}'
            f'{"" if use_crm else "Анкеты CRM не запрашивались. "}',
            '',
            'В файле расшифровки разговоров (в них звучат имена) — не публикуйте его.',
            '',
        ]
        totals = {'calls': 0, 'city': 0, 'stations': 0}
        tokens = [0, 0]
        for audio in calls:
            self.stdout.write(f'#{audio.pk} …')
            lines += self._call(audio, crm_client, gemini, totals, tokens)

        lines[4:4] = [
            f'Совпадения ИИ с контролёром (из {totals["calls"]} проверенных): '
            f'город {totals["city"]}, радиостанции {totals["stations"]}.',
            '',
        ]
        if rerun:
            lines[4:4] = [f'Токенов на повторный прогон: {tokens[0]} на входе, {tokens[1]} на выходе.', '']

        path = out or settings.MEDIA_ROOT / 'prompt_review' / f'{day or "calls"}-{datetime.now():%Y%m%d-%H%M}.md'
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'Готово: {path}'))

    def _call(self, audio, crm_client, gemini, totals, tokens):
        when = timezone.localtime(audio.call_started_at) if audio.call_started_at else None
        out = [
            '---', '',
            f'## Звонок #{audio.pk} — {when:%d.%m.%Y %H:%M}' if when else f'## Звонок #{audio.pk}',
            '',
            f'Внутр. номер оператора {audio.operator or "?"}, {audio.duration or 0:.0f} с.',
            '',
        ]

        review = getattr(audio, 'review', None)
        stored = getattr(audio, 'analysis', None)
        out += ['### Форма контролёра и подсказка ИИ', '']
        if review:
            out += [
                f'Проверено: {"да" if review.completed else "нет, черновик"}. '
                f'Результат: {review.get_result_display() or "—"}. Ошибки: {"да" if review.has_errors else "нет"}.',
                '',
            ]
        out += ['| Поле | Контролёр | ИИ (сохранённый) | |', '|---|---|---|---|']
        for field, title in (('city', 'Город'), ('listen_city', 'Город слушания'), ('stations', 'Радиостанции')):
            mine = getattr(review, field, '') if review else ''
            ai = getattr(stored, field, '') if stored and stored.status == 'done' else ''
            out.append(f'| {title} | {mine or "—"} | {ai or "—"} | {_mark(ai, mine) if review else ""} |')
        out.append('')
        if review and review.completed and stored and stored.status == 'done':
            totals['calls'] += 1
            totals['city'] += _same(stored.city, review.city)
            totals['stations'] += _same(stored.stations, review.stations)
        if stored and stored.status == 'done':
            out += [f'Подсказка ИИ сохранена{self._version(stored, "analysis")}.', '']
        if stored and stored.notes:
            out += [f'Заметка ИИ: {stored.notes}', '']
        if stored and stored.status == 'error':
            out += [f'Анализ ИИ не удался: {stored.error}', '']
        if review and review.error_comment:
            out += ['Комментарий контролёра к ошибкам:', '', '> ' + review.error_comment.replace('\n', '\n> '), '']
        if review and review.note:
            out += [f'Примечание контролёра: {review.note}', '']

        if gemini:
            try:
                fields, usage = _retry(lambda: analysis.suggest_fields(audio, gemini, model_override=self.model), self.stdout.write)
                tokens[0] += usage['input_tokens']
                tokens[1] += usage['output_tokens']
                out += [
                    '**ИИ заново (текущий промпт):** '
                    f'город «{fields.city}», слушания «{fields.listen_city}», станции «{fields.stations}»',
                    '',
                ]
                if fields.notes:
                    out += [f'Заметка: {fields.notes}', '']
            except (analysis.AnalysisError, analysis.RetryLater) as exc:
                out += [f'ИИ заново: ошибка — {exc}', '']

        answers = None
        if crm_client and audio.phone:
            try:
                survey, answers = compare.find_answers(audio, crm_client)
                if not survey:
                    out += ['### Анкета оператора', '', 'В CRM не найдена.', '']
            except crm.CrmError as exc:
                out += ['### Анкета оператора', '', f'Ошибка CRM: {exc}', '']
        if answers:
            out += [
                '### Анкета оператора (как её видит Gemini)', '',
                '```json', json.dumps(answers, ensure_ascii=False, indent=1), '```', '',
            ]

        out += ['### Сверка анкеты с разговором', '']
        saved = getattr(audio, 'comparison', None)
        if saved and saved.status == 'done':
            out += self._discrepancies(f'Сохранённая{self._version(saved, "compare")}', saved.summary,
                                       saved.discrepancies)
        elif saved:
            out += [f'Сохранённая: {saved.get_status_display()} {saved.error}'.strip(), '']
        else:
            out += ['Сохранённой сверки нет.', '']
        if gemini and answers:
            try:
                result, usage = _retry(lambda: compare.check(audio, answers, gemini, model_override=self.model),
                                       self.stdout.write)
                tokens[0] += usage['input_tokens']
                tokens[1] += usage['output_tokens']
                out += self._discrepancies('Заново (текущий промпт)', result.summary, compare.clean(result))
                out += ['<details><summary>Разбор модели по полям</summary>', '', result.review, '', '</details>', '']
            except (analysis.AnalysisError, analysis.RetryLater) as exc:
                out += [f'Сверка заново: ошибка — {exc}', '']

        out += ['### Расшифровка', '', '```', analysis.transcript_text(audio), '```', '']
        return out

    def _version(self, item, kind):
        """« (модель, промпт abc123 — текущий)» for a saved AI answer."""
        version = item.prompt_version or '?'
        state = 'текущий' if version == self.versions[kind] else 'старый'
        return f' ({item.model_name or "?"}, промпт {version} — {state})'

    @staticmethod
    def _discrepancies(title, summary, items):
        out = [f'**{title}:** {summary}', '']
        for d in items:
            at = f' [{d["time"]}]' if d.get('time') else ''
            out.append(f'- *{d["severity"]}* **{d["field"]}**{at}: анкета «{d["survey_value"]}», '
                       f'разговор «{d["call_value"]}». {d["comment"]}')
        if items:
            out.append('')
        return out
