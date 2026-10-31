from psutil import cpu_percent, virtual_memory, disk_usage
from time import time
from asyncio import gather, iscoroutinefunction

from pyrogram.enums import ChatType
from pyrogram.errors import QueryIdInvalid
from .. import user_data

from .. import (
    task_dict_lock,
    status_dict,
    task_dict,
    bot_start_time,
    intervals,
    sabnzbd_client,
    DOWNLOAD_DIR,
)
from ..core.config_manager import Config
from ..core.torrent_manager import TorrentManager
from ..core.jdownloader_booter import jdownloader
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import (
    EngineStatus,
    MirrorStatus,
    STATUSES,
    get_readable_file_size,
    get_readable_time,
    speed_string_to_bytes,
)
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.message_utils import (
    send_message,
    delete_message,
    clear_status_message,
    auto_delete_message,
    send_status_message,
    update_status_message,
    _edit_rich_status,
)
from ..helper.telegram_helper.button_build import ButtonMaker


@new_task
async def task_status(_, message):
    async with task_dict_lock:
        count = len(task_dict)
    if count == 0:
        # A final refresh may not have run yet; remove the matching stale status card.
        command_args = (message.text or "").split()
        is_private = message.chat and message.chat.type == ChatType.PRIVATE
        u_id = message.from_user.id if message.from_user else 0
        target_sid = message.chat.id
        if is_private:
            is_sudo = u_id == Config.OWNER_ID or (u_id in user_data and user_data[u_id].get("SUDO"))
            target_sid = (
                int(command_args[1])
                if len(command_args) > 1
                and command_args[1].lstrip("-").isdigit()
                and is_sudo
                else u_id or message.chat.id
            )
        elif len(command_args) > 1:
            if command_args[1] == "me":
                target_sid = u_id or message.chat.id
            elif command_args[1].lstrip("-").isdigit():
                target_sid = int(command_args[1])
        await clear_status_message(target_sid)

        currentTime = get_readable_time(time() - bot_start_time)
        free = get_readable_file_size(disk_usage(DOWNLOAD_DIR).free)
        msg = (
            "<b>📊 Active Bot Status</b>\n\n"
            "<i>No active tasks running.</i>\n\n"
            "<b>🖥 System</b>\n"
            f"⚡ CPU: <b>{cpu_percent()}%</b>\n"
            f"💾 Free disk: <b>{free}</b> ({round(100 - disk_usage(DOWNLOAD_DIR).percent, 1)}%)\n"
            f"🧠 RAM: <b>{virtual_memory().percent}%</b>\n"
            f"⏱️ Uptime: <b>{currentTime}</b>\n\n"
            f"Use <code>/{BotCommands.StatusCommand[0]} me</code> for personal tasks or "
            f"<code>/{BotCommands.StatusCommand[0]} user_id</code> for a specific user."
        )
        reply_message = await send_message(message, msg)
        await auto_delete_message(message, reply_message)
    else:
        text = message.text.split()
        is_private = message.chat and message.chat.type == ChatType.PRIVATE
        u_id = message.from_user.id if message.from_user else 0

        if is_private:
            is_sudo = u_id == Config.OWNER_ID or (u_id in user_data and user_data[u_id].get("SUDO"))
            if len(text) > 1 and text[1].lstrip("-").isdigit() and is_sudo:
                target_user_id = int(text[1])
            else:
                target_user_id = u_id
        else:
            if len(text) > 1:
                if text[1] == "me":
                    target_user_id = u_id
                elif text[1].lstrip("-").isdigit():
                    target_user_id = int(text[1])
                else:
                    target_user_id = 0
            else:
                target_user_id = 0
                sid = message.chat.id
                if obj := intervals["status"].get(sid):
                    obj.cancel()
                    del intervals["status"][sid]

        await send_status_message(message, target_user_id, force_new=True)
        await delete_message(message)


async def get_download_status(download):
    eng = download.engine
    speed = (
        download.speed()
        if eng.startswith(("WzPyro", "yt-dlp", "RClone", "Google-API"))
        else 0
    )
    return (
        (
            await download.status()
            if iscoroutinefunction(download.status)
            else download.status()
        ),
        speed,
        eng,
    )


