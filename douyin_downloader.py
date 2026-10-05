"""通过 Chromium 匿名会话解析抖音，媒体采用流式下载。"""
import asyncio
import logging
import os
import threading
import time
from collections import deque
from urllib.parse import parse_qs, urlparse

import requests
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

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


# 在浏览器中只筛选目标作品，避免反复传输整份页面数据。
_PAGE_DATA = """(awemeId) => {
    let visits = 0;
    const seen = new WeakSet();
    function find(value) {
        if (!value || typeof value !== 'object' || seen.has(value) || ++visits > 20000) return null;
        seen.add(value);
        const id = value.aweme_id || value.awemeId || value.item_id;
        if (String(id) === awemeId && value.video) return value;
        for (const child of Object.values(value)) {
            const found = find(child);
            if (found) return found;
        }
        return null;
    }
    let item = find(window._ROUTER_DATA) || find(window.RENDER_DATA);
    if (!item) {
        for (const script of document.querySelectorAll('script[type="application/json"], script#RENDER_DATA, script#__NEXT_DATA__')) {
            let data;
            try { data = JSON.parse(script.textContent); }
            catch (_) { try { data = JSON.parse(decodeURIComponent(script.textContent)); } catch (_) {} }
            item = find(data);
            if (item) break;
        }
    }
    const text = (document.body?.textContent || '').slice(0, 5000);
    return {
        item, ready_state: document.readyState,
        captcha: /验证码|完成验证|安全验证|拖动滑块/.test(text),
        video_loading: text.includes('视频数据加载中')
    };
}"""


def _safe_location(url: str) -> str:
    parsed = urlparse(url)
    # 不记录查询参数、Cookie 或媒体签名。
    return f"{parsed.hostname or 'unknown'}{parsed.path}"


def _is_douyin_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in ("douyin.com", "iesdouyin.com"))


def _is_video_request(url: str) -> bool:
    path = urlparse(url).path.lower()
    return _is_douyin_host(url) and any(key in path for key in ("/aweme/detail", "/aweme/v1/web/aweme/detail", "/aweme/v1/web/feed", "/iteminfo"))


async def _read_page_data(page, aweme_id: str, stats: dict):
    try:
        # 页面渲染卡住时，不允许 evaluate 无限占住下载线程。
        snapshot = await asyncio.wait_for(page.evaluate(_PAGE_DATA, aweme_id), timeout=2)
        for key in ("ready_state", "captcha", "video_loading"):
            stats[key] = snapshot.get(key)
        return _find_video(snapshot.get("item"), aweme_id)
    except (asyncio.TimeoutError, BrowserTimeout):
        stats["page_read_timeouts"] += 1
    except BrowserError:
        stats["page_read_errors"] += 1
    return None


