import logging
import random
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from douyin_a_bogus import get_a_bogus


logger = logging.getLogger(__name__)

COOKIE_FILE = "/app/cookies.txt"
DOUYIN_COOKIE_SUFFIXES = (
    "douyin.com",
    ".douyin.com",
    "www.douyin.com",
    "v.douyin.com",
    "www.douyin.com",
    "iesdouyin.com",
    ".iesdouyin.com",
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def load_douyin_cookie_header(cookie_file: str = COOKIE_FILE) -> tuple[str, list[str]]:
    cookie_path = Path(cookie_file)
    if not cookie_path.exists():
        raise FileNotFoundError("抖音下载缺少 cookies.txt，请将 cookies.txt 挂载到 /app/cookies.txt")

    now = int(time.time())
    cookies: OrderedDict[str, str] = OrderedDict()
    for line in cookie_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        parts = stripped.split("\t")
        if len(parts) != 7:
            continue

        domain, _, _, _, expires, name, value = parts
        domain = domain.strip().lower()
        if not any(domain == suffix or domain.endswith(suffix) for suffix in DOUYIN_COOKIE_SUFFIXES):
            continue

        expires = expires.strip()
        if expires.isdigit():
            expires_at = int(expires)
            if expires_at > 0 and expires_at < now:
                continue

        cookies[name] = value

    if not cookies:
        raise RuntimeError("cookies.txt 中未找到可用的抖音 Cookie")

    return "; ".join(f"{name}={value}" for name, value in cookies.items()), list(cookies.keys())


def resolve_douyin_url(url: str, cookie_header: str, user_agent: str = DEFAULT_USER_AGENT) -> str:
    session = requests.Session()
    response = session.get(
        url,
        headers={
            "User-Agent": user_agent,
            "Cookie": cookie_header,
            "Referer": "https://www.douyin.com/",
        },
        allow_redirects=True,
        timeout=15,
    )
    response.raise_for_status()
    return response.url


def extract_aweme_id(url: str) -> str:
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(path_parts):
        if part == "video" and index + 1 < len(path_parts) and path_parts[index + 1].isdigit():
            return path_parts[index + 1]

    query = parse_qs(parsed.query)
    for key in ("aweme_id", "modal_id"):
        value = query.get(key, [None])[0]
        if value and value.isdigit():
            return value

    raise RuntimeError("抖音链接解析失败，未提取到 aweme_id")


def generate_ms_token(length: int = 107) -> str:
    alphabet = "ABCDEFGHIGKLMNOPQRSTUVWXYZabcdefghigklmnopqrstuvwxyz0123456789="
    return "".join(random.choice(alphabet) for _ in range(length))


def build_douyin_detail_api_url(aweme_id: str, ms_token: str) -> str:
    return (
        "https://www.douyin.com/aweme/v1/web/aweme/detail/"
        "?device_platform=webapp"
        "&aid=6383"
        "&channel=channel_pc_web"
        f"&aweme_id={aweme_id}"
        "&update_version_code=170400"
        "&pc_client_type=1"
        "&version_code=190500"
        "&version_name=19.5.0"
        "&cookie_enabled=true"
        "&screen_width=1440"
        "&screen_height=900"
        "&browser_language=zh-CN"
        "&browser_platform=Win32"
        "&browser_name=Chrome"
        "&browser_version=124.0.0.0"
        "&browser_online=true"
        "&engine_name=Blink"
        "&engine_version=124.0.0.0"
        "&os_name=Windows"
        "&os_version=10"
        "&cpu_core_num=8"
        "&device_memory=8"
        "&platform=PC"
        "&downlink=10"
        "&effective_type=4g"
        "&round_trip_time=50"
        "&webid=7319780293514495522"
        "&verifyFp=verify_lw66uj9x_y69HTWBr_NOK0_4O0k_As0k_vZiOREH3U5SC"
        "&fp=verify_lw66uj9x_y69HTWBr_NOK0_4O0k_As0k_vZiOREH3U5SC"
        f"&msToken={ms_token}"
    )


def sign_douyin_api_url(api_url: str, user_agent: str, cookie_header: str) -> tuple[str, int, str, str]:
    query = urlparse(api_url).query
    a_bogus, returncode, stdout, stderr = get_a_bogus(query, user_agent, cookie_header)
    separator = "&" if "?" in api_url else "?"
    return f"{api_url}{separator}a_bogus={a_bogus}", returncode, stdout, stderr


def _pick_video_url(aweme_detail: dict) -> str | None:
    video = aweme_detail.get("video") or {}
    for key in ("play_addr", "play_addr_265", "play_addr_h264"):
        url_list = ((video.get(key) or {}).get("url_list") or [])
        if url_list:
            return url_list[0]

    bit_rate = video.get("bit_rate") or []
    for item in bit_rate:
        url_list = ((item.get("play_addr") or {}).get("url_list") or [])
        if url_list:
            return url_list[0]
    return None


def fetch_douyin_video_meta(url: str, cookie_header: str) -> dict:
    logger.info("开始解析抖音链接 raw_url=%s", url)
    resolved_url = resolve_douyin_url(url, cookie_header, DEFAULT_USER_AGENT)
    aweme_id = extract_aweme_id(resolved_url)

    logger.info("抖音链接解析完成 raw_url=%s resolved_url=%s aweme_id=%s", url, resolved_url, aweme_id)
    ms_token = generate_ms_token()
    api_url = build_douyin_detail_api_url(aweme_id, ms_token)
    signed_url, node_returncode, node_stdout, node_stderr = sign_douyin_api_url(api_url, DEFAULT_USER_AGENT, cookie_header)

    logger.info(
        "抖音签名完成 aweme_id=%s node_returncode=%s node_stdout_preview=%s node_stderr_preview=%s",
        aweme_id,
        node_returncode,
        node_stdout[:300],
        node_stderr[:300],
    )

    response = requests.get(
        signed_url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": f"https://www.douyin.com/video/{aweme_id}",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie_header,
        },
        timeout=15,
    )
    logger.info(
        "抖音详情接口响应 aweme_id=%s status_code=%s body_preview=%s",
        aweme_id,
        response.status_code,
        response.text[:500],
    )
    response.raise_for_status()

    payload = response.json()
    aweme_detail = payload.get("aweme_detail") or {}
    video_url = _pick_video_url(aweme_detail)
    if not video_url:
        raise RuntimeError("抖音接口返回异常，未获取到视频地址")

    return {
        "aweme_id": aweme_id,
        "resolved_url": resolved_url,
        "video_url": video_url,
        "title": aweme_detail.get("desc") or aweme_detail.get("caption") or "",
        "headers": {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": f"https://www.douyin.com/video/{aweme_id}",
            "Cookie": cookie_header,
        },
    }


def download_file(url: str, output_path: str, headers: dict) -> str:
    with requests.get(url, headers=headers, stream=True, timeout=30) as response:
        response.raise_for_status()
        with open(output_path, "wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    output.write(chunk)
    return output_path
