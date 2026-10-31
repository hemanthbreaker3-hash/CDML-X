"""Per-user persistent download API manager for /api."""
import asyncio
import re
from urllib.parse import urlparse
import requests
from pyrogram import filters
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from .. import LOGGER, user_data
from ..core.tg_client import TgClient
from ..core.config_manager import Config
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.ext_utils.db_handler import database

DOMAIN = "gdflix.dev"
DEFAULT_API = "https://ddl-y1lj.onrender.com/api/gdflix"
_PENDING = "API_PENDING"

def _valid_api(value):
    try:
        p = urlparse(value.strip())
        return p.scheme == "https" and bool(p.netloc) and "/api/" in p.path and not any(c.isspace() for c in value)
    except Exception:
        return False

def _is_private_chat(message):
    """Handle Pyrogram enum values and string chat types consistently."""
    chat = getattr(message, "chat", None)
    chat_type = getattr(chat, "type", None)
    # Pyrogram forks expose this as an enum, a string, or "ChatType.PRIVATE".
    value = str(getattr(chat_type, "value", chat_type) or "").lower()
    return value == "private" or value.endswith(".private")

def _is_gdflix(url):
    try:
        host = (urlparse(url).hostname or "").lower()
        return host == DOMAIN or host.endswith("." + DOMAIN)
    except Exception:
        return False

async def resolve_gdflix(url, user_id):
    """Resolve only gdflix.dev URLs using the user's API, or global API when enabled."""
    if not _is_gdflix(url):
        return None
    data = user_data.setdefault(int(user_id), {})
    apis = data.get("DOWNLOAD_APIS", [])
    # Any user who enables Global API shares their first configured endpoint with all users.
    # Per-user endpoints remain private and are used as fallback when no global endpoint resolves.
    global_api = None
    for owner_id, owner_data in user_data.items():
        if owner_data.get("GLOBAL_API_ENABLED"):
            owner_apis = owner_data.get("DOWNLOAD_APIS", [])
            if owner_apis and _valid_api(owner_apis[0]):
                global_api = owner_apis[0]
                break
    candidates = ([global_api] if global_api else []) + [a for a in apis if _valid_api(a)]
    for api in candidates:
        try:
            def _request():
                r = requests.get(api, params={"url": url}, timeout=120)
                r.raise_for_status()
                result = r.json()
                return result if isinstance(result, str) and result.startswith("https://") else None
            result = await asyncio.to_thread(_request)
            if result:
                return result
        except (requests.RequestException, ValueError) as exc:
            LOGGER.warning("Download API failed for user %s (%s): %s", user_id, api, exc)
    return None

async def _save(user_id):
    try:
        await database.update_user_data(int(user_id))
    except Exception as exc:
        LOGGER.warning("Could not persist API settings for %s: %s", user_id, exc)

def _menu(user_id):
    data = user_data.setdefault(int(user_id), {})
    apis = data.get("DOWNLOAD_APIS", [])
    bm = ButtonMaker()
    bm.data_button("➕ Add API", f"api:add:{user_id}")
    if apis:
        bm.data_button("♻️ Replace API", f"api:replace:{user_id}")
        bm.data_button("🗑 Delete API", f"api:delete:{user_id}")
    bm.data_button("🌐 Global API: " + ("ON" if data.get("GLOBAL_API_ENABLED") else "OFF"), f"api:global:{user_id}")
    return bm.build_menu(2)

async def api_command(_, message):
    uid = message.from_user.id if message.from_user else 0
    if not uid:
        return
    if not _is_private_chat(message):
        me = await TgClient.bot.get_me()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open API settings", url=f"https://t.me/{me.username}?start=api")]])
        await message.reply_text("🔐 Open the bot's private chat to manage your download APIs.", reply_markup=kb)
        return
    text = "<b>🔗 Download API Manager</b>\n\nAdd unlimited API endpoints. Each endpoint must accept a <code>url</code> query parameter and return the resolved HTTPS URL as a JSON string.\n\nExample:\n<code>https://ddl-y1lj.onrender.com/api/gdflix?url=https://gdflix.dev/file/wwJRE7SdHJGwbPP</code>\n\nYour API settings are saved persistently. Global API mode is a per-user toggle; when enabled, the global endpoint is tried first."
    await message.reply_text(text, reply_markup=_menu(uid), disable_web_page_preview=True)

