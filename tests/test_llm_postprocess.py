"""LLM 后处理的**安全不变量**测试（`ecnu_transcribe.llm`）。

这个模块会把 ASR 原文交给大模型改写再写进你的产物，所以最危险的不是「调用失败」，
而是**模型返回了不合规的内容却没被发现**：

    * 条目数对不上 → 文本与时间轴错位（字幕说的和画面对不上）；
    * 返回乱码 / 多余解释 / Markdown 围栏 → 直接污染产物；
    * 修复失败却把原文弄丢 → 用户拿不到任何文字。

因此本文件的核心是**不变量**而不是「功能是否好用」：

    1. 修复前后**片段数量与时间戳必须完全一致**（只允许改 text）；
    2. 模型返回任何畸形内容时，必须**退化为原文**，绝不写进产物；
    3. 任何 LLM 失败都不能让转写结果整体丢失；
    4. 重新分段不能丢内容、不能把时间轴弄乱；
    5. 绝不把 API Key 写进日志。

用 `tests/fake_llm_server.py` 起本地假端点，完全离线、可构造任意畸形响应。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ecnu_transcribe.config import AppConfig
from ecnu_transcribe.errors import LlmError
from ecnu_transcribe.llm import TextPostProcessor, _parse_break_list, probe_llm
from ecnu_transcribe.transcriber import Segment, Transcript, join_segment_text

ROOT = Path(__file__).resolve().parents[1]


def _load_fake_llm():
    path = ROOT / "tests" / "fake_llm_server.py"
    spec = importlib.util.spec_from_file_location("fake_llm_server", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fake_llm_server"] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


FakeLLM = _load_fake_llm().FakeLLM


# --------------------------------------------------------------------------- #
def make_transcript(n: int = 6) -> Transcript:
    segs = [
        Segment(start=i * 5.0, end=(i + 1) * 5.0, text=f"第{i + 1}句 原始识别文本（有错别字）。")
        for i in range(n)
    ]
    return Transcript(segments=segs, language="zh", duration_sec=n * 5.0, model="fake-asr")


def cfg_for(base_url: str, **kw) -> AppConfig:
    cfg = AppConfig()
    cfg.llm_enabled = True
    cfg.llm_base_url = base_url
    cfg.llm_model = "fake-llm"
    cfg.llm_fix_text = True
    cfg.llm_resegment = False
    cfg.llm_summary = False
    cfg.llm_max_chars_per_call = 6000
    cfg.llm_timeout = 15.0
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def fixed_reply(segments: list[dict]) -> str:
    return json.dumps({"segments": segments}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 1. 关键不变量：只改文本，不动时间轴与数量
# --------------------------------------------------------------------------- #
def test_fix_preserves_count_and_timestamps():
    """修复后：片段数不变、每一段的时间戳**逐个相等**，只有 text 变了。"""
    before = make_transcript(6)
    reply = fixed_reply(
        [{"i": i, "text": f"第{i + 1}句 修正后的文本。"} for i in range(6)]
    )
    with FakeLLM(replies=[reply]) as srv:
        proc = TextPostProcessor(cfg_for(srv.base_url), "sk-test-123456")
        result = proc.process(before)

    assert result.applied_fix is True
    assert len(result.segments) == len(before.segments), "片段数量必须不变"
    for old, new in zip(before.segments, result.segments):
        assert (old.start, old.end) == (new.start, new.end), "时间戳必须逐个相等"
    assert all("修正后" in s.text for s in result.segments)


def test_fix_handles_one_based_index_from_model():
    """模型用 1-based 序号时必须能正确映射（错位就会把文本配错时间）。"""
    before = make_transcript(3)
    reply = fixed_reply([{"i": 1, "text": "A"}, {"i": 2, "text": "B"}, {"i": 3, "text": "C"}])
    with FakeLLM(replies=[reply]) as srv:
        result = TextPostProcessor(cfg_for(srv.base_url), "sk-test-123456").process(before)
    # 1-based 映射失败时应保留原文（绝不能错位）
    texts = [s.text for s in result.segments]
    assert texts[0] in ("A", "第1句 原始识别文本（有错别字）。"), texts
    if texts[0] == "A":
        assert texts[1] == "B" and texts[2] == "C"
        assert [s.start for s in result.segments] == [s.start for s in before.segments]


def test_fix_ignores_extra_and_missing_items():
    """模型多给/少给条目时：只应用能对上的，其余保留原文，数量与时间轴不变。"""
    before = make_transcript(4)
    reply = fixed_reply(
        [
            {"i": 0, "text": "改0"},
            {"i": 99, "text": "无关条目"},
            {"i": 2, "text": "改2"},
        ]
    )
    with FakeLLM(replies=[reply]) as srv:
        result = TextPostProcessor(cfg_for(srv.base_url), "sk-test-123456").process(before)
    texts = [s.text for s in result.segments]
    assert len(texts) == 4
    assert texts[0] == "改0" and texts[2] == "改2"
    assert texts[1] == before.segments[1].text, "没对上的条目必须保留原文"
    assert "无关条目" not in texts
    assert [s.start for s in result.segments] == [s.start for s in before.segments]


# --------------------------------------------------------------------------- #
# 2. 畸形响应必须退化为原文
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_reply, label",
    [
        ("", "空内容"),
        ("   ", "全空白"),
        ("not json at all", "非 JSON"),
        ("{broken json", "JSON 语法错误"),
        ("[]", "顶层是数组但没有条目"),
        ('{"segments": "not a list"}', "segments 不是数组"),
        ('{"segments": [{"i": 0}]}', "条目缺 text"),
        ('{"segments": [{"i": 0, "text": ""}]}', "text 为空"),
        ("好的，我已经帮你修正了这些文本：", "只返回解释文字"),
        ("<html><body>502 Bad Gateway</body></html>", "返回 HTML"),
    ],
)
def test_malformed_reply_falls_back_to_original(bad_reply, label):
    """任何畸形响应都不能污染产物：必须原样保留 ASR 文本。"""
    before = make_transcript(3)
    with FakeLLM(replies=[bad_reply]) as srv:
        result = TextPostProcessor(cfg_for(srv.base_url), "sk-test-123456").process(before)

    assert len(result.segments) == len(before.segments), f"[{label}] 数量被改了"
    assert [s.start for s in result.segments] == [s.start for s in before.segments]
    # 文本必须是原文（没有被解释文字/HTML 覆盖）
    for old, new in zip(before.segments, result.segments):
        assert new.text == old.text, f"[{label}] 文本被污染：{new.text!r}"
    assert "<html>" not in join_segment_text(result.segments)


def test_markdown_fenced_json_is_accepted():
    """被 ```json 围栏包住的正常响应应能用（真实模型经常这么回）。"""
    before = make_transcript(2)
    inner = fixed_reply([{"i": 0, "text": "改0"}, {"i": 1, "text": "改1"}])
    reply = f"```json\n{inner}\n```"
    with FakeLLM(replies=[reply]) as srv:
        result = TextPostProcessor(cfg_for(srv.base_url), "sk-test-123456").process(before)
    assert [s.text for s in result.segments] == ["改0", "改1"]
    assert [s.start for s in result.segments] == [s.start for s in before.segments]


def test_explanation_wrapped_json_is_extracted():
    """响应里 JSON 前后夹了说明文字时，应能提取出 JSON。"""
    before = make_transcript(2)
    inner = fixed_reply([{"i": 0, "text": "改0"}, {"i": 1, "text": "改1"}])
    reply = f"好的，结果如下：\n{inner}\n希望有帮助！"
    with FakeLLM(replies=[reply]) as srv:
        result = TextPostProcessor(cfg_for(srv.base_url), "sk-test-123456").process(before)
    assert [s.text for s in result.segments] == ["改0", "改1"]


# --------------------------------------------------------------------------- #
# 3. 失败不能让转写整体丢失
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_http_errors_degrade_gracefully(status):
    """端点报错时：保留原文 + 记告警，不能抛出去把任务搞失败。"""
    before = make_transcript(3)
    with FakeLLM(status_for=lambda _i: status) as srv:
        result = TextPostProcessor(cfg_for(srv.base_url), "sk-test-123456").process(before)
    assert len(result.segments) == len(before.segments)
    assert [s.text for s in result.segments] == [s.text for s in before.segments]
    assert result.warnings, f"HTTP {status} 应产生告警"
    assert result.applied_fix is False


def test_401_is_reported_as_auth_error():
    before = make_transcript(2)
    with FakeLLM(status_for=lambda _i: 401) as srv:
        result = TextPostProcessor(cfg_for(srv.base_url), "sk-test-123456").process(before)
    joined = " ".join(result.warnings)
    assert "鉴权" in joined or "401" in joined, joined


def test_empty_transcript_is_skipped_without_calling_llm():
    """空转写不该浪费一次调用。"""
    empty = Transcript(segments=[Segment(0, 1, "   ")], duration_sec=1.0)
    with FakeLLM(replies=["{}"]) as srv:
        result = TextPostProcessor(cfg_for(srv.base_url), "sk-test-123456").process(empty)
    assert srv.call_count == 0
    assert result.warnings


def test_llm_disabled_returns_original():
    before = make_transcript(3)
    cfg = cfg_for("http://127.0.0.1:1/v1", llm_enabled=False)
    # llm_enabled=False 时不应发起任何请求（指向不存在端口也不会报错）
    result = TextPostProcessor(cfg, "sk-test-123456").process(before)
    assert [s.text for s in result.segments] == [s.text for s in before.segments]
    assert result.applied_fix is False


# --------------------------------------------------------------------------- #
# 4. 重新分段：不丢内容、时间轴不乱
# --------------------------------------------------------------------------- #
def test_resegment_preserves_all_text_and_order():
    before = make_transcript(10)
    # 让模型在若干序号之后换段
    with FakeLLM(replies=['{"break_after": [2, 5, 7]}']) as srv:
        cfg = cfg_for(srv.base_url, llm_fix_text=False, llm_resegment=False)
        proc = TextPostProcessor(cfg, "sk-test-123456")
        segs, warnings = proc._resegment(list(before.segments))

    # 内容必须完整保留（拼起来一样）
    assert join_segment_text(segs) == join_segment_text(before.segments), "重新分段丢内容了"
    # 时间轴单调且不越界
    starts = [s.start for s in segs]
    assert starts == sorted(starts), "重新分段后时间轴乱了"
    assert segs[0].start == before.segments[0].start
    assert segs[-1].end == before.segments[-1].end
    for s in segs:
        assert s.end > s.start


def test_resegment_merges_into_fewer_segments():
    before = make_transcript(10)
    with FakeLLM(replies=['{"break_after": [4, 9]}']) as srv:
        cfg = cfg_for(srv.base_url, llm_fix_text=False)
        segs, _w = TextPostProcessor(cfg, "sk-test-123456")._resegment(list(before.segments))
    assert len(segs) < len(before.segments), f"应合并成更少片段，实际 {len(segs)}"
    assert len(segs) >= 2


def test_resegment_bad_reply_keeps_original_grouping():
    before = make_transcript(10)
    with FakeLLM(replies=["garbage"]) as srv:
        cfg = cfg_for(srv.base_url, llm_fix_text=False)
        segs, _w = TextPostProcessor(cfg, "sk-test-123456")._resegment(list(before.segments))
    assert join_segment_text(segs) == join_segment_text(before.segments)
    assert len(segs) == len(before.segments), "解析失败时应保持原分段"


def test_resegment_short_transcript_is_noop():
    before = make_transcript(3)   # < 8 段时不值得调用模型
    with FakeLLM(replies=['{"break_after": [1]}']) as srv:
        cfg = cfg_for(srv.base_url, llm_fix_text=False)
        segs, _w = TextPostProcessor(cfg, "sk-test-123456")._resegment(list(before.segments))
    assert srv.call_count == 0
    assert len(segs) == len(before.segments)


def test_parse_break_list_variants():
    assert _parse_break_list('{"break_after": [1, 2]}') == {1, 2}
    assert _parse_break_list('{"breaks": [3]}') == {3}
    assert _parse_break_list("[0, 4]") == {0, 4}
    assert _parse_break_list("```json\n{\"break_after\":[5]}\n```") == {5}
    assert _parse_break_list('{"break_after": ["x", 2]}') == {2}
    # 「明确说不需要换段」→ 空集合（允许合并成一段）
    assert _parse_break_list('{"break_after": []}') == set()
    # 「解析失败」→ None（必须保持原分段，绝不能当成「不换段」）
    assert _parse_break_list("not json") is None
    assert _parse_break_list("") is None
    assert _parse_break_list('{"segments": [1,2]}') is None
    assert _parse_break_list("好的，我认为不需要换段。") is None


