import asyncio
import contextlib
import time
from telethon import errors


class Sender:
    def __init__(self, account, store, interval, limiter=None):
        self.account, self.store, self.interval = account, store, interval
        self.task = None
        self.stop_event = asyncio.Event()
        self.status = 'Рассылка не запущена.'
        self.limiter = limiter

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def start(self, chats, text):
        if self.busy:
            self.status = 'Рассылка уже выполняется.'
            return False
        remaining = self.store.cooldown_until() - time.time()
        if remaining > 0:
            self.status = f'Telegram требует подождать ещё {int(remaining)+1} сек.'
            return False
        if not chats or not text:
            self.status = 'Нужно выбрать чаты и написать текст.'
            return False
        self.stop_event.clear()
        key = self.store.create([chat.id for chat in chats])
        self.status = f'Запуск рассылки: {len(chats)} получателей.'
        self.task = asyncio.create_task(self.run(key, tuple(chats), text))
        return True

    async def wait(self, seconds):
        try:
            await asyncio.wait_for(self.stop_event.wait(), seconds)
            return True
        except TimeoutError:
            return False

    async def run(self, key, chats, text):
        if self.limiter is None:
            await self._run(key, chats, text)
            return
        acquired = False
        try:
            self.status = 'Ожидание свободного отправителя…'
            deadline = asyncio.get_running_loop().time() + 600
            while not self.stop_event.is_set() and asyncio.get_running_loop().time() < deadline:
                try:
                    await asyncio.wait_for(self.limiter.acquire(), 1)
                    acquired = True
                    break
                except TimeoutError:
                    continue
            if acquired:
                await self._run(key, chats, text)
            else:
                self.store.finish(key, 'stopped')
                self.status = self.store.report()
        except asyncio.CancelledError:
            self.store.finish(key, 'interrupted')
            raise
        finally:
            if acquired:
                self.limiter.release()

    async def _run(self, key, chats, text):
        state, current, sent = 'completed', None, 0
        try:
            client = await self.account.connect()
            for chat in chats:
                if self.stop_event.is_set():
                    state = 'stopped'
                    break
                current = chat.id
                self.store.delivery(key, current, 'sending')
                try:
                    await asyncio.wait_for(client.send_message(chat.peer, text, parse_mode=None, link_preview=False), 45)
                except (errors.FloodWaitError, errors.SlowModeWaitError) as exc:
                    self.store.delivery(key, current, 'pending', 'rate_limit')
                    self.store.set_cooldown(time.time() + exc.seconds + 1)
                    state = 'rate_limited'
                    break
                except errors.PeerFloodError:
                    self.store.delivery(key, current, 'error', 'PeerFloodError')
                    state = 'restricted'
                    break
                except errors.ForbiddenError as exc:
                    self.store.delivery(key, current, 'error', type(exc).__name__)
                except (TimeoutError, OSError, errors.RPCError):
                    self.store.delivery(key, current, 'unknown', 'verify_before_retry')
                    state = 'uncertain'
                    break
                else:
                    self.store.delivery(key, current, 'sent')
                    sent += 1
                current = None
                self.status = f'Отправлено: {sent}/{len(chats)}.'
                if await self.wait(self.interval):
                    state = 'stopped'
                    break
        except asyncio.CancelledError:
            if current is not None:
                self.store.delivery(key, current, 'unknown', 'shutdown')
            state = 'interrupted'
            raise
        except Exception:
            if current is not None:
                self.store.delivery(key, current, 'unknown', 'verify_before_retry')
            state = 'failed'
        finally:
            self.store.finish(key, state)
            self.status = self.store.report()

    async def close(self):
        self.stop_event.set()
        if self.busy:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
