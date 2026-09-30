import io
import shutil
import sys
import tempfile
import wave
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import openpyxl
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import report, worker
from .forms import SettingsForm
from .models import AppSettings, AudioFile, CallReview, Interviewer, PbxSync, Segment, parse_call_name
from .pbx import FreePbxClient, PbxError, Recording, parse_cdr_csv
from .sync import run_sync

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

    def test_copy_to_clipboard_uses_txt_export(self):
        r = self.client.get(self.audio.get_absolute_url())
        txt_url = reverse('player:download_subtitles', args=[self.audio.pk, 'txt'])
        self.assertContains(r, f'id="copy-subtitles" data-url="{txt_url}"')


class UploadTests(TestCase):
    def test_rejects_non_wav(self):
        r = self.client.post(reverse('player:upload'), {
            'file': ContentFile(b'x', name='song.mp3'),
        }, follow=True)
        self.assertContains(r, 'Поддерживаются только файлы .wav')
        self.assertFalse(AudioFile.objects.exists())

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_pbx_name_gives_call_details(self):
        r = self.client.post(reverse('player:upload'), {'file': ContentFile(make_wav(), name=CALL_A)})
        audio = AudioFile.objects.get()
        self.assertRedirects(r, audio.get_absolute_url())
        self.assertEqual((audio.phone, audio.operator), ('375290000103', '308'))
        self.assertEqual(timezone.localtime(audio.call_started_at).date(), date(2026, 9, 25))
        self.assertEqual(audio.status, AudioFile.Status.PENDING)  # queued for the worker
        self.assertAlmostEqual(audio.duration, 1.0)

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_other_name_requires_date(self):
        r = self.client.post(reverse('player:upload'), {'file': ContentFile(make_wav(), name='Интервью.wav')})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'укажите дату и время звонка')
        self.assertContains(r, '<details id="upload" open>', html=False)
        self.assertFalse(AudioFile.objects.exists())

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_other_name_with_entered_details(self):
        self.client.post(reverse('player:upload'), {
            'file': ContentFile(make_wav(), name='Интервью.wav'),
            'call_started_at': '2026-09-27T10:15', 'operator': '308', 'phone': '+375 (29) 000-00-99',
        })
        audio = AudioFile.objects.get()
        self.assertEqual(timezone.localtime(audio.call_started_at).strftime('%Y-%m-%d %H:%M'), '2026-09-27 10:15')
        self.assertEqual((audio.operator, audio.phone), ('308', '375290000099'))
        # The call now shows up under its day.
        r = self.client.get(reverse('player:index') + '?day=2026-09-27')
        self.assertContains(r, audio.get_absolute_url())

    @override_settings(MEDIA_ROOT=MEDIA_ROOT)
    def test_same_call_twice_opens_existing(self):
        existing = make_call(CALL_A)
        r = self.client.post(reverse('player:upload'), {'file': ContentFile(make_wav(), name=CALL_A)})
        self.assertRedirects(r, existing.get_absolute_url())
        self.assertEqual(AudioFile.objects.count(), 1)


def make_wav(seconds=1.0, rate=8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b'\x00\x00' * int(seconds * rate))
    return buf.getvalue()


def make_call(name, duration=120.0, **fields):
    audio = AudioFile(duration=duration, **fields)
    audio.file.save(name, ContentFile(b'RIFF'))
    return audio


