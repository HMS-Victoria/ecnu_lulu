"""下载器分支测试（`media.build_input_args` / `AudioDownloader`）。

这块此前几乎没有测试，而它是「能不能拿到音频」的唯一路径。本轮重点：

    1. **限速选项必须能真的下载** —— 回归：``-maxrate`` 被放进输入侧参数，
       而它是**输出侧编码**选项，ffmpeg 直接报
       ``Codec AVOption maxrate ... is not a decoding option`` /
       ``Error opening input files``：**只要用户设了限速，所有下载必然失败**。
    2. 限速要**真的变慢**（不要求字节级精确，但不能是装饰）。
    3. 播放地址里的**时效签名参数**不能被丢掉。
    4. Cookie / Referer / UA 要传进 ffmpeg。
    5. 本地文件走纯转码路径（不带网络参数）。
"""

from __future__ import annotations

import http.server
import shutil
import socketserver
import threading
import time
from pathlib import Path

import pytest

from ecnu_transcribe import media
from ecnu_transcribe.catalog import Resource
from ecnu_transcribe.config import AppConfig
from ecnu_transcribe.downloader import AudioDownloader
from ecnu_transcribe.errors import MediaError

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 1. 参数构造（纯逻辑，快）
# --------------------------------------------------------------------------- #
def test_input_args_never_contain_maxrate():
    """回归：输入侧绝不能出现 -maxrate（会让 ffmpeg 直接拒绝打开输入）。"""
    for limit in (0, 32, 64, 512, 4096):
        args = media.build_input_args("http://x/y.m3u8", speed_limit_kib=limit)
        assert "-maxrate" not in args, f"speed_limit_kib={limit} 时仍塞了 -maxrate：{args}"


def test_input_args_carry_headers_cookie_referer_ua():
    args = media.build_input_args(
        "https://x/y.m3u8",
        cookie="JSESSIONID=abc; OTHER=1",
        referer="https://portal/#/home",
        user_agent="UA-Test/1.0",
    )
    blob = args[args.index("-headers") + 1]
    assert "Cookie: JSESSIONID=abc; OTHER=1" in blob
    assert "Referer: https://portal/#/home" in blob
    assert "User-Agent: UA-Test/1.0" in blob
    assert "-reconnect" in args


def test_output_args_audio_only_mp3():
    args = media.build_output_args(fmt="mp3", bitrate="64k", sample_rate=16000, channels=1)
    assert "-vn" in args and "-sn" in args and "-dn" in args
    assert args[args.index("-c:a") + 1] == "libmp3lame"
    assert args[args.index("-b:a") + 1] == "64k"
    assert args[args.index("-ar") + 1] == "16000"
    assert args[args.index("-ac") + 1] == "1"


# --------------------------------------------------------------------------- #
# 素材：本地 HLS（含播放列表 + 分片）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def hls_server(tmp_path_factory):
    """造一份本地 HLS 并用 HTTP 发出去（下载器的真实路径）。"""
    work = tmp_path_factory.mktemp("hls")
    exe = media.find_ffmpeg()
    src = work / "src.wav"
    media.run_ffmpeg(
        [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=90",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(src)],
        timeout=180, check=True,
    )
    hls = work / "hls"
    hls.mkdir()
    media.run_ffmpeg(
        [str(exe), "-hide_banner", "-nostdin", "-y", "-i", str(src),
         "-c:a", "aac", "-b:a", "128k", "-f", "hls", "-hls_time", "2",
         "-hls_list_size", "0", "-hls_playlist_type", "vod",
         "-hls_segment_filename", str(hls / "s-%03d.ts"), str(hls / "index.m3u8")],
        timeout=300, check=True,
    )
    size_kib = sum(p.stat().st_size for p in hls.glob("*.ts")) / 1024

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # noqa: A003
            pass

    httpd = socketserver.ThreadingTCPServer(
        ("127.0.0.1", 0), lambda *a, **kw: Handler(*a, directory=str(hls), **kw)
    )
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/index.m3u8"
    yield {"url": url, "dir": hls, "size_kib": size_kib, "port": httpd.server_address[1]}
    httpd.shutdown()


# --------------------------------------------------------------------------- #
# 2. 真实下载（含限速）
# --------------------------------------------------------------------------- #
def test_download_with_speed_limit_succeeds(hls_server, tmp_path):
    """回归：设了限速也必须能下载成功（旧代码会直接 4 次重试后失败）。"""
    cfg = AppConfig()
    cfg.speed_limit_kib = 256
    cfg.cache_enabled = False
    cfg.output_dir = str(tmp_path)
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="SPEED-OK", title="限速下载", course_name="t",
                   play_url=hls_server["url"])
    out = dl.fetch(res, play_url=hls_server["url"], force=True)
    assert out.path.is_file() and out.path.stat().st_size > 1024
    assert out.duration_sec > 10, out.duration_sec


