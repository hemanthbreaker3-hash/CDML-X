"""Runtime force-subscription manager and command gate."""
import json
import os
import re
from pathlib import Path

from pyrogram import ContinuePropagation, StopPropagation
from pyrogram.filters import create, regex
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import LOGGER, sudo_users, user_data
from ..core.config_manager import Config
from ..core.tg_client import TgClient

_FILE = Path("fsub_channels.json")
_CHANNELS = []
_PENDING = {}


def _load():
    global _CHANNELS
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
        _CHANNELS = data if isinstance(data, list) else []
    except (OSError, ValueError):
        _CHANNELS = []


def _save():
    tmp = _FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_CHANNELS, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _FILE)


_load()


def _admin(uid):
    return bool(uid and (int(uid) == int(Config.OWNER_ID or 0) or int(uid) in sudo_users or (uid in user_data and (user_data[uid].get("SUDO") or user_data[uid].get("is_sudo")))))


def _menu():
    rows = [[InlineKeyboardButton("➕ Add channel/group", callback_data="fsub:add")]]
    for i, item in enumerate(_CHANNELS):
        name = item.get("title") or item.get("chat_id")
        rows.append([InlineKeyboardButton(f"🗑 Delete {name}"[:60], callback_data=f"fsub:del:{i}"), InlineKeyboardButton("♻️ Replace", callback_data=f"fsub:rep:{i}")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="fsub:menu")])
    return InlineKeyboardMarkup(rows)


async def fsub_command(_, message):
    uid = getattr(getattr(message, "from_user", None), "id", None)
    if not _admin(uid):
        return await message.reply_text("⛔ This command is for bot admins only.")
    await message.reply_text(
        "<b>Force Subscription Manager</b>\n\n"
        f"Configured channels/groups: <code>{len(_CHANNELS)}</code>\n"
        "Add unlimited channels or groups. The bot must be an administrator in each one, and the channel must allow membership checks.",
        reply_markup=_menu(),
    )


async def fsub_callback(_, query):
    uid = getattr(getattr(query, "from_user", None), "id", None)
    if not _admin(uid):
        return await query.answer("Admins only.", show_alert=True)
    data = query.data or ""
    if data == "fsub:menu":
        return await query.message.edit_reply_markup(_menu())
    if data == "fsub:add":
        _PENDING[uid] = ("add", None)
        return await query.message.reply_text("Send the channel/group username (e.g. @mychannel) or numeric chat ID. Make sure the bot is an admin there.")
    match = re.fullmatch(r"fsub:(del|rep):(\d+)", data)
    if match:
        action, index = match.group(1), int(match.group(2))
        if index >= len(_CHANNELS):
            return await query.answer("That channel entry no longer exists. Refresh the menu.", show_alert=True)
        if action == "del":
            removed = _CHANNELS.pop(index)
            _save()
            await query.answer("Channel removed.")
            return await query.message.edit_text(f"🗑 Removed <code>{removed.get('title') or removed.get('chat_id')}</code>.", reply_markup=_menu())
        _PENDING[uid] = ("replace", index)
        return await query.message.reply_text("Send the new channel/group username or numeric chat ID to replace this entry.")
    await query.answer()


async def fsub_input(_, message):
    uid = getattr(getattr(message, "from_user", None), "id", None)
    if uid not in _PENDING or not _admin(uid):
        raise ContinuePropagation
    raw = (message.text or "").strip()
    if not raw or raw.startswith("/"):
        return await message.reply_text("Send a channel username (e.g. @mychannel) or numeric chat ID.")
    action, index = _PENDING.pop(uid)
    try:
        chat = await TgClient.bot.get_chat(raw)
        me = await TgClient.bot.get_me()
        member = await TgClient.bot.get_chat_member(chat.id, me.id)
        status = str(getattr(member, "status", "")).lower()
        if "administrator" not in status and "owner" not in status and "creator" not in status:
            return await message.reply_text("❌ Please promote the bot to administrator in that channel/group, then try again.")
        invite = getattr(chat, "invite_link", None)
        if not invite:
            try:
                invite = (await TgClient.bot.create_chat_invite_link(chat.id)).invite_link
            except Exception:
                username = getattr(chat, "username", None)
                invite = f"https://t.me/{username}" if username else None
        if not invite:
            return await message.reply_text("❌ Could not obtain an invite/join link. Give the bot permission to create invite links or use a public channel.")
        item = {"chat_id": int(chat.id), "title": chat.title or str(chat.id), "link": invite, "username": getattr(chat, "username", None)}
        if action == "replace" and index is not None and index < len(_CHANNELS):
            _CHANNELS[index] = item
        else:
            if any(int(x.get("chat_id", 0)) == int(chat.id) for x in _CHANNELS):
                return await message.reply_text("That channel/group is already configured.", reply_markup=_menu())
            _CHANNELS.append(item)
        _save()
        return await message.reply_text(f"✅ <b>{'Replaced' if action == 'replace' else 'Added'} force-subscription channel</b>\n{item['title']}", reply_markup=_menu())
    except Exception as exc:
        LOGGER.warning("FSub channel setup failed for %s: %s", uid, exc)
        return await message.reply_text("❌ Could not access that channel/group. Check the chat ID/username and make sure the bot is an administrator.")


