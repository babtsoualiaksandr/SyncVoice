import mimetypes
import re
import socket
import warnings
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

import requests
import urllib3
from django.conf import settings as django_settings
from django.contrib import messages
from django.core.cache import cache
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date as _parse_date
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST

from .audio_utils import wav_duration
from .credentials import CRM_SERVICE, SERVICE as PBX_SERVICE, get_password, set_password, store_name
from .forms import AudioUploadForm, ControllerReportForm, ReviewForm, SettingsForm, StationFormSet, StationImportForm
from . import ai_report, analysis, compare, crm, stats
from . import stations as station_directory
from .models import CITIES as STATION_CITIES, AppSettings, AudioFile, CallAnalysis, CallComparison, CallReview, Interviewer, PbxSync, RadioStation, parse_call_name
from .pbx import PbxError
from .report import build_report, calls_for_day, report_filename
from .subtitle_formats import FORMATS, export_filename
from .sync import make_client

CITIES = ['Минск', 'Брест', 'Витебск', 'Гомель', 'Гродно', 'Могилев']
STATIONS = ['Не слушает', 'Не слушал вчера', 'Не слушала вчера', 'за 30 дней слушал', 'за 30 дней слушала']


def parse_date(value):
    """ISO date or None — also for well-formed but impossible dates like 2026-13-45."""
    try:
        return _parse_date(value or '')
    except ValueError:
        return None


def _days_summary():
    """[{day, total, reviewed, transcribed}] newest first."""
    return (
        AudioFile.objects.exclude(call_started_at=None)
        .annotate(day=TruncDate('call_started_at'))
        .values('day')
        .annotate(
            total=Count('id'),
            reviewed=Count('id', filter=Q(review__completed=True)),
            transcribed=Count('id', filter=Q(status=AudioFile.Status.DONE)),
        )
        .order_by('-day')
    )


MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь',
          'октябрь', 'ноябрь', 'декабрь']


def _pick_day(days: list, params) -> tuple:
    """The day to show: ?day=, else the latest day of ?month=YYYY-MM or ?year=YYYY, else the latest day."""
    known = [d['day'] for d in days]
    if (day := parse_date(params.get('day') or '')) and day in known:
        return day
    month, year = params.get('month', ''), params.get('year', '')
    for d in known:  # newest first
        if (month and f'{d:%Y-%m}' == month) or (not month and year and f'{d:%Y}' == year):
            return d
    return parse_date(params.get('day') or '') or (known[0] if known else None)


def _calendar(days: list, day) -> dict:
    """Year and month choices and the selected month's downloaded days, for the filter above the table."""
    years = sorted({d['day'].year for d in days}, reverse=True)
    months = {}
    for d in days:
        if day and d['day'].year == day.year:
            m = months.setdefault(d['day'].month, {'total': 0, 'reviewed': 0})
            m['total'] += d['total']
            m['reviewed'] += d['reviewed']
    return {
        'years': years,
        'months': [{'value': f'{day.year}-{m:02}', 'label': MONTHS[m - 1], 'selected': m == day.month, **c}
                   for m, c in sorted(months.items(), reverse=True)] if day else [],
        'month_days': [d for d in days if day and (d['day'].year, d['day'].month) == (day.year, day.month)],
    }


AI_FILTERS = [('', 'Все'), ('error', 'ИИ: ошибка'), ('check', 'проверить'), ('advice', 'рекомендация'),
              ('ok', 'ок'), ('none', 'без сверки')]


def _filter_ai(calls: list, key: str) -> list:
    if key == 'none':
        return [a for a in calls if not a.ai_verdict]
    if key:
        return [a for a in calls if a.ai_verdict and a.ai_verdict['key'] == key]
    return calls


