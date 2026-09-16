"""本地 OpenAI 兼容 ASR 服务（无需任何云 API Key）。

把 ``faster-whisper`` 包一层 HTTP，暴露与云端一致的接口：

    POST /v1/audio/transcriptions      ← 与 OpenAI / DashScope 兼容端点同形
    GET  /v1/models
    GET  /health

这样应用（以及任何 OpenAI 兼容客户端）只要把
``Base URL`` 指向 ``http://127.0.0.1:8000/v1``、``模型`` 填服务暴露的模型名，
就能**完全离线**转写，不需要阿里云/硅基流动等任何 Key。

用法::

    # 首次会自动下载模型（tiny ≈ 75MB，small ≈ 500MB，medium ≈ 1.5GB）
    .venv\\Scripts\\python scripts\\local_asr_server.py --model small --port 8000

    # 装了 faster-whisper 后，验证一下服务是否可用
    curl http://127.0.0.1:8000/v1/models

    # 让应用用它：设置 → 语音识别 → 类型选「OpenAI 兼容端点」
    #   Base URL = http://127.0.0.1:8000/v1
    #   模型     = faster-whisper-small

说明
----
* 只监听 ``127.0.0.1``（默认），不对外暴露；音频不离开本机。
* 有 CUDA 时自动用 GPU（``--device auto``），否则 CPU + int8 量化。
* 这是**可选**功能：不装 ``faster-whisper`` 时应用照样能用云端 ASR。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(ROOT / "src"))

_MODEL = None
_MODEL_LOCK = threading.Lock()
_MODEL_ARGS: dict[str, object] = {}


# --------------------------------------------------------------------------- #
#: CUDA 探测用的极短音频（0.5s 静音，16 kHz 单声道，faster-whisper 需要 float32 numpy 数组）
def _probe_audio():
    import numpy as np

    return np.zeros(8000, dtype=np.float32)


def _cuda_available(model_size: str = "tiny") -> tuple[bool, str]:
    """**真实跑一次 GPU 前向**，确认 CUDA 真的可用。

    坑点（踩过）：``ctranslate2.get_cuda_device_count()`` 在有 GPU 驱动时会返回 >0，
    但 pip 装的 ``ctranslate2`` 是 **CPU-only 构建**，缺少 ``cublas64_12.dll``；
    ``WhisperModel(device="auto")`` 于是也选 CUDA，错误直到**编码阶段**才爆：
    ``RuntimeError: Library cublas64_12.dll is not found or cannot be loaded``。
    表现是「服务能起、健康检查 OK、一转写就 500」，很难排查。
    所以这里在**加载模型时**就用一小段静音做真实前向，跑通才算 CUDA 可用。

    ``model_size`` 用 ``tiny`` 做探针（最小开销）；探测失败时立刻释放，不留驻内存。
    """
    try:
        import ctranslate2  # type: ignore
    except ImportError as exc:
        return False, f"未安装 ctranslate2：{exc}"
    try:
        count = int(ctranslate2.get_cuda_device_count())
    except Exception as exc:  # noqa: BLE001
        return False, f"查询 CUDA 设备数失败：{exc}"
    if count <= 0:
        return False, "未检测到 CUDA 设备"

    # 设备数 > 0 还不够：必须真跑一次前向，才能确认 cublas/cudnn 等运行库齐全
    try:
        from faster_whisper import WhisperModel  # type: ignore

        probe = WhisperModel(model_size, device="cuda", compute_type="int8_float16")
        try:
            segs, _info = probe.transcribe(_probe_audio(), language="zh", vad_filter=False, beam_size=1)
            list(segs)  # 必须消费生成器，前向才真正执行
        finally:
            del probe
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"CUDA 前向自检失败（{type(exc).__name__}: {str(exc)[:120]}）"


def resolve_device(device: str) -> tuple[str, str]:
    """把 ``auto`` 解析成**确定可用**的设备，返回 ``(device, compute_type)``。"""
    if device in ("auto", "cuda"):
        has_cuda, why = _cuda_available()
        if has_cuda:
            print("[asr] CUDA 自检通过，使用 GPU 推理（float16）", flush=True)
            return "cuda", "float16"
        if device == "cuda":
            print(f"[asr] ⚠ 指定了 cuda 但不可用（{why}），自动回退 CPU", flush=True)
        else:
            print(f"[asr] 使用 CPU 推理（{why}）", flush=True)
        import os

        threads = max(2, (os.cpu_count() or 4) - 1)
        print(f"[asr] CPU 线程数：{threads}（int8 量化）", flush=True)
        return "cpu", "int8"
    return "cpu", "int8"


def env_device_hint() -> str:
    """给用户看的一行环境提示（写进日志/启动横幅）。"""
    try:
        import ctranslate2  # type: ignore

        return f"ctranslate2 {getattr(ctranslate2, '__version__', '?')}，CUDA 设备数 {ctranslate2.get_cuda_device_count()}"
    except Exception as exc:  # noqa: BLE001
        return f"ctranslate2 信息不可用：{exc}"


def load_model(model_size: str, device: str, compute_type: str) -> None:
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None:
            return
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "未安装 faster-whisper。请先运行：\n"
                "  .venv\\Scripts\\python -m pip install faster-whisper\n"
                "（首次运行会自动下载模型；tiny 约 75MB，small 约 500MB）"
            ) from exc

        use_device, use_compute = resolve_device(device)
        if compute_type and compute_type != "auto":
            use_compute = compute_type
        _MODEL_ARGS["device"] = use_device
        _MODEL_ARGS["compute_type"] = use_compute

        print(
            f"[asr] 正在加载模型 {model_size}（device={use_device}, compute_type={use_compute}）…",
            flush=True,
        )
        t0 = time.time()
        try:
            _MODEL = WhisperModel(model_size, device=use_device, compute_type=use_compute)
        except Exception as exc:  # noqa: BLE001
            if use_device != "cpu":
                print(f"[asr] ⚠ {use_device} 加载失败（{exc}），回退 CPU + int8", flush=True)
                _MODEL = WhisperModel(model_size, device="cpu", compute_type="int8")
                _MODEL_ARGS["device"] = "cpu"
                _MODEL_ARGS["compute_type"] = "int8"
            else:
                raise
        print(f"[asr] 模型就绪，用时 {time.time() - t0:.1f}s", flush=True)


def transcribe_file(path: Path, *, language: str, want_timestamps: bool) -> dict:
    assert _MODEL is not None
    segments, info = _MODEL.transcribe(
        str(path),
        language=(language or None),
        vad_filter=True,
        beam_size=5,
        word_timestamps=False,
        condition_on_previous_text=False,  # 长音频更稳，减少重复幻觉
    )
    out_segments = []
    texts = []
    for seg in segments:
        text = str(seg.text or "").strip()
        if not text:
            continue
        start = float(seg.start or 0.0)
        end = float(seg.end or 0.0)
        out_segments.append({"id": len(out_segments), "start": start, "end": end, "text": text})
        texts.append(text)
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    payload = {
        "task": "transcribe",
        "language": str(getattr(info, "language", language) or ""),
        "duration": duration,
        "text": "".join(texts),
    }
    if want_timestamps:
        payload["segments"] = out_segments
    return payload


# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "LocalWhisperASR/1.0"
    protocol_version = "HTTP/1.1"

    # --- 工具 ------------------------------------------------------------- #
    def _json(self, code: int, obj: object) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, text: str, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A002
        print(f"[asr] {self.address_string()} {fmt % args}", flush=True)

    # --- 路由 ------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", ""):
            self._json(200, {"status": "ok", "model_loaded": _MODEL is not None, **_MODEL_ARGS})
        elif path == "/v1/models":
            name = f"faster-whisper-{_MODEL_ARGS.get('model', 'unknown')}"
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": name, "object": "model", "created": int(time.time()), "owned_by": "local"}
                    ],
                },
            )
        else:
            self._json(404, {"error": {"message": f"未知路径 {self.path}", "type": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/v1/audio/transcriptions", "/audio/transcriptions"):
            self._json(404, {"error": {"message": f"未知路径 {self.path}", "type": "not_found"}})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._json(400, {"error": {"message": "空请求体", "type": "invalid_request_error"}})
            return
        raw = self.rfile.read(length)

        try:
            fields, file_bytes, filename = _parse_multipart(raw, self.headers.get("Content-Type", ""))
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"error": {"message": f"multipart 解析失败：{exc}", "type": "invalid_request_error"}})
            return
        if not file_bytes:
            self._json(400, {"error": {"message": "缺少 file 字段", "type": "invalid_request_error"}})
            return

        language = str(fields.get("language") or "zh")
        response_format = str(fields.get("response_format") or "json")
        want_ts = response_format in ("verbose_json", "srt", "vtt")
        model_name = str(fields.get("model") or _MODEL_ARGS.get("model") or "small")
        _MODEL_ARGS["last_model_requested"] = model_name

        suffix = Path(filename or "audio.mp3").suffix or ".mp3"
        tmp = Path(tempfile.mkdtemp(prefix="local-asr-")) / f"in{suffix}"
        tmp.write_bytes(file_bytes)
        print(
            f"[asr] 收到音频 {filename or '(no name)'} {len(file_bytes)} bytes，"
            f"language={language} format={response_format}",
            flush=True,
        )
        try:
            with _MODEL_LOCK:
                payload = transcribe_file(tmp, language=language, want_timestamps=want_ts)
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            self._json(500, {"error": {"message": f"转写失败：{exc}", "type": "server_error"}})
            return
        finally:
            try:
                tmp.unlink(missing_ok=True)
                tmp.parent.rmdir()
            except OSError:
                pass

        if response_format == "text":
            self._text(200, payload["text"])
        elif response_format == "srt":
            self._text(200, _to_srt(payload.get("segments", [])), "application/x-subrip; charset=utf-8")
        else:
            self._json(200, payload)


def _to_srt(segments: list[dict]) -> str:
    def fmt(sec: float) -> str:
        sec = max(0.0, sec)
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int(round((sec - int(sec)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = [
        f"{i}\n{fmt(s['start'])} --> {fmt(s['end'])}\n{s['text']}\n"
        for i, s in enumerate(segments, 1)
    ]
    return "\n".join(blocks)


def _parse_multipart(raw: bytes, content_type: str) -> tuple[dict[str, str], bytes, str]:
    """multipart/form-data 解析（文本字段 + 文件字段）。

    **关键点**：分隔符只能出现在「行首」（前有 ``\\r\\n``，后跟 ``\\r\\n`` 或 ``--``）。
    不能对整段 body 做裸 ``split(boundary)`` —— 音频是二进制，里面完全可能恰好包含
    boundary 字节序列，那样会把文件内容切碎（踩过一次：症状是转写接口 500，
    排查半天才发现是解析层截断了音频）。
    """
    marker = "boundary="
    if marker not in content_type:
        raise ValueError("不是 multipart/form-data")
    boundary = content_type.split(marker, 1)[1].strip().strip('"').split(";")[0].strip()
    if not boundary:
        raise ValueError("boundary 为空")
    delim = b"--" + boundary.encode("ascii")

    def find_delimiter(start: int) -> int:
        """找**行首**的分隔符（而不是任意位置的字节序列）。"""
        pos = start
        while True:
            idx = raw.find(delim, pos)
            if idx < 0:
                return -1
            at_line_start = idx == 0 or raw[idx - 2 : idx] == b"\r\n" or raw[idx - 1 : idx] == b"\n"
            after = raw[idx + len(delim) : idx + len(delim) + 2]
            if at_line_start and after in (b"\r\n", b"--", b"\n"):
                return idx
            pos = idx + 1

    fields: dict[str, str] = {}
    file_bytes = b""
    filename = ""

    start = find_delimiter(0)
    if start < 0:
        raise ValueError("找不到起始分隔符")
    pos = start + len(delim)
    while pos < len(raw):
        nl = raw.find(b"\n", pos)
        if nl < 0:
            break
        pos = nl + 1
        if raw[pos : pos + 2] == b"--":  # 结束分隔符
            break
        head_end = raw.find(b"\r\n\r\n", pos)
        if head_end >= 0:
            body_start = head_end + 4
        else:
            head_end = raw.find(b"\n\n", pos)
            if head_end < 0:
                break
            body_start = head_end + 2

        next_delim = find_delimiter(body_start)
        if next_delim < 0:
            body_end = len(raw)
        else:
            body_end = next_delim
            while body_end > body_start and raw[body_end - 1 : body_end] in (b"\n", b"\r"):
                body_end -= 1

        head_text = raw[pos:head_end].decode("utf-8", "replace")
        name = ""
        for token in head_text.split(";"):
            token = token.strip()
            if token.startswith("name="):
                # 去掉引号前先切掉可能存在的换行，否则尾引号会留下
                name = token[5:].split("\r")[0].split("\n")[0].strip().strip('"')
            elif token.startswith("filename="):
                filename = token[9:].split("\r")[0].split("\n")[0].strip().strip('"')

        body = raw[body_start:body_end]
        if name == "file":
            file_bytes = body
        elif name:
            fields[name] = body.decode("utf-8", "replace")
        if next_delim < 0:
            break
        pos = next_delim + len(delim)
    return fields, file_bytes, filename


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="本地 OpenAI 兼容 ASR 服务（faster-whisper）")
    ap.add_argument("--model", default="small", help="模型规格：tiny/base/small/medium/large-v3 或本地模型目录")
    ap.add_argument("--device", default="auto", help="auto / cpu / cuda")
    ap.add_argument("--compute-type", default="auto", help="auto / int8 / int8_float16 / float16 / float32")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机）")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--preload", action="store_true", default=True, help="启动时即加载模型（默认开）")
    args = ap.parse_args()

    _MODEL_ARGS.update({"model": args.model})
    if args.preload:
        load_model(args.model, args.device, args.compute_type)
    else:
        _MODEL_ARGS.update({"device": args.device, "compute_type": args.compute_type})

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    name = f"faster-whisper-{args.model}"
    print("=" * 70)
    print("本地 ASR 服务已启动（OpenAI 兼容）")
    print(f"  Base URL : http://{args.host}:{args.port}/v1")
    print(f"  模型     : {name}")
    print(f"  设备     : {_MODEL_ARGS.get('device')} / {_MODEL_ARGS.get('compute_type')}")
    print(f"  健康检查 : http://{args.host}:{args.port}/health")
    print()
    print("在「大夏学堂转写助手」里这样配：")
    print("  设置 → 语音识别 → 类型 = OpenAI 兼容端点")
    print(f"  Base URL = http://{args.host}:{args.port}/v1")
    print(f"  模型     = {name}")
    print("  API Key  = （留空或随便填）")
    print("=" * 70)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
