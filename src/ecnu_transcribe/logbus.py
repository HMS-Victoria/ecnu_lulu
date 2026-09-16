"""统一日志总线：Qt 信号 / 标准 logging / 文件轮转 / 敏感信息脱敏。

两条使用路径：
    * 核心库（无 Qt 依赖）用 :func:`get_logger` 拿标准 logger。
    * GUI 用 :class:`LogBus` 订阅，日志既能落盘又能在右栏实时显示。

**脱敏**是本模块的硬性职责：任何写出的日志都先过 :func:`redact`，
确保 Cookie / Authorization / api_key / 密码 / 手机号不会进入日志文件。
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections.abc import Callable, Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import paths

_LOGGER_NAME = "ecnu_transcribe"
_configured = False
_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
_SENSITIVE_KEYS = (
    "cookie",
    "set-cookie",
    "authorization",
    "auth",
    "token",
    "access_token",
    "refresh_token",
    "jsessionid",
    "session",
    "sessionid",
    "password",
    "passwd",
    "pwd",
    "secret",
    "api_key",
    "apikey",
    "api-key",
    "app_key",
    "appkey",
    "access_key",
    "accesskey",
    "signature",
    "sign",
    "vpn_key",
    "ticket",
    "code",
    "state",
    "csrf",
    "xsrf",
    "phone",
    "mobile",
    "idcard",
    "id_card",
    "username",
    "account",
    "学号",
)

_KEY_ALT = "|".join(re.escape(k) for k in sorted(_SENSITIVE_KEYS, key=len, reverse=True))

#: 只有这些「毫无歧义」的键名才允许匹配**不带引号**的 ``key=value`` 形式。
#: 像 ``code`` / ``state`` / ``session`` 这类词在普通句子里太常见，必须带引号才处理，
#: 否则会把 "storage_state = <路径>" 之类的正常日志也遮掉。
_BARE_KEYS = (
    "password", "passwd", "pwd", "api_key", "apikey", "api-key", "app_key", "appkey",
    "access_key", "accesskey", "secret", "token", "access_token", "refresh_token",
    "jsessionid", "sessionid", "cookie", "authorization", "vpn_key", "signature",
    "ticket", "mobile", "phone", "id_card", "idcard", "csrf", "xsrf",
)
_BARE_ALT = "|".join(re.escape(k) for k in sorted(_BARE_KEYS, key=len, reverse=True))

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Cookie: a=b; c=d
    (re.compile(r"(?i)\b(cookie|set-cookie)\s*[:=]\s*[^\r\n]*"), r"\1: <REDACTED>"),
    # Authorization: Bearer xxx
    (re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*[^\r\n]*"), r"\1: <REDACTED>"),
    # JSON / 带引号的敏感键（值必须带引号，避免误伤普通句子）
    (re.compile(rf'(?i)(["\'])({_KEY_ALT})(["\']\s*[:=]\s*)(["\'])([^"\']*)'), r"\1\2\3\4<REDACTED>"),
    # 无歧义键名的 key=value（值不带引号；= 两侧不许有空格，避免误伤 "state = path"）
    (re.compile(rf"(?i)(?<![\w])({_BARE_ALT})=([^\s,;&\"']+)"), r"\1=<REDACTED>"),
    # URL query 里的敏感键
    (re.compile(rf"(?i)([?&]({_KEY_ALT})=)([^&#\s]+)"), r"\1<REDACTED>"),
)

#: 需要按「整段数字」识别的个人标识长度（不依赖正则 lookaround，行为可预期）
_SID_DIGIT_LENGTH = 10            # 华东师大学号形如 10xxxxxxxx
_SID_PREFIX = "10"                # 学号前缀（与手机号 1[3-9] 不冲突）
_ID_DIGIT_LENGTH = 18             # 中国大陆身份证（末位可能是 X）
_NUMERIC_RUN = re.compile(r"\d+")
#: 已知的「用户本人标识」字面量（学号等），加载配置时登记，确保一定被脱敏
_KNOWN_IDENTIFIERS: set[str] = set()


def register_identifier(value: str) -> None:
    """登记用户本人的标识（学号等）。长度 >= 6 才登记，避免误伤普通数字。"""
    value = (value or "").strip()
    if len(value) >= 6:
        _KNOWN_IDENTIFIERS.add(value)


def _mask_numeric_identifiers(text: str) -> str:
    """掩掉手机号 / 身份证 / 学号。

    不用 lookaround（实测在同一解释器上 ``(?!\\d)`` 的表现不一致），
    改为扫描数字串并按长度判定；命中后把可选的尾随 ``X``（身份证校验位）
    一并吃掉，避免留下 ``X`` 尾巴。
    """
    out: list[str] = []
    cursor = 0
    for match in _NUMERIC_RUN.finditer(text):
        start, end = match.span()
        run = match.group(0)
        if start < cursor:
            continue  # 已被上一条规则覆盖
        prev_ch = text[start - 1] if start > 0 else ""
        next_ch = text[end] if end < len(text) else ""
        # 身份证末位校验位可能是 X：17 位数字 + X 整体处理（必须在字母邻接跳过之前）
        id_with_x = len(run) == _ID_DIGIT_LENGTH - 1 and next_ch in ("X", "x")
        if id_with_x:
            out.append(text[cursor:start])
            out.append("<REDACTED-ID>")
            cursor = end + 1
            continue
        # 紧邻字母数字则视为普通标识（mp3 / x264 / h264 等），不动它
        if (prev_ch.isalnum() and prev_ch.isascii()) or (next_ch.isalnum() and next_ch.isascii()):
            continue
        replacement = _classify_numeric_run(run)
        if replacement is None:
            continue
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _classify_numeric_run(run: str) -> str | None:
    """按「整段数字」的长度判定是否属于敏感标识。

    判定必须**按长度精确匹配**，任何分支都不能对「其它长度」提前返回，
    否则会短路掉后面的规则（这里曾经踩过一次坑）。

    注意：华东师大学号是 **10-11 位**且以 ``10`` 开头（如 ``20261234567``），
    而手机号要求第二位在 ``3-9``，两者不冲突；以 ``10`` 开头的 10/11 位数字
    更可能是学号，因此不做手机号掩码。
    """
    n = len(run)
    if n == _ID_DIGIT_LENGTH:
        return "<REDACTED-ID>"
    if n == 11 or n == _SID_DIGIT_LENGTH:
        if len(run) >= 2 and run[0] == "1" and run[1] in "3456789":
            return "<REDACTED-PHONE>"
        if run.startswith(_SID_PREFIX):
            return "<REDACTED-SID>"
        return None
    return None


_MASK = "<REDACTED>"


def redact(text: object, *, extra_secrets: Iterable[str] = ()) -> str:
    """把文本中的敏感信息替换成占位符。

    除了固定的正则规则，还会替换：
        * 运行期通过 :func:`register_secret` 登记的密钥（API Key / 密码）；
        * 通过 :func:`register_identifier` 登记的本人标识（学号）。
    """
    s = str(text)
    for pat, repl in _REDACTIONS:
        s = pat.sub(repl, s)
    s = _mask_numeric_identifiers(s)

    secrets: set[str] = set(_EXTRA_SECRETS)
    secrets.update(str(x) for x in extra_secrets if x)
    for secret in secrets:
        if len(secret) >= 4:
            s = s.replace(secret, _MASK)

    # 本人标识（学号等）最后兜底替换
    for ident in _KNOWN_IDENTIFIERS:
        s = s.replace(ident, "<REDACTED-SID>")
    return s


def register_secret(value: str) -> None:
    """把运行期拿到的高价值密钥登记为需要脱敏的字面量（api_key / 密码）。"""
    if value and len(value) >= 4:
        _EXTRA_SECRETS.add(value)


_EXTRA_SECRETS: set[str] = set()


# --------------------------------------------------------------------------- #
# 日志格式
# --------------------------------------------------------------------------- #
class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        return redact(super().format(record))


class _MultiProcessSafeRotatingHandler(RotatingFileHandler):
    """轮转失败时**不要丢日志、也不要刷一屏 traceback**（缺陷 52）。

    触发场景是真实的：桌面应用被打开两次（或验收脚本与 GUI 并发），
    两个进程持有同一个 ``logs/app.log``；轮到 2MB 时
    :meth:`RotatingFileHandler.doRollover` 去 ``os.rename`` 一个**对方正开着的**文件，
    Windows 直接回 ``PermissionError: [WinError 32]``（句柄没有 FILE_SHARE_DELETE）。

    后果比「少转一次」糟：logging 会把异常打到 stderr 并**丢掉触发轮转的那条记录**，
    日志里于是出现一屏与业务无关的堆栈。

    这里的做法：轮转失败就**本轮不轮转**（继续往当前文件追加），
    并只提示一次；等另一个进程退出后自然还会轮转。
    """

    def doRollover(self) -> None:  # noqa: D102
        try:
            super().doRollover()
        except OSError as exc:
            if not getattr(self, "_rollover_warned", False):
                self._rollover_warned = True
                # 只在**真终端**里提示：如果 stderr 是管道（脚本调用、CI），
                # 写 stderr 会被 PowerShell 当成 NativeCommandError，把退出码
                # 染成 1，从而掩盖真正的结果 —— 这类提示不值得付这个代价。
                try:
                    if sys.stderr is not None and sys.stderr.isatty():
                        sys.stderr.write(
                            "[logbus] 日志轮转暂时失败（多为另一个进程正持有同一日志文件），"
                            f"继续写当前文件：{exc}\n"
                        )
                except Exception:  # noqa: BLE001
                    pass


def _build_file_handler(path: Path) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _MultiProcessSafeRotatingHandler(
        path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True
    )
    handler.setFormatter(
        _RedactingFormatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    )
    return handler


def get_logger(name: str = "") -> logging.Logger:
    """取得项目 logger；首次调用时完成配置。"""
    setup_logging()
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)


def setup_logging(level: int = logging.INFO, *, log_file: Path | None = None) -> logging.Logger:
    """幂等地配置根 logger（控制台 + 轮转文件 + GUI 总线）。"""
    global _configured
    with _lock:
        root = logging.getLogger(_LOGGER_NAME)
        if _configured:
            return root
        root.setLevel(logging.DEBUG)
        root.propagate = False

        # 只在**真终端**里往 stderr 打日志：
        # ① 打包成 GUI exe 时根本没有控制台；② 脚本被 PowerShell 调用且 stderr 是管道时，
        # 任何一行 stderr 都会被当成 NativeCommandError，把**成功**脚本的退出码染成 1
        # —— 自动化判读就不可靠了（本轮实测反复踩到）。日志照旧全量写 logs/app.log。
        if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
            stream = logging.StreamHandler(stream=sys.stderr)
            stream.setLevel(level)
            stream.setFormatter(
                _RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
            )
            root.addHandler(stream)

        try:
            target = log_file or (paths.log_dir() / "app.log")
            fh = _build_file_handler(Path(target))
            fh.setLevel(logging.DEBUG)
            root.addHandler(fh)
        except OSError:  # 只读目录等，退化为仅控制台
            pass

        root.addHandler(LogBus.instance())
        _configured = True
        return root


# --------------------------------------------------------------------------- #
# GUI 日志总线
# --------------------------------------------------------------------------- #
class LogBus(logging.Handler):
    """既是 ``logging.Handler``，又是可订阅的事件源。

    核心库通过标准 logging 写入，GUI 通过 :meth:`subscribe` 收到
    ``(level_name, message)`` 回调；回调在**写日志的线程**里触发，
    PySide6 侧应把它转成 QueuedConnection 的信号再更新 UI。
    """

    _singleton: LogBus | None = None

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        self._subscribers: list[Callable[[str, str], None]] = []
        self._history: list[tuple[str, str]] = []
        self._history_limit = 2000
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> LogBus:
        if cls._singleton is None:
            cls._singleton = cls()
        return cls._singleton

    # -- logging.Handler ---------------------------------------------------- #
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # pragma: no cover - 日志不能反过来炸掉主流程
            return
        with self._lock:
            self._history.append((record.levelname, msg))
            if len(self._history) > self._history_limit:
                del self._history[: len(self._history) - self._history_limit]
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(record.levelname, msg)
            except Exception:
                continue

    # -- 订阅 --------------------------------------------------------------- #
    def subscribe(self, callback: Callable[[str, str], None]) -> Callable[[], None]:
        """订阅后续日志；返回取消订阅的函数。"""
        with self._lock:
            self._subscribers.append(callback)
        return lambda: self.unsubscribe(callback)

    def unsubscribe(self, callback: Callable[[str, str], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def history(self) -> list[tuple[str, str]]:
        """已产生的日志（供 GUI 初次挂载时回填）。"""
        with self._lock:
            return list(self._history)

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
