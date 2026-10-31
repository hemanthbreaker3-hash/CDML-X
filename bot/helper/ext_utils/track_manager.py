from asyncio import wait_for, gather, TimeoutError as AsyncTimeoutError
from functools import partial
import re
from html import escape
from re import sub
from contextlib import suppress
from os import path as ospath, walk
from asyncio.subprocess import PIPE
from asyncio import create_subprocess_exec

from aiofiles.os import remove, path as aiopath
from aioshutil import move
from pyrogram import filters
from pyrogram.types import CallbackQuery, Message
from pyrogram.handlers import MessageHandler

from ... import LOGGER, bot_loop, task_dict, task_dict_lock
from ...core.config_manager import BinConfig
from ..telegram_helper.button_build import ButtonMaker
from .bot_utils import new_task, sync_to_async
from .media_utils import FFMpeg, FFProgress, get_document_type
from ..mirror_leech_utils.status_utils.ffmpeg_status import FFmpegStatus
from ..telegram_helper.message_utils import delete_message, edit_message, send_message, update_status_message

track_manager_sessions = {}


class _TrackManagerProgress(FFProgress):
    """Use the shared rich FFmpeg progress reader and retain error diagnostics."""

    def __init__(self):
        super().__init__()
        self.stderr_tail = []

    async def readlines(self, stream):
        async for line in super().readlines(stream):
            if line:
                self.stderr_tail.append(line.decode("utf-8", errors="replace"))
                self.stderr_tail = self.stderr_tail[-40:]
            yield line


def _disposition_flags(track):
    """Return enabled FFmpeg disposition names for a track."""
    disposition = track.get("disposition") or {}
    return [name for name, enabled in disposition.items() if bool(enabled)]


def _set_track_flag(track, flag, enabled):
    disposition = track.setdefault("disposition", {})
    disposition[flag] = 1 if enabled else 0
    track[flag] = bool(enabled)
    track["disposition_changed"] = True


def get_short_lang(stream):
    tags = stream.get("tags") or {}
    lang = tags.get("language") or ""
    title = tags.get("title") or ""

    code = lang.strip() or title.strip() or "und"
    code_lower = code.lower()

    mapping = {
        "tel": "Tel", "te": "Tel", "telugu": "Tel",
        "tam": "Tam", "ta": "Tam", "tamil": "Tam",
        "hin": "Hin", "hi": "Hin", "hindi": "Hin",
        "eng": "Eng", "en": "Eng", "english": "Eng",
        "kan": "Kan", "kn": "Kan", "kannada": "Kan",
        "mal": "Mal", "ml": "Mal", "malayalam": "Mal",
        "mar": "Mar", "mr": "Mar", "marathi": "Mar",
        "ben": "Ben", "bn": "Ben", "bengali": "Ben",
        "pan": "Pan", "pa": "Pan", "punjabi": "Pan",
        "guj": "Guj", "gu": "Guj", "gujarati": "Guj",
        "ori": "Ori", "or": "Ori", "odia": "Ori",
        "jpn": "Jpn", "ja": "Jpn", "japanese": "Jpn", "jap": "Jpn",
        "kor": "Kor", "ko": "Kor", "korean": "Kor",
        "chi": "Chi", "zh": "Chi", "zho": "Chi", "chinese": "Chi",
        "spa": "Spa", "es": "Spa", "spanish": "Spa",
        "fre": "Fre", "fra": "Fre", "fr": "Fre", "french": "Fre",
        "ger": "Ger", "deu": "Ger", "de": "Ger", "german": "Ger",
        "rus": "Rus", "ru": "Rus", "russian": "Rus",
        "ita": "Ita", "it": "Ita", "italian": "Ita",
        "und": "Und", "unk": "Und", "unknown": "Und",
    }

    if code_lower in mapping:
        return mapping[code_lower]

    for k, v in mapping.items():
        if k in code_lower:
            return v

    with suppress(Exception):
        from langcodes import Language
        l_obj = Language.get(code)
        if l_obj and l_obj.is_valid():
            autonym = l_obj.autonym() or l_obj.display_name()
            if autonym:
                return autonym[:3].capitalize()

    clean_code = sub(r"[^a-zA-Z]", "", code)
    if clean_code:
        return clean_code[:3].capitalize()
    return "Und"


