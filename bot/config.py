"""
config.py — Central configuration loader.
Reads all secrets from .env and exposes them as typed constants.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Bot ──────────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ["BOT_TOKEN"]

# ── Supabase ─────────────────────────────────────────────────────────────────
SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_KEY: str = os.environ["SUPABASE_KEY"]

# ── Role IDs ─────────────────────────────────────────────────────────────────
SUPER_ADMIN_ID: int = int(os.environ["SUPER_ADMIN_ID"])

# ── Trial duration ────────────────────────────────────────────────────────────
TRIAL_DAYS: int = 3

# ── Telethon API Credentials ──────────────────────────────────────────────────
TELEGRAM_API_ID: int = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "")

# ── Razorpay Payment Gateway ──────────────────────────────────────────────
RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
# ── Groq support agent ────────────────────────────────────────────────────────
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


# ── Webhook Server ──────────────────────────────────────────────────
WEBHOOK_PORT: int = int(os.getenv("WEBHOOK_PORT", "8080"))
