import mimetypes
import re
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST

from .audio_utils import wav_duration
from .credentials import set_password
from .forms import AudioUploadForm, ReviewForm, SettingsForm
from .models import AppSettings, AudioFile, CallReview, Interviewer, PbxSync, parse_call_name
from .pbx import PbxError
from .report import build_report, calls_for_day, report_filename
from .subtitle_formats import FORMATS, export_filename
from .sync import make_client

CITIES = ['Минск', 'Брест', 'Витебск', 'Гомель', 'Гродно', 'Могилев']
STATIONS = ['Не слушает', 'Не слушал вчера', 'Не слушала вчера', 'за 30 дней слушал', 'за 30 дней слушала']


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


def index(request):
    days = list(_days_summary())
    day = parse_date(request.GET.get('day') or '') or (days[0]['day'] if days else None)
    calls = calls_for_day(day) if day else AudioFile.objects.none()
    settings = AppSettings.load()
    return render(request, 'player/index.html', {
        'days': days,
        'day': day,
        'calls': calls,
        'other_audio': AudioFile.objects.filter(call_started_at=None),
        'upload_form': AudioUploadForm(),
        'sync_day': timezone.localdate() - timedelta(days=1),
        'last_sync': PbxSync.objects.first(),
        'settings': settings,
        'pbx_configured': bool(settings.pbx_url and settings.pbx_username),
    })


@require_POST
def upload(request):
    form = AudioUploadForm(request.POST, request.FILES)
    if form.is_valid():
        info = parse_call_name(form.cleaned_data['file'].name)
        existing = info and AudioFile.objects.filter(call_id=info['call_id']).first()
        if existing:
            messages.info(request, 'Этот звонок уже загружен.')
            return redirect(existing)
        audio = form.save()
        audio.duration = wav_duration(audio.file.path)
        audio.save(update_fields=['duration'])  # status is pending: the worker transcribes it
        return redirect(audio)
    for error in form.errors.get('file', []):
        messages.error(request, error)
    return redirect('player:index')


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
            'error': sync.error,
        },
        'queue': AudioFile.objects.filter(
            status__in=[AudioFile.Status.PENDING, AudioFile.Status.PROCESSING]
        ).count(),
    })


def settings_view(request):
    settings = AppSettings.load()
    form = SettingsForm(request.POST or None, instance=settings)
    if request.method == 'POST' and form.is_valid():
        try:
            if form.cleaned_data['pbx_password']:
                set_password(form.cleaned_data['pbx_username'], form.cleaned_data['pbx_password'])
        except Exception as exc:
            form.add_error('pbx_password', f'Не удалось сохранить пароль в хранилище ОС: {exc}')
        else:
            form.save()
            form.save_interviewers()
            messages.success(request, 'Настройки сохранены.')
            return redirect('player:settings')
    return render(request, 'player/settings.html', {'form': form})


@require_POST
def test_connection(request):
    try:
        client = make_client()
        client.login()
    except PbxError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)})
    return JsonResponse({'ok': True, 'message': 'Подключение к АТС работает.'})


def _day_neighbours(audio):
    """Previous/next call of the same day and the next call not yet reviewed."""
    if not audio.call_started_at:
        return None, None, None
    calls = list(calls_for_day(timezone.localtime(audio.call_started_at).date()))
    i = next(n for n, a in enumerate(calls) if a.pk == audio.pk)
    prev_call = calls[i - 1] if i > 0 else None
    next_call = calls[i + 1] if i + 1 < len(calls) else None
    ordered = calls[i + 1:] + calls[:i]
    next_unreviewed = next(
        (a for a in ordered if not (hasattr(a, 'review') and a.review.completed)), None
    )
    return prev_call, next_call, next_unreviewed


def _review_form(audio, data=None):
    review = CallReview.objects.filter(audio=audio).first()
    initial = None
    if not review:
        initial = {
            'interviewer': audio.default_interviewer(),
            'controller': AppSettings.load().controller,
            'review_date': timezone.localdate(),
        }
    return ReviewForm(data, instance=review or CallReview(audio=audio), initial=initial), review


def detail(request, pk):
    audio = get_object_or_404(AudioFile, pk=pk)
    form, review = _review_form(audio)
    prev_call, next_call, next_unreviewed = _day_neighbours(audio)
    used_stations = CallReview.objects.exclude(stations='').values_list('stations', flat=True).distinct()
    return render(request, 'player/detail.html', {
        'audio': audio,
        'review_form': form,
        'review': review,
        'interviewer_choices': _interviewer_choices(audio),
        'cities': CITIES,
        'stations': list(dict.fromkeys([*STATIONS, *used_stations])),
        'prev_call': prev_call,
        'next_call': next_call,
        'next_unreviewed': next_unreviewed,
    })


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
    _, _, next_unreviewed = _day_neighbours(audio)
    return JsonResponse({
        'ok': True,
        'completed': review.completed,
        'next_url': next_unreviewed.get_absolute_url() if next_unreviewed else None,
    })


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
