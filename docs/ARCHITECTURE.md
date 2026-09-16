# 架构说明 — 大夏学堂录播转写助手

> 面向 review / 面试的完整技术说明：架构图、模块职责、数据流、关键设计决策、
> 已知限制与后续演进。

---

## 1. 全局架构（文字版）

```
                    ┌──────────────────────────────────────────────┐
                    │            用户（本人账号）                    │
                    └───────────────────┬──────────────────────────┘
                                        │ 手动输入学号+密码（只在这里）
                                        ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │  登录层  login.LoginSession（Playwright 可见 Chromium + 持久化 profile）  │
   │  · launch_persistent_context(%LOCALAPPDATA%\ecnu-transcribe\browser)    │
   │  · CDP 监听 request/response → NetworkRecorder（逐条脱敏）              │
   │  · 多信号判定登录成功 → storage_state.json（仅本用户可读写）             │
   │  · probe_api_in_page()：动态签名场景的「页面上下文代打」桥               │
   │  · sniff_api_paths()：接口全失败时反推真实 XHR 路径                     │
   └───────────────────────────────┬────────────────────────────────────────┘
                                   │ Cookie + localStorage token
                                   ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │  清单层  client.EcnuClient（httpx 同步，无浏览器）                       │
   │  · 候选路径探测 + 嗅探兜底 + 结果缓存                                    │
   │  · {code,msg,data} 多层解包；业务码/消息 → AuthExpiredError             │
   │  · 有界 DFS 定位列表；自动翻页；300–800ms 抖动                          │
   │  · 302 分类：登录页 vs webVPN 网关 → 人话提示                           │
   │  · diagnose_access()：可达性诊断（direct/requires_login/vpn_required）  │
   └───────────────────────────────┬────────────────────────────────────────┘
                                   │ Course / Resource（catalog 模型，宽容解析）
                                   ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │  调度层  app.workers（QThread × 4） + pipeline.Pipeline                 │
   │  LoginWorker / CatalogWorker / PipelineWorker / ProbeWorker             │
   │  · 全部阻塞 IO 在工作线程；UI 只经 Signal/Slot 更新                      │
   │  · PipelineHooks(cancel=threading.Event) → 可优雅中断                   │
   └───────────────────────────────┬────────────────────────────────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        ▼                          ▼                          ▼
   ┌─────────┐              ┌─────────────┐            ┌──────────────┐
   │ 媒体层   │              │  转写层      │            │  后处理层     │
   │ media   │              │ transcriber │            │  llm         │
   │ ffmpeg  │              │ ASR 抽象     │            │  DeepSeek    │
   └────┬────┘              └──────┬──────┘            └──────┬───────┘
        │                          │                          │
        ▼                          ▼                          ▼
   downloader.AudioDownloader  Transcript(segments)     修复/分段/摘要
   · HLS / AES-128 / mp4       · 静音切分 + 重叠去重     · 失败不阻塞产物
   · 重试/缓存/sha256/并发≤2    · 时间轴平移合并
        │                          │                          │
        └──────────────────────────┴──────────────────────────┘
                                   ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │  产物层  exporter  →  output/<课程名>/<标题>.{txt,srt,md}                 │
   │                     + <标题>.transcript.json（机器可读）                 │
   │  只增不改：覆盖前备份 *.bak-<时间戳>                                      │
   └───────────────────────────────┬────────────────────────────────────────┘
                                   ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │  状态层  store.StateStore（sqlite3 WAL）                                 │
   │  tasks（含 id/course/resource_id/title/stage/progress/retry/error/       │
   │         output_dir/updated_at … 共 24 列）                               │
   │  artifacts（产物 sha256 审计）  events（状态迁移审计）                    │
   │  · 显式状态机；recover_orphans() 启动恢复；行锁防并发                     │
   └────────────────────────────────────────────────────────────────────────┘

   横切关注点：
   logbus（统一日志 + 脱敏）  config（DPAPI 凭据）  paths（开发态/冻结态）
   errors（统一异常：AuthExpired / ApiChanged / DrmDetected / …）
```

