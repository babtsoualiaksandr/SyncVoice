"""Suggest report fields from a call transcript with Gemini.

Only the transcript text goes to Google — no audio, phone number or
interviewer. The result is a suggestion: the controller checks it while
listening, the review form stays editable.
"""
import hashlib
import logging
import time as time_module
from datetime import datetime, time, timedelta, timezone as dt_timezone
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from pydantic import BaseModel, Field

from . import stations as station_directory
from .models import AppSettings, AudioFile, CallAnalysis, GeminiPace, GeminiQuota

logger = logging.getLogger(__name__)

TIMEOUT_MS = 60_000

QUESTIONNAIRE = """\
Интервьюер идёт по анкете:
1. Как к вам обращаться (имя).
2. В каком населённом пункте постоянно проживаете: Брест, Витебск, Гомель, Гродно, Минск, Могилев. \
Другой населённый пункт или отказ — интервью завершается.
3. Возраст (0–14 и 65+ — интервью завершается). 4. Пол — интервьюер отмечает сам, не спрашивает.
5. Слушали ли радио хотя бы раз за последние 7 дней? Да — к вопросу 7, нет — к вопросу 6.
6. Слушали ли хотя бы раз за последние 30 дней? Да или нет — к вопросу 13.
7. Интервьюер зачитывает список радиостанций, которые звучат в городе респондента; \
респондент называет те, что слушал ЗА ПОСЛЕДНИЕ 7 ДНЕЙ.
8. «Списки больше зачитывать не будем. Вы слушали радио вчера?» Не слушал — к вопросу 13.
9–12. Вчерашний день по периодам: утром, днём, вечером, ночью. По каждому периоду, когда слушал: \
место (9.1), устройство (9.2), время (9.3), какие именно радиостанции (9.4), слушал ли ещё где-то (9.5).
13–15. Образование, занятость, доля дохода на продукты. 16–17. Можно ли перезвонить, прощание.
"""

SYSTEM_PROMPT = """\
Ты помогаешь контролёру качества колл-центра «МедиаИзмеритель», который проводит телефонный опрос \
о слушании белорусского радио. Тебе дают автоматическую расшифровку звонка. В ней много ошибок \
распознавания: искажённые слова и названия радиостанций, реплики интервьюера и респондента \
не разделены.

{questionnaire}

Извлеки ответы респондента (не интервьюера):

city — ответ на вопрос 2 в обычном написании: Минск, Брест, Витебск, Гомель, Гродно или Могилев. \
Распознавание искажает названия: «Митке», «Нинск», «Минт», «Минус», «Мед» — это Минск, «Омель» — Гомель, \
«Витебский» — Витебск. Если ответ похож на город из списка — запиши этот город. Другой населённый \
пункт пиши, только если он назван ясно (например «Бобруйск», «Слуцк»), и отметь в notes, что по анкете \
интервью должно было завершиться. Пустая строка, если ответ не прозвучал или неразборчив.

listen_city — в анкете такого вопроса нет. Заполняй, только если респондент сам сказал, что \
вчера слушал радио в другом городе, чем город проживания. Иначе пустая строка.

stations — ровно один вариант по веткам анкеты:
• вопрос 8 «слушал вчера» — радиостанции, которые он слушал ВЧЕРА (ответы на 9.4 по всем \
периодам), через запятую, без повторов. Станции, названные только на вопросе 7 (за 7 дней), \
сюда НЕ входят;
• вопрос 5 «да», вопрос 8 «не слушал» — «Не слушал вчера» или «Не слушала вчера»;
• вопрос 5 «нет», вопрос 6 «да» — «за 30 дней слушал» или «за 30 дней слушала»;
• вопросы 5 и 6 «нет» — «Не слушает».
Род (слушал/слушала) — по полу респондента: по имени и тому, как он говорит о себе.
Пустая строка, если по расшифровке ветку или станции определить нельзя.

Как сопоставлять станции со справочником ниже: по названию, другому названию или по частоте \
(респонденты часто называют частоту, например «сто три и семь»). Сначала ищи среди станций, \
у которых есть частота в городе респондента — именно их список зачитывали на вопросе 7. \
Пиши станцию точно так, как она указана в «кавычках» в начале строки справочника. \
Распознавание искажает названия станций: «я не стар», «унистар» — Радио Юнистар; «автор радио», \
«авторадео» — Авторадио; «рокс» — Радио РОКС. Если искажённое название похоже на станцию из \
справочника — пиши станцию из справочника. Станцию, которой точно нет в справочнике, напиши как \
расслышал и добавь «(Другое)».

notes — одно-два коротких предложения для контролёра по-русски, без названий полей (city, stations) \
и номеров вопросов: на каком ответе основаны поля и что сомнительно (искажённое название города \
или станции, неясный ответ «угу», отступление от анкеты, влияющее на эти поля). Не пиши о возрасте: \
«шесть», «четыре» и т. п. — это обрывки числа при распознавании, а не возраст. Пустая строка, если всё однозначно.

Используй только то, что есть в расшифровке. Не угадывай: при сомнении оставь поле пустым \
и объясни в notes.

Справочник радиостанций (в «кавычках» — как писать в ответе):
{stations}
"""


