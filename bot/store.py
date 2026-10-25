import sqlite3
import time
import uuid


class Database:
    """Shared connection; each tenant operation explicitly scopes its SQL by user_id."""

    def __init__(self, path, vault):
        self.vault = vault
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS v2_users (
                user_id INTEGER PRIMARY KEY, session BLOB, workspace BLOB,
                cooldown REAL NOT NULL DEFAULT 0,
                auth_attempts INTEGER NOT NULL DEFAULT 0,
                auth_blocked REAL NOT NULL DEFAULT 0,
                next_code REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS v2_campaigns (
                id TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES v2_users(user_id),
                created REAL NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(user_id, id));
            CREATE TABLE IF NOT EXISTS v2_deliveries (
                user_id INTEGER NOT NULL, campaign TEXT NOT NULL, peer INTEGER NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(user_id, campaign, peer),
                FOREIGN KEY(user_id, campaign) REFERENCES v2_campaigns(user_id, id));
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(v2_users)')}
        if 'next_code' not in columns:
            self.db.execute('ALTER TABLE v2_users ADD COLUMN next_code REAL NOT NULL DEFAULT 0')
        self.db.execute("UPDATE v2_campaigns SET status='interrupted' WHERE status='running'")
        self.db.execute("UPDATE v2_deliveries SET status='unknown' WHERE status='sending'")
        self.db.commit()

    def tenant(self, user_id):
        if type(user_id) is not int or user_id <= 0:
            raise ValueError('Invalid tenant')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO v2_users(user_id) VALUES (?)', (user_id,))
        return TenantStore(self, user_id)

    def close(self):
        self.db.close()


class TenantStore:
    def __init__(self, database, user_id):
        self.database, self.user_id = database, user_id
        self.db = database.db

    def load_session(self):
        blob = self.db.execute('SELECT session FROM v2_users WHERE user_id=?', (self.user_id,)).fetchone()[0]
        return self.database.vault.open(self.user_id, blob) if blob else ''

    def save_session(self, session):
        blob = self.database.vault.seal(self.user_id, session) if session else None
        with self.db:
            self.db.execute('UPDATE v2_users SET session=? WHERE user_id=?', (blob, self.user_id))

    def workspace(self):
        blob = self.db.execute('SELECT workspace FROM v2_users WHERE user_id=?', (self.user_id,)).fetchone()[0]
        return self.database.vault.open(self.user_id, blob) if blob else {}

    def save_workspace(self, workspace):
        blob = self.database.vault.seal(self.user_id, workspace)
        with self.db:
            self.db.execute('UPDATE v2_users SET workspace=? WHERE user_id=?', (blob, self.user_id))

    def auth_state(self):
        return self.db.execute('SELECT auth_attempts, auth_blocked FROM v2_users WHERE user_id=?', (self.user_id,)).fetchone()

    def auth_save(self, attempts, blocked_until):
        with self.db:
            self.db.execute('UPDATE v2_users SET auth_attempts=?, auth_blocked=? WHERE user_id=?',
                            (attempts, blocked_until, self.user_id))

    def create(self, peers):
        key = uuid.uuid4().hex[:16]
        with self.db:
            self.db.execute("INSERT INTO v2_campaigns VALUES (?, ?, ?, 'running')", (key, self.user_id, time.time()))
            self.db.executemany("INSERT INTO v2_deliveries(user_id, campaign, peer, status) VALUES (?, ?, ?, 'pending')",
                                [(self.user_id, key, peer) for peer in peers])
        return key

    def delivery(self, key, peer, status, detail=''):
        with self.db:
            self.db.execute('UPDATE v2_deliveries SET status=?, detail=? WHERE user_id=? AND campaign=? AND peer=?',
                            (status, detail, self.user_id, key, peer))

    def finish(self, key, status):
        with self.db:
            self.db.execute('UPDATE v2_campaigns SET status=? WHERE user_id=? AND id=?', (status, self.user_id, key))

    def cooldown_until(self):
        return self.db.execute('SELECT cooldown FROM v2_users WHERE user_id=?', (self.user_id,)).fetchone()[0]

    def set_cooldown(self, until):
        with self.db:
            self.db.execute('UPDATE v2_users SET cooldown=max(cooldown, ?) WHERE user_id=?', (until, self.user_id))

    def report(self):
        row = self.db.execute('SELECT id, status FROM v2_campaigns WHERE user_id=? ORDER BY created DESC LIMIT 1', (self.user_id,)).fetchone()
        if not row:
            return 'Рассылок пока нет.'
        labels = {'sent': 'Отправлено', 'pending': 'Не отправлено', 'unknown': 'Результат неизвестен',
                  'sending': 'Отправляется', 'error': 'Ошибки'}
        states = {'running': 'в работе', 'completed': 'завершена', 'interrupted': 'прервана перезапуском',
                  'stopped': 'остановлена', 'rate_limited': 'остановлена: лимит Telegram',
                  'restricted': 'ограничение аккаунта', 'uncertain': 'проверьте доставку', 'failed': 'ошибка'}
        counts = self.db.execute('SELECT status, count(*) FROM v2_deliveries WHERE user_id=? AND campaign=? GROUP BY status', (self.user_id, row[0])).fetchall()
        result = f'Рассылка {row[0]}: {states.get(row[1], row[1])}\n'
        result += '\n'.join(f'{labels.get(status, status)}: {count}' for status, count in counts)
        details = self.db.execute("SELECT peer, detail FROM v2_deliveries WHERE user_id=? AND campaign=? AND detail != '' LIMIT 10", (self.user_id, row[0])).fetchall()
        if details:
            result += '\nДетали:\n' + '\n'.join(f'{peer}: {detail}' for peer, detail in details)
        remaining = int(self.cooldown_until() - time.time()) + 1
        if remaining > 0:
            result += f'\nПауза Telegram: ещё {remaining} сек.'
        return result