---

## 2. 模块职责表

| 模块 | 职责 | 关键约束 |
| --- | --- | --- |
| `paths` | 开发态/冻结态路径解析 | 敏感文件固定放 `%LOCALAPPDATA%`，不进工作区 |
| `logbus` | 日志总线 + 脱敏 | **唯一**的脱敏出口；Cookie/token/key/手机号/身份证/学号全遮 |
| `errors` | 异常体系 | 登录失效/接口变更/DRM 必须**显式**抛出，不许静默 |
| `config` | 配置 + 凭据 | 明文密钥**绝不**落盘；DPAPI 不可用则只留内存并告警 |
| `store` | 状态、断点续跑、审计 | 显式状态机；启动 `recover_orphans()`；有音频→`audio_ready` 不重下 |
| `catalog` | 数据模型 + 宽容解析 | 关键字段全缺时抛 `ApiChangedError`，不产出空清单 |
| `media` | ffmpeg 出口 | **唯一**调用 ffmpeg 的地方；DRM 检测；`CREATE_NO_WINDOW` |
| `client` | 平台 HTTP | 登录态复用；候选路径 + 嗅探；302 分类；抖动限流 |
| `login` | 人工登录 + 抓包 | **不绕过验证**；抓包按键名+值双重脱敏 |
| `downloader` | 取音频 | HLS/AES-128；缓存命中即跳过；并发硬上限 2 |
| `transcriber` | ASR 抽象 + 3 实现 | 切分/重叠/去重；重试退避；端点能力回退 |
| `llm` | DeepSeek 文本加工 | **只改文本不改时间轴**；解析失败保持原分段；任何失败退化为原文（34 项不变量测试守着） |
| `exporter` | 三产物写出 | 只增不改（备份）；SRT 切分与去重叠；UTF-8 **带 BOM**（Windows 记事本/播放器不乱码，可关） |
| `pausegate` | 暂停闸门 | 暂停作用于**任务内部的天然断点**；取消优先于暂停；未暂停时是无锁快速路径 |
| `login.run_readiness_check` | 首启一键诊断 | 汇总所有前置条件，每个失败项给出**可照做的动作** + 一句「下一步」 |
| `pipeline` | 编排 | 断点续跑；音频 sha256 一致则复用转写（省钱） |
| `app.workers` | 线程 | 阻塞 IO 全在 QThread；UI 只经信号 |
| `app.ui.*` | 界面 | 三栏 + 设置 + 登录引导；不在工作线程碰控件 |

---

## 3. 核心数据流

### 3.1 清单流（M0 → M1）

```
用户点「登录」
  → LoginSession.open()
      ├─ restore_storage_state()：把已保存的 Cookie / localStorage 注入浏览器
      │   （Chromium 用户目录**不保留会话级 Cookie**，不注入的话「明明登录过还要重新认证」）
      └─ 打开可见 Chromium（持久化 profile）
  → 用户手动完成统一身份认证（本工具不碰密码、不做验证码识别）
  → _detect_logged_in() 按**主机 + 路径 + 页面特征**判定（不用 URL 子串，踩过坑）
  → save_storage_state() 落 %LOCALAPPDATA%\ecnu-transcribe\storage_state.json
  → 用户点「刷新清单」
  → CatalogWorker → EcnuClient.fetch_catalog()
      ├─ **真实接口路径（实测契约）**：
      │    GET /jy-application-resourcemanage/oauth2/token → result.jwt_token
      │    请求头 jwt-token: <910 字符>
      │    GET /v1/list/termYear → 学期（acteId）
      │    GET /v1/myself/curriculum?acteId=&page.pageIndex=&page.pageSize= → 我的课表
      │        （行在 records；courVodOpen=1 的才算有录播）
      │    播放地址**按需解析**：GET /v1/course_vod_urls_new?courseId=<courId>
      ├─ 兜底：拿不到 jwt-token 时回退「候选路径探测」（测试/模拟平台走这条）
      └─ diagnose_access()：不可达时按异常特征给出可照做的下一步
          （连接被重置 → 代理直连规则；解析失败 → DNS/校园网；超时 → SSL-VPN）
  → data/catalog.json + 自动 upsert 进 state.db（幂等，不覆盖已有进度）
  → 左栏课程树渲染（按科目分组；搜索/勾选/时长合计/已转写状态）
```

