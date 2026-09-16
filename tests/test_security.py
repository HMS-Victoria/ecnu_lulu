"""安全 / 脱敏 / 配置测试（M6 + DoD 第 5 条）。

DoD 要求：工作区无明文密码、无明文 Cookie、无 token 提交。
本文件用**构造的假凭据**验证日志与网络记录的脱敏链路真的生效。
"""

from __future__ import annotations

import json

import pytest

from ecnu_transcribe import logbus
from ecnu_transcribe.config import AppConfig, ConfigManager, dpapi_available


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
def test_redact_cookie_header():
    out = logbus.redact("Cookie: JSESSIONID=ABCDEF123456; SESSION=zzz")
    assert "ABCDEF123456" not in out
    assert "<REDACTED>" in out


def test_redact_authorization_bearer():
    out = logbus.redact("Authorization: Bearer sk-abcdef0123456789")
    assert "sk-abcdef0123456789" not in out


def test_redact_json_token_fields():
    payload = json.dumps({"access_token": "tok_1234567890", "user": "u"})
    out = logbus.redact(payload)
    assert "tok_1234567890" not in out
    assert "access_token" in out  # 键名保留，便于定位


def test_redact_query_token():
    out = logbus.redact("https://x/y?token=secretvalue123&a=1")
    assert "secretvalue123" not in out


def test_redact_phone_and_id():
    out = logbus.redact("联系电话 13800138000，身份证 11010119900307123X")
    assert "13800138000" not in out
    assert "11010119900307123X" not in out


def test_redact_student_id():
    out = logbus.redact("学号 20261234567 已登录")
    assert "20261234567" not in out


def test_redact_does_not_mangle_ordinary_paths():
    """回归：普通句子里的英文单词（如 storage_state）不应被误伤。"""
    text = "storage_state = D:\\app\\storage_state.json"
    out = logbus.redact(text)
    assert "D:\\app\\storage_state.json" in out


def test_register_secret_literal():
    logbus.register_secret("my-super-secret-key")
    out = logbus.redact("调用失败, key=my-super-secret-key 无效")
    assert "my-super-secret-key" not in out
    assert "<REDACTED>" in out


def test_redact_short_secret_not_registered():
    logbus.register_secret("ab")  # 太短，不予登记（避免误伤普通文本）
    assert "ab" in logbus.redact("about")


def test_logbus_captures_and_redacts():
    bus = logbus.LogBus.instance()
    seen: list[tuple[str, str]] = []
    unsub = bus.subscribe(lambda lvl, msg: seen.append((lvl, msg)))
    try:
        logger = logbus.get_logger("test")
        logger.info("token=%s", "abcdef1234567890")
    finally:
        unsub()
    assert seen, "LogBus 应收到日志"
    joined = "\n".join(m for _l, m in seen)
    assert "abcdef1234567890" not in joined


def test_logbus_unsubscribe():
    bus = logbus.LogBus.instance()
    seen: list[str] = []
    cb = lambda _l, m: seen.append(m)  # noqa: E731
    unsub = bus.subscribe(cb)
    bus.unsubscribe(cb)
    logbus.get_logger("test").info("after-unsubscribe")
    assert all("after-unsubscribe" not in m for m in seen)


# --------------------------------------------------------------------------- #
# 配置 / 凭据
# --------------------------------------------------------------------------- #
def test_config_roundtrip(tmp_path):
    cfg_file = tmp_path / "config.json"
    cm = ConfigManager(config_file=cfg_file, secrets_file=tmp_path / "secrets.json")
    cfg = cm.load()
    cfg.output_dir = str(tmp_path / "out")
    cfg.concurrency = 1
    cfg.asr_model = "paraformer-v2"
    cm.save(cfg)

    cm2 = ConfigManager(config_file=cfg_file, secrets_file=tmp_path / "secrets.json")
    cfg2 = cm2.load()
    assert cfg2.output_dir == str(tmp_path / "out")
    assert cfg2.concurrency == 1
    assert cfg2.asr_model == "paraformer-v2"


def test_config_ignores_unknown_keys(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"asr_model": "m", "brand_new_key": 1}), encoding="utf-8")
    cm = ConfigManager(config_file=cfg_file, secrets_file=tmp_path / "s.json")
    cfg = cm.load()
    assert cfg.asr_model == "m"
    assert cfg.extra.get("brand_new_key") == 1


def test_config_survives_corrupt_file(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{not json", encoding="utf-8")
    cm = ConfigManager(config_file=cfg_file, secrets_file=tmp_path / "s.json")
    cfg = cm.load()
    assert isinstance(cfg, AppConfig)


def test_secrets_are_not_plaintext_on_disk(tmp_path):
    """核心断言：写入磁盘的 secrets.json 里不能出现明文 Key。"""
    secrets = tmp_path / "secrets.json"
    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=secrets)
    cm.load()
    fake_key = "sk-PLAINTEXT-MUST-NOT-APPEAR-0123456789"
    persisted = cm.set_secret("asr_api_key", fake_key)
    cm.save(cm.cfg)

    if persisted:
        assert secrets.is_file()
        raw = secrets.read_text(encoding="utf-8")
        assert fake_key not in raw, "密钥以明文落盘了！"
        assert "dpapi:" in raw
        # 同机同用户应能解回来
        cm2 = ConfigManager(config_file=tmp_path / "c.json", secrets_file=secrets)
        cm2.load()
        assert cm2.secret("asr_api_key") == fake_key
    else:
        # DPAPI 不可用时：不落盘，仅内存
        assert not secrets.is_file() or fake_key not in secrets.read_text(encoding="utf-8")


def test_secret_env_fallback(tmp_path, monkeypatch):
    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=tmp_path / "s.json")
    cm.load()
    monkeypatch.setenv("ECNU_ASR_API_KEY", "env-key-123456")
    assert cm.secret("asr_api_key") == "env-key-123456"


def test_clear_secret(tmp_path):
    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=tmp_path / "s.json")
    cm.load()
    cm.set_secret("asr_api_key", "k-1234567890")
    cm.set_secret("asr_api_key", "")
    assert cm.secret("asr_api_key") == "" or cm.secret("asr_api_key") == ""


def test_dpapi_availability_is_boolean():
    assert isinstance(dpapi_available(), bool)


def test_gitignore_covers_sensitive_artifacts():
    """DoD 第 5 条：recon/ 与 storage_state.json 必须在 .gitignore 里。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    text = (root / ".gitignore").read_text(encoding="utf-8")
    assert "recon/" in text
    assert "storage_state.json" in text
    assert ".venv/" in text
