"""用户配置：JSON 文件 + Windows DPAPI 加密的凭据。

分层的原则：
    * **非敏感**设置（输出目录、并发、模型名、开关）落在 ``config.json``，明文可读。
    * **敏感**凭据（ASR API Key、DeepSeek API Key）用 DPAPI（``CryptProtectData``）
      加密后落在 ``secrets.json``，只能用**当前 Windows 用户**在本机解开。
    * 代码里、日志里、仓库里绝不出现明文密码 / Cookie / token。

DPAPI 不可用时（非 Windows / pywin32 缺失）退化为「不落盘」策略：
凭据仅存在于内存，重启后需重新输入，并显式告警，绝不静默写明文。
"""

from __future__ import annotations

import base64
import copy
import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import paths
from .logbus import get_logger, register_identifier, register_secret

log = get_logger("config")

# --------------------------------------------------------------------------- #
# DPAPI
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - 取决于平台
    import win32crypt  # type: ignore

    _HAS_DPAPI = True
except Exception:  # pragma: no cover
    win32crypt = None  # type: ignore
    _HAS_DPAPI = False


def dpapi_available() -> bool:
    return _HAS_DPAPI


def _dpapi_encrypt(plaintext: str) -> str:
    if not _HAS_DPAPI:
        raise RuntimeError("DPAPI 不可用")
    blob = win32crypt.CryptProtectData(plaintext.encode("utf-8"), "ecnu-transcribe", None, None, None, 0)
    return "dpapi:" + base64.b64encode(blob).decode("ascii")


def _dpapi_decrypt(token: str) -> str:
    if not _HAS_DPAPI:
        raise RuntimeError("DPAPI 不可用")
    raw = base64.b64decode(token.split(":", 1)[1])
    _desc, data = win32crypt.CryptUnprotectData(raw, None, None, None, 0)
    return data.decode("utf-8")


# --------------------------------------------------------------------------- #
# 默认配置
# --------------------------------------------------------------------------- #
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: 大夏学堂 / 资源管理平台入口（前端 SPA，Vue hash 路由）
DEFAULT_PORTAL_URL = "https://courses.ecnu.edu.cn/jy-application-resourcemanage-ui/#/home"
#: 统一身份认证 OAuth2 入口（实测：proxy.ecnu.edu.cn/users/sign_in 会 302 到这里）
DEFAULT_SSO_URL = "https://api.ecnu.edu.cn/oauth2/authorize"
#: webVPN / SSL-VPN 入口（校外访问课程平台的前置网关，实测 /portal/ 存活）
DEFAULT_VPN_URL = "https://vpn.ecnu.edu.cn/portal/"


