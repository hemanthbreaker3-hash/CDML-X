"""Runtime verification controls and multi-shortener administration."""

from asyncio import Lock
from html import escape
from re import findall, search as re_search
from time import time
from uuid import uuid4
from urllib.parse import urlsplit

from aiofiles import open as aiopen
from pyrogram import ContinuePropagation, StopPropagation
from pyrogram.enums import ChatType, ButtonStyle, ChatAction
from pyrogram.filters import create
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import LOGGER, bot_cache, shortener_dict, sudo_users, user_data
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.status_utils import get_readable_time
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.filters import CustomFilters
from ..helper.telegram_helper.message_utils import edit_message, send_message
from ..helper.telegram_helper.tg_utils import (
    is_verification_enabled,
    make_verification_link,
    verification_timeout,
    premium_valid,
)


_SETTINGS_LOCK = Lock()
_PAGE_SIZE = 8


def _duration_seconds(raw):
    """Parse readable durations, plain seconds, or explicit unlimited validity."""
    value = str(raw or "").strip().lower()
    if value in {"never", "permanent", "forever", "unlimited", "infinite"}:
        return -1
    if value.isdigit():
        seconds = int(value)
        if seconds <= 0:
            raise ValueError("Duration must be greater than zero, or use 'never'.")
        return seconds
    unit_seconds = {
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
        "w": 604800, "week": 604800, "weeks": 604800,
        "mo": 2592000, "month": 2592000, "months": 2592000,
        "y": 31536000, "yr": 31536000, "yrs": 31536000, "year": 31536000, "years": 31536000,
    }
    chunks = findall(r"(\d+)\s*(months?|mo|years?|yrs?|yr|y|weeks?|w|days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)", value)
    if not chunks or "".join(number + unit for number, unit in chunks).replace(" ", "") != value.replace(" ", ""):
        raise ValueError("Use a duration such as 30m, 12h, 7d, 2w, 1mo, 1y, seconds, or never.")
    seconds = sum(int(number) * unit_seconds[unit] for number, unit in chunks)
    if seconds <= 0:
        raise ValueError("Duration must be greater than zero, or use 'never'.")
    return seconds


def _format_validity(seconds):
    return "Unlimited (until revoked)" if int(seconds) < 0 else get_readable_time(int(seconds))


async def _save_verify_config():
    await database.update_config({
        "VERIFY_ENABLED": Config.VERIFY_ENABLED,
        "VERIFY_TIMEOUT": Config.VERIFY_TIMEOUT,
    })


async def _save_shorteners():
    async with _SETTINGS_LOCK:
        async with aiopen("shortener.txt", "w", encoding="utf-8") as file:
            for domain, api_key in shortener_dict.items():
                domain = str(domain).strip()
                api_key = str(api_key).strip()
                if domain and api_key:
                    await file.write(f"{domain} {api_key}\n")
        if Config.DATABASE_URL:
            await database.update_private_file("shortener.txt")


def _normalize_domain(value):
    raw = str(value or "").strip()
    if not raw or any(char.isspace() for char in raw):
        raise ValueError("Enter a domain only, for example: example.com")
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Invalid domain. Include a valid host such as example.com.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Enter the shortener domain only, without a path or query.")
    return parsed.netloc.lower()


def _masked_key(value):
    value = str(value or "")
    if len(value) <= 4:
        return "••••"
    return f"••••••{value[-4:]}"


