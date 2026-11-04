"""
bot/db/supabase_client.py
Singleton Supabase client shared across all DB modules.
"""

from threading import RLock

from supabase import create_client, Client
from bot.config import SUPABASE_URL, SUPABASE_KEY

_client: Client | None = None
_client_lock = RLock()


def get_client() -> Client:
    """Return the singleton Supabase client, creating it on first call."""
    global _client
    with _client_lock:
        if _client is None:
            _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client
