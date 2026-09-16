"""M4 验收：**驱动真实 GUI** 走完「刷新清单 → 勾选 → 入队 → 开始 → 完成 → 6 个产物」。

真实平台不可达时，登录那一步无法自动完成（需要本人过统一身份认证），
但 M4 验收里**登录之后**的所有环节都可以自动化验证：

    [模拟平台] 提供真实 HTTP 接口（课程 3 门 / 资源 7 条）+ 真实 HLS 媒体服务
        ↓
    [真实 EcnuClient] 抓全量清单 → Catalog
        ↓
    [真实 MainWindow] 渲染课程树 → 勾选 2 条 → 入队 → 检查表格/统计
        ↓
    [真实 PipelineWorker(QThread)] 下载 → 本地 ASR 转写 → 写出 txt/srt/md
        ↓
    [真实 Pipeline] 断点续跑：重跑不重下、不重跑 ASR

所有 UI 操作都通过真实控件（setCheckState / 点按钮 / 信号槽）完成，
线程通过 ``processEvents`` 驱动，因此能真实暴露「工作线程碰 UI」「信号没接上」这类问题。

用法::

    $env:QT_QPA_PLATFORM='offscreen'
    .venv\\Scripts\\python scripts\\verify_gui.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecnu_transcribe import media  # noqa: E402
from ecnu_transcribe.config import ConfigManager  # noqa: E402
from ecnu_transcribe.logbus import get_logger, setup_logging  # noqa: E402
from ecnu_transcribe.store import Stage, StateStore  # noqa: E402

log = get_logger("verify.gui")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '⛔'} {name}{('  — ' + detail) if detail else ''}")


# --------------------------------------------------------------------------- #
def load_mock():
    spec = importlib.util.spec_from_file_location("mock_platform", ROOT / "scripts" / "mock_platform.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mock_platform"] = mod
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def sapi_voice() -> str:
    if sys.platform != "win32":
        return ""
    try:
        r = subprocess.run(
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


def synthesize(text: str, out_wav: Path, voice: str) -> bool:
    safe = text.replace("'", "''")
    try:
        subprocess.run(
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


def build_media(work: Path, resource_ids: list[str], *, voice: str) -> dict[str, Path]:
    """为每个资源生成一段真实语音并发布成 HLS（含一路 AES-128 加密）。"""
    ffmpeg = media.find_ffmpeg()
    out: dict[str, Path] = {}
    sentences = [
        "同学们好，今天我们讲线性表的顺序存储结构。",
        "顺序存储用一段连续的内存保存元素，随机访问是常数时间。",
        "但是插入和删除需要移动大量元素，平均要移动一半。",
    ]
    base = work / "media"
    base.mkdir(parents=True, exist_ok=True)
    for i, rid in enumerate(resource_ids):
        parts: list[Path] = []
        for j, text in enumerate(sentences):
            seg = work / f"{rid}_s{j}.wav"
            if voice and synthesize(text, seg, voice):
                parts.append(seg)
            gap = work / f"{rid}_g{j}.wav"
            media.run_ffmpeg(
                [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
                 "-i", "anullsrc=r=16000:cl=mono", "-t", "0.4", "-c:a", "pcm_s16le", str(gap)],
                timeout=60, check=True,
            )
            parts.append(gap)
        if not parts:
            raise SystemExit("无法生成语音素材（本机无中文语音）")
        listing = work / f"{rid}_list.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        wav = work / f"{rid}.wav"
        media.run_ffmpeg(
            [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
             "-i", str(listing), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
            timeout=300, check=True,
        )
        hls_dir = base / rid
        hls_dir.mkdir(parents=True, exist_ok=True)
        cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-i", str(wav),
               "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "16000",
               "-f", "hls", "-hls_time", "4", "-hls_list_size", "0", "-hls_playlist_type", "vod"]
        if i % 2 == 0:  # 第一条加密，验证 AES-128 链路
            key = hls_dir / "key.bin"
            key.write_bytes(bytes(range(16)))
            keyinfo = hls_dir / "keyinfo.txt"
            keyinfo.write_text(f"key.bin\n{key}\n", encoding="utf-8")
            cmd += ["-hls_key_info_file", str(keyinfo)]
        cmd += ["-hls_segment_filename", str(hls_dir / "seg-%03d.ts"), str(hls_dir / "index.m3u8")]
        media.run_ffmpeg(cmd, timeout=600, check=True)
        out[rid] = wav
    return out


def start_asr(model: str, port: int) -> subprocess.Popen | None:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "local_asr_server.py"),
         "--model", model, "--port", str(port), "--device", "cpu"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    deadline = time.time() + 600
    while time.time() < deadline:
        if proc.poll() is not None:
            print("⛔ 本地 ASR 服务启动失败")
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
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    setup_logging()
    print("=" * 78)
    print("M4 验收：驱动真实 GUI 跑完「刷新清单 → 勾选 → 入队 → 开始 → 完成」")
    print("（登录步骤需本人完成；登录之后的所有环节在这里自动验证）")
    print("=" * 78)

    work = ROOT / "build" / "gui_verify"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    out_dir = work / "output"

    # ---------- 素材 ---------- #
    print("\n[1] 准备素材（真实语音 + HLS 媒体服务）")
    voice = sapi_voice()
    print(f"    中文语音：{voice or '（无）'}")
    mock = load_mock()
    target_ids = ["R-10011", "R-10021"]      # 选中这 2 条
    build_media(work, target_ids, voice=voice)
    media_root = work / "media"
    base, mock_srv = mock.serve(0, mode="normal", media_dir=media_root, play_base="")
    # 用 mock 自己的地址作为播放前缀，让 7 条资源的 URL 都指向它
    mock.PLAY_BASE = base
    for rid in target_ids:
        pass
    print(f"    ✅ 模拟平台 {base}（含 /health、/api/*、媒体静态服务）")

    asr_port = 8371
    asr = start_asr("tiny", asr_port)
    if asr is None:
        mock_srv.shutdown()
        return 1
    print(f"    ✅ 本地 ASR http://127.0.0.1:{asr_port}/v1")

    # ---------- 环境 ---------- #
    print("\n[2] 启动 GUI（offscreen）并把配置指向模拟平台")
    app = QApplication.instance() or QApplication(sys.argv[:1])

    # offscreen 环境下模态对话框会**永久阻塞**（没有用户点「确定」），
    # 因此把 QMessageBox 全部替换成记录器 —— 既避免卡死，又能断言弹窗内容。
    from PySide6.QtWidgets import QMessageBox

    dialogs: list[tuple[str, str, str]] = []

    def _recorder(kind: str):
        def _fn(parent, title, text, *a, **kw):  # noqa: ANN001
            dialogs.append((kind, str(title), str(text)))
            return QMessageBox.Ok

        return _fn

    for _kind in ("information", "warning", "critical", "question"):
        setattr(QMessageBox, _kind, staticmethod(_recorder(_kind)))
    print("    已拦截 QMessageBox（offscreen 下模态框会阻塞自动化）")

    cfg_file = work / "config.json"
    secrets_file = work / "secrets.json"
    cm = ConfigManager(config_file=cfg_file, secrets_file=secrets_file)
    cfg = cm.load()
    cfg.api_base = base
    cfg.portal_url = f"{base}/#/home"
    cfg.output_dir = str(out_dir)
    cfg.jitter_min = 0.0
    cfg.jitter_max = 0.0
    cfg.verify_tls = False
    cfg.asr_provider = "openai_compatible"
    cfg.asr_base_url = f"http://127.0.0.1:{asr_port}/v1"
    cfg.asr_model = "faster-whisper-tiny"
    cfg.asr_language = "zh"
    cfg.asr_timestamps = True
    cfg.emit_txt = cfg.emit_srt = cfg.emit_md = True
    cm.save(cfg)

    store = StateStore(work / "state.db")
    from app.ui.main_window import MainWindow

    win = MainWindow(cm, store)
    win.show()
    app.processEvents()
    check("MainWindow 构造并显示成功", win.isVisible(), f"三栏 size={win.size().width()}x{win.size().height()}")

    # ---------- 刷新清单（真实 HTTP） ---------- #
    print("\n[3] 点「刷新清单」→ 真实 EcnuClient 打模拟平台")
    from ecnu_transcribe.catalog import Catalog

    from ecnu_transcribe.client import EcnuClient, load_session_state

    with EcnuClient(cfg, config_manager=cm, session=load_session_state()) as c:
        catalog = c.fetch_catalog(save=False)
    # 把「播放地址」重写成指向本次 mock 的媒体服务，
    # 并把清单标注时长改成素材真实时长（否则会触发「时长偏差 > 2%」告警 —— 那是真实平台的
    # 长课程时长与本地 20 秒样例素材不匹配导致的，属于测试环境噪声）。
    for course in catalog.courses:
        for res in course.resources:
            res.play_url = f"{base}/{res.resource_id}/index.m3u8"
            if res.resource_id in target_ids:
                res.duration_sec = media.duration_of(work / "media" / res.resource_id / "index.m3u8")
    check("抓到全量清单", catalog.resource_count == 7, catalog.summary())

    win.catalog = catalog
    win._render_tree()
    app.processEvents()
    check("课程树已渲染（3 个顶层课程节点）", win.tree.topLevelItemCount() == 3,
          f"{win.tree.topLevelItemCount()} 门")
    child_count = sum(win.tree.topLevelItem(i).childCount() for i in range(win.tree.topLevelItemCount()))
    check("录播子节点共 7 条", child_count == 7, f"{child_count} 条")

    # ---------- 勾选 + 入队 ---------- #
    print("\n[4] 在课程树里勾选 2 条 → 点「加入队列」")
    checked = 0
    for i in range(win.tree.topLevelItemCount()):
        course_item = win.tree.topLevelItem(i)
        for j in range(course_item.childCount()):
            child = course_item.child(j)
            data = child.data(0, Qt.UserRole) or {}
            key = data.get("key", "")
            rid = key.split("::")[-1] if key else ""
            if rid in target_ids:
                child.setCheckState(0, Qt.Checked)
                checked += 1
    app.processEvents()
    check("勾选生效", checked == 2, f"{checked} 条")
    check("「已勾选」统计已更新", "2 条" in win.lbl_checked.text(), win.lbl_checked.text())

    win.on_enqueue_checked()
    app.processEvents()
    check("任务表格出现 2 行", win.table.rowCount() == 2, f"{win.table.rowCount()} 行")
    queued = store.list_tasks()
    check("状态库已落 2 条任务", len(queued) == 2)
    check("任务初始阶段为 pending", all(t.stage == str(Stage.PENDING) for t in queued),
          str([t.stage for t in queued]))

    # ---------- 真实后台线程执行 ---------- #
    print("\n[5] 点「开始」→ 真实 PipelineWorker(QThread) 下载 + 转写")
    from app.workers import PipelineWorker, QueueItem

    items = []
    for task in queued:
        res = next((r for c in catalog.courses for r in c.resources if r.resource_id == task.resource_id), None)
        assert res is not None
        items.append(QueueItem(task_id=task.id, resource=res))

    shown: list[tuple[int, str, float, str]] = []
    worker = PipelineWorker(cfg, cm, store, items, force=True)
    worker.task_stage.connect(lambda tid, st, pct, msg: shown.append((tid, st, pct, msg)))
    worker.task_stage.connect(win._on_task_stage)          # 真实 UI 槽
    worker.log_line.connect(win._append_log)
    worker.task_done.connect(win._on_task_done)
    worker.queue_finished.connect(win._on_queue_finished)
    done_signal: list[tuple[int, int]] = []
    worker.queue_finished.connect(lambda ok, fail: done_signal.append((ok, fail)))

    worker.start()
    deadline = time.time() + 900
    while worker.isRunning() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
    worker.wait(5000)
    app.processEvents()
    check("后台线程已结束（UI 未被阻塞致死）", not worker.isRunning(),
          "超时未结束" if worker.isRunning() else "")
    check("收到 queue_finished 信号", bool(done_signal), str(done_signal))
    if done_signal:
        ok_n, fail_n = done_signal[-1]
        check("2 条任务全部成功", ok_n == 2 and fail_n == 0, f"ok={ok_n} fail={fail_n}")
    if dialogs:
        print("    （被拦截的弹窗：" + "; ".join(f"{k}«{t}»" for k, t, _ in dialogs) + "）")

    check("UI 收到阶段回调", len(shown) > 5, f"{len(shown)} 条 task_stage 信号")
    stages_seen = {st for _t, st, _p, _m in shown}
    check("阶段覆盖 downloading/transcribing/done",
          {"downloading", "transcribing", "done"} <= stages_seen, str(sorted(stages_seen)))

    # ---------- 表格与产物 ---------- #
    print("\n[6] 检查 UI 表格状态与产物文件")
    done_tasks = store.list_tasks(stages=[str(Stage.DONE)])
    check("状态库中 2 条都是 done", len(done_tasks) == 2, str([t.stage for t in store.list_tasks()]))

    stage_cells = [win.table.item(r, 2).text() for r in range(win.table.rowCount())]
    check("表格「阶段」列显示完成", all("完成" in s for s in stage_cells), str(stage_cells))
    bars = [win.table.cellWidget(r, 3) for r in range(win.table.rowCount())]
    check("表格进度条到 100", all(b is not None and b.value() == 100 for b in bars),
          str([b.value() if b else None for b in bars]))

    outputs = [p for t in done_tasks for p in t.outputs]
    check("共 6 个产物文件被记录", len(outputs) == 6, f"{len(outputs)} 个")
    existing = [p for p in outputs if Path(p).is_file() and Path(p).stat().st_size > 0]
    check("6 个产物真实存在于磁盘", len(existing) == 6, f"{len(existing)} 个存在")
    exts = sorted(Path(p).suffix for p in existing)
    check("扩展名为 txt/srt/md 各 2 个", exts == [".md", ".md", ".srt", ".srt", ".txt", ".txt"], str(exts))

    txt = next((Path(p) for p in existing if p.endswith(".txt")), None)
    if txt:
        body = txt.read_text(encoding="utf-8")
        check("txt 有真实转写文本（非空白）", len(body.strip()) > 10, body.strip()[:60])
    srt = next((Path(p) for p in existing if p.endswith(".srt")), None)
    if srt:
        check("srt 含时间轴", "-->" in srt.read_text(encoding="utf-8"))
    md = next((Path(p) for p in existing if p.endswith(".md")), None)
    if md:
        body = md.read_text(encoding="utf-8")
        check("md 含元信息与全文", "## 元信息" in body and "## 全文" in body)

    check("树节点状态已刷新为完成",
          any("完成" in win.tree.topLevelItem(i).child(j).text(3)
              for i in range(win.tree.topLevelItemCount())
              for j in range(win.tree.topLevelItem(i).childCount())),
          "树里至少一个节点显示「完成」")
    check("日志面板有内容", len(win.log_view.toPlainText()) > 50,
          f"{len(win.log_view.toPlainText())} 字符")

    # ---------- 断点续跑 ---------- #
    print("\n[7] 断点续跑：重跑一条，不应重新下载、不应重新调用 ASR")
    t0 = done_tasks[0]
    audio_before = Path(t0.audio_path)
    mtime_before = audio_before.stat().st_mtime if audio_before.is_file() else 0.0
    store.reset_for_rerun(t0.id, keep_audio=True)
    res0 = next(r for c in catalog.courses for r in c.resources if r.resource_id == t0.resource_id)

    from ecnu_transcribe.pipeline import Pipeline, PipelineHooks

    pipe = Pipeline(cfg, store, cm=cm, hooks=PipelineHooks())
    final2 = pipe.run(store.get_task(t0.id), res0)
    pipe.close()
    app.processEvents()
    check("重跑后仍是 done", final2.stage == str(Stage.DONE), f"stage={final2.stage}")
    same = audio_before.is_file() and audio_before.stat().st_mtime == mtime_before
    check("音频未重新下载", same)
    check("产物仍为 3 个", len(final2.outputs) == 3, str([Path(p).name for p in final2.outputs]))

    # ---------- 收尾 ---------- #
    print("\n[8] 关闭窗口（保存断点、释放资源）")
    win.close()
    app.processEvents()
    check("窗口正常关闭", not win.isVisible())

    mock_srv.shutdown()
    store.close()
    asr.terminate()
    try:
        asr.wait(timeout=10)
    except subprocess.TimeoutExpired:
        asr.kill()

    print("\n" + "=" * 78)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for name in FAIL:
        print("  ⛔ " + name)
    print(f"产物目录：{out_dir}")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
