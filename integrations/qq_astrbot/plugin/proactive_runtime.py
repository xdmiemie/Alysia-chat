"""Private-chat planning and due-time decisions; never a second Agent."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
import json
import re

from .diary_jobs import resolve_timezone
from .prompt_loader import load_prompts
from .proactive_store import ProactiveStore
from .reply_content import has_control_markup


PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "actions": {"type": "array", "maxItems": 8, "items": {"type": "object", "additionalProperties": False,
        "properties": {"action": {"type": "string", "enum": ["schedule", "cancel"]},
            "topic": {"type": "string", "minLength": 1, "maxLength": 80},
            "purpose": {"type": "string", "maxLength": 300},
            "after_seconds": {"type": ["integer", "null"], "minimum": 1},
            "due_at": {"type": ["string", "null"]}},
        "required": ["action", "topic", "purpose", "after_seconds", "due_at"]}}}, "required": ["actions"]}
REPLY_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "message": {"type": ["string", "null"], "maxLength": 300},
    "thinking_level": {"type": "integer", "minimum": 1, "maximum": 5}},
    "required": ["message", "thinking_level"]}
REVIEW_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "event_present": {"type": "boolean"},
    "event_finished": {"type": "boolean"},
    "contact_blocked": {"type": "boolean"},
    "periodic_has_content": {"type": "boolean"},
    "greeting": {"type": ["string", "null"], "enum": ["morning", None]},
    "focus": {"type": ["string", "null"], "maxLength": 200}},
    "required": ["event_present", "event_finished", "contact_blocked", "periodic_has_content", "greeting", "focus"]}


class ProactiveInterrupted(Exception):
    pass


class ProactiveUnavailable(Exception):
    pass


class ProactiveRuntime:
    def __init__(self, owner, directory):
        self.owner = owner
        self.store = ProactiveStore(directory)
        self.task = None
        self.closed = False
        self.config = self.store.configure(owner._ui_config)

    def configure(self, values):
        self.config = self.store.configure(values)

    def observe(self, event, incoming, *, replace_text=False):
        if incoming.group_id or getattr(self.owner, 'test_peer', ''):
            return
        binding = {'scope': incoming.scope, 'origin': event.unified_msg_origin,
            'transport': event.get_extra('qq_transport_origin') or event.unified_msg_origin,
            'platform': event.get_platform_id(), 'bot': incoming.bot_uin, 'recipient': incoming.sender_uin}
        if replace_text:
            with self.store.connection() as db:
                db.execute("UPDATE peers SET message=? WHERE scope=? AND message_id=?",
                    (incoming.text[:12000], incoming.scope, incoming.message_id))
        else:
            self.store.observe(binding, incoming.message_id, incoming.text, self.config)

    def allowed(self, peer):
        if not re.fullmatch(r'qq:[0-9]+:dm:[0-9]+', peer['scope']):
            return False
        if peer['scope'] != f"qq:{peer['bot']}:dm:{peer['recipient']}":
            return False
        config = self.owner.context.get_config(umo=peer['transport'])
        settings = config.get('platform_settings', {})
        whitelist = {str(v).strip() for v in settings.get('id_whitelist', []) if str(v).strip()}
        if settings.get('enable_id_white_list') and whitelist:
            if not {peer['origin'], peer['transport']} & whitelist:
                if not (settings.get('wl_ignore_admin_on_friend') and peer['recipient'] in
                        {str(v) for v in config.get('admins_id', [])}):
                    return False
        platform = self.owner.context.get_platform_inst(peer['platform'])
        return bool(platform and platform.meta().name == 'aiocqhttp')

    async def start(self):
        self.store.recover()
        # Existing private conversations get their first future periodic slot.
        manager = self.owner.context.conversation_manager
        for conversation in await manager.get_conversations():
            match = re.fullmatch(r'([^:]+):FriendMessage:(qq:([0-9]+):dm:([0-9]+))', conversation.user_id)
            if not match:
                continue
            binding = {'scope': match[2], 'origin': conversation.user_id,
                'transport': f'{match[1]}:FriendMessage:{match[4]}', 'platform': match[1],
                'bot': match[3], 'recipient': match[4]}
            # Plugins initialize before platform adapters; bind existing native
            # conversations now and enforce live permissions at decision/send time.
            self.store.observe(binding, '', '', self.config, seed=True)
        self.task = asyncio.create_task(self.run())

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def enabled(self, kind):
        return self.config['context_plans_enabled' if kind == 'context' else 'periodic_proactive_enabled']

    def check(self, peer, kind, *, foreground=True):
        current = self.store.peer(peer['scope'])
        if (self.closed or not self.enabled(kind) or not current or current['version'] != peer['version']
                or not self.allowed(peer) or (foreground and self.owner.foreground_activity.blocked())):
            raise ProactiveInterrupted()

    async def controlled(self, operation, peer, kind, *, foreground=True):
        task = asyncio.ensure_future(operation)
        try:
            while not task.done():
                self.check(peer, kind, foreground=foreground)
                await asyncio.wait({task}, timeout=.1)
            self.check(peer, kind, foreground=foreground)
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def check_services(self, peer):
        from astrbot.core.star.session_llm_manager import SessionServiceManager
        for origin in {peer['origin'], peer['transport']}:
            if (not await SessionServiceManager.is_session_enabled(origin) or
                    not await SessionServiceManager.is_llm_enabled_for_session(origin)):
                raise ProactiveUnavailable()

    def zone(self):
        # Same operator-configured timezone as background app settings.
        from services.douyin_worker.config import ConfigStore
        return resolve_timezone(ConfigStore().read().timezone)

    def timestamp(self, value=None):
        return datetime.fromtimestamp(self.store.clock() if value is None else value, self.zone()).isoformat()

    async def history(self, peer):
        manager = self.owner.context.conversation_manager
        cid = await manager.get_curr_conversation_id(peer['origin'])
        conversation = await manager.get_conversation(peer['origin'], cid) if cid else None
        if not conversation or conversation.user_id != peer['origin']:
            return None, []
        return cid, json.loads(conversation.history or '[]')

    @staticmethod
    def dialogue(history):
        from .reply_content import dialogue_view
        return [{**m, 'content': m['content'][-2000:]} for m in dialogue_view(history)[-20:]
                if isinstance(m.get('content'), str)]

    async def generate(self, key, payload, schema, peer, kind):
        if key == 'proactive_reply':
            # Classify before introducing persona instructions. A chatty persona
            # must not turn an invalid/finished plan into a reason to contact.
            # First establish the event from actual dialogue. A prior planner's
            # persuasive explanation is not evidence that follow-up is needed.
            review_payload = {k: v for k, v in payload.items() if k != 'purpose'}
            review_payload['latest_user_message'] = next((m.get('content', '')
                for m in reversed(payload.get('conversation', [])) if m.get('role') == 'user'), '')
            review = await self.generate('proactive_review', review_payload, REVIEW_SCHEMA, peer, kind)
            eligible = (review['event_present'] and not review['event_finished']
                if kind == 'context' else review['periodic_has_content'])
            if review['contact_blocked'] or not eligible:
                return {'message': None, 'thinking_level': 1}
            focus = (review['focus'] or '').strip()
            if not any(c.isalnum() for c in focus):
                # The event was approved independently of its old description.
                # Reuse that event's purpose only when the classifier omitted
                # a usable summary, never when contact was rejected.
                focus = payload.get('purpose', '').strip() if kind == 'context' else ''
            if not focus:
                return {'message': None, 'thinking_level': 1}
            # The writer needs the reviewed goal, not an old user utterance to
            # answer again or an invalid purpose to obey. A new user message
            # still interrupts both stages through the peer version check.
            payload = {'now': payload['now'], 'kind': kind, 'verified_focus': focus,
                'greeting': review['greeting'],
                'recent_proactive': payload.get('recent_proactive', [])}
        import jsonschema
        from astrbot.core.agent.message import Message
        from .context_budget import RequestBudget, ContextCapacityError
        from .search_provider import request_search_chat as request_chat
        from .vision_analysis import grammar_schema
        async def call():
            await self.check_services(peer)
            async with self.owner.media.client.admission():
                provider = await self.owner.memory.provider_factory()
                phase = 'proactive_plan' if key == 'proactive_plan' else 'proactive_reply'
                # Planning is an internal decision, not a persona conversation.
                # Give due-time decisions the task first and persona as style data;
                # otherwise a chatty persona can turn old dialogue into a new reply.
                system = load_prompts()[key]
                payload_copy = {**payload, 'conversation': list(payload.get('conversation', []))}
                if key == 'proactive_reply':
                    persona = await self.owner.memory.persona_factory()
                    payload_copy['persona_style_reference'] = persona
                budget = RequestBudget(provider, phase)
                decision = key in {'proactive_plan', 'proactive_review'}
                # Planning needs time calculations; the separate review is a
                # short classification and writing follows its approved focus.
                thinking_enabled = (provider.provider_config.get('custom_extra_body') or {}).get(
                    'chat_template_kwargs', {}).get('enable_thinking', False)
                budget.output_tokens = 2048 if key == 'proactive_plan' else 512
                request_options = ({'chat_template_kwargs': {'enable_thinking': True},
                    'reasoning_budget_tokens': 1024}
                    if thinking_enabled and key == 'proactive_plan' else {})
                if not decision:
                    request_options['temperature'] = .5
                while True:
                    messages = [Message(role='system', content=system),
                        Message(role='user', content=json.dumps(payload_copy, ensure_ascii=False))]
                    try:
                        fitted, _, _ = await budget.fit(messages)
                        break
                    except ContextCapacityError as exc:
                        if str(exc) != 'current_turn_exceeds_context_capacity' or not payload_copy['conversation']:
                            raise
                        payload_copy['conversation'].pop(0)
                memory_usage = memory_context = None
                if key == 'proactive_reply' and hasattr(self.owner.memory, 'shared') and self.owner.memory.enabled and budget.window:
                    from .shared_memory import RequestContext, encoded, digest
                    from .memory_request import assemble
                    memory_context = RequestContext(self.owner.memory.character, peer['bot'], 'qq', peer['scope'],
                        purpose='proactive_reply', activity='authorized_proactive_reply')
                    system += '\n' + load_prompts()['shared_memory_use']
                    async def count_request(value):
                        return await budget.count([Message(role='system',content=system),Message(role='user',content=encoded(value))])
                    payload_copy, memory_usage = await assemble(self.owner.memory.shared, memory_context, payload_copy,
                        count_request=count_request,count_text=budget.count_text,window=budget.window,output=budget.output_tokens,
                        margin=budget.margin_tokens,model=digest(encoded(provider.provider_config)))
                    fitted=[Message(role='system',content=system),Message(role='user',content=encoded(payload_copy))]
                response = await request_chat(provider, phase=phase, session_id=peer['scope'], contexts=fitted,
                    func_tool=None, max_tokens=budget.output_tokens,
                    response_format={'type':'json_schema','json_schema':{'name':key,'strict':True,'schema':grammar_schema(schema)}},
                    request_max_retries=1, queue_priority=20, **request_options)
                if memory_usage:
                    memory_usage['model']=provider.provider_config.get('model',provider.provider_config.get('id'))
                    self.owner.memory.shared.record_usage(memory_context, **memory_usage)
                if response.tools_call_name:
                    raise ValueError('proactive_unexpected_tool_call')
                value = json.loads(response.completion_text or '')
                jsonschema.validate(value, schema)
                if key == 'proactive_reply' and not any(c.isalnum() for c in value['message'] or ''):
                    value['message'] = None
                if key == 'proactive_plan':
                    canceled = {a['topic'] for a in value['actions'] if a['action'] == 'cancel'}
                    normalized = []
                    for action in value['actions']:
                        if action['action'] == 'cancel':
                            action = {**action, 'purpose': '', 'after_seconds': None, 'due_at': None}
                        elif action['topic'] in canceled:
                            # Cancel wins within one decision; a model must not
                            # resurrect the same invalid plan later in its JSON.
                            continue
                        elif action['due_at']:
                            # Same absolute-time precedence used by plan().
                            action = {**action, 'after_seconds': None}
                        normalized.append(action)
                    value['actions'] = normalized
                return value
        return await self.controlled(asyncio.wait_for(call(), 90), peer, kind)

    async def plan(self, peer):
        _, history = await self.history(peer)
        pending = self.store.pending(peer['scope'])
        payload = {'now': self.timestamp(), 'received_at': self.timestamp(peer['last_user']),
            'message': peer['message'], 'conversation': self.dialogue(history),
            'pending_plans': [{'topic': p['topic'], 'purpose': p['purpose'], 'due_at': self.timestamp(p['due'])} for p in pending],
            'pending_limit': self.config['context_plan_limit']}
        result = await self.generate('proactive_plan', payload, PLAN_SCHEMA, peer, 'context')
        actions = []
        for action in result['actions']:
            action = dict(action)
            if action['action'] == 'cancel':
                actions.append(action)
                continue
            if not action['purpose'].strip() or has_control_markup(action['purpose']):
                continue
            if action['due_at']:
                try:
                    due = datetime.fromisoformat(action['due_at'])
                except ValueError:
                    continue
                if due.tzinfo is None:
                    continue
                due = due.timestamp()
            elif action['after_seconds']:
                due = peer['last_user'] + action['after_seconds']
            else:
                continue
            if due <= self.store.clock():
                continue
            actions.append({**action, 'due': due})
        self.check(peer, 'context')
        self.store.apply_plans(peer, actions, self.config['context_plan_limit'])

    async def decide(self, plan):
        peer = self.store.peer(plan['scope'])
        if not peer or not self.allowed(peer):
            self.store.finish(plan['id'], 'canceled', error='recipient_not_available')
            return
        # New user content must first update/cancel obsolete plans.
        if self.config['context_plans_enabled'] and peer['planned_version'] < peer['version']:
            return
        cid, history = await self.history(peer)
        if not cid or not history:
            self.store.finish(plan['id'], 'skipped', error='conversation_unavailable')
            return
        payload = {'now': self.timestamp(), 'kind': plan['kind'], 'topic': plan['topic'], 'purpose': plan['purpose'],
            'due_at': self.timestamp(plan['due']), 'last_user_at': self.timestamp(peer['last_user']) if peer['last_user'] else None,
            'conversation': self.dialogue(history), 'recent_proactive': self.store.recent(peer['scope'])}
        result = await self.generate('proactive_reply', payload, REPLY_SCHEMA, peer, plan['kind'])
        text = (result['message'] or '').strip()
        if not text or has_control_markup(text):
            self.store.finish(plan['id'], 'skipped', error='nothing_to_say' if not text else 'invalid_text')
            return
        await self.send(plan, peer, cid, text, result['thinking_level'])

    async def send(self, plan, peer, cid, text, level):
        from astrbot.core.utils.session_lock import session_lock_manager
        from .reply_pacing import typing_seconds, THINKING_SECONDS
        async with session_lock_manager.acquire_lock(peer['origin']):
            self.check(peer, plan['kind'])
            platform = self.owner.context.get_platform_inst(peer['platform'])
            login = await self.controlled(platform.get_client().call_action('get_login_info', self_id=peer['bot']), peer, plan['kind'])
            login = login.get('data', login)
            if str(login.get('user_id')) != peer['bot']:
                self.store.finish(plan['id'], 'canceled', error='account_mismatch')
                return
            current_cid, _ = await self.history(peer)
            if current_cid != cid:
                raise ProactiveInterrupted()
            self.check(peer, plan['kind'])
            lease = self.owner.foreground_activity.begin()
            reserved = False
            try:
                await self.controlled(asyncio.sleep(typing_seconds(text) + THINKING_SECONDS[level]), peer, plan['kind'], foreground=False)
                await self.check_services(peer)
                self.check(peer, plan['kind'], foreground=False)
                if not self.store.reserve_send(plan, peer, text):
                    return
                reserved = True
                receipt = await platform.get_client().call_action('send_private_msg', self_id=peer['bot'],
                    user_id=int(peer['recipient']), message=[{'type':'text','data':{'text':text}}])
                data = receipt.get('data', receipt) if isinstance(receipt, dict) else {}
                if not data.get('message_id'):
                    raise RuntimeError('proactive_send_unconfirmed')
                self.store.finish(plan['id'], 'sent', text=text, receipt=str(data['message_id']))
                if memory := getattr(self.owner, 'memory', None):
                    memory.record_sent(peer['scope'], text, event_id='proactive:' + str(data['message_id']))
                reserved = False
                manager = self.owner.context.conversation_manager
                conversation = await manager.get_conversation(peer['origin'], cid)
                if conversation and conversation.user_id == peer['origin']:
                    history = json.loads(conversation.history or '[]')
                    history.append({'role': 'assistant', 'content': text})
                    await manager.update_conversation(peer['origin'], cid, history=history)
                    with self.store.connection() as db:
                        db.execute('UPDATE plans SET history_saved=1 WHERE id=?', (plan['id'],))
            except BaseException:
                if reserved:
                    self.store.finish(plan['id'], 'uncertain', text=text, error='interrupted_or_unconfirmed')
                raise
            finally:
                self.owner.foreground_activity.end(lease)

    async def tick(self):
        if self.owner.foreground_activity.blocked() or self.owner.media.client.state.mode() != 'ready':
            return
        if self.config['context_plans_enabled'] and (peer := self.store.planner_peer()):
            if not self.allowed(peer):
                self.store.apply_plans(peer, [], self.config['context_plan_limit'])
            else:
                try:
                    await self.plan(peer)
                except ProactiveUnavailable:
                    self.store.apply_plans(peer, [], self.config['context_plan_limit'])
                except ProactiveInterrupted:
                    return
                except Exception as exc:
                    self.store.retry_planner(peer['scope'])
                    self.store.audit(peer['scope'], 'planning_failed', type(exc).__name__)
            return
        if plan := self.store.due(self.config):
            try:
                await self.decide(plan)
            except ProactiveUnavailable:
                self.store.finish(plan['id'], 'canceled', error='session_disabled')
            except ProactiveInterrupted:
                return
            except Exception as exc:
                self.store.audit(plan['scope'], 'decision_failed', type(exc).__name__)
                # A generation failure may retry later; a sending/uncertain row cannot.
                with self.store.connection() as db:
                    db.execute("UPDATE plans SET due=? WHERE id=? AND status='pending'", (self.store.clock() + 60, plan['id']))

    async def run(self):
        while not self.closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.owner.logger.exception('Private proactive scheduler tick failed')
            await asyncio.sleep(5)
