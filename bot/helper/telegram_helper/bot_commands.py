from ...core.config_manager import Config
from ...core.plugin_manager import get_plugin_manager


class BotCommands:
    _static_commands = {
        "Start": "start",
        "Api": "api",
        "Login": "login",
        "Mirror": ["mirror", "m"],
        "QbMirror": ["qbmirror", "qm"],
        "JdMirror": ["jdmirror", "jm"],
        "Ytdl": ["ytdl", "y"],
        "Socialdl": ["socialdl", "sdl"],
        "SocialdlTelegram": "sdt",
        "CookieSettings": ["cookiesettings", "cookies"],
        "UpHoster": ["uphoster", "up"],
        "NzbMirror": ["nzbmirror", "nm"],
        "Leech": ["leech", "l", "lh"],
        "QbLeech": ["qbleech", "ql"],
        "JdLeech": ["jdleech", "jl"],
        "YtdlLeech": ["ytdlleech", "yl"],
        "NzbLeech": ["nzbleech", "nl"],
        "SeedrLink": ["seedrlink", "slink", "srlink"],
        "Clone": ["clone", "cl"],
        "Count": "count",
        "Delete": "del",
        "List": "list",
        "Search": "search",
        "Users": "users",
        "CancelTask": ["cancel", "c"],
        "CancelRangeLink": ["cancelrl", "cancelrangelink", "cancel_rl", "cancelrange"],
        "CancelAll": ["cancelall", "call"],
        "ForceStart": ["forcestart", "fs"],
        "Status": ["status", "s", "statusall"],
        "Stream": ["stream", "sl"],
        "Ping": "ping",
        "Restart": ["restart", "r", "restartall"],
        "RestartSessions": ["restartses", "rses"],
        "Broadcast": ["broadcast", "bc"],
        "Stats": ["stats", "st"],
        "Help": ["help", "h"],
        "Log": "log",
        "Shell": "shell",
        "AExec": "aexec",
        "Exec": "exec",
        "ClearLocals": "clearlocals",
        "Rss": "rss",
        "AddImage": ["addimage", "ai"],
        "Images": ["images", "img"],
        "Authorize": ["authorize", "a"],
        "UnAuthorize": ["unauthorize", "ua"],
        "AddSudo": ["addsudo", "as"],
        "RmSudo": ["rmsudo", "rs"],
        "BlackList": ["blacklist", "bl"],
        "RmBlackList": ["rmblacklist", "rbl"],
        "BotSet": ["bsetting", "bs"],
        "UserSet": ["usetting", "us"],
        "AddBot": ["addbot", "ab"],
        "Select": ["select", "sel"],
        "CategorySelect": ["category", "ctsel"],
        "GDClean": ["gdclean", "gdc"],
        "Plugins": "plugins",
        "Memory": ["memory", "mem"],
        "RequestFF": ["requestff", "reqff"],
        "TaskM": ["taskm", "tm"],
        "TaskUser": ["taskuser", "tu"],
        "Verify": "verify",
        "Shorteners": "shorteners",
        "AddPremium": "addpremium",
        "PremiumUsers": "premium_users",
        "RemovePremium": "removepremium",
        "Mention": "mention",
    }

    @classmethod
    def get_commands(cls):
        commands = {
            key: (list(value) if isinstance(value, list) else value)
            for key, value in cls._static_commands.items()
        }
        taken = set()
        for value in commands.values():
            taken.update(value if isinstance(value, list) else [value])

        plugin_manager = get_plugin_manager()
        if plugin_manager:
            for rec in plugin_manager.list_plugins():
                if not rec.enabled:
                    continue
                for primary, names in rec.command_map().items():
                    fresh = [name for name in names if name not in taken]
                    if not fresh:
                        continue
                    key = primary.capitalize()
                    if key in commands:
                        key = f"{rec.name.capitalize()}{key}"
                    commands[key] = fresh
                    taken.update(fresh)

        return commands

    @classmethod
    def _build_command_vars(cls):
        commands = cls.get_commands()
        suffix = str(getattr(Config, "CMD_SUFFIX", "") or "").strip()
        # Exempt only these exact public command names. Every other alias,
        # including plugin commands, is registered in suffixed form only.
        exempt = {"start", "cancelall", "statusall"}
        for key, cmds in commands.items():
            names = [str(name) for name in (cmds if isinstance(cmds, list) else [cmds])]
            expanded = []
            for name in names:
                if suffix and name.casefold() not in exempt:
                    candidate = f"{name}{suffix}"
                else:
                    candidate = name
                if candidate not in expanded:
                    expanded.append(candidate)
            if key == "Start":
                setattr(cls, f"{key}Command", expanded[0])
            elif isinstance(cmds, list) or len(expanded) > 1:
                setattr(cls, f"{key}Command", expanded)
            else:
                setattr(cls, f"{key}Command", expanded[0])

    @classmethod
    def refresh_commands(cls):
        cls._build_command_vars()


BotCommands._build_command_vars()
