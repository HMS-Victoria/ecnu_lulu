"""端到端跑一条录播：解析播放地址 → ffmpeg 取音频 → ASR → 写出 txt/srt/md。

用法::

    # 按清单序号（1-based）跑第 2 条
    .venv\\Scripts\\python scripts\\run_one.py --index 2

    # 按资源 ID 跑
    .venv\\Scripts\\python scripts\\run_one.py --resource-id 123456

    # 只跑本地已有音频（跳过下载，用于验证 ASR 与产物）
    .venv\\Scripts\\python scripts\\run_one.py --audio cache\\media\\xxx.mp3

    # 强制重跑（忽略缓存与既有产物）
    .venv\\Scripts\\python scripts\\run_one.py --index 1 --force

这是 M2/M3 验收的主要工具；GUI 走的是同一套 :class:`~ecnu_transcribe.pipeline.Pipeline`。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecnu_transcribe import paths  # noqa: E402
from ecnu_transcribe.catalog import Catalog, Resource  # noqa: E402
from ecnu_transcribe.client import load_session_state  # noqa: E402
from ecnu_transcribe.config import AppConfig, ConfigManager  # noqa: E402
from ecnu_transcribe.errors import (  # noqa: E402
    ApiChangedError,
    AsrNotConfiguredError,
    AuthExpiredError,
    DrmDetectedError,
    MediaError,
    SiteUnreachableError,
    TranscriptionError,
)
from ecnu_transcribe.logbus import get_logger, setup_logging  # noqa: E402
from ecnu_transcribe.pipeline import Pipeline, PipelineHooks  # noqa: E402
from ecnu_transcribe.store import Stage, StateStore, TaskRecord  # noqa: E402
from ecnu_transcribe.transcriber import create_transcriber  # noqa: E402


def pick_resource(catalog: Catalog, args) -> Resource:
    resources = catalog.resources
    if not resources:
        raise SystemExit("清单里没有任何资源；先运行 scripts/fetch_catalog.py")
    if args.resource_id:
        for res in resources:
            if res.resource_id == args.resource_id:
                return res
        raise SystemExit(f"清单里找不到 resource_id={args.resource_id}")
    idx = max(1, int(args.index or 1))
    if idx > len(resources):
        raise SystemExit(f"--index {idx} 超出范围（共 {len(resources)} 条）")
    return resources[idx - 1]


def run_audio_only(cfg: AppConfig, cm: ConfigManager, audio: Path, args) -> int:
    """跳过下载，直接对本地音频跑 ASR + 产物（用于验证 M3）。"""
    from ecnu_transcribe import media
    from ecnu_transcribe.exporter import export_all

    if not audio.is_file():
        print(f"⛔ 音频不存在：{audio}", file=sys.stderr)
        return 1
    duration = media.duration_of(audio)
    print(f"音频：{audio}（{duration:.1f}s）")

    progress = lambda pct, msg: print(f"  [{pct:5.1f}%] {msg}")  # noqa: E731
    transcriber = create_transcriber(cfg, cm=cm, on_progress=progress)
    print(f"ASR provider={getattr(transcriber, 'name', '?')} model={cfg.asr_model}")
    transcript = transcriber.transcribe(audio, duration_sec=duration)
    if transcript.is_empty():
        print("⛔ ASR 返回空结果", file=sys.stderr)
        return 1
    print(f"转写完成：{len(transcript.segments)} 段 / {transcript.char_count} 字")
    for seg in transcript.segments[:3]:
        print(f"  [{seg.start:7.2f}-{seg.end:7.2f}] {seg.text[:80]}")

    resource = Resource(
        resource_id=args.resource_id or "local-audio",
        title=args.title or audio.stem,
        course_name=args.course or "本地音频",
        duration_sec=duration,
    )
    out_dir = Path(args.output_dir) if args.output_dir else cfg.resolved_output_dir()
    result = export_all(
        resource, transcript, out_dir,
        emit_txt=cfg.emit_txt, emit_srt=cfg.emit_srt, emit_md=cfg.emit_md,
    )
    print("\n产物：")
    for p in result.files:
        print(f"  {p}  ({p.stat().st_size} bytes)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="端到端跑一条录播")
    ap.add_argument("--index", type=int, help="清单里的序号（1-based）")
    ap.add_argument("--resource-id", help="资源 ID")
    ap.add_argument("--audio", help="只跑本地音频（跳过下载）")
    ap.add_argument("--course", help="覆盖课程名（配合 --audio）")
    ap.add_argument("--title", help="覆盖标题（配合 --audio）")
    ap.add_argument("--output-dir", help="覆盖输出目录")
    ap.add_argument("--force", action="store_true", help="强制重跑（忽略缓存与既有产物）")
    args = ap.parse_args()

    setup_logging()
    log = get_logger("scripts.run_one")
    cm = ConfigManager()
    cfg = cm.load()

    if args.audio:
        try:
            return run_audio_only(cfg, cm, Path(args.audio), args)
        except AsrNotConfiguredError as exc:
            print(f"⛔ {exc}", file=sys.stderr)
            return 5
        except TranscriptionError as exc:
            print(f"⛔ 转写失败：{exc}", file=sys.stderr)
            return 6

    catalog_path = paths.catalog_path()
    if not catalog_path.is_file():
        print(f"⛔ 找不到清单 {catalog_path}；先运行 scripts/fetch_catalog.py", file=sys.stderr)
        return 1
    catalog = Catalog.load(catalog_path)
    resource = pick_resource(catalog, args)
    print(f"目标：{resource.course_name} / {resource.title}（{resource.duration_sec:.0f}s）")
    print(f"play_url：{'有' if resource.play_url else '无（将调用播放接口解析）'}")

    store = StateStore()
    task = store.upsert_task(
        TaskRecord(
            course=resource.course_name,
            course_id=resource.course_id,
            resource_id=resource.resource_id,
            title=resource.title,
            output_dir=args.output_dir or str(cfg.resolved_output_dir()),
            duration_sec=resource.duration_sec,
            play_url=resource.play_url,
            stage=str(Stage.PENDING),
        )
    )
    print(f"任务 ID：{task.id}（状态库 {store.db_path}）")

    hooks = PipelineHooks(
        on_stage=lambda tid, stage, pct, msg: print(f"  [{pct:5.1f}%] {stage:14s} {msg}"),
        on_log=lambda level, msg: print(f"  {level:7s} {msg}"),
    )
    pipeline = Pipeline(cfg, store, cm=cm, hooks=hooks)
    try:
        final = pipeline.run(task, resource, force=args.force)
    except AuthExpiredError as exc:
        print(f"\n⛔ 登录态失效：{exc}\n   请重新运行 scripts/login.py", file=sys.stderr)
        return 2
    except SiteUnreachableError as exc:
        print(f"\n⛔ 站点不可达：{exc}", file=sys.stderr)
        return 3
    except DrmDetectedError as exc:
        print(f"\n⛔ 检测到 DRM（不做绕过，已停止）：{exc}", file=sys.stderr)
        return 7
    except (MediaError, ApiChangedError) as exc:
        print(f"\n⛔ 处理失败：{exc}", file=sys.stderr)
        return 4
    finally:
        pipeline.close()
        store.close()

    print()
    print(f"最终阶段：{final.stage}  进度：{final.progress}%")
    if final.error:
        print(f"错误：{final.error}", file=sys.stderr)
    if final.stage == str(Stage.DONE):
        print("产物：")
        for p in final.outputs:
            path = Path(p)
            size = path.stat().st_size if path.is_file() else 0
            print(f"  {p}  ({size} bytes)")
        print(f"\n音频缓存：{final.audio_path}")
        print(f"转写缓存：{final.transcript_path}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
