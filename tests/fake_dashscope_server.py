"""假 DashScope（阿里云百炼）原生异步 ASR 服务。

用于离线验证 ``DashScopeTranscriber._transcribe_native()`` 的完整链路：

    GET  /api/v1/uploads?action=getPolicy      → 上传凭证
    POST {upload_host}                          → OSS 上传（本项目用本地端点模拟）
    POST /api/v1/services/audio/asr/transcription → 提交异步任务，返回 task_id
    GET  /api/v1/tasks/{task_id}                → 轮询（可配置前 N 次 PENDING）
    GET  {transcription_url}                    → 取转写结果

这个链路是**长课程推荐模式**，但此前完全没有测试覆盖；它也是最容易因为
「接口结构理解错」而失效的地方（凭证字段名、task_id 位置、结果结构、轮询状态机）。

用法::

    srv = FakeDashScope(pending_polls=2)
    srv.start()
    os.environ["ECNU_DASHSCOPE_NATIVE_BASE"] = srv.api_base
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class FakeDashScope:
    """可编排的假 DashScope 服务（uploads + 提交 + 轮询 + 结果）。"""

    def __init__(
        self,
        *,
        pending_polls: int = 2,
        final_status: str = "SUCCEEDED",
        sentences: list[str] | None = None,
        fail_upload_policy: bool = False,
        fail_submit: bool = False,
        auth_error: int = 0,
        omit_task_id: bool = False,
        delay_ms: int = 10,
        legacy_policy_keys: bool = False,
    ) -> None:
        self.pending_polls = pending_polls
        self.final_status = final_status
        self.sentences = sentences or ["第一句话。", "第二句话。", "第三句话。"]
        #: 设为 True 时结果里**只给整段 text、不给 sentences**（模拟句级结果缺失）
        self.omit_sentences = False
        #: 句级结果缺失时用的整段文本
        self.plain_text = "整段文本兜底内容。"
        self.fail_upload_policy = fail_upload_policy
        self.fail_submit = fail_submit
        self.auth_error = auth_error
        self.omit_task_id = omit_task_id
        self.delay_ms = delay_ms
        #: 上传凭证的字段名风格。**实测真实接口用** ``oss_access_key_id`` /
        #: ``x_oss_object_acl``；早先这里的假服务写的是 ``access_key_id``，
        #: 于是"字段名写错"这个真 bug 一直测不出来（缺陷 55）。
        #: 置 True 可复现旧风格，用来验证向后兼容。
        self.legacy_policy_keys = legacy_policy_keys

        self.uploaded: list[dict[str, Any]] = []
        self.submitted: list[dict[str, Any]] = []
        #: 收到的 /chat/completions 请求体（千问 ASR 走这条路，缺陷 55）
        self.chat_requests: list[dict[str, Any]] = []
        #: /chat/completions 返回的文本
        self.chat_text = "这是千问 ASR 返回的文本。"
        #: 模拟真实的「单请求时长上限」：超过该秒数就回 400 The audio is too long（0 = 不限）
        self.max_audio_seconds = 0.0
        self.poll_count = 0
        self.result_fetches = 0
        self.task_id = "task-fake-0001"
        self.api_base = ""
        self.upload_host = ""
        self._httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ #
    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: A003
                pass

            # -- helpers -------------------------------------------------- #
            def _json(self, code: int, obj: Any) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _throttle(self) -> None:
                if outer.delay_ms:
                    time.sleep(outer.delay_ms / 1000.0)

            # -- routes --------------------------------------------------- #
            def do_GET(self) -> None:  # noqa: N802
                raw_path = self.path.split("?")[0]
                # 去掉 /api/v1 前缀后再匹配路由（结果 URL 是 api_base 拼出来的）
                path = raw_path
                prefix = "/api/v1"
                if path.startswith(prefix):
                    path = path[len(prefix):] or "/"
                self._throttle()

                if path.endswith("/uploads"):
                    if outer.auth_error:
                        self._json(outer.auth_error, {"code": "InvalidApiKey", "message": "bad key"})
                        return
                    if outer.fail_upload_policy:
                        self._json(200, {"request_id": "r1", "data": {}})
                        return
                    self._json(200, {
                        "request_id": "r1",
                        "data": {
                            "upload_host": outer.upload_host,
                            "upload_dir": "dashscope/asr",
                            # 真实接口的字段名（实测 2026-09-14）；旧风格可用
                            # legacy_policy_keys=True 复现，用于验证向后兼容
                            **(
                                {"access_key_id": "FAKE_AK"}
                                if outer.legacy_policy_keys
                                else {
                                    "oss_access_key_id": "FAKE_AK",
                                    "x_oss_object_acl": "private",
                                    "x_oss_forbid_overwrite": "true",
                                }
                            ),
                            "policy": "FAKE_POLICY",
                            "signature": "FAKE_SIG",
                            "expire_in_seconds": 3600,
                            "max_file_size_mb": 1000,
                        },
                    })
                    return

                if "/tasks/" in path:
                    outer.poll_count += 1
                    if outer.poll_count <= outer.pending_polls:
                        self._json(200, {"output": {"task_id": outer.task_id, "task_status": "RUNNING"}})
                        return
                    if outer.final_status != "SUCCEEDED":
                        self._json(200, {"output": {"task_id": outer.task_id, "task_status": outer.final_status,
                                                    "message": "asr failed: unsupported format"}})
                        return
                    self._json(200, {
                        "output": {
                            "task_id": outer.task_id,
                            "task_status": "SUCCEEDED",
                            "results": [{
                                "file_url": "oss://dashscope/asr/audio.mp3",
                                "transcription_url": f"{outer.api_base}/results/{outer.task_id}.json",
                            }],
                        }
                    })
                    return

                if path.startswith("/results/"):
                    outer.result_fetches += 1
                    if outer.omit_sentences:
                        # 句级结果缺失：只有整段 text
                        self._json(200, {"transcripts": [{"channel_id": 0, "text": outer.plain_text}]})
                        return
                    sentences = [
                        {"begin_time": i * 2000, "end_time": (i + 1) * 2000, "text": t}
                        for i, t in enumerate(outer.sentences)
                    ]
                    # 整段 text 按句级内容拼接（当句级 text 为空时它也会为空 —— 真实接口同理）
                    merged_text = "".join(t.strip() for t in outer.sentences if t.strip())
                    if not merged_text:
                        merged_text = outer.plain_text
                    self._json(200, {"transcripts": [{"channel_id": 0, "text": merged_text,
                                                      "sentences": sentences}]})
                    return

                self._json(404, {"message": f"no route {path}"})

            def do_POST(self) -> None:  # noqa: N802
                raw_path = self.path.split("?")[0]
                path = raw_path
                prefix = "/api/v1"
                if path.startswith(prefix):
                    path = path[len(prefix):] or "/"
                raw = self._read()
                self._throttle()

                # OSS 上传（multipart，含 file 字段）
                if path.startswith("/oss-upload"):
                    fields: dict[str, str] = {}
                    size = 0
                    boundary = ""
                    ctype = self.headers.get("Content-Type", "")
                    if "boundary=" in ctype:
                        boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
                    if boundary:
                        delim = b"--" + boundary.encode()
                        for part in raw.split(delim):
                            if b"\r\n\r\n" not in part:
                                continue
                            head, body = part.split(b"\r\n\r\n", 1)
                            head_text = head.decode("utf-8", "replace")
                            name = ""
                            for token in head_text.split(";"):
                                token = token.strip()
                                if token.startswith("name="):
                                    name = token[5:].split("\r")[0].strip().strip('"')
                            if name == "file":
                                size = len(body.rstrip(b"\r\n"))
                            elif name:
                                fields[name] = body.rstrip(b"\r\n").decode("utf-8", "replace")
                    outer.uploaded.append({"fields": fields, "file_bytes": size, "raw_len": len(raw)})
                    if not fields.get("key") or not fields.get("OSSAccessKeyId"):
                        self._json(400, {"message": "missing oss fields"})
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if path.endswith("/chat/completions"):
                    # 千问/Fun ASR 的**唯一可用**调用方式（缺陷 55 实测）。
                    # 真实服务要求 content 里只有音频项、data 是 data URL。
                    payload = json.loads(raw.decode("utf-8", "replace") or "{}")
                    outer.chat_requests.append(payload)
                    content = ((payload.get("messages") or [{}])[0] or {}).get("content")
                    if not isinstance(content, list) or not content:
                        self._json(400, {"error": {"message": "content must be a list"}})
                        return
                    types = [c.get("type") for c in content if isinstance(c, dict)]
                    if types != ["input_audio"]:
                        # 真实服务原文：The dedicated task `asr` ... does not support this input
                        self._json(400, {"error": {"message": (
                            "The dedicated task `asr` corresponding to the current service "
                            "does not support this input."
                        )}})
                        return
                    data = str((content[0].get("input_audio") or {}).get("data") or "")
                    if not data.startswith("data:audio/"):
                        # 真实服务原文：The provided URL does not appear to be valid
                        self._json(400, {"error": {"message": (
                            "The provided URL does not appear to be valid. Ensure it is correctly formatted."
                        )}})
                        return
                    # 模拟真实的「音频过长」拒绝：按 base64 体积估算时长（64 kbps ⇒ 8 KB/s）
                    if outer.max_audio_seconds:
                        import base64 as _b64
                        try:
                            raw_audio = _b64.b64decode(data.split(",", 1)[1])
                            approx_sec = len(raw_audio) / 8000.0
                        except Exception:  # noqa: BLE001
                            approx_sec = 0.0
                        if approx_sec > outer.max_audio_seconds:
                            self._json(400, {"error": {"message": (
                                "<400> InternalError.Algo.InvalidParameter: The audio is too long"
                            )}, "code": "invalid_request_error"})
                            return
                    self._json(200, {
                        "choices": [{"message": {"role": "assistant", "content": outer.chat_text}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    })
                    return

                if path.endswith("/services/audio/asr/transcription"):
                    if outer.fail_submit:
                        self._json(400, {"code": "InvalidParameter", "message": "bad model"})
                        return
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except Exception:
                        payload = {}
                    outer.submitted.append({
                        "payload": payload,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    })
                    out: dict[str, Any] = {"task_status": "PENDING"}
                    if not outer.omit_task_id:
                        out["task_id"] = outer.task_id
                    self._json(200, {"request_id": "r2", "output": out})
                    return

                self._json(404, {"message": f"no route {path}"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        port = self._httpd.server_address[1]
        self.api_base = f"http://127.0.0.1:{port}/api/v1"
        self.upload_host = f"http://127.0.0.1:{port}/oss-upload"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self.api_base

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