def _sort_columns(day, sort, ai=''):
    """Header cells of the day's call table: label, link that sorts by it, arrow of the current sort."""
    keep = f'&ai={ai}' if ai else ''
    base = f"{reverse('player:index')}?day={day:%Y-%m-%d}{keep}&sort=" if day else '?sort='
    columns = []
    for key, label in [('time', 'Время'), (None, 'Телефон'), ('respondent', 'Респондент'),
                       ('interviewer', 'Интервьюер'), ('duration', 'Длит.'), ('status', 'Обработка'),
                       ('city', 'Город (ИИ)'), ('ai', 'ИИ'), ('result', 'Контроль')]:
        if key is None:
            columns.append({'label': label})
            continue
        current = sort.lstrip('-') == key
        descending = sort.startswith('-')
        columns.append({
            'label': label,
            'url': base + (key if current and descending else f'-{key}' if current else key),
            'arrow': ('↓' if descending else '↑') if current else '',
        })
    return columns


def index(request, upload_form=None):
    days = list(_days_summary())
    day = _pick_day(days, request.GET)
    all_calls = stats.label_calls(calls_for_day(day)) if day else []
    ai = request.GET.get('ai', '')
    calls, sort = stats.sort_calls(_filter_ai(all_calls, ai), request.GET.get('sort', 'time'))
    day_stats, day_total = stats.interviewer_stats(all_calls)
    ai_counts = {key: len(_filter_ai(all_calls, key)) for key, _ in AI_FILTERS}
    settings = AppSettings.load()
    return render(request, 'player/index.html', {
        'days': days,
        'day': day,
        'calls': calls,
        'sort': sort,
        'columns': _sort_columns(day, sort, ai),
        'list_query': urlencode(_list_params({'sort': sort, 'ai': ai})),
        'calendar': _calendar(days, day),
        'ai_filter': ai,
        'ai_filters': [{'key': k, 'label': label, 'count': ai_counts[k]} for k, label in AI_FILTERS],
        'day_stats': day_stats,
        'day_total': day_total,
        'other_audio': AudioFile.objects.filter(call_started_at=None),
        'upload_form': upload_form or AudioUploadForm(),
        'report_form': ControllerReportForm(),
        'ai_enabled': analysis.enabled(),
        'sync_day': timezone.localdate() - timedelta(days=1),
        'last_sync': PbxSync.objects.first(),
        'settings': settings,
        'pbx_configured': bool(settings.pbx_url and settings.pbx_username),
    })


@require_POST
def upload(request):
    form = AudioUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return index(request, upload_form=form)
    others = form.other_files()
    created, duplicates = [], []
    seen_call_ids = set()
    for f in form.cleaned_data['files']:
        info = parse_call_name(f.name)
        if info:
            if info['call_id'] in seen_call_ids or AudioFile.objects.filter(call_id=info['call_id']).exists():
                duplicates.append(f.name)
                continue
            seen_call_ids.add(info['call_id'])
            audio = AudioFile(file=f)
        else:
            audio = AudioFile(
                file=f,
                call_started_at=form.cleaned_data['call_started_at'],
                operator=form.cleaned_data['operator'],
                phone=form.cleaned_data['phone'] if len(others) == 1 else '',
            )
        audio.save()  # status is pending: the worker transcribes it
        audio.duration = wav_duration(audio.file.path)
        audio.save(update_fields=['duration'])
        created.append(audio)

    if len(created) == 1 and not duplicates:
        messages.success(request, 'Файл загружен и поставлен в очередь на распознавание.')
        return redirect(created[0])
    if created:
        messages.success(request, f'Загружено файлов: {len(created)}. Они поставлены в очередь на распознавание.')
    if duplicates:
        messages.info(request, f'Уже были загружены, пропущено {len(duplicates)}: {", ".join(duplicates)}')
    if len(duplicates) == 1 and not created:
        info = parse_call_name(duplicates[0])
        return redirect(AudioFile.objects.get(call_id=info['call_id']))
    url = reverse('player:index')
    days = [timezone.localtime(a.call_started_at).date() for a in created if a.call_started_at]
    days += [parse_call_name(name)['call_started_at'].date() for name in duplicates]
    if days:
        url += f'?day={days[0]:%Y-%m-%d}'
    return redirect(url)


