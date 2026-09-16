"""ASR 抽象与实现。

**重要事实（务必写进 README 与 GUI 设置页）**

* DeepSeek 开放平台（``api.deepseek.com``）目前只有 ``deepseek-chat`` /
  ``deepseek-reasoner`` 这类**文本**模型，**没有** ``/v1/audio/transcriptions``
  这类语音转文字（ASR）接口。填了 DeepSeek Key **并不能**直接转写。
* 所以本项目的结构是：
    - **ASR（语音→文字）**：走用户配置的**阿里云百炼 DashScope**
      （默认 ``paraformer-v2``，OpenAI 兼容端点），或任意 OpenAI 兼容端点，
      或本地 ``faster-whisper`` 兜底；
    - **LLM（文字加工）**：**DeepSeek** 负责错别字/标点修复、专业术语纠正、
      口语冗余清理、语义分段、摘要大纲（见 :mod:`llm`），可选开关。

切分策略
--------
单文件超过端点上限（默认 25MB）或时长上限（默认 600s）时：
    1. 用 ffmpeg ``silencedetect`` 找静音点，按静音切（``silence`` 策略）；
    2. 找不到合适静音点时退化为等长固定切分（``fixed`` 策略）；
    3. 相邻段重叠 ``asr_overlap_sec``（默认 1.5s），拼接时按时间轴去重；
    4. 失败段落单独记录，可续跑（``state.db`` 的 ``tasks.meta`` 里存段状态）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import httpx

from . import media, paths
from .config import AppConfig, ConfigManager
from .errors import AsrNotConfiguredError, AuthExpiredError, TaskCancelled, TranscriptionError
from .logbus import get_logger, redact, register_secret
from .pausegate import PauseGate

log = get_logger("transcriber")

ProgressFn = Callable[[float, str], None]


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Segment:
    """带时间轴的转写片段。"""

    start: float = 0.0
    end: float = 0.0
    text: str = ""
    speaker: str = ""
    confidence: float = 0.0
    source: str = ""  # 来自哪个分段文件（便于排障）

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Segment":
        return cls(
            start=float(d.get("start") or 0.0),
            end=float(d.get("end") or 0.0),
            text=str(d.get("text") or ""),
            speaker=str(d.get("speaker") or ""),
            confidence=float(d.get("confidence") or 0.0),
            source=str(d.get("source") or ""),
        )


@dataclass
class Transcript:
    """一份完整转写结果。"""

    segments: list[Segment] = field(default_factory=list)
    language: str = ""
    duration_sec: float = 0.0
    model: str = ""
    provider: str = ""
    created_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return join_segment_text(self.segments)

    @property
    def char_count(self) -> int:
        return len(self.text)

    def is_empty(self) -> bool:
        return not any(s.text.strip() for s in self.segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "duration_sec": self.duration_sec,
            "model": self.model,
            "provider": self.provider,
            "created_at": self.created_at,
            "char_count": self.char_count,
            "segment_count": len(self.segments),
            "meta": self.meta,
            "segments": [s.to_dict() for s in self.segments],
        }

    def save_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load_json(cls, path: Path) -> "Transcript":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        t = cls(
            language=str(data.get("language", "")),
            duration_sec=float(data.get("duration_sec") or 0.0),
            model=str(data.get("model", "")),
            provider=str(data.get("provider", "")),
            created_at=float(data.get("created_at") or time.time()),
            meta=dict(data.get("meta") or {}),
        )
        t.segments = [Segment.from_dict(d) for d in data.get("segments", [])]
        return t


# --------------------------------------------------------------------------- #
# 文本拼接（时间轴去重）
# --------------------------------------------------------------------------- #
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _normalize_for_compare(text: str) -> str:
    return re.sub(r"[\s，。、！？；：,.!?;:\"'（）()\[\]【】…—\-]+", "", text or "")


def join_segment_text(segments: Iterable[Segment]) -> str:
    """把片段拼成纯文本：中文之间不加空格，英文/数字之间补空格。"""
    parts: list[str] = []
    for seg in segments:
        t = (seg.text or "").strip()
        if not t:
            continue
        if not parts:
            parts.append(t)
            continue
        prev = parts[-1]
        if _CJK.search(prev[-1:]) or _CJK.search(t[:1]):
            parts.append(t)
        else:
            parts.append(" " + t)
    return "".join(parts)


def merge_segments(
    chunks: list[tuple[float, list[Segment]]],
    *,
    overlap_sec: float = 1.5,
    dedup: bool = True,
) -> list[Segment]:
    """把多个分段的转写结果按时间轴平移、去重、排序后合并。

    ``chunks``: ``[(offset_sec, segments_of_that_chunk), ...]``

    **offset 语义**：每个 ``seg`` 的时间戳被当作**该分段内部的相对时间**，
    输出时统一加上 ``offset`` 变成全局时间轴。
    如果调用方拿到的 ``seg`` 已经是全局时间戳（例如
    ``_transcribe_one(..., offset=chunk.start)`` 的返回值），
    必须传 ``offset=0.0``，否则偏移会被叠加两次。

    去重规则：因为相邻段有 ``overlap_sec`` 重叠，重叠区间内的句子会重复出现。
    做法是把每个段的时间平移到全局轴，然后
        1. 丢弃与已有段「时间区间重叠 > 50% 且文本高度相似」的段；
        2. 对文本完全相同且时间接近（< overlap+2s）的段直接去重。
    """
    merged: list[Segment] = []
    for offset, segs in chunks:
        for seg in segs:
            start = max(0.0, seg.start + offset)
            end = max(start + 0.05, seg.end + offset)
            cand = Segment(
                start=start,
                end=end,
                text=(seg.text or "").strip(),
                speaker=seg.speaker,
                confidence=seg.confidence,
                source=seg.source,
            )
            if not cand.text:
                continue
            if dedup and merged and _is_duplicate(merged, cand, overlap_sec=overlap_sec):
                continue
            merged.append(cand)
    merged.sort(key=lambda s: (s.start, s.end))
    return merged


def _is_duplicate(existing: list[Segment], cand: Segment, *, overlap_sec: float) -> bool:
    """判断 ``cand`` 是否是紧邻历史段的重复。

    两类重复：
    1. **文本重复**（相邻段有重叠时必然发生）：文本相同，或其中一个是另一个的
       子串/高度相似前缀 —— 只要在时间上足够接近就算重复；
    2. **时间重叠重复**：文本高度相似且时间区间重叠超过 ``cand`` 时长的一半。

    注意：绝不能把「相隔很远的同义短句」当重复（把时间门的阈值设得足够宽
    以容忍切分点抖动，但又不能大到跨越大半节课）。
    """
    norm = _normalize_for_compare(cand.text)
    if not norm:
        return True
    for prev in reversed(existing[-8:]):
        prev_norm = _normalize_for_compare(prev.text)
        if not prev_norm:
            continue

        gap = cand.start - prev.end  # 负值 = 时间重叠
        # 时间门：相隔太远的历史段不参与文本判重
        if gap > max(30.0, overlap_sec * 20):
            continue

        if prev_norm == norm:
            return True

        shorter = min(len(prev_norm), len(norm)) or 1
        identical = prev_norm in norm or norm in prev_norm
        common = 0
        for a, b in zip(prev_norm, norm):
            if a != b:
                break
            common += 1
        similar = identical and (shorter / max(len(prev_norm), len(norm))) >= 0.55
        prefix_similar = common / shorter >= 0.75

        if similar or prefix_similar:
            # 时间重叠则直接判重
            overlap = min(prev.end, cand.end) - max(prev.start, cand.start)
            if overlap > 0 or gap <= overlap_sec * 2 + 2.0:
                return True
    return False


# --------------------------------------------------------------------------- #
# 切分
# --------------------------------------------------------------------------- #
@dataclass
class AudioChunk:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def plan_chunks(
    duration: float,
    *,
    silences: list[media.SilenceSpan] | None = None,
    max_segment_sec: float = 600.0,
    overlap_sec: float = 1.5,
    min_segment_sec: float = 20.0,
    strategy: str = "silence",
) -> list[AudioChunk]:
    """规划切分方案。

    * ``silence``：优先在静音区中点下刀，单段不超过 ``max_segment_sec``；
    * ``fixed``：等长切分（无静音信息时的退化方案）；
    * 段间重叠 ``overlap_sec``，切点不会切进静音区的边缘 0.2s 内。
    """
    duration = max(0.0, float(duration))
    if duration <= max_segment_sec or max_segment_sec <= 0:
        return [AudioChunk(0, 0.0, duration)] if duration > 0 else []

    # 候选切点：静音区中点
    cut_candidates: list[float] = []
    if strategy == "silence" and silences:
        for span in silences:
            mid = (span.start + span.end) / 2.0
            if 0.5 < mid < duration - 0.5:
                cut_candidates.append(mid)
        cut_candidates.sort()

    chunks: list[AudioChunk] = []
    start = 0.0
    idx = 0
    guard = 0
    #: 收尾门槛：最后一段若短于这个比例，就并入上一段而不是单独成段
    tail_ratio = 0.25
    while start < duration - 0.5 and guard < 10000:
        guard += 1
        hard_end = min(duration, start + max_segment_sec)
        if hard_end >= duration - 0.5:
            chunks.append(AudioChunk(idx, start, duration))
            break

        # 在 (start + min_segment_sec, hard_end] 里挑最靠后的静音切点
        lower = start + max(min_segment_sec, max_segment_sec * 0.4)
        pick: float | None = None
        for cand in cut_candidates:
            if lower <= cand <= hard_end:
                pick = cand
        if pick is None:
            upper = [c for c in cut_candidates if cand_ok(c, start, hard_end)]
            pick = upper[-1] if upper else hard_end
        end = max(start + 1.0, min(pick, hard_end))

        # 若剩余尾巴太短，直接并入本段收尾（避免一个只有几秒的碎片段）
        remaining = duration - end
        if 0 < remaining < max_segment_sec * tail_ratio:
            chunks.append(AudioChunk(idx, start, duration))
            break

        chunks.append(AudioChunk(idx, start, end))
        idx += 1
        start = max(start + 1.0, end - overlap_sec)

    if not chunks:
        chunks = [AudioChunk(0, 0.0, duration)]
    return chunks


def cand_ok(cand: float, start: float, hard_end: float) -> bool:
    return start + 5.0 <= cand <= hard_end


def _clamp_to_range(segments: list[Segment], start: float, end: float) -> None:
    """把片段时间戳夹进 ``[start, end]``（原地修改）。

    ffmpeg 用 ``-ss/-t`` 切片时会略微超出请求长度，真实 ASR 端点也会按实际解码长度
    返回时间戳；不夹住就会得到「结束时间超过音频总长」的片段，
    写进 SRT 后是一条永远播不到的字幕。
    """
    lo, hi = float(start), float(end)
    if hi <= lo:
        return
    for seg in segments:
        if seg.start < lo:
            seg.start = lo
        if seg.end > hi:
            seg.end = hi
        if seg.end <= seg.start:
            # 夹完变成零长度（例如整个片段都在下一段的区域）→ 给一个最小可见时长
            seg.end = min(hi, seg.start + 0.2)
            if seg.end <= seg.start:
                seg.start = max(lo, seg.end - 0.2)


# --------------------------------------------------------------------------- #
# 抽象接口
# --------------------------------------------------------------------------- #
class Transcriber(Protocol):
    """转写器接口：``transcribe(audio_path) -> Transcript``。"""

    name: str

    def transcribe(self, audio_path: Path, *, duration_sec: float = 0.0) -> Transcript:  # pragma: no cover
        ...


# --------------------------------------------------------------------------- #
# OpenAI 兼容实现
# --------------------------------------------------------------------------- #
class OpenAICompatibleTranscriber:
    """任意 OpenAI 兼容的 ``/v1/audio/transcriptions`` 端点。

    端点能力差异用 ``cfg.extra`` 里的开关描述：
        * ``supports_verbose_json``：是否支持 ``response_format=verbose_json``（带时间戳）
        * ``supports_timestamps``：不支持 verbose_json 时退化为 ``srt`` 文本解析

    **关于 API Key**：本地端点（``localhost`` / ``127.0.0.1``，例如项目自带的
    ``scripts/local_asr_server.py``）通常不校验鉴权，因此这里允许 Key 为空 ——
    为空时不发送 ``Authorization`` 头。只有**远程**端点才强制要求 Key。
    """

    name = "openai_compatible"

    def __init__(
        self,
        cfg: AppConfig,
        api_key: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        on_progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
        gate: PauseGate | None = None,
    ) -> None:
        self.cfg = cfg
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or cfg.asr_base_url).rstrip("/")
        self.model = model or cfg.asr_model
        if self.api_key:
            register_secret(self.api_key)
        elif not self.is_local_endpoint():
            raise AsrNotConfiguredError(
                f"未配置 ASR API Key（设置页 → 语音识别）。\n"
                f"  端点：{self.base_url}\n"
                f"  若这是你自己在本机跑的 OpenAI 兼容服务（如 scripts/local_asr_server.py），"
                f"请把 Base URL 设为 http://127.0.0.1:<端口>/v1 —— 本机端点不要求 Key。"
            )
        self.on_progress = on_progress or (lambda _p, _m: None)
        self.cancel = cancel or threading.Event()
        self.gate = gate
        #: 是否走 DashScope 的「chat + input_audio」调用方式（缺陷 55）。
        #: 该端点上的千问/Fun ASR 只能这样调（``/audio/transcriptions`` 一律 404）。
        self.chat_audio = _use_chat_audio(cfg)

    def is_local_endpoint(self) -> bool:
        """Base URL 指向本机时不要求 API Key（本地服务通常不校验鉴权）。"""
        from urllib.parse import urlparse

        host = (urlparse(self.base_url).hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal")

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _wait_gate(self) -> None:
        """在天然断点处响应「暂停」：阻塞直到继续/取消。

        暂停语义是「**在分段边界停下**」——当前正在调用的那一段不会被打断
        （打断会白费已花的 ASR 额度），停下后已完成的段落全部保留，继续时不再重跑。
        """
        if self.gate is None or not self.gate.is_paused:
            return
        self._notify(0.0, "⏸ 已暂停（分段边界，点「继续」恢复）")
        idle = 0.0
        while self.gate.is_paused and not self.cancel.is_set():
            self.gate.wait(timeout=2.0)
            idle += 2.0
            if idle >= 10.0:
                idle = 0.0
                self._notify(0.0, f"⏸ 暂停中（已暂停 {self.gate.paused_seconds():.0f}s）")
        if not self.cancel.is_set():
            self._notify(0.0, "▶ 已恢复")

    def _notify(self, pct: float, msg: str) -> None:
        try:
            self.on_progress(pct, msg)
        except Exception:
            pass

    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/audio/transcriptions"):
            return base
        return f"{base}/audio/transcriptions"

    def effective_max_segment(self) -> float:
        """本次实际使用的「单段最长秒数」——**受模型硬上限约束**（缺陷 62）。

        实测（2026-09-14，用真实 Key 打出来的边界）：

        | 输入 | 结果 |
        | --- | --- |
        | 300s @64kbps（2.29 MB） | ✅ 通过 |
        | **301s** @64kbps（2.30 MB） | ⛔ ``The audio is too long`` |
        | 300s @192kbps（5.72 MB） | ✅ 通过 |

        ⇒ ``qwen3-asr-flash`` 的单请求上限是 **时长 300 秒**（与体积无关）。
        用户配置里的默认值是 600s，于是整条课 6 段**全部**被拒（日志里
        ``全部 6 段都失败了``）。这里统一夹到 300s 以下并留出余量：
        静音切分可能让某段略微超过设定值（切点落在静音区间边界），
        所以取 ``CHAT_AUDIO_MAX_SEGMENT_SEC = 270``，不贴着 300 走。

        用户**可以**把它调得更小（字幕更细），这里只做上限夹取。
        """
        configured = float(self.cfg.asr_max_segment_sec or 600)
        if getattr(self, "chat_audio", False):
            return min(configured, CHAT_AUDIO_MAX_SEGMENT_SEC)
        return configured

    def transcribe(self, audio_path: Path, *, duration_sec: float = 0.0) -> Transcript:
        path = Path(audio_path)
        if not path.is_file():
            raise TranscriptionError(f"音频文件不存在：{path}")
        duration = duration_sec or media.duration_of(path)
        size_mb = path.stat().st_size / 1024 / 1024
        limit_mb = float(self.cfg.asr_max_upload_mb or 25.0)
        max_seg = self.effective_max_segment()

        if size_mb <= limit_mb and (duration <= max_seg or duration <= 0):
            self._notify(10.0, f"整段转写（{size_mb:.1f}MB / {duration:.0f}s）")
            segs = self._transcribe_one(path, offset=0.0)
            return Transcript(
                segments=segs,
                language=self.cfg.asr_language,
                duration_sec=duration,
                model=self.model,
                provider=self.name,
                meta={"single_file": True, "size_mb": round(size_mb, 2)},
            )

        self._notify(5.0, f"文件 {size_mb:.1f}MB / {duration:.0f}s 超限，按静音切分…")
        return self.transcribe_chunked(path, duration_sec=duration)

    def _segment_usable(self, seg_file: Path, chunk: AudioChunk) -> bool:
        """判断缓存的分段切片能否直接复用。

        **不能只看文件存在**：切片是按 ``part_NNN`` 命名的，一旦上一次运行的方案
        与这一次不同（或上一次被中断留下半截文件），就会拿旧内容配新偏移，
        产出错位字幕而且日志毫无异常。所以这里按**时长**校验：

        * 文件存在且不小于 512 字节；
        * 探测到的时长与期望时长的偏差不超过 25%（且不小于 0.3s）。

        探测失败（无法读取）时按不可用处理，宁可重新切一次。
        """
        try:
            if not seg_file.is_file() or seg_file.stat().st_size < 512:
                return False
            expected = chunk.duration
            if expected <= 0:
                return True
            actual = media.duration_of(seg_file)
            if actual <= 0:
                return False
            if actual < 0.3:
                return False
            return abs(actual - expected) <= max(0.5, expected * 0.25)
        except OSError:
            return False

    # ------------------------------------------------------------------ #
    # 分段结果的落盘与复用（缺陷 46）
    # ------------------------------------------------------------------ #
    def _segment_result_path(self, work_dir: Path, index: int) -> Path:
        return work_dir / f"result_{index:03d}.json"

    def _load_segment_result(
        self, work_dir: Path, index: int, chunk: AudioChunk
    ) -> list[Segment] | None:
        """读回上一次运行留下的**分段转写结果**；不匹配就返回 None。

        为什么必须落盘：一节课 55 分钟会被切成十几段，云端 ASR 逐段调用要花真金白银。
        原先结果只在内存里、成功后连切片都删了 —— 中途一断（崩溃/关机/被环境回收），
        重跑就得**把已经付过费的分段全部重来一遍**。

        校验三件事，任一不符就当没有缓存：
        * ``index`` 与 ``start``/``end`` 与本次切分方案一致（防止方案变了却复用旧结果）；
        * ``model`` 与 ``provider`` 一致（换模型必须重跑）；
        * 结果本身可解析（半个文件、手改坏了都按无效处理）。
        """
        p = self._segment_result_path(work_dir, index)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            if int(data["index"]) != index:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        if str(data.get("model") or "") != str(self.model or ""):
            return None
        if str(data.get("provider") or "") != str(self.name or ""):
            return None
        # ⚠️ 这里不能用 `data.get("start") or -1`：**第一段的 start 就是 0.0**，
        # 而 `0.0 or -1` 得到 -1 —— 那样第一段永远命不中缓存（真实踩过：
        # 日志上表现为「只有第 2..N 段被复用」，第 1 段每次都重新花钱识别）。
        try:
            start = float(data["start"])
            end = float(data["end"])
        except (KeyError, TypeError, ValueError):
            return None
        if abs(start - chunk.start) > 0.5 or abs(end - chunk.end) > 0.5:
            return None
        raw = data.get("segments")
        if not isinstance(raw, list):
            return None
        out: list[Segment] = []
        for item in raw:
            if not isinstance(item, dict):
                return None
            try:
                out.append(
                    Segment(
                        start=float(item["start"]),
                        end=float(item["end"]),
                        text=str(item.get("text") or ""),
                        source=str(item.get("source") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                return None
        return out

    def _save_segment_result(
        self, work_dir: Path, index: int, chunk: AudioChunk, segs: list[Segment]
    ) -> None:
        """原子落盘一段的结果（写 ``.tmp`` 再替换，避免断电留下半个 JSON）。"""
        p = self._segment_result_path(work_dir, index)
        tmp = p.with_suffix(p.suffix + ".tmp")
        payload = {
            "index": index,
            "start": chunk.start,
            "end": chunk.end,
            "model": str(self.model or ""),
            "provider": str(self.name or ""),
            "segments": [
                {"start": s.start, "end": s.end, "text": s.text, "source": s.source} for s in segs
            ],
        }
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except OSError as exc:
            log.debug("写分段结果失败（不影响本次转写）：%s", exc)

    def transcribe_chunked(self, audio_path: Path, *, duration_sec: float = 0.0) -> Transcript:
        """切分 + 逐段转写 + 时间轴平移去重合并。"""
        path = Path(audio_path)
        duration = duration_sec or media.duration_of(path)
        silences: list[media.SilenceSpan] = []
        if self.cfg.asr_chunk_strategy == "silence":
            try:
                silences = media.detect_silences(path, timeout=3600)
                log.info("静音检测到 %s 个静音区间", len(silences))
            except Exception as exc:
                log.warning("静音检测失败，退化为固定切分：%s", exc)

        chunks = plan_chunks(
            duration,
            silences=silences,
            max_segment_sec=self.effective_max_segment(),
            overlap_sec=float(self.cfg.asr_overlap_sec or 1.5),
            min_segment_sec=float(self.cfg.asr_min_segment_sec or 1.0) * 20,
            strategy=self.cfg.asr_chunk_strategy,
        )
        log.info("切分方案：%s 段（策略=%s，重叠=%.1fs）", len(chunks), self.cfg.asr_chunk_strategy, self.cfg.asr_overlap_sec)

        # 切片缓存目录必须把**切分方案**也算进 key。
        # 只用音频哈希的话，改了 max_segment/overlap/策略之后，
        # 上一次运行遗留的 part_NNN 会被当成这一次的切片直接复用 ——
        # 内容是旧方案的、时间戳却按新方案平移，换来一堆错位的字幕
        # （这个坑很隐蔽：文件名一样、文件也存在、单看日志毫无异常）。
        plan_sig = hashlib.sha256(
            "|".join(
                f"{c.index}:{c.start:.3f}:{c.end:.3f}" for c in chunks
            ).encode("utf-8")
        ).hexdigest()[:8]
        audio_sig = media.sha256_file(path)[:12]
        work_dir = paths.cache_dir() / "segments" / f"{audio_sig}-{plan_sig}"
        work_dir.mkdir(parents=True, exist_ok=True)
        log.debug("切片缓存目录：%s（段数=%s）", work_dir, len(chunks))

        results: list[tuple[float, list[Segment]]] = []
        failures: list[dict[str, Any]] = []
        reused = 0
        for i, chunk in enumerate(chunks, 1):
            # 天然断点：每一段转写前都可以暂停/取消（长课程不用等整段跑完）
            self._wait_gate()
            if self.cancel.is_set():
                raise TaskCancelled("转写被用户取消")
            base_pct = (i - 1) / max(1, len(chunks)) * 85.0
            self._notify(base_pct, f"第 {i}/{len(chunks)} 段（{chunk.start:.0f}-{chunk.end:.0f}s）")
            seg_file = work_dir / f"part_{i:03d}.{self.cfg.audio_format}"

            # ① 先看有没有上一次留下的**结果**（省一次 ASR 调用/一次云端计费）
            cached = self._load_segment_result(work_dir, i, chunk)
            if cached is not None:
                reused += 1
                self._notify(base_pct, f"第 {i}/{len(chunks)} 段：复用上次结果（不重复识别）")
                log.info("第 %s 段复用已缓存结果（%s 条）", i, len(cached))
                results.append((0.0, cached))
                continue

            try:
                if not self._segment_usable(seg_file, chunk):
                    media.extract_segment(
                        path,
                        seg_file,
                        chunk.start,
                        chunk.end,
                        fmt=self.cfg.audio_format,
                        sample_rate=self.cfg.audio_sample_rate,
                        channels=self.cfg.audio_channels,
                        bitrate=self.cfg.audio_bitrate,
                    )
                segs = self._transcribe_one(seg_file, offset=chunk.start, attempt=1)
                for s in segs:
                    s.source = seg_file.name
                # 分段的边界是**权威**的：端点返回的时间戳可能超出这一段
                # （ffmpeg 切片本身会略微超出 -t，真实端点也会按实际解码长度给时间戳），
                # 不夹住的话合并后会出现「时间轴超过音频总长」的字幕，播不出来。
                _clamp_to_range(segs, chunk.start, chunk.end)
                # segs 已经是**全局时间戳**（_transcribe_one 传了 offset=chunk.start），
                # 所以这里传 0.0，配合 merge_segments 的 offset 语义（局部时间 + offset）。
                # 曾经这里传 chunk.start，导致偏移被叠加两次：第 N 段变成 2*start+local，
                # 越往后越离谱（5 段 20s 音频的末段被算到 32~36s），而日志毫无异常。
                results.append((0.0, segs))
                # ② 结果先落盘，再删切片 —— 顺序反了的话「删了切片又没存结果」
                #    就等于这一段白跑（而且钱已经花掉了）。
                self._save_segment_result(work_dir, i, chunk, segs)
                try:
                    seg_file.unlink(missing_ok=True)
                except OSError:
                    pass
            except Exception as exc:  # noqa: BLE001
                log.error("第 %s 段转写失败：%s", i, exc)
                failures.append(
                    {"index": i, "start": chunk.start, "end": chunk.end, "error": str(exc)[:500]}
                )
                self._notify(base_pct, f"第 {i} 段失败：{exc}")
        if reused:
            log.info("本次共复用 %s/%s 段已缓存结果（未重复调用 ASR）", reused, len(chunks))

        # 注意：``_transcribe_one(..., offset=chunk.start)`` 已经把时间戳平移到了全局轴，
        # 所以这里必须传 offset=0.0 —— 再传 chunk.start 会把偏移**叠加两次**，
        # 第 N 段的时间戳会变成 2*start+local，越往后的字幕越离谱
        # （真实踩过：5 段 20s 音频，末段被算到 32~36s；而且日志上完全看不出异常）。
        merged = merge_segments(results, overlap_sec=float(self.cfg.asr_overlap_sec or 1.5))
        if merged:
            _clamp_to_range(merged, 0.0, duration)
        if failures and not merged:
            raise TranscriptionError(
                f"全部 {len(chunks)} 段都失败了；首个错误：{failures[0]['error']}"
            )
        return Transcript(
            segments=merged,
            language=self.cfg.asr_language,
            duration_sec=duration,
            model=self.model,
            provider=self.name,
            meta={
                "chunk_count": len(chunks),
                "chunk_strategy": self.cfg.asr_chunk_strategy,
                "overlap_sec": self.cfg.asr_overlap_sec,
                "failed_chunks": failures,
                "silence_count": len(silences),
                #: 本次有多少段是复用上次结果（没重复调 ASR）——续跑是否省钱看这个数
                "reused_segments": reused,
            },
        )

    # ------------------------------------------------------------------ #
    def _transcribe_one(self, path: Path, *, offset: float, attempt: int = 1) -> list[Segment]:
        """对单个音频文件调用一次 ASR。"""
        max_tries = max(1, int(self.cfg.asr_retries))
        last: Exception | None = None
        for try_no in range(1, max_tries + 1):
            # 每次真正发起请求前都过一遍闸门：暂停时不会白白多打一次 ASR
            self._wait_gate()
            if self.cancel.is_set():
                raise TaskCancelled("转写被用户取消")
            try:
                return self._call_endpoint(path, offset=offset)
            except _AuthFailed as exc:
                raise AuthExpiredError(f"ASR 端点鉴权失败：{exc}") from exc
            except _PermanentAsrError as exc:
                # 配置 / 环境类问题：重试不会有帮助，直接失败并说明
                raise TranscriptionError(f"ASR 端点不可用（重试无意义）：{exc}") from exc
            except Exception as exc:  # noqa: BLE001
                last = exc
                if _is_permanent_error_text(str(exc)):
                    # 「音频过长」不是环境问题，别把它混进"缺 CUDA / 模型名不存在"那套提示里
                    # —— 实测用户就是照着那句提示去查环境，而真正该调的是分段长度。
                    if _is_audio_too_long(str(exc)):
                        raise TranscriptionError(
                            f"ASR 拒绝：单次请求的音频过长（{exc}）。\n"
                            "提示：把设置里的「单段 ASR 最长秒数」调小即可"
                            "（千问 ASR 实测上限 300 秒；本应用对这类模型已自动夹到 270 秒）。"
                        ) from exc
                    raise TranscriptionError(
                        f"ASR 调用失败且属于不可重试的错误：{exc}\n"
                        "提示：这通常是端点配置或运行环境问题（例如缺少 CUDA 运行库、"
                        "本地服务未启动、模型名不存在）。请修正后重跑——已完成的音频与分段不会重做。"
                    ) from exc
                if try_no >= max_tries:
                    break
                wait = min(60.0, (2 ** try_no) + random.uniform(0, 1.5))
                log.warning(
                    "ASR 调用失败（第 %s/%s 次）：%s；%.1fs 后重试", try_no, max_tries, exc, wait
                )
                self._notify(0.0, f"ASR 重试 {try_no}/{max_tries}（{wait:.0f}s）：{exc}")
                end = time.time() + wait
                while time.time() < end:
                    self._wait_gate()
                    if self.cancel.is_set():
                        raise TaskCancelled("转写被用户取消")
                    time.sleep(0.25)
        raise TranscriptionError(f"ASR 调用失败（重试 {max_tries} 次）：{last}")

    def _call_endpoint(self, path: Path, *, offset: float) -> list[Segment]:
        if self.chat_audio:
            return self._call_endpoint_chat_audio(path, offset=offset)
        data: dict[str, Any] = {
            "model": self.model,
            "language": self.cfg.asr_language or "zh",
        }
        if self.cfg.asr_vocabulary:
            data["prompt"] = self.cfg.asr_vocabulary[:800]
        want_ts = bool(self.cfg.asr_timestamps)
        data["response_format"] = "verbose_json" if want_ts else "json"

        headers = self._auth_headers()
        timeout = httpx.Timeout(600.0, connect=30.0)

        with open(path, "rb") as fh:
            files = {"file": (path.name, fh, "application/octet-stream")}
            with httpx.Client(timeout=timeout, trust_env=not self.cfg.proxy, proxy=self.cfg.proxy or None) as client:
                resp = client.post(self._endpoint(), headers=headers, data=data, files=files)

        if resp.status_code in (401, 403):
            raise _AuthFailed(f"HTTP {resp.status_code}: {redact(resp.text[:200])}")
        if resp.status_code == 400 and "response_format" in resp.text.lower():
            # 端点不支持 verbose_json，退回 srt 或 json
            return self._call_endpoint_fallback(path, offset=offset)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TranscriptionError(
                f"ASR 端点暂时不可用（HTTP {resp.status_code}），将退避重试：{redact(resp.text[:300])}"
            )
        if resp.status_code >= 400:
            raise TranscriptionError(
                f"ASR HTTP {resp.status_code}: {redact(resp.text[:400])}"
            )

        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            payload = resp.json()
            return self._parse_json_payload(payload, offset=offset)
        # 某些端点直接回 srt / text
        return parse_srt_text(resp.text, offset=offset)

    def _call_endpoint_chat_audio(
        self, path: Path, *, offset: float, _depth: int = 0
    ) -> list[Segment]:
        """DashScope 千问/Fun ASR 的**实测可用**调用方式（缺陷 55）。

        2026-09-14 在这个端点上逐条实测得到的结论（别再凭直觉改）：

        * ``POST {base}/audio/transcriptions``：**对所有 ASR 模型都 404**
          —— 应用原先的云端路因此整条不可用；
        * ``POST {base}/chat/completions``，content 里放
          ``{"type": "input_audio", "input_audio": {"data": "data:audio/mpeg;base64,..."}}``
          → **200 且质量很好**（同一段 60s 课堂音频：本机 small 出 347 字且大量繁体，
          千问出 377 字简体带标点，2.2 秒完成 ≈27× 实时）；
        * ``input_audio.data`` **必须是 data URL**：裸 base64 会被当成 URL，
          报 ``The provided URL does not appear to be valid``；
        * content 里**只能有音频项**：再加一个 text 项会报
          ``The dedicated task `asr` corresponding to the current service does not support this input``；
        * 返回的是**纯文本、没有时间戳**（``asr_options.enable_timestamp`` 实测无效）。

        所以时间轴只能由**调用方按静音切分的边界**给出：这里把整段文本作为
        **一个** segment 返回，区间就是该切片自身的 ``[offset, offset+时长]``
        —— 宁可给粗但真实的时间，也不按标点去"编"时间戳。
        """
        b64 = base64.b64encode(path.read_bytes()).decode()
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "input_audio",
                    "input_audio": {"data": f"data:audio/mpeg;base64,{b64}"},
                }],
            }],
        }
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0), trust_env=not self.cfg.proxy,
                          proxy=self.cfg.proxy or None) as client:
            resp = client.post(f"{self._chat_endpoint()}", headers=self._auth_headers(), json=body)
        if resp.status_code in (401, 403):
            raise _AuthFailed(f"HTTP {resp.status_code}: {redact(resp.text[:200])}")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TranscriptionError(
                f"ASR 端点暂时不可用（HTTP {resp.status_code}），将退避重试：{redact(resp.text[:300])}"
            )
        if resp.status_code >= 400:
            detail = redact(resp.text[:400])
            if _is_audio_too_long(detail):
                return self._split_and_retry(path, offset=offset, depth=_depth, detail=detail)
            raise TranscriptionError(f"ASR HTTP {resp.status_code}: {detail}")
        payload = resp.json()
        text = _chat_message_text(payload)
        if not text:
            return []
        return [Segment(start=offset, end=offset + media.duration_of(path), text=text)]

    def _split_and_retry(self, path: Path, *, offset: float, depth: int, detail: str) -> list[Segment]:
        """被拒为「音频过长」时**自动对半切开重试**（最多 3 层）。

        这是一道兜底：分片上限已经按实测夹过（``CHAT_AUDIO_MAX_SEGMENT_SEC``），
        但上限可能随模型/账号变化。与其把整条课判失败、让用户自己去猜该调哪个参数，
        不如就地切一半再试 —— 每半段各自带上正确的起始偏移，时间轴不受影响。
        """
        dur = media.duration_of(path)
        if depth >= 3 or dur <= 30:
            raise TranscriptionError(
                f"ASR HTTP 400：音频过长，且已自动对半重试 {depth} 次仍被拒。\n"
                f"当前片段 {dur:.0f}s —— 请把设置里的「单段 ASR 最长秒数」调小"
                f"（例如 60）。原始报错：{detail}"
            )
        mid = dur / 2.0
        log.warning("片段 %.0fs 被拒为过长，自动对半切分后重试（第 %s 层）", dur, depth + 1)
        out: list[Segment] = []
        tmp_dir = paths.cache_dir() / "split"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        halves = ((0.0, mid), (mid, dur))
        for idx, (a, b) in enumerate(halves):
            clip = tmp_dir / f"{path.stem}_split{depth}_{idx}.{self.cfg.audio_format or 'mp3'}"
            try:
                media.extract_segment(
                    path, clip, a, b,
                    fmt=self.cfg.audio_format or "mp3",
                    sample_rate=self.cfg.audio_sample_rate,
                    channels=self.cfg.audio_channels,
                    bitrate=self.cfg.audio_bitrate,
                )
                out.extend(self._call_endpoint_chat_audio(clip, offset=offset + a, _depth=depth + 1))
            finally:
                clip.unlink(missing_ok=True)
        return out

    def _chat_endpoint(self) -> str:
        """``…/compatible-mode/v1`` → ``…/compatible-mode/v1/chat/completions``。"""
        base = (self.cfg.asr_base_url or "").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _call_endpoint_fallback(self, path: Path, *, offset: float) -> list[Segment]:
        fmt = "srt" if self.cfg.asr_timestamps else "json"
        data = {
            "model": self.model,
            "language": self.cfg.asr_language or "zh",
            "response_format": fmt,
        }
        headers = self._auth_headers()
        with open(path, "rb") as fh:
            files = {"file": (path.name, fh, "application/octet-stream")}
            with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0), trust_env=not self.cfg.proxy,
                              proxy=self.cfg.proxy or None) as client:
                resp = client.post(self._endpoint(), headers=headers, data=data, files=files)
        if resp.status_code >= 400:
            raise TranscriptionError(f"ASR HTTP {resp.status_code}: {redact(resp.text[:400])}")
        if fmt == "srt":
            return parse_srt_text(resp.text, offset=offset)
        payload = resp.json()
        if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
            return self._parse_json_payload(payload, offset=offset)
        text = str(payload.get("text", "") if isinstance(payload, dict) else payload)
        dur = media.duration_of(path)
        return [Segment(start=offset, end=offset + dur, text=text.strip())] if text.strip() else []

    @staticmethod
    def _parse_json_payload(payload: Any, *, offset: float) -> list[Segment]:
        if not isinstance(payload, dict):
            return []
        segs_raw = payload.get("segments")
        out: list[Segment] = []
        if isinstance(segs_raw, list) and segs_raw:
            for item in segs_raw:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                start = _first_float(item, "start", "start_time", "begin", "from", "timestamp")
                end = _first_float(item, "end", "end_time", "stop", "to")
                if end <= start:
                    end = start + _first_float(item, "duration", default=2.0)
                out.append(
                    Segment(
                        start=offset + start,
                        end=offset + end,
                        text=text,
                        confidence=_first_float(item, "confidence", "probability", "score"),
                    )
                )
            return out
        text = str(payload.get("text") or "").strip()
        if text:
            return [Segment(start=offset, end=offset, text=text)]
        return []


class _AuthFailed(Exception):
    """内部：ASR 端点鉴权失败（转成 AuthExpiredError 抛出）。"""


#: 这些模型在 DashScope 上**只能**通过 ``/chat/completions`` + ``input_audio`` 调用
#: （实测 ``/audio/transcriptions`` 对它们一律 404）。``realtime`` 变体走 websocket，
#: 不在本实现的覆盖范围内，故意排除。
_CHAT_AUDIO_HINTS = ("qwen3-asr", "qwen-audio", "qwen-asr", "fun-asr")

#: 走 ``/chat/completions`` 的 ASR 模型的**单请求时长上限**（实测，缺陷 62）。
#: `qwen3-asr-flash`：300s 通过、301s 报 ``The audio is too long``；
#: 300s@192kbps（5.72 MB）仍通过 ⇒ 是**时长**限制而非体积限制。
#: 这里留 ~10% 余量取 270s：静音切分可能让某一段略微超过设定值。
CHAT_AUDIO_MAX_SEGMENT_SEC = 270.0


def _use_chat_audio(cfg: AppConfig) -> bool:
    """判断当前配置是否应该走「chat + input_audio」调用方式。

    显式开关 ``cfg.asr_chat_audio`` 优先（用户/测试可强制开或关）；
    否则按模型名自动判断 —— 因为对这些模型来说，老路径是**必然 404** 的。
    """
    explicit = getattr(cfg, "asr_chat_audio", None)
    if explicit is not None:
        return bool(explicit)
    model = (cfg.asr_model or "").lower()
    if "realtime" in model:
        return False
    return any(k in model for k in _CHAT_AUDIO_HINTS)


def _is_audio_too_long(detail: str) -> bool:
    """识别「音频过长」这类 400（实测原文：``The audio is too long``）。"""
    low = (detail or "").lower()
    return "too long" in low or "audio is too long" in low or "音频过长" in (detail or "")


def _chat_message_text(payload: Any) -> str:
    """从 ``/chat/completions`` 响应里取文本（content 可能是字符串或分片列表）。"""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("text")
        ]
        return "".join(parts).strip()
    return ""
class _PermanentAsrError(Exception):
    """内部：不会因为重试而好转的失败（配置/环境问题）。"""


#: 命中这些特征的错误属于「重试也没用」，直接失败并给出建议，避免白等好几次退避。
_PERMANENT_HINTS = (
    "library cublas64", "library cudnn", "cannot be loaded",
    "no such file or directory", "not found or cannot be loaded",
    "unsupported model", "model not found", "invalid model",
    "response_format", "400 bad request", "invalid_request_error",
    "context length", "out of memory", "cuda error",
    "connection refused", "actively refused", "failed to establish",
    "name or service not known", "nodename nor servname",
)


def _is_permanent_error_text(text: str) -> bool:
    low = (text or "").lower()
    return any(h in low for h in _PERMANENT_HINTS)


def _first_float(d: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return default


_SRT_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def parse_srt_text(text: str, *, offset: float = 0.0) -> list[Segment]:
    """解析 SRT 文本为片段列表（很多 OpenAI 兼容端点直接回 srt）。"""
    out: list[Segment] = []
    blocks = re.split(r"\r?\n\r?\n+", (text or "").strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        m = None
        text_lines: list[str] = []
        for i, line in enumerate(lines):
            m = _SRT_TIME.search(line)
            if m:
                text_lines = lines[i + 1 :]
                break
        if not m:
            continue
        start = _srt_secs(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _srt_secs(m.group(5), m.group(6), m.group(7), m.group(8))
        body = " ".join(text_lines).strip()
        if body:
            out.append(Segment(start=offset + start, end=offset + end, text=body))
    return out


def _srt_secs(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


# --------------------------------------------------------------------------- #
# 阿里云百炼 DashScope
# --------------------------------------------------------------------------- #
class DashScopeTranscriber:
    """阿里云百炼（DashScope）ASR。

    两种模式：
        * **OpenAI 兼容**（默认，``asr_use_native_api=False``）：
          ``POST {base}/audio/transcriptions``，``model=paraformer-v2`` 等；
        * **原生异步 API**（``asr_use_native_api=True``）：
          上传文件到 OSS 的 ``/api/v1/uploads``，再提交 ``/api/v1/services/audio/asr/transcription``
          异步任务并轮询 ``/api/v1/tasks/{task_id}``。适合大文件 / 长音频。

    无论哪种模式，切分与合并逻辑都复用父类的实现。
    """

    def __init__(
        self,
        cfg: AppConfig,
        api_key: str,
        *,
        on_progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
        gate: PauseGate | None = None,
    ) -> None:
        if not api_key:
            raise AsrNotConfiguredError(
                "未配置阿里云百炼 API Key。请在设置页填入 DashScope API Key"
                "（https://bailian.console.aliyun.com/ → API-KEY 管理）。"
            )
        self.cfg = cfg
        self.api_key = api_key
        register_secret(api_key)
        self.on_progress = on_progress or (lambda _p, _m: None)
        self.cancel = cancel or threading.Event()
        self.gate = gate
        self.name = "dashscope-native" if cfg.asr_use_native_api else "dashscope"
        self._openai = OpenAICompatibleTranscriber(
            cfg, api_key, on_progress=on_progress, cancel=cancel, gate=gate
        )

    def transcribe(self, audio_path: Path, *, duration_sec: float = 0.0) -> Transcript:
        if not self.cfg.asr_use_native_api:
            t = self._openai.transcribe(audio_path, duration_sec=duration_sec)
            t.provider = self.name
            return t
        return self._transcribe_native(Path(audio_path), duration_sec=duration_sec)

    # ------------------------------------------------------------------ #
    def _notify(self, pct: float, msg: str) -> None:
        """进度上报。

        **曾经漏了这个方法**：``_transcribe_native`` 一路上都在调 ``self._notify``，
        但 ``DashScopeTranscriber`` 自己没定义它，也没有继承 ``OpenAICompatibleTranscriber``
        （是组合不是继承）—— 于是原生异步模式一进去就
        ``AttributeError: 'DashScopeTranscriber' object has no attribute '_notify'``，
        整个「大文件/长音频更稳」的推荐模式**完全不可用**。
        """
        try:
            self.on_progress(pct, msg)
        except Exception:
            pass

    def _wait_gate(self) -> None:
        """暂停闸门（原生模式在轮询循环里也会调用）。"""
        if self.gate is None or not self.gate.is_paused:
            return
        self._notify(0.0, "⏸ 已暂停（点「继续」恢复）")
        while self.gate.is_paused and not self.cancel.is_set():
            self.gate.wait(timeout=2.0)
        if not self.cancel.is_set():
            self._notify(0.0, "▶ 已恢复")

    # -- 原生异步 API ---------------------------------------------------- #
    #: DashScope 原生异步 API 的默认基址。
    #: 可用 ``ECNU_DASHSCOPE_NATIVE_BASE`` 环境变量覆盖 —— 便于：
    #:   ① 指向私有化部署 / 代理网关；② 在离线环境下跑集成测试。
    DEFAULT_NATIVE_BASE = "https://dashscope.aliyuncs.com/api/v1"

    @property
    def NATIVE_BASE(self) -> str:  # noqa: N802 - 保持类属性式的可读性
        return os.environ.get("ECNU_DASHSCOPE_NATIVE_BASE", "").rstrip("/") or self.DEFAULT_NATIVE_BASE

    def _transcribe_native(self, path: Path, *, duration_sec: float = 0.0) -> Transcript:
        duration = duration_sec or media.duration_of(path)
        base = self.NATIVE_BASE
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-DashScope-Async": "enable",
        }
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=30.0), trust_env=not self.cfg.proxy,
                          proxy=self.cfg.proxy or None) as client:
            # 1) 取上传凭证并上传
            self._notify(5.0, "申请 DashScope 上传凭证…")
            r = client.get(
                f"{base}/uploads",
                params={"action": "getPolicy", "model": self.cfg.asr_model},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if r.status_code in (401, 403):
                raise AuthExpiredError(f"DashScope 鉴权失败：{redact(r.text[:200])}")
            r.raise_for_status()
            policy = r.json().get("data", {})
            if not policy:
                raise TranscriptionError(
                    "DashScope 未返回上传凭证（data 为空）。"
                    "可能是 API Key 未开通该模型，或接口结构变更。"
                )
            oss_url = self._upload_to_oss(client, path, policy)

            # 2) 提交异步转写任务
            self._notify(20.0, "提交 DashScope 异步转写任务…")
            body = {
                "model": self.cfg.asr_model,
                "input": {"file_urls": [oss_url]},
                "parameters": {
                    "language_hints": [self.cfg.asr_language or "zh"],
                    "channel_id": [0],
                    "enable_timestamp": bool(self.cfg.asr_timestamps),
                    "enable_words": False,
                },
            }
            if self.cfg.asr_vocabulary:
                body["parameters"]["vocabulary_id"] = self.cfg.asr_vocabulary
            if self.cfg.asr_speaker_diarization:
                body["parameters"]["diarization_enabled"] = True
            r = client.post(
                f"{base}/services/audio/asr/transcription",
                headers={**headers, "Content-Type": "application/json"},
                json=body,
            )
            if r.status_code >= 400:
                raise TranscriptionError(f"DashScope 提交失败 HTTP {r.status_code}: {redact(r.text[:400])}")
            task_id = str(r.json().get("output", {}).get("task_id", ""))
            if not task_id:
                raise TranscriptionError(f"DashScope 未返回 task_id：{redact(r.text[:300])}")

            # 3) 轮询（间隔自适应：前几次密一点，之后拉长，避免空转刷接口）
            deadline = time.time() + max(600.0, duration * 3)
            result_url = ""
            poll_interval = 2.0
            polls = 0
            while time.time() < deadline:
                if self.cancel.is_set():
                    raise TaskCancelled("转写被用户取消")
                if self.gate is not None and self.gate.is_paused:
                    self._notify(0.0, "⏸ 已暂停（等待 DashScope 转写中，点「继续」恢复）")
                    while self.gate.is_paused and not self.cancel.is_set():
                        self.gate.wait(timeout=2.0)
                    if self.cancel.is_set():
                        raise TaskCancelled("转写被用户取消")
                time.sleep(poll_interval)
                polls += 1
                poll_interval = min(10.0, 2.0 + polls * 0.5)
                self._notify(
                    min(85.0, 20.0 + (time.time() % 60)),
                    f"等待 DashScope 转写（task={task_id[:8]}，已轮询 {polls} 次）…",
                )
                rr = client.get(f"{base}/tasks/{task_id}", headers=headers)
                rr.raise_for_status()
                out = rr.json().get("output", {})
                status = str(out.get("task_status", ""))
                if status == "SUCCEEDED":
                    results = out.get("results") or []
                    if results:
                        result_url = str(results[0].get("transcription_url", ""))
                    break
                if status in ("FAILED", "UNKNOWN"):
                    detail = redact(json.dumps(out, ensure_ascii=False)[:400])
                    raise TranscriptionError(
                        f"DashScope 任务失败（{status}）：{detail}\n"
                        "常见原因：音频格式不支持、时长超限、模型未开通。"
                        "可改用「切分后逐段转写」或换用 OpenAI 兼容模式。"
                    )
            if not result_url:
                raise TranscriptionError("DashScope 任务超时或未返回 transcription_url")

            # 4) 取结果
            self._notify(90.0, "下载 DashScope 转写结果…")
            try:
                res = client.get(result_url)
                res.raise_for_status()
                payload = res.json()
            except httpx.HTTPStatusError as exc:
                raise TranscriptionError(
                    f"下载 DashScope 转写结果失败（HTTP {exc.response.status_code}）："
                    f"{redact(result_url)[:200]}\n"
                    "常见原因：结果链接已过期（有效期通常 24 小时）或网络无法访问该域名。"
                    "音频与已完成的处理都保留在本地，重跑不会重下。"
                ) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise TranscriptionError(
                    f"下载或解析 DashScope 转写结果失败：{type(exc).__name__}: {exc}"
                ) from exc

        segments = self._parse_native_result(payload)
        return Transcript(
            segments=segments,
            language=self.cfg.asr_language,
            duration_sec=duration,
            model=self.cfg.asr_model,
            provider=self.name,
            meta={"native_task": True, "raw_transcripts": len(payload.get("transcripts", []))},
        )

    def _upload_to_oss(self, client: httpx.Client, path: Path, policy: dict[str, Any]) -> str:
        """用 ``getPolicy`` 返回的凭证把文件 POST 到 OSS，返回 ``oss://`` 地址。

        ⚠ 字段名是实测出来的，别照直觉改（缺陷 55）：``getPolicy`` 返回的凭证键叫
        **``oss_access_key_id``**，不是 ``access_key_id``。写错时 OSS 会回
        ``Post request accessKeyId is empty``（HTTP 400），而错误发生在**上传阶段**，
        看起来像"网络/权限问题"，其实是自己把凭证读空了 —— 云端异步路因此整条不可用。
        """
        if not policy:
            raise TranscriptionError("DashScope 上传凭证为空")
        upload_host = str(policy.get("upload_host", ""))
        upload_dir = str(policy.get("upload_dir", ""))
        access_key_id = str(
            policy.get("oss_access_key_id") or policy.get("access_key_id") or ""
        )
        if not (upload_host and upload_dir and access_key_id):
            raise TranscriptionError(
                "DashScope 上传凭证不完整（缺少 upload_host / upload_dir / oss_access_key_id）："
                f"拿到 {sorted(policy.keys())}。接口结构可能已变更。"
            )
        key = f"{upload_dir}/{path.name}"
        fields = {
            "OSSAccessKeyId": access_key_id,
            "policy": policy.get("policy", ""),
            "Signature": policy.get("signature", ""),
            "key": key,
            "success_action_status": "200",
        }
        # 这两个字段接口给了就必须原样回传（值要对、**表单字段名**也要对）：
        # JSON 里的键是下划线形式（``x_oss_object_acl``），而 OSS 表单字段是连字符形式
        # （``x-oss-object-acl``）。名字写错时 OSS 回
        # ``Invalid according to Policy: Policy Condition failed: ["eq", "$x-oss-object-acl", "private"]``
        # —— 同样是"上传阶段报错、看不出真正原因"的那类坑（缺陷 55 的第二处）。
        for api_field, form_field in (
            ("x_oss_object_acl", "x-oss-object-acl"),
            ("x_oss_forbid_overwrite", "x-oss-forbid-overwrite"),
        ):
            if policy.get(api_field):
                fields[form_field] = policy[api_field]
        with open(path, "rb") as fh:
            files = {k: (None, str(v)) for k, v in fields.items()}
            files["file"] = (path.name, fh, "application/octet-stream")
            resp = client.post(upload_host, files=files, timeout=httpx.Timeout(1800.0, connect=30.0))
        if resp.status_code >= 400:
            raise TranscriptionError(f"OSS 上传失败 HTTP {resp.status_code}: {redact(resp.text[:300])}")
        return f"oss://{key}"

    @staticmethod
    def _parse_native_result(payload: dict[str, Any]) -> list[Segment]:
        """解析 DashScope 原生结果。

        结构：``{"transcripts":[{"text": "...", "sentences":[{"begin_time":ms, "end_time":ms, "text": "..."}]}]}``

        注意两个坑：
        * 时间戳单位是**毫秒**；
        * ``sentences`` 可能是**空列表**（句级结果缺失）——此时必须回退到
          ``transcripts[].text``，否则会返回空转写，让用户看到
          「ASR 返回了空结果」这种误导性的报错（其实识别是成功的）。
        """
        out: list[Segment] = []
        for tr in payload.get("transcripts", []) or []:
            if not isinstance(tr, dict):
                continue
            sentences = tr.get("sentences") or tr.get("words") or []
            produced = 0
            if isinstance(sentences, list):
                for s in sentences:
                    if not isinstance(s, dict):
                        continue
                    text = str(s.get("text") or "").strip()
                    if not text:
                        continue
                    start = _first_float(s, "begin_time", "start_time", "start", "begin") / 1000.0
                    end = _first_float(s, "end_time", "end", "stop") / 1000.0
                    out.append(
                        Segment(start=start, end=max(end, start + 0.2), text=text,
                                speaker=str(s.get("speaker") or ""))
                    )
                    produced += 1
            if produced == 0:
                # sentences 缺失或全为空 → 用整段文本兜底
                text = str(tr.get("text") or "").strip()
                if text:
                    out.append(Segment(start=0.0, end=0.0, text=text))
        return out


# --------------------------------------------------------------------------- #
# 本地 faster-whisper 兜底
# --------------------------------------------------------------------------- #
class FasterWhisperTranscriber:
    """本地 ``faster-whisper`` 兜底（无需联网、无需 Key）。

    只有在用户选择 ``faster_whisper_local`` 时才会动态导入该库；
    未安装时给出明确的安装指引，不影响其它功能。
    """

    name = "faster-whisper-local"

    def __init__(
        self,
        cfg: AppConfig,
        *,
        model_size: str = "",
        on_progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.cfg = cfg
        self.model_size = model_size or str(cfg.extra.get("whisper_model") or "small")
        self.on_progress = on_progress or (lambda _p, _m: None)
        self.cancel = cancel or threading.Event()
        self._model = None

    def transcribe(self, audio_path: Path, *, duration_sec: float = 0.0) -> Transcript:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise AsrNotConfiguredError(
                "未安装 faster-whisper。本地兜底可用：\n"
                "  .venv\\Scripts\\python -m pip install faster-whisper\n"
                "（首次会自动下载模型，约 0.5~1.5GB；也可在设置页改用云端 ASR）"
            ) from exc

        if self._model is None:
            device = str(self.cfg.extra.get("whisper_device") or "auto")
            compute = str(self.cfg.extra.get("whisper_compute_type") or "auto")
            self.on_progress(5.0, f"加载本地模型 {self.model_size}（device={device}）…")
            self._model = WhisperModel(self.model_size, device=device, compute_type=compute)

        path = Path(audio_path)
        duration = duration_sec or media.duration_of(path)
        self.on_progress(15.0, "本地转写中…")
        seg_iter, info = self._model.transcribe(
            str(path),
            language=(self.cfg.asr_language or "zh"),
            vad_filter=True,
            beam_size=5,
            word_timestamps=False,
        )
        segs: list[Segment] = []
        for seg in seg_iter:
            if self.cancel.is_set():
                raise TaskCancelled("转写被用户取消")
            segs.append(
                Segment(
                    start=float(seg.start or 0.0),
                    end=float(seg.end or 0.0),
                    text=str(seg.text or "").strip(),
                    confidence=float(getattr(seg, "avg_logprob", 0.0) or 0.0),
                )
            )
            if duration:
                self.on_progress(min(95.0, 15.0 + float(seg.end or 0) / duration * 80.0), "本地转写中…")
        self.on_progress(98.0, "本地转写完成")
        final_segments = [s for s in segs if s.text]
        return Transcript(
            segments=final_segments,
            language=str(getattr(info, "language", self.cfg.asr_language) or ""),
            duration_sec=duration,
            model=f"faster-whisper:{self.model_size}",
            provider=self.name,
            meta={"segment_count": len(final_segments)},
        )


class NullTranscriber:
    """未配置 ASR 时的占位实现：只生成一份「未转写」占位文本，绝不伪造内容。"""

    name = "none"

    def __init__(self, cfg: AppConfig, reason: str = "") -> None:
        self.cfg = cfg
        self.reason = reason or "尚未配置语音识别（ASR）端点"

    def transcribe(self, audio_path: Path, *, duration_sec: float = 0.0) -> Transcript:
        raise AsrNotConfiguredError(
            f"{self.reason}。请到「设置 → 语音识别」配置阿里云百炼 DashScope 或其它 "
            "OpenAI 兼容端点后重试。"
        )


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def create_transcriber(
    cfg: AppConfig,
    *,
    cm: ConfigManager | None = None,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    gate: PauseGate | None = None,
) -> Any:
    """按配置构造合适的转写器。"""
    provider = (cfg.asr_provider or "dashscope").lower()
    api_key = ""

    if provider == "dashscope":
        api_key = cm.secret("asr_api_key") if cm else os.environ.get("ECNU_ASR_API_KEY", "")
        return DashScopeTranscriber(cfg, api_key, on_progress=on_progress, cancel=cancel, gate=gate)
    if provider in ("openai_compatible", "openai", "custom"):
        api_key = cm.secret("asr_api_key") if cm else os.environ.get("ECNU_ASR_API_KEY", "")
        return OpenAICompatibleTranscriber(cfg, api_key, on_progress=on_progress, cancel=cancel, gate=gate)
    if provider in ("faster_whisper_local", "local", "faster-whisper"):
        return FasterWhisperTranscriber(cfg, on_progress=on_progress, cancel=cancel)
    return NullTranscriber(cfg, f"未知的 ASR provider：{provider}")


def probe_asr_endpoint(cfg: AppConfig, api_key: str, *, sample: Path | None = None) -> tuple[bool, str]:
    """连通性自检：不发真实音频（避免计费），只做鉴权与模型可见性检查。

    本机端点（``localhost`` / ``127.0.0.1``）不要求 Key。
    返回 ``(ok, message)``。
    """
    from urllib.parse import urlparse

    base = (cfg.asr_base_url or "").rstrip("/")
    host = (urlparse(base).hostname or "").lower()
    is_local = host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    if not api_key and not is_local:
        return False, "未填写 API Key（若端点在本机，请把 Base URL 写成 http://127.0.0.1:<端口>/v1）"
    url = base + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=20.0, trust_env=not cfg.proxy, proxy=cfg.proxy or None) as client:
            r = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"无法连接 {url}：{exc}"
    if r.status_code in (401, 403):
        return False, f"鉴权失败（HTTP {r.status_code}）：API Key 不正确或无权限"
    if r.status_code >= 400:
        return False, f"端点返回 HTTP {r.status_code}：{redact(r.text[:200])}"
    models: list[str] = []
    try:
        payload = r.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            models = [str(m.get("id", "")) for m in rows if isinstance(m, dict)]
    except Exception:
        pass
    if models and cfg.asr_model and cfg.asr_model not in models:
        sample_models = ", ".join(models[:8])
        low = (cfg.asr_model or "").lower()
        if any(k in low for k in ("asr", "audio", "paraformer", "sensevoice")):
            # 实测：`/models` **不列 ASR 模型**。`qwen3-asr-flash` 明明可以正常调用，
            # 却不在这个列表里；照旧报「模型不存在」会把用户指去改一个本来正确的配置。
            return True, (
                f"鉴权通过。注意：该端点的 /models 列表不含 ASR 模型"
                f"（「{cfg.asr_model}」不在其中属正常），能否使用要看**实际调用**。"
                f"可先跑 scripts/verify_cloud_asr.py --live 做一次 20 秒试转。"
            )
        return True, (
            f"鉴权通过，但模型列表里没有「{cfg.asr_model}」。可用模型示例：{sample_models}。"
            "若该端点不暴露 /models 可忽略此提示。"
        )
    return True, f"鉴权通过（{len(models)} 个模型可见）"


def transcribe_short_sample(cfg: AppConfig, api_key: str, audio: Path, *, seconds: float = 60.0) -> Transcript:
    """「1 分钟短样本试转」：验证单价与质量，避免整段跑完才发现不对。"""
    audio = Path(audio)
    tmp = paths.cache_dir() / "sample" / f"sample_{int(time.time())}.{cfg.audio_format}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    media.extract_segment(
        audio, tmp, 0.0, min(seconds, max(5.0, media.duration_of(audio))),
        fmt=cfg.audio_format, sample_rate=cfg.audio_sample_rate,
        channels=cfg.audio_channels, bitrate=cfg.audio_bitrate,
    )
    tr = OpenAICompatibleTranscriber(cfg, api_key)
    return tr.transcribe(tmp, duration_sec=seconds)
