import asyncio
import unittest
import time
from cryptography.fernet import Fernet
from telethon import errors
from bot.account import Result as Chat
from bot.store import Database
from bot.security import Vault
from bot.sender import Sender


class FakeAccount:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def connect(self):
        return self

    async def send_message(self, peer, text, **kwargs):
        self.sent.append((peer, text))
        if self.error:
            raise self.error


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(':memory:', Vault(Fernet.generate_key().decode()))
        self.store = self.db.tenant(7)
        self.chats = [Chat(1, 'One', 1), Chat(2, 'Two', 2)]

    async def asyncTearDown(self):
        self.db.close()

    async def test_one_campaign_and_exact_snapshot(self):
        account = FakeAccount()
        sender = Sender(account, self.store, 0)
        self.assertTrue(sender.start(self.chats, 'hello'))
        self.assertFalse(sender.start(self.chats, 'duplicate'))
        self.chats.clear()
        await sender.task
        self.assertEqual(account.sent, [(1, 'hello'), (2, 'hello')])
        self.assertIn('Отправлено: 2', self.store.report())

    async def test_uncertain_result_stops_without_retry(self):
        account = FakeAccount(TimeoutError())
        sender = Sender(account, self.store, 0)
        sender.start(self.chats, 'hello')
        await sender.task
        self.assertEqual(len(account.sent), 1)
        self.assertIn('Результат неизвестен: 1', self.store.report())
        self.assertIn('Не отправлено: 1', self.store.report())

    async def test_floodwait_persists_cooldown(self):
        account = FakeAccount(errors.FloodWaitError(request=None, capture=30))
        sender = Sender(account, self.store, 0)
        sender.start(self.chats, 'hello')
        await sender.task
        self.assertEqual(len(account.sent), 1)
        self.assertFalse(sender.start(self.chats, 'again'))
        self.assertGreater(self.store.cooldown_until(), time.time())

    async def test_permission_error_skips_chat(self):
        account = FakeAccount()
        async def send(peer, text, **kwargs):
            if peer == 1:
                raise errors.ChatWriteForbiddenError(request=None)
            account.sent.append((peer, text))
        account.send_message = send
        sender = Sender(account, self.store, 0)
        sender.start(self.chats, 'hello')
        await sender.task
        self.assertEqual(account.sent, [(2, 'hello')])
        self.assertIn('Ошибки: 1', self.store.report())

    async def test_stop_interrupts_delay(self):
        account = FakeAccount()
        sender = Sender(account, self.store, 3600)
        sender.start(self.chats, 'hello')
        for _ in range(20):
            await asyncio.sleep(0)
            if account.sent:
                break
        sender.stop_event.set()
        await asyncio.wait_for(sender.task, 1)
        self.assertEqual(len(account.sent), 1)

