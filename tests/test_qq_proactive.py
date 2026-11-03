"""Private proactive flows with a real durable queue and isolated fake delivery."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timezone, timedelta
import json
from pathlib import Path
import random
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'external/AstrBot'))

from integrations.qq_astrbot.plugin.proactive_store import DEFAULTS, ProactiveStore, normalize_settings, probability_curve
from integrations.qq_astrbot.plugin.proactive_runtime import ProactiveInterrupted, ProactiveRuntime, PLAN_SCHEMA, REPLY_SCHEMA
from integrations.qq_astrbot.plugin.foreground_activity import ForegroundActivity
from integrations.qq_astrbot.plugin.message_router import Incoming
from scripts.qq_companion.probe_q3_local import LocalLoop


def binding(recipient='2'):
    scope = 'qq:1:dm:' + recipient
    return dict(scope=scope, origin='snow:FriendMessage:' + scope,
                transport='snow:FriendMessage:' + recipient, platform='snow', bot='1', recipient=recipient)


def action(topic='wake', due=20000):
    return dict(action='schedule', topic=topic, purpose='Ask about waking up', due=due)


def review_state(present=True, *, finished=False, blocked=False, focus=None):
    return dict(event_present=present, event_finished=finished, contact_blocked=blocked,
                periodic_has_content=False, greeting=None, focus=focus)


@pytest.fixture
def loop():
    value = LocalLoop()
    yield value
    value.run_until_complete(value.shutdown_asyncgens())
    value.close()


def test_probability_is_increasing_and_actual_mean_is_calibrated():
    for pity in (1, 2, 5, 20):
        for mean in (.10, .30, .75, 1):
            config = normalize_settings({'followup_pity_limit': pity, 'followup_average_probability': mean})
            curve = probability_curve(config['followup_average_probability'], pity)
            assert sorted(curve) == list(curve) and curve[-1] == 1
            survival, expectation = 1., 1.
            for p in curve[:-1]:
                survival *= 1 - p
                expectation += survival
            assert 1 / expectation == pytest.approx(max(1/pity, mean))
    assert normalize_settings({'followup_average_probability': .1})['followup_average_probability'] == .2


def test_draws_persist_reset_are_scoped_and_idempotent(tmp_path):
    rng = NS(random=lambda: .999999)
    store = ProactiveStore(tmp_path, rng=rng)
    for i in range(1, 6):
        draw = store.draw('qq:1:dm:2', str(i), .3, 5)
        assert draw['number'] == i and draw['hit'] == (i == 5)
        assert store.draw('qq:1:dm:2', str(i), .3, 5) == draw
        store = ProactiveStore(tmp_path, rng=rng)
    assert store.draw('qq:1:dm:2', 'six', .3, 5)['number'] == 1
    assert store.draw('qq:1:dm:3', 'other', .3, 5)['number'] == 1
    # Reducing the configured pity threshold cannot lose a previously earned guarantee.
    assert store.draw('qq:1:dm:2', 'changed', .5, 2)['hit'] == 1


def test_queue_cap_update_cancel_scope_version_and_restart(tmp_path):
    store = ProactiveStore(tmp_path, clock=lambda: 1000)
    cfg = store.configure(DEFAULTS)
    store.observe(binding(), 'm1', 'sleep', cfg)
    peer = store.peer(binding()['scope'])
    assert store.apply_plans(peer, [action(str(i)) for i in range(6)], 5) == ['scheduled']*5 + ['queue_full_skipped']
    store = ProactiveStore(tmp_path, clock=lambda: 1000)
    assert len(store.pending(peer['scope'])) == 5
    assert store.apply_plans(peer, [action('0', 40000)], 5) == ['updated']
    assert store.apply_plans(peer, [dict(action='cancel', topic='1'), action('new')], 5) == ['canceled', 'scheduled']
    assert len(store.pending(peer['scope'])) == 5
    store.observe(binding('3'), 'm1', 'sleep', cfg)
    assert store.apply_plans(store.peer(binding('3')['scope']), [action()], 5) == ['scheduled']
    store.observe(binding(), 'm2', 'awake already', cfg)
    assert store.apply_plans(peer, [action()], 5) == ['superseded']


def test_periodic_jitter_restart_disable_skip_and_no_backlog(tmp_path):
    now = [1000]
    store = ProactiveStore(tmp_path, clock=lambda: now[0], rng=random.Random(42))
    cfg = store.configure(DEFAULTS)
    intervals = [store.interval(cfg) / 60 for _ in range(300)]
    assert min(intervals) >= 100 and max(intervals) <= 140 and len(set(intervals)) == 300
    store.observe(binding(), '', '', cfg, seed=True)
    due = store.peer(binding()['scope'])['next_periodic']
    store = ProactiveStore(tmp_path, clock=lambda: now[0], rng=random.Random(7))
    store.configure(cfg)
    assert store.peer(binding()['scope'])['next_periodic'] == due
    now[0] += 86400
    plan = store.due(cfg)
    assert plan['kind'] == 'periodic'
    assert now[0]+6000 <= store.peer(binding()['scope'])['next_periodic'] <= now[0]+8400
    store.finish(plan['id'], 'skipped', error='nothing_to_say')
    assert store.due(cfg) is None
    cfg = store.configure({**cfg, 'periodic_proactive_enabled': False})
    assert store.peer(binding()['scope'])['next_periodic'] is None
    cfg = store.configure({**cfg, 'periodic_proactive_enabled': True})
    assert store.peer(binding()['scope'])['next_periodic'] > now[0]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    now = [1804176000.]
    b = binding()
    calls, requests = [], []
    conversation = NS(user_id=b['origin'], history=json.dumps([{'role':'user','content':'I am going to sleep'},
        {'role':'assistant','content':'Good night'}]))
    async def cid(origin): return 'cid'
    async def get(origin, key): return conversation
    async def update(origin, key, history): conversation.history = json.dumps(history)
    async def all_conversations(): return [conversation, NS(user_id='snow:GroupMessage:qq:1:group:3')]
    async def call_action(name, **kwargs):
        calls.append((name, kwargs))
        return {'user_id': 1} if name == 'get_login_info' else {'message_id': 77}
    platform = NS(meta=lambda:NS(name='aiocqhttp'), get_client=lambda:NS(call_action=call_action))
    config = {'platform_settings':{'enable_id_white_list': True, 'id_whitelist':[b['transport']]}}
    @asynccontextmanager
    async def admission(): yield
    async def text_chat(**kwargs):
        requests.append(kwargs)
        return NS(completion_text=json.dumps({'actions':[]}), tools_call_name=[])
    provider = NS(provider_config={'id':'test','max_context_tokens':0}, text_chat=text_chat)
    async def provider_factory(): return provider
    async def persona_factory(): return 'PERSONA'
    owner = NS(_ui_config=DEFAULTS, test_peer='',
        context=NS(conversation_manager=NS(get_curr_conversation_id=cid, get_conversation=get,
            update_conversation=update, get_conversations=all_conversations),
            get_config=lambda **kw:config, get_platform_inst=lambda key:platform),
        foreground_activity=ForegroundActivity(tmp_path),
        media=NS(client=NS(state=NS(mode=lambda:'ready'), admission=admission)),
        memory=NS(provider_factory=provider_factory, persona_factory=persona_factory),
        logger=NS(exception=lambda *a:None))
    rt = ProactiveRuntime(owner, tmp_path)
    rt.store.clock = lambda: now[0]
    rt.zone = lambda: timezone(timedelta(hours=8))
    async def services(peer): pass
    monkeypatch.setattr(rt, 'check_services', services)
    monkeypatch.setattr('integrations.qq_astrbot.plugin.reply_pacing.typing_seconds', lambda _:0)
    monkeypatch.setattr('integrations.qq_astrbot.plugin.reply_pacing.THINKING_SECONDS', {1:0})
    rt.store.observe(b, 'm1', 'I am going to sleep', rt.config)
    return NS(rt=rt, now=now, calls=calls, requests=requests, config=config,
              conversation=conversation, platform=platform, owner=owner, binding=b, provider=provider)


def due_context(value):
    rt = value.rt
    rt.store.apply_plans(rt.store.peer(value.binding['scope']), [action(due=value.now[0])], 5)
    return rt.store.due(rt.config)


def test_planning_uses_received_time_no_cooldown_and_updated_context(runtime, loop):
    v, captured = runtime, []
    async def generated(key, payload, schema, peer, kind):
        captured.append(payload)
        return {'actions':[{'action':'schedule', 'topic':'sleep', 'purpose':'Ask if awake',
                            'after_seconds':8*3600, 'due_at':None}]}
    v.rt.generate = generated
    original = v.now[0]
    v.now[0] += 20
    loop.run_until_complete(v.rt.plan(v.rt.store.peer(v.binding['scope'])))
    plan = v.rt.store.pending()[0]
    assert plan['due'] == original + 8*3600
    assert captured[0]['now'].endswith('+08:00') and captured[0]['pending_limit'] == 5
    assert captured[0]['conversation'][-1]['content'] == 'Good night'
    # No cooldown: another due plan in the same private chat is immediately eligible.
    v.rt.store.apply_plans(v.rt.store.peer(v.binding['scope']), [action('other', v.now[0])], 5)
    assert v.rt.store.due(v.rt.config)['topic'] == 'other'


def test_planning_keeps_persona_out_of_internal_decisions_and_uses_background_priority(runtime, loop):
    v = runtime
    result = loop.run_until_complete(v.rt.generate('proactive_plan', {'now':v.rt.timestamp()},
        PLAN_SCHEMA, v.rt.store.peer(v.binding['scope']), 'context'))
    assert result == {'actions':[]}
    request = v.requests[0]
    assert request['queue_priority'] == 20 and request['func_tool'] is None
    assert request['max_tokens'] == 2048 and request['chat_template_kwargs']['enable_thinking'] is False
    assert 'PERSONA' not in request['contexts'][0].content and '+08:00' in request['contexts'][1].content


@pytest.mark.parametrize('facts', [review_state(False), review_state(finished=True), review_state(blocked=True)])
def test_rejected_proactive_contact_never_loads_persona_or_generates_message(runtime, loop, facts):
    v = runtime
    async def persona():
        raise AssertionError('Persona must not influence contact approval')
    async def generated(**kwargs):
        v.requests.append(kwargs)
        return NS(completion_text=json.dumps(facts), tools_call_name=[])
    v.owner.memory.persona_factory = persona
    v.provider.text_chat = generated
    result = loop.run_until_complete(v.rt.generate('proactive_reply', {'kind':'context'},
        REPLY_SCHEMA, v.rt.store.peer(v.binding['scope']), 'context'))
    assert result == {'message':None, 'thinking_level':1}
    assert len(v.requests) == 1 and not v.calls
    assert 'PERSONA' not in v.requests[0]['contexts'][0].content


def test_placeholder_reply_is_not_deliverable(runtime, loop):
    v = runtime
    outputs = iter([review_state(focus='Ask about the pending event'),
        {'message':'...', 'thinking_level':1}])
    async def generated(**kwargs):
        return NS(completion_text=json.dumps(next(outputs)), tools_call_name=[])
    v.provider.text_chat = generated
    result = loop.run_until_complete(v.rt.generate('proactive_reply', {'now':v.rt.timestamp()},
        REPLY_SCHEMA, v.rt.store.peer(v.binding['scope']), 'context'))
    assert result['message'] is None and not v.calls


def test_planner_cannot_resurrect_a_canceled_topic_and_absolute_time_wins(runtime, loop):
    v = runtime
    old = action('old', v.now[0] + 60)
    v.rt.store.apply_plans(v.rt.store.peer(v.binding['scope']), [old], 5)
    async def generated(**kwargs):
        return NS(completion_text=json.dumps({'actions':[
            {'action':'cancel','topic':'old','purpose':'','after_seconds':None,'due_at':None},
            {'action':'schedule','topic':'old','purpose':'Continue old chat','after_seconds':60,'due_at':None},
            {'action':'schedule','topic':'new','purpose':'A valid future event','after_seconds':60,
             'due_at':v.rt.timestamp(v.now[0]+1800)}]}), tools_call_name=[])
    v.provider.text_chat = generated
    loop.run_until_complete(v.rt.plan(v.rt.store.peer(v.binding['scope'])))
    plans = v.rt.store.pending()
    assert len(plans) == 1 and plans[0]['topic'] == 'new' and plans[0]['due'] == v.now[0]+1800
    assert not v.calls


@pytest.mark.parametrize('kind,focus', [('context','Ask if the journey is finished'),
    ('context','...'), ('periodic','Ask if the journey is finished')])
def test_approved_proactive_contact_supplies_reviewed_focus_before_persona_reply(runtime, loop, kind, focus):
    v = runtime
    v.provider.provider_config['custom_extra_body'] = {'chat_template_kwargs':{'enable_thinking':True}}
    facts = review_state(kind == 'context', focus=focus)
    facts['periodic_has_content'] = kind == 'periodic'
    outputs = iter([facts,
        {'message':'Have you arrived?', 'thinking_level':1}])
    async def generated(**kwargs):
        v.requests.append(kwargs)
        return NS(completion_text=json.dumps(next(outputs)), tools_call_name=[])
    v.provider.text_chat = generated
    payload = {'now':v.rt.timestamp(), 'kind':kind,
        'conversation':[{'role':'user','content':'I just boarded'}], 'purpose':'Ask if the journey is finished'}
    result = loop.run_until_complete(v.rt.generate('proactive_reply', payload,
        REPLY_SCHEMA, v.rt.store.peer(v.binding['scope']), kind))
    assert result['message'] == 'Have you arrived?' and len(v.requests) == 2
    review, reply = v.requests
    assert 'purpose' not in json.loads(review['contexts'][1].content)
    assert json.loads(review['contexts'][1].content)['latest_user_message'] == 'I just boarded'
    assert review['chat_template_kwargs']['enable_thinking'] is False and review['max_tokens'] == 512
    assert reply['chat_template_kwargs']['enable_thinking'] is False and reply['max_tokens'] == 512
    assert 'PERSONA' not in review['contexts'][0].content
    writer_input = json.loads(reply['contexts'][1].content)
    assert writer_input['persona_style_reference'] == 'PERSONA'
    assert 'PERSONA' not in reply['contexts'][0].content
    assert writer_input['verified_focus'] == 'Ask if the journey is finished'
    assert not writer_input.get('conversation') and 'purpose' not in writer_input
    assert 'verified_focus' not in payload
    assert all(r['queue_priority'] == 20 and r['func_tool'] is None for r in v.requests)
    assert not v.calls


@pytest.mark.parametrize('reason', ['new_message','disabled','whitelist'])
def test_inflight_decision_is_cancelled_before_delivery(runtime, loop, reason):
    v = runtime
    async def scenario():
        async def blocked(**kwargs):
            await asyncio.Event().wait()
        v.provider.text_chat = blocked
        task = asyncio.create_task(v.rt.generate('proactive_plan', {}, PLAN_SCHEMA,
            v.rt.store.peer(v.binding['scope']), 'context'))
        await asyncio.sleep(.01)
        if reason == 'new_message': v.rt.store.observe(v.binding, 'm2', 'awake', v.rt.config)
        elif reason == 'disabled': v.rt.configure({**v.rt.config,'context_plans_enabled':False})
        else: v.config['platform_settings']['id_whitelist'] = ['snow:FriendMessage:999']
        with pytest.raises(ProactiveInterrupted): await asyncio.wait_for(task, 1)
        assert not v.calls
    loop.run_until_complete(scenario())


@pytest.mark.parametrize('reply', [None, 'Good morning'])
def test_due_decision_null_skips_or_delivers_to_bound_private_chat_and_saves_history(runtime, loop, reply):
    v = runtime
    async def generated(*args): return {'message':reply, 'thinking_level':1}
    v.rt.generate = generated
    plan = due_context(v)
    loop.run_until_complete(v.rt.decide(plan))
    with v.rt.store.connection() as db:
        row = db.execute('SELECT * FROM plans WHERE id=?',(plan['id'],)).fetchone()
    assert row['status'] == ('sent' if reply else 'skipped')
    if reply:
        assert v.calls[-1] == ('send_private_msg', {'self_id':'1','user_id':2,
            'message':[{'type':'text','data':{'text':reply}}]})
        assert json.loads(v.conversation.history)[-1] == {'role':'assistant','content':reply}
        assert row['history_saved'] == 1
    else:
        assert v.calls == [] and len(json.loads(v.conversation.history)) == 2
    assert not v.owner.foreground_activity.blocked()


def test_unconfirmed_send_never_replays(runtime, loop):
    v = runtime
    plan = due_context(v)
    peer = v.rt.store.peer(v.binding['scope'])
    async def call(name, **kwargs):
        v.calls.append(name)
        return {'user_id':1} if name == 'get_login_info' else {}
    v.platform.get_client = lambda:NS(call_action=call)
    with pytest.raises(RuntimeError, match='unconfirmed'):
        loop.run_until_complete(v.rt.send(plan, peer, 'cid', 'Hello', 1))
    v.rt.store.recover()
    assert v.rt.store.due(v.rt.config) is None
    assert len(json.loads(v.conversation.history)) == 2
    assert not v.owner.foreground_activity.blocked()


def test_account_mismatch_never_sends(runtime, loop):
    v = runtime
    plan = due_context(v)
    async def call(name, **kwargs):
        assert name == 'get_login_info'
        return {'user_id':999}
    v.platform.get_client = lambda:NS(call_action=call)
    loop.run_until_complete(v.rt.send(plan, v.rt.store.peer(v.binding['scope']), 'cid', 'Hello', 1))
    with v.rt.store.connection() as db:
        assert db.execute('SELECT status,error FROM plans WHERE id=?',(plan['id'],)).fetchone()[:] == ('canceled','account_mismatch')
    assert len(json.loads(v.conversation.history)) == 2


def test_disabled_session_cannot_starve_other_peers(runtime, loop):
    from integrations.qq_astrbot.plugin.proactive_runtime import ProactiveUnavailable
    v = runtime
    async def services(peer): raise ProactiveUnavailable()
    v.rt.check_services = services
    # A disabled planner is retired instead of taking the first slot forever.
    loop.run_until_complete(v.rt.tick())
    assert v.rt.store.planner_peer() is None
    plan = due_context(v)
    loop.run_until_complete(v.rt.tick())
    with v.rt.store.connection() as db:
        assert db.execute('SELECT status,error FROM plans WHERE id=?',(plan['id'],)).fetchone()[:] == ('canceled','session_disabled')
    assert not v.calls


def test_oversized_dialogue_trims_old_messages_without_losing_current_input(runtime, loop, monkeypatch):
    from integrations.qq_astrbot.plugin.context_budget import ContextCapacityError
    v, measured = runtime, []
    async def fit(budget, messages, tools=None):
        payload = json.loads(messages[-1].content)
        measured.append(len(payload['conversation']))
        if len(payload['conversation']) > 1:
            raise ContextCapacityError('current_turn_exceeds_context_capacity')
        assert payload['message'] == 'current'
        return messages, 100, 0
    monkeypatch.setattr('integrations.qq_astrbot.plugin.context_budget.RequestBudget.fit', fit)
    payload = {'message':'current','conversation':[{'role':'user','content':str(i)} for i in range(3)]}
    loop.run_until_complete(v.rt.generate('proactive_plan', payload, PLAN_SCHEMA,
        v.rt.store.peer(v.binding['scope']), 'context'))
    assert measured == [3,2,1] and len(payload['conversation']) == 3


def test_recover_submitted_send_and_private_only_observation(runtime, loop):
    v = runtime
    plan = due_context(v)
    assert v.rt.store.reserve_send(plan, v.rt.store.peer(v.binding['scope']), 'Hello')
    v.rt.store.recover()
    assert v.rt.store.due(v.rt.config) is None
    v.rt.observe(NS(), Incoming('1','3','m1','hello',group_id='5'))
    assert v.rt.store.peer('qq:1:group:5') is None
    # Native adapters initialize after plugins. Seed existing private history anyway.
    v.owner.context.get_platform_inst = lambda _:None
    async def scenario():
        await v.rt.start()
        await v.rt.close()
    loop.run_until_complete(scenario())
    assert v.rt.store.peer(v.binding['scope'])['next_periodic'] > v.now[0]


def test_history_owner_is_checked_and_no_cross_conversation_send(runtime, loop):
    v = runtime
    v.conversation.user_id = binding('3')['origin']
    assert loop.run_until_complete(v.rt.history(v.rt.store.peer(v.binding['scope']))) == (None, [])
    plan = due_context(v)
    loop.run_until_complete(v.rt.decide(plan))
    assert not v.calls
