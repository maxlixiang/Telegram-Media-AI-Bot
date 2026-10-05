# 使用官方轻量级 Python 镜像作为基础
FROM python:3.10-slim-bookworm

# 设定时区，并安装媒体处理依赖
RUN apt-get update && apt-get install -y \
    ffmpeg \
    tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装完整 Chromium 和 Linux 依赖，使用新无头模式，无需系统 Chrome。
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps --no-shell chromium

# 复制项目中的所有代码到容器内
COPY . .

# 启动机器人
CMD ["python", "main.py"]
