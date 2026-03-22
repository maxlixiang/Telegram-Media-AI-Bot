import os
import subprocess
import time
import asyncio
import logging
from urllib.parse import urlparse
import yt_dlp

from douyin_downloader import COOKIE_FILE, download_file, fetch_douyin_video_meta, load_douyin_cookie_header
from twitter_downloader import download_file as download_twitter_file
from twitter_downloader import fetch_twitter_video_meta, is_twitter_url

COOKIE_FILE = '/app/cookies.txt'
DOUYIN_HOSTS = {"douyin.com", "www.douyin.com", "v.douyin.com", "iesdouyin.com"}
logger = logging.getLogger(__name__)

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


def is_douyin_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in DOUYIN_HOSTS


def extract_audio_to_mp3(input_video_path: str, output_mp3_path: str) -> str:
    process = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_video_path,
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "64k",
            output_mp3_path,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode != 0:
        logger.error("FFmpeg 转换失败 stderr_preview=%s", process.stderr[:500])
        raise RuntimeError("媒体音频提取失败")
    return output_mp3_path


def download_douyin_media(url: str, filepath_base: str, is_audio: bool) -> str:
    cookie_header, cookie_names = load_douyin_cookie_header(COOKIE_FILE)
    logger.info(
        "抖音 Cookie 读取结果 raw_url=%s cookie_loaded=%s cookie_names=%s",
        url,
        bool(cookie_names),
        cookie_names,
    )

    media_meta = fetch_douyin_video_meta(url, cookie_header)
    mp4_path = filepath_base + ".mp4"
    download_file(media_meta["video_url"], mp4_path, media_meta["headers"])

    if not is_audio:
        return mp4_path

    mp3_path = filepath_base + ".mp3"
    try:
        extract_audio_to_mp3(mp4_path, mp3_path)
    finally:
        if os.path.exists(mp4_path):
            os.remove(mp4_path)
    return mp3_path


def download_twitter_media(url: str, filepath_base: str, is_audio: bool) -> str:
    media_meta = fetch_twitter_video_meta(url)
    mp4_path = filepath_base + ".mp4"
    download_twitter_file(media_meta["video_url"], mp4_path, media_meta["headers"])

    if not is_audio:
        return mp4_path

    mp3_path = filepath_base + ".mp3"
    try:
        extract_audio_to_mp3(mp4_path, mp3_path)
    finally:
        if os.path.exists(mp4_path):
            os.remove(mp4_path)
    return mp3_path

def sync_download(url: str, filepath_base: str, status_msg, loop, context, is_audio: bool) -> str:
    if is_douyin_url(url):
        return download_douyin_media(url, filepath_base, is_audio)
    if is_twitter_url(url):
        return download_twitter_media(url, filepath_base, is_audio)

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