async def _parse_douyin_page(context, url: str, stats: dict):
    started = time.monotonic()
    page = await context.new_page()
    payloads = deque(maxlen=8)
    tasks = set()
    matched = None
    aweme_id = extract_aweme_id(url)

    async def route_request(route):
        if route.request.resource_type in {"image", "media", "font"}:
            stats["blocked_resources"] += 1
            await route.abort()
        else:
            await route.continue_()

    def on_request(request):
        if _is_video_request(request.url):
            stats["video_requests"] += 1
            logger.info("抖音视频请求 endpoint=%s", _safe_location(request.url))

    def on_request_failed(request):
        if request.resource_type in {"image", "media", "font"}:
            return  # 我们主动阻止的资源不算网络故障。
        stats["request_failures"] += 1
        if request.resource_type == "script":
            stats["script_request_failures"] += 1
        if (urlparse(request.url).hostname or "") == "invalid":
            stats["invalid_requests"] += 1
        if _is_video_request(request.url):
            stats["video_request_failures"] += 1
        if _is_video_request(request.url) or (request.resource_type == "script" and stats["script_request_failures"] <= 10) or (stats["request_failures"] <= 10 and stats["invalid_requests"] <= 2):
            failure = request.failure or "unknown"
            # 只保留浏览器错误码，不输出可能包含 URL 的整段异常。
            code = failure if failure.startswith("net::") and " " not in failure else "REQUEST_FAILED"
            logger.warning("抖音请求失败 resource=%s endpoint=%s error=%s", request.resource_type, _safe_location(request.url), code)

    def on_page_error(error):
        stats["script_errors"] += 1
        logger.warning("抖音页面脚本异常 error_type=%s", error.name)

    async def read_response(response):
        nonlocal matched
        try:
            data = await asyncio.wait_for(response.json(), timeout=3)
            stats["json_responses"] += 1
            target = aweme_id or extract_aweme_id(page.url)
            if target:
                item = _find_video(data, target)
                if item:
                    matched = item
            else:
                payloads.append(data)
        except (asyncio.TimeoutError, BrowserError, ValueError):
            stats["json_read_errors"] += 1

    def on_response(response):
        if response.request.resource_type == "script" and response.status >= 400:
            stats["script_http_errors"] += 1
            logger.warning("抖音脚本响应异常 endpoint=%s status=%s", _safe_location(response.url), response.status)
        if not _is_douyin_host(response.url):
            return
        if _is_video_request(response.url):
            stats["video_responses"] += 1
            if response.status >= 400:
                stats["video_http_errors"] += 1
            logger.info("抖音视频响应 endpoint=%s status=%s content_type=%s", _safe_location(response.url), response.status, response.headers.get("content-type", ""))
        if response.status == 200 and "json" in response.headers.get("content-type", "").lower() and (_is_video_request(response.url) or len(tasks) < 8):
            task = asyncio.create_task(read_response(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    await context.route("**/*", route_request)
    page.on("request", on_request)
    page.on("requestfailed", on_request_failed)
    page.on("pageerror", on_page_error)
    page.on("response", on_response)
    try:
        try:
            response = await page.goto(url, wait_until="commit", timeout=15000)
            stats["navigation_status"] = response.status if response else None
        except BrowserTimeout:
            stats["navigation_timeout"] = True
            logger.warning("抖音导航等待超时，继续监听视频响应")
        next_page_read = time.monotonic() + 1
        while True:
            stats["final_page"] = _safe_location(page.url)
            aweme_id = aweme_id or extract_aweme_id(page.url)
            stats["aweme_id"] = aweme_id
            if aweme_id:
                while payloads and not matched:
                    matched = _find_video(payloads.popleft(), aweme_id)
                if not matched and time.monotonic() >= next_page_read:
                    item = await _read_page_data(page, aweme_id, stats)
                    # 读取页面时网络响应也可能已匹配成功，不能将其覆盖。
                    matched = matched or item
                    next_page_read = time.monotonic() + 5
            if matched:
                video_urls = _video_urls(matched)
                logger.info("抖音匿名解析成功 aweme_id=%s candidate_count=%s elapsed_seconds=%.2f stats=%s", aweme_id, len(video_urls), time.monotonic() - started, stats)
                return {
                    "aweme_id": aweme_id, "resolved_url": page.url,
                    "video_url": video_urls[0], "video_urls": video_urls,
                    "title": matched.get("desc") or "",
                    "headers": {"User-Agent": DEFAULT_USER_AGENT, "Referer": page.url},
                    "cookies": await context.cookies(video_urls),
                }
            if stats["captcha"]:
                raise DouyinDownloadError("抖音要求验证码，当前服务器匿名会话无法解析；请稍后重试并检查 VPS 出口网络")
            await asyncio.sleep(0.25)
    finally:
        page.remove_listener("response", on_response)
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _diagnostic_code(stats: dict) -> str:
    if stats["captcha"]:
        return "CAPTCHA_REQUIRED"
    if stats["video_http_errors"]:
        return "VIDEO_HTTP_ERROR"
    if stats["video_request_failures"]:
        return "VIDEO_REQUEST_FAILED"
    if stats["script_http_errors"] or stats["script_request_failures"]:
        return "SCRIPT_LOAD_FAILED"
    if stats["page_read_timeouts"] >= 2:
        return "PAGE_UNRESPONSIVE"
    if not stats["video_requests"]:
        return "NO_VIDEO_REQUEST"
    return "VIDEO_DATA_MISSING"


async def _fetch_douyin_video_meta(url: str, timeout: int) -> dict:
    stats = dict.fromkeys(("video_requests", "video_responses", "video_http_errors", "video_request_failures", "json_responses", "json_read_errors", "request_failures", "invalid_requests", "script_errors", "script_http_errors", "script_request_failures", "page_read_timeouts", "page_read_errors", "blocked_resources"), 0)
    stats.update(captcha=False, video_loading=False, ready_state="unknown", navigation_timeout=False)
    started = time.monotonic()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chromium", headless=True, timeout=15000)
        try:
            context = await browser.new_context(
                user_agent=DEFAULT_USER_AGENT, locale="zh-CN",
                timezone_id="Asia/Shanghai", viewport={"width": 1440, "height": 900},
            )
            try:
                return await asyncio.wait_for(_parse_douyin_page(context, url, stats), timeout=timeout)
            except (asyncio.TimeoutError, DouyinDownloadError) as exc:
                code = _diagnostic_code(stats)
                logger.error("抖音解析诊断 diagnostic=%s elapsed_seconds=%.2f stats=%s", code, time.monotonic() - started, stats)
                if isinstance(exc, DouyinDownloadError):
                    raise DouyinDownloadError(f"{exc}（诊断：{code}）") from exc
                raise DouyinDownloadError(f"抖音解析超时，未取得目标视频地址（诊断：{code}）；请查看服务器日志") from exc
        finally:
            await browser.close()


def fetch_douyin_video_meta(url: str) -> dict:
    timeout = max(10, int(os.getenv("DOUYIN_BROWSER_TIMEOUT_SECONDS", "60")))
    if not _BROWSER_LOCK.acquire(timeout=timeout):
        raise DouyinDownloadError("抖音解析繁忙，请稍后重试")
    try:
        # 下载调度器已在独立线程调用此入口，保持其他平台和后处理接口不变。
        return asyncio.run(_fetch_douyin_video_meta(url, timeout))
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
