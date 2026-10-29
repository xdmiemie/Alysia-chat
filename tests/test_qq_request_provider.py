"""Exercise the pinned Provider and real OpenAI SDK up to the HTTP body."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

if sys.version_info < (3, 12):
    pytest.skip('Pinned AstrBot requires Python 3.12', allow_module_level=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'external/AstrBot'))

import httpx
from openai import AsyncOpenAI
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from integrations.qq_astrbot.plugin.agent_runtime import CHECK_FORMAT, CLOSE_FORMAT, REPLY_FORMAT
from integrations.qq_astrbot.plugin.request_provider import RequestProvider, request_chat
from scripts.qq_companion.probe_q3_local import LocalLoop


def test_request_schema_and_overrides_reach_wire_without_mutating_shared_provider():
    async def check():
        bodies = []
        def respond(request):
            body = json.loads(request.content)
            bodies.append(body)
            text = {'claims_music_started': False, 'claims_music_completed': False, 'claims_link_sent': False, 'claims_audio_sent': False,
                    'decision': 'finish', 'instruction': ''} if body.get('response_format') == CHECK_FORMAT else {
                'reply': 'ready', 'thinking_level': 1, 'voice': False}
            return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
                'model': 'qwen3', 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': json.dumps(text)}}]})
        provider = ProviderOpenAIOfficial({'id': 'test', 'model': 'qwen3', 'key': ['local'],
            'api_base': 'http://127.0.0.1:8080/v1', 'custom_extra_body': {
                'max_tokens': 1536, 'reasoning_budget_tokens': 1024,
                'chat_template_kwargs': {'enable_thinking': True, 'other': 'keep'}}}, {})
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key='local', base_url='http://127.0.0.1:8080/v1', max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        before = deepcopy(provider.provider_config)
        try:
            for phase in ("agent", "selection", "repair", "check", "close", "music_result", "compression"):
                await request_chat(provider, phase=phase, prompt=phase, request_max_retries=1)
            await asyncio.gather(
                request_chat(provider, prompt='close', response_format=CLOSE_FORMAT, max_tokens=256,
                    reasoning_budget_tokens=128, chat_template_kwargs={'enable_thinking': False}, request_max_retries=1),
                request_chat(provider, prompt='check', response_format=CHECK_FORMAT, request_max_retries=1))
            await RequestProvider(provider, response_format=REPLY_FORMAT).text_chat(prompt='main', request_max_retries=1)
            from integrations.qq_astrbot.plugin.agent_tools import AgentTools
            from types import SimpleNamespace
            await RequestProvider(provider, response_format=REPLY_FORMAT).text_chat(prompt='tool step',
                func_tool=ToolSet(AgentTools(SimpleNamespace()).registered()), request_max_retries=1)
            await RequestProvider(provider, content_only=True, response_format=REPLY_FORMAT).text_chat(prompt='repair',
                func_tool=ToolSet(AgentTools(SimpleNamespace()).registered()), request_max_retries=1)
            choice = {'type': 'function', 'function': {'name': 'weapi'}}
            await RequestProvider(provider, tool_choice=choice, response_format=REPLY_FORMAT).text_chat(prompt='tool repair',
                func_tool=ToolSet(AgentTools(SimpleNamespace()).registered()), request_max_retries=1)
            await RequestProvider(provider, response_format=REPLY_FORMAT).text_chat(prompt='after tool repair',
                func_tool=ToolSet(AgentTools(SimpleNamespace()).registered()), request_max_retries=1)
            by_prompt = {body['messages'][-1]['content']: body for body in bodies}
            # Inspect each wire body, including concurrent requests of different
            # phases, rather than trusting the Python call-site kwargs.
            for body in bodies[:7]:
                enabled = body['messages'][-1]['content'] in {'agent', 'selection'}
                assert body['chat_template_kwargs']['enable_thinking'] is enabled
                assert body['reasoning_budget_tokens'] == (1024 if enabled else 0)
                assert 'phase' not in body and 'thinking_phases' not in body
            close, check_body, main = (by_prompt[key] for key in ['close', 'check', 'main'])
            assert close['response_format'] == CLOSE_FORMAT and close['max_tokens'] == 256
            assert close['reasoning_budget_tokens'] == 128
            assert close['chat_template_kwargs'] == {'enable_thinking': False, 'other': 'keep'}
            assert check_body['response_format'] == CHECK_FORMAT and check_body['max_tokens'] == 1536
            assert check_body['chat_template_kwargs']['enable_thinking'] is True
            assert main['response_format'] == REPLY_FORMAT
            assert 'response_format' not in by_prompt['tool step']
            assert 'tools' not in by_prompt['repair'] and by_prompt['repair']['response_format'] == REPLY_FORMAT
            assert by_prompt['tool repair']['tool_choice'] == choice
            assert 'response_format' not in by_prompt['tool repair']
            assert by_prompt['after tool repair']['tool_choice'] == 'auto'
            assert {tool['function']['name'] for tool in by_prompt['tool step']['tools']} == {
                'emotion', 'musiccover', 'music', 'sticker', 'weapi'}
            assert provider.provider_config == before
        finally:
            await provider.terminate()
    loop = LocalLoop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()


def test_background_notifications_and_selection_use_wire_options_and_keep_persona(tmp_path):
    from types import SimpleNamespace
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    from integrations.qq_astrbot.plugin.prompt_loader import load_prompts
    from scripts.qq_companion.configure_model import configure
    async def check():
        bodies = []
        def respond(request):
            body = json.loads(request.content)
            bodies.append(body)
            text = {"candidate": 2} if body['messages'][0]['content'] == load_prompts()['music_selection'] else {
                "follow_up": "ready", "thinking_level": 1}
            return httpx.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
                'model': 'qwen3', 'choices': [{'index': 0, 'finish_reason': 'stop',
                    'message': {'role': 'assistant', 'content': json.dumps(text)}}]})
        model = configure({}, 'qwen3', 'elysia')['provider'][0]
        provider = ProviderOpenAIOfficial({**model, 'key': ['local'], 'api_base': 'http://127.0.0.1:8080/v1'}, {})
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key='local', base_url='http://127.0.0.1:8080/v1', max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        class Manager:
            async def get_provider_by_id(self, key):
                assert key == 'astrbot_chat_qwen3'
                return provider
        media = MediaRouter(SimpleNamespace(provider_manager=Manager()), tmp_path)
        payload = {'persona': 'CURRENT PERSONA', 'music': {'mode': 'cover', 'query': 'song'}, 'user_request': 'sing'}
        try:
            assert await media.result_line(payload, {'status': 'completed', 'song': {
                'song_id': 98765, 'title': 'song', 'album': 'album-secret'}, 'has_audio': True}, key='a'*32) == 'ready'
            candidates = [{'song_id': 98765 + i, 'title': 'song', 'album': 'album-secret'} for i in range(12)]
            assert await media.select_song('a'*32, payload, candidates) == 98766
            result_body, select_body = bodies
            assert result_body['chat_template_kwargs']['enable_thinking'] is False
            assert result_body['reasoning_budget_tokens'] == 0 and result_body['temperature'] == 0.5
            assert 'CURRENT PERSONA' in result_body['messages'][0]['content']
            assert 'album-secret' not in repr(bodies) and 'song_id' not in repr(bodies)
            assert select_body['chat_template_kwargs']['enable_thinking'] is True
            assert select_body['reasoning_budget_tokens'] == 1024 and select_body['top_k'] == 20
            assert len(json.loads(select_body['messages'][-1]['content'].split('\n')[-1])['candidates']['songs']) == 10
        finally:
            await provider.terminate()
    loop = LocalLoop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()
