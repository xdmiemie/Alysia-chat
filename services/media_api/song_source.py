"""Bounded NetEase Cloud Music search and authorized audio acquisition."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

_PUBLIC_KEY = int("010001", 16)
_MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725"
    "152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312"
    "ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424"
    "d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7", 16)
_NONCE = b"0CoJUm6Qyw8W8jud"
_IV = b"0102030405060708"
_SONG_LINK = re.compile(r"(?:https?://)?music\.163\.com/(?:#?/)?song\?id=(\d{1,12})")
_AUDIO_HOST = re.compile(r"(?:^|\.)(?:music\.126\.net|music\.163\.com)$")
RETRYABLE_SOURCE_STATUS = frozenset({429, 500, 502, 503, 504})


class SongSourceError(ValueError):
    """A safe error code, without URLs, cookies or upstream response bodies."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def classify_search_error(exc: Exception) -> SongSourceError | None:
    if isinstance(exc, SongSourceError):
        return exc
    if isinstance(exc, requests.exceptions.SSLError):
        return SongSourceError("source_tls_error")
    if isinstance(exc, requests.Timeout):
        return SongSourceError("source_timeout", retryable=True)
    if isinstance(exc, requests.exceptions.ChunkedEncodingError):
        return SongSourceError("source_response_interrupted", retryable=True)
    if isinstance(exc, requests.ConnectionError):
        return SongSourceError("source_connection_error", retryable=True)
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        code = f"source_http_{status}" if isinstance(status, int) else "source_http_error"
        return SongSourceError(code, retryable=status in RETRYABLE_SOURCE_STATUS)
    if isinstance(exc, ValueError):
        return SongSourceError("song_not_found" if str(exc) == "song_not_found" else "source_invalid_response")
    return None


@dataclass(frozen=True)
class Song:
    source: str
    song_id: int
    title: str
    artists: str
    duration_seconds: float
    available: bool
    source_version: str = "weapi-v1"
    album: str = ""
    aliases: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {"source": self.source, "song_id": self.song_id, "title": self.title,
                "artists": self.artists, "duration_seconds": self.duration_seconds,
                "available": self.available, "source_version": self.source_version,
                "album": self.album, "aliases": list(self.aliases)}


def parse_song_id(value: str) -> int | None:
    value = value.strip()
    if value.isdecimal() and 0 < int(value) < 10**12:
        return int(value)
    match = _SONG_LINK.fullmatch(value)
    return int(match.group(1)) if match else None


def _aes(data: bytes, key: bytes) -> str:
    encrypted = AES.new(key, AES.MODE_CBC, _IV).encrypt(pad(data, 16))
    return base64.b64encode(encrypted).decode("ascii")


def weapi_form(payload: dict[str, Any]) -> dict[str, str]:
    secret = secrets.token_bytes(8).hex().encode("ascii")
    first = _aes(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(), _NONCE)
    params = _aes(first.encode("ascii"), secret)
    enc_key = format(pow(int(secret[::-1].hex(), 16), _PUBLIC_KEY, _MODULUS), "x").zfill(256)
    return {"params": params, "encSecKey": enc_key}


