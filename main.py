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

from config import (
    TOKEN,
    ALLOWED_USERS,
    SHARED_DIR,
    MEDIA_MAX_AGE_HOURS,
    CLEANUP_INTERVAL_MINUTES,
    TELEGRAM_UPLOAD_TIMEOUT_SECONDS,
)
from cleanup import cleanup_stale_media_files, cleanup_task_files
from downloader import sync_download
from ai_services import process_groq_transcription, format_text_with_llm

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

URL_PATTERN = re.compile(r'https?://[^\s<>"\'\u3000，。！？；：、（）【】《》]+', re.IGNORECASE)
URL_TRAILING_NOISE = '.,!?;:)]}>\'"，。！？；：、）】》'
DOUYIN_HOSTS = {"douyin.com", "www.douyin.com", "v.douyin.com", "iesdouyin.com"}
TELEGRAM_SEND_TIMEOUTS = {
    "connect_timeout": 120,
    "read_timeout": TELEGRAM_UPLOAD_TIMEOUT_SECONDS,
    "write_timeout": TELEGRAM_UPLOAD_TIMEOUT_SECONDS,
    "pool_timeout": 120,
}
UPLOAD_STATUS_INTERVAL_SECONDS = 30
UPLOAD_PROGRESS_FRAMES = (
    "■■■□□□□□□□",
    "□■■■□□□□□□",
    "□□■■■□□□□□",
    "□□□■■■□□□□",
    "□□□□■■■□□□",
    "□□□□□■■■□□",
    "□□□□□□■■■□",
    "□□□□□□□■■■",
    "□□□□□□■■■□",
    "□□□□□■■■□□",
    "□□□□■■■□□□",
    "□□□■■■□□□□",
    "□□■■■□□□□□",
    "□■■■□□□□□□",
)


async def cleanup_media_loop():
    max_age_seconds = MEDIA_MAX_AGE_HOURS * 60 * 60
    while True:
        removed_count, removed_bytes = await asyncio.to_thread(
            cleanup_stale_media_files,
            SHARED_DIR,
            max_age_seconds,
        )
        if removed_count:
            logging.info(
                "自动清理临时媒体完成 removed_count=%s freed_mb=%.2f max_age_hours=%s",
                removed_count,
                removed_bytes / (1024 * 1024),
                MEDIA_MAX_AGE_HOURS,
            )
        await asyncio.sleep(CLEANUP_INTERVAL_MINUTES * 60)


async def post_init(application):
    os.makedirs(SHARED_DIR, exist_ok=True)
    application.bot_data["cleanup_task"] = asyncio.create_task(
        cleanup_media_loop(),
        name="media-cleanup-loop",
    )


async def post_shutdown(application):
    cleanup_task = application.bot_data.get("cleanup_task")
    if cleanup_task:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


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


def format_elapsed_time(elapsed_seconds: float) -> str:
    total_seconds = max(0, int(elapsed_seconds))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{seconds} 秒"


def classify_send_error(error: Exception) -> str:
    error_text = str(error).lower()
    if isinstance(error, TimedOut) or "connecttimeout" in error_text or "timedout" in error_text:
        return "UPLOAD_TIMEOUT"
    if isinstance(error, BadRequest):
        if "request entity too large" in error_text or "file is too big" in error_text:
            return "FILE_TOO_LARGE"
        return "TELEGRAM_BAD_REQUEST"
    if isinstance(error, RetryAfter):
        return "TELEGRAM_RATE_LIMIT"
    if isinstance(error, NetworkError):
        return "TELEGRAM_NETWORK_ERROR"
    if isinstance(error, FileNotFoundError):
        return "LOCAL_FILE_NOT_FOUND"
    return "UNKNOWN_SEND_ERROR"


def build_send_failure_message(error: Exception) -> str:
    error_text = str(error)
    diagnostic_code = classify_send_error(error)
    if isinstance(error, TimedOut) or "ConnectTimeout" in error_text or "TimedOut" in error_text:
        message = "发送失败：Telegram 上传超时，请稍后重试"
    elif isinstance(error, BadRequest):
        if "Request Entity Too Large" in error_text or "file is too big" in error_text.lower():
            message = "发送失败：文件过大，无法发送到 Telegram"
        else:
            message = f"发送失败：Telegram 返回异常 ({error_text})"
    elif isinstance(error, RetryAfter):
        message = f"发送失败：Telegram 限流，请稍后重试 ({error_text})"
    elif isinstance(error, NetworkError):
        message = f"发送失败：Telegram 网络异常 ({error_text})"
    elif isinstance(error, FileNotFoundError):
        message = "发送失败：文件不存在或路径错误"
    else:
        message = f"发送失败：未知 Telegram 异常 ({error.__class__.__name__})"
    return f"{message}\n诊断编号：{diagnostic_code}"


def should_fallback_to_document(error: Exception) -> bool:
    if not isinstance(error, BadRequest):
        return False

    error_text = str(error).lower()
    excluded_errors = (
        "file is too big",
        "request entity too large",
        "chat not found",
        "bot was blocked",
        "not enough rights",
        "reply message not found",
        "message thread not found",
    )
    if any(message in error_text for message in excluded_errors):
        return False

    video_errors = (
        "failed to process",
        "failed to get video",
        "wrong file",
        "video",
        "mime",
        "duration",
    )
    return any(message in error_text for message in video_errors)


