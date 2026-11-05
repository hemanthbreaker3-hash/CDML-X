"""Validate browser-exported cookie jars safely for yt-dlp.

A valid Netscape cookie file is passed through unchanged. Non-canonical exports
are converted into a private sidecar rather than overwriting the user's upload,
so a parser issue can never permanently discard authentication cookies.
"""

from json import loads
from logging import getLogger
from os import path as ospath
from time import time

LOGGER = getLogger(__name__)

HEADER = "# Netscape HTTP Cookie File"
_LOGIN_COOKIES = {
    "SID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
}
_cache = {}  # path -> (mtime, size, report)


def _bool(v):
    return "TRUE" if str(v).strip().upper() in ("TRUE", "1", "YES") else "FALSE"


def _expiry(v):
    try:
        return str(int(float(str(v).strip())))
    except (TypeError, ValueError):
        return "0"


def _from_json(text):
    data = loads(text)
    if isinstance(data, dict):
        data = data.get("cookies", [])
    rows = []
    for c in data:
        if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
            continue
        domain = str(c["domain"])
        if c.get("httpOnly"):
            domain = f"#HttpOnly_{domain}"
        host_only = c.get("hostOnly")
        include_sub = (
            "FALSE" if host_only else ("TRUE" if str(c["domain"]).startswith(".") else "FALSE")
        )
        rows.append(
            "\t".join(
                [
                    domain,
                    include_sub,
                    str(c.get("path") or "/"),
                    _bool(c.get("secure")),
                    _expiry(c.get("expirationDate", c.get("expires", 0))),
                    str(c["name"]),
                    str(c.get("value", "")),
                ]
            )
        )
    return rows


def _from_netscape(text):
    rows, dropped = [], 0
    for raw in text.split("\n"):
        line = raw.strip("\r\n ").lstrip("﻿")
        if not line:
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue  # comment / header, rewritten below
        parts = line.split("\t")
        if len(parts) < 6 or len(parts) > 7:
            parts = line.split(None, 6)  # extension used spaces
        if len(parts) == 6:
            parts.append("")  # cookie with empty value
        if len(parts) != 7:
            dropped += 1
            continue
        domain, sub, cpath, secure, exp, name, value = parts
        if not name.strip() or not domain.strip():
            dropped += 1
            continue
        rows.append(
            "\t".join(
                [domain.strip(), _bool(sub), cpath.strip() or "/", _bool(secure), _expiry(exp), name.strip(), value]
            )
        )
    return rows, dropped


