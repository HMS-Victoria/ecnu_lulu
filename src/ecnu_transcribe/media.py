"""ffmpeg / ffprobe 定位与媒体工具。

定位顺序（可被配置项 ``ffmpeg_path`` 覆盖）：
    1. 配置里的显式路径
    2. 环境变量 ``ECNU_FFMPEG``
    3. 随包携带（``assets/ffmpeg.exe``、``_MEIPASS/ffmpeg.exe``、exe 同目录）
    4. 系统 PATH 上的 ``ffmpeg``
    5. ``imageio-ffmpeg`` 自带的二进制（兜底，保证「装了 pip 依赖就能跑」）

所有下载 / 切片 / 转码都必须走 ffmpeg —— 本模块是**唯一**的 ffmpeg 调用出口。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .errors import DrmDetectedError, FfmpegMissingError, MediaError, StreamStalledError
from .logbus import get_logger

log = get_logger("media")

#: 创建子进程时不弹黑窗（打包成 GUI exe 后必须）
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_ffmpeg_lock = threading.Lock()
_ffmpeg_cache: Path | None = None
_probe_cache: Path | None = None


# --------------------------------------------------------------------------- #
# 定位
# --------------------------------------------------------------------------- #
def _which(name: str) -> Path | None:
    p = shutil.which(name)
    return Path(p) if p else None


def _imageio_ffmpeg() -> Path | None:
    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return Path(exe) if exe else None
    except Exception:
        return None


def find_ffmpeg(explicit: str = "") -> Path:
    """返回可用的 ffmpeg 路径，找不到抛 :class:`FfmpegMissingError`。"""
    global _ffmpeg_cache
    with _ffmpeg_lock:
        candidates: list[Path | None] = []
        if explicit:
            candidates.append(Path(explicit))
        env = os.environ.get("ECNU_FFMPEG")
        if env:
            candidates.append(Path(env))
        candidates.append(paths.bundled_ffmpeg())
        candidates.append(_which("ffmpeg"))
        candidates.append(_imageio_ffmpeg())
        for cand in candidates:
            if not cand:
                continue
            try:
                if cand.is_file():
                    _ffmpeg_cache = cand
                    return cand
            except OSError:
                continue
        raise FfmpegMissingError(
            "找不到 ffmpeg。请执行 `winget install Gyan.FFmpeg`，"
            "或在设置页手动指定 ffmpeg.exe 路径。"
        )


def find_ffprobe(ffmpeg_path: Path | None = None) -> Path | None:
    """优先找同目录的 ffprobe；找不到返回 None（改用 ffmpeg -i 解析）。"""
    global _probe_cache
    if _probe_cache and _probe_cache.is_file():
        return _probe_cache
    cands: list[Path | None] = []
    if ffmpeg_path:
        cands.append(ffmpeg_path.with_name("ffprobe.exe" if sys.platform == "win32" else "ffprobe"))
    cands.append(_which("ffprobe"))
    for cand in cands:
        if cand and cand.is_file():
            _probe_cache = cand
            return cand
    return None


def ffmpeg_version(ffmpeg_path: Path | None = None) -> str:
    exe = ffmpeg_path or find_ffmpeg()
    out = run_ffmpeg([str(exe), "-version"], timeout=20)
    return (out or "").splitlines()[0] if out else "(unknown)"


# --------------------------------------------------------------------------- #
# 子进程封装
# --------------------------------------------------------------------------- #
def run_ffmpeg(
    cmd: list[str],
    *,
    timeout: float | None = None,
    check: bool = False,
    on_stderr_line=None,
    watchdog=None,
    watchdog_interval: float = 2.0,
) -> str:
    """执行 ffmpeg/ffprobe 命令，返回 stderr 文本。

    ffmpeg 把进度与错误都写在 stderr，因此这里统一返回 stderr；
    ``on_stderr_line`` 可用于解析 ``-progress`` 或 ``time=`` 输出做进度回调。

    ``watchdog``：可选的「停滞看门狗」回调，每 ``watchdog_interval`` 秒在
    独立线程里调用一次；返回非空字符串表示要中止（字符串作为原因），
    此时本函数会杀掉子进程并抛出 :class:`StreamStalledError`。

    为什么需要它：网络流挂住时 ffmpeg **不会自己退出**（它安静地等对端），
    而主线程正阻塞在 ``proc.stdout`` 的读循环上，任何写在回调里的检查都
    不会被触发 —— 所以必须有一个**独立线程**来看门（缺陷 49）。
    """
    log.log(5, "exec: %s", " ".join(_mask_cmd(cmd)))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise FfmpegMissingError(f"无法启动 ffmpeg：{cmd[0]}") from exc

    lines: list[str] = []
    abort: dict[str, str] = {}
    stop_watch = threading.Event()

    def _watch() -> None:
        while not stop_watch.wait(watchdog_interval):
            if proc.poll() is not None:
                return
            try:
                reason = watchdog()
            except Exception as exc:  # noqa: BLE001 — 看门狗自身出错不能影响主流程
                log.warning("看门狗回调异常（忽略）：%s", exc)
                continue
            if reason:
                abort["reason"] = str(reason)
                log.warning("看门狗中止拉流：%s", reason)
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                return

    watcher: threading.Thread | None = None
    if watchdog is not None:
        watcher = threading.Thread(target=_watch, name="ffmpeg-watchdog", daemon=True)
        watcher.start()

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            lines.append(line)
            if on_stderr_line:
                try:
                    on_stderr_line(line)
                except Exception:
                    pass
        proc.wait(timeout=timeout if timeout else None)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
        raise MediaError(f"ffmpeg 超时（{timeout}s）：{cmd[0]}")
    finally:
        stop_watch.set()
        if watcher is not None:
            watcher.join(timeout=watchdog_interval + 1.0)
        if proc.poll() is None:
            proc.kill()
    out = "\n".join(lines)
    if abort:
        raise StreamStalledError(abort["reason"])
    if check and proc.returncode != 0:
        raise MediaError(f"ffmpeg 退出码 {proc.returncode}：{out[-1500:]}")
    return out


def _mask_cmd(cmd: list[str]) -> list[str]:
    """日志中的命令行脱敏：Cookie / Token / Header 值一律遮掉。"""
    masked: list[str] = []
    secretish = ("cookie", "authorization", "token", "key", "secret", "signature")
    for i, part in enumerate(cmd):
        low = part.lower()
        if any(s in low for s in secretish) and "=" in part and not part.startswith("-"):
            masked.append(part.split("=", 1)[0] + "=<REDACTED>")
        elif i > 0 and cmd[i - 1].lower().rstrip(":") in {"-headers", "-cookie", "-authorization"}:
            masked.append("<REDACTED>")
        elif "://" in part and "?" in part:
            masked.append(part.split("?", 1)[0] + "?<REDACTED>")
        else:
            masked.append(part)
    return masked


# --------------------------------------------------------------------------- #
# ffmpeg 网络输入参数
# --------------------------------------------------------------------------- #
def build_input_args(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    cookie: str = "",
    referer: str = "",
    user_agent: str = "",
    speed_limit_kib: int = 0,
    reconnect: bool = True,
) -> list[str]:
    """构造 ``-i`` 之前的输入侧参数（HLS 分片与 AES-128 key 都会带上同样的 Header）。"""
    args: list[str] = []
    hdrs: dict[str, str] = dict(headers or {})
    if user_agent:
        hdrs.setdefault("User-Agent", user_agent)
    if referer:
        hdrs.setdefault("Referer", referer)
    if cookie:
        hdrs.setdefault("Cookie", cookie)
    if hdrs:
        blob = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
        args += ["-headers", blob]
    if reconnect:
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
        ]
    args += ["-rw_timeout", "30000000"]
    # 注意：**不要**在这里放 ``-maxrate``。
    # ffmpeg 的 ``-maxrate`` 是**输出侧编码**选项（质量控制的 VBV 参数），
    # 放到 ``-i`` 前面会被当成输入/解码选项直接报错：
    #   Codec AVOption maxrate ... is not a decoding option
    #   Error opening input files: Invalid argument
    # 即「只要用户设了限速，所有下载必然失败」。限速改由 downloader 侧按速率节流实现。
    _ = speed_limit_kib  # 保留签名（调用方仍在传），实际节流见 downloader.AudioDownloader
    return args


def muxer_for(fmt: str) -> str:
    """音频格式 → ffmpeg 封装名。

    必须显式给 ``-f``：中间产物叫 ``x.mp3.part`` / ``x.mp3.rest``，
    ffmpeg 从扩展名推断不出封装格式，会直接报
    ``Unable to choose an output format``（缺陷 50 的续传拼接踩过）。
    """
    return {"mp3": "mp3", "wav": "wav", "m4a": "ipod", "flac": "flac", "opus": "ogg"}.get(
        (fmt or "mp3").lower(), "mp3"
    )


def build_output_args(
    *,
    fmt: str = "mp3",
    bitrate: str = "64k",
    sample_rate: int = 16000,
    channels: int = 1,
    overwrite: bool = True,
) -> list[str]:
    """只留音频的输出参数：``-vn -ac 1 -ar 16000 -c:a libmp3lame -b:a 64k``。"""
    args = ["-y" if overwrite else "-n", "-vn", "-sn", "-dn"]
    args += ["-ac", str(int(channels)), "-ar", str(int(sample_rate))]
    codec_by_fmt = {
        "mp3": ["-c:a", "libmp3lame", "-b:a", bitrate],
        "wav": ["-c:a", "pcm_s16le"],
        "m4a": ["-c:a", "aac", "-b:a", bitrate],
        "flac": ["-c:a", "flac"],
        "opus": ["-c:a", "libopus", "-b:a", bitrate],
    }
    args += codec_by_fmt.get(fmt, codec_by_fmt["mp3"])
    if fmt in ("mp3", "m4a"):
        args += ["-map_metadata", "-1"]
    args += ["-f", muxer_for(fmt)]
    return args


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
@dataclass
class MediaInfo:
    duration_sec: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    format_name: str = ""
    bit_rate: int = 0
    size_bytes: int = 0
    raw: dict | None = None


def probe(path_or_url: str, *, input_args: list[str] | None = None, timeout: float = 90) -> MediaInfo:
    """用 ffprobe（没有就用 ``ffmpeg -i``）探测媒体信息。"""
    ffmpeg_exe = find_ffmpeg()
    ffprobe = find_ffprobe(ffmpeg_exe)
    if ffprobe:
        cmd = [str(ffprobe), "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams"]
        cmd += input_args or []
        cmd += [str(path_or_url)]
        out = run_ffmpeg(cmd, timeout=timeout)
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = {}
        streams = data.get("streams", [])
        fmt = data.get("format", {})
        info = MediaInfo(
            duration_sec=float(fmt.get("duration") or 0.0),
            format_name=str(fmt.get("format_name") or ""),
            bit_rate=int(float(fmt.get("bit_rate") or 0)),
            size_bytes=int(float(fmt.get("size") or 0)),
            raw=data,
        )
        info.has_video = any(s.get("codec_type") == "video" for s in streams)
        info.has_audio = any(s.get("codec_type") == "audio" for s in streams)
        if not info.duration_sec:
            for s in streams:
                if s.get("duration"):
                    info.duration_sec = max(info.duration_sec, float(s["duration"]))
        return info

    # 退化路径：解析 `ffmpeg -i` 的 stderr
    cmd = [str(ffmpeg_exe), "-hide_banner"]
    cmd += input_args or []
    cmd += ["-i", str(path_or_url)]
    text = run_ffmpeg(cmd, timeout=timeout)
    info = MediaInfo(raw={"stderr": text})
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info.duration_sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    info.has_video = bool(re.search(r"Stream #\d+:\d+.*: Video:", text))
    info.has_audio = bool(re.search(r"Stream #\d+:\d+.*: Audio:", text))
    return info


def duration_of(path: Path, *, timeout: float = 60) -> float:
    try:
        return probe(str(path), timeout=timeout).duration_sec
    except Exception as exc:
        log.debug("探测时长失败 %s: %s", path, exc)
        return 0.0


# --------------------------------------------------------------------------- #
# DRM 检测（检测到即停止，不做任何绕过）
# --------------------------------------------------------------------------- #
_DRM_MARKERS = (
    b"widevine",
    b"playready",
    b"fairplay",
    b"com.widevine.alpha",
    b"com.microsoft.playready",
    b"com.apple.fps",
    b"urn:uuid:edef8ba9",  # Widevine system ID
    b"urn:uuid:9a04f079",  # PlayReady system ID
    # SAMPLE-AES 在 HLS 中用于 DRM 保护
    b"METHOD=SAMPLE-AES",
    b"METHOD=SAMPLE-AES-CTR",
)


def detect_drm_in_text(text: str, *, source: str = "") -> None:
    """文本层面检测 DRM 标记；命中抛 :class:`DrmDetectedError`。"""
    low = text.lower()
    for marker in _DRM_MARKERS:
        if marker.lower().decode("ascii", "ignore") in low:
            raise DrmDetectedError(
                f"检测到 DRM 保护（{marker.decode('ascii', 'ignore')}）"
                f"{' @ ' + source if source else ''}。本项目不做 DRM 绕过，已停止处理该资源。"
            )


def detect_drm_in_file(path: Path, *, sniff_bytes: int = 1_000_000) -> None:
    """文件层面粗筛 DRM 标记（扫描头部若干字节，避免读整个大文件）。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(sniff_bytes)
    except OSError:
        return
    detect_drm_in_text(head.decode("utf-8", "ignore"), source=str(path))


