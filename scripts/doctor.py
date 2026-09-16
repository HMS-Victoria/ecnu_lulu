"""打包产物自检：验证 exe 在**冻结态**下的关键能力。

在 ``dist\\大夏学堂转写助手\\`` 目录里运行（或直接运行打包好的 exe 时也能用）::

    "dist\\大夏学堂转写助手\\大夏学堂转写助手.exe" --doctor

检查项：
    1. 冻结态路径解析（resource_root / app_root / output / cache / logs）；
    2. 内嵌 ffmpeg / ffprobe 可执行（真实跑一次 ``-version`` 与一次转码）；
    3. DPAPI 可用（凭据能加密落盘）；
    4. sqlite 状态库可建可写；
    5. Playwright Chromium 是否可用（打包版**不内嵌** Chromium，缺失时给出明确指引）；
    6. 脱敏与配置读写；
    7. 产物编码：在冻结态真跑一次导出，断言三产物带 UTF-8 BOM（中文不乱码）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(ROOT / "src"))

PASS: list[str] = []
FAIL: list[str] = []
WARN: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '⛔'} {name}{('  — ' + detail) if detail else ''}")


def warn(name: str, detail: str = "") -> None:
    WARN.append(name)
    print(f"  ⚠️  {name}{('  — ' + detail) if detail else ''}")


def main() -> int:
    from ecnu_transcribe import __version__, media, paths
    from ecnu_transcribe.config import ConfigManager, dpapi_available
    from ecnu_transcribe.logbus import redact
    from ecnu_transcribe.store import Stage, StateStore, TaskRecord

    print("=" * 78)
    print(f"打包产物自检 v{__version__}（frozen={paths.is_frozen()}）")
    print("=" * 78)

    # 1) 路径
    print("\n[1] 路径解析")
    for key, value in paths.describe().items():
        print(f"      {key} = {value}")
    if paths.is_frozen():
        check("运行在打包产物中（frozen）", True, str(paths.app_root()))
    else:
        warn("当前是源码模式（frozen=False）", "要看打包效果请直接运行 dist 下的 exe --doctor")
    check("app_root 可写（output 已创建）", paths.output_dir().is_dir(), str(paths.output_dir()))
    check("cache/logs/data 目录就绪",
          all(p.is_dir() for p in (paths.media_cache_dir(), paths.log_dir(), paths.data_dir())))

    # 2) ffmpeg
    print("\n[2] 内嵌 ffmpeg / ffprobe")
    try:
        exe = media.find_ffmpeg()
        check("找到 ffmpeg", exe.is_file(), str(exe))
        ver = media.ffmpeg_version(exe)
        print(f"      {ver}")
        check("ffmpeg 版本可读", "ffmpeg version" in ver.lower(), ver[:60])
        probe = media.find_ffprobe(exe)
        check("找到 ffprobe（更精确的时长探测）", probe is not None, str(probe))
    except Exception as exc:  # noqa: BLE001
        check("找到 ffmpeg", False, f"{type(exc).__name__}: {exc}")
        exe = None

    if exe is not None:
        print("\n[2b] 真实跑一次转码（生成 2s 音频 → 抽 mp3 → 探测时长）")
        tmp = Path(tempfile.mkdtemp(prefix="ecnu-doctor-"))
        src = tmp / "tone.wav"
        out = tmp / "tone.mp3"
        try:
            media.run_ffmpeg(
                [str(exe), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=2", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", str(src)],
                timeout=60, check=True,
            )
            check("ffmpeg 生成测试音频", src.is_file() and src.stat().st_size > 1000,
                  f"{src.stat().st_size} bytes")
            media.convert_audio(src, out, fmt="mp3", bitrate="64k", sample_rate=16000, channels=1)
            check("ffmpeg 抽取音频（-vn -ac 1 -ar 16000 -c:a libmp3lame）",
                  out.is_file() and out.stat().st_size > 500, f"{out.stat().st_size} bytes")
            dur = media.duration_of(out)
            check("产出音频时长可探测", dur > 0, f"{dur:.2f}s")
        except Exception as exc:  # noqa: BLE001
            check("ffmpeg 真实转码", False, f"{type(exc).__name__}: {exc}")

    # 3) DPAPI
    print("\n[3] 凭据加密（Windows DPAPI）")
    check("DPAPI 可用", dpapi_available() is True, "不可用则凭据只留内存")

    # 4) 状态库
    print("\n[4] sqlite 状态库")
    try:
        store = StateStore(paths.data_dir() / "doctor.db")
        t = store.upsert_task(TaskRecord(course_id="doc", resource_id="r1", title="doctor"))
        store.update_stage(t.id, Stage.DONE, progress=100,
                           outputs=["a.txt", "a.srt", "a.md"])
        got = store.get_task(t.id)
        check("写入并读回任务", got is not None and got.stage == str(Stage.DONE))
        check("产物列表正确落库", got is not None and len(got.outputs) == 3, str(got.outputs if got else None))
        store.delete_task(t.id, hard=True)
        store.close()
        (paths.data_dir() / "doctor.db").unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        check("状态库读写", False, f"{type(exc).__name__}: {exc}")

    # 5) Playwright Chromium
    print("\n[5] Playwright Chromium（登录环节需要；打包版不内嵌浏览器）")
    print(f"      PLAYWRIGHT_BROWSERS_PATH = {__import__('os').environ.get('PLAYWRIGHT_BROWSERS_PATH', '(未设置)')}")
    try:
        from playwright.sync_api import sync_playwright

        ok = False
        detail = ""
        with sync_playwright() as pw:
            try:
                b = pw.chromium.launch(headless=True)
                b.close()
                ok = True
            except Exception as exc:  # noqa: BLE001
                detail = str(exc).splitlines()[0][:200]
        if ok:
            check("Chromium 可启动", True)
        else:
            if "Executable doesn't exist" in detail:
                warn(
                    "Chromium 未安装",
                    "只需装一次（全机共用）： .venv\\Scripts\\python -m playwright install chromium\n"
                    "        没有 Python 的机器可下载官方 Chromium 后设置环境变量 "
                    "PLAYWRIGHT_BROWSERS_PATH 指向其父目录",
                )
            else:
                warn("Chromium 不可用", detail)
    except ImportError as exc:
        warn("未安装 playwright", str(exc))

    # 6) 脱敏 / 配置
    print("\n[6] 脱敏与配置")
    check("Cookie 脱敏", "SECRETVALUE" not in redact("Cookie: S=SECRETVALUE; x=1"))
    check("token 脱敏", "abcdef123456" not in redact("access_token=abcdef123456"))
    check("学号脱敏", "20261234567" not in redact("学号 20261234567 已登录"))
    try:
        cm = ConfigManager()
        cfg = cm.load()
        cm.save(cfg)
        check("配置可读写", paths.config_path().is_file(), str(paths.config_path()))
    except Exception as exc:  # noqa: BLE001
        check("配置可读写", False, f"{type(exc).__name__}: {exc}")

    # 7) 产物编码（在**冻结态**里真跑一次导出，验字节头）
    #    这条检查的意义：编码问题只在「产物离开本程序之后」才暴露 ——
    #    中文 Windows 的 ANSI 代码页是 cp936，无 BOM 的 UTF-8 会被猜错成乱码。
    #    所以必须在打包产物里对**真实写出的字节**断言，而不是只信源码。
    print("\n[7] 产物编码（真实导出一次，验字节头）")
    try:
        from ecnu_transcribe.catalog import Resource
        from ecnu_transcribe.exporter import export_all
        from ecnu_transcribe.transcriber import Segment, Transcript

        res = Resource(resource_id="doctor", title="编码自检", course_name="自检")
        tr = Transcript(
            segments=[Segment(0.0, 3.0, "中文编码自检：同学们好，今天讲二叉树。")],
            duration_sec=3.0,
            model="doctor",
        )
        with tempfile.TemporaryDirectory(prefix="ecnu-doctor-") as tmp:
            out = export_all(res, tr, Path(tmp))
            heads = {p.suffix: p.read_bytes()[:3] for p in out.files}
            check(
                "三产物均带 UTF-8 BOM（Windows 记事本/字幕播放器不乱码）",
                len(heads) == 3 and all(v == b"\xef\xbb\xbf" for v in heads.values()),
                " / ".join(f"{k}={v.hex(' ').upper()}" for k, v in sorted(heads.items())),
            )
            # transcript.json 是机器可读产物，**有意**不带 BOM
            js = next(Path(tmp).rglob("*.transcript.json"))
            check(
                "transcript.json 不带 BOM（机器可读，避免解析器踩坑）",
                js.read_bytes()[:3] != b"\xef\xbb\xbf",
            )
            # 按 cp936 猜会读出错文 —— 用「带 BOM 时能被 UTF-8 读取器正确还原」反证
            txt = out.txt.read_text(encoding="utf-8-sig")
            check("正文可被 UTF-8 读取器正确还原", "二叉树" in txt, txt.strip()[:24])
    except Exception as exc:  # noqa: BLE001
        check("产物编码", False, f"{type(exc).__name__}: {exc}")

    print("\n" + "=" * 78)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败，{len(WARN)} 项提醒")
    for name in FAIL:
        print("  ⛔ " + name)
    for name in WARN:
        print("  ⚠️  " + name)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