async def _membership(uid):
    """Return joined only when Telegram positively confirms membership in every entry.

    Telegram/API errors fail closed: an unreachable or misconfigured channel must
    never accidentally unlock the bot. ChatMemberStatus may be an enum, so normalize
    its value/name rather than relying on the enum's display string.
    """
    if not _CHANNELS:
        return True, None

    for item in list(_CHANNELS):
        try:
            chat_id = int(item["chat_id"])
            member = await TgClient.bot.get_chat_member(chat_id, int(uid))
            raw_status = getattr(member, "status", None)
            status = str(getattr(raw_status, "value", None) or getattr(raw_status, "name", None) or raw_status or "").lower()
            status = status.rsplit(".", 1)[-1]

            # These statuses positively confirm that the account is currently in
            # the chat. A restricted user counts only if Telegram says is_member.
            if status in {"owner", "creator", "administrator", "admin", "member"}:
                continue
            if status == "restricted" and bool(getattr(member, "is_member", False)):
                continue

            # left, kicked/banned, unknown, and future/unrecognized statuses fail closed.
            return False, item
        except Exception as exc:
            LOGGER.warning(
                "Force-sub membership check failed for user %s in chat %s (%s): %s",
                uid, item.get("chat_id"), item.get("title", "unknown"), exc,
            )
            return False, item
    return True, None


async def fsub_guard(_, update):
    if not _CHANNELS:
        raise ContinuePropagation
    user = getattr(update, "from_user", None)
    uid = getattr(user, "id", None)
    if not uid or _admin(uid):
        raise ContinuePropagation
    # Only gate bot commands and callback buttons, not ordinary chat messages.
    is_callback = getattr(update, "data", None) is not None
    if not is_callback:
        text = (getattr(update, "text", None) or getattr(update, "caption", None) or "").strip()
        if not text.startswith("/"):
            raise ContinuePropagation
        cmd = text.split(None, 1)[0].split("@", 1)[0].lstrip("/").lower()
        suffix = str(Config.CMD_SUFFIX or "").strip().lower()
        if cmd in {"start", "start" + suffix, "fsub", "fsub" + suffix}:
            raise ContinuePropagation
    else:
        data = str(update.data)
        if data.startswith("fsub:"):
            raise ContinuePropagation
    # Show a short italic checking notice before allowing a command to proceed.
    # It is removed immediately for members so it doesn't clutter the chat.
    wait_message = None
    target = update if hasattr(update, "reply_text") else getattr(update, "message", None)
    if not is_callback and target:
        try:
            wait_message = await target.reply_text("<i>⏳ Please wait, checking your channel membership…</i>")
        except Exception:
            wait_message = None
    ok, item = await _membership(uid)
    if ok:
        if wait_message:
            try:
                await wait_message.delete()
            except Exception:
                pass
        raise ContinuePropagation
    if wait_message:
        try:
            await wait_message.delete()
        except Exception:
            pass
    buttons = [[InlineKeyboardButton(f"📢 Join {str(channel.get('title') or 'Channel')[:35]}", url=channel["link"])] for channel in _CHANNELS]
    buttons.append([InlineKeyboardButton("🔄 I Joined — Check Again", callback_data="fsub:check")])
    text = ("🔒 <b>Force Subscription Required</b>\n\n"
            "Join every channel/group below to unlock all bot commands.\n"
            "After joining, tap <b>🔄 I Joined — Check Again</b>.")
    target = update if hasattr(update, "reply_text") else getattr(update, "message", None)
    try:
        if is_callback:
            await update.answer("Please join all required channels first.", show_alert=True)
            if target:
                await target.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        elif target:
            await target.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        pass
    raise StopPropagation


async def fsub_check_callback(_, query):
    uid = getattr(getattr(query, "from_user", None), "id", None)
    if not uid:
        return await query.answer("Could not identify your account.", show_alert=True)
    ok, item = await _membership(uid)
    if ok:
        await query.answer("✅ Membership confirmed. You can use the bot now.", show_alert=True)
        try:
            await query.message.delete()
        except Exception:
            pass
    else:
        await query.answer(f"❌ Please join {item.get('title') or 'all required channels'} first.", show_alert=True)


def register_fsub_handlers(bot):
    # Run before other handlers so all commands are gated consistently.
    bot.add_handler(MessageHandler(fsub_guard, create(lambda _, __, u: bool(getattr(u, 'id', None) and hasattr(u, 'chat')))), group=-3)
    bot.add_handler(CallbackQueryHandler(fsub_guard, regex(r"^(?!fsub:check$).+")), group=-3)
    bot.add_handler(CallbackQueryHandler(fsub_check_callback, regex(r"^fsub:check$")))
    suffix = re.escape(str(Config.CMD_SUFFIX or "").strip())
    bot.add_handler(MessageHandler(fsub_command, regex(rf"^/fsub{suffix}(?:@[A-Za-z0-9_]+)?(?:\s|$)", re.IGNORECASE)))
    bot.add_handler(CallbackQueryHandler(fsub_callback, regex(r"^fsub:(?:menu|add|del:\d+|rep:\d+)$")))
    bot.add_handler(MessageHandler(fsub_input, create(lambda _, __, m: bool(getattr(getattr(m, 'from_user', None), 'id', None) in _PENDING and getattr(m, 'text', None)))))