# --------------------------------------------------------------------------- #
# 静音切分（配合 ASR 端点的体积/时长上限）
# --------------------------------------------------------------------------- #
@dataclass
class SilenceSpan:
    start: float
    end: float


def detect_silences(
    audio_path: Path,
    *,
    noise_db: float = -32.0,
    min_silence_sec: float = 0.6,
    timeout: float = 1800,
) -> list[SilenceSpan]:
    """用 ``silencedetect`` 找出静音区间（用于「按静音切分」）。"""
    exe = find_ffmpeg()
    cmd = [
        str(exe), "-hide_banner", "-nostdin", "-i", str(audio_path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_sec}",
        "-f", "null", "-",
    ]
    text = run_ffmpeg(cmd, timeout=timeout)
    spans: list[SilenceSpan] = []
    start: float | None = None
    for line in text.splitlines():
        m = re.search(r"silence_start:\s*(-?\d+(?:\.\d+)?)", line)
        if m:
            start = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*(-?\d+(?:\.\d+)?)", line)
        if m and start is not None:
            spans.append(SilenceSpan(start=max(0.0, start), end=float(m.group(1))))
            start = None
    return spans


def extract_segment(
    src: Path,
    dst: Path,
    start: float,
    end: float,
    *,
    fmt: str = "wav",
    sample_rate: int = 16000,
    channels: int = 1,
    bitrate: str = "64k",
    timeout: float = 1800,
) -> Path:
    """从本地音频里切一段（``-ss`` 放在 ``-i`` 前，快速定位）。"""
    exe = find_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(exe), "-hide_banner", "-nostdin", "-ss", f"{start:.3f}", "-i", str(src),
           "-t", f"{max(0.05, end - start):.3f}"]
    cmd += build_output_args(fmt=fmt, bitrate=bitrate, sample_rate=sample_rate, channels=channels)
    cmd += [str(dst)]
    run_ffmpeg(cmd, timeout=timeout, check=True)
    return dst


