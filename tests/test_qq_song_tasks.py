from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import pytest

from integrations.qq_astrbot.plugin.song_tasks import SongTasks, SongHeld, SongCanceled


def task_payload(tasks, key='a' * 32, owner='101', scope='qq:999:group:201'):
    row = tasks.register(key, scope, owner, 'cover', 'run', 'song')
    return {'task_id': key, 'task_revision': row['revision'], 'owner': owner, 'scope': scope,
            'group': scope.split(':')[-1] if ':group:' in scope else '', 'generation': 0,
            'version': 1, 'run_id': 'run', 'origin': 'snowluma:GroupMessage:201',
            'history_origin': 'snowluma:GroupMessage:qq:999:group:201',
            'music': {'mode': 'cover', 'query': 'song'}, 'selected_song_id': 1}


def test_task_identity_survives_restart_and_holds_are_owner_scoped(tmp_path):
    tasks = SongTasks(tmp_path / 'tasks.db')
    payload = task_payload(tasks)
    tasks.hold(payload['scope'], '102', 3)
    tasks.guard(payload)
    tasks.hold(payload['scope'], '101', 4)
    with pytest.raises(SongHeld): tasks.guard(payload)
    tasks.release(payload['scope'], '101', 2)
    with pytest.raises(SongHeld): SongTasks(tmp_path / 'tasks.db').guard(payload)
    tasks.release(payload['scope'], '101', 4)
    tasks.guard(payload)


def test_cancel_and_replace_cannot_touch_another_requester_or_group(tmp_path):
    tasks = SongTasks(tmp_path / 'tasks.db')
    old = task_payload(tasks)
    other = task_payload(tasks, 'b' * 32, owner='102')
    other_group = task_payload(tasks, 'c' * 32, scope='qq:999:group:202')
    replacement = task_payload(tasks, 'd' * 32)
    rows = tasks.cancel(old['scope'], owner='101', except_id=replacement['task_id'], reason='explicit_replace')
    assert [row['id'] for row in rows] == [old['task_id']]
    with pytest.raises(SongCanceled): tasks.guard(old)
    for payload in (other, other_group, replacement): tasks.guard(payload)


def test_startup_releases_abandoned_holds_but_keeps_task_and_part_receipts(loop, tmp_path):
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    media = MediaRouter(SimpleNamespace(), tmp_path)
    payload = task_payload(media.song_tasks)
    media.song_tasks.hold(payload['scope'], payload['owner'], 2)
    assert media.song_tasks.begin_part(payload['task_id'], 'audio')
    media.song_tasks.finish_part(payload['task_id'], 'audio')
    media.client.state.remember(payload['task_id'], payload)
    resumed = []
    def spawn(coro):
        resumed.append(coro)
        coro.close()
    media.spawn = spawn
    media.resume()
    media.resume()
    media.song_tasks.guard(payload)
    assert media.song_tasks.part_sent(payload['task_id'], 'audio')
    assert len(resumed) == 1


def test_background_history_preserves_model_content_and_cleans_legacy_receipts(loop, tmp_path):
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    history = [{'role': 'assistant', 'content': '[已发送《旧歌曲》整首翻唱] 旧回话'}]
    groups, saved = [], []
    class Manager:
        async def get_curr_conversation_id(self, origin): return 'cid'
        async def get_conversation(self, origin, cid): return SimpleNamespace(history=json.dumps(history))
        async def update_conversation(self, origin, cid, **kwargs): saved.append(kwargs['history'])
    media = MediaRouter(SimpleNamespace(conversation_manager=Manager()), tmp_path)
    media.record_group_result = lambda scope, key, text: groups.append(text)
    async def check():
        payload = task_payload(media.song_tasks)
        raw = '{"follow_up":"唱好啦","thinking_level":4}'
        media.client.state.record_song_reply('job', 1, status='generated', raw=raw, reply='唱好啦')
        await media.remember_result('job', payload, {'has_audio': True, 'song': {'title': '雨爱'}}, '唱好啦')
        assert groups == [raw]
        assert saved == [[{'role': 'assistant', 'content': '旧回话'}, {'role': 'assistant', 'content': raw}]]
        linked = '唱好啦 https://music.163.com/song?id=1'
        await media.remember_result('job', payload, {'has_audio': False}, linked)
        assert json.loads(saved[-1][-1]['content']) == {'follow_up': linked, 'thinking_level': 4}
    loop.run_until_complete(check())


@pytest.fixture
def loop():
    if sys.version_info < (3, 12): pytest.skip('Pinned AstrBot requires Python 3.12')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'external/AstrBot'))
    from scripts.qq_companion.probe_q3_local import LocalLoop
    value = LocalLoop()
    yield value
    value.close()


