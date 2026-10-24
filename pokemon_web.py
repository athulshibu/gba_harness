"""Local browser UI for the Pokemon harness; no third-party web framework."""

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import webbrowser

import requests


class Dashboard:
    def __init__(self, operator, emulator_url, port, terminal):
        self.operator = operator
        self.emulator_url = emulator_url.rstrip("/")
        self.terminal = terminal
        self.lock = threading.Lock()
        self.lines = deque(maxlen=2000)
        self.sequence = 0
        self.token = secrets.token_urlsafe(32)
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self.handler())
        self.server.daemon_threads = True
        self.thread = None

    def write(self, text):
        with self.lock:
            self.terminal.write(text)
            if text:
                self.sequence += 1
                self.lines.append((self.sequence, text))
        return len(text)

    def flush(self):
        self.terminal.flush()

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        url = f"http://127.0.0.1:{self.server.server_port}"
        print(f"Browser view: {url}", flush=True)
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            print("Open the browser view URL manually.", flush=True)

    def close(self):
        if self.thread is not None:
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()

    def handler(self):
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, status, body, content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def local_host(self):
                # Reject DNS rebinding. No cross-origin access is enabled.
                return self.headers.get("Host") == f"127.0.0.1:{dashboard.server.server_port}"

            def do_GET(self):
                if not self.local_host():
                    self.respond(403, b'{"error":"Invalid host"}')
                    return
                path = self.path.split("?", 1)[0]
                if path == "/":
                    page = Path(__file__).with_name("pokemon_web.html").read_text(encoding="utf-8")
                    self.respond(200, page.replace("__TOKEN__", dashboard.token).encode(), "text/html; charset=utf-8")
                elif path == "/events":
                    with dashboard.lock:
                        lines = list(dashboard.lines)
                    version, paused, stopped, _ = dashboard.operator.snapshot()
                    self.respond(200, json.dumps({"lines": lines, "version": version,
                                                  "paused": paused, "stopped": stopped}).encode())
                elif path == "/screen":
                    try:
                        # Independent session: never share the controller's Session.
                        with requests.Session() as session:
                            session.trust_env = False
                            response = session.get(dashboard.emulator_url + "/screen", timeout=(2, 3))
                            response.raise_for_status()
                        self.respond(200, response.content, "image/png")
                    except requests.RequestException:
                        self.respond(503, b'{"error":"Waiting for emulator"}')
                else:
                    self.respond(404, b'{"error":"Not found"}')

            def do_POST(self):
                if not self.local_host() or self.headers.get("X-Operator-Token") != dashboard.token:
                    self.respond(403, b'{"error":"Invalid control token"}')
                    return
                if self.path != "/control":
                    self.respond(404, b'{"error":"Not found"}')
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 16384:
                        raise ValueError("Message must be between 1 and 16384 bytes.")
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError("Expected a control object.")
                    message = data.get("message")
                    if isinstance(message, str):
                        dashboard.operator.interrupt_message(message)
                    elif data.get("command") in ("/pause", "/resume", "/quit"):
                        dashboard.operator.receive(data["command"])
                    else:
                        raise ValueError("Unknown command.")
                except (ValueError, UnicodeError) as exc:
                    self.respond(400, json.dumps({"error": str(exc)}).encode())
                    return
                self.respond(200, b'{"ok":true}')

        return Handler
