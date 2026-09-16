# API 文档 — 大夏学堂 / 资源管理平台

> 状态标记：**✅ 实测** = 本机真实请求验证过；**🟡 推断** = 依据页面/网关行为推断，待登录后校正；
> **⬜ 待抓包** = 需要登录后由 `scripts/login.py --capture` 抓真实请求填充。
>
> 最后更新：2026-09-12（第 22 轮：真实浏览器跳转链实测 + 更正「校外=被封」的旧结论）
> 抓包方式：`python scripts\login.py --capture` → `recon/network.jsonl` + `recon/api_candidates.json`（**均已脱敏**）

---

## 0. 站点与访问路径

### 0.1 入口

| 项 | 值 | 状态 |
| --- | --- | --- |
| 前端入口 | `https://courses.ecnu.edu.cn/jy-application-resourcemanage-ui/#/home` | ✅ 实测（302，见下） |
| 应用根路径 | `/jy-application-resourcemanage-ui/` | ✅ |
| 技术形态 | 前端 SPA，Vue hash 路由（`#/`） | 🟡 由 URL 结构推断 |
| 站点真实 IP | `202.120.88.100` | ✅ 公共 DoH（223.5.5.5）解析 |
| 本机解析 | Clash fake-IP `198.18.0.181` | ✅ 本机 DNS 被 Clash 接管 |

### 0.2 校外访问会跳到 webVPN 网关 —— 这是**正常入口**，不是「被封」（✅ 实测）

```http
GET /jy-application-resourcemanage-ui/ HTTP/1.1
Host: courses.ecnu.edu.cn
```

```http
HTTP/1.1 302 Found
location: https://proxy.ecnu.edu.cn:443/vpn_key/update?origin=https%3A%2F%2Fcourses.ecnu.edu.cn%2Fjy-application-resourcemanage-ui%2F&reason=site+courses.ecnu.edu.cn+not+found
set-cookie: SERVERID2=Server1; path=/
content-type: text/html; charset=utf-8
content-length: 192
```

要点：
- 用 `Host: courses.ecnu.edu.cn` 直连真实 IP，或经代理，**结果相同**（都是这个 302），
  说明拦在服务端而不是本地网络层。
- `reason=site ... not found`：网关（Astraeus）还没有为当前会话登记该站点 —— 它会先把
  你送去认证，认证完再登记。**这不是「站点不存在」的拒绝。**
- 结论（**2026-09-12 用真实浏览器抓包更正**）：这条 302 是校外访问的**正常第一跳**。
  校外**不必**先装 SSL-VPN，按 §0.2.1 的链路在浏览器里完成一次统一身份认证即可。

#### 0.2.1 真实浏览器里的完整跳转链（✅ 2026-09-12 实测，含最终落地页）

```
① GET https://courses.ecnu.edu.cn/jy-application-resourcemanage-ui/          302
② GET https://proxy.ecnu.edu.cn/vpn_key/update?...reason=site+...+not+found  302
③ GET https://proxy.ecnu.edu.cn/users/sign_in                                302
④ GET https://api.ecnu.edu.cn/oauth2/authorize?scope=ECNU-Basic&...          301
⑤ GET https://sso.ecnu.edu.cn/oauth2.0/authorize?client_id=...              302
⑥ GET https://sso.ecnu.edu.cn/login?service=...                              200  ← 统一身份认证登录页
⑦   GET  /public/cas-login-new/assets/i18n/zh-CN.json                        200
     GET  /public/deploy/domain/domain.json                                  200
     POST /sso-extend/protected/api/dictconfig/get                           200
     GET  /sso-extend/protected/api/login/configs/get                        200
     GET  /protected/api/aggregate/login_info/get                            200
     GET  /public/svg/name-icon.svg   /public/svg/password-icon.svg          200
```

判定依据（**别再靠 URL 关键字猜**）：
- 第 ⑥ 跳返回 **200 且登录页的 JS/CSS/配置接口全部 200** → 页面已就绪，正如实等待用户输入；
- 此后若 600s 内**没有任何新请求**，说明「人没提交凭据」，与网络无关；
- 只有**停在 ②/③ 且页面正文写着 `site not found` / 「站点不存在」**时，才是网关真的拒绝，
  这时才需要接入校园网或 SSL-VPN（`https://vpn.ecnu.edu.cn/portal/`）。