@require_POST
def sync_start(request):
    day = parse_date(request.POST.get('day') or '')
    if not day:
        messages.error(request, 'Укажите день.')
        return redirect('player:index')
    if not PbxSync.objects.filter(status__in=[PbxSync.Status.PENDING, PbxSync.Status.RUNNING], day=day).exists():
        PbxSync.objects.create(day=day)
    return redirect(f"{reverse('player:index')}?day={day:%Y-%m-%d}")


@require_GET
def status(request):
    """Worker/sync/transcription progress for the main page (polled)."""
    settings = AppSettings.load()
    sync = PbxSync.objects.first()
    return JsonResponse({
        'worker_alive': settings.worker_alive,
        'sync': sync and {
            'day': sync.day.strftime('%d.%m.%Y'),
            'status': sync.status,
            'status_display': sync.get_status_display(),
            'found': sync.found,
            'downloaded': sync.downloaded,
            'skipped': sync.skipped,
            'failed': sync.failed,
            'error': sync.error,
        },
        'queue': AudioFile.objects.filter(
            status__in=[AudioFile.Status.PENDING, AudioFile.Status.PROCESSING]
        ).count(),
        'ai': _ai_queue(),
    })


def _ai_queue():
    """Gemini jobs still to do and, if they wait (overloaded model, CRM down), why."""
    waiting = [
        model.objects.filter(status=model.Status.PENDING) for model in (CallAnalysis, CallComparison)
    ]
    reason = next((
        item.error for qs in waiting
        for item in qs.exclude(error='').order_by('-updated_at')[:1]
    ), '')
    exhausted = [
        {'model': name, 'until': timezone.localtime(until).strftime('%d.%m %H:%M')}
        for name, until in analysis.exhausted_models().items()
    ]
    return {'analysis': waiting[0].count(), 'compare': waiting[1].count(), 'waiting': reason,
            'exhausted': exhausted, 'now': analysis.current_models() if analysis.enabled() else None}


def _keep_password(form, old_username, user_field, password_field, service):
    """Save the entered password (or move the stored one to a renamed login). Errors go on the form."""
    username = form.cleaned_data[user_field]
    password = form.cleaned_data[password_field]
    if not password and username and username != old_username:
        password = get_password(old_username, service) or ''  # login renamed: keep the saved password
    try:
        if password and username:
            set_password(username, password, service)
    except Exception as exc:  # keyring backends raise various errors
        form.add_error(password_field, f'Не удалось сохранить пароль в {store_name()}: {exc}')


def settings_view(request):
    settings = AppSettings.load()
    old_pbx_user, old_crm_user = settings.pbx_username, settings.crm_username
    form = SettingsForm(request.POST or None, instance=settings)
    if request.method == 'POST' and form.is_valid():
        _keep_password(form, old_pbx_user, 'pbx_username', 'pbx_password', PBX_SERVICE)
        _keep_password(form, old_crm_user, 'crm_username', 'crm_password', CRM_SERVICE)
        if not form.errors:
            form.save()
            form.save_interviewers()
            saved = [name for field, name in (('pbx_password', 'АТС'), ('crm_password', 'CRM'))
                     if form.cleaned_data[field]]
            note = f' Пароль {" и ".join(saved)} сохранён.' if saved else ''
            messages.success(request, 'Настройки сохранены.' + note)
            return redirect('player:settings')
    if form.errors:
        messages.error(request, 'Настройки не сохранены — исправьте отмеченные поля.')
    current = form.instance if form.is_bound else settings
    return render(request, 'player/settings.html', {
        'form': form,
        'password_saved': bool(get_password(current.pbx_username)),
        'password_username': current.pbx_username,
        'crm_password_saved': bool(get_password(current.crm_username, CRM_SERVICE)),
        'crm_username': current.crm_username,
        'password_store': store_name(),
        'survey_example_call': (example := AudioFile.objects.exclude(phone='').first()),
        'survey_example': example.survey_url() if example else None,
        'ai_enabled': analysis.enabled(),
        'compare_enabled': compare.enabled(),
        'exhausted_models': analysis.exhausted_models(),
        'models_in_use': analysis.saved_models(),
        'fallback_in_use': analysis.fallback_models(),
        'builtin_analysis_prompt': analysis.system_prompt(rules=''),
        'builtin_compare_prompt': compare.system_prompt(rules=''),
        'try_calls': AudioFile.objects.filter(status=AudioFile.Status.DONE)
                     .order_by('-call_started_at', '-created_at')[:50],
    })


