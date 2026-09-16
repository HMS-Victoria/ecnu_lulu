"""大夏学堂录播转写助手 —— 源码入口。

用法::

    .venv\\Scripts\\python main.py            # 启动 GUI
    .venv\\Scripts\\python main.py --selftest # 冒烟测试（2 秒后自动退出）

打包后的可执行文件为 ``dist/大夏学堂转写助手/大夏学堂转写助手.exe``。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
