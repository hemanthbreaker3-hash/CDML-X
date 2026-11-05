import re
from asyncio import gather, sleep
from html import escape
from time import time
from mimetypes import guess_type
from contextlib import suppress
from os import path as ospath, walk, replace as os_replace
from pyrogram.enums import ButtonStyle

from aiofiles.os import listdir, remove, path as aiopath
from niquests import utils as rutils

from ... import (
    bot_loop,
    intervals,
    task_dict,
    task_dict_lock,
    LOGGER,
    non_queued_up,
    non_queued_dl,
    queued_up,
    queued_dl,
    queue_dict_lock,
    same_directory_lock,
    DOWNLOAD_DIR,
)
from ...modules.metadata import apply_metadata_title
from ..common import TaskConfig
from ..ext_utils.task_manager import clear_task_state
from ...core.tg_client import TgClient
from ...core.config_manager import Config
from ...core.torrent_manager import TorrentManager
from ..ext_utils.bot_utils import sync_to_async, parse_dest
from ..ext_utils.links_utils import encode_slink
from ..ext_utils.db_handler import database
from ..ext_utils.files_utils import (
    clean_download,
    clean_target,
    create_recursive_symlink,
    get_path_size,
    join_files,
    remove_excluded_files,
    move_and_merge,
)
from ..ext_utils.links_utils import is_gdrive_id
from ..ext_utils.status_utils import get_readable_file_size, get_readable_time
from ..ext_utils.task_manager import check_running_tasks, start_from_queued
from ..mirror_leech_utils.uphoster_utils.multi_upload import MultiUphosterUpload
from ..mirror_leech_utils.gdrive_utils.upload import GoogleDriveUpload
from ..mirror_leech_utils.rclone_utils.transfer import RcloneTransferHelper
from ..mirror_leech_utils.upload_utils.mega_upload import add_mega_upload
from ..mirror_leech_utils.status_utils.uphoster_status import UphosterStatus
from ..mirror_leech_utils.status_utils.gdrive_status import (
    GoogleDriveStatus,
)
from ..mirror_leech_utils.status_utils.queue_status import QueueStatus
from ..mirror_leech_utils.status_utils.rclone_status import RcloneStatus
from ..mirror_leech_utils.status_utils.telegram_status import TelegramStatus
from ..mirror_leech_utils.status_utils.yt_status import YtStatus
from ..mirror_leech_utils.upload_utils.telegram_uploader import TelegramUploader
from ..mirror_leech_utils.youtube_utils.youtube_upload import YouTubeUpload
from ..telegram_helper.button_build import ButtonMaker
from ..telegram_helper.message_utils import (
    delete_links,
    delete_message,
    delete_status,
    send_message,
    update_status_message,
)


