import mimetypes
import re
import wave

from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST

from .forms import AudioUploadForm
from .models import AudioFile
from .subtitle_formats import FORMATS, export_filename
from .transcription import transcribe_in_background


def wav_duration(path) -> float | None:
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, OSError):
        return None  # non-PCM WAV; the browser will report the duration itself


def index(request):
    if request.method == 'POST':
        form = AudioUploadForm(request.POST, request.FILES)
        if form.is_valid():
            audio = form.save()
            audio.duration = wav_duration(audio.file.path)
            audio.save(update_fields=['duration'])
            transcribe_in_background(audio.pk)
            return redirect(audio)
    else:
        form = AudioUploadForm()
    return render(request, 'player/index.html', {
        'form': form,
        'audio_files': AudioFile.objects.all(),
    })


def detail(request, pk):
    audio = get_object_or_404(AudioFile, pk=pk)
    return render(request, 'player/detail.html', {'audio': audio})


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
        transcribe_in_background(audio.pk)
    return redirect(audio)


@require_POST
def delete(request, pk):
    audio = get_object_or_404(AudioFile, pk=pk)
    audio.file.delete(save=False)
    audio.delete()
    return redirect('player:index')


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
