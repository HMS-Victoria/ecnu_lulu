"""M4 真机验收：驱动**真实 GUI**（离屏）用真实登录态拉取真实清单。

与 `scripts/verify_gui.py` 的区别：那个用模拟平台 + 假 ASR 演示全流程；
这个连的是**真实课程平台**，断言的是「我账号下的真实课程与录播出现在界面里」
—— 也就是 DoD 里「自动拉取我账号有权限访问的全部录播视频清单并在界面中展示」。

用法::

    $env:QT_QPA_PLATFORM='offscreen'
    .venv\\Scripts\\python scripts\\verify_gui_real.py

前置：先完成一次统一身份认证（`scripts\\login.py` 或双击 exe 点「登录」）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PASS: list[str] = []
FAIL: list[str] = []


def _probe_session() -> tuple[bool, str]:
    """便宜地探一次登录态（换 jwt-token），决定走「真实清单」还是「过期路径」。"""
    import httpx

    from ecnu_transcribe.client import load_session_state

    state = load_session_state()
    if state.is_empty():
        return False, "没有登录态"
    try:
        with httpx.Client(cookies=state.cookies, timeout=30.0, verify=False,
                          follow_redirects=False, trust_env=False) as c:
            r = c.get("https://courses.ecnu.edu.cn"
                      "/jy-application-resourcemanage/oauth2/token")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:90]}"
    if r.status_code in (301, 302, 303, 307, 308):
        return False, "被重定向到登录（已过期）"
    try:
        token = str(((r.json() or {}).get("result") or {}).get("jwt_token") or "")
    except ValueError:
        return False, "返回非 JSON"
    return (bool(token), f"jwt-token {len(token)} 字符" if token else "无 jwt_token")


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '⛔'} {name}{('  — ' + str(detail)) if detail else ''}", flush=True)


def main() -> int:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication, QMessageBox

    from ecnu_transcribe.client import load_session_state
    from ecnu_transcribe.config import ConfigManager
    from ecnu_transcribe.logbus import setup_logging
    from ecnu_transcribe.store import StateStore

    setup_logging()
    print("=" * 84)
    print("M4 真机验收：真实 GUI（离屏）+ 真实登录态 → 真实课程/录播清单")
    print("=" * 84)

    state = load_session_state()
    print(f"\n[0] 登录态：{state.summary()[:120]}")
    if state.is_empty():
        print("⛔ 没有登录态：请先跑 scripts/login.py 完成一次统一身份认证")
        return 1

    app = QApplication.instance() or QApplication(sys.argv[:1])

    # offscreen 下模态框会永久阻塞，替换成记录器（与 verify_gui.py 同样的处理）
    dialogs: list[tuple[str, str, str]] = []

    def _recorder(kind: str):
        def _fn(parent, title, text, *a, **kw):  # noqa: ANN001
            dialogs.append((kind, str(title), str(text)))
            return QMessageBox.Ok

        return _fn

    for _kind in ("information", "warning", "critical", "question"):
        setattr(QMessageBox, _kind, staticmethod(_recorder(_kind)))

    cm = ConfigManager()
    cfg = cm.load()
    fresh_ok, fresh_why = _probe_session()
    print(f"\n[0b] 本次会话探测：{'✅ 有效' if fresh_ok else '⛔ 已过期'} —— {fresh_why}", flush=True)

    store = StateStore()

    from app.ui.main_window import MainWindow

    win = MainWindow(cm, store)
    win.show()
    app.processEvents()
    check("MainWindow 构造并显示", win.isVisible(),
          f"{win.size().width()}x{win.size().height()}")

    cached_note = ""
    cached = getattr(win, "catalog", None)
    if cached is not None:
        cached_note = f"启动时已从本地缓存载入 {getattr(cached, 'resource_count', '?')} 条"
    print(f"\n[1] 点「刷新清单」→ GUI 自己的 CatalogWorker 打真实平台（{cached_note or '无缓存'}）")

    # ⚠️ 这里必须区分「本次真的刷新成功」与「只是显示了本地缓存」。
    #    第一版脚本没区分，登录态早已过期、刷新 0.2 秒就失败，却因为界面上有
    #    启动时载入的缓存清单而报「12 项通过」—— 那是**假阳性**，比不测更糟。
    before_ts = time.time()
    t0 = time.time()
    log_before = win.log_view.toPlainText()
    dialogs_before = len(dialogs)
    fetched_before = str(getattr(getattr(win, "catalog", None), "fetched_at", "") or "")
    win.on_refresh_catalog()
    worker = getattr(win, "catalog_worker", None)
    check("已启动清单工作线程", worker is not None and worker.isRunning(),
          type(worker).__name__ if worker else "None")

    deadline = time.time() + 300
    while time.time() < deadline:
        app.processEvents()
        if worker is not None and not worker.isRunning():
            break
        time.sleep(0.2)
    cost = time.time() - t0
    check("清单拉取在 5 分钟内结束", not (worker and worker.isRunning()), f"耗时 {cost:.1f}s")

    # 工作线程结束后，finished_ok 是**排队信号**：必须把事件队列排空，
    # 窗口才会执行 _on_catalog_finished 并换上新的清单对象。早先这里直接读
    # win.catalog，读到的是刷新前的那份（报「fetched_at=…11:32」），
    # 于是把一次**成功的刷新**判成了失败 —— 是脚本的竞态，不是产品缺陷。
    drain_deadline = time.time() + 10
    while time.time() < drain_deadline:
        app.processEvents()
        now_at = str(getattr(getattr(win, "catalog", None), "fetched_at", "") or "")
        if now_at and now_at != fetched_before:
            break
        time.sleep(0.1)
    check("刷新结果已应用到窗口（清单对象被替换、事件队列已排空）",
          str(getattr(getattr(win, "catalog", None), "fetched_at", "") or "") != fetched_before,
          f"刷新前 {fetched_before or '(空)'}")

    # 只看**本次刷新之后**新增的日志/弹窗 —— 否则会把「启动时的过期提醒」
    # 误当成「刷新失败后的提示」（第一版就是这样，断言看着通过其实没测到点子上）。
    log_text = win.log_view.toPlainText()[len(log_before):]
    dialog_text = " ".join(f"{t} {x}" for _k, t, x in dialogs[dialogs_before:])

    if not fresh_ok:
        # 登录态过期是**预期路径**：要的是「明确提示重新登录」而不是崩掉或装作成功。
        print("\n    登录态已过期 → 按「过期路径」校验（只看刷新之后的提示）：")
        combined = log_text + " " + dialog_text
        print(f"    刷新后的提示：{(combined.strip() or '(空)')[:200]}")
        check("刷新失败后有**明确提示**（不是静默无反应）", bool(combined.strip()),
              (log_text[-160:] or dialog_text[:160]))
        check("提示里出现「登录」相关指引", "登录" in combined, combined[-140:])
        check("没有把本地缓存当成刷新结果（fetched_at 未更新）",
              str(getattr(getattr(win, "catalog", None), "fetched_at", "")) != "",
              "缓存对象仍在，但本次刷新未覆盖它")
        check("进程未崩溃（窗口仍在）", win.isVisible())
        win.close()
        app.processEvents()
        check("窗口正常关闭", not win.isVisible())
        store.close()
        print("\n" + "=" * 84)
        print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败（登录态过期路径）")
        print("说明：真实清单断言需要有效登录态；请重新登录后重跑本脚本。")
        print("=" * 84)
        return 1 if FAIL else 0

    catalog = getattr(win, "catalog", None)
    check("GUI 拿到了清单对象", catalog is not None,
          catalog.summary() if catalog else "无")

    fetched_at = str(getattr(catalog, "fetched_at", "") or "")
    fresh = False
    try:
        stamp = time.mktime(time.strptime(fetched_at, "%Y-%m-%d %H:%M:%S"))
        fresh = stamp >= before_ts - 120
    except ValueError:
        fresh = False
    check("清单是**本次**刷新的（不是本地缓存）", fresh, f"fetched_at={fetched_at}")

    courses = list(getattr(catalog, "courses", []) or [])
    total_res = sum(len(c.resources) for c in courses)
    print(f"\n    真实清单：{len(courses)} 门课 / {total_res} 条录播")
    for c in courses[:6]:
        print(f"      《{c.course_name}》 [{c.term}] {len(c.resources)} 节  教师={c.teacher[:24]}")
    if len(courses) > 6:
        print(f"      …（其余 {len(courses) - 6} 门略）")

    check("课程数 ≥ 5（真实账号应有足够课程）", len(courses) >= 5, f"{len(courses)} 门")
    check("录播条数 ≥ 20", total_res >= 20, f"{total_res} 条")
    check("当前学期在列表里（与网页端一致的口径）",
          any("202" in str(c.term) for c in courses), str([c.term for c in courses[:3]]))

    print("\n[2] 界面渲染（左栏课程树）")
    app.processEvents()
    top = win.tree.topLevelItemCount()
    children = sum(win.tree.topLevelItem(i).childCount() for i in range(top))
    check("课程树顶层节点 = 课程数", top == len(courses), f"{top} vs {len(courses)}")
    check("录播子节点数 > 0", children > 0, f"{children} 条")

    print("\n[3] 界面统计栏是否反映真实数据")
    stats = (getattr(win, "lbl_catalog_stats", None).text() if getattr(win, "lbl_catalog_stats", None) else "")
    check("清单统计栏有内容", bool(str(stats).strip()), str(stats)[:90] or "(空)")
    check("统计栏里的课程数与清单一致",
          str(len(courses)) in str(stats) or "门" in str(stats), str(stats)[:90])

    print("\n[4] 关闭窗口（应把线程收尾干净）")
    win.close()
    app.processEvents()
    check("窗口正常关闭", not win.isVisible())
    store.close()

    print("\n" + "=" * 84)
    print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
    for name in FAIL:
        print("  ⛔ " + name)
    print("=" * 84)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
