"""Small SnowLuma/Qzone protocol adapter; no plugin scheduler or LLM tools.

Protocol references and review notes: docs/qq_companion/q6.md.
Cookies stay in memory, redirects and automatic write retries are disabled.
"""
from __future__ import annotations

from http.cookies import SimpleCookie
import json
import re
import time

import httpx


class QzoneError(RuntimeError):
    def __init__(self, code, *, uncertain=False, auth=False):
        self.code, self.uncertain, self.auth = code, uncertain, auth
        super().__init__(code)  # Never include raw server text/URL/Cookie.


def parse_response(text):
    text = text.strip()
    if not text.startswith("{"):
        match = re.fullmatch(r"(?:[\w.]+)?\s*\((.*)\)\s*;?", text, re.S)
        if not match:
            raise QzoneError("qzone_non_json")
        text = match[1]
    try:
        value = json.loads(text)
    except ValueError:
        raise QzoneError("qzone_invalid_json") from None
    if not isinstance(value, dict):
        raise QzoneError("qzone_invalid_object")
    return value


def gtk(key):
    value = 5381
    for char in key:
        value += (value << 5) + ord(char)
    return value & 0x7fffffff


class OneBotHTTP:
    ACTIONS = {"get_login_info", "get_cookies", "get_group_list", "get_group_member_list", "get_friend_list"}

    def __init__(self, token="", *, transport=None):
        self.http = httpx.AsyncClient(base_url="http://127.0.0.1:3000", trust_env=False,
            timeout=10, follow_redirects=False, transport=transport,
            headers={"Authorization": "Bearer " + token} if token else {})

    async def call(self, action, **params):
        if action not in self.ACTIONS:
            raise ValueError("qzone_onebot_action_forbidden")
        try:
            response = await self.http.post("/" + action, json=params)
            if response.status_code in {401, 403}:
                raise QzoneError("onebot_auth", auth=True)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict) or value.get("retcode") != 0:
                raise QzoneError("onebot_action_failed")
            return value.get("data")
        except (httpx.HTTPError, ValueError):
            raise QzoneError("onebot_unavailable") from None

    async def close(self):
        await self.http.aclose()