def _shorteners_markup(page=0):
    domains = list(shortener_dict.items())
    page_count = max(1, (len(domains) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(int(page), 0), page_count - 1)
    start = page * _PAGE_SIZE
    current = domains[start:start + _PAGE_SIZE]
    keyboard = [[InlineKeyboardButton("➕ Add Shortener", callback_data="shorteners add", style=ButtonStyle.SUCCESS)]]
    for idx, (domain, _) in enumerate(current, start=start):
        keyboard.append([
            InlineKeyboardButton(f"✏️ Replace #{idx + 1}", callback_data=f"shorteners replace {idx}"),
            InlineKeyboardButton(f"🗑 Delete #{idx + 1}", callback_data=f"shorteners delete {idx}", style=ButtonStyle.DANGER),
        ])
    if page_count > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Previous", callback_data=f"shorteners page {page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{page_count}", callback_data="shorteners noop"))
        if page + 1 < page_count:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"shorteners page {page + 1}"))
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="shorteners close", style=ButtonStyle.DANGER)])
    lines = ["<b>🔗 Shortener Manager</b>", "", f"<b>Configured domains:</b> {len(domains)}"]
    if current:
        lines.append("")
        for idx, (domain, api_key) in enumerate(current, start=start):
            lines.append(f"<b>{idx + 1}.</b> <code>{escape(str(domain))}</code> · API <code>{escape(_masked_key(api_key))}</code>")
    else:
        lines += ["", "No shorteners configured yet. Add a domain and API key to enable random shortener selection."]
    lines += ["", "Use <b>Replace</b> to change a domain/key pair or <b>Delete</b> to remove one."]
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def _show_shorteners(message_or_query, page=0, edit=False):
    text, markup = _shorteners_markup(page)
    if edit and getattr(message_or_query, "message", None):
        await edit_message(message_or_query.message, text, markup)
    else:
        await send_message(message_or_query, text, markup)


async def shorteners_command(_, message):
    if not await CustomFilters.sudo("", message):
        return await send_message(message, "⛔ Only the owner or sudo admins can manage shorteners.")
    await _show_shorteners(message)


async def shorteners_callback(_, query):
    if not await CustomFilters.sudo("", query):
        return await query.answer("Admin access required.", show_alert=True)
    parts = (query.data or "").split()
    if len(parts) < 2:
        return await query.answer("Invalid action.", show_alert=True)
    action = parts[1].lower()
    state_map = bot_cache.setdefault("shortener_setup", {})
    user_id = query.from_user.id
    if action == "add":
        state_map[user_id] = {"mode": "add", "step": "domain", "page": 0}
        try:
            await send_message(user_id, "<b>➕ Add Shortener</b>\nSend the shortener domain only (for example <code>example.com</code>).", InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="shorteners cancel")]]))
        except Exception:
            state_map.pop(user_id, None)
            return await query.answer("Open the bot in your DM and send /start, then try again.", show_alert=True)
        return await query.answer("Continue in your DM.")
    if action == "replace":
        if len(parts) < 3 or not parts[2].isdigit():
            return await query.answer("Invalid shortener selection.", show_alert=True)
        index = int(parts[2])
        domains = list(shortener_dict)
        if index >= len(domains):
            return await query.answer("That shortener no longer exists. Refresh the menu.", show_alert=True)
        state_map[user_id] = {"mode": "replace", "step": "domain", "old_domain": domains[index], "page": index // _PAGE_SIZE}
        try:
            await send_message(user_id, f"<b>✏️ Replace Shortener</b>\nCurrent domain: <code>{escape(domains[index])}</code>\nSend the new domain (or the same domain to replace only the API key).", InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="shorteners cancel")]]))
        except Exception:
            state_map.pop(user_id, None)
            return await query.answer("Open the bot in your DM and send /start, then try again.", show_alert=True)
        return await query.answer("Continue in your DM.")
    if action == "delete":
        if len(parts) < 3 or not parts[2].isdigit():
            return await query.answer("Invalid shortener selection.", show_alert=True)
        index = int(parts[2])
        domains = list(shortener_dict)
        if index >= len(domains):
            return await query.answer("That shortener no longer exists. Refresh the menu.", show_alert=True)
        domain = domains[index]
        shortener_dict.pop(domain, None)
        await _save_shorteners()
        await query.answer(f"Deleted {domain}")
        return await _show_shorteners(query, index // _PAGE_SIZE, edit=True)
    if action == "page":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await query.answer()
        return await _show_shorteners(query, page, edit=True)
    if action == "cancel":
        state_map.pop(user_id, None)
        await query.answer("Cancelled.")
        return await _show_shorteners(query, 0, edit=True)
    if action == "close":
        state_map.pop(user_id, None)
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            await edit_message(query.message, "Shortener manager closed.", None)
        return
    if action == "noop":
        return await query.answer()
    await query.answer("Unknown action.", show_alert=True)


async def _pending_shortener_input(_, __, message):
    user = message.from_user
    if not user or not message.text or message.text.strip().startswith("/"):
        return False
    state = bot_cache.get("shortener_setup", {}).get(user.id)
    return bool(state and await CustomFilters.sudo("", message))


shortener_input_filter = create(_pending_shortener_input)


async def shortener_input(_, message):
    user_id = message.from_user.id
    state_map = bot_cache.setdefault("shortener_setup", {})
    state = state_map.get(user_id)
    if not state:
        return
    value = (message.text or "").strip()
    if value.casefold() in {"cancel", "stop", "exit"}:
        state_map.pop(user_id, None)
        await send_message(message, "Shortener update cancelled.")
        raise StopPropagation

    if state["step"] == "domain":
        try:
            domain = _normalize_domain(value)
        except ValueError as exc:
            await send_message(message, f"⚠️ {escape(str(exc))}")
            raise StopPropagation
        state["domain"] = domain
        state["step"] = "api_key"
        await send_message(message, f"<b>Domain:</b> <code>{escape(domain)}</code>\nNow send the API key. Spaces are not supported in API keys.", InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="shorteners cancel")]]))
        raise StopPropagation

    if state["step"] == "api_key":
        if not value or any(char.isspace() for char in value):
            await send_message(message, "⚠️ API key cannot be empty or contain spaces. Send the key again.")
            raise StopPropagation
        old_domain = state.get("old_domain")
        domain = state.get("domain") or old_domain
        if not domain:
            state_map.pop(user_id, None)
            await send_message(message, "The setup expired. Run /shorteners again.")
            raise StopPropagation
        if old_domain and old_domain != domain:
            shortener_dict.pop(old_domain, None)
        shortener_dict[domain] = value
        await _save_shorteners()
        state_map.pop(user_id, None)
        await send_message(message, f"✅ Shortener saved: <code>{escape(domain)}</code>")
        await _show_shorteners(message, state.get("page", 0))
        raise StopPropagation


