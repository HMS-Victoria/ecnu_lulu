"""一键离线演示：不需要校园网、不需要任何 API Key，直接看到完整产物。

流程（与 GUI 里勾选后点「开始」跑的是**同一套** Pipeline / Downloader / Transcriber / Exporter）::

    1. 用 ffmpeg 合成几段「模拟课堂」音频
       （每段 = 提示音 + 语音 + 静音 + 语音，模仿一节录播的动静结构）
    2. 用本地 HTTP server 把它们当作 **HLS 流**发布（含 AES-128 加密那一路）
    3. 走 AudioDownloader 拉流取音频（ffmpeg，只留音频）
    4. 走 Pipeline 完成 下载 → 转写 → 写出 txt/srt/md
    5. 打印产物路径 + 内容节选；并演示**断点续跑**（第二次不重下、不重跑 ASR）

语音来源（自动选择）::

    * Windows 中文语音合成（SAPI）—— 有真实语音，推荐
    * 没有可用语音时退化为「音调序列」，仅用于验证链路（会在输出里如实标注）

用法::

    .venv\\Scripts\\python scripts\\demo_offline.py
    .venv\\Scripts\\python scripts\\demo_offline.py --asr local --model small
    .venv\\Scripts\\python scripts\\demo_offline.py --asr stub          # 完全不起 ASR 服务，用占位转写
    .venv\\Scripts\\python scripts\\demo_offline.py --out D:\\demo输出
"""

from __future__ import annotations

import argparse
import http.server
import json
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

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
from ecnu_transcribe.exporter import export_all  # noqa: E402
from ecnu_transcribe.logbus import get_logger, setup_logging  # noqa: E402
from ecnu_transcribe.pipeline import Pipeline, PipelineHooks  # noqa: E402
from ecnu_transcribe.store import Stage, StateStore, TaskRecord  # noqa: E402
from ecnu_transcribe.transcriber import Segment, Transcript, create_transcriber  # noqa: E402

log = get_logger("demo.offline")

# --------------------------------------------------------------------------- #
# 演示素材：模拟一堂「数据结构」课的录播
# --------------------------------------------------------------------------- #
LESSONS = [
    {
        "resource_id": "DEMO-L01",
        "title": "第1讲 绪论与算法复杂度",
        "course": "数据结构与算法（离线演示）",
        "segments": [
            "同学们好，今天我们讲数据结构的第一讲，绪论与算法复杂度。",
            "算法的时间复杂度用大O记号表示，它描述的是输入规模趋于无穷时运行时间的增长量级。",
            "常见的时间复杂度从低到高依次是：常数阶、对数阶、线性阶、线性对数阶、平方阶和指数阶。",
            "举个例子，二分查找的时间复杂度是对数阶，而归并排序是线性对数阶。",
        ],
    },
    {
        "resource_id": "DEMO-L02",
        "title": "第2讲 线性表：顺序存储与链式存储",
        "course": "数据结构与算法（离线演示）",
        "segments": [
            "这一讲我们讨论线性表的两种基本实现方式。",
            "第一种是顺序存储，用数组实现，随机访问是常数时间，但是插入和删除平均需要移动一半元素。",
            "第二种是链式存储，用指针把结点串起来，插入删除只要改指针，但随机访问必须从头遍历。",
            "所以选哪种实现，取决于你的应用是查询多还是增删多。",
        ],
    },
]


# --------------------------------------------------------------------------- #
def _sapi_voice() -> str:
    """找一个可用的 Windows 中文语音；没有就返回空串。"""
    if sys.platform != "win32":
        return ""
    ps = [
        "Add-Type -AssemblyName System.Speech;",
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;",
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' } | Select-Object -First 1;",
        "if ($v) { $v.VoiceInfo.Name } else { '' }",
    ]
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", " ".join(ps)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        return (out.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("探测 SAPI 语音失败：%s", exc)
        return ""