> ⚠️ 踩坑记录（缺陷 37）：`sso.ecnu.edu.cn/login?service=...` 的 query 里
> **URL-encode 了 `proxy.ecnu.edu.cn`**（`redirect_uri` 参数）。早先的诊断代码对整条 URL
> 做 `"proxy.ecnu.edu.cn" in url` 子串匹配，于是把「停在登录页等人输密码」误报成
> 「被 webVPN 网关拦下」，把用户指去折腾网络。现已改为**按主机 + 路径 + 页面正文**判断。

### 0.3 统一身份认证链路（✅ 实测）

```
GET https://proxy.ecnu.edu.cn/users/sign_in
  → 302 https://api.ecnu.edu.cn/oauth2/authorize
        ?scope=ECNU-Basic
        &redirect_uri=https://proxy.ecnu.edu.cn/ecnu_oauth2
        &response_type=code
        &client_id=d46ba84ffc58611f
        &state=<32位随机串>

GET https://api.ecnu.edu.cn/oauth2/authorize?...
  → 301 https://sso.ecnu.edu.cn/oauth2.0/authorize
        ?client_id=d46ba84ffc58611f&response_type=code
        &redirect_uri=...&scope=ECNU-Basic&state=...
```

| 项 | 值 |
| --- | --- |
| OAuth2 client_id | `d46ba84ffc58611f` |
| scope | `ECNU-Basic` |
| redirect_uri | `https://proxy.ecnu.edu.cn/ecnu_oauth2` |
| 认证服务 | `https://sso.ecnu.edu.cn/oauth2.0/authorize` |
| 网关会话 Cookie | `_astraeus_session`（值加密，形如 base64；**抓包里已脱敏**） |
| 负载均衡 Cookie | `SERVERID2=Server1` |

### 0.4 SSL-VPN 门户（✅ 实测）

```http
GET https://vpn.ecnu.edu.cn/portal/   → 200 OK，约 14 KB HTML（SSL-VPN 门户）
```

### 0.5 登录态如何维持（✅ 实测校正）

| 载体 | 实测结果 |
| --- | --- |
| Cookie（共 16 个） | 平台业务：**`jy-application-resourcemanage`**；网关：`_astraeus_session`、`_webvpn_key`、`webvpn_username`；认证：`SOURCEID_TGC`、`SESSION`、`SERVERID1/2`；其它：`__snaker__id`、`clientThemeKey`、`gdxidpyhxdE`、`rg_objectid`、`route`、`sidTheme`、`_bl_dept`、`_bl_usercode` |
| Cookie 域 | `.ecnu.edu.cn`、`courses.ecnu.edu.cn`、`proxy.ecnu.edu.cn`、`portal1-443.proxy.ecnu.edu.cn`、`sso-443.proxy.ecnu.edu.cn`、`sso.ecnu.edu.cn` |
| localStorage | `jy-application-resourcemanage-ui_STORAGE_KEY_REFRESH_TOKEN`（刷新令牌） |
| 真正的接口鉴权 | **不是** Cookie 单打独斗：先用 Cookie 换 `jwt-token`（见 §1.1），再把 `jwt-token` 放进请求头 |
| 过期表现 | `/oauth2/token` 被 **302** 重定向回 `proxy.ecnu.edu.cn/vpn_key/update` → 映射为 `AuthExpiredError` |
| **有效期（实测）** | **不跨天**：2026-09-12 21:03 登录，2026-09-14 09:47 已失效。应用启动时会检查保存时间，超过 12 小时就提示「很可能已过期」 |

---

## 1. 真实接口（✅ 2026-09-12 用真实账号实测打通，客户端已按此实现）

> 这一节是**真机验证过的契约**，`EcnuClient` 默认走这条路（`cfg.platform_api = True`）。
> 旧的第 2 节「候选路径探测」只在拿不到 jwt-token 时兜底（例如对着模拟平台跑测试）。

### 1.1 鉴权：先换 `jwt-token`

```http
GET /jy-application-resourcemanage/oauth2/token HTTP/1.1
Cookie: jy-application-resourcemanage=…; _webvpn_key=…; SOURCEID_TGC=…
```

