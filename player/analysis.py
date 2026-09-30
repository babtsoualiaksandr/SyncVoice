"""Suggest report fields from a call transcript with Gemini.

Only the transcript text goes to Google — no audio, phone number or
interviewer. The result is a suggestion: the controller checks it while
listening, the review form stays editable.
"""
import logging
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from django.conf import settings
from pydantic import BaseModel, Field

from . import stations as station_directory
from .models import AudioFile, CallAnalysis

logger = logging.getLogger(__name__)

TIMEOUT_MS = 60_000

SYSTEM_PROMPT = """\
Ты помогаешь контролёру качества колл-центра, который проводит телефонный опрос о слушании радио \
в Беларуси. Тебе дают автоматическую расшифровку звонка. В ней много ошибок распознавания: \
искажённые слова и названия радиостанций, реплики интервьюера и респондента не разделены.

Извлеки ответы респондента (не интервьюера):

city — город проживания респондента в обычном написании: Минск, Брест, Витебск, Гомель, Гродно, \
Могилев или другой названный населённый пункт. Пустая строка, если не прозвучал.

listen_city — город, где респондент слушает радио, только если он явно назван и отличается от \
города проживания. Иначе пустая строка.

stations — ровно один вариант:
• респондент слушал радио вчера — радиостанции, которые он слушал вчера, через запятую. \
Сопоставь сказанное со справочником ниже: по названию, другому названию или по частоте \
в городе респондента (респонденты часто называют частоту, например «сто три и семь»). \
Пиши станцию точно так, как она указана в «кавычках» в начале строки справочника. \
Станцию не из справочника напиши как расслышал и добавь «(Другое)»;
• за последние 7 дней слушал, но вчера нет — «Не слушал вчера» или «Не слушала вчера»;
• за 7 дней не слушал, но за последние 30 дней слушал — «за 30 дней слушал» или «за 30 дней слушала»;
• радио не слушает — «Не слушает».
Род (слушал/слушала) определи по имени и речи респондента. \
Пустая строка, если по расшифровке определить нельзя.

notes — одно-два коротких предложения для контролёра: на чём основан ответ и что сомнительно \
(например, неразборчивое название станции). Пустая строка, если всё однозначно.

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


def enabled() -> bool:
    return bool(settings.GEMINI_API_KEY)


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


def suggest_fields(audio: AudioFile, client=None) -> tuple[SuggestedFields, dict]:
    """Ask Gemini for the report fields. Returns (fields, usage)."""
    from google.genai import errors, types

    client = client or make_client()
    try:
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=f'Расшифровка звонка:\n\n{transcript_text(audio)}',
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT.format(stations=station_directory.prompt_block()),
                response_mime_type='application/json',
                response_schema=SuggestedFields,
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

    fields = response.parsed
    if not isinstance(fields, SuggestedFields):
        raise AnalysisError(f'Gemini вернул ответ не по схеме: {(response.text or "")[:300]}')
    meta = response.usage_metadata
    usage = {
        'input_tokens': (meta and meta.prompt_token_count) or 0,
        'output_tokens': ((meta and meta.candidates_token_count) or 0) + ((meta and meta.thoughts_token_count) or 0),
    }
    return fields, usage


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
    analysis.model_name = settings.GEMINI_MODEL
    analysis.input_tokens = usage['input_tokens']
    analysis.output_tokens = usage['output_tokens']
    analysis.status = CallAnalysis.Status.DONE
    analysis.error = ''
    analysis.save()