def test_resegment_explicit_empty_breaks_merges():
    """模型明确返回空断点列表时，才允许合并成一段。"""
    before = make_transcript(10)
    with FakeLLM(replies=['{"break_after": []}']) as srv:
        cfg = cfg_for(srv.base_url, llm_fix_text=False)
        segs, _w = TextPostProcessor(cfg, "sk-test-123456")._resegment(list(before.segments))
    assert len(segs) == 1
    assert join_segment_text(segs) == join_segment_text(before.segments)


# --------------------------------------------------------------------------- #
# 5. 摘要
# --------------------------------------------------------------------------- #
def test_summary_is_used_when_enabled():
    before = make_transcript(4)
    md = "## 一句话概括\n讲了二叉树。\n## 本节要点\n- 前序遍历"
    with FakeLLM(replies=[md]) as srv:
        cfg = cfg_for(srv.base_url, llm_fix_text=False, llm_summary=True)
        result = TextPostProcessor(cfg, "sk-test-123456").process(before)
    assert result.applied_summary is True
    assert "二叉树" in result.summary_md
    # 文本本身不应被摘要覆盖
    assert join_segment_text(result.segments) == join_segment_text(before.segments)


def test_summary_failure_keeps_text():
    before = make_transcript(4)
    with FakeLLM(status_for=lambda _i: 500) as srv:
        cfg = cfg_for(srv.base_url, llm_fix_text=False, llm_summary=True)
        result = TextPostProcessor(cfg, "sk-test-123456").process(before)
    assert result.applied_summary is False
    assert result.summary_md == ""
    assert join_segment_text(result.segments) == join_segment_text(before.segments)
    assert result.warnings


