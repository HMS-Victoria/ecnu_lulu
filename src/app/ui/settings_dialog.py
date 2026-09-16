"""设置对话框：语音识别（ASR）/ DeepSeek / 输出 / 网络 / 高级。

关键的用户教育点（必须在界面上写清楚，避免误解）
------------------------------------------------
    **ASR ≠ DeepSeek**：DeepSeek 开放平台没有语音转文字接口，
    转写必须由 DashScope / 任意 OpenAI 兼容端点 / 本地 faster-whisper 完成；
    DeepSeek 只用于转写后的文本加工（纠错、分段、摘要）。

API Key 用 Windows DPAPI 加密后存到 ``%LOCALAPPDATA%\\ecnu-transcribe\\secrets.json``，
绝不写入源码、config.json 或日志。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ecnu_transcribe import media, paths
from ecnu_transcribe.config import AppConfig, ConfigManager, dpapi_available

from ..workers import ProbeWorker
from . import theme

ASR_PRESETS: list[tuple[str, str, str, str]] = [
    ("① 本地 faster-whisper（零成本、无需 Key、音频不出本机）", "openai_compatible", "http://127.0.0.1:8000/v1", "faster-whisper-small"),
    ("② 阿里云百炼 DashScope（中文课堂最准，推荐）", "dashscope", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-asr-flash"),
    ("③ 硅基流动 SiliconFlow", "openai_compatible", "https://api.siliconflow.cn/v1", "FunAudioLLM/SenseVoiceSmall"),
    ("④ OpenAI 官方", "openai_compatible", "https://api.openai.com/v1", "whisper-1"),
    ("⑤ 本地 faster-whisper 进程内模式（实验性，不推荐）", "faster_whisper_local", "", "small"),
    ("⑥ 自定义 OpenAI 兼容端点", "openai_compatible", "", ""),
]

#: 「预设」下拉里代表「当前配置不匹配任何预设」的那一项。
#: 必须存在：多个预设共用同一个 provider（都是 openai_compatible），
#: 只按 provider 反查会把「本地服务」显示成「OpenAI 官方」，用户会被误导。
CUSTOM_PRESET_LABEL = "（自定义 / 当前配置）"

#: 本地 ASR 服务的启动指引（写进设置页说明里，避免用户不知道有这个选项）
LOCAL_ASR_HINT = (
    "启动本地 ASR 服务（零成本，不需要任何云 Key）：<br>"
    "<code>.venv\\Scripts\\python scripts\\local_asr_server.py --model small --port 8000</code><br>"
    "然后把上面的「预设」选 <b>①</b>，并点「连通性自检」确认可用。"
)


class SettingsDialog(QDialog):
    def __init__(self, cfg: AppConfig, cm: ConfigManager, parent=None) -> None:
        super().__init__(parent)
        self.cm = cm
        self.cfg = cfg
        self.setWindowTitle("设置")
        self.resize(900, 720)
        self._probe_worker: ProbeWorker | None = None

        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_asr(), "语音识别（ASR）")
        self.tabs.addTab(self._tab_llm(), "文本加工（DeepSeek）")
        self.tabs.addTab(self._tab_output(), "输出与产物")
        self.tabs.addTab(self._tab_media(), "下载与媒体")
        self.tabs.addTab(self._tab_network(), "网络与登录")
        self.tabs.addTab(self._tab_advanced(), "高级")
        outer.addWidget(self.tabs, 1)

        self.lbl_probe = QLabel("")
        self.lbl_probe.setWordWrap(True)
        self.lbl_probe.setStyleSheet(
            f"color:{theme.TEXT}; background:{theme.BG_ALT}; padding:6px; border-radius:4px;"
        )
        outer.addWidget(self.lbl_probe)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.btn_check = buttons.addButton("连通性自检", QDialogButtonBox.ActionRole)
        self.btn_check.clicked.connect(self.on_probe)
        buttons.accepted.connect(self.on_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._load()

    # ================================================================== #
    # 各标签页
    # ================================================================== #
    def _scrolled(self, inner: QWidget) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        return scroll

    def _tab_asr(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)

        notice = QLabel(
            "<b style='color:#b42318'>先看清楚：语音转文字（ASR）和 DeepSeek 是两件事。</b><br>"
            "• <b>ASR</b>（把音频变成文字）：由下面的端点完成。<br>"
            "&nbsp;&nbsp;- <b>零成本方案</b>：本地 faster-whisper（预设 ①），不需要任何 API Key，"
            "音频不出本机；<br>"
            "&nbsp;&nbsp;- <b>最准方案</b>：<b>阿里云百炼 DashScope</b>（预设 ②，"
            "<code>qwen3-asr-flash</code>，中文课堂场景明显更准；注意它**不返回时间戳**，<br>&nbsp;&nbsp;&nbsp;&nbsp;字幕时间轴按静音切分边界给出）。<br>"
            "• <b>DeepSeek</b>（把文字改得更好）：<b>没有</b>语音转文字接口，"
            "你填了 DeepSeek Key 也<b>不能</b>直接转写，它只做「文本加工」——见下一个标签页。"
        )
        notice.setWordWrap(True)
        notice.setStyleSheet(
            f"background:{theme.NOTICE_BG}; border:1px solid {theme.NOTICE_BORDER};"
            f" padding:8px; border-radius:4px;"
        )
        lay.addWidget(notice)

        local_hint = QLabel(LOCAL_ASR_HINT)
        local_hint.setWordWrap(True)
        local_hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        local_hint.setStyleSheet(
            f"background:{theme.INFO_BG}; border:1px solid {theme.INFO_BORDER};"
            f" padding:8px; border-radius:4px;"
        )
        lay.addWidget(local_hint)

        box = QGroupBox("ASR 端点")
        form = QFormLayout(box)

        self.cmb_preset = QComboBox()
        for label, _prov, _base, _model in ASR_PRESETS:
            self.cmb_preset.addItem(label)
        self.cmb_preset.addItem(CUSTOM_PRESET_LABEL)
        self.cmb_preset.currentIndexChanged.connect(self._on_preset_changed)
        form.addRow("预设", self.cmb_preset)

        self.cmb_provider = QComboBox()
        self.cmb_provider.addItem("阿里云百炼 DashScope（官方模型名如 qwen3-asr-flash）", "dashscope")
        self.cmb_provider.addItem("OpenAI 兼容端点（/v1/audio/transcriptions）", "openai_compatible")
        self.cmb_provider.addItem("本地 faster-whisper（离线、无需 Key）", "faster_whisper_local")
        form.addRow("类型", self.cmb_provider)

        self.edit_base = QLineEdit()
        self.edit_base.setPlaceholderText("https://dashscope.aliyuncs.com/compatible-mode/v1")
        form.addRow("Base URL", self.edit_base)

        key_row = QHBoxLayout()
        self.edit_key = QLineEdit()
        self.edit_key.setEchoMode(QLineEdit.Password)
        self.edit_key.setPlaceholderText("在此粘贴 ASR API Key（不会写进日志）")
        btn_show = QPushButton("显示")
        btn_show.setCheckable(True)
        btn_show.setFixedWidth(56)
        btn_show.toggled.connect(
            lambda on: self.edit_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        btn_clear = QPushButton("清除")
        btn_clear.setFixedWidth(56)
        btn_clear.clicked.connect(lambda: self.edit_key.setText(""))
        key_row.addWidget(self.edit_key, 1)
        key_row.addWidget(btn_show)
        key_row.addWidget(btn_clear)
        wrapper = QWidget()
        wrapper.setLayout(key_row)
        form.addRow("API Key", wrapper)

        self.edit_model = QLineEdit()
        self.edit_model.setPlaceholderText("qwen3-asr-flash")
        form.addRow("模型", self.edit_model)

        self.cmb_lang = QComboBox()
        for code, label in [
            ("zh", "中文（推荐）"), ("en", "英语"), ("ja", "日语"), ("ko", "韩语"),
            ("yue", "粤语"), ("auto", "自动检测"),
        ]:
            self.cmb_lang.addItem(label, code)
        form.addRow("语言", self.cmb_lang)

        self.chk_native = QCheckBox("使用 DashScope 原生异步 API（大文件/长音频更稳，需要能访问 OSS）")
        form.addRow("", self.chk_native)

        self.chk_timestamps = QCheckBox("请求时间戳（生成 .srt 必需；不勾选则 .srt 的时间轴会退化）")
        form.addRow("", self.chk_timestamps)

        self.chk_diarization = QCheckBox("说话人分离（若端点支持）")
        form.addRow("", self.chk_diarization)

        self.edit_vocab = QLineEdit()
        self.edit_vocab.setPlaceholderText("提示词/术语表，如：拓扑排序、动态规划、傅里叶变换…（可留空）")
        form.addRow("术语提示", self.edit_vocab)
        lay.addWidget(box)

        box2 = QGroupBox("长音频切分（端点有体积/时长上限时自动生效）")
        form2 = QFormLayout(box2)
        self.spin_max_mb = QDoubleSpinBox()
        self.spin_max_mb.setRange(1.0, 500.0)
        self.spin_max_mb.setSuffix(" MB")
        form2.addRow("单次上传上限", self.spin_max_mb)

        self.spin_max_seg = QSpinBox()
        self.spin_max_seg.setRange(30, 7200)
        self.spin_max_seg.setSuffix(" 秒")
        form2.addRow("单段最长", self.spin_max_seg)

        self.spin_overlap = QDoubleSpinBox()
        self.spin_overlap.setRange(0.0, 10.0)
        self.spin_overlap.setSingleStep(0.5)
        self.spin_overlap.setSuffix(" 秒")
        form2.addRow("段间重叠", self.spin_overlap)

        self.cmb_chunk = QComboBox()
        self.cmb_chunk.addItem("按静音切分（推荐，切点自然）", "silence")
        self.cmb_chunk.addItem("等长切分（无静音信息时）", "fixed")
        form2.addRow("切分策略", self.cmb_chunk)

        self.spin_asr_retries = QSpinBox()
        self.spin_asr_retries.setRange(1, 10)
        form2.addRow("失败重试次数", self.spin_asr_retries)
        lay.addWidget(box2)
        lay.addStretch(1)
        return self._scrolled(page)

    # ------------------------------------------------------------------ #
    def _tab_llm(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        notice = QLabel(
            "DeepSeek 负责<b>转写之后的文字加工</b>：错别字与标点修复、课程专业术语纠正、"
            "口语冗余清理、按语义重新分段、生成结构化摘要与大纲。"
            "这是<b>可选</b>功能——关掉它也能正常产出 .txt/.srt/.md（就是 ASR 的原始文字）。<br>"
            "另外：任何 LLM 调用失败都<b>不会</b>影响转写产物，只会退化成未加工的原文，并在 .md 里标注告警。"
        )
        notice.setWordWrap(True)
        notice.setStyleSheet(
            f"background:{theme.INFO_BG}; border:1px solid {theme.INFO_BORDER};"
            f" padding:8px; border-radius:4px;"
        )
        lay.addWidget(notice)

        box = QGroupBox("DeepSeek（文本加工，可选）")
        form = QFormLayout(box)
        self.chk_llm = QCheckBox("启用文本后处理（需要 DeepSeek API Key）")
        form.addRow("", self.chk_llm)

        self.edit_llm_base = QLineEdit()
        self.edit_llm_base.setPlaceholderText("https://api.deepseek.com/v1")
        form.addRow("Base URL", self.edit_llm_base)

        row = QHBoxLayout()
        self.edit_llm_key = QLineEdit()
        self.edit_llm_key.setEchoMode(QLineEdit.Password)
        self.edit_llm_key.setPlaceholderText("sk-...（DeepSeek 平台的 API Key）")
        btn_show = QPushButton("显示")
        btn_show.setCheckable(True)
        btn_show.setFixedWidth(56)
        btn_show.toggled.connect(
            lambda on: self.edit_llm_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
        )
        row.addWidget(self.edit_llm_key, 1)
        row.addWidget(btn_show)
        w = QWidget()
        w.setLayout(row)
        form.addRow("API Key", w)

        self.edit_llm_model = QLineEdit()
        self.edit_llm_model.setPlaceholderText("deepseek-chat")
        form.addRow("模型", self.edit_llm_model)

        self.chk_fix = QCheckBox("错别字 / 标点 / 术语修复（保留原时间轴）")
        self.chk_reseg = QCheckBox("按语义重新分段（在修复之后执行）")
        self.chk_summary = QCheckBox("生成结构化摘要与大纲（写进 .md）")
        for c in (self.chk_fix, self.chk_reseg, self.chk_summary):
            form.addRow("", c)

        self.spin_llm_chars = QSpinBox()
        self.spin_llm_chars.setRange(1000, 30000)
        self.spin_llm_chars.setSingleStep(1000)
        form.addRow("单次调用最大字符", self.spin_llm_chars)
        lay.addWidget(box)
        lay.addStretch(1)
        return self._scrolled(page)

    # ------------------------------------------------------------------ #
    def _tab_output(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        box = QGroupBox("输出目录与产物")
        form = QFormLayout(box)

        row = QHBoxLayout()
        self.edit_out = QLineEdit()
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_output)
        row.addWidget(self.edit_out, 1)
        row.addWidget(btn_browse)
        w = QWidget()
        w.setLayout(row)
        form.addRow("输出目录", w)

        self.chk_txt = QCheckBox("生成 .txt（纯文本全文）")
        self.chk_srt = QCheckBox("生成 .srt（带时间轴字幕）")
        self.chk_md = QCheckBox("生成 .md（标题 + 元信息 + 摘要 + 全文）")
        for c in (self.chk_txt, self.chk_srt, self.chk_md):
            form.addRow("", c)

        self.chk_bom = QCheckBox("产物写入 UTF-8 BOM（Windows 记事本 / 字幕播放器不乱码，建议保持开启）")
        self.chk_bom.setToolTip(
            "简体中文 Windows 的默认代码页是 GBK；不带 BOM 的 UTF-8 文件\n"
            "在旧版记事本和部分字幕播放器里会被猜错编码、显示成乱码。\n"
            "只有在你确定后续处理工具不接受 BOM 时才关闭。"
        )
        form.addRow("", self.chk_bom)

        hint = QLabel(
            f"默认目录结构：<code>&lt;输出目录&gt;/&lt;课程名&gt;/&lt;标题&gt;.{{txt,srt,md}}</code><br>"
            f"另外会写一份 <code>&lt;标题&gt;.transcript.json</code>（机器可读，便于二次加工）。<br>"
            f"目标文件已存在时会先备份为 <code>*.bak-时间戳</code>，<b>只增不改</b>，不会静默覆盖你的产物。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        form.addRow("", hint)
        lay.addWidget(box)
        lay.addStretch(1)
        return self._scrolled(page)

    # ------------------------------------------------------------------ #
    def _tab_media(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        box = QGroupBox("下载与音频")
        form = QFormLayout(box)

        self.edit_ffmpeg = QLineEdit()
        self.edit_ffmpeg.setPlaceholderText("留空自动探测（随包 → PATH → imageio-ffmpeg）")
        row = QHBoxLayout()
        row.addWidget(self.edit_ffmpeg, 1)
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._browse_ffmpeg)
        row.addWidget(btn)
        w = QWidget()
        w.setLayout(row)
        form.addRow("ffmpeg 路径", w)

        self.spin_concurrency = QSpinBox()
        self.spin_concurrency.setRange(1, 2)
        self.spin_concurrency.setToolTip("并发上限硬性为 2，避免触发平台风控")
        form.addRow("并发数", self.spin_concurrency)

        self.cmb_fmt = QComboBox()
        for fmt in ("mp3", "wav", "m4a", "flac"):
            self.cmb_fmt.addItem(fmt, fmt)
        form.addRow("音频格式", self.cmb_fmt)

        self.spin_bitrate = QSpinBox()
        self.spin_bitrate.setRange(16, 320)
        self.spin_bitrate.setSuffix(" kbps")
        form.addRow("音频码率", self.spin_bitrate)

        self.cmb_sr = QComboBox()
        for sr in (8000, 16000, 22050, 44100, 48000):
            self.cmb_sr.addItem(f"{sr} Hz", sr)
        form.addRow("采样率", self.cmb_sr)

        self.cmb_ch = QComboBox()
        self.cmb_ch.addItem("单声道（ASR 推荐）", 1)
        self.cmb_ch.addItem("双声道", 2)
        form.addRow("声道", self.cmb_ch)

        self.chk_cache = QCheckBox("启用音频缓存（cache/media 命中即跳过下载，断点续跑不重下）")
        self.chk_keep_audio = QCheckBox("额外在输出目录保留音频副本")
        self.chk_keep_video = QCheckBox("保留原始视频（体积很大，默认关闭——按你的要求只留文本）")
        for c in (self.chk_cache, self.chk_keep_audio, self.chk_keep_video):
            form.addRow("", c)

        self.spin_retries = QSpinBox()
        self.spin_retries.setRange(1, 10)
        form.addRow("下载重试次数", self.spin_retries)

        self.spin_speed = QSpinBox()
        self.spin_speed.setRange(0, 102400)
        self.spin_speed.setSuffix(" KiB/s（0=不限速）")
        self.spin_speed.setToolTip(
            "近似限速：按已下载字节数与经过时间做节流，用于降低对学校服务器的压力。\n"
            "受 ffmpeg 内部缓冲与进度上报粒度影响，实测约为目标值的 1/3~1/5，不是字节级精确。"
        )
        form.addRow("限速", self.spin_speed)
        lay.addWidget(box)

        info = QLabel(
            f"当前 ffmpeg：<code>{self._current_ffmpeg_text()}</code><br>"
            f"音频缓存目录：<code>{paths.media_cache_dir()}</code>"
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        lay.addWidget(info)
        lay.addStretch(1)
        return self._scrolled(page)

    def _current_ffmpeg_text(self) -> str:
        try:
            return str(media.find_ffmpeg(self.cfg.ffmpeg_path))
        except Exception as exc:  # noqa: BLE001
            return f"未找到（{exc}）"

    # ------------------------------------------------------------------ #
    def _tab_network(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        box = QGroupBox("网络")
        form = QFormLayout(box)

        self.edit_proxy = QLineEdit()
        self.edit_proxy.setPlaceholderText("留空=直连；例：http://127.0.0.1:7890")
        form.addRow("代理", self.edit_proxy)

        self.chk_verify = QCheckBox("校验 TLS 证书（关闭仅用于自签名代理的排障）")
        form.addRow("", self.chk_verify)

        self.spin_timeout = QDoubleSpinBox()
        self.spin_timeout.setRange(5.0, 300.0)
        self.spin_timeout.setSuffix(" 秒")
        form.addRow("请求超时", self.spin_timeout)

        self.spin_jitter_min = QDoubleSpinBox()
        self.spin_jitter_min.setRange(0.0, 10.0)
        self.spin_jitter_min.setSingleStep(0.1)
        form.addRow("请求间隔下限", self.spin_jitter_min)

        self.spin_jitter_max = QDoubleSpinBox()
        self.spin_jitter_max.setRange(0.0, 10.0)
        self.spin_jitter_max.setSingleStep(0.1)
        form.addRow("请求间隔上限", self.spin_jitter_max)
        lay.addWidget(box)

        box2 = QGroupBox("站点入口与统一身份认证")
        form2 = QFormLayout(box2)
        self.edit_portal = QLineEdit()
        form2.addRow("门户入口", self.edit_portal)
        self.edit_api_base = QLineEdit()
        form2.addRow("接口 Base", self.edit_api_base)
        self.edit_student = QLineEdit()
        self.edit_student.setPlaceholderText("学号（仅用于显示与清单标注，不会作为密码保存）")
        form2.addRow("学号", self.edit_student)

        state = QLabel(
            f"登录态文件：<code>{paths.storage_state_path()}</code><br>"
            f"浏览器用户目录：<code>{paths.browser_profile_dir()}</code><br>"
            f"凭据加密：<b>{'Windows DPAPI 可用' if dpapi_available() else '不可用（凭据仅内存保存）'}</b>"
        )
        state.setWordWrap(True)
        state.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        form2.addRow("", state)

        tip = QLabel(
            "<b>校园网 / VPN 提示：</b>课程平台在校外通常需要学校 SSL-VPN。"
            "点「连通性自检」会告诉你当前网络是「直连可达」「需要登录」还是「被 webVPN 网关拦截」。"
            "本应用<b>不做</b>任何验证绕过。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{theme.WARN}; font-weight:600;")
        form2.addRow("", tip)
        lay.addWidget(box2)
        lay.addStretch(1)
        return self._scrolled(page)

    # ------------------------------------------------------------------ #
    def _tab_advanced(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        box = QGroupBox("运行信息")
        form = QFormLayout(box)
        info = "\n".join(f"{k} = {v}" for k, v in paths.describe().items())
        label = QLabel(f"<pre style='font-size:11px'>{info}</pre>")
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(label)
        lay.addWidget(box)

        box2 = QGroupBox("危险操作")
        v = QVBoxLayout(box2)
        btn_clear_media = QPushButton("清空音频缓存（cache/media）")
        btn_clear_media.clicked.connect(self._clear_media_cache)
        btn_clear_seg = QPushButton("清空切分临时目录（cache/segments）")
        btn_clear_seg.clicked.connect(self._clear_segments)
        v.addWidget(btn_clear_media)
        v.addWidget(btn_clear_seg)
        note = QLabel("注意：清空缓存后，下次运行会重新下载音频；已经写出的产物文件不受影响。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.DANGER}; font-weight:600;")
        v.addWidget(note)
        lay.addWidget(box2)

        self.txt_probe = QPlainTextEdit()
        self.txt_probe.setReadOnly(True)
        self.txt_probe.setPlaceholderText("「连通性自检」的结果会显示在这里…")
        lay.addWidget(QLabel("自检输出："))
        lay.addWidget(self.txt_probe, 1)
        return page

    # ================================================================== #
    # 读写
    # ================================================================== #
    def _load(self) -> None:
        c = self.cfg
        # ASR：预设按**三元组**反查，别让下拉显示与实际配置不符的预设
        self.cmb_preset.blockSignals(True)
        self.cmb_preset.setCurrentIndex(self._match_preset(c))
        self.cmb_preset.blockSignals(False)
        self.edit_base.setEnabled((c.asr_provider or "") != "faster_whisper_local")
        self._select_combo(self.cmb_provider, c.asr_provider)
        self.edit_base.setText(c.asr_base_url)
        self.edit_key.setText(self.cm.secret("asr_api_key"))
        self.edit_model.setText(c.asr_model)
        self._select_combo(self.cmb_lang, c.asr_language)
        self.chk_native.setChecked(c.asr_use_native_api)
        self.chk_timestamps.setChecked(c.asr_timestamps)
        self.chk_diarization.setChecked(c.asr_speaker_diarization)
        self.edit_vocab.setText(c.asr_vocabulary)
        self.spin_max_mb.setValue(c.asr_max_upload_mb)
        self.spin_max_seg.setValue(int(c.asr_max_segment_sec))
        self.spin_overlap.setValue(c.asr_overlap_sec)
        self._select_combo(self.cmb_chunk, c.asr_chunk_strategy)
        self.spin_asr_retries.setValue(int(c.asr_retries))
        # LLM
        self.chk_llm.setChecked(c.llm_enabled)
        self.edit_llm_base.setText(c.llm_base_url)
        self.edit_llm_key.setText(self.cm.secret("llm_api_key"))
        self.edit_llm_model.setText(c.llm_model)
        self.chk_fix.setChecked(c.llm_fix_text)
        self.chk_reseg.setChecked(c.llm_resegment)
        self.chk_summary.setChecked(c.llm_summary)
        self.spin_llm_chars.setValue(int(c.llm_max_chars_per_call))
        # 输出
        self.edit_out.setText(c.output_dir or str(paths.output_dir()))
        self.chk_txt.setChecked(c.emit_txt)
        self.chk_srt.setChecked(c.emit_srt)
        self.chk_md.setChecked(c.emit_md)
        self.chk_bom.setChecked(bool(getattr(c, "emit_utf8_bom", True)))
        # 媒体
        self.edit_ffmpeg.setText(c.ffmpeg_path)
        self.spin_concurrency.setValue(int(c.concurrency))
        self._select_combo(self.cmb_fmt, c.audio_format)
        try:
            self.spin_bitrate.setValue(int(str(c.audio_bitrate).rstrip("kK")))
        except ValueError:
            self.spin_bitrate.setValue(64)
        self._select_combo(self.cmb_sr, c.audio_sample_rate)
        self._select_combo(self.cmb_ch, c.audio_channels)
        self.chk_cache.setChecked(c.cache_enabled)
        self.chk_keep_audio.setChecked(c.write_audio_cache)
        self.chk_keep_video.setChecked(c.keep_video)
        self.spin_retries.setValue(int(c.download_retries))
        self.spin_speed.setValue(int(c.speed_limit_kib))
        # 网络
        self.edit_proxy.setText(c.proxy)
        self.chk_verify.setChecked(c.verify_tls)
        self.spin_timeout.setValue(float(c.request_timeout))
        self.spin_jitter_min.setValue(float(c.jitter_min))
        self.spin_jitter_max.setValue(float(c.jitter_max))
        self.edit_portal.setText(c.portal_url)
        self.edit_api_base.setText(c.api_base)
        self.edit_student.setText(c.student_id)

    @staticmethod
    def _select_combo(combo: QComboBox, value) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    def _match_preset(self, cfg: AppConfig) -> int:
        """按 (provider, base_url, model) **三元组**反查预设。

        只按 provider 匹配是不行的：预设 ①③④⑥ 的 provider 都是
        ``openai_compatible``，用户配了本地服务却看到「OpenAI 官方」被选中，
        会以为自己配错了。匹配不上就落到「（自定义 / 当前配置）」。
        """
        base = (cfg.asr_base_url or "").strip().rstrip("/")
        model = (cfg.asr_model or "").strip()
        for i, (_label, provider, pbase, pmodel) in enumerate(ASR_PRESETS):
            if provider != (cfg.asr_provider or "").lower():
                continue
            if (pbase or "").rstrip("/") != base:
                continue
            if (pmodel or "") != model:
                continue
            return i
        return len(ASR_PRESETS)  # → CUSTOM_PRESET_LABEL

    def _on_preset_changed(self, index: int) -> None:
        if index < 0 or index >= len(ASR_PRESETS):
            return  # 选中「（自定义）」时不动用户已填的内容
        _label, provider, base, model = ASR_PRESETS[index]
        self._select_combo(self.cmb_provider, provider)
        if base:
            self.edit_base.setText(base)
        if model:
            self.edit_model.setText(model)
        self.chk_native.setChecked(False)
        self.edit_base.setEnabled(provider != "faster_whisper_local")

    def on_save(self) -> None:
        c = self.cfg
        c.asr_provider = str(self.cmb_provider.currentData() or "dashscope")
        c.asr_base_url = self.edit_base.text().strip() or c.asr_base_url
        c.asr_model = self.edit_model.text().strip() or c.asr_model
        c.asr_language = str(self.cmb_lang.currentData() or "zh")
        c.asr_use_native_api = self.chk_native.isChecked()
        c.asr_timestamps = self.chk_timestamps.isChecked()
        c.asr_speaker_diarization = self.chk_diarization.isChecked()
        c.asr_vocabulary = self.edit_vocab.text().strip()
        c.asr_max_upload_mb = float(self.spin_max_mb.value())
        c.asr_max_segment_sec = int(self.spin_max_seg.value())
        c.asr_overlap_sec = float(self.spin_overlap.value())
        c.asr_chunk_strategy = str(self.cmb_chunk.currentData() or "silence")
        c.asr_retries = int(self.spin_asr_retries.value())

        c.llm_enabled = self.chk_llm.isChecked()
        c.llm_base_url = self.edit_llm_base.text().strip() or c.llm_base_url
        c.llm_model = self.edit_llm_model.text().strip() or c.llm_model
        c.llm_fix_text = self.chk_fix.isChecked()
        c.llm_resegment = self.chk_reseg.isChecked()
        c.llm_summary = self.chk_summary.isChecked()
        c.llm_max_chars_per_call = int(self.spin_llm_chars.value())

        c.output_dir = self.edit_out.text().strip() or str(paths.output_dir())
        c.emit_txt = self.chk_txt.isChecked()
        c.emit_srt = self.chk_srt.isChecked()
        c.emit_md = self.chk_md.isChecked()
        c.emit_utf8_bom = self.chk_bom.isChecked()

        c.ffmpeg_path = self.edit_ffmpeg.text().strip()
        c.concurrency = int(self.spin_concurrency.value())
        c.audio_format = str(self.cmb_fmt.currentData() or "mp3")
        c.audio_bitrate = f"{int(self.spin_bitrate.value())}k"
        c.audio_sample_rate = int(self.cmb_sr.currentData() or 16000)
        c.audio_channels = int(self.cmb_ch.currentData() or 1)
        c.cache_enabled = self.chk_cache.isChecked()
        c.write_audio_cache = self.chk_keep_audio.isChecked()
        c.keep_video = self.chk_keep_video.isChecked()
        c.download_retries = int(self.spin_retries.value())
        c.speed_limit_kib = int(self.spin_speed.value())

        c.proxy = self.edit_proxy.text().strip()
        c.verify_tls = self.chk_verify.isChecked()
        c.request_timeout = float(self.spin_timeout.value())
        c.jitter_min = float(self.spin_jitter_min.value())
        c.jitter_max = float(self.spin_jitter_max.value())
        c.portal_url = self.edit_portal.text().strip() or c.portal_url
        c.api_base = self.edit_api_base.text().strip() or c.api_base
        c.student_id = self.edit_student.text().strip() or c.student_id

        if c.jitter_max < c.jitter_min:
            c.jitter_max = c.jitter_min

        # 凭据单独加密保存
        asr_key = self.edit_key.text().strip()
        llm_key = self.edit_llm_key.text().strip()
        self.cm.set_secret("asr_api_key", asr_key)
        self.cm.set_secret("llm_api_key", llm_key)
        self.cm.save(c)

        if asr_key and not dpapi_available():
            QMessageBox.warning(
                self, "凭据未落盘",
                "当前环境没有 Windows DPAPI，API Key 只保存在内存里，关闭应用后需要重新输入。"
                "（不会以明文写入磁盘）",
            )
        self.accept()

    # ================================================================== #
    # 自检 / 浏览
    # ================================================================== #
    def on_probe(self) -> None:
        if self._probe_worker is not None and self._probe_worker.isRunning():
            return
        # 先把当前界面上的 Key 应用进去，再自检
        self.cm.set_secret("asr_api_key", self.edit_key.text().strip())
        self.cm.set_secret("llm_api_key", self.edit_llm_key.text().strip())
        self.cfg.asr_base_url = self.edit_base.text().strip() or self.cfg.asr_base_url
        self.cfg.asr_model = self.edit_model.text().strip() or self.cfg.asr_model
        self.cfg.asr_provider = str(self.cmb_provider.currentData() or "dashscope")
        self.cfg.proxy = self.edit_proxy.text().strip()
        self.cfg.verify_tls = self.chk_verify.isChecked()

        self.txt_probe.clear()
        self.txt_probe.appendPlainText("开始自检…（ffmpeg / 站点 / ASR / DeepSeek）")
        self.btn_check.setEnabled(False)
        w = ProbeWorker(self.cfg, self.cm)
        w.result.connect(self._on_probe_result)
        w.finished.connect(lambda: self.btn_check.setEnabled(True))
        self._probe_worker = w
        w.start()

    def _on_probe_result(self, name: str, ok: bool, message: str) -> None:
        icon = "✅" if ok else "⛔"
        self.txt_probe.appendPlainText(f"{icon} {name}\n{message}\n")
        self.lbl_probe.setText(f"{icon} {name}：{message.splitlines()[0] if message else ''}")

    def _browse_output(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", self.edit_out.text() or str(paths.output_dir()))
        if d:
            self.edit_out.setText(d)

    def _browse_ffmpeg(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "选择 ffmpeg.exe", "", "可执行文件 (*.exe);;所有文件 (*)")
        if f:
            self.edit_ffmpeg.setText(f)

    def _clear_media_cache(self) -> None:
        import shutil

        if QMessageBox.question(self, "确认", f"将删除 {paths.media_cache_dir()} 下的全部音频缓存，确定吗？") != QMessageBox.Yes:
            return
        d = paths.media_cache_dir()
        n = 0
        for p in d.glob("*"):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
                n += 1
            except OSError:
                pass
        QMessageBox.information(self, "完成", f"已清理 {n} 项。")

    def _clear_segments(self) -> None:
        import shutil

        d = paths.cache_dir() / "segments"
        if QMessageBox.question(self, "确认", f"将删除 {d} 下的全部切分临时文件，确定吗？") != QMessageBox.Yes:
            return
        shutil.rmtree(d, ignore_errors=True)
        QMessageBox.information(self, "完成", "已清理切分临时目录。")
