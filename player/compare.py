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

Как устроена анкета (JSON):
- «Профиль» — город, возраст, пол, образование, занятость, доля дохода на продукты, можно ли перезвонить;
- «День «вчера»» — дата, о которой спрашивали «вчера»;
- «Ответы о слушании»: WeekListening.flag — слушал ли за 7 дней (Yes/No), WeekListening.Stations — \
станции за 7 дней (вопрос 7). SurveyRecords — слушание ВЧЕРА по периодам (06-12, 12-18 и т. д.): время \
с/по, станции (ChannelName), устройство, место, город. Нет SurveyRecords — значит, вчера не слушал; \
это не пропуск. Ответ про 30 дней в анкете не хранится — его не сверяй.

Варианты ответов анкеты:
- доля дохода на продукты: 75 % и более / 50–75 % / 25–50 % / 25 % и менее / затрудняюсь ответить. \
Оператор зачитывает их как «до 25 %, до 50 %, до 75 %, более 75 %», поэтому ответ «до 50 %» — это \
вариант 25–50 %, «до 75 %» — вариант 50–75 %, «до 25 %» — 25 % и менее: это выбор варианта, не граница;
- образование: неполное среднее / среднее (в т. ч. ПТУ) / среднее специальное (техникум, колледж) / \
высшее / затрудняюсь ответить. «Среднее техническое», «техникум», «колледж» → среднее специальное;
- занятость: руководитель / специалист / служащий / рабочий / учащийся / домохозяйка / безработный / \
пенсионер / самозанятый / другое. Респонденты называют должность своими словами — сопоставь с вариантом \
(госслужащий, бухгалтер в госучреждении, педагог → служащий; инженер, врач, программист → специалист; \
водитель, продавец, уборщица, оператор станка → рабочий; директор, начальник → руководитель). \
Работающий пенсионер может быть отмечен и пенсионером, и по своей работе — оба варианта верны.

Расшифровка сделана автоматически, и это главный источник ложных расхождений:
- реплики не разделены по говорящим. Строка, где перечисляются варианты ответа («до 25 %, до 50 %, \
до 75 %…», «неполное среднее, среднее, высшее»), — это оператор зачитывает вопрос, а не ответ респондента. \
Ответ — короткая реплика после вопроса;
- числа часто распознаны частично: «шесть» вместо «шестьдесят», «сейчас один» вместо «пятьдесят один», \
«3005» вместо «тридцать пять» или «пятьдесят пять». Если число из анкеты согласуется с частично \
распознанным (60 и «шесть», 64 и «четыре», 51 и «один»), это не расхождение. Возраст меньше 15 или \
явно невозможный — ошибка распознавания, а не ответ;
- слова и названия искажены («Митке», «Нинск», «Минт» — это Минск; «Омель» — Гомель). Не считай \
расхождением то, что объясняется ошибкой распознавания;
- пол оператор отмечает сам по голосу. По расшифровке его проверить нельзя — пол не сверяй.

Как оценивать, по опыту контролёров:
- если респондент уточнил или поправил ответ, считается последний ответ («до 30 %… до 50, точно» → 25–50 %);
- возраст считается полными годами: «26 неполных», «скоро 26» — это 25;
- ответ на границе вариантов или охватывающий два варианта («75 %», «50 %», «70–80 %», «меньше половины») \
оператор должен был уточнить; если не уточнил — это ошибка. Ответ целиком внутри одного варианта \
(«до 70 %» → 50–75 %, «30 %» → 25–50 %) — не граница, уточнять не нужно;
- оператор не должен подсказывать ответ («отметьте хотя бы одну станцию») — это рекомендация;
- радиостанции «вчера» и «за 7 дней» сверяй по ответам о слушании; не путай их между собой.

Степени:
- «ошибка» — только когда ответ респондента в расшифровке ясно слышен, это именно его ответ (не чтение \
вариантов оператором), и анкета ему противоречит;
- «проверить» — ответа в расшифровке нет, он неразборчив или может быть ошибкой распознавания. \
Отсутствие ответа в расшифровке — всегда «проверить», никогда не «ошибка» и не «оператор сам проставил»;
- «рекомендация» — анкета верна, но оператор вёл опрос не по правилам (подсказывал, не уточнил лишнее).

Сначала заполни review: пройди по каждому полю анкеты одной строкой — какая реплика в расшифровке \
отвечает на этот вопрос, чья она (оператор зачитывает вопрос и варианты или отвечает респондент), \
какому варианту соответствует и совпадает ли с анкетой. Расхождения выбирай только из этого разбора.

