"""Vercel website configuration. Environment variables override defaults."""
import os

SITE_NAME = os.getenv("SITE_NAME", "CANTARELLABOTS")
SITE_TAGLINE = os.getenv("SITE_TAGLINE", "Your media hub, beautifully organized.")
# Vercel automatically provides VERCEL_URL without the scheme.
_BASE = os.getenv("BASE_URL", "").strip()
if not _BASE:
    _host = os.getenv("VERCEL_URL", "").strip()
    _BASE = ("https://" + _host) if _host else "http://localhost:3000"
BASE_URL = _BASE.rstrip("/")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
