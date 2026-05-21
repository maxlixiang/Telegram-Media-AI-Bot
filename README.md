

# 视频下载机器人：各平台下载路径总览

## 1. 项目当前总体架构

这是一个基于 Telegram 的多平台视频下载机器人，当前采用的是：

**通用下载器 + 平台专用下载器混合架构**

核心原则：

- 能稳定走通用方案的平台，继续使用通用下载器
- 通用方案不稳定的平台，拆分为专用 downloader
- 所有平台下载完成后，统一复用同一套后处理链路：
  - 发送视频
  - 提取纯音频
  - 转写文稿

---

## 2. 用户交互总流程

### 2.1 用户输入
用户向 Telegram 机器人发送：

- 标准视频链接
- 或平台分享文本（例如抖音分享文案）

### 2.2 URL 提取
由 `main.py` 负责：

- 从消息文本中提取目标 URL
- 判断 URL 所属平台
- 生成内部 `url_id`
- 存入 `context.user_data`

### 2.3 按钮菜单
机器人返回统一操作菜单：

- 下载高清视频
- 提取纯音频
- 提取音频 + 智能文稿

### 2.4 用户点击按钮后
进入统一下载入口：

- `downloader.py`
- `sync_download(...)`

由下载调度器判断平台，路由到对应下载路径。

---

## 3. 各平台下载路径总览

## 3.1 YouTube

### 下载路径
- 使用 `yt-dlp`

### 说明
- 属于默认通用下载分支
- 不使用专用 downloader
- 由 `downloader.py` 中统一逻辑处理

### 输出
- 视频模式：输出 `.mp4`
- 音频/文稿模式：先下载视频，再提取 `.mp3`

### 当前状态
- 可正常下载
- 可正常发送到 Telegram
- 可提取音频
- 可生成智能文稿

---

## 3.2 Bilibili

### 下载路径
- 使用 `yt-dlp`

### 说明
- 属于默认通用下载分支
- 不使用专用 downloader
- 当前稳定性较好

### 输出
- 视频模式：输出 `.mp4`
- 音频/文稿模式：输出 `.mp3`

### 当前状态
- 可正常下载
- 可正常发送到 Telegram
- 可提取音频
- 可生成智能文稿

---

## 3.3 抖音 / Douyin

### 下载路径
- 使用专用 downloader
- 不再依赖 `yt-dlp` 处理抖音下载

### 关键文件
- `douyin_downloader.py`
- `douyin_a_bogus.py`
- `douyin_a_bogus.js`

### 为什么改成专用分支
因为 `yt-dlp` 的 Douyin extractor 经常出现：

- `Fresh cookies are needed`
- 即使更新 cookies 也不稳定

所以最终改为专用下载逻辑。

### 专用分支核心流程
1. 从消息文本中提取抖音 URL
2. 识别抖音域名：
   - `douyin.com`
   - `www.douyin.com`
   - `v.douyin.com`
   - `iesdouyin.com`
3. 处理分享短链跳转
4. 提取 `aweme_id`
5. 读取 `/app/cookies.txt`
6. 将 Netscape cookies 解析为请求头 Cookie 字符串
7. 生成 `msToken`
8. 调用 Node.js + JS 脚本生成 `a_bogus`
9. 请求抖音 detail 接口
10. 获取视频直链
11. 流式下载本地 `.mp4`

### 输出
- 视频模式：返回 `.mp4`
- 音频/文稿模式：
  - 先下载 `.mp4`
  - 再用 `ffmpeg` 提取 `.mp3`
  - 返回 `.mp3`

### 依赖
- `cookies.txt`
- `Node.js`
- `jsdom`
- `ffmpeg`

### 当前状态
- 已稳定可用
- 支持抖音分享文本
- 支持视频下载
- 支持音频提取
- 支持智能文稿

---

## 3.4 X / Twitter

### 下载路径
- 使用专用 downloader
- 不再依赖 `yt-dlp`
- 不再使用旧版 metadata / variants 解析方案

### 关键文件
- `twitter_downloader.py`

### 当前实现方案
将 tweet/status 链接直接转换为：

`https://fxtwitter.com/i/status/<tweet_id>.mp4`

然后直接下载 mp4。

### 为什么改成这条路径
之前尝试过：

- `yt-dlp`
- Twitter metadata / variants 解析

