"""产物导出：``.txt`` / ``.srt`` / ``.md``。

输出约定（默认，可在设置页改目录）::

    output/<课程名>/<标题>.txt    纯文本全文（按段落）
    output/<课程名>/<标题>.srt    带时间轴的 SRT 字幕
    output/<课程名>/<标题>.md     标题 + 元信息 + 摘要 + 全文

「只增不改」原则：如果目标文件已存在，会先写一份 ``*.bak-<时间戳>``，
再用原子替换写入新内容 —— 用户的既有产物永远不会被静默覆盖丢失。
"""

from __future__ import annotations

import math
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .catalog import Resource, safe_filename
from .logbus import get_logger
from .transcriber import Segment, Transcript, join_segment_text

log = get_logger("exporter")

#: 段落最长字符数（超过则在句子边界断行，纯文本产物更好读）
PARAGRAPH_MAX_CHARS = 800
#: SRT 单条字幕最长展示时长（秒），超过就按句切分
SRT_MAX_DURATION = 12.0
#: SRT 单条字幕最大字符数
SRT_MAX_CHARS = 60


@dataclass
class ExportResult:
    txt: Path | None = None
    srt: Path | None = None
    md: Path | None = None
    backups: list[Path] = field(default_factory=list)

    @property
    def files(self) -> list[Path]:
        return [p for p in (self.txt, self.srt, self.md) if p]

    def to_dict(self) -> dict[str, str]:
        return {
            "txt": str(self.txt or ""),
            "srt": str(self.srt or ""),
            "md": str(self.md or ""),
        }


# --------------------------------------------------------------------------- #
# 时间格式化
# --------------------------------------------------------------------------- #
def fmt_srt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_clock(seconds: float) -> str:
    return fmt_srt_time(seconds).replace(",", ".")


# --------------------------------------------------------------------------- #
# 文本组织
# --------------------------------------------------------------------------- #
_SENT_END = re.compile(r"(?<=[。！？!?；;])\s*")


def split_into_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_END.split(text or "") if p and p.strip()]
    return parts or ([text.strip()] if text and text.strip() else [])


def _pack_sentences(sentences: Sequence[str], max_chars: int) -> list[str]:
    """把句子打包成不超过 ``max_chars`` 的字幕块；单句超长时硬切。"""
    chunks: list[str] = []
    buf = ""
    for raw in sentences:
        sentence = (raw or "").strip()
        while sentence and len(sentence) > max_chars:
            # 单句本身超长：先冲掉缓冲区，再硬切
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if not sentence:
            continue
        if buf and len(buf) + len(sentence) > max_chars:
            chunks.append(buf)
            buf = sentence
        else:
            buf += sentence
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c and c.strip()]


def build_paragraphs(segments: Sequence[Segment], *, max_chars: int = PARAGRAPH_MAX_CHARS) -> list[str]:
    """把片段拼成自然段：按片段边界聚合，超长则在句子边界断开。"""
    paragraphs: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def flush() -> None:
        nonlocal cur, cur_len
        if cur:
            paragraphs.append(join_segment_text([Segment(text=t) for t in cur]).strip())
        cur = []
        cur_len = 0

    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        # 片段自带段落分隔（有些 ASR 输出会保留换行）
        if "\n" in text:
            for piece in [p.strip() for p in text.splitlines() if p.strip()]:
                if cur_len + len(piece) > max_chars:
                    flush()
                cur.append(piece)
                cur_len += len(piece)
            continue
        if cur_len + len(text) > max_chars:
            flush()
        cur.append(text)
        cur_len += len(text)
    flush()
    return [p for p in paragraphs if p]


def _split_text_by_length(text: str, max_chars: int) -> list[str]:
    """按「尽量整除」的方式切分长文本，避免出现不足一行的碎片块。"""
    n = len(text)
    if n <= max_chars:
        return [text]
    parts = math.ceil(n / max_chars)
    step = math.ceil(n / parts)
    return [text[i : i + step] for i in range(0, n, step)]


