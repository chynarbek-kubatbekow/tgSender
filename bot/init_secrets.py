"""Generate a local encryption key without printing it or replacing an existing key."""
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values, set_key

from bot.config import ROOT


def main():
    path = ROOT / '.env'
    if not path.exists():
        path.write_text((ROOT / '.env.example').read_text(encoding='utf-8'), encoding='utf-8')
    if dotenv_values(path).get('SESSION_ENCRYPTION_KEY'):
        print('Encryption key already exists; left unchanged.')
        return
    set_key(str(path), 'SESSION_ENCRYPTION_KEY', Fernet.generate_key().decode())
    print('Encryption key saved to local .env; value hidden. Keep a secure backup.')


if __name__ == '__main__':
    main()
