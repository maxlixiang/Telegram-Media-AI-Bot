import logging
import re
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TWITTER_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


def is_twitter_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname in TWITTER_HOSTS


def extract_tweet_id(url: str) -> str:
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(path_parts):
        if part == "status" and index + 1 < len(path_parts):
            candidate = path_parts[index + 1]
            if candidate.isdigit():
                return candidate

    match = re.search(r"/status/(\d+)", url)
    if match:
        return match.group(1)

    raise RuntimeError("Twitter 视频解析失败：无法提取 tweet_id")


def build_fxtwitter_mp4_url(tweet_id: str) -> str:
    return f"https://fxtwitter.com/i/status/{tweet_id}.mp4"


def fetch_twitter_video_meta(url: str) -> dict[str, str]:
    tweet_id = extract_tweet_id(url)
    fxtwitter_url = build_fxtwitter_mp4_url(tweet_id)
    logger.info(
        "Twitter 直链生成 raw_url=%s tweet_id=%s fxtwitter_url=%s",
        url,
        tweet_id,
        fxtwitter_url,
    )
    return {
        "tweet_id": tweet_id,
        "video_url": fxtwitter_url,
        "headers": {"User-Agent": USER_AGENT},
    }


def download_file(url: str, output_path: str, headers: dict[str, str] | None = None) -> str:
    try:
        with requests.get(url, headers=headers or {}, stream=True, timeout=30) as response:
            logger.info("Twitter 直链响应 fxtwitter_url=%s status_code=%s", url, response.status_code)
            if response.status_code != 200:
                raise RuntimeError("Twitter 视频下载失败：直链不可用")

            total_size = 0
            first_chunk = b""
            with open(output_path, "wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    if not first_chunk:
                        first_chunk = chunk[:16]
                    total_size += len(chunk)
                    output.write(chunk)

        if total_size == 0:
            raise RuntimeError("Twitter 视频下载失败：下载内容为空")

        with open(output_path, "rb") as downloaded_file:
            header = downloaded_file.read(16)
        if len(header) < 8 or header[4:8] != b"ftyp":
            raise RuntimeError("Twitter 视频下载失败：下载到的不是有效 mp4")

        logger.info(
            "Twitter 下载完成 fxtwitter_url=%s file_size_bytes=%s output_path=%s",
            url,
            total_size,
            output_path,
        )
        return output_path
    except requests.Timeout as exc:
        raise RuntimeError("Twitter 视频下载失败：请求超时") from exc
    except requests.RequestException as exc:
        raise RuntimeError("Twitter 视频下载失败：直链不可用") from exc
