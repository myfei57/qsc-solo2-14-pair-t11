"""HTTP 服务器与静态页面分发。"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..core.errors import BreweryError, NotFoundError, ValidationError
from .api import ApiRouter
from .pages import page_file

LOGGER = logging.getLogger("breweryctl.http")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


class ConsoleServer:
    """封装 ThreadingHTTPServer 的启动与停止。"""

    def __init__(
        self,
        router: ApiRouter,
        web_dir: Path,
        host: str,
        port: int,
    ) -> None:
        self.router = router
        self.web_dir = Path(web_dir).resolve()
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> ThreadingHTTPServer:
        """创建并绑定监听套接字。"""

        handler = build_handler(self.router, self.web_dir)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        LOGGER.info("控制台监听 %s:%s", self.host, self.port)
        return self._httpd

    def serve_forever(self) -> None:
        """进入请求循环。"""

        httpd = self._httpd or self.start()
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
            self._httpd = None

    def stop(self) -> None:
        """停止请求循环。"""

        if self._httpd is not None:
            self._httpd.shutdown()

    @property
    def address(self) -> tuple[str, int]:
        """返回实际绑定地址。"""

        if self._httpd is None:
            return self.host, self.port
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)


def build_handler(router: ApiRouter, web_dir: Path) -> type[BaseHTTPRequestHandler]:
    """生成绑定到指定路由与页面目录的请求处理器。"""

    root = Path(web_dir).resolve()

    class ConsoleHandler(BaseHTTPRequestHandler):
        server_version = "BreweryCtl/1.0"
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            LOGGER.info("%s - %s", self.address_string(), format % args)

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path or "/"
            if path.startswith("/api/"):
                self._handle_api(method, path, parse_qs(parsed.query))
                return
            if method != "GET":
                self._send_json(405, {"error": "method_not_allowed", "path": path})
                return
            self._serve_static(path)

        def _handle_api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
            try:
                body = self._read_json() if method == "POST" else {}
            except BreweryError as exc:
                self._send_json(exc.http_status, exc.to_doc())
                return
            status, payload = router.handle(method, path, query, body)
            self._send_json(status, payload)

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError as exc:
                raise ValidationError("Content-Length 不合法") from exc
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大", length=length)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是合法 JSON") from exc
            if not isinstance(payload, dict):
                raise ValidationError("请求体根节点必须是对象")
            return payload

        def _serve_static(self, path: str) -> None:
            if path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path in ("/", ""):
                self._send_file(root / page_file("mash"))
                return
            if path.startswith("/static/"):
                relative = path[len("/static/") :]
                self._send_file(root / "static" / relative)
                return
            slug = path.strip("/")
            try:
                filename = page_file(slug)
            except NotFoundError:
                self._send_json(404, {"error": "not_found", "path": path})
                return
            self._send_file(root / filename)

        def _send_file(self, target: Path) -> None:
            resolved = target.resolve()
            if root not in resolved.parents and resolved != root:
                self._send_json(403, {"error": "forbidden", "path": str(target)})
                return
            if not resolved.exists() or not resolved.is_file():
                self._send_json(404, {"error": "not_found", "path": str(target)})
                return
            content = resolved.read_bytes()
            content_type = CONTENT_TYPES.get(resolved.suffix.lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", CONTENT_TYPES[".json"])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return ConsoleHandler
