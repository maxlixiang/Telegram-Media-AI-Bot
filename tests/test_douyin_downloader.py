import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import requests

from douyin_downloader import (
    DouyinDownloadError, _find_video, _video_urls,
    download_douyin_video, download_file, extract_aweme_id, fetch_douyin_video_meta,
)
from playwright.async_api import Error as BrowserError


class DouyinTests(unittest.TestCase):
    def test_ids_from_canonical_share_and_modal_urls(self):
        for url in ("https://www.douyin.com/video/123", "https://www.iesdouyin.com/share/video/123/", "https://www.douyin.com/?modal_id=123"):
            self.assertEqual(extract_aweme_id(url), "123")
        self.assertIsNone(extract_aweme_id("https://v.douyin.com/short/"))

    def test_recommendations_are_never_selected(self):
        wrong = {"aweme_id": "456", "video": {"play_addr": {"url_list": ["https://cdn.example/wrong"]}}}
        right = {"aweme_id": "123", "video": {"play_addr": {"url_list": ["https://cdn.example/right"]}}}
        self.assertIs(_find_video({"data": [wrong, right]}, "123"), right)
        self.assertIsNone(_find_video({"data": [wrong]}, "123"))

    def test_share_page_camel_case_and_backup_addresses(self):
        item = {"awemeId": "123", "video": {
            "playAddr": "https://cdn.example/a",
            "play_addr": {"url_list": ["https://cdn.example/a", "https://cdn.example/b", "javascript:bad"]},
            "download_addr": {"url_list": ["https://cdn.example/watermarked"]},
        }}
        self.assertIs(_find_video({"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [item]}}}}, "123"), item)
        self.assertEqual(_video_urls(item), ["https://cdn.example/a", "https://cdn.example/b"])

    def _session(self, body, declared_size=None):
        session = MagicMock()
        response = MagicMock()
        response.iter_content.return_value = [body[:3], body[3:7], body[7:]]
        response.headers = {"Content-Length": str(declared_size or len(body))}
        session.get.return_value.__enter__.return_value = response
        return session

    def test_streamed_mp4_and_domain_scoped_anonymous_cookies(self):
        body = b"\x00\x00\x00\x18ftypisom" + b"0" * 20
        session = self._session(body)
        with patch("builtins.open", mock_open()) as output, patch("douyin_downloader.requests.Session") as factory:
            factory.return_value.__enter__.return_value = session
            path = "test.mp4"
            download_file("https://cdn.example/a", path, {}, [{"name": "anonymous", "value": "ticket", "domain": ".douyin.com", "path": "/", "secure": True}])
            self.assertEqual(b"".join(call.args[0] for call in output().write.call_args_list), body)
            session.cookies.set.assert_called_once_with("anonymous", "ticket", domain=".douyin.com", path="/", secure=True)

    def test_html_and_truncated_downloads_rejected(self):
        for body, declared_size in [(b"<html>captcha</html>", None), (b"0000ftypisom", 100)]:
            with self.subTest(body=body), patch("builtins.open", mock_open()), patch("douyin_downloader.requests.Session") as factory:
                factory.return_value.__enter__.return_value = self._session(body, declared_size)
                with self.assertRaises(DouyinDownloadError):
                    download_file("https://cdn.example/a", "test.mp4", {})

    def test_failed_candidate_retries_next_address(self):
        meta = {"aweme_id": "123", "video_urls": ["https://cdn.example/a", "https://cdn.example/b"], "headers": {}, "cookies": []}
        with patch("douyin_downloader.fetch_douyin_video_meta", return_value=meta), patch("douyin_downloader.download_file", side_effect=[requests.HTTPError(), "out.mp4"]) as download:
            self.assertEqual(download_douyin_video("https://www.douyin.com/video/123", "out.mp4"), "out.mp4")
            self.assertEqual(download.call_count, 2)
            self.assertEqual(download.call_args.args[0], "https://cdn.example/b")

    def _browser_fixture(self, factory):
        playwright = factory.return_value.__aenter__.return_value
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        context.cookies = AsyncMock(return_value=[])
        context.route = AsyncMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.evaluate = AsyncMock(return_value={"item": None, "ready_state": "complete", "captcha": False, "video_loading": True})
        page.url = "https://www.douyin.com/video/123"
        listeners = {}
        page.on.side_effect = lambda name, fn: listeners.update({name: fn})
        return playwright, browser, context, page, listeners

    def test_browser_reads_network_data_in_fresh_windows_context(self):
        item = {"aweme_id": "123", "video": {"play_addr": {"url_list": ["https://cdn.example/a"]}}}
        with patch("douyin_downloader.async_playwright") as factory:
            playwright, browser, context, page, listeners = self._browser_fixture(factory)
            response = MagicMock()
            response.url = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
            response.status = 200
            response.headers = {"content-type": "application/json"}
            response.body = AsyncMock(return_value=json.dumps({"aweme_detail": item}).encode())
            async def navigate(*args, **kwargs):
                listeners["request"](MagicMock(url=response.url))
                listeners["response"](response)
                listeners["requestfinished"](response.request)
                await asyncio.sleep(0)
                return MagicMock(status=200)
            page.goto.side_effect = navigate
            meta = fetch_douyin_video_meta("https://v.douyin.com/short/")
            self.assertEqual(meta["aweme_id"], "123")
            self.assertIn("Windows NT", browser.new_context.call_args.kwargs["user_agent"])
            self.assertTrue(playwright.chromium.launch.call_args.kwargs["headless"])
            self.assertEqual(playwright.chromium.launch.call_args.kwargs["channel"], "chromium")
            self.assertEqual(page.goto.call_args.kwargs["wait_until"], "commit")
            page.evaluate.assert_not_called()
            context.add_cookies.assert_not_called()
            browser.close.assert_awaited_once()
            handler = context.route.call_args.args[1]
            for kind in ("image", "media", "font", "script", "xhr", "fetch", "document"):
                route = MagicMock()
                route.request.resource_type = kind
                route.abort = AsyncMock()
                route.continue_ = AsyncMock()
                asyncio.run(handler(route))
                if kind in {"image", "media", "font"}:
                    route.abort.assert_awaited_once()
                    route.continue_.assert_not_called()
                else:
                    route.continue_.assert_awaited_once()
                    route.abort.assert_not_called()

    def test_browser_reads_executed_page_data_when_no_json_response(self):
        item = {"aweme_id": "123", "video": {"play_addr": {"url_list": ["https://cdn.example/a"]}}}
        with patch("douyin_downloader.async_playwright") as factory:
            _, browser, _, page, _ = self._browser_fixture(factory)
            page.evaluate.return_value = {"item": item, "ready_state": "interactive", "captcha": False, "video_loading": False}
            self.assertEqual(fetch_douyin_video_meta(page.url)["video_urls"], ["https://cdn.example/a"])
            browser.close.assert_awaited_once()

    def test_browser_failure_releases_serialization_lock(self):
        from douyin_downloader import _BROWSER_LOCK
        with patch("douyin_downloader.async_playwright") as factory:
            playwright, _, _, _, _ = self._browser_fixture(factory)
            playwright.chromium.launch.side_effect = BrowserError("missing browser")
            with self.assertLogs("douyin_downloader", level="ERROR"), self.assertRaises(DouyinDownloadError):
                fetch_douyin_video_meta("https://www.douyin.com/video/123")
            self.assertFalse(_BROWSER_LOCK.locked())

    def test_total_timeout_closes_browser_and_logs_without_query_secrets(self):
        from douyin_downloader import _fetch_douyin_video_meta
        with patch("douyin_downloader.async_playwright") as factory:
            _, browser, _, page, listeners = self._browser_fixture(factory)
            async def navigate(*args, **kwargs):
                request = MagicMock(url="https://www.douyin.com/aweme/v1/web/aweme/detail/?secret=PRIVATE", resource_type="fetch", failure="net::ERR_FAILED")
                listeners["request"](request)
                listeners["requestfailed"](request)
                return MagicMock(status=200)
            page.goto.side_effect = navigate
            with self.assertLogs("douyin_downloader", level="INFO") as logs, self.assertRaisesRegex(DouyinDownloadError, "VIDEO_REQUEST_FAILED"):
                asyncio.run(_fetch_douyin_video_meta(page.url, 0.05))
            self.assertNotIn("PRIVATE", "\n".join(logs.output))
            browser.close.assert_awaited_once()

    def test_captcha_is_reported_and_browser_closed(self):
        with patch("douyin_downloader.async_playwright") as factory:
            _, browser, _, page, _ = self._browser_fixture(factory)
            page.evaluate.return_value = {"item": None, "ready_state": "complete", "captcha": True, "video_loading": False}
            with self.assertLogs("douyin_downloader", level="ERROR"), self.assertRaisesRegex(DouyinDownloadError, "CAPTCHA_REQUIRED"):
                fetch_douyin_video_meta(page.url)
            browser.close.assert_awaited_once()

    def test_page_read_timeout_does_not_hang_parser(self):
        from douyin_downloader import _read_page_data
        page = MagicMock()
        async def stuck(*args):
            await asyncio.Future()
        page.evaluate = AsyncMock(side_effect=stuck)
        stats = {"page_read_timeouts": 0, "page_read_errors": 0}
        self.assertIsNone(asyncio.run(_read_page_data(page, "123", stats)))
        self.assertEqual(stats["page_read_timeouts"], 1)

    def _emit_video_response(self, page, listeners, body, finished=None):
        response = MagicMock()
        response.url = "https://www.douyin.com/aweme/v1/web/aweme/detail/?ticket=PRIVATE"
        response.status = 200
        response.headers = {"content-type": "text/plain; charset=utf-8"}
        response.body = AsyncMock(return_value=body)
        async def navigate(*args, **kwargs):
            listeners["request"](MagicMock(url=response.url))
            listeners["response"](response)
            if finished is not None:
                await finished()
            listeners["requestfinished"](response.request)
            await asyncio.sleep(0)
            return MagicMock(status=200)
        page.goto.side_effect = navigate
        return response

    def test_plain_text_json_detail_is_not_skipped(self):
        item = {"aweme_id": "123", "video": {"play_addr": {"url_list": ["https://cdn.example/a"]}}}
        with patch("douyin_downloader.async_playwright") as factory:
            _, browser, _, page, listeners = self._browser_fixture(factory)
            response = self._emit_video_response(page, listeners, json.dumps({"aweme_detail": item}).encode())
            self.assertEqual(fetch_douyin_video_meta(page.url)["aweme_id"], "123")
            response.body.assert_awaited_once()
            page.evaluate.assert_not_called()
            browser.close.assert_awaited_once()

    def test_empty_non_json_and_missing_video_are_distinguished(self):
        from douyin_downloader import _fetch_douyin_video_meta
        for body, code in [(b"", "VIDEO_EMPTY_RESPONSE"), (b"<html>PRIVATE</html>", "VIDEO_NON_JSON_RESPONSE"), (b'{"status_code": 0}', "VIDEO_JSON_WITHOUT_MEDIA")]:
            with self.subTest(code=code), patch("douyin_downloader.async_playwright") as factory:
                _, browser, _, page, listeners = self._browser_fixture(factory)
                self._emit_video_response(page, listeners, body)
                with self.assertLogs("douyin_downloader", level="INFO") as logs, self.assertRaisesRegex(DouyinDownloadError, code):
                    asyncio.run(_fetch_douyin_video_meta(page.url, 0.1))
                self.assertNotIn("PRIVATE", "\\n".join(logs.output))
                browser.close.assert_awaited_once()

    def test_response_waits_for_body_before_reading(self):
        item = {"aweme_id": "123", "video": {"play_addr": {"url_list": ["https://cdn.example/a"]}}}
        with patch("douyin_downloader.async_playwright") as factory:
            _, browser, _, page, listeners = self._browser_fixture(factory)
            async def finish():
                response.body.assert_not_called()
                await asyncio.sleep(0.02)
            response = self._emit_video_response(page, listeners, json.dumps({"aweme_detail": item}).encode(), finish)
            self.assertEqual(fetch_douyin_video_meta(page.url)["aweme_id"], "123")
            browser.close.assert_awaited_once()

    def test_unfinished_response_is_cancelled_with_distinct_diagnostic(self):
        from douyin_downloader import _fetch_douyin_video_meta
        with patch("douyin_downloader.async_playwright") as factory:
            _, browser, _, page, listeners = self._browser_fixture(factory)
            async def unfinished():
                await asyncio.Future()
            response = self._emit_video_response(page, listeners, b"", unfinished)
            with self.assertLogs("douyin_downloader", level="ERROR"), self.assertRaisesRegex(DouyinDownloadError, "VIDEO_BODY_PENDING"):
                asyncio.run(_fetch_douyin_video_meta(page.url, 0.05))
            response.body.assert_not_called()
            browser.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
