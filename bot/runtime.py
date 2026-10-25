import asyncio
import logging
import time
from dataclasses import dataclass, field

from aiogram import Router
from aiogram.types import Message

from bot.sender import Sender
from bot.account import UserAccount
from bot.panel import UserPanel


@dataclass
class Context:
    config: object
    store: object
    account: object
    sender: object
    panel: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seen: float = field(default_factory=time.monotonic)


class Runtime:
    def __init__(self, config, database):
        self.config, self.database = config, database
        self.users = {}
        self.slots = asyncio.Semaphore(config.max_senders)
        self.router = Router(name='multi-user')
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback)
        self.router.error.register(self.error)

    def get(self, user_id):
        if user_id not in self.users:
            if len(self.users) >= self.config.max_users:
                raise RuntimeError('Active user capacity reached')
            store = self.database.tenant(user_id)
            account = UserAccount(self.config, store)
            sender = Sender(account, store, self.config.send_interval, self.slots)
            context = Context(self.config, store, account, sender)
            context.panel = UserPanel(context)
            self.users[user_id] = context
        context = self.users[user_id]
        context.seen = time.monotonic()
        return context

    async def message(self, message):
        if (message.chat.type != 'private' or not message.from_user or message.from_user.is_bot
                or message.chat.id != message.from_user.id):
            return
        try:
            context = self.get(message.from_user.id)
        except RuntimeError:
            await message.answer('Сервис занят. Попробуйте позже.')
            return
        async with context.lock:
            await context.panel.message(message)

    async def callback(self, call):
        message = call.message
        if (not isinstance(message, Message) or message.chat.type != 'private'
                or call.from_user.is_bot or message.chat.id != call.from_user.id):
            await call.answer('Нет доступа.', show_alert=True)
            return
        try:
            context = self.get(call.from_user.id)
        except RuntimeError:
            await call.answer('Сервис занят.', show_alert=True)
            return
        if context.lock.locked():
            await call.answer('Предыдущее действие ещё выполняется.')
            return
        async with context.lock:
            await context.panel.callback(call)

    async def reap(self):
        while True:
            await asyncio.sleep(60)
            for user_id, context in list(self.users.items()):
                if not context.sender.busy and not context.lock.locked() and time.monotonic() - context.seen > 900:
                    async with context.lock:
                        await context.account.close()
                        self.users.pop(user_id, None)

    async def close(self):
        async def close_one(context):
            await context.sender.close()
            await context.account.close()
        results = await asyncio.gather(*(close_one(context) for context in self.users.values()), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logging.getLogger(__name__).error('Ошибка завершения: %s', type(result).__name__)

    async def error(self, event):
        logging.getLogger(__name__).error('Ошибка действия: %s', type(event.exception).__name__)
        # Do not log Telegram updates, exception text, session strings, or login data.
        return True
