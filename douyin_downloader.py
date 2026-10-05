"""通过 Chromium 匿名会话解析抖音，媒体采用流式下载。"""
import logging
import os
import threading
import time
from urllib.parse import parse_qs, urlparse

import requests
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_BROWSER_LOCK = threading.Lock()


class DouyinDownloadError(RuntimeError):
    """可直接展示给用户的抖音错误。"""


def extract_aweme_id(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part in {"video", "note"} and parts[index + 1].isdigit():
            return parts[index + 1]
    query = parse_qs(parsed.query)
    for key in ("aweme_id", "modal_id", "item_id"):
        value = query.get(key, [None])[0]
        if value and value.isdigit():
            return value
    return None


def _video_urls(item: dict) -> list[str]:
    video = item.get("video") or {}
    if not isinstance(video, dict):
        return []
    addresses = [video.get(key) for key in ("play_addr", "playAddr", "play_addr_h264", "play_addr_265")]
    for variant in video.get("bit_rate") or []:
        if isinstance(variant, dict):
            addresses.append(variant.get("play_addr"))
    urls = []
    for address in addresses:
        if isinstance(address, str):
            candidates = [address]
        elif isinstance(address, dict):
            candidates = address.get("url_list") or address.get("urlList") or []
        else:
            continue
        for candidate in candidates:
            if isinstance(candidate, str) and urlparse(candidate).scheme in {"http", "https"} and candidate not in urls:
                urls.append(candidate)
    return urls


def _find_video(payload, aweme_id: str) -> dict | None:
    # 必须匹配作品 ID，防止误下载推荐视频。
    if isinstance(payload, dict):
        item_id = payload.get("aweme_id") or payload.get("awemeId") or payload.get("item_id")
        if str(item_id) == aweme_id and _video_urls(payload):
            return payload
        for value in payload.values():
            found = _find_video(value, aweme_id)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_video(value, aweme_id)
            if found:
                return found
    return None


# 读取浏览器执行脚本后的数据，不使用 eval 或另行请求旧详情接口。
_PAGE_DATA = """() => {
    const result = [window._ROUTER_DATA, window.RENDER_DATA].filter(Boolean);
    for (const script of document.querySelectorAll('script[type="application/json"], script#RENDER_DATA, script#__NEXT_DATA__')) {
        const text = script.textContent;
        try { result.push(JSON.parse(text)); }
        catch (_) { try { result.push(JSON.parse(decodeURIComponent(text))); } catch (_) {} }
    }
    return result;
}"""


def fetch_douyin_video_meta(url: str) -> dict:
    timeout = max(10, int(os.getenv("DOUYIN_BROWSER_TIMEOUT_SECONDS", "60")))
    if not _BROWSER_LOCK.acquire(timeout=timeout):
        raise DouyinDownloadError("抖音解析繁忙，请稍后重试")
    started = time.monotonic()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", headless=True, timeout=timeout * 1000)
            try:
                context = browser.new_context(
                    user_agent=DEFAULT_USER_AGENT, locale="zh-CN",
                    timezone_id="Asia/Shanghai", viewport={"width": 1440, "height": 900},
                )
                page = context.new_page()
                payloads = []

                def on_response(response):
                    host = (urlparse(response.url).hostname or "").lower()
                    if not (host == "douyin.com" or host.endswith(".douyin.com") or host == "iesdouyin.com" or host.endswith(".iesdouyin.com")):
                        return
                    if response.status != 200 or "json" not in response.headers.get("content-type", "").lower():
                        return
                    try:
                        payloads.append(response.json())
                        if len(payloads) > 64:
                            payloads.pop(0)
                    except (BrowserError, ValueError):
                        pass

                page.on("response", on_response)
                deadline = time.monotonic() + timeout
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                except BrowserTimeout:
                    logger.warning("抖音导航超时，检查已收到的数据")
                aweme_id = extract_aweme_id(url) or extract_aweme_id(page.url)
                while True:
                    aweme_id = aweme_id or extract_aweme_id(page.url)
                    item = None
                    if aweme_id:
                        for payload in payloads:
                            item = _find_video(payload, aweme_id)
                            if item:
                                break
                        if not item:
                            try:
                                item = _find_video(page.evaluate(_PAGE_DATA), aweme_id)
                            except BrowserError:
                                pass
                    if item:
                        video_urls = _video_urls(item)
                        logger.info("抖音匿名解析成功 aweme_id=%s candidate_count=%s elapsed_seconds=%.2f", aweme_id, len(video_urls), time.monotonic() - started)
                        return {
                            "aweme_id": aweme_id, "resolved_url": page.url,
                            "video_url": video_urls[0], "video_urls": video_urls,
                            "title": item.get("desc") or "",
                            "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": page.url},
                            "cookies": context.cookies(video_urls),
                        }
                    if time.monotonic() >= deadline:
                        break
                    page.wait_for_timeout(500)
                try:
                    body = page.locator("body").inner_text(timeout=2000)
                except BrowserError:
                    body = ""
                if any(word in body for word in ("验证码", "完成验证", "安全验证", "拖动滑块")):
                    raise DouyinDownloadError("抖音要求验证码，当前服务器匿名会话无法解析；请稍后重试并检查 VPS 出口网络")
                raise DouyinDownloadError("抖音解析超时或作品不可公开访问，未取得目标视频地址")
            finally:
                browser.close()
    except DouyinDownloadError:
        raise
    except BrowserError as exc:
        logger.exception("抖音浏览器运行失败")
        raise DouyinDownloadError("抖音浏览器运行失败，请检查 Chromium 安装、容器依赖和服务器网络") from exc
    finally:
        _BROWSER_LOCK.release()


def download_file(url: str, output_path: str, headers: dict, cookies: list | None = None) -> str:
    # Cookie 保留域名和路径限制，不发送给无关 CDN。
    with requests.Session() as session:
        for cookie in cookies or []:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"], secure=cookie.get("secure", False))
        with session.get(url, headers=headers, stream=True, timeout=(15, 60)) as response:
            response.raise_for_status()
            total = 0
            header = bytearray()
            with open(output_path, "wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        if len(header) < 16:
                            header.extend(chunk[:16 - len(header)])
                        total += len(chunk)
                        output.write(chunk)
            if total < 8 or header[4:8] != b"ftyp":
                raise DouyinDownloadError("抖音下载返回的不是有效 MP4 视频")
            expected = response.headers.get("Content-Length")
            if expected and not response.headers.get("Content-Encoding") and total != int(expected):
                raise DouyinDownloadError("抖音视频下载不完整")
    return output_path


def download_douyin_video(url: str, output_path: str) -> str:
    meta = fetch_douyin_video_meta(url)
    for index, candidate in enumerate(meta["video_urls"]):
        try:
            return download_file(candidate, output_path, meta["headers"], meta["cookies"])
        except (requests.RequestException, DouyinDownloadError) as exc:
            logger.warning("抖音媒体候选下载失败 aweme_id=%s candidate_index=%s error_type=%s", meta["aweme_id"], index, type(exc).__name__)
    raise DouyinDownloadError("抖音视频地址已解析，但所有候选地址下载失败，请检查服务器网络或稍后重试")
