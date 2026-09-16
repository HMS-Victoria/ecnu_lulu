"""DeepSeek 文本后处理（可选开关）。

再次强调分工（与 README / 设置页一致）：

    ASR：语音 → 文字（阿里云百炼 DashScope / 任意 OpenAI 兼容 / 本地 faster-whisper）
    LLM：文字 → 更好的文字（**DeepSeek** 负责错别字与标点修复、专业术语纠正、
         口语冗余清理、按语义重新分段、生成结构化摘要与大纲）

处理策略
--------
* 转写文本按 ``llm_max_chars_per_call`` 切成若干块，逐块调用（保持顺序）；
* **修复类**任务要求模型返回 JSON：``{"segments":[{"i":<序号>,"text":"<修订后>"}]}``，
  只替换文本、保留原时间轴 —— 这样 SRT 时间戳不会因为分段变化而错位；
* **摘要类**任务单独一次调用（可喂入全文或分块摘要的再汇总）；
* 任何 LLM 失败都**不阻塞**产物输出：退化为「未加工的原文」，并在日志与
  ``.md`` 的元信息里如实标注。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import AppConfig, ConfigManager
from .errors import LlmError
from .logbus import get_logger, redact, register_secret
from .transcriber import Segment, Transcript, join_segment_text

log = get_logger("llm")

ProgressFn = Callable[[float, str], None]


SYSTEM_PROMPT = """你是中文课程录播的转写整理助手。用户会给你一段**JSON**，形如：
{"segments":[{"i":0,"text":"..."},{"i":1,"text":"..."}]}
每个条目是一句由语音识别（ASR）产出的课程文字，可能包含同音错别字、缺失标点、
口语冗余（呃、那个、这个、就是说）、重复词与断句错误。

你的任务（只做这些，不要增删信息、不要编造内容）：
1. 修正明显的同音字/错别字与专业术语错误；
2. 补齐中英文标点，规范数字与单位；
3. 删除纯口语冗余词与无意义重复，但**保留原意**；
4. 不要总结、不要改写句子结构、不要合并或删除句子。

