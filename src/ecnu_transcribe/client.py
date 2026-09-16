"""大夏学堂 / 资源管理平台 HTTP 客户端。

职责
----
* 复用 ``storage_state.json`` 里的 Cookie（以及 localStorage 里的 token）发起请求；
* 自动翻页拉全量课程 / 资源；
* 登录态失效抛 :class:`AuthExpiredError`（**绝不静默返回空列表**）；
* 接口结构变化抛 :class:`ApiChangedError`（接口变更检测是显式的失败提示）；
* 站内请求之间加 300–800ms 抖动延时，避免触发风控；
* 支持「借浏览器环境算签名」的降级预案：当某个接口必须由页面上下文发起时，
  由 :mod:`ecnu_transcribe.login` 的浏览器会话代打（``BridgeFetcher``）。

访问路径（实测，2026-09，见 docs/API.md）
-----------------------------------------
``courses.ecnu.edu.cn`` (202.120.88.100) 在校外会**先跳到学校 webVPN 网关**：

    GET /jy-application-resourcemanage-ui/  → 302 https://proxy.ecnu.edu.cn/vpn_key/update?...

这条跳转是校外访问的**正常入口**（不是「被封」）：浏览器继续走
``proxy.ecnu.edu.cn/users/sign_in`` → ``api.ecnu.edu.cn/oauth2/authorize``
→ ``sso.ecnu.edu.cn/login``（统一身份认证页），完成认证后即可进入平台。
纯 HTTP 客户端拿不到登录态，所以本应用的做法是「可见浏览器登录一次 + 复用登录态」。

因此本模块把「怎么到达站点」抽象成 :class:`AccessProbe` 的探测结果，
在 GUI 里如实呈现给用户（校外需先完成统一身份认证；网关明确报站点不存在时才需
校园网 / SSL-VPN），不做任何验证绕过。
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from . import paths
from .catalog import Catalog, Course, Resource, pick
from .config import AppConfig, ConfigManager
from .errors import (
    ApiBusinessError,
    ApiChangedError,
    AuthExpiredError,
    NetworkError,
    SiteUnreachableError,
    TranscribeHelperError,
)
from .logbus import get_logger, redact

log = get_logger("client")

#: 业务层「成功」码的常见取值
_OK_CODES = {0, 200, "0", "200", "0000", "00000", "000000", "success", "SUCCESS", True}
#: 业务层「未登录」码
_AUTH_CODES = {
    401, 403, "401", "403",
    "40100", "40300", "A0230", "A0301", "A0310",
    "NOT_LOGIN", "NOTLOGIN", "UNAUTHORIZED", "TOKEN_EXPIRED", "TOKEN_INVALID",
    "LOGIN_REQUIRED", "NO_LOGIN", "USER_NOT_LOGIN", "SESSION_EXPIRED",
}
#: 出现这些关键词基本可以判定登录态失效
_AUTH_HINTS = (
    "未登录", "请登录", "登录已过期", "登录过期", "重新登录", "登录失效", "会话过期",
    "未授权", "无权限访问", "token 过期", "token过期", "invalid token", "unauthorized",
    "not logged in", "login required", "session expired", "authentication required",
)
#: 列表容器候选键
_LIST_KEYS = (
    "rows", "records", "list", "items", "data", "content", "result",
    "dataList", "recordList", "courseList", "resourceList", "resultList", "pageData",
)
#: 总数候选键
_TOTAL_KEYS = ("total", "totalCount", "totalElements", "count", "totalNum", "recordsTotal", "totalRows")


# --------------------------------------------------------------------------- #
# 访问探测
# --------------------------------------------------------------------------- #
@dataclass
class AccessDiagnosis:
    """站点可达性诊断结果（GUI「登录/诊断」按钮展示）。"""

    reachable: bool = False
    mode: str = "unknown"  # direct | vpn_required | proxy | error
    status: int = 0
    final_url: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    detail: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        icon = "✅" if self.reachable else "⛔"
        lines = [f"{icon} 站点可达性：{self.mode}（HTTP {self.status}）"]
        lines.append(f"    入口：{self.final_url or '-'}")
        if self.redirect_chain:
            lines.append("    跳转链：" + " → ".join(self.redirect_chain[:6]))
        if self.detail:
            lines.append(f"    说明：{self.detail}")
        for ev in self.evidence:
            lines.append(f"    证据：{ev}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 登录态
# --------------------------------------------------------------------------- #
@dataclass
class SessionState:
    """从 storage_state.json 里提炼出的请求所需凭据。"""

    cookies: dict[str, str] = field(default_factory=dict)
    local_storage: dict[str, dict[str, str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    saved_at: float = 0.0
    origins: list[str] = field(default_factory=list)
    source_path: str = ""

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def is_empty(self) -> bool:
        return not self.cookies and not self.local_storage

    def auth_token_candidates(self) -> dict[str, str]:
        """从 localStorage 里猜 token（常见键名）。"""
        out: dict[str, str] = {}
        for _origin, kv in self.local_storage.items():
            for key, value in (kv or {}).items():
                low = key.lower()
                if any(h in low for h in ("token", "jwt", "ticket", "authorization", "auth")):
                    if isinstance(value, str) and 8 <= len(value) <= 4096:
                        out[key] = value
        return out

    def summary(self) -> str:
        """**脱敏**摘要，可安全写日志 / 展示。"""
        cookie_names = ", ".join(sorted(self.cookies)) or "(无)"
        token_names = ", ".join(sorted(self.auth_token_candidates())) or "(无)"
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.saved_at)) if self.saved_at else "(未知)"
        return (
            f"Cookie {len(self.cookies)} 个 [{cookie_names}]；"
            f"localStorage token 键 [{token_names}]；保存于 {when}"
        )


def _as_dict_list(value: Any) -> list[dict]:
    """只保留「字典元素」的列表。

    ``storage_state.json`` 是被外部工具（Playwright）写的文件，可能被手改、
    被半途截断、或被旧版本写成别的形状。**合法 JSON 但结构不对**时必须当成
    「没有登录态」，而不是让 `.get` 炸掉整个应用 —— 这个函数就是那道防线
    （实测：文件内容是 `null` / `[]` / `{"cookies": "not a list"}` 时，
    旧代码会抛 `AttributeError`，用户在启动应用时就会看到崩溃）。
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def load_session_state(path: Path | None = None) -> SessionState:
    """读取 Playwright 导出的 storage_state.json。

    **任何**异常或结构不符都退化为「空的登录态」（并记一条警告），
    绝不向上抛 —— 调用方（GUI 启动路径）依赖它是安全的。
    """
    p = Path(path) if path else paths.storage_state_path()
    if not p.is_file():
        return SessionState(source_path=str(p))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("storage_state.json 解析失败：%s", exc)
        return SessionState(source_path=str(p))

    if not isinstance(data, dict):
        log.warning(
            "storage_state.json 顶层不是对象（实际是 %s），按「未登录」处理",
            type(data).__name__,
        )
        return SessionState(source_path=str(p))

    try:
        saved_at = p.stat().st_mtime
    except OSError:
        saved_at = 0.0

    st = SessionState(saved_at=saved_at, source_path=str(p))
    for entry in _as_dict_list(data.get("origins")):
        origin = str(entry.get("origin", "") or "")
        if origin:
            st.origins.append(origin)

    for c in _as_dict_list(data.get("cookies")):
        name = str(c.get("name", "") or "")
        if not name:
            continue
        st.cookies[name] = str(c.get("value", "") or "")
        dom = str(c.get("domain", "") or "")
        if dom and dom not in st.origins:
            st.origins.append(dom)

    for origin in _as_dict_list(data.get("origins")):
        o = str(origin.get("origin", "") or "")
        kv = {
            str(i.get("name", "") or ""): str(i.get("value", "") or "")
            for i in _as_dict_list(origin.get("localStorage"))
        }
        kv.pop("", None)
        if o and kv:
            st.local_storage[o] = kv
    return st


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0")


