"""Authorized-chat mention-all command."""
from html import escape

from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait, RPCError

from .. import auth_chats, sudo_users, user_data
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.message_utils import send_message


@new_task
async def mention_all(_, message):
    chat = message.chat
    actor = message.from_user
    if not actor:
        return await send_message(message, "Only a chat administrator can use this command.")
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await send_message(message, "Use this command in an authorized group.")

    # Only chats explicitly authorized for bot tasks may run mention-all.
    chat_auth = chat.id in auth_chats or bool(user_data.get(chat.id, {}).get("AUTH"))
    if not chat_auth:
        return await send_message(message, "This chat is not authorized to use /mention.")

    try:
        member = await _.get_chat_member(chat.id, actor.id)
        is_admin = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except RPCError:
        is_admin = False
    if not is_admin and actor.id not in sudo_users:
        return await send_message(message, "Only a group admin can use /mention.")

    target = message.reply_to_message
    if not target:
        return await send_message(message, "Reply to the message you want to pin and mention everyone for, then send /mention.")

    try:
        await _.pin_chat_message(chat.id, target.id, disable_notification=True)
    except RPCError as exc:
        return await send_message(message, f"I couldn't pin that message. Check my pin-message permission.\n<code>{escape(str(exc)[:300])}</code>")

    mentions = []
    try:
        async for member in _.get_chat_members(chat.id):
            user = member.user
            if not user or user.is_bot or user.is_deleted:
                continue
            label = f"@{user.username}" if user.username else (user.first_name or "member")
            mentions.append(f'<a href="tg://user?id={user.id}">{escape(label)}</a>')
    except RPCError as exc:
        return await send_message(message, f"Message pinned, but I couldn't fetch the member list.\n<code>{escape(str(exc)[:300])}</code>")

    if not mentions:
        return await send_message(message, "Message pinned, but no mentionable members were found.")

    # Keep each message comfortably below Telegram's 4096-character limit.
    chunks, current = [], "📣 <b>Everyone</b>\n\n"
    for mention in mentions:
        piece = mention + " "
        if len(current) + len(piece) > 3500 and current.strip():
            chunks.append(current.rstrip())
            current = "📣 <b>Everyone (continued)</b>\n\n"
        current += piece
    if current.strip():
        chunks.append(current.rstrip())

    sent = 0
    for chunk in chunks:
        try:
            await _.send_message(chat.id, chunk, disable_web_page_preview=True, reply_to_message_id=target.id)
            sent += 1
        except FloodWait as exc:
            import asyncio
            await asyncio.sleep(exc.value)
            await _.send_message(chat.id, chunk, disable_web_page_preview=True, reply_to_message_id=target.id)
            sent += 1
        except RPCError:
            break
    await send_message(message, f"✅ Pinned the replied message and mentioned {len(mentions)} members in {sent} message(s).")