CALL_A = 'out-375290000103-308-20260925-093215-1790000001.100.wav'
CALL_B = 'out-375290000104-308-20260925-095111-1790000002.101.wav'
CALL_C = 'out-375290000105-306-20260925-091817-1790000003.102.wav'


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class ReportTests(TestCase):
    def setUp(self):
        Interviewer.objects.create(extension='308', name='Марина')
        Interviewer.objects.create(extension='306', name='Елена')
        self.a = make_call(CALL_A, duration=122.4)
        self.b = make_call(CALL_B, duration=171)
        self.c = make_call(CALL_C, duration=123)
        CallReview.objects.create(
            audio=self.a, review_date=date(2026, 9, 26), interviewer='Марина 308',
            city='Брест', stations='Не слушает', result='ок', controller='ИБ', completed=True,
        )
        CallReview.objects.create(
            audio=self.c, review_date=date(2026, 9, 26), interviewer='Елена 306', city='Минск',
            has_errors=True, error_comment='неверно указан возраст', result='ошибка',
            controller='ИБ', fixed='исправ ИБ', completed=True,
        )
        # Another day must not leak into the report.
        make_call('out-375290000000-308-20260926-100000-1790000009.109.wav')

    def load(self):
        r = self.client.get(reverse('player:report', args=['2026-09-25']))
        self.assertEqual(r.status_code, 200)
        self.assertIn('2026', r['Content-Disposition'])
        return openpyxl.load_workbook(io.BytesIO(r.content))['Лист1']

    def test_layout_matches_template(self):
        ws = self.load()
        headers = [ws.cell(row=1, column=i).value for i in range(1, 17)]
        self.assertEqual(headers, [h for h, _ in report.COLUMNS])
        self.assertEqual(ws['D1'].value, 'Дата опроса  Время')  # two spaces, as in the template
        self.assertTrue(ws['B1'].font.b)
        self.assertEqual(ws.freeze_panes, 'D2')
        self.assertEqual(ws.auto_filter.ref, 'A1:O4')
        self.assertAlmostEqual(ws.column_dimensions['K'].width, 29.3)
        self.assertEqual(ws['B2'].number_format, 'dd.mm.yyyy')
        self.assertEqual(ws['C2'].number_format, '@')
        self.assertEqual(ws['D2'].number_format, 'yyyy-mm-dd h:mm:ss')

    def test_rows(self):
        ws = self.load()
        rows = [[c.value for c in row] for row in ws.iter_rows(min_row=2, max_col=16)]
        self.assertEqual(len(rows), 3)
        # Sorted by interviewer, then time: Елена first, then Марина's two calls.
        self.assertEqual([r[4] for r in rows], ['Елена 306', 'Марина 308', 'Марина 308'])
        elena, marina_reviewed, marina_open = rows
        self.assertEqual(elena[2], '375290000105')
        self.assertEqual(elena[3], datetime(2026, 9, 25, 9, 18, 17))
        self.assertEqual(elena[9], 'есть')
        self.assertEqual(elena[10], 'неверно указан возраст')
        self.assertEqual(elena[11], 'ошибка')
        self.assertEqual(elena[15], 'исправ ИБ')
        self.assertEqual(marina_reviewed[1], datetime(2026, 9, 26))
        self.assertEqual(marina_reviewed[6], 'Брест')
        self.assertEqual(marina_reviewed[13], 122)
        self.assertEqual(marina_reviewed[14], 'ИБ')
        # Not reviewed yet: auto fields only, interviewer from the directory.
        self.assertIsNone(marina_open[1])
        self.assertEqual(marina_open[13], 171)
        self.assertIsNone(marina_open[11])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class ReviewTests(TestCase):
    def setUp(self):
        AppSettings.objects.create(pk=1, controller='ИБ')
        Interviewer.objects.create(extension='308', name='Марина')
        self.a = make_call(CALL_A)
        self.b = make_call(CALL_B)

    def test_form_prefilled(self):
        r = self.client.get(self.a.get_absolute_url())
        self.assertContains(r, 'value="Марина 308"')
        self.assertContains(r, 'value="ИБ"')

    def test_autosave_then_complete(self):
        url = reverse('player:review_save', args=[self.a.pk])
        data = {'interviewer': 'Марина 308', 'review_date': '2026-09-26', 'city': 'Минск', 'result': 'ок'}
        r = self.client.post(url, data)
        self.assertEqual(r.json()['completed'], False)
        self.assertEqual(CallReview.objects.get(audio=self.a).city, 'Минск')

        r = self.client.post(url, {**data, 'complete': '1'})
        self.assertTrue(r.json()['completed'])
        self.assertEqual(r.json()['next_url'], self.b.get_absolute_url())
        self.assertEqual(CallReview.objects.count(), 1)

    def test_invalid_result(self):
        r = self.client.post(reverse('player:review_save', args=[self.a.pk]), {'result': 'отлично', 'review_date': '2026-09-26'})
        self.assertEqual(r.status_code, 400)

    def test_default_interviewer_remembers_last_choice(self):
        CallReview.objects.create(audio=self.a, interviewer='Маргарита 308')
        self.assertEqual(self.b.default_interviewer(), 'Маргарита 308')


class FakePbx:
    def __init__(self, recordings, body=None):
        self.recordings = recordings
        self.body = body or make_wav()
        self.downloaded = []

    def list_recordings(self, day):
        return self.recordings

    def download(self, rec, dest):
        self.downloaded.append(rec.filename)
        Path(dest).write_bytes(self.body)


def rec(filename, billsec=100, uniqueid='u'):
    return Recording(uniqueid=uniqueid, filename=filename, calldate=datetime(2026, 9, 25),
                     src='308', dst='', billsec=billsec, disposition='ANSWERED')


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class SyncTests(TestCase):
    def setUp(self):
        AppSettings.objects.create(pk=1, min_duration=30)
        Interviewer.objects.create(extension='308', name='Марина')

    def test_downloads_new_calls_and_skips_known(self):
        make_call(CALL_A)  # already downloaded before
        fake = FakePbx([
            rec(CALL_A),
            rec(CALL_B),
            rec(CALL_C),                                    # extension 306 is not in the list
            rec('out-375290000001-308-20260925-120000-1790000010.110.wav', billsec=5),  # too short
        ])
        sync = PbxSync.objects.create(day=date(2026, 9, 25))
        run_sync(sync, client=fake)

        sync.refresh_from_db()
        self.assertEqual(sync.status, PbxSync.Status.DONE)
        self.assertEqual((sync.found, sync.downloaded, sync.skipped), (2, 1, 1))
        self.assertEqual(fake.downloaded, [CALL_B])
        new = AudioFile.objects.get(call_id='1790000002.101')
        self.assertEqual(new.status, AudioFile.Status.PENDING)
        self.assertEqual(new.sync, sync)
        self.assertAlmostEqual(new.duration, 1.0)

    def test_pbx_error_is_reported(self):
        class Broken(FakePbx):
            def list_recordings(self, day):
                raise PbxError('Не удалось войти в АТС: неверный логин или пароль.')

        sync = PbxSync.objects.create(day=date(2026, 9, 25))
        run_sync(sync, client=Broken([]))
        sync.refresh_from_db()
        self.assertEqual(sync.status, PbxSync.Status.ERROR)
        self.assertIn('неверный логин', sync.error)