class QzoneClient:
    BASE = "https://user.qzone.qq.com"
    ENDPOINTS = {
        "publish": BASE + "/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6",
        "list": BASE + "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6",
        "detail": BASE + "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6",
        "comment": BASE + "/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds",
        "like": BASE + "/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app",
        "delete": "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6",
    }

    def __init__(self, onebot, account, *, transport=None):
        self.onebot, self.account = onebot, str(account)
        self.cookies, self.token, self.loaded = {}, 0, 0.0
        self.http = httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20, transport=transport)

    async def login(self):
        info = await self.onebot.call("get_login_info")
        if not isinstance(info, dict) or str(info.get("user_id")) != self.account:
            raise QzoneError("qzone_account_mismatch", auth=True)
        data = await self.onebot.call("get_cookies", domain="user.qzone.qq.com")
        try:
            parsed = SimpleCookie(str(data.get("cookies", "")))
            cookies = {k: v.value for k, v in parsed.items()}
            uin = str(int(cookies.get("uin", "").lstrip("oO")))
        except (AttributeError, ValueError):
            raise QzoneError("qzone_invalid_cookie", auth=True) from None
        if uin != self.account or not all(cookies.get(k) for k in ("skey", "p_skey")):
            raise QzoneError("qzone_missing_cookie", auth=True)
        self.cookies = {k: cookies[k] for k in ("uin", "skey", "p_skey")}
        self.token, self.loaded = gtk(cookies["p_skey"]), time.monotonic()
        return {k: "present" for k in self.cookies}

    async def request(self, operation, *, data=None, params=None):
        if not self.cookies or time.monotonic() - self.loaded > 300:
            await self.login()  # Before the write; never retry a submitted write.
        write = operation not in {"list", "detail"}
        try:
            response = await self.http.request("POST" if write else "GET", self.ENDPOINTS[operation],
                params={"g_tk": self.token, **(params or {})}, data=data,
                headers={"Cookie": "; ".join(k + "=" + v for k, v in self.cookies.items()),
                         "Referer": self.BASE + "/" + self.account, "Origin": self.BASE})
            if response.status_code in {401, 403} or 300 <= response.status_code < 400:
                self.cookies = {}
                raise QzoneError("qzone_auth_or_permission", auth=True, uncertain=write)
            if response.status_code != 200:
                raise QzoneError("qzone_http_error", uncertain=write)
            if len(response.content) > 4_000_000:
                raise QzoneError("qzone_oversized_response", uncertain=write)
            try:
                result = parse_response(response.text)
            except QzoneError:
                raise QzoneError("qzone_invalid_response", uncertain=write) from None
            code = result.get("code", result.get("ret"))
            if str(code) != "0":
                auth = str(code) in {"-3000", "-10000", "-100", "-3", "100000"}
                if auth:
                    self.cookies = {}
                raise QzoneError("qzone_rejected_" + re.sub(r"[^0-9-]", "", str(code))[:12], auth=auth)
            return result
        except httpx.HTTPError:
            raise QzoneError("qzone_transport", uncertain=write) from None

    async def list_posts(self, target=None, *, limit=10):
        target = str(target or self.account)
        value = await self.request("list", params={"uin": target, "pos": 0, "num": max(1, min(20, limit)),
            "format": "json", "ftype": 0, "sort": 0, "replynum": 20, "need_comment": 1, "code_version": 1})
        raw = value.get("msglist")
        if raw is None:
            raw = value.get("data", {}).get("msglist") if isinstance(value.get("data"), dict) else None
        if raw is None and value.get("total") == 0:
            raw = []
        if not isinstance(raw, list):
            raise QzoneError("qzone_missing_list")
        return [dict(r, uin=str(r.get("uin", target))) for r in raw if isinstance(r, dict)]

    async def detail(self, target, tid):
        return await self.request("detail", params={"uin": str(target), "tid": tid, "format": "json"})

    async def publish(self, text, audience):
        visibility = audience["visibility"]
        friends = audience.get("friends", [])
        if visibility not in {"self", "allowlist"} or (visibility == "allowlist" and not friends):
            raise ValueError("qzone_audience_required")
        value = await self.request("publish", params={"uin": self.account}, data={
            "con": text, "hostuin": self.account, "syn_tweet_verson": "1", "paramstr": "1", "who": "1",
            "feedversion": "1", "ver": "1", "ugc_right": "64" if visibility == "self" else "16",
            "allow_uins": "|".join(friends) if visibility == "allowlist" else "",
            "to_sign": "0", "code_version": "1", "format": "json", "qzreferrer": self.BASE + "/" + self.account})
        tid = value.get("tid") or (value.get("data", {}).get("tid") if isinstance(value.get("data"), dict) else None)
        if not tid or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(tid)):
            raise QzoneError("qzone_missing_post_id", uncertain=True)
        return str(tid)

    async def delete(self, tid):
        return await self.request("delete", data={"uin": self.account,
            "topicId": f"{self.account}_{tid}__1", "feedsType": "0", "feedsFlag": "0", "feedsKey": tid,
            "feedsAppid": "311", "feedsTime": str(int(time.time())), "fupdate": "1", "ref": "feeds"})

    async def comment(self, target, tid, text):
        return await self.request("comment", data={"topicId": f"{target}_{tid}__1", "uin": self.account,
            "hostUin": target, "feedsType": "100", "inCharset": "utf-8", "outCharset": "utf-8",
            "plat": "qzone", "source": "ic", "platformid": "52", "format": "json", "ref": "feeds", "content": text})

    async def like(self, target, tid):
        link = f"http://user.qzone.qq.com/{target}/mood/{tid}"
        return await self.request("like", data={"opuin": self.account, "unikey": link, "curkey": link,
            "appid": "311", "from": "1", "typeid": "0", "abstime": str(int(time.time())),
            "fid": tid, "active": "0", "format": "json", "fupdate": "1"})

    async def close(self):
        self.cookies.clear()
        await self.http.aclose()
        await self.onebot.close()
