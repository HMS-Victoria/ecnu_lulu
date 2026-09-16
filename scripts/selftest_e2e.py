"""离线端到端集成测试：真实 ffmpeg 拉取 **AES-128 加密的本地 HLS** 流。

覆盖点（不依赖任何外网）：
    1. ``media.find_ffmpeg`` / ``build_input_args`` / ``build_output_args`` 真实可用；
    2. HLS 分片被正确还原（m3u8 里的相对路径）；
    3. ``EXT-X-KEY:METHOD=AES-128`` 的 key 请求带上同样的 Cookie/Header 并能解密；
    4. ``AudioDownloader`` 产出的音频时长与源音频一致（误差 < 2%）；
    5. 缓存命中会跳过重复下载；
    6. DRM 保护的 m3u8 会被 ``DrmDetectedError`` 拦下（不做绕过）；
    7. ``Pipeline`` 在注入式假 ASR 下跑完 pending → done，并写出 txt/srt/md；
    8. 断点续跑：第二次运行不重新下载音频、不重新调用 ASR。

运行::

    .venv\\Scripts\\python scripts\\selftest_e2e.py
"""

from __future__ import annotations

import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

#: Windows 控制台默认 GBK，emoji / 中文会抛 UnicodeEncodeError，这里强制 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecnu_transcribe import media, paths  # noqa: E402
from ecnu_transcribe.catalog import Resource  # noqa: E402
from ecnu_transcribe.config import AppConfig, ConfigManager  # noqa: E402
from ecnu_transcribe.downloader import AudioDownloader  # noqa: E402
from ecnu_transcribe.errors import DrmDetectedError  # noqa: E402
from ecnu_transcribe.exporter import export_all  # noqa: E402
from ecnu_transcribe.logbus import get_logger, setup_logging  # noqa: E402
from ecnu_transcribe.pipeline import Pipeline, PipelineHooks  # noqa: E402
from ecnu_transcribe.store import Stage, StateStore, TaskRecord  # noqa: E402
from ecnu_transcribe.transcriber import Segment, Transcript  # noqa: E402

log = get_logger("selftest.e2e")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '⛔'} {name}{('  — ' + detail) if detail else ''}")


