import time
import asyncio
import yt_dlp

def progress_hook(d, status_msg, loop, context):
    if d['status'] == 'downloading':
        last_update_time = context.user_data.get('last_update', 0)
        current_time = time.time()
        
        if current_time - last_update_time > 2.5: 
            percent = d.get('_percent_str', '0%')
            speed = d.get('_speed_str', 'N/A')
            eta = d.get('_eta_str', 'N/A')
            
            try:
                p = float(percent.replace('%','').strip()) / 10
                bar = "■" * int(p) + "□" * (10 - int(p))
            except:
                bar = "■■■■■□□□□□"
            
            progress_text = f"正在极速下载中... ⏳\n\n进度: [{bar}] {percent}\n速度: {speed}\n剩余时间: {eta}"
            asyncio.run_coroutine_threadsafe(status_msg.edit_text(progress_text), loop)
            context.user_data['last_update'] = current_time

def sync_download(url: str, filepath_base: str, status_msg, loop, context, is_audio: bool) -> str:
    if is_audio:
        ydl_opts = {
            'outtmpl': filepath_base,
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '64'}],
            'quiet': True, 'no_warnings': True,
            'progress_hooks': [lambda d: progress_hook(d, status_msg, loop, context)],
        }
        final_file = filepath_base + '.mp3'
    else:
        ydl_opts = {
            'outtmpl': filepath_base + '.mp4',
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best', 
            'quiet': True, 'no_warnings': True,
            'progress_hooks': [lambda d: progress_hook(d, status_msg, loop, context)],
        }
        final_file = filepath_base + '.mp4'
        
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
        
    return final_file