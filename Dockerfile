# 使用官方轻量级 Python 镜像作为基础
FROM python:3.10-slim

# 设定时区，并安装关键系统依赖 ffmpeg 与 Node.js
RUN apt-get update && apt-get install -y \
    ffmpeg \
    nodejs \
    npm \
    tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目中的所有代码到容器内
COPY . .

# 安装抖音签名脚本依赖
RUN npm install jsdom

# 启动机器人
CMD ["python", "main.py"]