def convert_audio(
    src: Path,
    dst: Path,
    *,
    fmt: str = "mp3",
    bitrate: str = "64k",
    sample_rate: int = 16000,
    channels: int = 1,
    timeout: float = 1800,
) -> Path:
    """本地音频格式转换（例如 ASR 端点只吃 mp3/wav 时）。"""
    exe = find_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(exe), "-hide_banner", "-nostdin", "-i", str(src)]
    cmd += build_output_args(fmt=fmt, bitrate=bitrate, sample_rate=sample_rate, channels=channels)
    cmd += [str(dst)]
    run_ffmpeg(cmd, timeout=timeout, check=True)
    return dst


# --------------------------------------------------------------------------- #
# 电平检测与增益（缺陷 42）
# --------------------------------------------------------------------------- #
#: 峰值低于这个值（dBFS）就认为「几乎没有声音」。
SILENT_PEAK_DB = -50.0
#: **判断「有没有语音」要看平均电平，不能看峰值**（实测教训）：
#: 那条没录到声音的线性代数录像，整段峰值有 -23.8 dB（某处有个响声/咔哒），
#: 但 mean 只有 -54.5 dB —— 用峰值判断会漏掉它。
#: 实测对照：有语音的录像 mean 在 -13 ~ -19 dB；没语音的在 -54 ~ -80 dB。
#: 门槛取 -45 dB 正好落在两簇中间。
SILENT_MEAN_DB = -45.0
#: 自动增益的目标峰值（dBFS）
TARGET_PEAK_DB = -3.0
#: 触发自动增益的门槛：峰值低于此值才动手。
#: 定在 -30 dB 而不是「目标值附近」是有意的 —— 实测语音峰值 -18 dB 也能正常识别，
#: 没必要为了好看去重编码一遍（多一次编解码就多一次质量损失）。
AUTO_GAIN_TRIGGER_DB = -30.0
#: 自动增益上限（dB）。避免把纯底噪放大成「像语音」的东西。
MAX_AUTO_GAIN_DB = 40.0