def test_speed_limit_actually_slows_download(hls_server, tmp_path):
    """限速要真的变慢（不要求字节级精确，但不能是装饰）。"""
    def run(limit: int) -> float:
        cfg = AppConfig()
        cfg.speed_limit_kib = limit
        cfg.cache_enabled = False
        cfg.output_dir = str(tmp_path)
        dl = AudioDownloader(cfg)
        res = Resource(resource_id=f"SPD{limit}", title=f"spd{limit}", course_name="t",
                       play_url=hls_server["url"])
        t0 = time.time()
        dl.fetch(res, play_url=hls_server["url"], force=True)
        return time.time() - t0

    fast = run(0)
    slow = run(128)
    # 不低于 1.8 倍即认为节流生效（受 ffmpeg 缓冲/进度粒度影响，不做字节级断言）
    assert slow > max(fast * 1.8, fast + 1.0), f"限速没生效：不限速 {fast:.2f}s vs 限速 {slow:.2f}s"


def test_play_url_signature_survives(hls_server, tmp_path):
    """带时效签名的播放地址必须能正常拉取（query 不能被丢掉/截断）。"""
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = False
    dl = AudioDownloader(cfg)
    signed = f"{hls_server['url']}?sign=abc123&expires=1790000000"
    res = Resource(resource_id="SIGNED", title="签名", course_name="t", play_url=signed)
    out = dl.fetch(res, play_url=signed, force=True)
    assert out.path.is_file()
    assert "sign=abc123" in out.source_url


def test_404_on_playlist_gives_clear_error(hls_server, tmp_path):
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = False
    cfg.download_retries = 1
    dl = AudioDownloader(cfg)
    bad = f"http://127.0.0.1:{hls_server['port']}/nope.m3u8"
    res = Resource(resource_id="MISS", title="缺失", course_name="t", play_url=bad)
    with pytest.raises(MediaError) as exc:
        dl.fetch(res, play_url=bad, force=True)
    msg = str(exc.value)
    assert "404" in msg or "失效" in msg or "无法获取 m3u8" in msg, msg


def test_local_file_uses_pure_transcode_path(tmp_path):
    """本地文件当播放源：不能带网络参数（回归：曾导致 Option headers not found）。"""
    exe = media.find_ffmpeg()
    local = tmp_path / "local.wav"
    media.run_ffmpeg(
        [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=5", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(local)],
        timeout=60, check=True,
    )
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = False
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="LOCAL1", title="本地", course_name="t", play_url=str(local))
    out = dl.fetch(res, play_url=str(local), force=True)
    assert out.path.is_file()
    assert out.duration_sec == pytest.approx(5.0, abs=0.5)


def test_missing_local_file_reports_clearly(tmp_path):
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.download_retries = 1
    dl = AudioDownloader(cfg)
    missing = str(tmp_path / "not-here.wav")
    res = Resource(resource_id="NOLOCAL", title="缺", course_name="t", play_url=missing)
    with pytest.raises(MediaError) as exc:
        dl.fetch(res, play_url=missing, force=True)
    assert "不存在" in str(exc.value) or "失败" in str(exc.value)