async def update_upload_status(status_msg, file_size: int):
    started_at = time.monotonic()
    frame_index = 0
    while True:
        await asyncio.sleep(UPLOAD_STATUS_INTERVAL_SECONDS)
        elapsed_text = format_elapsed_time(time.monotonic() - started_at)
        progress_frame = UPLOAD_PROGRESS_FRAMES[frame_index % len(UPLOAD_PROGRESS_FRAMES)]
        frame_index += 1
        try:
            await status_msg.edit_text(
                "文件已下载完成，正在上传至 Telegram... 🚀\n\n"
                f"文件大小：{format_file_size(file_size)}\n"
                f"上传状态：[{progress_frame}] 处理中\n"
                f"已等待：{elapsed_text}\n\n"
                "提示：Telegram 不提供真实上传百分比，此进度条表示任务仍在运行。"
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logging.warning("上传状态更新失败 exception=%s", exc)
        except NetworkError as exc:
            logging.warning("上传状态更新遇到网络异常 exception=%s", exc)
        except Exception as exc:
            logging.warning("上传状态更新已停止 exception_type=%s exception=%s", exc.__class__.__name__, exc)
            return


async def send_media_file(context, status_msg, final_file: str, is_audio: bool, original_msg_id: int):
    send_method_name = "send_audio" if is_audio else "send_video"
    retry_attempted = False
    send_started_at = time.monotonic()
    file_exists = os.path.exists(final_file)
    file_size = os.path.getsize(final_file) if file_exists else -1
    logging.info(
        "Telegram 发送开始 file_path=%s file_exists=%s file_size_bytes=%s file_size_mb=%s send_method=%s transport=%s retry_attempted=%s",
        final_file,
        file_exists,
        file_size,
        format_file_size(file_size) if file_size >= 0 else "N/A",
        send_method_name,
        "local_path",
        False,
    )

    if not file_exists:
        raise FileNotFoundError(final_file)

    upload_status_task = asyncio.create_task(
        update_upload_status(status_msg, file_size),
        name="telegram-upload-status",
    )
    try:
        if is_audio:
            await context.bot.send_audio(
                chat_id=status_msg.chat_id,
                audio=final_file,
                reply_to_message_id=original_msg_id,
                **TELEGRAM_SEND_TIMEOUTS,
            )
        else:
            try:
                await context.bot.send_video(
                    chat_id=status_msg.chat_id,
                    video=final_file,
                    supports_streaming=True,
                    reply_to_message_id=original_msg_id,
                    **TELEGRAM_SEND_TIMEOUTS,
                )
            except Exception as video_error:
                if not should_fallback_to_document(video_error):
                    raise
                logging.warning(
                    "视频接口处理失败，回退为普通文件发送 file_path=%s exception=%s",
                    final_file,
                    video_error,
                )
                retry_attempted = True
                send_method_name = "send_document_fallback"
                await context.bot.send_document(
                    chat_id=status_msg.chat_id,
                    document=final_file,
                    filename=os.path.basename(final_file),
                    reply_to_message_id=original_msg_id,
                    **TELEGRAM_SEND_TIMEOUTS,
                )
        logging.info(
            "Telegram 发送成功 file_path=%s file_size_bytes=%s send_method=%s elapsed_seconds=%.2f",
            final_file,
            file_size,
            send_method_name,
            time.monotonic() - send_started_at,
        )
    except Exception as exc:
        logging.error(
            "Telegram 发送失败 file_path=%s file_size_bytes=%s send_method=%s elapsed_seconds=%.2f diagnostic_code=%s exception_type=%s exception=%s retry_attempted=%s",
            final_file,
            file_size,
            send_method_name,
            time.monotonic() - send_started_at,
            classify_send_error(exc),
            exc.__class__.__name__,
            exc,
            retry_attempted,
        )
        raise
    finally:
        upload_status_task.cancel()
        try:
            await upload_status_task
        except asyncio.CancelledError:
            pass


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
    retain_task_files = False
    context.user_data['last_update'] = 0
    loop = asyncio.get_event_loop()

    try:
        final_file = await asyncio.to_thread(sync_download, url, filepath_base, status_msg, loop, context, is_audio)
        if not os.path.exists(final_file):
            await status_msg.edit_text("下载失败❌")
            return

        await status_msg.edit_text(
            "媒体获取完成！正在发送至 Telegram... 🚀\n\n"
            f"文件大小：{format_file_size(os.path.getsize(final_file))}\n"
            "上传状态：[■■■□□□□□□□] 正在建立连接\n\n"
            "提示：大文件上传期间，状态将每 30 秒更新一次。"
        )
        original_msg_id = status_msg.reply_to_message.message_id if status_msg.reply_to_message else status_msg.message_id

        upload_started_at = time.monotonic()
        try:
            await send_media_file(context, status_msg, final_file, is_audio, original_msg_id)
        except Exception as send_error:
            retain_task_files = True
            await status_msg.edit_text(
                f"{build_send_failure_message(send_error)}\n\n"
                f"发送耗时：{format_elapsed_time(time.monotonic() - upload_started_at)}\n"
                f"服务器文件将暂存最多 {MEDIA_MAX_AGE_HOURS} 小时，便于排查。"
            )
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
                retain_task_files = True
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
        if retain_task_files:
            logging.warning(
                "发送失败，任务文件暂存等待定时清理 prefix=%s max_age_hours=%s",
                filepath_base,
                MEDIA_MAX_AGE_HOURS,
            )
        else:
            removed_count, removed_bytes = await asyncio.to_thread(cleanup_task_files, filepath_base)
            logging.info(
                "任务临时文件清理完成 prefix=%s removed_count=%s freed_mb=%.2f",
                filepath_base,
                removed_count,
                removed_bytes / (1024 * 1024),
            )

if __name__ == '__main__':
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .base_url("http://127.0.0.1:8081/bot")
        .local_mode(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), receive_link))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("🤖 模块化双引擎机器人已启动...")
    app.run_polling()
