from asyncio import sleep

from PIL import Image
from pyrogram import StopTransmission
from pyrogram.errors import FloodWait, PhotoInvalidDimensions

try:
    from pyrogram.errors import FloodPremiumWait
except ImportError:
    FloodPremiumWait = FloodWait

from os import path as ospath
import hashlib
from aiofiles.os import path as aiopath, remove

from ... import LOGGER
from ...core.config_manager import Config
from ...core.tg_client import TgClient
from ..telegram_helper.tg_transfer import HypertgTransfer
from ..ext_utils.media_utils import (
    apply_thumbnail_watermark,
    format_tg_thumbnail,
    get_audio_thumbnail,
    get_document_type,
    get_media_info,
    get_multiple_frames_thumbnail,
    get_video_thumbnail,
)
from ..ext_utils.bot_utils import sync_to_async
from ..ext_utils.bot_utils import parse_dest
from ..ext_utils.tmdb_utils import get_auto_thumbnail


class HypertgUpload(HypertgTransfer):
    def __init__(self, obj):
        super().__init__(obj)
        self._up_file = ""
        self._file_progress = {}
        if hasattr(obj, "_hu_clients") and obj._hu_clients:
            self.clients = dict(obj._hu_clients)
            self.client_ids = list(self.clients.keys())
            self.work_loads = {k: 0 for k in self.client_ids}
            self.num_clients = len(self.clients)
            from ..telegram_helper.tg_transfer import MtprotoPool
            self._pool = MtprotoPool(self.clients)
            self._use_user_bots = True

    def _auto_thumb_enabled(self):
        """Respect the modern thumbnail mode setting, with legacy-config fallback."""
        user_settings = self._listener.user_dict
        # User settings are loaded from the bot database into user_dict. Prefer
        # the explicit mode, then the legacy per-user toggle, then global config.
        mode = user_settings.get("THUMBNAIL_MODE")
        if mode is not None:
            return str(mode).strip().lower() == "auto"
        legacy = user_settings.get("AUTO_THUMBNAIL")
        if legacy is not None:
            return bool(legacy)
        return bool(getattr(Config, "AUTO_THUMBNAIL", False))

    async def _make_hd_thumbnail(self, thumb, file_path):
        """Keep the selected artwork untouched when HD thumbnail mode is on.

        Do not apply blur-like filters, artificial sharpening, contrast changes,
        or an upscale/downscale cycle here. Telegram requires its attached
        thumbnail to be JPEG, at most 320x320, and below its size limit; the
        final Telegram-specific conversion handles those unavoidable limits
        using the highest quality that fits.
        """
        if not self._listener.user_dict.get("HD_THUMBNAIL", False):
            return thumb
        if not thumb or thumb == "none" or not await aiopath.exists(str(thumb)):
            return thumb
        return thumb

    async def _get_auto_thumb(
        self, file_path, is_video=False, duration=0, is_audio=False, as_doc=False
    ):
        if not self._auto_thumb_enabled():
            return None

        # IMDb title search is attempted first. Video uploads request the first
        # landscape still/backdrop; document uploads request the portrait poster.
        # Documents (including video files forced to document mode) use
        # portrait posters. Actual video/media uploads use landscape backdrops.
        use_portrait = bool(as_doc or not is_video)
        auto_t = await get_auto_thumbnail(file_path, as_doc=use_portrait)
        if auto_t and await aiopath.exists(str(auto_t)):
            return auto_t

        # If no catalogue image exists, retain the local-frame fallback. For a
        # document-uploaded video crop the frame to portrait proportions.
        if self._listener.thumbnail_layout and is_video and not as_doc:
            grid_t = await get_multiple_frames_thumbnail(
                file_path, self._listener.thumbnail_layout, False
            )
            if grid_t and await aiopath.exists(str(grid_t)):
                return grid_t

        if is_video:
            frame = await get_video_thumbnail(file_path, duration)
            if frame and as_doc and await aiopath.exists(str(frame)):
                try:
                    def _portrait_crop():
                        from PIL import Image, ImageOps
                        import hashlib
                        import os

                        with Image.open(frame) as source:
                            image = ImageOps.fit(
                                source.convert("RGB"),
                                (480, 720),
                                method=Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS,
                                centering=(0.5, 0.42),
                            )
                            os.makedirs("thumbnails/auto_portrait", exist_ok=True)
                            digest = hashlib.sha256(str(file_path).encode()).hexdigest()[:16]
                            output = f"thumbnails/auto_portrait/{digest}.jpg"
                            image.save(output, "JPEG", quality=92, optimize=True)
                            return output
                    frame = await sync_to_async(_portrait_crop)
                except Exception as exc:
                    LOGGER.debug("Could not crop fallback thumbnail to portrait: %s", exc)
            return frame
        elif is_audio:
            return await get_audio_thumbnail(file_path)

        return None

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
        reply_to_message_id,
        force_document=False,
        user_thumb=None,
        user_session=False,
    ):
        self._cancel.clear()
        self._up_file = ospath.basename(file_path)

        is_video, is_audio, is_image = await get_document_type(file_path)

        user_perm = self._listener.user_dict.get("THUMBNAIL") or f"thumbnails/{self._listener.user_id}.jpg"
        has_custom = await aiopath.exists(str(user_perm))
        # Use the same default mode for every upload path: a saved custom thumb
        # is preferred only when present; otherwise automatic artwork is enabled.
        thumb_mode = self._listener.user_dict.get("THUMBNAIL_MODE") or (
            "custom" if has_custom else "auto"
        )
        hd_thumbnail = bool(self._listener.user_dict.get("HD_THUMBNAIL", False))

        duration = 0
        width = 480
        height = 320
        artist = ""
        title = ""
        if is_video:
            duration = (await get_media_info(file_path))[0]
        elif is_audio:
            duration, artist, title = await get_media_info(file_path)

        thumb = None
        if thumb_mode == "auto":
            send_as_document = (
                force_document
                or self._listener.as_doc
                or (not is_video and not is_audio and not is_image)
            )
            thumb = await self._get_auto_thumb(
                file_path,
                is_video=is_video,
                duration=duration,
                is_audio=is_audio,
                as_doc=send_as_document,
            )
        elif thumb_mode == "custom":
            if user_thumb and user_thumb != "none" and await aiopath.exists(str(user_thumb)):
                thumb = user_thumb
            elif self._listener.thumb and self._listener.thumb != "none" and await aiopath.exists(str(self._listener.thumb)):
                thumb = self._listener.thumb
            elif has_custom:
                thumb = user_perm
        elif thumb_mode == "random" and is_video:
            thumb = await get_video_thumbnail(file_path, duration)
        elif thumb_mode == "none":
            thumb = None

        if thumb and thumb != "none" and await aiopath.exists(str(thumb)):
            thumb = await self._make_hd_thumbnail(thumb, file_path)
            thumb = await apply_thumbnail_watermark(thumb, self._listener.user_dict)

        if (
            force_document
            or self._listener.as_doc
            or (not is_video and not is_audio and not is_image)
        ):
            key = "documents"
            if thumb and thumb != "none" and await aiopath.exists(str(thumb)):
                doc_thumb = f"{thumb}_320.jpg"
                try:
                    with Image.open(thumb) as img:
                        img = img.convert("RGB")
                        img.thumbnail((320, 320), Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS)
                        img.save(
                            doc_thumb,
                            "JPEG",
                            quality=100 if hd_thumbnail else 90,
                            subsampling=0 if hd_thumbnail else -1,
                            optimize=True,
                        )
                    thumb = doc_thumb
                except Exception as e:
                    LOGGER.warning(f"Document thumbnail formatting error: {e}")
        elif is_video:
            key = "videos"
            if thumb and thumb != "none" and await aiopath.exists(str(thumb)):
                try:
                    with Image.open(thumb) as img:
                        img = img.convert("RGB")
                        width, height = img.size
                except Exception:
                    pass
        elif is_audio:
            key = "audios"
        else:
            key = "photos"

        if thumb and thumb != "none" and await aiopath.exists(str(thumb)):
            formatted_t = await sync_to_async(
                format_tg_thumbnail, thumb, None,
                hd_thumbnail
            )
            if formatted_t and await aiopath.exists(str(formatted_t)):
                thumb = formatted_t

        if thumb == "none":
            thumb = None

        up_size = ospath.getsize(file_path)
        hyper_user_only = False
        personal_user = getattr(self._listener, "tg_user_client", None)
        if personal_user:
            # A per-user session explicitly owns this upload; do not silently
            # switch it to a global helper/user account.
            use_hyper = False
            user_session = True
        elif up_size > 2097152000 and any(k < 0 for k in self.clients):
            if TgClient.user:
                use_hyper = False
                user_session = True
            else:
                use_hyper = True
                hyper_user_only = True
                user_session = False
        else:
            use_hyper = (
            (Config.USE_HYPER or getattr(self, "_use_user_bots", False))
            and self.clients
            and getattr(self._listener, "tg_user_client", None) is None
        )
        # Reuse the resolved destination from TelegramUploader. A separate
        # destination calculation here used to send media somewhere other than
        # the starter message (especially for preset dumps and per-user dumps).
        upload_chat_id = getattr(self._listener, "_upload_chat_id", None)
        if upload_chat_id is None:
            upload_chat_id = reply_target.chat.id
        thread_id = getattr(self._listener, "_upload_thread_id", None)
        if upload_chat_id != reply_target.chat.id:
            # Never reply to a message from another chat.
            reply_to_message_id = None

        # Destination validation and AUTHORIZED_CHATS policy are handled when
        # TelegramUploader resolves the task destination. Do not re-check an
        # undefined local auth_ids variable here, and do not silently redirect
        # a valid explicitly configured dump destination to the user's DM.
        try:
            if use_hyper:
                hyper_rply = (
                    reply_to_message_id
                    if upload_chat_id == reply_target.chat.id
                    else None
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
                except Exception as hyper_error:
                    # A helper bot may not have permission/contact with this
                    # destination even though the task's primary client does.
                    # Fall back only for permission/peer errors to avoid a
                    # duplicate upload after an ambiguous transport timeout.
                    error_text = str(hyper_error).upper().replace(" ", "_")
                    fallback_markers = (
                        "CHAT_WRITE_FORBIDDEN",
                        "CHAT_ADMIN_REQUIRED",
                        "USER_IS_BLOCKED",
                        "USER_PRIVACY_RESTRICTED",
                        "PEER_ID_INVALID",
                        "CHAT_ID_INVALID",
                        "BOT_METHOD_INVALID",
                        "FORBIDDEN",
                    )
                    if not any(marker in error_text for marker in fallback_markers):
                        raise
                    LOGGER.warning(
                        "HyperUP helper cannot write to destination; retrying with primary client: %s",
                        hyper_error,
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
                        user_session=user_session,
                    )
            else:
                direct_rply = (
                    reply_to_message_id
                    if upload_chat_id == reply_target.chat.id
                    else None
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
            if thumb and (thumb.endswith("_320.jpg") or thumb.endswith("_tg.jpg")) and await aiopath.exists(thumb):
                try:
                    await remove(thumb)
                except Exception:
                    pass
            elif user_thumb is None and thumb is not None and await aiopath.exists(thumb):
                user_perm_thumb = self._listener.user_dict.get("THUMBNAIL") or f"thumbnails/{self._listener.user_id}.jpg"
                if thumb != user_perm_thumb and thumb != f"thumbnails/{self._listener.user_id}.jpg":
                    try:
                        await remove(thumb)
                    except Exception:
                        pass

    async def _send_with_retry(self, send_func, **kwargs):
        timeout_attempts = 0
        while True:
            try:
                return await send_func(**kwargs)
            except (FloodWait, FloodPremiumWait) as f:
                wait_seconds = max(1, int(getattr(f, "value", 1)))
                LOGGER.warning(
                    "HypertgUL flood wait %ss on %s", wait_seconds, self._up_file
                )
                await sleep(wait_seconds + 1)
            except (TimeoutError, ConnectionError, OSError) as exc:
                timeout_attempts += 1
                if timeout_attempts >= 3:
                    LOGGER.error(
                        "HypertgUL Telegram request timed out after %s attempts on %s: %s",
                        timeout_attempts,
                        self._up_file,
                        exc,
                    )
                    raise
                delay = min(2 ** timeout_attempts, 8)
                LOGGER.warning(
                    "HypertgUL transient network error (%s/3) on %s: %s; retrying in %ss",
                    timeout_attempts,
                    self._up_file,
                    exc,
                    delay,
                )
                await sleep(delay)

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
        except (PhotoInvalidDimensions, Exception) as e:
            err_str = str(e).upper()
            if (
                isinstance(e, PhotoInvalidDimensions)
                or "PHOTO_INVALID_DIMENSIONS" in err_str
                or "WIDTH_INVALID" in err_str
                or "HEIGHT_INVALID" in err_str
                or "MEDIA_EMPTY" in err_str
            ):
                LOGGER.warning(f"Thumbnail invalid dimensions ({e}), retrying upload without thumbnail...")
                kwargs.pop("thumb", None)
                kwargs.pop("video_cover", None)
                kwargs.pop("width", None)
                kwargs.pop("height", None)
                if key == "videos":
                    return await self._send_with_retry(client.send_video, **kwargs)
                elif key == "audios":
                    return await self._send_with_retry(client.send_audio, **kwargs)
                elif key == "photos":
                    return await self._send_with_retry(client.send_photo, **kwargs)
                else:
                    return await self._send_with_retry(client.send_document, **kwargs)
            raise

    async def _hyper_send(
        self,
        file_path,
        key,
        thumb,
        cap_mono,
        chat_id,
        reply_to_message_id,
        thread_id=None,
        duration=0,
        width=0,
        height=0,
        artist="",
        title="",
        user_only=False,
    ):
        if user_only:
            candidates = {k: self.work_loads[k] for k in self.clients if k < 0}
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
            if reply_to_message_id:
                kwargs["reply_to_message_id"] = reply_to_message_id
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
                    kwargs["video_cover"] = thumb
                    kwargs["thumb"] = thumb
            elif key == "audios":
                if duration:
                    kwargs["duration"] = duration
                if artist:
                    kwargs["performer"] = artist
                if title:
                    kwargs["title"] = title
                if thumb:
                    kwargs["thumb"] = thumb
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
        reply_to_message_id,
        thread_id=None,
        duration=0,
        width=0,
        height=0,
        artist="",
        title="",
        user_session=False,
    ):
        client = (
            getattr(self._listener, "tg_user_client", None)
            if user_session and getattr(self._listener, "tg_user_client", None)
            else (TgClient.user if user_session and TgClient.user else self._listener.client)
        )
        kwargs = {
            "chat_id": chat_id,
            "disable_notification": True,
            "progress": self._progress,
            "progress_args": (file_path,),
        }
        if cap_mono:
            kwargs["caption"] = cap_mono
        if reply_to_message_id:
            kwargs["reply_to_message_id"] = reply_to_message_id
        elif thread_id:
            kwargs["message_thread_id"] = thread_id

        if key == "videos":
            if thumb:
                kwargs["thumb"] = thumb
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

        return await self._try_send(key, client, kwargs)

    async def cancel(self):
        await super().cancel()
