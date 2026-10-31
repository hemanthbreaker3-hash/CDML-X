from asyncio import gather, iscoroutinefunction
from html import escape
from pyrogram.enums import ButtonStyle
from re import findall
from time import time

from psutil import cpu_percent, disk_usage, virtual_memory

from ... import (
    DOWNLOAD_DIR,
    bot_cache,
    bot_start_time,
    status_dict,
    task_dict,
    task_dict_lock,
)
from ...core.config_manager import Config
from ..telegram_helper.button_build import ButtonMaker

SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


class MirrorStatus:
    STATUS_UPLOAD = "Uploading"
    STATUS_DOWNLOAD = "Downloading"
    STATUS_CLONE = "Cloning"
    STATUS_QUEUEDL = "Queued DL"
    STATUS_QUEUEUP = "Queued UP"
    STATUS_PAUSED = "Paused"
    STATUS_ARCHIVE = "Archiving"
    STATUS_EXTRACT = "Extracting"
    STATUS_SPLIT = "Splitting"
    STATUS_CHECK = "Checking"
    STATUS_SEED = "Seeding"
    STATUS_SAMVID = "Sample Video"
    STATUS_CONVERT = "Converting"
    STATUS_MERGE = "Merging"
    STATUS_ENCODE = "Encoding"
    STATUS_COMPRESS = "Compressing"
    STATUS_WATERMARK = "Applying Watermark"
    STATUS_FFMPEG = "FFmpeg Processing"
    STATUS_YT = "YouTube Uploading"
    STATUS_METADATA = "Applying Metadata"
    STATUS_TRACKMGR = "Track Manager"
    STATUS_SEEDR = "Seedr Downloading"
    STATUS_COMPLETED = "Completed"


class EngineStatus:
    def __init__(self):
        ver = bot_cache.get("eng_versions", {})
        get_v = lambda k, d: ver.get(k) if ver.get(k) and ver.get(k) != "N/A" else d
        self.STATUS_ARIA2 = f"Aria2 v{get_v('aria2', '1.37.0')}"
        self.STATUS_AIOHTTP = f"AioHttp v{get_v('aiohttp', '3.14.3')}"
        self.STATUS_GDAPI = f"Google-API v{get_v('gapi', '2.201.0')}"
        self.STATUS_QBIT = f"qBit v{get_v('qBittorrent', '5.2.3')}"
        self.STATUS_TGRAM = f"wzgram v{get_v('wzgram', '3.1.5')}"
        self.STATUS_MEGA = f"MegaSDK v{get_v('mega', '10.20.20')}"
        self.STATUS_YTDLP = f"yt-dlp v{get_v('yt-dlp', '2026.08.19')}"
        self.STATUS_FFMPEG = f"ffmpeg v{get_v('ffmpeg', '9.0.2')}"
        self.STATUS_7Z = f"7z v{get_v('7z', '26.03')}"
        self.STATUS_RCLONE = f"RClone v{get_v('rclone', '1.75.1')}"
        self.STATUS_SABNZBD = f"SABnzbd+ v{get_v('SABnzbd+', '4.2.2')}"
        self.STATUS_QUEUE = "QSystem v2"
        self.STATUS_JD = "JDownloader v2"
        self.STATUS_YT = "Youtube-Api"
        self.STATUS_METADATA = "Metadata"
        self.STATUS_UPHOSTER = "Uphoster"
        self.STATUS_SEEDR = "Seedr"


STATUSES = {
    "ALL": "All",
    "DL": MirrorStatus.STATUS_DOWNLOAD,
    "UP": MirrorStatus.STATUS_UPLOAD,
    "QD": MirrorStatus.STATUS_QUEUEDL,
    "QU": MirrorStatus.STATUS_QUEUEUP,
    "AR": MirrorStatus.STATUS_ARCHIVE,
    "EX": MirrorStatus.STATUS_EXTRACT,
    "SD": MirrorStatus.STATUS_SEED,
    "CL": MirrorStatus.STATUS_CLONE,
    "CM": MirrorStatus.STATUS_CONVERT,
    "MG": MirrorStatus.STATUS_MERGE,
    "EN": MirrorStatus.STATUS_ENCODE,
    "CP": MirrorStatus.STATUS_COMPRESS,
    "WM": MirrorStatus.STATUS_WATERMARK,
    "YT": MirrorStatus.STATUS_YT,
    "MD": MirrorStatus.STATUS_METADATA,
    "TM": MirrorStatus.STATUS_TRACKMGR,
    "SR": MirrorStatus.STATUS_SEEDR,
    "DN": MirrorStatus.STATUS_COMPLETED,
    "SP": MirrorStatus.STATUS_SPLIT,
    "SV": MirrorStatus.STATUS_SAMVID,
    "FF": MirrorStatus.STATUS_FFMPEG,
    "PA": MirrorStatus.STATUS_PAUSED,
    "CK": MirrorStatus.STATUS_CHECK,
}