@dataclass
class AppConfig:
    """全部可配置项。字段名与设置页、config.json 一一对应。"""

    # ---- 站点 ----
    portal_url: str = DEFAULT_PORTAL_URL
    api_base: str = "https://courses.ecnu.edu.cn"
    #: 学号（仅用于日志脱敏与清单标注，不参与任何鉴权）。示例值，请填自己的。
    student_id: str = "20261234567"

    # ---- 网络 ----
    #: "" = 直连（遵守 *noproxy*）；也可填 "http://127.0.0.1:7890" 等
    proxy: str = ""
    no_proxy: str = "localhost,127.0.0.1"
    request_timeout: float = 30.0
    verify_tls: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    #: 校园网/VPN 不可达时的显式开关；不做任何绕过
    access_mode: str = "auto"  # auto | direct | proxy_gateway
    #: 站内请求之间的抖动延时（秒），避免触发风控
    jitter_min: float = 0.3
    jitter_max: float = 0.8

    # ---- 下载 / 媒体 ----
    concurrency: int = 2
    ffmpeg_path: str = ""
    keep_video: bool = False
    audio_format: str = "mp3"  # mp3 | wav | m4a | flac
    audio_bitrate: str = "64k"
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    download_retries: int = 4
    cache_enabled: bool = True
    speed_limit_kib: int = 0  # 0 = 不限速
    #: 并行取音频的连接数（缺陷 63）。实测该 CDN **单连接 ~670 KB/s、4 并发聚合 ~2.1 MB/s**
    #: （持续 160 MB 校验过字节数），一节课从 ~21 分钟降到 ~6.6 分钟。
    #: 1 = 退回原来的单流下载（也用于不支持 Range、时长未知、本地文件等情形）。
    download_connections: int = 4
    #: 并行时每个时间窗至少这么长，避免把短视频切成一堆碎片请求。
    download_window_min_sec: float = 120.0
    #: 拉流停滞看门狗（缺陷 49）：窗口内「推进的媒体秒数 ÷ 窗口秒数」低于
    #: ``download_min_speed_ratio`` 即判定停滞并中止本次拉流。
    #: 实测健康网速约 0.9~2.0× 实时（3301s 的课用了 3676s / 955s 抓回 1900s），
    #: 所以 0.25× 是个很宽松的下限：真到这一步已经是「等下去也没有意义」。
    download_stall_seconds: float = 120.0
    download_min_speed_ratio: float = 0.25
    #: 时长明显短于清单标注（截断）时的容忍度：超过该比例直接判失败（缺陷 50），
    #: 产物转存为 ``<目标>.partial`` 供断点续传，绝不当成「完成」继续转写。
    download_truncate_tolerance: float = 0.05

    # ---- ASR（语音转文字）----
    #: 用户选型：阿里云百炼 DashScope（OpenAI 兼容 / 原生两种都支持）
    asr_provider: str = "dashscope"  # dashscope | openai_compatible | faster_whisper_local | none
    asr_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    #: 2026-09-14 实测：这个账号的百炼端点上**没有** `paraformer-v2`（250 个模型里查无此名，
    #: 且原异步接口对它返回 SERVER_ERROR），可用的中文 ASR 是千问系列。
    #: `qwen3-asr-flash` 实测最好用（简体、带标点、约 27× 实时）。
    asr_model: str = "qwen3-asr-flash"
    asr_language: str = "zh"
    asr_use_native_api: bool = False  # True: DashScope 原生异步 API；False: OpenAI 兼容
    #: 是否走 DashScope 的「chat + input_audio(data URL)」调用方式（缺陷 55 实测）。
    #: None = 按模型名自动判断（千问/Fun ASR 系列自动走这条路，因为该端点上
    #: `/audio/transcriptions` 对它们**必然 404**）；True/False = 强制。
    #: 注意：这条路**不返回时间戳**，`.srt` 的时间轴由静音切分边界给出。
    asr_chat_audio: bool | None = None
    asr_timestamps: bool = True
    asr_max_upload_mb: float = 25.0
    asr_max_segment_sec: int = 600
    asr_overlap_sec: float = 1.5
    asr_retries: int = 4
    asr_concurrency: int = 1
    asr_chunk_strategy: str = "silence"  # silence | fixed
    #: 送 ASR 前是否自动增益。实测学校部分录播电平只有 -57 dB（正常 -20~-30 dB），
    #: 不增益时 VAD 会把整段判成无语音 → 空转写。
    asr_auto_gain: bool = True
    #: 音轨近乎无声（峰值 < -50 dB）时是否仍然强行送 ASR。
    #: 默认 False：实测强行识别会让模型编造出「字幕by索兰娅」这类不存在的文字，
    #: 与其给用户一份幻觉产物，不如明确告诉他「这条录像没录到声音」。
    asr_allow_silent: bool = False
    asr_vocabulary: str = ""  # 术语表（hot words），可留空
    asr_speaker_diarization: bool = False
    asr_min_segment_sec: float = 1.0

    # ---- LLM 后处理（DeepSeek，可选开关）----
    llm_enabled: bool = False
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_fix_text: bool = True  # 错别字 / 标点 / 术语纠正 / 口语清理
    llm_resegment: bool = True  # 按语义重新分段
    llm_summary: bool = True  # 结构化摘要 + 大纲
    llm_max_chars_per_call: int = 6000
    llm_timeout: float = 120.0

    # ---- 输出 ----
    output_dir: str = ""
    emit_txt: bool = True
    emit_srt: bool = True
    emit_md: bool = True
    #: 产物是否写入 UTF-8 BOM。Windows 记事本与多数字幕播放器靠 BOM 判断编码，
    #: 关闭后在简体中文系统上可能显示乱码（高级用户可关）。
    emit_utf8_bom: bool = True
    #: 是否优先使用**实测出的真实平台接口**（换 jwt-token → 教学班 → 节次 → 录播）。
    #: 关掉后回退到「候选路径探测」的旧逻辑（仅排障用）。
    platform_api: bool = True
    write_audio_cache: bool = True

    # ---- 其它 ----
    log_level: str = "INFO"
    last_login_at: str = ""
    seen_schema_version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    def resolved_output_dir(self) -> Path:
        d = Path(self.output_dir).expanduser() if self.output_dir else paths.output_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def public(self) -> dict[str, Any]:
        """可安全写日志 / 展示的字段（本类不含密钥，直接全量即可）。"""
        return asdict(self)


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #
_SECRET_FIELDS = ("asr_api_key", "llm_api_key")


