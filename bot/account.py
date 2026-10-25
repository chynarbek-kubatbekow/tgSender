import asyncio
import re
import time
from dataclasses import dataclass

from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import StringSession


@dataclass(frozen=True)
class Result:
    id: int
    title: str
    peer: object
    username: str = ''
    eligible: bool = True
    reason: str = ''


def eligibility(entity):
    if not isinstance(entity, (types.Channel, types.Chat)):
        return False, 'Не группа или канал'
    if getattr(entity, 'deactivated', False) or getattr(entity, 'restricted', False):
        return False, 'Доступ ограничен'
    rights = getattr(entity, 'admin_rights', None)
    creator = getattr(entity, 'creator', False)
    if getattr(entity, 'broadcast', False):
        allowed = creator or bool(rights and rights.post_messages)
        return bool(allowed), '' if allowed else 'Только администраторам'
    if getattr(entity, 'gigagroup', False) and not (creator or rights):
        return False, 'Только администраторам'
    if getattr(entity, 'left', False) and getattr(entity, 'join_to_send', True):
        return False, 'Требуется вступление'
    if not (creator or rights):
        for restriction in (getattr(entity, 'banned_rights', None), getattr(entity, 'default_banned_rights', None)):
            if restriction and (restriction.send_messages or getattr(restriction, 'send_plain', False)
                                or restriction.view_messages):
                return False, 'Отправка текста запрещена'
    return True, ''


class UserAccount:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.client = None
        self.validated = False
        self.phone, self.phone_hash, self.nonce = '', '', ''
        self.auth_expires = 0.0

    @property
    def configured(self):
        return bool(self.store.load_session())

    def make_client(self, session=''):
        return TelegramClient(StringSession(session), self.config.api_id, self.config.api_hash,
                              timeout=15, request_retries=0, connection_retries=2, flood_sleep_threshold=0)

    async def connect(self):
        if self.client and self.client.is_connected() and self.validated:
            return self.client
        session = self.store.load_session()
        if not session:
            raise ValueError('Account is not connected')
        if not self.client:
            self.client = self.make_client(session)
        await asyncio.wait_for(self.client.connect(), 25)
        me = await asyncio.wait_for(self.client.get_me(), 20)
        if not me or me.bot or me.id != self.store.user_id:
            await self.close()
            raise ValueError('Account owner mismatch')
        self.validated = True
        return self.client

    async def begin(self, phone, nonce):
        if self.configured:
            raise ValueError('Account already connected')
        await self.close()
        self.client = self.make_client()
        await asyncio.wait_for(self.client.connect(), 25)
        sent = await asyncio.wait_for(self.client.send_code_request(phone), 25)
        self.phone, self.phone_hash, self.nonce = phone, sent.phone_code_hash, nonce
        self.auth_expires = time.monotonic() + 300

    async def sign_in(self, nonce, code='', password=''):
        if (not self.client or not self.phone_hash or self.nonce != nonce
                or time.monotonic() > self.auth_expires):
            raise ValueError('Login expired')
        try:
            if password:
                await asyncio.wait_for(self.client.sign_in(password=password), 30)
            else:
                await asyncio.wait_for(self.client.sign_in(self.phone, code=code, phone_code_hash=self.phone_hash), 25)
        except errors.SessionPasswordNeededError:
            return 'password'
        me = await asyncio.wait_for(self.client.get_me(), 20)
        if not me or me.bot or me.id != self.store.user_id:
            try:
                await asyncio.wait_for(self.client.log_out(), 20)
            finally:
                await self.close()
            raise ValueError('Account owner mismatch')
        self.store.save_session(self.client.session.save())
        self.validated = True
        self.clear_auth()
        return 'connected'

    def clear_auth(self):
        self.phone, self.phone_hash, self.nonce = '', '', ''
        self.auth_expires = 0

    async def disconnect(self):
        revoked = False
        try:
            client = await self.connect()
            revoked = bool(await asyncio.wait_for(client.log_out(), 20))
        except Exception:
            pass
        finally:
            self.store.save_session('')
            self.store.save_workspace({})
            await self.close()
        return revoked

    async def chats(self, folder=''):
        client = await self.connect()
        wanted = None
        if folder:
            match = re.fullmatch(r'https://t\.me/addlist/([A-Za-z0-9_-]{1,200})', folder.strip())
            if not match:
                raise ValueError('Invalid folder URL')
            invite = await client(functions.chatlists.CheckChatlistInviteRequest(match[1]))
            peers = getattr(invite, 'peers', None)
            if peers is None:
                peers = list(invite.already_peers) + list(invite.missing_peers)
            wanted = {utils.get_peer_id(peer) for peer in peers}
        result = []
        async for dialog in client.iter_dialogs(limit=1000):
            if not isinstance(dialog.entity, (types.Chat, types.Channel)):
                continue
            if wanted is not None and dialog.id not in wanted:
                continue
            allowed, reason = eligibility(dialog.entity)
            result.append(Result(dialog.id, dialog.name or str(dialog.id), dialog.input_entity,
                                 getattr(dialog.entity, 'username', '') or '', allowed, reason))
        return sorted(result, key=lambda chat: chat.title.casefold())

    async def search_public(self, query):
        client = await self.connect()
        found = await client(functions.contacts.SearchRequest(q=query, limit=50))
        result = []
        for entity in found.chats[:50]:
            if not isinstance(entity, types.Channel) or not entity.username:
                continue
            # Resolve full objects: rights from a min constructor are not authoritative.
            entity = await client.get_entity(entity)
            if not getattr(entity, 'left', True):
                continue  # External search: exclude chats the account already belongs to.
            allowed, reason = eligibility(entity)
            result.append(Result(utils.get_peer_id(entity), entity.title, utils.get_input_peer(entity),
                                 entity.username or '', allowed, reason))
        return result

    async def close(self):
        self.validated = False
        self.clear_auth()
        if self.client:
            await self.client.disconnect()
        self.client = None
