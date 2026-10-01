"""Local read-only gateway to the CRM: http://127.0.0.1:<CRM_PROXY_PORT>/...

The CRM (http://192.168.12.230) can't keep a login inside SyncVoice's page:
it is another site, so the browser doesn't send its session cookie to an
embedded frame. The gateway forwards requests to the CRM with SyncVoice's
own logged-in session instead, so the survey shows in the panel and the
controller never logs in there.

Only GET/HEAD pass: the gateway is for viewing; nothing can be changed in
the CRM under SyncVoice's account. It listens on 127.0.0.1 only.
"""
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from django.conf import settings
from django.db import close_old_connections

from .crm import CrmError, get_client

logger = logging.getLogger(__name__)

HOP_BY_HOP = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
    'transfer-encoding', 'upgrade',
    # set by the gateway itself / not for the browser
    'content-encoding', 'content-length', 'set-cookie', 'x-frame-options',
}
TEXT_TYPES = ('text/', 'application/javascript', 'application/json', 'application/xml')


def proxy_origin() -> str:
    return f'http://127.0.0.1:{settings.CRM_PROXY_PORT}'


def _page(status: int, title: str, text: str) -> tuple[int, list, bytes]:
    body = (f'<!doctype html><meta charset="utf-8"><body style="font:15px sans-serif;padding:20px">'
            f'<h3>{title}</h3><p>{text}</p></body>').encode()
    return status, [('Content-Type', 'text/html; charset=utf-8')], body


def _without_frame_ancestors(csp: str) -> str:
    kept = [d for d in csp.split(';') if d.strip() and not d.strip().lower().startswith('frame-ancestors')]
    return ';'.join(kept).strip()


def proxy_response(client, method: str, path: str) -> tuple[int, list, bytes]:
    """(status, headers, body) for one request to the gateway."""
    if method not in ('GET', 'HEAD'):
        return _page(405, 'Только просмотр',
                     'Через SyncVoice анкету можно только смотреть. Чтобы изменить её, откройте «Окно рядом».')
    try:
        response = client.request(method, path, allow_redirects=False)
    except CrmError as exc:
        return _page(502, 'CRM недоступна', str(exc))

    crm_origin = client.base_url.rstrip('/')
    crm_host = urlsplit(crm_origin).netloc
    origin = proxy_origin()

    def rewrite(text: str) -> str:
        return re.sub(rf'(https?:)?//{re.escape(crm_host)}', origin, text)

    headers = []
    for name, value in response.headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP:
            continue
        if lower == 'location':
            value = rewrite(value)
        elif lower == 'content-security-policy':
            value = _without_frame_ancestors(value)
            if not value:
                continue
        headers.append((name, value))

    body = response.content
    content_type = response.headers.get('Content-Type', '')
    if body and content_type.startswith(TEXT_TYPES):
        encoding = response.encoding or 'utf-8'
        body = rewrite(body.decode(encoding, 'replace')).encode(encoding, 'replace')
    return response.status_code, headers, body


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _handle(self):
        close_old_connections()
        try:
            status, headers, body = proxy_response(get_client(), self.command, self.path)
        except CrmError as exc:  # not configured
            status, headers, body = _page(503, 'CRM не настроена', f'{exc}')
        except Exception:
            logger.exception('CRM gateway failed on %s', self.path)
            status, headers, body = _page(500, 'Ошибка', 'Не удалось получить страницу CRM.')
        finally:
            close_old_connections()
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def log_message(self, fmt, *args):
        logger.debug('CRM gateway: ' + fmt, *args)


def start() -> ThreadingHTTPServer | None:
    """Run the gateway in a daemon thread (from run_app). None if the port is busy."""
    try:
        server = ThreadingHTTPServer(('127.0.0.1', settings.CRM_PROXY_PORT), _Handler)
    except OSError as exc:
        logger.warning('CRM gateway not started on port %s: %s', settings.CRM_PROXY_PORT, exc)
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name='crm-gateway').start()
    return server