def _track_display(track):
    lang = str(track.get("short_lang") or "Und")
    title = str(track.get("title") or "").strip()
    label = f"{lang} — {title}" if title else lang
    flags = []
    if track.get("default"):
        flags.append("Default")
    if track.get("forced"):
        flags.append("Forced")
    return f"{label} ({', '.join(flags)})" if flags else label


def format_tm_ui(session):
    mid = session["mid"]
    files = session["files"]
    view_mode = session["view_mode"]
    page = session["page"]
    page_size = session.get("page_size", 5)
    cur_idx = session.get("current_file_idx", 0)
    is_multi = session["is_multi"]

    total_files = len(files)
    total_pages = max(1, (total_files + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    session["page"] = page

    buttons = ButtonMaker()

    if view_mode == "list":
        start_i = (page - 1) * page_size
        end_i = min(start_i + page_size, total_files)
        page_files = files[start_i:end_i]

        lines = [
            "<b>📂 Track Manager - Files List</b>\n",
            f"• <b>Total Files:</b> {total_files}",
            f"• <b>Page:</b> {page}/{total_pages}\n",
            "<b>Track Summary:</b>",
        ]

        for i, f in enumerate(page_files, start=start_i + 1):
            aud_str_list = []
            for pos in f["audio_order"]:
                if pos in f["selected_audio"]:
                    lang = _track_display(f["audio_tracks"][pos])
                    aud_str_list.append(lang)
            aud_str = ", ".join(aud_str_list) if aud_str_list else "None"

            sub_str_list = []
            for pos in f["sub_order"]:
                if pos in f["selected_sub"]:
                    lang = _track_display(f["sub_tracks"][pos])
                    sub_str_list.append(lang)
            sub_str = ", ".join(sub_str_list) if sub_str_list else "None"

            lines.append(f"{i}. <code>{escape(f['name'])}</code>")
            lines.append(f"   ↳ Audio: <b>{escape(aud_str)}</b>")
            lines.append(f"   ↳ Subtitle: <b>{escape(sub_str)}</b>")

        caption = "\n".join(lines)

        # Pagination in header (h_cols=3)
        if total_pages > 1:
            prev_p = page - 1 if page > 1 else total_pages
            next_p = page + 1 if page < total_pages else 1
            buttons.data_button("◀️ Prev", f"tmcb page {mid} {prev_p}", position="header")
            buttons.data_button(f"Page {page}/{total_pages}", "tmcb dummy", position="header")
            buttons.data_button("Next ▶️", f"tmcb page {mid} {next_p}", position="header")

        # Main controls in default (b_cols=1)
        buttons.data_button("🔄 Apply to All", f"tmcb apply_all {mid}", position="default")

        # Last row in footer (f_cols=3): Cancel (left), Select File (middle), Done (right)
        buttons.data_button("❌ Cancel", f"tmcb cancel {mid}", position="footer")
        buttons.data_button("📁 Select File", f"tmcb select_file {mid}", position="footer")
        buttons.data_button("✅ Done", f"tmcb done {mid}", position="footer")

        return caption, buttons.build_menu(b_cols=1, h_cols=3, f_cols=3)

    elif view_mode == "select_file":
        start_i = (page - 1) * page_size
        end_i = min(start_i + page_size, total_files)
        page_files = files[start_i:end_i]

        caption = f"<b>📂 Select a File to Edit Tracks:</b>\n[Page {page}/{total_pages}]"

        for i, f in enumerate(page_files, start=start_i):
            buttons.data_button(f"{i + 1}. {f['name'][:30]}", f"tmcb file {mid} {i}", position="default")

        if total_pages > 1:
            prev_p = page - 1 if page > 1 else total_pages
            next_p = page + 1 if page < total_pages else 1
            buttons.data_button("◀️ Prev", f"tmcb page {mid} {prev_p}", position="header")
            buttons.data_button(f"Page {page}/{total_pages}", "tmcb dummy", position="header")
            buttons.data_button("Next ▶️", f"tmcb page {mid} {next_p}", position="header")

        buttons.data_button("◀️ Back", f"tmcb back {mid}", position="footer")
        return caption, buttons.build_menu(b_cols=1, h_cols=3, f_cols=1)

    elif view_mode in ("audio", "sub"):
        cur_file = files[cur_idx] if 0 <= cur_idx < len(files) else files[0]
        fname = cur_file["name"]

        is_aud = (view_mode == "audio")
        tracks = cur_file["audio_tracks"] if is_aud else cur_file["sub_tracks"]
        order = cur_file["audio_order"] if is_aud else cur_file["sub_order"]
        selected = cur_file["selected_audio"] if is_aud else cur_file["selected_sub"]

        mode_title = "🎵 Audio Track Selection" if is_aud else "💬 Subtitle Track Selection"

        lines = [
            f"<b>{mode_title}</b>\n",
            f"• <b>File:</b> <code>{escape(fname)}</code>\n",
            "<b>Available Tracks:</b>",
        ]

        if not tracks:
            lines.append("<i>No tracks available</i>")
        else:
            for display_pos, pos in enumerate(order, start=1):
                t = tracks[pos]
                status = "✓" if pos in selected else "✗"
                lang_disp = escape(str(t.get("short_lang") or "Und"))
                title_disp = escape(str(t.get("title") or "").strip())
                title_text = f" — <i>{title_disp}</i>" if title_disp else ""
                language_code = escape(str(t.get("full_lang") or "und"))
                flags = []
                if t.get("default"):
                    flags.append("⭐ Default")
                if not is_aud and t.get("forced"):
                    flags.append("🚩 Forced")
                flag_text = f" · <b>{' / '.join(flags)}</b>" if flags else ""
                lines.append(f"{display_pos}. <b>{lang_disp}</b> [{status}] · <code>{language_code}</code>{title_text}{flag_text}")

        caption = "\n".join(lines)

        # First column: Available tracks with short language names (b_cols=1)
        for display_pos, pos in enumerate(order, start=1):
            t = tracks[pos]
            status = "✓" if pos in selected else "✗"
            btn_label = f"{display_pos}. {t['short_lang']} [{status}]"
            toggle_action = "toggle_aud" if is_aud else "toggle_sub"
            edit_action = "edit_aud" if is_aud else "edit_sub"
            buttons.data_button(btn_label, f"tmcb {toggle_action} {mid} {pos}", position="default")
            buttons.data_button(f"✏️ Edit #{display_pos}", f"tmcb {edit_action} {mid} {pos}", position="f_body")
            default_icon = "⭐" if t.get("default") else "☆"
            default_action = "toggle_default_aud" if is_aud else "toggle_default_sub"
            buttons.data_button(f"{default_icon} Default #{display_pos}", f"tmcb {default_action} {mid} {pos}", position="f_body")
            if not is_aud:
                forced_icon = "🚩" if t.get("forced") else "⚑"
                buttons.data_button(f"{forced_icon} Forced #{display_pos}", f"tmcb toggle_forced_sub {mid} {pos}", position="f_body")

        # Up and Down reorder buttons below track list in f_body (fb_cols=2)
        if len(order) > 1:
            reorder_action = "move_aud" if is_aud else "move_sub"
            for display_pos in range(len(order)):
                up_cb = f"tmcb {reorder_action} {mid} {display_pos} -1" if display_pos > 0 else "tmcb dummy"
                dn_cb = f"tmcb {reorder_action} {mid} {display_pos} 1" if display_pos < len(order) - 1 else "tmcb dummy"
                buttons.data_button(f"#{display_pos + 1} ⬆️", up_cb, position="f_body")
                buttons.data_button(f"#{display_pos + 1} ⬇️", dn_cb, position="f_body")

        # Footer controls: bulk selection, switch track type, and multi-file actions.
        bulk_all = "select_aud_all" if is_aud else "select_sub_all"
        bulk_none = "select_aud_none" if is_aud else "select_sub_none"
        buttons.data_button("☑️ Select All", f"tmcb {bulk_all} {mid}", position="footer")
        buttons.data_button("◻️ Select None", f"tmcb {bulk_none} {mid}", position="footer")
        if is_aud:
            buttons.data_button("💬 Subtitles", f"tmcb view {mid} sub", position="footer")
        else:
            buttons.data_button("🎵 Audio", f"tmcb view {mid} audio", position="footer")

        if is_multi:
            buttons.data_button("🔄 Apply to All", f"tmcb apply_all {mid}", position="footer")
            buttons.data_button("◀️ Back", f"tmcb back {mid}", position="footer")

        buttons.data_button("✅ Done", f"tmcb done {mid}", position="footer")

        footer_cols = 3
        return caption, buttons.build_menu(b_cols=1, fb_cols=2, f_cols=footer_cols)


@new_task
async def tm_callback(client, query: CallbackQuery):
    data = query.data.split()
    cmd = data[1] if len(data) > 1 else ""

    if cmd == "dummy":
        return await query.answer()

    mid = int(data[2]) if len(data) > 2 and data[2].isdigit() else 0
    session = track_manager_sessions.get(mid)

    if not session:
        return await query.answer("Track Manager session expired!", show_alert=True)

    if query.from_user.id != session["user_id"]:
        return await query.answer("This menu is not for you!", show_alert=True)

    files = session["files"]
    cur_idx = session.get("current_file_idx", 0)

    if cmd == "page":
        target_page = int(data[3])
        session["page"] = target_page
        await query.answer(f"Page {target_page}")
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)

    elif cmd == "select_file":
        session["view_mode"] = "select_file"
        await query.answer()
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)

    elif cmd == "file":
        target_f_idx = int(data[3])
        session["current_file_idx"] = target_f_idx
        session["view_mode"] = "audio"
        await query.answer(f"Editing file #{target_f_idx + 1}")
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)

    elif cmd == "view":
        target_view = data[3]
        session["view_mode"] = target_view
        await query.answer()
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)

    elif cmd in ("edit_aud", "edit_sub"):
        track_pos = int(data[3])
        cur_file = files[cur_idx] if 0 <= cur_idx < len(files) else files[0]
        kind = "audio" if cmd == "edit_aud" else "subtitle"
        tracks = cur_file["audio_tracks"] if kind == "audio" else cur_file["sub_tracks"]
        if not 0 <= track_pos < len(tracks):
            return await query.answer("Track no longer exists", show_alert=True)
        track = tracks[track_pos]
        session["pending_edit"] = {"kind": kind, "position": track_pos, "file_idx": cur_idx}
        await query.answer("Send track metadata in chat")
        await send_message(
            session["source_message"],
            "<b>Edit Track Metadata</b>\n\n"
            f"Track: <b>{escape(_track_display(track))}</b>\n"
            "Send <code>language | title</code> in one message.\n"
            "Examples: <code>eng | English Dub</code>, <code>tel | Telugu AAC</code>.\n"
            "Use a 2–3 letter language code (such as <code>eng</code>, <code>tel</code>, <code>jpn</code>) and leave the title blank to clear it.\n"
            "Send <code>cancel</code> to abandon this edit."
        )

    elif cmd in ("toggle_default_aud", "toggle_default_sub", "toggle_forced_sub"):
        track_pos = int(data[3])
        cur_file = files[cur_idx] if 0 <= cur_idx < len(files) else files[0]
        is_audio = cmd == "toggle_default_aud"
        kind = "audio" if is_audio else "subtitle"
        tracks_key = "audio_tracks" if is_audio else "sub_tracks"
        tracks = cur_file[tracks_key]
        if not 0 <= track_pos < len(tracks):
            return await query.answer("Track no longer exists", show_alert=True)
        flag = "forced" if cmd == "toggle_forced_sub" else "default"
        track = tracks[track_pos]
        new_value = not bool(track.get(flag, False))
        if flag == "default" and new_value:
            # Only one default stream per type should be active at a time.
            for other_pos, other in enumerate(tracks):
                if other_pos != track_pos and other.get("default"):
                    _set_track_flag(other, "default", False)
        _set_track_flag(track, flag, new_value)
        label = "Forced subtitle" if flag == "forced" else f"Default {kind} track"
        await query.answer(f"{label} {'enabled' if new_value else 'disabled'}")
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)

    elif cmd in ("select_aud_all", "select_aud_none", "select_sub_all", "select_sub_none"):
        cur_file = files[cur_idx] if 0 <= cur_idx < len(files) else files[0]
        is_audio = cmd.startswith("select_aud")
        tracks_key = "audio_tracks" if is_audio else "sub_tracks"
        selected_key = "selected_audio" if is_audio else "selected_sub"
        if cmd.endswith("_all"):
            cur_file[selected_key] = set(range(len(cur_file[tracks_key])))
            await query.answer("All tracks selected")
        else:
            cur_file[selected_key] = set()
            await query.answer("All tracks deselected")
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)

    elif cmd == "toggle_aud":
        track_pos = int(data[3])
        cur_file = files[cur_idx] if 0 <= cur_idx < len(files) else files[0]
        if track_pos in cur_file["selected_audio"]:
            cur_file["selected_audio"].remove(track_pos)
            await query.answer("Audio track deselected")
        else:
            cur_file["selected_audio"].add(track_pos)
            await query.answer("Audio track selected")
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)

    elif cmd == "toggle_sub":
        track_pos = int(data[3])
        cur_file = files[cur_idx] if 0 <= cur_idx < len(files) else files[0]
        if track_pos in cur_file["selected_sub"]:
            cur_file["selected_sub"].remove(track_pos)
            await query.answer("Subtitle track deselected")
        else:
            cur_file["selected_sub"].add(track_pos)
            await query.answer("Subtitle track selected")
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)

    elif cmd == "move_aud":
        display_pos = int(data[3])
        direction = int(data[4])
        target_disp_pos = display_pos + direction
        cur_file = files[cur_idx] if 0 <= cur_idx < len(files) else files[0]
        order = cur_file["audio_order"]
        if 0 <= display_pos < len(order) and 0 <= target_disp_pos < len(order):
            order[display_pos], order[target_disp_pos] = order[target_disp_pos], order[display_pos]
            await query.answer("Audio track reordered")
            caption, markup = format_tm_ui(session)
            await edit_message(session["msg"], caption, markup)

    elif cmd == "move_sub":
        display_pos = int(data[3])
        direction = int(data[4])
        target_disp_pos = display_pos + direction
        cur_file = files[cur_idx] if 0 <= cur_idx < len(files) else files[0]
        order = cur_file["sub_order"]
        if 0 <= display_pos < len(order) and 0 <= target_disp_pos < len(order):
            order[display_pos], order[target_disp_pos] = order[target_disp_pos], order[display_pos]
            await query.answer("Subtitle track reordered")
            caption, markup = format_tm_ui(session)
            await edit_message(session["msg"], caption, markup)

    elif cmd == "apply_all":
        cur_file = files[cur_idx] if 0 <= cur_idx < len(files) else files[0]

        def apply_track_rules(kind, source_order, source_selected, target_files):
            source_key = "audio_tracks" if kind == "audio" else "sub_tracks"
            selected_key = "selected_audio" if kind == "audio" else "selected_sub"
            order_key = "audio_order" if kind == "audio" else "sub_order"
            source_tracks = cur_file[source_key]

            for target_file in target_files:
                target_tracks = target_file[source_key]
                used = set()
                new_order = []
                new_selected = set()

                for source_pos in source_order:
                    source_track = source_tracks[source_pos]
                    source_match = str(source_track.get("match_lang") or source_track.get("short_lang") or "und").lower()
                    target_pos = next(
                        (
                            pos for pos, target_track in enumerate(target_tracks)
                            if pos not in used
                            and str(target_track.get("match_lang") or target_track.get("short_lang") or "und").lower() == source_match
                        ),
                        None,
                    )
                    if target_pos is None:
                        continue

                    target_track = target_tracks[target_pos]
                    used.add(target_pos)
                    new_order.append(target_pos)
                    if source_pos in source_selected:
                        new_selected.add(target_pos)

                    if source_track.get("metadata_changed"):
                        target_track["full_lang"] = source_track.get("full_lang") or "und"
                        target_track["title"] = source_track.get("title", "")
                        target_track["short_lang"] = source_track.get("short_lang") or get_short_lang({"tags": {"language": target_track["full_lang"], "title": target_track["title"]}})
                        target_track["metadata_changed"] = True

                    if source_track.get("disposition_changed"):
                        for flag in ("default", "forced"):
                            _set_track_flag(target_track, flag, bool(source_track.get(flag, False)))

                # Include unmatched tracks at the end so they are retained, but
                # leave them unselected, matching the source selection rules.
                for target_pos in range(len(target_tracks)):
                    if target_pos not in used:
                        new_order.append(target_pos)

                target_file[order_key] = new_order
                target_file[selected_key] = new_selected

        apply_track_rules(
            "audio", cur_file["audio_order"], cur_file["selected_audio"], files
        )
        apply_track_rules(
            "subtitle", cur_file["sub_order"], cur_file["selected_sub"], files
        )

        await query.answer("Applied matching track rules to all files", show_alert=True)
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)

    elif cmd == "back":
        session["view_mode"] = "list"
        await query.answer()
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)

    elif cmd == "cancel":
        await query.answer("Track Manager cancelled")
        fut = session.get("future")
        if fut and not fut.done():
            fut.set_result(False)

    elif cmd == "done":
        if session.get("pending_edit"):
            return await query.answer("Finish or cancel the metadata edit first", show_alert=True)
        await query.answer("Applying track choices...")
        fut = session.get("future")
        if fut and not fut.done():
            fut.set_result(True)


