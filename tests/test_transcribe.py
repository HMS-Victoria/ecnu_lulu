"""转写相关纯逻辑测试（M6）：切分规划、时间轴合并去重、ASR 响应解析。"""

from __future__ import annotations

import pytest

from ecnu_transcribe import media
from ecnu_transcribe.transcriber import (
    AudioChunk,
    OpenAICompatibleTranscriber,
    Segment,
    Transcript,
    join_segment_text,
    merge_segments,
    parse_srt_text,
    plan_chunks,
)


# --------------------------------------------------------------------------- #
# 切分
# --------------------------------------------------------------------------- #
def test_plan_chunks_short_audio_single_chunk():
    chunks = plan_chunks(120, max_segment_sec=600)
    assert len(chunks) == 1
    assert chunks[0].start == 0 and chunks[0].end == 120


def test_plan_chunks_fixed_strategy_respects_max():
    chunks = plan_chunks(1800, max_segment_sec=600, overlap_sec=1.5, strategy="fixed")
    assert len(chunks) >= 3
    # 单段不超过上限；最后一段可能因「尾巴并入」而略超（不超过 5%）
    for c in chunks[:-1]:
        assert c.duration <= 600.5
    assert chunks[-1].duration <= 600 * 1.05
    assert chunks[0].start == 0
    assert chunks[-1].end == pytest.approx(1800)
    # 相邻段必须有重叠（避免切点上丢词）
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start < prev.end


def test_plan_chunks_prefers_silence_midpoints():
    silences = [media.SilenceSpan(595, 605), media.SilenceSpan(1195, 1205)]
    chunks = plan_chunks(1800, silences=silences, max_segment_sec=600, overlap_sec=1.5)
    assert len(chunks) == 3
    # 切点应落在静音中点附近
    assert abs(chunks[0].end - 600) < 2
    assert abs(chunks[1].end - 1200) < 2


def test_plan_chunks_covers_whole_duration():
    silences = [media.SilenceSpan(100, 101), media.SilenceSpan(303, 304)]
    chunks = plan_chunks(700, silences=silences, max_segment_sec=300, overlap_sec=2.0)
    assert chunks[0].start == 0
    assert chunks[-1].end == pytest.approx(700)
    # 无空洞：后一段起点 <= 前一段终点
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start <= prev.end


def test_plan_chunks_zero_duration():
    assert plan_chunks(0) == []


# --------------------------------------------------------------------------- #
# 时间轴合并 / 去重
# --------------------------------------------------------------------------- #
def test_merge_segments_offsets_and_sorts():
    a = [Segment(0.0, 5.0, "第一句"), Segment(5.0, 10.0, "第二句")]
    b = [Segment(0.0, 5.0, "第三句"), Segment(5.0, 10.0, "第四句")]
    merged = merge_segments([(0.0, a), (100.0, b)])
    assert [s.text for s in merged] == ["第一句", "第二句", "第三句", "第四句"]
    assert merged[2].start == pytest.approx(100.0)
    assert merged[3].end == pytest.approx(110.0)


def test_merge_segments_dedups_overlap_exact_duplicate():
    first = [Segment(0.0, 30.0, "重叠区的一句话"), Segment(30.0, 60.0, "第一段独有")]
    second = [Segment(0.0, 27.0, "重叠区的一句话"), Segment(27.0, 60.0, "第二段独有")]
    merged = merge_segments([(0.0, first), (58.5, second)], overlap_sec=1.5)
    texts = [s.text for s in merged]
    assert texts.count("重叠区的一句话") == 1
    assert "第一段独有" in texts and "第二段独有" in texts


def test_merge_segments_dedups_truncated_overlap():
    first = [Segment(0.0, 12.0, "这是一个被切分点截断的比较长的句子")]
    second = [Segment(0.0, 8.0, "这是一个被切分点截断的比较长")]
    merged = merge_segments([(0.0, first), (10.0, second)], overlap_sec=2.0)
    assert len(merged) == 1


def test_merge_segments_keeps_distinct_short_sentences():
    first = [Segment(0.0, 5.0, "好的")]
    second = [Segment(4.0, 9.0, "我们继续")]
    merged = merge_segments([(0.0, first), (4.0, second)], overlap_sec=1.5)
    assert len(merged) == 2


def test_merge_segments_empty_input():
    assert merge_segments([]) == []
    assert merge_segments([(0.0, [])]) == []


def test_merge_segments_dedups_identical_text_in_overlap_region():
    """真实场景：同一句在上一段结尾和下一段开头各出现一次（重叠 1.5s）→ 去重。"""
    first = [Segment(590.0, 600.0, "重复的句子")]
    second_local = [Segment(0.0, 10.0, "重复的句子")]
    merged = merge_segments([(0.0, first), (598.5, second_local)], overlap_sec=1.5)
    assert len(merged) == 1
    assert merged[0].start == pytest.approx(590.0)


def test_merge_segments_keeps_identical_text_far_apart():
    """相隔很远的相同短句是**真实重复的讲课内容**，不能当重复删掉。"""
    a = [Segment(0.0, 5.0, "好，我们继续")]
    b = [Segment(0.0, 5.0, "好，我们继续")]
    merged = merge_segments([(0.0, a), (300.0, b)], overlap_sec=1.5)
    assert len(merged) == 2
    assert merged[1].start == pytest.approx(300.0)


