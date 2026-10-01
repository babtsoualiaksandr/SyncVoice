"""FreePBX web client: log in to /admin, list a day's calls from CDR Reports,
download call recordings.

Access to the PBX is web-only, so this does what the browser does. The login,
the CDR search form, the result table and the download link are taken from
real requests to the PBX (FreePBX at 192.168.3.80, «Отчёты CDR»).
"""
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import requests
import urllib3

from .audio_utils import extract_wav

logger = logging.getLogger(__name__)

CDR_PATH = 'admin/config.php?display=cdr'
LOGIN_PATH = CDR_PATH  # the browser logs in on the CDR page itself
# Tried in order until one returns a WAV. {uniqueid} is the CDR uniqueid.
DOWNLOAD_PATHS = [
    'admin/config.php?display=cdr&action=download_audio&cdr_file={uniqueid}',  # seen on the PBX
    'admin/ajax.php?module=cdr&command=download&msgid={uniqueid}&type=download&format=wav',
]
TIMEOUT = 30
# Text filters of the CDR search form, all left empty ("begins with" as the browser sends).
_EMPTY_FILTERS = ['cnum', 'cnam', 'outbound_cnum', 'did', 'dst', 'dst_cnam', 'userfield', 'accountcode']


class PbxError(Exception):
    """A problem the user can act on (wrong password, PBX unreachable, ...)."""


@dataclass
class Recording:
    uniqueid: str
    filename: str  # recording file name, e.g. out-375...-304-20260925-093215-1790....wav
    calldate: datetime
    src: str
    dst: str
    billsec: int
    disposition: str


def cdr_search_form(day: date, min_duration: int = 0, max_duration: int = 0) -> dict:
    """The CDR Reports search form for one day, as the browser submits it."""
    form = {
        'startday': f'{day.day:02}', 'startmonth': f'{day.month:02}', 'startyear': str(day.year),
        'starthour': '00', 'startmin': '00',
        'endday': f'{day.day:02}', 'endmonth': f'{day.month:02}', 'endyear': str(day.year),
        'endhour': '23', 'endmin': '59',
        'need_html': 'true',
        'limit': '1000',
        'order': 'calldate',
        'sort': 'ASC',
        'group': 'day',
        'disposition': 'all',
        'dur_min': str(min_duration or ''),
        'dur_max': str(max_duration or ''),
    }
    for name in _EMPTY_FILTERS:
        form[name] = ''
        form[f'{name}_mod'] = 'begins_with'
    return form


class FreePbxClient:
    def __init__(self, base_url: str, username: str, password: str, verify_ssl: bool = False):
        if not base_url or not username or not password:
            raise PbxError('Не заданы адрес АТС, логин или пароль — откройте «Настройки».')
        self.base_url = base_url.rstrip('/') + '/'
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.verify = verify_ssl
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._logged_in = False

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = self.session.request(method, self._url(path), timeout=TIMEOUT, **kwargs)
        except requests.exceptions.SSLError as exc:
            raise PbxError(f'Ошибка SSL при подключении к АТС. Выключите проверку сертификата в настройках. ({exc})')
        except requests.exceptions.RequestException as exc:
            raise PbxError(f'АТС недоступна: {exc}')
        if response.status_code >= 400:
            raise PbxError(f'АТС ответила ошибкой {response.status_code} на {path}')
        return response

    @staticmethod
    def _is_login_page(response: requests.Response) -> bool:
        return 'loginform' in response.text or 'name="password"' in response.text

    def _form_headers(self) -> dict:
        origin = self.base_url.rstrip('/')
        return {'Origin': origin, 'Referer': self._url(CDR_PATH)}

    def login(self) -> None:
        response = self._request('POST', LOGIN_PATH, data={
            'username': self.username,
            'password': self.password,
        }, headers=self._form_headers())
        if self._is_login_page(response):
            raise PbxError('Не удалось войти в АТС: неверный логин или пароль.')
        self._logged_in = True

    def _ensure_login(self):
        if not self._logged_in:
            self.login()

    def list_recordings(self, day: date, min_duration: int = 0, max_duration: int = 0) -> list[Recording]:
        """All calls of `day` that have a recording, from the CDR Reports call list."""
        self._ensure_login()
        form = cdr_search_form(day, min_duration, max_duration)
        headers = self._form_headers()
        response = self._request('POST', CDR_PATH, data=form, headers=headers)
        if self._is_login_page(response):  # session expired
            self._logged_in = False
            self._ensure_login()
            response = self._request('POST', CDR_PATH, data=form, headers=headers)
        return parse_cdr_html(response.text)

    def download(self, recording: Recording, dest: Path, debug_dir: Path | None = None) -> None:
        """Download `recording` to `dest`. Raises PbxError if no URL returns a WAV.

        If the PBX answers with something else, the reply is described in the
        error and, with `debug_dir`, saved there for inspection.
        """
        self._ensure_login()
        problems = []
        for template in DOWNLOAD_PATHS:
            path = template.format(uniqueid=quote(recording.uniqueid, safe='.'))
            try:
                response = self._request('GET', path, stream=True, headers=self._form_headers())
            except PbxError as exc:
                problems.append(str(exc))
                continue
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False, suffix='.part') as tmp:
                for chunk in response.iter_content(64 * 1024):  # unpacks gzip/deflate, unlike .raw
                    tmp.write(chunk)
            tmp_path = Path(tmp.name)
            if extract_wav(tmp_path):
                tmp_path.replace(dest)
                return
            problems.append(_describe_reply(path, response, tmp_path))
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                kind = 'ajax' if 'ajax.php' in path else 'download_audio'
                shutil.copyfile(tmp_path, debug_dir / f'{recording.uniqueid}-{kind}.html')
            tmp_path.unlink()
        raise PbxError(f'Не удалось скачать запись {recording.filename} — ' + '; '.join(problems))


