"""Dependency-free HTTP server for the TunerLab web console.

Uses only the Python standard library: :class:`http.server.ThreadingHTTPServer`
for concurrency, and a tiny hand-written router for the JSON API plus static
file serving. No Flask, no FastAPI, no external package.
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import webbrowser
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import parse_qs

import numpy as np

from . import handlers
from .state import state

STATIC_DIR = Path(__file__).resolve().parent / 'static'


def _resolve_static_dir() -> Path:
    """Where the console's HTML/CSS/JS live.

    From source that is the package's own ``static`` folder.  In a frozen
    build the module lives inside the PYZ, so ``__file__`` is a pseudo path
    under ``sys._MEIPASS`` and the assets are shipped as *data* next to it —
    the packaging spec puts them in ``tuner/web/static``.
    """
    here = Path(__file__).resolve().parent / 'static'
    if here.is_dir():
        return here
    frozen_root = getattr(sys, '_MEIPASS', None)
    if frozen_root:
        candidate = Path(frozen_root) / 'tuner' / 'web' / 'static'
        if candidate.is_dir():
            return candidate
    return here


STATIC_DIR = _resolve_static_dir()


def to_json(obj):
    """Recursively convert numpy / dataclass / complex values to JSON-safe types."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj
    if isinstance(obj, complex):
        return {'re': obj.real, 'im': obj.imag}
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return [to_json(v) for v in obj.tolist()]
    if is_dataclass(obj) and not isinstance(obj, type):
        return to_json(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): to_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_json(v) for v in obj]
    if hasattr(obj, 'tolist'):
        return to_json(obj.tolist())
    if hasattr(obj, '__dict__'):
        return to_json(vars(obj))
    return str(obj)


def build_response_payload(ok: bool, data=None, error=None) -> bytes:
    payload = {'ok': ok}
    if ok:
        payload['data'] = to_json(data)
    else:
        payload['error'] = str(error)
    return json.dumps(payload, ensure_ascii=False).encode('utf-8')


class RequestHandler(BaseHTTPRequestHandler):
    server_version = 'TunerLabWeb/1.0'
    protocol_version = 'HTTP/1.1'

    #: The state every request acts on.  The default is the module-level
    #: singleton; :func:`make_server` binds a different one when the desktop
    #: shell starts the console inside its own process, so both faces act on
    #: the *same* session, the same loaded library and the same log.
    state = state

    # Silence the default request logging; the console has its own log area.
    def log_message(self, *args):  # noqa: D401 - override intentionally quiet
        return

    # -- helpers ---------------------------------------------------------
    def _send_json(self, payload: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode('utf-8'))

    # -- dispatch --------------------------------------------------------
    def do_GET(self):  # noqa: N802 - required BaseHTTPRequestHandler name
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ('/', '/index.html'):
            index = STATIC_DIR / 'index.html'
            if index.is_file():
                return self._send_file(index)
            self._send_json(build_response_payload(False, error='index.html missing'), 500)
            return
        if path.startswith('/static/'):
            rel = path[len('/static/'):]
            candidate = (STATIC_DIR / rel).resolve()
            if candidate.is_relative_to(STATIC_DIR) and candidate.is_file():
                return self._send_file(candidate)
            self._send_json(build_response_payload(False, error='not found'), 404)
            return
        self._dispatch('GET', path, parsed, {})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_body()
        self._dispatch('POST', parsed.path, parsed, body)

    def _dispatch(self, method: str, path: str, parsed, body: dict) -> None:
        route_key = f'{method} {path}'
        func = handlers.ROUTES.get(route_key)
        if func is None:
            self._send_json(build_response_payload(False, error=f'no route: {route_key}'), 404)
            return
        try:
            data = func(self.state, body or {})
            self._send_json(build_response_payload(True, data=data))
        except Exception as exc:  # noqa: BLE001 - surface every backend error to the UI
            self._send_json(build_response_payload(False, error=f'{type(exc).__name__}: {exc}'), 200)


def make_server(host='127.0.0.1', port=8000, app_state=None):
    """Build a console server bound to ``app_state``.

    ``app_state`` defaults to the module-level singleton.  The desktop shell
    passes its own state, which is what makes the two faces agree: a library
    opened in the desktop is the same library the browser console shows.
    """
    if app_state is None:
        return ThreadingHTTPServer((host, port), RequestHandler)
    handler = type('BoundRequestHandler', (RequestHandler,), {'state': app_state})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m tuner.web``."""
    import argparse

    parser = argparse.ArgumentParser(description='TunerLab web console')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)

    httpd = make_server(args.host, args.port)
    url = f'http://{args.host}:{args.port}/'
    print(f'TunerLab web console running at {url}')
    print('Press Ctrl+C to stop.')
    if not args.no_browser:
        # Give the server a moment, then open the default browser.
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
    finally:
        httpd.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
