import os
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
# 将字符串形式的 ID 转换为整数列表
ALLOWED_USERS = [int(id.strip()) for id in os.getenv("ALLOWED_USERS", "").split(",") if id.strip()]
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_BASE_URL = 'https://api.groq.com/openai/v1'
SHARED_DIR = "/var/lib/telegram-bot-api"
MEDIA_MAX_AGE_HOURS = max(1, int(os.getenv("MEDIA_MAX_AGE_HOURS", "24")))
CLEANUP_INTERVAL_MINUTES = max(1, int(os.getenv("CLEANUP_INTERVAL_MINUTES", "60")))
TELEGRAM_UPLOAD_TIMEOUT_SECONDS = max(600, int(os.getenv("TELEGRAM_UPLOAD_TIMEOUT_SECONDS", "1800")))
