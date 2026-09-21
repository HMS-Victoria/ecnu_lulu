"""大夏学堂录播转写助手 —— 核心库。

模块划分：
    config      配置与路径（含 DPAPI 加密的凭据存储）
    logbus      统一日志总线（GUI 实时日志 + 文件日志 + 脱敏）
    store       sqlite3 状态库（任务、断点续跑）
    paths       运行时路径解析（开发态 / PyInstaller 冻结态）
    media       ffmpeg 定位与音视频工具
    catalog     清单数据模型
    client      大夏学堂 / 资源管理平台 HTTP 客户端（登录态复用、翻页、AuthExpiredError）
    login       Playwright 人工登录 + 抓包 + storage_state 落盘
    downloader  ffmpeg 拉流取音频（HLS/AES-128/mp4）+ 缓存 + 重试
    transcriber ASR 抽象 + 阿里云百炼 DashScope + OpenAI 兼容 + 本地兜底
    llm         DeepSeek 文本后处理（纠错/标点/术语/分段/摘要）
    pipeline    下载→转写→产物（txt/srt/md）流水线 + 断点续跑
    exporter    txt / srt / md 产物写出
    pausegate   暂停闸门（作用于任务内部的天然断点）
"""

__version__ = "0.12.0"
__all__ = ["__version__"]