但稳定性不理想。

最终切换为更简单的固定直链方案：

- 提取 `tweet_id`
- 生成 `fxtwitter mp4` 链接
- 流式下载

### 专用分支核心流程
1. 判断是否为 Twitter/X 链接
2. 提取 `tweet_id`
3. 生成：
   `https://fxtwitter.com/i/status/<tweet_id>.mp4`
4. 使用 `httpx` 流式下载本地 `.mp4`
5. 下载成功后返回本地文件

### 输出
- 视频模式：返回 `.mp4`
- 音频/文稿模式：
  - 从 `.mp4` 提取 `.mp3`
  - 返回 `.mp3`

### 当前支持范围
- 公开单视频 tweet

### 当前不支持
- 受限 tweet
- 年龄限制内容
- 多视频
- Space / Live
- 复杂媒体组合

### 当前状态
- 小视频和普通公开视频已验证可正常下载
- 受限内容会明确报错
- 稳定性已明显优于之前方案

---

## 3.5 其他平台

### 下载路径
- 默认走 `yt-dlp`

### 说明
- 如果平台未命中抖音或 Twitter 专用分支
- 则进入通用下载逻辑

### 是否可用
- 取决于 `yt-dlp` 对该平台的支持情况

---

## 4. 下载调度逻辑

统一入口：

- `downloader.py`
- `sync_download(...)`

平台路由策略：

1. 如果是 Douyin URL
   - 走 `download_douyin_media(...)`

2. 如果是 Twitter/X URL
   - 走 `twitter_downloader.py` 专用逻辑

3. 否则
   - 走 `yt-dlp` 通用逻辑

---

## 5. 下载后统一后处理链路

无论哪个平台，下载完成后都回到统一后处理流程。

## 5.1 视频模式
- 直接发送视频到 Telegram

## 5.2 提取纯音频
- 使用 `ffmpeg`
- 从视频提取 `.mp3`
- 发送音频文件

## 5.3 提取音频 + 智能文稿
1. 使用 `ffmpeg` 提取 `.mp3`
2. 调用 Whisper / Groq 做转写
3. 调用 LLM 整理文稿
4. 发送音频和文稿文件

---

## 6. Telegram 发送层

## 6.1 生产环境
当前生产环境使用本地 Telegram Bot API：

- `telegram-bot-api`
- 地址：`http://127.0.0.1:8081/bot`

`main.py` 使用：

- `base_url(...)`
- `local_mode(True)`

### 作用
- 提高发送大文件时的稳定性
- 避免直接使用官方 Bot API 的限制

## 6.2 本地 Docker 测试环境
本地调试时一般直接使用官方 Telegram API：

- 不使用 `127.0.0.1:8081`
- 否则容器内会访问不到宿主机本地地址

---

## 7. 当前部署依赖

## 7.1 Python 依赖
- `python-telegram-bot`
- `yt-dlp`
- `httpx`
- 其他项目原有依赖

## 7.2 系统依赖
- `ffmpeg`

## 7.3 抖音专用额外依赖
- `Node.js`
- `npm`
- `jsdom`

## 7.4 外部文件
- `.env`
- `cookies.txt`

---

## 8. 当前行为边界总结

## 8.1 稳定支持的平台
- YouTube
- Bilibili
- Douyin
- Twitter/X（公开单视频）

## 8.2 明确不支持或有限支持
- 未加入tiktok的支持，其他大部分视频都能下载

---

## 9. 当前项目的最终架构总结

### 通用分支
- YouTube → `yt-dlp`
- Bilibili → `yt-dlp`
- 其他常规站点 → `yt-dlp`

### 专用分支
- Douyin → 自定义 downloader + `a_bogus` + cookies + detail API
- Twitter/X → `fxtwitter` 固定 mp4 直链下载

### 统一后处理
- 视频发送
- 音频提取
- 智能文稿生成

---

## 10. 一句话总结

当前这个视频下载机器人已经演化成：

**“yt-dlp 通用下载 + 平台专用 downloader + 统一音频/文稿后处理”的混合架构。**

其中：

- YouTube / Bilibili 继续依赖 `yt-dlp`
- Douyin 已切换为专用下载路径
- Twitter/X 已切换为专用下载路径
- 所有平台下载完成后，共用同一套音频提取与智能文稿链路