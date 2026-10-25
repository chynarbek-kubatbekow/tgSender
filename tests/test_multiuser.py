import asyncio
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet
from telethon import errors, types

from bot.auth_web import create_app
from bot.store import Database
from bot.security import Vault, telegram_user
from bot.config import Config
from bot.runtime import Runtime
from bot.sender import Sender
from bot.account import Result, UserAccount, eligibility

TOKEN = '123456:' + 'x' * 35


def signed_data(user_id=7, age=0):
    data = {'auth_date': str(int(time.time()-age)),
            'user': json.dumps({'id': user_id, 'first_name': 'Test'})}
    check = '\n'.join(f'{key}={value}' for key, value in sorted(data.items()))
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    data['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


class IdentityTests(unittest.TestCase):
    def test_valid_signature_and_invalid_or_expired_rejected(self):
        self.assertEqual(telegram_user(signed_data(), TOKEN), 7)
        for data in (signed_data(age=600), signed_data().replace('Test', 'Other'),
                     signed_data() + '&auth_date=0', '', signed_data(age=-100)):
            with self.assertRaises(ValueError):
                telegram_user(data, TOKEN)

    def test_encryption_bound_to_owner(self):
        vault = Vault(Fernet.generate_key().decode())
        blob = vault.seal(7, 'private-session')
        self.assertNotIn(b'private-session', blob)
        self.assertEqual(vault.open(7, blob), 'private-session')
        with self.assertRaises(ValueError):
            vault.open(8, blob)


class TenantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.vault = Vault(Fernet.generate_key().decode())
        self.db = Database(self.path, self.vault)
        self.a, self.b = self.db.tenant(7), self.db.tenant(8)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_cross_tenant_reads_and_writes_are_isolated(self):
        self.a.save_session('a-session')
        self.a.save_workspace({'draft': 'private text'})
        key = self.a.create([123])
        self.b.delivery(key, 123, 'sent')
        self.b.finish(key, 'completed')
        self.b.set_cooldown(time.time()+100)
        self.assertEqual(self.b.load_session(), '')
        self.assertEqual(self.b.workspace(), {})
        self.assertEqual(self.b.report(), 'Рассылок пока нет.')
        self.assertIn('Не отправлено: 1', self.a.report())
        self.assertEqual(self.a.cooldown_until(), 0)
        self.a.delivery(key, 123, 'sent')
        self.assertIn('Отправлено: 1', self.a.report())

    def test_sessions_and_drafts_survive_restart_and_unknown_not_retried(self):
        self.a.save_session('a-session')
        self.a.save_workspace({'draft': 'hello', 'selected': [123]})
        key = self.a.create([123, 456])
        self.a.delivery(key, 123, 'sending')
        self.db.close()
        self.db = Database(self.path, self.vault)
        a = self.db.tenant(7)
        self.assertEqual(a.load_session(), 'a-session')
        self.assertEqual(a.workspace()['selected'], [123])
        self.assertIn('Результат неизвестен: 1', a.report())
        self.assertIn('прервана перезапуском', a.report())


class AuthWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(':memory:', Vault(Fernet.generate_key().decode()))
        config = Config(TOKEN, 123, 'hash', '', Path('.'), 'https://sender.example')
        self.runtime = Runtime(config, self.db)
        self.client = TestClient(TestServer(create_app(self.runtime)))
        await self.client.start_server()
        self.headers = {'Origin': 'https://sender.example', 'X-Telegram-Init-Data': signed_data()}

    async def asyncTearDown(self):
        await self.client.close()
        await self.runtime.close()
        self.db.close()

    async def test_auth_requires_origin_and_signed_fresh_identity(self):
        response = await self.client.post('/api/auth', json={'action': 'status'})
        self.assertEqual(response.status, 403)
        response = await self.client.post('/api/auth', json={'action': 'status'}, headers={'Origin': 'https://sender.example'})
        self.assertEqual(response.status, 401)
        headers = dict(self.headers, **{'X-Telegram-Init-Data': signed_data(age=600)})
        response = await self.client.post('/api/auth', json={'action': 'status'}, headers=headers)
        self.assertEqual(response.status, 401)
        response = await self.client.post('/api/auth', json={'action': 'status', 'user_id': 99}, headers=self.headers)
        self.assertEqual(response.status, 400)

    async def test_successful_phone_code_and_2fa_flow_is_scoped(self):
        context = self.runtime.get(7)
        context.account.begin = AsyncMock()
        context.account.sign_in = AsyncMock(side_effect=['password', 'connected'])
        response = await self.client.post('/api/auth', json={'action': 'begin', 'phone': '+79991234567'}, headers=self.headers)
        data = await response.json()
        self.assertEqual(data['status'], 'code')
        nonce = data['nonce']
        response = await self.client.post('/api/auth', json={'action': 'code', 'nonce': nonce, 'code': '12345'}, headers=self.headers)
        self.assertEqual((await response.json())['status'], 'password')
        response = await self.client.post('/api/auth', json={'action': 'password', 'nonce': nonce, 'password': 'secret'}, headers=self.headers)
        self.assertEqual((await response.json())['status'], 'connected')
        context.account.sign_in.assert_awaited_with(nonce, password='secret')
        self.assertEqual(self.db.tenant(8).load_session(), '')

    async def test_failed_logins_are_rate_limited(self):
        context = self.runtime.get(7)
        context.account.sign_in = AsyncMock(side_effect=ValueError())
        for _ in range(5):
            response = await self.client.post('/api/auth', json={'action': 'code', 'nonce': 'n', 'code': '12345'}, headers=self.headers)
            self.assertEqual(response.status, 400)
        response = await self.client.post('/api/auth', json={'action': 'code', 'nonce': 'n', 'code': '12345'}, headers=self.headers)
        self.assertEqual(response.status, 429)
        self.assertEqual(self.db.tenant(8).auth_state(), (0, 0))

    async def test_phone_code_cooldown_and_body_size(self):
        context = self.runtime.get(7)
        context.account.begin = AsyncMock()
        for expected in (200, 429):
            response = await self.client.post('/api/auth', json={'action': 'begin', 'phone': '+79991234567'}, headers=self.headers)
            self.assertEqual(response.status, expected)
        self.assertEqual(context.account.begin.await_count, 1)
        response = await self.client.post('/api/auth', json={'action': 'code', 'code': 'x'*5000}, headers=self.headers)
        self.assertEqual(response.status, 413)

    async def test_static_page_and_no_cache_or_private_health(self):
        response = await self.client.get('/auth')
        self.assertEqual(response.status, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
        response = await self.client.get('/healthz')
        self.assertEqual(await response.text(), 'ok')
        response = await self.client.get('/assets/unknown')
        self.assertEqual(response.status, 404)


class AccountAndPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(':memory:', Vault(Fernet.generate_key().decode()))
        config = Config(TOKEN, 123, 'hash', '', Path('.'), 'https://sender.example')
        self.runtime = Runtime(config, self.db)

    async def asyncTearDown(self):
        await self.runtime.close()
        self.db.close()

    async def test_wrong_account_is_revoked_and_not_saved(self):
        context = self.runtime.get(7)
        account = context.account
        fake = SimpleNamespace(sign_in=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=8, bot=False)),
                               log_out=AsyncMock(return_value=True), disconnect=AsyncMock())
        account.client = fake
        account.phone, account.phone_hash, account.nonce = '+79991234567', 'hash', 'nonce'
        account.auth_expires = time.monotonic()+300
        with self.assertRaises(ValueError):
            await account.sign_in('nonce', code='12345')
        self.assertEqual(context.store.load_session(), '')
        fake.log_out.assert_awaited_once()

    async def test_code_is_bound_to_own_challenge(self):
        a, b = self.runtime.get(7), self.runtime.get(8)
        a.account.nonce = 'private-nonce'
        with self.assertRaises(ValueError):
            await b.account.sign_in('private-nonce', code='12345')

    async def test_public_channel_requires_admin_group_join_flag_enforced(self):
        channel = types.Channel(id=1, title='Public', photo=types.ChatPhotoEmpty(), date=None,
                                broadcast=True, username='public', left=True)
        self.assertEqual(eligibility(channel), (False, 'Только администраторам'))
        group = types.Channel(id=2, title='Group', photo=types.ChatPhotoEmpty(), date=None,
                              megagroup=True, username='group', left=True, join_to_send=True)
        self.assertFalse(eligibility(group)[0])
        group.join_to_send = False
        self.assertTrue(eligibility(group)[0])

    async def test_search_select_only_eligible_and_single_anchor_message(self):
        context = self.runtime.get(7)
        context.store.save_session('fake')
        sent, edited = [], []
        anchor = SimpleNamespace()
        async def edit(text, **kwargs):
            edited.append((text, kwargs))
            return anchor
        anchor.edit_text = edit
        async def answer(text, **kwargs):
            sent.append((text, kwargs))
            return anchor
        message = SimpleNamespace(answer=answer)
        context.account.search_public = AsyncMock(return_value=[
            Result(1, 'Developers', 1, 'dev', True), Result(2, 'Channel', 2, 'channel', False, 'Только администраторам')])
        await context.panel.search(message, 'программисты')
        self.assertEqual(len(sent), 1)
        self.assertGreaterEqual(len(edited), 1)
        call = SimpleNamespace(message=message, data='all', answer=AsyncMock())
        await context.panel.callback(call)
        self.assertEqual(context.panel.selected, {1})
        self.assertEqual(self.runtime.get(8).panel.selected, set())
        self.assertEqual(len(sent), 1)
        context.panel.draft = 'hello'
        await context.panel.callback(SimpleNamespace(message=message, data='preview', answer=AsyncMock()))
        nonce = context.panel.confirmation[0]
        context.sender.start = lambda chats, text: sent.append(('send', (chats, text))) or True
        for _ in range(2):
            await context.panel.callback(SimpleNamespace(message=message, data=f'send:{nonce}', answer=AsyncMock()))
        self.assertEqual(sum(item[0] == 'send' for item in sent), 1)

    async def test_tenants_send_through_their_own_clients(self):
        a, b = self.runtime.get(7), self.runtime.get(8)
        client_a, client_b = SimpleNamespace(send_message=AsyncMock()), SimpleNamespace(send_message=AsyncMock())
        a.account.connect = AsyncMock(return_value=client_a)
        b.account.connect = AsyncMock(return_value=client_b)
        a.sender.interval = b.sender.interval = 0
        a.sender.start([Result(1, 'A', 1)], 'text-a')
        b.sender.start([Result(2, 'B', 2)], 'text-b')
        await asyncio.gather(a.sender.task, b.sender.task)
        self.assertEqual(client_a.send_message.call_args.args, (1, 'text-a'))
        self.assertEqual(client_b.send_message.call_args.args, (2, 'text-b'))
        self.assertIn('Отправлено: 1', a.store.report())
        self.assertIn('Отправлено: 1', b.store.report())

    async def test_runtime_rejects_group_and_foreign_chat_callbacks(self):
        message = SimpleNamespace(chat=SimpleNamespace(type='group', id=7),
                                  from_user=SimpleNamespace(id=7, is_bot=False), answer=AsyncMock())
        await self.runtime.message(message)
        self.assertEqual(self.runtime.users, {})
        message.chat.type, message.chat.id = 'private', 8
        await self.runtime.message(message)
        self.assertEqual(self.runtime.users, {})
        call = SimpleNamespace(message=message, from_user=message.from_user, answer=AsyncMock())
        await self.runtime.callback(call)
        call.answer.assert_awaited_once()
        self.assertEqual(self.runtime.users, {})

    async def test_global_sender_limit_and_queued_stop(self):
        a, b = self.runtime.get(7), self.runtime.get(8)
        slots = asyncio.Semaphore(1)
        gate = asyncio.Event()
        active = []
        async def send_a(*args, **kwargs):
            active.append('a')
            await gate.wait()
        client_a = SimpleNamespace(send_message=send_a)
        client_b = SimpleNamespace(send_message=AsyncMock())
        a.account.connect = AsyncMock(return_value=client_a)
        b.account.connect = AsyncMock(return_value=client_b)
        a.sender.limiter = b.sender.limiter = slots
        a.sender.interval = b.sender.interval = 0
        a.sender.start([Result(1, 'A', 1)], 'text-a')
        for _ in range(30):
            await asyncio.sleep(0)
            if active:
                break
        b.sender.start([Result(2, 'B', 2)], 'text-b')
        await asyncio.sleep(0)
        self.assertEqual(client_b.send_message.await_count, 0)
        b.sender.stop_event.set()
        await asyncio.wait_for(b.sender.task, 2)
        self.assertEqual(client_b.send_message.await_count, 0)
        gate.set()
        await a.sender.task
