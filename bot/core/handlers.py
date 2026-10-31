# ruff: noqa: F403, F405

from pyrogram.filters import command, regex, private
from re import escape as re_escape
from pyrogram.handlers import CallbackQueryHandler, EditedMessageHandler, MessageHandler
from pyrogram.types import BotCommand

from ..core.config_manager import Config
from ..helper.ext_utils.help_messages import BOT_COMMANDS
from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.filters import CustomFilters
from ..modules import *
from ..modules.taskm import taskm_command, taskm_callback, taskuser_command, taskuser_callback
from ..modules.verification import addpremium_command, premium_users_command, premium_users_callback, removepremium_command
from ..helper.ext_utils.track_manager import tm_callback
from .tg_client import TgClient


async def add_handlers():
    from ..modules.api_manager import register_api_handlers
    from ..modules.fsub import register_fsub_handlers
    register_fsub_handlers(TgClient.bot)
    register_api_handlers(TgClient.bot)

    # Verify gating runs before ordinary commands and callback actions. /start
    # and /verify stay reachable so users can complete verification.
    TgClient.bot.add_handler(
        MessageHandler(verification_guard, filters=verification_guard_filter),
        group=-1,
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(verification_guard, filters=verification_guard_filter),
        group=-1,
    )
    TgClient.bot.add_handler(
        MessageHandler(shortener_input, filters=shortener_input_filter),
        group=-2,
    )
    TgClient.bot.add_handler(
        MessageHandler(
            mention_all,
            filters=command(BotCommands.MentionCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            verification_command,
            filters=command(BotCommands.VerifyCommand, case_sensitive=False),
        )
    )
    TgClient.bot.add_handler(MessageHandler(addpremium_command, filters=command(BotCommands.AddPremiumCommand, case_sensitive=False) & CustomFilters.sudo))
    TgClient.bot.add_handler(MessageHandler(premium_users_command, filters=command(BotCommands.PremiumUsersCommand, case_sensitive=False) & CustomFilters.sudo))
    TgClient.bot.add_handler(MessageHandler(removepremium_command, filters=command(BotCommands.RemovePremiumCommand, case_sensitive=False) & CustomFilters.sudo))
    TgClient.bot.add_handler(CallbackQueryHandler(premium_users_callback, filters=regex(r"^premiumusers:\d+$") & CustomFilters.sudo))
    TgClient.bot.add_handler(
        MessageHandler(
            shorteners_command,
            filters=command(BotCommands.ShortenersCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(shorteners_callback, filters=regex(r"^shorteners(?:\s|$)"))
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(verify_dm_callback, filters=regex(r"^verifydm:"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            authorize,
            filters=command(BotCommands.AuthorizeCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            unauthorize,
            filters=command(BotCommands.UnAuthorizeCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            add_sudo,
            filters=command(BotCommands.AddSudoCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            remove_sudo,
            filters=command(BotCommands.RmSudoCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            add_blacklist,
            filters=command(BotCommands.BlackListCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            remove_blacklist,
            filters=command(BotCommands.RmBlackListCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            black_listed,
            filters=regex(r"^/") & CustomFilters.authorized & CustomFilters.blacklisted,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            send_bot_settings,
            filters=command(BotCommands.BotSetCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            broadcast,
            filters=command(BotCommands.BroadcastCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(
            edit_bot_settings, filters=regex("^botset") & CustomFilters.sudo
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            memory_stats,
            filters=command(BotCommands.MemoryCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(
            memory_callback, filters=regex("^mem") & CustomFilters.sudo
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            cancel,
            filters=regex(rf"(?i)^/{BotCommands.CancelTaskCommand[1]}_(?:\w+).*$")
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            cancel_range_link,
            filters=command(BotCommands.CancelRangeLinkCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(cancel_range_link_cb, filters=regex("^cancelrl"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            cancel_all_buttons,
            filters=command(BotCommands.CancelAllCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(cancel_all_update, filters=regex("^canall"))
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(cancel_multi, filters=regex("^stopm"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            clone_node,
            filters=command(BotCommands.CloneCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            aioexecute,
            filters=command(BotCommands.AExecCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            execute,
            filters=command(BotCommands.ExecCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            clear,
            filters=command(BotCommands.ClearLocalsCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            select,
            filters=regex(rf"(?i)^/{BotCommands.SelectCommand[1]}_(?:\w+).*$")
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(confirm_selection, filters=regex("^sel"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            remove_from_queue,
            filters=command(BotCommands.ForceStartCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            count_node,
            filters=command(BotCommands.CountCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            delete_file,
            filters=command(BotCommands.DeleteCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            gdrive_search,
            filters=command(BotCommands.ListCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(select_type, filters=regex("^list_types"))
    )
    TgClient.bot.add_handler(CallbackQueryHandler(arg_usage, filters=regex("^help")))
    TgClient.bot.add_handler(
        MessageHandler(
            mirror,
            filters=command(BotCommands.MirrorCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            qb_mirror,
            filters=command(BotCommands.QbMirrorCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            jd_mirror,
            filters=command(BotCommands.JdMirrorCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            nzb_mirror,
            filters=command(BotCommands.NzbMirrorCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            ytdl,
            filters=command(BotCommands.YtdlCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            leech,
            filters=command(BotCommands.LeechCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            ytdl_leech,
            filters=command(BotCommands.YtdlLeechCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            socialdl,
            filters=command(BotCommands.SocialdlCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            socialdl_leech,
            filters=command(BotCommands.SocialdlTelegramCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            cookiesettings,
            filters=command(BotCommands.CookieSettingsCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(social_cookie_callback, filters=regex("^scookie"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            qb_leech,
            filters=command(BotCommands.QbLeechCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            jd_leech,
            filters=command(BotCommands.JdLeechCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            nzb_leech,
            filters=command(BotCommands.NzbLeechCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            seedr_link,
            filters=command(BotCommands.SeedrLinkCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            uphoster,
            filters=command(BotCommands.UpHosterCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            get_rss_menu,
            filters=command(BotCommands.RssCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(CallbackQueryHandler(rss_listener, filters=regex("^rss")))
    TgClient.bot.add_handler(
        MessageHandler(
            run_shell,
            filters=command(BotCommands.ShellCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        EditedMessageHandler(
            run_shell,
            filters=command(BotCommands.ShellCommand, case_sensitive=False)
            & CustomFilters.owner,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            start,
            filters=(
                command(BotCommands.StartCommand, case_sensitive=False)
                | regex(r"^/start(?:@[A-Za-z0-9_]+)?\s+verify(?:\s|$)")
            ),
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            login, filters=command(BotCommands.LoginCommand, case_sensitive=False)
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            log,
            filters=command(BotCommands.LogCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            restart_bot,
            filters=command(BotCommands.RestartCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(
            confirm_restart, filters=regex("^botrestart") & CustomFilters.sudo
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            restart_sessions,
            filters=command(BotCommands.RestartSessionsCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            ping,
            filters=command(BotCommands.PingCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            bot_help,
            filters=command(BotCommands.HelpCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            stream_links,
            filters=command(BotCommands.StreamCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            picture_add,
            filters=command(BotCommands.AddImageCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            pictures,
            filters=command(BotCommands.ImagesCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(pics_callback, filters=regex("^images"))
    )

    TgClient.bot.add_handler(
        MessageHandler(
            bot_stats,
            filters=command(BotCommands.StatsCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            task_status,
            filters=command(BotCommands.StatusCommand, case_sensitive=False)
            & (CustomFilters.authorized | private),
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(status_pages, filters=regex("^status"))
    )
    TgClient.bot.add_handler(CallbackQueryHandler(stats_pages, filters=regex("^stats")))
    TgClient.bot.add_handler(CallbackQueryHandler(log_cb, filters=regex("^log")))
    TgClient.bot.add_handler(CallbackQueryHandler(start_cb, filters=regex("^start ")))
    TgClient.bot.add_handler(
        MessageHandler(
            torrent_search,
            filters=command(BotCommands.SearchCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(torrent_search_update, filters=regex("^torser"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            get_users_settings,
            filters=command(BotCommands.UsersCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            send_user_settings,
            filters=command(BotCommands.UserSetCommand, case_sensitive=False)
            & CustomFilters.authorized_uset,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(edit_user_settings, filters=regex("^userset"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            add_bot_command,
            # Each user manages their own added-bot requirement and tokens.
            filters=command(BotCommands.AddBotCommand, case_sensitive=False),
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(add_bot_cb, filters=regex("^addbot"))
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(ht_merge_callback, filters=regex("^htmerge"))
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(planner_callback, filters=regex("^plcb"))
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(start_merge_callback, filters=regex("^startmerge"))
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(tm_callback, filters=regex("^tmcb"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            taskm_command,
            filters=command(BotCommands.TaskMCommand, case_sensitive=False)
            & CustomFilters.sudo,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(taskm_callback, filters=regex("^taskm"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            taskuser_command,
            filters=command(BotCommands.TaskUserCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(taskuser_callback, filters=regex("^taskuser"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            change_category,
            filters=command(BotCommands.CategorySelectCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(confirm_category, filters=regex("^scat"))
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(confirm_dump_chat, filters=regex("^sdump"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            drive_clean,
            filters=command(BotCommands.GDCleanCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(confirm_drive_clean_cb, filters=regex("^gdccat"))
    )
    TgClient.bot.add_handler(
        MessageHandler(
            auto_task_handler,
            filters=CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        MessageHandler(
            request_ff,
            filters=command(BotCommands.RequestFFCommand, case_sensitive=False)
            & CustomFilters.authorized,
        )
    )
    TgClient.bot.add_handler(
        CallbackQueryHandler(reqff_callback, filters=regex("^reqff"))
    )
    if Config.SET_COMMANDS:
        global BOT_COMMANDS

        def insert_at(d, k, v, i):
            return dict(list(d.items())[:i] + [(k, v)] + list(d.items())[i:])

        if Config.JD_EMAIL and Config.JD_PASS:
            BOT_COMMANDS = insert_at(
                BOT_COMMANDS,
                "JdMirror",
                "[link/file] Mirror to Upload Destination using JDownloader",
                2,
            )
            BOT_COMMANDS = insert_at(
                BOT_COMMANDS,
                "JdLeech",
                "[link/file] Leech files to Upload to Telegram using JDownloader",
                6,
            )

        if len(Config.USENET_SERVERS) != 0:
            BOT_COMMANDS = insert_at(
                BOT_COMMANDS,
                "NzbMirror",
                "[nzb] Mirror to Upload Destination using Sabnzbd",
                2,
            )
            BOT_COMMANDS = insert_at(
                BOT_COMMANDS,
                "NzbLeech",
                "[nzb] Leech files to Upload to Telegram using Sabnzbd",
                6,
            )

        if not Config.DISABLE_SEEDR:
            BOT_COMMANDS = insert_at(
                BOT_COMMANDS,
                "SeedrLink",
                "[magnet] Get direct Seedr HTTP download links",
                9,
            )

        if Config.LOGIN_PASS:
            BOT_COMMANDS = insert_at(
                BOT_COMMANDS, "Login", "[password] Login to Bot", 14
            )

        await TgClient.bot.set_bot_commands(
            [
                BotCommand(
                    cmds[0] if isinstance(cmds, list) else cmds,
                    description,
                )
                for cmd, description in BOT_COMMANDS.items()
                for cmds in [getattr(BotCommands, f"{cmd}Command", None)]
                if cmds is not None
            ]
        )
