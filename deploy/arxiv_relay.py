from __future__ import annotations

import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ALLOWED_HOSTS = {"arxiv.org", "export.arxiv.org"}
MAX_BYTES = 256 * 1024 * 1024


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "MaxReadArxivRelay/1.0"

    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/healthz":
            self._respond(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
            return
        if parsed.path != "/fetch":
            self._respond(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            return
        target = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
        try:
            data, content_type = self._fetch_target(target)
        except ValueError as exc:
            self._respond(HTTPStatus.BAD_REQUEST, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return
        except Exception:
            self._respond(HTTPStatus.BAD_GATEWAY, b"upstream unavailable\n", "text/plain; charset=utf-8")
            return
        self._respond(HTTPStatus.OK, data, content_type)

    def do_POST(self):
        self._respond(HTTPStatus.METHOD_NOT_ALLOWED, b"GET only\n", "text/plain; charset=utf-8")

    def _fetch_target(self, target: str):
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError("only arxiv.org and export.arxiv.org are allowed")
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise ValueError("unsupported upstream URL")
        request = urllib.request.Request(
            target,
            headers={
                "User-Agent": "MaxReadArxivRelay/1.0",
                "Accept": "*/*",
            },
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length > MAX_BYTES:
                raise ValueError("upstream response is too large")
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("upstream response is too large")
            return data, response.headers.get_content_type() or "application/octet-stream"

    def _respond(self, status, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class RelayServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = RelayServer(("10.214.232.141", 18080), RelayHandler)
    print("MaxRead arXiv relay: http://10.214.232.141:18080", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