def _describe_reply(path: str, response: requests.Response, body_path: Path) -> str:
    """Short description of a non-WAV reply, for the user and for debugging."""
    with open(body_path, 'rb') as f:
        head = f.read(300)
    size = body_path.stat().st_size
    text = re.sub(r'\s+', ' ', head.decode('utf-8', 'replace')).strip()
    kind = 'страница входа (сессия не принята)' if 'loginform' in text or 'name="password"' in text else \
        'пустой ответ' if not size else f'начало: «{text[:120]}»'
    label = 'ajax.php' if 'ajax.php' in path else 'download_audio'
    content_type = response.headers.get('Content-Type', '?')
    return f'{label}: HTTP {response.status_code}, {content_type}, {size} байт, {kind}'


@dataclass
class _Cell:
    title: str = ''
    text: str = ''
    links: list = None


class _CdrTableParser(HTMLParser):
    """Collects the cells of every <tr class="record"> of the CDR call list.

    The page is not well-formed (playback rows are never closed), so a new
    <tr> simply starts a new row.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'tr':
            self._finish_row()
            if 'record' in (attrs.get('class') or '').split():
                self._row = []
        elif self._row is not None and tag == 'td':
            self._finish_cell()
            self._cell = _Cell(title=attrs.get('title') or '', links=[])
        elif self._cell is not None and tag == 'a' and attrs.get('href'):
            self._cell.links.append(attrs['href'])

    def handle_endtag(self, tag):
        if tag == 'td':
            self._finish_cell()
        elif tag in ('tr', 'table'):
            self._finish_row()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.text += data

    def _finish_cell(self):
        if self._row is not None and self._cell is not None:
            self._cell.text = self._cell.text.strip()
            self._row.append(self._cell)
        self._cell = None

    def _finish_row(self):
        self._finish_cell()
        if self._row:
            self.rows.append(self._row)
        self._row = None

    def close(self):
        super().close()
        self._finish_row()


_DURATION_RE = re.compile(r'(\d+):(\d{2})(?::(\d{2}))?')


def _seconds(text: str) -> int:
    """'01:46' -> 106, '1:02:03' -> 3723, anything else -> 0."""
    m = _DURATION_RE.search(text or '')
    if not m:
        return 0
    a, b, c = m.groups()
    return int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)


def parse_cdr_html(text: str) -> list[Recording]:
    """Calls with a recording from the CDR Reports result page."""
    if 'startday' not in text:  # the search form is on every CDR Reports page
        raise PbxError('АТС вернула не страницу «Отчёты CDR» — проверьте адрес АТС и права пользователя.')
    parser = _CdrTableParser()
    parser.feed(text)
    parser.close()

    recordings = []
    for cells in parser.rows:
        if len(cells) < 10:
            continue
        filename = Path(cells[1].title.strip()).name
        download = next((link for link in cells[1].links if 'cdr_file=' in link), '')
        uniqueid = (parse_qs(urlsplit(download).query).get('cdr_file') or [cells[2].text])[0].strip()
        if not filename.lower().endswith('.wav') or not uniqueid:
            continue  # no recording for this call
        try:
            calldate = datetime.strptime(cells[0].text, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        src = re.search(r'<(\d+)>', cells[4].text) or re.search(r'<(\d+)>', cells[3].text)
        recordings.append(Recording(
            uniqueid=uniqueid,
            filename=filename,
            calldate=calldate,
            src=src.group(1) if src else '',
            dst=cells[7].text,
            billsec=_seconds(cells[9].title) or _seconds(cells[9].text),
            disposition=cells[8].text,
        ))
    return recordings
