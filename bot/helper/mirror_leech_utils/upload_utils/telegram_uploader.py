from asyncio import sleep
from logging import getLogger
from os import path as ospath, walk
from re import match as re_match, sub as re_sub
from time import time
import json
import os
import tempfile
from contextlib import suppress

from aioshutil import rmtree
from natsort import natsorted
from pyrogram import StopTransmission
from pyrogram.enums import ChatType
from pyrogram.errors import FloodWait, RPCError

try:
    from pyrogram.errors import FloodPremiumWait
except ImportError:
    FloodPremiumWait = FloodWait

from aiofiles.os import (
    path as aiopath,
    remove,
    rename,
)
from pyrogram.types import (
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    ReplyParameters,
)

from ....core.config_manager import Config
from ....core.tg_client import TgClient
from ...ext_utils.bot_utils import sync_to_async
from ...ext_utils.files_utils import get_base_name, is_archive
from ...ext_utils.status_utils import get_readable_file_size, get_readable_time

from ...ext_utils.media_utils import get_md5_hash, get_media_info, get_language_names
from ...telegram_helper.message_utils import delete_message
from ...ext_utils.hyperul_utils import HypertgUpload

LOGGER = getLogger(__name__)


async def _call_with_flood_retry(method, *args, **kwargs):
    while True:
        try:
            return await method(*args, **kwargs)
        except (FloodWait, FloodPremiumWait) as f:
            LOGGER.warning(f"FloodWait {f.value}s, retrying {method.__name__}")
            await sleep(f.value + 1)



async def _apply_track_data_metadata(file_path, user_dict=None, track_data_enabled=True):
    """Write player-visible audio titles only when the user's settings require it."""
    if not file_path or not ospath.isfile(file_path):
        return False
    user_dict = user_dict if isinstance(user_dict, dict) else {}

    def configured_audio_title():
        value = user_dict.get("AUDIO_METADATA")
        if isinstance(value, str):
            try:
                from ...ext_utils.metadata_utils import MetadataProcessor
                value = MetadataProcessor.parse_string(value)
            except Exception:
                value = {}
        if isinstance(value, dict):
            for key, title in value.items():
                if str(key).strip().lower() == "title" and title is not None and str(title).strip():
                    return str(title).strip()
        return ""

    custom_title = configured_audio_title()
    # A disabled detail setting with no explicit audio title means "leave media
    # metadata as-is". The old path unnecessarily remuxed every uploaded video
    # before the first byte was sent, delaying uploads and risking remux errors.
    if not track_data_enabled and not custom_title:
        return True

    try:
        # Probe stream metadata without loading the media into memory.
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", file_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return False
        streams = json.loads(out.decode("utf-8", "replace")).get("streams", [])
        audio = [st for st in streams if st.get("codec_type") == "audio"]
        if not audio:
            return False

        # Only AUDIO_METADATA.title is an audio-track title. A generic
        # METADATA/SET_ALL_METADATA title is handled by the video stream mapper.
        codec_names = {
            "eac3": "DD+", "ac3": "AC3", "aac": "AAC", "truehd": "TrueHD",
            "dts": "DTS", "dts_hd": "DTS-HD", "opus": "Opus", "mp3": "MP3",
            "flac": "FLAC", "alac": "ALAC", "pcm_s16le": "PCM",
        }
        titles = []
        for st in audio:
            tags = st.get("tags") or {}
            raw_lang = str(tags.get("language") or tags.get("LANGUAGE") or "und").strip()
            display_lang, _native_lang = get_language_names(raw_lang)
            lang = display_lang or "Unknown"

            existing_title = str(tags.get("title") or tags.get("TITLE") or "").strip()
            # Prefer the already-applied stream title: metadata placeholders such
            # as {audiolang} have been resolved there. The raw configured value
            # is a fallback only, never suffixed with codec/language notes.
            track_title = existing_title or custom_title
            if custom_title:
                titles.append(track_title[:180])
                continue

            # Disabled: preserve a title that is already on this audio stream,
            # followed by its clean language label. No global video title.
            if not track_data_enabled:
                base_title = existing_title
                parts = [base_title] if base_title else []
                language_enabled = user_dict.get("TRACK_DATA_LANGUAGE", True)
                if language_enabled and lang != "Unknown" and lang.casefold() not in base_title.casefold():
                    parts.append(lang)
                title = " - ".join(parts) or (lang if language_enabled and lang != "Unknown" else "Audio")
                titles.append(title[:180])
                continue

            codec = codec_names.get(str(st.get("codec_name", "")).lower(),
                                    str(st.get("codec_name", "Audio")).upper())
            layout = str(st.get("channel_layout") or "")
            channels = st.get("channels")
            if layout:
                layout = layout.replace("stereo", "2.0").replace("mono", "1.0")
                channel_text = layout
            elif channels == 6:
                channel_text = "5.1"
            elif channels == 8:
                channel_text = "7.1"
            elif channels == 2:
                channel_text = "2.0"
            elif channels == 1:
                channel_text = "1.0"
            elif channels:
                channel_text = f"{channels}ch"
            else:
                channel_text = ""
            bitrate = st.get("bit_rate")
            try:
                bitrate_text = f"{max(1, round(int(bitrate) / 1000))}Kbps" if bitrate else ""
            except (TypeError, ValueError):
                bitrate_text = ""
            detail = " ".join(x for x in (codec, channel_text) if x)
            if bitrate_text:
                detail = f"{detail} - {bitrate_text}" if detail else bitrate_text
            # Language in track-data titles is independently configurable.
            # Keep codec/channel/bitrate details regardless of this toggle.
            language_enabled = user_dict.get("TRACK_DATA_LANGUAGE", True)
            title = f"{detail} [{lang}]" if language_enabled and lang != "Unknown" else detail
            titles.append(title[:180] or (lang if language_enabled and lang != "Unknown" else "Audio"))

        # Stream-copy remux: no re-encoding, so quality is unchanged. Keep original if unsupported.
        ext = ospath.splitext(file_path)[1]
        fd, temp_path = tempfile.mkstemp(prefix=".trackdata_", suffix=ext, dir=ospath.dirname(file_path) or ".")
        os.close(fd)
        try:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", file_path, "-map", "0", "-c", "copy"]
            for idx, title in enumerate(titles):
                cmd.extend([f"-metadata:s:a:{idx}", f"title={title}"])
            cmd.append(temp_path)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, err = await proc.communicate()
            if proc.returncode != 0 or not ospath.exists(temp_path) or ospath.getsize(temp_path) <= 0:
                return False
            os.replace(temp_path, file_path)
            return True
        finally:
            if ospath.exists(temp_path):
                with suppress(Exception):
                    os.remove(temp_path)
    except Exception as e:
        LOGGER.warning(f"Could not apply track-data metadata to {file_path}: {e}")
        return False