输出要求（必须严格遵守）：
- 输出一个 JSON 对象，形如 {"segments":[{"i":0,"text":"..."},{"i":1,"text":"..."}]}；
- **条目的数量、顺序与 i 的值必须与输入完全一致**（只改 text，绝不增删条目）；
- 只输出 JSON，不要输出任何解释文字、不要用 Markdown 代码块包裹。
"""

SUMMARY_PROMPT = """你是课程内容分析助手。下面是一门课一次录播的转写文字。
请输出 Markdown 格式的结构化摘要，包含：
## 一句话概括
## 本节要点
（3~8 条，每条一句，尽量带关键概念/公式/结论）
## 关键词
（5~10 个，用中文顿号分隔）
## 章节目录（按内容顺序）
（每条：`时间感描述` —— 讲的是什么；若原文没有时间信息就用「开头/中段/结尾」描述）
不要编造原文没有的内容。"""


@dataclass
class LlmResult:
    """后处理结果。"""

    segments: list[Segment] = field(default_factory=list)
    summary_md: str = ""
    applied_fix: bool = False
    applied_resegment: bool = False
    applied_summary: bool = False
    model: str = ""
    warnings: list[str] = field(default_factory=list)
    tokens_hint: int = 0

    @property
    def text(self) -> str:
        return join_segment_text(self.segments)


class _ChatClient:
    """极简 OpenAI 兼容 chat 客户端（DeepSeek 用 ``/v1/chat/completions``）。"""

    def __init__(self, cfg: AppConfig, api_key: str, base_url: str = "", model: str = "") -> None:
        if not api_key:
            raise LlmError("未配置 DeepSeek API Key")
        self.cfg = cfg
        self.api_key = api_key
        register_secret(api_key)
        base = (base_url or cfg.llm_base_url or "https://api.deepseek.com/v1").rstrip("/")
        self.url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        self.model = model or cfg.llm_model or "deepseek-chat"

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
        attempts: int = 3,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        last: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(self.cfg.llm_timeout or 120.0, connect=30.0),
                    trust_env=not self.cfg.proxy,
                    proxy=self.cfg.proxy or None,
                ) as client:
                    r = client.post(self.url, headers=headers, json=payload)
                if r.status_code in (401, 403):
                    raise LlmError(f"DeepSeek 鉴权失败（HTTP {r.status_code}）：{redact(r.text[:200])}")
                if r.status_code == 400 and json_mode and "response_format" in r.text:
                    # 端点不支持 json_object，去掉再试一次
                    payload.pop("response_format", None)
                    json_mode = False
                    continue
                if r.status_code >= 400:
                    raise LlmError(f"DeepSeek HTTP {r.status_code}: {redact(r.text[:300])}")
                data = r.json()
                choices = data.get("choices") or []
                if not choices:
                    raise LlmError(f"DeepSeek 返回空 choices：{redact(json.dumps(data, ensure_ascii=False)[:300])}")
                content = str(choices[0].get("message", {}).get("content", "") or "")
                if not content.strip():
                    raise LlmError("DeepSeek 返回空内容")
                return content
            except LlmError as exc:
                if "鉴权失败" in str(exc):
                    raise
                last = exc
            except httpx.HTTPError as exc:
                last = exc
            if attempt < attempts:
                wait = 2 ** attempt
                log.warning("DeepSeek 调用失败（%s/%s）：%s；%ss 后重试", attempt, attempts, last, wait)
                time.sleep(wait)
        raise LlmError(f"DeepSeek 调用失败（重试 {attempts} 次）：{last}")


class TextPostProcessor:
    """把 ASR 原始结果交给 DeepSeek 加工。"""

    def __init__(
        self,
        cfg: AppConfig,
        api_key: str,
        *,
        on_progress: ProgressFn | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = _ChatClient(cfg, api_key)
        self.on_progress = on_progress or (lambda _p, _m: None)
        self.cancel = cancel or threading.Event()

    # ------------------------------------------------------------------ #
    def process(self, transcript: Transcript) -> LlmResult:
        """按配置执行修复 / 分段 / 摘要。任何一步失败都退化为原文并记录告警。"""
        result = LlmResult(segments=list(transcript.segments), model=self.client.model)
        if not self.cfg.llm_enabled:
            result.warnings.append("LLM 后处理未启用（设置页可开启）")
            return result

        segs = result.segments
        if not segs or not any(s.text.strip() for s in segs):
            result.warnings.append("转写为空，跳过 LLM 后处理")
            return result

        if self.cfg.llm_fix_text:
            try:
                segs, warn = self._fix_segments(segs)
                result.applied_fix = True
                result.warnings.extend(warn)
                result.segments = segs
                if self.cfg.llm_resegment:
                    segs, warn2 = self._resegment(segs)
                    result.applied_resegment = True
                    result.warnings.extend(warn2)
                    result.segments = segs
            except LlmError as exc:
                result.warnings.append(f"文本修复失败，保留 ASR 原文：{exc}")
                log.warning("LLM 文本修复失败：%s", exc)

        if self.cfg.llm_summary:
            try:
                result.summary_md = self.summarize(result.segments)
                result.applied_summary = bool(result.summary_md.strip())
            except LlmError as exc:
                result.warnings.append(f"摘要生成失败：{exc}")
                log.warning("LLM 摘要失败：%s", exc)
        return result

    # ------------------------------------------------------------------ #
    def _fix_segments(self, segments: list[Segment]) -> tuple[list[Segment], list[str]]:
        """按块修复；返回新片段列表与告警。**时间轴保持不变**（下标一一对应）。"""
        warnings: list[str] = []
        max_chars = max(500, int(self.cfg.llm_max_chars_per_call or 6000))
        out: list[Segment] = list(segments)

        blocks: list[tuple[int, int]] = []
        cur_start = 0
        cur_len = 0
        for i, seg in enumerate(segments):
            n = len(seg.text or "")
            if cur_len + n > max_chars and i > cur_start:
                blocks.append((cur_start, i))
                cur_start = i
                cur_len = 0
            cur_len += n
        if cur_start < len(segments):
            blocks.append((cur_start, len(segments)))

        for bi, (lo, hi) in enumerate(blocks, 1):
            if self.cancel.is_set():
                warnings.append("LLM 修复被取消，已处理部分生效")
                break
            self.on_progress((bi - 1) / max(1, len(blocks)) * 100.0, f"DeepSeek 修复第 {bi}/{len(blocks)} 块…")
            items = [{"i": i, "text": segments[i].text} for i in range(lo, hi)]
            # 用户消息里**只放数据**，格式说明一律放 system。
            # 曾经把「输出格式：{"segments":[{"i":0,...}]}」也塞进用户消息，
            # 结果模型偶尔会把这条**示例**原样回显 —— 示例里的 i=0 与真实数据的 i=0
            # 撞车，看起来「调用成功了」，实际只改了第 0 条甚至什么都没改。
            content = self.client.chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps({"segments": items}, ensure_ascii=False),
                    },
                ],
                json_mode=True,
                temperature=0.1,
            )
            mapping = self._parse_fix_response(content)
            if not mapping:
                warnings.append(f"第 {bi} 块 LLM 输出无法解析，保留原文")
                continue
            applied = 0
            for i in range(lo, hi):
                new_text = mapping.get(i)
                if new_text is None:
                    # 模型可能用 1-based 或返回顺序数组
                    new_text = mapping.get(i - lo)
                if new_text is None:
                    continue
                cleaned = _strip_markdown(new_text).strip()
                if cleaned:
                    out[i] = Segment(
                        start=segments[i].start,
                        end=segments[i].end,
                        text=cleaned,
                        speaker=segments[i].speaker,
                        confidence=segments[i].confidence,
                        source=segments[i].source,
                    )
                    applied += 1
            log.info("LLM 修复块 %s/%s：%s/%s 条已更新", bi, len(blocks), applied, hi - lo)
        return out, warnings

    @staticmethod
    def _parse_fix_response(content: str) -> dict[int, str]:
        text = _strip_markdown(content)
        data: Any
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                return {}
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return {}
        if isinstance(data, dict):
            rows = data.get("segments") or data.get("items") or data.get("data")
        else:
            rows = data
        if not isinstance(rows, list):
            return {}
        out: dict[int, str] = {}
        for pos, row in enumerate(rows):
            if isinstance(row, dict):
                idx = row.get("i", row.get("index", row.get("id", pos)))
                try:
                    key = int(idx)
                except (TypeError, ValueError):
                    key = pos
                out[key] = str(row.get("text") or "")
            elif isinstance(row, str):
                out[pos] = row
        return out

    # ------------------------------------------------------------------ #
    def _resegment(self, segments: list[Segment]) -> tuple[list[Segment], list[str]]:
        """按语义重新分段：LLM 只给「切分点」，时间轴仍由原片段推导。

        做法：把整段文本交给模型，要求返回 ``{"breaks":[句子序号...]}``，
        以原片段为单位重组 —— 坚决不让模型重写时间戳。
        """
        warnings: list[str] = []
        if len(segments) < 8:
            return segments, warnings
        max_chars = max(1000, int(self.cfg.llm_max_chars_per_call or 6000))
        numbered = [f"[{i}] {(s.text or '').strip()}" for i, s in enumerate(segments)]
        # 分块处理，块间不做跨块重组（保证时间轴单调且不丢内容）
        blocks: list[list[int]] = []
        cur: list[int] = []
        cur_len = 0
        for i, line in enumerate(numbered):
            if cur_len + len(line) > max_chars and cur:
                blocks.append(cur)
                cur = []
                cur_len = 0
            cur.append(i)
            cur_len += len(line)
        if cur:
            blocks.append(cur)

        out: list[Segment] = []
        for bi, idxs in enumerate(blocks, 1):
            if self.cancel.is_set():
                warnings.append("LLM 分段被取消")
                out.extend(segments[i] for i in idxs)
                continue
            self.on_progress((bi - 1) / max(1, len(blocks)) * 100.0, f"DeepSeek 语义分段 {bi}/{len(blocks)}…")
            listing = "\n".join(numbered[i] for i in idxs)
            try:
                content = self.client.chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                "你是中文课程转写的分段助手。用户在每句话前标注了 [序号]。"
                                "请判断哪些地方应该**换段**（语义/话题转换处），"
                                '只输出 JSON：{"break_after":[序号,...]}，'
                                "表示在给定序号之后换段。不要改写任何文字，不要增删序号。"
                            ),
                        },
                        {"role": "user", "content": listing},
                    ],
                    json_mode=True,
                    temperature=0.0,
                    max_tokens=2048,
                )
                breaks = _parse_break_list(content)
            except LlmError as exc:
                warnings.append(f"语义分段失败（第 {bi} 块）：{exc}")
                out.extend(segments[i] for i in idxs)
                continue
            if breaks is None:
                # 解析失败 ≠ 「不需要换段」。必须保持原分段后原样输出，
                # 否则会被当成「一个断点都没有」→ 把整块合并成一段，
                # 时间轴信息被悄悄抹掉（10 段变 1 段，内容还在所以很难发现）。
                warnings.append(f"第 {bi} 块分段结果无法解析，该块保持原分段")
                out.extend(segments[i] for i in idxs)
                continue
            current: list[Segment] = []
            for i in idxs:
                current.append(segments[i])
                if i in breaks:
                    out.append(_coalesce(current))
                    current = []
            if current:
                out.append(_coalesce(current))
        return [s for s in out if s.text.strip()], warnings

    # ------------------------------------------------------------------ #
    def summarize(self, segments: list[Segment]) -> str:
        """生成结构化摘要（超长时分块摘要再汇总）。"""
        text = join_segment_text(segments)
        if not text.strip():
            return ""
        max_chars = max(2000, int(self.cfg.llm_max_chars_per_call or 6000))
        if len(text) <= max_chars * 2:
            self.on_progress(50.0, "DeepSeek 生成摘要…")
            return self.client.chat(
                [
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": text[: max_chars * 2]},
                ],
                temperature=0.3,
                max_tokens=2048,
            ).strip()

        parts = [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
        partials: list[str] = []
        for i, part in enumerate(parts, 1):
            if self.cancel.is_set():
                break
            self.on_progress(i / len(parts) * 60.0, f"DeepSeek 分段摘要 {i}/{len(parts)}…")
            try:
                partials.append(
                    self.client.chat(
                        [
                            {"role": "system", "content": "请用 5 条以内要点概括以下课程转写片段，不要编造。"},
                            {"role": "user", "content": part},
                        ],
                        temperature=0.2,
                        max_tokens=1024,
                    )
                )
            except LlmError as exc:
                log.warning("分段摘要失败：%s", exc)
        if not partials:
            return ""
        self.on_progress(80.0, "DeepSeek 汇总摘要…")
        return self.client.chat(
            [
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": "以下是各段要点，请汇总成最终结构化摘要：\n\n" + "\n\n".join(partials)},
            ],
            temperature=0.3,
            max_tokens=2048,
        ).strip()


def _coalesce(segments: list[Segment]) -> Segment:
    """把若干片段合并成一段：时间取并集，文本按中英文规则拼接。"""
    if len(segments) == 1:
        return segments[0]
    text = join_segment_text(segments).strip()
    return Segment(
        start=segments[0].start,
        end=max(s.end for s in segments),
        text=text,
        speaker=segments[0].speaker,
        confidence=min((s.confidence for s in segments if s.confidence), default=0.0),
        source=segments[0].source,
    )


def _strip_markdown(content: str) -> str:
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_break_list(content: str) -> set[int] | None:
    """解析「在哪些序号之后换段」。

    返回 ``None`` 表示**解析失败**（无法判断），返回空集合表示「模型明确说不需要换段」。
    这两者语义完全不同：前者必须保持原分段，后者才允许合并。
    """
    text = _strip_markdown(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    rows: Any = None
    if isinstance(data, dict):
        for key in ("break_after", "breaks", "data"):
            if key in data and isinstance(data[key], list):
                rows = data[key]
                break
    elif isinstance(data, list):
        rows = data
    if not isinstance(rows, list):
        return None
    out: set[int] = set()
    for row in rows:
        try:
            out.add(int(row))
        except (TypeError, ValueError):
            continue
    return out


def probe_llm(cfg: AppConfig, api_key: str) -> tuple[bool, str]:
    """DeepSeek 连通性自检（一次极小的 chat 调用）。"""
    if not api_key:
        return False, "未填写 DeepSeek API Key"
    try:
        client = _ChatClient(cfg, api_key)
        reply = client.chat(
            [{"role": "user", "content": "回复两个字：可用"}],
            max_tokens=16,
            temperature=0.0,
            attempts=1,
        )
        return True, f"DeepSeek 可用（model={client.model}，返回：{reply.strip()[:20]}）"
    except LlmError as exc:
        return False, str(exc)