Для каждого расхождения:
field — что не сходится (например «Возраст», «Доля дохода на продукты питания», «Радиостанции вчера»);
survey_value — что записано в анкете;
call_value — что сказано в разговоре (коротко, своими словами или цитатой);
time — время в расшифровке, где это слышно, в формате «м:сс» (пусто, если не привязать);
severity — «ошибка», «проверить» или «рекомендация»;
comment — одна фраза для контролёра в стиле: «респ. озвучила, что ей 26 неполных лет — следовало \
указать 25, в анкете 26».

summary — одно-два предложения: есть ли расхождения и главное из них. Если всё сходится — так и напиши, \
а список оставь пустым. Не придумывай расхождений. Не включай пункты, где анкета верна: если survey_value \
и call_value по смыслу совпадают или ответ оператора правильно сопоставлен с вариантом — это не расхождение.
"""


class Discrepancy(BaseModel):
    field: str = Field(description='Что не сходится')
    survey_value: str = Field(description='Что в анкете')
    call_value: str = Field(description='Что сказано в разговоре')
    time: str = Field(description='Время в расшифровке, м:сс, или пусто')
    severity: str = Field(description='ошибка, проверить или рекомендация')
    comment: str = Field(description='Фраза для комментария контролёра')


class Comparison(BaseModel):
    review: str = Field(description='Разбор по полям анкеты: реплика, чья она, вариант, совпадает ли')
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


def find_answers(audio: AudioFile, crm_client=None):
    """The CRM survey of this call and its answers as sent to Gemini (no personal data).

    Returns (None, None) when the CRM has no survey for the call; raises crm.CrmError.
    """
    client = crm_client or crm.get_client()
    surveys = client.find(audio.phone)
    call_time = timezone.localtime(audio.call_started_at).replace(tzinfo=None) if audio.call_started_at else None
    survey = crm.pick_survey(surveys, call_time)
    if not survey:
        return None, None
    return survey, crm.fetch_answers(client, survey)


def check(audio: AudioFile, answers: dict, gemini_client=None) -> tuple[Comparison, dict]:
    """Ask Gemini to compare the survey answers with the transcript. Returns (result, usage)."""
    contents = (
        f'Анкета, внесённая оператором:\n{json.dumps(answers, ensure_ascii=False, indent=1)}\n\n'
        f'Расшифровка звонка:\n{analysis.transcript_text(audio)}'
    )
    return analysis.generate(
        SYSTEM_PROMPT.format(questionnaire=analysis.QUESTIONNAIRE), contents, Comparison, gemini_client,
    )


def _same(a: str, b: str) -> bool:
    """Equal up to case, spaces and punctuation («50-75%» and «50–75 %»)."""
    def norm(value):
        return ''.join(ch for ch in (value or '').casefold() if ch.isalnum())
    return norm(a) == norm(b) != ''


def clean(result: Comparison) -> list[dict]:
    """Normalise severities; drop items where the survey already says what was said."""
    return [
        {**d.model_dump(), 'severity': d.severity if d.severity in SEVERITIES else 'проверить'}
        for d in result.discrepancies
        if not _same(d.survey_value, d.call_value)
    ]


def run_comparison(item: CallComparison, gemini_client=None, crm_client=None) -> None:
    """Fill `item`. Raises RetryLater to keep it queued (Gemini or CRM temporarily unavailable)."""
    audio = item.audio
    if not audio.segments.exists():
        return _fail(item, 'Нет расшифровки.')
    if not audio.phone:
        return _fail(item, 'У звонка нет телефона — анкету не найти.')
    try:
        survey, answers = find_answers(audio, crm_client)
    except crm.CrmError as exc:
        raise RetryLater(f'CRM: {exc}')
    if not survey:
        return _fail(item, 'Анкета этого звонка в CRM не найдена.')
    try:
        result, usage = check(audio, answers, gemini_client)
    except AnalysisError as exc:
        return _fail(item, str(exc))

    item.survey_id = survey.id
    item.discrepancies = clean(result)
    item.summary = result.summary.strip()
    item.model_name = settings.GEMINI_MODEL
    item.input_tokens = usage['input_tokens']
    item.output_tokens = usage['output_tokens']
    item.status = CallComparison.Status.DONE
    item.error = ''
    item.save()