### 3.2 单条任务流（M2 → M3）

```
PipelineWorker 取一条 pending 任务
  → Pipeline._run_inner()

  ① pending → probing
       resolve_play_url()（清单已带 play_url 则跳过）
         真实平台：GET /v1/course_vod_urls_new?courseId=<courId> → 带 auth_key 的直连 mp4
         多机位时取时长最长的一条

  ② probing → downloading（或直接 audio_ready）
       AudioDownloader.fetch()
         ├─ cache/media/<标题>__<id>.<ext> 命中且时长一致 → 跳过（断点续跑）
         │     时长**不一致且偏短** → 不删，转存 <目标>.partial 作为断点基准
         ├─ 有 .partial → 断点续传：-ss <已有秒数> 只补抓剩余 → concat -c copy 拼接
         │     → 校验时长，通过才替换正式产物；不通过则保留较长的一段转整段重下
         ├─ m3u8 预检：DRM 标记 → DrmDetectedError（立即停止）
         ├─ ffmpeg -headers(UA/Referer/Cookie) -i <url> -vn -ac 1 -ar 16000
         │         -c:a libmp3lame -b:a 64k <part>（check=True：非 0 退出判失败）
         │   ↑ 独立线程**看门狗**（缺陷 49）：窗口内推进的媒体秒数 ÷ 窗口秒数 < 0.25×
         │     即中止本次拉流并重试；暂停中 / 已抓完正在封头不计入
         ├─ 失败退避重试（最多 download_retries 次，退避期间可取消；
         │     重试时会先试断点续传，不从头再抓）
         ├─ **截断裁决**（缺陷 50）：实际时长 < 清单 95% ⇒ StreamTruncatedError，
         │     残件转存 .partial 交由上层重试；绝不把半份音频当「完成」
         ├─ 产物 sha256 + 时长（与清单偏差 > 2% 记告警）
         └─ **电平策略** media.normalize_for_asr()：
              峰值 < -30 dB → highpass=80 + volume=+N dB + alimiter（上限 +40 dB）
              仍 < -50 dB → 记「该录像音轨几乎没有声音」告警（是录像的问题，不是工具的）

       ⚠ 续跑复用前**验明正身**（缺陷 51）：任务记为 audio_ready 只说明「当时以为完成了」。
         复用缓存音频前用 ffprobe 量实际时长，与清单差 > 5% ⇒ 重新拉流。
         （触发过真实事故：半份音频被复用去转写，产物残缺而无标记）

  ③ audio_ready → transcribing
       <标题>.transcript.json 存在且音频 sha256 一致 → 直接复用（不花 ASR 钱）
       否则 create_transcriber()：
         ├─ 文件未超限 → 整段转写
         └─ 超限 → silencedetect 找静音点 → plan_chunks()（≤ 600s，重叠 1.5s）
              → extract_segment() 逐段切片 → 逐段 ASR（指数退避，最多 4 次）
              → merge_segments()：时间轴平移 + 两类重复判定去重
              → 失败分段记录进 meta.failed_chunks（可续跑）
       转写为空时 → 先量电平再决定怎么说（pipeline._empty_transcript_hint）：
         峰值 < -50 dB → 明确告知「是这条录像没录到声音，不是你的配置」
         否则        → 列出「确实无人说话 / 语言不符 / 端点不支持」

  ④ transcribing → post_processing（可选，llm_enabled）
       DeepSeek：修复（返回 {"segments":[{"i","text"}]}，只换文本保时间轴）
                → 语义分段（返回 {"break_after":[...]}，以原片段为单位重组）
                → 结构化摘要（超长时分块摘要再汇总）

  ⑤ → writing
       exporter.export_all()
         ├─ txt：按 800 字自然分段
         ├─ srt：句子边界打包 ≤ 12s/60 字；极端情况按字数均分；消除时间轴重叠
         └─ md：标题 + 元信息 + 摘要 + 带时间轴全文
       编码：UTF-8 + BOM（emit_utf8_bom，默认开）—— 中文 Windows 的 ANSI 代码页是
             cp936，无 BOM 时记事本/字幕播放器会猜错编码显示乱码；
             .transcript.json 是机器可读产物，**始终不带 BOM**；换行恒为 LF
       每个文件覆盖前备份 *.bak-<时间戳>（只增不改）

  ⑥ → done（outputs / transcript_path / audio_path 落库）
```

