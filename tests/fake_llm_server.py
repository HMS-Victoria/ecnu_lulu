"""OpenAI 兼容的假 LLM 服务（用于测试 `ecnu_transcribe.llm` 的后处理逻辑）。

后处理最危险的地方不是「调用失败」，而是**模型返回了不合规的内容却没被发现**：
    * 条目数对不上 → 文本与时间轴错位（字幕内容与画面不符）；
    * 返回乱码 / 多余解释文字 → 直接写进产物；
    * 返回 HTML 或超长内容 → 产物被污染。
所以这里提供「按调用序号返回任意预设内容」的能力，方便构造畸形响应。

用法::

    with FakeLLM(replies=['{"segments":[{"i":0,"text":"改好的"}]}']) as srv:
        cfg.llm_base_url = srv.base_url
        ...
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class FakeLLM:
    """可编排的 chat/completions 端点。

    ``replies``：按调用序号返回的字符串列表（字符串直接作为 message.content）。
    ``reply_for``：可选，``(index, request_payload) -> str``，优先级高于 replies。
    ``status_for``：可选，``(index) -> int``，用来模拟 401/429/500。
    """

    def __init__(
        self,
        *,
        replies: list[str] | None = None,
        reply_for: Callable[[int, dict[str, Any]], str] | None = None,
        status_for: Callable[[int], int] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.replies = replies or []
        self.reply_for = reply_for
        self.status_for = status_for
        self.delay = delay
        self.requests: list[dict[str, Any]] = []
        self.base_url = ""
        self._httpd: ThreadingHTTPServer | None = None

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def content_for(self, index: int, payload: dict[str, Any]) -> str:
        if self.reply_for is not None:
            return self.reply_for(index, payload)
        if index < len(self.replies):
            return self.replies[index]
        return json.dumps({"segments": []}, ensure_ascii=False)

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: A003
                pass

            def do_GET(self) -> None:  # noqa: N802
                body = json.dumps({"data": [{"id": "fake-llm"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    payload = {}
                with threading.Lock():
                    index = len(outer.requests)
                    outer.requests.append(payload)
                if outer.delay:
                    import time

                    time.sleep(outer.delay)
                status = outer.status_for(index) if outer.status_for else 200

                if status != 200:
                    err = json.dumps({"error": {"message": f"http {status}"}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return

                content = outer.content_for(index, payload)
                body = json.dumps(
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion",
                        "model": payload.get("model", "fake"),
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
                        "usage": {"total_tokens": 1},
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self.base_url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