def stations_view(request):
    """Radio station directory: edit, add, delete (used by the AI prompt and form hints)."""
    formset = StationFormSet(request.POST or None, queryset=RadioStation.objects.all())
    if request.method == 'POST' and formset.is_valid():
        formset.save()
        messages.success(request, 'Радиостанции сохранены.')
        return redirect('player:stations')
    return render(request, 'player/stations.html', {
        'formset': formset,
        'import_form': StationImportForm(),
        'cities': STATION_CITIES,
        'total': RadioStation.objects.count(),
        'active': RadioStation.objects.filter(active=True).count(),
    })


@require_POST
def stations_import(request):
    form = StationImportForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            created, updated = station_directory.import_workbook(form.cleaned_data['file'])
        except Exception as exc:  # malformed workbook: openpyxl raises many kinds
            messages.error(request, f'Не удалось прочитать файл: {exc}')
        else:
            messages.success(request, f'Импорт: добавлено {created}, обновлено {updated}.')
    else:
        for error in form.errors.get('file', []):
            messages.error(request, error)
    return redirect('player:stations')


@require_POST
def test_connection(request):
    try:
        client = make_client()
        client.login()
    except PbxError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)})
    return JsonResponse({'ok': True, 'message': 'Подключение к АТС работает.'})


@require_POST
def ai_try(request):
    """Run the field suggestion or the survey check on one call with the rules typed on the
    settings page (not saved yet). Nothing is stored — a sandbox for tuning the rules."""
    if not analysis.enabled():
        return JsonResponse({'ok': False, 'message': 'Не задан GEMINI_API_KEY в файле .env.'})
    audio = AudioFile.objects.filter(pk=request.POST.get('audio') or 0, status=AudioFile.Status.DONE).first()
    if not audio:
        return JsonResponse({'ok': False, 'message': 'Выберите распознанный звонок.'})
    kind = request.POST.get('kind')
    rules = request.POST.get('rules', '')
    # The models typed on the page (maybe not saved), resolved the same way the worker does.
    main, check = analysis.resolve_models(request.POST.get('main_model', ''), request.POST.get('compare_model', ''))
    model = (main if kind == 'analysis' else check)['model']
    try:
        if kind == 'analysis':
            fields, usage = analysis.suggest_fields(audio, rules=rules, model_override=model)
            result = {'fields': fields.model_dump()}
        elif kind == 'compare':
            if not crm.configured():
                return JsonResponse({'ok': False, 'message': 'Для сверки настройте CRM.'})
            survey, answers = compare.find_answers(audio)
            if not survey:
                return JsonResponse({'ok': False, 'message': 'Анкета этого звонка в CRM не найдена.'})
            comparison, usage = compare.check(audio, answers, rules=rules, model_override=model)
            result = {'summary': comparison.summary, 'review': comparison.review,
                      'discrepancies': compare.clean(comparison)}
        else:
            return JsonResponse({'ok': False, 'message': 'Неизвестная проверка.'}, status=400)
    except (analysis.AnalysisError, analysis.RetryLater, crm.CrmError) as exc:
        return JsonResponse({'ok': False, 'message': str(exc)})
    return JsonResponse({'ok': True, **result, 'model': usage['model'], 'prompt_version': usage['prompt_version'],
                         'tokens': usage['input_tokens'] + usage['output_tokens']})


@require_POST
def test_crm(request):
    try:
        client = crm.get_client()
        client.login()
    except crm.CrmError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)})
    return JsonResponse({'ok': True, 'message': 'Вход в CRM работает.'})


SORT_LABELS = {
    'time': 'по времени', 'respondent': 'по респонденту', 'interviewer': 'по интервьюеру',
    'duration': 'по длительности', 'status': 'по обработке', 'city': 'по городу ИИ', 'ai': 'по выводу ИИ',
    'result': 'по контролю',
}
AI_FILTER_LABELS = {'error': 'ИИ: ошибка', 'check': 'ИИ: проверить', 'advice': 'ИИ: рекомендация',
                    'ok': 'ИИ: ок', 'none': 'без сверки'}