def normalize_cookie_file(file_path):
    """Validate a cookie jar without ever destructively rewriting the uploaded file.

    Proper Netscape files are passed to yt-dlp byte-for-byte. If an upload is a
    non-canonical export, a private sidecar is created for yt-dlp; the original
    is retained so normalization can never permanently discard session cookies.
    """
    report = {
        "total": 0, "dropped": 0, "google": 0, "login": 0,
        "login_expired": 0, "fixed": False, "normalized_copy": False,
        "error": "", "path": file_path,
    }
    try:
        with open(file_path, "rb") as f:
            original_bytes = f.read()
        original = original_bytes.decode("utf-8-sig", errors="replace")
        text = original.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        valid_headers = {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}
        has_header = bool(lines and lines[0].strip() in valid_headers)
        raw_cookie_lines = [
            line for line in lines
            if line.strip() and (not line.lstrip().startswith("#") or line.lstrip().startswith("#HttpOnly_"))
        ]

        # Standard Netscape cookie jars should not be rewritten at all. In
        # particular, do not turn a 21-cookie jar into a smaller jar just to
        # normalize line endings or comments.
        canonical = has_header and all(
            len(line.split("\t")) == 7
            and bool(line.split("\t")[0].strip())
            and bool(line.split("\t")[5].strip())
            for line in raw_cookie_lines
        )
        if canonical:
            rows = [line for line in raw_cookie_lines]
            if not rows:
                report["error"] = "Cookie file contains a Netscape header but no cookie rows."
                return report
        else:
            stripped = text.lstrip()
            if stripped[:1] in ("[", "{"):
                try:
                    rows = _from_json(stripped)
                    dropped = 0
                except Exception as e:
                    report["error"] = f"Unreadable JSON cookie file: {e}"
                    return report
            else:
                rows, dropped = _from_netscape(text)
            report["dropped"] = dropped
            if not rows:
                report["error"] = "No valid cookie lines found (expected Netscape cookies.txt)."
                return report
            if dropped:
                # Do not silently create a reduced cookie jar. Losing even one
                # authentication cookie can make YouTube reject the session.
                report["error"] = (
                    f"Cookie export has {dropped} malformed row(s); refusing to create "
                    "a reduced cookie copy. Re-export a Netscape-format cookies.txt."
                )
                LOGGER.error("Cookie normalization refused for %s: %s", file_path, report["error"])
                return report
            # Never overwrite the user's original file. Use a protected
            # yt-dlp-compatible copy instead and point the downloader to it.
            sidecar = f"{file_path}.yt-dlp.txt"
            cleaned = HEADER + "\n" + "\n".join(rows) + "\n"
            with open(sidecar, "w", encoding="utf-8", newline="\n") as f:
                f.write(cleaned)
            try:
                import os
                os.chmod(sidecar, 0o600)
            except OSError:
                pass
            report["path"] = sidecar
            report["normalized_copy"] = True
            report["fixed"] = True
            LOGGER.info(
                "Cookie file compatibility copy created: %s (%s cookies; original preserved; %s malformed lines)",
                sidecar, len(rows), report["dropped"],
            )

        report["total"] = len(rows)
        now = time()
        for row in rows:
            parts = row.split("\t")
            if len(parts) != 7:
                continue
            domain, _, _, _, exp, name, _ = parts
            d = domain.replace("#HttpOnly_", "").lower().lstrip(".")
            if d == "youtube.com" or d.endswith(".youtube.com") or d == "youtu.be" or d == "google.com" or d.endswith(".google.com"):
                report["google"] += 1
                if name in _LOGIN_COOKIES:
                    report["login"] += 1
                    if exp != "0" and int(exp) < now:
                        report["login_expired"] += 1
        if report["normalized_copy"]:
            LOGGER.info(
                "Cookie file checked: %s (%s cookies; %s Google/YouTube); original upload unchanged",
                file_path, report["total"], report["google"],
            )
    except Exception as e:
        report["error"] = str(e)
    return report

def ensure_cookie_file(file_path):
    """Validate once per file version without mutating the original upload."""
    if not file_path or not ospath.exists(file_path):
        return {}
    try:
        st = ospath.getmtime(file_path), ospath.getsize(file_path)
    except OSError:
        return {}
    cached = _cache.get(file_path)
    if cached and cached[0] == st:
        selected = cached[1].get("path", file_path)
        if not selected or ospath.isfile(selected):
            return cached[1]
    report = normalize_cookie_file(file_path)
    with_new = (ospath.getmtime(file_path), ospath.getsize(file_path))
    _cache[file_path] = (with_new, report)
    return report


def describe_cookie_report(report):
    """One-line human summary, used on upload and in error messages."""
    if not report:
        return "no cookie file"
    if report.get("error"):
        return f"cookie file problem: {report['error']}"
    if not report.get("login"):
        login = "NO YouTube login cookies found (export while logged in to YouTube)"
    elif report.get("login") == report.get("login_expired"):
        login = "YouTube login cookies are EXPIRED (re-export)"
    else:
        login = "YouTube login cookies present"
    extra = f", {report['dropped']} bad line(s) removed" if report.get("dropped") else ""
    fixed = ", format auto-fixed" if report.get("fixed") else ""
    return f"{report['total']} cookies ({report['google']} Google/YouTube); {login}{extra}{fixed}"


