import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from bot.security import Vault

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    token: str
    api_id: int
    api_hash: str
    encryption_key: str
    data_dir: Path
    public_url: str
    port: int = 10000
    send_interval: float = 5
    max_users: int = 50
    max_senders: int = 3


def load_config():
    load_dotenv(ROOT / '.env')
    key = os.environ['SESSION_ENCRYPTION_KEY']
    Vault(key)  # Fail early, never silently create a replacement key.
    url = (os.getenv('PUBLIC_BASE_URL') or os.getenv('RENDER_EXTERNAL_URL') or '').rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('PUBLIC_BASE_URL must be an HTTPS origin')
    directory = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
    directory.mkdir(parents=True, exist_ok=True)
    config = Config(os.environ['BOT_TOKEN'], int(os.environ['API_ID']), os.environ['API_HASH'], key,
                    directory, url, int(os.getenv('PORT', '10000')),
                    float(os.getenv('SEND_INTERVAL_SECONDS', '5')),
                    int(os.getenv('MAX_ACTIVE_USERS', '50')), int(os.getenv('MAX_CONCURRENT_SENDERS', '3')))
    if not config.token or config.api_id <= 0 or not config.api_hash or not 3 <= config.send_interval <= 3600:
        raise ValueError('Invalid configuration')
    if not 1 <= config.max_users <= 500 or not 1 <= config.max_senders <= 20:
        raise ValueError('Invalid concurrency limits')
    return config