async def _resolve_premium_target(raw):
    raw = str(raw or "").strip()
    if not raw:
        raise ValueError("Provide a user ID or @username.")
    if raw.lstrip("@").isdigit():
        return int(raw.lstrip("@"))
    user = await TgClient.bot.get_users(raw.lstrip("@"))
    return int(user.id)


async def addpremium_command(_, message):
    args = (getattr(message, "text", "") or "").split()[1:]
    if len(args) < 2:
        return await send_message(message, "Usage: <code>/addpremium USER_ID_OR_USERNAME 30d</code>\nExamples: <code>/addpremium 123456789 7d</code>, <code>/addpremium @username 1mo</code>, or duration <code>never</code>.")
    try:
        target_id = await _resolve_premium_target(args[0])
        duration = _duration_seconds(" ".join(args[1:]))
    except Exception as exc:
        return await send_message(message, f"⚠️ Could not add premium: {escape(str(exc))}")
    # Normalize any legacy string-keyed record before writing the entitlement.
    existing = user_data.get(str(target_id), {})
    if target_id not in user_data and existing:
        user_data[target_id] = existing
    user_data.setdefault(target_id, {})
    until = -1 if duration < 0 else time() + duration
    user_data[target_id]["PREMIUM_UNTIL"] = until
    if Config.DATABASE_URL:
        try:
            await database.update_user_data(target_id)
        except Exception as exc:
            LOGGER.exception("Could not persist premium entitlement for %s", target_id)
            return await send_message(message, f"⚠️ Premium was set for this running session, but database saving failed for <code>{target_id}</code>: {escape(str(exc))}. Fix the database before restarting the bot.")
    duration_text = _format_validity(duration)
    return await send_message(message, f"✅ Premium enabled for <code>{target_id}</code> for <code>{escape(duration_text)}</code>. Verification is bypassed while premium is active.")


async def removepremium_command(_, message):
    args = (getattr(message, "text", "") or "").split()[1:]
    if not args:
        return await send_message(message, "Usage: <code>/removepremium USER_ID_OR_USERNAME</code>")
    try:
        target_id = await _resolve_premium_target(args[0])
    except Exception as exc:
        return await send_message(message, f"⚠️ Could not resolve user: {escape(str(exc))}")
    data = user_data.get(target_id, user_data.get(str(target_id), {}))
    existed = data.pop("PREMIUM_UNTIL", None) is not None
    if Config.DATABASE_URL:
        try:
            await database.update_user_data(target_id)
        except Exception as exc:
            LOGGER.exception("Could not persist premium removal for %s", target_id)
            return await send_message(message, f"⚠️ Premium was removed for this running session, but database saving failed for <code>{target_id}</code>: {escape(str(exc))}.")
    return await send_message(message, ("✅ Premium removed" if existed else "ℹ️ User did not have premium") + f" for <code>{target_id}</code>.")