class NetEaseSource:
    version = "weapi-v1"

    def __init__(self, cookie: str = "", session: requests.Session | None = None) -> None:
        self.http = session or requests.Session()
        self.http.headers.update({"User-Agent": "Mozilla/5.0 AstrbotChatSong/1.0",
                                  "Referer": "https://music.163.com/"})
        if cookie:
            self.http.headers["Cookie"] = cookie

    @classmethod
    def from_environment(cls) -> "NetEaseSource":
        return cls(os.environ.get("ASTRBOT_CHAT_NETEASE_COOKIE") or os.environ.get("ALYSIA_NETEASE_COOKIE", ""))

    def _post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.http.post("https://music.163.com/weapi/" + route,
                                  data=weapi_form(payload), timeout=(5, 15), allow_redirects=False)
        try:
            if response.is_redirect:
                raise SongSourceError("source_redirect_refused")
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise SongSourceError("source_invalid_response") from exc
            if not isinstance(data, dict):
                raise SongSourceError("source_invalid_response")
            if data.get("code") != 200:
                code = data.get("code")
                label = f"source_code_{code}" if type(code) is int else "source_rejected"
                raise SongSourceError(label, retryable=code in RETRYABLE_SOURCE_STATUS if type(code) is int else False)
            return data
        finally:
            close = getattr(response, "close", None)
            if close:
                close()

    @staticmethod
    def _song(data: dict[str, Any]) -> Song:
        artists = data.get("ar") or data.get("artists") or []
        title = str(data.get("name") or "")[:120]
        names = ", ".join(str(item.get("name") or "")[:60] for item in artists[:5]
                          if isinstance(item, dict))[:160]
        album = data.get("al") or data.get("album") or {}
        aliases = data.get("alia") or data.get("alias") or data.get("transNames") or []
        return Song("netease", int(data["id"]), title, names,
                    float(data.get("dt") or data.get("duration") or 0) / 1000,
                    bool(data.get("dt") or data.get("duration")),
                    album=str(album.get("name") or "")[:160] if isinstance(album, dict) else "",
                    aliases=tuple(str(item)[:120] for item in aliases[:5]) if isinstance(aliases, list) else ())

    def search(self, query: str, page: int = 1, *, limit: int = 20) -> list[Song]:
        if not query.strip() or len(query) > 120 or not 1 <= page <= 5 or not 1 <= limit <= 20:
            raise ValueError("invalid song search")
        data = self._post("search/get", {"s": query, "type": 1,
                                         "limit": limit, "offset": (page - 1) * limit,
                                         "total": "true"})
        songs = (data.get("result") or {}).get("songs") or []
        candidates = [self._song(item) for item in songs[:limit] if isinstance(item, dict)]
        playable = self._playable_ids([song.song_id for song in candidates])
        return [replace(song, available=song.available and song.song_id in playable)
                for song in candidates]

    def detail(self, song_id: int) -> Song:
        if not 0 < song_id < 10**12:
            raise ValueError("invalid song ID")
        data = self._post("v3/song/detail", {"c": json.dumps([{"id": song_id}]),
                                                    "ids": f"[{song_id}]"})
        songs = data.get("songs") or []
        if not songs or int(songs[0].get("id", 0)) != song_id:
            raise ValueError("song is unavailable")
        song = self._song(songs[0])
        return replace(song, available=song.available and song_id in self._playable_ids([song_id]))

    def _playable_ids(self, song_ids: list[int]) -> set[int]:
        if not song_ids:
            return set()
        data = self._post("song/enhance/player/url/v1",
                          {"ids": json.dumps(song_ids, separators=(",", ":")),
                           "level": "standard", "encodeType": "mp3"})
        return {int(item["id"]) for item in data.get("data") or []
                if isinstance(item, dict) and item.get("url") and item.get("code") == 200
                and not item.get("freeTrialInfo") and not (item.get("freeTrialPrivilege") or {}).get("listenType")}

    def playable_url(self, song_id: int) -> str:
        data = self._post("song/enhance/player/url/v1", {"ids": f"[{song_id}]",
                                                          "level": "standard", "encodeType": "mp3"})
        items = data.get("data") or []
        item = next((row for row in items if int(row.get("id", 0)) == song_id), None)
        if (not item or not item.get("url") or item.get("code") != 200 or item.get("freeTrialInfo") or
                (item.get("freeTrialPrivilege") or {}).get("listenType")):
            raise PermissionError("song audio is unavailable for this account")
        url = str(item["url"])
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not _AUDIO_HOST.search(parsed.hostname or ""):
            raise ValueError("untrusted audio host")
        return parsed._replace(scheme="https").geturl()

    def download(self, song_id: int, destination: Path, *, max_bytes: int = 30 * 1024 * 1024) -> str:
        url = self.playable_url(song_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        metadata = destination.with_suffix(destination.suffix + ".part.json")
        previous = json.loads(metadata.read_text(encoding="utf-8")) if metadata.is_file() else {}
        offset = partial.stat().st_size if partial.is_file() and previous.get("url") == url else 0
        headers = {"User-Agent": "Mozilla/5.0 AstrbotChatSong/1.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            if previous.get("etag"):
                headers["If-Range"] = previous["etag"]
        digest = hashlib.sha256()
        size = offset
        try:
            # The login Cookie belongs only on music.163.com API calls.
            with requests.get(url, headers=headers,
                              stream=True, timeout=(5, 30), allow_redirects=False) as response:
                response.raise_for_status()
                if response.is_redirect or not response.headers.get("Content-Type", "").lower().startswith("audio/"):
                    raise ValueError("source did not return audio")
                resumed = (offset > 0 and response.status_code == 206 and
                           response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"))
                if not resumed:
                    size = 0
                else:
                    with partial.open("rb") as existing:
                        for block in iter(lambda: existing.read(1024 * 1024), b""):
                            digest.update(block)
                metadata.write_text(json.dumps({"url": url, "etag": response.headers.get("ETag", "")}),
                                    encoding="utf-8")
                with partial.open("ab" if resumed else "wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError("source audio exceeds size limit")
                        digest.update(chunk)
                        output.write(chunk)
            if size < 1024:
                raise ValueError("source audio is empty")
            partial.replace(destination)
            metadata.unlink(missing_ok=True)
            return digest.hexdigest()
        except ValueError:
            partial.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            raise

