"""Playwright 人工登录 + 抓包 + storage_state 落盘（M0 侦察）。

设计底线（对应目标的「禁止事项」）
------------------------------------
* **不绕过任何身份验证**：不识别验证码、不爆破、不伪造票据。
  统一身份认证交给用户在**可见浏览器**里手动完成。
* 交互式登录用 ``launch_persistent_context(headless=False)``，
  持久化目录固定在 ``%LOCALAPPDATA%\\ecnu-transcribe\\browser``。
* 抓到的请求 / 响应先脱敏再写入 ``recon/network.jsonl``：
  Cookie / Authorization / token / 密码 / 手机号 / 学号一律替换为占位符。
* ``storage_state.json`` 只落 ``%LOCALAPPDATA%``，并已被 ``.gitignore`` 覆盖。

``BrowserBridge`` 是「动态签名」降级预案的实现：当某个接口必须由页面上下文
计算签名时，把请求交给已登录的浏览器页面代打，再把响应交回 httpx 侧继续处理。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import paths
from .config import AppConfig
from .errors import AuthExpiredError, SiteUnreachableError, TranscribeHelperError
from .logbus import get_logger, redact

log = get_logger("login")

#: 抓包时只记录这些资源类型（XHR / Fetch 才是接口）
_CAPTURE_TYPES = {"xhr", "fetch", "document"}

#: 这些**请求/响应头**的值一律整段替换（值本身看不出敏感，靠键名判断）
_SECRET_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-auth-token",
        "x-token",
        "x-access-token",
        "x-csrf-token",
        "x-xsrf-token",
        "token",
        "access-token",
        "refresh-token",
        "x-api-key",
        "api-key",
        "x-vpn-key",
    }
)


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """头部脱敏：按**键名**判定，敏感头整段替换（不依赖值里出现敏感词）。"""
    if not headers:
        return {}
    out: dict[str, str] = {}
    for key, value in headers.items():
        if str(key).lower() in _SECRET_HEADERS:
            out[key] = "<REDACTED>"
        else:
            out[key] = redact(value)
    return out


# --------------------------------------------------------------------------- #
@dataclass
class CapturedCall:
    """一条脱敏后的接口记录。"""

    method: str
    url: str
    status: int = 0
    resource_type: str = ""
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: str = ""
    started_at: float = 0.0
    duration_ms: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.started_at or time.time(),
            "method": self.method,
            "url": redact(self.url),
            "status": self.status,
            "resource_type": self.resource_type,
            "duration_ms": round(self.duration_ms, 1),
            "request_headers": redact_headers(self.request_headers),
            "request_body": redact(self.request_body)[:4000],
            "response_headers": redact_headers(self.response_headers),
            "response_body": redact(self.response_body)[:8000],
            "note": self.note,
        }


class NetworkRecorder:
    """把 CDP 事件落成 ``recon/network.jsonl``（脱敏 + 人工确认后可用）。"""

    def __init__(self, path: Path | None = None, *, capture_bodies: bool = True) -> None:
        self.path = Path(path) if path else (paths.recon_dir() / "network.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.capture_bodies = capture_bodies
        self.calls: list[CapturedCall] = []
        self._pending: dict[str, CapturedCall] = {}
        self._lock = threading.Lock()

    def record_request(self, req: Any) -> None:
        try:
            rtype = req.resource_type
            if rtype not in _CAPTURE_TYPES:
                return
            headers = {k: v for k, v in (req.headers or {}).items()}
            call = CapturedCall(
                method=req.method,
                url=req.url,
                resource_type=rtype,
                request_headers=headers,
                request_body=(req.post_data or "") if self.capture_bodies else "",
                started_at=time.time(),
            )
            with self._lock:
                self._pending[f"{req.method} {req.url}"] = call
        except Exception as exc:  # 抓包不能影响主流程
            log.debug("记录请求失败：%s", exc)

    def record_response(self, resp: Any) -> None:
        try:
            req = resp.request
            key = f"{req.method} {req.url}"
            with self._lock:
                call = self._pending.pop(key, None)
            if call is None:
                if req.resource_type not in _CAPTURE_TYPES:
                    return
                call = CapturedCall(method=req.method, url=req.url, resource_type=req.resource_type)
            call.status = resp.status
            call.response_headers = {k: v for k, v in (resp.headers or {}).items()}
            call.duration_ms = (time.time() - call.started_at) * 1000 if call.started_at else 0.0
            if self.capture_bodies:
                ctype = str(call.response_headers.get("content-type", "")).lower()
                if any(k in ctype for k in ("json", "text", "javascript", "xml")):
                    try:
                        call.response_body = resp.text()
                    except Exception:
                        call.response_body = ""
            self.append(call)
        except Exception as exc:
            log.debug("记录响应失败：%s", exc)

    def append(self, call: CapturedCall) -> None:
        with self._lock:
            self.calls.append(call)
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(call.to_dict(), ensure_ascii=False) + "\n")
            except OSError as exc:
                log.debug("写入抓包文件失败：%s", exc)

    def dump_summary(self, path: Path | None = None) -> Path:
        """汇总「疑似接口」清单，便于快速反推 API。"""
        target = Path(path) if path else (paths.recon_dir() / "api_candidates.json")
        interesting = [
            c.to_dict()
            for c in self.calls
            if c.resource_type in ("xhr", "fetch")
            and re.search(r"/(api|jy-application|resource|course|user|login|auth)/", c.url, re.I)
        ]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(interesting, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("接口候选已写入 %s（%s 条）", target, len(interesting))
        return target


# --------------------------------------------------------------------------- #
class LoginSession:
    """一次可见浏览器登录会话的封装（供 GUI 与命令行共用）。"""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        on_status: Callable[[str], None] | None = None,
        profile_dir: Path | None = None,
        storage_state_path: Path | None = None,
        headless: bool = False,
        record_network: bool = True,
        restore_saved_state: bool = True,
    ) -> None:
        self.cfg = cfg
        self.on_status = on_status or (lambda _m: None)
        self.profile_dir = Path(profile_dir) if profile_dir else paths.browser_profile_dir()
        self.storage_state_path = (
            Path(storage_state_path) if storage_state_path else paths.storage_state_path()
        )
        self.headless = headless
        self.record_network = record_network
        #: 打开浏览器时是否注入已保存的登录态（默认注入；测试可关）
        self.restore_saved_state = restore_saved_state
        self._pending_local_storage: dict[str, dict[str, str]] = {}
        self.recorder = NetworkRecorder() if record_network else None
        self._stop = threading.Event()
        self._browser = None
        self._context = None
        self._page = None
        self.saved = False
        self.last_error = ""

    # -- 生命周期 ----------------------------------------------------------- #
    def _status(self, message: str) -> None:
        log.info("%s", message)
        try:
            self.on_status(message)
        except Exception:
            pass

    def open(self) -> None:
        """启动持久化浏览器并挂上抓包（不阻塞）。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise TranscribeHelperError(
                "未安装 playwright。请执行：pip install -r requirements.txt && python -m playwright install chromium"
            ) from exc

        self._pw_ctx = sync_playwright()
        self._pw = self._pw_ctx.__enter__()
        self._status(f"启动 Chromium（用户目录：{self.profile_dir}）…")
        launch_kwargs: dict[str, Any] = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
                # 缺陷 48：实测窗口曾被窗口管理器摆到 (-25600,-25600) 且最小化 ——
                # 用户根本看不见它，只好去自己的浏览器里登录，而那是**另一个 profile**，
                # 登录态传不过来，于是变成「我明明登录了，你这儿却说没登录」。
                # 显式给一个屏幕内的位置与尺寸，是最省事的一道保险。
                "--window-position=60,60",
                "--window-size=1440,900",
                # 上一次异常退出后 Chromium 会弹「要恢复页面吗？」气泡，挡住右上角；
                # 登录窗口只需要那一张页面，不要这类干扰。
                "--hide-crash-restore-bubble",
            ],
            ignore_default_args=["--enable-automation"],
        )
        if self.cfg.proxy:
            launch_kwargs["proxy"] = {"server": self.cfg.proxy}
        elif self.cfg.extra.get("browser_no_proxy"):
            launch_kwargs["proxy"] = {"server": "direct://"}

        self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        self._context.set_default_timeout(20000)

        if self.recorder is not None:
            self._context.on("request", self.recorder.record_request)
            self._context.on("response", self.recorder.record_response)

        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

        # 先注入已保存的登录态再导航：Chromium 用户目录不保留会话级 Cookie，
        # 不注入的话「明明登录过」也会被弹回统一身份认证页（缺陷 41）。
        if self.restore_saved_state:
            try:
                self.restore_storage_state()
            except Exception as exc:  # noqa: BLE001
                log.warning("注入登录态失败（继续走正常登录）：%s", exc)

        self._status(f"打开入口：{self.cfg.portal_url}")
        try:
            self._page.goto(self.cfg.portal_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            self._status(f"入口加载告警（可忽略，请手动继续）：{exc}")

        # localStorage 必须在页面处于该 origin 时写入；写完后重载一次让前端读取
        if self.restore_saved_state and self.apply_local_storage():
            try:
                self._page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:  # noqa: BLE001
                self._status(f"写入登录态后重载告警（可忽略）：{exc}")

        self.ensure_window_visible()

    def ensure_window_visible(self) -> None:
        """把登录窗口拉回可见区域并置前（缺陷 48）。

        实测：窗口被摆到 ``(-25600, -25600)``（Windows 对**最小化**窗口报的哨兵坐标）且
        宽度只剩几十像素 —— 用户根本看不到。用户找不到窗口就去自己的浏览器登录，
        而那是**另一个 profile**，登录态传不过来，于是双方都觉得莫名其妙：
        用户说「我明明登录了啊」，程序说「你没登录」。

        ⚠️ **不能只靠 JS**：这台机器上 ``window.screenX`` 与实际窗口矩形不一致
        （JS 报 x=60，user32 报 x=-25600），``window.moveTo()`` 对主窗口也常被忽略。
        所以主力是 :meth:`_move_window_os_level`（直接对窗口句柄调 user32）。
        """
        page = self._page
        if page is None:
            return
        pos: dict[str, Any] = {}
        try:
            pos = page.evaluate(
                "() => ({x: window.screenX, y: window.screenY,"
                " w: window.outerWidth, h: window.outerHeight,"
                " sw: (window.screen && window.screen.availWidth) || 0,"
                " sh: (window.screen && window.screen.availHeight) || 0})"
            ) or {}
        except Exception as exc:  # noqa: BLE001
            log.debug("读取窗口位置失败：%s", exc)
        moved = self._move_window_os_level()
        try:
            page.bring_to_front()
        except Exception as exc:  # noqa: BLE001
            log.debug("置前失败：%s", exc)
        log.info(
            "登录窗口：JS 报 x=%s y=%s；OS 层修正%s",
            pos.get("x"), pos.get("y"), ("：" + moved) if moved else "（无需调整）",
        )
        if moved:
            self._status(f"登录窗口原先在屏幕外/被最小化，已移回可见区域（{moved}）")

    def _move_window_os_level(self) -> str:
        """用 user32 把 Chromium 窗口还原、移到 (60,60)、置前。返回做了什么（空串=没动）。

        只认「类名 ``Chrome_WidgetWin_1`` + 标题里带 ``Chromium``」的窗口 ——
        Playwright 那份 Chromium 的标题是 ``… - Chromium``，而用户自己的 Edge/Firefox
        标题不会长这样，所以不会误动用户的浏览器。
        """
        if os.name != "nt":  # pragma: no cover - 只在 Windows 上有意义
            return ""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            SW_RESTORE, SW_SHOWMAXIMIZED = 9, 3
            HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = -1, 0x0002, 0x0001, 0x0040
            EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            hits: list[int] = []

            def _cb(hwnd, _lparam):  # noqa: ANN001
                cls = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(hwnd, cls, 128)
                if cls.value != "Chrome_WidgetWin_1":
                    return True
                title = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title, 256)
                if "Chromium" in title.value:
                    hits.append(int(hwnd))
                return True

            user32.EnumWindows(EnumProc(_cb), 0)
            if not hits:
                return ""
            hwnd = hits[0]
            rect = wintypes.RECT()
            user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
            width = rect.right - rect.left
            iconic = bool(user32.IsIconic(wintypes.HWND(hwnd)))
            if not iconic and rect.left > -200 and rect.top > -200 and width >= 800:
                return ""
            user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
            user32.MoveWindow(wintypes.HWND(hwnd), 60, 60, 1400, 880, True)
            # **不最大化、不置顶**：用户的明确要求是"别挡着我看任务进度"。
            # 这里只把它带到前台（SetForegroundWindow 足够让人看见），
            # 之前这行还有 SW_SHOWMAXIMIZED + SetWindowPos(HWND_TOPMOST)，正是被要求去掉的行为。
            user32.SetForegroundWindow(wintypes.HWND(hwnd))
            after = wintypes.RECT()
            user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(after))
            return (f"最小化={iconic} ({rect.left},{rect.top}) {width}px → "
                    f"({after.left},{after.top})，已移回屏幕内（不置顶、不最大化）")
        except Exception as exc:  # noqa: BLE001 - 窗口修饰失败不该影响登录本身
            log.debug("OS 层移动窗口失败：%s", exc)
            return ""

    def wait_for_login(
        self,
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
        should_stop: Callable[[], bool] | None = None,
    ) -> bool:
        """轮询等待用户完成统一身份认证，成功后保存 storage_state。

        判定「已登录」的信号（任一命中即可）：
            1. URL 回到 courses.ecnu.edu.cn 且不再含 login/oauth/cas 关键字；
            2. 页面里出现课程 / 资源相关的中文关键词或平台容器节点；
            3. localStorage 里出现 token 类键。
        """
        assert self._page is not None, "请先调用 open()"
        deadline = time.time() + timeout
        self._status("请在打开的浏览器窗口里完成统一身份认证（学号 + 密码 / 可能的验证码）。")
        self._status("登录成功后本应用会自动保存登录态；也可以点「我已登录完成」手动确认。")
        last_url = ""
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            if should_stop and should_stop():
                return False
            try:
                current = (self._page.url or "") if self._page else ""
                if current and current != last_url:
                    last_url = current
                    self._status(f"当前页面：{redact(current)[:140]}")
                if self._detect_logged_in():
                    self.save_storage_state()
                    self._status("✅ 已检测到登录态，已保存 storage_state.json")
                    return True
            except Exception as exc:
                log.debug("登录态检测异常：%s", exc)
            time.sleep(poll_interval)
        self.last_error = self._diagnose_stuck(last_url, timeout)
        self._status("⛔ " + self.last_error)
        return False

    def _diagnose_stuck(self, last_url: str, timeout: float) -> str:
        """超时未登录时，给一句能直接照做的诊断，而不是干巴巴的「超时」。

        ⚠️ 这里必须**按主机 + 路径判断，不能对整条 URL 做子串匹配**。
        实测踩过的坑（缺陷 37）：校外访问时，跳转链是

            courses.ecnu.edu.cn → proxy.ecnu.edu.cn/vpn_key/update → /users/sign_in
              → api.ecnu.edu.cn/oauth2/authorize → sso.ecnu.edu.cn/login

        而 `sso.ecnu.edu.cn/login?service=...redirect_uri=https%3A%2F%2Fproxy.ecnu.edu.cn%2Fecnu_oauth2`
        **本身就把 `proxy.ecnu.edu.cn` 编码在 query 里**。于是子串匹配会把
        「老老实实停在登录页等你输密码」误报成「被 webVPN 网关拦下」，
        把用户指去折腾网络 —— 而真正该做的只是把密码输进去。
        **`vpn_key` 跳转是校外访问的正常一环，不是拦截。**
        """
        base = f"等待登录超时（{timeout:.0f}s）。"
        parts = urlsplit(last_url or "")
        host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
        path = (parts.path or "").lower()
        body = self._body_snippet().lower()

        # 1) 真正的拦截：停留页**本身就是网关**，且页面上写明了站点不存在 / 无权限
        gateway_markers = (
            "site not found", "站点不存在", "资源不存在", "无权限访问", "无法访问该站点",
        )
        if host.endswith("proxy.ecnu.edu.cn") and any(m in body for m in gateway_markers):
            return (
                base + " 浏览器停在 webVPN 网关的错误页（站点不存在 / 无权限）。\n"
                "处理：先接入校园网，或连上学校 SSL-VPN（https://vpn.ecnu.edu.cn/portal/）后再重新登录。"
            )

        # 2) 停在统一身份认证页 —— 绝大多数情况就是「没输完 / 没提交」
        if host.endswith("sso.ecnu.edu.cn") or any(
            k in path for k in ("/login", "/sign_in", "/signin", "/oauth", "/cas", "/authorize")
        ):
            return (
                base + " 浏览器还停在统一身份认证页面：页面已经打开，但没有完成提交。\n"
                "处理：重新点「登录」，在窗口里填学号 + 密码并点登录按钮；\n"
                "      如出现图形验证码 / 短信二次验证，请手动完成（本工具不做识别、不做绕过）。"
            )
        if not last_url:
            return base + " 浏览器窗口可能被关闭了。请重新点「登录」。"
        if host.endswith("proxy.ecnu.edu.cn"):
            return (
                base + " 浏览器停在 webVPN 网关（proxy.ecnu.edu.cn）。\n"
                "处理：在窗口里完成 webVPN 的登录/授权；若反复回到本页，请先接入校园网或 SSL-VPN。"
            )
        return base + f" 最后停留的页面：{redact(last_url)[:160]}"

    def _body_snippet(self, limit: int = 4000) -> str:
        """读当前页面可见文本；任何异常都退化为空串（诊断不能因为读不到就崩）。"""
        page = self._page
        if page is None:
            return ""
        try:
            return (page.inner_text("body", timeout=3000) or "")[:limit]
        except Exception:
            return ""

    def _detect_logged_in(self) -> bool:
        page = self._page
        if page is None:
            return False
        url = page.url or ""
        parts = urlsplit(url)
        host = (parts.netloc or "").split("@")[-1].split(":")[0].lower()
        path = (parts.path or "").lower()

        # 认证域 = 一定还没登录进去。注意：**不能**拿 `vpn_key` 这种 query 关键字判断 ——
        # 校外 webVPN 会在平台 URL 上带 `vpn_key`，那样会把「已登录成功」永久判成未登录。
        auth_hosts = ("sso.ecnu.edu.cn", "api.ecnu.edu.cn", "proxy.ecnu.edu.cn", "login.ecnu.edu.cn")
        if any(host == h or host.endswith("." + h) for h in auth_hosts):
            return False
        if any(k in path for k in ("/login", "/sign_in", "/signin", "/oauth", "/cas", "/authorize")):
            return False

        on_site = host.endswith("courses.ecnu.edu.cn") or host.endswith(".ecnu.edu.cn")
        body_text = self._body_snippet()
        markers = ("录播", "课程", "资源管理", "我的课程", "回放", "章节", "资源列表", "转写")
        ui_hits = sum(1 for m in markers if m in body_text)
        try:
            ls_count = page.evaluate(
                "() => { try { return Object.keys(window.localStorage||{}).filter("
                "k => /token|jwt|ticket|auth/i.test(k)).length } catch(e) { return 0 } }"
            )
        except Exception:
            ls_count = 0
        return on_site and (ui_hits >= 1 or ls_count > 0)

    def save_storage_state(self, path: Path | None = None) -> Path:
        assert self._context is not None, "请先调用 open()"
        target = Path(path) if path else self.storage_state_path
        target.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(target))
        self.saved = True
        # 立刻收紧权限（同机其它用户不可读）
        try:
            import os
            import stat

            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
        cookie_count = len(_read_cookie_names(target))
        self._status(f"登录态已保存：{target}（Cookie {cookie_count} 个）")
        return target

    def restore_storage_state(self, path: Path | None = None) -> int:
        """把 ``storage_state.json`` 里的 Cookie / localStorage **载回浏览器上下文**。

        为什么必须有这个方法（第 24 轮实测缺陷 41）：
        Chromium 的持久化用户目录**不会**跨进程保留会话级 Cookie
        （``_webvpn_key`` 这类没有 Expires 的 Cookie 一关浏览器就没了），
        而 ``storage_state.json`` 是 Playwright 导出的**完整**快照（含会话 Cookie）。
        结果是：用户明明刚登录成功、httpx 侧用得好好的，
        再点一次「登录」却被弹回统一身份认证页要求重新认证 —— 用户完全无法理解。

        现在打开浏览器时会先注入已保存的登录态，能直接用就不必再认证。
        返回注入的 Cookie 条数（0 表示没有可用登录态）。
        """
        assert self._context is not None, "请先调用 open()"
        target = Path(path) if path else self.storage_state_path
        if not target.is_file():
            return 0
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("登录态文件无法解析，跳过注入：%s", exc)
            return 0
        if not isinstance(data, dict):
            return 0

        raw_cookies = data.get("cookies")
        cookies: list[dict[str, Any]] = []
        for item in raw_cookies if isinstance(raw_cookies, list) else []:
            if not isinstance(item, dict):
                continue
            name, value = item.get("name"), item.get("value")
            domain = item.get("domain") or ""
            if not name or value is None or not domain:
                continue
            cookie: dict[str, Any] = {
                "name": str(name),
                "value": str(value),
                "domain": str(domain),
                "path": str(item.get("path") or "/"),
            }
            expires = item.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                cookie["expires"] = float(expires)
            if isinstance(item.get("httpOnly"), bool):
                cookie["httpOnly"] = item["httpOnly"]
            if isinstance(item.get("secure"), bool):
                cookie["secure"] = item["secure"]
            same_site = item.get("sameSite")
            if same_site in ("Strict", "Lax", "None"):
                cookie["sameSite"] = same_site
            cookies.append(cookie)

        added = 0
        if cookies:
            try:
                self._context.add_cookies(cookies)
                added = len(cookies)
            except Exception as exc:  # noqa: BLE001
                log.warning("注入 Cookie 失败（继续，用户可手动登录）：%s", exc)

        # localStorage：必须等页面处于对应 origin 才能写，所以先写入待办，
        # 由 apply_local_storage() 在导航后执行。
        origins = data.get("origins")
        pending: dict[str, dict[str, str]] = {}
        for origin in origins if isinstance(origins, list) else []:
            if not isinstance(origin, dict):
                continue
            url = str(origin.get("origin") or "")
            items = origin.get("localStorage")
            kv = {
                str(it["name"]): str(it["value"])
                for it in (items if isinstance(items, list) else [])
                if isinstance(it, dict) and "name" in it and "value" in it
            }
            if url and kv:
                pending[url] = kv
        self._pending_local_storage = pending

        if added or pending:
            self._status(
                f"已注入保存的登录态：Cookie {added} 个、localStorage {len(pending)} 个来源"
                "（若仍要求登录，说明会话已过期，请重新认证）"
            )
        return added

    def apply_local_storage(self) -> int:
        """把待写入的 localStorage 写进当前页面（导航后调用）。"""
        page = self._page
        pending = getattr(self, "_pending_local_storage", None) or {}
        if page is None or not pending:
            return 0
        current = (page.url or "").split("#")[0]
        written = 0
        for origin, kv in pending.items():
            base = origin.rstrip("/")
            if not current.startswith(base):
                continue
            try:
                page.evaluate(
                    """(items) => { for (const [k, v] of Object.entries(items)) {
                        try { window.localStorage.setItem(k, v); } catch (e) {}
                    } }""",
                    kv,
                )
                written += len(kv)
            except Exception as exc:  # noqa: BLE001
                log.debug("写 localStorage 失败：%s", exc)
        return written

    def confirm_logged_in(self) -> bool:
        """用户在 GUI 上点「我已登录完成」时调用。"""
        if self._page is None:
            return False
        try:
            ok = self._detect_logged_in()
        except Exception:
            ok = False
        if ok:
            self.save_storage_state()
            return True
        # 即使用户页面形态不匹配，也保存一次（cookie 可能是有效的）
        try:
            self.save_storage_state()
        except Exception as exc:
            self._status(f"保存登录态失败：{exc}")
            return False
        self._status("已保存当前登录态（页面未完全匹配课程页特征，若后续拉取失败请重新登录）。")
        return True

    def close(self, *, keep_open: bool = False) -> None:
        self._stop.set()
        if self.recorder is not None:
            try:
                self.recorder.dump_summary()
            except Exception:
                pass
        if keep_open:
            return
        try:
            if self._context is not None:
                self._context.close()
        except Exception as exc:
            log.debug("关闭浏览器上下文异常：%s", exc)
        try:
            if getattr(self, "_pw_ctx", None) is not None:
                self._pw_ctx.__exit__(None, None, None)
        except Exception:
            pass
        self._context = None
        self._page = None
        self._status("浏览器已关闭")

    def request_stop(self) -> None:
        self._stop.set()

    # -- 交互能力（供 GUI 调用）-------------------------------------------- #
    def bring_to_front(self) -> None:
        if self._page is not None:
            try:
                self._page.bring_to_front()
            except Exception:
                pass

    def goto(self, url: str) -> None:
        if self._page is not None:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def probe_api_in_page(self, url: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
        """在**已登录页面上下文内**发请求（借浏览器环境算签名）。

        返回 ``{"status": int, "body": str}``。用于动态签名 / 加密参数的降级方案。
        """
        if self._page is None:
            raise AuthExpiredError("浏览器会话未启动")
        script = """
        async ([url, method, body]) => {
            const opts = { method, credentials: 'include', headers: {'Accept': 'application/json, text/plain, */*'} };
            if (body !== null) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
            try {
                const r = await fetch(url, opts);
                const t = await r.text();
                return { status: r.status, body: t };
            } catch (e) { return { status: -1, body: String(e) }; }
        }
        """
        return self._page.evaluate(script, [url, method, body])


def _read_cookie_names(path: Path) -> list[str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [str(c.get("name", "")) for c in data.get("cookies", []) if c.get("name")]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
def interactive_login(
    cfg: AppConfig,
    *,
    timeout: float = 600.0,
    on_status: Callable[[str], None] | None = None,
    headless: bool = False,
) -> bool:
    """同步版本：开浏览器 → 等用户登录 → 保存登录态 → 关浏览器。

    命令行用法::

        python scripts/login.py
    """
    sess = LoginSession(cfg, on_status=on_status, headless=headless)
    try:
        sess.open()
        ok = sess.wait_for_login(timeout=timeout)
        return ok
    except SiteUnreachableError:
        raise
    finally:
        sess.close()


def check_session_alive(cfg: AppConfig) -> tuple[bool, str]:
    """快速检查 storage_state 是否还能用（不弹浏览器）。"""
    from .client import EcnuClient, load_session_state

    state = load_session_state()
    if state.is_empty():
        return False, "尚未登录（找不到 storage_state.json）"
    with EcnuClient(cfg, session=state) as client:
        try:
            diag = client.diagnose_access()
        except TranscribeHelperError as exc:
            return False, str(exc)
    if not diag.reachable:
        return False, diag.detail or diag.mode
    if diag.mode == "requires_login":
        return False, "登录态已失效，需要重新登录"
    return True, f"登录态看起来可用（{diag.mode}）；{state.summary()}"


# --------------------------------------------------------------------------- #
# 首启一键诊断：把「所有前置条件」一次查完，并明确告诉用户下一步做什么
# --------------------------------------------------------------------------- #
@dataclass
class ReadinessItem:
    """一项前置条件检查结果。"""

    name: str
    ok: bool
    detail: str = ""
    action: str = ""          # 失败时建议的动作
    blocking: bool = True     # 是否阻塞主流程

    @property
    def icon(self) -> str:
        if self.ok:
            return "✅"
        return "⛔" if self.blocking else "⚠️"


@dataclass
class ReadinessReport:
    items: list[ReadinessItem] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", action: str = "", *, blocking: bool = True) -> None:
        self.items.append(ReadinessItem(name, ok, detail, action, blocking))

    @property
    def ready(self) -> bool:
        return all(i.ok for i in self.items if i.blocking)

    def next_action(self) -> str:
        """给出「现在最该做的那一件事」，避免用户面对一堆红叉不知从哪下手。"""
        for item in self.items:
            if not item.ok and item.blocking:
                return item.action or f"请先处理：{item.name}"
        for item in self.items:
            if not item.ok:
                return item.action or f"建议处理：{item.name}"
        return "各项前置条件就绪，可以点「刷新清单」拉取课程与录播了。"

    def to_text(self) -> str:
        lines = ["【首启一键诊断】"]
        for item in self.items:
            lines.append(f"  {item.icon} {item.name}：{item.detail or ('正常' if item.ok else '未通过')}")
            if not item.ok and item.action:
                lines.append(f"      → {item.action}")
        lines.append("")
        lines.append("下一步：" + self.next_action())
        return "\n".join(lines)


def run_readiness_check(cfg: AppConfig, *, cm: object | None = None, deep: bool = True) -> ReadinessReport:
    """首启一键诊断：输出目录 / ffmpeg / Chromium / 站点可达性 / 登录态 / ASR 端点。

    ``deep=False`` 时跳过联网的 ASR 自检（离线环境快速看一眼）。
    """
    from . import media

    report = ReadinessReport()

    # 1) 输出目录可写
    try:
        out = cfg.resolved_output_dir()
        probe = out / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        report.add("输出目录可写", True, str(out))
    except Exception as exc:  # noqa: BLE001
        report.add("输出目录可写", False, str(exc)[:140], "到「设置 → 输出与产物」换一个目录")

    # 2) ffmpeg
    try:
        exe = media.find_ffmpeg(cfg.ffmpeg_path)
        report.add("ffmpeg 可用", True, str(exe))
    except Exception as exc:  # noqa: BLE001
        report.add(
            "ffmpeg 可用", False, str(exc)[:140],
            "装一个 ffmpeg（winget install Gyan.FFmpeg），或在「设置 → 下载与媒体」指定 ffmpeg.exe",
        )

    # 3) Chromium（登录需要）
    try:
        from playwright.sync_api import sync_playwright

        ok, detail = False, ""
        with sync_playwright() as pw:
            try:
                b = pw.chromium.launch(headless=True)
                b.close()
                ok = True
            except Exception as exc:  # noqa: BLE001
                detail = str(exc).splitlines()[0][:170]
        report.add(
            "登录用浏览器（Chromium）", ok, detail,
            "运行一次： python -m playwright install chromium（只需装一次，全机共用）",
        )
    except ImportError as exc:
        report.add("登录用浏览器（Chromium）", False, str(exc)[:120],
                   "先安装依赖： pip install -r requirements.txt")

    # 4) 站点可达性（最关键的一项）
    from .client import EcnuClient, load_session_state

    state = load_session_state()
    diag = None
    try:
        with EcnuClient(cfg, session=state) as client:
            diag = client.diagnose_access()
    except TranscribeHelperError as exc:
        report.add("课程平台网络可达", False, str(exc)[:170],
                   "先接入校园网，或连接学校 SSL-VPN 后重试")
    if diag is not None:
        if diag.reachable:
            report.add("课程平台网络可达", True, f"模式={diag.mode}")
        else:
            action = (
                "当前既不在校园网、也没走 VPN。请接入校园网，或连接学校 SSL-VPN"
                "（https://vpn.ecnu.edu.cn/portal/），然后点「重新检查」。"
                if diag.mode == "vpn_required"
                else (diag.detail or "站点不可达")
            )
            report.add("课程平台网络可达", False, diag.detail or diag.mode, action)

    # 5) 登录态
    if state.is_empty():
        report.add(
            "已保存登录态", False, "尚未登录",
            "点「登录」，在弹出的浏览器里完成一次统一身份认证（学号 + 密码，可能还有验证码）",
        )
    else:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(state.saved_at or 0))
        stale = diag is not None and diag.mode == "requires_login"
        report.add(
            "已保存登录态", not stale, f"{state.summary()}（保存于 {when}）",
            "登录态看起来已过期，点「登录」重新认证一次",
        )

    # 6) ASR 端点
    provider = (cfg.asr_provider or "dashscope").lower()
    if provider in ("faster_whisper_local", "local", "faster-whisper"):
        try:
            import faster_whisper  # type: ignore  # noqa: F401

            report.add("语音识别（本地 faster-whisper）", True, f"模型={cfg.asr_model}")
        except ImportError:
            report.add(
                "语音识别（本地 faster-whisper）", False, "未安装 faster-whisper",
                "pip install -r requirements-optional.txt，或到设置里改用云端 ASR",
            )
    else:
        key = ""
        try:
            if cm is not None and hasattr(cm, "secret"):
                key = str(cm.secret("asr_api_key"))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            key = ""
        from urllib.parse import urlparse

        host = (urlparse(cfg.asr_base_url or "").hostname or "").lower()
        is_local = host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")
        if not key and not is_local:
            report.add(
                "语音识别（ASR）已配置", False,
                f"未填 API Key（端点 {cfg.asr_base_url}）",
                "二选一：① 跑本地服务 scripts/local_asr_server.py，Base URL 指向 "
                "http://127.0.0.1:8000/v1（不需要 Key）；"
                "② 到「设置 → 语音识别」填 DashScope API Key",
            )
        elif deep:
            try:
                from .transcriber import probe_asr_endpoint

                ok, msg = probe_asr_endpoint(cfg, key)
                report.add("语音识别（ASR）端点可用", ok, msg,
                           "检查 Base URL / 模型名 / API Key，或先启动本地 ASR 服务")
            except Exception as exc:  # noqa: BLE001
                report.add("语音识别（ASR）端点可用", False, str(exc)[:150], "检查网络与端点配置")
        else:
            report.add(
                "语音识别（ASR）已配置", True,
                f"{cfg.asr_provider} / {cfg.asr_model}（本次未做联网自检）",
            )

    return report


# --------------------------------------------------------------------------- #
# 接口自动嗅探（M0「从真实抓包反推接口」的自动化版本）
# --------------------------------------------------------------------------- #
#: 站点自身流量（不嗅探第三方域名）
_SITE_HOSTS = ("ecnu.edu.cn",)


def sniff_api_paths(
    cfg: AppConfig,
    *,
    timeout: float = 45.0,
    headless: bool = True,
    max_paths: int = 40,
) -> dict[str, list[str]]:
    """借已登录的浏览器**真实打开一次页面**，观察它自己调用了哪些 XHR/Fetch 接口。

    这是「反推接口」的兜底方案：当候选路径全部探测失败时，用它把真实路径找出来，
    交给 :class:`~ecnu_transcribe.client.EcnuClient` 的 ``discovered_endpoints`` 使用。

    返回 ``{"json": [...], "list": [...], "play": [...]}``（相对路径，按启发式分类）。

    设计约束：
        * **只嗅探** ``*.ecnu.edu.cn``（不把请求引向第三方）；
        * 使用持久化 profile，因此在已登录状态下无需重新认证；
        * 任何异常都返回空结果，由调用方回退到候选路径 —— 嗅探失败不影响主流程。
    """
    paths_out: dict[str, list[str]] = {"json": [], "list": [], "play": []}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.info("未安装 playwright，跳过接口嗅探（改用候选路径探测）")
        return paths_out

    seen: dict[str, dict[str, Any]] = {}

    def on_response(resp: Any) -> None:
        try:
            url = resp.url or ""
            if not any(h in url for h in _SITE_HOSTS):
                return
            ctype = str((resp.headers or {}).get("content-type", "")).lower()
            if "json" not in ctype:
                return
            from urllib.parse import urlparse

            parts = urlparse(url)
            rel = parts.path + (("?" + parts.query) if parts.query else "")
            if rel in seen:
                return
            body = ""
            try:
                body = resp.text()[:20000]
            except Exception:
                body = ""
            seen[rel] = {"status": resp.status, "body": body, "url": url}
        except Exception:
            pass

    _LIST_HINT = ("total", "rows", "records", "list", "pageSize", "pageNum", "pageData")
    _PLAY_HINT = ("playurl", "play_url", "m3u8", "hls", "videourl", "video_url", "playpath", "streamurl")
    _RESOURCE_HINT = ("resource", "courseware", "ware", "video", "media", "material", "replay")
    _COURSE_HINT = ("course", "teachingclass", "clazz", "class")

    try:
        with sync_playwright() as pw:
            launch_kwargs: dict[str, Any] = dict(
                user_data_dir=str(paths.browser_profile_dir()),
                headless=headless,
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            )
            if cfg.proxy:
                launch_kwargs["proxy"] = {"server": cfg.proxy}
            ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
            try:
                ctx.on("response", on_response)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                for url in (cfg.portal_url,):
                    try:
                        page.goto(url, wait_until="networkidle", timeout=int(timeout * 1000))
                    except Exception as exc:
                        log.info("嗅探时页面加载告警（可忽略）：%s", exc)
                    page.wait_for_timeout(3000)
            finally:
                ctx.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("接口嗅探失败（将回退到候选路径探测）：%s", exc)
        return paths_out

    for rel, info in seen.items():
        low = rel.lower()
        item = {"path": rel, "status": info["status"], "url": info["url"]}
        log.info("嗅探到接口：%s（HTTP %s）", redact(rel)[:160], info["status"])
        paths_out["json"].append(rel)
        body_low = (info["body"] or "").lower()
        if any(h in low for h in _PLAY_HINT) or any(h in body_low[:4000] for h in ("playurl", "m3u8", "hlsurl")):
            paths_out["play"].append(rel)
        elif any(h in body_low for h in _LIST_HINT):
            if any(h in low for h in _RESOURCE_HINT):
                paths_out["list"].append(rel)
            elif any(h in low for h in _COURSE_HINT):
                paths_out.setdefault("course", []).append(rel)
            else:
                paths_out["list"].append(rel)
        elif any(h in low for h in _RESOURCE_HINT):
            paths_out["list"].append(rel)
        elif any(h in low for h in _COURSE_HINT):
            paths_out.setdefault("course", []).append(rel)

    # 记录到 recon/，便于人工确认
    try:
        dump = paths.recon_dir() / "sniffed_endpoints.json"
        dump.write_text(
            json.dumps(
                {"portal": cfg.portal_url, "endpoints": {k: v for k, v in seen.items()}},
                ensure_ascii=False,
                indent=2,
            )[:200000],
            encoding="utf-8",
        )
        log.info("嗅探结果已写入 %s", dump)
    except OSError:
        pass

    for key in ("json", "list", "play"):
        paths_out[key] = paths_out.get(key, [])[:max_paths]
    log.info(
        "嗅探汇总：json=%s list=%s play=%s course=%s",
        len(paths_out["json"]), len(paths_out["list"]),
        len(paths_out["play"]), len(paths_out.get("course", [])),
    )
    return paths_out
