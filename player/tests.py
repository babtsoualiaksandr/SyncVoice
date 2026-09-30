import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AudioFile, Segment, parse_call_name

MEDIA_ROOT = tempfile.mkdtemp()
DATA = bytes(range(256)) * 40  # 10240 bytes


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class StreamTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.audio = AudioFile()
        self.audio.file.save('test.wav', ContentFile(DATA))
        self.url = reverse('player:stream', args=[self.audio.pk])

    def get(self, range_header=None):
        headers = {'Range': range_header} if range_header else {}
        return self.client.get(self.url, headers=headers)

    def test_full_file(self):
        r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Accept-Ranges'], 'bytes')
        self.assertEqual(b''.join(r.streaming_content), DATA)

    def test_range(self):
        r = self.get('bytes=100-199')
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r['Content-Range'], f'bytes 100-199/{len(DATA)}')
        self.assertEqual(r.content, DATA[100:200])

    def test_open_range(self):
        r = self.get('bytes=10000-')
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.content, DATA[10000:])

    def test_suffix_range(self):
        r = self.get('bytes=-50')
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.content, DATA[-50:])

    def test_range_past_end_is_clamped(self):
        r = self.get('bytes=10200-99999')
        self.assertEqual(r['Content-Range'], f'bytes 10200-10239/{len(DATA)}')

    def test_unsatisfiable_range(self):
        r = self.get(f'bytes={len(DATA)}-')
        self.assertEqual(r.status_code, 416)
        self.assertEqual(r['Content-Range'], f'bytes */{len(DATA)}')

    def test_malformed_range_returns_full_file(self):
        r = self.get('items=0-10')
        self.assertEqual(r.status_code, 200)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class SubtitlesTests(TestCase):
    def test_segments_in_order(self):
        audio = AudioFile(status=AudioFile.Status.DONE, language='ru')
        audio.file.save('s.wav', ContentFile(b'x'))
        Segment.objects.create(audio=audio, index=1, start=2.0, end=3.5, text='Второй')
        Segment.objects.create(audio=audio, index=0, start=0.0, end=2.0, text='Первый')

        data = self.client.get(reverse('player:subtitles', args=[audio.pk])).json()

        self.assertEqual(data['status'], 'done')
        self.assertEqual([s['text'] for s in data['segments']], ['Первый', 'Второй'])
        self.assertEqual(data['segments'][1], {'index': 1, 'start': 2.0, 'end': 3.5, 'text': 'Второй'})

    def test_title_defaults_to_file_name(self):
        audio = AudioFile()
        audio.file.save('my-call.wav', ContentFile(b'x'))
        self.assertEqual(audio.title, 'my-call')


class CallNameTests(TestCase):
    def test_parse(self):
        info = parse_call_name('out-375290000101-304-20260926-123305-1790415184.21439.wav')
        self.assertEqual(info['phone'], '375290000101')
        self.assertEqual(info['operator'], '304')
        self.assertEqual(info['call_id'], '1790415184.21439')
        started = timezone.localtime(info['call_started_at'])
        self.assertEqual(started.strftime('%Y-%m-%d %H:%M:%S'), '2026-09-26 12:33:05')

    def test_copy_suffix_and_path_are_ignored(self):
        info = parse_call_name('audio/out-375250000102-304-20260926-121304-1790413982.21348 (1).wav')
        self.assertEqual(info['call_id'], '1790413982.21348')

    def test_storage_suffix_is_ignored(self):
        info = parse_call_name('audio/out-375290000101-304-20260926-123305-1790415184_kRlAzSK.21439.wav')
        self.assertEqual(info['phone'], '375290000101')
        self.assertEqual(info['call_id'], '1790415184.21439')

    def test_other_names(self):
        self.assertIsNone(parse_call_name('interview.wav'))
        self.assertIsNone(parse_call_name('out-375-304-20261399-123305-1.2.wav'))  # bad date

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_filled_on_save(self):
        audio = AudioFile()
        audio.file.save('out-375290000101-304-20260926-123305-1790415184.21439.wav', ContentFile(b'x'))
        audio.refresh_from_db()
        self.assertEqual(audio.phone_display, '+375 (29) 000-01-01')
        self.assertEqual(audio.operator, '304')


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class DownloadSubtitlesTests(TestCase):
    def setUp(self):
        self.audio = AudioFile(status=AudioFile.Status.DONE)
        self.audio.file.save('out-375290000101-304-20260926-123305-1790415184.21439.wav', ContentFile(b'x'))
        Segment.objects.create(audio=self.audio, index=0, start=0.0, end=2.5, text='Добрый день!')
        Segment.objects.create(audio=self.audio, index=1, start=3661.25, end=3662.0, text='До свидания.')

    def download(self, fmt):
        return self.client.get(reverse('player:download_subtitles', args=[self.audio.pk, fmt]))

    def test_srt(self):
        r = self.download('srt')
        self.assertEqual(r.content.decode(), (
            '1\n00:00:00,000 --> 00:00:02,500\nДобрый день!\n\n'
            '2\n01:01:01,250 --> 01:01:02,000\nДо свидания.\n'
        ))
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertIn('375290000101_304_2026-09-26_12-33-05.srt', r['Content-Disposition'])

    def test_vtt(self):
        body = self.download('vtt').content.decode()
        self.assertTrue(body.startswith('WEBVTT\n\n00:00:00.000 --> 00:00:02.500\nДобрый день!\n'))

    def test_txt_has_call_details(self):
        body = self.download('txt').content.decode()
        self.assertIn('Телефон: +375 (29) 000-01-01', body)
        self.assertIn('Оператор: 304', body)
        self.assertIn('Дата и время: 26.09.2026 12:33:05', body)
        self.assertIn('[01:01:01] До свидания.', body)

    def test_unknown_format(self):
        self.assertEqual(self.download('docx').status_code, 404)


class UploadTests(TestCase):
    def test_rejects_non_wav(self):
        r = self.client.post(reverse('player:index'), {
            'file': ContentFile(b'x', name='song.mp3'),
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Поддерживаются только файлы .wav')
        self.assertFalse(AudioFile.objects.exists())