def apply_youtube_client_fallbacks(options, url):
    """Enable current yt-dlp YouTube client fallbacks without losing user args.

    The official yt-dlp PO-token provider guidance recommends trying mweb, tv,
    and web_safari when the default clients fail before requesting a token.
    The provider is installed by deploy.vps; cookie auth remains separately
    enabled through cookiefile.
    """
    from copy import deepcopy
    from urllib.parse import urlparse

    host = (urlparse(str(url)).hostname or "").lower().rstrip(".")
    if not (host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be" or host.endswith(".youtu.be")):
        return options
    result = dict(options or {})
    extractor_args = result.get("extractor_args")
    if isinstance(extractor_args, dict):
        extractor_args = deepcopy(extractor_args)
    else:
        extractor_args = {}
    youtube_args = extractor_args.get("youtube")
    if not isinstance(youtube_args, dict):
        youtube_args = {}
    else:
        youtube_args = dict(youtube_args)
    existing = youtube_args.get("player_client", [])
    if isinstance(existing, str):
        existing = [existing]
    extra_clients = []
    for item in existing if isinstance(existing, (list, tuple)) else []:
        extra_clients.extend(part.strip() for part in str(item).split(",") if part.strip())
    # Current yt-dlp guidance recommends mweb with a PO-token provider.
    # Keep tv and web_safari as fallbacks, then retain user-configured clients.
    clients = ["mweb", "tv", "web_safari"]
    for client in extra_clients:
        if client not in clients:
            clients.append(client)
    youtube_args["player_client"] = clients
    extractor_args["youtube"] = youtube_args
    result["extractor_args"] = extractor_args
    return result


SOCIAL_COOKIE_PLATFORMS = {
    "youtube": ("YouTube", ("youtube.com", "youtu.be", "google.com")),
    "facebook": ("Facebook", ("facebook.com", "fb.com")),
    "instagram": ("Instagram", ("instagram.com",)),
    "x": ("X / Twitter", ("x.com", "twitter.com")),
    "tiktok": ("TikTok", ("tiktok.com",)),
    "reddit": ("Reddit", ("reddit.com",)),
    "generic": ("Generic Social", ()),
}


def social_cookie_path(user_id, platform):
    """Return the per-user cookie path for a social platform."""
    platform = str(platform).lower().strip()
    if platform not in SOCIAL_COOKIE_PLATFORMS:
        platform = "generic"
    return f"cookies/{user_id}/social_{platform}.txt"


def get_social_platform(url):
    """Map a URL to a cookie profile. Unknown yt-dlp sites use generic."""
    from urllib.parse import urlparse
    host = (urlparse(str(url)).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    for platform, (_, domains) in SOCIAL_COOKIE_PLATFORMS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return platform
    if host.endswith("youtube.com") or host.endswith("youtu.be"):
        return "youtube"
    return "generic"


def add_youtube_cookie_error_hint(error, url, cookie_file=None):
    """Explain cookie-related YouTube bot challenges without exposing cookie data."""
    message = str(error)
    lowered = message.lower()
    if not any(term in lowered for term in ("sign in to confirm", "not a bot", "confirm you’re not a bot", "confirm you're not a bot")):
        return message
    from urllib.parse import urlparse

    host = (urlparse(str(url)).hostname or "").lower()
    if not (host.endswith("youtube.com") or host.endswith("youtu.be")):
        return message

    report = ensure_cookie_file(cookie_file) if cookie_file and ospath.isfile(cookie_file) else {}
    if report:
        summary = describe_cookie_report(report)
        advice = (
            "\n\nYouTube cookie check: " + summary + ". "
            "Re-export a fresh Netscape-format cookies.txt while signed in to YouTube and upload it again. "
            "If valid, unexpired login cookies are present, YouTube may still be challenging the VPS IP; "
            "cookies alone cannot guarantee access."
        )
    else:
        advice = (
            "\n\nYouTube cookie check: no readable cookie file was found at the selected path. "
            "Upload a valid Netscape-format cookies.txt exported while signed in to YouTube."
        )
    return message + advice


def get_social_cookie_file(user_id, url, user_dict=None):
    """Prefer a platform-specific cookie, then generic, then the legacy user cookie."""
    user_dict = user_dict or {}
    platform = get_social_platform(url)
    candidates = []
    if platform != "youtube":
        candidates.append(social_cookie_path(user_id, platform))
    candidates.append(social_cookie_path(user_id, "generic"))
    legacy = user_dict.get("USER_COOKIE_FILE", "")
    if legacy:
        candidates.append(legacy)
    if ospath.exists("cookies.txt"):
        candidates.append("cookies.txt")
    for candidate in candidates:
        if candidate and ospath.exists(candidate):
            report = ensure_cookie_file(candidate)
            return report.get("path", candidate) if report else candidate
    return None
