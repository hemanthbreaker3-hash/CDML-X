from asyncio import gather
from time import time
from uuid import uuid4

from pyrogram.enums import ChatAction
from pyrogram.errors import ChannelInvalid, PeerIdInvalid, RPCError, UserNotParticipant

from ..ext_utils.links_utils import encode_slink

from ... import LOGGER, sudo_users, user_data
from ...core.config_manager import Config
from ...core.tg_client import TgClient
from ..ext_utils.shortener_utils import short_url
from ..ext_utils.status_utils import get_readable_time
from .button_build import ButtonMaker


async def chat_info(channel_id):
    channel_id = str(channel_id).strip()
    if channel_id.startswith("-100"):
        channel_id = int(channel_id)
    elif channel_id.startswith("@"):
        channel_id = channel_id.replace("@", "")
    else:
        return None
    try:
        return await TgClient.bot.get_chat(channel_id)
    except (PeerIdInvalid, ChannelInvalid) as e:
        LOGGER.error(f"{e.NAME}: {e.MESSAGE} for {channel_id}")
        return None


async def forcesub(message, ids, button=None):
    button = ButtonMaker() if button is None else button
    join_button = {}
    _msg = ""

    async def _check_channel(channel_id):
        chat = await chat_info(channel_id)
        if chat is None:
            return None
        try:
            await chat.get_member(message.from_user.id)
            return None
        except UserNotParticipant:
            if username := chat.username:
                invite_link = f"https://t.me/{username}"
            else:
                invite_link = chat.invite_link
            return (chat.title, invite_link)
        except RPCError as e:
            LOGGER.error(f"{e.NAME}: {e.MESSAGE} for {channel_id}")
        except Exception as e:
            LOGGER.error(f"{e} for {channel_id}")
        return None

    results = await gather(*[_check_channel(cid) for cid in ids.split()])
    for result in results:
        if result:
            title, link = result
            join_button[title] = link

    if join_button:
        if button is None:
            button = ButtonMaker()
        _msg = "┠ Channel(s) pending to be joined, Join Now!"
        for key, value in join_button.items():
            button.url_button(f"Join {key}", value, "footer")
    return _msg, button


async def user_info(user_id):
    try:
        return await TgClient.bot.get_users(user_id)
    except Exception:
        return ""


async def check_botpm(message, button=None):
    button = ButtonMaker() if button is None else button
    try:
        await TgClient.bot.send_chat_action(message.from_user.id, ChatAction.TYPING)
        return None, button
    except Exception:
        _msg = "┠ <i>Bot isn't Started in PM or Inbox (Private)</i>"
        button.url_button(
            "Start Bot Now", f"https://t.me/{TgClient.BNAME}?start=start", "header"
        )
        return _msg, button


def is_verification_enabled():
    """Respect the explicit runtime toggle while retaining legacy timeout config."""
    configured = getattr(Config, "VERIFY_ENABLED", None)
    if configured is None:
        return bool(Config.VERIFY_TIMEOUT)
    return bool(configured)


def verification_timeout(user_id=None):
    """Return validity seconds; -1 means unlimited validity until revoked."""
    if user_id is not None:
        custom = user_data.get(user_id, {}).get("VERIFY_VALIDITY")
        if custom is not None:
            try:
                return int(custom)
            except (TypeError, ValueError):
                pass
    try:
        timeout = int(Config.VERIFY_TIMEOUT or 0)
    except (TypeError, ValueError):
        timeout = 0
    return timeout if timeout != 0 else 86400



def premium_valid(user_id):
    """Return True while a user's premium entitlement is active.

    Mongo/user-data imports may expose IDs as either integers or strings, so
    normalize both forms before checking the expiry timestamp.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError, OverflowError):
        return False
    data = user_data.get(uid, user_data.get(str(uid), {}))
    until = data.get("PREMIUM_UNTIL")
    if until is None:
        return False
    try:
        until = float(until)
    except (TypeError, ValueError, OverflowError):
        return False
    return until < 0 or time() < until


def _verification_valid(user_id):
    # Premium is a first-class verification bypass everywhere verification
    # state is checked, including /start and link-generation paths.
    if premium_valid(user_id):
        return True
    try:
        user_id = int(user_id)
    except (TypeError, ValueError, OverflowError):
        return False
    data = user_data.get(user_id, user_data.get(str(user_id), {}))
    if Config.LOGIN_PASS and data.get("VERIFY_TOKEN", "") == Config.LOGIN_PASS:
        return True
    expire = data.get("VERIFY_TIME")
    if expire is None:
        return False
    timeout = verification_timeout(user_id)
    try:
        started_at = float(expire)
    except (TypeError, ValueError, OverflowError):
        return False
    return timeout < 0 or (time() - started_at) <= timeout


async def _persist_verification_user(user_id):
    if not Config.DATABASE_URL:
        return
    try:
        from ..ext_utils.db_handler import database

        await database.update_user_data(user_id)
    except Exception as exc:
        LOGGER.warning("Could not persist verification details for %s: %s", user_id, exc)


async def make_verification_link(user_id, renew=False):
    """Return (shortlink, already_verified), persisting a pending token as needed."""
    user_id = int(user_id)
    user_data.setdefault(user_id, {})
    data = user_data[user_id]
    if _verification_valid(user_id) and not renew:
        return None, True

    expire = data.get("VERIFY_TIME")
    timeout = verification_timeout(user_id)
    try:
        expired = expire is not None and timeout >= 0 and (time() - float(expire)) > timeout
    except (TypeError, ValueError, OverflowError):
        expired = expire is not None
    token = data.get("VERIFY_TOKEN")
    if renew or expired or not token or (Config.LOGIN_PASS and token == Config.LOGIN_PASS):
        token = str(uuid4())
        data["VERIFY_TOKEN"] = token
    if expired or renew:
        data.pop("VERIFY_TIME", None)
    await _persist_verification_user(user_id)
    payload = encode_slink(f"{token}&&{user_id}")
    return await short_url(f"https://t.me/{TgClient.BNAME}?start={payload}"), False


async def verify_token(user_id, button=None):
    button = ButtonMaker() if button is None else button
    if premium_valid(user_id) or not is_verification_enabled() or bool(
        user_id == Config.OWNER_ID
        or user_id in sudo_users
        or user_id in user_data
        and (user_data[user_id].get("SUDO") or user_data[user_id].get("is_sudo"))
    ):
        return None, button
    user_data.setdefault(user_id, {})
    if _verification_valid(user_id):
        return None, button

    link, _ = await make_verification_link(user_id)
    button.url_button("🔐 Complete Verification", link)
    timeout = verification_timeout(user_id)
    validity = "Unlimited (until revoked)" if timeout < 0 else get_readable_time(timeout)
    return (
        "┏ <b>Verification Required</b>\n"
        "┣ Complete verification before using bot commands or starting tasks.\n"
        f"┣ <b>Validity after activation:</b> <code>{validity}</code>\n"
        "┗ Use the button below to verify.",
        button,
    )