async def premium_users_command(_, message):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    users = []
    now = time()
    for uid, data in user_data.items():
        until = data.get("PREMIUM_UNTIL")
        if until is None:
            continue
        try:
            until = float(until)
        except (TypeError, ValueError, OverflowError):
            continue
        if until < 0 or until > now:
            users.append((int(uid), until))
    users.sort(key=lambda item: item[0])
    page_size = 10
    page = 0
    lines = [f"<b>💎 Active Premium Users</b>\nTotal: <code>{len(users)}</code>\n"]
    if not users:
        lines.append("No active premium users.")
    else:
        for uid, until in users[:page_size]:
            expiry = "Never" if until < 0 else get_readable_time(max(0, int(until-now))) + " remaining"
            lines.append(f"• <code>{uid}</code> — <code>{escape(expiry)}</code>")
    buttons = []
    if len(users) > page_size:
        buttons.append([InlineKeyboardButton("Next ➡️", callback_data="premiumusers:1")])
    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return await send_message(message, "\n".join(lines), markup)


async def verification_command(_, message):
    """/verify status, on/off, time, manual user authorization, or link request."""
    parts = (message.text or "").split()
    args = parts[1:]
    is_sudo = await CustomFilters.sudo("", message)
    if args:
        action = args[0].lower()
        if action in {"on", "off", "time", "validity", "status"}:
            if not is_sudo and not await CustomFilters.authorized("", message):
                return await send_message(message, "⛔ Only authorized users or admins can change verification settings.")
        elif action == "user" or (len(args) >= 2 and args[0].isdigit()):
            if not is_sudo:
                return await send_message(message, "⛔ Only the owner or sudo admins can manage individual authorizations.")

    if args and args[0].lower() in {"on", "off"}:
        mode = args[0].lower()
        if mode == "on":
            if len(args) > 1:
                try:
                    Config.VERIFY_TIMEOUT = _duration_seconds(" ".join(args[1:]))
                except ValueError as exc:
                    return await send_message(message, f"⚠️ {escape(str(exc))}")
            elif int(Config.VERIFY_TIMEOUT or 0) == 0:
                Config.VERIFY_TIMEOUT = 86400
            Config.VERIFY_ENABLED = True
            await _save_verify_config()
            validity = _format_validity(Config.VERIFY_TIMEOUT)
            return await send_message(message, f"✅ Verification <b>enabled</b>.\nValidity: <code>{escape(validity)}</code>.")
        Config.VERIFY_ENABLED = False
        await _save_verify_config()
        return await send_message(message, "✅ Verification <b>disabled</b>. Users can use the bot without verification.")

    if args and args[0].lower() in {"time", "validity"}:
        if len(args) < 2:
            return await send_message(message, "Usage: <code>/verify time 7d</code> or <code>/verify time never</code>")
        try:
            Config.VERIFY_TIMEOUT = _duration_seconds(" ".join(args[1:]))
        except ValueError as exc:
            return await send_message(message, f"⚠️ {escape(str(exc))}")
        await _save_verify_config()
        return await send_message(message, f"✅ Default verification validity set to <code>{escape(_format_validity(Config.VERIFY_TIMEOUT))}</code>.")

    if args and args[0].lower() == "status":
        enabled = is_verification_enabled()
        return await send_message(message, f"<b>Verification status:</b> {'ON' if enabled else 'OFF'}\n<b>Default validity:</b> <code>{escape(_format_validity(verification_timeout()))}</code>\n<b>Configured shorteners:</b> <code>{len(shortener_dict)}</code>")

    if args and args[0].lower() == "user":
        args = args[1:]
    if len(args) >= 2 and args[0].isdigit():
        target_id = int(args[0])
        value = " ".join(args[1:]).strip().lower()
        user_data.setdefault(target_id, {})
        data = user_data[target_id]
        if value in {"revoke", "remove", "off"}:
            data.pop("VERIFY_TIME", None)
            data.pop("VERIFY_VALIDITY", None)
            data["VERIFY_TOKEN"] = str(uuid4())
            await database.update_user_data(target_id) if Config.DATABASE_URL else None
            return await send_message(message, f"🔒 Verification revoked for <code>{target_id}</code>. They must verify again.")
        try:
            validity = _duration_seconds(value)
        except ValueError as exc:
            return await send_message(message, f"⚠️ {escape(str(exc))}")
        data["VERIFY_VALIDITY"] = validity
        data["VERIFY_TOKEN"] = str(uuid4())
        data["VERIFY_TIME"] = time()
        if Config.DATABASE_URL:
            await database.update_user_data(target_id)
        return await send_message(message, f"✅ User <code>{target_id}</code> authorized for <code>{escape(_format_validity(validity))}</code> from now.")

    if args and is_sudo:
        return await send_message(message, "<b>Verification controls</b>\n<code>/verify on [duration]</code>\n<code>/verify off</code>\n<code>/verify time 7d</code> or <code>never</code>\n<code>/verify user USER_ID 7d</code>\n<code>/verify user USER_ID revoke</code>\n<code>/verify status</code>")

    if not await CustomFilters.authorized("", message):
        return await send_message(message, "Use <code>/verify</code> from an authorized chat or account.")
    if not is_verification_enabled():
        return await send_message(message, "✅ Verification is currently disabled. You can use the bot without verification.")
    link, already_verified = await make_verification_link(message.from_user.id)
    if already_verified:
        return await send_message(message, "✅ You are already verified. You can continue using the bot.")
    if message.chat.type != ChatType.PRIVATE:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("📩 Send verification link to DM", callback_data=f"verifydm:{message.from_user.id}")]])
        return await send_message(message, "🔐 Complete verification in your private chat. Tap below and I’ll send you the verification link in DM.", markup)
    buttons = ButtonMaker()
    buttons.url_button("🔐 Complete Verification", link)
    validity = _format_validity(verification_timeout(message.from_user.id))
    return await send_message(message, f"<b>🔐 Verification Required</b>\nComplete verification to unlock bot commands and tasks.\n<b>Validity:</b> <code>{escape(validity)}</code>", buttons.build_menu(1))


