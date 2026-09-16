"""产物导出测试（M6）：SRT 时间轴、段落组织、只增不改的备份语义。"""

from __future__ import annotations

import pytest

from ecnu_transcribe import exporter
from ecnu_transcribe.catalog import Resource
from ecnu_transcribe.exporter import (
    ExportResult,
    build_paragraphs,
    build_srt_entries,
    export_all,
    fmt_srt_time,
    split_into_sentences,
)
from ecnu_transcribe.transcriber import Segment, Transcript


def _segs():
    return [
        Segment(0.0, 4.2, "今天我们讲一下二叉树。"),
        Segment(4.2, 12.5, "二叉树的遍历有三种方式：前序、中序、后序。"),
        Segment(12.5, 20.0, "先看前序遍历。"),
    ]


# --------------------------------------------------------------------------- #
def test_fmt_srt_time():
    assert fmt_srt_time(0) == "00:00:00,000"
    assert fmt_srt_time(1.5) == "00:00:01,500"
    assert fmt_srt_time(3661.25) == "01:01:01,250"
    assert fmt_srt_time(-5) == "00:00:00,000"


def test_split_into_sentences():
    parts = split_into_sentences("第一句。第二句！第三句？")
    assert parts == ["第一句。", "第二句！", "第三句？"]
    assert split_into_sentences("") == []
    assert split_into_sentences("没有标点") == ["没有标点"]


def test_build_paragraphs_groups_by_max_chars():
    segs = [Segment(text="甲" * 300), Segment(text="乙" * 300), Segment(text="丙" * 300)]
    paras = build_paragraphs(segs, max_chars=700)
    assert len(paras) == 2
    assert paras[0].startswith("甲")
    assert paras[1].startswith("丙")


def test_build_paragraphs_handles_embedded_newlines():
    segs = [Segment(text="第一行\n第二行"), Segment(text="第三行")]
    paras = build_paragraphs(segs, max_chars=1000)
    assert len(paras) == 1
    assert "第一行" in paras[0] and "第三行" in paras[0]


def test_build_srt_entries_basic():
    entries = build_srt_entries(_segs())
    assert len(entries) == 3
    assert entries[0][2] == "今天我们讲一下二叉树。"
    # 时间轴单调不减
    for prev, nxt in zip(entries, entries[1:]):
        assert nxt[0] >= prev[1] - 1e-6


def test_build_srt_entries_splits_long_segment():
    long_text = "这是一个很长的句子。" * 12
    segs = [Segment(0.0, 120.0, long_text)]
    entries = build_srt_entries(segs, max_duration=12.0, max_chars=60)
    assert len(entries) > 1
    assert all(len(e[2]) <= 70 for e in entries)
    # 单条时长不超过限制的 1.2 倍（切分点对齐会带来少量溢出）
    assert all(e[1] - e[0] <= 12.0 * 1.2 for e in entries)
    # 时间轴覆盖原区间
    assert entries[0][0] == pytest.approx(0.0)
    assert entries[-1][1] == pytest.approx(120.0, abs=1.0)


def test_build_srt_entries_very_long_sentence_short_duration():
    """极端情况：一句话撑满 60 秒 —— 也不能碎成十几个 3 秒的条目。"""
    text = "这是一个完全没有标点的超长句子" * 10
    entries = build_srt_entries([Segment(0.0, 60.0, text)], max_duration=12.0, max_chars=60)
    assert len(entries) <= 8
    assert all(len(e[2]) <= 70 for e in entries)
    assert entries[-1][1] == pytest.approx(60.0, abs=1.0)


def test_build_srt_entries_zero_duration_gets_span():
    entries = build_srt_entries([Segment(5.0, 5.0, "无时间信息")])
    assert len(entries) == 1
    assert entries[0][1] > entries[0][0]


def test_build_srt_entries_skips_blank():
    assert build_srt_entries([Segment(0, 1, "   "), Segment(1, 2, "")]) == []