def build_srt_entries(
    segments: Sequence[Segment],
    *,
    max_duration: float = SRT_MAX_DURATION,
    max_chars: int = SRT_MAX_CHARS,
) -> list[tuple[float, float, str]]:
    """生成 SRT 条目。过长/过久的片段会被切分，时间轴按字符比例分配。

    切分策略：先按句子边界打包；若打包结果仍不满足「单条时长 / 字数」约束，
    再按「尽量整除」的字数切分 —— 这样极端情况（超长句 + 短时长）也不会碎成
    十几个只有几个字的条目。
    """
    entries: list[tuple[float, float, str]] = []
    for seg in segments:
        text = (seg.text or "").strip().replace("\n", " ")
        if not text:
            continue
        start, end = float(seg.start), float(seg.end)
        if end <= start:
            end = start + max(1.0, len(text) / 5.0)
        dur = end - start
        if dur <= max_duration and len(text) <= max_chars:
            entries.append((start, end, text))
            continue

        sentences = split_into_sentences(text)
        chunks = _pack_sentences(sentences, max_chars) if sentences else []
        # 打包后仍不满足时长约束 → 改用字数均分
        if not chunks or len(chunks) * max_duration < dur * 0.9:
            chunks = _split_text_by_length(text, max_chars)
        if len(chunks) > 1 and len(chunks) * max_duration < dur * 0.9:
            chunks = _split_text_by_length(text, max(8, math.ceil(len(text) * max_duration / dur)))

        total_chars = sum(len(c) for c in chunks) or 1
        cursor = start
        for c in chunks:
            share = len(c) / total_chars * dur
            c_start = cursor
            c_end = min(end, cursor + max(0.8, share))
            entries.append((c_start, c_end, c))
            cursor = c_end
    entries.sort(key=lambda e: e[0])
    # 消除时间轴重叠（SRT 播放器对重叠条目表现不一）
    fixed: list[tuple[float, float, str]] = []
    for s, e, t in entries:
        if fixed and s < fixed[-1][1]:
            s = fixed[-1][1] + 0.001
            if e <= s:
                e = s + 0.5
        fixed.append((s, e, t))
    return fixed


# --------------------------------------------------------------------------- #
# 写出
# --------------------------------------------------------------------------- #
#: UTF-8 BOM。
#: Windows 简体中文环境的 ANSI 代码页是 **cp936(GBK)**，而旧版记事本、
#: 以及**很多字幕播放器**（PotPlayer / MPC 等）在打开「无 BOM 的 UTF-8」时
#: 会按系统代码页猜编码 —— 结果中文全是乱码。加 BOM 是 Windows 上最保险的做法
#: （现代编辑器/播放器都能正确识别带 BOM 的 UTF-8）。
UTF8_BOM = "\ufeff"


def _atomic_write(
    path: Path, content: str, *, backup: bool = True, bom: bool = True
) -> list[Path]:
    """原子写入；若目标已存在，先备份为 ``*.bak-<ts>``（只增不改）。

    ``bom=True`` 时写入 UTF-8 BOM，保证 Windows 记事本 / 字幕播放器不乱码。
    """
    backups: list[Path] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and backup:
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = path.with_name(f"{path.name}.bak-{ts}")
        try:
            shutil.copy2(path, bak)
            backups.append(bak)
        except OSError as exc:
            log.warning("备份 %s 失败：%s", path, exc)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = (UTF8_BOM + content) if bom else content
    # newline="" 配合内容里已经规范化的 "\n"：不让 Python 再翻译换行，
    # 从而保证 SRT 严格用 LF（部分播放器对 CRLF/LF 混用敏感）
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(payload)
    tmp.replace(path)
    return backups


def write_txt(
    path: Path, paragraphs: Sequence[str], *, backup: bool = True, bom: bool = True
) -> list[Path]:
    body = "\n\n".join(p.strip() for p in paragraphs if p and p.strip())
    return _atomic_write(path, body + ("\n" if body else ""), backup=backup, bom=bom)


def write_srt(
    path: Path,
    entries: Sequence[tuple[float, float, str]],
    *,
    backup: bool = True,
    bom: bool = True,
) -> list[Path]:
    blocks: list[str] = []
    for i, (start, end, text) in enumerate(entries, 1):
        blocks.append(f"{i}\n{fmt_srt_time(start)} --> {fmt_srt_time(end)}\n{text.strip()}\n")
    return _atomic_write(path, "\n".join(blocks), backup=backup, bom=bom)