class ConfigManager:
    """配置 + 凭据的统一入口（线程安全）。"""

    def __init__(self, config_file: Path | None = None, secrets_file: Path | None = None) -> None:
        self.config_file = Path(config_file) if config_file else paths.config_path()
        self.secrets_file = Path(secrets_file) if secrets_file else paths.secrets_path()
        self._lock = threading.RLock()
        self._cfg = AppConfig()
        self._secrets: dict[str, str] = {}

    # -- 加载 / 保存 -------------------------------------------------------- #
    def load(self) -> AppConfig:
        with self._lock:
            if self.config_file.is_file():
                try:
                    raw = json.loads(self.config_file.read_text(encoding="utf-8"))
                    self._cfg = self._from_dict(raw)
                except Exception as exc:
                    log.warning("config.json 解析失败，使用默认配置：%s", exc)
            self._secrets = self._load_secrets()
            if self._cfg.output_dir == "":
                self._cfg.output_dir = str(paths.output_dir())
            # 学号属于个人标识：登记后日志里出现即脱敏
            register_identifier(self._cfg.student_id)
            log.debug("配置已加载：%s", paths.describe())
            return self._cfg

    def _from_dict(self, raw: dict[str, Any]) -> AppConfig:
        valid = {f.name for f in fields(AppConfig)}
        known = {k: v for k, v in raw.items() if k in valid}
        unknown = {k: v for k, v in raw.items() if k not in valid}
        cfg = AppConfig(**known)
        if unknown:
            cfg.extra.update(unknown)
        return cfg

    def save(self, cfg: AppConfig | None = None) -> None:
        with self._lock:
            if cfg is not None:
                self._cfg = cfg
            data = asdict(self._cfg)
            self._atomic_write_json(self.config_file, data)
            log.debug("配置已写入 %s", self.config_file)

    @staticmethod
    def _atomic_write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # -- 凭据 --------------------------------------------------------------- #
    def _load_secrets(self) -> dict[str, str]:
        if not self.secrets_file.is_file():
            return {}
        try:
            raw = json.loads(self.secrets_file.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("secrets.json 解析失败：%s", exc)
            return {}
        out: dict[str, str] = {}
        for key in _SECRET_FIELDS:
            token = raw.get(key)
            if not token:
                continue
            try:
                value = _dpapi_decrypt(token)
                out[key] = value
                register_secret(value)
            except Exception as exc:
                log.warning("凭据 %s 解密失败（可能换机器/换用户）：%s", key, exc)
        return out

    def secret(self, key: str) -> str:
        """读取凭据：优先 DPAPI 存储，其次环境变量，最后空串。

        环境变量便于 CI / 无 GUI 场景：``ECNU_ASR_API_KEY`` / ``ECNU_LLM_API_KEY``。
        """
        with self._lock:
            if key in self._secrets:
                return self._secrets[key]
        env_name = "ECNU_" + key.upper()
        val = os.environ.get(env_name, "")
        if val:
            register_secret(val)
        return val

    def set_secret(self, key: str, value: str) -> bool:
        """写入凭据。返回 True 表示已落盘（DPAPI 可用），False 表示仅内存。"""
        with self._lock:
            value = (value or "").strip()
            if value:
                self._secrets[key] = value
                register_secret(value)
            else:
                self._secrets.pop(key, None)

            payload: dict[str, str] = {}
            if self.secrets_file.is_file():
                try:
                    payload = json.loads(self.secrets_file.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}

            if not _HAS_DPAPI:
                log.warning(
                    "DPAPI 不可用：凭据「%s」仅保存在内存中，关闭应用后需重新输入。"
                    "（不会以明文形式写入磁盘）",
                    key,
                )
                return False

            if value:
                payload[key] = _dpapi_encrypt(value)
            else:
                payload.pop(key, None)

            if payload:
                self._atomic_write_json(self.secrets_file, payload)
            elif self.secrets_file.is_file():
                try:
                    self.secrets_file.unlink()
                except OSError:
                    pass
            return True

    def secrets_status(self) -> dict[str, bool]:
        return {k: bool(self.secret(k)) for k in _SECRET_FIELDS}

    # -- 便捷访问 ----------------------------------------------------------- #
    @property
    def cfg(self) -> AppConfig:
        return self._cfg

    def snapshot(self) -> AppConfig:
        """返回深拷贝，避免工作线程读到写一半的配置。"""
        with self._lock:
            return copy.deepcopy(self._cfg)

    def worker_snapshot(self, cfg: AppConfig | None = None, *, secrets: dict[str, str] | None = None) -> "ConfigManager":
        """只在内存中保存本批配置和密钥；运行中保存设置不影响当前任务。"""
        with self._lock:
            result = ConfigManager(self.config_file, self.secrets_file)
            result._cfg = copy.deepcopy(cfg if cfg is not None else self._cfg)
            result._secrets = {key: self.secret(key) for key in ("asr_api_key", "llm_api_key")}
            if secrets:
                result._secrets.update(secrets)
                for value in secrets.values():
                    if value:
                        register_secret(value)
            return result
