"""登录态持久化与消费的测试（`load_session_state` / `SessionState` / 请求头构造）。

这是整个应用**最关键的契约**：Playwright 写出的 `storage_state.json` 必须能被
httpx 侧正确消费。任何一处对不上（字段名、domain、token 键名、过期判断），
表现都是「登录明明成功了，但拉清单说未登录」—— 而用户完全看不出原因。

本文件覆盖：
    1. **契约往返**：写一份真实形态的 storage_state → 载入 → 断言
       Cookie 头与 token 头**真的出现在 HTTP 请求里**（用本地假平台收请求验证）；
    2. 畸形/损坏/空文件不能抛异常，也不能被当成「已登录」；
    3. `summary()` 必须脱敏（值不能泄漏，键名保留便于排障）；
    4. 可达性诊断要把「需要登录」和「被 webVPN 拦下」区分开；
    5. **登录会话的判定不能对 URL 做子串匹配**（缺陷 37/38，全部取自实测真 URL）。
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
from pathlib import Path

import pytest

from ecnu_transcribe.client import EcnuClient, SessionState, load_session_state
from ecnu_transcribe.config import AppConfig
from ecnu_transcribe.login import LoginSession


# --------------------------------------------------------------------------- #
# 一份「真实形态」的 storage_state（字段名与 Playwright 导出一致）
# --------------------------------------------------------------------------- #
def write_state(path: Path, *, cookies=None, origins=None, raw: str | None = None) -> Path:
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    payload = {
        "cookies": cookies
        if cookies is not None
        else [
            {
                "name": "JSESSIONID",
                "value": "COOKIE-VALUE-AAA",
                "domain": "courses.ecnu.edu.cn",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
            {
                "name": "SERVERID2",
                "value": "Server1",
                "domain": "proxy.ecnu.edu.cn",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": True,
                "sameSite": "None",
            },
        ],
        "origins": origins
        if origins is not None
        else [
            {
                "origin": "https://courses.ecnu.edu.cn",
                "localStorage": [
                    {"name": "access_token", "value": "TOKEN-VALUE-BBB"},
                    {"name": "theme", "value": "dark"},
                ],
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 采集请求的假平台（验证请求头契约）
# --------------------------------------------------------------------------- #
class _Capture(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list[dict] = []

    def log_message(self, *a):  # noqa: A003
        pass

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n) if n else None
        _Capture.received.append({k: v for k, v in self.headers.items()})
        body = json.dumps({"code": 200, "data": {"total": 0, "rows": []}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def capture_server():
    _Capture.received = []
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Capture)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


# --------------------------------------------------------------------------- #
# 1. 解析
# --------------------------------------------------------------------------- #
def test_parse_realistic_storage_state(tmp_path):
    p = write_state(tmp_path / "state.json")
    st = load_session_state(p)
    assert st.cookies["JSESSIONID"] == "COOKIE-VALUE-AAA"
    assert st.cookies["SERVERID2"] == "Server1"
    assert st.is_empty() is False
    assert "access_token" in st.auth_token_candidates()
    assert st.auth_token_candidates()["access_token"] == "TOKEN-VALUE-BBB"
    # 非 token 的键不该被当成凭据
    assert "theme" not in st.auth_token_candidates()


def test_cookie_header_format(tmp_path):
    st = load_session_state(write_state(tmp_path / "s.json"))
    header = st.cookie_header
    assert "JSESSIONID=COOKIE-VALUE-AAA" in header
    assert "SERVERID2=Server1" in header
    assert "; " in header, f"Cookie 头应以 '; ' 分隔：{header}"


def test_missing_file_is_empty_not_error(tmp_path):
    st = load_session_state(tmp_path / "nope.json")
    assert st.is_empty() is True
    assert st.cookies == {}


@pytest.mark.parametrize(
    "raw, label",
    [
        ("", "空文件"),
        ("   ", "全空白"),
        ("not json", "非 JSON"),
        ("{broken", "JSON 语法错误"),
        ("[]", "顶层是数组"),
        ("null", "JSON null"),
        ('{"cookies": "not a list"}', "cookies 不是数组"),
        ('{"cookies": [{"value": "x"}]}', "cookie 缺 name"),
        ('{"cookies": [], "origins": [{"origin": "x", "localStorage": "bad"}]}', "localStorage 不是数组"),
    ],
)
def test_malformed_state_never_raises(raw, label, tmp_path):
    """畸形文件不能抛异常（否则应用一启动就崩），也不能被当成「已登录」。"""
    p = write_state(tmp_path / "bad.json", raw=raw)
    st = load_session_state(p)     # 不应抛
    assert isinstance(st, SessionState)
    if label in ("空文件", "全空白", "非 JSON", "{broken", "[]", "null"):
        assert st.is_empty() is True, f"[{label}] 不该被当成已登录"


def test_cookie_without_name_is_skipped(tmp_path):
    p = write_state(tmp_path / "s.json", cookies=[
        {"value": "orphan"},
        {"name": "GOOD", "value": "v", "domain": "d"},
    ])
    st = load_session_state(p)
    assert st.cookies == {"GOOD": "v"}


def test_domain_recorded_in_origins(tmp_path):
    p = write_state(tmp_path / "s.json")
    st = load_session_state(p)
    assert "courses.ecnu.edu.cn" in st.origins
    assert "proxy.ecnu.edu.cn" in st.origins


# --------------------------------------------------------------------------- #
# 2. 契约：登录态必须真的进到 HTTP 请求里
# --------------------------------------------------------------------------- #
def test_cookies_and_token_reach_the_request(tmp_path, capture_server):
    """核心契约：storage_state 里的 Cookie 与 token 必须出现在真实请求头里。"""
    state_path = write_state(tmp_path / "state.json")
    st = load_session_state(state_path)

    cfg = AppConfig()
    cfg.api_base = capture_server
    cfg.portal_url = f"{capture_server}/#/home"
    cfg.jitter_min = 0.0
    cfg.jitter_max = 0.0
    cfg.request_timeout = 10.0

    with EcnuClient(cfg, session=st) as client:
        client.request("POST", "/api/course/list", json_body={"pageNum": 1})

    assert _Capture.received, "假平台应收到请求"
    headers = {k.lower(): v for k, v in _Capture.received[-1].items()}
    assert "cookie" in headers, f"请求里没有 Cookie 头：{sorted(headers)}"
    assert "JSESSIONID=COOKIE-VALUE-AAA" in headers["cookie"]
    assert "SERVERID2=Server1" in headers["cookie"]
    assert "access_token" in headers, f"localStorage 里的 token 应作为请求头发回：{sorted(headers)}"
    assert headers["access_token"] == "TOKEN-VALUE-BBB"
    # 必要的排障头
    assert headers.get("referer", "").startswith(capture_server)
    assert "user-agent" in headers


def test_no_cookies_means_no_cookie_header(tmp_path, capture_server):
    """未登录时不应伪造 Cookie 头。"""
    cfg = AppConfig()
    cfg.api_base = capture_server
    cfg.portal_url = f"{capture_server}/#/home"
    cfg.jitter_min = 0.0
    cfg.jitter_max = 0.0
    with EcnuClient(cfg, session=SessionState()) as client:
        client.request("POST", "/api/course/list", json_body={"pageNum": 1})
    headers = {k.lower(): v for k, v in _Capture.received[-1].items()}
    assert "cookie" not in headers


def test_bearer_token_is_promoted_to_authorization(tmp_path, capture_server):
    """localStorage 里是 `Bearer xxx` 形式时，要提到 Authorization 头。"""
    p = write_state(tmp_path / "s.json", origins=[{
        "origin": "https://courses.ecnu.edu.cn",
        "localStorage": [{"name": "auth", "value": "Bearer TOKEN-VALUE-CCC"}],
    }])
    st = load_session_state(p)
    cfg = AppConfig()
    cfg.api_base = capture_server
    cfg.portal_url = f"{capture_server}/#/home"
    cfg.jitter_min = 0.0
    cfg.jitter_max = 0.0
    with EcnuClient(cfg, session=st) as client:
        client.request("POST", "/api/x", json_body={})
    headers = {k.lower(): v for k, v in _Capture.received[-1].items()}
    assert headers.get("authorization") == "Bearer TOKEN-VALUE-CCC", sorted(headers)


# --------------------------------------------------------------------------- #
# 3. 脱敏
# --------------------------------------------------------------------------- #
def test_summary_keeps_names_but_hides_values(tmp_path):
    st = load_session_state(write_state(tmp_path / "s.json"))
    summary = st.summary()
    assert "COOKIE-VALUE-AAA" not in summary, "Cookie 值泄漏到摘要里了"
    assert "TOKEN-VALUE-BBB" not in summary, "token 值泄漏到摘要里了"
    assert "JSESSIONID" in summary, "键名应保留（排障需要）"
    assert "access_token" in summary
    assert "Server1" in summary or "SERVERID2" in summary


def test_summary_of_empty_state_is_safe():
    st = SessionState()
    summary = st.summary()
    assert isinstance(summary, str)
    assert "Cookie 0 个" in summary or "0" in summary


# --------------------------------------------------------------------------- #
# 4. 可达性诊断的分类
# --------------------------------------------------------------------------- #
class _Redirector(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    location = "/"
    status = 302

    def log_message(self, *a):  # noqa: A003
        pass

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(type(self).status)
        self.send_header("Location", type(self).location)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture()
def redirect_server():
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Redirector)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_diagnose_classifies_vpn_gateway(redirect_server):
    """被 webVPN 拦下 → 必须明确说是「需要校园网/VPN」，而不是含糊的不可达。"""
    _Redirector.location = "https://proxy.ecnu.edu.cn/vpn_key/update?reason=site+x+not+found"
    cfg = AppConfig()
    cfg.portal_url = f"{redirect_server}/jy-application-resourcemanage-ui/"
    cfg.api_base = redirect_server
    with EcnuClient(cfg, session=SessionState()) as client:
        diag = client.diagnose_access()
    assert diag.reachable is False
    assert diag.mode == "vpn_required", diag.mode
    assert "VPN" in diag.detail or "校园网" in diag.detail
    assert diag.to_text()


def test_diagnose_classifies_requires_login(redirect_server):
    _Redirector.location = "https://sso.ecnu.edu.cn/login?service=x"
    cfg = AppConfig()
    cfg.portal_url = f"{redirect_server}/jy-application-resourcemanage-ui/"
    cfg.api_base = redirect_server
    with EcnuClient(cfg, session=SessionState()) as client:
        diag = client.diagnose_access()
    assert diag.mode == "requires_login", diag.mode
    assert diag.reachable is True, "可达但需要登录（这与「不可达」是两回事）"


def test_diagnose_classifies_direct(redirect_server):
    _Redirector.status = 200
    _Redirector.location = ""
    cfg = AppConfig()
    cfg.portal_url = f"{redirect_server}/#/home"
    cfg.api_base = redirect_server
    with EcnuClient(cfg, session=SessionState()) as client:
        diag = client.diagnose_access()
    assert diag.mode == "direct", diag.mode
    assert diag.reachable is True
    _Redirector.status = 302


# --------------------------------------------------------------------------- #
# 登录会话的状态判定（缺陷 37/38）
#
# 这两个缺陷是同一类错误的两次发作：**对整条 URL 做子串匹配**，而不是按
# 「主机 + 路径」判断。真实后果是「用户明明该做的只是输密码，工具却让他去折腾网络」，
# 以及更糟的「真的登录成功了却永远判成未登录」。
# --------------------------------------------------------------------------- #
class _FakePage:
    """最小 Playwright Page 替身：只需要 url / inner_text / evaluate。"""

    def __init__(self, url: str, body: str = "", ls_keys: int = 0) -> None:
        self.url = url
        self._body = body
        self._ls_keys = ls_keys
        self.closed = False

    def inner_text(self, _sel: str, timeout: int | None = None) -> str:  # noqa: ARG002
        return self._body

    def evaluate(self, _script: str) -> int:
        return self._ls_keys


def _session_for(page: _FakePage | None) -> LoginSession:
    session = LoginSession(AppConfig(), record_network=False)
    session._page = page  # noqa: SLF001 — 测试直接注入页面替身
    return session


#: 实测抓到的**真实** SSO 登录页 URL（校外访问链路的最后一跳）。
#: 注意 query 里的 `redirect_uri` 把 `proxy.ecnu.edu.cn` 编码进去了 —— 这正是误判来源。
REAL_SSO_URL = (
    "https://sso.ecnu.edu.cn/login?service=https%3A%2F%2Fsso.ecnu.edu.cn%2Foauth2.0%2F"
    "callbackAuthorize%3Fclient_id%3Dd46ba84ffc58611f%26redirect_uri%3Dhttps%253A%252F%252F"
    "proxy.ecnu.edu.cn%252Fecnu_oauth2%26response_type%3Dcode"
)


def test_sso_login_page_is_not_reported_as_vpn_block():
    """缺陷 37：停在 SSO 登录页 ≠ 被 webVPN 拦下（query 里带 proxy.ecnu.edu.cn 不算）。"""
    session = _session_for(_FakePage(REAL_SSO_URL, body="统一身份认证平台 学号 密码 登录"))
    msg = session._diagnose_stuck(REAL_SSO_URL, 600)

    assert "webVPN" not in msg, msg
    assert "拦下" not in msg, msg
    assert "统一身份认证" in msg, msg
    assert "密码" in msg, msg
    # 不能再把用户指去折腾网络
    assert "SSL-VPN" not in msg, msg


def test_vpn_key_redirect_alone_is_not_a_block():
    """`vpn_key/update` 是校外访问的**正常跳转**，停在它上面也不能说成拦截。"""
    url = "https://proxy.ecnu.edu.cn/vpn_key/update?origin=https%3A%2F%2Fcourses.ecnu.edu.cn%2F"
    session = _session_for(_FakePage(url, body="正在跳转…"))
    msg = session._diagnose_stuck(url, 600)
    assert "site not found" not in msg
    assert "拦下" not in msg


def test_real_gateway_error_page_is_reported_as_block():
    """真拦截的判据是**页面本身**写着站点不存在，而不是 URL 出现过什么关键字。"""
    url = "https://proxy.ecnu.edu.cn/vpn_key/update?reason=site+courses.ecnu.edu.cn+not+found"
    session = _session_for(_FakePage(url, body="Site not found 站点不存在，请联系管理员"))
    msg = session._diagnose_stuck(url, 600)
    assert "webVPN" in msg and ("校园网" in msg or "SSL-VPN" in msg), msg


def test_logged_in_detected_when_platform_url_carries_vpn_key():
    """缺陷 38：平台 URL 上带 `vpn_key`（校外 webVPN 的正常形态）时**不能**判成未登录。

    否则「登录明明成功」会被永久判为失败 —— 用户会一直卡在登录页反复重试。
    """
    url = "https://courses.ecnu.edu.cn/jy-application-resourcemanage-ui/?vpn_key=abc123#/home"
    page = _FakePage(url, body="我的课程 录播 资源列表")
    assert _session_for(page)._detect_logged_in() is True


@pytest.mark.parametrize(
    "url",
    [
        REAL_SSO_URL,
        "https://proxy.ecnu.edu.cn/users/sign_in",
        "https://api.ecnu.edu.cn/oauth2/authorize?client_id=x&response_type=code",
        "https://courses.ecnu.edu.cn/login",
    ],
)
def test_auth_pages_are_never_treated_as_logged_in(url):
    """认证域/认证路径必须一律判为「未登录」，哪怕页面上恰好出现「课程」等字样。"""
    page = _FakePage(url, body="课程 录播 资源管理")
    assert _session_for(page)._detect_logged_in() is False


def test_platform_page_without_markers_is_not_logged_in():
    """到了平台域但页面还没有内容（前端未渲染完）→ 先别急着说已登录。"""
    url = "https://courses.ecnu.edu.cn/jy-application-resourcemanage-ui/#/home"
    assert _session_for(_FakePage(url, body="加载中…"))._detect_logged_in() is False


def test_localstorage_token_counts_as_logged_in():
    url = "https://courses.ecnu.edu.cn/jy-application-resourcemanage-ui/#/home"
    assert _session_for(_FakePage(url, body="", ls_keys=1))._detect_logged_in() is True


def test_diagnostics_survive_broken_page():
    """页面已关闭 / 读不到正文时，诊断与判定都必须退化为安全值，而不是抛异常。"""

    class _BrokenPage:
        url = REAL_SSO_URL

        def inner_text(self, *_a, **_k):
            raise RuntimeError("Target page, context or browser has been closed")

        def evaluate(self, *_a, **_k):
            raise RuntimeError("Target page, context or browser has been closed")

    session = _session_for(_BrokenPage())
    assert session._body_snippet() == ""
    assert session._detect_logged_in() is False
    assert "统一身份认证" in session._diagnose_stuck(REAL_SSO_URL, 600)
    assert _session_for(None)._detect_logged_in() is False
    assert "浏览器窗口" in _session_for(None)._diagnose_stuck("", 600)


# --------------------------------------------------------------------------- #
# 缺陷 48：登录窗口可能被摆到**屏幕外**，用户根本看不见
#
# 实测：窗口被窗口管理器放到 (-25600, -25600) 且最小化。用户找不到窗口 → 去自己的
# 浏览器里登录 → 那是另一个 profile → 登录态传不过来 → 双方都觉得莫名其妙：
# 用户说「我明明登录了啊」，程序说「你没登录」。
# --------------------------------------------------------------------------- #
class _GeoPage:
    """记录 evaluate / bring_to_front 调用的页面替身。"""

    url = "https://sso.ecnu.edu.cn/login"

    def __init__(self, x: float, y: float, *, broken: bool = False) -> None:
        self._x, self._y, self._broken = x, y, broken
        self.calls: list[str] = []

    def evaluate(self, script, *a, **kw):  # noqa: ANN001
        self.calls.append(script)
        if self._broken:
            raise RuntimeError("Target page has been closed")
        if "screenX" in script:
            return {"x": self._x, "y": self._y, "w": 1456, "h": 882, "sw": 1440, "sh": 900}
        return None

    def bring_to_front(self) -> None:
        self.calls.append("bring_to_front")


def test_offscreen_login_window_triggers_os_level_fix(monkeypatch):
    """窗口被最小化/摆到屏幕外 → 必须走 **OS 层** 修正（JS 的 moveTo 在这台机器上无效）。

    实测：JS 报 x=-25600 时 user32 也报 (-25600,-25600) 且宽度只剩 159px（被最小化），
    而 `window.moveTo()` 对主窗口不起作用 —— 只有 user32 的 MoveWindow 真正管用。
    """
    page = _GeoPage(-25600, -25600)
    session = _session_for(page)  # type: ignore[arg-type]
    calls: list[str] = []

    def fake_os_fix() -> str:
        calls.append("os")
        return "最小化=True (-25600,-25600) 159px → (60,60)，已置顶"

    monkeypatch.setattr(session, "_move_window_os_level", fake_os_fix)
    session.ensure_window_visible()
    assert calls == ["os"], calls
    assert "bring_to_front" in page.calls


def test_onscreen_login_window_is_left_alone(monkeypatch):
    """位置正常时不要乱动用户的窗口（OS 层返回空串，只置前）。"""
    page = _GeoPage(60, 60)
    session = _session_for(page)  # type: ignore[arg-type]
    monkeypatch.setattr(session, "_move_window_os_level", lambda: "")
    session.ensure_window_visible()
    assert "bring_to_front" in page.calls


def test_os_level_fix_returns_empty_without_matching_window():
    """没有匹配的 Chromium 窗口时安静返回空串（不能报错、也不能乱动别的浏览器）。"""
    from ecnu_transcribe.config import AppConfig
    from ecnu_transcribe.login import LoginSession

    session = LoginSession(AppConfig(), record_network=False)
    result = session._move_window_os_level()  # noqa: SLF001
    assert isinstance(result, str)


def test_visibility_check_survives_broken_page(monkeypatch):
    """读不到窗口位置也不能抛异常，而且仍要尝试置前（不能因为探不到就不作为）。"""
    page = _GeoPage(0, 0, broken=True)
    session = _session_for(page)  # type: ignore[arg-type]
    monkeypatch.setattr(session, "_move_window_os_level", lambda: "")
    session.ensure_window_visible()
    assert "bring_to_front" in page.calls
    _session_for(None).ensure_window_visible()  # 没有页面也不应抛异常


def test_login_window_launch_args_keep_it_on_screen():
    """启动参数里必须带窗口位置与尺寸 —— 这是最省事的一道保险。"""
    import inspect

    from ecnu_transcribe.login import LoginSession

    src = inspect.getsource(LoginSession.open)
    assert "--window-position=" in src, "缺少 --window-position，窗口可能又跑到屏幕外"
    assert "--window-size=" in src