class TelegramUploader:
    def __init__(self, listener, path):
        self._processed_bytes = 0
        self._listener = listener
        self._path = path
        self._start_time = time()
        self._total_files = 0
        self._thumb = self._listener.thumb or listener.user_dict.get("THUMBNAIL") or f"thumbnails/{listener.user_id}.jpg"
        self._msgs_dict = {}
        self._corrupted = 0
        self._is_corrupted = False
        self._media_dict = {"videos": {}, "documents": {}}
        self._last_msg_in_group = False
        self._lprefix = ""
        self._lsuffix = ""
        self._lcaption = ""
        self._lfont = ""
        self._track_data_enabled = False
        self._bot_pm = False
        self._media_group = False
        self._is_private = False
        self._sent_msg = None
        self._user_session = self._listener.transmission_mode in ("user", "both")
        self._user_client = None
        self._hu: HypertgUpload | None = None
        self._error = ""
        self._upload_seq = []
        self._msg_to_seq = {}

    async def _user_settings(self):
        settings_map = {
            "MEDIA_GROUP": ("_media_group", False),
            "BOT_PM": ("_bot_pm", False),
            "LEECH_PREFIX": ("_lprefix", ""),
            "LEECH_SUFFIX": ("_lsuffix", ""),
            "LEECH_CAPTION": ("_lcaption", ""),
            "LEECH_FONT": ("_lfont", ""),
            "TRACK_DATA_ENABLE": ("_track_data_enabled", False),
        }

        for key, (attr, default) in settings_map.items():
            user_value = self._listener.user_dict.get(key)
            value = getattr(Config, key, default) if user_value is None else user_value
            setattr(self, attr, value)

        user_perm = self._listener.user_dict.get("THUMBNAIL") or f"thumbnails/{self._listener.user_id}.jpg"
        has_custom = await aiopath.exists(str(user_perm))
        thumb_mode = self._listener.user_dict.get("THUMBNAIL_MODE") or ("custom" if has_custom else "auto")

        self._thumb = None
        if thumb_mode == "custom":
            user_thumb = self._listener.thumb or self._listener.user_dict.get("THUMBNAIL") or f"thumbnails/{self._listener.user_id}.jpg"
            if user_thumb and user_thumb != "none":
                if await aiopath.exists(str(user_thumb)):
                    self._thumb = user_thumb
                    self._listener.thumb = user_thumb
                elif isinstance(user_thumb, str) and user_thumb.startswith(("http://", "https://")):
                    from ...ext_utils.media_utils import download_image_thumb
                    downloaded = await download_image_thumb(user_thumb)
                    if downloaded and await aiopath.exists(downloaded):
                        self._thumb = downloaded
                        self._listener.thumb = downloaded

        if self._thumb and self._thumb != "none" and await aiopath.exists(str(self._thumb)):
            from ...ext_utils.media_utils import apply_thumbnail_watermark
            self._thumb = await apply_thumbnail_watermark(self._thumb, self._listener.user_dict)
            self._listener.thumb = self._thumb

        # Optional per-user Telegram session string for uploads.
        session_mode = self._listener.user_dict.get("TELEGRAM_SESSION_MODE", "disabled")
        if session_mode in ("upload", "both"):
            self._user_client = await TgClient.get_personal_user(
                self._listener.user_id,
                self._listener.user_dict.get("TELEGRAM_SESSION_STRING"),
            )
            if self._user_client:
                self._user_session = True
                self._listener.tg_user_client = self._user_client
            else:
                LOGGER.warning(
                    "Personal Telegram upload session unavailable; falling back to configured bot/user session"
                )
        if self._user_client is None and TgClient.user is not None and self._user_session:
            self._user_client = TgClient.user
        if self._user_client is not None:
            self._listener.tg_user_client = self._user_client

        # User-bot mode is strict: when enabled, use only that user's added bots.
        # Keep one bot for upload + dump copies so it can access its own messages.
        user_tokens = self._listener.user_dict.get("BOT_TOKENS", [])
        addbot_required = self._listener.user_dict.get("ADDBOT_REQUIRED") is True
        if addbot_required and isinstance(user_tokens, list) and user_tokens:
            ubots = await TgClient.get_user_bots(self._listener.user_id, user_tokens)
            if ubots:
                # One added bot handles the upload and every dump copy, avoiding
                # cross-bot message-ID access failures with a multi-bot pool.
                first_key = next(iter(ubots))
                self._hu_clients = {first_key: ubots[first_key]}
                self._listener.client = self._hu_clients[first_key]

        if addbot_required and not getattr(self, "_hu_clients", None):
            raise RuntimeError("/addbot is ON, but no added bot is connected. Add a valid bot with /addbot before starting tasks.")

        # Global helper bots are allowed only when strict per-user mode is OFF.
        if (
            not addbot_required
            and not getattr(self, "_hu_clients", None)
            and getattr(Config, "USE_HYPER", True)
            and getattr(TgClient, "helper_bots", None)
        ):
            self._hu_clients = dict(TgClient.helper_bots)
            LOGGER.info("HyperUP upload engine enabled with %d global helper client(s)", len(self._hu_clients))

    async def _msg_to_reply(self):
        from ...ext_utils.bot_utils import parse_dest

        if self._listener.user_dict.get("ADDBOT_REQUIRED") is True and not getattr(self, "_hu_clients", None):
            raise RuntimeError(
                "/addbot is ON, but no added bot is connected. Open /addbot, replace the invalid token, and try again."
            )

        if self._user_session and self._user_client is None:
            self._user_session = False

        # Resolve one destination once and use it for the starter message and
        # the HyperUP/direct upload. This prevents the upload engine from
        # silently choosing a different chat than the task's selected dump.
        target_thread_id = None
        if self._listener.up_dest:
            if isinstance(self._listener.up_dest, int):
                target_chat_id = self._listener.up_dest
                target_thread_id = getattr(self._listener, "chat_thread_id", None)
            else:
                target_chat_id, target_thread_id = parse_dest(self._listener.up_dest)
                target_thread_id = target_thread_id or getattr(
                    self._listener, "chat_thread_id", None
                )
        elif getattr(self._listener, "has_preset_dump", False) and self._listener.key_dump_dests:
            first_dump = self._listener.key_dump_dests[0]
            if isinstance(first_dump, int):
                target_chat_id = first_dump
            else:
                target_chat_id, target_thread_id = parse_dest(first_dump)
        elif self._listener.leech_dest:
            if isinstance(self._listener.leech_dest, int):
                target_chat_id = self._listener.leech_dest
                target_thread_id = getattr(self._listener, "leech_thread_id", None)
            else:
                target_chat_id, target_thread_id = parse_dest(self._listener.leech_dest)
                target_thread_id = target_thread_id or getattr(
                    self._listener, "leech_thread_id", None
                )
        elif self._listener.user_dict.get("LEECH_DUMP_CHAT"):
            udump = self._listener.user_dict.get("LEECH_DUMP_CHAT")
            if isinstance(udump, int):
                target_chat_id = udump
            else:
                target_chat_id, target_thread_id = parse_dest(udump)
        else:
            target_chat_id = self._listener.user_id

        # Shared state makes HyperUP target exactly the chat in which this
        # starter message is created, including the selected topic/thread.
        self._listener._upload_chat_id = target_chat_id
        self._listener._upload_thread_id = target_thread_id
        sender_client = (
            self._user_client
            if self._user_session and self._user_client is not None
            else self._listener.client
        )

        if self._user_session:
            try:
                self._sent_msg = await _call_with_flood_retry(
                    self._user_client.get_messages,
                    chat_id=target_chat_id,
                    message_ids=self._listener.mid,
                )
            except Exception:
                self._sent_msg = None

        else:
            self._sent_msg = (
                self._listener.message
                if target_chat_id == self._listener.message.chat.id
                else None
            )

        if self._sent_msg is None or getattr(self._sent_msg, "chat", None) is None:
            try:
                kwargs = {
                    "chat_id": target_chat_id,
                    "text": "Starting upload...",
                    "disable_web_page_preview": True,
                    "disable_notification": True,
                }
                if target_thread_id:
                    kwargs["message_thread_id"] = target_thread_id
                self._sent_msg = await _call_with_flood_retry(
                    sender_client.send_message, **kwargs
                )
            except Exception as e:
                LOGGER.warning(
                    "Unable to create upload starter message in chat %s: %s",
                    target_chat_id,
                    e,
                )
                self._sent_msg = self._listener.message

        if self._sent_msg is None or getattr(self._sent_msg, "chat", None) is None:
            await self._listener.on_upload_error(
                f"Unable to initialize upload target {target_chat_id}"
            )
            return False
        self._is_private = self._sent_msg.chat.type == ChatType.PRIVATE
        return True

    async def _prepare_file(self, pre_file_, dirpath):
        cap_file_ = file_ = pre_file_
        lprefix = self._lprefix
        lsuffix = self._lsuffix
        lcaption = self._lcaption

        if lprefix:
            cap_file_ = lprefix.replace(r"\s", " ") + file_
            lprefix = re_sub(r"<.*?>", "", lprefix).replace(r"\s", " ")
            if not file_.startswith(lprefix):
                file_ = f"{lprefix}{file_}"

        if lsuffix:
            name, ext = ospath.splitext(cap_file_)
            cap_file_ = name + lsuffix.replace(r"\s", " ") + ext
            lsuffix = re_sub(r"<.*?>", "", lsuffix).replace(r"\s", " ")

        lfont = self._lfont
        font_tag = "tg-spoiler" if lfont in ("tg-spoiler", "spoiler") else (lfont if (lfont and lfont != "none") else "")
        cap_mono = cap_file_

        if lcaption:
            lcaption = re_sub(
                r"(\\\||\\\{|\\\}|\\s)",
                lambda m: {r"\|": "%%", r"\{": "&%&", r"\}": "$%$", r"\s": " "}[
                    m.group(0)
                ],
                lcaption,
            )

            parts = lcaption.split("|")
            parts[0] = re_sub(
                r"\{([^}]+)\}", lambda m: f"{{{m.group(1).lower()}}}", parts[0]
            )
            up_path = ospath.join(dirpath, pre_file_)
            media_info_res = await get_media_info(up_path, True)
            if len(media_info_res) == 5:
                dur, qual, lang, subs, langr = media_info_res
            else:
                dur, qual, lang, subs = media_info_res[:4]
                langr = lang
            # Keep template placeholders predictable and ensure missing media metadata
            # never leaves the caption formatter with undefined values.
            # Keep both language placeholders useful when only one script/name
            # is returned by the probe. get_media_info resolves ISO language tags
            # into display (languages) and native (languagesr) names.
            lang = ", ".join(dict.fromkeys(
                item.strip() for item in str(lang or "").split(",") if item.strip()
            ))
            langr = ", ".join(dict.fromkeys(
                item.strip() for item in str(langr or "").split(",") if item.strip()
            ))
            if not lang and langr:
                lang = langr
            if not langr and lang:
                langr = lang
            subs = ", ".join(dict.fromkeys(
                item.strip() for item in str(subs or "").split(",") if item.strip()
            ))
            cap_mono = parts[0].format(
                filename=cap_file_,
                size=get_readable_file_size(await aiopath.getsize(up_path)),
                duration=get_readable_time(dur),
                quality=qual,
                languages=lang,
                languagesr=langr,
                subtitles=subs,
                md5_hash=await sync_to_async(get_md5_hash, up_path),
                mime_type=self._listener.file_details.get("mime_type", "text/plain"),
                prefilename=self._listener.file_details.get("filename", ""),
                precaption=self._listener.file_details.get("caption", ""),
            )

            for part in parts[1:]:
                args = part.split(":")
                cap_mono = cap_mono.replace(
                    args[0],
                    args[1] if len(args) > 1 else "",
                    int(args[2]) if len(args) == 3 else -1,
                )
            cap_mono = re_sub(
                r"%%|&%&|\$%\$",
                lambda m: {"%%": "|", "&%&": "{", "$%$": "}"}[m.group()],
                cap_mono,
            )

        if font_tag and not cap_mono.strip().startswith(f"<{font_tag}>"):
            cap_mono = f"<{font_tag}>{cap_mono}</{font_tag}>"

        if lsuffix:
            name, ext = ospath.splitext(file_)
            file_ = f"{name}{lsuffix}{ext}"

        old_path = ospath.join(dirpath, pre_file_)
        new_path = ospath.join(dirpath, file_)
        if old_path != new_path and not await aiopath.exists(new_path):
            try:
                await rename(old_path, new_path)
            except Exception as e:
                LOGGER.warning(f"Failed to rename {old_path} to {new_path}: {e}")
                new_path = old_path

        # Always apply the user's display mode to incoming media: detailed track
        # data when enabled, otherwise only configured/existing title + language.
        await _apply_track_data_metadata(
            new_path,
            self._listener.user_dict,
            track_data_enabled=self._track_data_enabled,
        )

        return new_path, cap_mono

    def _get_input_media(self, subkey, key):
        rlist = []
        for msg in self._media_dict[key][subkey]:
            caption = msg.caption.html if msg.caption else None
            if key == "videos":
                input_media = InputMediaVideo(media=msg.video.file_id, caption=caption)
            else:
                input_media = InputMediaDocument(
                    media=msg.document.file_id, caption=caption
                )
            rlist.append(input_media)
        return rlist

    def _delivery_bot(self):
        """Use the added bot for all copies when strict addbot mode is enabled."""
        if self._listener.user_dict.get("ADDBOT_REQUIRED") is True:
            return self._listener.client
        return TgClient.bot

    async def _send_screenshots(self, dirpath, outputs):
        inputs = [
            InputMediaPhoto(ospath.join(dirpath, p), p.rsplit("/", 1)[-1])
            for p in outputs
        ]
        for i in range(0, len(inputs), 10):
            batch = inputs[i : i + 10]
            if Config.BOT_PM:
                await _call_with_flood_retry(
                    self._delivery_bot().send_media_group,
                    chat_id=self._listener.user_id,
                    media=batch,
                    disable_notification=True,
                )
            self._sent_msg = (
                await _call_with_flood_retry(
                    self._sent_msg.reply_media_group,
                    media=batch,
                    reply_parameters=ReplyParameters(message_id=self._sent_msg.id),
                    disable_notification=True,
                )
            )[-1]

    async def _send_media_group(self, subkey, key, msgs):
        old_ids = [(msg[0], msg[1]) for msg in msgs]
        for index, msg in enumerate(msgs):
            if self._listener.transmission_mode == "both" or not self._user_session:
                msgs[index] = await _call_with_flood_retry(
                    self._listener.client.get_messages,
                    chat_id=msg[0],
                    message_ids=msg[1],
                )
            else:
                msgs[index] = await _call_with_flood_retry(
                    self._user_client.get_messages, chat_id=msg[0], message_ids=msg[1]
                )
        media = self._get_input_media(subkey, key)
        del self._media_dict[key][subkey]
        if reply_target := msgs[0].reply_to_message:
            msgs_list = await _call_with_flood_retry(
                reply_target.reply_media_group,
                media=media,
                reply_parameters=ReplyParameters(message_id=reply_target.id),
                disable_notification=True,
            )
        else:
            msgs_list = await _call_with_flood_retry(
                (
                    self._listener.client
                    if self._listener.transmission_mode == "both"
                    or not self._user_session
                    else self._user_client
                ).send_media_group,
                chat_id=msgs[0].chat.id,
                media=media,
                disable_notification=True,
                message_thread_id=(
                    self._listener.chat_thread_id
                    if msgs[0].chat.id == self._listener.up_dest
                    else None
                ),
            )
        for msg in msgs:
            if msg.link in self._msgs_dict:
                del self._msgs_dict[msg.link]
            await delete_message(msg)
        if self._listener.is_super_chat or self._listener.up_dest:
            for m in msgs_list:
                self._msgs_dict[m.link] = m.caption
        for i, (old_cid, old_mid) in enumerate(old_ids):
            old_key = (old_cid, old_mid)
            if old_key in self._msg_to_seq:
                seq_idx = self._msg_to_seq.pop(old_key)
                new_msg = msgs_list[i]
                self._upload_seq[seq_idx] = {
                    "chat_id": new_msg.chat.id,
                    "msg_id": new_msg.id,
                    "link": new_msg.link,
                    "file_": self._upload_seq[seq_idx]["file_"],
                }
                self._msg_to_seq[(new_msg.chat.id, new_msg.id)] = seq_idx
        self._sent_msg = msgs_list[-1]

    async def _copy_media(self):
        try:
            if self._bot_pm:
                await _call_with_flood_retry(
                    self._delivery_bot().copy_message,
                    chat_id=self._listener.user_id,
                    from_chat_id=self._sent_msg.chat.id,
                    message_id=self._sent_msg.id,
                    reply_to_message_id=(
                        self._listener.pm_msg.id if self._listener.pm_msg else None
                    ),
                )
        except Exception as err:
            if not self._listener.is_cancelled:
                err_msg = str(err)
                if "Can't copy" in err_msg:
                    LOGGER.warning(
                        f"BotPM copy skipped (restricted content): {err_msg}"
                    )
                else:
                    LOGGER.error(f"Failed To Send in BotPM:\n{err_msg}")

    async def _sequence_copies(self, src_chat):
        """Deliver each uploaded message to exactly one final destination.

        A configured dump takes precedence. If no dump is configured (or its
        copy fails), deliver once to the requesting user's DM. Do not fan out
        to every configured dump and then also DM the same file.
        """
        from ...ext_utils.bot_utils import parse_dest

        def parse_target(raw):
            if not raw:
                return None
            try:
                chat, thread = (
                    parse_dest(raw)
                    if not isinstance(raw, int)
                    else (raw, None)
                )
                if isinstance(chat, str) and chat.lstrip("-").isdigit():
                    chat = int(chat)
                return (chat, thread) if chat else None
            except Exception as err:
                LOGGER.warning("Ignoring invalid leech dump destination %r: %s", raw, err)
                return None

        # A task-level -up destination is an explicit routing override.
        # Delivering there is the whole operation: do not additionally fan out
        # to preset/global dumps or the user's DM.
        dump_target = None
        preset_dests = getattr(self._listener, "key_dump_dests", None) or []
        explicit_up = parse_target(getattr(self._listener, "up_dest", None))
        if explicit_up and str(explicit_up[0]) != str(self._listener.user_id):
            dump_target = explicit_up
        else:
            # Pick ONE configured dump deterministically when -up is absent.
            if getattr(self._listener, "has_preset_dump", False):
                for raw in preset_dests:
                    dump_target = parse_target(raw)
                    if dump_target:
                        break

            if dump_target is None:
                for raw in (getattr(self._listener, "leech_dest", None),
                            self._listener.user_dict.get("LEECH_DUMP_CHAT"),
                            getattr(Config, "LEECH_LOG_CHAT", "")):
                    candidate = parse_target(raw)
                    if candidate and str(candidate[0]) != str(self._listener.user_id):
                        dump_target = candidate
                        break
        if dump_target is None:
            dump_chats = getattr(Config, "LEECH_DUMP_CHATS", {}) or {}
            if isinstance(dump_chats, dict):
                for raw in dump_chats.values():
                    candidate = parse_target(raw)
                    if candidate and str(candidate[0]) != str(self._listener.user_id):
                        dump_target = candidate
                        break

        auth_ids = set()
        for raw in str(getattr(Config, "AUTHORIZED_CHATS", "") or "").split():
            base = raw.split("|", 1)[0].strip()
            if base.lstrip("-").isdigit():
                auth_ids.add(int(base))
        if dump_target and isinstance(dump_target[0], int):
            if dump_target[0] in auth_ids:
                # Authorized chats are not valid destinations unless explicitly
                # configured as a dump above.
                explicitly_configured = any(
                    parse_target(raw) == dump_target
                    for raw in [self._listener.user_dict.get("LEECH_DUMP_CHAT"),
                                getattr(Config, "LEECH_LOG_CHAT", "")]
                    + list((getattr(Config, "LEECH_DUMP_CHATS", {}) or {}).values())
                    + (preset_dests if getattr(self._listener, "has_preset_dump", False) else [])
                )
                if not explicitly_configured:
                    dump_target = None

        for entry in self._upload_seq:
            if not entry or self._listener.is_cancelled:
                continue

            source_chat = entry["chat_id"]
            source_msg = entry["msg_id"]
            target = dump_target
            delivered = False

            if target:
                dest_chat, thread_id = target
                if str(dest_chat) == str(source_chat):
                    # The media was uploaded directly into its final dump chat.
                    delivered = True
                else:
                    try:
                        kwargs = {"message_thread_id": thread_id} if thread_id else {}
                        dump_client = (
                            self._listener.client
                            if self._listener.user_dict.get("ADDBOT_REQUIRED") is True
                            else TgClient.bot
                        )
                        await _call_with_flood_retry(
                            dump_client.copy_message,
                            chat_id=dest_chat,
                            from_chat_id=source_chat,
                            message_id=source_msg,
                            **kwargs,
                        )
                        delivered = True
                    except Exception as err:
                        LOGGER.error(
                            "Failed to deliver %s to dump %s: %s",
                            entry.get("file_"), dest_chat, err,
                        )

            if (
                not delivered
                and self._listener.user_dict.get("ADDBOT_REQUIRED") is not True
                and not self._listener.is_cancelled
                and not (explicit_up and str(explicit_up[0]) != str(self._listener.user_id))
            ):
                try:
                    if str(source_chat) == str(self._listener.user_id):
                        delivered = True
                    else:
                        await _call_with_flood_retry(
                            self._delivery_bot().copy_message,
                            chat_id=self._listener.user_id,
                            from_chat_id=source_chat,
                            message_id=source_msg,
                        )
                        delivered = True
                except Exception as err:
                    LOGGER.error(
                        "Could not deliver output to user DM %s: %s",
                        self._listener.user_id, err,
                    )

    async def _upload_file_task(self, file_, f_path, dirpath, user_session, seq_idx):
        up_path = None
        try:
            up_path, cap_mono = await self._prepare_file(file_, dirpath)
            sent = await self._upload_file(
                cap_mono, up_path, file_, seq_idx, user_session=user_session
            )
            if sent and not self._is_corrupted:
                if self._listener.is_super_chat or self._listener.up_dest:
                    if not self._is_private:
                        entry = self._upload_seq[seq_idx]
                        if entry["msg_id"] == sent.id:
                            self._msgs_dict[sent.link] = file_
            return sent
        except StopTransmission:
            return None
        except Exception as err:
            LOGGER.error(f"{err}. Path: {f_path}", exc_info=True)
            self._error = str(err)
            self._corrupted += 1
            return None
        finally:
            path_to_clean = up_path or f_path
            if not self._listener.is_cancelled and await aiopath.exists(path_to_clean):
                await remove(path_to_clean)

    async def upload(self):
        await self._user_settings()
        res = await self._msg_to_reply()
        if not res:
            return
        upload_tasks = []
        seq_idx = 0
        for dirpath, _, files in natsorted(await sync_to_async(walk, self._path)):
            if dirpath.strip().endswith("/yt-dlp-thumb"):
                continue
            if dirpath.strip().endswith("_mltbss"):
                await self._send_screenshots(dirpath, files)
                await rmtree(dirpath, ignore_errors=True)
                continue
            for file_ in natsorted(files):
                self._error = ""
                f_path = ospath.join(dirpath, file_)
                if not await aiopath.exists(f_path):
                    LOGGER.error(f"{f_path} not exists! Continue uploading!")
                    continue
                try:
                    f_size = await aiopath.getsize(f_path)
                    self._total_files += 1
                    if f_size == 0:
                        LOGGER.warning(f"{f_path} size is zero, skipping")
                        self._corrupted += 1
                        if not self._listener.is_cancelled:
                            await remove(f_path)
                        continue
                    if self._listener.is_cancelled:
                        return
                    if self._last_msg_in_group:
                        group_lists = [
                            x for v in self._media_dict.values() for x in v.keys()
                        ]
                        match = re_match(r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)", f_path)
                        if not match or match and match.group(0) not in group_lists:
                            for key, value in list(self._media_dict.items()):
                                for subkey, msgs in list(value.items()):
                                    if len(msgs) > 1:
                                        await self._send_media_group(subkey, key, msgs)
                    if self._user_client is not None:
                        self._user_session = True
                    elif self._listener.transmission_mode == "both":
                        self._user_session = f_size > 2097152000
                    elif (
                        not self._user_session
                        and f_size > 2097152000
                        and TgClient.user is not None
                    ):
                        self._user_session = True
                    self._last_msg_in_group = False
                    self._upload_seq.append(None)
                    sent = await self._upload_file_task(
                        file_, f_path, dirpath, self._user_session, seq_idx
                    )
                    upload_tasks.append(sent)
                    seq_idx += 1
                    if self._listener.is_cancelled:
                        return
                except Exception as err:
                    LOGGER.error(f"{err}. Path: {f_path}", exc_info=True)
                    self._error = str(err)
                    self._corrupted += 1
                    if self._listener.is_cancelled:
                        return
        # All upload calls above have been awaited. Avoid an arbitrary one-second
        # delay before flushing grouped media; yield once so status callbacks can run.
        await sleep(0)
        for key, value in list(self._media_dict.items()):
            for subkey, msgs in list(value.items()):
                if len(msgs) > 1:
                    try:
                        await self._send_media_group(subkey, key, msgs)
                    except Exception as e:
                        LOGGER.info(
                            f"While sending media group at the end of task. Error: {e}"
                        )
        if self._upload_seq:
            src_chat = self._listener.message.chat
            await self._sequence_copies(src_chat)
            self._msgs_dict = {
                e["link"]: e["file_"] for e in self._upload_seq if e is not None
            }
        if self._listener.is_cancelled:
            return
        if self._total_files == 0:
            await self._listener.on_upload_error(
                "No files to upload. In case you have filled EXCLUDED_EXTENSIONS, then check if all files have those extensions or not."
            )
            return
        if self._total_files <= self._corrupted:
            await self._listener.on_upload_error(
                f"Files Corrupted or unable to upload. {self._error or 'Check logs!'}"
            )
            return
        LOGGER.info(f"Leech Completed: {self._listener.name}")
        await self._listener.on_upload_complete(
            None, self._msgs_dict, self._total_files, self._corrupted
        )
        return

    async def _upload_file(
        self, cap_mono, o_path, file_, seq_idx, force_document=False, user_session=False
    ):
        if self._sent_msg is None:
            LOGGER.error("Cannot upload: _sent_msg is None")
            await self._listener.on_upload_error(
                "Upload failed: Message not initialized"
            )
            return

        if not hasattr(self._sent_msg, "chat") or self._sent_msg.chat is None:
            LOGGER.error("Cannot upload: _sent_msg.chat is None")
            await self._listener.on_upload_error(
                "Upload failed: Invalid message object"
            )
            return

        if (
            self._thumb is not None
            and not await aiopath.exists(self._thumb)
            and self._thumb != "none"
        ):
            self._thumb = None
        self._is_corrupted = False
        try:
            if self._hu is None:
                self._hu = HypertgUpload(self)

            sent_msg = await self._hu.upload(
                file_path=o_path,
                cap_mono=cap_mono,
                reply_target=self._sent_msg,
                reply_to_message_id=self._sent_msg.id,
                force_document=force_document,
                user_session=user_session,
                user_thumb=self._thumb,
            )

            if self._listener.is_cancelled:
                return

            self._sent_msg = sent_msg

            self._upload_seq[seq_idx] = {
                "chat_id": sent_msg.chat.id,
                "msg_id": sent_msg.id,
                "link": sent_msg.link,
                "file_": file_,
            }
            self._msg_to_seq[(sent_msg.chat.id, sent_msg.id)] = seq_idx

            if (
                not self._listener.is_cancelled
                and self._media_group
                and (sent_msg.video or sent_msg.document)
            ):
                key = "documents" if sent_msg.document else "videos"
                if match := re_match(r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)", o_path):
                    pname = match.group(0)
                    if pname in self._media_dict[key].keys():
                        self._media_dict[key][pname].append(
                            [sent_msg.chat.id, sent_msg.id]
                        )
                    else:
                        self._media_dict[key][pname] = [[sent_msg.chat.id, sent_msg.id]]
                    msgs = self._media_dict[key][pname]
                    if len(msgs) == 10:
                        await self._send_media_group(pname, key, msgs)
                    else:
                        self._last_msg_in_group = True

            return sent_msg
        except StopTransmission:
            raise
        except Exception as err:
            err_type = "RPCError: " if isinstance(err, RPCError) else ""
            LOGGER.error(f"{err_type}{err}. Path: {o_path}", exc_info=True)
            raise err

    @property
    def speed(self):
        try:
            return self._processed_bytes / (time() - self._start_time)
        except ZeroDivisionError:
            return 0

    @property
    def processed_bytes(self):
        return self._processed_bytes

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Upload: {self._listener.name}")
        await self._listener.on_upload_error("your upload has been stopped!")
