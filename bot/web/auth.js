'use strict';
const tg = window.Telegram?.WebApp;
const $ = id => document.getElementById(id);
let stage = 'phone';
let nonce = '';
let busy = false;

function view(next) {
  stage = next;
  for (const field of ['phone', 'code', 'password']) {
    $(field + '-label').hidden = field !== stage;
    $(field).required = field === stage;
    $(field).disabled = field !== stage;
  }
  const content = {
    phone: ['Шаг 1 из 3', 'Номер телефона', 'Укажите номер аккаунта, из которого вы открыли этого бота.', 'Получить код →'],
    code: ['Шаг 2 из 3', 'Проверьте Telegram', 'Введите код входа, полученный от Telegram. Не отправляйте его сообщением боту.', 'Подтвердить код →'],
    password: ['Шаг 3 из 3', 'Двухэтапная проверка', 'Для этого аккаунта включён дополнительный пароль Telegram.', 'Подключить аккаунт →'],
    connected: ['Готово', 'Аккаунт подключён', 'Закройте форму и нажмите «Проверить подключение» в боте. В следующий раз входить заново не потребуется.', ''],
  }[next];
  $('step').textContent = content[0];
  $('heading').textContent = content[1];
  $('hint').textContent = content[2];
  $('submit').textContent = content[3];
  $('login').hidden = next === 'connected';
  $('restart').hidden = next === 'phone' || next === 'connected';
  $('done').hidden = next !== 'connected';
  if (next === 'connected') for (const field of ['phone', 'code', 'password']) $(field).value = '';
  else $(next).focus();
}

async function api(body) {
  const response = await fetch('/api/auth', {
    method: 'POST', credentials: 'omit', cache: 'no-store',
    headers: {'Content-Type': 'application/json', 'X-Telegram-Init-Data': tg.initData},
    body: JSON.stringify(body), signal: AbortSignal.timeout(60000),
  });
  if (response.status === 401) throw new Error('Форма устарела. Закройте её и откройте заново кнопкой в боте.');
  let data;
  try { data = await response.json(); }
  catch { throw new Error('Сервис временно недоступен. Повторите позже.'); }
  if (!response.ok) throw new Error(data.error || 'Не удалось выполнить действие. Повторите позже.');
  return data;
}

$('login').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  $('submit').disabled = true;
  $('restart').disabled = true;
  $('error').textContent = '';
  try {
    let data;
    if (stage === 'phone') data = await api({action: 'begin', phone: $('phone').value.replace(/[\s()-]/g, '')});
    else if (stage === 'code') data = await api({action: 'code', nonce, code: $('code').value.trim()});
    else data = await api({action: 'password', nonce, password: $('password').value});
    if (data.nonce) nonce = data.nonce;
    $('code').value = '';
    $('password').value = '';
    view(data.status);
  } catch (error) {
    $('password').value = '';
    $('error').textContent = error.name === 'TimeoutError' ? 'Истекло время ожидания. Повторно откройте форму, чтобы проверить результат.' : error.message;
  } finally {
    busy = false;
    $('submit').disabled = false;
    $('restart').disabled = false;
  }
});

$('restart').addEventListener('click', async () => {
  if (busy) return;
  try { await api({action: 'cancel'}); nonce = ''; view('phone'); }
  catch (error) { $('error').textContent = error.message; }
});
$('done').addEventListener('click', () => tg.close());

async function init() {
  if (!tg?.initData) {
    $('error').textContent = 'Откройте эту форму кнопкой «Подключить мой Telegram» в боте.';
    $('submit').disabled = true;
    return;
  }
  tg.ready(); tg.expand();
  try { view((await api({action: 'status'})).status); }
  catch (error) { $('error').textContent = error.message; }
}
init();