async def api_callback(_, query):
    try:
        _, action, raw_uid = query.data.split(":", 2)
        uid = int(raw_uid)
    except Exception:
        await query.answer("Invalid action", show_alert=True); return
    if not query.from_user or query.from_user.id != uid:
        await query.answer("These controls belong to another user.", show_alert=True); return
    data = user_data.setdefault(uid, {})
    if action in ("add", "replace"):
        data[_PENDING] = action
        await query.message.reply_text("Send the full HTTPS API endpoint now. Example:\n<code>https://ddl-y1lj.onrender.com/api/gdflix</code>\n\nSend /cancel to stop.", disable_web_page_preview=True)
        await query.answer()
    elif action == "delete":
        apis = data.get("DOWNLOAD_APIS", [])
        if not apis:
            await query.answer("No configured APIs.", show_alert=True); return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 {i+1}. {api[:35]}", callback_data=f"api:delidx:{uid}:{i}")] for i,api in enumerate(apis)])
        await query.message.reply_text("Choose the API to delete:", reply_markup=kb); await query.answer()
    elif action == "delidx":
        # handled by the four-field callback below
        await query.answer()
    elif action == "global":
        data["GLOBAL_API_ENABLED"] = not bool(data.get("GLOBAL_API_ENABLED", False))
        await _save(uid)
        await query.message.edit_reply_markup(reply_markup=_menu(uid))
        await query.answer("Global API mode updated")

async def api_delete_index(_, query):
    try:
        _, _, raw_uid, raw_idx = query.data.split(":", 3); uid=int(raw_uid); idx=int(raw_idx)
    except Exception:
        await query.answer("Invalid action", show_alert=True); return
    if not query.from_user or query.from_user.id != uid:
        await query.answer("Not your settings.", show_alert=True); return
    data=user_data.setdefault(uid, {}); apis=data.get("DOWNLOAD_APIS", [])
    if 0 <= idx < len(apis):
        removed=apis.pop(idx); data["DOWNLOAD_APIS"]=apis; await _save(uid)
        await query.message.reply_text("✅ Deleted API:\n<code>"+removed+"</code>", reply_markup=_menu(uid), disable_web_page_preview=True)
    await query.answer()

async def api_input(_, message):
    if not message.from_user or not _is_private_chat(message): return
    uid=message.from_user.id; data=user_data.setdefault(uid, {})
    pending=data.get(_PENDING)
    if not pending: return
    value=(message.text or "").strip()
    if value == "/cancel":
        data.pop(_PENDING, None); await message.reply_text("❎ API setup cancelled."); return
    if not _valid_api(value):
        await message.reply_text("⚠️ Invalid API endpoint. Use a full HTTPS URL containing <code>/api/</code> and no spaces."); return
    apis=list(data.get("DOWNLOAD_APIS", []))
    if pending == "replace" and apis:
        data["DOWNLOAD_APIS"]=[value]
    else:
        if value not in apis: apis.append(value)
        data["DOWNLOAD_APIS"]=apis
    data.pop(_PENDING, None); await _save(uid)
    await message.reply_text("✅ API saved permanently.\n<code>"+value+"</code>", reply_markup=_menu(uid), disable_web_page_preview=True)

def register_api_handlers(bot):
    # The project's CMD_SUFFIX is appended to every command (e.g. CMD_SUFFIX="h"
    # means /apih). Support the configured command and the unsuffixed alias.
    # Telegram's bot mention follows the command: /apih@BotUsername.
    suffix = str(getattr(Config, "CMD_SUFFIX", "") or "")
    names = {"api", f"api{suffix}"}
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    api_filter = filters.regex(rf"(?i)^/(?:{alternatives})(?:@[A-Za-z0-9_]+)?(?:\s|$)")
    bot.add_handler(MessageHandler(api_command, api_filter), group=-1)
    bot.add_handler(CallbackQueryHandler(api_callback, filters.regex(r"^api:(?:add|replace|delete|global):")))
    bot.add_handler(CallbackQueryHandler(api_delete_index, filters.regex(r"^api:delidx:")))
    bot.add_handler(MessageHandler(api_input, filters.private & filters.text & ~api_filter), group=5)