def _list_params(params) -> dict:
    """The call table's sort and AI filter, carried from the list to a call page and back."""
    out = {}
    sort = params.get('sort', '')
    if sort and sort.lstrip('-') in stats.SORT_KEYS and sort != 'time':
        out['sort'] = sort
    if params.get('ai', '') in AI_FILTER_LABELS:
        out['ai'] = params['ai']
    return out


def _with_query(url: str, query: str) -> str:
    return f'{url}?{query}' if query else url


def _day_neighbours(audio, params=None) -> dict:
    """Previous / next call and the next one not yet reviewed, in the order of the call table
    the controller came from (its sort and AI filter); without them — the whole day by time."""
    nav = {'prev_call': None, 'next_call': None, 'next_unreviewed': None, 'query': '', 'label': ''}
    if not audio.call_started_at:
        return nav
    context = _list_params(params or {})
    calls = stats.label_calls(calls_for_day(timezone.localtime(audio.call_started_at).date()))
    listed, _ = stats.sort_calls(_filter_ai(calls, context.get('ai', '')), context.get('sort', 'time'))
    if not any(a.pk == audio.pk for a in listed):  # e.g. its AI verdict changed meanwhile
        context, listed = {}, calls
    i = next(n for n, a in enumerate(listed) if a.pk == audio.pk)
    ordered = listed[i + 1:] + listed[:i]
    sort = context.get('sort', 'time')
    label = [f'{i + 1} из {len(listed)}']
    if 'ai' in context:
        label.append(AI_FILTER_LABELS[context['ai']])
    if 'sort' in context:
        label.append(SORT_LABELS[sort.lstrip('-')] + (' ↓' if sort.startswith('-') else ''))
    return {
        'prev_call': listed[i - 1] if i > 0 else None,
        'next_call': listed[i + 1] if i + 1 < len(listed) else None,
        'next_unreviewed': next((a for a in ordered if not (hasattr(a, 'review') and a.review.completed)), None),
        'query': urlencode(context),
        'label': ' · '.join(label),
    }


def _review_form(audio, data=None):
    review = CallReview.objects.filter(audio=audio).first()
    initial = None
    if not review:
        initial = {
            'interviewer': audio.default_interviewer(),
            'controller': AppSettings.load().controller,
            'review_date': timezone.localdate(),
        }
        suggestion = _analysis_of(audio)
        if suggestion and suggestion.status == CallAnalysis.Status.DONE:
            initial.update({f: getattr(suggestion, f) for f in CallAnalysis.FIELDS})
    return ReviewForm(data, instance=review or CallReview(audio=audio), initial=initial), review


def _analysis_of(audio):
    return CallAnalysis.objects.filter(audio=audio).first()


def _analysis_json(item):
    if not item:
        return None
    return {
        'status': item.status,
        'status_display': item.get_status_display(),
        'fields': {f: getattr(item, f) for f in CallAnalysis.FIELDS},
        'notes': item.notes,
        'error': item.error,
    }


def detail(request, pk):
    audio = get_object_or_404(AudioFile, pk=pk)
    form, review = _review_form(audio)
    nav = _day_neighbours(audio, request.GET)
    used_stations = CallReview.objects.exclude(stations='').values_list('stations', flat=True).distinct()
    return render(request, 'player/detail.html', {
        'audio': audio,
        'review_form': form,
        'review': review,
        'interviewer_choices': _interviewer_choices(audio),
        'cities': CITIES,
        'stations': [s for s in dict.fromkeys([*STATIONS, *station_directory.answer_names(), *used_stations]) if s],
        'ai_enabled': analysis.enabled(),
        'survey_url': audio.survey_url(),
        'compare_enabled': compare.enabled(),
        'comparison': _comparison_json(CallComparison.objects.filter(audio=audio).first()),
        'crm_lookup_url': reverse('player:crm_lookup', args=[audio.pk]) if crm.configured() and audio.phone else '',
        'ai': _analysis_json(_analysis_of(audio)),
        'prev_call': nav['prev_call'],
        'next_call': nav['next_call'],
        'prev_url': _with_query(nav['prev_call'].get_absolute_url(), nav['query']) if nav['prev_call'] else '',
        'next_url': _with_query(nav['next_call'].get_absolute_url(), nav['query']) if nav['next_call'] else '',
        'next_unreviewed': nav['next_unreviewed'],
        'next_unreviewed_url': (_with_query(nav['next_unreviewed'].get_absolute_url(), nav['query'])
                                if nav['next_unreviewed'] else ''),
        'list_query': nav['query'],
        'list_label': nav['label'],
        'save_url': _with_query(reverse('player:review_save', args=[audio.pk]), nav['query']),
    })


