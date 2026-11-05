from asyncio import sleep

from PIL import Image
from pyrogram import StopTransmission
from pyrogram.errors import FloodWait, PhotoInvalidDimensions
from pyrogram.types import ReplyParameters

try:
    from pyrogram.errors import FloodPremiumWait
except ImportError:
    FloodPremiumWait = FloodWait

from os import path as ospath
from aiofiles.os import path as aiopath, remove

from ... import LOGGER
from ...core.config_manager import Config
from ...core.tg_client import TgClient
from ..telegram_helper.tg_transfer import HypertgTransfer
from ..ext_utils.media_utils import (
    get_audio_thumbnail,
    get_document_type,
    get_media_info,
    get_multiple_frames_thumbnail,
    get_video_thumbnail,
)
from ..ext_utils.bot_utils import parse_dest
from ..ext_utils.tmdb_utils import get_auto_thumbnail


class HypertgUpload(HypertgTransfer):
    def __init__(self, obj):
        super().__init__(obj)
        self._up_file = ""
        self._file_progress = {}

    async def _progress(self, current, total, file_path):
        if self._listener.is_cancelled:
            raise StopTransmission()
        self._file_progress[file_path] = current
        self._obj._processed_bytes = sum(self._file_progress.values())

    async def upload(
        self,
        file_path,
        cap_mono,
        reply_target,
        reply_msg_id,
        force_document=False,
        user_thumb=None,
        user_session=False,
    ):
        self._cancel.clear()
        self._up_file = ospath.basename(file_path)

        is_video, is_audio, is_image = await get_document_type(file_path)

        thumb = user_thumb if user_thumb and user_thumb != "none" else None
        hd_cover = getattr(self._obj, "_hd_thumb", None)
        if hd_cover and not await aiopath.isfile(hd_cover):
            hd_cover = None

        if not is_image and thumb is None:
            file_name = ospath.splitext(self._up_file)[0]
            base_path = getattr(self._obj, "_path", "")
            thumb_path = f"{base_path}/yt-dlp-thumb/{file_name}.jpg"
            if await aiopath.isfile(thumb_path):
                thumb = thumb_path
            elif await aiopath.isfile(thumb_path.replace("/yt-dlp-thumb", "")):
                thumb = thumb_path.replace("/yt-dlp-thumb", "")
            elif is_audio and not is_video:
                thumb = await get_audio_thumbnail(file_path)

        if not is_image and thumb is None and user_thumb is None:
            user_dict = self._listener.user_dict
            if user_dict.get("AUTO_THUMBNAIL") or (
                "AUTO_THUMBNAIL" not in user_dict and Config.AUTO_THUMBNAIL
            ):
                try:
                    thumb = await get_auto_thumbnail(
                        self._up_file or self._listener.name,
                        force_document or self._listener.as_doc,
                    )
                except Exception as e:
                    LOGGER.warning(f"Auto thumbnail failed: {e}")

        duration = 0
        width = 480
        height = 320
        artist = ""
        title = ""

        if (
            force_document
            or self._listener.as_doc
            or (not is_video and not is_audio and not is_image)
        ):
            key = "documents"
            if is_video and thumb is None:
                thumb = await get_video_thumbnail(file_path, None)
        elif is_video:
            key = "videos"
            duration = (await get_media_info(file_path))[0]
            if thumb is None and self._listener.thumbnail_layout:
                thumb = await get_multiple_frames_thumbnail(
                    file_path,
                    self._listener.thumbnail_layout,
                    self._listener.screen_shots,
                )
            if thumb is None:
                thumb = await get_video_thumbnail(file_path, duration)
            if thumb is not None and thumb != "none":
                with Image.open(thumb) as img:
                    width, height = img.size
        elif is_audio:
            key = "audios"
            duration, artist, title = await get_media_info(file_path)
        else:
            key = "photos"

        if thumb == "none":
            thumb = None

        up_size = ospath.getsize(file_path)
        hyper_user_only = False

        if self._listener.up_dest:
            upload_chat_id = self._listener.up_dest
            thread_id = self._listener.chat_thread_id
        elif Config.LEECH_LOG_CHAT:
            upload_chat_id, thread_id = parse_dest(Config.LEECH_LOG_CHAT)
        else:
            # Never upload into the authorized group chat; default to the user's Bot PM
            upload_chat_id, thread_id = self._listener.user_id, None

        is_private_dm_target = (
            upload_chat_id == self._listener.user_id
            or (isinstance(upload_chat_id, int) and upload_chat_id > 0)
        )

        if is_private_dm_target:
            # Sending to user DM must go through TgClient.bot only (never helper bots or raw user DM)
            use_hyper = False
            if up_size > 2097152000 and TgClient.user is not None:
                user_session = True
            else:
                user_session = False
        elif up_size > 2097152000 and (TgClient.user or any(k < 0 for k in self.clients)):
            if TgClient.user:
                use_hyper = False
                user_session = True
            else:
                use_hyper = True
                hyper_user_only = True
                user_session = False
        else:
            use_hyper = Config.USE_HYPER and self.clients and up_size > 10 * 1024 * 1024

        try:
            if use_hyper:
                hyper_rply = (
                    reply_msg_id if upload_chat_id == reply_target.chat.id else None
                )
                try:
                    sent = await self._hyper_send(
                        file_path,
                        key,
                        thumb,
                        cap_mono,
                        upload_chat_id,
                        hyper_rply,
                        thread_id,
                        duration=duration,
                        width=width,
                        height=height,
                        artist=artist,
                        title=title,
                        user_only=hyper_user_only,
                    )
                except Exception as hyper_err:
                    LOGGER.warning(
                        f"HypertgUL helper upload to {upload_chat_id} failed ({hyper_err}); falling back to direct send"
                    )
                    sent = await self._direct_send(
                        file_path,
                        key,
                        thumb,
                        cap_mono,
                        upload_chat_id,
                        hyper_rply,
                        thread_id,
                        duration=duration,
                        width=width,
                        height=height,
                        artist=artist,
                        title=title,
                        user_session=user_session or (up_size > 2097152000),
                    )
            else:
                direct_rply = (
                    reply_msg_id if upload_chat_id == reply_target.chat.id else None
                )
                sent = await self._direct_send(
                    file_path,
                    key,
                    thumb,
                    cap_mono,
                    upload_chat_id,
                    direct_rply,
                    thread_id,
                    duration=duration,
                    width=width,
                    height=height,
                    artist=artist,
                    title=title,
                    user_session=user_session,
                )

            LOGGER.info(f"HypertgUL uploaded {self._up_file}")
            return sent

        except StopTransmission:
            LOGGER.warning(f"HypertgUL cancelled {self._up_file}")
            raise
        except Exception as e:
            LOGGER.error(f"HypertgUL fail {self._up_file}: {type(e).__name__}: {e}")
            raise
        finally:
            if user_thumb is None and thumb is not None and await aiopath.exists(thumb):
                try:
                    await remove(thumb)
                except Exception:
                    pass

    async def _send_with_retry(self, send_func, **kwargs):
        while True:
            try:
                return await send_func(**kwargs)
            except (FloodWait, FloodPremiumWait) as f:
                LOGGER.warning(f"HypertgUL flood {f.value}s on {self._up_file}")
                await sleep(f.value + 1)

    async def _try_send(self, key, client, kwargs):
        try:
            if key == "videos":
                return await self._send_with_retry(client.send_video, **kwargs)
            elif key == "audios":
                return await self._send_with_retry(client.send_audio, **kwargs)
            elif key == "photos":
                return await self._send_with_retry(client.send_photo, **kwargs)
            else:
                return await self._send_with_retry(client.send_document, **kwargs)
        except PhotoInvalidDimensions:
            kwargs.pop("thumb", None)
            kwargs.pop("video_cover", None)
            if key == "videos":
                return await self._send_with_retry(client.send_video, **kwargs)
            elif key == "audios":
                return await self._send_with_retry(client.send_audio, **kwargs)
            elif key == "photos":
                return await self._send_with_retry(client.send_photo, **kwargs)
            else:
                return await self._send_with_retry(client.send_document, **kwargs)

    async def _upload_via_bot_bridge(
        self,
        key,
        user_client,
        kwargs,
        target_chat_id,
        reply_msg_id=None,
        thread_id=None,
    ):
        """Stage >2 GiB upload from user_client to TgClient.bot, then copy to target_chat_id via TgClient.bot."""
        bot_uname = TgClient.BNAME or getattr(TgClient.bot.me, "username", "")
        bot_peer = f"@{str(bot_uname).lstrip('@')}"
        user_me = getattr(user_client, "me", None) or await user_client.get_me()
        user_uid = user_me.id

        anchor = None
        try:
            anchor = await TgClient.bot.send_message(
                chat_id=user_uid, text="⏳", disable_notification=True
            )
        except Exception:
            try:
                s_msg = await user_client.send_message(bot_peer, "/start")
                await sleep(0.5)
                await s_msg.delete()
                anchor = await TgClient.bot.send_message(
                    chat_id=user_uid, text="⏳", disable_notification=True
                )
            except Exception:
                anchor = None

        tag = f"__wzbridge_{id(self)}_{int(ospath.getsize(kwargs.get('video') or kwargs.get('document') or kwargs.get('audio') or kwargs.get('photo')))}__"
        stage_kwargs = dict(kwargs)
        stage_kwargs["chat_id"] = bot_peer
        stage_kwargs["caption"] = tag
        stage_kwargs.pop("reply_parameters", None)
        stage_kwargs.pop("message_thread_id", None)

        staged_user_msg = None
        bot_view_msg = None
        try:
            staged_user_msg = await self._try_send(key, user_client, stage_kwargs)
            if anchor is not None:
                probe_ids = list(range(anchor.id + 1, anchor.id + 30))
                for _ in range(4):
                    try:
                        cands = await TgClient.bot.get_messages(
                            chat_id=user_uid, message_ids=probe_ids
                        )
                        if not isinstance(cands, list):
                            cands = [cands]
                        for c in cands:
                            if c and not getattr(c, "empty", False) and tag in str(getattr(c, "caption", "") or ""):
                                bot_view_msg = c
                                break
                    except Exception:
                        pass
                    if bot_view_msg is not None:
                        break
                    await sleep(0.5)

            if bot_view_msg is None:
                raise RuntimeError("Bot bridge could not locate staged upload message")

            copy_kwargs = {
                "chat_id": target_chat_id,
                "from_chat_id": user_uid,
                "message_id": bot_view_msg.id,
                "caption": kwargs.get("caption") or "",
                "disable_notification": True,
            }
            if reply_msg_id:
                copy_kwargs["reply_parameters"] = ReplyParameters(message_id=reply_msg_id)
            elif thread_id:
                copy_kwargs["message_thread_id"] = thread_id

            try:
                return await self._send_with_retry(TgClient.bot.copy_message, **copy_kwargs)
            except Exception:
                copy_kwargs.pop("reply_parameters", None)
                copy_kwargs.pop("message_thread_id", None)
                return await self._send_with_retry(TgClient.bot.copy_message, **copy_kwargs)
        finally:
            for m in (anchor, bot_view_msg, staged_user_msg):
                if m is not None:
                    try:
                        await m.delete()
                    except Exception:
                        pass

    async def _hyper_send(
        self,
        file_path,
        key,
        thumb,
        cap_mono,
        chat_id,
        reply_msg_id,
        thread_id=None,
        duration=0,
        width=0,
        height=0,
        artist="",
        title="",
        user_only=False,
    ):
        if user_only:
            candidates = {
                k: self.work_loads.get(k, 0) for k in self.clients if k < 0
            }
            if not candidates:
                idx = self._pick_client()
            else:
                idx = min(candidates, key=candidates.get)
        else:
            idx = self._pick_client()
        client = self.clients[idx]
        self.work_loads[idx] += 1

        try:
            kwargs = {
                "chat_id": chat_id,
                "disable_notification": True,
                "progress": self._progress,
                "progress_args": (file_path,),
            }
            if cap_mono:
                kwargs["caption"] = cap_mono
            if reply_msg_id:
                kwargs["reply_parameters"] = ReplyParameters(message_id=reply_msg_id)
            elif thread_id:
                kwargs["message_thread_id"] = thread_id

            if key == "videos":
                if duration:
                    kwargs["duration"] = duration
                if width:
                    kwargs["width"] = width
                if height:
                    kwargs["height"] = height
                if thumb:
                    kwargs["thumb"] = thumb
                if hd_cover:
                    # Telegram's custom video cover is separate from the small
                    # upload thumbnail; keep the higher-resolution saved artwork.
                    kwargs["video_cover"] = hd_cover
                elif thumb:
                    kwargs["video_cover"] = thumb
            elif key == "audios":
                if duration:
                    kwargs["duration"] = duration
                if artist:
                    kwargs["performer"] = artist
                if title:
                    kwargs["title"] = title
                if thumb:
                    kwargs["thumb"] = thumb

            if key == "videos":
                kwargs["video"] = file_path
            elif key == "audios":
                kwargs["audio"] = file_path
            elif key == "photos":
                kwargs["photo"] = file_path
            else:
                kwargs["document"] = file_path

            sent = await self._try_send(key, client, kwargs)
            return sent
        finally:
            self.work_loads[idx] -= 1

    async def _direct_send(
        self,
        file_path,
        key,
        thumb,
        cap_mono,
        chat_id,
        reply_msg_id,
        thread_id=None,
        duration=0,
        width=0,
        height=0,
        artist="",
        title="",
        user_session=False,
    ):
        up_size = ospath.getsize(file_path)
        is_private_dm_target = (
            chat_id == self._listener.user_id
            or (isinstance(chat_id, int) and chat_id > 0)
        )
        client = (
            TgClient.user
            if (user_session and TgClient.user and not is_private_dm_target)
            else TgClient.bot
        )
        kwargs = {
            "chat_id": chat_id,
            "disable_notification": True,
            "progress": self._progress,
            "progress_args": (file_path,),
        }
        if cap_mono:
            kwargs["caption"] = cap_mono
        if reply_msg_id:
            kwargs["reply_parameters"] = ReplyParameters(message_id=reply_msg_id)
        elif thread_id:
            kwargs["message_thread_id"] = thread_id

        if key == "videos":
            if thumb:
                kwargs["thumb"] = thumb
            if hd_cover:
                kwargs["video_cover"] = hd_cover
            elif thumb:
                kwargs["video_cover"] = thumb
            if duration:
                kwargs["duration"] = duration
            if width:
                kwargs["width"] = width
            if height:
                kwargs["height"] = height
        elif key == "audios":
            if thumb:
                kwargs["thumb"] = thumb
            if duration:
                kwargs["duration"] = duration
            if artist:
                kwargs["performer"] = artist
            if title:
                kwargs["title"] = title
        else:
            if thumb:
                kwargs["thumb"] = thumb

        if key == "videos":
            kwargs["video"] = file_path
        elif key == "audios":
            kwargs["audio"] = file_path
        elif key == "photos":
            kwargs["photo"] = file_path
        else:
            kwargs["document"] = file_path

        # If target is User PM and file > 2 GiB, stage via TgClient.user -> TgClient.bot bridge
        if is_private_dm_target and up_size > 2097152000 and TgClient.user is not None:
            return await self._upload_via_bot_bridge(
                key,
                TgClient.user,
                kwargs,
                chat_id,
                reply_msg_id=reply_msg_id,
                thread_id=thread_id,
            )

        try:
            return await self._try_send(key, client, kwargs)
        except Exception as err:
            # If user_session failed on a dump chat, fallback to TgClient.bot (or bot bridge for >2 GiB)
            if client is TgClient.user:
                LOGGER.warning(
                    f"User session direct upload to {chat_id} failed ({err}); falling back to bot delivery"
                )
                if up_size <= 2097152000:
                    try:
                        return await self._try_send(key, TgClient.bot, kwargs)
                    except Exception:
                        kwargs["chat_id"] = self._listener.user_id
                        kwargs.pop("reply_parameters", None)
                        kwargs.pop("message_thread_id", None)
                        return await self._try_send(key, TgClient.bot, kwargs)
                return await self._upload_via_bot_bridge(
                    key,
                    TgClient.user,
                    kwargs,
                    chat_id,
                    reply_msg_id=reply_msg_id,
                    thread_id=thread_id,
                )
            elif chat_id != self._listener.user_id:
                LOGGER.warning(
                    f"Bot upload to dump {chat_id} failed ({err}); delivering directly to User PM {self._listener.user_id}"
                )
                kwargs["chat_id"] = self._listener.user_id
                kwargs.pop("reply_parameters", None)
                kwargs.pop("message_thread_id", None)
                return await self._try_send(key, TgClient.bot, kwargs)
            raise

    async def cancel(self):
        await super().cancel()
