import asyncio
import json
import re
import secrets
import time
from pathlib import Path

from aiohttp import web
from telethon import errors

from bot.security import telegram_user

STATIC = Path(__file__).resolve().parent / 'web'
RUNTIME = web.AppKey('runtime', object)


@web.middleware
async def headers(request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = web.Response(status=exc.status, body=exc.body, headers=exc.headers)
    response.headers.update({
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer',
        'Strict-Transport-Security': 'max-age=31536000',
        'Content-Security-Policy': "default-src 'none'; script-src 'self' https://telegram.org; "
                                  "style-src 'self'; connect-src 'self'; img-src 'self'; "
                                  "base-uri 'none'; form-action 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org",
    })
    return response


async def auth_api(request):
    runtime = request.app[RUNTIME]
    if request.headers.get('Origin') != runtime.config.public_url:
        raise web.HTTPForbidden(text='Invalid origin')
    if request.content_type != 'application/json':
        raise web.HTTPUnsupportedMediaType()
    try:
        user_id = telegram_user(request.headers.get('X-Telegram-Init-Data', ''), runtime.config.token)
    except (ValueError, KeyError, TypeError):
        raise web.HTTPUnauthorized(text='Reopen this form from Telegram') from None
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise web.HTTPBadRequest() from None
    allowed = {'action', 'phone', 'code', 'password', 'nonce'}
    if (not isinstance(data, dict) or set(data) - allowed
            or any(not isinstance(value, str) or len(value) > 512 for value in data.values())):
        raise web.HTTPBadRequest(text='Invalid fields')
    action = data.get('action', '')
    if action not in ('status', 'begin', 'code', 'password', 'cancel'):
        raise web.HTTPBadRequest(text='Unknown action')
    try:
        context = runtime.get(user_id)
    except RuntimeError:
        raise web.HTTPServiceUnavailable(text='Service busy') from None
    if context.lock.locked():
        raise web.HTTPConflict(text='Action in progress')
    async with context.lock:
        account, store = context.account, context.store
        if action == 'status':
            return web.json_response({'status': 'connected' if account.configured else 'phone'})
        if action == 'cancel':
            if not account.configured:
                await account.close()
            return web.json_response({'status': 'phone'})
        attempts, blocked = store.auth_state()
        now = time.time()
        if now < blocked:
            return web.json_response({'error': f'Подождите {int(blocked-now)+1} сек. перед новой попыткой.'}, status=429)
        if blocked:
            attempts = 0
        try:
            if action == 'begin':
                phone = data.get('phone', '')
                if not re.fullmatch(r'\+[1-9][0-9]{6,14}', phone):
                    raise ValueError('Invalid phone')
                # One requested code per minute, even across process restarts.
                next_code = store.db.execute('SELECT next_code FROM v2_users WHERE user_id=?', (user_id,)).fetchone()[0]
                if now < next_code:
                    return web.json_response({'error': f'Новый код можно запросить через {int(next_code-now)+1} сек.'}, status=429)
                with store.db:
                    store.db.execute('UPDATE v2_users SET next_code=? WHERE user_id=?', (now+60, user_id))
                nonce = secrets.token_urlsafe(24)
                await account.begin(phone, nonce)
                return web.json_response({'status': 'code', 'nonce': nonce})
            nonce = data.get('nonce', '')
            if action == 'code':
                code = data.get('code', '')
                if not re.fullmatch(r'[0-9]{4,8}', code):
                    raise ValueError('Invalid code')
                status = await account.sign_in(nonce, code=code)
            else:
                password = data.get('password', '')
                if not password or len(password) > 256:
                    raise ValueError('Invalid password')
                status = await account.sign_in(nonce, password=password)
            if status == 'connected':
                store.auth_save(0, 0)
            return web.json_response({'status': status})
        except errors.FloodWaitError as exc:
            store.auth_save(attempts, now + exc.seconds + 1)
            return web.json_response({'error': f'Telegram требует подождать {exc.seconds} сек.'}, status=429)
        except (errors.RPCError, ValueError, TimeoutError, OSError):
            attempts += 1
            store.auth_save(attempts, now+900 if attempts >= 5 else 0)
            if attempts >= 5 and not account.configured:
                await account.close()
            return web.json_response({'error': 'Вход не выполнен. Проверьте данные и срок кода. '
                                      'Подключать можно только тот аккаунт, из которого открыт бот.'}, status=400)


def create_app(runtime):
    app = web.Application(client_max_size=4096, middlewares=[headers])
    app[RUNTIME] = runtime

    async def health(request):
        return web.Response(text='ok')

    async def static(request):
        name = request.match_info.get('name', 'auth.html')
        if name not in {'auth.html', 'auth.js', 'auth.css'}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    app.router.add_get('/healthz', health)
    app.router.add_get('/auth', static)
    app.router.add_get('/assets/{name}', static)
    app.router.add_post('/api/auth', auth_api)
    return app