class PbxClientTests(TestCase):
    CSV = (
        'calldate,clid,src,dst,dcontext,billsec,disposition,uniqueid,recordingfile\n'
        f'2026-09-25 09:32:15,"Марина" <308>,308,375290000103,from-internal,122,ANSWERED,1790000001.100,{CALL_A}\n'
        '2026-09-25 09:40:00,"Марина" <308>,308,375290000000,from-internal,0,NO ANSWER,1790000005.105,\n'
    )

    def test_parse_cdr_csv(self):
        recs = parse_cdr_csv(self.CSV)
        self.assertEqual(len(recs), 1)  # the call without a recording is dropped
        self.assertEqual(recs[0].filename, CALL_A)
        self.assertEqual(recs[0].billsec, 122)
        self.assertEqual(recs[0].calldate, datetime(2026, 9, 25, 9, 32, 15))

    def test_parse_rejects_html(self):
        with self.assertRaises(PbxError):
            parse_cdr_csv('<html><form id="loginform"></form></html>')

    def test_download_rejects_login_page(self):
        client = FreePbxClient('https://pbx.local', 'admin', 'secret')
        client._logged_in = True
        html = mock.Mock(status_code=200, raw=io.BytesIO(b'<html>login</html>'))
        with mock.patch.object(client.session, 'request', return_value=html), \
                tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PbxError):
                client.download(rec(CALL_A), Path(tmp) / CALL_A)
            self.assertEqual(list(Path(tmp).iterdir()), [])  # no .part leftovers

    def test_download_saves_wav(self):
        client = FreePbxClient('https://pbx.local', 'admin', 'secret')
        client._logged_in = True
        ok = mock.Mock(status_code=200, raw=io.BytesIO(make_wav()))
        with mock.patch.object(client.session, 'request', return_value=ok), \
                tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / CALL_A
            client.download(rec(CALL_A), dest)
            self.assertTrue(dest.read_bytes().startswith(b'RIFF'))

    def test_missing_credentials(self):
        with self.assertRaises(PbxError):
            FreePbxClient('', 'admin', '')


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class WorkerTests(TestCase):
    def test_sync_runs_before_transcription(self):
        audio = make_call(CALL_A)
        sync = PbxSync.objects.create(day=date(2026, 9, 25))
        with mock.patch('player.worker.run_sync') as run_sync_mock, \
                mock.patch('player.worker.transcribe') as transcribe_mock:
            self.assertTrue(worker.run_once())
            run_sync_mock.assert_called_once_with(sync)
            transcribe_mock.assert_not_called()

            PbxSync.objects.update(status=PbxSync.Status.DONE)
            self.assertTrue(worker.run_once())
            transcribe_mock.assert_called_once_with(audio)
        self.assertTrue(AppSettings.load().worker_alive)

    def test_idle(self):
        self.assertFalse(worker.run_once())

    def test_recover_interrupted(self):
        audio = make_call(CALL_A, status=AudioFile.Status.PROCESSING)
        worker.recover_interrupted()
        audio.refresh_from_db()
        self.assertEqual(audio.status, AudioFile.Status.PENDING)


class EngineTests(TestCase):
    def test_available_engines_does_not_import(self):
        # Importing both torch and CTranslate2 in one process segfaults.
        from .transcription import available_engines

        with mock.patch.dict('sys.modules'):
            for module in ('whisper', 'faster_whisper'):
                sys.modules.pop(module, None)
            available_engines()
            self.assertNotIn('whisper', sys.modules)
            self.assertNotIn('faster_whisper', sys.modules)


class SettingsFormTests(TestCase):
    def test_interviewers_parsed_and_replaced(self):
        Interviewer.objects.create(extension='301', name='Эдуард')
        form = SettingsForm({
            'min_duration': 30,
            'interviewers': '301 Виолетта\n\n308 Марина\n308 Маргарита\n',
        }, instance=AppSettings.load())
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        form.save_interviewers()
        self.assertEqual(
            sorted(map(str, Interviewer.objects.all())),
            ['Виолетта 301', 'Маргарита 308', 'Марина 308'],
        )

    def test_bad_line(self):
        form = SettingsForm({'min_duration': 30, 'interviewers': 'Марина'}, instance=AppSettings.load())
        self.assertFalse(form.is_valid())
        self.assertIn('Строка 1', str(form.errors))