async def get_task_by_gid(gid: str):
    async with task_dict_lock:
        for tk in task_dict.values():
            if hasattr(tk, "seeding"):
                await tk.update()
            if tk.gid() == gid or tk.gid().startswith(gid):
                return tk
        return None


async def get_specific_tasks(status, user_id):
    if status == "All":
        if user_id:
            return [tk for tk in task_dict.values() if tk.listener.user_id == user_id]
        else:
            return list(task_dict.values())
    tasks_to_check = (
        [tk for tk in task_dict.values() if tk.listener.user_id == user_id]
        if user_id
        else list(task_dict.values())
    )
    coro_tasks = []
    coro_tasks.extend(tk for tk in tasks_to_check if iscoroutinefunction(tk.status))
    coro_statuses = await gather(*[tk.status() for tk in coro_tasks])
    result = []
    coro_index = 0
    for tk in tasks_to_check:
        if tk in coro_tasks:
            st = coro_statuses[coro_index]
            coro_index += 1
        else:
            st = tk.status()
        if (st == status) or (
            status == MirrorStatus.STATUS_DOWNLOAD and st not in STATUSES.values()
        ):
            result.append(tk)
    return result


async def get_all_tasks(req_status: str, user_id):
    async with task_dict_lock:
        return await get_specific_tasks(req_status, user_id)


def get_raw_file_size(size):
    num, unit = size.split()
    return int(float(num) * (1024 ** SIZE_UNITS.index(unit)))


def get_readable_file_size(size_in_bytes):
    if not size_in_bytes:
        return "0B"
    if size_in_bytes < 0:
        return "Unknown"

    index = 0
    while size_in_bytes >= 1024 and index < len(SIZE_UNITS) - 1:
        size_in_bytes /= 1024
        index += 1

    return f"{size_in_bytes:.2f}{SIZE_UNITS[index]}"


def get_readable_time(seconds: int):
    periods = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    result = ""
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result += f"{int(period_value)}{period_name}"
    return result or "0s"


def get_raw_time(time_str: str) -> int:
    time_units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(
        int(value) * time_units[unit]
        for value, unit in findall(r"(\d+)([dhms])", time_str)
    )


def time_to_seconds(time_duration):
    try:
        parts = time_duration.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = map(float, parts)
        elif len(parts) == 2:
            hours = 0
            minutes, seconds = map(float, parts)
        elif len(parts) == 1:
            hours = 0
            minutes = 0
            seconds = float(parts[0])
        else:
            return 0
        return hours * 3600 + minutes * 60 + seconds
    except Exception:
        return 0


def speed_string_to_bytes(size_text: str):
    size = 0
    size_text = size_text.lower()
    if "k" in size_text:
        size += float(size_text.split("k")[0]) * 1024
    elif "m" in size_text:
        size += float(size_text.split("m")[0]) * 1048576
    elif "g" in size_text:
        size += float(size_text.split("g")[0]) * 1073741824
    elif "t" in size_text:
        size += float(size_text.split("t")[0]) * 1099511627776
    elif "b" in size_text:
        size += float(size_text.split("b")[0])
    return size


BAR_LENGTH = 12  # matches the "12 blocks" documented for PROGRESS_BAR in bot settings
UPDATE_INTERVAL = 2.0


def _progress_percent(percent) -> float:
    """Normalize progress inputs and safely clamp them to 0–100."""
    try:
        value = float(str(percent).strip().removesuffix("%"))
    except (TypeError, ValueError):
        value = 0.0
    if not (value == value and value not in (float("inf"), float("-inf"))):
        value = 0.0
    return max(0.0, min(100.0, value))


def progress_bar(percent: float, task_key=None) -> str:
    """Render exactly one flat, single-line progress bar for every task,
    using the user-configured PROGRESS_BAR style (filled char + empty char),
    wrapped in brackets, e.g. [■■□□□□□□□□□□]. Never returns multiple
    lines/frames - just the bar's current state."""
    value = _progress_percent(percent)
    # Resolve Config locally as well as at module scope. This keeps status
    # rendering resilient to partial imports/reloads in long-running workers.
    config_cls = globals().get("Config")
    if config_cls is None:
        try:
            from ...core.config_manager import Config as config_cls
        except Exception:
            config_cls = None
    style = str(getattr(config_cls, "PROGRESS_BAR", "■□") or "■□")
    filled_char = style[0] if style else "■"
    empty_char = style[1] if len(style) > 1 else "□"
    filled = min(BAR_LENGTH, int(value * BAR_LENGTH / 100))
    bar = filled_char * filled + empty_char * (BAR_LENGTH - filled)
    return f"[{bar}]"