### 3.3 断点续跑语义

| 中断点 | 重开后行为 |
| --- | --- |
| 卡在 `probing/downloading/splitting/transcribing/post_processing/writing` | `recover_orphans()` 回退：有音频 → `audio_ready`；无音频 → `pending` |
| 音频已下好 | `cache/media` 命中（**校验时长一致性**：与清单差 > 5% 视为残缺 → 重下/续传）→ 不重下 |
| 音频只下了一半 | `cache/media/*.partial` 作为断点基准：`-ss` 只补抓剩余部分再拼接并校验 |
| 转写已完成 | `<标题>.transcript.json` + 音频 sha256 一致 → **不重跑 ASR**（省钱） |
| 分段转写中途断 | `cache/segments/<音频哈希>-<方案哈希>/result_NNN.json` 逐段复用，只重跑缺失段 |
| 产物已写出 | 重新写出并备份旧的（不丢历史） |
| 用户点「停止」 | 任务置 `canceled`，可单条「重跑」；音频与转写缓存保留 |

---

## 4. 关键设计决策与取舍

| 决策 | 备选方案 | 为什么这样选 |
| --- | --- | --- |
| **人工登录一次 + 持久化复用** | 自动填写表单 | 统一身份认证有验证码/二次验证；且「不绕过身份验证」是硬约束 |
| **候选路径探测 + 浏览器嗅探兜底** | 硬编码单一接口路径 | 平台接口会变；嗅探让「接口变更」从报错变成自愈 |
| **宽容解析（候选键）+ 严格校验** | 严格按字段名解析 | 同平台不同版本字段名不一致；但关键字段全缺必须显式失败，不能伪装成「没有录播」 |
| **状态机用「前进 + 出口态」而非固定迁移表** | 枚举所有合法迁移对 | 固定表会挡住合法的重跑/续跑路径（实测踩过），前进模型更耐改且仍能挡乱序回写 |
| **`media` 作为唯一 ffmpeg 出口** | 各处直接调 subprocess | 便于统一注入 Cookie/Header、统一 DRM 检测、统一脱敏日志、统一无窗口 |
| **音频缓存 + 转写缓存双层** | 只缓存音频 | ASR 按量计费；转写结果复用能直接省钱，这是本地优先工具的核心价值 |
| **LLM 只改文本不改时间轴** | 让模型重写整段 | 模型重写会破坏 SRT 时间戳；用「序号→文本」映射可保证时间轴严格不变 |
| **DRM 检测后立即停止** | 尝试处理 | 合规硬约束，不做绕过 |
| **凭据用 DPAPI** | 明文 JSON / 自实现加密 | DPAPI 绑定当前用户+机器，无需自管主密钥；不可用时降级为「只留内存」而非明文 |
| **脱敏按「键名 + 值」双路** | 只对值做正则 | `Authorization: Bearer xxx` 这类值本身不含敏感词，必须靠键名判定 |
| **数字标识按长度识别，不用 lookaround** | `(?<!\d)…(?!\d)` | 实测本机解释器对 lookahead 行为不一致（同一 pattern 结果矛盾），改为确定性扫描 |
| **`--onedir` 而非 `--onefile`** | 单文件 | 启动快、杀软误报少、便于内嵌 ffmpeg；稳定优先于体积 |
| **打包版不内嵌 Chromium** | 内嵌 | 会多 ~150 MB 且登录才需要；改为「装一次全机共用」+ `--doctor` 明确检查 |