# --------------------------------------------------------------------------- #
def test_export_all_writes_three_files(tmp_path):
    res = Resource(
        resource_id="R1", title="第1讲 绪论", course_name="数据结构", teacher="张老师",
        duration_sec=20.0, record_time="2025-09-08 08:00:00",
    )
    t = Transcript(segments=_segs(), language="zh", duration_sec=20.0, model="paraformer-v2", provider="dashscope")
    result = export_all(res, t, tmp_path, summary_md="## 一句话概括\n讲了二叉树。")
    assert isinstance(result, ExportResult)
    assert len(result.files) == 3
    for p in result.files:
        assert p.is_file() and p.stat().st_size > 0
    assert result.txt.name == "第1讲 绪论.txt"
    assert result.srt.name == "第1讲 绪论.srt"
    assert result.md.name == "第1讲 绪论.md"
    assert result.txt.parent.name == "数据结构"

    # utf-8-sig = 感知并剥掉 BOM 的读取方式，等价于现代编辑器/播放器的行为
    txt = result.txt.read_text(encoding="utf-8-sig")
    assert "二叉树" in txt
    srt = result.srt.read_text(encoding="utf-8-sig")
    assert "-->" in srt and srt.startswith("1\n")
    md = result.md.read_text(encoding="utf-8-sig")
    assert md.startswith("# 第1讲 绪论")
    assert "## 元信息" in md and "## 摘要" in md and "## 全文" in md
    assert "paraformer-v2" in md


def test_export_all_respects_emit_flags(tmp_path):
    res = Resource(resource_id="R2", title="T", course_name="C")
    t = Transcript(segments=_segs(), duration_sec=20.0)
    result = export_all(res, t, tmp_path, emit_txt=True, emit_srt=False, emit_md=True)
    assert result.txt is not None and result.md is not None and result.srt is None
    assert len(result.files) == 2


def test_export_all_backs_up_existing_file(tmp_path):
    res = Resource(resource_id="R3", title="T", course_name="C")
    t = Transcript(segments=_segs(), duration_sec=20.0)
    first = export_all(res, t, tmp_path)
    assert first.txt is not None
    original = first.txt.read_text(encoding="utf-8-sig")

    # 第二次导出：应产生 .bak-*，且旧内容被保留在备份里
    second = export_all(res, t, tmp_path, summary_md="新摘要")
    backups = [p for p in second.backups if p.name.startswith("T.txt.bak-")]
    assert backups, "应产生 txt 备份"
    assert backups[0].read_text(encoding="utf-8-sig") == original


def test_export_all_writes_transcript_json(tmp_path):
    res = Resource(resource_id="R4", title="T", course_name="C")
    t = Transcript(segments=_segs(), duration_sec=20.0)
    export_all(res, t, tmp_path)
    js = tmp_path / "C" / "T.transcript.json"
    assert js.is_file()
    loaded = Transcript.load_json(js)
    assert len(loaded.segments) == 3


def test_export_all_marks_llm_warnings(tmp_path):
    res = Resource(resource_id="R5", title="T", course_name="C")
    t = Transcript(segments=_segs(), duration_sec=20.0)
    t.meta["llm_warnings"] = ["摘要生成失败：超时"]
    result = export_all(res, t, tmp_path)
    assert result.md is not None
    assert "后处理告警" in result.md.read_text(encoding="utf-8-sig")


def test_write_txt_no_backup_when_file_absent(tmp_path):
    p = tmp_path / "a.txt"
    assert exporter.write_txt(p, ["hello"]) == []
    assert p.read_text(encoding="utf-8-sig").startswith("hello")


# --------------------------------------------------------------------------- #
# UTF-8 BOM：Windows 记事本 / 字幕播放器靠它认出 UTF-8，否则按 cp936 猜 → 中文乱码
# --------------------------------------------------------------------------- #
def _no_bom_read(path):
    """按 cp936 之外的常见误解方式读：不做 BOM 剥离，直接看原始头字节。"""
    return path.read_bytes()


def _cp936_misreads(raw: bytes) -> bool:
    """判断「按系统代码页 cp936 猜测」是否会毁掉内容。

    真实世界两种表现都会发生：字节序列非法 → 直接报错（工具往往吞掉错误显示乱码），
    或者恰好合法 → 解出完全不同的文本（mojibake）。任一种都算「猜错」。
    """
    try:
        return raw.decode("cp936") != raw.decode("utf-8")
    except UnicodeDecodeError:
        return True


