"""回归（缺陷 52）：日志轮转撞上另一个进程持有的句柄时，不能丢日志、不能刷堆栈。

实测现场：验收脚本与 GUI 同时写 `logs/app.log`，2MB 轮转时
`RotatingFileHandler.doRollover()` 去 `os.rename` 对方正开着的文件 →
Windows 回 `PermissionError: [WinError 32]` → logging 打印一屏 `--- Logging error ---`
并**丢掉触发轮转的那条记录**。

钉住行为的方式：让底层的 `os.rename` 失败（**不是**替换 `doRollover` 本身 ——
那样会把被测的 try/except 一起绕过去，第一版就是这么写成假绿的）：
* 记录仍然写进文件（不丢）
* 不冒出 logging 内部堆栈
* 提示只出现一次
"""

from __future__ import annotations

import logging
import os
import sys

from ecnu_transcribe.logbus import _MultiProcessSafeRotatingHandler


class _FakeTty:
    """假终端：`isatty()` 为真 —— 轮转提示只在真终端里输出（否则会污染 CLI 退出码）。"""

    def __init__(self) -> None:
        self.buf: list[str] = []

    def write(self, text: str) -> int:
        self.buf.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return True

    @property
    def text(self) -> str:
        return "".join(self.buf)


def _handler(target, *, max_bytes: int = 200) -> _MultiProcessSafeRotatingHandler:
    # backupCount 给足：本用例要断言「一条不丢」，而正常语义下超出 backupCount 的
    # 旧备份是**应当**被删掉的（第一版没注意，误判成丢日志）。
    handler = _MultiProcessSafeRotatingHandler(
        target, maxBytes=max_bytes, backupCount=50, encoding="utf-8", delay=True
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _logger(name: str, handler: logging.Handler) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def test_rollover_failure_keeps_writing_and_does_not_raise(tmp_path, capsys, monkeypatch):
    target = tmp_path / "app.log"
    handler = _handler(target)
    logger = _logger("test.logbus.rollover", handler)
    fake_tty = _FakeTty()
    monkeypatch.setattr(sys, "stderr", fake_tty)

    real_rename = os.rename
    calls = {"n": 0}

    def flaky_rename(src, dst, *a, **kw):  # noqa: ANN001
        # 只在「轮转 app.log → app.log.1」这一步失败一次，模拟对方进程占着句柄
        if str(dst).endswith("app.log.1") and calls["n"] == 0:
            calls["n"] += 1
            raise PermissionError(32, "另一个程序正在使用此文件")
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", flaky_rename)
    try:
        for i in range(40):
            logger.info("第 %02d 行 %s", i, "x" * 40)
    finally:
        handler.close()

    assert calls["n"] == 1, "本用例需要真的撞上一次轮转失败"
    # 撞过失败之后**后续轮转仍会成功**（第一次失败只是这一轮不转），
    # 所以「一条不丢」要跨当前文件与所有 .1/.2 备份一起看。
    body = "".join(
        p.read_text(encoding="utf-8")
        for p in sorted(tmp_path.glob("app.log*"))
        if p.is_file()
    )
    missing = [i for i in range(40) if f"第 {i:02d} 行" not in body]
    assert not missing, f"这些行在轮转中丢了：{missing}"
    assert "第 39 行" in target.read_text(encoding="utf-8"), "最后一条必须落在当前文件里"
    err = capsys.readouterr().err
    assert "Logging error" not in err, f"不该冒出 logging 内部堆栈：{err[:300]}"
    assert "日志轮转暂时失败" in fake_tty.text, f"应当提示一次：{fake_tty.text[:300]}"


def test_rollover_warning_is_silent_when_stderr_is_a_pipe(tmp_path, capsys, monkeypatch):
    """stderr 是管道（脚本/CI）时不写提示 —— 否则 PowerShell 会把退出码染成 1。"""
    target = tmp_path / "app3.log"
    handler = _handler(target, max_bytes=120)
    logger = _logger("test.logbus.rollover3", handler)

    class _Pipe:
        def __init__(self) -> None:
            self.buf: list[str] = []

        def write(self, text: str) -> int:
            self.buf.append(text)
            return len(text)

        def flush(self) -> None:
            return None

        def isatty(self) -> bool:
            return False

    pipe = _Pipe()
    monkeypatch.setattr(sys, "stderr", pipe)

    def always_fail(src, dst, *a, **kw):  # noqa: ANN001
        if str(dst).endswith((".1", ".2")):
            raise PermissionError(32, "另一个程序正在使用此文件")
        return os.rename(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", always_fail)
    try:
        for i in range(30):
            logger.info("行 %02d %s", i, "y" * 30)
    finally:
        handler.close()

    assert pipe.buf == [], f"管道里不该出现提示：{pipe.buf[:2]}"
    assert "Logging error" not in capsys.readouterr().err
    assert "行 29" in target.read_text(encoding="utf-8"), "日志不能丢"


def test_rollover_warning_is_emitted_once(tmp_path, capsys, monkeypatch):
    target = tmp_path / "app2.log"
    handler = _handler(target, max_bytes=120)
    logger = _logger("test.logbus.rollover2", handler)
    fake_tty = _FakeTty()
    monkeypatch.setattr(sys, "stderr", fake_tty)

    def always_fail(src, dst, *a, **kw):  # noqa: ANN001
        if str(dst).endswith((".1", ".2")):
            raise PermissionError(32, "另一个程序正在使用此文件")
        return os.rename(src, dst, *a, **kw)

    monkeypatch.setattr(os, "rename", always_fail)
    try:
        for i in range(30):
            logger.info("行 %02d %s", i, "y" * 30)
    finally:
        handler.close()

    assert fake_tty.text.count("日志轮转暂时失败") == 1, f"提示应当只出现一次：{fake_tty.text[:400]}"
    assert "Logging error" not in capsys.readouterr().err, "不该冒出 logging 内部堆栈"
    assert "行 29" in target.read_text(encoding="utf-8"), "日志不能丢"
