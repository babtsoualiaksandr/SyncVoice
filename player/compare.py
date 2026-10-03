"""Check the operator's survey (CRM) against the call transcript with Gemini.

Sent to Gemini: the questionnaire, the transcript and the survey answers —
without the respondent's name, phone or IDs (see crm.PERSONAL_KEYS).
The result is a note for the controller; nothing is changed automatically.
"""
import json
import logging
import re

from django.conf import settings
from django.utils import timezone
from pydantic import BaseModel, Field

from . import analysis, crm
from .analysis import AnalysisError, RetryLater
from .models import AppSettings, AudioFile, CallComparison

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
Работающий пенсионер может быть отмечен и пенсионером, и по своей работе — оба варианта верны; \
это не расхождение и не рекомендация, не включай его.

Расшифровка сделана автоматически, и это главный источник ложных расхождений:
- реплики не разделены по говорящим. Строка, где перечисляются варианты ответа («до 25 %, до 50 %, \
до 75 %…», «неполное среднее, среднее, высшее»), — это оператор зачитывает вопрос, а не ответ респондента. \
Ответ — короткая реплика после вопроса;
- числа часто распознаны частично: «шесть» вместо «шестьдесят», «сейчас один» вместо «пятьдесят один», \
«3005» вместо «тридцать пять» или «пятьдесят пять». Если число из анкеты согласуется с частично \
распознанным (60 и «шесть», 64 и «четыре», 51 и «один»), это не расхождение. Возраст меньше 15 или \
явно невозможный — ошибка распознавания, а не ответ;
- слова и названия искажены («Митке», «Нинск», «Минт», «Минус», «Мед» — это Минск; «Омель» — Гомель). Не считай \
расхождением то, что объясняется ошибкой распознавания;
- распознавание теряет куски разговора — это видно по паузам между метками времени (00:43 → 00:57). \
Если вопроса или ответа нет в расшифровке («не спрошено», «не озвучено»), это только «проверить», \
никогда не «ошибка»: контролёр прослушает запись;
- короткие слова путаются: «не рабочий» может быть «ну, рабочий». Если смысл меняется от одной частицы — \
«проверить»;
- пол оператор отмечает сам по голосу. По расшифровке его проверить нельзя — пол не сверяй.

Главное, что ищут контролёры, — ошибки в блоке слушания радио:
- вчерашний день по периодам: время с/по, место (дома, на работе, в автомобиле…), устройство \
(радиоприёмник, автомагнитола, телефон, умная колонка…) и станции. Например, респондент слушал вчера \
с 18 до 19 в машине, а в анкете «на работе» — ошибка; время или станция периода не совпадают — ошибка;
- станции за 7 дней: респондент ясно назвал станцию, а её нет в анкете, или в анкете станция, которую \
он не называл, — ошибка (если названия искажены распознаванием — «проверить»);
- ветка: слушал ли за 7 дней, вчера.
Сверяй этот блок внимательнее всего, по каждому периоду.

Как оценивать, по опыту контролёров:
- если респондент уточнил или поправил ответ, считается последний ответ («до 30 %… до 50, точно» → 25–50 %);
- возраст считается полными годами: «26 неполных», «скоро 26» — это 25;
- «Можно ли перезвонить» сверяй, только если респондент явно отказался, а в анкете «Да»;
- ответ на границе вариантов дохода или охватывающий два варианта («75 %», «50 %», «70–80 %», «половина», \
«меньше половины», «больше половины») оператору лучше было уточнить; если не уточнил, а выбрал соседний \
вариант — это «рекомендация», не «ошибка» (так оценивают контролёры). Ответ целиком внутри одного \
варианта («до 70 %» → 50–75 %, «30 %» → 25–50 %) — не граница, не включай;
- респондент должен сам назвать вариант. Если оператор подсказал конкретный вариант («Рабочий?»), \
а респондент лишь согласился, — ошибка. Общие подсказки без варианта («отметьте хотя бы одну станцию») — \
рекомендация;
- образование сверяй с возрастом: подростку 15–16 лет, который ещё учится в школе, — «неполное среднее», \
даже если он сказал «среднее»; оператор должен был уточнить, сколько классов окончено;
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


def check(audio: AudioFile, answers: dict, gemini_client=None, rules: str | None = None,
          model_override: str | None = None) -> tuple[Comparison, dict]:
    """Ask Gemini to compare the survey answers with the transcript.

    Returns (result, usage); usage also names the model and prompt version.
    """
    contents = (
        f'Анкета, внесённая оператором:\n{json.dumps(answers, ensure_ascii=False, indent=1)}\n\n'
        f'Расшифровка звонка:\n{analysis.transcript_text(audio)}'
    )
    system = system_prompt(rules)
    result, usage, name = analysis.generate_any(
        system, contents, Comparison, gemini_client, analysis.model_chain(model_name(), model_override),
    )
    return result, {**usage, 'model': name, 'prompt_version': analysis.prompt_version(system)}


def _same(a: str, b: str) -> bool:
    """Equal up to case, spaces and punctuation («50-75%» and «50–75 %»)."""
    def norm(value):
        return ''.join(ch for ch in (value or '').casefold() if ch.isalnum())
    return norm(a) == norm(b) != ''


# «разговор» says the answer isn't in the transcript: only the recording can tell, never an «ошибка».
NO_ANSWER = re.compile(
    r'не\s*ответ|нет\s+ответа|не\s*озвуч|не\s*спрош|не\s*зада|неразборчив|не\s*прозвуч|не\s*назва|молчал',
    re.IGNORECASE,
)


def _severity(d: Discrepancy) -> str:
    if d.severity not in SEVERITIES:
        return 'проверить'
    if d.severity == 'ошибка' and NO_ANSWER.search(d.call_value or ''):
        return 'проверить'
    return d.severity


def clean(result: Comparison) -> list[dict]:
    """Normalise severities; drop items where the survey already says what was said."""
    return [
        {**d.model_dump(), 'severity': _severity(d)}
        for d in result.discrepancies
        if not _same(d.survey_value, d.call_value)
    ]


def model_name(override: str | None = None) -> str:
    """Model for the survey check: override, else as analysis.resolve_models() says."""
    return analysis.clean_model(override) or analysis.saved_models()[1]['model']


def system_prompt(rules: str | None = None) -> str:
    """The survey-check prompt; `rules` None — the controllers' rules saved in the settings."""
    base = SYSTEM_PROMPT.format(questionnaire=analysis.QUESTIONNAIRE)
    return analysis.with_rules(base, AppSettings.load().ai_rules_compare if rules is None else rules)


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
    item.survey_id = survey.id
    item.respondent_name = survey.respondent_name
    try:
        result, usage = check(audio, answers, gemini_client)
    except AnalysisError as exc:
        return _fail(item, str(exc))

    item.discrepancies = clean(result)
    item.summary = result.summary.strip()
    item.model_name = usage['model']
    item.prompt_version = usage['prompt_version']
    item.input_tokens = usage['input_tokens']
    item.output_tokens = usage['output_tokens']
    item.status = CallComparison.Status.DONE
    item.error = ''
    item.save()