def analysis_status(request, pk):
    """GET: AI suggestion for the call (polled). POST: (re)run the analysis."""
    audio = get_object_or_404(AudioFile, pk=pk)
    if request.method == 'POST':
        if not analysis.enabled():
            return JsonResponse({'ok': False, 'message': 'Не задан GEMINI_API_KEY в файле .env'}, status=400)
        if audio.status != AudioFile.Status.DONE:
            return JsonResponse({'ok': False, 'message': 'Сначала дождитесь расшифровки.'}, status=400)
        analysis.queue(audio)
    return JsonResponse({'ok': True, 'analysis': _analysis_json(_analysis_of(audio))})


def _interviewer_choices(audio):
    return [str(i) for i in Interviewer.objects.filter(extension=audio.operator)]


@require_POST
def review_save(request, pk):
    """Autosave of the review form. With `complete=1` marks the call as reviewed."""
    audio = get_object_or_404(AudioFile, pk=pk)
    form, _ = _review_form(audio, request.POST)
    if not form.is_valid():
        return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
    review = form.save(commit=False)
    if request.POST.get('complete') == '1':
        review.completed = True
    review.save()
    nav = _day_neighbours(audio, request.GET)  # the call table's order, from the form's action URL
    next_unreviewed = nav['next_unreviewed']
    return JsonResponse({
        'ok': True,
        'completed': review.completed,
        'next_url': _with_query(next_unreviewed.get_absolute_url(), nav['query']) if next_unreviewed else None,
        'next_survey_url': next_unreviewed.survey_url() if next_unreviewed else None,
    })