def probe_volume(path: Path, *, timeout: float = 600) -> dict[str, float]:
    """用 ffmpeg 的 ``volumedetect`` 量一段音频的电平。

    返回 ``{"mean_db": …, "max_db": …}``；量不到就返回空字典（不抛异常）。
    实测价值：学校有的录播音轨电平只有 **-57 dB**（正常语音约 -20~-30 dB），
    ASR 会直接判成静音并返回空文本 —— 必须在送 ASR 之前就知道这件事。
    """
    exe = find_ffmpeg()
    cmd = [str(exe), "-hide_banner", "-nostdin", "-i", str(path),
           "-af", "volumedetect", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("音量检测失败：%s", exc)
        return {}
    found: dict[str, float] = {}
    for line in (proc.stderr or "").splitlines():
        for key, name in (("mean_volume:", "mean_db"), ("max_volume:", "max_db")):
            if key in line:
                try:
                    found[name] = float(line.split(key, 1)[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass
    return found


def normalize_for_asr(
    path: Path,
    *,
    fmt: str = "mp3",
    bitrate: str = "64k",
    sample_rate: int = 16000,
    channels: int = 1,
    target_peak_db: float = TARGET_PEAK_DB,
    max_gain_db: float = MAX_AUTO_GAIN_DB,
    trigger_db: float = AUTO_GAIN_TRIGGER_DB,
    timeout: float = 3600,
) -> dict[str, Any]:
    """把电平过低的音频抬到可识别范围（高通去低频噪声 + fixed gain + 限幅）。

    返回信息字典（含 ``gain_db`` / 前后电平 / ``silent``），便于写日志与测试：
        {"changed": bool, "gain_db": float, "before": {...}, "after": {...},
         "reason": str, "silent": bool}

    为什么要 fixed gain 而不是 ``loudnorm``/``dynaudnorm``：**实测**（见 PROGRESS §1.65.7）
    对同一段真实课堂音频，``volume=+28dB`` 能转出文字，而 loudnorm / dynaudnorm
    反而让 VAD 判成无语音（0 字）。固定增益最简单、最可预测。

    **近乎无声时不放大**（``silent=True``）：实测把 -67 dB 的底噪硬抬 40 dB 之后，
    Whisper 会在「无语音」的输入上**编出**「字幕by索兰娅」这类模板文本 ——
    那比诚实失败更糟（用户会拿到一份看起来像转写、其实是幻觉的产物）。
    所以峰值低于 ``SILENT_PEAK_DB`` 时只标记、不放大，交由上层明确报错。
    """
    info: dict[str, Any] = {"changed": False, "gain_db": 0.0, "reason": "", "silent": False}
    before = probe_volume(path, timeout=min(timeout, 900))
    info["before"] = before
    peak = before.get("max_db")
    mean = before.get("mean_db")
    if peak is None and mean is None:
        info["reason"] = "无法测量电平"
        return info
    # 有没有语音看 **mean**；峰值只用来决定要不要增益（峰值会被单个响声带偏）
    if (mean is not None and mean < SILENT_MEAN_DB) or (peak is not None and peak < SILENT_PEAK_DB):
        info.update(
            silent=True,
            after=before,
            reason=(
                f"音轨几乎无声（平均 {mean if mean is None else round(mean, 1)} dB / "
                f"峰值 {peak if peak is None else round(peak, 1)} dB）：不放大 —— "
                "放大底噪会让 ASR 编造出不存在的文字"
            ),
        )
        return info
    if peak is None or peak >= trigger_db:
        info["reason"] = (
            f"电平够用（峰值 {peak} dB，未达增益门槛 {trigger_db:.0f} dB）" if peak is not None
            else "无法测量峰值，跳过增益"
        )
        info["after"] = before
        return info

    gain = min(max_gain_db, target_peak_db - peak)
    if gain <= 1.0:
        info["reason"] = "无需增益"
        info["after"] = before
        return info

    tmp = path.with_suffix(path.suffix + ".gain.tmp" + (f".{fmt}" if not path.suffix.endswith(fmt) else ""))
    exe = find_ffmpeg()
    chain = f"highpass=f=80,volume={gain:.1f}dB,alimiter=limit=0.95"
    cmd = [str(exe), "-hide_banner", "-nostdin", "-y", "-i", str(path), "-af", chain]
    cmd += build_output_args(fmt=fmt, bitrate=bitrate, sample_rate=sample_rate, channels=channels)
    cmd += [str(tmp)]
    try:
        run_ffmpeg(cmd, timeout=timeout, check=True)
    except Exception as exc:  # noqa: BLE001
        info["reason"] = f"增益失败：{type(exc).__name__}: {exc}"
        tmp.unlink(missing_ok=True)
        return info
    if not tmp.is_file() or tmp.stat().st_size == 0:
        info["reason"] = "增益未产出文件"
        tmp.unlink(missing_ok=True)
        return info
    tmp.replace(path)
    after = probe_volume(path, timeout=min(timeout, 900))
    info.update(changed=True, gain_db=gain, after=after,
                reason=f"峰值 {peak:.1f} → {after.get('max_db', float('nan')):.1f} dB（+{gain:.0f} dB）")
    return info


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #
def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def estimate_duration_from_progress(line: str) -> tuple[str, float] | None:
    """解析 ``-progress`` 输出的 ``out_time_ms=`` 行，供进度回调使用。"""
    if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
        raw = line.split("=", 1)[1].strip()
        try:
            return "time", float(raw) / 1_000_000.0
        except ValueError:
            return None
    if line.startswith("progress="):
        return "done" if line.strip().endswith("end") else "running", 0.0
    m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if m:
        secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        return "time", secs
    return None


def human_duration(seconds: float) -> str:
    """``3725`` → ``01:02:05``。"""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