def _synthesize_windows(text: str, out_wav: Path, voice: str) -> bool:
    """用 SAPI 合成一段语音到 wav。"""
    safe = text.replace("'", "''")
    ps = [
        "Add-Type -AssemblyName System.Speech;",
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;",
        f"$s.SelectVoice('{voice}');",
        "$s.Rate = 0;",
        f"$s.SetOutputToWaveFile('{out_wav}');",
        f"$s.Speak('{safe}');",
        "$s.Dispose();",
    ]
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", " ".join(ps)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        return out_wav.is_file() and out_wav.stat().st_size > 2000
    except Exception as exc:  # noqa: BLE001
        log.warning("SAPI 合成失败：%s", exc)
        return False


def _synth_fallback(text: str, out_wav: Path, seconds_per_char: float = 0.16) -> bool:
    """没有语音时，用不同音高的「嘟嘟声」序列占位（只验证链路，不产生可读文字）。"""
    n = max(3, int(len(text) * seconds_per_char / 0.5))
    freqs = "|".join(str(300 + (i * 37) % 500) for i in range(n))
    cmd = [
        str(media.find_ffmpeg()), "-hide_banner", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"sine=frequency={freqs.split('|')[0]}:duration={n * 0.5}",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav),
    ]
    try:
        media.run_ffmpeg(cmd, timeout=120, check=True)
        return out_wav.is_file() and out_wav.stat().st_size > 2000
    except Exception as exc:  # noqa: BLE001
        log.warning("合成占位音频失败：%s", exc)
        return False


def build_lesson_audio(lesson: dict, work: Path, *, voice: str) -> tuple[Path, str]:
    """把一条「课」合成成一个 wav：提示音 + 静音 + 各句语音（句间静音）。

    返回 ``(wav路径, 语音来源说明)``。
    """
    parts_dir = work / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = media.find_ffmpeg()
    pieces: list[Path] = []

    # 开头 0.6s 静音（模拟录音起始）
    lead = parts_dir / "lead.wav"
    media.run_ffmpeg(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono", "-t", "0.6", "-c:a", "pcm_s16le", str(lead)],
        timeout=60, check=True,
    )
    pieces.append(lead)

    source_note = "Windows 中文语音合成（SAPI）"
    for idx, text in enumerate(lesson["segments"]):
        seg_wav = parts_dir / f"{lesson['resource_id']}_s{idx:02d}.wav"
        ok = _synthesize_windows(text, seg_wav, voice) if voice else False
        if not ok:
            source_note = "合成音调占位（本机无中文语音，文本不可读，仅验证链路）"
            _synth_fallback(text, seg_wav)
        pieces.append(seg_wav)
        # 句间 0.45s 静音，给 ASR 明确的切分点（也顺便验证静音检测）
        gap = parts_dir / f"gap{idx}.wav"
        media.run_ffmpeg(
            [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
             "-i", "anullsrc=r=16000:cl=mono", "-t", "0.45", "-c:a", "pcm_s16le", str(gap)],
            timeout=60, check=True,
        )
        pieces.append(gap)

    listing = parts_dir / f"{lesson['resource_id']}_list.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
    out = work / f"{lesson['resource_id']}.wav"
    media.run_ffmpeg(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
        timeout=300, check=True,
    )
    return out, source_note


def publish_hls(src: Path, hls_dir: Path, *, encrypt: bool) -> str:
    """把 wav 发布成 HLS（可选 AES-128 加密），返回主播放列表文件名。"""
    ffmpeg = media.find_ffmpeg()
    hls_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-i", str(src),
           "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "16000",
           "-f", "hls", "-hls_time", "4", "-hls_list_size", "0", "-hls_playlist_type", "vod"]
    if encrypt:
        key = hls_dir / "key.bin"
        key.write_bytes(bytes(range(16)))
        keyinfo = hls_dir / "keyinfo.txt"
        keyinfo.write_text(f"key.bin\n{key}\n", encoding="utf-8")
        cmd += ["-hls_key_info_file", str(keyinfo)]
    cmd += ["-hls_segment_filename", str(hls_dir / "seg-%03d.ts"), str(hls_dir / "index.m3u8")]
    media.run_ffmpeg(cmd, timeout=600, check=True)
    return "index.m3u8"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A002
        log.debug("http " + fmt, *args)


def serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    handler = lambda *a, **kw: _Quiet(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", httpd


# --------------------------------------------------------------------------- #
class StubTranscriber:
    """占位转写器：不调用任何 ASR，只为演示「产物长什么样」。

    音频文件名形如 ``<安全标题>__<resource_id>.<ext>``（见 AudioDownloader.cache_path），
    所以这里从文件名里解析出 resource_id 来取对应文本。
    """

    name = "stub（占位，不是真实转写）"
    _ID_RE = __import__("re").compile(r"__([A-Za-z0-9_\-]+)\.[A-Za-z0-9]+$")

    def __init__(self, cfg: AppConfig, plan: dict[str, list[str]]) -> None:
        self.cfg = cfg
        self.plan = plan
        self.calls = 0

    def _texts_for(self, audio_path: Path) -> list[str]:
        m = self._ID_RE.search(audio_path.name)
        if m and m.group(1) in self.plan:
            return self.plan[m.group(1)]
        stem = audio_path.stem
        for key, value in self.plan.items():
            if key in stem:
                return value
        return ["（占位转写：未调用任何语音识别服务）"]

    def transcribe(self, audio_path: Path, *, duration_sec: float = 0.0) -> Transcript:
        self.calls += 1
        dur = duration_sec or media.duration_of(Path(audio_path))
        texts = self._texts_for(Path(audio_path))
        step = dur / max(1, len(texts))
        segs = [
            Segment(start=i * step, end=min(dur, (i + 1) * step), text=t) for i, t in enumerate(texts)
        ]
        return Transcript(
            segments=segs, language="zh", duration_sec=dur,
            model="stub", provider=self.name, meta={"stub": True},
        )


def _start_local_asr(model: str, port: int) -> subprocess.Popen | None:
    """后台起本地 ASR 服务，等它就绪。"""
    py = sys.executable
    proc = subprocess.Popen(
        [py, str(ROOT / "scripts" / "local_asr_server.py"), "--model", model,
         "--port", str(port), "--device", "cpu"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    deadline = time.time() + 600
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            print("⛔ 本地 ASR 服务启动失败：\n" + out[-1500:])
            return None
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
                if resp.status == 200:
                    return proc
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    print("⛔ 等待本地 ASR 服务就绪超时")
    proc.terminate()
    return None


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="一键离线演示（不需要网络与任何 API Key）")
    ap.add_argument("--asr", choices=["local", "stub", "auto"], default="auto",
                    help="local=起本地 faster-whisper 服务；stub=不转写只演示产物；auto=能起就 local")
    ap.add_argument("--model", default="tiny", help="本地 ASR 模型（tiny/base/small/medium）")
    ap.add_argument("--port", type=int, default=8321)
    ap.add_argument("--out", default="", help="输出目录（默认 build/demo/output）")
    ap.add_argument("--keep", action="store_true", help="保留中间音频与 HLS 目录")
    args = ap.parse_args()

    setup_logging()
    print("=" * 78)
    print("大夏学堂录播转写助手 —— 离线端到端演示")
    print("（不需要校园网、不需要任何 API Key；走的是和 GUI 完全相同的流水线）")
    print("=" * 78)

    work = ROOT / "build" / "demo"
    if work.exists() and not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out) if args.out else (work / "output")
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 0) 环境 ---------- #
    print("\n[0] 环境")
    ffmpeg = media.find_ffmpeg()
    print(f"    ffmpeg : {ffmpeg}")
    print("    " + media.ffmpeg_version(ffmpeg))

    # ---------- 1) 合成演示音频 ---------- #
    print("\n[1] 合成演示音频（模拟课堂录播）")
    voice = _sapi_voice()
    print(f"    中文语音：{voice or '（未找到，将用音调占位）'}")
    lessons: list[dict] = []
    for lesson in LESSONS:
        wav, note = build_lesson_audio(lesson, work, voice=voice)
        dur = media.duration_of(wav)
        print(f"    ✅ {lesson['title']}  → {wav.name}  {dur:.1f}s  [{note}]")
        lessons.append({**lesson, "wav": wav, "duration": dur, "source_note": note})

    # ---------- 2) 发布成 HLS（一路加密） ---------- #
    print("\n[2] 发布为 HLS 流（其中一路 AES-128 加密，验证加密分片链路）")
    base_url, httpd = serve(work)
    for i, lesson in enumerate(lessons):
        hls_dir = work / "hls" / lesson["resource_id"]
        encrypt = i % 2 == 0  # 第一条加密，模拟真实平台的常见形态
        rel = publish_hls(lesson["wav"], hls_dir, encrypt=encrypt)
        lesson["play_url"] = f"{base_url}/hls/{lesson['resource_id']}/{rel}"
        playlist = (hls_dir / rel).read_text(encoding="utf-8")
        lesson["encrypted"] = "AES-128" in playlist
        print(f"    ✅ 《{lesson['title']}》 {'AES-128 加密' if lesson['encrypted'] else '明文'} "
              f"→ {lesson['play_url']}")

    # ---------- 3) ASR ---------- #
    asr_proc: subprocess.Popen | None = None
    asr_mode = args.asr
    if asr_mode in ("auto", "local"):
        print("\n[3] 启动本地 ASR 服务（faster-whisper，零成本）")
        asr_proc = _start_local_asr(args.model, args.port)
        if asr_proc is not None:
            print(f"    ✅ 就绪：http://127.0.0.1:{args.port}/v1（模型 faster-whisper-{args.model}）")
            asr_mode = "local"
        elif args.asr == "local":
            print("    ⛔ 本地 ASR 不可用；若未安装请运行： "
                  ".venv\\Scripts\\python -m pip install -r requirements-optional.txt")
            httpd.shutdown()
            return 1
        else:
            print("    ⚠ 本地 ASR 不可用，改用占位转写（产物结构仍可查看）")
            asr_mode = "stub"
    else:
        print("\n[3] 跳过本地 ASR（--asr stub），使用占位转写")

    # ---------- 4) 配置与队列 ---------- #
    cfg = AppConfig()
    cfg.output_dir = str(out_dir)
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = f"http://127.0.0.1:{args.port}/v1"
    cfg.asr_model = f"faster-whisper-{args.model}"
    cfg.asr_language = "zh"
    cfg.asr_timestamps = True
    cfg.emit_txt = cfg.emit_srt = cfg.emit_md = True
    cfg.cache_enabled = True
    cm = ConfigManager(config_file=work / "config.json", secrets_file=work / "secrets.json")
    cm.load()

    store = StateStore(work / "state.db")
    resources: dict[int, Resource] = {}
    for lesson in lessons:
        res = Resource(
            resource_id=lesson["resource_id"],
            title=lesson["title"],
            course_id="DEMO-COURSE",
            course_name=lesson["course"],
            teacher="离线演示",
            duration_sec=lesson["duration"],
            record_time="2026-09-11 08:00:00",
            mime="application/vnd.apple.mpegurl",
            play_url=lesson["play_url"],
        )
        task = store.upsert_task(TaskRecord(
            course=res.course_name, course_id=res.course_id, resource_id=res.resource_id,
            title=res.title, output_dir=str(out_dir), duration_sec=res.duration_sec,
            play_url=res.play_url, stage=str(Stage.PENDING),
        ))
        resources[task.id] = res

    stub = StubTranscriber(cfg, {lesson["resource_id"]: lesson["segments"] for lesson in lessons})

    # 注入转写器（local 模式用真实端点；stub 模式用占位）
    import ecnu_transcribe.pipeline as pipeline_mod

    original_factory = pipeline_mod.create_transcriber
    if asr_mode == "stub":
        pipeline_mod.create_transcriber = lambda *a, **kw: stub  # type: ignore[assignment]

    hooks = PipelineHooks(
        on_stage=lambda tid, stage, pct, msg: print(f"      [{pct:5.1f}%] {stage:14s} {msg}"),
        on_log=lambda lvl, msg: None,
    )
    pipeline = Pipeline(cfg, store, cm=cm, hooks=hooks)

    # ---------- 5) 跑流水线 ---------- #
    print(f"\n[4] 执行流水线（{len(resources)} 条任务；下载 → 转写 → txt/srt/md）")
    results = []
    try:
        for tid, res in resources.items():
            print(f"\n    ── 任务 {tid}《{res.title}》")
            final = pipeline.run(store.get_task(tid), res)
            results.append((res, final))
    finally:
        pipeline.close()

    # ---------- 6) 断点续跑验证 ---------- #
    print("\n[5] 断点续跑验证（打回 pending 重跑：不应重新下载，也不应重新调用 ASR）")
    import ecnu_transcribe.pipeline as pm

    counting = {"asr": 0}
    if asr_mode == "local":
        real_factory = pm.create_transcriber

        def counting_factory(*a, **kw):
            inner = real_factory(*a, **kw)
            orig = inner.transcribe

            def wrapped(ap, **k):
                counting["asr"] += 1
                return orig(ap, **k)

            inner.transcribe = wrapped  # type: ignore[assignment]
            return inner

        pm.create_transcriber = counting_factory  # type: ignore[assignment]
    else:
        counting["asr"] = stub.calls

    try:
        tid0 = next(iter(resources))
        res0 = resources[tid0]
        audio_before = Path(store.get_task(tid0).audio_path or "")
        mtime_before = audio_before.stat().st_mtime if audio_before.is_file() else 0.0
        asr_before = counting["asr"]
        store.reset_for_rerun(tid0, keep_audio=True)
        p2 = Pipeline(cfg, store, cm=cm, hooks=PipelineHooks())
        final2 = p2.run(store.get_task(tid0), res0)
        p2.close()
        audio_after = Path(store.get_task(tid0).audio_path or "")
        same_file = audio_after.is_file() and audio_after.stat().st_mtime == mtime_before
        print(f"    ✅ 重跑后状态：{final2.stage}")
        print(f"    ✅ 音频未重新下载：{same_file}")
        print(f"    ✅ ASR 调用次数：重跑前 {asr_before} → 重跑后 {counting['asr']}"
              f"（{'未重复调用 ✔' if counting['asr'] == asr_before else '被重复调用了 ✘'}）")
    finally:
        if asr_mode == "stub":
            pm.create_transcriber = original_factory  # type: ignore[assignment]

    # ---------- 7) 汇总 ---------- #
    print("\n[6] 产物汇总")
    total_files = 0
    ok_count = 0
    for res, final in results:
        done = final.stage == str(Stage.DONE)
        ok_count += int(done)
        print(f"\n    {'✅' if done else '⛔'} 《{res.title}》  状态={final.stage}  进度={final.progress:.0f}%")
        for p in final.outputs:
            path = Path(p)
            size = path.stat().st_size if path.is_file() else 0
            try:
                shown = path.relative_to(out_dir)
            except ValueError:
                shown = path
            print(f"      • {shown}  ({size} bytes)")
            total_files += 1
        if final.error:
            print(f"      ⛔ {final.error[:300]}")

    # 展示第一份产物的内容节选
    if results and results[0][1].outputs:
        first_md = next((Path(p) for p in results[0][1].outputs if p.endswith(".md")), None)
        if first_md and first_md.is_file():
            print(f"\n[7] 产物内容节选（{first_md.name}）")
            print("    " + "\n    ".join(first_md.read_text(encoding="utf-8").splitlines()[:26]))

    print("\n" + "=" * 78)
    print(f"完成：{ok_count}/{len(results)} 条任务成功，共 {total_files} 个产物文件")
    print(f"输出目录：{out_dir}")
    print(f"音频缓存：{paths.media_cache_dir()}")
    if asr_mode == "stub":
        print("注意：本次用的是**占位转写**（--asr stub），文本不是真实识别结果。")
    else:
        print(f"本次转写由本地 faster-whisper-{args.model} 完成（音频未离开本机）。")
        src = lessons[0].get("source_note", "")
        if "音调占位" in src:
            print("注意：本机没有中文语音，演示音频是音调占位，转写结果不具可读性。")
    print("=" * 78)

    httpd.shutdown()
    store.close()
    if asr_proc is not None:
        asr_proc.terminate()
        try:
            asr_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            asr_proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
