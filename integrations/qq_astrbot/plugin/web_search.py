"""Shared Tavily transport. Every process spends from one durable free-only budget."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

import httpx
import jsonschema
import yaml

from .inference_queue import state_directory
from .prompt_loader import load_prompts, prompt_path

SEARCH_NAMES = frozenset({"web_search", "read_webpage"})
FREE_CREDITS = 1000


def settings():
    path = Path(os.environ.get("ASTRBOT_CHAT_SEARCH_CONFIG", prompt_path().parents[2] / "configs/qq_companion/search.yaml"))
    config = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or type(config.get("enabled")) is not bool:
        raise ValueError("invalid_search_config")
    if type(config.get("monthly_credit_limit")) is not int or not 0 <= config["monthly_credit_limit"] <= FREE_CREDITS:
        raise ValueError("invalid_free_credit_limit")
    return config


def tool_schemas(prompts=None):
    prompts = prompts or load_prompts()
    return [
        {"type": "function", "function": {"name": "web_search", "description": prompts["tool_web_search"],
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 300,
                          "description": "需要查询的公开信息，不能包含私聊、个人记忆或密钥。"}},
                "required": ["query"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "read_webpage", "description": prompts["tool_read_webpage"],
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "minLength": 1, "maxLength": 2000,
                        "description": "需要阅读的公开网页完整 HTTP(S) 地址。"}},
                "required": ["url"], "additionalProperties": False}}},
    ]


def rejected(reason):
    return {"ok": False, "status": "unavailable", "reason": reason, "retryable": False,
            "continue_without_search": True,
            "instruction": "本轮停止联网，继续完成原任务或正常回复；仅依靠已有信息，对不确定的事实明确保留，不编造搜索结果。"}


def public_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


def credit_number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid_usage_response")
    return float(value)


def free_account(data):
    """Unknown billing state is a refusal, never permission to spend."""
    account = data["account"]
    plan = account["current_plan"].strip().lower()
    usage, limit = credit_number(account["plan_usage"]), credit_number(account["plan_limit"])
    paygo = credit_number(account["paygo_usage"])
    # The live Researcher API returns null here (and for an uncapped key).
    # Null is not a paid allowance. Accept it only together with a known free
    # plan, zero paid usage, and the independent account/local 1,000-credit caps.
    paygo_limit = 0 if account["paygo_limit"] is None else credit_number(account["paygo_limit"])
    if plan not in {"researcher", "free"} or not 0 < limit <= FREE_CREDITS or paygo or paygo_limit:
        raise ValueError("free_plan_with_paygo_disabled_required")
    key = data["key"]
    key_used = credit_number(key["usage"])
    # Tavily may return null for an API key without an additional key-level cap.
    key_limit = key["limit"]
    remaining = limit - usage
    if key_limit is not None:
        remaining = min(remaining, credit_number(key_limit) - key_used)
    return usage, limit, remaining


class SearchService:
    def __init__(self, directory=None, *, config=None, api_key=None, transport=None, clock=time.time):
        self.directory = Path(directory) if directory is not None else state_directory()
        self.config, self.api_key, self.transport, self.clock = config, api_key, transport, clock
        self.path = self.directory / "web_search.sqlite3"

    def configuration(self):
        try:
            config = self.config if self.config is not None else settings()
            if not config.get("enabled"):
                return None, "search_disabled"
            limit = config["monthly_credit_limit"]
            if type(limit) is not int or not 0 <= limit <= FREE_CREDITS:
                return None, "invalid_search_config"
            key = self.api_key if self.api_key is not None else os.environ.get("TAVILY_API_KEY", "").strip()
            if not key and (self.directory / "tavily_api_key").is_file():
                key = (self.directory / "tavily_api_key").read_text(encoding="utf-8-sig").strip()
            if not key or any(c.isspace() for c in key):
                return None, "tavily_key_missing"
            return (key, limit), ""
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
            return None, "search_configuration_unavailable"

    def configured(self):
        return self.configuration()[0] is not None

    def month(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m")

    @contextmanager
    def connection(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS search_quota (month TEXT PRIMARY KEY, "
                           "reserved REAL NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '')")
                yield db
        finally:
            db.close()

    def status(self):
        configuration, reason = self.configuration()
        with self.connection() as db:
            row = db.execute("SELECT reserved,blocked_until,reason FROM search_quota WHERE month=?", (self.month(),)).fetchone()
        used, until, blocked = row or (0, 0, "")
        limit = configuration[1] if configuration else 0
        reason = reason or (blocked if until > self.clock() else "") or ("free_quota_exhausted" if used + 1 > limit else "")
        return {"provider": "tavily", "month_utc": self.month(), "configured": configuration is not None,
                "reserved_credits": used, "monthly_limit": limit, "available": not bool(reason), "reason": reason}

    def reserve(self, remote_used, remote_limit, remote_remaining, limit):
        month = self.month()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO search_quota(month) VALUES(?)", (month,))
            used, until, reason = db.execute("SELECT reserved,blocked_until,reason FROM search_quota WHERE month=?", (month,)).fetchone()
            if until > self.clock():
                return reason
            used = max(used, remote_used)
            # Reserve the upper bound (1 credit) for both basic search and a
            # single-page basic extract. Never refund an ambiguous timeout.
            if used + 1 > min(limit, remote_limit) or remote_remaining < 1:
                db.execute("UPDATE search_quota SET reserved=?,reason='free_quota_exhausted' WHERE month=?", (used, month))
                return "free_quota_exhausted"
            db.execute("UPDATE search_quota SET reserved=?,reason='' WHERE month=?", (used + 1, month))
        return ""

    def block(self, reason, *, monthly=False):
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=timezone.utc).timestamp()
        with self.connection() as db:
            db.execute("INSERT INTO search_quota(month,blocked_until,reason) VALUES(?,?,?) "
                       "ON CONFLICT(month) DO UPDATE SET blocked_until=excluded.blocked_until,reason=excluded.reason",
                       (self.month(), end if monthly else self.clock() + 60, reason))

    async def http(self, client, method, route, body=None):
        async with client.stream(method, "https://api.tavily.com" + route, json=body) as response:
            response.raise_for_status()
            raw = bytearray()
            async for part in response.aiter_bytes():
                raw.extend(part)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError("search_response_too_large")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("invalid_search_response")
            return value

    async def account_status(self):
        """Read-only setup check. Never issues a search or reveals the API key."""
        configuration, reason = self.configuration()
        if not configuration:
            return rejected(reason)
        try:
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + configuration[0]},
                    timeout=10, follow_redirects=False, transport=self.transport) as client:
                data = await self.http(client, "GET", "/usage")
            used, limit, remaining = free_account(data)
            local = self.status()
            available = local["available"] and remaining >= 1 and max(local["reserved_credits"], used) + 1 <= min(configuration[1], limit)
            return {"ok": True, "plan": data["account"]["current_plan"], "account_used": used,
                    "account_limit": limit, "account_remaining": remaining,
                    "paygo_usage": data["account"]["paygo_usage"], "paygo_limit": data["account"]["paygo_limit"],
                    **local, "available": available, "reason": local["reason"] or ("" if available else "free_quota_exhausted")}
        except Exception as exc:
            return rejected(str(exc) if isinstance(exc, ValueError) and str(exc).startswith("free_plan_") else "account_usage_unavailable")

    async def execute(self, name, arguments):
        try:
            schema = next((s["function"]["parameters"] for s in tool_schemas() if s["function"]["name"] == name), None)
            if schema is None:
                return rejected("unknown_search_tool")
            jsonschema.validate(arguments, schema)
            if name == "web_search":
                query = arguments["query"].strip()
                if not query:
                    return rejected("invalid_search_query")
                route, body = "/search", {"query": query, "search_depth": "basic", "auto_parameters": False,
                    "max_results": 5, "include_answer": False, "include_raw_content": False, "include_usage": True}
            else:
                if not public_url(arguments["url"]):
                    return rejected("public_webpage_required")
                route, body = "/extract", {"urls": [arguments["url"]], "extract_depth": "basic",
                    "format": "text", "include_images": False, "include_usage": True}
            configuration, reason = self.configuration()
            if not configuration:
                return rejected(reason)
            local = self.status()
            if not local["available"]:
                return rejected(local["reason"])
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + configuration[0]},
                    timeout=httpx.Timeout(12, connect=4), follow_redirects=False, transport=self.transport) as client:
                data = await self.http(client, "GET", "/usage")
                try:
                    used, limit, remaining = free_account(data)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    reason = "free_plan_with_paygo_disabled_required" if str(exc) == "free_plan_with_paygo_disabled_required" else "account_usage_unavailable"
                    self.block(reason)
                    return rejected(reason)
                reason = self.reserve(used, limit, remaining, configuration[1])
                if reason:
                    return rejected(reason)
                result = await self.http(client, "POST", route, body)
            rows = result.get("results")
            if not isinstance(rows, list):
                return rejected("invalid_search_response")
            results = [{"title": str(row.get("title") or "")[:200], "url": row["url"],
                        "content": str(row.get("raw_content" if name == "read_webpage" else "content") or "")[:6000 if name == "read_webpage" else 900]}
                       for row in rows[:1 if name == "read_webpage" else 5]
                       if isinstance(row, dict) and isinstance(row.get("url"), str) and public_url(row["url"])]
            return {"ok": True, "status": "completed", "provider": "tavily", "untrusted_external_content": True,
                    "results": results}
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reason = "free_quota_exhausted" if status in {402, 432, 433} else "search_temporarily_unavailable"
            try:
                self.block(reason, monthly=status in {402, 432, 433})
            except (OSError, sqlite3.Error):
                pass
            return rejected(reason)
        except jsonschema.ValidationError:
            return rejected("invalid_search_arguments")
        except Exception:
            # Do not leak URLs, API keys or upstream response bodies to logs/model.
            return rejected("search_temporarily_unavailable")
