"""首启一键诊断（ReadinessReport / run_readiness_check）测试。

目标是让用户打开应用就能知道「缺什么、下一步做什么」，而不是面对一堆报错。
所以这里重点验证：
    * 报告结构正确（图标 / 阻塞判定 / 下一步建议）；
    * 各类前置条件失败时**必须给出可照做的动作**；
    * 离线环境下不抛异常（诊断本身不能成为新的故障点）。
"""

from __future__ import annotations

import pytest

from ecnu_transcribe.config import AppConfig, ConfigManager
from ecnu_transcribe.login import ReadinessItem, ReadinessReport, run_readiness_check


# --------------------------------------------------------------------------- #
# 报告结构
# --------------------------------------------------------------------------- #
def test_item_icons():
    assert ReadinessItem("x", True).icon == "✅"
    assert ReadinessItem("x", False, blocking=True).icon == "⛔"
    assert ReadinessItem("x", False, blocking=False).icon == "⚠️"


def test_ready_ignores_nonblocking_items():
    rep = ReadinessReport()
    rep.add("硬性项", True)
    rep.add("可选提醒", False, blocking=False)
    assert rep.ready is True, "非阻塞项失败不应判为未就绪"


def test_ready_false_when_blocking_fails():
    rep = ReadinessReport()
    rep.add("硬性项", False, action="去做某事")
    assert rep.ready is False


def test_next_action_prefers_blocking_failure():
    rep = ReadinessReport()
    rep.add("可选提醒", False, action="可选动作", blocking=False)
    rep.add("硬性项", False, action="硬性动作")
    assert rep.next_action() == "硬性动作"


def test_next_action_falls_back_to_nonblocking():
    rep = ReadinessReport()
    rep.add("硬性项", True)
    rep.add("可选提醒", False, action="可选动作", blocking=False)
    assert rep.next_action() == "可选动作"


def test_next_action_when_all_ready():
    rep = ReadinessReport()
    rep.add("a", True)
    rep.add("b", True)
    assert "刷新清单" in rep.next_action()


def test_to_text_contains_action_lines():
    rep = ReadinessReport()
    rep.add("网络", False, "被网关拦下", "先连 VPN")
    text = rep.to_text()
    assert "首启一键诊断" in text
    assert "网络" in text and "被网关拦下" in text
    assert "先连 VPN" in text
    assert "下一步" in text


def test_to_text_marks_nonblocking_with_warning_icon():
    rep = ReadinessReport()
    rep.add("可选", False, "没配", "可以不管", blocking=False)
    assert "⚠️" in rep.to_text()


# --------------------------------------------------------------------------- #
# 真实跑一次（离线环境）
# --------------------------------------------------------------------------- #
def test_run_readiness_check_offline_does_not_raise(tmp_path, monkeypatch):
    """离线环境下诊断本身不能抛异常，且必须给出「网络不通」的结论与动作。"""
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path / "out")
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=tmp_path / "s.json")
    cm.load()

    report = run_readiness_check(cfg, cm=cm, deep=False)
    assert report.items, "至少要产出检查项"
    names = [i.name for i in report.items]
    assert "输出目录可写" in names
    assert "ffmpeg 可用" in names
    assert "课程平台网络可达" in names
    assert "已保存登录态" in names

    # 每个失败项都必须带可执行动作
    for item in report.items:
        if not item.ok:
            assert item.action, f"{item.name} 失败但没有给建议动作"


def test_output_dir_check_fails_on_unwritable_path(tmp_path):
    cfg = AppConfig()
    # 用一个「以文件充目录」的路径，写入必然失败
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    cfg.output_dir = str(blocker / "sub")

    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=tmp_path / "s.json")
    cm.load()
    report = run_readiness_check(cfg, cm=cm, deep=False)
    item = next(i for i in report.items if i.name == "输出目录可写")
    assert item.ok is False
    assert item.action


def test_asr_check_flags_missing_key_for_remote_endpoint(tmp_path):
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path / "out")
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = "https://api.example.com/v1"   # 远程 + 无 Key

    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=tmp_path / "s.json")
    cm.load()
    report = run_readiness_check(cfg, cm=cm, deep=False)
    item = next(i for i in report.items if "ASR" in i.name)
    assert item.ok is False
    assert "Key" in item.detail or "Key" in item.action
    # 建议里要同时给出「本地零成本」与「云端」两条路
    assert "local_asr_server" in item.action or "127.0.0.1" in item.action


def test_asr_check_accepts_local_endpoint_without_key(tmp_path):
    """本机端点不需要 Key：不应被标成未配置。"""
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path / "out")
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = "http://127.0.0.1:8000/v1"

    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=tmp_path / "s.json")
    cm.load()
    report = run_readiness_check(cfg, cm=cm, deep=False)
    item = next(i for i in report.items if "ASR" in i.name)
    # 本机端点 + deep=False → 视为「已配置」（跳过联网自检）
    assert item.ok is True, item.detail


def test_report_text_is_copyable_plaintext(tmp_path):
    """报告要能直接贴给别人看：纯文本、无异常对象 str()。"""
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path / "out")
    cm = ConfigManager(config_file=tmp_path / "c.json", secrets_file=tmp_path / "s.json")
    cm.load()
    text = run_readiness_check(cfg, cm=cm, deep=False).to_text()
    assert isinstance(text, str) and len(text) > 50
    assert "Error" not in text and "Traceback" not in text
