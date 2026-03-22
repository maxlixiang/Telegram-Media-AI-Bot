import json
import logging
import math
import re
from typing import Any
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TWITTER_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
TWITTER_RESTRICTED_ERROR = "Twitter 视频解析失败：该内容受限，当前仅支持公开单视频 tweet"


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

    raise RuntimeError("Twitter 链接解析失败，未提取到 tweet_id")


def get_syndication_token(tweet_id: str) -> str:
    return ((int(tweet_id) / 1e15) * math.pi).__format__("f").replace("0", "").replace(".", "")


def build_metadata_url(tweet_id: str) -> str:
    token = get_syndication_token(tweet_id)
    return f"{SYNDICATION_URL}?id={tweet_id}&lang=en&token={token}"


def fetch_tweet_payload(tweet_id: str, raw_url: str) -> tuple[str, dict[str, Any]]:
    metadata_url = build_metadata_url(tweet_id)
    response = requests.get(
        metadata_url,
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    body_preview = response.text[:500]
    logger.info(
        "Twitter metadata 响应 raw_url=%s tweet_id=%s metadata_url=%s status_code=%s body_preview=%s",
        raw_url,
        tweet_id,
        metadata_url,
        response.status_code,
        body_preview,
    )

    response.raise_for_status()
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError("Twitter 元信息请求失败") from exc

    return metadata_url, payload


def normalize_variants(raw_variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in raw_variants:
        if not isinstance(item, dict):
            continue
        variant_url = item.get("url") or item.get("src")
        content_type = item.get("content_type") or item.get("type")
        bitrate = item.get("bitrate", -1)
        if not variant_url or not content_type:
            continue
        normalized.append(
            {
                "url": variant_url,
                "content_type": content_type,
                "bitrate": bitrate if isinstance(bitrate, int) else -1,
            }
        )
    return normalized


def collect_video_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("__typename") == "TweetTombstone":
        raise RuntimeError(TWITTER_RESTRICTED_ERROR)

    candidates: list[dict[str, Any]] = []
    seen_signatures: set[tuple[str, ...]] = set()

    def add_candidate(source: str, raw_variants: list[dict[str, Any]]) -> None:
        normalized = normalize_variants(raw_variants)
        signature = tuple(sorted(item["url"] for item in normalized if item.get("url")))
        if not normalized or signature in seen_signatures:
            return
        seen_signatures.add(signature)
        candidates.append({"source": source, "variants": raw_variants})

    media_details = payload.get("mediaDetails")
    if isinstance(media_details, list):
        for item in media_details:
            if not isinstance(item, dict):
                continue
            variants = item.get("video_info", {}).get("variants")
            if variants:
                add_candidate("mediaDetails.video_info", variants)
            elif item.get("type") == "video" and item.get("video", {}).get("variants"):
                add_candidate("mediaDetails.video", item["video"]["variants"])

    video = payload.get("video")
    if isinstance(video, dict) and isinstance(video.get("variants"), list):
        add_candidate("payload.video", video["variants"])

    return candidates


def select_best_mp4_variant(variants: list[dict[str, Any]]) -> dict[str, Any] | None:
    mp4_variants = [item for item in variants if item.get("content_type") == "video/mp4"]
    if not mp4_variants:
        return None
    return sorted(mp4_variants, key=lambda item: item.get("bitrate", -1), reverse=True)[0]


def fetch_twitter_video_meta(url: str) -> dict[str, Any]:
    tweet_id = extract_tweet_id(url)
    metadata_url, payload = fetch_tweet_payload(tweet_id, url)

    candidates = collect_video_candidates(payload)
    media_detected = bool(candidates)
    logger.info(
        "Twitter 媒体识别 raw_url=%s tweet_id=%s metadata_url=%s media_detected=%s candidate_count=%s",
        url,
        tweet_id,
        metadata_url,
        media_detected,
        len(candidates),
    )

    if not candidates:
        raise RuntimeError(TWITTER_RESTRICTED_ERROR)
    if len(candidates) != 1:
        raise RuntimeError("Twitter 视频解析失败：当前仅支持公开单视频 tweet")

    variants = normalize_variants(candidates[0]["variants"])
    best_mp4 = select_best_mp4_variant(variants)
    logger.info(
        "Twitter 视频变体识别 raw_url=%s tweet_id=%s metadata_url=%s mp4_variants_found=%s",
        url,
        tweet_id,
        metadata_url,
        bool(best_mp4),
    )

    if not best_mp4:
        raise RuntimeError(TWITTER_RESTRICTED_ERROR)

    return {
        "tweet_id": tweet_id,
        "metadata_url": metadata_url,
        "video_url": best_mp4["url"],
        "bitrate": best_mp4["bitrate"],
        "content_type": best_mp4["content_type"],
        "headers": {"User-Agent": USER_AGENT},
    }


def download_file(url: str, output_path: str, headers: dict[str, str] | None = None) -> str:
    with requests.get(url, headers=headers or {}, stream=True, timeout=30) as response:
        response.raise_for_status()
        with open(output_path, "wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    output.write(chunk)
    return output_path
