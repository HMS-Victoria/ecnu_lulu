"""用**真实**那条近乎无声的录像，验证「不编造」这条底线（缺陷 45）。

素材：`cache/media/线性代数 2025-12-11 vod701828__VOD-701828.mp3`（25.18 MB，55 分钟）
—— 从学校平台真实拉下来的课堂音轨，实测峰值 -34 dB / mean -57 dB，
且实测「强行识别」会让 Whisper 输出不存在的模板文本（"字幕by索兰娅"）。

期望行为（修复后）：
    1. 电平策略判定 silent=True，**不放大**；
    2. 流水线**不调用 ASR**，直接以明确文案失败；
    3. 失败文案说明「是录像没录到声音，不是你的配置」；
    4. 不产生任何 .txt/.srt/.md（绝不交出幻觉产物）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from ecnu_transcribe.catalog import Resource  # noqa: E402
from ecnu_transcribe.config import ConfigManager  # noqa: E402
from ecnu_transcribe.logbus import setup_logging  # noqa: E402
from ecnu_transcribe import media  # noqa: E402
from ecnu_transcribe.pipeline import Pipeline, PipelineHooks  # noqa: E402
from ecnu_transcribe.store import Stage, StateStore, TaskRecord  # noqa: E402

AUDIO = next((ROOT / "cache" / "media").glob("*VOD-701828*.mp3"), None)
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '⛔'} {name}{('  — ' + str(detail)) if detail else ''}", flush=True)


def main() -> int:
    setup_logging()
    print("=" * 88)
    print("真实近无声录像 → 应当「诚实失败」，不产出幻觉文本")
    print("=" * 88)
    if AUDIO is None or not AUDIO.is_file():
        print("⛔ 找不到真实音频素材（cache/media/*VOD-701828*.mp3）")
        return 2
    print(f"\n素材：{AUDIO.name}  {AUDIO.stat().st_size/1024/1024:.2f} MB")

    vol = media.probe_volume(AUDIO)
    print(f"电平：mean={vol.get('mean_db')} dB  max={vol.get('max_db')} dB")
    # 判据是**平均电平**：这条素材的峰值其实不低（约 -24 dB，某处有响声），
    # 只看峰值会误判成「电平正常」——这正是本脚本要盯住的地方。
    check("素材本身确实是「近乎无声」（按平均电平判定）",
          (vol.get("mean_db") or 0) < media.SILENT_MEAN_DB,
          f"mean={vol.get('mean_db')} < {media.SILENT_MEAN_DB}；峰值 {vol.get('max_db')}（不代表有语音）")

    import shutil
    probe_copy = ROOT / "build" / "silent_probe.mp3"
    shutil.copy2(AUDIO, probe_copy)
    info = media.normalize_for_asr(probe_copy, fmt="mp3", bitrate="64k")
    print(f"\n电平策略：silent={info.get('silent')} changed={info.get('changed')}  {info.get('reason')}")
    check("判定为 silent 且**未放大**", info.get("silent") is True and info.get("changed") is False,
          str(info.get("reason")))
    after = media.probe_volume(probe_copy)
    check("文件未被改动（峰值不变）",
          abs((after.get("max_db") or 0) - (vol.get("max_db") or 0)) < 0.5,
          f"{vol.get('max_db')} → {after.get('max_db')}")

    print("\n跑流水线（音频命中缓存 → 应在送 ASR 之前停下）")
    cfg = ConfigManager().load()
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = "http://127.0.0.1:8418/v1"
    cfg.asr_model = "faster-whisper-small"
    cfg.llm_enabled = False
    cfg.output_dir = str(ROOT / "build" / "silent_out")
    cfg.cache_enabled = True
    store = StateStore(ROOT / "build" / "silent_state.db")
    hooks = PipelineHooks()
    hooks.stage = lambda tid, st, pct, msg="": print(f"    [{st}] {pct:.0f}% {msg[:70]}", flush=True)
    pipe = Pipeline(cfg, store, hooks=hooks)
    res = Resource(
        resource_id="VOD-701828",
        title="线性代数 2025-12-11 vod701828",
        course_name="线性代数",
        teacher="杨争峰",
        duration_sec=3301.0,
        play_url="https://dudaomedia.ecnu.edu.cn:40443/cached",
    )
    task = store.upsert_task(TaskRecord(
        course_id="11949", course="线性代数", resource_id=res.resource_id, title=res.title,
        output_dir=cfg.output_dir, duration_sec=res.duration_sec,
    ))
    final = pipe.run(task, res)
    pipe.close()

    print(f"\n结果：stage={final.stage}")
    print(f"错误信息：{(final.error or '')[:300]}")
    check("任务以失败结束（而不是「success」）", final.stage == str(Stage.FAILED), final.stage)
    err = final.error or ""
    check("错误文案说明了「是录像没录到声音」", "没有声音" in err or "没录到声音" in err, err[:120])
    check("错误文案点明「不是你的配置问题」", "不是你的配置" in err, err[:160])
    check("错误文案给出下一步（试听/换一条）", "试听" in err or "换一条" in err, err[:160])

    produced = list((ROOT / "build" / "silent_out").rglob("*.txt")) + \
        list((ROOT / "build" / "silent_out").rglob("*.srt")) + \
        list((ROOT / "build" / "silent_out").rglob("*.md"))
    check("**没有**产出任何 txt/srt/md（不交幻觉产物）", not produced,
          str([p.name for p in produced]))
    store.delete_task(task.id, hard=True)
    store.close()

    print("\n" + "=" * 88)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for n in FAIL:
        print("  ⛔ " + n)
    print("=" * 88)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
