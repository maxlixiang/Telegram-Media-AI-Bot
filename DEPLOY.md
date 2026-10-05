# Telegram Media AI Bot 部署说明

本文记录将本项目部署或迁移到新 VPS 的步骤。项目仓库代码可以从 GitHub 拉取，但 `.env` 不会提交到仓库，需要手动迁移。抖音改为匿名 Chromium 解析，不再需要 cookies.txt。

## 1. 需要准备的文件

必须放在项目根目录：

```text
.env
```

`.env` 至少包含：

```env
BOT_TOKEN=你的 Telegram Bot Token
ALLOWED_USERS=允许使用机器人的 Telegram 用户 ID，多个用英文逗号分隔
GROQ_API_KEY=你的 Groq API Key
```

可选的自动清理配置（不填写时使用默认值）：

```env
# 下载残留文件最多保留 24 小时
MEDIA_MAX_AGE_HOURS=24
# 每 60 分钟扫描一次残留文件
CLEANUP_INTERVAL_MINUTES=60
# 大文件上传最多等待 30 分钟
TELEGRAM_UPLOAD_TIMEOUT_SECONDS=1800
```

抖音解析使用 Playwright 自带的完整 Chromium、新无头模式、Windows UA 和全新匿名会话。Docker 构建自动安装浏览器与 Linux 依赖；无需系统 Chrome、桌面或登录 Cookie。首次构建需要访问 Playwright 下载源。

可选配置 `DOUYIN_BROWSER_TIMEOUT_SECONDS=60`，控制解析等待时间。若触发验证码，先查看机器人提示和容器日志，确认服务器出口网络是否允许匿名访问。

本项目没有 SQLite 数据库，也没有必须迁移的长期下载目录。媒体和文稿文件会临时写入宿主机 `/var/lib/telegram-bot-api`（容器内路径相同）。每个任务结束后，程序会清理该任务的成品、`.part` 文件和音视频合并中间文件；启动后还会定时清理超过保留期的 `media_<UUID>*` 孤儿文件。清理器只匹配这一严格命名规则，不会删除 Telegram Bot API 自身的数据目录。

## 2. 拉取代码

在新 VPS 上执行：

```bash
git clone https://github.com/maxlixiang/Telegram-Media-AI-Bot.git /root/Telegram-Media-AI-Bot
cd /root/Telegram-Media-AI-Bot
```

把 `.env` 上传到 `/root/Telegram-Media-AI-Bot`。

检查文件：

```bash
ls -lah .env docker-compose.yml Dockerfile
```

## 3. 部署本地 Telegram Bot API

本项目生产环境使用本地 Telegram Bot API：

```text
http://127.0.0.1:8081/bot
```

`main.py` 使用：

```python
ApplicationBuilder().token(TOKEN).base_url("http://127.0.0.1:8081/bot").local_mode(True)
```

所以必须先启动本地 `telegram-bot-api` 服务，否则机器人会报：

```text
telegram.error.NetworkError: httpx.ConnectError: All connection attempts failed
```

### 3.1 准备 Telegram API ID/Hash

`TELEGRAM_API_ID` 和 `TELEGRAM_API_HASH` 来自：

```text
https://my.telegram.org
```

注意：

- `TELEGRAM_API_ID` 是纯数字。
- `TELEGRAM_API_HASH` 通常是 32 位十六进制字符串，只包含 `0-9` 和 `a-f`。
- 它们不是 `BOT_TOKEN`。

建议用 env 文件保存，避免命令行粘贴时把 `-`、`—` 或空格混进去。

创建配置文件：

```bash
nano /root/telegram-bot-api.env
```

内容示例：

```env
TELEGRAM_API_ID=你的纯数字API_ID
TELEGRAM_API_HASH=你的32位API_HASH
```

保存后退出。

### 3.2 启动 telegram-bot-api 容器

```bash
mkdir -p /var/lib/telegram-bot-api

docker run -d \
  --name telegram-bot-api \
  --restart unless-stopped \
  -p 8081:8081 \
  -v /var/lib/telegram-bot-api:/var/lib/telegram-bot-api \
  --env-file /root/telegram-bot-api.env \
  -e TELEGRAM_LOCAL=1 \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  aiogram/telegram-bot-api:latest
```

注意：`aiogram/telegram-bot-api` 镜像通过环境变量生成启动参数。不要把 `--local` 写在镜像名后面；该镜像的入口脚本不会使用这些额外参数。必须设置 `TELEGRAM_LOCAL=1`，否则大文件本地路径会被当作不支持的 URL。

检查服务：

```bash
docker ps | grep telegram-bot-api
docker logs --tail 50 telegram-bot-api
docker top telegram-bot-api -eo pid,args
curl http://127.0.0.1:8081
```

`docker logs` 或 `docker top` 显示的实际进程参数中必须包含 `--local`。

`curl` 返回 `404` 或类似响应可以接受，只要不是：

```text
Couldn't connect to server
```

