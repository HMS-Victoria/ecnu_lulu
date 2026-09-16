"""暂停功能端到端验证：在**真实运行的流水线**中间暂停/恢复。

用本地加密 HLS + 本地 ASR 起一条真任务，然后在任务进行中触发暂停，
验证：
    1. 任务确实在断点处停下（进度长时间不前进）；
    2. 恢复后任务继续并最终完成；
    3. 暂停期间点「停止」能立刻中断，任务落到 canceled；
    4. 暂停不破坏产物：恢复后仍产出 txt/srt/md。

运行::

    .venv\\Scripts\\python scripts\\verify_pause.py
"""

from __future__ import annotations

import shutil
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

from ecnu_transcribe import media  # noqa: E402
from ecnu_transcribe.catalog import Resource  # noqa: E402
from ecnu_transcribe.config import AppConfig, ConfigManager  # noqa: E402
from ecnu_transcribe.logbus import get_logger, setup_logging  # noqa: E402
from ecnu_transcribe.pausegate import PauseGate  # noqa: E402
from ecnu_transcribe.pipeline import Pipeline, PipelineHooks  # noqa: E402
from ecnu_transcribe.store import Stage, StateStore, TaskRecord  # noqa: E402

log = get_logger("verify.pause")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '⛔'} {name}{('  — ' + detail) if detail else ''}")


# --------------------------------------------------------------------------- #
def build_long_audio(work: Path, seconds: int = 30) -> Path:
    """合成一段较长的**真实语音**音频（多句 + 句间静音）。

    注意不能用纯正弦音：本地 ASR 服务开了 ``vad_filter``，会把纯音调判成静音、
    返回空结果（踩过一次）。所以优先用 Windows 中文语音合成；没有语音时退化为
    音调 + 放宽断言（本脚本会如实说明）。
    """
    out = work / "long.wav"
    exe = media.find_ffmpeg()

    voice = _sapi_voice()
    if voice:
        parts: list[Path] = []
        sentences = [
            "第一节，我们讨论线性表的顺序存储结构。",
            "顺序存储用一段连续的内存保存元素，随机访问的时间复杂度是常数阶。",
            "但是插入和删除需要移动大量元素，平均要移动一半。",
            "所以顺序存储适合查询多、增删少的场景。",
            "下一节我们会讲链式存储，它用指针把结点串起来。",
            "链式存储的插入和删除只需要修改指针，代价是随机访问必须从头遍历。",
        ]
        for i, text in enumerate(sentences):
            seg = work / f"seg{i:02d}.wav"
            if not _synthesize(text, seg, voice):
                break
            parts.append(seg)
            gap = work / f"gap{i:02d}.wav"
            media.run_ffmpeg(
                [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
                 "-i", "anullsrc=r=16000:cl=mono", "-t", "0.5", "-c:a", "pcm_s16le", str(gap)],
                timeout=60, check=True,
            )
            parts.append(gap)
        if len(parts) >= 4:
            listing = work / "list.txt"
            listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
            media.run_ffmpeg(
                [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(listing), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
                timeout=300, check=True,
            )
            return out

    # 退化：纯音调（链路可用，但 ASR 可能返回空 —— 调用方需据此放宽断言）
    print("    ⚠ 本机无中文语音，改用音调占位（ASR 可能返回空结果）")
    media.run_ffmpeg(
        [
            str(exe), "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds // 2}",
            "-f", "lavfi", "-i", f"sine=frequency=660:duration={seconds - seconds // 2}",
            "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
        ],
        timeout=180, check=True,
    )
    return out


def _sapi_voice() -> str:
    import subprocess as _sp

    if sys.platform != "win32":
        return ""
    try:
        r = _sp.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Add-Type -AssemblyName System.Speech; "
             "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
             "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' } | Select-Object -First 1; "
             "if ($v) { $v.VoiceInfo.Name } else { '' }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _synthesize(text: str, out_wav: Path, voice: str) -> bool:
    import subprocess as _sp

    safe = text.replace("'", "''")
    try:
        _sp.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Add-Type -AssemblyName System.Speech; "
             "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
             f"$s.SelectVoice('{voice}'); $s.Rate = 0; "
             f"$s.SetOutputToWaveFile('{out_wav}'); $s.Speak('{safe}'); $s.Dispose();"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        return out_wav.is_file() and out_wav.stat().st_size > 2000
    except Exception:  # noqa: BLE001
        return False


def start_asr_server(model: str, port: int) -> subprocess.Popen | None:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "local_asr_server.py"),
         "--model", model, "--port", str(port), "--device", "cpu"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    deadline = time.time() + 600
    while time.time() < deadline:
        if proc.poll() is not None:
            print("⛔ ASR 服务启动失败")
            return None
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                if r.status == 200:
                    return proc
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    proc.terminate()
    return None


