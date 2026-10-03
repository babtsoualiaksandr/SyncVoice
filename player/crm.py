"""Client for the operators' survey CRM (Symfony app at http://192.168.12.230).

Known from real requests:
- login form at /login with fields `email`, `password` and a hidden
  `_csrf_token` (the form is read and submitted as the browser does);
- POST /admin/Reports/find with PhoneN=<phone> (XHR) returns the surveys of
  that phone as JSON: Id, MembersSurveyID, MemberID, Phone, OPUserID,
  OPTime ("29-09-2026 09:24:59");
- a survey page is /admin/Reports/update<Id>.

SyncVoice logs in with its own CRM account; the controller never has to log
in to view a survey (see player/crm_proxy.py).
"""
import logging
import threading
import warnings
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

logger = logging.getLogger(__name__)

TIMEOUT = 20
LOGIN_PATH = 'login'
FIND_PATH = 'admin/Reports/find'


class CrmError(Exception):
    """A problem the user can act on (wrong password, CRM unreachable, ...)."""


@dataclass
class Survey:
    id: int
    members_survey_id: str
    member_id: str
    phone: str
    operator_user_id: int | None
    time: datetime | None
    respondent_name: str = ''  # filled by fetch_answers; kept locally, never sent to Gemini

    @property
    def path(self) -> str:
        return survey_path(self.id)


def survey_path(survey_id) -> str:
    return f'admin/Reports/update{int(survey_id)}'


class _FormParser(HTMLParser):
    """Collects the inputs of the first <form> that has a password field."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []  # [(action, method, [(name, type, value)])]
        self._form = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self._form = (attrs.get('action') or '', (attrs.get('method') or 'get').lower(), [])
            self.forms.append(self._form)
        elif tag == 'input' and self._form is not None and attrs.get('name'):
            self._form[2].append((attrs['name'], (attrs.get('type') or 'text').lower(), attrs.get('value') or ''))

    def handle_endtag(self, tag):
        if tag == 'form':
            self._form = None


def login_form_data(html: str, username: str, password: str) -> tuple[str, dict]:
    """(action, data) for the login form in `html`, with hidden fields kept."""
    parser = _FormParser()
    parser.feed(html)
    form = next((f for f in parser.forms if any(t == 'password' for _, t, _ in f[2])), None)
    if not form:
        raise CrmError('На странице входа CRM не найдена форма с паролем.')
    action, _, inputs = form
    data = {}
    user_field = None
    for name, kind, value in inputs:
        if kind == 'password':
            data[name] = password
        elif kind in ('text', 'email') and user_field is None:
            user_field = name
            data[name] = username
        elif kind in ('hidden', 'text', 'email'):
            data[name] = value  # e.g. Symfony's _csrf_token; checkboxes («remember me») are left off
    if user_field is None:
        raise CrmError('На странице входа CRM не найдено поле логина.')
    return action, data


def _is_login_page(response: requests.Response) -> bool:
    """The CRM sent us to log in: the login page itself or a redirect to it."""
    if response.is_redirect:
        return urlsplit(response.headers.get('Location', '')).path.rstrip('/').endswith('/login')
    return urlsplit(response.url).path.rstrip('/').endswith('/login') or 'type="password"' in response.text


class CrmClient:
    """Thread-safe: one login at a time, shared session for all requests."""

    def __init__(self, base_url: str, username: str, password: str):
        if not base_url or not username or not password:
            raise CrmError('Не заданы адрес CRM, логин или пароль — откройте «Настройки».')
        self.base_url = base_url.rstrip('/') + '/'
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.trust_env = False  # a LAN CRM: never through system proxy variables
        self.session.verify = False
        self._lock = threading.Lock()
        self._logged_in = False

    def url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip('/'))

    def _send(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', urllib3.exceptions.InsecureRequestWarning)
                response = self.session.request(method, self.url(path), timeout=TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise CrmError(f'CRM недоступна: {exc}')
        if response.status_code >= 500:
            raise CrmError(f'CRM ответила ошибкой {response.status_code} на {path}')
        return response

    def login(self) -> None:
        with self._lock:
            page = self._send('GET', LOGIN_PATH)
            action, data = login_form_data(page.text, self.username, self.password)
            response = self._send('POST', urljoin(page.url, action or page.url), data=data,
                                  headers={'Referer': page.url, 'Origin': self.base_url.rstrip('/')})
            if _is_login_page(response):
                raise CrmError('Не удалось войти в CRM: неверный логин или пароль.')
            self._logged_in = True

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Request as the logged-in SyncVoice account; logs in again if the session expired."""
        if not self._logged_in:
            self.login()
        response = self._send(method, path, **kwargs)
        if _is_login_page(response) and not path.rstrip('/').endswith('login'):
            self._logged_in = False
            self.login()
            response = self._send(method, path, **kwargs)
        return response

    def find(self, phone: str) -> list[Survey]:
        response = self.request('POST', FIND_PATH, data={'PhoneN': phone}, headers={
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': self.url('admin/Reports'),
        })
        try:
            rows = response.json()
        except ValueError:
            raise CrmError('CRM вернула не список анкет — проверьте права учётной записи SyncVoice.')
        return [parse_survey(row) for row in rows or []]


def parse_survey(row: dict) -> Survey:
    try:
        time = datetime.strptime(str(row.get('OPTime', '')), '%d-%m-%Y %H:%M:%S')
    except ValueError:
        time = None
    return Survey(
        id=int(row['Id']),
        members_survey_id=str(row.get('MembersSurveyID') or ''),
        member_id=str(row.get('MemberID') or ''),
        phone=str(row.get('Phone') or ''),
        operator_user_id=row.get('OPUserID'),
        time=time,
    )