@require_POST
def report_with_ai(request):
    """The controller's hand-made report back, with the AI's answers added next to theirs."""
    form = ControllerReportForm(request.POST, request.FILES)
    if not form.is_valid():
        for error in form.errors.get('file', []):
            messages.error(request, error)
        return redirect('player:index')
    upload = form.cleaned_data['file']
    try:
        content, _ = ai_report.build(upload)
    except Exception as exc:  # malformed workbook: openpyxl raises many kinds
        messages.error(request, f'Не удалось прочитать отчёт: {exc}')
        return redirect('player:index')
    response = HttpResponse(
        content, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = content_disposition_header(
        as_attachment=True, filename=ai_report.output_filename(upload.name),
    )
    return response


@require_GET
def report(request, day):
    day = parse_date(day)
    if not day:
        raise Http404
    response = HttpResponse(
        build_report(day),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = content_disposition_header(
        as_attachment=True, filename=report_filename(day),
    )
    return response


@require_GET
def subtitles(request, pk):
    """Transcription status and subtitles; the player polls this until done."""
    audio = get_object_or_404(AudioFile, pk=pk)
    return JsonResponse({
        'status': audio.status,
        'status_display': audio.get_status_display(),
        'error': audio.error,
        'language': audio.language,
        'segments': list(audio.segments.values('index', 'start', 'end', 'text')),
    })


@require_GET
def download_subtitles(request, pk, fmt):
    audio = get_object_or_404(AudioFile, pk=pk)
    if fmt not in FORMATS:
        raise Http404('Неизвестный формат субтитров')
    content_type, render_subtitles = FORMATS[fmt]
    body = render_subtitles(audio, list(audio.segments.all()))
    response = HttpResponse(body, content_type=f'{content_type}; charset=utf-8')
    response['Content-Disposition'] = content_disposition_header(
        as_attachment=True, filename=export_filename(audio, fmt),
    )
    return response


@require_POST
def retranscribe(request, pk):
    audio = get_object_or_404(AudioFile, pk=pk)
    if audio.status != AudioFile.Status.PROCESSING:
        audio.status = AudioFile.Status.PENDING
        audio.save(update_fields=['status'])
    return redirect(audio)


@require_POST
def delete(request, pk):
    audio = get_object_or_404(AudioFile, pk=pk)
    day = audio.call_started_at and timezone.localtime(audio.call_started_at).date()
    audio.file.delete(save=False)
    audio.delete()
    url = reverse('player:index')
    return redirect(f'{url}?day={day:%Y-%m-%d}' if day else url)


_RANGE_RE = re.compile(r'bytes=(\d*)-(\d*)$')
_MAX_RANGE_CHUNK = 8 * 1024 * 1024


@require_GET
def stream(request, pk):
    """Serve the audio file with HTTP Range support.

    Browsers need Range (206 Partial Content) to seek in <audio>; without it
    setting currentTime jumps back to 0.
    """
    audio = get_object_or_404(AudioFile, pk=pk)
    path = audio.file.path
    size = audio.file.size
    content_type = mimetypes.guess_type(path)[0] or 'audio/wav'

    match = _RANGE_RE.match(request.headers.get('Range', '').strip())
    if not match or match.groups() == ('', ''):
        response = FileResponse(open(path, 'rb'), content_type=content_type)
        response['Accept-Ranges'] = 'bytes'
        return response

    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:  # suffix range: last N bytes
        start = max(size - int(last), 0)
        end = size - 1
    if start > end or start >= size:
        response = HttpResponse(status=416)
        response['Content-Range'] = f'bytes */{size}'
        return response
    # Don't load huge ranges (e.g. "bytes=0-") into memory; the browser
    # requests the rest itself.
    end = min(end, start + _MAX_RANGE_CHUNK - 1)

    length = end - start + 1
    with open(path, 'rb') as f:
        f.seek(start)
        data = f.read(length)
    response = HttpResponse(data, status=206, content_type=content_type)
    response['Content-Range'] = f'bytes {start}-{end}/{size}'
    response['Content-Length'] = str(length)
    response['Accept-Ranges'] = 'bytes'
    return response


def frame_policy(url: str) -> tuple[bool | None, str]:
    """Can `url` be shown in an <iframe> on SyncVoice's page?

    Looks at X-Frame-Options and CSP frame-ancestors of the reply (fetched
    without the user's CRM session, so usually the login page — CRMs send
    the same headers there). (None, reason) when the CRM can't be reached.
    """
    try:
        with warnings.catch_warnings(), requests.Session() as session:
            warnings.simplefilter('ignore', urllib3.exceptions.InsecureRequestWarning)  # self-signed LAN CRM
            session.trust_env = False  # straight to the CRM, not through system proxy variables
            response = session.get(url, timeout=5, stream=True, verify=False, allow_redirects=True)
        response.close()
    except requests.exceptions.RequestException as exc:
        return None, f'CRM недоступна с этого компьютера: {exc.__class__.__name__}'
    xfo = response.headers.get('X-Frame-Options', '').strip().lower()
    if xfo:
        return False, f'CRM запрещает показ внутри других страниц (X-Frame-Options: {xfo.upper()})'
    for directive in response.headers.get('Content-Security-Policy', '').split(';'):
        parts = directive.split()
        if parts and parts[0].lower() == 'frame-ancestors':
            sources = [p.strip("'").lower() for p in parts[1:]]
            if '*' in sources or any('127.0.0.1' in s or 'localhost' in s for s in sources):
                break
            return False, f'CRM запрещает показ внутри других страниц (frame-ancestors {" ".join(parts[1:])})'
    return True, ''


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'.lower()


@require_GET
def survey_check(request):
    """Whether the CRM page can be embedded (polled once by the call page)."""
    url = request.GET.get('url', '')
    template = AppSettings.load().survey_url
    if not template or not url or _origin(url) != _origin(template):
        return JsonResponse({'ok': False, 'message': 'Адрес не совпадает с CRM из настроек.'}, status=400)
    key = f'survey-frame:{_origin(url)}'
    result = cache.get(key)
    if result is None:
        result = frame_policy(url)
        cache.set(key, result, 600)
    embeddable, reason = result
    return JsonResponse({'ok': True, 'embeddable': embeddable, 'reason': reason})


def _gateway_running() -> bool:
    try:
        with socket.create_connection(('127.0.0.1', django_settings.CRM_PROXY_PORT), timeout=0.3):
            return True
    except OSError:
        return False


@require_GET
def crm_lookup(request, pk):
    """The CRM surveys of this call's phone, the one entered for this call marked."""
    audio = get_object_or_404(AudioFile, pk=pk)
    if not crm.configured():
        return JsonResponse({'ok': False, 'message': 'CRM не настроена.'}, status=404)
    if not audio.phone:
        return JsonResponse({'ok': False, 'message': 'У звонка нет телефона.'})
    try:
        client = crm.get_client()
        surveys = client.find(audio.phone)
    except crm.CrmError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)})
    call_time = timezone.localtime(audio.call_started_at).replace(tzinfo=None) if audio.call_started_at else None
    chosen = crm.pick_survey(surveys, call_time)
    gateway = f'http://127.0.0.1:{django_settings.CRM_PROXY_PORT}/'
    surveys.sort(key=lambda s: s.time or datetime.min, reverse=True)
    return JsonResponse({
        'ok': True,
        'gateway': _gateway_running(),
        'selected': chosen.id if chosen else None,
        'surveys': [{
            'id': s.id,
            'time': f'{s.time:%d.%m.%Y %H:%M:%S}' if s.time else '',
            'operator_user_id': s.operator_user_id,
            'view_url': gateway + s.path,
            'direct_url': client.url(s.path),
        } for s in surveys],
    })