async def _tm_text_handler(client, message: Message, mid: int):
    session = track_manager_sessions.get(mid)
    pending = session.get("pending_edit") if session else None
    if not session or not pending or not message.text:
        return

    raw = message.text.strip()
    if raw.lower() in {"cancel", "/cancel"}:
        session["pending_edit"] = None
        with suppress(Exception):
            await message.delete()
        caption, markup = format_tm_ui(session)
        await edit_message(session["msg"], caption, markup)
        return

    if "|" not in raw:
        await send_message(
            session["source_message"],
            "Invalid format. Send <code>language | title</code>, for example <code>eng | English Dub</code>."
        )
        return

    language, title = (part.strip() for part in raw.split("|", 1))
    if not re.fullmatch(r"[A-Za-z]{2,3}", language):
        await send_message(
            session["source_message"],
            "Invalid language code. Use 2–3 letters such as <code>eng</code>, <code>tel</code>, or <code>jpn</code>."
        )
        return
    if len(title) > 200:
        await send_message(session["source_message"], "Track title must be 200 characters or fewer.")
        return

    file_idx = pending["file_idx"]
    cur_file = session["files"][file_idx]
    tracks_key = "audio_tracks" if pending["kind"] == "audio" else "sub_tracks"
    track_pos = pending["position"]
    if not 0 <= track_pos < len(cur_file[tracks_key]):
        session["pending_edit"] = None
        await send_message(session["source_message"], "This track is no longer available. Please reopen Track Manager.")
        return

    track = cur_file[tracks_key][track_pos]
    track["full_lang"] = language.lower()
    track["title"] = title
    track["short_lang"] = get_short_lang({"tags": {"language": language.lower(), "title": title}})
    track["metadata_changed"] = True
    session["pending_edit"] = None
    with suppress(Exception):
        await message.delete()
    caption, markup = format_tm_ui(session)
    await edit_message(session["msg"], caption, markup)