class SuggestedFields(BaseModel):
    city: str = Field(description='Город проживания')
    listen_city: str = Field(description='Город слушания, если отличается от города проживания')
    stations: str = Field(description='Радиостанции, слушанные вчера, или статус слушания')
    notes: str = Field(description='Пояснение для контролёра')


class AnalysisError(Exception):
    """Permanent failure (bad key, blocked request): mark the analysis as failed."""


class RetryLater(Exception):
    """Temporary failure (rate limit, server/network error): keep it queued."""


class UnknownModel(AnalysisError):
    """No such model (a typo in the settings): the next spare model may still answer."""


class DailyLimit(RetryLater):
    """The model's free daily request limit ran out: another model may still answer."""


RULES_HEADER = ('Дополнительные правила контролёров. Если они расходятся с общими правилами выше — '
                'следуй им:')


def enabled() -> bool:
    return bool(settings.GEMINI_API_KEY)


DEFAULT_MODEL = 'gemini-3.1-flash-lite'


def clean_model(name: str | None) -> str:
    """A model name as typed in .env or the settings: no spaces, quotes, «models/» or a stray dot at the end."""
    name = (name or '').strip().strip('"\'').strip().rstrip('.,;').strip()
    return name.removeprefix('models/')


def resolve_models(ai_model: str = '', ai_compare_model: str = '') -> tuple[dict, dict]:
    """Which model the suggestions and the survey check use, and where it comes from.

    Suggestions: the settings page, else GEMINI_MODEL in .env, else DEFAULT_MODEL.
    Survey check: the settings page, else GEMINI_COMPARE_MODEL in .env, else the suggestions' model.
    """
    if name := clean_model(ai_model):
        main = {'model': name, 'source': 'настройки'}
    elif name := clean_model(settings.GEMINI_MODEL):
        main = {'model': name, 'source': '.env GEMINI_MODEL'}
    else:
        main = {'model': DEFAULT_MODEL, 'source': 'по умолчанию'}
    if name := clean_model(ai_compare_model):
        check = {'model': name, 'source': 'настройки'}
    elif name := clean_model(settings.GEMINI_COMPARE_MODEL):
        check = {'model': name, 'source': '.env GEMINI_COMPARE_MODEL'}
    else:
        check = {'model': main['model'], 'source': 'как для подсказок'}
    return main, check


def saved_models() -> tuple[dict, dict]:
    app = AppSettings.load()
    return resolve_models(app.ai_model, app.ai_compare_model)


def model(override: str | None = None) -> str:
    """Model for the field suggestions: this run's override, else as resolve_models() says."""
    return clean_model(override) or saved_models()[0]['model']


def fallback_models() -> list[str]:
    """Spare models from the settings page, in order."""
    text = AppSettings.load().ai_fallback_models.replace('\n', ',')
    return [name for name in map(clean_model, text.split(',')) if name]


def model_chain(main: str, override: str | None = None) -> list[str]:
    """Models to try in turn: an explicit override alone, else the main model and the spare ones."""
    if override := clean_model(override):
        return [override]
    return list(dict.fromkeys([main, *fallback_models()]))


def quota_reset(now: datetime | None = None) -> datetime:
    """When Gemini's daily limits reset: the next midnight Pacific time."""
    now = now or timezone.now()
    try:
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo('America/Los_Angeles')
    except Exception:  # no time zone data: Pacific standard time is close enough
        pacific = dt_timezone(timedelta(hours=-8))
    local = now.astimezone(pacific)
    return datetime.combine(local.date() + timedelta(days=1), time(0), pacific)


def exhausted_models() -> dict[str, datetime]:
    """Models with a spent daily limit and when they come back."""
    return dict(GeminiQuota.objects.filter(exhausted_until__gt=timezone.now())
                .order_by('exhausted_until').values_list('model', 'exhausted_until'))


def mark_exhausted(name: str) -> datetime:
    until = quota_reset()
    GeminiQuota.objects.update_or_create(model=name, defaults={'exhausted_until': until})
    logger.warning('Gemini %s: daily limit reached, skipped until %s', name, until)
    return until


