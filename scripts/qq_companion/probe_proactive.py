"""Exercise the production planner prompts/provider against Qwen, with no transport."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'external/AstrBot')]


async def main():
    import httpx
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from integrations.qq_astrbot.plugin.api_client import MediaClient
    from integrations.qq_astrbot.plugin.foreground_activity import ForegroundActivity
    from integrations.qq_astrbot.plugin.proactive_store import DEFAULTS
    from integrations.qq_astrbot.plugin.proactive_runtime import ProactiveRuntime, PLAN_SCHEMA, REPLY_SCHEMA
    from services.douyin_worker.persona import active_persona

    state = ROOT / 'storage/qq_companion'
    report_dir = ROOT / 'storage/temporary/proactive-probe'
    report_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(trust_env=False) as client:
        models = (await client.get('http://127.0.0.1:8080/v1/models')).json()
    provider = ProviderOpenAIOfficial({'id':'proactive_local_probe', 'type':'openai_chat_completion',
        'key':['local'], 'api_base':'http://127.0.0.1:8080/v1', 'model':models['data'][0]['id'],
        'max_context_tokens':32768, 'inference_queue':True}, {})
    async def provider_factory(): return provider
    async def persona_factory(): return active_persona(ROOT, 'elysia')
    def no_transport(): raise AssertionError('This probe must never use QQ transport')
    platform = NS(meta=lambda:NS(name='aiocqhttp'), get_client=no_transport)
    owner = NS(_ui_config=DEFAULTS, context=NS(get_platform_inst=lambda _:platform,
        get_config=lambda **_:{}), foreground_activity=ForegroundActivity(state),
        media=NS(client=MediaClient(state)), memory=NS(provider_factory=provider_factory,
        persona_factory=persona_factory), logger=logging.getLogger(__name__))
    try:
        with tempfile.TemporaryDirectory(dir=report_dir) as directory:
            rt = ProactiveRuntime(owner, directory)
            async def services(peer): pass  # Synthetic conversation; no live native session.
            rt.check_services = services
            received = datetime(2026,11,2,0,0,tzinfo=timezone(timedelta(hours=8))).timestamp()
            rt.store.clock = lambda:received
            b = {'scope':'qq:1:dm:2','origin':'probe:FriendMessage:qq:1:dm:2',
                 'transport':'probe:FriendMessage:2','platform':'probe','bot':'1','recipient':'2'}
            rt.store.observe(b, 'probe', '我睡了', rt.config)
            peer = rt.store.peer(b['scope'])
            plan = await rt.generate('proactive_plan', {'now':rt.timestamp(), 'received_at':rt.timestamp(),
                'message':'我睡了', 'conversation':[{'role':'user','content':'我睡了'},
                    {'role':'assistant','content':'晚安，睡个好觉。'}], 'pending_plans':[], 'pending_limit':5},
                PLAN_SCHEMA, peer, 'context')
            scheduled = [p for p in plan['actions'] if p['action']=='schedule']
            assert scheduled, 'Sleep message did not produce a plan'
            due = scheduled[0]['after_seconds']
            if due is None:
                due = datetime.fromisoformat(scheduled[0]['due_at']).timestamp()-received
            assert due == 8*3600, plan
            rt.store.clock = lambda:received+8*3600
            reply = await rt.generate('proactive_reply', {'now':rt.timestamp(), 'kind':'context',
                'purpose':scheduled[0]['purpose'], 'due_at':rt.timestamp(),
                'last_user_at':rt.timestamp(received), 'conversation':[{'role':'user','content':'我睡了'},
                    {'role':'assistant','content':'晚安，睡个好觉。'}], 'recent_proactive':[]},
                REPLY_SCHEMA, peer, 'context')
            assert reply['message'] and '早' in reply['message'] and any(t in reply['message'] for t in ('醒','睡'))
            quiet = await rt.generate('proactive_reply', {'now':rt.timestamp(), 'kind':'periodic',
                'purpose':'', 'due_at':rt.timestamp(), 'last_user_at':rt.timestamp(),
                'conversation':[{'role':'user','content':'我现在要睡觉，请不要主动发消息打扰我，等我找你。'},
                    {'role':'assistant','content':'好，等你找我。'}], 'recent_proactive':[]},
                REPLY_SCHEMA, peer, 'periodic')
            assert quiet['message'] is None, quiet
            report = {'sleep_delay_seconds':due, 'plan':plan, 'morning_reply':reply,
                      'quiet_periodic':quiet, 'external_sends':0}
            (report_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'success':True,'sleep_delay_seconds':due,'external_sends':0,
                              'report':str(report_dir/'report.json')}))
    finally:
        await provider.terminate()


if __name__ == '__main__':
    asyncio.run(main())
