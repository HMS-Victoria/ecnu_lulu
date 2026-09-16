"""pytest 公共夹具。

原则：**不依赖网络**。所有需要真实平台数据的地方都用 ``tests/fixtures/`` 里的
录制样本（fixture），这样即使平台接口变了，回归测试也能稳定跑。

另一条同样重要的原则：**测试绝不写工作区**。见 :func:`_isolate_app_home`。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _isolate_app_home(tmp_path_factory):
    """把「应用可写根目录」与「用户数据目录」都指向临时目录。

    这是缺陷 39 的修复：`EcnuClient.fetch_catalog()` 结尾会
    `dump_network_log()` → 写到 `paths.recon_dir()/network.jsonl`，而
    `recon/network.jsonl` 是**真实抓包**产物（用于反推平台接口）。
    于是每跑一次测试套件就往里灌 20 多条 `127.0.0.1` 的模拟流量 ——
    实测该文件一度累积到 **644 条**，把 5 条真实请求彻底埋掉；
    更糟的是它会在**真实登录进行中**并发写入同一个文件，真伪混在一处。

    同时这也防止测试顺手在 `%LOCALAPPDATA%\\ecnu-transcribe\\` 里建浏览器目录
    （`LoginSession` 默认会解析 `browser_profile_dir()`），或往工作区的
    `data/`、`cache/`、`output/`、`logs/` 里写东西。

    注意：`paths.resource_root()`（只读的仓库/打包资源，测试用它定位
    `scripts/`、`tests/*_server.py`）**不受影响** —— 那些路径不经过 app_root。
    """
    home = tmp_path_factory.mktemp("ecnu-home")
    local = home / "localappdata"
    local.mkdir(parents=True, exist_ok=True)

    saved = {k: os.environ.get(k) for k in ("ECNU_TRANSCRIBE_HOME", "LOCALAPPDATA")}
    os.environ["ECNU_TRANSCRIBE_HOME"] = str(home)
    os.environ["LOCALAPPDATA"] = str(local)
    try:
        yield home
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="session", autouse=True)
def _configure_logging(tmp_path_factory):
    """把项目日志接到 LogBus（但不写工作区日志文件），验证脱敏链路。"""
    import logging

    from ecnu_transcribe.logbus import LogBus, setup_logging

    setup_logging(log_file=tmp_path_factory.mktemp("logs") / "test.log")
    root = logging.getLogger("ecnu_transcribe")
    assert LogBus.instance() in root.handlers
    yield


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    return FIXTURES


@pytest.fixture()
def tmp_store(tmp_path):
    from ecnu_transcribe.store import StateStore

    store = StateStore(tmp_path / "state.db")
    yield store
    store.close()


@pytest.fixture()
def cfg(tmp_path):
    from ecnu_transcribe.config import AppConfig

    c = AppConfig()
    c.output_dir = str(tmp_path / "output")
    c.cache_enabled = True
    return c


@pytest.fixture()
def tone_audio(tmp_path):
    """用 ffmpeg 生成一段 3 秒静音 + 提示音的 wav（离线，不依赖网络）。"""
    from ecnu_transcribe import media

    out = tmp_path / "tone.wav"
    exe = media.find_ffmpeg()
    cmd = [
        str(exe), "-hide_banner", "-nostdin", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
    ]
    media.run_ffmpeg(cmd, timeout=60, check=True)
    assert out.is_file() and out.stat().st_size > 1000
    return out


@pytest.fixture()
def sample_catalog_json() -> dict:
    """一份**结构仿真但内容虚构**的清单 fixture（字段名沿用平台常见命名）。"""
    return json.loads((FIXTURES / "catalog_sample.json").read_text(encoding="utf-8"))