def write_md(
    path: Path,
    *,
    title: str,
    meta_lines: Sequence[str],
    summary_md: str,
    paragraphs: Sequence[str],
    segments: Sequence[Segment] | None = None,
    with_timeline: bool = True,
    backup: bool = True,
    bom: bool = True,
) -> list[Path]:
    parts: list[str] = [f"# {title}", ""]
    if meta_lines:
        parts.append("## 元信息")
        parts.extend(f"- {line}" for line in meta_lines)
        parts.append("")
    if summary_md.strip():
        parts.append("## 摘要")
        parts.append(summary_md.strip())
        parts.append("")
    parts.append("## 全文")
    parts.append("")
    if with_timeline and segments:
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            parts.append(f"**[{fmt_clock(seg.start)}]** {text}")
            parts.append("")
    else:
        for para in paragraphs:
            parts.append(para.strip())
            parts.append("")
    content = "\n".join(parts).rstrip() + "\n"
    return _atomic_write(path, content, backup=backup, bom=bom)


# --------------------------------------------------------------------------- #
def export_all(
    resource: Resource,
    transcript: Transcript,
    output_dir: Path,
    *,
    emit_txt: bool = True,
    emit_srt: bool = True,
    emit_md: bool = True,
    summary_md: str = "",
    extra_meta: dict[str, str] | None = None,
    with_timeline: bool = True,
    bom: bool = True,
) -> ExportResult:
    """按用户开关一次性产出全部产物。

    ``bom=True``（默认）会给每个产物写入 UTF-8 BOM —— Windows 上中文记事本与
    多数字幕播放器靠它判断编码，否则会按 cp936 猜、显示乱码。
    """
    course_dir = Path(output_dir) / safe_filename(resource.course_name or "未分课程")
    course_dir.mkdir(parents=True, exist_ok=True)
    base = safe_filename(resource.title or resource.resource_id or "untitled")

    paragraphs = build_paragraphs(transcript.segments)
    srt_entries = build_srt_entries(transcript.segments)
    result = ExportResult()

    from .media import human_duration

    meta_lines = [
        f"课程：{resource.course_name or '-'}",
        f"讲师：{resource.teacher or '-'}",
        f"标题：{resource.title or '-'}",
        f"录制时间：{resource.record_time or '-'}",
        f"时长：{human_duration(transcript.duration_sec or resource.duration_sec)}",
        f"资源 ID：{resource.resource_id or '-'}",
        f"转写模型：{transcript.model or '-'}",
        f"转写时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(transcript.created_at))}",
        f"字数：{transcript.char_count}",
    ]
    if extra_meta:
        meta_lines.extend(f"{k}：{v}" for k, v in extra_meta.items())
    warnings = transcript.meta.get("llm_warnings") if isinstance(transcript.meta, dict) else None
    if warnings:
        meta_lines.append("后处理告警：" + "；".join(str(w) for w in warnings)[:500])

    if emit_txt:
        p = course_dir / f"{base}.txt"
        result.backups += write_txt(p, paragraphs, bom=bom)
        result.txt = p
    if emit_srt:
        p = course_dir / f"{base}.srt"
        result.backups += write_srt(p, srt_entries, bom=bom)
        result.srt = p
    if emit_md:
        p = course_dir / f"{base}.md"
        result.backups += write_md(
            p,
            title=resource.title or base,
            meta_lines=meta_lines,
            summary_md=summary_md,
            paragraphs=paragraphs,
            segments=transcript.segments,
            with_timeline=with_timeline,
            bom=bom,
        )
        result.md = p

    # 同时留一份机器可读的转写 JSON（便于二次加工；不算「三份产物」之一）
    try:
        transcript.save_json(course_dir / f"{base}.transcript.json")
    except OSError as exc:
        log.debug("写 transcript.json 失败：%s", exc)

    log.info(
        "产物已写出：txt=%s srt=%s md=%s（备份 %s 个）",
        result.txt.name if result.txt else "-",
        result.srt.name if result.srt else "-",
        result.md.name if result.md else "-",
        len(result.backups),
    )
    return result
