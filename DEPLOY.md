# Telegram Media AI Bot 部署说明

本文记录将本项目部署或迁移到新 VPS 的步骤。项目仓库代码可以从 GitHub 拉取，但 `.env` 和 `cookies.txt` 不会提交到仓库，需要手动迁移。

## 1. 需要准备的文件

必须放在项目根目录：

```text
.env
cookies.txt
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

`cookies.txt` 是抖音下载使用的 Netscape cookies 文件。`docker-compose.yml` 会把它挂载到容器内：

```text
/app/cookies.txt
```

本项目没有 SQLite 数据库，也没有必须迁移的长期下载目录。媒体和文稿文件会临时写入宿主机 `/var/lib/telegram-bot-api`（容器内路径相同）。每个任务结束后，程序会清理该任务的成品、`.part` 文件和音视频合并中间文件；启动后还会定时清理超过保留期的 `media_<UUID>*` 孤儿文件。清理器只匹配这一严格命名规则，不会删除 Telegram Bot API 自身的数据目录。

## 2. 拉取代码

在新 VPS 上执行：

```bash
git clone https://github.com/maxlixiang/Telegram-Media-AI-Bot.git /root/Telegram-Media-AI-Bot
cd /root/Telegram-Media-AI-Bot
```

把 `.env` 和 `cookies.txt` 上传到 `/root/Telegram-Media-AI-Bot`。

检查文件：

```bash
ls -lah .env cookies.txt docker-compose.yml Dockerfile
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
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  aiogram/telegram-bot-api:latest \
  --dir=/var/lib/telegram-bot-api \
  --temp-dir=/tmp/telegram-bot-api \
  --http-port=8081 \
  --local
```

检查服务：

```bash
docker ps | grep telegram-bot-api
docker logs --tail 50 telegram-bot-api
curl http://127.0.0.1:8081
```

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
cookies.txt
```

新 VPS 额外需要重新准备：

```text
/root/telegram-bot-api.env
/var/lib/telegram-bot-api
telegram-bot-api Docker 容器
```

项目代码建议直接从 GitHub 重新 clone。
