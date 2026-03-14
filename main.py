import os
import uuid
import asyncio
import logging
import time
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from config import TOKEN, ALLOWED_USERS, SHARED_DIR
from downloader import sync_download
from ai_services import process_groq_transcription, format_text_with_llm

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text(f"🛑 无权限。您的 User ID 是：`{user_id}`")
        return

    text = update.message.text
    url = next((word for word in text.split() if word.startswith(('http://', 'https://'))), None)
    if not url:
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

        if is_audio:
            await context.bot.send_audio(chat_id=status_msg.chat_id, audio=final_file, reply_to_message_id=original_msg_id, read_timeout=300, write_timeout=300)
        else:
            await context.bot.send_video(chat_id=status_msg.chat_id, video=final_file, supports_streaming=True, reply_to_message_id=original_msg_id, read_timeout=300, write_timeout=300)
            
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
            
            await context.bot.send_document(chat_id=status_msg.chat_id, document=txt_filepath, filename=clean_filename, caption="🎉 **排版完成！请查收附件。**", parse_mode="Markdown", reply_to_message_id=original_msg_id)

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