async def proceed_track_manager(listener, dl_path, gid):
    if not dl_path or not await aiopath.exists(dl_path):
        return dl_path

    # Discover media files
    all_files = []
    if listener.is_file or await aiopath.isfile(dl_path):
        is_vid, is_aud, _ = await get_document_type(dl_path)
        if is_vid or is_aud:
            all_files.append(dl_path)
    else:
        for dirpath, _, files in await sync_to_async(walk, dl_path, topdown=False):
            for file_ in files:
                fp = ospath.join(dirpath, file_)
                is_vid, is_aud, _ = await get_document_type(fp)
                if is_vid or is_aud:
                    all_files.append(fp)

    if not all_files:
        LOGGER.info("Track Manager: No media files found to inspect.")
        return dl_path

    ffmpeg = FFMpeg(listener)
    files_data = []

    for fp in all_files:
        streams = await ffmpeg.get_streams(fp)
        if not streams:
            continue

        aud_streams = [s for s in streams if s.get("codec_type") == "audio"]
        sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

        audio_tracks = [
            {
                "index": s.get("index"),
                "codec": s.get("codec_name"),
                "short_lang": get_short_lang(s),
                "match_lang": get_short_lang(s),
                "title": s.get("tags", {}).get("title", ""),
                "full_lang": s.get("tags", {}).get("language", "und"),
                "disposition": dict(s.get("disposition") or {}),
                "default": bool((s.get("disposition") or {}).get("default", 0)),
                "forced": bool((s.get("disposition") or {}).get("forced", 0)),
                "disposition_changed": False,
                "metadata_changed": False,
            }
            for s in aud_streams
        ]

        sub_tracks = [
            {
                "index": s.get("index"),
                "codec": s.get("codec_name"),
                "short_lang": get_short_lang(s),
                "match_lang": get_short_lang(s),
                "title": s.get("tags", {}).get("title", ""),
                "full_lang": s.get("tags", {}).get("language", "und"),
                "disposition": dict(s.get("disposition") or {}),
                "default": bool((s.get("disposition") or {}).get("default", 0)),
                "forced": bool((s.get("disposition") or {}).get("forced", 0)),
                "disposition_changed": False,
                "metadata_changed": False,
            }
            for s in sub_streams
        ]

        files_data.append(
            {
                "path": fp,
                "name": ospath.basename(fp),
                "audio_tracks": audio_tracks,
                "sub_tracks": sub_tracks,
                "audio_order": list(range(len(audio_tracks))),
                "sub_order": list(range(len(sub_tracks))),
                "selected_audio": set(range(len(audio_tracks))),
                "selected_sub": set(range(len(sub_tracks))),
            }
        )

    if not files_data:
        LOGGER.info("Track Manager: No audio/subtitle streams found.")
        return dl_path

    mid = listener.mid
    user_id = listener.user_id
    fut = bot_loop.create_future()

    session = {
        "mid": mid,
        "user_id": user_id,
        "dl_path": dl_path,
        "gid": gid,
        "is_multi": len(files_data) > 1,
        "files": files_data,
        "current_file_idx": 0,
        "view_mode": "list" if len(files_data) > 1 else "audio",
        "page": 1,
        "page_size": 5,
        "future": fut,
        "source_message": listener.message,
        "pending_edit": None,
    }

    track_manager_sessions[mid] = session

    caption, markup = format_tm_ui(session)

    try:
        msg = await send_message(listener.message, caption, markup)
    except Exception as e:
        LOGGER.error(f"Failed to send Track Manager message: {e}")
        track_manager_sessions.pop(mid, None)
        return dl_path

    session["msg"] = msg

    text_handler = listener.client.add_handler(
        MessageHandler(
            partial(_tm_text_handler, mid=mid),
            filters.text & filters.user(user_id) & filters.chat(listener.message.chat.id),
        ),
        group=-3,
    )
    try:
        res = await wait_for(fut, timeout=600)
    except AsyncTimeoutError:
        LOGGER.info(f"Track Manager timed out for {mid}. Proceeding with default track selection.")
        res = True
    finally:
        listener.client.remove_handler(*text_handler)
        track_manager_sessions.pop(mid, None)
        await delete_message(msg)

    if not res:
        LOGGER.info("Track Manager cancelled by user.")
        return dl_path

    # Execute FFmpeg modifications for each file. Publish a named status while
    # FFmpeg remuxing is active, and make the child process cancellable.
    status_obj = None
    previous_status = None
    status_registered = False
    try:
        async with task_dict_lock:
            previous_status = task_dict.get(mid)
        for f in files_data:
            fp = f["path"]
            aud_tracks = f["audio_tracks"]
            sub_tracks = f["sub_tracks"]
            aud_order = f["audio_order"]
            sub_order = f["sub_order"]
            sel_aud = f["selected_audio"]
            sel_sub = f["selected_sub"]

            # Check if modification is needed
            aud_modified = (
                len(sel_aud) != len(aud_tracks)
                or aud_order != list(range(len(aud_tracks)))
                or any(pos in sel_aud and (aud_tracks[pos].get("metadata_changed") or aud_tracks[pos].get("disposition_changed")) for pos in range(len(aud_tracks)))
            )
            sub_modified = (
                len(sel_sub) != len(sub_tracks)
                or sub_order != list(range(len(sub_tracks)))
                or any(pos in sel_sub and (sub_tracks[pos].get("metadata_changed") or sub_tracks[pos].get("disposition_changed")) for pos in range(len(sub_tracks)))
            )

            if not aud_modified and not sub_modified:
                continue

            out_path = f"{fp}.tm_out.mkv"
            progress_obj = _TrackManagerProgress()
            progress_obj.listener = listener
            progress_obj.path = fp
            progress_obj.outfile = out_path
            status_obj = progress_obj
            async with task_dict_lock:
                task_dict[mid] = FFmpegStatus(listener, progress_obj, gid, "VT:Track Manager (Remux)")
                status_registered = True
            with suppress(Exception):
                await update_status_message(listener.message.chat.id)
            cmd = [
                BinConfig.FFMPEG_NAME, "-hide_banner", "-loglevel", "info", "-stats_period", "0.5", "-y",
                "-i", fp, "-map", "0:v?", "-map_metadata", "0", "-map_chapters", "0",
            ]

            # Map selected audio streams in requested order and apply edited metadata.
            audio_out_pos = 0
            for pos in aud_order:
                if pos in sel_aud:
                    track = aud_tracks[pos]
                    cmd.extend(["-map", f"0:{track['index']}"])
                    if track.get("metadata_changed"):
                        cmd.extend([f"-metadata:s:a:{audio_out_pos}", f"language={track.get('full_lang') or 'und'}"])
                        cmd.extend([f"-metadata:s:a:{audio_out_pos}", f"title={track.get('title', '')}"])
                    audio_out_pos += 1

            # Map selected subtitle streams in requested order and apply edited metadata.
            subtitle_out_pos = 0
            for pos in sub_order:
                if pos in sel_sub:
                    track = sub_tracks[pos]
                    cmd.extend(["-map", f"0:{track['index']}"])
                    if track.get("metadata_changed"):
                        cmd.extend([f"-metadata:s:s:{subtitle_out_pos}", f"language={track.get('full_lang') or 'und'}"])
                        cmd.extend([f"-metadata:s:s:{subtitle_out_pos}", f"title={track.get('title', '')}"])
                    subtitle_out_pos += 1


            if any(pos in sel_aud and aud_tracks[pos].get("disposition_changed") for pos in range(len(aud_tracks))):
                audio_out_pos = 0
                for pos in aud_order:
                    if pos in sel_aud:
                        flags = _disposition_flags(aud_tracks[pos])
                        cmd.extend(["-disposition:a:" + str(audio_out_pos), "+".join(flags) if flags else "0"])
                        audio_out_pos += 1
            if any(pos in sel_sub and sub_tracks[pos].get("disposition_changed") for pos in range(len(sub_tracks))):
                subtitle_out_pos = 0
                for pos in sub_order:
                    if pos in sel_sub:
                        flags = _disposition_flags(sub_tracks[pos])
                        cmd.extend(["-disposition:s:" + str(subtitle_out_pos), "+".join(flags) if flags else "0"])
                        subtitle_out_pos += 1

            cmd.extend(["-c", "copy", out_path])
            proc = await create_subprocess_exec(*cmd, stderr=PIPE)
            listener.subproc = proc
            try:
                _, code = await gather(progress_obj.progress(), proc.wait())
            finally:
                listener.subproc = None
            err = "\n".join(progress_obj.stderr_tail[-20:]).strip()
            try:
                if code == 0 and await aiopath.exists(out_path):
                    await move(out_path, fp)
                    LOGGER.info(f"Track Manager successfully updated tracks for {fp}")
                else:
                    LOGGER.error(f"Track Manager FFmpeg error for {fp} (exit={code}): {err}")
                    if await aiopath.exists(out_path):
                        await remove(out_path)
            except Exception as e:
                LOGGER.error(f"Track Manager could not commit remux output for {fp}: {e}", exc_info=True)
                with suppress(Exception):
                    if await aiopath.exists(out_path):
                        await remove(out_path)

    finally:
        if status_registered:
            async with task_dict_lock:
                current = task_dict.get(mid)
                if current is not None and getattr(current, "_obj", None) is status_obj:
                    if previous_status is not None:
                        task_dict[mid] = previous_status
                    else:
                        task_dict.pop(mid, None)

    return dl_path