---

## 5. 已知限制

1. **真实账号验收尚未完成**：校外访问需先经学校 webVPN 网关并完成一次统一身份认证
   （链路已实测，见 `docs/API.md` §0.2.1）；`webvpn.ecnu.edu.cn` 无公共 DNS（内网专用）。
   2026-09-12 实测更正：那条 `302 → proxy.ecnu.edu.cn/vpn_key/update` **不是拦截**，
   而是正常第一跳 —— 剩下的只差「人在浏览器里把密码输进去」。
2. **业务接口路径与字段仍是待校正状态**：`docs/API.md` 中标为「⬜ 待抓包」；
   代码以「候选路径 + 嗅探 + 宽容解析」应对。
3. **打包版首次登录需要 Chromium**：未内嵌（体积取舍），`--doctor` 会检查并给指引。
4. **本地 `faster-whisper` 兜底需自行安装**：未放进默认依赖（避免为可选功能拖入 GB 级模型）。
5. **单进程 GUI**：任务队列在 QThread 里跑，不是多进程；GIL 对 IO 密集的下载/ASR 影响很小，
   但若将来加本地模型推理，应改为进程池。
6. **摘要质量依赖 DeepSeek**：关闭 LLM 时 `.md` 只有元信息 + 全文，没有摘要（如实标注，不伪造）。
7. **音频缓存不自动清理**：提供「设置 → 高级 → 清空音频缓存」手动清理，避免误删用户数据。
8. **暂停的粒度是「分段边界」而非「随时」**：正在发出的那一次 ASR 请求不会被中断
   （中断会白费已花的额度），因此最长等待时间 = 单段处理时间（默认单段 ≤ 10 分钟）。
   要更细的暂停粒度就得把单段时长调小。ffmpeg 拉流是例外 —— 它会被就地冻结，响应很快。
9. **暂停状态不跨进程持久化**：应用退出后重开是「从断点续跑」，不会恢复「暂停中」这个状态
   （这是有意的：重启后自动继续跑更符合预期）。

---

## 6. 后续可做的事

| 优先级 | 事项 | 价值 |
| --- | --- | --- |
| 高 | 打通网络后完成真实验收，用真实响应填满 `docs/API.md` | 闭合 M0–M4 验收 |
| 高 | 真实资源跑通后，把嗅探到的真实接口**固化**为默认候选（放在候选列表最前） | 首次使用少一次嗅探 |
| 中 | 课程/资源增量刷新（只拉变更，用 `record_time`/`updateTime` 做水位） | 大账号下刷新更快 |
| 中 | 批量导出：把一门课的全部转写合并成一个 `.md`（带目录） | 复习/检索体验 |
| 中 | 转写结果全文检索（sqlite FTS5） | 在 GUI 里搜「哪节课讲过某个概念」 |
| 中 | ASR 结果质量自检：对低置信度分段标注、支持人工校正后回写 | 提升可用性 |
| 低 | 说话人分离 + 角色标注（DashScope 支持时） | 多人课堂场景 |
| 低 | 任务级速率自适应：遇 429 自动降速并记住 | 抗风控更稳 |
| 低 | 单实例锁 + 系统托盘通知 | 桌面体验 |
| 低 | 把 `--onedir` 改为可选 `--onefile` 附加产物 | 便携分发 |

---

## 7. 测试策略