# --------------------------------------------------------------------------- #
# 3. 缓存路径与命名
# --------------------------------------------------------------------------- #
def test_cache_path_is_stable_and_safe(hls_server, tmp_path):
    cfg = AppConfig()
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="R-1", title="第1讲 绪论/算法：复杂度?", course_name="c")
    p1 = dl.cache_path(res)
    p2 = dl.cache_path(res)
    assert p1 == p2, "同一资源必须得到同一缓存路径（断点续跑依赖它）"
    assert "/" not in p1.name and ":" not in p1.name and "?" not in p1.name, p1.name
    assert "R-1" in p1.name


def test_cache_hit_skips_download(hls_server, tmp_path):
    cfg = AppConfig()
    cfg.cache_enabled = True
    cfg.output_dir = str(tmp_path)
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="CACHE1", title="缓存命中", course_name="t",
                   play_url=hls_server["url"], duration_sec=90.0)
    first = dl.fetch(res, play_url=hls_server["url"], force=True)
    second = dl.fetch(res, play_url=hls_server["url"])
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.sha256 == first.sha256


# --------------------------------------------------------------------------- #
# 电平检测与自动增益（缺陷 42）
#
# 实测背景：学校部分录播的课堂音轨电平只有 -57 dB（正常语音 -20~-30 dB），
# 本地 ASR 的 Silero VAD 会把这种音频整段判成「无语音」→ 空转写。
# --------------------------------------------------------------------------- #
def _make_tone(path, *, gain_db: float, seconds: float = 3.0) -> None:
    """生成测试音频。

    注意：ffmpeg ``sine`` 源本身约 -18 dBFS 峰值，所以实际峰值 ≈ ``-18 + gain_db``。
    这个换算踩过一次坑（写测试时以为 ``volume=0dB`` 就是 0 dBFS）。
    """
    from ecnu_transcribe import media

    exe = media.find_ffmpeg()
    media.run_ffmpeg(
        [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-af", f"volume={gain_db}dB",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(path)],
        timeout=60, check=True,
    )


def test_volume_probe_and_gain_on_quiet_audio(tmp_path):
    from ecnu_transcribe import media

    quiet, mid = tmp_path / "quiet.wav", tmp_path / "mid.wav"
    _make_tone(quiet, gain_db=-16)   # 实际峰值 ≈ -34 dB、均值 ≈ -37 dB（安静但有内容）
    _make_tone(mid, gain_db=0)       # 实际峰值 ≈ -18 dB（实测这种电平能正常识别）

    before = media.probe_volume(quiet)
    assert before.get("max_db") is not None
    assert before["max_db"] < media.AUTO_GAIN_TRIGGER_DB, before

    info = media.normalize_for_asr(quiet, fmt="wav", bitrate="64k", channels=1)
    assert info["changed"] is True, info
    assert info["gain_db"] > 20, info
    assert info["after"]["max_db"] > before["max_db"] + 20, info
    assert info["after"]["max_db"] <= 1.0, f"不应削顶：{info}"

    # 够用的电平不做无谓的重编码（多一次编解码就多一次质量损失）
    ok = media.normalize_for_asr(mid, fmt="wav", channels=1)
    assert ok["changed"] is False, ok
    assert ok["reason"].startswith("电平够用"), ok


def test_gain_is_capped_for_near_silence(tmp_path):
    from ecnu_transcribe import media

    silence = tmp_path / "silence.wav"
    exe = media.find_ffmpeg()
    media.run_ffmpeg(
        [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono", "-t", "2", "-c:a", "pcm_s16le", str(silence)],
        timeout=60, check=True,
    )
    info = media.normalize_for_asr(silence, fmt="wav", channels=1)
    assert info["gain_db"] <= media.MAX_AUTO_GAIN_DB + 0.01, info


def test_silent_audio_is_detected_by_mean_not_peak(tmp_path):
    """**判据是平均电平，不是峰值**（这条教训来自真实录像）。

    实测：那条没录到声音的线性代数录像，整段峰值仍有 -23.8 dB（某处有个响声），
    但平均只有 -54.5 dB。若按峰值判断，就会把它当成「电平正常」送去 ASR，
    然后拿到空结果（甚至被编造出文本）。
    """
    from ecnu_transcribe import media

    noisy = tmp_path / "noise_only.wav"
    exe = media.find_ffmpeg()
    media.run_ffmpeg(
        [str(exe), "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", "anoisesrc=r=16000:c=pink:a=0.0008:d=5",
         "-c:a", "pcm_s16le", str(noisy)],
        timeout=60, check=True,
    )
    vol = media.probe_volume(noisy)
    assert vol.get("mean_db") is not None, vol
    info = media.normalize_for_asr(noisy, fmt="wav", channels=1)
    if (vol.get("mean_db") or 0) < media.SILENT_MEAN_DB:
        assert info["silent"] is True, (info, vol)
        assert info["changed"] is False, "判定无声时不应放大（会把底噪变成幻觉文本）"


def test_real_silent_recording_is_flagged_silent():
    """用**真实**素材回归：那条没录到声音的线性代数录像必须被判为 silent。

    素材在 `cache/media/`（真实拉流得到），不存在时跳过。
    """
    from pathlib import Path

    from ecnu_transcribe import media

    audio = next(
        (Path(__file__).resolve().parents[1] / "cache" / "media").glob("*VOD-701828*.mp3"), None
    )
    if audio is None or not audio.is_file():
        pytest.skip("没有真实素材（cache/media/*VOD-701828*.mp3）")
    vol = media.probe_volume(audio)
    assert (vol.get("mean_db") or 0) < media.SILENT_MEAN_DB, vol
    info = media.normalize_for_asr(audio, fmt="mp3", bitrate="64k")
    assert info["silent"] is True, info
    assert info["changed"] is False, info


def test_empty_transcript_message_blames_the_recording_when_silent(tmp_path):
    from ecnu_transcribe.pipeline import _empty_transcript_hint

    class _Silent:
        # 真实素材的样子：均值极低、峰值反而不低
        level = {"before": {"mean_db": -54.5, "max_db": -23.8},
                 "after": {"mean_db": -54.5, "max_db": -23.8}}

    msg = _empty_transcript_hint(tmp_path / "missing.mp3", _Silent())
    assert "原因不在你的配置" in msg, msg
    assert "录制设备" in msg or "麦克风" in msg, msg

    class _Loud:
        level = {"before": {"mean_db": -18.7, "max_db": -4.2},
                 "after": {"mean_db": -15.0, "max_db": -3.0}}

    msg2 = _empty_transcript_hint(tmp_path / "missing.mp3", _Loud())
    assert "电平正常" in msg2, msg2
    assert "录制设备" not in msg2, msg2
    assert "录制设备" not in msg2, msg2


def test_level_policy_can_be_disabled(tmp_path):
    cfg = AppConfig()
    cfg.asr_auto_gain = False
    dl = AudioDownloader(cfg)
    quiet = tmp_path / "q.wav"
    _make_tone(quiet, gain_db=-46, seconds=2.0)
    size_before = quiet.stat().st_size
    warnings: list[str] = []
    assert dl._apply_level_policy(quiet, warnings) == {}  # noqa: SLF001
    assert quiet.stat().st_size == size_before
    assert warnings == []


def test_level_policy_warns_on_near_silent_recording(tmp_path):
    """近乎无声的录像要留下**如实**告警（是录像的问题，不是工具的问题）。"""
    from ecnu_transcribe import media

    cfg = AppConfig()
    cfg.audio_format = "wav"
    dl = AudioDownloader(cfg)
    silence = tmp_path / "silence.wav"
    exe = media.find_ffmpeg()
    media.run_ffmpeg(
        [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono", "-t", "2", "-c:a", "pcm_s16le", str(silence)],
        timeout=60, check=True,
    )
    warnings: list[str] = []
    info = dl._apply_level_policy(silence, warnings)  # noqa: SLF001
    assert info, "应当返回电平信息"
    assert any("几乎没有声音" in w for w in warnings), warnings
    assert any("录制设备" in w or "麦克风" in w for w in warnings), warnings


# --------------------------------------------------------------------------- #
# 6. 缺陷 49/50：停滞看门狗、截断裁决、断点续传
# --------------------------------------------------------------------------- #
def _serve_dir(directory: Path, request) -> dict:
    """把目录用 HTTP 发出去（下载器的真实网络路径）。"""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # noqa: A003
            pass

    httpd = socketserver.ThreadingTCPServer(
        ("127.0.0.1", 0), lambda *a, **kw: Handler(*a, directory=str(directory), **kw)
    )
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    request.addfinalizer(httpd.shutdown)
    return {"port": httpd.server_address[1]}


def _make_mp3(path: Path, seconds: float, freq: int = 440, *, start: float = 0.0) -> Path:
    exe = media.find_ffmpeg()
    cmd = [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
           "-i", f"sine=frequency={freq}:duration={seconds + start}"]
    if start:
        cmd += ["-ss", f"{start}"]
    cmd += ["-t", f"{seconds}", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
            "-f", "mp3", str(path)]
    media.run_ffmpeg(cmd, timeout=120, check=True)
    return path


def test_watchdog_policy_aborts_on_no_progress():
    """看门狗策略：窗口内推进不足即给出中止原因（缺陷 49）。"""
    cfg = AppConfig()
    cfg.download_stall_seconds = 1.0
    cfg.download_min_speed_ratio = 0.25
    dl = AudioDownloader(cfg)
    monitor = {"media": 0.0, "total": 100.0, "window_at": time.monotonic() - 3.0,
               "window_media": 0.0}
    reason = dl._watchdog_factory(monitor)()  # noqa: SLF001
    assert reason and "停滞" in reason, reason
    assert "0.00×" in reason, reason


def test_watchdog_policy_allows_healthy_and_completed_stream():
    cfg = AppConfig()
    cfg.download_stall_seconds = 1.0
    cfg.download_min_speed_ratio = 0.25
    dl = AudioDownloader(cfg)
    # 健康：窗口 3s 内推进 9s（3× 实时）→ 放行，并把窗口滚动到当下
    healthy = {"media": 9.0, "total": 100.0, "window_at": time.monotonic() - 3.0,
               "window_media": 0.0}
    assert dl._watchdog_factory(healthy)() is None  # noqa: SLF001
    assert healthy["window_at"] > time.monotonic() - 1.0, "应当滚动观察窗"
    # 已抓完（正在封头）：不能当成停滞
    done = {"media": 100.0, "total": 100.0, "window_at": time.monotonic() - 30.0,
            "window_media": 90.0}
    assert dl._watchdog_factory(done)() is None  # noqa: SLF001


def test_watchdog_actually_kills_hung_ffmpeg():
    """看门狗必须真的能杀掉「安静等待」的 ffmpeg（主线程正阻塞在读循环上）。"""
    from ecnu_transcribe.errors import StreamStalledError

    exe = media.find_ffmpeg()
    t0 = time.monotonic()
    cmd = [str(exe), "-hide_banner", "-loglevel", "quiet", "-re",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=120", "-f", "null", "-"]

    def watch() -> str | None:
        return "测试：假停滞" if time.monotonic() - t0 > 1.5 else None

    with pytest.raises(StreamStalledError) as exc:
        media.run_ffmpeg(cmd, watchdog=watch, watchdog_interval=0.3)
    assert "假停滞" in str(exc.value)
    assert time.monotonic() - t0 < 30, "看门狗没有及时中止"


def test_truncated_download_is_rejected_and_kept_for_resume(tmp_path):
    """回归（缺陷 50）：时长只有清单 20% 的产物**绝不能**当成「完成」。"""
    from ecnu_transcribe.errors import StreamTruncatedError

    exe = media.find_ffmpeg()
    src = tmp_path / "src.wav"
    media.run_ffmpeg(
        [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=4", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(src)],
        timeout=60, check=True,
    )
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = False
    cfg.download_retries = 1
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="TRUNC1", title="截断样例", course_name="t", play_url=str(src))
    with pytest.raises(StreamTruncatedError) as exc:
        dl.fetch(res, play_url=str(src), expect_duration=300.0, force=True)
    msg = str(exc.value)
    assert "截断" in msg and "断点" in msg, msg
    partial = dl.partial_path(dl.cache_path(res))
    assert partial.is_file(), "应当保留半份音频作为断点基准"
    assert media.duration_of(partial) == pytest.approx(4.0, abs=0.6)
    assert not dl.cache_path(res).is_file(), "残缺产物不能留在正式缓存路径上"


def test_complete_download_has_no_partial_left(tmp_path):
    exe = media.find_ffmpeg()
    src = tmp_path / "src.wav"
    media.run_ffmpeg(
        [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=4", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(src)],
        timeout=60, check=True,
    )
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = False
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="FULL1", title="完整样例", course_name="t", play_url=str(src))
    out = dl.fetch(res, play_url=str(src), expect_duration=4.0, force=True)
    assert out.duration_sec == pytest.approx(4.0, abs=0.5)
    assert not dl.partial_path(dl.cache_path(res)).exists()
    assert not any("截断" in w for w in out.warnings), out.warnings


def test_resume_fetches_only_the_remainder(tmp_path, request):
    """断点续传：只补抓剩余部分，拼起来要接近清单时长（缺陷 50 配套能力）。"""
    work = tmp_path / "www"
    work.mkdir()
    full = _make_mp3(work / "full.mp3", 12.0)
    srv = _serve_dir(work, request)
    url = f"http://127.0.0.1:{srv['port']}/full.mp3"

    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = False
    cfg.download_retries = 1
    cfg.download_stall_seconds = 60.0  # 本机测试别被看门狗误伤
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="RESUME1", title="续传样例", course_name="t", play_url=url)
    target = dl.cache_path(res)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 预置断点：完整音频的前 5 秒
    _make_mp3(dl.partial_path(target), 5.0)
    assert 4.5 < media.duration_of(dl.partial_path(target)) < 5.6

    out = dl.fetch(res, play_url=url, expect_duration=12.0, force=True)
    assert out.duration_sec == pytest.approx(12.0, abs=1.0), out.duration_sec
    assert out.duration_sec > 10.5, "续传结果不能只剩前半段"
    assert any("断点" in w for w in out.warnings), out.warnings
    assert not dl.partial_path(target).exists(), "续传成功后断点应当被清理"
    assert full.is_file()


def test_complete_leftover_part_is_promoted_without_redownload(tmp_path, request):
    """回归（缺陷 59 第二段）：`.part` 其实**已经下完**时要扶正，而不是从零重下。

    实测现场：用户的 `.part` 是 25.19 MB / ffprobe 3301.3s，清单 3301.0s —— 下载已经完成，
    只是进程在收尾改名那一刻被关了。旧逻辑会把它当"不存在"重下 25 MB。
    """
    work = tmp_path / "www"
    work.mkdir()
    _make_mp3(work / "full.mp3", 12.0)
    srv = _serve_dir(work, request)
    url = f"http://127.0.0.1:{srv['port']}/full.mp3"

    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = True
    cfg.download_retries = 1
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="PART2", title="完整part样例", course_name="t", play_url=url)
    target = dl.cache_path(res)
    target.parent.mkdir(parents=True, exist_ok=True)
    # `.part` 里放的是一份**完整**音频（12s，与清单一致），模拟"下完但没改名"
    _make_mp3(target.with_suffix(target.suffix + ".part"), 12.0)

    before = srv.get_count if hasattr(srv, "get_count") else None
    out = dl.fetch(res, play_url=url, expect_duration=12.0)
    assert out.duration_sec == pytest.approx(12.0, abs=0.8), out.duration_sec
    assert out.from_cache is True, "应当直接扶正并命中缓存，而不是重新下载"
    assert target.is_file(), "扶正后正式缓存路径上应有文件"
    assert not target.with_suffix(target.suffix + ".part").exists(), ".part 应已被消费"
    _ = before


def test_complete_partial_is_adopted_without_redownload(tmp_path, request):
    """回归（缺陷 61）：`.partial` 已经下完时也要扶正（用户实测就卡在这一种）。

    现场：完整的 25.19 MB 音频被改名成 `.partial`（因为清单时长传成了 0），
    随后 `_try_resume` 又因"不知道总时长"拒绝续传 ⇒ 从头重下 55 分钟课。
    """
    work = tmp_path / "www"
    work.mkdir()
    _make_mp3(work / "full.mp3", 12.0)
    srv = _serve_dir(work, request)
    url = f"http://127.0.0.1:{srv['port']}/full.mp3"

    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = True
    cfg.download_retries = 1
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="PART3", title="完整partial样例", course_name="t", play_url=url)
    target = dl.cache_path(res)
    target.parent.mkdir(parents=True, exist_ok=True)
    _make_mp3(dl.partial_path(target), 12.0)   # 断点基准里放的是一份完整音频

    out = dl.fetch(res, play_url=url, expect_duration=12.0)
    assert out.from_cache is True, "应当扶正并命中缓存，而不是重新下载"
    assert out.duration_sec == pytest.approx(12.0, abs=0.8)
    assert target.is_file()
    assert not dl.partial_path(target).exists(), "扶正后断点应被消费"


def test_unknown_duration_leaves_leftover_untouched(tmp_path):
    """清单时长未知（0）时**不许动**残留文件：扶正会把半成品当完整，
    改名成断点又续不了（没有总时长）—— 实测那份完整音频就是这么变成白重下的。"""
    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="PART4", title="未知时长样例", course_name="t")
    target = dl.cache_path(res)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    _make_mp3(part, 5.0)
    size_before = part.stat().st_size

    dl._promote_leftover_part(target, 0.0)  # noqa: SLF001
    assert part.is_file(), "时长未知时不能动它"
    assert part.stat().st_size == size_before
    assert not dl.partial_path(target).exists(), "也不该改名成断点（那样既没扶正也续不了）"
    assert not dl._adopt_complete_leftover(target, 0.0)  # noqa: SLF001


