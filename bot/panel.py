import asyncio
import contextlib
import re
import secrets
import time

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo


CATEGORIES = {
    'dev': ('Программисты', 'программисты', ('програм', 'разработ', 'python', 'javascript', 'developer', 'programmer', 'кодинг')),
    'jobs': ('Работа', 'вакансии', ('работ', 'ваканс', 'job', 'hiring')),
    'crypto': ('Криптовалюты', 'криптовалюта', ('крипт', 'crypto', 'bitcoin', 'web3')),
    'business': ('Бизнес', 'бизнес', ('бизнес', 'business', 'предприним')),
}


def buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=title, callback_data=data) for title, data in row
    ] for row in rows])


MENU = buttons([
    [('🌐 Внешний поиск', 'public'), ('📂 Мои чаты', 'mine')],
    [('🗂 Категории', 'categories'), ('✅ Получатели', 'list')],
    [('✏️ Текст сообщения', 'compose'), ('👁 Предпросмотр', 'preview')],
    [('📊 Статус', 'status'), ('⏹ Стоп', 'stop')],
    [('🔗 Папка', 'folder'), ('🔌 Отключить аккаунт', 'disconnect')],
])


class UserPanel:
    def __init__(self, context):
        self.ctx = context
        self.mode = ''
        self.anchor = None
        self.results, self.catalog = [], {}
        saved = context.store.workspace()
        self.selected = set(saved.get('selected', []))
        self.draft = saved.get('draft', '')
        self.source, self.query = 'public', ''
        self.confirmation = None
        self.epoch = secrets.token_hex(5)
        self.last_search = 0.0

    def save(self):
        self.ctx.store.save_workspace({'selected': sorted(self.selected), 'draft': self.draft})

    async def render(self, message, text, markup=MENU):
        if self.anchor is not None:
            try:
                result = await self.anchor.edit_text(text, reply_markup=markup)
                if hasattr(result, 'edit_text'):
                    self.anchor = result
                return
            except TelegramBadRequest as exc:
                if 'message is not modified' in exc.message.lower():
                    return
        result = await message.answer(text, reply_markup=markup)
        if hasattr(result, 'edit_text'):
            self.anchor = result

    async def home(self, message):
        connected = self.ctx.account.configured
        if connected:
            await self.render(message, f'Ваш аккаунт подключён. Выбрано: {len(self.selected)}.\n'
                              'Внешний поиск ищет публичные сообщества вне ваших чатов. '
                              'Отправка доступна только там, где Telegram разрешает писать.')
        else:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='Подключить мой Telegram',
                                     web_app=WebAppInfo(url=self.ctx.config.public_url + '/auth'))
            ], [InlineKeyboardButton(text='Проверить подключение', callback_data='menu')]])
            await self.render(message, 'Подключите свой аккаунт через защищённую форму внутри Telegram.\n'
                              'Номер, код и 2FA не нужно отправлять в переписку. '
                              'Будут доступны только ваши настройки и отправки. '
                              'Сервис получит доступ к подключённой Telegram-сессии; отключить её можно в меню.', markup)

    async def message(self, message):
        text = message.text or ''
        if text in ('/start', '/cancel', '/menu'):
            self.mode, self.confirmation = '', None
            await self.home(message)
            return
        if not self.ctx.account.configured:
            await self.home(message)
            return
        mode, self.mode = self.mode, ''
        if mode == 'compose':
            if not text.strip() or len(text.encode('utf-16-le')) // 2 > 4096:
                self.mode = mode
                await self.render(message, 'Введите обычный текст до 4096 символов Telegram. Медиа пока не поддерживаются.')
                return
            self.draft, self.confirmation = text, None
            self.save()
            await self.render(message, 'Текст сохранён. Выберите получателей и откройте предпросмотр.')
        elif mode in ('public', 'mine'):
            if not 2 <= len(text.strip()) <= 100:
                self.mode = mode
                await self.render(message, 'Введите от 2 до 100 символов для поиска.')
                return
            await self.search(message, text.strip(), mode)
        elif mode == 'folder':
            await self.search(message, text.strip(), 'folder')
        else:
            await self.home(message)

    async def search(self, message, query, source='public', terms=()):
        if self.ctx.sender.busy:
            await self.render(message, 'Поиск недоступен во время вашей рассылки. Нажмите «Стоп» или дождитесь окончания.')
            return
        now = asyncio.get_running_loop().time()
        if now - self.last_search < 3:
            await self.render(message, 'Подождите 3 секунды между поисковыми запросами.')
            return
        remaining = self.ctx.store.cooldown_until() - time.time()
        if remaining > 0:
            await self.render(message, f'Лимит Telegram: подождите {int(remaining)+1} сек.')
            return
        self.last_search = now
        await self.render(message, 'Ищу сообщества…')
        try:
            if source == 'public':
                results = await asyncio.wait_for(self.ctx.account.search_public(query), 60)
            else:
                results = await asyncio.wait_for(self.ctx.account.chats(query if source == 'folder' else ''), 60)
                if source == 'mine' and query:
                    words = terms or (query.casefold(),)
                    results = [chat for chat in results if any(word in (chat.title + ' ' + chat.username).casefold() for word in words)]
        except Exception as exc:
            from telethon.errors import FloodWaitError
            if isinstance(exc, FloodWaitError):
                self.ctx.store.set_cooldown(time.time() + exc.seconds + 1)
            await self.render(message, f'Поиск не выполнен: {type(exc).__name__}. '
                              'Проверьте подключение аккаунта или повторите позже.')
            return
        self.results, self.source, self.query = results, source, query
        # Keep selected recipients from other searches; prune unselected cache entries.
        self.catalog = {key: value for key, value in self.catalog.items() if key in self.selected}
        for chat in results:
            self.catalog[chat.id] = chat
            if not chat.eligible:
                self.selected.discard(chat.id)
        self.epoch, self.confirmation = secrets.token_hex(5), None
        self.save()
        await self.show_list(message)

    async def show_list(self, message, page=0):
        pages = max(1, (len(self.results) + 7) // 8)
        page = max(0, min(page, pages-1))
        rows = []
        for chat in self.results[page*8:page*8+8]:
            label = f"{'✅' if chat.id in self.selected else '⬜'} {chat.title[:38]}"
            if chat.eligible:
                row = [InlineKeyboardButton(text=label, callback_data=f'pick:{self.epoch}:{chat.id}:{page}')]
            else:
                row = [InlineKeyboardButton(text=f'🔒 {chat.title[:26]} · {chat.reason}', callback_data='locked')]
            if re.fullmatch(r'[A-Za-z0-9_]{1,64}', chat.username):
                row.append(InlineKeyboardButton(text='↗', url=f'https://t.me/{chat.username}'))
            rows.append(row)
        controls = buttons([
            [('Выбрать доступных', 'all'), ('Снять найденных', 'none')],
            [('Снять всех', 'clear'), ('Показать выбранных', 'selected')],
            [('◀', f'page:{self.epoch}:{page-1}'), ('▶', f'page:{self.epoch}:{page+1}')],
            [('🌐 Поиск', 'public'), ('Категории', 'categories'), ('Меню', 'menu')],
        ])
        available = sum(chat.eligible for chat in self.results)
        text = (f'Найдено: {len(self.results)}, доступны для выбора: {available}.\n'
                f'Выбрано всего: {len(self.selected)}/1000 · Страница {page+1}/{pages}\n'
                f'Запрос: {self.query or "все"}\n'
                '🔒 — нет права отправки. Публичный поиск ограничен выдачей Telegram (до 50 результатов за запрос).')
        await self.render(message, text, InlineKeyboardMarkup(inline_keyboard=rows+controls.inline_keyboard))

    async def callback(self, call):
        message, data = call.message, call.data or ''
        await call.answer()
        self.mode = ''
        if data == 'menu':
            self.confirmation = None
            await self.home(message)
            return
        if not self.ctx.account.configured:
            await self.home(message)
            return
        if data in ('public', 'mine', 'compose', 'folder'):
            self.mode, self.confirmation = data, None
            prompts = {'public': 'Введите тему или название для поиска вне ваших чатов, например «программисты».',
                       'mine': 'Введите название или тему для поиска среди своих чатов. Для всех своих чатов нажмите кнопку ниже.',
                       'compose': 'Напишите текст для отправки отдельным сообщением. /cancel — отмена.',
                       'folder': 'Пришлите ссылку https://t.me/addlist/... . Автовступления нет.'}
            extra = buttons([[('Все мои чаты', 'loadmine')], [('Меню', 'menu')]]) if data == 'mine' else MENU
            await self.render(message, prompts[data], extra)
        elif data == 'loadmine':
            await self.search(message, '', 'mine')
        elif data == 'categories':
            rows = [[(label, f'cat:{key}')] for key, (label, _, _) in CATEGORIES.items()]
            rows += [[('Искать в моих чатах' if self.source == 'public' else 'Искать вне моих чатов', 'source')], [('Меню', 'menu')]]
            await self.render(message, f'Категория · режим: {"внешний поиск" if self.source == "public" else "мои чаты"}.\n'
                              'Это тематические запросы, а не полный каталог Telegram.', buttons(rows))
        elif data == 'source':
            self.source = 'mine' if self.source == 'public' else 'public'
            await self.render(message, 'Режим изменён. Выберите категорию.', buttons([
                [(label, f'cat:{key}')] for key, (label, _, _) in CATEGORIES.items()]))
        elif data.startswith('cat:') and data[4:] in CATEGORIES:
            _, query, terms = CATEGORIES[data[4:]]
            await self.search(message, query, 'public' if self.source == 'public' else 'mine', terms)
        elif data in ('list', 'selected'):
            if data == 'selected':
                self.results = [chat for key, chat in self.catalog.items() if key in self.selected]
                self.query, self.epoch = 'Выбранные', secrets.token_hex(5)
            await self.show_list(message)
        elif data in ('all', 'none', 'clear'):
            if data == 'all':
                candidates = [chat.id for chat in self.results if chat.eligible and chat.id not in self.selected]
                self.selected.update(candidates[:max(0, 1000-len(self.selected))])
            elif data == 'none':
                self.selected.difference_update(chat.id for chat in self.results)
            else:
                self.selected.clear()
            self.confirmation = None
            self.save()
            await self.show_list(message)
        elif data.startswith(('pick:', 'page:')):
            parts = data.split(':')
            if len(parts) not in (3, 4) or parts[1] != self.epoch:
                return
            try:
                page = int(parts[-1])
                if parts[0] == 'pick':
                    peer = int(parts[2])
                    chat = next((chat for chat in self.results if chat.id == peer and chat.eligible), None)
                    if not chat:
                        return
                    if peer not in self.selected and len(self.selected) >= 1000:
                        await self.render(message, 'Можно выбрать до 1000 получателей на одну рассылку.')
                        return
                    self.selected.symmetric_difference_update({peer})
                    self.confirmation = None
                    self.save()
            except ValueError:
                return
            await self.show_list(message, page)
        elif data == 'locked':
            await self.render(message, 'В этот чат нельзя отправить текст без необходимых прав или вступления. '
                              'Открыть сообщество можно по кнопке ↗. Обход ограничений не выполняется.')
        elif data == 'preview':
            recipients = tuple(chat for key, chat in self.catalog.items() if key in self.selected and chat.eligible)
            if not self.draft or not recipients:
                await self.render(message, 'Напишите текст и выберите доступных получателей. После перезапуска обновите поиск.')
                return
            nonce = secrets.token_hex(12)
            self.confirmation = (nonce, recipients, self.draft)
            await self.render(message, f'Получателей: {len(recipients)}. Текст показан отдельным сообщением.\n'
                              'Отправка от вашего аккаунта с учётом лимитов Telegram. Убедитесь, что публикация разрешена правилами чатов.',
                              buttons([[('Отправить', f'send:{nonce}')], [('Меню', 'menu')]]))
            await message.answer(self.draft)
        elif data.startswith('send:'):
            saved, self.confirmation = self.confirmation, None
            if not saved or data != f'send:{saved[0]}':
                return
            started = self.ctx.sender.start(saved[1], saved[2])
            await self.render(message, 'Рассылка запущена. «Статус» — прогресс.' if started else self.ctx.sender.status)
        elif data == 'status':
            await self.render(message, self.ctx.sender.status if self.ctx.sender.busy else self.ctx.store.report())
        elif data == 'stop':
            self.ctx.sender.stop_event.set()
            self.confirmation = None
            await self.render(message, 'Остановка запрошена. Текущее сообщение может успеть дойти.')
        elif data == 'disconnect':
            await self.render(message, 'Отключить аккаунт? Активная рассылка будет остановлена, '
                              'сохранённая сессия и черновик удалены.',
                              buttons([[('Да, отключить', 'disconnect_yes')], [('Отмена', 'menu')]]))
        elif data == 'disconnect_yes':
            await self.ctx.sender.close()
            revoked = await self.ctx.account.disconnect()
            self.draft, self.selected, self.catalog, self.results = '', set(), {}, []
            self.confirmation = None
            if not revoked:
                await message.answer('Локальная сессия удалена. Отзыв в Telegram не подтверждён: '
                                     'завершите сеанс в Telegram → Настройки → Устройства.')
            await self.home(message)