```json
{"code":"0","result":{
  "access_token":"<108 字符>",
  "jwt_token":"<910 字符>",          ← 真实接口要的是这个
  "refresh_token":"<108 字符>","token_type":"…","expires_in":86400,
  "clientSign":"jy-application-resourcemanage","scope":"…","username":"<学号>"
}}
```

| 项 | 值 |
| --- | --- |
| 鉴权头 | `jwt-token: <result.jwt_token>`（**不是** `Authorization: Bearer`） |
| Cookie | 仍必须带（`jy-application-resourcemanage` + webVPN 的 `_webvpn_key` 等 16 个） |
| 接口前缀 | `/jy-application-resourcemanage`（由 SPA 的 `global-production.json` 里的 `BASE_URL` 给出） |
| 失效表现 | 302 跳登录 / 401 / 业务 `ok:false` → `AuthExpiredError` |

### 1.2 我的课表（录播清单的主口径）

```http
GET /jy-application-resourcemanage/v1/list/termYear
→ [{acteTerm, acyeCode, currentTerm, id, ...}]        # id 即 acteId

GET /jy-application-resourcemanage/v1/myself/curriculum
      ?acteId=<acteId>&page.pageIndex=1&page.pageSize=50
jwt-token: <…>
```

| 项 | 值 |
| --- | --- |
| 分页参数 | **`page.pageIndex` / `page.pageSize`**（写成 `pageNum/pageSize` 会被回「分页不能为空」） |
| 返回 | `{data:{records:[…], rowCount:N, pageCount:M}}`（**行在 `records`**，不在 `list`） |
| 每行含义 | **一节课**：`id` = courId、`subjName` 科目、`teclId` 教学班、`courBeginTime` 上课时间、`clroName` 教室、`teacNames` 教师、`courVodOpen` = 1 表示有录像 |
| 实测数据 | 单个真实测试账号：3 个学期有课，共 **30 门课 / 894 节有录播的课**（2025-2026 第1/2学期 + 2026-2027 第1学期） |
| 状态 | ✅ 实测 |

> **踩过的坑**：`GET /v1/teachingclass/list?type=1&allType=1` 返回的是**行政班级**
> （「2022法学心理学统招生…」这类年级班），不是本人在上的课 —— 别拿它当"我的课程"。
> `POST /v1/statistics/teaching-class/user/course-list` 的 id 字段名是
> **`teachingClassId`**（写成 `teclId` 会被回「教学班id不为空」）；它按教学班列节次，
> 可作为补充口径。

### 1.3 播放地址（带签名的直连 mp4）

```http
GET /jy-application-resourcemanage/v1/course_vod_urls_new?courseId=<courId>
jwt-token: <…>
```

```json
{"code":null,"data":{
  "courName":"线性代数","classRoomName":"教书院319","courBeginTime":1765420800000,
  "courseVodVideoDtoList":[
    {"vodId":701828,"vodTime":3301,"viewNum":1,
     "url":"https://dudaomedia.ecnu.edu.cn:40443/vod/…/1765420500000-1765423800000.mp4?auth_key=1789190665-0-0-c315f6…"},
    {"vodId":701815,"vodTime":3299,"viewNum":5,"url":"…auth_key=…"}
  ]}}
```

| 项 | 值 |
| --- | --- |
| 媒体形态 | **直连 MP4（HTTPS，带 `auth_key` 签名）**，不是 HLS |
| 点播服务器 | `dudaomedia.ecnu.edu.cn:40443`（由 `/v1/config/vodNmediaConfigInfo` 给出） |
| 实测 | `ffprobe` 读到 `duration=3301.217`、`size=838798382`；`Range` 请求返回 **206**；ffmpeg 可正常抽音频 |
| 同一节课多机位 | `courseVodVideoDtoList` 可能有多条（本例 2 条：701828 / 701815），客户端取**时长最长**的一条 |
| 状态 | ✅ 实测（真实拉流成功） |

### 1.4 其它已确认的接口

