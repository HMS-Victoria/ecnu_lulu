"""可编排的假 ASR 服务：对**每个音频分段**返回指定文本。

用途：在完全离线的情况下验证长音频切分管线（`plan_chunks` 静音检测 → 分段切片 →
逐段 ASR → 时间轴平移合并去重），并且能**故意制造边界重叠**，检查拼接是否会
重复或丢内容 —— 这是长课程（1~2 小时）最容易出问题、也最难人工发现的环节。

每个分段音频会被转成指纹（时长 + 幅度特征），再用调用序号作为兜底键，
保证「第 N 次调用返回第 N 组文本」稳定可控。

用法::

    from fake_asr_server import FakeASRServer
    srv = FakeASRServer(port=8390, batches=[["第一段文本"], ["第二段文本"]])
    srv.start()
    # 让应用把 Base URL 指向 http://127.0.0.1:8390/v1
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def audio_fingerprint(path: Path) -> dict[str, float]:
    """算一个粗略的内容指纹（时长 / 均方根 / 峰值），用于日志与断言。"""
    try:
        with wave.open(str(path), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate() or 1
            raw = w.readframes(min(frames, rate * 600))
            width = w.getsampwidth()
    except Exception:
        return {"duration": 0.0, "rms": 0.0, "peak": 0.0}
    if width != 2 or not raw:
        return {"duration": 0.0, "rms": 0.0, "peak": 0.0}
    samples = [
        int.from_bytes(raw[i : i + 2], "little", signed=True) for i in range(0, len(raw) - 1, 2)
    ]
    if not samples:
        return {"duration": 0.0, "rms": 0.0, "peak": 0.0}
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
    peak = max(abs(s) for s in samples) / 32768.0
    return {"duration": frames / rate, "rms": round(rms, 5), "peak": round(peak, 5)}


class FakeASRServer:
    """按调用序号返回预设文本的 OpenAI 兼容 ASR 服务。"""

    def __init__(
        self,
        *,
        port: int = 0,
        batches: list[list[str]] | None = None,
        text_for_call=None,
        delay: float = 0.0,
        verbose: bool = False,
    ) -> None:
        self.port = port
        self.batches = batches or []
        self.text_for_call = text_for_call   # 可选：call_index -> list[str]
        self.delay = delay
        self.verbose = verbose
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self.base_url = ""
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def texts_for(self, index: int) -> list[str]:
        if self.text_for_call is not None:
            return list(self.text_for_call(index))
        if index < len(self.batches):
            return list(self.batches[index])
        return [f"第{index + 1}段（未预设）"]

    # ------------------------------------------------------------------ #
    def start(self) -> str:
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A002
                if srv.verbose:
                    print(f"[fake-asr] {fmt % args}", flush=True)

            def _json(self, code: int, obj: Any) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0].rstrip("/")
                if path in ("/health", ""):
                    self._json(200, {"status": "ok", "calls": srv.call_count})
                elif path == "/v1/models":
                    self._json(200, {"object": "list", "data": [{"id": "fake-asr", "object": "model"}]})
                else:
                    self._json(404, {"error": {"message": path}})

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?")[0].rstrip("/")
                if path not in ("/v1/audio/transcriptions", "/audio/transcriptions"):
                    self._json(404, {"error": {"message": path}})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                fields, blob, filename = _parse_multipart(raw, self.headers.get("Content-Type", ""))

                # 落一个临时文件，便于按真实时长生成时间戳
                tmp_dir = Path(tempfile.mkdtemp(prefix="fake-asr-"))
                tmp_path = tmp_dir / (filename or "audio.wav")
                try:
                    tmp_path.write_bytes(blob)
                except OSError:
                    pass

                with srv._lock:
                    index = len(srv.calls)
                    srv.calls.append(
                        {
                            "index": index,
                            "filename": filename,
                            "bytes": len(blob),
                            "language": fields.get("language", ""),
                            "model": fields.get("model", ""),
                            "response_format": fields.get("response_format", ""),
                        }
                    )
                if srv.delay:
                    time.sleep(srv.delay)

                texts = srv.texts_for(index)
                # 时间戳要按**该分段的真实时长**均分，否则合并后时间轴会越界
                # （曾经用固定的 10.0s 兜底，导致 5s 的分段被报成 10s）。
                dur = 0.0
                try:
                    with wave.open(str(tmp_path), "rb") as w:
                        if w.getframerate():
                            dur = w.getnframes() / float(w.getframerate())
                except Exception:
                    dur = 0.0
                if dur <= 0:
                    # 非 wav 时退回按字节率估算（16kHz 单声道 mp3 ≈ 8KB/s）
                    dur = max(1.0, len(blob) / 8000.0)
                # 真实端点会把整段语音切成若干句，**时间戳落在整段范围内**。
                # 这里按句数均分时长，让每句只覆盖自己那一小段（而不是每句都覆盖整段）——
                # 后者会让「切分合并」测试里的片段互相重叠，误判成重复而被去重丢掉。
                step = dur / max(1, len(texts))
                segments = [
                    {"id": i, "start": i * step, "end": (i + 1) * step, "text": t}
                    for i, t in enumerate(texts)
                ]
                payload = {
                    "task": "transcribe",
                    "language": fields.get("language", "zh"),
                    "duration": dur,
                    "text": "".join(texts),
                    "segments": segments,
                }
                fmt = fields.get("response_format", "json")
                if fmt == "text":
                    body = payload["text"].encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif fmt == "srt":
                    from ecnu_transcribe.transcriber import Segment

                    segs = [Segment(s["start"], s["end"], s["text"]) for s in segments]
                    out = "\n".join(
                        f"{i}\n{_srt(s.start)} --> {_srt(s.end)}\n{s.text}\n"
                        for i, s in enumerate(segs, 1)
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-subrip; charset=utf-8")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                else:
                    self._json(200, payload)

                try:
                    tmp_path.unlink(missing_ok=True)
                    tmp_dir.rmdir()
                except OSError:
                    pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._httpd.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None


def _srt(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_multipart(raw: bytes, content_type: str) -> tuple[dict[str, str], bytes, str]:
    """与 local_asr_server 相同的「行首分隔符」解析（避免二进制被误切）。"""
    marker = "boundary="
    if marker not in content_type:
        raise ValueError("not multipart")
    boundary = content_type.split(marker, 1)[1].strip().strip('"').split(";")[0].strip()
    delim = b"--" + boundary.encode("ascii")

    def find_delim(start: int) -> int:
        pos = start
        while True:
            idx = raw.find(delim, pos)
            if idx < 0:
                return -1
            at_start = idx == 0 or raw[idx - 2 : idx] == b"\r\n" or raw[idx - 1 : idx] == b"\n"
            after = raw[idx + len(delim) : idx + len(delim) + 2]
            if at_start and after in (b"\r\n", b"--", b"\n"):
                return idx
            pos = idx + 1

    fields: dict[str, str] = {}
    blob = b""
    filename = ""
    start = find_delim(0)
    if start < 0:
        raise ValueError("no start boundary")
    pos = start + len(delim)
    while pos < len(raw):
        nl = raw.find(b"\n", pos)
        if nl < 0:
            break
        pos = nl + 1
        if raw[pos : pos + 2] == b"--":
            break
        head_end = raw.find(b"\r\n\r\n", pos)
        body_start = head_end + 4 if head_end >= 0 else -1
        if body_start < 0:
            break
        nxt = find_delim(body_start)
        body_end = nxt if nxt >= 0 else len(raw)
        while body_end > body_start and raw[body_end - 1 : body_end] in (b"\n", b"\r"):
            body_end -= 1
        head = raw[pos:head_end].decode("utf-8", "replace")
        name = ""
        for token in head.split(";"):
            token = token.strip()
            if token.startswith("name="):
                name = token[5:].split("\r")[0].split("\n")[0].strip().strip('"')
            elif token.startswith("filename="):
                filename = token[9:].split("\r")[0].split("\n")[0].strip().strip('"')
        body = raw[body_start:body_end]
        if name == "file":
            blob = body
        elif name:
            fields[name] = body.decode("utf-8", "replace")
        if nxt < 0:
            break
        pos = nxt + len(delim)
    return fields, blob, filename