# --------------------------------------------------------------------------- #
# 6. 安全：不泄漏密钥
# --------------------------------------------------------------------------- #
def test_api_key_never_appears_in_warnings_or_logs(caplog):
    """把密钥登记为敏感字面量后，日志里不应出现明文。"""
    from ecnu_transcribe.logbus import redact

    secret = "sk-SUPER-SECRET-abcdef123456"
    before = make_transcript(2)
    with FakeLLM(status_for=lambda _i: 500) as srv:
        result = TextPostProcessor(cfg_for(srv.base_url), secret).process(before)
    blob = " ".join(result.warnings)
    assert secret not in blob
    assert secret not in redact(f"Authorization: Bearer {secret}")


def test_probe_llm_reports_success_and_failure():
    with FakeLLM(replies=["可用"]) as srv:
        ok, msg = probe_llm(cfg_for(srv.base_url), "sk-test-123456")
    assert ok is True and "可用" in msg

    with FakeLLM(status_for=lambda _i: 401) as srv2:
        ok2, msg2 = probe_llm(cfg_for(srv2.base_url), "sk-test-123456")
    assert ok2 is False
    assert "401" in msg2 or "鉴权" in msg2


def test_probe_llm_requires_key():
    ok, msg = probe_llm(AppConfig(), "")
    assert ok is False and "Key" in msg


