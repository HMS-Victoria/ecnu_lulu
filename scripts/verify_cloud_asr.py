"""云端 ASR 冒烟验证：填完 Key 后先花 20 秒确认「能用」，再看「好不好」。

为什么需要它
------------
1. **免费鉴权检查**（`probe_asr_endpoint`）：只打 `/models`，不发音频、不计费。
   它能把「Key 错」「没权限」「网络/代理不通」区分开，并给出原始报文。
2. **同段对照试转**（可选 `--live`）：从**已缓存**的真实课堂录音里切一小段，
   分别用「你配置的云端 ASR」与「本机 faster-whisper」转一遍，并排打印。
   这样在花掉整节课的费用之前，就能看出中文课堂场景下云端是否明显更好
   （实测本地 small 在杂音段会出现繁体字与噪声串）。

音频来自 `cache/media/`（验收时已下好的完整音频），**不会重新下载**。

用法：
    python scripts/verify_cloud_asr.py                 # 只做免费鉴权检查
    python scripts/verify_cloud_asr.py --live          # 再做 20 秒同段对照
    python scripts/verify_cloud_asr.py --live --at 600 --seconds 30 --no-local
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecnu_transcribe import logbus, media, paths  # noqa: E402
from ecnu_transcribe.config import ConfigManager  # noqa: E402
from ecnu_transcribe.logbus import get_logger  # noqa: E402
from ecnu_transcribe.transcriber import (  # noqa: E402
    create_transcriber,
    probe_asr_endpoint,
)

log = get_logger("verify_cloud_asr")

LOCAL_BASE = "http://127.0.0.1:8418/v1"
LOCAL_MODEL = "faster-whisper-small"


def _mask(key: str) -> str:
    if not key:
        return "（空）"
    head = key[:4] if len(key) >= 4 else key
    return f"{head}…（共 {len(key)} 字符）"


def _pick_source(explicit: str) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    cands = sorted(paths.media_cache_dir().glob("*.mp3"), key=lambda p: p.stat().st_size)
    for p in reversed(cands):
        if p.stat().st_size > 5 * 1024 * 1024:  # 用长录音，保证某一秒一定在讲课
            return p
    return cands[-1] if cands else None


def _auto_offset() -> float:
    """从**已有转写稿**里挑一个确认有语音的位置，避免切到静音/噪声段。

    实测教训两连：
    ① 随便取第 300 秒切 8 秒，正好落在一段停顿上 —— 本机 ASR 返回 0 字，
       而脚本还报「成功」，对照完全失真；
    ② 只要求「≥15 字」时会挑到开场那种嘈杂又零碎的段落（云端只回一句「嗯。」），
       对照同样没意义。
    所以这里挑**最长**的那条分段（长文本≈连续讲课），并从它开始切。
    """
    import json

    best: tuple[int, float] | None = None
    for p in sorted((ROOT / "output").glob("*/*.transcript.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for seg in data.get("segments") or []:
            start = float(seg.get("start") or 0)
            text = str(seg.get("text") or "").strip()
            if start < 60 or len(text) < 15:
                continue
            if best is None or len(text) > best[0]:
                best = (len(text), start)
    return best[1] if best else 300.0


def _slice(src: Path, at: float, seconds: float) -> Path:
    out = ROOT / "build" / "smoke" / f"sample_{int(at)}_{int(seconds)}s.mp3"
    out.parent.mkdir(parents=True, exist_ok=True)
    media.run_ffmpeg(
        [str(media.find_ffmpeg()), "-hide_banner", "-nostdin", "-y",
         "-ss", f"{at:.1f}", "-t", f"{seconds:.1f}", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
         "-f", "mp3", str(out)],
        timeout=300, check=True,
    )
    return out


def _transcribe(cfg, cm, audio: Path, *, label: str) -> tuple[bool, str, float]:
    t0 = time.time()
    try:
        tr = create_transcriber(cfg, cm=cm)
    except Exception as exc:  # noqa: BLE001
        return False, f"构造转写器失败：{exc}", 0.0
    if getattr(tr, "name", "") in ("null", ""):
        return False, f"该 provider 不可用：{getattr(tr, 'detail', '')}", 0.0
    try:
        result = tr.transcribe(audio, duration_sec=media.duration_of(audio))
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", time.time() - t0
    text = " ".join(s.text.strip() for s in result.segments if s.text.strip())
    cost = time.time() - t0
    print(f"\n  【{label}】{len(result.segments)} 段 / {len(text)} 字 / 用时 {cost:.1f}s")
    print(f"    {text[:260]}{'…' if len(text) > 260 else ''}")
    if not text:
        # 空文本**不能**当成功：要么切到了静音，要么端点真的什么都没识别出来。
        # 早先这里返回 True，于是「0 字」被当成通过，对照毫无意义。
        print("    ⚠️ 这一段没有任何文字 —— 多半是切到静音了，用 --at 换一个位置再试")
        return False, "", cost
    return True, text, cost


def main() -> int:
    ap = argparse.ArgumentParser(description="云端 ASR 冒烟验证")
    ap.add_argument("--live", action="store_true", help="额外做一次真实音频试转（会计费，通常几分钱）")
    ap.add_argument("--seconds", type=float, default=20.0, help="试转时长（默认 20s）")
    ap.add_argument("--at", type=float, default=-1.0,
                    help="从音频第几秒开始切；默认 -1 = 自动从已有转写稿里挑一段有语音的位置")
    ap.add_argument("--source", default="", help="音频来源（默认取缓存里最长的一条真实录音）")
    ap.add_argument("--no-local", action="store_true", help="不跑本机对照")
    ap.add_argument("--local-base", default=LOCAL_BASE)
    args = ap.parse_args()
    if args.at is None or args.at < 0:
        args.at = _auto_offset()

    logbus.setup_logging()
    cm = ConfigManager()
    cfg = cm.load()
    key = cm.secret("asr_api_key")
    if key:
        logbus.register_secret(key)  # 兜底：任何日志里都不得出现明文

    print("=" * 84)
    print("云端 ASR 冒烟验证")
    print(f"  provider : {cfg.asr_provider}")
    print(f"  base_url : {cfg.asr_base_url}")
    print(f"  model    : {cfg.asr_model}")
    print(f"  API Key  : {_mask(key)}")
    print(f"  原生异步 : {bool(cfg.asr_use_native_api)}")
    print("=" * 84)

    print("\n[1] 免费鉴权检查（只打 /models，不发音频、不计费）")
    ok, msg = probe_asr_endpoint(cfg, key)
    print(f"    {'✅' if ok else '⛔'} {msg}")
    if not ok:
        print("\n下一步：打开应用 →「设置」→ 预设选 ②「阿里云百炼 DashScope（中文课堂最准，推荐）」")
        print("        → 在「API Key」框粘贴 Key → 保存 → 再按 F2「首启检查」看 ASR 一项是否 ✅")
        return 2

    if not args.live:
        print("\n（想再看质量对照，加 --live：会切 20 秒真实课堂音频，云端与本机各转一遍）")
        return 0

    src = _pick_source(args.source)
    if src is None:
        print("\n⛔ 没有可用的音频素材（cache/media 为空）。先跑一条录播再来。")
        return 1
    audio = _slice(src, args.at, args.seconds)
    print(f"\n[2] 同段对照试转：{audio.name}（{media.duration_of(audio):.1f}s，取自 {src.name} 第 {args.at:.0f}s）")

    ok_cloud, text_cloud, _c = _transcribe(cfg, cm, audio, label=f"云端 {cfg.asr_provider}/{cfg.asr_model}")
    if not ok_cloud:
        print("\n⛔ 云端试转失败。原因：")
        print(f"    {text_cloud or '(无错误文本)'}")
        print("\n  排查顺序：")
        print("    1) 模型名是否在当前账号可用 —— 跑本脚本不带 --live 会列出可用模型；")
        print("    2) 若模型是千问/Fun ASR 系列，应用会自动走 /chat/completions，无需手配；")
        print("    3) 若是连接被重置（WinError 10054），多为代理/网络抖动，重跑一次即可。")
        return 1

    if not args.no_local:
        import httpx

        base = args.local_base.rstrip("/")
        try:
            alive = httpx.get(f"{base}/models", timeout=5.0).status_code < 400
        except Exception:  # noqa: BLE001
            alive = False
        if alive:
            local_cfg = cm.load()
            local_cfg.asr_provider = "openai_compatible"
            local_cfg.asr_base_url = base
            local_cfg.asr_model = LOCAL_MODEL
            _transcribe(local_cfg, cm, audio, label=f"本机 {LOCAL_MODEL}")
        else:
            print(f"\n  （本机 ASR 未运行，跳过对照：先跑 scripts\\local_asr_server.py 再试）")

    print("\n结论：中文课堂场景下，若云端文本明显更少噪声/更少繁体字，就用它跑整节课；")
    print("      音频已缓存在 cache/media，换 ASR 重跑**不需要重新下载**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