更准确的测试方式：

```bash
TOKEN=$(grep '^BOT_TOKEN=' /root/Telegram-Media-AI-Bot/.env | cut -d= -f2-)
curl "http://127.0.0.1:8081/bot${TOKEN}/getMe"
```

如果返回 JSON，说明本地 Telegram Bot API 正常。

## 4. 启动媒体机器人

进入项目目录：

```bash
cd /root/Telegram-Media-AI-Bot
```

启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker logs --tail 100 video_downloader_bot
```

正常情况下会看到：

```text
模块化双引擎机器人已启动
```

## 5. 常见问题

### 5.1 磁盘空间排查与清理

先判断空间被哪一类数据占用：

```bash
df -h
sudo du -xh --max-depth=1 /var/lib/telegram-bot-api 2>/dev/null | sort -h
docker system df
sudo du -sh /var/lib/docker/containers/* 2>/dev/null | sort -h | tail
```

查看本项目遗留的媒体文件（不会删除）：

```bash
sudo find /var/lib/telegram-bot-api -maxdepth 1 -type f -name 'media_*' -printf '%TY-%Tm-%Td %TH:%TM %10s %p\n' | sort
```

部署新版代码并重启机器人后，超过 `MEDIA_MAX_AGE_HOURS` 的遗留媒体会在启动时自动清理。若需要尽快清理，可以临时在 `.env` 中设置 `MEDIA_MAX_AGE_HOURS=1`，重启并确认日志出现“自动清理临时媒体完成”，之后再改回 24。

Docker Compose 已为媒体机器人设置日志轮转。对于以前创建、没有日志上限的 `telegram-bot-api` 容器，需要按第 3.2 节参数重建；`--log-opt` 只在创建容器时生效。不要直接删除 `/var/lib/telegram-bot-api` 整个目录，其中还包含本地 Telegram Bot API 的工作数据。

### 5.2 8081 连接失败

现象：

```bash
curl http://127.0.0.1:8081
```

返回：

```text
Couldn't connect to server
```

原因：`telegram-bot-api` 容器没有成功运行。

检查：

```bash
docker ps | grep telegram-bot-api
docker logs --tail 50 telegram-bot-api
```

### 5.3 API ID/Hash 无效

日志：

```text
telegram-bot-api: You must provide valid api-id and api-hash
```

原因通常是：

- `TELEGRAM_API_ID` 不是纯数字。
- `TELEGRAM_API_HASH` 粘贴错了。
- `TELEGRAM_API_HASH` 里混入了长横线 `—`、普通横线 `-`、空格或其他不可见字符。

处理：

```bash
docker rm -f telegram-bot-api
nano /root/telegram-bot-api.env
```

修正后重新运行 `docker run ...`。

### 5.4 媒体 bot 连接本地 API 失败

日志：

```text
telegram.error.NetworkError: httpx.ConnectError: All connection attempts failed
```

原因：媒体 bot 启动时访问 `http://127.0.0.1:8081/bot`，但本地 Telegram Bot API 没有运行。

处理顺序：

```text
1. 先启动 telegram-bot-api 容器
2. 确认 127.0.0.1:8081 可访问
3. 再启动 video_downloader_bot
```

### 5.5 重建服务

如果需要重新部署：

```bash
cd /root/Telegram-Media-AI-Bot
docker compose down
docker compose up -d --build
docker logs --tail 100 video_downloader_bot
```

如果需要重建本地 Telegram Bot API：

```bash
docker rm -f telegram-bot-api
```

然后重新执行第 3.2 节的 `docker run` 命令。

## 6. 迁移清单

从旧服务器或本地电脑迁移到新 VPS 时，重点保存：

```text
.env
```

新 VPS 额外需要重新准备：

```text
/root/telegram-bot-api.env
/var/lib/telegram-bot-api
telegram-bot-api Docker 容器
```

项目代码建议直接从 GitHub 重新 clone。

## 7. 匿名抖音下载验证（2026-10-05）

新版不再挂载 cookies.txt。推送 main 会触发现有 GitHub Actions 自动构建部署，首次下载 Chromium 可能比旧版部署慢。

部署后查看日志，再向机器人发送公开抖音分享链接，依次测试视频、音频和文稿。成功日志包含“抖音匿名解析成功”、作品 ID 和候选地址数量；日志不输出匿名 Cookie 或完整媒体签名地址。

```bash
docker logs --tail 100 video_downloader_bot
```

若需要独立检查解析，不要启动第二个机器人轮询进程，使用：

```bash
docker compose exec tg_media_bot python -c 'from douyin_downloader import fetch_douyin_video_meta; m=fetch_douyin_video_meta("https://v.douyin.com/usslc6OROu8/"); print(m["aweme_id"], len(m["video_urls"]))'
```

Docker Compose 已设置 init 和共享内存。浏览器任务串行执行，并在每次解析后关闭；VPS 出口触发风控时仍可能出现验证码，不保证所有机房 IP 可用。