def test_exports_carry_utf8_bom_by_default(tmp_path):
    res = Resource(resource_id="B1", title="第1讲 绪论", course_name="数据结构")
    t = Transcript(segments=_segs(), duration_sec=20.0)
    result = export_all(res, t, tmp_path, summary_md="讲了二叉树。")
    for p in result.files:
        raw = _no_bom_read(p)
        assert raw[:3] == b"\xef\xbb\xbf", f"{p.name} 缺少 UTF-8 BOM"
        # BOM 之后必须仍是合法 UTF-8（BOM 不是把编码换掉，只是加个标记）
        raw[3:].decode("utf-8")

    # 不带 BOM 时，简体中文 Windows 的默认代码页 cp936 会把同样的字节
    # 解成完全不同的文本（甚至直接报错）—— 这就是加 BOM 要解决的问题。
    payload = result.txt.read_bytes()[3:]
    assert _cp936_misreads(payload)
    # 带 BOM 后，任何 UTF-8 读取器（含 utf-8-sig）都能正确还原正文
    assert "二叉树" in payload.decode("utf-8")


def test_bom_can_be_disabled(tmp_path):
    res = Resource(resource_id="B2", title="T", course_name="C")
    t = Transcript(segments=_segs(), duration_sec=20.0)
    result = export_all(res, t, tmp_path, bom=False)
    for p in result.files:
        raw = p.read_bytes()
        assert raw[:3] != b"\xef\xbb\xbf", f"{p.name} 不应有 BOM"
        assert raw.decode("utf-8")            # 仍是 UTF-8，只是没有 BOM
    assert result.srt.read_text(encoding="utf-8").startswith("1\n")
    assert result.md.read_text(encoding="utf-8").startswith("# T")
    # 无 BOM 的老问题：cp936 读取得到 mojibake 或直接报错（关掉 BOM 就把这风险交回用户）
    assert _cp936_misreads(result.txt.read_bytes())


def test_write_txt_bom_flag_is_honored(tmp_path):
    p = tmp_path / "a.txt"
    exporter.write_txt(p, ["你好"], bom=False)
    assert p.read_bytes()[:3] != b"\xef\xbb\xbf"
    exporter.write_txt(p, ["你好"], bom=True)
    assert p.read_bytes()[:3] == b"\xef\xbb\xbf"
    assert p.read_text(encoding="utf-8-sig").startswith("你好")


def test_srt_still_parses_after_bom(tmp_path):
    """带 BOM 的 SRT 必须仍能被解析：剥掉 BOM 后首行是序号，时间轴格式不变。"""
    import re as _re

    res = Resource(resource_id="B3", title="T", course_name="C")
    t = Transcript(segments=_segs(), duration_sec=20.0)
    result = export_all(res, t, tmp_path)
    raw = result.srt.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"
    text = raw.decode("utf-8-sig")
    blocks = [b for b in text.strip().split("\n\n") if b.strip()]
    assert len(blocks) == 3
    assert blocks[0].splitlines()[0] == "1"
    assert _re.fullmatch(
        r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", blocks[0].splitlines()[1]
    )
    # 换行仍然是 LF（BOM 不该顺手把换行改成 CRLF）
    assert b"\r\n" not in raw


def test_bom_does_not_leak_into_transcript_json(tmp_path):
    """BOM 只针对给人看的产物；机器可读的 transcript.json 必须保持纯 UTF-8。"""
    res = Resource(resource_id="B4", title="T", course_name="C")
    t = Transcript(segments=_segs(), duration_sec=20.0)
    export_all(res, t, tmp_path)
    js = tmp_path / "C" / "T.transcript.json"
    raw = js.read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf"
    assert Transcript.load_json(js).segments


def test_srt_entries_no_overlap_after_fix():
    segs = [Segment(0.0, 10.0, "A"), Segment(2.0, 8.0, "B")]
    entries = build_srt_entries(segs)
    assert entries[1][0] > entries[0][0]
    assert entries[1][0] >= entries[0][1] - 1e-6