| 层 | 手段 | 数量 |
| --- | --- | --- |
| 单元 | `pytest`，全部离线、fixture 驱动（`tests/fixtures/catalog_sample.json` 为结构仿真、内容虚构） | **312** |
| 契约 | 平台响应解析用多种字段命名样本覆盖（camelCase / snake_case / 别名） | 含在上项 |
| 集成 | `scripts/selftest_e2e.py`：本地生成 **AES-128 加密 HLS** → 真实 ffmpeg 拉流 → 假 ASR → 三产物 → 断点续跑 | **22 项检查** |
| 集成 | `scripts/demo_offline.py`：合成真实中文语音 → 加密 HLS → 拉流 → **真实本地 ASR** → 6 个产物 → 断点续跑 | **全流程（2/2 成功）** |
| 集成 | `tests/test_login_state.py`：登录态**契约往返**（假平台收真实请求头验证 Cookie/token 真的发出去了）+ 10 种畸形文件不崩 | **22 项检查** |
| 集成 | `tests/test_store_concurrency.py`：状态库**并发安全**与**真·强杀后崩溃恢复**、`recover_orphans` 幂等性 | **13 项检查** |
| 集成 | `tests/test_downloader.py`：真实 HLS 下载 + 限速生效 + 签名 URL + 本地文件路径 + 缓存命中 | **11 项检查** |
| 集成 | `tests/test_settings_dialog.py`：设置对话框的**往返保真度**（40+ 控件逐项断言、凭据不落明文、预设与 README 一致） | **11 项检查** |
| 集成 | `tests/fake_dashscope_server.py` + `tests/test_dashscope_native.py`：DashScope **原生异步模式**全链路（凭证→OSS 上传→异步提交→轮询→取结果） | **15 项检查** |
| 集成 | `tests/fake_llm_server.py` + `tests/test_llm_postprocess.py`：LLM 后处理的**安全不变量**（数量/时间戳不变、10 种畸形响应退化为原文、失败不丢结果） | **34 项检查** |
| 集成 | `tests/fake_asr_server.py` + `tests/test_chunked_pipeline.py`：真实多段切分 → 逐段 ASR → 平移合并，断言内容不丢不重、时间轴只加一次偏移且不越界 | **10 项检查** |
| 集成 | `scripts/verify_pause.py`：真实流水线中途暂停 → 恢复 → 完成；暂停中停止 → 立刻 canceled | **9 项检查** |
| 集成 | `scripts/mock_platform.py`：本地模拟平台 + 真实 HTTP，验证分页 / 业务码 / 鉴权失效 / 候选探测 / 播放地址 / 脱敏 | **26 项检查** |
| 集成 | `tests/test_gui_recovery.py`：真实 MainWindow + 真实 PipelineWorker 的**异常路径与恢复操作**（登录失效/DRM/取消/失败、移除/重跑/清空/清除登录态） | **16 项检查** |
| 验收 | `scripts/verify_gui.py`：驱动**真实 GUI 控件**跑完 M4 验收（刷新→勾选→入队→QThread 执行→6 产物→断点续跑） | **29 项检查** |
| 安全 | 凭据不明文落盘、日志端到端脱敏、抓包按头名脱敏、`.gitignore` 覆盖 | 含在上项 |
| 产物 | `scripts/verify_dist.py`：复制到纯 ASCII 路径 + 全新用户目录 + 无 Python 的 PATH 下启动 exe | **20 项检查** |
| 产物 | `scripts/doctor.py`（冻结态路径 / 真实 ffmpeg 转码 / DPAPI / 状态库 / Chromium） | **17 项检查** |
| 冒烟 | `exe --selftest`（GUI 启动退出）、`exe --doctor` | 2 条 |

**为什么集成测试要自己造加密 HLS**：真实平台不可达时，仍需验证「HLS 分片 + AES-128 key 请求带 Cookie
+ 相对路径还原 + 时长一致性」这条最容易出错、也最影响用户体感的链路。
用 `ffmpeg -hls_key_info_file` 本地生成加密流 + 内置 HTTP server，就能在完全离线的情况下
把这条链路测成确定性用例（实测时长偏差 0.27%）。

**为什么还要一个「离线演示」脚本**：单元测试证明的是「各部分正确」，
`demo_offline.py` 证明的是「**整条产品链路在真实语音、真实加密流上能跑出可读的产物**」——
并且它能给任何拿到这个仓库的人一条命令就看到结果，不需要账号、网络或 API Key。
语音来源优先用 Windows 中文语音合成（SAPI），没有可用语音时退化为音调占位并**在输出里如实标注**，
绝不把「链路通」伪装成「识别准」。