@require_GET
def crm_open(request, pk):
    """Redirect to this call's survey in the CRM itself (for «Окно рядом»)."""
    audio = get_object_or_404(AudioFile, pk=pk)
    try:
        client = crm.get_client()
        surveys = client.find(audio.phone) if audio.phone else []
    except crm.CrmError as exc:
        return HttpResponse(f'CRM: {exc}', status=502, content_type='text/plain; charset=utf-8')
    call_time = timezone.localtime(audio.call_started_at).replace(tzinfo=None) if audio.call_started_at else None
    chosen = crm.pick_survey(surveys, call_time)
    return redirect(client.url(chosen.path if chosen else 'admin/Reports'))


def stats_view(request):
    """Per-interviewer statistics for a period (default: this month up to today)."""
    today = timezone.localdate()
    start = parse_date(request.GET.get('from') or '') or today.replace(day=1)
    end = parse_date(request.GET.get('to') or '') or today
    if start > end:
        start, end = end, start
    calls = stats.label_calls(
        AudioFile.objects.filter(call_started_at__date__range=(start, end)).select_related('review')
    )
    rows, total = stats.interviewer_stats(calls)
    return render(request, 'player/stats.html', {'start': start, 'end': end, 'rows': rows, 'total': total})


def _comparison_json(item):
    if not item:
        return None
    return {
        'status': item.status,
        'discrepancies': item.discrepancies,
        'summary': item.summary,
        'error': item.error,
        'survey_id': item.survey_id,
    }


def comparison_status(request, pk):
    """GET: the survey check of the call (polled). POST: run it again."""
    audio = get_object_or_404(AudioFile, pk=pk)
    if request.method == 'POST':
        if not compare.enabled():
            return JsonResponse({'ok': False, 'message': 'Нужны настроенные CRM и Gemini.'}, status=400)
        if audio.status != AudioFile.Status.DONE:
            return JsonResponse({'ok': False, 'message': 'Сначала дождитесь расшифровки.'}, status=400)
        compare.queue(audio)
    return JsonResponse({'ok': True, 'comparison': _comparison_json(CallComparison.objects.filter(audio=audio).first())})
