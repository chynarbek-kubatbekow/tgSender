import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from cryptography.fernet import Fernet


def telegram_user(init_data: str, token: str, max_age=300) -> int:
    """Verify the raw Mini App initData; never trust initDataUnsafe or a posted user ID."""
    if not init_data or len(init_data) > 16384:
        raise ValueError('Invalid init data')
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError('Duplicate fields')
    provided = fields.pop('hash', '')
    check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided, expected):
        raise ValueError('Invalid signature')
    age = time.time() - int(fields['auth_date'])
    if not -30 <= age <= max_age:
        raise ValueError('Expired init data')
    user = json.loads(fields['user'])
    user_id = user.get('id')
    if type(user_id) is not int or user_id <= 0 or user.get('is_bot', False):
        raise ValueError('Invalid user')
    return user_id


class Vault:
    def __init__(self, key: str):
        self.fernet = Fernet(key.encode())

    def seal(self, user_id: int, data) -> bytes:
        payload = json.dumps({'user_id': user_id, 'data': data}, ensure_ascii=False).encode()
        return self.fernet.encrypt(payload)

    def open(self, user_id: int, blob: bytes):
        payload = json.loads(self.fernet.decrypt(blob))
        if payload['user_id'] != user_id:
            raise ValueError('Encrypted record owner mismatch')
        return payload['data']
