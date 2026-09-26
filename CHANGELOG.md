# TwTube 修改与优化日志 (CHANGELOG)

本项目基于开源项目 [MeTube](https://github.com/alexta69/metube)（基于 `yt-dlp` 的 Web 视频下载工具）进行深度定制开发，主要用于 NAS 环境下的多平台视频下载、自动化归档与推送。

---

## 📌 核心定制特性概览

1. **界面汉化与体验优化**
   - Web 界面全面汉化，网页标题改为 **TwTube**。
   - 任务列表与已完成列表显示平台徽章、博主名、发布日期与视频参数。

2. **抖音 (Douyin) 专属最高清直连下载**
   - 突破 `yt-dlp` 默认仅能获取带水印 720P 的限制。
   - 接入 `a_bogus` / `X-Bogus` 算法签名与 `ttwid`/`msToken` 自动化生成。
   - 适配 App 端接口 (`aid=6383`) 绕过网页端风控，支持 1080P/1440P/4K 无水印原画下载。
   - 自动清洗短链、重定向解析与 `modal_id` 提取。

3. **Telegram 机器人自动化集成 (`app/tg_bot.py`)**
   - 支持通过 Telegram Bot 发送视频链接直接推入下载队列。
   - **媒体采集**：机器人收到的视频/图片/文件自动落盘到 `telegram/` 子目录（MTProto/Telethon，单文件上限 2GB），并在 Web 完成列表单独筛选。
   - **长连接自愈机制**：针对代理抖动导致长轮询挂起问题，实现 60 秒主动健康检查（API 探活、Pending 消息积压检测）及自动重建。
   - 下载完成时自动回传通知，包含博主名、发布日期及视频名称。

4. **平台自动识别与目录分流**
   - 核心层自动识别视频来源平台（抖音、TikTok、Instagram、YouTube、B站等）。
   - 自动分流下载到对应的平台专属子目录，方便 NAS 媒体分类管理。

5. **视频真实参数探测与规范命名 (`app/video_info.py`)**
   - 文件命名统一为：`[博主名]_[发布日期]_[视频标题]`（固定北京时间）。
   - 视频下载/合并完成后，通过 `ffprobe` 探测真实分辨率与帧率，自动在文件名末尾追加标签（如 `[2160x3840 60fps]`）。
   - 持久化保存博主名与日期，避免服务重启后已完成列表信息丢失。

6. **防同名覆盖机制**
   - 下载与重命名时检测同名文件，自动追加 `_1`、`_2` 计数后缀，杜绝文件被静默覆盖。

7. **容器化与自动化构建**
   - Dockerfile 增加 `tzdata` 时区支持，锁定北京时间。
   - GitHub Actions 自动化检测 `yt-dlp` 更新并构建发布多架构 Docker 镜像。

---

## 🕒 历史变更记录

### 2026-09-26
- **feat(TG)**: 新增 Telegram 媒体采集——机器人收到的视频/图片/文件自动落盘并登记进完成列表。
  - 走 MTProto（Telethon）下载，突破 Bot API `getFile` 的 20MB 上限，单文件可达 2GB；用同一 bot token 登录，无需手机验证码，只需补 `TG_API_ID`/`TG_API_HASH`（`app/tg_bot.py`、`pyproject.toml`）。
  - 媒体落盘到 `DOWNLOAD_DIR/<TG_MEDIA_DIR>`（默认 `telegram`），文件名来自 Telegram 原名并做路径清洗 + 重名 `_1/_2` 防覆盖；登记记录的 `folder=telegram`、`filename` 仅存文件名，与前端 `download/<folder>/<filename>` 链接拼接一致（`app/ytdl.py::allocate_tg_media_path`/`enqueue_tg_media`）。
  - 相册（media group）按 `media_group_id` 缓冲聚合后统一登记，避免一次相册被拆成多条记录（`app/tg_bot.py`）。
  - Telethon **不支持 http 代理**（仅 socks5/mtproto），新增 `TG_PROXY_URL` 单独配置；未配置时直连，Bot API 侧仍照常走 `HTTP_PROXY`（`app/tg_bot.py::_parse_proxy_url`）。
- **feat(前端)**: 完成列表平台筛选新增 **Telegram** 一档，并补 `getPlatformKey`/`getPlatformName` 对 `tg://` 标记的识别（`ui/src/app/app.ts`、`ui/src/app/app.html`）。
- **feat(格式)**: `dl_formats` 放行 `images`/`document` 两种非 yt-dlp 下载类型，避免构造 Download 槽位时抛错（`app/dl_formats.py`）。
- **feat(配置)**: `Config` 新增 `TG_MEDIA_DIR`（含相对路径校验，非法值回退默认）并加入 `_FRONTEND_KEYS`（`app/main.py`）。

### 2026-09-19
- **fix(TG)**: 代理抖动致轮询停摆不自愈，改为 60s 主动健康检查（含 pending 堆积检测）+ 重建 (`app/tg_bot.py`)。
- **fix(TG)**: 修复 Telegram 机器人异常崩溃问题。
- **feat(命名)**: 下载完成后使用 `ffprobe` 探测真实分辨率/帧率并写入文件名，如 `[2160x3840 60fps]` (`app/video_info.py`)。
- **build**: Dockerfile 增加 `tzdata` 时区支持 (`Dockerfile`)。
- **ci**: 用 PAT 触发镜像构建，修复 yt-dlp 自动更新后不构建镜像的问题 (`.github/workflows/`)。
- **fix(抖音)**: 改用 App 端接口 (`aid=6383`) 绕过网页风控、恢复最高清；日期固定为北京时间 (`app/douyin_hd.py`)。
- **fix(防覆盖)**: 下载前预留唯一文件名，重名自动加 `_1`/`_2` (`app/ytdl.py`)。
- **refactor(命名)**: 命名格式统一为 `博主名+日期+视频名`（涉及文件名、已完成列表、TG 通知）。
- **fix(持久化)**: 修复重启后已完成列表日期与博主名丢失的问题（`uploader`/`upload_date` 持久化）。
- **feat(UI/TG)**: 抖音文件名加日期前缀，TG 机器人增加平台中文名，完成通知附带日期+博主名。

### 早期基础定制
- **feat**: 重新加入抖音最高清直连下载（`a_bogus`/`X-Bogus` 签名 + `ttwid`/`msToken` 生成）。
- **feat**: 已完成列表新增平台徽章显示。
- **feat**: 全局核心层统一注入平台子目录自动分流。
- **feat**: 完成界面汉化、抖音/IG 支持及 Telegram 机器人自动分流下载。
- **chore**: 网页标题与品牌修改为 TwTube。

---

## 📝 后续修改记录规范

后续每次在本代码库进行功能修改、Bug 修复或优化时，均按如下格式在本章节追加记录：

```markdown
### YYYY-MM-DD - [修改简述]
- **修改类型**: [feat / fix / refactor / perf / chore]
- **涉及文件**:
  - `path/to/file1`
  - `path/to/file2`
- **详细说明**:
  1. 具体修改原因与解决的问题。
  2. 实现细节与逻辑说明。
  3. 验证情况或注意事项。
```

---

### 2026-09-20 - Telegram精准引用回复、抖音图集与断点续传/异步I/O、NAS碎片清理与超时熔断、前端分类搜索分页
- **修改类型**: feat / perf / fix
- **涉及文件**:
  - `app/tg_bot.py`
  - `app/main.py`
  - `app/douyin_hd.py`
  - `app/ytdl.py`
  - `ui/src/app/app.ts`
  - `ui/src/app/app.html`
  - `ui/src/app/app.spec.ts`
  - `app/tests/test_douyin_enhancements.py`
  - `AGENTS.md`
  - `.gitignore`
- **详细说明**:
  1. **Telegram 机器人下载结果精准引用回复**:
     - `TelegramBotManager` 中增加 `_msg_contexts` 追踪 URL/ID 对应的 `chat_id` 与 `message_id`。
     - 用户发送视频链接入队时记录上下文，下载完成或失败推送通知时，精准引用（Reply）触发下载的原始消息。
     - 增加容错回退机制：当原消息已被删除导致 Telegram API 报错时，自动降级为直接发送无引用消息。
  2. **抖音图集/图文笔记支持与下载流优化**:
     - `app/douyin_hd.py` 增加 `extract_images` 与 `extract_music`，在 `resolve_douyin_video` 中识别图集类型并返回 `type: 'images'`。
     - `app/ytdl.py` 增加 `__douyin_unique_dest_dir` 与 `__douyin_images_download_worker`，自动在目标目录下创建独立图集文件夹，批量下载无水印图片并拉取背景音乐，实时汇报进度。
     - 优化视频直连下载 `download_stream`：支持 HTTP `Range: bytes={existing}-`（HTTP 206）断点续传；写磁盘操作由直接调用改为 256KB 批量并经由 `asyncio.to_thread` 在线程池中异步写入，避免在 NAS 机械硬盘或网络共享盘上阻塞 asyncio 事件循环。
  3. **NAS 存储与 I/O 调优**:
     - `app/main.py` 注册后台周期性任务 `_cleanup_stale_temp_files`（每 6 小时运行一次），自动清理 `TEMP_DIR` 与 `DOWNLOAD_DIR` 中超过 24 小时未修改的 `.part`/`.ytdl`/`.temp` 残留临时文件。
     - `app/ytdl.py` 的 `Download.start()` 中增加 `DOWNLOAD_TIMEOUT` 超时熔断机制（默认 3600 秒），防止 yt-dlp 或 ffmpeg 偶发死锁进程长期霸占下载并发槽位。
  4. **Web 前端已完成列表分类、搜索与分页**:
     - `ui/src/app/app.ts` 与 `app.html` 增强已完成列表：增加平台分类筛选按钮组（全部 / 抖音 / YouTube / B站 / TikTok / Instagram / 其他）并带数量徽章。
     - 增加实时搜索输入框，支持根据视频标题、博主名或文件名进行多字段模糊过滤，带一键清空按钮。
     - 增加客户端分页与分页大小选择（10 / 20 / 50 / 100 / 全部），支持首页/上一页/页码/下一页/末页导航，且全选复选框在当前分页上平滑生效。
  5. **备份机制与长期记忆规范**:
     - 将用户规定的「代码修改前备份与变更记录规范」固化写入 `AGENTS.md`。
     - 本次修改前已将所有涉及的原始文件完整快照备份至 `.backup/`，并在 `.gitignore` 中忽略备份目录。

---

### 2026-09-20 - 修复前端严格模板检查与 CI/Docker 构建编译错误
- **修改类型**: fix
- **涉及文件**:
  - `ui/src/app/app.ts`
  - `ui/src/app/app.html`
- **详细说明**:
  1. **原因**: GitHub Actions 在 Docker 构建前端静态资源阶段（`pnpm run build`）报错失败。经排查，`ui/tsconfig.json` 开启了 `"noPropertyAccessFromIndexSignature": true` 与 `"strictTemplates": true` 严格检查。
  2. **解决**:
     - 在 `app.ts` 中新增强类型接口 `PlatformCounts` 与 `PlatformKey`，将 `donePlatformCounts` 从 `Record<string, number>` 索引签名类型改为明确命名的强类型接口，消除 Angular 模板点访问（`.all`、`.douyin` 等）触发的 TS4111 严格报错。
     - 将 `app.html` 分页控制栏中的内联复杂三元运算抽取为 `App` 组件的计算属性 `doneStartIndex` 与 `doneEndIndex`，避免在 HTML 模板插值表达式中解析比较运算符引发的语法歧义。
     - 规范搜索输入框的 `[ngModel]` 与 `(ngModelChange)` 传参，确保响应即时且类型安全。
