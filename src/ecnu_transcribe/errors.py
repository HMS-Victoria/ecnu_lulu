"""统一的异常体系。

设计要点（对应 PROGRESS.md 的降级预案）：
    * 登录态失效必须是**显式异常** ``AuthExpiredError``，绝不允许静默重试成空列表。
    * 平台接口结构变更必须是**显式异常** ``ApiChangedError``，提示用户而不是产出空清单。
    * DRM 必须是**显式异常** ``DrmDetectedError``，立即停止，不做任何绕过。
"""

from __future__ import annotations


class TranscribeHelperError(Exception):
    """本项目所有异常的基类。"""


# --------------------------------------------------------------------------- #
# 网络 / 站点
# --------------------------------------------------------------------------- #
class NetworkError(TranscribeHelperError):
    """通用网络故障（超时、DNS、连接重置）。"""


class SiteUnreachableError(NetworkError):
    """站点在当前网络路径下不可达（例如不在校园网且未开 VPN）。"""


class AuthExpiredError(TranscribeHelperError):
    """登录态缺失或已失效。

    调用方必须提示用户「重新登录」，而**不是**返回空清单。
    """

    def __init__(self, message: str = "登录态已失效，请重新登录", *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class ApiChangedError(TranscribeHelperError):
    """平台接口返回结构与预期不符（接口变更检测）。"""

    def __init__(self, message: str, *, url: str = "", sample: str = "") -> None:
        super().__init__(message)
        self.url = url
        self.sample = sample


class ApiBusinessError(TranscribeHelperError):
    """平台以业务码形式返回的失败。"""

    def __init__(self, message: str, *, code: object = None, url: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.url = url


# --------------------------------------------------------------------------- #
# 媒体
# --------------------------------------------------------------------------- #
class MediaError(TranscribeHelperError):
    """音频获取 / 转码失败。"""


class DrmDetectedError(MediaError):
    """检测到 DRM（Widevine / PlayReady / FairPlay）。

    本项目**不做**任何 DRM 绕过，检测到即停止并如实报告。
    """


class FfmpegMissingError(MediaError):
    """找不到可用的 ffmpeg 可执行文件。"""


class PlayUrlExpiredError(MediaError):
    """播放地址已失效（签名过期 / 401 / 403）。

    真实平台的播放地址形如
    ``…/xxx.mp4?auth_key=1789190665-0-0-<md5>`` —— **带时限签名**。
    用户把任务暂停一晚、或者隔天点「继续」时，这个地址就死了。

    这类失败**重试同一个 URL 毫无意义**（试 4 次只是白等几十秒），
    正确做法是**重新向平台要一个新鲜地址**再下一次。
    所以它单独成一个异常类型：``downloader`` 遇到即快速失败，
    ``pipeline`` 捕获后重新解析播放地址并重试一次。
    """


class StreamStalledError(MediaError):
    """拉流停滞：长时间没有任何有效推进（缺陷 49）。

    真实场景：CDN 节点挂住连接不放（TCP 还活着、但一个字节都不再发），
    ffmpeg 会**永远等下去** —— 任务就此挂死，界面上只剩一个不动的百分比，
    用户既不知道发生了什么，也没法判断要不要等。

    看门狗按「窗口内推进的媒体秒数 ÷ 窗口秒数」判定（见
    :meth:`AudioDownloader._watchdog_factory`）：完全没推进、或者慢到
    等效倍速低于阈值，都算停滞。
    """


class StreamTruncatedError(MediaError):
    """音频被截断：拿到的时长明显短于清单标注（缺陷 50）。

    实测事故：ffmpeg 被强杀后留下 1900.5s 的 MP3（清单标注 3301.0s，
    只占 57%），旧代码只在日志里写了一句 WARNING 就**当成「音频完成」**
    继续转写 —— 用户最终拿到的是一份**残缺却看不出残缺**的转写稿。

    现在这类结果一律判为失败：产物另存为 ``<目标>.partial`` 作为断点续传
    基准，并由上层重试；重试仍不完整就**如实报错**，绝不产出半份稿子。
    """


# --------------------------------------------------------------------------- #
# 转写 / LLM
# --------------------------------------------------------------------------- #
class TranscriptionError(TranscribeHelperError):
    """ASR 调用失败（重试耗尽后抛出）。"""


class AsrNotConfiguredError(TranscriptionError):
    """用户尚未配置任何可用的 ASR 端点。"""


class LlmError(TranscribeHelperError):
    """文本后处理（LLM）调用失败。"""


# --------------------------------------------------------------------------- #
# 状态库
# --------------------------------------------------------------------------- #
class StoreError(TranscribeHelperError):
    """状态库读写失败。"""


class TaskCancelled(TranscribeHelperError):
    """任务被用户取消（用于优雅退出工作线程）。"""