| 接口 | 方法 | 用途 | 状态 |
| --- | --- | --- | --- |
| `/v1/app/info` | GET | 应用信息 | ✅ |
| `/authority/me` | GET | 我的权限与菜单树 | ✅ |
| `/resource/resources_tree_me` | GET | 资源/菜单树 | ✅ |
| `/v1/list/recentWatchRecord` | POST（body `{"page":{…}}`） | 观看记录（含 courId/teclId/科目/教师） | ✅ |
| `/v1/getVodCourseVideo?courId=` | GET | 某节课的录播文件清单（vodId + 服务器侧 path + 大小） | ✅ |
| `/v1/web/login/acknowledge` | GET | 登录确认（前端调用） | ✅ |
| `/v1/vod/keepAlive` | POST | 播放心跳（需 courId + 科目编号） | 待用 |
| `/v1/course/ai/course/subtitle/export` | — | 平台自带的 AI 字幕导出 | 未使用（本工具自己转写） |

---

## 2. 兜底：候选路径探测（仅在拿不到 jwt-token 时使用）

> 当 `GET /oauth2/token` 不返回令牌（例如指向模拟平台做测试）时，客户端回退到
> 「候选路径探测 + 结构宽容解析」：按顺序尝试下列路径，命中即缓存；
> 全部失败则抛 `ApiChangedError` 并给出每个候选的失败原因，**绝不静默返回空清单**。

### 2.1 课程 / 开课列表

| 项 | 值 |
| --- | --- |
| 候选路径 | `/jy-application-resourcemanage-ui/api/course/list`、`/api/course/list`、`/api/courses`、`/jy-application-resourcemanage-ui/api/teachingClass/list`、`/api/teachingClass/list`、`/api/resource/course/list` |
| 方法 | POST |
| 必要 header | `User-Agent`、`Accept: application/json`、`Referer: <入口 URL>`、`X-Requested-With: XMLHttpRequest`、`Cookie`（+ 可能的 token 头） |
| 请求体 | `{"pageNum":1,"pageSize":100,"current":1,"size":100}` |
| 分页 | 同时兼容 `pageNum/pageSize` 与 `current/size`；用 `total` 或「本页不足 pageSize」判定结束 |
| 鉴权 | Cookie / token；失效 → `AuthExpiredError` |
| 响应样例 | 见 `tests/fixtures/catalog_sample.json` → `course_list`（**结构仿真、内容虚构**） |
| 状态 | ⬜ 待抓包校正 |

**已实现的响应解包能力**（`EcnuClient._check_business` + `extract_list`）：

- 业务包装：`{code, msg, data}`，`code ∈ {0,200,"0","200","0000","00000","success"}` 视为成功；
  多层包装递归剥离（`{code, data:{code, data:{...}}}`）。
- 未登录码：`401/403/"401"/"403"/"40100"/"A0230"/"NOT_LOGIN"/"TOKEN_EXPIRED"/…`
  以及消息含「未登录 / 请登录 / 登录已过期 / unauthorized / session expired」等 → `AuthExpiredError`。
- 列表定位：**有界深度优先搜索**，容器键候选
  `rows/records/list/items/data/content/result/dataList/...`；
  总数键候选 `total/totalCount/totalElements/count/totalNum/recordsTotal`。

### 1.2 课程下的资源（录播 / 回放 / 章节）

| 项 | 值 |
| --- | --- |
| 候选路径 | `/jy-application-resourcemanage-ui/api/resource/list`、`/api/resource/list`、`/api/course/resource/list`、`/api/resource/page`、`/api/courseResource/list`、`/api/ware/list` |
| 方法 | POST |
| 请求体 | `{"courseId":"<id>","id":"<id>","pageNum":1,"pageSize":100,...}` |
| 兜底 | 若资源列表接口不可用，则使用课程列表响应里**内嵌**的资源数组（`resources/resourceList/records/wareList/videoList/...`） |
| 状态 | ⬜ 待抓包校正 |

**资源字段的宽容映射**（`Resource.from_api`，按候选键取第一个有值项）：