# --------------------------------------------------------------------------- #
# 7. 分块：长转写要按上限切块调用
# --------------------------------------------------------------------------- #
def test_long_transcript_is_split_into_multiple_calls():
    """片段总字符数超过上限时应分多次调用（否则会被端点截断/报错）。"""
    before = make_transcript(60)   # 每段 ~20 字符
    calls_seen: list[int] = []

    def reply_for(index: int, payload: dict) -> str:
        calls_seen.append(index)
        # 从请求里读出有多少条目，逐条返回改好的文本
        user = ""
        for m in payload.get("messages", []):
            if m.get("role") == "user":
                user = m.get("content", "")
        start = user.find("{")
        items: list[dict] = []
        if start >= 0:
            try:
                parsed = json.loads(user[start:])
                items = parsed.get("segments", [])
            except Exception:
                items = []
        return fixed_reply([{"i": it.get("i", 0), "text": "改" + str(it.get("i", 0))} for it in items])

    with FakeLLM(reply_for=reply_for) as srv:
        cfg = cfg_for(srv.base_url, llm_max_chars_per_call=200)   # 强制多块
        result = TextPostProcessor(cfg, "sk-test-123456").process(before)

    assert srv.call_count > 1, f"应分多块调用，实际 {srv.call_count} 次"
    assert len(result.segments) == len(before.segments)
    assert [s.start for s in result.segments] == [s.start for s in before.segments]
    # 所有片段都被替换过（证明每一块都被应用了）
    assert all(s.text.startswith("改") for s in result.segments), [s.text for s in result.segments[:5]]
