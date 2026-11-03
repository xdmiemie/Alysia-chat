"""Offline UI smoke test using synthetic state and an isolated headless browser."""
import json
from pathlib import Path
import tempfile
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from integrations.qq_astrbot.plugin.shared_memory import SharedMemory,ModuleDescriptor
from integrations.qq_astrbot.plugin.memory_policies import ModulePolicy


def main():
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory() as directory:
        s=SharedMemory(Path(directory)/"memory.db")
        s.register_module(ModuleDescriptor("reading","测试阅读模块",("note",)),ModulePolicy())
        s.append_short_term(bot="1",character="test",module="reading",kind="note",event_id="book",
            body="这是一条用于界面验收的合成阅读摘要。",visibility="shared")
        fixture={"rows":[],"total":0,"page":1,"inventory":s.get_inventory_stats("test"),"requests":[]}
        script="window.AstrBotPluginView={ready:()=>Promise.resolve(),apiGet:async(route)=>route.endsWith('overview')?{gpu:'ready'}:"+json.dumps(fixture,ensure_ascii=False)+",apiPost:async()=>({message:'synthetic saved'})};"
        output=ROOT/"storage/temporary/shared-memory-ui"
        output.mkdir(parents=True,exist_ok=True)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True, channel="msedge")
            page=browser.new_page(viewport={"width":1440,"height":1100},device_scale_factor=1)
            page.add_init_script(script)
            errors=[]
            page.on("pageerror",lambda error:errors.append(str(error)))
            page.goto((ROOT/"integrations/qq_astrbot/plugin/views/console/index.html").as_uri())
            page.get_by_role("button",name="记忆库",exact=True).click()
            page.get_by_text("测试阅读模块",exact=True).wait_for()
            assert "暂无请求，未统计占比" in page.locator("body").inner_text()
            page.screenshot(path=str(output/"inventory.png"),full_page=True)
            page.get_by_role("button",name="参数设置",exact=True).click()
            assert page.locator("#field-selected_history_limit").input_value()=="80"
            page.screenshot(path=str(output/"settings.png"),full_page=True)
            page.locator("#close-dialog").click()
            fixture["requests"]=[dict(id="synthetic-request",created=1,character="test",module="qq",scope="synthetic",
                payload=dict(model="synthetic",window=4096,total_input=800,reserved_output=512,memory_budget=256,
                    memory_tokens=160,common_tokens=8,wrapper_tokens=2,attribution=[
                        dict(module="qq",layer="short",tokens=50),dict(module="reading",layer="long",tokens=100)]))]
            page.evaluate("fixture=>window.AstrBotPluginView.apiGet=async route=>route.endsWith('overview')?{gpu:'ready'}:fixture",fixture)
            page.get_by_role("button",name="刷新数据",exact=True).click()
            page.get_by_text("50 / 1.22%",exact=True).wait_for()
            page.get_by_text("100 / 2.44%",exact=True).wait_for()
            page.screenshot(path=str(output/"request-usage.png"),full_page=True)
            assert not errors, errors
            browser.close()
    print("SHARED_MEMORY_UI_PASS")


if __name__=="__main__":
    main()
