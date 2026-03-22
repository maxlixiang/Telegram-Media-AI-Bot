import json
import math
import re
import sys
from typing import Any
from urllib.parse import urlparse

import requests


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"


def extract_tweet_id(url: str) -> str | None:
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(path_parts):
        if part == "status" and index + 1 < len(path_parts):
            candidate = path_parts[index + 1]
            if candidate.isdigit():
                return candidate
    match = re.search(r"/status/(\d+)", url)
    return match.group(1) if match else None


def get_syndication_token(tweet_id: str) -> str:
    return ((int(tweet_id) / 1e15) * math.pi).__format__("f").replace("0", "").replace(".", "")


def build_metadata_url(tweet_id: str) -> str:
    token = get_syndication_token(tweet_id)
    return f"{SYNDICATION_URL}?id={tweet_id}&lang=en&token={token}"


def fetch_payload(metadata_url: str) -> tuple[int | None, str, dict[str, Any] | None]:
    response = requests.get(
        metadata_url,
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    body_preview = response.text[:500]
    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = None
    return response.status_code, body_preview, payload


def collect_video_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_signatures: set[tuple[str, ...]] = set()

    def add_candidate(source: str, raw_variants: list[dict[str, Any]]) -> None:
        normalized = normalize_variants(raw_variants)
        signature = tuple(sorted(item["url"] for item in normalized if item.get("url")))
        if not normalized or signature in seen_signatures:
            return
        seen_signatures.add(signature)
        candidates.append(
            {
                "source": source,
                "variants": raw_variants,
            }
        )

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

    quoted_tweet = payload.get("quoted_tweet")
    if isinstance(quoted_tweet, dict):
        quoted_video = quoted_tweet.get("video")
        if isinstance(quoted_video, dict) and isinstance(quoted_video.get("variants"), list):
            add_candidate("quoted_tweet.video", quoted_video["variants"])

    return candidates


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


def select_best_mp4_variant(variants: list[dict[str, Any]]) -> dict[str, Any] | None:
    mp4_variants = [item for item in variants if item.get("content_type") == "video/mp4"]
    if not mp4_variants:
        return None
    return sorted(mp4_variants, key=lambda item: item.get("bitrate", -1), reverse=True)[0]


def probe_twitter_url(url: str) -> int:
    print(f"Input URL: {url}")

    tweet_id = extract_tweet_id(url)
    print(f"tweet_id extracted: {bool(tweet_id)}")
    if tweet_id:
        print(f"tweet_id: {tweet_id}")
    else:
        print("failure_point: tweet_id 提取失败")
        return 1

    metadata_url = build_metadata_url(tweet_id)
    print(f"metadata_url: {metadata_url}")

    try:
        status_code, body_preview, payload = fetch_payload(metadata_url)
    except Exception as exc:
        print(f"http_error: {exc}")
        print("failure_point: metadata URL 不可用")
        return 2

    print(f"http_status: {status_code}")
    print(f"body_preview: {body_preview[:300]}")

    if payload is None:
        print("payload_json: False")
        print("failure_point: metadata URL 不可用")
        return 3

    print("payload_json: True")

    candidates = collect_video_candidates(payload)
    print(f"media_detected: {bool(candidates)}")
    print(f"video_candidate_count: {len(candidates)}")

    if not candidates:
        print("single_video_detected: False")
        print("mp4_variants_found: False")
        print("failure_point: payload 里没有视频")
        return 4

    is_single_video = len(candidates) == 1
    print(f"single_video_detected: {is_single_video}")
    if not is_single_video:
        print("mp4_variants_found: False")
        print("failure_point: 非单个视频或媒体结构超出当前范围")
        return 5

    candidate = candidates[0]
    variants = normalize_variants(candidate["variants"])
    best_mp4 = select_best_mp4_variant(variants)

    print(f"variant_source: {candidate['source']}")
    print(f"variant_count: {len(variants)}")
    print(f"mp4_variants_found: {bool(best_mp4)}")

    if not best_mp4:
        has_hls = any(item.get("content_type") == "application/x-mpegURL" for item in variants)
        if has_hls:
            print("failure_point: 只有 HLS/m3u8 没有 mp4")
        else:
            print("failure_point: payload 里没有视频")
        return 6

    print(f"best_mp4_bitrate: {best_mp4.get('bitrate')}")
    print(f"best_mp4_url_preview: {best_mp4.get('url', '')[:160]}")
    print("probe_result: success")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python twitter_probe.py <twitter_or_x_status_url>")
        return 1
    return probe_twitter_url(sys.argv[1].strip())


if __name__ == "__main__":
    raise SystemExit(main())