def _serve_range(directory: Path, request) -> dict:
    """支持 `Range` 的本地服务器（并行取音频必须的 206 语义）。

    Python 自带的 `SimpleHTTPRequestHandler` **不支持 Range**，所以单测里必须自己实现，
    否则「并行」会静默退化回单流，测试看似通过其实什么都没测到。
    """
    import http.server

    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: A003
            pass

        def do_GET(self):  # noqa: N802
            name = self.path.split("?")[0].lstrip("/")
            f = Path(directory) / name
            if not f.is_file():
                self.send_error(404)
                return
            data = f.read_bytes()
            rng = self.headers.get("Range")
            seen.append(rng or "(none)")
            if rng and rng.startswith("bytes="):
                spec = rng[len("bytes="):]
                a, _, b = spec.partition("-")
                start = int(a) if a else 0
                end = int(b) if b else len(data) - 1
                end = min(end, len(data) - 1)
                chunk = data[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            else:
                chunk = data
                self.send_response(200)
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(chunk)

    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    request.addfinalizer(httpd.shutdown)
    return {"port": httpd.server_address[1], "ranges": seen}


def test_parallel_download_splits_into_windows_and_concatenates(tmp_path, request):
    """回归（缺陷 63）：并行取音频 —— 按时间窗分多路抓取再拼接，结果必须完整。

    实测依据（真实 CDN，2026-09-14）：单连接 ~670 KB/s、4 路聚合 ~2.1 MB/s；
    600 秒音频单流 ~3.2× 实时、4 路 **8.4× 实时**（提速 2.6×）。
    这里用本地 Range 服务器验证**机制正确**：分成多路 → 每路带正确 `-ss` → 拼回完整时长。
    """
    work = tmp_path / "www"
    work.mkdir()
    full = _make_mp3(work / "full.mp3", 40.0)
    srv = _serve_range(work, request)
    url = f"http://127.0.0.1:{srv['port']}/full.mp3"

    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = False
    cfg.download_retries = 1
    cfg.download_connections = 4
    cfg.download_window_min_sec = 10.0      # 40s / 10s ⇒ 4 路
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="PAR1", title="并行样例", course_name="t", play_url=url)
    out = dl.fetch(res, play_url=url, expect_duration=40.0, force=True)

    assert out.duration_sec == pytest.approx(40.0, abs=1.5), out.duration_sec
    assert any("并行" in w for w in out.warnings), out.warnings
    # 服务器应当看到多个不同的 Range（4 路各一次 + 能力探测）
    ranges = [r for r in srv["ranges"] if r != "(none)"]
    assert len(ranges) >= 4, f"应当发出多路 Range 请求，实际 {ranges}"
    assert full.is_file()
    # 中间产物必须清理干净，别在缓存目录里留 .seg/.parallel
    leftovers = [p.name for p in dl.cache_path(res).parent.glob("*PAR1*") if p.suffix != ".mp3"]
    assert not leftovers, f"并行中间产物未清理：{leftovers}"


