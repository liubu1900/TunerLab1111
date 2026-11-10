"""Opening the browser console from the desktop shell.

The desktop shell and the web console are two faces of the same program:
they call the *same* backend handlers and, when the console is started from
inside the shell, they act on the *same* state — one controller handle, one
loaded calibration library, one operation log.  Whatever the operator does in
the browser is visible in the shell and the other way round.

The console is served on a free local port (never a fixed one: two copies of
the program must not fight over port 8000) and the default browser is opened
on it.  The server runs in a daemon thread; closing the shell stops it.
"""
from __future__ import annotations

import socket
import threading
import webbrowser

from tuner.web.server import make_server

__all__ = ['WebConsole', 'free_port']


def free_port(host: str = '127.0.0.1') -> int:
    """A port nobody is using on this machine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


class WebConsole:
    """The browser console, running inside the desktop process."""

    def __init__(self, app_state, host: str = '127.0.0.1'):
        self.state = app_state
        self.host = host
        self.port = 0
        self.url = ''
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, open_browser: bool = True) -> str:
        """Start the console; returns its URL.

        Already running? The existing URL is returned — starting a second
        console on the same state would only duplicate the same numbers in a
        second window.
        """
        if self.running:
            return self.url
        # Bind once: releasing a probe socket first lets another process claim
        # its port before the HTTP server can bind it (especially on Windows).
        self._server = make_server(self.host, 0, self.state)
        self.port = int(self._server.server_address[1])
        self.url = f'http://{self.host}:{self.port}/'
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name='tunerlab-web-console', daemon=True)
        self._thread.start()
        if open_browser:
            # Give the socket a moment to accept before the browser knocks.
            threading.Timer(0.4, lambda: webbrowser.open(self.url)).start()
        return self.url

    def stop(self) -> None:
        """Stop the console; safe to call when it is not running."""
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001 - stopping must never raise
                pass
            try:
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
        self.port = 0
        self.url = ''
