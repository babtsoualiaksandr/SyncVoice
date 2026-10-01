"""Check the operator's survey (CRM) against the call transcript with Gemini.

Sent to Gemini: the questionnaire, the transcript and the survey answers —
without the respondent's name, phone or IDs (see crm.PERSONAL_KEYS).
The result is a note for the controller; nothing is changed automatically.
"""
import json
import logging

from django.conf import settings
from django.utils import timezone
from pydantic import BaseModel, Field

from . import analysis, crm
from .analysis import AnalysisError, RetryLater
from .models import AudioFile, CallComparison

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты помогаешь контролёру качества колл-центра «МедиаИзмеритель». Оператор провёл телефонный опрос \
о слушании радио и внёс ответы респондента в анкету. Сверь анкету с тем, что на самом деле \
сказано в разговоре, и найди расхождения.

{questionnaire}

Варианты ответов анкеты:
- доля дохода на продукты: 75 % и более / 50–75 % / 25–50 % / 25 % и менее / затрудняюсь ответить;
- образование: неполное среднее / среднее (в т. ч. ПТУ) / среднее специальное (техникум, колледж) / \
высшее / затрудняюсь ответить;
- занятость: руководитель / специалист / служащий / рабочий / учащийся / домохозяйка / безработный / \
пенсионер / самозанятый / другое. Респонденты называют должность своими словами — сопоставь с вариантом \
(госслужащий, бухгалтер в госучреждении → служащий; инженер, врач, программист → специалист; \
водитель, продавец, оператор станка → рабочий; директор, начальник → руководитель). Если оператор выбрал \
подходящий вариант — это не расхождение.

Как оценивать, по опыту контролёров:
- возраст считается полными годами: «26 неполных», «скоро 26» — это 25;
- если ответ респондента попадает на границу вариантов («50–55 %») или неоднозначен, оператор должен \
был уточнить; если не уточнил — это ошибка;
- оператор не должен подсказывать ответ («отметьте хотя бы одну станцию») — это рекомендация;
- лишнее уточнение, когда ответ и так однозначен, — рекомендация;
- радиостанции «вчера» и «за 7 дней» сверяй по ответам о слушании; не путай их между собой.

Расшифровка сделана автоматически и содержит ошибки распознавания: искажённые слова и названия, \
реплики не разделены по говорящим, ответы респондента иногда не распознаны вовсе. Не считай расхождением \
то, что объясняется ошибкой распознавания. «Ошибка» — только когда ответ респондента в расшифровке ясно \
слышен и анкета ему противоречит. Если ответа в расшифровке нет или он неразборчив — это «проверить» \
(контролёру нужно прослушать), а не «ошибка» и не «оператор сам проставил ответ».

Для каждого расхождения:
field — что не сходится (например «Возраст», «Доля дохода на продукты», «Радиостанции вчера»);
survey_value — что записано в анкете;
call_value — что сказано в разговоре (коротко, своими словами или цитатой);
time — время в расшифровке, где это слышно, в формате «м:сс» (пусто, если не привязать);
severity — «ошибка» (анкета не соответствует ответу), «проверить» (неясно, нужно прослушать) \
или «рекомендация» (анкета верна, но оператор вёл опрос не по правилам);
comment — одна фраза для контролёра в стиле: «респ. озвучила, что ей 26 неполных лет — следовало \
указать 25, в анкете 26».

summary — одно-два предложения: есть ли расхождения и главное из них. Если всё сходится — так и напиши, \
а список оставь пустым. Не придумывай расхождений и не включай в список пункты, где анкета верна \
и оператор действовал по правилам.
"""


class Discrepancy(BaseModel):
    field: str = Field(description='Что не сходится')
    survey_value: str = Field(description='Что в анкете')
    call_value: str = Field(description='Что сказано в разговоре')
    time: str = Field(description='Время в расшифровке, м:сс, или пусто')
    severity: str = Field(description='ошибка, проверить или рекомендация')
    comment: str = Field(description='Фраза для комментария контролёра')


class Comparison(BaseModel):
    discrepancies: list[Discrepancy]
    summary: str


SEVERITIES = ('ошибка', 'проверить', 'рекомендация')


def enabled() -> bool:
    return analysis.enabled() and crm.configured()


def queue(audio: AudioFile) -> None:
    CallComparison.objects.update_or_create(
        audio=audio, defaults={'status': CallComparison.Status.PENDING, 'error': ''},
    )


def _fail(item: CallComparison, message: str) -> None:
    item.status = CallComparison.Status.ERROR
    item.error = message
    item.save()


def run_comparison(item: CallComparison, gemini_client=None, crm_client=None) -> None:
    """Fill `item`. Raises RetryLater to keep it queued (Gemini or CRM temporarily unavailable)."""
    audio = item.audio
    if not audio.segments.exists():
        return _fail(item, 'Нет расшифровки.')
    if not audio.phone:
        return _fail(item, 'У звонка нет телефона — анкету не найти.')
    try:
        client = crm_client or crm.get_client()
        surveys = client.find(audio.phone)
        call_time = timezone.localtime(audio.call_started_at).replace(tzinfo=None) if audio.call_started_at else None
        survey = crm.pick_survey(surveys, call_time)
        if not survey:
            return _fail(item, 'Анкета этого звонка в CRM не найдена.')
        answers = crm.fetch_answers(client, survey)
    except crm.CrmError as exc:
        raise RetryLater(f'CRM: {exc}')

    contents = (
        f'Анкета, внесённая оператором:\n{json.dumps(answers, ensure_ascii=False, indent=1)}\n\n'
        f'Расшифровка звонка:\n{analysis.transcript_text(audio)}'
    )
    try:
        result, usage = analysis.generate(
            SYSTEM_PROMPT.format(questionnaire=analysis.QUESTIONNAIRE), contents, Comparison, gemini_client,
        )
    except AnalysisError as exc:
        return _fail(item, str(exc))

    item.survey_id = survey.id
    item.discrepancies = [
        {**d.model_dump(), 'severity': d.severity if d.severity in SEVERITIES else 'проверить'}
        for d in result.discrepancies
    ]
    item.summary = result.summary.strip()
    item.model_name = settings.GEMINI_MODEL
    item.input_tokens = usage['input_tokens']
    item.output_tokens = usage['output_tokens']
    item.status = CallComparison.Status.DONE
    item.error = ''
    item.save()
