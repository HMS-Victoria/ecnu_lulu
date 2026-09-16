"""音频获取：用 ffmpeg 从播放地址拉流，只保留音频。

要点（对齐 M2 验收）
--------------------
* HLS 分片 / AES-128 加密：``-headers`` 里带上与页面同样的 Cookie / Referer / UA，
  ffmpeg 请求 m3u8 与 key 时会复用同一份头（密钥不落地，只进 ffmpeg 进程）。
* ``.m3u8`` 里的相对路径由 ffmpeg 自行还原（它按 m3u8 的 base URL 解析）。
* 超时重试 + 抖动退避；``cache/media/`` 命中即跳过；记录 ``sha256`` 与时长。
* 并发 ≤ 2（``concurrency`` 可配），请求间 300–800ms 抖动，避免触发风控。
* DRM 检测：命中即抛 :class:`DrmDetectedError` 并停止，**不做任何绕过**。
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx

from . import media, paths
from .catalog import Resource, safe_filename
from .config import AppConfig
from .errors import (
    DrmDetectedError,
    MediaError,
    PlayUrlExpiredError,
    StreamStalledError,
    StreamTruncatedError,
    TaskCancelled,
)
from .logbus import get_logger, redact
from .pausegate import PauseGate

log = get_logger("downloader")

ProgressFn = Callable[[float, str], None]


@dataclass
class AudioResult:
    """音频获取结果。"""

    path: Path
    sha256: str = ""
    duration_sec: float = 0.0
    from_cache: bool = False
    source_url: str = ""
    bytes: int = 0
    drm_free: bool = True
    warnings: list[str] = field(default_factory=list)
    #: 电平检测/增益信息（缺陷 42）：``{changed, gain_db, before, after, reason}``
    level: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "duration_sec": self.duration_sec,
            "from_cache": self.from_cache,
            "bytes": self.bytes,
            "source_url": redact(self.source_url),
            "level": self.level,
        }


# --------------------------------------------------------------------------- #
class AudioDownloader:
    """把一条录播资源变成一份**只有音频**的本地文件。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        session_cookies: dict[str, str] | None = None,
        client: httpx.Client | None = None,
        cancel: threading.Event | None = None,
        on_progress: ProgressFn | None = None,
        gate: PauseGate | None = None,
    ) -> None:
        self.cfg = cfg
        self.cookies = session_cookies or {}
        self._client = client
        self.cancel = cancel or threading.Event()
        self.on_progress = on_progress or (lambda _p, _m: None)
        self.gate = gate
        self._sem = threading.Semaphore(max(1, int(self.cfg.concurrency)))
        self.ffmpeg = media.find_ffmpeg(self.cfg.ffmpeg_path)
        #: Range 支持情况按 URL 缓存，避免每次下载都探测
        self._range_ok: dict[str, bool] = {}

    # ------------------------------------------------------------------ #
    def _notify(self, progress: float, message: str) -> None:
        try:
            self.on_progress(max(0.0, min(100.0, progress)), message)
        except Exception:
            pass

    def reuse_http_client(self, client: httpx.Client) -> None:
        """复用外部（已带登录态）的连接池做 m3u8 预检，避免重复建连。"""
        self._client = client

    def _check_cancel(self, *, stage: str = "downloading") -> None:
        """取消检查 + 暂停闸门。

        暂停在这里用「阻塞 ffmpeg 输出读取」实现：
        调用方（:meth:`_download_once` 的输出行回调）会在返回前等闸门放行，
        于是 ffmpeg 的 stdout 管道被写满、ffmpeg 自身随之阻塞 ——
        相当于把整条拉流**就地冻结**，恢复后从原处继续，不需要重下。
        """
        if self.cancel.is_set():
            raise TaskCancelled("任务已被用户取消")
        if self.gate is not None and self.gate.is_paused:
            self._notify(0.0, "⏸ 已暂停（拉流已冻结，点「继续」恢复）")
            while True:
                if not self.gate.wait(timeout=2.0):
                    raise TaskCancelled("任务在暂停期间被取消")
                if self.cancel.is_set():
                    raise TaskCancelled("任务已被用户取消")
                if not self.gate.is_paused:
                    self._notify(0.0, "▶ 已恢复")
                    return
                self._notify(0.0, f"⏸ 暂停中（已暂停 {self.gate.paused_seconds():.0f}s）")

    # ------------------------------------------------------------------ #
    def cache_path(self, resource: Resource) -> Path:
        """缓存文件名：``<安全标题>__<resource_id 短哈希>.<ext>``。"""
        ext = self.cfg.audio_format or "mp3"
        stem = safe_filename(resource.title, max_len=80)
        rid = re.sub(r"[^0-9A-Za-z_-]", "", resource.resource_id or "")[:16]
        return paths.media_cache_dir() / f"{stem}__{rid}.{ext}"

    def output_path(self, resource: Resource, output_dir: Path) -> Path:
        """产物目录里的音频（用户选择「保留音频」时才有意义）。"""
        return output_dir / "audio" / f"{safe_filename(resource.title)}{'.' + (self.cfg.audio_format or 'mp3')}"

    # ------------------------------------------------------------------ #
    def fetch(
        self,
        resource: Resource,
        *,
        play_url: str = "",
        output_dir: Path | None = None,
        expect_duration: float | None = None,
        force: bool = False,
    ) -> AudioResult:
        """获取音频（命中缓存直接返回；``force=True`` 时忽略缓存重下）。"""
        url = play_url or resource.play_url
        if not url:
            raise MediaError(f"资源《{resource.title}》没有可用的播放地址")

        target = self.cache_path(resource)
        expect = float(expect_duration if expect_duration is not None else resource.duration_sec or 0)

        # 不完整的缓存文件先降级为「断点基准」（**无论 force 与否**）。
        # force 的语义是「不要相信缓存」，但半份文件不是「不可信的缓存」，
        # 而是**已经花掉的下载时间的成果** —— 直接重下等于白扔几十分钟。
        if target.is_file() and target.stat().st_size > 1024 and expect > 0:
            cached_dur = media.duration_of(target)
            tol = float(getattr(self.cfg, "download_truncate_tolerance", 0.05) or 0.05)
            if cached_dur > 0 and (expect - cached_dur) / expect > tol:
                log.info(
                    "缓存音频不完整（%.1fs/%.1fs），降级为断点基准以便续传", cached_dur, expect
                )
                self._keep_as_partial(target, cached_dur, expect)

        # 上次留下的成果先用起来（缺陷 59/61）：
        #   ① 已经下完的（`.part` 或 `.partial`）⇒ 直接扶正为正式音频；
        #   ② 只下一半的 `.part` ⇒ 转成断点基准，后面只补抓剩余部分。
        self._adopt_complete_leftover(target, expect)
        self._promote_leftover_part(target, expect)

        if self.cfg.cache_enabled and not force and target.is_file() and target.stat().st_size > 1024:
            dur = media.duration_of(target)
            if expect <= 0 or abs(dur - expect) <= max(5.0, expect * 0.02):
                self._notify(100.0, f"命中音频缓存：{target.name}")
                log.info("命中缓存（时长 %.1fs）：%s", dur, target)
                warnings: list[str] = []
                level = self._apply_level_policy(target, warnings)
                return AudioResult(
                    path=target,
                    sha256=media.sha256_file(target),
                    duration_sec=dur,
                    from_cache=True,
                    source_url=url,
                    bytes=target.stat().st_size,
                    warnings=warnings,
                    level=level,
                )
            log.info("缓存时长不符（缓存 %.1fs vs 预期 %.1fs），重新下载", dur, expect)
            # 短的那份已在 fetch() 开头降级成 .partial（断点基准）；走到这里说明
            # 缓存文件是**偏长**的（例如换了清晰度/重录），直接覆盖即可。

        with self._sem:
            self._check_cancel()
            self._notify(1.0, "等待连接槽位…" if self._sem._value <= 0 else "开始拉流")
            result = self._download_with_retry(url, target, expect_duration=expect)

        if output_dir is not None and self.cfg.keep_video:
            # 保留音频副本到输出目录（用户显式开启时）
            dst = self.output_path(resource, output_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            media.convert_audio(
                result.path,
                dst,
                fmt=self.cfg.audio_format,
                bitrate=self.cfg.audio_bitrate,
                sample_rate=self.cfg.audio_sample_rate,
                channels=self.cfg.audio_channels,
            )
            result.warnings.append(f"已复制音频到输出目录：{dst}")
        return result

    # ------------------------------------------------------------------ #
    def _download_with_retry(self, url: str, target: Path, *, expect_duration: float = 0.0) -> AudioResult:
        attempts = max(1, int(self.cfg.download_retries))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._check_cancel()
            try:
                self._notify(2.0, f"开始拉流（第 {attempt}/{attempts} 次）")
                return self._download_once(url, target, expect_duration=expect_duration)
            except DrmDetectedError:
                raise
            except TaskCancelled:
                raise
            except PlayUrlExpiredError:
                # 签名过期的地址**重试没有意义**：立刻上抛，交给 pipeline 重新解析播放地址。
                # 早先这里会拿同一个死 URL 退避重试 4 次（白等几十秒），最后还给出一句
                # 「音频获取失败（重试 4 次）」——用户既不知道要重试什么，也不知道能做什么。
                self._notify(2.0, "播放地址已失效，正在重新获取…")
                log.warning("播放地址已失效（不再重试同一 URL）：%s", exc)
                raise
            except (MediaError, Exception) as exc:  # noqa: BLE001
                last_error = exc
                wait = min(30.0, 2.0 ** attempt) + (time.time() % 1.0)
                log.warning("拉流失败（第 %s/%s 次）：%s；%.1fs 后重试", attempt, attempts, exc, wait)
                self._notify(2.0, f"拉流失败，{wait:.0f}s 后重试（{attempt}/{attempts}）：{exc}")
                if attempt < attempts:
                    # 退避期间也要能被取消
                    end = time.time() + wait
                    while time.time() < end:
                        self._check_cancel()
                        time.sleep(0.25)
        if isinstance(last_error, (StreamTruncatedError, StreamStalledError)):
            # 保留类型：上层（任务页/复验脚本）要能区分「抓了一半」和「根本打不开」，
            # 这两类给用户的建议完全不同。
            raise type(last_error)(
                f"音频获取失败（已重试 {attempts} 次）：{last_error}"
            ) from last_error
        raise MediaError(f"音频获取失败（重试 {attempts} 次）：{last_error}")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_local_source(url: str) -> bool:
        """判断播放源是不是**本地文件**（而不是网络流）。

        本地文件不能带 ``-headers`` / ``-reconnect*`` 这些网络侧参数，
        否则 ffmpeg 会直接报 ``Option headers not found`` 之类打不开
        （这里踩过一次：拿本地 wav 当播放源做暂停验证时任务失败）。
        """
        low = (url or "").strip().lower()
        if low.startswith("file://"):
            return True
        if "://" in low:
            return False
        # 没有 scheme：当成本地路径
        try:
            return Path(url).exists()
        except OSError:
            return False

    # ------------------------------------------------------------------ #
    @staticmethod
    def partial_path(target: Path) -> Path:
        """断点续传基准文件：``<目标>.partial``（与 ffmpeg 的 ``.part`` 区分开）。"""
        return target.with_name(target.name + ".partial")

    # ------------------------------------------------------------------ #
    # 并行取音频（缺陷 63）
    # ------------------------------------------------------------------ #
    def _url_supports_range(self, url: str) -> bool:
        """探测服务端是否支持 `Range`（支持才能按时间窗并行取）。结果按 URL 缓存。"""
        cached = self._range_ok.get(url)
        if cached is not None:
            return cached
        headers = {
            "Referer": self.cfg.portal_url,
            "User-Agent": self.cfg.user_agent,
            "Range": "bytes=0-1023",
        }
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        owned = self._client is None
        client = self._client or httpx.Client(
            timeout=30.0, follow_redirects=True, verify=self.cfg.verify_tls,
            trust_env=not self.cfg.proxy, proxy=self.cfg.proxy or None,
        )
        try:
            r = client.get(url, headers=headers)
            ok = r.status_code == 206 and len(r.content) <= 8192
        except Exception as exc:  # noqa: BLE001 — 探测失败就退回单流，不影响主流程
            log.info("Range 探测失败（将用单流下载）：%s", exc)
            ok = False
        finally:
            if owned:
                client.close()
        self._range_ok[url] = ok
        log.info("播放地址支持 Range：%s", "是（可并行取音频）" if ok else "否（用单流下载）")
        return ok

    def _download_parallel(
        self, url: str, target: Path, *, expect_duration: float, input_args: list[str]
    ) -> AudioResult | None:
        """按**时间窗**并行取音频：每个窗口各起一个 ffmpeg（`-ss/-t` 只取该段字节），
        最后用 concat 解复用器拼成一个文件。

        为什么这样做而不是"整段 mp4 并行下载再抽音频"：`-ss` 在支持 Range 的服务端是
        **按字节跳转**的（实测 206），所以每个窗口只拉自己那 1/N 的字节，
        既省磁盘（只需 ~26 MB 音频，不用落 838 MB 的 mp4），也能直接复用已验证的拼接逻辑。

        返回 ``None`` 表示"没做成"（不支持 Range / 某窗口失败 / 校验不过），
        调用方会退回原来的单流下载 —— 不冒险。
        """
        conns = int(getattr(self.cfg, "download_connections", 0) or 0)
        total = float(expect_duration or 0)
        min_win = float(getattr(self.cfg, "download_window_min_sec", 120.0) or 120.0)
        if conns < 2 or total <= 0 or total < min_win * 2:
            return None
        if not self._url_supports_range(url):
            return None

        parts_n = max(2, min(conns, int(total // min_win) or 2))
        win = total / parts_n
        segs = [target.with_suffix(target.suffix + f".seg{i:02d}") for i in range(parts_n)]
        merged = target.with_suffix(target.suffix + ".parallel")
        for stale in [*segs, merged]:
            stale.unlink(missing_ok=True)

        started = time.time()
        self._notify(3.0, f"并行取音频：{parts_n} 路，每路 {win:.0f}s（实测单连接 ~670 KB/s，多路更快）")
        log.info("并行取音频：%d 路 × %.0fs（总 %.0fs）", parts_n, win, total)

        progress = {"media": 0.0, "total": total, "window_at": time.monotonic(), "window_media": 0.0}
        per_part = [0.0] * parts_n
        lock = threading.Lock()
        failures: list[str] = []
        cancelled = threading.Event()

        def _watch_factory(index: int, part_start: float, part_len: float):
            base = self._watchdog_factory(progress)

            def _watch() -> str | None:
                if self.cancel.is_set():
                    cancelled.set()
                    return "任务已取消"
                # 每个窗口自己的进度：本窗口的 out_time 直接参与总进度
                local = progress.get(f"part{index}", 0.0)
                with lock:
                    per_part[index] = local
                    progress["media"] = min(total, sum(per_part))
                if local >= part_len * 0.995:
                    return None  # 本窗口抓完，正在收尾
                return base()

            return _watch

        def _run_part(index: int, start: float) -> None:
            length = min(win, max(0.0, total - start))
            seg = segs[index]
            cmd = [str(self.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "warning",
                   "-progress", "pipe:1", "-ss", f"{start:.3f}", "-t", f"{length:.3f}"]
            cmd += input_args
            cmd += ["-i", url]
            cmd += media.build_output_args(
                fmt=self.cfg.audio_format, bitrate=self.cfg.audio_bitrate,
                sample_rate=self.cfg.audio_sample_rate, channels=self.cfg.audio_channels,
            )
            cmd += [str(seg)]

            def on_line(line: str) -> None:
                stripped = line.strip()
                if self.gate is not None and self.gate.is_paused and not self.cancel.is_set():
                    self.gate.wait(timeout=2.0)
                parsed = media.estimate_duration_from_progress(stripped)
                if not parsed:
                    return
                kind, secs = parsed
                if kind == "time" and secs > 0:
                    progress[f"part{index}"] = max(progress.get(f"part{index}", 0.0), float(secs))
                    with lock:
                        progress["media"] = min(total, sum(
                            progress.get(f"part{i}", 0.0) for i in range(parts_n)
                        ))

            try:
                media.run_ffmpeg(
                    cmd, on_stderr_line=on_line, timeout=None, check=True,
                    watchdog=_watch_factory(index, start, length),
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"第 {index + 1}/{parts_n} 路（{start:.0f}s 起）：{exc}"[:300])
                return
            if not seg.is_file() or seg.stat().st_size < 1024:
                failures.append(f"第 {index + 1}/{parts_n} 路没有产出有效音频")

        threads = [
            threading.Thread(target=_run_part, args=(i, i * win), name=f"dl-part{i}", daemon=True)
            for i in range(parts_n)
        ]
        for t in threads:
            t.start()

        # 协调线程：定期上报总进度；取消时统一收尾
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
            if self.cancel.is_set():
                cancelled.set()
                break
            pct = 3.0 + min(80.0, progress["media"] / total * 80.0)
            self._notify(pct, f"并行拉流中 {progress['media']:.0f}/{total:.0f}s"
                              f"（{parts_n} 路，最快 {progress['media'] / max(0.1, time.time() - started):.1f}× 实时）")
        for t in threads:
            t.join(timeout=30)

        if cancelled.is_set() or self.cancel.is_set():
            for stale in [*segs, merged]:
                stale.unlink(missing_ok=True)
            raise TaskCancelled("任务已被用户取消")

        if failures:
            log.warning("并行取音频失败，退回单流下载：%s", "；".join(failures[:2]))
            self._notify(3.0, "并行取音频未成功，退回单流下载…")
            for stale in [*segs, merged]:
                stale.unlink(missing_ok=True)
            return None

        # 拼接（顺序严格按窗口起点）
        listing = target.with_suffix(target.suffix + ".parallel.txt")
        listing.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in segs), encoding="utf-8"
        )
        try:
            media.run_ffmpeg(
                [str(self.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", str(listing),
                 "-c", "copy", "-f", media.muxer_for(self.cfg.audio_format), "-y", str(merged)],
                timeout=1800, check=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("并行取音频拼接失败，退回单流下载：%s", exc)
            for stale in [*segs, merged, listing]:
                stale.unlink(missing_ok=True)
            return None
        finally:
            listing.unlink(missing_ok=True)

        duration = media.duration_of(merged)
        tol = float(getattr(self.cfg, "download_truncate_tolerance", 0.05) or 0.05)
        if duration <= 0 or (total - duration) / total > tol:
            log.warning("并行取音频结果不完整（%.0fs/%.0fs），退回单流下载", duration, total)
            for stale in [*segs, merged]:
                stale.unlink(missing_ok=True)
            return None

        merged.replace(target)
        for stale in segs:
            stale.unlink(missing_ok=True)

        sha = media.sha256_file(target)
        warnings = [f"并行取音频：{parts_n} 路并发（单连接约 670 KB/s，并行为其 {parts_n} 路聚合）"]
        level = self._apply_level_policy(target, warnings)
        if level.get("changed"):
            sha = media.sha256_file(target)
        speed = total / max(0.1, time.time() - started)
        self._notify(85.0, f"音频就绪（并行）：{target.name}（{duration:.0f}s, "
                           f"{_mb(target.stat().st_size)}, {speed:.1f}× 实时）")
        log.info(
            "并行取音频完成：%s 时长=%.1fs 大小=%s 用时=%.1fs（%.1f× 实时，%d 路）",
            target.name, duration, _mb(target.stat().st_size), time.time() - started, speed, parts_n,
        )
        return AudioResult(
            path=target, sha256=sha, duration_sec=duration, from_cache=False,
            source_url=url, bytes=target.stat().st_size, warnings=warnings, level=level,
        )

    def _adopt_complete_leftover(self, target: Path, expect: float) -> bool:
        """把上次留下的 `.part` / `.partial` 里**已经下完**的那份扶正为正式音频。

        返回 True 表示已扶正 —— 调用方随后的缓存判定会直接命中，一次下载都不会发生。

        为什么单独做这一步：`.part` 是 ffmpeg 的中间产物，`.partial` 是断点基准，
        两者都可能**已经完整**（进程在收尾改名那一刻被杀，实测用户的文件正是
        ffprobe 3301.3s / 清单 3301.0s）。少了这一步，25 MB 的成果会被当成"不存在"重下。

        ``expect <= 0``（清单时长未知）时**什么都不做**：无法判断完整性时把半成品扶正，
        就等于把残缺当完整，那是本末倒置。
        """
        if expect <= 0:
            return False
        for cand in (target.with_suffix(target.suffix + ".part"), self.partial_path(target)):
            if not cand.is_file() or cand.stat().st_size < 1024:
                continue
            dur = media.duration_of(cand)
            if dur < expect * 0.98:
                continue
            try:
                cand_size = cand.stat().st_size   # 必须在改名**之前**取：改名后它就不存在了
                cand.replace(target)
            except OSError as exc:
                log.warning("扶正已下完的 %s 失败（将按断点/重下处理）：%s", cand.name, exc)
                return False
            log.info(
                "发现已下完的 %s（%.1fs / 清单 %.1fs），扶正为正式音频，无需重下",
                cand.name, dur, expect,
            )
            # 顺手清掉另一份更小的残留（例如一边是完整 .partial、一边是半截 .part），
            # 免得磁盘上留两份让人以为"还在下载"。只删比它更小的，绝不删更长的。
            for other in (target.with_suffix(target.suffix + ".part"), self.partial_path(target)):
                if other == cand or not other.is_file():
                    continue
                try:
                    if other.stat().st_size <= cand_size:
                        other.unlink(missing_ok=True)
                except OSError:
                    pass
            return True
        return False

    def _promote_leftover_part(self, target: Path, expect: float) -> None:
        """把上次被中断留下的 ``.part`` 提升为断点基准（缺陷 59）。

        ffmpeg 的输出写的是 ``<目标>.part``；进程被杀、应用被关窗时，它**留在磁盘上**。
        旧行为是下次整段重下 —— 实测用户关窗那一刻已经下了 95%（25.19 MB / 26.4 MB），
        等于白扔 25 MB。这里把它认成断点，交给 `_try_resume` 只补抓剩余部分。
        """
        part = target.with_suffix(target.suffix + ".part")
        if not part.is_file() or part.stat().st_size < 1024:
            return
        if expect <= 0:
            # 清单时长未知：既不能扶正（怕把半成品当完整），也不该改名（会把它从
            # "下次可直接扶正"变成"断点"，而断点又因为没有总时长而无法续传 ——
            # 实测就是这么把一份完整音频变成白重下的）。原样留着，等有总时长时再处理。
            log.info(
                "残留的 .part 暂时无法处理（清单时长未知，%.1f MB）：%s",
                part.stat().st_size / 1048576, part.name,
            )
            return
        dur = media.duration_of(part)
        if expect > 0 and dur >= expect * 0.98:
            # `.part` 其实**已经下完**了（进程在收尾/改名那一刻被杀）。
            # 实测用户就卡在这个状态：25.19 MB、ffprobe 3301.3s vs 清单 3301.0s。
            # 直接扶正为正式音频 —— 否则下面的缓存判定看不到它，会从零重下 25 MB。
            try:
                part.replace(target)
            except OSError as exc:
                log.warning("扶正已下完的 .part 失败（将整段重下）：%s", exc)
                return
            log.info("发现已下完的 .part（%.1fs/预期 %.1fs），直接扶正为正式音频，无需重下",
                     dur, expect)
            return
        if dur <= 0:
            log.info("残留的 .part 读不出时长（%.1f MB），无法作为断点：%s",
                     part.stat().st_size / 1048576, part.name)
            return
        log.info("发现上次中断留下的 .part（%.1fs / 预期 %.1fs），转为断点基准",
                 dur, expect or 0.0)
        self._keep_as_partial(part, dur, expect, base=target)

    def _keep_as_partial(
        self, src: Path, duration: float, expect: float, *, base: Path | None = None
    ) -> None:
        """把一份**不完整**的音频转存为断点续传基准（缺陷 50）。"""
        if not src.is_file() or duration <= 0:
            return
        if expect > 0 and duration >= expect * 0.98:
            return  # 不缺，别拿它当断点
        partial = self.partial_path(base or src)
        if partial.is_file() and media.duration_of(partial) >= duration:
            src.unlink(missing_ok=True)  # 已有更长的断点，别留更短的那份
            return
        try:
            src.replace(partial)
        except OSError as exc:
            log.warning("保留断点文件失败（忽略）：%s", exc)
            return
        log.info(
            "已保留断点基准：%s（%.1fs/%.1fs，下次可从断点续传）", partial.name, duration, expect
        )

    # ------------------------------------------------------------------ #
    def _watchdog_factory(self, monitor: dict[str, float]) -> Callable[[], str | None]:
        """构造看门狗回调：判定「拉流是否还有意义」（缺陷 49）。

        判据是**等效倍速** —— 每次触发时看：自窗口起点以来推进了多少媒体秒数。
        低于 ``download_min_speed_ratio`` 倍实时就中止本次拉流，
        因为「继续等」在时间上已经不可接受（55 分钟的课要等几小时）。

        两种情况都必须能被抓住：
        * 完全卡死（一个字节都不再发）→ 推进 0s，必然低于阈值；
        * 龟速推进（服务端严重限速）→ 推进量远小于阈值，同样中止。
        """
        stall_secs = float(getattr(self.cfg, "download_stall_seconds", 120.0) or 0.0)
        min_ratio = float(getattr(self.cfg, "download_min_speed_ratio", 0.25) or 0.0)
        if stall_secs <= 0:
            return lambda: None

        def _watch() -> str | None:
            now = time.monotonic()
            # 暂停不算停滞：这时管道被写满、ffmpeg 本来就该停住
            if self.gate is not None and self.gate.is_paused:
                monitor["window_at"] = now
                monitor["window_media"] = monitor["media"]
                return None
            if self.cancel.is_set():
                return None
            total = monitor.get("total", 0.0)
            if total > 0 and monitor["media"] >= total * 0.995:
                return None  # 已经抓完，正在收尾（封头 / 写 tail）
            elapsed = now - monitor["window_at"]
            if elapsed < stall_secs:
                return None
            advanced = monitor["media"] - monitor["window_media"]
            if advanced >= min_ratio * elapsed:
                monitor["window_at"] = now  # 还有意义 → 开一个新观察窗
                monitor["window_media"] = monitor["media"]
                return None
            return (
                f"拉流停滞：{elapsed:.0f}s 内只推进了 {advanced:.0f}s 音频"
                f"（等效 {advanced / elapsed:.2f}× 实时，低于 {min_ratio:.2f}× 下限）。"
                "服务端可能已停止发送数据或严重限速；已中止本次拉流并重试。"
            )

        return _watch

    # ------------------------------------------------------------------ #
    def _try_resume(
        self, url: str, target: Path, input_args: list[str], *, total: float
    ) -> AudioResult | None:
        """从 ``.partial`` 断点续传：只抓剩余部分再拼接（缺陷 50 的配套能力）。

        做法：``-ss <已有秒数>`` 让 ffmpeg 从断点处开始抓（输入侧 seek，配合
        默认的 ``-accurate_seek`` 会精确落在断点上），再用 concat 解复用器把
        两段 ``-c copy`` 接起来 —— 两段是同一次配置产出的 16kHz 单声道 CBR mp3，
        帧彼此独立，可以直接拼接。

        返回 ``None`` 表示「没续上」，交给正常流程整段重下（绝不半途而废地
        交出半份音频）。续传结果同样要过截断检查。
        """
        partial = self.partial_path(target)
        if total <= 0 or not partial.is_file() or partial.stat().st_size < 1024:
            return None
        have = media.duration_of(partial)
        if have <= 0 or have >= total * 0.98:
            return None

        tol = float(getattr(self.cfg, "download_truncate_tolerance", 0.05) or 0.05)
        rest = target.with_suffix(target.suffix + ".rest")
        merged = target.with_suffix(target.suffix + ".part")
        listfile = target.with_suffix(target.suffix + ".concat.txt")
        for stale in (rest, merged, listfile):
            stale.unlink(missing_ok=True)

        started = time.time()
        warnings = [f"从断点续传：沿用已抓到的 {have:.0f}s，只补抓剩余 {total - have:.0f}s"]
        self._notify(3.0, f"发现未完成的音频（{have:.0f}s/{total:.0f}s），从断点续传…")
        log.info("断点续传：%s 已有 %.1fs，补抓剩余 %.1fs", partial.name, have, total - have)

        monitor: dict[str, float] = {
            "media": have,
            "total": float(total),
            "window_at": time.monotonic(),
            "window_media": have,
        }
        last_notify = 0.0
        tail: list[str] = []

        def on_line(line: str) -> None:
            nonlocal last_notify
            stripped = line.strip()
            if self.gate is not None and self.gate.is_paused and not self.cancel.is_set():
                self.gate.wait(timeout=2.0)
            tail.append(line)
            if len(tail) > 200:
                del tail[:100]
            parsed = media.estimate_duration_from_progress(line)
            if not parsed:
                return
            kind, secs = parsed
            if kind != "time" or secs <= 0:
                return
            monitor["media"] = max(monitor["media"], have + float(secs))
            now = time.time()
            if now - last_notify >= 1.0:
                last_notify = now
                done = min(total, have + float(secs))
                pct = 3.0 + min(80.0, done / total * 80.0)
                self._notify(pct, f"续传中 {done:.0f}/{total:.0f}s（断点 {have:.0f}s）")

        cmd = [str(self.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "warning", "-progress", "pipe:1"]
        cmd += ["-ss", f"{have:.3f}"]
        cmd += input_args
        cmd += ["-i", url]
        cmd += media.build_output_args(
            fmt=self.cfg.audio_format,
            bitrate=self.cfg.audio_bitrate,
            sample_rate=self.cfg.audio_sample_rate,
            channels=self.cfg.audio_channels,
        )
        cmd += [str(rest)]
        try:
            media.run_ffmpeg(
                cmd,
                on_stderr_line=on_line,
                timeout=None,
                check=True,
                watchdog=self._watchdog_factory(monitor),
            )
        except Exception as exc:  # noqa: BLE001 — 续传失败就退回整段重下
            log.warning("断点续传抓取剩余部分失败（转整段重下）：%s", exc)
            rest.unlink(missing_ok=True)
            return None

        if not rest.is_file() or rest.stat().st_size < 1024:
            log.warning("断点续传没抓到有效剩余部分（转整段重下）")
            rest.unlink(missing_ok=True)
            return None

        # 拼接：concat 解复用器 + -c copy（两段同编码参数，直接接得上）
        listfile.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in (partial, rest)), encoding="utf-8"
        )
        try:
            media.run_ffmpeg(
                [
                    str(self.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", str(listfile),
                    "-c", "copy", "-f", media.muxer_for(self.cfg.audio_format),
                    "-y", str(merged),
                ],
                timeout=1800,
                check=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("断点续传拼接失败（转整段重下）：%s", exc)
            for stale in (rest, merged, listfile):
                stale.unlink(missing_ok=True)
            return None
        finally:
            listfile.unlink(missing_ok=True)

        duration = media.duration_of(merged)
        if duration <= 0 or (total - duration) / total > tol:
            log.warning("断点续传结果仍不完整（%.0fs/%.0fs），保留较长的一段转为整段重下", duration, total)
            self._keep_as_partial(merged, duration, total, base=target)
            rest.unlink(missing_ok=True)
            return None
        rest.unlink(missing_ok=True)
        merged.replace(target)
        # 断点已并入正式产物，必须清掉：留着它下次会被当成「已有 5s」的过期基准，
        # 一旦缓存失效就会从旧断点重新续传。
        partial.unlink(missing_ok=True)

        sha = media.sha256_file(target)
        level = self._apply_level_policy(target, warnings)
        if level.get("changed"):
            sha = media.sha256_file(target)
        self._notify(85.0, f"断点续传完成：{target.name}（{duration:.0f}s, {_mb(target.stat().st_size)}）")
        log.info(
            "断点续传完成：%s 时长=%.1fs 大小=%s sha256=%s 用时=%.1fs（断点 %.1fs）",
            target.name, duration, _mb(target.stat().st_size), sha[:16], time.time() - started, have,
        )
        return AudioResult(
            path=target,
            sha256=sha,
            duration_sec=duration,
            from_cache=False,
            source_url=url,
            bytes=target.stat().st_size,
            warnings=warnings,
            level=level,
        )

    # ------------------------------------------------------------------ #
    def _download_once(self, url: str, target: Path, *, expect_duration: float = 0.0) -> AudioResult:
        """一次 ffmpeg 下载。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        local_file = False
        if self._is_local_source(url):
            local_path = Path(url[7:]) if url.lower().startswith("file://") else Path(url)
            if not local_path.is_file():
                raise MediaError(f"本地音频文件不存在：{local_path}")
            url = str(local_path)
            local_file = True

        # 1) 明文预检：m3u8 里若有 DRM 标记，直接停（仅网络流需要）
        warnings: list[str] = []
        if not local_file and url.lower().split("?")[0].endswith(".m3u8"):
            self._precheck_hls(url, warnings)

        if local_file:
            # 本地文件走纯转码路径：不带任何网络参数
            input_args: list[str] = []
            self._notify(3.0, "本地音频文件：直接抽取音频（无需下载）")
        else:
            headers = {"Referer": self.cfg.portal_url, "User-Agent": self.cfg.user_agent}
            input_args = media.build_input_args(
                url,
                headers=headers,
                cookie="; ".join(f"{k}={v}" for k, v in self.cookies.items()),
                referer=self.cfg.portal_url,
                user_agent=self.cfg.user_agent,
                speed_limit_kib=self.cfg.speed_limit_kib,
            )
            # 有断点就先补抓剩余部分（省掉重下几十分钟），成功即直接返回
            resumed = self._try_resume(url, target, input_args, total=float(expect_duration or 0.0))
            if resumed is not None:
                return resumed

        # 2) 先试**并行取音频**（缺陷 63：实测该 CDN 单连接 ~670 KB/s、4 路聚合 ~2.1 MB/s）；
        #    任何一步不成就退回下面的单流下载，绝不冒险。
        if not local_file:
            parallel = self._download_parallel(
                url, target, expect_duration=float(expect_duration or 0.0), input_args=input_args
            )
            if parallel is not None:
                return parallel

        # 3) ffmpeg 只取音频
        cmd = [str(self.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "warning", "-progress", "pipe:1"]
        cmd += input_args
        cmd += ["-i", url]
        cmd += media.build_output_args(
            fmt=self.cfg.audio_format,
            bitrate=self.cfg.audio_bitrate,
            sample_rate=self.cfg.audio_sample_rate,
            channels=self.cfg.audio_channels,
        )
        cmd += [str(part)]

        self._notify(3.0, "ffmpeg 拉流并抽取音频…")
        total = expect_duration or 0.0
        last_notify = 0.0
        tail: list[str] = []
        drm_hits: list[str] = []
        started = time.time()
        # 看门狗监视器（缺陷 49）：media=已推进的媒体秒数，window_* 是当前观察窗
        monitor: dict[str, float] = {
            "media": 0.0,
            "total": float(total),
            "window_at": time.monotonic(),
            "window_media": 0.0,
        }
        watchdog = self._watchdog_factory(monitor)
        # 限速：ffmpeg 输入侧**没有**带宽限制选项（-maxrate 是输出侧编码参数，
        # 放到 -i 前面会直接报 "is not a decoding option" 让下载全失败）。
        # 所以这里自己节流：读 `-progress` 的 total_size，按目标速率睡眠。
        # 副作用是 ffmpeg 的 stdout 管道被写满后它自己也会慢下来 —— 正是我们要的。
        limit_bps = float(self.cfg.speed_limit_kib or 0) * 1024.0
        downloaded = 0
        in_progress_block = False

        def _apply_rate_limit() -> None:
            if limit_bps <= 0 or downloaded <= 0:
                return
            expected = downloaded / limit_bps
            elapsed = time.time() - started
            ahead = expected - elapsed
            if ahead > 0.02:
                # 上限 2s，避免长时间睡死导致暂停/取消响应迟钝
                time.sleep(min(2.0, ahead))

        def on_line(line: str) -> None:
            nonlocal last_notify, downloaded, in_progress_block
            # 暂停闸门：在这里阻塞会写满 ffmpeg 的 stdout 管道，从而把拉流就地冻结
            if self.gate is not None and self.gate.is_paused and not self.cancel.is_set():
                self._notify(0.0, "⏸ 已暂停（拉流已冻结，点「继续」恢复）")
                reloaded = 0.0
                while self.gate.is_paused and not self.cancel.is_set():
                    self.gate.wait(timeout=2.0)
                    reloaded += 2.0
                    if reloaded >= 10.0:  # 每 10s 提示一次
                        reloaded = 0.0
                        self._notify(0.0, f"⏸ 暂停中（已暂停 {self.gate.paused_seconds():.0f}s）")
                if not self.cancel.is_set():
                    self._notify(0.0, "▶ 已恢复")

            # 收集 ffmpeg -progress 的字节数（进度块以 progress= 结束）
            stripped = line.strip()
            if stripped.startswith("progress="):
                in_progress_block = False
                _apply_rate_limit()
            elif stripped.startswith("total_size="):
                in_progress_block = True
                try:
                    downloaded = max(downloaded, int(stripped.split("=", 1)[1]))
                except ValueError:
                    pass

            tail.append(line)
            if len(tail) > 400:
                del tail[:200]
            parsed = media.estimate_duration_from_progress(line)
            if parsed:
                kind, secs = parsed
                # 看门狗只看「推进了多少」——即使清单没给时长（total=0）也必须记账，
                # 否则健康下载会被误判成停滞。
                if kind == "time" and secs > 0:
                    monitor["media"] = max(monitor["media"], float(secs))
            if parsed and total > 0:
                kind, secs = parsed
                if kind == "time" and secs > 0:
                    now = time.time()
                    if now - last_notify >= 1.0:
                        last_notify = now
                        pct = 3.0 + min(80.0, secs / total * 80.0)
                        limit_note = f"，限速 {self.cfg.speed_limit_kib} KiB/s" if limit_bps else ""
                        self._notify(pct, f"拉流中 {secs:.0f}/{total:.0f}s{limit_note}")
            # 只做标记，不在回调里抛异常（异常会被 run_ffmpeg 的容错逻辑吞掉）
            if any(k in line.lower() for k in ("widevine", "playready", "fairplay", "sample-aes")):
                drm_hits.append(line.strip()[:200])

        exit_error = ""
        try:
            # check=True：ffmpeg 非 0 退出（被杀、输入中断）绝不能当成功 —— 缺陷 50 就是
            # 这样来的：被强杀的 ffmpeg 留下 1900s/3301s 的半份音频，旧代码照样
            # 记「音频完成」并继续转写。下面会先看产物是否完整，再决定是否放行。
            text = media.run_ffmpeg(
                cmd, on_stderr_line=on_line, timeout=None, check=True, watchdog=watchdog
            )
        except StreamStalledError as exc:
            # 停滞：保留已抓到的部分作为断点基准，让上层重试（缺陷 49）
            self._keep_as_partial(part, media.duration_of(part), total, base=target)
            part.unlink(missing_ok=True)
            raise
        except MediaError as exc:
            # 退出码非 0 / 看门狗之外的失败：先不判死，看产物是否完整（下面统一裁决）
            exit_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            part.unlink(missing_ok=True)
            raise MediaError(f"ffmpeg 执行失败：{exc}") from exc

        if drm_hits:
            part.unlink(missing_ok=True)
            raise DrmDetectedError(
                "检测到 DRM 保护标记，已停止处理该资源（本项目不做 DRM 绕过）。"
                f"\n证据：{'; '.join(drm_hits[:3])}"
            )

        if not part.is_file() or part.stat().st_size < 1024:
            part.unlink(missing_ok=True)
            detail = "\n".join(tail[-25:])
            low = detail.lower()
            if any(k in low for k in ("widevine", "playready", "fairplay", "sample-aes", "drm")):
                raise DrmDetectedError(f"疑似 DRM 保护，已停止。ffmpeg 输出：{detail[-800:]}")
            if any(k in low for k in ("401", "403", "unauthorized", "forbidden",
                                      "auth_key", "signature", "expired", "过期")):
                # 播放地址是**带时限签名**的（auth_key=…）。重试同一个死 URL 毫无意义，
                # 抛专用异常让 pipeline 去**重新解析**一个新鲜地址（缺陷 47）。
                raise PlayUrlExpiredError(
                    "拉流被拒绝（401/403 或签名过期）：播放地址已失效，需要重新获取。"
                    f"\nffmpeg 输出：{detail[-800:]}"
                )
            if "404" in low or "not found" in low:
                raise PlayUrlExpiredError(
                    f"播放地址 404，可能已失效（需要重新获取）。\nffmpeg 输出：{detail[-800:]}"
                )
            raise MediaError(
                f"ffmpeg 未产出有效音频文件。{('（' + exit_error[-300:] + '）') if exit_error else ''}"
                f"\nffmpeg 输出：{detail[-800:]}"
            )

        duration = media.duration_of(part)
        if duration <= 0 and total > 0:
            duration = total  # ffprobe 解不出来时用清单时长兜底

        # 3) 截断裁决（缺陷 50）：**明显短于清单标注**就是残件，绝不是「完成」
        if total > 0 and 0 < duration:
            shortfall = (total - duration) / total
            tol = float(getattr(self.cfg, "download_truncate_tolerance", 0.05) or 0.05)
            if shortfall > tol:
                self._keep_as_partial(part, duration, total, base=target)
                part.unlink(missing_ok=True)
                raise StreamTruncatedError(
                    f"音频被截断：只抓到 {duration:.0f}s / 清单 {total:.0f}s"
                    f"（缺 {shortfall * 100:.0f}%，超过 {tol * 100:.0f}% 容忍度）。"
                    "已保留半份音频作为断点，将重试补全；若反复失败，"
                    "通常是网络中途断开或服务端限流，可稍后在「任务」页点「重试」。"
                    + (f"\nffmpeg 报错：{exit_error[-300:]}" if exit_error else "")
                )

        part.replace(target)
        if total > 0 and duration > 0:
            err = abs(duration - total) / total
            if err > 0.02:
                warnings.append(
                    f"音频时长 {duration:.1f}s 与清单标注 {total:.1f}s 偏差 {err * 100:.1f}%（>2%）"
                )
                log.warning("时长偏差较大：%s", warnings[-1])
        if exit_error:
            # 产物完整 + 非 0 退出：多半是收尾时的可忽略告警，如实记一笔但不判失败
            warnings.append(f"ffmpeg 退出码非 0（产物按清单完整，已按完整处理）：{exit_error[:200]}")
            log.warning("ffmpeg 非 0 退出但产物完整：%s", exit_error[:200])

        sha = media.sha256_file(target)
        level = self._apply_level_policy(target, warnings)
        if level.get("changed"):
            sha = media.sha256_file(target)

        self._notify(85.0, f"音频就绪：{target.name}（{duration:.0f}s, {_mb(target.stat().st_size)}）")
        log.info(
            "音频完成：%s 时长=%.1fs 大小=%s sha256=%s 用时=%.1fs",
            target.name, duration, _mb(target.stat().st_size), sha[:16], time.time() - started,
        )
        return AudioResult(
            path=target,
            sha256=sha,
            duration_sec=duration,
            from_cache=False,
            source_url=url,
            bytes=target.stat().st_size,
            warnings=warnings,
            level=level,
        )

    # ------------------------------------------------------------------ #
    def _apply_level_policy(self, target: Path, warnings: list[str]) -> dict[str, Any]:
        """量电平：过低就自动增益；近乎无声则**如实告警**（缺陷 42）。

        实测背景：学校部分录播的课堂音轨电平只有 -57 dB（正常语音 -20~-30 dB），
        而本地 ASR 用 Silero VAD 会把这种音频整段判成「无语音」返回空文本 ——
        用户看到的却是「ASR 返回了空结果，可能音频无声」这种含糊提示。

        这里做两件事：
        1. 电平偏低 → 高通 + 固定增益 + 限幅（实测能救回可识别内容）；
        2. 增益后仍然近乎无声 → 明确告诉用户「是录像本身没录到声音」，
           并区分于本工具故障（不伪造转写内容）。
        """
        if not bool(getattr(self.cfg, "asr_auto_gain", True)):
            return {}
        try:
            info = media.normalize_for_asr(
                target,
                fmt=self.cfg.audio_format,
                bitrate=self.cfg.audio_bitrate,
                sample_rate=self.cfg.audio_sample_rate,
                channels=self.cfg.audio_channels,
            )
        except Exception as exc:  # noqa: BLE001 — 电平处理失败不影响主流程
            log.warning("电平检测/增益失败（继续）：%s", exc)
            return {}
        if info.get("changed"):
            self._notify(86.0, f"音频电平偏低，已自动增益：{info.get('reason')}")
            log.info("已自动增益：%s（%s）", target.name, info.get("reason"))
            warnings.append(f"原始音频电平偏低，已自动增益（{info.get('reason')}）")
        peak = (info.get("after") or info.get("before") or {}).get("max_db")
        if peak is not None and peak < media.SILENT_PEAK_DB:
            warnings.append(
                f"该录像音轨几乎没有声音（实测峰值 {peak:.1f} dB）："
                "很可能是学校录制设备/麦克风的问题，不是本工具故障；"
                "建议在平台上直接试听这条录播确认"
            )
            log.warning("音轨近乎无声：%s 峰值=%.1f dB", target.name, peak)
        return info

    # ------------------------------------------------------------------ #
    def _precheck_hls(self, url: str, warnings: list[str]) -> None:
        """预检 m3u8：DRM 检测 + 加密方式提示（AES-128 属正常，会被 ffmpeg 透明解密）。"""
        headers = {
            "User-Agent": self.cfg.user_agent,
            "Referer": self.cfg.portal_url,
            "Accept": "*/*",
        }
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        owned = self._client is None
        client = self._client or httpx.Client(
            timeout=20.0, follow_redirects=True, verify=self.cfg.verify_tls,
            trust_env=not self.cfg.proxy,
            proxy=self.cfg.proxy or None,
        )
        try:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            text = resp.text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                from .errors import AuthExpiredError

                raise AuthExpiredError(
                    f"请求 m3u8 被拒绝（HTTP {exc.response.status_code}），登录态或播放地址已失效"
                ) from exc
            raise MediaError(f"无法获取 m3u8：{exc}") from exc
        except httpx.HTTPError as exc:
            raise MediaError(f"无法获取 m3u8：{exc}") from exc
        finally:
            if owned:
                client.close()

        media.detect_drm_in_text(text, source=url)
        if "EXT-X-KEY" in text:
            if "METHOD=AES-128" in text:
                warnings.append("HLS 使用 AES-128 加密分片，将由 ffmpeg 透明解密（带同一份 Cookie/Header 请求 key）")
            elif "METHOD=NONE" not in text:
                methods = set(re.findall(r"METHOD=([A-Z0-9\-]+)", text))
                warnings.append(f"HLS 加密方式：{', '.join(sorted(methods))}")
        # 相对路径还原检查（仅供日志）
        for line in text.splitlines():
            if line and not line.startswith("#"):
                absu = urljoin(url, line.strip())
                log.log(5, "分片基准：%s → %s", line.strip()[:60], redact(absu)[:120])
                break

    # ------------------------------------------------------------------ #
    def fetch_many(
        self,
        items: list[tuple[Resource, str]],
        *,
        output_dirs: dict[str, Path] | None = None,
    ) -> dict[str, AudioResult]:
        """并发获取多条音频（并发上限由 ``cfg.concurrency`` 控制，默认 2）。"""
        results: dict[str, AudioResult] = {}
        errors: dict[str, Exception] = {}
        workers = max(1, min(2, int(self.cfg.concurrency)))  # 硬上限 2，避免风控

        def job(res: Resource, url: str) -> None:
            try:
                results[res.unique_key] = self.fetch(
                    res, play_url=url, output_dir=(output_dirs or {}).get(res.unique_key)
                )
            except Exception as exc:  # noqa: BLE001
                errors[res.unique_key] = exc
                log.error("《%s》音频获取失败：%s", res.title, exc)

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dl") as pool:
            futures = [pool.submit(job, res, url) for res, url in items]
            for fut in futures:
                fut.result()
        if errors and not results:
            first = next(iter(errors.values()))
            raise first
        return results


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.2f} MB"
