from asyncio import sleep
from functools import partial
from html import escape
from pyrogram.enums import ChatType, ButtonStyle
from pyrogram.filters import create
from pyrogram.handlers import MessageHandler

from .. import user_data
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import new_task, update_user_ldata
from ..helper.ext_utils.db_handler import database
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)

_ADDBOT_HANDLER_DICT = {}


def get_addbot_menu(from_user):
    user_id = from_user.id
    user_name = from_user.mention(style="html")
    user_dict = user_data.get(user_id, {})
    tokens = user_dict.get("BOT_TOKENS", [])
    if not isinstance(tokens, list):
        tokens = []

    text = f"<b>🤖 User Bot Tokens Configuration</b>\n\n"
    text += f"<blockquote>• <b>User:</b> {user_name}\n"
    text += f"• <b>Added Bots:</b> <code>{len(tokens)} / 5</code></blockquote>\n\n"

    buttons = ButtonMaker()

    if tokens:
        text += "<b>Configured Bot Tokens:</b>\n"
        for i, token in enumerate(tokens, start=1):
            if ":" in token:
                parts = token.split(":", 1)
                masked = f"{parts[0]}:{parts[1][:4]}...{parts[1][-4:]}"
            else:
                masked = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else "****"
            text += f"• <b>Bot {i}:</b> <code>{escape(masked)}</code>\n"
            buttons.data_button(f"🗑️ Delete Bot {i}", f"addbot del {i-1}")

    if len(tokens) < 5:
        buttons.data_button("➕ Add Bot Token", f"addbot add", position="header")

    buttons.data_button(
        "❌ Close",
        f"addbot close",
        position="footer",
        style=ButtonStyle.DANGER,
    )

    return text, buttons.build_menu(2)


async def addbot_task_allowed(message):
    """Enforce per-user helper-bot mode and validate at least one added bot."""
    user = getattr(message, "from_user", None)
    user_id = user.id if user else (getattr(getattr(message, "sender_chat", None), "id", 0))
    if not user_id:
        return True
    data = user_data.get(user_id, {})
    if data.get("ADDBOT_REQUIRED") is not True:
        return True

    tokens = data.get("BOT_TOKENS", [])
    if isinstance(tokens, list):
        valid_format = [t.strip() for t in tokens if isinstance(t, str) and ":" in t and len(t.strip()) >= 20]
    else:
        valid_format = []

    # Format-only checks allowed broken tokens through and then silently used the
    # main/global bot. Start the token clients here so invalid tokens fail closed.
    if valid_format:
        from ..core.tg_client import TgClient
        clients = await TgClient.get_user_bots(user_id, valid_format)
        if clients:
            return True

    await send_message(
        message,
        "<b>🤖 Add a bot before starting tasks</b>\n\n"
        "<blockquote><code>/addbot</code> requirement is <b>ON</b>, but no working user bot is available.\n"
        "1. Open this bot in DM and send <code>/addbot</code>.\n"
        "2. Choose <b>Add Bot Token</b> and send a valid token from @BotFather.\n"
        "3. Make sure your added bot can message you and has access to the destination dump chat.\n"
        "If a token is invalid, delete it and add a working token. Use <code>/addbot off</code> to restore the normal main-bot upload mode.</blockquote>",
    )
    return False


@new_task
async def add_bot_command(client, message):
    # /addbot on|off controls whether a user must configure at least one helper bot.
    args = (message.text or "").split()
    if len(args) > 1 and args[1].lower() in ("on", "off"):
        enabled = args[1].lower() == "on"
        update_user_ldata(message.from_user.id, "ADDBOT_REQUIRED", enabled)
        try:
            await database.update_user_data(message.from_user.id)
        except Exception as e:
            from .. import LOGGER
            LOGGER.exception("Failed to persist ADDBOT_REQUIRED for %s", message.from_user.id)
            await send_message(message, f"<blockquote>Setting changed for this session, but saving failed: {escape(str(e)[:180])}</blockquote>")
            return
        state = "ON" if enabled else "OFF"
        detail = ("Users must add at least one bot before starting a task. All task uploads/dumps use their added bot(s)." if enabled else "Bot requirement is disabled. Existing default upload behavior is restored.")
        await send_message(message, f"<b>✅ /addbot requirement: {state}</b>\n\n<blockquote>{detail}</blockquote>")
        return

    if message.chat.type != ChatType.PRIVATE:
        await send_message(
            message,
            "<blockquote>The <code>/addbot</code> command can only be used in DM (Direct Message) because bot tokens could be leaked in group chats!</blockquote>",
        )
        return

    text, buttons = get_addbot_menu(message.from_user)
    await send_message(message, text, buttons)


