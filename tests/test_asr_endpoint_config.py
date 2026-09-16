"""ASR 端点配置与本地端点兼容性测试（M6）。

回归两个真实踩过的坑：
    1. **本地 ASR 服务不需要 API Key**（项目自带的 ``scripts/local_asr_server.py`` 不校验鉴权），
       但实现里硬要求 Key → 离线演示直接失败。改为：本机端点允许空 Key（不发 Authorization 头），
       远程端点仍强制要求。
    2. ``probe_asr_endpoint`` 的自检对空 Key 一律失败 → 本地服务明明起着却报「未填写 API Key」。
"""

from __future__ import annotations

import pytest

from ecnu_transcribe.config import AppConfig
from ecnu_transcribe.errors import AsrNotConfiguredError
from ecnu_transcribe.transcriber import OpenAICompatibleTranscriber, create_transcriber


def _cfg(base_url: str) -> AppConfig:
    c = AppConfig()
    c.asr_base_url = base_url
    c.asr_model = "test-model"
    c.asr_provider = "openai_compatible"
    return c


# --------------------------------------------------------------------------- #
# 本地端点允许空 Key
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8000/v1",
        "http://localhost:8000/v1",
        "http://localhost/v1",
        "http://0.0.0.0:9000/v1",
    ],
)
def test_local_endpoint_allows_empty_key(base_url):
    tr = OpenAICompatibleTranscriber(_cfg(base_url), "")
    assert tr.is_local_endpoint() is True
    assert tr.api_key == ""
    assert tr._auth_headers() == {}, "本地端点不应发送 Authorization 头"


def test_remote_endpoint_requires_key():
    with pytest.raises(AsrNotConfiguredError) as exc:
        OpenAICompatibleTranscriber(_cfg("https://dashscope.aliyuncs.com/compatible-mode/v1"), "")
    msg = str(exc.value)
    assert "API Key" in msg
    # 错误信息要给出可操作的建议
    assert "127.0.0.1" in msg


def test_remote_endpoint_with_key_sends_bearer():
    tr = OpenAICompatibleTranscriber(_cfg("https://api.example.com/v1"), "sk-test-12345")
    assert tr.is_local_endpoint() is False
    assert tr._auth_headers() == {"Authorization": "Bearer sk-test-12345"}


def test_lookalike_host_is_not_local():
    """``127.0.0.1.evil.com`` 这类域名不能被当成本机。"""
    tr = OpenAICompatibleTranscriber(_cfg("https://127.0.0.1.evil.com/v1"), "sk-x-123456")
    assert tr.is_local_endpoint() is False


def test_localhost_subdomain_is_not_local():
    tr = OpenAICompatibleTranscriber(_cfg("https://localhost.evil.com/v1"), "sk-x-123456")
    assert tr.is_local_endpoint() is False


def test_whitespace_key_treated_as_empty_for_remote():
    with pytest.raises(AsrNotConfiguredError):
        OpenAICompatibleTranscriber(_cfg("https://api.example.com/v1"), "   ")


def test_local_with_key_still_works():
    """本机端点填了 Key 也允许（有些本地服务会校验）。"""
    tr = OpenAICompatibleTranscriber(_cfg("http://127.0.0.1:8000/v1"), "sk-local-123456")
    assert tr._auth_headers() == {"Authorization": "Bearer sk-local-123456"}


# --------------------------------------------------------------------------- #
# 工厂 & 自检
# --------------------------------------------------------------------------- #
def test_create_transcriber_local_without_key(tmp_path):
    """配置成 openai_compatible + 本机 URL + 无 Key：工厂应能造出可用对象。"""
    from ecnu_transcribe.config import ConfigManager

    cfg = _cfg("http://127.0.0.1:8000/v1")
    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=tmp_path / "s.json")
    cm.load()
    tr = create_transcriber(cfg, cm=cm)
    assert tr.name == "openai_compatible"
    assert tr.is_local_endpoint() is True


def test_create_transcriber_remote_without_key_raises(tmp_path):
    from ecnu_transcribe.config import ConfigManager

    cfg = _cfg("https://api.example.com/v1")
    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=tmp_path / "s.json")
    cm.load()
    with pytest.raises(AsrNotConfiguredError):
        create_transcriber(cfg, cm=cm)


def test_probe_asr_endpoint_local_without_key_mentions_localhost(monkeypatch):
    """自检在空 Key + 非本机时，提示里要教用户写 127.0.0.1。"""
    from ecnu_transcribe.transcriber import probe_asr_endpoint

    ok, msg = probe_asr_endpoint(_cfg("https://api.example.com/v1"), "")
    assert ok is False
    assert "127.0.0.1" in msg


def test_probe_asr_endpoint_connection_error_is_reported():
    """本机端点没起服务时，自检要给出可读的连接错误而不是抛异常。"""
    from ecnu_transcribe.transcriber import probe_asr_endpoint

    cfg = _cfg("http://127.0.0.1:1/v1")  # 1 号端口不会有人监听
    cfg.request_timeout = 3.0
    ok, msg = probe_asr_endpoint(cfg, "")
    assert ok is False
    assert "无法连接" in msg or "HTTP" in msg