# --------------------------------------------------------------------------- #
def build_encrypted_hls(work: Path, seconds: int = 24, seg_sec: int = 4) -> tuple[Path, str]:
    """生成一份 AES-128 加密的 HLS（含 key 文件），返回 (目录, 主 m3u8 相对路径)。"""
    import os

    src = work / "source.wav"
    media.run_ffmpeg(
        [
            str(media.find_ffmpeg()), "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
            "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(src),
        ],
        timeout=120, check=True,
    )

    hls = work / "hls"
    hls.mkdir(parents=True, exist_ok=True)
    key = hls / "key.bin"
    key.write_bytes(b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10")
    keyinfo = hls / "keyinfo.txt"
    keyinfo.write_text(f"key.bin\n{key}\n", encoding="utf-8")

    media.run_ffmpeg(
        [
            str(media.find_ffmpeg()), "-hide_banner", "-nostdin", "-y",
            "-i", str(src),
            "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "16000",
            "-f", "hls", "-hls_time", str(seg_sec), "-hls_list_size", "0",
            "-hls_playlist_type", "vod",
            "-hls_key_info_file", str(keyinfo),
            "-hls_segment_filename", str(hls / "seg-%03d.ts"),
            str(hls / "index.m3u8"),
        ],
        timeout=180, check=True,
    )

    playlist = (hls / "index.m3u8").read_text(encoding="utf-8")
    assert "AES-128" in playlist, "未生成 AES-128 加密的 m3u8"
    assert "seg-000.ts" in playlist
    return hls, "index.m3u8"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A002
        log.debug("http: " + fmt, *args)


def serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", httpd


# --------------------------------------------------------------------------- #
class FakeTranscriber:
    """假 ASR：按音频时长产出两条带时间戳的片段（用于验证流水线，不联网）。"""

    name = "fake-asr"

    def __init__(self, cfg: AppConfig, *_: object, **__: object) -> None:
        self.cfg = cfg
        self.calls = 0

    def transcribe(self, audio_path: Path, *, duration_sec: float = 0.0) -> Transcript:
        self.calls += 1
        dur = duration_sec or media.duration_of(Path(audio_path))
        return Transcript(
            segments=[
                Segment(0.0, dur / 2, "第一段：二叉树的前序遍历从根节点开始。"),
                Segment(dur / 2, dur, "第二段：中序遍历的顺序是左、根、右。"),
            ],
            language="zh",
            duration_sec=dur,
            model="fake-asr-v1",
            provider=self.name,
            meta={"fake": True, "audio_path": str(audio_path)},
        )


def main() -> int:
    setup_logging()
    print("=" * 78)
    print("离线端到端自检：AES-128 加密 HLS → ffmpeg 取音频 → ASR → txt/srt/md")
    print("=" * 78)

    work = ROOT / "build" / "e2e"
    work.mkdir(parents=True, exist_ok=True)

    # ---- 0) ffmpeg 可用性 ---- #
    print("\n[0] ffmpeg")
    ffmpeg = media.find_ffmpeg()
    check("ffmpeg 可用", ffmpeg.is_file(), str(ffmpeg))
    print("      " + media.ffmpeg_version(ffmpeg))

    # ---- 1) 构造本地加密 HLS ---- #
    print("\n[1] 构造本地 AES-128 加密 HLS")
    hls_dir, playlist_rel = build_encrypted_hls(work)
    base_url, httpd = serve(hls_dir)
    stream_url = f"{base_url}/{playlist_rel}"
    check("m3u8 生成", (hls_dir / playlist_rel).is_file())
    check("分片生成", len(list(hls_dir.glob("seg-*.ts"))) >= 3)
    src_duration = media.duration_of(work / "source.wav")
    print(f"      源音频时长 {src_duration:.2f}s；流地址 {stream_url}")

    cfg = AppConfig()
    cfg.output_dir = str(work / "output")
    cfg.cache_enabled = True
    cfg.audio_format = "mp3"
    cfg.keep_video = False
    cfg.asr_provider = "faster_whisper_local"

    resource = Resource(
        resource_id="E2E-HLS-1",
        title="第1讲 自检样例（加密HLS）",
        course_id="E2E-C1",
        course_name="自检课程",
        duration_sec=src_duration,
        record_time="2026-01-01 09:00:00",
        mime="application/vnd.apple.mpegurl",
        play_url=stream_url,
    )

    # ---- 2) 下载器：带 Cookie/Header 拉加密流 ---- #
    print("\n[2] AudioDownloader 拉取加密 HLS（AES-128）")
    dl = AudioDownloader(cfg, session_cookies={"E2E_SESSION": "dummy-cookie-value"})
    result = dl.fetch(resource, play_url=stream_url, expect_duration=src_duration)
    drift = abs(result.duration_sec - src_duration) / max(src_duration, 0.01)
    check("产出音频文件", result.path.is_file() and result.path.stat().st_size > 4096, result.path.name)
    check("只保留音频（mp3）", result.path.suffix == ".mp3")
    check(
        "时长与源一致（误差 < 2%）",
        drift < 0.02,
        f"源 {src_duration:.2f}s / 产出 {result.duration_sec:.2f}s（偏差 {drift*100:.2f}%）",
    )
    check("记录了 sha256", len(result.sha256) == 64, result.sha256[:16] + "…")
    for w in result.warnings:
        print("      告警：" + w)

    # ---- 3) 缓存命中 ---- #
    print("\n[3] 缓存命中（断点续跑：不重下）")
    result2 = dl.fetch(resource, play_url=stream_url, expect_duration=src_duration)
    check("第二次命中缓存", result2.from_cache is True)
    check("sha256 与首次一致", result2.sha256 == result.sha256)

    # ---- 4) DRM 拒绝 ---- #
    print("\n[4] DRM 检测（不做绕过）")
    drm_dir = work / "drm"
    drm_dir.mkdir(parents=True, exist_ok=True)
    (drm_dir / "index.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:7\n"
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://widevine",KEYFORMAT="com.widevine.alpha"\n'
        "#EXTINF:10.000,\nseg-000.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    drm_url = f"{base_url.replace(str(hls_dir), str(drm_dir))}/index.m3u8"
    drm_base, drm_httpd = serve(drm_dir)
    print(f"      DRM 流地址 {drm_base}/index.m3u8")
    drm_resource = Resource(
        resource_id="E2E-DRM-1",
        title="DRM 保护样例",
        course_name="自检课程",
        play_url=f"{drm_base}/index.m3u8",
    )
    try:
        dl_drm = AudioDownloader(cfg, session_cookies={"E2E_SESSION": "dummy"})
        dl_drm.fetch(drm_resource, play_url=f"{drm_base}/index.m3u8", force=True)
        check("DRM 应被拦下", False, "没有抛出 DrmDetectedError")
    except DrmDetectedError as exc:
        check("DRM 被拦下并给出证据", True, str(exc).splitlines()[0][:90])
    except Exception as exc:  # noqa: BLE001
        check("DRM 应被拦下", False, f"抛出的是 {type(exc).__name__}: {exc}")

    # ---- 5) 流水线（注入假 ASR）---- #
    print("\n[5] Pipeline：pending → done，产出 txt/srt/md")
    import ecnu_transcribe.pipeline as pipeline_mod

    # 清掉上一次自检留下的产物，保证这次是从零跑通（而不是复用旧转写缓存）
    import shutil

    for stale in (work / "output", work / "state.db"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        elif stale.is_file():
            stale.unlink()
    dl.cache_path(resource).unlink(missing_ok=True)

    fake = FakeTranscriber(cfg)
    original_factory = pipeline_mod.create_transcriber
    pipeline_mod.create_transcriber = lambda *a, **kw: fake  # type: ignore[assignment]
    try:
        store = StateStore(work / "state.db")
        task = store.upsert_task(
            TaskRecord(
                course=resource.course_name,
                course_id=resource.course_id,
                resource_id=resource.resource_id,
                title=resource.title,
                output_dir=cfg.output_dir,
                duration_sec=src_duration,
                play_url=stream_url,
                stage=str(Stage.PENDING),
            )
        )
        hooks = PipelineHooks(on_stage=lambda tid, st, pct, msg: log.info("[%5.1f%%] %s %s", pct, st, msg))
        pipe = Pipeline(cfg, store, cm=ConfigManager(config_file=work / "cfg.json", secrets_file=work / "sec.json"), hooks=hooks)
        final = pipe.run(task, resource)
        pipe.close()

        check("任务状态为 done", final.stage == str(Stage.DONE), f"stage={final.stage} error={final.error[:120]}")
        check("产出 3 个文件", len(final.outputs) == 3, ", ".join(Path(p).name for p in final.outputs))
        suffixes = sorted(Path(p).suffix for p in final.outputs)
        check("后缀为 .md/.srt/.txt", suffixes == [".md", ".srt", ".txt"], str(suffixes))
        for p in final.outputs:
            path = Path(p)
            check(f"文件存在且非空 {path.name}", path.is_file() and path.stat().st_size > 0, f"{path.stat().st_size} bytes")
        srt = Path(final.transcript_path).parent / f"{resource.title}.srt"
        if srt.is_file():
            body = srt.read_text(encoding="utf-8")
            check("SRT 含时间轴", "-->" in body, body.splitlines()[1] if len(body.splitlines()) > 1 else "")
        md = [p for p in final.outputs if p.endswith(".md")][0]
        check("MD 含元信息与全文", "## 元信息" in Path(md).read_text(encoding="utf-8"))
        check("ASR 被调用了一次", fake.calls == 1, f"calls={fake.calls}")

        # ---- 6) 断点续跑 ---- #
        print("\n[6] 断点续跑（第二次不重下、不重跑 ASR）")
        audio_before = Path(final.audio_path)
        mtime_before = audio_before.stat().st_mtime
        task2 = store.get_task(task.id)
        store.reset_for_rerun(task.id, keep_audio=True)
        task2 = store.get_task(task.id)
        final2 = pipe2 = Pipeline(
            cfg, store, cm=ConfigManager(config_file=work / "cfg.json", secrets_file=work / "sec.json"), hooks=hooks
        )
        final2 = pipe2.run(task2, resource)
        pipe2.close()
        check("重跑后仍为 done", final2.stage == str(Stage.DONE), final2.error[:120])
        check("音频未被重新下载", audio_before.stat().st_mtime == mtime_before)
        check("ASR 未被重复调用（复用 transcript.json）", fake.calls == 1, f"calls={fake.calls}")
        store.close()
    finally:
        pipeline_mod.create_transcriber = original_factory  # type: ignore[assignment]
        httpd.shutdown()
        drm_httpd.shutdown()

    # ---- 汇总 ---- #
    print("\n" + "=" * 78)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        for name in FAIL:
            print("  ⛔ " + name)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
