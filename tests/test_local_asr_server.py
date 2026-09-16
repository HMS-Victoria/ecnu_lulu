"""本地 OpenAI 兼容 ASR 服务测试（M6）。

重点回归两个**真实踩过的坑**：
    1. multipart 解析若对整段 body 做裸 ``split(boundary)``，二进制音频里恰好包含
       boundary 字节序列时会把文件切碎 → 服务端 500。必须只在**行首**识别分隔符。
    2. ``WhisperModel(device="auto")`` 在 CPU-only 的 ctranslate2 上也会选 CUDA，
       错误直到编码阶段才爆（``cublas64_12.dll not found``）；
       必须用**真实前向自检**来决定设备。

这两个测试都**不加载模型、不联网**。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_server_module():
    """从 scripts/ 载入 local_asr_server 模块（它不是包的一部分）。"""
    path = ROOT / "scripts" / "local_asr_server.py"
    spec = importlib.util.spec_from_file_location("local_asr_server", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["local_asr_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def srv():
    return _load_server_module()


# --------------------------------------------------------------------------- #
# multipart 解析
# --------------------------------------------------------------------------- #
def _build_multipart(boundary: str, fields: dict[str, str], file_name: str, file_bytes: bytes) -> tuple[bytes, str]:
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def test_multipart_basic(srv):
    boundary = "----WebKitFormBoundaryABC123"
    raw, ctype = _build_multipart(
        boundary, {"model": "whisper-1", "language": "zh"}, "a.mp3", b"\x00\x01\x02BINARYDATA"
    )
    fields, file_bytes, filename = srv._parse_multipart(raw, ctype)
    assert fields["model"] == "whisper-1"
    assert fields["language"] == "zh"
    assert file_bytes == b"\x00\x01\x02BINARYDATA"
    assert filename == "a.mp3"


def test_multipart_survives_boundary_like_bytes_in_file(srv):
    """回归：文件内容里恰好出现 boundary 字节序列时，不能被切碎。"""
    boundary = "----WebKitFormBoundaryXYZ"
    # 构造一段「看起来像分隔符」的音频字节（真实音频里完全可能出现）
    tricky = b"\x00\xff" + f"--{boundary}".encode("ascii") + b"\x00audio-continues-here" + b"\xfb\xfc"
    payload = b"HEAD" + tricky + b"TAIL"
    raw, ctype = _build_multipart(boundary, {"model": "m"}, "tricky.mp3", payload)
    fields, file_bytes, filename = srv._parse_multipart(raw, ctype)
    assert file_bytes == payload, "文件内容被 boundary 误切了"
    assert filename == "tricky.mp3"
    assert fields["model"] == "m"


def test_multipart_large_binary_roundtrip(srv):
    boundary = "----BoundaryLarge"
    # 1 MB 伪随机二进制（含大量可能的边界样式字节）
    import hashlib

    blob = hashlib.sha256(b"seed").digest() * (1024 * 1024 // 32)
    raw, ctype = _build_multipart(boundary, {"response_format": "verbose_json"}, "big.wav", blob)
    fields, file_bytes, filename = srv._parse_multipart(raw, ctype)
    assert len(file_bytes) == len(blob)
    assert file_bytes == blob
    assert fields["response_format"] == "verbose_json"


def test_multipart_rejects_non_multipart(srv):
    with pytest.raises(ValueError):
        srv._parse_multipart(b"whatever", "application/json")


def test_multipart_handles_quoted_boundary(srv):
    boundary = "----QuotedBoundary"
    raw, ctype = _build_multipart(boundary, {"model": "m"}, "q.mp3", b"DATA")
    _, file_bytes, _ = srv._parse_multipart(raw, ctype.replace(boundary, f'"{boundary}"'))
    assert file_bytes == b"DATA"


# --------------------------------------------------------------------------- #
# SRT 生成
# --------------------------------------------------------------------------- #
def test_to_srt_format(srv):
    out = srv._to_srt(
        [
            {"id": 0, "start": 0.0, "end": 4.2, "text": "第一句。"},
            {"id": 1, "start": 4.2, "end": 12.5, "text": "第二句。"},
        ]
    )
    assert out.startswith("1\n00:00:00,000 --> 00:00:04,200\n第一句。")
    assert "2\n00:00:04,200 --> 00:00:12,500\n第二句。" in out


def test_to_srt_empty(srv):
    assert srv._to_srt([]) == ""


# --------------------------------------------------------------------------- #
# 设备选择
# --------------------------------------------------------------------------- #
def test_resolve_device_explicit_cpu(srv):
    device, compute = srv.resolve_device("cpu")
    assert device == "cpu"
    assert compute == "int8"


@pytest.mark.slow
def test_resolve_device_auto_returns_usable_device(srv):
    """auto 必须返回**确定可用**的设备；本机无 CUDA 运行库时应落到 cpu。

    标记为 ``slow``：CUDA 自检会真的加载一次 tiny 模型做前向（几秒~几十秒）。
    默认跑：``pytest -m "not slow"``；要跑全部：``pytest -m ""``。
    """
    device, compute = srv.resolve_device("auto")
    assert device in ("cpu", "cuda")
    assert compute in ("int8", "float16")


def test_env_device_hint_never_raises(srv):
    hint = srv.env_device_hint()
    assert isinstance(hint, str) and hint


def test_cuda_probe_never_raises(srv):
    """CUDA 自检只返回 (bool, reason)，绝不向上抛异常。"""
    ok, why = srv._cuda_available()
    assert isinstance(ok, bool)
    assert isinstance(why, str)
    if not ok:
        assert why, "不可用时必须给出原因"


def test_probe_audio_is_float32_mono(srv):
    import numpy as np

    a = srv._probe_audio()
    assert isinstance(a, np.ndarray)
    assert a.dtype == np.float32
    assert a.ndim == 1
    assert len(a) == 8000


# --------------------------------------------------------------------------- #
# 服务端路由（不加载模型：模型未就绪时也要给出结构化响应）
# --------------------------------------------------------------------------- #
def test_handler_class_routes_are_registered(srv):
    assert hasattr(srv.Handler, "do_GET")
    assert hasattr(srv.Handler, "do_POST")
    assert srv.Handler.server_version.startswith("LocalWhisperASR")
