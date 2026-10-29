"""One-shot loopback form for importing an operator's existing NetEase login."""

from __future__ import annotations

import argparse
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.media_api.netease_auth import LoginError, NetEaseLogin, save_session, session_path


def import_cookie(music_u: str, csrf: str = "") -> dict:
    music_u, csrf = music_u.strip(), csrf.strip()
    if (not music_u or len(music_u) > 30000 or len(csrf) > 1000 or
            any(c in music_u + csrf for c in ";\r\n\0")):
        raise LoginError("netease_cookie_invalid")
    cookie = "MUSIC_U=" + music_u + ("; __csrf=" + csrf if csrf else "")
    client = NetEaseLogin(cookie)
    try:
        account = client.account()
    finally:
        client.close()
    if not account["logged_in"]:
        raise LoginError("netease_session_verification_failed")
    save_session(cookie)
    return account


def make_server(port: int = 0) -> HTTPServer:
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, *args):
            pass  # Never log form bodies, Cookie values or browser headers.

        def send_page(self, message: str = "", *, success: bool = False, status: int = 200):
            form = "" if success else f"""
<p>先在<a href="https://music.163.com/" target="_blank" rel="noreferrer">网易云官网</a>登录 VIP 账号。</p>
<ol><li>按 F12 打开开发者工具，切换到「应用 / Application」（可能在顶部 » 菜单内）。</li>
<li>左侧展开「存储 / Storage → Cookie / Cookies」，选择 https://music.163.com。</li>
<li>在表格里找到 MUSIC_U，复制它的「值 / Value」，粘贴到下面第一栏。</li>
<li>如果有 __csrf，再复制它的值到第二栏，点击「验证并加密保存」。</li></ol>
<form method="post" action="/save" autocomplete="off">
<input type="hidden" name="token" value="{token}">
<label>MUSIC_U 的值（必填）<input type="password" name="music_u" required maxlength="30000" autocomplete="off" spellcheck="false"></label>
<label>__csrf 的值（如有）<input type="password" name="csrf" maxlength="1000" autocomplete="off" spellcheck="false"></label>
<button type="submit">验证并加密保存</button></form>
<p>只粘贴 Value，不包含字段名。不需要账号密码，也不要把 Cookie 发到聊天里。</p>
<p>凭据只发送到本机导入程序及网易云官方接口，验证后用 Windows 当前用户加密保存。
后台机器人和模型不会启动。</p>"""
            page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>网易云账号导入</title>
<style>body{{max-width:740px;margin:40px auto;padding:0 24px;font:16px/1.7 system-ui;color:#17202a}}
label{{display:block;margin:18px 0}}input[type=password]{{display:block;box-sizing:border-box;width:100%;padding:10px}}
button{{padding:10px 24px;cursor:pointer}}.message{{padding:16px;background:#edf4fb;word-break:break-all}}</style>
<h1>网易云账号导入</h1><p class="message">{html.escape(message or '请在浏览器完成登录，然后按下面步骤导入。')}</p>
{form}</html>""".encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            # A plain form POST under no-referrer can send Origin: null,
            # including in Edge. Preserve the same-origin source required by
            # the form guard; external links still use rel=noreferrer.
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(page)

        def do_GET(self):
            if self.headers.get("Host") != self.server.expected_host or self.path != "/":
                self.send_error(404)
                return
            self.send_page()

        def do_POST(self):
            origin = self.headers.get("Origin")
            if (self.headers.get("Host") != self.server.expected_host or self.path != "/save" or
                    origin != "http://" + self.server.expected_host):
                print(json.dumps({"status": "request_rejected", "error": "local_origin_check_failed"}), flush=True)
                self.send_page("本机页面来源校验未通过，请重新打开导入首页后提交。错误：local_origin_check_failed", status=403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= 65536 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self.send_error(400)
                return
            data = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
            if not secrets.compare_digest(data.get("token", [""])[0], token):
                print(json.dumps({"status": "request_rejected", "error": "local_form_expired"}), flush=True)
                self.send_page("导入页面已更新，请重新打开导入首页后提交。错误：local_form_expired", status=403)
                return
            try:
                account = import_cookie(data.get("music_u", [""])[0], data.get("csrf", [""])[0])
            except LoginError as exc:
                print(json.dumps({"status": "import_failed", "error": str(exc)}), flush=True)
                self.send_page("未保存：" + str(exc) + "。请确认已登录，并重新复制 Cookie 的值。", status=400)
                return
            except OSError:
                self.send_page("无法写入本机加密凭据，请告知助手检查存储目录。", status=500)
                return
            self.send_page("登录验证成功，已加密保存。后续启动大肥鱼会自动读取此账号。可关闭本页。", success=True)
            self.server.account = account
            for name in ("pending.dpapi", "last_response.dpapi", "qr.png"):
                (session_path().parent / "login" / name).unlink(missing_ok=True)
            print(json.dumps({"status": "saved", **account}), flush=True)

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.expected_host = f"127.0.0.1:{server.server_port}"
    server.account = None
    server.timeout = 1
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = make_server(args.port)
    print(json.dumps({"status": "import_ready", "url": "http://" + server.expected_host + "/"}), flush=True)
    deadline = time.monotonic() + 1800
    try:
        while server.account is None and time.monotonic() < deadline:
            server.handle_request()
        return 0 if server.account else 2
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
