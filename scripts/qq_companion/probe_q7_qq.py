"""Send original Q7 fixtures only to the explicitly authorized friend."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.qq_companion.configure import locate_snowluma_config, read_json
from scripts.qq_companion.q7_fixtures import generate

PEER = "1301049831"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kinds", nargs="+", choices=["image", "sticker", "gif", "video"],
                        default=["image", "sticker", "gif", "video"])
    args = parser.parse_args()
    config = read_json(locate_snowluma_config(os.getenv("QQ_COMPANION_SNOWLUMA_CONFIG")))
    server = next(s for s in config["networks"]["httpServers"] if s.get("port") == 3000)
    if server.get("host") not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("OneBot must be local")
    opener = build_opener(ProxyHandler({}))
    def call(action, payload):
        headers = {"Content-Type": "application/json"}
        if server.get("accessToken"):
            headers["Authorization"] = "Bearer " + server["accessToken"]
        request = Request("http://127.0.0.1:3000/" + action,
            data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
        with opener.open(request, timeout=60) as response:
            result = json.load(response)
        if result.get("retcode") != 0:
            raise RuntimeError("OneBot action failed: " + action + ": " + str(result.get("retcode")))
        return result.get("data")
    friends = call("get_friend_list", {})
    if PEER not in {str(row.get("user_id")) for row in friends}:
        raise RuntimeError("Authorized test peer is not a current friend")
    fixtures = generate(ROOT / "storage/temporary/q7-fixtures")
    report = {"started": time.time(), "peer": PEER, "outbound": [], "inbound": "requires actual peer return; not inferred from send success"}
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "qq_report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("peer") != PEER:
            raise RuntimeError("Existing report has a different peer")
    def save():
        (args.output / "qq_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for kind in args.kinds:
        if any(row["kind"] == kind and row["accepted"] for row in report["outbound"]):
            print(kind + ": already accepted; skipped", flush=True)
            continue
        fixture = fixtures[kind][0]
        path = Path(fixture["path"]).resolve()
        file = path.as_uri() if kind == "video" else "base64://" + base64.b64encode(path.read_bytes()).decode()
        payload = {"user_id": int(PEER), "message": [
            {"type": "text", "data": {"text": "【Q7测试素材·" + kind + "】请将这份素材发回给我，并问：" + fixture["question"]}},
            {"type": "video" if kind == "video" else "image", "data": {"file": file}}]}
        if kind == "sticker":
            payload["message"][-1]["data"]["sub_type"] = 1
        if kind == "video":
            instruction = call("send_private_msg", {"user_id": int(PEER), "message": [payload["message"][0]]})
            report.setdefault("instructions", []).append({"kind": kind, "message_id": instruction.get("message_id")})
            save()
            payload["message"] = [payload["message"][-1]]
        result = call("send_private_msg", payload)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        row = {"kind": kind, "accepted": bool(message_id), "message_id": message_id, "time": time.time()}
        if message_id:
            try:
                echoed = call("get_msg", {"message_id": message_id})
                row["record_available"] = bool(echoed)
            except Exception as exc:
                row["record_error"] = type(exc).__name__
        report["outbound"].append(row)
        save()
        print(kind + ": " + ("accepted" if message_id else "unconfirmed"), flush=True)
    return 0 if all(row["accepted"] for row in report["outbound"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
