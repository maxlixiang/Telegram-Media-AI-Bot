import os
import uuid
import asyncio
import logging
import time
import re
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from config import TOKEN, ALLOWED_USERS, SHARED_DIR
from downloader import sync_download
from ai_services import process_groq_transcription, format_text_with_llm

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

URL_PATTERN = re.compile(r'https?://[^\s<>"\'\u3000，。！？；：、（）【】《》]+', re.IGNORECASE)
URL_TRAILING_NOISE = '.,!?;:)]}>\'"，。！？；：、）】》'
DOUYIN_HOSTS = {"douyin.com", "v.douyin.com", "iesdouyin.com"}
TELEGRAM_SEND_TIMEOUTS = {
    "connect_timeout": 120,
    "read_timeout": 600,
    "write_timeout": 600,
    "pool_timeout": 120,
}


def extract_target_url(text: str) -> str | None:
    if not text:
        return None

    for match in URL_PATTERN.finditer(text):
        candidate = match.group(0).rstrip(URL_TRAILING_NOISE)
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return candidate
    return None


def is_douyin_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in DOUYIN_HOSTS


def format_file_size(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def build_send_failure_message(error: Exception) -> str:
    error_text = str(error)
    if isinstance(error, TimedOut) or "ConnectTimeout" in error_text or "TimedOut" in error_text:
        return "发送失败：Telegram 上传超时，请稍后重试"
    if isinstance(error, BadRequest):
        if "Request Entity Too Large" in error_text or "file is too big" in error_text.lower():
            return "发送失败：文件过大，无法发送到 Telegram"
        return f"发送失败：Telegram 返回异常 ({error_text})"
    if isinstance(error, RetryAfter):
        return f"发送失败：Telegram 限流，请稍后重试 ({error_text})"
    if isinstance(error, NetworkError):
        return f"发送失败：Telegram 网络异常 ({error_text})"
    if isinstance(error, FileNotFoundError):
        return "发送失败：文件不存在或路径错误"
    return f"发送失败：未知 Telegram 异常 ({error.__class__.__name__})"


async def send_media_file(context, status_msg, final_file: str, is_audio: bool, original_msg_id: int):
    send_method_name = "send_audio" if is_audio else "send_video"
    file_exists = os.path.exists(final_file)
    file_size = os.path.getsize(final_file) if file_exists else -1
    logging.info(
        "Telegram 发送开始 file_path=%s file_exists=%s file_size_bytes=%s file_size_mb=%s send_method=%s retry_attempted=%s",
        final_file,
        file_exists,
        file_size,
        format_file_size(file_size) if file_size >= 0 else "N/A",
        send_method_name,
        False,
    )

    if not file_exists:
        raise FileNotFoundError(final_file)

    try:
        with open(final_file, "rb") as media_file:
            if is_audio:
                await context.bot.send_audio(
                    chat_id=status_msg.chat_id,
                    audio=media_file,
                    reply_to_message_id=original_msg_id,
                    **TELEGRAM_SEND_TIMEOUTS,
                )
            else:
                await context.bot.send_video(
                    chat_id=status_msg.chat_id,
                    video=media_file,
                    supports_streaming=True,
                    reply_to_message_id=original_msg_id,
                    **TELEGRAM_SEND_TIMEOUTS,
                )
        logging.info(
            "Telegram 发送成功 file_path=%s file_size_bytes=%s send_method=%s",
            final_file,
            file_size,
            send_method_name,
        )
    except Exception as exc:
        logging.error(
            "Telegram 发送失败 file_path=%s file_size_bytes=%s send_method=%s exception_type=%s exception=%s retry_attempted=%s",
            final_file,
            file_size,
            send_method_name,
            exc.__class__.__name__,
            exc,
            False,
        )
        raise


async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text(f"🛑 无权限。您的 User ID 是：`{user_id}`")
        return

    text = update.message.text or ""
    url = extract_target_url(text)

    if not url:
        await update.message.reply_text("未识别到有效链接，请发送标准链接或包含抖音链接的分享文本。", reply_to_message_id=update.message.message_id)
        return

    url_id = str(uuid.uuid4())[:8]
    context.user_data[url_id] = url

    keyboard = [
        [InlineKeyboardButton("🎬 下载高清视频", callback_data=f"video|{url_id}")],
        [InlineKeyboardButton("🎵 提取纯音频", callback_data=f"audio|{url_id}")],
        [InlineKeyboardButton("📝 提取音频 + 智能双擎文稿", callback_data=f"transcript|{url_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("请选择您需要的处理方式：👇", reply_markup=reply_markup, reply_to_message_id=update.message.message_id)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    action, url_id = data.split('|')
    url = context.user_data.get(url_id)
    
    if not url:
        await query.edit_message_text("❌ 链接已过期或失效。")
        return
        
    is_audio = (action in ["audio", "transcript"])
    need_transcript = (action == "transcript")
    mode_name = "纯音频 (MP3)" if is_audio else "高清视频"
    if need_transcript: mode_name = "音频及 AI 润色文稿"
        
    status_msg = query.message
    await status_msg.edit_text(f"开始为您解析{mode_name}... 🔎")
    
    os.makedirs(SHARED_DIR, exist_ok=True)
    filepath_base = os.path.join(SHARED_DIR, f"media_{uuid.uuid4().hex}")
    final_file = ""
    txt_filepath = "" 
    context.user_data['last_update'] = 0
    loop = asyncio.get_event_loop()

    try:
        final_file = await asyncio.to_thread(sync_download, url, filepath_base, status_msg, loop, context, is_audio)
        if not os.path.exists(final_file):
            await status_msg.edit_text("下载失败❌")
            return

        await status_msg.edit_text(f"媒体获取完成！正在发送至 Telegram... 🚀")
        original_msg_id = status_msg.reply_to_message.message_id if status_msg.reply_to_message else status_msg.message_id

        try:
            await send_media_file(context, status_msg, final_file, is_audio, original_msg_id)
        except Exception as send_error:
            await status_msg.edit_text(build_send_failure_message(send_error))
            return
            
        if need_transcript:
            await status_msg.edit_text("🎵 正在唤醒 Whisper 引擎极速听写，请稍候... 🧠✨")
            original_text = await process_groq_transcription(final_file)
            
            await status_msg.edit_text("📝 听写完成！正在交由 Llama 大模型进行智能断句、润色排版与表情包插入... ⏳")
            formatted_text = await format_text_with_llm(original_text)
            
            combined_content = f"【✨ 智能整理排版版】\n====================\n\n{formatted_text}\n\n\n【🎙️ 原始语音听写版】\n====================\n\n{original_text}"
            clean_filename = f"精排文稿_{str(time.time()).replace('.','')[-4:]}.txt"
            txt_filepath = filepath_base + ".txt"
            
            with open(txt_filepath, "w", encoding="utf-8") as f:
                f.write(combined_content)
            
            txt_file_exists = os.path.exists(txt_filepath)
            txt_file_size = os.path.getsize(txt_filepath) if txt_file_exists else -1
            logging.info(
                "Telegram 文稿发送开始 file_path=%s file_exists=%s file_size_bytes=%s file_size_mb=%s send_method=%s retry_attempted=%s",
                txt_filepath,
                txt_file_exists,
                txt_file_size,
                format_file_size(txt_file_size) if txt_file_size >= 0 else "N/A",
                "send_document",
                False,
            )
            try:
                with open(txt_filepath, "rb") as document_file:
                    await context.bot.send_document(
                        chat_id=status_msg.chat_id,
                        document=document_file,
                        filename=clean_filename,
                        caption="🎉 **排版完成！请查收附件。**",
                        parse_mode="Markdown",
                        reply_to_message_id=original_msg_id,
                        **TELEGRAM_SEND_TIMEOUTS,
                    )
                logging.info(
                    "Telegram 文稿发送成功 file_path=%s file_size_bytes=%s send_method=%s",
                    txt_filepath,
                    txt_file_size,
                    "send_document",
                )
            except Exception as doc_error:
                logging.error(
                    "Telegram 文稿发送失败 file_path=%s file_size_bytes=%s send_method=%s exception_type=%s exception=%s retry_attempted=%s",
                    txt_filepath,
                    txt_file_size,
                    "send_document",
                    doc_error.__class__.__name__,
                    doc_error,
                    False,
                )
                await status_msg.edit_text(build_send_failure_message(doc_error))
                return
        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"发生了一些意外错误😵")
        logging.error(f"全局错误: {e}")
    finally:
        if final_file and os.path.exists(final_file): os.remove(final_file)
        if txt_filepath and os.path.exists(txt_filepath): os.remove(txt_filepath)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).base_url("http://127.0.0.1:8081/bot").local_mode(True).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), receive_link))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("🤖 模块化双引擎机器人已启动...")
    app.run_polling()