| 输出字段 | 候选键 |
| --- | --- |
| `resource_id` | `resourceId, resource_id, id, courseResourceId, coursewareId, wareId, videoId, mediaId, materialId, fileId, bizId, uuid` |
| `title` | `title, resourceName, resource_name, name, wareName, videoName, coursewareName, fileName, materialName, nodeName, subTitle, caption` |
| `play_url` | `playUrl, play_url, playurl, url, videoUrl, video_url, fileUrl, file_url, mediaUrl, resourceUrl, downloadUrl, hlsUrl, m3u8, m3u8Url, playPath, path, src, content` |
| `duration_sec` | `duration, durationSec, duration_sec, durationSeconds, videoDuration, length, timeLength, playTime, totalTime, mediaDuration`（支持 `"01:02:05"` 形式） |
| `record_time` | `recordTime, record_time, startTime, createTime, beginTime, publishTime, updateTime, classTime, liveStartTime, date, gmtCreate, occTime`（支持毫秒时间戳） |
| `size_hint` | `size, fileSize, file_size, sizeHint, bytes, contentLength` |
| `mime` | `mime, mimeType, contentType, mediaType, fileType, format, type`（缺失时按 URL 后缀猜） |

### 1.3 单个资源的播放地址与音视频元信息

> **实际主路径不是这里的候选探测**，而是 §1.1 的 `GET /v1/course_vod_urls_new?courseId=<courId>`
> （实测契约）。本节保留的是「拿不到 jwt-token 时」的兜底候选与字段表。

| 项 | 值 |
| --- | --- |
| 候选路径 | `/jy-application-resourcemanage-ui/api/resource/play`、`/api/resource/play`、`/api/resource/detail`、`/api/resource/playInfo`、`/api/courseResource/playUrl` |
| 方法 | POST |
| 请求体 | `{"resourceId":"<id>","id":"<id>","courseId":"<id>","resourceType":"<type>"}` |
| 响应字段 | `playUrl/url/videoUrl/hlsUrl/m3u8Url/fileUrl/playPath/path/src`；多码率时取 `playUrls/urls/qualities/list[0]` |
| 相对路径还原 | `//host/...` → 补 scheme；`/vod/...` → 与 `api_base` 拼接 |
| 播放地址时效签名 | ✅ **带时限签名**（`auth_key=<unix ts>-0-0-<md5>`）。实测隔天复用旧地址会被拒（401/403）—— 代码据此抛 `PlayUrlExpiredError` 并**重新解析**，而不是拿死 URL 反复重试 |
| 状态 | ✅ 真实路径已实测（30 门课 / 894 条录播） |

#### 播放地址的传输特性（实测，2026-09-14）

| 项 | 实测值 | 对实现的影响 |
| --- | --- | --- |
| 文件大小 | `838874682` 字节（≈800 MB / 55 分钟课） | 只抽音频（64 kbps 单声道）能省 97% 磁盘 |
| Range 支持 | `Range: bytes=0-1023` → **HTTP 206** + `Content-Range: bytes 0-1023/838874682` | 断点续传可以用 `-ss N` **按字节跳转**，只补抓剩余部分（实测补抓段达到 1.9× 实时） |
| 实测下载速度 | 0.9× ~ 2.0× 实时（随节点/时段波动） | 看门狗下限取 0.25× 实时，留足余量；单条约 30–60 分钟 |
| 音轨 | AAC 单声道 16 kHz | 直接转 16 kHz 单声道 mp3 送 ASR，无需重采样对齐 |

---

## 2. 已实现但待真实验证的媒体侧能力

| 能力 | 实现位置 | 已验证方式 |
| --- | --- | --- |
| HLS（`.m3u8` + 分片） | `downloader._download_once` → ffmpeg | ✅ 本地加密 HLS 端到端 |
| AES-128 加密分片 | ffmpeg 带同一份 `Cookie/Referer/UA` 请求 key | ✅ 本地 AES-128 HLS（时长偏差 0.27%） |
| m3u8 内相对路径还原 | 交给 ffmpeg 按 base URL 解析；同时日志打印基准分片 | ✅ |
| DRM 检测 | `media.detect_drm_in_text` + ffmpeg 输出扫描 | ✅ 命中 widevine 即 `DrmDetectedError` |
| 只取音频 | `-vn -ac 1 -ar 16000 -c:a libmp3lame -b:a 64k` | ✅ |
| 超时重试 + 指数退避 | `_download_with_retry` | ✅（`download_retries` 可配） |
| 缓存命中跳过 | `cache/media/<标题>__<id>.<ext>`，校验时长一致性 | ✅ 第二次命中缓存 |
| sha256 / 时长记录 | `AudioResult` + `state.db` | ✅ |
| 并发 ≤ 2 + 300–800ms 抖动 | `Semaphore(min(2, concurrency))` + `_jitter()` | ✅ |
| mp4 / 直链音频 | 同一 ffmpeg 通路 | 🟡 同代码路径，待真实资源验证 |

