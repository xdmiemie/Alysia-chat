"""Public share-page adapter. No login cookies, signature forging or bypass."""
from __future__ import annotations

import html
import json
import re
from urllib.parse import unquote

from .vision_media import MediaError, fetch_url, validate_url

LINK = re.compile(r"https?://(?:[A-Za-z0-9-]+\.)?(?:douyin\.com|iesdouyin\.com)/[^\s<>\"，。！？）]+")


def resolve_douyin(url, config):
    raw, final, content_type = fetch_url(url, config, limit=4 * 1024 * 1024,
        domains=["douyin.com", "iesdouyin.com"])
    text = raw.decode("utf-8", errors="replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    metadata = {"source_url": final, "title": html.unescape(title[1]).strip()[:500] if title else "",
                "author": "", "media_url": None}
    if "html" not in content_type:
        raise MediaError("douyin_share_page_invalid")
    # Public embedded JSON changes independently of the project. Recognize
    # explicit payloads only; absence is metadata-only, never 'watched'.
    candidates = []
    match = re.search(r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>', text, re.S)
    if match:
        try:
            candidates.append(json.loads(unquote(html.unescape(match[1]))))
        except ValueError:
            pass
    for match in re.finditer(r'(?:window\.)?_ROUTER_DATA\s*=\s*', text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.end():])
            candidates.append(value)
        except ValueError:
            pass
    def visit(value, depth=0):
        if depth > 20:
            return
        if isinstance(value, list):
            for item in value[:100]:
                visit(item, depth + 1)
        elif isinstance(value, dict):
            video = value.get("video")
            if isinstance(video, dict):
                play = video.get("play_addr") or video.get("playAddr") or video.get("playApi")
                urls = play.get("url_list", []) if isinstance(play, dict) else [play] if isinstance(play, str) else []
                for address in urls:
                    if isinstance(address, str) and address.startswith("http"):
                        try:
                            validate_url(address, config["allowed_media_domains"])
                            metadata["media_url"] = address
                            metadata["title"] = str(value.get("desc") or metadata["title"])[:500]
                            metadata["author"] = str((value.get("author") or {}).get("nickname", ""))[:100]
                            break
                        except MediaError:
                            pass
            for item in value.values():
                visit(item, depth + 1)
    for candidate in candidates:
        visit(candidate)
    return metadata