def test_parallel_is_skipped_when_disabled(tmp_path, request):
    """`download_connections=1` 时退回单流（且不产生并行中间产物）。"""
    work = tmp_path / "www"
    work.mkdir()
    _make_mp3(work / "full.mp3", 12.0)
    srv = _serve_range(work, request)
    url = f"http://127.0.0.1:{srv['port']}/full.mp3"

    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = False
    cfg.download_retries = 1
    cfg.download_connections = 1
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="SINGLE1", title="单流样例", course_name="t", play_url=url)
    out = dl.fetch(res, play_url=url, expect_duration=12.0, force=True)
    assert out.duration_sec == pytest.approx(12.0, abs=0.8)
    assert not any("并行" in w for w in out.warnings), out.warnings


def test_leftover_part_file_is_resumed_not_redownloaded(tmp_path, request):
    """回归（缺陷 59）：被强杀/关窗留下的 `.part` 不能白扔。

    实测现场：用户在应用里下到 95%（25.19 MB / 26.4 MB）时关窗，`.part` 留在磁盘上；
    旧逻辑下次整段重下。现在它会被认成断点，只补抓剩余部分。
    """
    work = tmp_path / "www"
    work.mkdir()
    _make_mp3(work / "full.mp3", 12.0)
    srv = _serve_dir(work, request)
    url = f"http://127.0.0.1:{srv['port']}/full.mp3"

    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = True
    cfg.download_retries = 1
    cfg.download_stall_seconds = 60.0
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="PART1", title="残留part样例", course_name="t", play_url=url)
    target = dl.cache_path(res)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 模拟「上次下到 5 秒就被中断」：ffmpeg 的中间产物就叫 <目标>.part
    _make_mp3(target.with_suffix(target.suffix + ".part"), 5.0)

    out = dl.fetch(res, play_url=url, expect_duration=12.0)
    assert out.duration_sec == pytest.approx(12.0, abs=1.0), out.duration_sec
    assert any("断点" in w for w in out.warnings), out.warnings
    assert not dl.partial_path(target).exists(), "续传成功后断点应清理"
    assert not target.with_suffix(target.suffix + ".part").exists(), "残留 .part 应被消费掉"


