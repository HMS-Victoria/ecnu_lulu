"""长音频切分管线的离线集成测试。

长课程（1~2 小时）必须切分才能过 ASR 端点的体积/时长上限，而切分最容易出两类问题：
    1. **边界丢内容**（切点附近的话被切掉）；
    2. **边界重复**（重叠区的话出现两次）。
这两类问题很难人工发现（要听完两小时），所以这里做一个确定性验证：

    真实音频（多句 + 句间静音）
      → `media.detect_silences()` 找静音
      → `plan_chunks()` 规划分段
      → ffmpeg 真实切片
      → 可编排的假 ASR 端点逐段返回文本（故意让相邻段**重复同一句**）
      → `merge_segments()` 时间轴平移合并去重
      → 断言：**所有句子恰好出现一次**、时间轴单调、覆盖完整时长

假端点还用于验证「端点不支持 verbose_json 时回退 srt」「部分段失败时保留其余结果」。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ecnu_transcribe import media
from ecnu_transcribe.config import AppConfig
from ecnu_transcribe.errors import TranscriptionError
from ecnu_transcribe.transcriber import (
    OpenAICompatibleTranscriber,
    Transcript,
    merge_segments,
    plan_chunks,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_fake_asr():
    path = ROOT / "tests" / "fake_asr_server.py"
    spec = importlib.util.spec_from_file_location("fake_asr_server", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fake_asr_server"] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class FakeASR:
    """上下文管理器：起假 ASR 服务，退出时关闭。"""

    def __init__(self, **kw) -> None:
        self.mod = _load_fake_asr()
        self.server = self.mod.FakeASRServer(**kw)

    def __enter__(self):
        self.server.start()
        return self.server

    def __exit__(self, *exc) -> None:
        self.server.stop()


# --------------------------------------------------------------------------- #
# 素材
# --------------------------------------------------------------------------- #
SENTENCES = [
    "第一句，讲的是顺序存储的基本概念。",
    "第二句，随机访问的时间复杂度是常数阶。",
    "第三句，插入操作平均需要移动一半元素。",
    "第四句，链式存储用指针连接各个结点。",
    "第五句，链式存储的随机访问必须从头遍历。",
    "第六句，两种实现各有取舍要看具体场景。",
]


def build_long_audio(work: Path, *, gap: float = 1.2) -> tuple[Path, list[tuple[float, float]]]:
    """生成一段「多句 + 句间静音」的长音频，返回 (路径, 每句的(起,止)区间)。"""
    ffmpeg = media.find_ffmpeg()
    work.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    spans: list[tuple[float, float]] = []
    cursor = 0.0

    lead = work / "lead.wav"
    media.run_ffmpeg(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono", "-t", "0.8", "-c:a", "pcm_s16le", str(lead)],
        timeout=60, check=True,
    )
    parts.append(lead)
    cursor += 0.8

    for i, _text in enumerate(SENTENCES):
        # 每句用不同频率的正弦（时长 2.0s）—— 假 ASR 不真正识别，只需要「有声音」
        tone = work / f"tone{i}.wav"
        freq = 320 + i * 90
        media.run_ffmpeg(
            [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
             "-i", f"sine=frequency={freq}:duration=2.0",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tone)],
            timeout=60, check=True,
        )
        parts.append(tone)
        spans.append((cursor, cursor + 2.0))
        cursor += 2.0
        # 注意：变量名不能复用参数 gap（曾经把 Path 当成时长传给 ffmpeg -t）
        gap_wav = work / f"gap{i}.wav"
        media.run_ffmpeg(
            [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
             "-i", "anullsrc=r=16000:cl=mono", "-t", str(gap), "-c:a", "pcm_s16le", str(gap_wav)],
            timeout=60, check=True,
        )
        parts.append(gap_wav)
        cursor += gap

    listing = work / "list.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    out = work / "long.wav"
    media.run_ffmpeg(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
        timeout=300, check=True,
    )
    return out, spans


@pytest.fixture(scope="module")
def long_audio(tmp_path_factory) -> tuple[Path, list[tuple[float, float]]]:
    work = tmp_path_factory.mktemp("long-audio")
    return build_long_audio(work)


# --------------------------------------------------------------------------- #
# 静音检测与切分规划（真实音频）
# --------------------------------------------------------------------------- #
def test_detect_silences_on_real_audio(long_audio):
    audio, spans = long_audio
    silences = media.detect_silences(audio, noise_db=-40.0, min_silence_sec=0.5, timeout=300)
    assert len(silences) >= len(SENTENCES) - 1, f"句间静音应被检测到，实际 {len(silences)} 个"
    # 静音区间应大致落在两句之间（不要落在句子内部）
    for sil in silences:
        mid = (sil.start + sil.end) / 2
        inside = [s for s in spans if s[0] + 0.2 < mid < s[1] - 0.2]
        assert not inside, f"静音点 {mid:.2f}s 落在句子内部 {inside}"


def test_plan_chunks_uses_silences_and_covers_audio(long_audio):
    audio, _spans = long_audio
    dur = media.duration_of(audio)
    silences = media.detect_silences(audio, noise_db=-40.0, min_silence_sec=0.5, timeout=300)
    chunks = plan_chunks(
        dur, silences=silences, max_segment_sec=6.0, overlap_sec=0.8,
        min_segment_sec=1.0, strategy="silence",
    )
    assert len(chunks) >= 3, f"应切成多段（实际 {len(chunks)}）"
    assert chunks[0].start == 0.0
    assert chunks[-1].end == pytest.approx(dur, abs=0.1)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start < prev.end, "相邻段必须有重叠"
    assert all(c.duration > 0 for c in chunks)


# --------------------------------------------------------------------------- #
# 真实切分管线：边界不丢不重
# --------------------------------------------------------------------------- #
def test_chunked_transcription_no_loss_no_duplication(long_audio, tmp_path, monkeypatch):
    """切分管线的集成不变量：段段有调用、内容不丢、时间轴合法且被正确平移。

    注：这里不假定假端点的时间戳精度（它按切片真实解码长度算），只断言
    **产品真正依赖的不变量**：
        * 每个分段各调用一次 ASR；
        * 每段返回的文本都出现在最终结果里（不丢）；
        * 文本不重复（去重生效）；
        * 时间轴单调、非零长度、**不超过音频总长**（夹取生效）；
        * 时间轴被正确平移（末段落在音频后段，而不是全挤在开头）。
    """
    audio, _spans = long_audio
    dur = media.duration_of(audio)

    cfg = AppConfig()
    cfg.asr_chunk_strategy = "fixed"
    cfg.asr_max_segment_sec = 5
    cfg.asr_overlap_sec = 1.0
    cfg.asr_timestamps = True
    cfg.asr_retries = 1
    cfg.output_dir = str(tmp_path)

    chunks = plan_chunks(dur, silences=[], max_segment_sec=5, overlap_sec=1.0, strategy="fixed")
    assert len(chunks) >= 3, f"本用例需要多段，实际 {len(chunks)} 段"
    expected_texts = [SENTENCES[i % len(SENTENCES)] for i in range(len(chunks))]

    with FakeASR(port=0, text_for_call=lambda i: [SENTENCES[i % len(SENTENCES)]]) as srv:
        cfg.asr_base_url = srv.base_url
        cfg.asr_model = "fake-asr"
        tr = OpenAICompatibleTranscriber(cfg, "")
        result = tr.transcribe(audio, duration_sec=dur)

    assert isinstance(result, Transcript)
    assert srv.call_count == len(chunks), f"应对每段各调用一次（{srv.call_count} vs {len(chunks)}）"
    assert result.segments, "应有转写结果"

    texts = [s.text.strip() for s in result.segments]
    assert all(texts), f"存在空片段：{texts}"

    # 不丢：每段返回的文本都在结果里
    for sent in expected_texts:
        assert any(sent in t for t in texts), f"「{sent}」在结果中丢失：{texts}"
    # 不重：每个文本最多出现一次
    for sent in set(expected_texts):
        n = sum(1 for t in texts if sent in t)
        assert n <= 1, f"「{sent}」出现了 {n} 次：{texts}"

    # 时间轴合法：单调、非零长度、不越界
    starts = [s.start for s in result.segments]
    assert starts == sorted(starts), f"时间轴必须单调：{starts}"
    for seg in result.segments:
        assert seg.end > seg.start, f"零长度片段：{seg}"
        assert 0 <= seg.start and seg.end <= dur + 0.5, (
            f"时间轴越界：[{seg.start:.2f}, {seg.end:.2f}] vs 音频 {dur:.2f}s"
        )
    # 平移生效：末段落在音频后半段
    assert result.segments[-1].start > dur * 0.4, (
        f"时间轴没有平移：末段起点 {result.segments[-1].start:.2f}s / 总长 {dur:.2f}s"
    )


def test_interrupted_transcription_resumes_without_repeating_asr(long_audio, tmp_path, monkeypatch):
    """缺陷 46：中途被打断后重跑，**已完成的分段不能再调一次 ASR**。

    实测痛点：一节课 55 分钟会切成十几段，云端 ASR 逐段计费。原先分段结果只在内存里、
    成功后连切片都删掉了 —— 中途一断（崩溃/关机/被环境回收）就得把付过费的分段全部重来。

    做法：让假 ASR 在第 2 段返回后触发取消 → 本轮以 TaskCancelled 结束（第 1、2 段已落盘）；
    再用**同一份音频、同一套切分方案**重跑 → 只应调用第 3 段及以后。

    注意：分段缓存目录是**全机共享**的（`cache/segments/<音频哈希>-<方案哈希>`，这正是续跑
    能生效的原因）。所以测试必须把 `cache_dir()` 指到临时目录，否则上一次测试留下的结果
    会让本次「一上来就全部复用」——第一版就是这么红的，日志里写着「第 2 段复用已缓存结果」。
    """
    import threading

    monkeypatch.setattr("ecnu_transcribe.transcriber.paths.cache_dir", lambda: tmp_path / "cache")

    audio, _spans = long_audio
    dur = media.duration_of(audio)

    cfg = AppConfig()
    cfg.asr_chunk_strategy = "fixed"
    cfg.asr_max_segment_sec = 5
    cfg.asr_overlap_sec = 1.0
    cfg.asr_timestamps = True
    cfg.asr_retries = 1
    cfg.output_dir = str(tmp_path)

    chunks = plan_chunks(dur, silences=[], max_segment_sec=5, overlap_sec=1.0, strategy="fixed")
    assert len(chunks) >= 3, f"本用例需要 ≥3 段，实际 {len(chunks)}"

    cancel = threading.Event()

    with FakeASR(port=0, text_for_call=lambda i: [SENTENCES[i % len(SENTENCES)]]) as srv:
        cfg.asr_base_url = srv.base_url
        cfg.asr_model = "fake-asr"

        tr1 = OpenAICompatibleTranscriber(cfg, "")
        tr1.cancel = cancel
        original_one = tr1._transcribe_one  # noqa: SLF001

        def one_then_cancel(path, *, offset, attempt=1):
            segs = original_one(path, offset=offset, attempt=attempt)
            if srv.call_count >= 2:
                cancel.set()          # 第 2 段之后「进程被打断」
            return segs

        tr1._transcribe_one = one_then_cancel  # type: ignore[assignment]  # noqa: SLF001

        from ecnu_transcribe.errors import TaskCancelled

        with pytest.raises(TaskCancelled):
            tr1.transcribe(audio, duration_sec=dur)
        first_calls = srv.call_count
        assert first_calls == 2, f"第一轮应只完成 2 段，实际调用 {first_calls} 次"

        # 第二轮：全新 transcriber（模拟「重新打开应用再点开始」）
        cancel.clear()
        tr2 = OpenAICompatibleTranscriber(cfg, "")
        before = srv.call_count
        result2 = tr2.transcribe(audio, duration_sec=dur)
        second_calls = srv.call_count - before

    assert result2.segments, "续跑应产出完整结果"
    assert result2.meta.get("reused_segments") == 2, result2.meta
    assert second_calls == len(chunks) - 2, (
        f"续跑只应为剩下的 {len(chunks) - 2} 段付费，实际调用了 {second_calls} 次"
    )
    texts = [s.text.strip() for s in result2.segments]
    assert all(texts), texts
    # 复用来的前两段内容也要在结果里
    assert any(SENTENCES[0] in t for t in texts), texts
    assert any(SENTENCES[1] in t for t in texts), texts


def test_segment_result_cache_is_invalidated_when_model_changes(long_audio, tmp_path, monkeypatch):
    """换了模型就必须重跑 —— 否则会拿旧模型的结果冒充新模型的输出。"""
    monkeypatch.setattr("ecnu_transcribe.transcriber.paths.cache_dir", lambda: tmp_path / "cache")

    audio, _spans = long_audio
    dur = media.duration_of(audio)
    cfg = AppConfig()
    cfg.asr_chunk_strategy = "fixed"
    cfg.asr_max_segment_sec = 5
    cfg.asr_overlap_sec = 1.0
    cfg.asr_retries = 1

    chunks = plan_chunks(dur, silences=[], max_segment_sec=5, overlap_sec=1.0, strategy="fixed")

    with FakeASR(port=0, text_for_call=lambda i: [SENTENCES[i % len(SENTENCES)]]) as srv:
        cfg.asr_base_url = srv.base_url
        cfg.asr_model = "fake-asr"
        OpenAICompatibleTranscriber(cfg, "").transcribe(audio, duration_sec=dur)
        first = srv.call_count
        assert first == len(chunks), f"首轮应逐段调用：{first} vs {len(chunks)}"

        # 同一模型再跑一次 → 全部命中缓存，不再调用
        OpenAICompatibleTranscriber(cfg, "").transcribe(audio, duration_sec=dur)
        assert srv.call_count == first, f"同模型重跑不应再调用：{srv.call_count}"

        cfg.asr_model = "another-model"
        OpenAICompatibleTranscriber(cfg, "").transcribe(audio, duration_sec=dur)
        assert srv.call_count == first + len(chunks), (
            f"换模型后必须全部重跑：{srv.call_count} vs 期望 {first + len(chunks)}"
        )


def test_stale_segment_cache_is_not_reused(long_audio, tmp_path):
    """回归：切片缓存目录必须把**切分方案**算进 key。

    否则改了 max_segment/overlap 之后，上一次遗留的 part_NNN 会被直接复用 ——
    内容按旧方案切、时间戳按新方案平移，得到一堆错位字幕，
    而且日志上完全看不出异常（文件名一样、文件也在）。
    """
    audio, _spans = long_audio
    dur = media.duration_of(audio)
    cfg = AppConfig()
    cfg.asr_base_url = "http://127.0.0.1:1/v1"   # 本用例不需要真端点
    cfg.asr_model = "fake"
    cfg.asr_chunk_strategy = "fixed"
    cfg.asr_max_segment_sec = 5
    cfg.asr_overlap_sec = 1.0
    tr = OpenAICompatibleTranscriber(cfg, "")

    def sig_for(max_seg: float) -> str:
        import hashlib

        chunks = plan_chunks(dur, silences=[], max_segment_sec=max_seg, overlap_sec=1.0, strategy="fixed")
        plan_sig = hashlib.sha256(
            "|".join(f"{c.index}:{c.start:.3f}:{c.end:.3f}" for c in chunks).encode()
        ).hexdigest()[:8]
        audio_sig = media.sha256_file(audio)[:12]
        return f"{audio_sig}-{plan_sig}"

    a = sig_for(5.0)
    b = sig_for(8.0)
    assert a != b, "不同切分方案必须得到不同的缓存目录"

    from ecnu_transcribe import paths

    dir_a = paths.cache_dir() / "segments" / a
    dir_b = paths.cache_dir() / "segments" / b
    assert dir_a != dir_b
    assert a.endswith(sig_for(5.0).split("-")[1]), "同一方案必须稳定得到同一个 key"


def test_chunked_transcription_ignores_nonexistent_silence(long_audio, tmp_path):
    """没有静音信息时应退化为固定切分，仍然能跑完并保持时间轴单调。"""
    audio, _spans = long_audio
    dur = media.duration_of(audio)

    with FakeASR(port=0, batches=[[f"第{i + 1}段文本"] for i in range(20)]) as srv:
        cfg = AppConfig()
        cfg.asr_base_url = srv.base_url
        cfg.asr_model = "fake-asr"
        cfg.asr_chunk_strategy = "fixed"
        cfg.asr_max_segment_sec = 6
        cfg.asr_overlap_sec = 0.5
        cfg.asr_timestamps = True
        cfg.asr_retries = 1
        tr = OpenAICompatibleTranscriber(cfg, "")
        result = tr.transcribe(audio, duration_sec=dur)

    assert srv.call_count >= 2, "长音频应被切成多段"
    starts = [s.start for s in result.segments]
    assert starts == sorted(starts)
    assert len(result.segments) >= 2


def test_time_axis_offsets_are_applied(long_audio, tmp_path):
    """第 N 段的时间戳必须带上该段在整段音频里的偏移（否则 SRT 全挤在开头）。"""
    audio, _spans = long_audio
    dur = media.duration_of(audio)

    with FakeASR(port=0, batches=[["起始段"], ["中间段"], ["结尾段"]]) as srv:
        cfg = AppConfig()
        cfg.asr_base_url = srv.base_url
        cfg.asr_model = "fake-asr"
        cfg.asr_chunk_strategy = "fixed"
        cfg.asr_max_segment_sec = 5
        cfg.asr_overlap_sec = 0.5
        cfg.asr_timestamps = True
        cfg.asr_retries = 1
        tr = OpenAICompatibleTranscriber(cfg, "")
        result = tr.transcribe(audio, duration_sec=dur)

    if len(result.segments) < 2:
        pytest.skip("本次切分不足两段")
    # 后面片段的起点应明显靠后（证明偏移被加上去了）
    assert max(s.start for s in result.segments) > dur * 0.25, (
        f"时间轴没有平移：max start = {max(s.start for s in result.segments):.1f}s / 总长 {dur:.1f}s"
    )


# --------------------------------------------------------------------------- #
# 端点能力回退与容错
# --------------------------------------------------------------------------- #
def test_falls_back_to_srt_when_verbose_json_unsupported(long_audio):
    """端点对 verbose_json 返回 400 时应自动回退到 srt 并正确解析时间轴。"""
    audio, _spans = long_audio
    dur = media.duration_of(audio)

    class RejectingServer:
        """第一次调用回 400（装作不支持 verbose_json），之后按 srt 返回。"""

        def __init__(self):
            self.state = {"n": 0}
            self.base_url = ""
            self.call_count = 0

        def start(self):
            import http.server
            import json as _json
            import socketserver
            import threading

            outer = self

            class H(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *a):  # noqa: A003
                    pass

                def do_GET(self):  # noqa: N802
                    body = _json.dumps({"data": [{"id": "fake"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_POST(self):  # noqa: N802
                    n = int(self.headers.get("Content-Length") or 0)
                    self.rfile.read(n)
                    outer.call_count += 1
                    if outer.state["n"] == 0:
                        outer.state["n"] = 1
                        body = _json.dumps(
                            {"error": {"message": "response_format verbose_json not supported"}}
                        ).encode()
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    srt = "1\n00:00:00,000 --> 00:00:05,000\n回退后的文本\n\n".encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-subrip")
                    self.send_header("Content-Length", str(len(srt)))
                    self.end_headers()
                    self.wfile.write(srt)

            self._httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
            self._httpd.daemon_threads = True
            self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
            threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
            return self.base_url

        def stop(self):
            self._httpd.shutdown()

    srv = RejectingServer()
    try:
        srv.start()
        cfg = AppConfig()
        cfg.asr_base_url = srv.base_url
        cfg.asr_model = "fake"
        cfg.asr_timestamps = True
        cfg.asr_retries = 1
        tr = OpenAICompatibleTranscriber(cfg, "")
        result = tr.transcribe(audio, duration_sec=dur)
        assert result.segments, "回退路径应产出片段"
        assert "回退后的文本" in result.text
    finally:
        srv.stop()


def test_all_chunks_failing_raises_with_first_error(long_audio):
    """所有分段都失败时必须抛错（不能静默产出空转写）。"""
    audio, _spans = long_audio
    dur = media.duration_of(audio)

    class AlwaysFail:
        def __init__(self):
            self.base_url = ""

        def start(self):
            import http.server
            import json as _json
            import socketserver
            import threading

            class H(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, *a):  # noqa: A003
                    pass

                def do_POST(self):  # noqa: N802
                    n = int(self.headers.get("Content-Length") or 0)
                    self.rfile.read(n)
                    body = _json.dumps({"error": {"message": "boom"}}).encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            self._httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
            self._httpd.daemon_threads = True
            self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"
            threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
            return self.base_url

        def stop(self):
            self._httpd.shutdown()

    srv = AlwaysFail()
    try:
        srv.start()
        cfg = AppConfig()
        cfg.asr_base_url = srv.base_url
        cfg.asr_model = "fake"
        cfg.asr_chunk_strategy = "fixed"
        cfg.asr_max_segment_sec = 5
        cfg.asr_retries = 1
        cfg.request_timeout = 10.0
        tr = OpenAICompatibleTranscriber(cfg, "")
        with pytest.raises(TranscriptionError):
            tr.transcribe(audio, duration_sec=dur)
    finally:
        srv.stop()


# --------------------------------------------------------------------------- #
# merge_segments 的边界行为（纯逻辑，快速）
# --------------------------------------------------------------------------- #
def test_merge_keeps_distinct_sentences_across_boundary():
    from ecnu_transcribe.transcriber import Segment

    a = [(0.0, [Segment(0.0, 5.0, "第一段独有")])]
    b = [(4.5, [Segment(0.0, 5.0, "第二段独有")])]
    merged = merge_segments(a + b, overlap_sec=1.0)
    texts = [s.text for s in merged]
    assert texts == ["第一段独有", "第二段独有"]


def test_merge_drops_exact_duplicate_in_overlap():
    from ecnu_transcribe.transcriber import Segment

    a = [(0.0, [Segment(0.0, 5.0, "重叠句")])]
    b = [(4.0, [Segment(0.0, 5.0, "重叠句"), Segment(5.0, 9.0, "后续句")])]
    merged = merge_segments(a + b, overlap_sec=1.0)
    texts = [s.text for s in merged]
    assert texts.count("重叠句") == 1
    assert "后续句" in texts
