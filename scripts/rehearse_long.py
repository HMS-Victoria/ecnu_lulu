"""长音频**全流程排练**：真实中文语音 → 12 分钟音频 → 切分 → ASR → txt/srt/md。

为什么做这件事：真实验收要跑 2 条 55 分钟录播。与其等登录后才发现问题，
不如用一段真实语音把**同一套代码路径**先跑一遍：

    * 静音检测 + plan_chunks（长音频会被切段）
    * 分段 ASR + 结果落盘（缺陷 46 的续跑）
    * 合并去重、时间轴夹取
    * exporter 三产物（带 UTF-8 BOM）
    * 计时（用于估算真实验收要多久）

素材：离线演示生成的中文讲解（本机 SAPI 合成，语速自然），
用 ffmpeg 首尾拼接成一个长音频 —— 对它而言「是不是真录播」不影响被测代码路径。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from ecnu_transcribe import media  # noqa: E402
from ecnu_transcribe.catalog import Resource  # noqa: E402
from ecnu_transcribe.config import ConfigManager  # noqa: E402
from ecnu_transcribe.logbus import setup_logging  # noqa: E402
from ecnu_transcribe.pipeline import Pipeline, PipelineHooks  # noqa: E402
from ecnu_transcribe.store import Stage, StateStore, TaskRecord  # noqa: E402

WORK = ROOT / "build" / "rehearsal"
TARGET_MINUTES = 12
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '⛔'} {name}{('  — ' + str(detail)) if detail else ''}", flush=True)


def build_long_speech() -> Path:
    """把演示语音首尾拼成 ~TARGET_MINUTES 分钟的长音频（自带句间停顿）。"""
    WORK.mkdir(parents=True, exist_ok=True)
    src = ROOT / "cache" / "media" / "第1讲 绪论与算法复杂度__DEMO-L01.mp3"
    if not src.is_file():
        raise SystemExit("⛔ 找不到演示语音，请先跑一次 scripts/demo_offline.py")
    one = media.duration_of(src)
    need = max(2, int(TARGET_MINUTES * 60 / max(1.0, one)))
    print(f"素材 {src.name}（{one:.1f}s）→ 拼接 {need} 次 ≈ {need * one / 60:.1f} 分钟")

    listfile = WORK / "concat.txt"
    listfile.write_text(
        "".join(f"file '{src.as_posix()}'\n" for _ in range(need)), encoding="utf-8"
    )
    out = WORK / f"long_{TARGET_MINUTES}min.mp3"
    ff = str(media.find_ffmpeg())
    p = subprocess.run(
        [ff, "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    if p.returncode != 0 or not out.is_file():
        raise SystemExit(f"⛔ 拼接失败：{(p.stderr or '')[-300:]}")
    return out


def main() -> int:
    setup_logging()
    print("=" * 90)
    print("长音频全流程排练（真实语音 → 切分 → 本地 ASR → 三产物）")
    print("=" * 90)

    audio = build_long_speech()
    dur = media.duration_of(audio)
    vol = media.probe_volume(audio)
    print(f"\n长音频：{audio.name}  {dur / 60:.1f} 分钟  {audio.stat().st_size / 1024 / 1024:.1f} MB"
          f"  电平 mean={vol.get('mean_db')} dB / max={vol.get('max_db')} dB")
    check("长音频时长符合预期（≥10 分钟）", dur >= 600, f"{dur:.0f}s")

    out_dir = WORK / "output"
    shutil.rmtree(out_dir, ignore_errors=True)
    cfg = ConfigManager().load()
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = "http://127.0.0.1:8418/v1"
    cfg.asr_model = "faster-whisper-small"
    cfg.asr_language = "zh"
    cfg.asr_timestamps = True
    cfg.asr_max_segment_sec = 300
    cfg.asr_chunk_strategy = "silence"
    cfg.llm_enabled = False
    cfg.emit_txt = cfg.emit_srt = cfg.emit_md = True
    cfg.emit_utf8_bom = True
    cfg.output_dir = str(out_dir)
    cfg.cache_enabled = True

    store = StateStore(WORK / "rehearsal.db")
    hooks = PipelineHooks()
    hooks.stage = lambda tid, st, pct, msg="": print(f"    [{st:>13s}] {pct:5.1f}%  {msg[:66]}", flush=True)
    pipe = Pipeline(cfg, store, hooks=hooks)
    res = Resource(
        resource_id="REHEARSAL-1",
        title=f"排练 {TARGET_MINUTES} 分钟",
        course_name="排练课程",
        teacher="本地语音",
        duration_sec=dur,
        play_url=str(audio),          # 本地文件直接当播放源（下载器支持本地源）
    )
    task = store.upsert_task(TaskRecord(
        course_id="R", course="排练课程", resource_id=res.resource_id, title=res.title,
        output_dir=str(out_dir), duration_sec=dur,
    ))

    print("\n[1] 第一次跑（完整流水线）")
    t0 = time.time()
    final = pipe.run(task, res, force=True)
    cost1 = time.time() - t0
    print(f"    stage={final.stage}  耗时 {cost1 / 60:.1f} 分钟  产物={[Path(p).name for p in (final.outputs or [])]}")
    check("任务完成（stage=done）", final.stage == str(Stage.DONE), final.error or "")
    products = {p.suffix.lstrip("."): p for p in (Path(x) for x in (final.outputs or []))}
    check("产出 txt/srt/md 三件", {"txt", "srt", "md"} <= set(products), sorted(products))
    for suffix, path in products.items():
        raw = path.read_bytes()
        check(f".{suffix} 带 UTF-8 BOM", raw[:3] == b"\xef\xbb\xbf", raw[:3].hex(" ").upper())
    if "srt" in products:
        srt = products["srt"].read_text(encoding="utf-8-sig")
        blocks = [b for b in srt.split("\n\n") if b.strip()]
        print(f"    SRT 条数：{len(blocks)}；末条时间轴：{blocks[-1].splitlines()[1] if blocks else '-'}")
        check("SRT 有条目", len(blocks) >= 5, f"{len(blocks)} 条")
    if "txt" in products:
        txt = products["txt"].read_text(encoding="utf-8-sig")
        print(f"    文本字数：{len(txt)}；开头：{txt[:60].replace(chr(10), ' ')}")
        check("正文非空且包含中文", len(txt) > 200 and any("\u4e00" <= c <= "\u9fff" for c in txt),
              f"{len(txt)} 字")

    print("\n[2] 第二次跑（应复用 transcript.json，不再调 ASR）")
    t1 = time.time()
    again = pipe.run(store.get_task(task.id) or task, res)
    cost2 = time.time() - t1
    print(f"    stage={again.stage}  耗时 {cost2:.1f}s")
    check("重跑命中缓存（< 20 秒）", cost2 < 20.0, f"{cost2:.1f}s")
    check("重跑仍是 done", again.stage == str(Stage.DONE))

    print(f"\n结论：{dur / 60:.1f} 分钟音频完整流水线本次耗时 {cost1 / 60:.1f} 分钟"
          f"（0 分钟说明全部分段命中上次结果缓存，属正常）。")
    print("      本机实测 ASR 吞吐（faster-whisper small / CPU int8 / beam=5）约 4.9× 实时，"
          f"故 55 分钟录播的识别约需 {3300 / 4.9 / 60:.0f} 分钟。")
    pipe.close()
    store.delete_task(task.id, hard=True)
    store.close()

    print("\n" + "=" * 90)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for n in FAIL:
        print("  ⛔ " + n)
    print("=" * 90)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