def format_progress(
    title: str,
    percent: float,
    speed: str = "Calculating...",
    filename: str = "Unknown",
) -> str:
    percent = _progress_percent(percent)
    return (
        f"<b>✨ {escape(str(title))}</b>\n"
        f"<blockquote>📊 <b>Progress</b>  <code>{progress_bar(percent)}</code> <b>{percent:.1f}%</b>\n"
        f"⚡ <b>Speed</b>  {escape(str(speed))}\n"
        f"📄 <b>File</b>  <code>{escape(str(filename))}</code></blockquote>"
    )


def bar_wave(pct, width=BAR_LENGTH):
    """Backward-compatible alias; all aliases render the same progress style."""
    return progress_bar(pct)


def get_progress_bar_string(pct):
    """Backward-compatible stats helper using the same universal (bracketed) progress bar."""
    return progress_bar(pct)


def _task_progress_key(task):
    """Return a stable identifier for selecting a task's visual bar style."""
    try:
        gid = task.gid()
        if gid:
            return str(gid)
    except Exception:
        pass
    try:
        listener = task.listener
        message = listener.message
        return f"{message.chat.id}:{message.id}:{listener.user_id}"
    except Exception:
        return f"{type(task).__name__}:{id(task)}"


async def get_readable_message(sid, is_user, page_no=1, status="All", page_step=1):
    msg = ""
    button = None

    tasks = await get_specific_tasks(status, sid if is_user else None)

    STATUS_LIMIT = max(1, int(Config.STATUS_LIMIT or 1))
    tasks_no = len(tasks)
    pages = (max(tasks_no, 1) + STATUS_LIMIT - 1) // STATUS_LIMIT
    if page_no > pages:
        page_no = (page_no - 1) % pages + 1
        status_dict[sid]["page_no"] = page_no
    elif page_no < 1:
        page_no = pages - (abs(page_no) % pages)
        status_dict[sid]["page_no"] = page_no
    start_position = (page_no - 1) * STATUS_LIMIT

    for index, task in enumerate(
        tasks[start_position : STATUS_LIMIT + start_position], start=1
    ):
        if status != "All":
            tstatus = status
        elif iscoroutinefunction(task.status):
            tstatus = await task.status()
        else:
            tstatus = task.status()
        msg += f"<b>📦 {index + start_position}. {escape(str(task.name()))}</b>\n\n"
        if task.listener.subname:
            msg += f"┖ <b>Sub Name:</b> <i>{escape(str(task.listener.subname))}</i>\n"
        source_message = task.listener.message
        source_date = getattr(source_message, "date", None)
        elapsed = time() - source_date.timestamp() if source_date else 0
        source_user = getattr(source_message, "from_user", None)
        if source_user:
            source_user_id = getattr(source_user, "id", task.listener.user_id)
            source_user_name = escape(str(getattr(source_user, "first_name", "User")))
            user_mention = f'<a href="tg://user?id={source_user_id}">{source_user_name}</a>'
            msg += f"\n👤 <b>User:</b> {user_mention} <i>(#ID{source_user_id})</i>"
        else:
            msg += f"\n👤 <b>User:</b> #{escape(str(task.listener.user_id))}"
        source_link = getattr(source_message, "link", None)
        if task.listener.is_super_chat and source_link and str(source_link).startswith(("https://", "http://")):
            msg += f' <i>· <a href="{escape(str(source_link), quote=True)}">Source</a></i>'

        if tstatus not in [MirrorStatus.STATUS_SEED, MirrorStatus.STATUS_QUEUEUP] and task.listener.progress:
            progress = task.progress()
            msg += f"\n<blockquote>📊 <b>Progress:</b> <code>{progress_bar(progress)}</code> <b>{_progress_percent(progress):.1f}%</b>"
            if task.listener.subname:
                subsize = f" / {get_readable_file_size(task.listener.subsize)}"
                total_files = len(task.listener.files_to_proceed)
                count = f"{task.listener.proceed_count}/{total_files or '?'}"
            else:
                subsize = ""
                count = ""
            msg += f"\n┠ <b>Processed:</b> {escape(str(task.processed_bytes()))}{escape(str(subsize))} of {escape(str(task.size()))}"
            if count:
                msg += f"\n┠ <b>Files:</b> {escape(str(count))}"
            msg += f"\n┠ <b>Status:</b> {escape(str(tstatus))}"
            msg += f"\n┠ <b>Speed:</b> {escape(str(task.speed()))}"
            msg += f"\n┠ <b>ETA:</b> {escape(str(task.eta()))}"
            msg += f"\n┠ <b>Elapsed:</b> {escape(str(get_readable_time(elapsed)))}"
            if tstatus == MirrorStatus.STATUS_DOWNLOAD and (task.listener.is_torrent or task.listener.is_qbit):
                try:
                    msg += f"\n┠ <b>Seeders:</b> {task.seeders_num()}"
                    msg += f"\n┠ <b>Leechers:</b> {task.leechers_num()}"
                except Exception:
                    pass
        elif tstatus == MirrorStatus.STATUS_SEED:
            msg += f"\n<blockquote>┠ <b>Size:</b> {escape(str(task.size()))}"
            msg += f"\n┠ <b>Uploaded:</b> {escape(str(task.uploaded_bytes()))}"
            msg += f"\n┠ <b>Status:</b> {escape(str(tstatus))}"
            msg += f"\n┠ <b>Speed:</b> {escape(str(task.seed_speed()))}"
            msg += f"\n┠ <b>Ratio:</b> {escape(str(task.ratio()))}"
            msg += f"\n┠ <b>Seeding Time:</b> {escape(str(task.seeding_time()))}"
            msg += f"\n┠ <b>Elapsed:</b> {escape(str(get_readable_time(elapsed)))}"
        else:
            msg += f"\n<blockquote>┠ <b>Size:</b> {escape(str(task.size()))}"
        msg += f"\n┠ <b>Engine:</b> {escape(str(task.engine))}"
        in_mode = str(task.listener.mode[0]).lstrip("#")
        out_mode = str(task.listener.mode[1]).lstrip("#")
        msg += f"\n┠ <b>Mode:</b> #{escape(in_mode)} ➜ #{escape(out_mode)}"

        from ..telegram_helper.bot_commands import BotCommands

        if tstatus in [MirrorStatus.STATUS_DOWNLOAD, MirrorStatus.STATUS_PAUSED, MirrorStatus.STATUS_QUEUEDL]:
            if task.listener.is_torrent or task.listener.is_qbit or task.listener.is_nzb:
                select_command = f"/{BotCommands.SelectCommand[1]}{str(Config.CMD_SUFFIX or '')}_{task.gid()[:8]}"
                msg += f"\n┠ <b>Select:</b> {select_command}"

        # Keep command text in the normal Telegram font instead of monospace/code styling.
        cancel_command = f"/{BotCommands.CancelTaskCommand[1]}{str(Config.CMD_SUFFIX or '')}_{task.gid()[:8]}"
        msg += f"\n┖ <b>Cancel:</b> {cancel_command}</blockquote>\n"

    if len(msg) == 0:
        if status == "All":
            return None, None
        else:
            msg = f"No Active {status} Tasks!\n"

    msg += f"\n<b>━━━━━━━━━━━━━━━━━━</b>\n<b>📊 Bot Status Summary</b> 📦 <b>Active Tasks:</b> {tasks_no}"
    buttons = ButtonMaker()
    if not is_user:
        buttons.data_button(
            "📜 TStats",
            f"status {sid} ov",
            position="header",
            style=ButtonStyle.PRIMARY,
        )
    if len(tasks) > STATUS_LIMIT:
        msg += f"<b>Page:</b> {page_no}/{pages} | <b>Tasks:</b> {tasks_no} | <b>Step:</b> {page_step}\n"
        buttons.data_button("<<", f"status {sid} pre", position="header")
        buttons.data_button(">>", f"status {sid} nex", position="header")
        if tasks_no > 30:
            for i in [1, 2, 4, 6, 8, 10, 15]:
                buttons.data_button(i, f"status {sid} ps {i}", position="footer")
    if status != "All" or tasks_no > 20:
        for label, status_value in list(STATUSES.items()):
            if status_value != status:
                # Send the stable status key: the human-readable status can contain spaces.
                buttons.data_button(label, f"status {sid} st {label}")
    buttons.data_button(
        "♻️ Refresh", f"status {sid} ref", position="header", style=ButtonStyle.PRIMARY
    )
    button = buttons.build_menu(8)
    msg += f" | ⚡ <b>CPU:</b> {cpu_percent()}% | 💾 <b>Free Disk:</b> {get_readable_file_size(disk_usage(DOWNLOAD_DIR).free)} ({round(100 - disk_usage(DOWNLOAD_DIR).percent, 1)}%) | 🧠 <b>RAM:</b> {virtual_memory().percent}% | ⏱️ <b>Uptime:</b> {get_readable_time(time() - bot_start_time)}"
    return msg, button
