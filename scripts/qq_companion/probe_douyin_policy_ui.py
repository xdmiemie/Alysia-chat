"""Offline console check: long-video percentage, quota preview and save payload."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    from playwright.sync_api import sync_playwright
    from services.douyin_worker.config import DouyinConfig
    from integrations.qq_astrbot.plugin.web_control import DOUYIN_FIELDS

    config = DouyinConfig().model_dump()
    fixture = {"fields": [], "douyin": [dict(key=k, name=name, value=config[k], type=type(config[k]).__name__,
        **({"min": 0, "max": 100} if k == "long_video_percent" else {})) for k, name in DOUYIN_FIELDS.items()],
        "timezone": config["timezone"], "revision": "synthetic", "followup_probabilities": [],
        "whitelist": {"rows": [], "platforms": [], "enabled": True, "revision": "synthetic"},
        "douyin_whitelist": {"rows": [], "account": None, "limit": 20, "revision": "synthetic"}}
    script = "const fixture=" + json.dumps(fixture, ensure_ascii=False) + ";" + """
    window.syntheticSaves=[];
    window.AstrBotPluginView={ready:()=>Promise.resolve(),
      apiGet:async route=>route.endsWith('overview')?{gpu:'ready'}:route.endsWith('settings')?fixture:{rows:[],total:0,page:1},
      apiPost:async (route,body)=>{window.syntheticSaves.push({route,body});
        if(route.endsWith('settings'))for(const field of fixture.douyin)field.value=body.douyin[field.key];
        return {message:'synthetic saved'};}};
    """
    output = ROOT / "storage/temporary/douyin-policy-ui"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
        page.route("https://**", lambda route: route.abort())
        page.route("http://**", lambda route: route.abort())
        page.add_init_script(script)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto((ROOT / "integrations/qq_astrbot/plugin/views/console/index.html").as_uri())
        page.get_by_role("button", name="应用设置", exact=True).click()
        percent = page.get_by_label("长视频额度占比（%）", exact=True)
        assert percent.input_value() == "40"
        assert percent.get_attribute("min") == "0" and percent.get_attribute("max") == "100"
        preview = page.locator("#long-video-quota-preview")
        assert "最多 8 条长视频" in preview.inner_text()
        assert "超过 600 秒直接跳过" in preview.inner_text()
        percent.fill("50")
        page.get_by_label("每小时视频数", exact=True).fill("5")
        assert "最多 2 条长视频" in preview.inner_text()
        page.get_by_role("button", name="保存应用设置", exact=True).click()
        page.get_by_text("synthetic saved", exact=True).wait_for()
        assert page.evaluate("syntheticSaves.at(-1).body.douyin.long_video_percent") == 50
        page.get_by_role("button", name="刷新数据", exact=True).click()
        assert percent.input_value() == "50"
        percent.fill("0")
        assert "最多 0 条长视频" in preview.inner_text()
        page.screenshot(path=str(output / "settings.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print("DOUYIN_POLICY_UI_PASS")


if __name__ == "__main__":
    main()