async def verify_dm_callback(_, query):
    try:
        _, target = (query.data or "").split(":", 1)
        target_id = int(target)
    except (ValueError, TypeError):
        return await query.answer("Invalid verification request.", show_alert=True)
    if query.from_user.id != target_id:
        return await query.answer("This verification button is not for you.", show_alert=True)
    if not is_verification_enabled():
        return await query.answer("Verification is currently disabled.", show_alert=True)
    # Check whether the user has started/unblocked the bot before attempting DM.
    # send_chat_action is a non-destructive probe; Telegram rejects it for users
    # who have never opened the bot or have blocked it.
    try:
        await TgClient.bot.send_chat_action(target_id, ChatAction.TYPING)
        can_dm = True
    except Exception as exc:
        LOGGER.info("User %s has not started the bot or cannot receive DMs: %s", target_id, exc)
        can_dm = False

    if not can_dm:
        start_url = f"https://t.me/{TgClient.BNAME}?start=verify"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start Bot to Get Verification Link", url=start_url, style=ButtonStyle.SUCCESS)]])
        text = (
            "<b>📩 Open the bot first</b>\n\n"
            "Telegram does not allow the bot to send you a private message until you start it.\n"
            "1. Tap <b>Start Bot</b> below.\n"
            "2. The bot will send you a fresh verification shortlink in the private chat."
        )
        await query.answer("Start the bot to receive your verification link.", show_alert=True)
        try:
            await edit_message(query.message, text, markup)
        except Exception:
            await send_message(query.message, text, markup)
        return

    link, already_verified = await make_verification_link(target_id)
    if already_verified:
        await query.answer("You are already verified.", show_alert=True)
        return await send_message(query.message, "✅ You are already verified.")
    buttons = ButtonMaker()
    buttons.url_button("🔐 Complete Verification", link, "header", style=ButtonStyle.SUCCESS)
    validity = _format_validity(verification_timeout(target_id))
    try:
        await send_message(
            target_id,
            f"<b>🔐 Verification Required</b>\n\nYour verification shortlink is ready. Tap below to continue.\n"
            f"<b>Validity after activation:</b> <code>{escape(validity)}</code>",
            buttons.build_menu(1),
        )
        await query.answer("Verification shortlink sent to your DM.")
    except Exception as exc:
        LOGGER.info("Could not DM verification link to %s: %s", target_id, exc)
        start_url = f"https://t.me/{TgClient.BNAME}?start=verify"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start Bot to Get Verification Link", url=start_url, style=ButtonStyle.SUCCESS)]])
        await query.answer("Start the bot to receive your verification link.", show_alert=True)
        try:
            await edit_message(query.message, "<b>📩 Start the bot first</b>\nTap below; the bot will then send your verification shortlink in DM.", markup)
        except Exception:
            pass