# --------------------------------------------------------------------------- #
# 文本拼接
# --------------------------------------------------------------------------- #
def test_join_segment_text_no_space_between_chinese():
    segs = [Segment(text="今天我们讲"), Segment(text="二叉树。")]
    assert join_segment_text(segs) == "今天我们讲二叉树。"


def test_join_segment_text_space_between_latin():
    segs = [Segment(text="hello"), Segment(text="world")]
    assert join_segment_text(segs) == "hello world"


def test_transcript_char_count_and_text():
    t = Transcript(segments=[Segment(0, 1, "abc"), Segment(1, 2, "def")])
    assert t.char_count == 7
    assert t.text == "abc def"
    assert not t.is_empty()
    assert Transcript(segments=[Segment(0, 1, "  ")]).is_empty()


# --------------------------------------------------------------------------- #
# ASR 响应解析
# --------------------------------------------------------------------------- #
def test_parse_srt_text(sample_catalog_json):
    segs = parse_srt_text(sample_catalog_json["asr_srt"])
    assert len(segs) == 2
    assert segs[0].start == pytest.approx(0.0)
    assert segs[0].end == pytest.approx(4.2)
    assert segs[0].text == "今天我们讲一下二叉树。"
    assert segs[1].end == pytest.approx(12.5)


def test_parse_srt_text_with_offset(sample_catalog_json):
    segs = parse_srt_text(sample_catalog_json["asr_srt"], offset=600.0)
    assert segs[0].start == pytest.approx(600.0)
    assert segs[1].end == pytest.approx(612.5)


def test_parse_json_payload_segments(sample_catalog_json):
    segs = OpenAICompatibleTranscriber._parse_json_payload(
        sample_catalog_json["asr_verbose_json"], offset=10.0
    )
    assert len(segs) == 2
    assert segs[0].start == pytest.approx(10.0)
    assert segs[0].confidence == pytest.approx(0.93)
    assert segs[1].end == pytest.approx(22.5)


def test_parse_json_payload_text_only():
    segs = OpenAICompatibleTranscriber._parse_json_payload({"text": "只有一段文字"}, offset=5.0)
    assert len(segs) == 1
    assert segs[0].text == "只有一段文字"
    assert segs[0].start == pytest.approx(5.0)


def test_parse_json_payload_missing_end_uses_duration():
    payload = {"segments": [{"start": 1.0, "duration": 3.5, "text": "x"}]}
    segs = OpenAICompatibleTranscriber._parse_json_payload(payload, offset=0.0)
    assert segs[0].end == pytest.approx(4.5)


def test_transcript_json_roundtrip(tmp_path):
    t = Transcript(
        segments=[Segment(0.0, 1.5, "你好", confidence=0.9)],
        language="zh", duration_sec=1.5, model="m", provider="p", meta={"k": "v"},
    )
    p = t.save_json(tmp_path / "t.json")
    loaded = Transcript.load_json(p)
    assert loaded.segments[0].text == "你好"
    assert loaded.segments[0].confidence == pytest.approx(0.9)
    assert loaded.meta["k"] == "v"
    assert loaded.model == "m"


# --------------------------------------------------------------------------- #
# DRM 检测
# --------------------------------------------------------------------------- #
def test_drm_detection_raises_on_widevine(sample_catalog_json):
    from ecnu_transcribe.errors import DrmDetectedError

    with pytest.raises(DrmDetectedError):
        media.detect_drm_in_text(sample_catalog_json["m3u8_drm"], source="x.m3u8")


def test_drm_detection_allows_aes128(sample_catalog_json):
    media.detect_drm_in_text(sample_catalog_json["m3u8_aes128"])  # 不应抛异常
    media.detect_drm_in_text(sample_catalog_json["m3u8_plain"])


# --------------------------------------------------------------------------- #
# 命令构造
# --------------------------------------------------------------------------- #
def test_build_output_args_audio_only():
    args = media.build_output_args(fmt="mp3", bitrate="64k", sample_rate=16000, channels=1)
    assert "-vn" in args
    assert args[args.index("-ac") + 1] == "1"
    assert args[args.index("-ar") + 1] == "16000"
    assert "libmp3lame" in args
    assert args[args.index("-b:a") + 1] == "64k"


def test_build_input_args_carries_cookie_and_referer():
    args = media.build_input_args(
        "https://x/y.m3u8", cookie="a=b", referer="https://portal/", user_agent="UA"
    )
    blob = args[args.index("-headers") + 1]
    assert "Cookie: a=b" in blob
    assert "Referer: https://portal/" in blob
    assert "User-Agent: UA" in blob
    assert "-reconnect" in args


def test_estimate_duration_from_progress():
    assert media.estimate_duration_from_progress("out_time_ms=1500000") == ("time", 1.5)
    assert media.estimate_duration_from_progress("progress=end")[0] == "done"
    assert media.estimate_duration_from_progress("time=00:01:30.50") == ("time", 90.5)
    assert media.estimate_duration_from_progress("garbage") is None


def test_audio_chunk_duration():
    assert AudioChunk(0, 10.0, 25.0).duration == pytest.approx(15.0)
    assert AudioChunk(0, 25.0, 10.0).duration == 0.0