def generate_any(system: str, contents: str, schema, client, models: list[str]):
    """generate() with the first model that still has its daily limit. Returns (parsed, usage, model).

    A model that answers «daily limit reached» is skipped until the quota resets
    and the next one is asked at once; with all of them spent the call waits (RetryLater).
    A single explicitly chosen model is always asked.
    """
    spent = exhausted_models() if len(models) > 1 else {}
    for name in models:
        if name in spent:
            continue
        try:
            parsed, usage = generate(system, contents, schema, client, model=name)
        except DailyLimit:
            spent[name] = mark_exhausted(name)
            continue
        except UnknownModel as exc:
            if name == models[-1]:
                raise
            logger.warning('Gemini model %s skipped: %s', name, exc)
            continue
        return parsed, usage, name
    back = min(spent.values())
    raise DailyLimit(
        f'Дневной лимит Gemini исчерпан: {", ".join(models)}. '
        f'Продолжу с {timezone.localtime(back):%d.%m %H:%M} или добавьте запасную модель в настройках.'
    )


def with_rules(prompt: str, rules: str) -> str:
    """The built-in prompt plus the controllers' own rules from the settings page."""
    rules = (rules or '').strip()
    return f'{prompt}\n{RULES_HEADER}\n{rules}\n' if rules else prompt


def prompt_version(prompt: str) -> str:
    """Short fingerprint of a system prompt: shows which answers came from which prompt."""
    return hashlib.sha1(prompt.encode()).hexdigest()[:8]


def system_prompt(rules: str | None = None) -> str:
    """The field-suggestion prompt; `rules` None — the controllers' rules saved in the settings."""
    base = SYSTEM_PROMPT.format(stations=station_directory.prompt_block(), questionnaire=QUESTIONNAIRE)
    return with_rules(base, AppSettings.load().ai_rules_analysis if rules is None else rules)


def proxy_url() -> str | None:
    """GEMINI_PROXY_URL with GEMINI_PROXY_USERNAME/PASSWORD inserted (URL-escaped)."""
    url = settings.GEMINI_PROXY_URL.strip()
    if not url:
        return None
    if settings.GEMINI_PROXY_USERNAME:
        parts = urlsplit(url)
        credentials = quote(settings.GEMINI_PROXY_USERNAME, safe='')
        if settings.GEMINI_PROXY_PASSWORD:
            credentials += ':' + quote(settings.GEMINI_PROXY_PASSWORD, safe='')
        host = parts.netloc.rsplit('@', 1)[-1]
        url = urlunsplit(parts._replace(netloc=f'{credentials}@{host}'))
    return url


def make_client():
    from google import genai
    from google.genai import types

    client_args = {'trust_env': False}  # ignore system proxy variables: use ours or none
    if proxy := proxy_url():
        client_args['proxy'] = proxy
    return genai.Client(
        api_key=settings.GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=TIMEOUT_MS, client_args=client_args),
    )


def transcript_text(audio: AudioFile) -> str:
    return '\n'.join(
        f'[{int(s.start) // 60:02}:{int(s.start) % 60:02}] {s.text}' for s in audio.segments.all()
    )


MAX_PACE_WAIT = 30  # seconds; a longer queue to the model — the call waits in SyncVoice's queue instead


# Free-tier requests per minute (AI Studio → Rate limit). Not listed: 10 for «…-lite», 5 for the rest.
FREE_TIER_RPM = {
    'gemini-3.1-flash-lite': 15,
    'gemini-3.5-flash-lite': 15,
    'gemini-2.5-flash-lite': 10,
}
PACE_SHARE = 0.8  # use 80 % of the limit: other tools on the same key, clock drift


def requests_per_minute(name: str) -> float:
    """GEMINI_RPM from .env, else 80 % of the model's free-tier limit (12 for 3.x Flash Lite, 4 for Flash)."""
    value = str(settings.GEMINI_RPM).strip()
    if value:
        return max(float(value), 0)
    limit = FREE_TIER_RPM.get(name) or (10 if 'lite' in name else 5)
    return int(limit * PACE_SHARE)


def wait_for_slot(name: str) -> None:
    """Keep every process (worker, commands, the settings page) under the model's per-minute limit.

    Reserves the model's next free slot in the database and sleeps until it;
    a slot further than MAX_PACE_WAIT away is not taken — RetryLater instead.
    """
    rpm = requests_per_minute(name)
    if not rpm:
        return
    interval = timedelta(seconds=60 / rpm)
    with transaction.atomic():  # SQLite: an immediate transaction — one process at a time
        now = timezone.now()
        pace, _ = GeminiPace.objects.get_or_create(model=name, defaults={'next_slot_at': now})
        slot = max(now, pace.next_slot_at)
        wait = (slot - now).total_seconds()
        if wait > MAX_PACE_WAIT:
            raise RetryLater(f'Очередь запросов к {name}: следующий через {wait:.0f} с '
                             f'(не больше {rpm:g} в минуту).')
        pace.next_slot_at = slot + interval
        pace.save(update_fields=['next_slot_at'])
    if wait > 0:
        logger.info('Gemini %s: waiting %.1f s to stay under %g requests a minute', name, wait, rpm)
        time_module.sleep(wait)