async def verification_guard(_, update):
    """Block unverified incoming messages and callback actions before handlers run.

    This runs in group=-1 ahead of every other message/callback handler in the
    bot, so any exit path here that does not need to block the update MUST
    raise ContinuePropagation - otherwise pyrogram stops dispatching the
    update entirely and no other handler (buttons included) ever sees it.
    """
    user = getattr(update, "from_user", None) or getattr(update, "sender_chat", None)
    user_id = getattr(user, "id", None)
    if not user_id or not is_verification_enabled():
        raise ContinuePropagation
    if user_id == Config.OWNER_ID or user_id in sudo_users or premium_valid(user_id):
        raise ContinuePropagation
    if user_id in user_data and (user_data[user_id].get("SUDO") or user_data[user_id].get("is_sudo")):
        raise ContinuePropagation

    # Verification applies only to bot commands. Ordinary text, links, files,
    # media, and replies must never trigger a verification-link message.
    if hasattr(update, "chat") and hasattr(update, "id"):
        text = (getattr(update, "text", None) or "").strip()
        if not text.startswith("/"):
            raise ContinuePropagation

        command_name = text.split(None, 1)[0].split("@", 1)[0].lstrip("/").lower()
        suffix = str(Config.CMD_SUFFIX or "").strip().lower()
        # The gate is deliberately limited to commands that can start a task.
        # Status/help/admin/settings commands and unknown commands must never
        # create verification links or be blocked by the verification guard.
        task_commands = {
            "mirror", "m", "qbmirror", "qm", "jdmirror", "jm",
            "ytdl", "y", "socialdl", "sdl", "sdt", "uphoster", "up",
            "nzbmirror", "nm", "leech", "l", "lh", "qbleech", "ql",
            "jdleech", "jl", "ytdlleech", "yl", "nzbleech", "nl",
            "seedrlink", "slink", "srlink", "clone", "cl", "stream", "sl",
            "forcestart", "fs", "search", "torrent", "tor", "magnet",
        }
        if suffix and command_name.endswith(suffix):
            base_name = command_name[:-len(suffix)]
        else:
            base_name = command_name
        if base_name in {"start", "cancelall", "call", "status", "s", "statusall", "help", "h", "verify", "usersettings", "usettings", "settings", "usetting", "us"}:
            raise ContinuePropagation
        if base_name not in task_commands:
            raise ContinuePropagation

        if not _user_verified(user_id):
            await _send_verification_block(update, user_id)
            raise StopPropagation
        raise ContinuePropagation

    # Callback clicks must never create or send verification links by themselves.
    # Links are generated only when the user explicitly invokes a bot command
    # (or opens /start verify in private chat). Keep activation and the dedicated
    # DM-delivery action available; other pre-verification callbacks are blocked.
    if getattr(update, "data", None) is not None:
        data = str(update.data)
        if data.startswith(("start pass ", "verifydm:", "userset ")):
            raise ContinuePropagation
        if not _user_verified(user_id):
            try:
                await update.answer(
                    "Please use a bot command to request verification, then complete it first.",
                    show_alert=True,
                )
            except Exception:
                pass
            raise StopPropagation
        raise ContinuePropagation
    raise ContinuePropagation


def _user_verified(user_id):
    from ..helper.telegram_helper.tg_utils import _verification_valid

    return _verification_valid(user_id)