def test_force_download_resumes_from_incomplete_cache(tmp_path, request):
    """即使 force=True（GUI「强制重跑」），半份缓存也要**续传**而不是白扔。

    force 的语义是「别信缓存」，但半份文件是已经花掉的下载时间换来的成果。
    实测事故里那半份是 1900s/3301s —— 直接重下等于白扔 16 分钟。
    """
    work = tmp_path / "www"
    work.mkdir()
    _make_mp3(work / "full.mp3", 12.0)
    srv = _serve_dir(work, request)
    url = f"http://127.0.0.1:{srv['port']}/full.mp3"

    cfg = AppConfig()
    cfg.output_dir = str(tmp_path)
    cfg.cache_enabled = True
    cfg.download_retries = 1
    cfg.download_stall_seconds = 60.0
    dl = AudioDownloader(cfg)
    res = Resource(resource_id="FORCE1", title="强制重跑样例", course_name="t", play_url=url)
    target = dl.cache_path(res)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 缓存路径上直接放一份**短**音频（模拟上次被中断留下的残件）
    _make_mp3(target, 5.0)
    assert media.duration_of(target) < 6.0

    out = dl.fetch(res, play_url=url, expect_duration=12.0, force=True)
    assert out.duration_sec == pytest.approx(12.0, abs=1.0), out.duration_sec
    assert any("断点" in w for w in out.warnings), out.warnings
    assert not dl.partial_path(target).exists()
    assert media.duration_of(target) == pytest.approx(12.0, abs=1.0)