def make_media(tmp_path):
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    sent, cancels = [], []
    class Bot:
        after_audio = None
        async def send_group_msg(self, *, group_id, message):
            assert group_id == 201
            sent.append(message[0]['type'])
            if sent[-1] == 'record' and self.after_audio:
                self.after_audio()
            return {'message_id': len(sent)}
    bot = Bot()
    platform = SimpleNamespace(bot=bot, meta=lambda: SimpleNamespace(name='aiocqhttp'))
    context = SimpleNamespace(get_platform_inst=lambda _: platform, conversation_manager=None)
    media = MediaRouter(context, tmp_path)
    payload = task_payload(media.song_tasks)
    key = payload['task_id']
    media.client.state.remember(key, payload)
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b'\0\0' * 160)
    async def request(path, body=None, **kwargs):
        if '/cancel?' in path:
            cancels.append(key)
            return {'status': 'canceled'}
        if '/audio?' in path: return audio.getvalue()
        return {'status': 'completed', 'has_audio': True, 'song': {'title': 'song'}}
    media.client.request = request
    generated = []
    async def line(*args, **kwargs):
        generated.append('line')
        return 'done'
    media.result_line = line
    return SimpleNamespace(media=media, payload=payload, bot=bot, sent=sent, cancels=cancels, generated=generated)


def test_completed_group_song_ignores_foreground_revision_and_other_chatter(loop, tmp_path):
    async def check():
        value = make_media(tmp_path)
        value.media.active_version = lambda *args: False
        await value.media.deliver_song(value.payload['task_id'], value.payload)
        assert value.sent == ['record', 'text']
        assert not value.cancels
        await value.media.deliver_song(value.payload['task_id'], value.payload)
        assert value.sent == ['record', 'text']
    loop.run_until_complete(check())


def test_hold_after_audio_sends_only_remaining_reply_when_released(loop, tmp_path):
    async def check():
        value = make_media(tmp_path)
        def hold():
            value.media.song_tasks.hold(value.payload['scope'], '101', 2)
            loop.call_later(0.01, value.media.song_tasks.release, value.payload['scope'], '101', 2)
        value.bot.after_audio = hold
        await value.media.deliver_song(value.payload['task_id'], value.payload)
        assert value.sent == ['record', 'text'] and len(value.generated) == 1
    loop.run_until_complete(check())


def test_explicit_cancel_blocks_already_completed_audio(loop, tmp_path):
    async def check():
        value = make_media(tmp_path)
        value.media.song_tasks.cancel(value.payload['scope'], owner='101')
        await value.media.deliver_song(value.payload['task_id'], value.payload)
        assert not value.sent and not value.generated
        assert value.cancels == [value.payload['task_id']]
    loop.run_until_complete(check())


def test_accepted_task_commits_after_foreground_was_superseded(loop, tmp_path):
    async def check():
        value = make_media(tmp_path)
        original = value.media.client.request
        commits = []
        async def request(path, body=None, **kwargs):
            if '/commit?' in path:
                commits.append(path)
            elif '/audio?' not in path and not commits:
                return {'status': 'awaiting_commit'}
            return await original(path, body, **kwargs)
        value.media.client.request = request
        value.media.run_status = lambda _: 'superseded'
        value.media.run_active = lambda _: False
        await value.media.deliver_song(value.payload['task_id'], value.payload)
        assert len(commits) == 1 and value.sent == ['record', 'text']
    loop.run_until_complete(check())


def test_group_control_blocks_completed_song(loop, tmp_path):
    async def check():
        value = make_media(tmp_path)
        value.media.active_group = lambda *args: False
        await value.media.deliver_song(value.payload['task_id'], value.payload)
        assert not value.sent and value.cancels
    loop.run_until_complete(check())


def test_cancel_rechecked_after_native_audio_encoding(loop, tmp_path, monkeypatch):
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    original = AiocqhttpMessageEvent._parse_onebot_json
    async def check():
        value = make_media(tmp_path)
        async def parse(chain):
            result = await original(chain)
            value.media.song_tasks.cancel(value.payload['scope'], owner='101')
            return result
        monkeypatch.setattr(AiocqhttpMessageEvent, '_parse_onebot_json', parse)
        await value.media.deliver_song(value.payload['task_id'], value.payload)
        assert not value.sent
    loop.run_until_complete(check())


def test_transport_uncertainty_never_replays_audio(loop, tmp_path):
    async def check():
        value = make_media(tmp_path)
        async def failed(**kwargs):
            value.sent.append('attempted')
            raise OSError('unknown transport outcome')
        value.bot.send_group_msg = failed
        await value.media.deliver_song(value.payload['task_id'], value.payload)
        await value.media.deliver_song(value.payload['task_id'], value.payload)
        assert value.sent == ['attempted']
        with value.media.client.state.connection() as db:
            assert db.execute('select state from delivery').fetchone()[0] == 'uncertain'
    loop.run_until_complete(check())
