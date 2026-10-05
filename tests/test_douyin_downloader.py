import unittest
from unittest.mock import MagicMock, mock_open, patch

import requests

from douyin_downloader import (
    DouyinDownloadError, _find_video, _video_urls,
    download_douyin_video, download_file, extract_aweme_id, fetch_douyin_video_meta,
)
from playwright.sync_api import Error as BrowserError


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

    def test_browser_reads_network_data_in_fresh_windows_context(self):
        item = {"aweme_id": "123", "video": {"play_addr": {"url_list": ["https://cdn.example/a"]}}}
        with patch("douyin_downloader.sync_playwright") as factory:
            playwright = factory.return_value.__enter__.return_value
            browser = playwright.chromium.launch.return_value
            context = browser.new_context.return_value
            page = context.new_page.return_value
            page.url = "https://www.douyin.com/video/123"
            context.cookies.return_value = []
            response = MagicMock()
            response.url = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
            response.status = 200
            response.headers = {"content-type": "application/json"}
            response.json.return_value = {"aweme_detail": item}
            page.goto.side_effect = lambda *args, **kwargs: page.on.call_args.args[1](response)
            meta = fetch_douyin_video_meta("https://v.douyin.com/short/")
            self.assertEqual(meta["aweme_id"], "123")
            self.assertIn("Windows NT", browser.new_context.call_args.kwargs["user_agent"])
            self.assertTrue(playwright.chromium.launch.call_args.kwargs["headless"])
            self.assertEqual(playwright.chromium.launch.call_args.kwargs["channel"], "chromium")
            context.add_cookies.assert_not_called()
            browser.close.assert_called_once()

    def test_browser_reads_executed_page_data_when_no_json_response(self):
        item = {"aweme_id": "123", "video": {"play_addr": {"url_list": ["https://cdn.example/a"]}}}
        with patch("douyin_downloader.sync_playwright") as factory:
            browser = factory.return_value.__enter__.return_value.chromium.launch.return_value
            page = browser.new_context.return_value.new_page.return_value
            page.url = "https://www.douyin.com/video/123"
            page.evaluate.return_value = [{"loaderData": {"item_list": [item]}}]
            self.assertEqual(fetch_douyin_video_meta(page.url)["video_urls"], ["https://cdn.example/a"])
            browser.close.assert_called_once()

    def test_browser_failure_releases_serialization_lock(self):
        from douyin_downloader import _BROWSER_LOCK
        with patch("douyin_downloader.sync_playwright") as factory:
            factory.return_value.__enter__.return_value.chromium.launch.side_effect = BrowserError("missing browser")
            with self.assertLogs("douyin_downloader", level="ERROR"), self.assertRaises(DouyinDownloadError):
                fetch_douyin_video_meta("https://www.douyin.com/video/123")
            self.assertFalse(_BROWSER_LOCK.locked())


if __name__ == "__main__":
    unittest.main()
