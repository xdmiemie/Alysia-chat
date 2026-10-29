"""403 recovery across store, download preparation, model selection and delivery."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import wave

import pytest
import requests

from services.media_api.songs import SongStore
from services.media_api.song_source import SongSourceError
from scripts.qq_companion.cover_dispatcher import CoverDispatcher
from scripts.qq_companion.song_source_prepare import prepare_source

KEY, SCOPE = 'a' * 32, 'qq:999:group:201'
MUSIC = {'mode': 'cover', 'query': '雨爱', 'artist': '杨丞琳'}
POOL = [{'song_id': i, 'title': '雨爱' if i != 1 else '淘汰条目', 'artists': '杨丞琳',
         'duration_seconds': 6, 'available': True, 'aliases': [], 'source': 'netease',
         'album': 'program-only', 'link': f'https://music.163.com/song?id={i}'} for i in (1, 2, 3)]


def submit(directory, mode='cover'):
    jobs = SongStore(directory)
    jobs.submit(KEY, SCOPE, {**MUSIC, 'mode': mode}, selected_song=POOL[0], candidates=POOL,
                user_request='唱一下杨丞琳的雨爱')
    return jobs


def forbidden():
    response = requests.Response()
    response.status_code = 403
    response.url = 'https://example.invalid/private-signed-url'
    return requests.HTTPError('sensitive upstream URL', response=response)


def test_elimination_is_durable_idempotent_and_scoped(tmp_path):
    jobs = submit(tmp_path)
    jobs.start_download(KEY, 1)
    jobs.reject_download(KEY, 1)
    jobs.reject_download(KEY, 1)
    jobs = SongStore(tmp_path)
    jobs.recover()
    result = jobs.public(KEY, SCOPE)
    assert result['status'] == 'awaiting_reselection' and result['retry_revision'] == 1
    assert [s['song_id'] for s in result['remaining_candidates']] == [2, 3]
    assert result['download_attempts'] == [{'song_id': 1, 'status': 'forbidden', 'error': 'source_http_403'}]
    with pytest.raises(KeyError): jobs.reselect(KEY, 'qq:999:group:202', 2, 1)
    with pytest.raises(ValueError): jobs.reselect(KEY, SCOPE, 1, 1)
    with pytest.raises(ValueError): jobs.reselect(KEY, SCOPE, 999, 1)
    with pytest.raises(ValueError, match='stale'): jobs.reselect(KEY, SCOPE, 2, 2)
    jobs.reselect(KEY, SCOPE, 2, 1)
    jobs.reselect(KEY, SCOPE, 2, 1)
    # An HTTP retry of the initial submission cannot erase the pool or switch back.
    assert submit(tmp_path).get(KEY)['song']['song_id'] == 2
    assert jobs.get(KEY)['payload']['mode'] == 'cover'
    jobs.start_download(KEY, 2)
    jobs.reject_download(KEY, 2)
    with pytest.raises(ValueError, match='stale'): jobs.reselect(KEY, SCOPE, 2, 1)
    jobs.reselect(KEY, SCOPE, 3, 2)
    jobs.start_download(KEY, 3)
    jobs.reject_download(KEY, 3)
    result = jobs.public(KEY, SCOPE)
    assert result['status'] == 'failed' and result['error'] == 'source_candidates_exhausted'
    assert result['remaining_candidates'] == [] and not result['has_audio']
    assert len(result['download_attempts']) == 3 and jobs.next_job() is None


def test_cancel_cannot_be_revived_by_late_reselection(tmp_path):
    jobs = submit(tmp_path)
    jobs.reject_download(KEY, 1)
    jobs.cancel(KEY, SCOPE)
    with pytest.raises(ValueError): jobs.reselect(KEY, SCOPE, 2, 1)
    jobs.reject_download(KEY, 1)
    assert jobs.get(KEY)['status'] == 'canceled'


def test_api_pool_uses_trusted_receipts_and_keeps_original_idempotency(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from services.media_api.app import create_app, SelectedSongPayload, SongSelectionPayload
    monkeypatch.setenv('ASTRBOT_CHAT_STATE_DIR', str(tmp_path))
    jobs = SongStore(tmp_path)
    receipts = [jobs.remember_candidate(SCOPE, song) for song in POOL]
    routes = {route.path: route.endpoint for route in create_app().routes}
    body = SelectedSongPayload(request_id=KEY, scope=SCOPE, music=MUSIC, song_id=1,
        source_receipt=receipts[0], candidate_receipts=receipts)
    assert len(routes['/v1/songs/selected'](body)['search_results']) == 3
    jobs.commit(KEY, SCOPE)
    jobs.reject_download(KEY, 1)
    routes['/v1/songs/{job_id}/select'](KEY, SCOPE, SongSelectionPayload(song_id=2, retry_revision=1))
    # Receipt expiry after acceptance must not affect idempotency or retries.
    with jobs.connection() as db: db.execute('DELETE FROM song_candidates')
    assert routes['/v1/songs/selected'](body)['song']['song_id'] == 2
    with pytest.raises(HTTPException):
        routes['/v1/songs/{job_id}/select'](KEY, SCOPE, SongSelectionPayload(song_id=1, retry_revision=1))
    with pytest.raises(ValueError): jobs.candidate_pool(receipts, 'qq:999:group:202')


class AudioSource:
    def __init__(self, *, all_forbidden=False):
        self.calls = []
        self.all_forbidden = all_forbidden

    def download(self, song_id, path):
        self.calls.append(song_id)
        if song_id == 1 or self.all_forbidden:
            raise forbidden()
        # Real decodable audio, verified by the production ffprobe.
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\0\0' * 16000 * 6)


def dispatcher(directory, jobs, source, *, gpu=None, worker=None):
    def no_gpu(*args): pytest.fail('403 must not stop models or reserve the GPU')
    return CoverDispatcher(directory, broker=SimpleNamespace(file=object()),
        prepare_source=lambda key: prepare_source(jobs, key, source=source),
        stop_models=(lambda stopped: gpu.append('stop')) if gpu is not None else no_gpu,
        restore_models=(lambda stopped: gpu.append('restore')) if gpu is not None else no_gpu,
        verify_gpu=(lambda: gpu.append('verify')) if gpu is not None else no_gpu,
        run_worker=worker or no_gpu)


def test_real_audio_is_cached_and_worker_does_not_download_it_again(tmp_path):
    jobs, source = submit(tmp_path), AudioSource()
    with pytest.raises(SongSourceError, match='source_http_403'):
        prepare_source(jobs, KEY, source=source)
    jobs.reselect(KEY, SCOPE, 2, 1)
    path = prepare_source(jobs, KEY, source=source)
    assert path.is_file() and prepare_source(jobs, KEY, source=source) == path
    assert source.calls == [1, 2]
    assert jobs.get(KEY)['download_attempts'][-1]['status'] == 'downloaded'
    # A checksum mismatch invalidates the cached input.
    path.write_bytes(b'corrupt')
    prepare_source(jobs, KEY, source=source)
    assert source.calls == [1, 2, 2]


def test_successful_download_canceled_before_gpu_never_starts_conversion(tmp_path):
    jobs = submit(tmp_path)
    class CanceledSource(AudioSource):
        def download(self, song_id, path):
            super().download(song_id, path)
            jobs.cancel(KEY, SCOPE)
    jobs.reject_download(KEY, 1)
    jobs.reselect(KEY, SCOPE, 2, 1)
    dispatcher(tmp_path, jobs, CanceledSource()).step()
    assert jobs.get(KEY)['status'] == 'canceled'


def test_non_403_download_failure_does_not_eliminate_other_candidates(tmp_path):
    jobs = submit(tmp_path)
    source = SimpleNamespace(download=lambda *args: (_ for _ in ()).throw(requests.Timeout()))
    dispatcher(tmp_path, jobs, source).step()
    result = jobs.public(KEY, SCOPE)
    assert result['status'] == 'failed' and result['error'] == 'source_timeout'
    assert len(result['remaining_candidates']) == 3 and result['retry_revision'] == 0


@pytest.fixture
def loop():
    if sys.version_info < (3, 12): pytest.skip('Pinned AstrBot requires Python 3.12')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'external/AstrBot'))
    from scripts.qq_companion.probe_q3_local import LocalLoop
    value = LocalLoop()
    yield value
    value.close()


def router(directory, jobs, answers):
    from integrations.qq_astrbot.plugin.media_router import MediaRouter
    from test_qq_song_tasks import task_payload
    sent, model_inputs = [], []
    class Bot:
        async def send_group_msg(self, *, group_id, message):
            sent.extend(part['type'] for part in message)
            return {'message_id': len(sent)}
    platform = SimpleNamespace(bot=Bot(), meta=lambda: SimpleNamespace(name='aiocqhttp'))
    media = MediaRouter(SimpleNamespace(get_platform_inst=lambda _: platform, conversation_manager=None), directory)
    payload = task_payload(media.song_tasks)
    payload.update(user_request='唱一下杨丞琳的雨爱', request_context=[], music=MUSIC)
    media.client.state.remember(KEY, payload)
    async def request(path, body=None, **kwargs):
        if '/select?' in path:
            jobs.reselect(KEY, SCOPE, body['song_id'], body['retry_revision'], selection_error=body['selection_error'])
        if '/audio?' in path: return jobs.output(KEY).read_bytes()
        return jobs.public(KEY, SCOPE)
    async def model(phase, **kwargs):
        assert phase == 'selection' and kwargs['func_tool'] is None
        data = json.loads(kwargs['prompt'])
        model_inputs.append(data)
        answer = answers.pop(0)
        if isinstance(answer, Exception): raise answer
        if callable(answer): answer = answer(media, payload)
        return SimpleNamespace(completion_text=answer, tools_call_name=[])
    media.client.request, media.model_request = request, model
    async def result(*args, **kwargs): return '这次完成了' if jobs.get(KEY)['status'] == 'completed' else '音源没能下载成功'
    media.result_line = result
    return media, payload, sent, model_inputs


def test_403_model_reselect_download_success_and_one_delivery(loop, tmp_path):
    jobs, source, gpu = submit(tmp_path), AudioSource(), []
    worker_calls = []
    def work(key):
        worker_calls.append(key)
        # GPU conversion is deliberately a fixture; the download and ffprobe are real.
        shutil.copyfile(prepare_source(jobs, key, source=source), jobs.output(key))
    dispatch = dispatcher(tmp_path, jobs, source, gpu=gpu, worker=work)
    dispatch.step()
    assert not gpu and not worker_calls and source.calls == [1]
    media, payload, sent, inputs = router(tmp_path, jobs, ['{"candidate":2}'])
    async def check():
        await media.reselect_source(KEY, payload, jobs.public(KEY, SCOPE))
        dispatch.step()
        assert source.calls == [1, 2] and worker_calls == [KEY]
        assert gpu == ['stop', 'verify', 'restore']
        assert jobs.get(KEY)['status'] == 'completed'
        await media.deliver_song(KEY, payload)
        await media.deliver_song(KEY, payload)
    loop.run_until_complete(check())
    assert sent == ['record', 'text']
    with media.song_tasks.connection() as db:
        assert db.execute('SELECT state FROM tasks WHERE id=?', (KEY,)).fetchone()[0] == 'sent'
    assert [row[0] for row in inputs[0]['songs']] == [2, 3]
    assert '淘汰条目' not in json.dumps(inputs, ensure_ascii=False)
    assert 'song_id' not in json.dumps(inputs) and 'program-only' not in json.dumps(inputs)


def test_all_403_exhausts_unique_candidates_and_records_failure(loop, tmp_path):
    jobs, source = submit(tmp_path), AudioSource(all_forbidden=True)
    dispatch = dispatcher(tmp_path, jobs, source)
    media, payload, sent, inputs = router(tmp_path, jobs, ['{"candidate":2}', '{"candidate":3}'])
    async def check():
        for _ in range(3):
            dispatch.step()
            if jobs.get(KEY)['status'] == 'awaiting_reselection':
                await media.reselect_source(KEY, payload, jobs.public(KEY, SCOPE))
        dispatch.step()
        assert jobs.get(KEY)['error'] == 'source_candidates_exhausted'
        await media.deliver_song(KEY, payload)
        await media.deliver_song(KEY, payload)
    loop.run_until_complete(check())
    assert source.calls == [1, 2, 3] and len(inputs) == 2
    assert sent == ['text', 'text']  # An actual link and a failure notice; no audio.
    with media.song_tasks.connection() as db:
        assert db.execute('SELECT state FROM tasks WHERE id=?', (KEY,)).fetchone()[0] == 'failed'
    assert not media.song_tasks.part_sent(KEY, 'audio')


def test_invalid_selection_and_timeout_budget_survives_restart(loop, tmp_path):
    jobs = submit(tmp_path)
    jobs.reject_download(KEY, 1)
    media, payload, sent, inputs = router(tmp_path, jobs, [asyncio.TimeoutError(), '{"candidate":1}'])
    audit_key = f'{KEY}:retry:1'
    media.client.state.record_song_selection(audit_key, 1, status='started')
    loop.run_until_complete(media.reselect_source(KEY, payload, jobs.public(KEY, SCOPE)))
    assert len(inputs) == 2 and jobs.get(KEY)['error'] == 'reselection_model_failed'
    assert jobs.get(KEY)['download_attempts'] == [{'song_id': 1, 'status': 'forbidden', 'error': 'source_http_403'}]
    assert not sent


def test_user_cancel_during_model_request_blocks_reselection_commit(loop, tmp_path):
    from integrations.qq_astrbot.plugin.song_tasks import SongCanceled
    jobs = submit(tmp_path)
    jobs.reject_download(KEY, 1)
    def cancel(media, payload):
        media.song_tasks.cancel(SCOPE, owner=payload['owner'])
        return '{"candidate":2}'
    media, payload, sent, _ = router(tmp_path, jobs, [cancel])
    with pytest.raises(SongCanceled):
        loop.run_until_complete(media.reselect_source(KEY, payload, jobs.public(KEY, SCOPE)))
    assert jobs.get(KEY)['status'] == 'awaiting_reselection' and not sent


def test_no_compatible_remaining_version_is_explicit_failure(loop, tmp_path):
    jobs = submit(tmp_path)
    jobs.reject_download(KEY, 1)
    media, payload, _, _ = router(tmp_path, jobs, ['{"candidate":null}'])
    loop.run_until_complete(media.reselect_source(KEY, payload, jobs.public(KEY, SCOPE)))
    assert jobs.get(KEY)['error'] == 'no_suitable_remaining_candidate'
    assert len(jobs.get(KEY)['remaining_candidates']) == 2


def test_original_worker_reuses_download_and_produces_transport_wav(loop, tmp_path, monkeypatch):
    from scripts.qq_companion.run_cover_job import run
    from services.media_api.song_audio import inspect_audio
    jobs, source = submit(tmp_path, mode='original'), AudioSource()
    jobs.reject_download(KEY, 1)
    jobs.reselect(KEY, SCOPE, 2, 1)
    prepare_source(jobs, KEY, source=source)
    # The sandbox scheduler has no subprocess transport; execute the real
    # ffmpeg command synchronously here, keeping production worker code intact.
    async def local_run(argv, timeout, **kwargs):
        import subprocess
        subprocess.run(argv, timeout=timeout, check=True, capture_output=True)
    monkeypatch.setattr('services.media_api.song_audio._run', local_run)
    loop.run_until_complete(run(jobs, KEY))
    info = inspect_audio(jobs.output(KEY), str(Path(__file__).resolve().parents[1] / 'tools/ffmpeg/bin/ffprobe.exe'))
    assert source.calls == [2] and info['sample_rate'] == 24000 and info['channels'] == 1
    assert abs(info['seconds'] - 6) < .1


def test_identical_recording_remaining_cannot_be_declared_unsuitable(loop, tmp_path):
    jobs = submit(tmp_path)
    song = {**POOL[0], 'title': '雨爱'}
    jobs.update(KEY, 'waiting_gpu', song=song)
    jobs.reject_download(KEY, 1)
    media, payload, _, _ = router(tmp_path, jobs, ['{"candidate":null}', '{"candidate":2}'])
    loop.run_until_complete(media.reselect_source(KEY, payload, jobs.public(KEY, SCOPE)))
    assert jobs.get(KEY)['status'] == 'waiting_gpu' and jobs.get(KEY)['song']['song_id'] == 2