def is_unknown_model(exc) -> bool:
    """400 «unexpected model name format» or 404 «models/… is not found»."""
    message = (exc.message or '').casefold()
    return exc.code == 404 or 'model name' in message or ('model' in message and 'not found' in message)


def is_daily_limit(exc) -> bool:
    """429 because of the per-day quota (quotaId «…PerDay…»), not the per-minute one."""
    return 'perday' in f'{exc.message} {exc.details}'.casefold().replace('_', '').replace(' ', '')


def generate(system: str, contents: str, schema, client=None, model: str | None = None):
    """One structured Gemini request. Returns (parsed schema object, usage).

    Raises RetryLater for temporary problems and AnalysisError for permanent ones.
    """
    from google.genai import errors, types

    client = client or make_client()
    wait_for_slot(model or DEFAULT_MODEL)
    try:
        response = client.models.generate_content(
            model=model or DEFAULT_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type='application/json',
                response_schema=schema,
                temperature=0,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    except errors.ClientError as exc:
        if exc.code == 429:
            if is_daily_limit(exc):
                raise DailyLimit(f'Дневной лимит Gemini для {model or DEFAULT_MODEL} исчерпан: {exc.message}')
            raise RetryLater(f'Превышен лимит запросов Gemini: {exc.message}')
        if is_unknown_model(exc):
            raise UnknownModel(f'Модель Gemini «{model or DEFAULT_MODEL}» не найдена ({exc.code}): {exc.message}')
        raise AnalysisError(f'Gemini отклонил запрос ({exc.code}): {exc.message}')
    except errors.ServerError as exc:
        raise RetryLater(f'Ошибка сервера Gemini ({exc.code}): {exc.message}')
    except errors.APIError as exc:
        raise AnalysisError(f'Ошибка Gemini ({exc.code}): {exc.message}')
    except httpx.ProxyError as exc:  # e.g. 407: wrong proxy login/password
        raise RetryLater(f'Прокси не пропустил запрос к Gemini: {exc}')
    except httpx.HTTPError as exc:  # network errors, timeouts
        raise RetryLater(f'Нет связи с Gemini: {exc}')

    parsed = response.parsed
    if not isinstance(parsed, schema):
        raise AnalysisError(f'Gemini вернул ответ не по схеме: {(response.text or "")[:300]}')
    meta = response.usage_metadata
    usage = {
        'input_tokens': (meta and meta.prompt_token_count) or 0,
        'output_tokens': ((meta and meta.candidates_token_count) or 0) + ((meta and meta.thoughts_token_count) or 0),
    }
    return parsed, usage


def suggest_fields(audio: AudioFile, client=None, rules: str | None = None,
                   model_override: str | None = None) -> tuple[SuggestedFields, dict]:
    """Ask Gemini for the report fields. Returns (fields, usage); usage also names the model and prompt version."""
    system = system_prompt(rules)
    fields, usage, name = generate_any(
        system, f'Расшифровка звонка:\n\n{transcript_text(audio)}', SuggestedFields, client,
        model_chain(model(), model_override),
    )
    return fields, {**usage, 'model': name, 'prompt_version': prompt_version(system)}


def queue(audio: AudioFile) -> None:
    """(Re)queue the analysis of a transcribed call."""
    CallAnalysis.objects.update_or_create(
        audio=audio, defaults={'status': CallAnalysis.Status.PENDING, 'error': ''},
    )


def run_analysis(analysis: CallAnalysis, client=None) -> None:
    """Fill `analysis` from its call's transcript. Raises RetryLater to keep it queued."""
    audio = analysis.audio
    if not audio.segments.exists():
        analysis.status = CallAnalysis.Status.ERROR
        analysis.error = 'Нет расшифровки.'
        analysis.save()
        return
    try:
        fields, usage = suggest_fields(audio, client)
    except AnalysisError as exc:
        analysis.status = CallAnalysis.Status.ERROR
        analysis.error = str(exc)
        analysis.save()
        return
    analysis.city = fields.city.strip()
    analysis.listen_city = fields.listen_city.strip()
    analysis.stations = fields.stations.strip()
    analysis.notes = fields.notes.strip()
    analysis.model_name = usage['model']
    analysis.prompt_version = usage['prompt_version']
    analysis.input_tokens = usage['input_tokens']
    analysis.output_tokens = usage['output_tokens']
    analysis.status = CallAnalysis.Status.DONE
    analysis.error = ''
    analysis.save()
