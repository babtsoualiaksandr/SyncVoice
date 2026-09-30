"""FreePBX web client: log in to /admin, list a day's calls from CDR Reports,
download call recordings.

Access to the PBX is web-only, so this does what the browser does.

NOTE: the endpoints below follow the stock FreePBX 15/16 `cdr` module and
must be checked against the real requests of the PBX in use (DevTools →
Network → Copy as cURL for login, CDR search and "download recording").
"""
import csv
import io
import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
import urllib3

from .audio_utils import is_wav

logger = logging.getLogger(__name__)

LOGIN_PATH = 'admin/config.php'
CDR_PATH = 'admin/config.php?display=cdr'
# Tried in order until one returns a WAV. {uniqueid} is the CDR uniqueid.
DOWNLOAD_PATHS = [
    'admin/ajax.php?module=cdr&command=download&msgid={uniqueid}&type=download&format=wav',
    'admin/config.php?display=cdr&action=download_audio&cdr_file={uniqueid}',
]
TIMEOUT = 30


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

    def login(self) -> None:
        response = self._request('POST', LOGIN_PATH, data={
            'username': self.username,
            'password': self.password,
        })
        if self._is_login_page(response):
            raise PbxError('Не удалось войти в АТС: неверный логин или пароль.')
        self._logged_in = True

    def _ensure_login(self):
        if not self._logged_in:
            self.login()

    def list_recordings(self, day: date) -> list[Recording]:
        """All calls of `day` that have a recording, from the CDR CSV export."""
        self._ensure_login()
        form = {
            'need_csv': 'true',
            'order': 'calldate', 'sort': 'ASC', 'limit': '',
            'startday': f'{day.day:02}', 'startmonth': f'{day.month:02}', 'startyear': str(day.year),
            'starthour': '00', 'startmin': '00',
            'endday': f'{day.day:02}', 'endmonth': f'{day.month:02}', 'endyear': str(day.year),
            'endhour': '23', 'endmin': '59',
        }
        response = self._request('POST', CDR_PATH, data=form)
        if self._is_login_page(response):  # session expired
            self._logged_in = False
            self._ensure_login()
            response = self._request('POST', CDR_PATH, data=form)
        return parse_cdr_csv(response.text)

    def download(self, recording: Recording, dest: Path) -> None:
        """Download `recording` to `dest`. Raises PbxError if no URL returns a WAV."""
        self._ensure_login()
        for path in DOWNLOAD_PATHS:
            path = path.format(uniqueid=recording.uniqueid)
            try:
                response = self._request('GET', path, stream=True)
            except PbxError as exc:
                logger.info('Download via %s failed: %s', path, exc)
                continue
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False, suffix='.part') as tmp:
                shutil.copyfileobj(response.raw, tmp)
            tmp_path = Path(tmp.name)
            if is_wav(tmp_path):
                tmp_path.replace(dest)
                return
            tmp_path.unlink()
        raise PbxError(f'Не удалось скачать запись {recording.filename}')


def parse_cdr_csv(text: str) -> list[Recording]:
    if 'calldate' not in text[:500]:
        raise PbxError('АТС вернула не CSV-выгрузку CDR — нужно сверить запрос поиска.')
    recordings = []
    for row in csv.DictReader(io.StringIO(text.lstrip('﻿'))):
        filename = Path((row.get('recordingfile') or '').strip()).name
        if not filename:
            continue
        recordings.append(Recording(
            uniqueid=row['uniqueid'].strip(),
            filename=filename,
            calldate=datetime.strptime(row['calldate'].strip(), '%Y-%m-%d %H:%M:%S'),
            src=row.get('src', '').strip(),
            dst=row.get('dst', '').strip(),
            billsec=int(row.get('billsec') or 0),
            disposition=row.get('disposition', '').strip(),
        ))
    return recordings