# --------------------------------------------------------------------------- #
def main() -> int:
    setup_logging()
    print("=" * 78)
    print("暂停功能端到端验证（真实流水线 + 本地 ASR）")
    print("=" * 78)

    work = ROOT / "build" / "pause_verify"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    out_dir = work / "output"

    # 分段结果缓存是**全机共享**的（`cache/segments/<音频哈希>-<方案哈希>`，这正是断点续跑
    # 能省钱的原因）。但本验证要观察的是「任务运行中被暂停」，一旦上一轮留下的分段结果命中，
    # 41s 的音频会在 0.1s 内跑完 —— 暂停请求（t≈0.7s）赶到时任务已经 done，
    # 于是「暂停期间任务未结束」「UI 收到暂停提示」全线失败（实测如此）。
    # 所以本次验证把缓存目录指到自己的临时目录，保证每轮都是冷启动、任务真的在跑。
    from ecnu_transcribe import paths as _paths

    _paths.cache_dir = lambda: work / "cache"  # type: ignore[assignment]

    print("\n[1] 准备素材")
    audio_src = build_long_audio(work, seconds=40)
    dur = media.duration_of(audio_src)
    print(f"    长音频 {dur:.1f}s → {audio_src.name}")

    asr_port = 8351
    asr = start_asr_server("tiny", asr_port)
    if asr is None:
        print("⛔ 无法启动本地 ASR 服务，跳过本验证")
        return 1
    print(f"    ✅ 本地 ASR 就绪 http://127.0.0.1:{asr_port}/v1")

    cfg = AppConfig()
    cfg.output_dir = str(out_dir)
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = f"http://127.0.0.1:{asr_port}/v1"
    cfg.asr_model = "faster-whisper-tiny"
    cfg.asr_language = "zh"
    cfg.asr_max_segment_sec = 8          # 强制切分成多段 → 制造分段断点
    cfg.asr_chunk_strategy = "fixed"
    cfg.emit_txt = cfg.emit_srt = cfg.emit_md = True

    cm = ConfigManager(config_file=work / "c.json", secrets_file=work / "s.json")
    cm.load()

    resource = Resource(
        resource_id="PAUSE-1",
        title="暂停验证样例",
        course_id="PAUSE-C",
        course_name="暂停验证课程",
        duration_sec=dur,
        play_url=str(audio_src),          # 本地文件当播放源，避免起 HTTP server
    )

    # ------------------------------------------------------------------ #
    print("\n[2] 场景 A：运行中暂停 → 恢复 → 完成")
    store = StateStore(work / "state_a.db")
    task = store.upsert_task(TaskRecord(
        course=resource.course_name, course_id=resource.course_id,
        resource_id=resource.resource_id, title=resource.title,
        output_dir=str(out_dir), duration_sec=dur, play_url=resource.play_url,
        stage=str(Stage.PENDING),
    ))

    gate = PauseGate(poll_interval=0.02)
    progress_log: list[tuple[float, float, str]] = []  # (时间, 进度, 消息)
    t0 = time.time()
    hooks = PipelineHooks(
        on_stage=lambda tid, st, pct, msg: progress_log.append((time.time() - t0, pct, msg)),
        gate=gate,
    )
    pipe = Pipeline(cfg, store, cm=cm, hooks=hooks)

    result_holder: dict[str, object] = {}

    def run_pipeline() -> None:
        try:
            result_holder["final"] = pipe.run(store.get_task(task.id), resource, force=True)
        except Exception as exc:  # noqa: BLE001
            result_holder["error"] = exc

    runner = threading.Thread(target=run_pipeline, daemon=True)
    runner.start()

    # 等任务真正开始（有进度上报）
    deadline = time.time() + 30
    while time.time() < deadline and not progress_log:
        time.sleep(0.05)
    check("任务已启动并上报进度", bool(progress_log), f"{len(progress_log)} 条进度事件")

    # 立刻暂停
    time.sleep(0.6)
    gate.pause()
    paused_at = time.time()
    paused_event_idx = len(progress_log)
    print(f"    → 已请求暂停（t={paused_at - t0:.1f}s，已收到 {paused_event_idx} 条进度事件）")

    # 等 1.5s，确认进度确实不再前进
    time.sleep(1.5)
    snap_idx = len(progress_log)
    last_pct = progress_log[-1][1] if progress_log else 0.0
    time.sleep(1.5)
    new_events = progress_log[snap_idx:]
    stalled = all(abs(pct - last_pct) < 0.01 for _t, pct, _m in new_events) if new_events else True
    pause_msgs = [m for _t, _p, m in progress_log[paused_event_idx:] if "⏸" in m or "暂停" in m]
    check("暂停后进度不再前进（确实停在断点）", stalled,
          f"暂停后新事件 {len(new_events)} 条，进度仍为 {last_pct:.1f}%")
    check("UI 收到暂停状态提示", bool(pause_msgs),
          (pause_msgs[-1] if pause_msgs else "（无）")[:70])
    check("暂停期间任务未结束", runner.is_alive() is True)

    # 恢复
    resume_at = time.time()
    gate.resume()
    print(f"    → 已恢复（暂停了 {resume_at - paused_at:.1f}s）")
    runner.join(timeout=600)
    final = result_holder.get("final")
    check("恢复后任务继续并完成", bool(final and final.stage == str(Stage.DONE)),
          f"stage={getattr(final, 'stage', None)} err={str(getattr(final, 'error', ''))[:110]}")
    if final is not None and getattr(final, "outputs", None):
        check("暂停/恢复未破坏产物（3 个文件）", len(final.outputs) == 3,
              ", ".join(Path(p).name for p in final.outputs))
    else:
        check("暂停/恢复未破坏产物（3 个文件）", False,
              f"outputs={getattr(final, 'outputs', None)}")
    recovery_msgs = [m for _t, _p, m in progress_log if "▶" in m or "恢复" in m]
    check("UI 收到恢复提示", bool(recovery_msgs), (recovery_msgs[-1] if recovery_msgs else "（无）")[:60])
    print(f"    实际暂停时长（闸门统计）：{gate.paused_seconds():.1f}s")
    pipe.close()
    store.close()

    # ------------------------------------------------------------------ #
    print("\n[3] 场景 B：暂停期间点「停止」→ 立刻中断且落到 canceled")
    # 场景 B **必须用与 A 不同的音频**：缺陷 46 引入的分段结果缓存按
    # 「音频哈希 + 切分方案哈希」复用，若 B 用同一份素材，6 段会在 0.1s 内全部复用、
    # 任务直接 done —— 「暂停中停止」根本来不及生效（实测 stage=done，本项失败）。
    # 这里只把音量压一点点（对识别无影响），换来一个不同的哈希。
    audio_b = work / "long_b.wav"
    if not audio_b.is_file():
        media.run_ffmpeg(
            [str(media.find_ffmpeg()), "-hide_banner", "-nostdin", "-y", "-i", str(audio_src),
             "-af", "volume=0.93", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio_b)],
            timeout=180, check=True,
        )
    store_b = StateStore(work / "state_b.db")
    task_b = store_b.upsert_task(TaskRecord(
        course=resource.course_name, course_id=resource.course_id,
        resource_id="PAUSE-2", title="暂停后停止样例",
        output_dir=str(out_dir), duration_sec=dur, play_url=resource.play_url,
        stage=str(Stage.PENDING),
    ))
    res_b = Resource(
        resource_id="PAUSE-2", title="暂停后停止样例", course_id="PAUSE-C",
        course_name="暂停验证课程", duration_sec=dur, play_url=str(audio_b),
    )
    cancel_ev = threading.Event()
    gate_b = PauseGate(poll_interval=0.02)
    gate_b.bind_cancel_event(cancel_ev)
    pipe_b = Pipeline(cfg, store_b, cm=cm, hooks=PipelineHooks(cancel=cancel_ev, gate=gate_b))

    holder_b: dict[str, object] = {}
    started = threading.Event()

    def run_b() -> None:
        try:
            holder_b["final"] = pipe_b.run(store_b.get_task(task_b.id), res_b, force=True)
        except Exception as exc:  # noqa: BLE001
            holder_b["error"] = exc
        finally:
            started.set()

    tb = threading.Thread(target=run_b, daemon=True)
    tb.start()
    time.sleep(1.0)
    gate_b.pause()
    time.sleep(0.8)
    t_stop = time.time()
    cancel_ev.set()
    gate_b.cancel()          # 模拟 UI 的「停止」
    tb.join(timeout=60)
    elapsed = time.time() - t_stop
    final_b = store_b.get_task(task_b.id)
    check("暂停中「停止」能在 30s 内退出", not tb.is_alive(), f"耗时 {elapsed:.1f}s")
    check("任务状态落到 canceled", bool(final_b and final_b.stage == str(Stage.CANCELED)),
          f"stage={getattr(final_b, 'stage', None)}")
    pipe_b.close()
    store_b.close()

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 78)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for name in FAIL:
        print("  ⛔ " + name)
    print("=" * 78)

    asr.terminate()
    try:
        asr.wait(timeout=10)
    except subprocess.TimeoutExpired:
        asr.kill()
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
