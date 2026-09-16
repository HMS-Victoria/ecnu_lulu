"""流水线：下载 → 转写 → 后处理 → 产物写入，全程可断点续跑。

阶段与 :class:`~ecnu_transcribe.store.Stage` 一一对应：

    pending ──(解析播放地址)──► probing ──(拉音频)──► downloading ──► audio_ready
      ──(切分+ASR)──► transcribing ──(DeepSeek)──► post_processing ──► writing ──► done

断点续跑语义
------------
* 启动时 :meth:`StateStore.recover_orphans` 把卡在中间态的任务回退到可续跑起点；
* 音频命中 ``cache/media`` 直接跳过下载（不重下）；
* 转写结果落 ``<标题>.transcript.json``；若已存在且音频未变（sha256 一致），
  直接复用，不重复花 ASR 费用（不重跑），除非用户显式「强制重跑」；
* 失败的任务保留 ``retry`` 计数与 ``error`` 详情，可单条重试。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import media, paths
from .catalog import Resource, safe_filename
from .client import EcnuClient, load_session_state
from .config import AppConfig, ConfigManager
from .downloader import AudioDownloader, AudioResult
from .errors import (
    ApiChangedError,
    AsrNotConfiguredError,
    AuthExpiredError,
    DrmDetectedError,
    MediaError,
    TaskCancelled,
    TranscribeHelperError,
    TranscriptionError,
)
from .exporter import ExportResult, export_all
from .llm import TextPostProcessor
from .logbus import get_logger
from .pausegate import PauseGate
from .store import Stage, StateStore, TaskRecord, can_transition
from .transcriber import Transcript, create_transcriber

log = get_logger("pipeline")

ProgressFn = Callable[[int, str, float, str], None]
#: 回调签名：``(task_id, stage, progress, message)``


@dataclass
class PipelineHooks:
    """GUI / CLI 关注的运行时事件。"""

    on_stage: ProgressFn | None = None
    on_log: Callable[[str, str], None] | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    #: 暂停闸门：让「暂停」作用于**任务内部**（阶段之间 / 每个分段 / 每个 LLM 块）
    gate: PauseGate = field(default_factory=PauseGate)

    def stage(self, task_id: int, stage: str, progress: float, message: str = "") -> None:
        if self.on_stage:
            try:
                self.on_stage(task_id, stage, progress, message)
            except Exception:
                pass

    def log_msg(self, level: str, message: str) -> None:
        if self.on_log:
            try:
                self.on_log(level, message)
            except Exception:
                pass

    def checkpoint(self, task_id: int, stage: str, progress: float, message: str = "已暂停") -> None:
        """天然断点：先尝试暂停，再检查取消。

        暂停期间会持续告诉 UI「已暂停」，让用户明确知道任务停在哪一步。
        """
        if not self.gate.is_paused:
            if self.cancel.is_set():
                raise TaskCancelled("任务已被用户取消")
            return
        self.stage(task_id, stage, progress, f"⏸ {message}（点「继续」恢复）")
        notified = 0.0
        while True:
            if not self.gate.wait(timeout=2.0):
                raise TaskCancelled("任务在暂停期间被取消")
            if self.cancel.is_set():
                raise TaskCancelled("任务已被用户取消")
            if not self.gate.is_paused:
                self.stage(task_id, stage, progress, "▶ 已恢复")
                return
            now = time.time()
            if now - notified >= 10.0:  # 每 10s 提醒一次，避免刷屏
                notified = now
                self.stage(task_id, stage, progress, f"⏸ 暂停中（已暂停 {self.gate.paused_seconds():.0f}s）")


def _fmt_db(value: float | None) -> str:
    """把 dB 值写成人类可读形式（None 显示为「未知」）。"""
    return "未知" if value is None else f"{value:.1f} dB"


def _empty_transcript_hint(audio_path: Path, audio_result: Any) -> str:
    """ASR 返回空结果时，给**基于实测**的判断，而不是罗列可能性（缺陷 42）。

    实测：学校有些录播的课堂音轨电平只有 -57 dB（正常语音 -20~-30 dB），
    本地 ASR 的 VAD 会整段判成「无语音」。这类情况必须明确说是**录像本身的问题**，
    否则用户会一直以为是自己的配置/网络/工具坏了。
    """
    level = getattr(audio_result, "level", None) or {}
    peak = (level.get("after") or level.get("before") or {}).get("max_db")
    mean = (level.get("after") or level.get("before") or {}).get("mean_db")
    try:
        if mean is None and peak is None:
            vol = media.probe_volume(Path(audio_path))
            mean, peak = vol.get("mean_db"), vol.get("max_db")
    except Exception:  # noqa: BLE001
        pass
    # 判据用 **平均电平**（实测：没录到声音的录像峰值可能仍有 -24 dB，但 mean 只有 -55 dB）
    if (mean is not None and mean < media.SILENT_MEAN_DB) or (
        mean is None and peak is not None and peak < media.SILENT_PEAK_DB
    ):
        shown = f"平均 {mean:.1f} dB" if mean is not None else f"峰值 {peak:.1f} dB"
        return (
            f"ASR 没有识别出任何文字，但**原因不在你的配置**：这条录像的音轨几乎没有声音"
            f"（实测 {shown}，正常课堂语音的平均电平约 -13 ~ -19 dB，"
            "已尝试自动增益）。这通常是学校录制设备/麦克风没收到声音，"
            "建议在平台上直接试听这条录播确认；换一条有声音的录播即可正常转写。"
        )
    if peak is not None or mean is not None:
        return (
            f"ASR 返回了空结果（音频电平正常：平均 {mean} dB / 峰值 {peak} dB）。"
            "可能原因：这段音频确实没有说话声、语言设置与实际不符、或端点模型不支持该音频。"
        )
    return "ASR 返回了空结果。可能原因：音频无声/过短、语言设置不对、模型不支持该音频编码。"


class Pipeline:
    """单条视频的端到端处理。"""

    def __init__(
        self,
        cfg: AppConfig,
        store: StateStore,
        *,
        cm: ConfigManager | None = None,
        hooks: PipelineHooks | None = None,
        client: EcnuClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.cm = cm
        self.hooks = hooks or PipelineHooks()
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ #
    def _client_or_new(self) -> EcnuClient:
        if self._client is None:
            self._client = EcnuClient(
                self.cfg, config_manager=self.cm, session=load_session_state()
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------ #
    def run(
        self,
        task: TaskRecord,
        resource: Resource,
        *,
        force: bool = False,
    ) -> TaskRecord:
        """跑完一条任务。异常一律转成 ``failed`` 状态返回（不向上抛，除非取消）。"""
        tid = task.id
        output_dir = Path(task.output_dir or self.cfg.resolved_output_dir())
        output_dir.mkdir(parents=True, exist_ok=True)
        self.store.acquire_lock(tid, f"pipe-{id(self)}")
        try:
            return self._run_inner(task, resource, output_dir=output_dir, force=force)
        except TaskCancelled:
            self.store.update_stage(tid, Stage.CANCELED, error="用户取消", force=True)
            self.hooks.stage(tid, str(Stage.CANCELED), 0.0, "已取消")
            raise
        except DrmDetectedError as exc:
            log.error("《%s》检测到 DRM，停止：%s", resource.title, exc)
            self.store.mark_failed(tid, f"DRM：{exc}", retry_bump=False)
            self.hooks.stage(tid, str(Stage.FAILED), 0.0, f"DRM 保护，已停止：{exc}")
            return self.store.get_task(tid) or task
        except AuthExpiredError as exc:
            log.error("登录态失效：%s", exc)
            self.store.mark_failed(tid, f"登录态失效：{exc}", retry_bump=False)
            self.hooks.stage(tid, str(Stage.FAILED), 0.0, f"登录态失效，请重新登录：{exc}")
            return self.store.get_task(tid) or task
        except (MediaError, TranscriptionError, AsrNotConfiguredError, ApiChangedError,
                TranscribeHelperError, Exception) as exc:  # noqa: BLE001
            log.exception("《%s》处理失败：%s", resource.title, exc)
            self.store.mark_failed(tid, str(exc))
            self.hooks.stage(tid, str(Stage.FAILED), 0.0, f"失败：{str(exc)[:200]}")
            return self.store.get_task(tid) or task
        finally:
            self.store.release_lock(tid, f"pipe-{id(self)}")

    # ------------------------------------------------------------------ #
    def _run_inner(
        self,
        task: TaskRecord,
        resource: Resource,
        *,
        output_dir: Path,
        force: bool,
    ) -> TaskRecord:
        tid = task.id
        title = resource.title or task.title
        self.hooks.log_msg("INFO", f"开始处理《{title}》")

        # ---------- 1) 解析播放地址 (probing) ---------- #
        url = resource.play_url or task.play_url
        if not url:
            self.store.update_stage(tid, Stage.PROBING, progress=0.0, message="解析播放地址")
            self.hooks.stage(tid, str(Stage.PROBING), 0.0, "解析播放地址…")
            client = self._client_or_new()
            url = client.resolve_play_url(resource)
        duration = resource.duration_sec or task.duration_sec or 0.0
        current_stage = str(task.stage)
        if can_transition(current_stage, str(Stage.PROBING)):
            self.store.update_stage(
                tid, Stage.PROBING, progress=2.0, play_url=url, duration_sec=duration,
                course=resource.course_name or task.course, title=title,
                message="play_url resolved",
            )
        else:
            # 续跑：任务可能已经比 PROBING 更靠后（例如 audio_ready）。这时退回 probing
            # 会被 store 拒绝，**连 play_url / duration 一起丢掉**，于是界面上的进度与
            # 地址全是旧值。保持当前阶段、只更新字段即可。
            self.store.update_stage(
                tid, current_stage, play_url=url, duration_sec=duration,
                course=resource.course_name or task.course, title=title,
                message="play_url refreshed",
            )

        # ---------- 2) 音频获取 (downloading → audio_ready) ---------- #
        self.hooks.checkpoint(tid, str(Stage.PROBING), 2.0, "解析地址完成，准备拉流")
        audio_path = Path(task.audio_path) if task.audio_path else None
        audio_result: AudioResult | None = None
        needs_audio = (
            force
            or audio_path is None
            or not audio_path.is_file()
            or audio_path.stat().st_size < 1024
        )
        # 「上次留下的音频」**必须验明正身**才能复用（缺陷 50）：
        # 事故现场是任务已记为 audio_ready、缓存里却只有 1900s/3301s 的半份音频，
        # 于是续跑时直接复用它去转写 —— 用户拿到一份**看不出残缺**的稿子。
        # 阶段标记只说明「当时以为完成了」，不能当作产物完整的证据。
        if not needs_audio and audio_path is not None and duration > 0:
            cached_dur = media.duration_of(audio_path)
            tol = float(getattr(self.cfg, "download_truncate_tolerance", 0.05) or 0.05)
            if cached_dur <= 0 or (duration - cached_dur) / duration > tol:
                log.warning(
                    "已有音频不完整：%.1fs / 清单 %.1fs（缺 %.0f%%），重新拉取补全",
                    cached_dur, duration, max(0.0, (duration - cached_dur) / duration * 100),
                )
                self.hooks.log_msg(
                    "WARN",
                    f"上次留下的音频不完整（{cached_dur:.0f}s / 清单 {duration:.0f}s），"
                    "将重新拉取补全，不用这半份做转写",
                )
                needs_audio = True
        if needs_audio or not self.cfg.cache_enabled:
            self.store.update_stage(tid, Stage.DOWNLOADING, progress=3.0, message="拉取音频")
            self.hooks.stage(tid, str(Stage.DOWNLOADING), 3.0, "拉取音频（ffmpeg）…")
            session = load_session_state()
            downloader = AudioDownloader(
                self.cfg,
                session_cookies=session.cookies,
                cancel=self.hooks.cancel,
                on_progress=lambda pct, msg: (
                    self.store.update_stage(tid, Stage.DOWNLOADING, progress=3.0 + pct * 0.85),
                    self.hooks.stage(tid, str(Stage.DOWNLOADING), 3.0 + pct * 0.85, msg),
                ),
                gate=self.hooks.gate,
            )
            if self._client is not None:
                downloader.reuse_http_client(self._client.client)  # 复用连接池
            # 必须把**已知时长**传下去（缺陷 61）：下载器的进度百分比是
            # `已下载秒数 / 总秒数`，时长传 0 时它**根本不上报进度**（界面永远停在 3%），
            # 而且"残件是否已下完"也无从判断 —— 实测 GUI 里那条课就是清单 Resource
            # 的 duration_sec=0（任务行里其实是 3301），于是完整的 25 MB 音频被当成
            # 半成品、白重下一遍。
            audio_result = downloader.fetch(
                resource, play_url=url, output_dir=None, force=force,
                expect_duration=duration or None,
            )
            audio_path = audio_result.path
            for w in audio_result.warnings:
                self.hooks.log_msg("WARN", w)
            self.store.update_stage(
                tid, Stage.AUDIO_READY, progress=88.0,
                audio_path=str(audio_path), audio_sha256=audio_result.sha256,
                duration_sec=audio_result.duration_sec or duration,
                message="audio ready",
            )
            self.hooks.stage(tid, str(Stage.AUDIO_READY), 88.0, f"音频就绪：{audio_path.name}")
        else:
            assert audio_path is not None
            sha = task.audio_sha256
            if not sha:
                # 注意：这里**不能**写 `from . import media` —— 函数级导入会让 media
                # 在整个 _run_inner 里变成局部名，上面用它量时长时会直接
                # UnboundLocalError（本轮的复用校验就这么踩过一次）。模块顶部已导入。
                sha = media.sha256_file(audio_path)
            audio_result = AudioResult(
                path=audio_path, sha256=sha, duration_sec=duration, from_cache=True, source_url=url
            )
            self.store.update_stage(
                tid, Stage.AUDIO_READY, progress=88.0, audio_path=str(audio_path), audio_sha256=sha,
                message="audio cached",
            )
            self.hooks.stage(tid, str(Stage.AUDIO_READY), 88.0, "复用已有音频（不重下）")

        assert audio_path is not None and audio_result is not None

        # 近乎无声的录像：**不送 ASR**（缺陷 45）。
        # 实测把 -67 dB 的底噪硬抬 40 dB 后，Whisper 会编出「字幕by索兰娅」这类模板文本；
        # 交出一份看起来像转写、其实是幻觉的产物，比诚实失败糟糕得多。
        level = getattr(audio_result, "level", None) or {}
        if level.get("silent") and not getattr(self.cfg, "asr_allow_silent", False):
            before = level.get("before") or {}
            mean, peak = before.get("mean_db"), before.get("max_db")
            raise TranscriptionError(
                "这条录像的音轨几乎没有声音"
                f"（平均电平 {_fmt_db(mean)}，正常课堂语音约 -13 ~ -19 dB"
                f"{'；峰值 ' + _fmt_db(peak) if peak is not None else ''}），"
                "已跳过语音识别 —— 对近乎无声的音频强行识别只会得到**编造的文字**。\n"
                "这通常是学校录制设备/麦克风的问题，**不是你的配置问题**："
                "建议在平台上直接试听这条录播确认，换一条有声音的录播即可正常转写。\n"
                "（若你确认要强行识别，可在设置里打开 asr_allow_silent。）"
            )

        # ---------- 3) 转写 (transcribing) ---------- #
        self.hooks.checkpoint(tid, str(Stage.AUDIO_READY), 88.0, "音频就绪，准备语音识别")
        course_dir = output_dir / safe_filename(resource.course_name or "未分课程")
        base = safe_filename(title)
        cached_json = course_dir / f"{base}.transcript.json"
        transcript: Transcript | None = None
        if (
            not force
            and cached_json.is_file()
            and str(cached_json) == (task.transcript_path or str(cached_json))
        ):
            try:
                cached = Transcript.load_json(cached_json)
                if cached.segments and (
                    not audio_result.sha256
                    or cached.meta.get("audio_sha256") in (None, "", audio_result.sha256)
                ):
                    transcript = cached
                    self.hooks.log_msg("INFO", f"复用已有转写结果（不重跑）：{cached_json.name}")
                    self.hooks.stage(tid, str(Stage.TRANSCRIBING), 95.0, "复用已有转写结果")
            except Exception as exc:
                log.warning("复用转写缓存失败：%s", exc)

        if transcript is None:
            self.store.update_stage(tid, Stage.TRANSCRIBING, progress=90.0, message="语音识别")
            self.hooks.stage(tid, str(Stage.TRANSCRIBING), 90.0, "调用语音识别（ASR）…")
            transcriber = create_transcriber(
                self.cfg,
                cm=self.cm,
                on_progress=lambda pct, msg: self.hooks.stage(
                    tid, str(Stage.TRANSCRIBING), 90.0 + pct * 0.05, msg
                ),
                cancel=self.hooks.cancel,
                gate=self.hooks.gate,
            )
            self.hooks.log_msg(
                "INFO", f"ASR provider={getattr(transcriber, 'name', '?')} model={self.cfg.asr_model}"
            )
            transcript = transcriber.transcribe(audio_path, duration_sec=audio_result.duration_sec or duration)
            if transcript.is_empty():
                raise TranscriptionError(_empty_transcript_hint(audio_path, audio_result))
            transcript.meta.setdefault("audio_sha256", audio_result.sha256)
            transcript.meta["audio_path"] = str(audio_path)
            course_dir.mkdir(parents=True, exist_ok=True)
            transcript.save_json(cached_json)
            self.store.update_stage(
                tid, Stage.TRANSCRIBING, progress=95.0, transcript_path=str(cached_json),
                message=f"transcribed {len(transcript.segments)} segments",
            )

        # ---------- 4) LLM 后处理 (post_processing) ---------- #
        self.hooks.checkpoint(tid, str(Stage.TRANSCRIBING), 95.0, "语音识别完成，准备文本加工")
        summary_md = ""
        llm_warnings: list[str] = []
        if self.cfg.llm_enabled:
            self.store.update_stage(tid, Stage.POST_PROCESSING, progress=96.0, message="DeepSeek 后处理")
            self.hooks.stage(tid, str(Stage.POST_PROCESSING), 96.0, "DeepSeek 文本加工…")
            api_key = self.cm.secret("llm_api_key") if self.cm else ""
            if not api_key:
                llm_warnings.append("已开启 LLM 后处理但未配置 DeepSeek API Key，跳过")
                self.hooks.log_msg("WARN", llm_warnings[-1])
            else:
                try:
                    processor = TextPostProcessor(
                        self.cfg, api_key,
                        on_progress=lambda pct, msg: self.hooks.stage(tid, str(Stage.POST_PROCESSING), 96.0, msg),
                        cancel=self.hooks.cancel,
                    )
                    res = processor.process(transcript)
                    if res.applied_fix or res.applied_resegment:
                        transcript = Transcript(
                            segments=res.segments,
                            language=transcript.language,
                            duration_sec=transcript.duration_sec,
                            model=transcript.model,
                            provider=transcript.provider,
                            created_at=transcript.created_at,
                            meta={**transcript.meta, "llm_applied": True, "llm_model": res.model},
                        )
                    summary_md = res.summary_md
                    llm_warnings.extend(res.warnings)
                    for w in res.warnings:
                        self.hooks.log_msg("WARN", f"LLM：{w}")
                except Exception as exc:  # noqa: BLE001 - LLM 失败不阻塞产物
                    llm_warnings.append(f"DeepSeek 后处理失败（已跳过，不影响转写产物）：{exc}")
                    self.hooks.log_msg("WARN", llm_warnings[-1])
        if llm_warnings:
            transcript.meta["llm_warnings"] = llm_warnings

        # ---------- 5) 写出产物 (writing → done) ---------- #
        self.store.update_stage(tid, Stage.WRITING, progress=98.0, message="写出 txt/srt/md")
        self.hooks.stage(tid, str(Stage.WRITING), 98.0, "写出 txt / srt / md…")
        result: ExportResult = export_all(
            resource.with_runtime(title=title, course=resource.course_name or task.course),
            transcript,
            output_dir,
            emit_txt=self.cfg.emit_txt,
            emit_srt=self.cfg.emit_srt,
            emit_md=self.cfg.emit_md,
            summary_md=summary_md,
            bom=bool(getattr(self.cfg, "emit_utf8_bom", True)),
        )
        outputs = [str(p) for p in result.files]
        for p in result.files:
            try:
                self.store.record_artifact(tid, p.suffix.lstrip("."), str(p), size=p.stat().st_size)
            except OSError:
                pass

        final = self.store.update_stage(
            tid, Stage.DONE, progress=100.0,
            outputs=outputs,
            output_dir=str(output_dir),
            audio_path=str(audio_path),
            transcript_path=str(cached_json),
            error="",
            message=f"done: {len(transcript.segments)} segments, {len(outputs)} files",
        )
        self.hooks.stage(tid, str(Stage.DONE), 100.0, f"完成（{len(outputs)} 个文件）")
        self.hooks.log_msg("INFO", f"《{title}》完成：{', '.join(Path(p).name for p in result.files)}")

        if not self.cfg.write_audio_cache and audio_path.is_file():
            try:
                audio_path.unlink()
            except OSError:
                pass
        return final or task


# --------------------------------------------------------------------------- #
# 便于测试 / 命令行的小工具
# --------------------------------------------------------------------------- #
def resource_from_task(task: TaskRecord) -> Resource:
    """从状态库记录还原一个最小可用 :class:`Resource`（用于续跑）。"""
    return Resource(
        resource_id=task.resource_id,
        title=task.title,
        course_id=task.course_id,
        course_name=task.course,
        duration_sec=task.duration_sec,
        play_url=task.play_url,
    )