class TaskListener(TaskConfig):
    def __init__(self):
        super().__init__()

    async def clean(self):
        with suppress(Exception):
            if st := intervals["status"]:
                for intvl in list(st.values()):
                    intvl.cancel()
            intervals["status"].clear()
            await gather(TorrentManager.aria2.purgeDownloadResult(), delete_status())

    def clear(self):
        self.subname = ""
        self.subsize = 0
        self.files_to_proceed = []
        self.proceed_count = 0
        self.progress = True

    async def _set_name_from_path(self, path, base_dir=None):
        """Keep file names intact when tool outputs live in nested folders."""
        if not path:
            return
        try:
            if await aiopath.isfile(path):
                self.name = ospath.basename(path)
                return
        except (TypeError, OSError):
            pass
        if base_dir:
            try:
                relative = ospath.relpath(path, base_dir)
                first_part = relative.split(ospath.sep, 1)[0]
                if first_part not in ("", ".", ".."):
                    self.name = first_part
                    return
            except (TypeError, ValueError, OSError):
                pass
        self.name = ospath.basename(ospath.normpath(path)) or self.name

    async def remove_from_same_dir(self):
        async with task_dict_lock:
            if (
                self.folder_name
                and self.same_dir
                and self.mid in self.same_dir[self.folder_name]["tasks"]
            ):
                self.same_dir[self.folder_name]["tasks"].remove(self.mid)
                self.same_dir[self.folder_name]["total"] -= 1

    async def on_download_start(self):
        mode_name = "Leech" if self.is_leech else "Mirror"
        if (self.bot_pm or self.is_leech) and self.is_super_chat:
            self.pm_msg = await send_message(
                self.user_id,
                f"""➲ <b><u>Task Started :</u></b>
┃
┖ <b>Link:</b> <a href='{self.source_url}'>Click Here</a>
""",
            )
        if Config.LINKS_LOG_ID:
            await send_message(
                Config.LINKS_LOG_ID,
                f"""➲  <b><u>{mode_name} Started:</u></b>
 ┃
 ┠ <b>User :</b> {self.tag} ( #ID{self.user_id} )
 ┠ <b>Message Link :</b> <a href='{self.message.link}'>Click Here</a>
 ┗ <b>Link:</b> <a href='{self.source_url}'>Click Here</a>
 """,
            )
        if (
            self.is_super_chat
            and (Config.INC_TASK_NOTIFY or Config.INC_TASK_RESUME)
            and Config.DATABASE_URL
        ):
            await database.add_incomplete_task(
                self.message.chat.id,
                self.message.link,
                self.tag,
                self.message.text or "",
                self.user_id,
                self.message.reply_to_message.id
                if self.message.reply_to_message
                else 0,
                self.dump_msg_id,
            )
    async def on_download_complete(self):
        # Every supported downloader backend (Aria2, qBittorrent, NZB,
        # JDownloader, Mega, Google Drive, Rclone, yt-dlp, Telegram and
        # direct HTTP) converges here. Keep opt-in post-download media tools
        # in this shared path so their behavior does not depend on the source.
        try:
            await self._on_download_complete()
        except Exception as err:
            LOGGER.error(f"Post-download failure: {err}", exc_info=True)
            await self.on_upload_error(f"Post-download failure: {err}")

    async def _on_download_complete(self):
        # The download callback already fires after completion; keep only a
        # short scheduling yield instead of delaying every upload by two seconds.
        await sleep(0.2)
        if self.is_cancelled:
            return
        if self.dump_msg_id and Config.DATABASE_URL:
            await database.update_task_dump_msg(
                self.message.link, self.dump_chat, self.dump_msg_id
            )
        multi_links = False
        if (
            self.folder_name
            and self.same_dir
            and self.mid in self.same_dir[self.folder_name]["tasks"]
        ):
            async with same_directory_lock:
                while True:
                    async with task_dict_lock:
                        if self.mid not in self.same_dir[self.folder_name]["tasks"]:
                            return
                        if (
                            self.same_dir[self.folder_name]["total"] <= 1
                            or len(self.same_dir[self.folder_name]["tasks"]) > 1
                        ):
                            if self.same_dir[self.folder_name]["total"] > 1:
                                self.same_dir[self.folder_name]["tasks"].remove(
                                    self.mid
                                )
                                self.same_dir[self.folder_name]["total"] -= 1
                                spath = f"{self.dir}{self.folder_name}"
                                des_id = list(self.same_dir[self.folder_name]["tasks"])[
                                    0
                                ]
                                des_path = f"{DOWNLOAD_DIR}{des_id}{self.folder_name}"
                                LOGGER.info(f"Moving files from {self.mid} to {des_id}")
                                await move_and_merge(spath, des_path, self.mid)
                                multi_links = True
                            break
                    await sleep(1)
        async with task_dict_lock:
            if self.is_cancelled:
                return
            if self.mid not in task_dict:
                return
            download = task_dict[self.mid]
            self.name = download.name()
            gid = download.gid()
        LOGGER.info(f"Download completed: {self.name}")

        if not (self.is_torrent or self.is_qbit):
            self.seed = False

        if multi_links:
            self.seed = False
            await self.on_upload_error(
                f"{self.name} Downloaded!\n\nWaiting for other tasks to finish..."
            )
            return
        elif self.same_dir:
            self.seed = False

        if self.folder_name:
            self.name = self.folder_name.strip("/").split("/", 1)[0]

        if not await aiopath.exists(f"{self.dir}/{self.name}"):
            try:
                files = await listdir(self.dir)
                self.name = files[-1]
                if self.name == "yt-dlp-thumb":
                    self.name = files[0]
            except Exception as e:
                await self.on_upload_error(str(e))
                return

        dl_path = f"{self.dir}/{self.name}"
        self.user_extract = bool(self.extract)
        self.is_file = await aiopath.isfile(dl_path)
        if self.join and not self.is_file:
            await join_files(dl_path)
            self.join = False
            self.is_file = await aiopath.isfile(dl_path)

        # Video + Video must use the same merge pipeline as -ht (Normal/Advanced),
        # not the standalone Vt2 executor. For archive inputs, extract first so
        # the existing merge pipeline sees the videos rather than the ZIP file.
        vt_mode = getattr(self, "vt_mode", None)
        vid_mode = getattr(self, "vidMode", None)
        selected_vt_mode = (vid_mode[0] if isinstance(vid_mode, (list, tuple)) and vid_mode else vt_mode)
        # User-level Audio Split is evaluated in the shared completion path,
        # so all downloader types receive the same opt-in behavior. An
        # explicit -vt choice wins; selecting Audio Split there still uses
        # the same saved Auto/Manual settings below.
        if (not getattr(self, "vt_requested", False)
                and bool(getattr(self, "user_dict", {}).get("AUDIO_SPLIT_ENABLED", False))):
            self.vt_requested = True
            self.vt_mode = "audio_split"
            self.vidMode = ["audio_split", "Audio Split (One Video per Audio Track)",
                            {"audio_split_mode": getattr(self, "user_dict", {}).get("AUDIO_SPLIT_MODE", "auto"), "audio_split_languages": getattr(self, "user_dict", {}).get("AUDIO_SPLIT_LANGUAGES", "")}]
            vid_mode = self.vidMode
            selected_vt_mode = "audio_split"
            LOGGER.info(
                "Automatic Audio Split enabled for completed task (mode=%s, languages=%s)",
                getattr(self, "user_dict", {}).get("AUDIO_SPLIT_MODE", "auto"),
                getattr(self, "user_dict", {}).get("AUDIO_SPLIT_LANGUAGES", ""),
            )
        vt_options = vid_mode[2] if isinstance(vid_mode, (list, tuple)) and len(vid_mode) > 2 and isinstance(vid_mode[2], dict) else {}
        if selected_vt_mode == "audio_split":
            vt_options.setdefault("audio_split_mode", getattr(self, "user_dict", {}).get("AUDIO_SPLIT_MODE", "auto"))
            vt_options.setdefault("audio_split_languages", getattr(self, "user_dict", {}).get("AUDIO_SPLIT_LANGUAGES", ""))
            if isinstance(self.vidMode, list) and len(self.vidMode) > 2:
                self.vidMode[2] = vt_options
        vt_multi_step = bool(vt_options.get('_multi_vt'))
        # The whole chain is selected up front in the -vt menu before the
        # download starts; this is the queue of steps still to run after the
        # current one, so the chain never needs to reopen the menu mid-task.
        self.vt_queue = list(vt_options.get('_multi_queue') or [])
        for idx_q, q_item in enumerate(self.vt_queue):
            if isinstance(q_item, (list, tuple)) and len(q_item) >= 3 and q_item[0] == "audio_split":
                q_kw = dict(q_item[2] or {})
                q_kw.setdefault("audio_split_mode", getattr(self, "user_dict", {}).get("AUDIO_SPLIT_MODE", "auto"))
                q_kw.setdefault("audio_split_languages", getattr(self, "user_dict", {}).get("AUDIO_SPLIT_LANGUAGES", ""))
                self.vt_queue[idx_q] = (q_item[0], q_item[1], q_kw)
        # Single-step Video + Video uses the same shared merge pipeline as -ht.
        # In MULTI VT it runs through the executor so the next tool can consume
        # the merged output instead of silently ending the chain after the merge.
        defer_vt_video_merge = bool(
            getattr(self, "vt_requested", False)
            and selected_vt_mode == "vid_vid"
            and not vt_multi_step
        )
        if defer_vt_video_merge:
            merge_mode_label = (vt_options.get('merge_mode') or getattr(self, 'merge_mode', None)
                                or self.user_dict.get('AUTO_MERGE_MODE', 'normal'))
            self.merge_mode = 'advanced' if str(merge_mode_label).lower() == 'advanced' else 'normal'
            self.vt_status_label = f"Video + Video ({self.merge_mode.title()})"
            archive_exts = (".zip", ".7z", ".rar", ".tar", ".gz", ".xz", ".bz2", ".001")
            if ospath.splitext(dl_path)[1].lower() in archive_exts:
                self.extract = True
            self.auto_merge = True
            self.manual_merge = False
            self.vidMode = None
            LOGGER.info(
                "Video + Video delegated to existing -ht merge flow (mode=%s); extract=%s",
                getattr(self, "merge_mode", None) or self.user_dict.get("AUTO_MERGE_MODE", "normal"),
                bool(self.extract),
            )
        # If Audio Split is the sole/final VT operation, defer its execution until
        # after archive extraction, symlinking (for seeding), excluded-file cleanup,
        # video merging, metadata injection, and auto-rename/name-swap so that
        # every task type (including unzip -e, zip -z, merge, metadata, rename) works.
        deferred_audio_split = None
        if getattr(self, "vt_requested", False) and getattr(self, "vidMode", None) and not defer_vt_video_merge:
            if selected_vt_mode == "audio_split" and not self.vt_queue:
                deferred_audio_split = list(self.vidMode)
                self.vidMode = None
            elif self.vt_queue and isinstance(self.vt_queue[-1], (list, tuple)) and self.vt_queue[-1][0] == "audio_split":
                last_step = self.vt_queue.pop(-1)
                deferred_audio_split = [last_step[0], last_step[1], dict(last_step[2] or {})]
                if not self.vt_queue and isinstance(self.vidMode, list) and len(self.vidMode) > 2 and isinstance(self.vidMode[2], dict):
                    self.vidMode[2]['_multi_vt'] = False

        # Other Video Tools operations use the Vt2 executor. MULTI VT is a
        # sequential chain whose steps were all picked upfront in the -vt
        # menu (self.vt_queue); each runs in order with no further menus.
        if (getattr(self, "vt_requested", False) and getattr(self, "vidMode", None)
                and not defer_vt_video_merge):
            try:
                from secrets import token_urlsafe
                from ..video_utils.executor import VidEcxecutor
                from ..telegram_helper.filters import CustomFilters
                gid = token_urlsafe(12)
                mode_name = getattr(self, "vt_mode", None) or self.vidMode[0]
                media_modes = {
                    "vid_vid", "vid_sub", "subsync", "compress", "convert", "watermark",
                    "remove_ads_names", "trim", "audio_swap", "audio_track",
                    "audio_split", "video_audio_sync", "audio_channel",
                    "subtitle_swap", "subtitle_rename", "rmstream", "rename",
                }
                if mode_name in media_modes:
                    try:
                        extracted_path = await self.proceed_extract(dl_path, gid)
                        if self.is_cancelled or not extracted_path:
                            return
                        if str(extracted_path) != str(dl_path) or self.extract:
                            dl_path = str(extracted_path)
                            self.extract = False
                            self.is_file = await aiopath.isfile(dl_path)
                            await self._set_name_from_path(dl_path, self.up_dir or self.dir)
                            self.size = await get_path_size(dl_path)
                            LOGGER.info("Video Tools input prepared from archive/folder (mode=%s, path=%s)", mode_name, dl_path)
                    except Exception:
                        LOGGER.error("Video Tools archive preparation failed for %s", mode_name, exc_info=True)
                        await self.on_upload_error("Video Tools could not extract the input archive. Check the archive and make sure extraction tools are installed.")
                        return
                privileged_vt_modes = {"compress", "convert", "watermark", "subsync", "video_audio_sync", "remove_ads_names"}
                is_privileged = await CustomFilters.sudo("", self.message) or await CustomFilters.authorized("", self.message)
                while self.vidMode:
                    mode_name, selected_name, kwargs = self.vidMode
                    # Keep the actual input basename for metadata-only Audio Track
                    # edits. A selector/menu label must never become the upload name.
                    source_basename = ospath.basename(dl_path) if mode_name == "audio_track" else None
                    if mode_name in privileged_vt_modes and not is_privileged:
                        LOGGER.warning(
                            "Blocked restricted Video Tool '%s' for non-sudo user %s",
                            mode_name, self.user_id,
                        )
                        await self.on_upload_error(
                            f"{mode_name.title()} is available only to the bot owner or sudo users."
                        )
                        return
                    kwargs = dict(kwargs or {})
                    multi_vt = bool(kwargs.pop('_multi_vt', False))
                    kwargs.pop('_multi_queue', None)
                    self.vt_mode = mode_name
                    self.vidMode = [mode_name, selected_name, kwargs]
                    LOGGER.info("Video Tools processing started: %s", mode_name or 'Rename')
                    result_path = await VidEcxecutor(self, dl_path, gid, False).execute()
                    if result_path and await aiopath.exists(str(result_path)):
                        dl_path = str(result_path)
                        if mode_name == "audio_track" and source_basename:
                            # Audio Track only changes audio metadata; preserve the
                            # source filename even if an old selector value leaked
                            # into the executor's output name.
                            result_dir = ospath.dirname(dl_path)
                            expected_path = ospath.join(result_dir, source_basename)
                            if ospath.basename(dl_path) != source_basename:
                                try:
                                    if not await aiopath.exists(expected_path):
                                        await sync_to_async(os_replace, dl_path, expected_path)
                                        dl_path = expected_path
                                except Exception:
                                    LOGGER.warning("Could not restore original Audio Track filename", exc_info=True)
                        self.name = ospath.basename(dl_path)
                    else:
                        await self.on_upload_error(f"Video tool failed: {mode_name or 'Rename'}")
                        return
                    LOGGER.info("Video Tools processing completed: %s", mode_name or 'Rename')
                    if not multi_vt:
                        break
                    # Preserve the current output as the input to the next tool.
                    self.link = dl_path
                    self.name = ospath.basename(dl_path)
                    if self.vt_queue:
                        # The rest of the chain was already picked by the user
                        # before the download started; consume the next step
                        # without reopening the menu.
                        next_mode, next_name, next_kwargs = self.vt_queue.pop(0)
                        next_kwargs = dict(next_kwargs or {})
                        next_kwargs['_multi_vt'] = bool(self.vt_queue)
                        self.vidMode = [next_mode, next_name, next_kwargs]
                        continue
                    self.vidMode = None
                    break
                self.vidMode = None
            except Exception as e:
                LOGGER.error("Video Tools processing failed", exc_info=True)
                await self.on_upload_error(f"Video Tools failed: {e}")
                return
        self.size = await get_path_size(dl_path)
        self.is_file = await aiopath.isfile(dl_path)

        if self.seed:
            up_dir = self.up_dir = f"{self.dir}10000"
            up_path = f"{self.up_dir}/{self.name}"
            await create_recursive_symlink(self.dir, self.up_dir)
            LOGGER.info(f"Shortcut created: {dl_path} -> {up_path}")
        else:
            up_dir = self.dir
            up_path = dl_path

        if not Config.QUEUE_ALL:
            async with queue_dict_lock:
                if self.mid in non_queued_dl:
                    non_queued_dl.remove(self.mid)
            await start_from_queued()

        if self.join and not self.is_file:
            await join_files(up_path)

        if (self.extract and not self.is_nzb) or deferred_audio_split is not None:
            up_path = await self.proceed_extract(up_path, gid)
            if self.is_cancelled or not up_path:
                return
            self.extract = False
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

        await remove_excluded_files(self.up_dir or self.dir, self.excluded_extensions)

        if getattr(self, "track_manager", False):
            from ..ext_utils.track_manager import proceed_track_manager
            up_path = await proceed_track_manager(self, up_path, gid)
            if self.is_cancelled or not up_path:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

        if getattr(self, "manual_reorder", False) or getattr(self, "manual_rm_stream", False) or getattr(self, "reorder_aud", None) or getattr(self, "reorder_sub", None):
            up_path = await self.proceed_reorder(up_path, gid)
            if self.is_cancelled or not up_path:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

        # Automatic Pipeline Order: Encode -> Compress -> Watermark -> Merge -> Track Manager
        from ..telegram_helper.filters import CustomFilters
        is_sudo_user = await CustomFilters.sudo("", self.message) or await CustomFilters.authorized("", self.message)

        enable_encode = Config.ENABLE_ENCODE and (self.user_dict.get("ENABLE_ENCODE") if "ENABLE_ENCODE" in self.user_dict else True) if is_sudo_user else False
        enable_compress = Config.ENABLE_COMPRESS and (self.user_dict.get("ENABLE_COMPRESS") if "ENABLE_COMPRESS" in self.user_dict else True) if is_sudo_user else False
        enable_watermark = Config.ENABLE_WATERMARK and (self.user_dict.get("ENABLE_WATERMARK") if "ENABLE_WATERMARK" in self.user_dict else True) if is_sudo_user else False

        if enable_encode:
            try:
                up_path = await self.proceed_encode(up_path, gid)
            except Exception as e:
                LOGGER.error(f"Encode processing error: {e}")
            if self.is_cancelled or not up_path:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

        if enable_compress:
            try:
                up_path = await self.proceed_compress_video(up_path, gid)
            except Exception as e:
                LOGGER.error(f"Compress processing error: {e}")
            if self.is_cancelled or not up_path:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

        if enable_watermark:
            try:
                up_path = await self.proceed_watermark(up_path, gid)
            except Exception as e:
                LOGGER.error(f"Watermark processing error: {e}")
            if self.is_cancelled or not up_path:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()
            await remove_excluded_files(up_dir, self.excluded_extensions)

        if getattr(self, "auto_merge", False) or getattr(self, "manual_merge", False):
            up_path = await self.proceed_merge(
                up_path, gid, getattr(self, "merge_custom_name", "")
            )
            if self.is_cancelled or not up_path:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

            self.clear()

        if getattr(self, "manual_trim", False):
            up_path = await self.proceed_trim(up_path, gid)
            if self.is_cancelled or not up_path:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

        if getattr(self, "manual_extract", False):
            up_path = await self.proceed_extract_content(up_path, gid)
            if self.is_cancelled or not up_path:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

        if self.ffmpeg_cmds:
            up_path = await self.proceed_ffmpeg(
                up_path,
                gid,
            )
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

        # Intro/Subtitle MUST run before metadata: apply_intro_subtitle overlays
        # the intro via a filter_complex ("[0:v:0][1:v:0]overlay=...[outv]"),
        # which produces a brand-new synthetic video stream that does not
        # inherit the original stream's per-stream metadata/disposition (only
        # "-map_metadata 0"/"-map_chapters 0" global-level carryover is done).
        # Running metadata application first meant its -metadata:s:v:0 title=
        # and thumbnail/disposition tags were written onto the pre-intro video
        # stream, then silently discarded the moment intro_sub rebuilt that
        # stream from scratch afterwards - so metadata and thumbnail looked
        # "configured" but never stuck on the final uploaded file. Doing the
        # intro/subtitle pass first and metadata last means metadata is always
        # the final, un-clobbered write onto the file that actually gets sent.
        if self.user_dict.get("INTRO_SUB_ENABLED", False) or getattr(
            Config, "INTRO_SUB_ENABLED", False
        ):
            try:
                from ..ext_utils.intro_sub_utils import apply_intro_subtitle

                up_path = await apply_intro_subtitle(self, up_path, gid)
                if self.is_cancelled:
                    return
                self.is_file = await aiopath.isfile(up_path)
                await self._set_name_from_path(up_path, up_dir)
                self.size = await get_path_size(up_dir)
                self.clear()
            except Exception as e:
                LOGGER.error(f"Intro Subtitle processing error: {e}", exc_info=True)

        has_meta_dicts = any(
            [
                (hasattr(self, "metadata_dict") and self.metadata_dict),
                (hasattr(self, "audio_metadata_dict") and self.audio_metadata_dict),
                (hasattr(self, "video_metadata_dict") and self.video_metadata_dict),
                (hasattr(self, "subtitle_metadata_dict") and self.subtitle_metadata_dict),
            ]
        )
        strip_meta = self.user_dict.get("STRIP_METADATA_ENABLE", False) or getattr(Config, "STRIP_METADATA_ENABLE", False)

        if has_meta_dicts or strip_meta:
            up_path = await apply_metadata_title(
                self,
                up_path,
                gid,
                getattr(self, "metadata_dict", {}),
                getattr(self, "audio_metadata_dict", {}),
                getattr(self, "video_metadata_dict", {}),
                getattr(self, "subtitle_metadata_dict", {}),
                strip_metadata=strip_meta,
            )
            if self.is_cancelled:
                return

            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_path)
            self.clear()

        if self.is_leech and self.is_file:
            fname = ospath.basename(up_path)
            self.file_details["filename"] = fname
            self.file_details["mime_type"] = (guess_type(fname))[
                0
            ] or "application/octet-stream"

        up_path = await self.auto_rename(up_path)
        if self.is_cancelled:
            return
        self.is_file = await aiopath.isfile(up_path)
        await self._set_name_from_path(up_path, up_dir)

        if self.name_swap:
            up_path = await self.substitute(up_path)
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)

        if self.screen_shots:
            up_path = await self.generate_screenshots(up_path)
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)

        if self.convert_audio or self.convert_video:
            up_path = await self.convert_media(
                up_path,
                gid,
            )
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

        if self.sample_video:
            up_path = await self.generate_sample_video(up_path, gid)
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            await self._set_name_from_path(up_path, up_dir)
            self.size = await get_path_size(up_dir)
            self.clear()

        if deferred_audio_split is not None:
            try:
                from secrets import token_urlsafe
                from ..video_utils.executor import VidEcxecutor
                as_gid = gid or token_urlsafe(12)
                as_mode, as_name, as_kwargs = deferred_audio_split
                as_kwargs = dict(as_kwargs or {})
                as_kwargs.pop("_multi_vt", None)
                as_kwargs.pop("_multi_queue", None)
                as_kwargs.setdefault("audio_split_mode", getattr(self, "user_dict", {}).get("AUDIO_SPLIT_MODE", "auto"))
                as_kwargs.setdefault("audio_split_languages", getattr(self, "user_dict", {}).get("AUDIO_SPLIT_LANGUAGES", ""))
                self.vt_mode = as_mode
                self.vidMode = [as_mode, as_name, as_kwargs]
                LOGGER.info("Audio Split processing started on prepared path: %s", up_path)
                as_result = await VidEcxecutor(self, up_path, as_gid, False).execute()
                self.vidMode = None
                if self.is_cancelled:
                    return
                if as_result and await aiopath.exists(str(as_result)):
                    up_path = str(as_result)
                    self.is_file = await aiopath.isfile(up_path)
                    await self._set_name_from_path(up_path, up_dir)
                    self.size = await get_path_size(up_dir)
                    self.clear()
                    # If Audio Split already created a .zip and -z was passed without a password,
                    # skip redundant re-zipping so we don't produce .zip.zip and allow proceed_split
                    # to split large >2GB archives if needed.
                    if (
                        self.is_file
                        and str(up_path).lower().endswith(".zip")
                        and self.compress
                        and not (isinstance(self.compress, str) and self.compress)
                    ):
                        self.compress = False
                else:
                    await self.on_upload_error("Video tool failed: audio_split")
                    return
            except Exception as e:
                LOGGER.error("Deferred Audio Split failed", exc_info=True)
                await self.on_upload_error(f"Audio Split failed: {e}")
                return

        if self.compress:
            if (
                await aiopath.isfile(str(up_path))
                and str(up_path).lower().endswith(".zip")
                and not (isinstance(self.compress, str) and self.compress)
            ):
                self.compress = False
            else:
                up_path = await self.proceed_compress(
                    up_path,
                    gid,
                )
                self.is_file = await aiopath.isfile(up_path)
                if self.is_cancelled:
                    return
                self.clear()

        await self._set_name_from_path(up_path, up_dir)
        self.size = await get_path_size(up_dir)

        if self.is_leech and not self.compress:
            await self.proceed_split(up_path, gid)
            if self.is_cancelled:
                return
            self.clear()

        self.subproc = None

        add_to_queue, event = await check_running_tasks(self, "up")
        await start_from_queued()
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Upload: {self.name}")
            async with task_dict_lock:
                task_dict[self.mid] = QueueStatus(self, gid, "Up")
            await event.wait()
            if self.is_cancelled:
                return
            LOGGER.info(f"Start from Queued/Upload: {self.name}")

        self.size = await get_path_size(up_dir)

        if self.is_yt:
            LOGGER.info(f"Up to yt Name: {self.name}")
            yt = YouTubeUpload(self, up_path)
            async with task_dict_lock:
                task_dict[self.mid] = YtStatus(self, yt, gid, "up")
            await gather(
                update_status_message(self.message.chat.id),
                sync_to_async(yt.upload),
            )
            del yt
        elif self.is_leech:
            LOGGER.info(f"Leech Name: {self.name}")
            tg = TelegramUploader(self, up_dir)
            async with task_dict_lock:
                task_dict[self.mid] = TelegramStatus(
                    self,
                    tg,
                    gid,
                    "up",
                    "hul" if Config.USE_HYPER and TgClient.helper_bots else "",
                )
            await gather(
                update_status_message(self.message.chat.id),
                tg.upload(),
            )
            del tg
        elif self.is_uphoster:
            LOGGER.info(f"Uphoster Upload Name: {self.name}")
            uphoster_service = self.user_dict.get("UPHOSTER_SERVICE", "gofile")
            services = uphoster_service.split(",")
            ddl = MultiUphosterUpload(
                self, up_path, services, self.folder_name.strip("/")
            )
            async with task_dict_lock:
                task_dict[self.mid] = UphosterStatus(self, ddl, gid, "up")
            await gather(
                update_status_message(self.message.chat.id),
                ddl.upload(),
            )
            del ddl
        elif is_gdrive_id(self.up_dest):
            LOGGER.info(f"Gdrive Upload Name: {self.name}")
            drive = GoogleDriveUpload(self, up_path)
            async with task_dict_lock:
                task_dict[self.mid] = GoogleDriveStatus(self, drive, gid, "up")
            await gather(
                update_status_message(self.message.chat.id),
                sync_to_async(drive.upload),
            )
            del drive
        elif self.up_dest == "mega:":
            LOGGER.info(f"Mega Upload Name: {self.name}")
            mega_email = self.user_dict.get("MEGA_EMAIL") or Config.MEGA_EMAIL or ""
            mega_password = self.user_dict.get("MEGA_PASSWORD") or Config.MEGA_PASSWORD or ""
            await add_mega_upload(self, up_path, mega_email, mega_password, gid)
        else:
            LOGGER.info(f"Rclone Upload Name: {self.name}")
            RCTransfer = RcloneTransferHelper(self)
            async with task_dict_lock:
                task_dict[self.mid] = RcloneStatus(self, RCTransfer, gid, "up")
            await gather(
                update_status_message(self.message.chat.id),
                RCTransfer.upload(up_path),
            )
            del RCTransfer
        return

    async def on_upload_complete(
        self, link, files, folders, mime_type, rclone_path="", dir_id=""
    ):
        if (
            self.is_super_chat
            and (Config.INC_TASK_NOTIFY or Config.INC_TASK_RESUME)
            and Config.DATABASE_URL
        ):
            await database.rm_complete_task(self.message.link)
        msg = (
            f"<b>📦 {escape(self.name)}</b>\n\n"
            f"<blockquote>• <b>Task Size:</b> {get_readable_file_size(self.size)}\n"
            f"• <b>Time Elapsed:</b> {get_readable_time(time() - self.message.date.timestamp())}\n"
            f"• <b>In Mode:</b> {self.mode[0]}\n"
            f"• <b>Out Mode:</b> {self.mode[1]}</blockquote>"
        )
        LOGGER.info(f"Task Done: {self.name}")
        if self.is_leech:
            msg += f"\n┠ <b>Total Files: </b>{folders}"
            if mime_type != 0:
                msg += f"\n┠ <b>Corrupted Files</b> → {mime_type}"
            msg += f"\n┖ <b>Task By</b> → {self.tag}\n\n"

            if self.is_super_chat:
                pmsg = msg
                pmsg += "〶 <b><u>Action Performed :</u></b>\n"
                pmsg += "⋗ <i>File(s) have been sent to User PM</i>\n\n"
                pm_btns = ButtonMaker()
                if TgClient.BNAME:
                    pm_btns.url_button(
                        "📁 View Files in Bot PM",
                        f"https://t.me/{TgClient.BNAME}",
                        style=ButtonStyle.PRIMARY,
                    )
                    await send_message(self.message, pmsg, pm_btns.build_menu(1))
                else:
                    await send_message(self.message, pmsg)

            log_chat = self.user_id
            if not files:
                await send_message(log_chat, msg)
            else:
                msg += "〶 <b><u>Files List :</u></b>\n"
                fmsg = ""
                for index, (link, name) in enumerate(files.items(), start=1):
                    fmsg += f"{index}. <a href='{link}'>{name}</a>"
                    if Config.MEDIA_STORE and (
                        self.is_super_chat or Config.LEECH_LOG_CHAT or self.up_dest
                    ):
                        parts = link.split("/")[-2:]
                        if len(parts) == 2:
                            chat_id, msg_id = parts
                            if chat_id.isdigit():
                                chat_id = f"-100{chat_id}"
                            flink = f"https://t.me/{TgClient.BNAME}?start={encode_slink('file' + chat_id + '&&' + msg_id)}"
                            fmsg += f"\n┠ <b>Get Media</b> → <a href='{flink}'>Store Link</a> | <a href='https://t.me/share/url?url={flink}'>Share Link</a>"
                            from ...modules.stream import gen_stream_link

                            slinks = await gen_stream_link(chat_id, msg_id)
                            if slinks:
                                fmsg += f"\n┖ <b>Direct</b> → <a href='{slinks[0]}'>Stream</a> | <a href='{slinks[1]}'>Download</a>"
                    fmsg += "\n"
                    if len(fmsg.encode() + msg.encode()) > 4000:
                        await send_message(log_chat, msg + fmsg)
                        await sleep(1)
                        fmsg = ""
                if fmsg != "":
                    await send_message(log_chat, msg + fmsg)
        else:
            msg += f"\n<blockquote>• <b>Type:</b> {mime_type}"
            if mime_type == "Folder":
                msg += f"\n• <b>Subfolders:</b> {folders}"
                msg += f"\n• <b>Files:</b> {files}"
            msg += "</blockquote>"

            multi_link_msg = ""
            multi_links = []
            if isinstance(link, dict) and not self.is_yt:
                for service, result in link.items():
                    if "error" in result:
                        multi_link_msg += (
                            f"{service.capitalize()}: Error - {result['error']}\n"
                        )
                    elif result.get("link"):
                        multi_links.append(
                            (f"{service.capitalize()} Link", result["link"])
                        )
                multi_link_msg = multi_link_msg.strip()
                link = None

            if (
                link
                or rclone_path
                and Config.RCLONE_SERVE_URL
                and not self.private_link
                or multi_links
            ):
                buttons = ButtonMaker()
                if link and Config.SHOW_CLOUD_LINK:
                    if "mega.nz" in link:
                        btn_label = "🔗 Mega Link"
                    else:
                        btn_label = "☁️ Cloud Link"
                    buttons.url_button(btn_label, link, style=ButtonStyle.PRIMARY)
                elif multi_links:
                    for name, url in multi_links:
                        buttons.url_button(name, url)
                else:
                    msg += f"\n\nPath: <code>{rclone_path}</code>"
                if rclone_path and Config.RCLONE_SERVE_URL and not self.private_link:
                    remote, rpath = rclone_path.split(":", 1)
                    url_path = rutils.quote(f"{rpath}")
                    share_url = f"{Config.RCLONE_SERVE_URL}/{remote}/{url_path}"
                    if mime_type == "Folder":
                        share_url += "/"
                    buttons.url_button(
                        "🔗 Rclone Link", share_url, style=ButtonStyle.PRIMARY
                    )
                if not rclone_path and dir_id:
                    INDEX_URL = self.user_dict.get("INDEX_URL", "") or ""
                    if not INDEX_URL:
                        INDEX_URL = Config.INDEX_URL or ""
                    if INDEX_URL and self.name:
                        safe_name = rutils.quote(self.name.strip("/"))
                        share_url = f"{INDEX_URL}/{safe_name}"
                        if mime_type == "Folder":
                            share_url += "/"
                        buttons.url_button(
                            "⚡ Index Link", share_url, style=ButtonStyle.PRIMARY
                        )
                        if mime_type.startswith(("image", "video", "audio")):
                            share_urls = f"{share_url}?a=view"
                            buttons.url_button(
                                "🌐 View Link", share_urls, style=ButtonStyle.PRIMARY
                            )
                button = buttons.build_menu(2)
            else:
                if not multi_link_msg and rclone_path:
                    msg += f"\n<blockquote>• <b>Path:</b> <code>{rclone_path}</code></blockquote>"
                button = None
            msg += f"\n\n<b>User:</b> {self.tag}"
            group_msg = (
                msg + "\n\n<b>✅ Action Performed:</b>\n"
                "<blockquote>Cloud link(s) sent to User PM.</blockquote>"
            )

            if multi_link_msg:
                group_msg += multi_link_msg + "\n"
                msg += multi_link_msg + "\n"

            if self.bot_pm and self.is_super_chat:
                await send_message(self.user_id, msg, button)

            if hasattr(Config, "MIRROR_LOG_ID") and Config.MIRROR_LOG_ID:
                await send_message(Config.MIRROR_LOG_ID, msg, button)

            await send_message(self.message, group_msg, button)
        if self.seed:
            await clean_target(self.up_dir)
            async with queue_dict_lock:
                if self.mid in non_queued_up:
                    non_queued_up.remove(self.mid)
            await start_from_queued()
            return

        if self.pm_msg and not Config.DELETE_LINKS:
            await delete_message(self.pm_msg)

        await delete_links(self.message)

        await clean_download(self.dir)
        if hasattr(self, "task_key") and self.task_key:
            await clear_task_state(self.task_key)
        async with task_dict_lock:
            if self.mid in task_dict:
                del task_dict[self.mid]
            count = len(task_dict)
        if count == 0:
            await self.clean()
        else:
            await update_status_message(self.message.chat.id)

        async with queue_dict_lock:
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)

        if hasattr(self, "done_event") and self.done_event and not self.done_event.done():
            self.done_event.set_result(True)

        await start_from_queued()

    async def on_download_error(self, error, button=None, is_limit=False):
        # During a supervised process restart, Telegram clients are intentionally
        # stopped. Do not retry or delete the task directory while Pyrogram is
        # still unwinding its .temp file; doing so races Pyrogram's own cleanup.
        if intervals.get("stopAll"):
            LOGGER.info(
                f"Skipping download retry/cleanup during bot restart: {self.name or self.link}"
            )
            return

        if (
            not self.is_cancelled
            and not is_limit
            and "Requested format is not available" not in str(error)
            and getattr(self, "_retry_count", 0) < 2
        ):
            self._retry_count = getattr(self, "_retry_count", 0) + 1
            LOGGER.warning(
                f"Download failed for {self.name or self.link}: {error}. Retrying attempt {self._retry_count}/2 silently..."
            )
            await clean_download(self.dir)
            if self.up_dir:
                await clean_download(self.up_dir)
            await sleep(2)
            bot_loop.create_task(self.new_event())
            return

        if hasattr(self, "task_key") and self.task_key:
            await clear_task_state(self.task_key)
        async with task_dict_lock:
            if self.mid in task_dict:
                del task_dict[self.mid]
            count = len(task_dict)
        await self.remove_from_same_dir()
        if magnet_id := getattr(self, "_alldebrid_magnet_id", 0) or 0:
            from ..mirror_leech_utils.download_utils.alldebrid_resolver import (
                delete_magnet,
            )

            await delete_magnet(magnet_id)
            self._alldebrid_magnet_id = 0
        msg = (
            f"<b>⚠️ Task Size Limit Exceeded</b>\n\n"
            f"<blockquote>• <b>Task Size:</b> {get_readable_file_size(self.size)}\n"
            f"• <b>In Mode:</b> {self.mode[0]}\n"
            f"• <b>Out Mode:</b> {self.mode[1]}\n\n"
            f"{error}</blockquote>"
            if is_limit
            else f"<b>🛑 Task Download Stopped</b>\n\n"
            f"<blockquote>• <b>Reason:</b> {escape(str(error))}\n"
            f"• <b>Task Size:</b> {get_readable_file_size(self.size)}\n"
            f"• <b>Time Elapsed:</b> {get_readable_time(time() - self.message.date.timestamp())}\n"
            f"• <b>In Mode:</b> {self.mode[0]}\n"
            f"• <b>Out Mode:</b> {self.mode[1]}\n"
            f"• <b>User:</b> {self.tag}</blockquote>"
        )

        await send_message(self.message, msg, button)
        await delete_links(self.message)
        if count == 0:
            await self.clean()
        else:
            await update_status_message(self.message.chat.id)

        if (
            self.is_super_chat
            and (Config.INC_TASK_NOTIFY or Config.INC_TASK_RESUME)
            and Config.DATABASE_URL
        ):
            await database.rm_complete_task(self.message.link)

        async with queue_dict_lock:
            if self.mid in queued_dl:
                queued_dl[self.mid].set()
                del queued_dl[self.mid]
            if self.mid in queued_up:
                queued_up[self.mid].set()
                del queued_up[self.mid]
            if self.mid in non_queued_dl:
                non_queued_dl.remove(self.mid)
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)

        if hasattr(self, "done_event") and self.done_event and not self.done_event.done():
            self.done_event.set_result(False)

        await start_from_queued()
        await sleep(3)
        await clean_download(self.dir)
        if self.up_dir:
            await clean_download(self.up_dir)
        if self.thumb and await aiopath.exists(self.thumb):
            user_perm_thumb = self.user_dict.get("THUMBNAIL") or f"thumbnails/{self.user_id}.jpg"
            if self.thumb != user_perm_thumb and self.thumb != f"thumbnails/{self.user_id}.jpg":
                await remove(self.thumb)

    async def on_upload_error(self, error):
        if hasattr(self, "task_key") and self.task_key:
            await clear_task_state(self.task_key)
        async with task_dict_lock:
            if self.mid in task_dict:
                del task_dict[self.mid]
            count = len(task_dict)
        await send_message(self.message, f"{self.tag} {escape(str(error))}")
        await delete_links(self.message)
        if count == 0:
            await self.clean()
        else:
            await update_status_message(self.message.chat.id)

        if (
            self.is_super_chat
            and (Config.INC_TASK_NOTIFY or Config.INC_TASK_RESUME)
            and Config.DATABASE_URL
        ):
            await database.rm_complete_task(self.message.link)

        async with queue_dict_lock:
            if self.mid in queued_dl:
                queued_dl[self.mid].set()
                del queued_dl[self.mid]
            if self.mid in queued_up:
                queued_up[self.mid].set()
                del queued_up[self.mid]
            if self.mid in non_queued_dl:
                non_queued_dl.remove(self.mid)
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)

        await start_from_queued()
        await sleep(3)
        await clean_download(self.dir)
        if self.up_dir:
            await clean_download(self.up_dir)
        if self.thumb and await aiopath.exists(self.thumb):
            user_perm_thumb = self.user_dict.get("THUMBNAIL") or f"thumbnails/{self.user_id}.jpg"
            if self.thumb != user_perm_thumb and self.thumb != f"thumbnails/{self.user_id}.jpg":
                await remove(self.thumb)
