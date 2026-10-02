"""Suggest report fields from a call transcript with Gemini.

Only the transcript text goes to Google — no audio, phone number or
interviewer. The result is a suggestion: the controller checks it while
listening, the review form stays editable.
"""
import hashlib
import logging
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from django.conf import settings
from pydantic import BaseModel, Field

from . import stations as station_directory
from .models import AppSettings, AudioFile, CallAnalysis

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


RULES_HEADER = ('Дополнительные правила контролёров. Если они расходятся с общими правилами выше — '
                'следуй им:')


def enabled() -> bool:
    return bool(settings.GEMINI_API_KEY)


def model(override: str | None = None) -> str:
    """Model for the field suggestions: this run's override, the settings page, then .env."""
    return override or AppSettings.load().ai_model.strip() or settings.GEMINI_MODEL


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


def generate(system: str, contents: str, schema, client=None, model: str | None = None):
    """One structured Gemini request. Returns (parsed schema object, usage).

    Raises RetryLater for temporary problems and AnalysisError for permanent ones.
    """
    from google.genai import errors, types

    client = client or make_client()
    try:
        response = client.models.generate_content(
            model=model or settings.GEMINI_MODEL,
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
            raise RetryLater(f'Превышен лимит запросов Gemini: {exc.message}')
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
    name = model(model_override)
    fields, usage = generate(
        system, f'Расшифровка звонка:\n\n{transcript_text(audio)}', SuggestedFields, client, model=name,
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
