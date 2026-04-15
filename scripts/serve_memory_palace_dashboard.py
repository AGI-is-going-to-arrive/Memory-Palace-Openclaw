#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.server
import socketserver
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    dashboard_root = Path(".")
    api_target = ""
    sse_target = ""

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def translate_path(self, path: str) -> str:
        parsed = urllib.parse.urlsplit(path)
        normalized = Path(parsed.path.lstrip("/"))
        candidate = (self.dashboard_root / normalized).resolve()
        try:
            candidate.relative_to(self.dashboard_root.resolve())
        except ValueError:
            candidate = self.dashboard_root / "index.html"
        return str(candidate)

    def _proxy_target(self) -> str | None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api" or parsed.path.startswith("/api/"):
            stripped = parsed.path[4:] or "/"
            target = self.api_target.rstrip("/") + stripped
            return f"{target}?{parsed.query}" if parsed.query else target
        if (
            parsed.path.startswith("/sse/messages")
            or parsed.path.startswith("/messages")
            or parsed.path.startswith("/sse")
        ):
            target = self.sse_target.rstrip("/") + parsed.path
            return f"{target}?{parsed.query}" if parsed.query else target
        return None

    def _forward(self) -> None:
        target_url = self._proxy_target()
        if target_url is None:
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(content_length) if content_length > 0 else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
        }
        headers.pop("Content-Length", None)
        request = urllib.request.Request(
            target_url,
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                self.send_response(int(getattr(response, "status", 200) or 200))
                for key, value in response.headers.items():
                    if key.lower() in HOP_BY_HOP_HEADERS or key.lower() == "content-length":
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read() if hasattr(exc, "read") else b""
            self.send_response(exc.code)
            for key, value in exc.headers.items():
                if key.lower() in HOP_BY_HOP_HEADERS or key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001
            self.send_error(502, explain=str(exc))

    def _serve_spa(self) -> None:
        requested = Path(self.translate_path(self.path))
        if requested.exists() and requested.is_file():
            return super().do_GET()
        index_path = self.dashboard_root / "index.html"
        if not index_path.is_file():
            self.send_error(404)
            return
        payload = index_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self._proxy_target() is not None:
            self._forward()
            return
        self._serve_spa()

    def do_HEAD(self) -> None:  # noqa: N802
        if self._proxy_target() is not None:
            self._forward()
            return
        self._serve_spa()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_PUT(self) -> None:  # noqa: N802
        self._forward()

    def do_PATCH(self) -> None:  # noqa: N802
        self._forward()

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._forward()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the packaged Memory Palace dashboard bundle.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--api-target", required=True)
    parser.add_argument("--sse-target")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Dashboard root does not exist: {root}")
    if not (root / "index.html").is_file():
        raise SystemExit(f"Dashboard bundle is missing index.html: {root}")

    DashboardHandler.dashboard_root = root
    DashboardHandler.api_target = str(args.api_target)
    DashboardHandler.sse_target = str(args.sse_target or args.api_target)

    server = ThreadingHTTPServer((str(args.host), int(args.port)), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