async def _send_verification_block(update, user_id):
    """Deliver verification links only in private chat; never expose them in groups."""
    from ..helper.telegram_helper.tg_utils import verify_token

    data = user_data.get(user_id, {})
    premium_until = data.get("PREMIUM_UNTIL")
    try:
        premium_expired = premium_until is not None and float(premium_until) >= 0 and time() >= float(premium_until)
    except (TypeError, ValueError, OverflowError):
        premium_expired = False
    if premium_expired:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("💎 Get Premium", url="https://t.me/alonekingstarbsckagain", style=ButtonStyle.SUCCESS)]])
        message = update if hasattr(update, "chat") else (getattr(update, "message", None) or update)
        await send_message(message, "⌛ <b>Your premium has expired.</b>\nGet premium to continue using the bot without verification.", markup)
        return

    text, buttons = await verify_token(user_id)
    if not text:
        return
    data = user_data.get(user_id, {})
    had_verification = data.get("VERIFY_TIME") is not None
    markup = buttons.build_menu(2) if buttons else None
    if had_verification:
        text = "⌛ <b>Your verification has expired.</b>\nComplete verification again to continue using the bot.\n\n<i>Tap the button below to verify.</i>"
    message = update if hasattr(update, "chat") else (getattr(update, "message", None) or update)
    try:
        if getattr(getattr(message, "chat", None), "type", None) == ChatType.PRIVATE:
            await send_message(message, text, markup)
            return
        # Group replies contain no verification URL or shortener buttons.
        # If the user has not started the bot, DM delivery fails and the group
        # notice must provide a direct Start button instead of dead-end text.
        await TgClient.bot.send_message(user_id, text, reply_markup=markup)
        # Keep the verification shortlink private, but give group users a
        # convenient button to open the bot's private chat and find the link.
        private_chat_url = f"https://t.me/{TgClient.BNAME}?start=verify"
        private_chat_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📩 Open Bot & Get Verification Link", url=private_chat_url, style=ButtonStyle.SUCCESS)]
        ])
        await send_message(
            message,
            "🔐 <b>Verification Required</b>\n\n"
            "I sent your verification link to your private chat. If you have not started the bot yet, "
            "tap the button below and press <b>Start</b> to receive a fresh link.",
            private_chat_markup,
        )
    except Exception as exc:
        LOGGER.info("Could not deliver verification privately to %s: %s", user_id, exc)
        if getattr(getattr(message, "chat", None), "type", None) != ChatType.PRIVATE:
            try:
                start_url = f"https://t.me/{TgClient.BNAME}?start=verify"
                start_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 Open Bot & Get Verification Link", url=start_url, style=ButtonStyle.SUCCESS)]
                ])
                await send_message(
                    message,
                    "🔐 Verification is required. Open the bot in private chat and press Start to receive your verification link.",
                    start_markup,
                )
            except Exception:
                pass


async def _verification_guard_filter(_, __, update):
    # Gate all incoming user messages and all callback actions.
    if getattr(update, "data", None) is not None:
        return True
    return bool(
        getattr(update, "id", None)
        and hasattr(update, "chat")
        and getattr(update, "from_user", None)
        and not getattr(update, "outgoing", False)
    )


verification_guard_filter = create(_verification_guard_filter)


async def premium_users_callback(_, query):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    try:
        page = max(0, int(query.data.split(":", 1)[1]))
    except (ValueError, IndexError):
        page = 0
    users = []
    now = time()
    for uid, data in user_data.items():
        until = data.get("PREMIUM_UNTIL")
        if until is None:
            continue
        try:
            until = float(until)
        except (TypeError, ValueError, OverflowError):
            continue
        if until < 0 or until > now:
            users.append((int(uid), until))
    users.sort(key=lambda item: item[0])
    size = 10
    page = min(page, max(0, (len(users)-1)//size))
    lines = [f"<b>💎 Active Premium Users</b>\nTotal: <code>{len(users)}</code>\nPage <code>{page+1}/{max(1, (len(users)+size-1)//size)}</code>\n"]
    for uid, until in users[page*size:(page+1)*size]:
        expiry = "Never" if until < 0 else get_readable_time(max(0, int(until-now))) + " remaining"
        lines.append(f"• <code>{uid}</code> — <code>{escape(expiry)}</code>")
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"premiumusers:{page-1}"))
    if (page+1)*size < len(users):
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"premiumusers:{page+1}"))
    markup = InlineKeyboardMarkup([row]) if row else None
    await query.message.edit_text("\n".join(lines), reply_markup=markup)
    await query.answer()