def _is_loopback_url(url: str) -> bool:
    """判断一条记录是不是「本机模拟流量」。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


class EcnuClient:
    """资源管理平台客户端。"""

    #: 列表接口候选（不同版本路径不同，按顺序探测，命中即用）
    COURSE_ENDPOINTS: tuple[str, ...] = (
        "/jy-application-resourcemanage-ui/api/course/list",
        "/api/course/list",
        "/api/courses",
        "/jy-application-resourcemanage-ui/api/teachingClass/list",
        "/api/teachingClass/list",
        "/api/resource/course/list",
    )
    RESOURCE_ENDPOINTS: tuple[str, ...] = (
        "/jy-application-resourcemanage-ui/api/resource/list",
        "/api/resource/list",
        "/api/course/resource/list",
        "/api/resource/page",
        "/api/courseResource/list",
        "/api/ware/list",
    )
    PLAY_ENDPOINTS: tuple[str, ...] = (
        "/jy-application-resourcemanage-ui/api/resource/play",
        "/api/resource/play",
        "/api/resource/detail",
        "/api/resource/playInfo",
        "/api/courseResource/playUrl",
    )

    def __init__(
        self,
        cfg: AppConfig,
        *,
        config_manager: ConfigManager | None = None,
        session: SessionState | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.cm = config_manager
        self.session = session if session is not None else load_session_state()
        self.on_progress = on_progress
        self._client: httpx.Client | None = None
        self._lock = threading.RLock()
        #: 接口路径探测缓存：避免每次刷新都重新猜
        self._endpoint_cache: dict[str, str] = {}
        #: 真实平台接口的 jwt-token 缓存（换一次用很久；失效时 force 刷新）
        self._jwt_token: str = ""
        #: 嗅探到的真实接口路径（``{"course": [...], "list": [...], "play": [...]}``）
        self.discovered_endpoints: dict[str, list[str]] = {}
        #: 是否还允许嗅探（每次运行最多一次，避免反复开浏览器）
        self._sniff_enabled: bool = bool(cfg.extra.get("sniff_endpoints", True))
        #: 抓包记录（脱敏后写 recon/network.jsonl）
        self.network_log: list[dict[str, Any]] = []
        self._record_enabled = True

    # ------------------------------------------------------------------ #
    # HTTP 基础设施
    # ------------------------------------------------------------------ #
    def _base_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        token = self.session.auth_token_candidates()
        h = {
            "User-Agent": self.cfg.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": self.cfg.portal_url,
            "Origin": self._origin(),
            "X-Requested-With": "XMLHttpRequest",
        }
        # 平台常见 token 头（有就带，没有就不带，不猜错）
        for name in ("Authorization", "token", "Token", "X-Token", "access_token", "X-Access-Token"):
            if name in token:
                h[name] = token[name]
        if "Authorization" not in h:
            for key, value in token.items():
                if key.lower() in ("authorization", "auth", "jwt") and value.startswith(("Bearer", "bearer")):
                    h["Authorization"] = value
                    break
        if self.session.cookies:
            h["Cookie"] = self.session.cookie_header
        if extra:
            h.update(extra)
        return h

    def _origin(self) -> str:
        p = urlparse(self.cfg.api_base or self.cfg.portal_url)
        return f"{p.scheme}://{p.netloc}"

    @property
    def client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                kwargs: dict[str, Any] = dict(
                    timeout=httpx.Timeout(self.cfg.request_timeout, connect=20.0),
                    follow_redirects=False,
                    verify=self.cfg.verify_tls,
                    headers={},
                    # 尊重 *noproxy*；显式 proxy 优先
                    trust_env=not self.cfg.proxy,
                )
                if self.cfg.proxy:
                    kwargs["proxy"] = self.cfg.proxy
                self._client = httpx.Client(**kwargs)
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def __enter__(self) -> "EcnuClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #
    def _jitter(self) -> None:
        lo, hi = float(self.cfg.jitter_min), float(self.cfg.jitter_max)
        if hi > 0 and hi >= lo:
            time.sleep(random.uniform(lo, hi))

    def _record(self, method: str, url: str, status: int, *, body: Any = None, note: str = "") -> None:
        if not self._record_enabled:
            return
        self.network_log.append(
            {
                "ts": time.time(),
                "method": method,
                "url": redact(url),
                "status": status,
                "note": note,
                "body_sample": redact(json.dumps(body, ensure_ascii=False)[:1500]) if body is not None else "",
            }
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = False,
        raise_on_redirect: bool = True,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> Any:
        """发一次请求并做统一的登录态 / 重定向 / 业务码判定。"""
        full = url if url.startswith("http") else urljoin(self.cfg.api_base, url)
        merged = self._base_headers(headers)
        try:
            resp = self.client.request(
                method,
                full,
                params=params,
                json=json_body,
                data=data,
                headers=merged,
                follow_redirects=allow_redirects,
                timeout=timeout if timeout else self.cfg.request_timeout,
            )
        except httpx.TimeoutException as exc:
            self._record(method, full, 0, note=f"timeout: {exc}")
            raise NetworkError(f"请求超时：{full}") from exc
        except httpx.HTTPError as exc:
            self._record(method, full, 0, note=f"error: {exc}")
            raise SiteUnreachableError(f"无法连接 {full}：{exc}") from exc

        self._record(method, full, resp.status_code, note="redirect" if resp.is_redirect else "")
        self._jitter()

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if raise_on_redirect:
                raise self._redirect_error(full, location)
            return {"__redirect__": location}

        if resp.status_code in (401, 403):
            raise AuthExpiredError(
                f"服务端返回 HTTP {resp.status_code}，登录态已失效，请重新登录",
                detail=redact(resp.text[:300]),
            )
        if resp.status_code >= 400:
            raise TranscribeHelperError(f"HTTP {resp.status_code}：{full}\n{redact(resp.text[:300])}")

        ctype = resp.headers.get("content-type", "")
        text = resp.text
        if not expect_json:
            return text

        # HTML 登录页 = 登录态失效（SPA 用前端路由时也可能返回 index.html，需区分）
        if "text/html" in ctype and self._looks_like_login_page(text):
            raise AuthExpiredError(
                f"接口 {full} 返回登录页，登录态已失效，请重新登录",
                detail="检测到 HTML 登录页特征（含登录表单 / 统一身份认证跳转）",
            )

        try:
            payload = resp.json()
        except Exception:
            if "text/html" in ctype:
                raise ApiChangedError(
                    f"接口 {full} 期望 JSON，实际返回 HTML（可能是 SPA 首页或接口已下线）",
                    url=full,
                    sample=text[:400],
                ) from None
            raise ApiChangedError(f"接口 {full} 返回的不是合法 JSON", url=full, sample=text[:400]) from None

        return self._check_business(payload, full)

    @staticmethod
    def _looks_like_login_page(text: str) -> bool:
        if len(text) > 60000:  # SPA 首页通常很大，不做关键词误判
            return False
        hints = (
            "统一身份认证", "id=\"login\"", "name=\"password\"", "sign_in", "signin",
            "oauth2/authorize", "vpn_key/update", "请登录", "用户登录",
        )
        low = text.lower()
        return any(h.lower() in low for h in hints)

    def _redirect_error(self, url: str, location: str) -> TranscribeHelperError:
        low = location.lower()
        if any(k in low for k in ("login", "sign_in", "signin", "oauth", "sso", "authorize", "cas")):
            return AuthExpiredError(
                f"请求 {url} 被重定向到登录页，登录态已失效，请重新登录",
                detail=f"Location: {redact(location)[:200]}",
            )
        if any(k in low for k in ("vpn_key", "proxy.ecnu.edu.cn", "webvpn")):
            return SiteUnreachableError(
                "站点被 webVPN 网关拦截：当前网络不在校园网内，且未建立 VPN。\n"
                f"  请求：{url}\n  跳转：{redact(location)[:200]}\n"
                "  处理：连接学校 SSL-VPN（https://vpn.ecnu.edu.cn/portal/）或改用校园网后重试。"
            )
        return SiteUnreachableError(f"请求 {url} 被重定向到 {redact(location)[:200]}")

    def _check_business(self, payload: Any, url: str) -> Any:
        """识别平台包装层（``{code, msg, data}``）并解包。

        支持多层包装（``{code, data:{code, data:{rows}}}``），递归剥离；
        只要某一层的业务码 / 消息表明未登录，就抛 :class:`AuthExpiredError`。
        """
        if not isinstance(payload, dict):
            return payload

        code = pick(payload, "code", "status", "resultCode", "respCode", "errCode", default=None)
        success = pick(payload, "success", "ok", default=None)
        msg = str(pick(payload, "message", "msg", "errorMsg", "errMsg", "error", "respMsg", default="") or "")

        if code is not None and (str(code) in {str(c) for c in _AUTH_CODES} or code in _AUTH_CODES):
            raise AuthExpiredError(
                f"接口 {url} 返回未登录业务码 {code}：{msg or '(无消息)'}",
                detail=msg,
            )
        if success is False or (code is not None and str(code) not in {str(c) for c in _OK_CODES}):
            low = msg.lower()
            if any(h in msg or h in low for h in _AUTH_HINTS):
                raise AuthExpiredError(f"接口 {url} 提示未登录：{msg}", detail=msg)
            raise ApiBusinessError(f"接口 {url} 业务失败 code={code}：{msg}", code=code, url=url)

        # 解包 data：递归处理多层包装，否则原样返回
        for key in ("data", "result", "payload", "body"):
            inner = payload.get(key)
            if isinstance(inner, dict):
                return self._check_business(inner, url)
            if isinstance(inner, list):
                return inner
        return payload

    # ------------------------------------------------------------------ #
    # 诊断
    # ------------------------------------------------------------------ #
    @staticmethod
    def _interference_hint(exc: Exception) -> str:
        """把「连不上」细分成能直接照做的几种原因（缺陷 44 的修复）。

        实测踩到的坑（2026-09-12 深夜）：本机 Clash 换了订阅后变成**全局代理**，
        出口 IP 从上海电信变成香港，学校网关对这类来源直接 **重置连接**
        （``WinError 10054 远程主机强迫关闭了一个现有的连接``）。
        当时界面上只显示「无法连接」，用户完全想不到是代理分流的问题。

        这里按异常特征给三种不同的下一步：
        * 连接被重置（10054/ECONNRESET/TLS EOF）→ 十有八九是代理/VPN 把校内域名也代理走了；
        * 域名解析失败 → DNS 问题（也可能是 fake-IP DNS 被关掉后内网域名无解析）；
        * 超时 → 网络慢/需要 VPN。
        """
        text = f"{exc}".lower()
        if any(k in text for k in ("10054", "connection reset", "econnreset",
                                   "unexpected_eof", "强迫关闭")):
            return (
                "连接被**重置**（不是域名解析失败，也不是被防火墙拦下）。\n"
                "最可能的原因：你开着代理/VPN 且把校内域名也走代理了 —— "
                "实测全局代理（出口变香港）时，学校网关会直接重置连接（WinError 10054）。\n"
                "处理：把 `*.ecnu.edu.cn` 设为直连（Clash 里加 "
                "`- DOMAIN-SUFFIX,ecnu.edu.cn,DIRECT`），或临时关闭代理后重试。"
            )
        if any(k in text for k in ("getaddrinfo", "name or service not known",
                                   "nodename nor servname", "no address associated",
                                   "dns")):
            return (
                "域名解析失败。若你在用「fake-IP」类 DNS（198.18.x.x 段），"
                "关掉代理后可能连内网域名都解析不了 —— 此时要么恢复代理的直连规则，"
                "要么接入校园网/学校 SSL-VPN。"
            )
        if any(k in text for k in ("timed out", "timeout", "超时")):
            return (
                "连接超时。请确认已接入校园网或学校 SSL-VPN；"
                "若已在校园网内仍超时，可能是代理/VPN 分流把校内流量带到了校外。"
            )
        return ""

    def diagnose_access(self) -> AccessDiagnosis:
        """探测站点可达性（不触发登录），返回可读诊断。"""
        diag = AccessDiagnosis(final_url=self.cfg.portal_url)
        chain: list[str] = []
        try:
            resp = self.client.get(
                self.cfg.portal_url,
                headers=self._base_headers({"Accept": "text/html,*/*"}),
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            diag.mode = "error"
            hint = self._interference_hint(exc)
            diag.detail = f"无法连接：{exc}" + (f"\n{hint}" if hint else "")
            diag.evidence.append(str(exc)[:200])
            if self.cfg.proxy:
                diag.evidence.append(f"当前配置了代理：{redact(self.cfg.proxy)}")
            return diag

        diag.status = resp.status_code
        location = resp.headers.get("location", "")
        if location:
            chain.append(location)
        diag.redirect_chain = chain
        diag.final_url = location or self.cfg.portal_url

        low = location.lower()
        if resp.status_code < 400 and not location:
            diag.reachable = True
            diag.mode = "direct"
            diag.detail = "入口可直接访问，登录态可能仍然有效"
            diag.evidence.append(f"GET {self.cfg.portal_url} → {resp.status_code}")
        elif "vpn_key" in low or "proxy.ecnu.edu.cn" in low:
            diag.reachable = False
            diag.mode = "vpn_required"
            # 实测（第 22 轮，真实抓包）这条跳转链是：
            #   courses.ecnu.edu.cn → proxy.ecnu.edu.cn/vpn_key/update → /users/sign_in
            #     → api.ecnu.edu.cn/oauth2/authorize → sso.ecnu.edu.cn/login（登录页 200）
            # 也就是说这是**校外访问的正常入口**，而且浏览器里有路可走（完成一次
            # 统一身份认证即可）—— 不要把它说成「被拦截」，更不要一上来就让用户去装 VPN。
            diag.detail = (
                "该入口需要先经学校 webVPN 网关（proxy.ecnu.edu.cn）并完成一次统一身份认证。\n"
                "直连 HTTP 客户端拿不到登录态，这是预期行为 —— 请点「登录」，"
                "在打开的浏览器窗口里完成学号 + 密码认证；本应用会保存登录态供后续请求复用。\n"
                "只有在浏览器里完成认证后仍被网关挡回（页面提示站点不存在 / 无权限）时，"
                "才需要接入校园网或学校 SSL-VPN（https://vpn.ecnu.edu.cn/portal/）。"
            )
            diag.evidence.append(f"Location: {redact(location)[:200]}")
        elif any(k in low for k in ("login", "sign_in", "oauth", "sso", "authorize", "cas")):
            diag.reachable = True
            diag.mode = "requires_login"
            diag.detail = "入口可达，但需要先完成统一身份认证（点击「登录」）。"
            diag.evidence.append(f"Location: {redact(location)[:200]}")
        else:
            diag.mode = "unknown"
            diag.detail = f"未预期的响应：HTTP {resp.status_code}"
            if location:
                diag.evidence.append(f"Location: {redact(location)[:200]}")
        return diag

    # ------------------------------------------------------------------ #
    # 真实平台接口（2025 学年实测，见 docs/API.md §1）
    # ------------------------------------------------------------------ #
    PLATFORM_API_PREFIX = "/jy-application-resourcemanage"

    def platform_api_base(self) -> str:
        """从入口 URL 推出真实接口前缀。

        ``https://courses.ecnu.edu.cn/jy-application-resourcemanage-ui/#/home``
        → ``https://courses.ecnu.edu.cn/jy-application-resourcemanage``
        """
        parts = urlparse(self.cfg.portal_url)
        origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else self.cfg.portal_url.rstrip("/")
        return origin.rstrip("/") + self.PLATFORM_API_PREFIX

    def fetch_jwt_token(self, *, force: bool = False) -> str:
        """用登录态 Cookie 换 ``jwt-token``（平台真实接口的鉴权头）。

        实测：``GET /jy-application-resourcemanage/oauth2/token`` →
        ``{"code":"0","result":{"access_token":…,"jwt_token":<910 字符>,…}}``
        真实接口要的是 ``jwt_token``（请求头名 ``jwt-token``），不是 access_token。
        """
        with self._lock:
            if self._jwt_token and not force:
                return self._jwt_token
        resp = self.client.get(
            f"{self.platform_api_base()}/oauth2/token",
            headers=self._base_headers({"Accept": "application/json, text/plain, */*"}),
            follow_redirects=False,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            raise AuthExpiredError(
                "换 jwt-token 时被重定向（登录态已失效）：请重新点「登录」完成统一身份认证"
            )
        if resp.status_code >= 400:
            raise SiteUnreachableError(f"换 jwt-token 失败：HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiChangedError(f"换 jwt-token 返回非 JSON：{resp.text[:200]}") from exc
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            result = payload.get("data") if isinstance(payload, dict) else None
        token = ""
        if isinstance(result, dict):
            token = str(result.get("jwt_token") or result.get("access_token") or "")
        if not token:
            raise ApiChangedError(f"换 jwt-token 的响应里没有令牌字段：{resp.text[:200]}")
        with self._lock:
            self._jwt_token = token
        log.info("已换取 jwt-token（%s 字符）", len(token))
        return token

    def _platform_headers(self) -> dict[str, str]:
        token = self.fetch_jwt_token()
        h = self._base_headers({"Accept": "application/json, text/plain, */*"})
        h["jwt-token"] = token
        return h

    def _platform_get(self, path: str, **params: Any) -> Any:
        resp = self.client.get(
            f"{self.platform_api_base()}{path}",
            params={k: v for k, v in params.items() if v is not None},
            headers=self._platform_headers(),
            follow_redirects=False,
        )
        return self._platform_payload(resp, path)

    def _platform_post(self, path: str, body: dict[str, Any] | None = None, **params: Any) -> Any:
        resp = self.client.post(
            f"{self.platform_api_base()}{path}",
            params={k: v for k, v in params.items() if v is not None},
            json=body or {},
            headers=self._platform_headers(),
            follow_redirects=False,
        )
        return self._platform_payload(resp, path)

    def _platform_payload(self, resp: httpx.Response, path: str) -> Any:
        """统一处理真实接口的响应包装与错误语义。"""
        body: Any = None
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:400]
        self._record(resp.request.method, str(resp.request.url), resp.status_code, body=body)
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            if "login" in loc or "oauth" in loc or "vpn_key" in loc:
                raise AuthExpiredError(
                    f"{path} 被重定向到登录（{redact(loc)[:80]}）：登录态已失效，请重新登录"
                )
            raise ApiChangedError(f"{path} 被重定向到 {redact(loc)[:120]}")
        if resp.status_code in (401, 403):
            raise AuthExpiredError(f"{path} 返回 HTTP {resp.status_code}：登录态已失效，请重新登录")
        if resp.status_code >= 400:
            raise ApiChangedError(f"{path} 返回 HTTP {resp.status_code}：{resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiChangedError(f"{path} 返回非 JSON：{resp.text[:200]}") from exc
        if isinstance(payload, dict):
            ok = payload.get("ok")
            data_ok = payload.get("dataOk")
            if ok is False or data_ok is False:
                message = str(payload.get("message") or "")
                code = str(payload.get("code") or "")
                if any(k in message for k in ("登录", "未授权", "token")) or code in ("401", "403"):
                    raise AuthExpiredError(f"{path} 业务失败：{message or code}")
                raise ApiChangedError(f"{path} 业务失败：{message or code}")
            for key in ("data", "result"):
                if payload.get(key) is not None:
                    return payload[key]
        return payload

    def fetch_terms(self) -> list[dict[str, Any]]:
        """学年学期列表。真实接口：``GET /v1/list/termYear``。"""
        return self._as_rows(self._platform_get("/v1/list/termYear"))

    def fetch_curriculum(self, acte_id: int | str, *, page_size: int = 100) -> list[dict[str, Any]]:
        """我的课表（按学期）。真实接口：``GET /v1/myself/curriculum``。

        返回的每一行是一**节课**：``id`` = courId、``subjName`` 科目、``teclId`` 教学班、
        ``courBeginTime`` 上课时间、``courVodOpen`` 是否有录像。
        分页参数是 ``page.pageIndex`` / ``page.pageSize``（写成 pageNum 会被回「分页不能为空」）。
        """
        rows: list[dict[str, Any]] = []
        page = 1
        while page <= 100:
            data = self._platform_get(
                "/v1/myself/curriculum",
                acteId=int(acte_id),
                **{"page.pageIndex": page, "page.pageSize": page_size},
            )
            items = self._as_rows(data)
            rows.extend(items)
            total = None
            if isinstance(data, dict):
                total = data.get("rowCount") or data.get("total")
            if len(items) < page_size:
                break
            if isinstance(total, int) and len(rows) >= total:
                break
            page += 1
        return rows

    def fetch_my_teaching_classes(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        """教学班列表（行政班级口径，**不是**个人课表，仅作补充信息）。"""
        rows: list[dict[str, Any]] = []
        page = 1
        while page <= 50:
            data = self._platform_get(
                "/v1/teachingclass/list",
                type=1,
                allType=1,
                **{"page.pageIndex": page, "page.pageSize": page_size},
            )
            items = self._as_rows(data)
            rows.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return rows

    def fetch_teaching_class_sessions(self, teaching_class_id: int | str, *, page_size: int = 100) -> list[dict[str, Any]]:
        """某个教学班的全部节次（含录像的课次）。

        真实接口：``POST /v1/statistics/teaching-class/user/course-list``，
        参数名是 **teachingClassId**（实测：写成 teclId 会被回「教学班id不为空」）。
        """
        rows: list[dict[str, Any]] = []
        page = 1
        while page <= 50:
            data = self._platform_post(
                "/v1/statistics/teaching-class/user/course-list",
                {"teachingClassId": int(teaching_class_id), "page": {"pageIndex": page, "pageSize": page_size}},
            )
            items = self._as_rows(data)
            rows.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return rows

    def fetch_course_videos(self, cour_id: int | str) -> list[dict[str, Any]]:
        """一节课的录播与播放地址。

        真实接口：``GET /v1/course_vod_urls_new?courseId=<courId>``
        → ``data.courseVodVideoDtoList[].url``（带 auth_key 签名的直连 mp4）+ ``vodId`` + ``vodTime``。
        """
        data = self._platform_get("/v1/course_vod_urls_new", courseId=int(cour_id))
        if isinstance(data, dict):
            # 实测字段名是 courseVodViewList（不是 courseVodVideoDtoList —— 那个名字只存在于我早期猜测里）
            for key in ("courseVodViewList", "courseVodVideoDtoList", "courseVodVideoList",
                        "vodList", "videoList"):
                val = data.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
            # 兜底：任何「带 url 的字典列表」
            for val in data.values():
                if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                    if any(x.get("url") for x in val):
                        return list(val)
        return []

    @staticmethod
    def _as_rows(data: Any) -> list[dict[str, Any]]:
        """把各种分页包装统一成行列表。"""
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("records", "list", "rows", "content", "items", "data"):
                val = data.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
        return []

    def fetch_platform_catalog(
        self, catalog: Catalog, *, notify: Callable[[str], None] | None = None
    ) -> bool:
        """用真实接口把清单填进 ``catalog``。返回是否成功（拿不到 jwt-token → False）。

        实测出的正确口径（2026-09 真机验证）::

            GET  /v1/list/termYear                                  → 学期列表（acteId）
            GET  /v1/myself/curriculum?acteId=&page.pageIndex=…      → 我的课表（每行 = 一节课，
                                                                        id 即 courId，含 courVodOpen）
            GET  /v1/course_vod_urls_new?courseId=<courId>           → 该节课的录播 + 签名播放地址

        早先试过 ``/v1/teachingclass/list``，它返回的是**行政班级**（法学年級班），
        不是本人在上的课 —— 已弃用为主口径，只在没有课表数据时做补充。
        """
        notify = notify or self._notify
        try:
            self.fetch_jwt_token()
        except (AuthExpiredError, SiteUnreachableError, ApiChangedError):
            return False

        notify("已通过统一身份认证，开始拉取我的课表…")
        terms = self.fetch_terms()
        if not terms:
            raise ApiChangedError("学期列表为空：无法确定要拉取哪些学期的课表")
        notify(f"共 {len(terms)} 个学期，逐学期拉取课表…")

        grouped: dict[str, Course] = {}
        total_videos = 0
        for idx, term in enumerate(terms, 1):
            acte_id = term.get("id")
            label = f"{term.get('acyeCode') or ''} 第{term.get('acteTerm') or '?'}学期"
            if not acte_id:
                continue
            try:
                rows = self.fetch_curriculum(acte_id)
            except (AuthExpiredError, ApiChangedError, SiteUnreachableError) as exc:
                notify(f"{label} 课表拉取失败：{exc}")
                continue
            with_vod = [r for r in rows if int(r.get("courVodOpen") or 0) == 1]
            notify(f"[{idx}/{len(terms)}] {label}：{len(rows)} 节课，其中有录播 {len(with_vod)} 节")
            for row in with_vod:
                cour_id = row.get("id") or row.get("courId")
                if not cour_id:
                    continue
                subj = str(row.get("subjName") or row.get("courName") or f"课程{cour_id}")
                tecl_id = row.get("teclId")
                teachers = row.get("teacNames") or []
                teacher = "、".join(str(t) for t in teachers if t)[:60]
                begin = str(row.get("courBeginTime") or "")
                course = grouped.get(subj)
                if course is None:
                    course = Course(
                        course_id=str(tecl_id or subj),
                        course_name=subj,
                        teacher=teacher,
                        term=label,
                        raw={"acteId": acte_id},
                    )
                    grouped[subj] = course
                    catalog.courses.append(course)
                room = str(row.get("clroName") or "")
                title = f"{subj} {begin[:16]}".strip()
                course.resources.append(
                    Resource(
                        resource_id=f"COUR-{cour_id}",
                        title=title,
                        course_id=str(tecl_id or ""),
                        course_name=subj,
                        teacher=teacher,
                        duration_sec=float(row.get("courTime") or 0),
                        record_time=begin,
                        play_url="",  # 播放地址按需解析（见 resolve_play_url）
                        chapter=room,
                        raw={**row, "acteId": acte_id, "termLabel": label},
                    )
                )
                total_videos += 1
        notify(f"课表汇总：{len(catalog.courses)} 门课 / {total_videos} 节有录播的课")
        return total_videos > 0

    # ------------------------------------------------------------------ #
    # 列表解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def extract_list(payload: Any, url: str = "", *, max_depth: int = 5) -> tuple[list[Any], int]:
        """从各种包装结构里抽出列表与总数。

        平台包装层数不固定（``data.rows`` / ``result.pageData.list`` /
        ``data.data.records`` …），所以这里做**有界深度优先搜索**：
        取第一个「元素全是对象的列表」作为数据行，并在同一层找 total。
        """
        if isinstance(payload, list):
            return payload, len(payload)
        if not isinstance(payload, dict):
            raise ApiChangedError(
                f"接口 {url} 返回结构无法识别（{type(payload).__name__}）",
                url=url,
                sample=json.dumps(payload, ensure_ascii=False)[:400] if payload is not None else "",
            )

        def scan(node: Any, depth: int) -> tuple[list[Any] | None, int]:
            if depth > max_depth:
                return None, 0
            if isinstance(node, dict):
                total = 0
                for key in _TOTAL_KEYS:
                    val = node.get(key)
                    if isinstance(val, (int, str)) and not isinstance(val, bool):
                        try:
                            total = int(val)
                            break
                        except (TypeError, ValueError):
                            continue
                # 先看这一层有没有列表容器
                for key in _LIST_KEYS:
                    val = node.get(key)
                    if isinstance(val, list):
                        return val, total or len(val)
                # 这一层的任意 list 值也算（但排除明显不是数据行的）
                dict_lists = [
                    v for k, v in node.items()
                    if isinstance(v, list) and k not in ("_note",)
                    and (v == [] or all(isinstance(x, (dict, list)) for x in v))
                ]
                if len(dict_lists) == 1:
                    return dict_lists[0], total or len(dict_lists[0])
                # 递归下探
                for key in list(_LIST_KEYS) + list(node.keys()):
                    child = node.get(key)
                    if isinstance(child, (dict, list)) and not isinstance(child, str):
                        found, found_total = scan(child, depth + 1)
                        if found is not None:
                            return found, found_total or total
                return None, total
            if isinstance(node, list):
                return node, len(node)
            return None, 0

        rows, total = scan(payload, 0)
        if rows is None:
            raise ApiChangedError(
                f"接口 {url} 响应里找不到列表字段，平台接口可能已变更",
                url=url,
                sample=json.dumps(payload, ensure_ascii=False)[:500],
            )
        return rows, total or len(rows)

    # ------------------------------------------------------------------ #
    # 清单抓取
    # ------------------------------------------------------------------ #
    def _try_endpoints(
        self,
        endpoints: Iterable[str],
        *,
        method: str = "POST",
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        cache_key: str = "",
    ) -> tuple[str, Any]:
        """按顺序探测可用接口，返回 (命中路径, 解包后的 payload)。

        顺序：**上次嗅探到的真实路径** → 内置候选路径 → （全都失败时）
        借已登录浏览器嗅探一次真实接口，再用嗅探结果重试。
        """
        if cache_key and cache_key in self._endpoint_cache:
            hit = self._endpoint_cache[cache_key]
            return hit, self.request(method, hit, params=params, json_body=body)

        discovered = self._discovered_for(cache_key)
        attempts = list(dict.fromkeys([*discovered, *endpoints]))

        errors: list[str] = []
        used_sniff = False
        while True:
            for path in attempts:
                try:
                    payload = self.request(method, path, params=params, json_body=body)
                except AuthExpiredError:
                    raise
                except (ApiChangedError, ApiBusinessError) as exc:
                    errors.append(f"{path}: {type(exc).__name__} {exc}")
                    continue
                except SiteUnreachableError:
                    raise
                except TranscribeHelperError as exc:
                    errors.append(f"{path}: {exc}")
                    continue
                if cache_key:
                    self._endpoint_cache[cache_key] = path
                return path, payload

            # 候选路径全灭：嗅探一次真实接口再试（只做一次，避免反复开浏览器）
            if used_sniff or not self._sniff_enabled:
                break
            used_sniff = True
            if self._sniff_endpoints():
                attempts = list(dict.fromkeys([*self._discovered_for(cache_key), *attempts]))
                continue
            break

        raise ApiChangedError(
            "所有候选接口都不可用，平台接口路径可能已变更。\n  " + "\n  ".join(errors[-8:]),
            url=", ".join(list(endpoints)[:4]),
        )

    # ------------------------------------------------------------------ #
    # 接口嗅探（反推真实路径）
    # ------------------------------------------------------------------ #
    def _discovered_for(self, cache_key: str) -> list[str]:
        """从嗅探结果里取该用途的候选路径。"""
        mapping = {
            "course_list": ("course", "list", "json"),
            "resource_list": ("list", "json"),
            "play": ("play", "json"),
        }
        out: list[str] = []
        for key in mapping.get(cache_key, ("json",)):
            out.extend(self.discovered_endpoints.get(key, []))
        return [p for p in dict.fromkeys(out) if p.startswith("/")]

    def _sniff_endpoints(self) -> bool:
        """借已登录浏览器打开一次页面，观察真实 XHR/Fetch 接口。"""
        if not self._sniff_enabled:
            return False
        self._sniff_enabled = False  # 每次运行只嗅探一次
        self._notify("候选接口都不可用，正在借浏览器嗅探真实接口（约 30~60 秒）…")
        try:
            from .login import sniff_api_paths

            found = sniff_api_paths(self.cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("接口嗅探失败：%s", exc)
            return False
        if any(found.get(k) for k in ("json", "list", "play", "course")):
            self.discovered_endpoints = {k: list(v) for k, v in found.items()}
            self._notify(
                "嗅探到真实接口："
                + "、".join(
                    f"{k}={len(v)}" for k, v in found.items() if v
                )
            )
            return True
        self._notify("嗅探未发现可用接口（可能页面未加载出资源列表）")
        return False

    def _get_json_via_browser_is_required(self) -> bool:
        """是否需要借浏览器上下文代打（动态签名场景的降级预案开关）。"""
        return bool(self.cfg.extra.get("use_browser_bridge", False))

    def fetch_courses(
        self,
        *,
        page_size: int = 100,
        max_pages: int = 200,
        endpoints: tuple[str, ...] | None = None,
    ) -> list[Course]:
        """拉取全部课程（自动翻页）。"""
        eps = endpoints or self.COURSE_ENDPOINTS
        courses: list[Course] = []
        page = 1
        seen: set[str] = set()
        while page <= max_pages:
            body = {"pageNum": page, "pageSize": page_size, "current": page, "size": page_size}
            self._notify(f"拉取课程列表 第 {page} 页…")
            path, payload = self._try_endpoints(eps, method="POST", body=body, cache_key="course_list")
            rows, total = self.extract_list(payload, path)
            if not rows:
                break
            for item in rows:
                try:
                    course = Course.from_api(item, strict=False)
                except ApiChangedError as exc:
                    log.warning("跳过无法解析的课程条目：%s", exc)
                    continue
                if course.course_id in seen:
                    continue
                seen.add(course.course_id)
                courses.append(course)
            log.info("课程页 %s：本页 %s 条，累计 %s 条（total=%s）", page, len(rows), len(courses), total)
            if total and len(courses) >= total:
                break
            if len(rows) < page_size:
                break
            page += 1
        return courses

    def fetch_resources(self, course: Course, *, page_size: int = 100, max_pages: int = 200) -> list[Resource]:
        """拉取某课程下全部资源（自动翻页）。"""
        resources: list[Resource] = []
        page = 1
        seen: set[str] = set()
        while page <= max_pages:
            body = {
                "courseId": course.course_id,
                "course_id": course.course_id,
                "id": course.course_id,
                "pageNum": page,
                "pageSize": page_size,
                "current": page,
                "size": page_size,
            }
            try:
                path, payload = self._try_endpoints(
                    self.RESOURCE_ENDPOINTS, method="POST", body=body, cache_key="resource_list"
                )
            except ApiChangedError:
                # 有些实现把资源内嵌在课程详情里
                if course.resources:
                    return course.resources
                raise
            rows, total = self.extract_list(payload, path)
            if not rows:
                break
            for item in rows:
                try:
                    res = Resource.from_api(
                        item,
                        course_id=course.course_id,
                        course_name=course.course_name,
                        teacher=course.teacher,
                        strict=False,
                    )
                except ApiChangedError:
                    continue
                if res.resource_id in seen:
                    continue
                seen.add(res.resource_id)
                resources.append(res)
            if total and len(resources) >= total:
                break
            if len(rows) < page_size:
                break
            page += 1
        return resources

    def fetch_catalog(
        self,
        *,
        on_progress: Callable[[str], None] | None = None,
        course_endpoints: tuple[str, ...] | None = None,
        resource_endpoints: tuple[str, ...] | None = None,
        save: bool = True,
    ) -> Catalog:
        """拉全量清单并（默认）落盘 ``data/catalog.json``。

        优先走**实测出来的真实平台接口**（2025 学年，见 docs/API.md §1）：
        换 jwt-token → 我的教学班 → 每班节次 → 每节课的录播与播放地址。
        只有在拿不到 jwt-token 时（例如对着模拟平台跑测试）才回退到
        「候选路径探测」的旧逻辑。
        """
        notify = on_progress or self._notify
        notify("开始拉取清单…")
        catalog = Catalog(
            fetched_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            student_id=self.cfg.student_id,
            source=self.cfg.portal_url,
        )
        if self.cfg.platform_api and not course_endpoints:
            try:
                if self.fetch_platform_catalog(catalog, notify=notify):
                    if save:
                        path = catalog.save(paths.catalog_path())
                        log.info("清单已保存：%s", path)
                        notify(f"清单已保存到 {path}")
                    notify(catalog.summary())
                    self.dump_network_log()
                    return catalog
            except AuthExpiredError:
                raise
            except (ApiChangedError, SiteUnreachableError) as exc:
                notify(f"真实接口不可用（{exc}），回退到候选路径探测…")
            except Exception as exc:  # noqa: BLE001
                log.warning("真实接口路径异常，回退候选探测：%s", exc)
                notify(f"真实接口路径异常（{type(exc).__name__}），回退候选探测…")

        courses = self.fetch_courses(endpoints=course_endpoints)
        notify(f"课程 {len(courses)} 门，开始逐门拉取资源…")
        for idx, course in enumerate(courses, 1):
            try:
                resources = self.fetch_resources(course) if not resource_endpoints else (
                    self.fetch_resources(course, max_pages=1)
                )
            except ApiChangedError as exc:
                log.error("课程《%s》资源拉取失败：%s", course.course_name, exc)
                notify(f"课程《{course.course_name}》资源拉取失败：{exc}")
                resources = course.resources
            course.resources = resources
            notify(f"[{idx}/{len(courses)}]《{course.course_name}》{len(resources)} 条录播")
            catalog.courses.append(course)

        if save:
            path = catalog.save(paths.catalog_path())
            log.info("清单已保存：%s", path)
            notify(f"清单已保存到 {path}")
        notify(catalog.summary())
        self.dump_network_log()
        return catalog

    # ------------------------------------------------------------------ #
    # 播放地址
    # ------------------------------------------------------------------ #
    def resolve_play_url(self, resource: Resource) -> str:
        """拿到最终的播放地址（HLS/mp4）。

        * 若清单里已带 ``play_url`` 直接用；
        * **真实平台**：用该节课的 courId 调
          ``GET /v1/course_vod_urls_new?courseId=<courId>``，返回带 ``auth_key`` 签名的直连 mp4
          （``data.courseVodVideoDtoList[].url``）；
        * 否则回退到「播放接口候选探测」并把 URL 做相对路径还原。
        """
        if resource.play_url and not resource.play_url.startswith(("javascript:", "#")):
            return self._absolutize(resource.play_url)

        cour_id = None
        if isinstance(resource.raw, dict):
            cour_id = resource.raw.get("id") or resource.raw.get("courId")
        if not cour_id and resource.resource_id.startswith("COUR-"):
            cour_id = resource.resource_id.split("-", 1)[1]
        if self.cfg.platform_api and cour_id:
            try:
                videos = self.fetch_course_videos(cour_id)
            except (AuthExpiredError, SiteUnreachableError):
                raise
            except ApiChangedError as exc:
                log.warning("真实播放接口失败，回退候选探测：%s", exc)
                videos = []
            if videos:
                # 优先取时长最长的（同一节课可能有多个分段/机位）
                best = max(videos, key=lambda v: float(v.get("vodTime") or 0))
                url = str(best.get("url") or "")
                if url:
                    resource.play_url = self._absolutize(url)
                    if not resource.duration_sec:
                        resource.duration_sec = float(best.get("vodTime") or 0)
                    if isinstance(resource.raw, dict):
                        resource.raw.setdefault("vods", [
                            {"vodId": v.get("vodId"), "vodTime": v.get("vodTime")} for v in videos
                        ])
                    return resource.play_url
            raise ApiChangedError(
                f"课程 {cour_id} 没有可用的录播地址（可能这节课没有录像权限或录像还在转码）"
            )

        body = {
            "resourceId": resource.resource_id,
            "id": resource.resource_id,
            "courseId": resource.course_id,
            "resourceType": resource.resource_type,
        }
        path, payload = self._try_endpoints(self.PLAY_ENDPOINTS, method="POST", body=body, cache_key="play")
        if not isinstance(payload, dict):
            raise ApiChangedError(f"播放接口 {path} 返回结构异常", url=path, sample=str(payload)[:300])
        url = str(
            pick(
                payload,
                "playUrl", "play_url", "url", "videoUrl", "hlsUrl", "m3u8Url", "m3u8",
                "fileUrl", "playPath", "path", "src",
                default="",
            )
            or ""
        )
        if not url:
            # 有的接口给的是多码率数组
            variants = pick(payload, "playUrls", "urls", "qualities", "list", default=None)
            if isinstance(variants, list) and variants:
                first = variants[0]
                if isinstance(first, dict):
                    url = str(pick(first, "url", "playUrl", "src", default="") or "")
                elif isinstance(first, str):
                    url = first
        if not url:
            raise ApiChangedError(
                f"播放接口 {path} 未返回播放地址，平台接口可能已变更",
                url=path,
                sample=json.dumps(payload, ensure_ascii=False)[:400],
            )
        resource.play_url = self._absolutize(url)
        if not resource.duration_sec:
            resource.duration_sec = float(pick(payload, "duration", "durationSec", "timeLength", default=0) or 0)
        return resource.play_url

    def _absolutize(self, url: str) -> str:
        if url.startswith("//"):
            return f"{urlparse(self.cfg.api_base).scheme}:{url}"
        if url.startswith("http"):
            return url
        return urljoin(self.cfg.api_base.rstrip("/") + "/", url.lstrip("/"))

    # ------------------------------------------------------------------ #
    # 其它
    # ------------------------------------------------------------------ #
    def _notify(self, message: str) -> None:
        log.info("%s", message)
        if self.on_progress:
            try:
                self.on_progress(message)
            except Exception:
                pass

    def dump_network_log(self, path: Path | None = None) -> Path:
        """把**脱敏后**的接口记录写到 ``recon/network.jsonl``。

        ``recon/network.jsonl`` 的用途是**记录真实平台流量**，供反推接口用
        （见 docs/API.md）。所以这里有一条刻意加的护栏（缺陷 39）：

        * 调用方显式给了 ``path`` → 照写（测试/脚本要隔离时走这条路）；
        * 默认路径 + 本次记录**全部指向本机环回**（`127.0.0.1` / `localhost` / `::1`）
          → **不写文件**，但**记录保留在内存**里，调用方随后仍可用显式 ``path`` 落盘。

        因为「模拟平台的流量」写进真实抓包文件里没有任何价值，却会把真实请求埋掉
        （实测：该文件曾累积 644 条 `127.0.0.1` 记录，真实请求只有 5 条，
        而且真实登录进行中也会被并发灌入）。
        """
        target = Path(path) if path else (paths.recon_dir() / "network.jsonl")
        count = len(self.network_log)
        if path is None and count and all(_is_loopback_url(str(i.get("url", ""))) for i in self.network_log):
            log.info(
                "本次接口记录全部是本机模拟流量（%s 条），不写入真实抓包文件 %s"
                "（记录仍在内存，可用 dump_network_log(显式路径) 另存）",
                count, target,
            )
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            for item in self.network_log:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.network_log.clear()
        log.debug("接口记录已写入 %s（%s 条，已脱敏）", target, count)
        return target
