"""Real Qwen + real VIP source retry, isolated state, never connects to QQ."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'external/AstrBot')]


async def probe(output):
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    from scripts.qq_companion.configure_model import configure, PROVIDER_ID
    from scripts.qq_companion.song_source_prepare import prepare_source
    from services.media_api.songs import SongStore
    from services.media_api.song_source import NetEaseSource, SongSourceError

    opener = build_opener(ProxyHandler({}))
    with opener.open('http://127.0.0.1:8080/v1/models', timeout=3) as response:
        model_id = json.load(response)['data'][0]['id']
    config = configure(json.loads((ROOT / 'external/AstrBot/data/cmd_config.json').read_text(encoding='utf-8-sig')), model_id, 'elysia')
    model = next(p for p in config['provider'] if p['id'] == PROVIDER_ID)
    source_config = next(p for p in config['provider_sources'] if p['id'] == model['provider_source_id'])
    provider = ProviderOpenAIOfficial({**source_config, **model, 'timeout': 60}, {})
    async def get_provider(_): return provider
    context = SimpleNamespace(provider_manager=SimpleNamespace(get_provider_by_id=get_provider))
    media = MediaRouter(context, output)
    source, jobs = NetEaseSource.from_environment(), SongStore(output)
    scope, key = 'qq:999:group:201', uuid.uuid4().hex
    songs = await asyncio.to_thread(source.search, '雨爱')
    pool = [{**song.public(), 'link': f'https://music.163.com/song?id={song.song_id}'} for song in songs]
    selected = next(song for song in pool if song['title'] == '雨爱' and song['artists'] == '杨丞琳')
    music = {'mode': 'cover', 'query': '雨爱', 'artist': '杨丞琳'}
    request = '请翻唱杨丞琳的雨爱'
    jobs.submit(key, scope, music, selected_song=selected, candidates=pool, user_request=request)
    task = media.song_tasks.register(key, scope, '101', 'cover', 'probe', '雨爱')
    payload = {'task_id': key, 'task_revision': task['revision'], 'scope': scope, 'owner': '101',
               'group': '201', 'generation': 0, 'user_request': request, 'request_context': [], 'music': music}
    async def api(path, body=None, **kwargs):
        assert '/select?' in path and parse_qs(urlsplit(path).query)['scope'] == [scope]
        jobs.reselect(key, scope, body['song_id'], body['retry_revision'], selection_error=body['selection_error'])
        return jobs.public(key, scope)
    media.client.request = api
    tried, decisions = [], []
    try:
        while jobs.get(key)['status'] == 'waiting_gpu':
            song = jobs.get(key)['song']
            assert song['song_id'] not in tried
            tried.append(song['song_id'])
            print(json.dumps({'attempt': len(tried), 'song_id': song['song_id'], 'artists': song['artists']}, ensure_ascii=False), flush=True)
            try:
                await asyncio.to_thread(prepare_source, jobs, key, source=source)
            except SongSourceError as error:
                if error.code != 'source_http_403': raise
            else:
                break  # Download verified; the cover itself has not run.
            job = jobs.public(key, scope)
            if job['status'] == 'awaiting_reselection':
                await media.reselect_source(key, payload, job)
                current = jobs.get(key)
                decisions.append({'revision': current['retry_revision'], 'status': current['status'],
                                  'selected_song_id': current['song']['song_id']})
                print(json.dumps(decisions[-1]), flush=True)
        final = jobs.public(key, scope)
        assert len(tried) == len(set(tried)) and len(tried) <= len(pool)
        assert {s['song_id'] for s in final['remaining_candidates']}.isdisjoint(
            item['song_id'] for item in final['download_attempts'] if item['status'] == 'forbidden')
        report = {'candidate_count': len(pool), 'attempted_song_ids': tried, 'decisions': decisions,
                  'final_status': final['status'], 'error': final['error'], 'download_attempts': final['download_attempts'],
                  'download_verified': any(item['status'] == 'downloaded' for item in final['download_attempts']),
                  'cover_conversion_run': False, 'qq_messages_sent': 0}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        await provider.terminate()


def main():
    from integrations.qq_astrbot.plugin.media_state import MediaState
    state = MediaState(ROOT / 'storage/qq_companion')
    lease = state.enter()
    if not lease: raise RuntimeError('managed_model_busy')
    output = ROOT / 'storage/temporary' / ('source-reselection-' + time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    os.environ['ASTRBOT_ROOT'] = str(output)
    try:
        asyncio.run(probe(output))
    finally:
        state.leave(lease)


if __name__ == '__main__':
    main()
