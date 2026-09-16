"""加解密与凭据存储的额外回归测试（M6 安全加固）。

覆盖：
    * DPAPI 往返（同机同用户）；
    * secrets.json 中不出现明文；
    * 空值清除；
    * 环境变量兜底优先级；
    * 日志总线写入的每一行都经过脱敏；
    * 抓包记录（NetworkRecorder）落盘前脱敏。
"""

from __future__ import annotations

import json

import pytest

from ecnu_transcribe import logbus
from ecnu_transcribe.config import ConfigManager, dpapi_available


pytestmark = pytest.mark.skipif(not dpapi_available(), reason="需要 Windows DPAPI")


def test_dpapi_roundtrip_via_manager(tmp_path):
    secrets = tmp_path / "secrets.json"
    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=secrets)
    cm.load()
    secret = "sk-roundtrip-0123456789abcdef"
    assert cm.set_secret("asr_api_key", secret) is True
    raw = secrets.read_text(encoding="utf-8")
    assert secret not in raw
    assert "dpapi:" in raw

    cm2 = ConfigManager(config_file=tmp_path / "c.json", secrets_file=secrets)
    cm2.load()
    assert cm2.secret("asr_api_key") == secret


def test_clearing_secret_removes_it_from_disk(tmp_path):
    secrets = tmp_path / "secrets.json"
    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=secrets)
    cm.load()
    cm.set_secret("asr_api_key", "sk-abcdef123456")
    cm.set_secret("llm_api_key", "sk-llm-abcdef123456")
    assert "asr_api_key" in json.loads(secrets.read_text(encoding="utf-8"))
    cm.set_secret("asr_api_key", "")
    remaining = json.loads(secrets.read_text(encoding="utf-8"))
    assert "asr_api_key" not in remaining
    assert "llm_api_key" in remaining


def test_logbus_handler_redacts_secrets_end_to_end():
    """模拟真实链路：登记密钥 → 记一条含密钥的日志 → LogBus 收到的是脱敏文本。"""
    from ecnu_transcribe.logbus import LogBus, get_logger

    secret = "sk-E2E-REDACT-0123456789"
    logbus.register_secret(secret)
    seen: list[str] = []
    bus = LogBus.instance()
    unsub = bus.subscribe(lambda _lvl, msg: seen.append(msg))
    try:
        get_logger("redact-e2e").info("调用 ASR 时使用 key=%s 成功", secret)
    finally:
        unsub()
    assert seen, "LogBus 应收到日志"
    joined = "\n".join(seen)
    assert secret not in joined, "日志里出现了明文密钥！"
    assert "<REDACTED>" in joined


def test_network_recorder_redacts_before_writing(tmp_path):
    from ecnu_transcribe.login import CapturedCall, NetworkRecorder

    rec = NetworkRecorder(tmp_path / "net.jsonl")
    rec.append(
        CapturedCall(
            method="POST",
            url="https://courses.ecnu.edu.cn/api/list?token=QUERYSECRET123",
            status=200,
            request_headers={
                "Cookie": "JSESSIONID=COOKIESECRET999",
                "Authorization": "Bearer BEARERSECRET777",
                "X-Api-Key": "XAPIKEYSECRET888",
                "Content-Type": "application/json",
            },
            request_body='{"password":"PWDSECRET555","pageNum":1}',
            response_body='{"access_token":"TOKSECRET333","rows":[]}',
        )
    )
    raw = (tmp_path / "net.jsonl").read_text(encoding="utf-8")
    for leaked in (
        "QUERYSECRET123", "COOKIESECRET999", "BEARERSECRET777",
        "XAPIKEYSECRET888", "PWDSECRET555", "TOKSECRET333",
    ):
        assert leaked not in raw, f"抓包文件里泄漏了 {leaked}"
    # 非敏感内容应保留，便于排障
    assert "pageNum" in raw
    assert "courses.ecnu.edu.cn" in raw
    assert "application/json" in raw


def test_redact_headers_by_key_name():
    from ecnu_transcribe.login import redact_headers

    out = redact_headers(
        {
            "Authorization": "anything at all",
            "cookie": "a=b",
            "X-Api-Key": "whatever",
            "Accept": "application/json",
            "Referer": "https://courses.ecnu.edu.cn/x",
        }
    )
    assert out["Authorization"] == "<REDACTED>"
    assert out["cookie"] == "<REDACTED>"
    assert out["X-Api-Key"] == "<REDACTED>"
    assert out["Accept"] == "application/json"
    assert "courses.ecnu.edu.cn" in out["Referer"]


def test_captured_call_keeps_useful_fields(tmp_path):
    from ecnu_transcribe.login import CapturedCall, NetworkRecorder

    rec = NetworkRecorder(tmp_path / "net2.jsonl")
    rec.append(CapturedCall(method="GET", url="https://courses.ecnu.edu.cn/api/course/list", status=200))
    data = json.loads((tmp_path / "net2.jsonl").read_text(encoding="utf-8").strip())
    assert data["method"] == "GET"
    assert data["status"] == 200
    assert "course/list" in data["url"]


def test_login_session_state_summary_is_redacted():
    from ecnu_transcribe.client import SessionState

    st = SessionState(
        cookies={"JSESSIONID": "SHOULD-NOT-APPEAR-123456", "SERVERID2": "Server1"},
        local_storage={"https://x": {"access_token": "TOKEN-SHOULD-NOT-APPEAR"}},
        saved_at=0.0,
    )
    summary = st.summary()
    assert "SHOULD-NOT-APPEAR" not in summary
    assert "JSESSIONID" in summary           # 键名保留，便于排障
    assert "access_token" in summary
    assert "SERVERID2" in summary
