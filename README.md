# Telegram Media AI Bot

通过 Telegram 接收视频链接，提供视频下载、MP3 音频提取，以及音频 + AI 文稿三种处理方式。用户通过 `ALLOWED_USERS` 白名单控制。

## 用户流程

发送视频链接或分享文本 → 提取第一个有效链接 → 选择操作按钮 → 下载媒体 → 发送 Telegram → 如需文稿则执行 Whisper 转写和 Llama 整理 → 清理任务文件。

文稿为 UTF-8 TXT，包含智能整理版和原始转写版。AI 接口使用 Groq，模型为 `whisper-large-v3` 和 `llama-3.3-70b-versatile`。

## 平台下载路径

| 平台 | 当前实现 |
| --- | --- |
| YouTube / Bilibili / 其他站点 | yt-dlp；可用性取决于平台支持 |
| 抖音 | Playwright 完整 Chromium，新无头模式，全新匿名会话，Windows UA |
| X / Twitter | fxtwitter 固定 MP4 直链，requests 流式下载 |

通用分支音频模式优先下载音轨，再转 MP3；抖音和 X 先下载视频，再通过 FFmpeg 提取 MP3。高清视频优先使用平台可提供的较好格式，未提供分辨率菜单。

## 抖音匿名浏览器方案（2026-10-05）

1. 启动 Playwright 自带的完整 Chromium（`channel="chromium"`，`headless=True`）。
2. 新建隔离的匿名浏览器上下文，使用 Windows 桌面 UA、中文 locale 和上海时区。
3. 在导航前监听抖音 JSON 网络响应，导航只等待响应开始（commit），不等待 DOMContentLoaded。
4. 屏蔽图片、字体和媒体播放请求，保留脚本和数据接口；取得视频信息后立即退出浏览器。
5. 通过作品 ID 匹配目标视频，避免误下载推荐视频。
6. 提取播放地址及备用地址，关闭浏览器后流式下载 MP4。
7. 使用此次匿名会话产生的必要 Cookie，保留域名和路径限制，不读取用户登录 Cookie。
8. 检查 MP4 文件头与已知内容长度；候选地址失败时尝试备用地址。

不再依赖 `cookies.txt`、Node.js、jsdom 或手工生成 `a_bogus`。旧签名文件保留在仓库中，但不再被主流程调用。

浏览器解析串行执行，避免并发启动多个浏览器占用 VPS 内存。每个任务结束后关闭浏览器，不保存登录状态。解析阶段采用异步总超时，默认 60 秒（浏览器启动最多额外等待 15 秒）。页面数据每隔约 5 秒读取一次，每次最多等待 2 秒；只传回目标作品，限制缓存和普通 JSON 读取并发，优先处理视频详情响应。可在 `.env` 配置：

```env
DOUYIN_BROWSER_TIMEOUT_SECONDS=60
```

匿名会话可能自动生成 Cookie，因此“免登录”不代表网络请求完全没有 Cookie。服务器出口网络仍可能触发验证码；机器人会提示验证码、解析超时或媒体下载失败，不自动绕过验证码。图文、私密视频和登录后才能访问的内容不在当前视频下载支持范围内。

## Telegram 大文件发送与清理

启动代码连接本地 Bot API：`http://127.0.0.1:8081/bot`，启用 `local_mode(True)`。媒体通过 `/var/lib/telegram-bot-api` 共享目录的绝对路径发送，避免重复上传到本地 API。Bot API 服务必须通过 `TELEGRAM_LOCAL=1` 真正启用 local 模式。

- 上传默认最长等待 30 分钟，每 30 秒显示大小、活动条和等待时间；活动条不是上传百分比。
- 部分视频格式处理错误会回退为普通文件发送。
- 发送失败提供诊断编号，任务文件暂存供排查。
- 完成后清理 `media_<UUID>*` 文件，定期清理过期残留；严格匹配命名规则，不清理 Bot API 工作目录。
- 默认残留保留 24 小时，每 60 分钟扫描。Docker 日志最多保留 3 个 10 MB 文件。

```env
TELEGRAM_UPLOAD_TIMEOUT_SECONDS=1800
MEDIA_MAX_AGE_HOURS=24
CLEANUP_INTERVAL_MINUTES=60
```

## 部署与验证

详见 [DEPLOY.md](DEPLOY.md)。Docker 构建自动安装 Chromium 和 Linux 系统依赖，VPS 无需预装 Chrome 或桌面。首次构建需要下载浏览器，因此耗时和镜像体积会增加。

Docker Compose 使用主机网络、共享媒体目录、`init: true` 和 512 MB 共享内存。GitHub Actions 在推送 `main` 后自动连接 VPS，拉取代码并重新构建部署。

仅需迁移 `.env`，不再需要抖音 Cookie 文件。`.dockerignore` 排除密钥、本地测试目录和媒体文件。

本地单元测试（无需登录或联网）：

```bash
python -B -m unittest discover -s tests -v
```

VPS 上独立验证匿名解析（不启动第二个 Telegram 机器人）：

```bash
docker compose exec tg_media_bot python -c 'from douyin_downloader import fetch_douyin_video_meta; m=fetch_douyin_video_meta("https://v.douyin.com/usslc6OROu8/"); print(m["aweme_id"], len(m["video_urls"]))'
```

也可直接向机器人发送同一链接，依次验证视频、音频和文稿。不要把住宅网络下的成功当作所有 VPS 出口都能成功的保证。

## 主要模块

- `main.py`：消息和按钮交互、状态提示、媒体发送、AI 流程与清理调度。
- `downloader.py`：平台分流、yt-dlp 通用下载、FFmpeg 音频提取。
- `douyin_downloader.py`：匿名浏览器解析、目标匹配、备用地址下载和文件校验。
- `twitter_downloader.py`：X / Twitter 下载。
- `ai_services.py`：Whisper 转写和 Llama 排版。
- `cleanup.py`：任务文件与过期残留清理。
- `config.py`：环境变量和运行参数。
- `twitter_probe.py`：独立诊断脚本，未接入主下载流程。

## VPS 解析诊断（2026-10-05）

失败时机器人提示诊断代码，容器日志记录“抖音视频请求”“抖音视频响应”和“抖音解析诊断”，包括作品 ID、响应状态、页面 readyState、JSON 数量、请求失败及页面读取超时次数。无需额外启动诊断浏览器。

| 代码 | 观察到的情况 |
| --- | --- |
| CAPTCHA_REQUIRED | 页面检测到验证提示 |
| VIDEO_HTTP_ERROR | 视频相关接口出现 HTTP 4xx/5xx |
| VIDEO_REQUEST_FAILED | 视频相关请求出现浏览器网络错误 |
| SCRIPT_LOAD_FAILED | 页面脚本请求失败或返回 HTTP 4xx/5xx |
| PAGE_UNRESPONSIVE | 多次读取页面数据超时 |
| NO_VIDEO_REQUEST | 超时前未观察到当前识别范围内的视频接口请求 |
| VIDEO_DATA_MISSING | 观察到了视频请求，但未匹配到有效视频数据 |

代码是诊断线索，不等同于确认 IP 风控或内存不足。`invalid/ ERR_BLOCKED_BY_CLIENT` 单独计数，本机成功时也可能出现。主动拦截的图片、媒体和字体不计入网络失败。日志只记录请求域名和路径，不记录签名查询参数或 Cookie；httpx 的 INFO 请求日志关闭，避免输出 Bot Token。

```bash
docker logs --since 10m video_downloader_bot 2>&1 | tail -n 120
```