@new_task
async def status_pages(_, query):
    data = (query.data or "").split()
    if len(data) < 3:
        try:
            await query.answer("Invalid status action", show_alert=True)
        except QueryIdInvalid:
            pass
        return
    try:
        key = int(data[1])
    except (ValueError, TypeError):
        try:
            await query.answer("Invalid status target", show_alert=True)
        except QueryIdInvalid:
            pass
        return
    if data[2] == "ref":
        await update_status_message(key, force=True)
    elif data[2] in ["nex", "pre"]:
        async with task_dict_lock:
            if key in status_dict:
                if data[2] == "nex":
                    status_dict[key]["page_no"] += status_dict[key]["page_step"]
                else:
                    status_dict[key]["page_no"] -= status_dict[key]["page_step"]
    elif data[2] == "ps":
        try:
            page_step = int(data[3])
        except (IndexError, ValueError, TypeError):
            page_step = 1
        page_step = min(30, max(1, page_step))
        async with task_dict_lock:
            if key in status_dict:
                status_dict[key]["page_step"] = page_step
    elif data[2] == "st":
        selected_status = STATUSES.get(data[3], "All") if len(data) > 3 else "All"
        async with task_dict_lock:
            if key in status_dict:
                status_dict[key]["status"] = selected_status
        await update_status_message(key, force=True)
    elif data[2] == "ov":
        message = query.message
        tasks = {
            "Download": 0,
            "Upload": 0,
            "Seed": 0,
            "Archive": 0,
            "Extract": 0,
            "Split": 0,
            "QueueDl": 0,
            "QueueUp": 0,
            "Clone": 0,
            "CheckUp": 0,
            "Pause": 0,
            "SamVid": 0,
            "ConvertMedia": 0,
            "Merge": 0,
            "Encode": 0,
            "Compress": 0,
            "Watermark": 0,
            "YouTubeUpload": 0,
            "Metadata": 0,
            "TrackManager": 0,
            "Seedr": 0,
            "Completed": 0,
            "FFmpeg": 0,
            "Other": 0,
        }
        dl_speed = 0
        up_speed = 0
        seed_speed = 0

        async with task_dict_lock:
            status_results = await gather(
                *(get_download_status(download) for download in task_dict.values())
            )

        eng_status = EngineStatus()
        if any(
            eng in (eng_status.STATUS_ARIA2, eng_status.STATUS_QBIT)
            for _, __, eng in status_results
        ):
            dl_speed, seed_speed = await TorrentManager.overall_speed()

        if any(eng == eng_status.STATUS_SABNZBD for _, __, eng in status_results):
            if not Config.DISABLE_NZB and sabnzbd_client.LOGGED_IN:
                dl_speed += (
                    int(
                        float(
                            (await sabnzbd_client.get_downloads())["queue"].get(
                                "kbpersec", "0"
                            )
                        )
                    )
                    * 1024
                )

        if any(eng == eng_status.STATUS_JD for _, __, eng in status_results):
            if not Config.DISABLE_JD and jdownloader.is_connected:
                dl_speed += (
                    await jdownloader.device.downloadcontroller.get_speed_in_bytes()
                )

        for status, speed, _ in status_results:
            match status:
                case MirrorStatus.STATUS_DOWNLOAD:
                    tasks["Download"] += 1
                    if speed:
                        dl_speed += speed_string_to_bytes(speed)
                case MirrorStatus.STATUS_UPLOAD:
                    tasks["Upload"] += 1
                    up_speed += speed_string_to_bytes(speed)
                case MirrorStatus.STATUS_SEED:
                    tasks["Seed"] += 1
                case MirrorStatus.STATUS_ARCHIVE:
                    tasks["Archive"] += 1
                case MirrorStatus.STATUS_EXTRACT:
                    tasks["Extract"] += 1
                case MirrorStatus.STATUS_SPLIT:
                    tasks["Split"] += 1
                case MirrorStatus.STATUS_QUEUEDL:
                    tasks["QueueDl"] += 1
                case MirrorStatus.STATUS_QUEUEUP:
                    tasks["QueueUp"] += 1
                case MirrorStatus.STATUS_CLONE:
                    tasks["Clone"] += 1
                case MirrorStatus.STATUS_CHECK:
                    tasks["CheckUp"] += 1
                case MirrorStatus.STATUS_PAUSED:
                    tasks["Pause"] += 1
                case MirrorStatus.STATUS_SAMVID:
                    tasks["SamVid"] += 1
                case MirrorStatus.STATUS_CONVERT:
                    tasks["ConvertMedia"] += 1
                case MirrorStatus.STATUS_MERGE:
                    tasks["Merge"] += 1
                case MirrorStatus.STATUS_ENCODE:
                    tasks["Encode"] += 1
                case MirrorStatus.STATUS_COMPRESS:
                    tasks["Compress"] += 1
                case MirrorStatus.STATUS_WATERMARK:
                    tasks["Watermark"] += 1
                case MirrorStatus.STATUS_YT:
                    tasks["YouTubeUpload"] += 1
                case MirrorStatus.STATUS_METADATA:
                    tasks["Metadata"] += 1
                case MirrorStatus.STATUS_TRACKMGR:
                    tasks["TrackManager"] += 1
                case MirrorStatus.STATUS_SEEDR:
                    tasks["Seedr"] += 1
                case MirrorStatus.STATUS_COMPLETED:
                    tasks["Completed"] += 1
                case MirrorStatus.STATUS_FFMPEG:
                    tasks["FFmpeg"] += 1
                case _:
                    # Unknown/new task statuses should not inflate the download count.
                    tasks["Other"] += 1

        total_tasks = sum(tasks.values())
        msg = (
            "<b>📊 System Tasks Overview</b>\n\n"
            f"📦 <b>Total Tasks:</b> {total_tasks}\n\n"
            "<b>Active work</b>\n"
            + "\n".join(f"• {k}: <b>{v}</b>" for k, v in tasks.items())
            + "\n\n"
            f"⚡ Download: <b>{get_readable_file_size(dl_speed)}/s</b>\n"
            f"⚡ Upload: <b>{get_readable_file_size(up_speed)}/s</b>\n"
            f"🌱 Seed: <b>{get_readable_file_size(seed_speed)}/s</b>"
        )
        button = ButtonMaker()
        button.data_button("◀️ Back", f"status {data[1]} ref")
        # Keep the overview on the same Rich Message editing path as task cards.
        # If the API requires a replacement message, save that new message so the
        # Back/Refresh callbacks continue editing the correct Telegram message.
        updated_message = await _edit_rich_status(message, msg, button.build_menu())
        if updated_message is not None and not isinstance(updated_message, str):
            updated_message.text = msg
            async with task_dict_lock:
                if key in status_dict:
                    status_dict[key]["message"] = updated_message
                    status_dict[key]["time"] = time()

    try:
        await query.answer()
    except QueryIdInvalid:
        pass