def pick_survey(surveys: list[Survey], call_time: datetime | None) -> Survey | None:
    """The survey entered for this call: same day, closest in time to the call.

    The operator saves the survey during or right after the call
    (call 09:23:05 -> survey 09:24:59). Other days are earlier waves.
    """
    if not call_time:
        return None
    same_day = [s for s in surveys if s.time and s.time.date() == call_time.date()]
    if not same_day:
        return None
    return min(same_day, key=lambda s: abs(s.time - call_time))


# One client per (address, login, password) for the whole process.
_clients: dict[tuple, CrmClient] = {}
_clients_lock = threading.Lock()


def get_client() -> CrmClient:
    from .credentials import CRM_SERVICE, get_password
    from .models import AppSettings

    settings = AppSettings.load()
    password = get_password(settings.crm_username, CRM_SERVICE) or ''
    key = (settings.crm_url, settings.crm_username, password)
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = CrmClient(*key)
            _clients.clear()
            _clients[key] = client
        return client


def configured() -> bool:
    from .models import AppSettings

    settings = AppSettings.load()
    return bool(settings.crm_url and settings.crm_username)



# ---------- reading a survey's answers ----------

# Profile fields on the survey page (input value / selected option text), by element id.
PROFILE_FIELDS = {
    'RespCity': 'Город',
    'RespAge': 'Возраст',
    'RespGender': 'Пол',
    'RespEdStatus': 'Образование',
    'RespWorkStatus': 'Занятость',
    'RespIncome': 'Доля дохода на продукты питания',
    'RespRecall': 'Можно ли перезвонить',
}
# Not sent anywhere: identifies the respondent, adds nothing to the check.
PERSONAL_KEYS = {'MemberName', 'MemberID', 'Phone', 'RespName'}


class _SurveyPageParser(HTMLParser):
    """Values of the profile inputs/selects and the MembersSurveyID of the survey page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self.respondent_name = ''
        self.members_survey_id = ''
        self._select = None
        self._option_selected = False
        self._option_text = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('id') == 'RespName':
            self.respondent_name = (attrs.get('value') or '').strip()
        elif tag == 'input' and attrs.get('id') in PROFILE_FIELDS:
            self.values[attrs['id']] = (attrs.get('value') or '').strip()
        elif tag == 'select' and attrs.get('id') in PROFILE_FIELDS:
            self._select = attrs['id']
            self.values.setdefault(self._select, '')
        elif tag == 'option' and self._select:
            self._option_selected = 'selected' in attrs
            self._option_text = ''
        elif tag == 'script' and attrs.get('data-members'):
            self.members_survey_id = attrs['data-members']

    def handle_data(self, data):
        if self._select:
            self._option_text += data

    def handle_endtag(self, tag):
        if tag == 'option' and self._select:
            if self._option_selected:
                self.values[self._select] = ' '.join(self._option_text.split())
            self._option_selected = False
        elif tag == 'select':
            self._select = None


def _parse_page(html: str) -> _SurveyPageParser:
    parser = _SurveyPageParser()
    parser.feed(html)
    return parser


def parse_survey_page(html: str) -> tuple[dict, str]:
    """({«Город»: «Минск», …}, MembersSurveyID) from a /admin/Reports/update<Id> page."""
    parser = _parse_page(html)
    profile = {label: parser.values.get(field, '') for field, label in PROFILE_FIELDS.items()}
    return profile, parser.members_survey_id


def respondent_name(html: str) -> str:
    """The respondent's name from the survey page — for the call list only, not for the AI."""
    return _parse_page(html).respondent_name


def _without_personal(value):
    if isinstance(value, dict):
        return {k: _without_personal(v) for k, v in value.items() if k not in PERSONAL_KEYS}
    if isinstance(value, list):
        return [_without_personal(v) for v in value]
    return value


def fetch_answers(client: 'CrmClient', survey: Survey) -> dict:
    """The operator's answers of `survey`, without name/phone/IDs, ready for the check.

    {'Профиль': {...}, 'День «вчера»': '28.09.2026', 'Ответы о слушании': {...},
     'Внесено': '29.09.2026 09:36:18'}
    """
    page = client.request('GET', survey.path)
    profile, members_survey_id = parse_survey_page(page.text)
    survey.respondent_name = respondent_name(page.text)
    members_survey_id = members_survey_id or survey.members_survey_id
    response = client.request('POST', 'admin/Reports/getMember', data={'MembersSurveyID': members_survey_id},
                              headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': client.url(survey.path)})
    try:
        member = response.json()
    except ValueError:
        raise CrmError('CRM вернула не данные анкеты (getMember) — проверьте права учётной записи SyncVoice.')
    survey_day = ''
    raw_day = (member.get('SurveyDate') or {}).get('date', '') if isinstance(member.get('SurveyDate'), dict) else ''
    if raw_day:
        try:
            survey_day = datetime.strptime(raw_day[:10], '%Y-%m-%d').strftime('%d.%m.%Y')
        except ValueError:
            survey_day = raw_day[:10]
    if not survey.respondent_name and isinstance(member.get('MemberName'), str):
        survey.respondent_name = member['MemberName'].strip()
    return {
        'Профиль': profile,
        'День «вчера»': survey_day,
        'Ответы о слушании': _without_personal(member.get('SurveyDataJSON') or {}),
        'Внесено': str(member.get('OPTime') or ''),
    }