@new_task
async def add_bot_cb(client, query):
    from_user = query.from_user
    user_id = from_user.id
    data = query.data.split()
    action = data[1]

    if action == "close":
        await query.answer()
        await delete_message(query.message)
        return

    user_dict = user_data.get(user_id, {})
    tokens = user_dict.get("BOT_TOKENS", [])
    if not isinstance(tokens, list):
        tokens = []

    if action == "del":
        idx = int(data[2])
        if 0 <= idx < len(tokens):
            del tokens[idx]
            update_user_ldata(user_id, "BOT_TOKENS", tokens)
            try:
                await database.update_user_data(user_id)
            except Exception:
                from .. import LOGGER
                LOGGER.exception("Failed to persist deleted bot token for %s", user_id)
                await query.answer("Token removed for this session, but database saving failed.", show_alert=True)
            else:
                await query.answer("Bot token deleted successfully!", show_alert=True)
        else:
            await query.answer("Invalid bot token index!", show_alert=True)
        text, buttons = get_addbot_menu(from_user)
        await edit_message(query.message, text, buttons)

    elif action == "add":
        if len(tokens) >= 5:
            return await query.answer(
                "You can add up to 5 bot tokens maximum!", show_alert=True
            )
        await query.answer()

        buttons = ButtonMaker()
        buttons.data_button(
            "❌ Cancel", f"addbot cancel", position="footer", style=ButtonStyle.DANGER
        )
        prompt_text = (
            "<b>🤖 Add Bot Token</b>\n\n"
            "<blockquote>Send your Telegram Bot Token from @BotFather in the chat below.\n"
            "⏱️ <b>Time Left:</b> <code>60 sec</code></blockquote>"
        )
        await edit_message(query.message, prompt_text, buttons.build_menu(1))

        _ADDBOT_HANDLER_DICT[user_id] = True

        async def token_filter(_, __, event):
            u = event.from_user or event.sender_chat
            return bool(
                u
                and u.id == user_id
                and event.chat.type == ChatType.PRIVATE
                and event.text
            )

        from asyncio import wait_for, get_running_loop
        try:
            event_done = get_running_loop().create_future()
        except Exception:
            from bot import bot_loop
            event_done = bot_loop.create_future()

        async def token_handler_v2(_, msg):
            _ADDBOT_HANDLER_DICT[user_id] = False
            tok = (msg.text or "").strip()
            await delete_message(msg)
            if ":" not in tok or len(tok) < 20:
                await send_message(msg, "<blockquote>Invalid Telegram Bot Token format! Please check and try again.</blockquote>")
            else:
                curr_tokens = user_data.get(user_id, {}).get("BOT_TOKENS", [])
                if not isinstance(curr_tokens, list):
                    curr_tokens = []
                if len(curr_tokens) >= 5:
                    await send_message(msg, "<blockquote>You can add up to 5 bot tokens maximum.</blockquote>")
                elif tok in curr_tokens:
                    await send_message(msg, "<blockquote>This bot token is already added!</blockquote>")
                else:
                    curr_tokens.append(tok)
                    update_user_ldata(user_id, "BOT_TOKENS", curr_tokens)
                    try:
                        await database.update_user_data(user_id)
                        await send_message(msg, "<b>✅ Bot token added and saved successfully!</b>")
                    except Exception:
                        from .. import LOGGER
                        LOGGER.exception("Failed to save bot token for %s", user_id)
                        await send_message(msg, "<blockquote>Bot token was added for this session, but database saving failed.</blockquote>")
            if not event_done.done():
                event_done.set_result(True)

        h_v2 = client.add_handler(
            MessageHandler(token_handler_v2, filters=create(token_filter)), group=-1
        )
        try:
            await wait_for(event_done, timeout=60)
        except Exception:
            _ADDBOT_HANDLER_DICT[user_id] = False
        finally:
            client.remove_handler(*h_v2)
            text, buttons = get_addbot_menu(from_user)
            await edit_message(query.message, text, buttons)

    elif action == "cancel":
        _ADDBOT_HANDLER_DICT[user_id] = False
        await query.answer("Cancelled", show_alert=False)
        text, buttons = get_addbot_menu(from_user)
        await edit_message(query.message, text, buttons)