---

## 3. ASR / LLM 端点（第三方，非学校平台）

### 3.1 阿里云百炼 DashScope（默认）

**OpenAI 兼容模式**（默认，`asr_use_native_api=false`）：

```http
POST https://dashscope.aliyuncs.com/compatible-mode/v1/audio/transcriptions
Authorization: Bearer <DASHSCOPE_API_KEY>
Content-Type: multipart/form-data

file=<音频>
model=paraformer-v2
language=zh
response_format=verbose_json      # 不支持时自动回退 srt / json
```

**原生异步模式**（`asr_use_native_api=true`，大文件/长音频更稳）：

```
GET  https://dashscope.aliyuncs.com/api/v1/uploads?action=getPolicy&model=<model>
       → data.upload_host / upload_dir / access_key_id / policy / signature
POST <upload_host>  (multipart: OSSAccessKeyId, policy, Signature, key, file)
POST https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription
       Header: X-DashScope-Async: enable
       Body:   {"model":..., "input":{"file_urls":["oss://..."]},
                "parameters":{"language_hints":["zh"],"enable_timestamp":true}}
       → output.task_id
GET  https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}   → SUCCEEDED
GET  <transcription_url>                                     → transcripts[].sentences[]
```

### 3.2 DeepSeek（仅文本加工，**没有** ASR）

```http
POST https://api.deepseek.com/v1/chat/completions
Authorization: Bearer <DEEPSEEK_API_KEY>
{"model":"deepseek-chat","messages":[...],"temperature":0.1,"response_format":{"type":"json_object"}}
```

用途：错别字/标点/术语修复（返回 `{"segments":[{"i":<序号>,"text":"..."}]}`，
**只改文本不改时间轴**）、按语义分段（返回 `{"break_after":[序号...]}`）、
生成结构化摘要与大纲。**任何一步失败都不阻塞产物输出。**

---

## 4. 实测清单（原「待抓包」逐项已填）

| 项 | 实测结果 |
| --- | --- |
| 课程/开课列表 | ✅ `GET /v1/myself/curriculum?acteId=&page.pageIndex=&page.pageSize=`（行在 `records`，含 `rowCount`/`pageCount`）；另有 `GET /v1/list/termYear` 提供 acteId |
| 资源列表（录播） | ✅ 每节课用 `GET /v1/course_vod_urls_new?courseId=<courId>`，字段 `data.courseVodViewList[]`（`vodId`/`vodTime`/`viewNum`/`url`） |
| 播放地址接口 | ✅ 同上；**URL 带 `auth_key` 时效签名**（形如 `auth_key=1789190665-0-0-<md5>`），且同一节课可能返回多个机位 |
| 登录态 | ✅ 16 个 Cookie + localStorage 刷新令牌；真实接口另需 `jwt-token`（见 §0.5 与 §1.1）；**实测不跨天失效** |
| 视频形态 | ✅ **直连 MP4（H.264 + AAC 单声道 16 kHz）**，由 `dudaomedia.ecnu.edu.cn:40443` 提供，支持 `Range`（返回 206）；**未发现 DRM**（另有 DRM 检测兜底） |
| 音频电平 | ⚠️ **实测跨度极大**：抽查 5 条为 `-2.0 / -4.2 / -6.2 / -29.1 / -66.8 dB` 峰值；极低的会让 ASR 判成静音 → 客户端已做自动增益与诚实报错（见 PROGRESS §1.65.7） |
| 章节/目录结构 | ✅ 课表按「教学班 → 节次」组织，节次里带 `letiNumber`（第几节）、`clroName`（教室）；界面按**科目**分组展示 |
| 风控表现 | 🟡 未触发验证码；实测出现过**代理出口变更导致连接被重置**（`WinError 10054`），已在诊断里给出直连规则建议 |

> 每条都在上文附了真实返回样例（脱敏后）。剩余未验证项：
> 大规模并发下的限流阈值（本应用默认并发 2、带抖动与退避）。
