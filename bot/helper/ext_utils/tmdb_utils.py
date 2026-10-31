import re
from asyncio import sleep
from difflib import SequenceMatcher
from os.path import splitext
from urllib.parse import unquote

from niquests import AsyncSession

from ... import LOGGER
from ...core.config_manager import Config

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/multi"
TMDB_IMAGE_URL = "https://image.tmdb.org/t/p/original"

_QUALITY_TAGS = re.compile(
    r"\b(?:\d{3,4}p|4k|x26[45]|h\.?26[45]|hevc|avc|xvid|divx|aac\d?|ac3|eac3|ddp?\d(?:\.\d)?"
    r"|dts(?:-hd)?|truehd|atmos|hdr\d*|10bit|8bit|web-?dl|web-?rip|blu-?ray|bd-?rip"
    r"|br-?rip|hd-?rip|hdrip|dvd-?rip|hdts|cam-?rip|hdtv|remux|repack|multi[- ]?subs?|esubs?|msubs?|subbed"
    r"|dubbed|dsnp|amzn|hmax|hotstar|mkv|mp4|m4v|avi|webm|mpe?g|rar|zip|7z|part\d+)\b",
    re.IGNORECASE,
)
_SITE_PREFIX = re.compile(
    r"^\W*(?:www\.)?[\w-]+\.(?:com|net|org|xyz|me|in|to|co|cc|info|tv|link|app|online"
    r"|site|club|work|icu|top|vip|pro|party|fun|cam|lol|sbs|ws)"
    r"(?:\s*[-–—]+\s*|\s+)",
    re.IGNORECASE,
)

_poster_cache = {}
_imdb_image_cache = {}


def _tidy(title):
    title = re.sub(r"\s-+\s", " ", title)
    return re.sub(r"\s+", " ", title).strip(" -_.")


def _final_clean(title):
    base = re.sub(r"[\[\](){}]", " ", title)
    base = _QUALITY_TAGS.sub(" ", base)
    # Strip emoji/decorative symbols commonly added by uploaders while
    # preserving Unicode letters, digits, spaces, and meaningful title marks.
    base = re.sub(r"[^\w\s'&:-]", " ", base, flags=re.UNICODE)
    return _tidy(base) or _tidy(title)


def clean_poster_title(raw_title):
    """Extract the actual movie/series name from a full path or release filename."""
    raw_title = unquote(str(raw_title or "")).replace("\\", "/")
    # Telegram task captions can include custom-emoji markup such as
    # [🎬](https://web.telegram.org/k/assets/img/emoji/1f3ac.png), or a bare
    # decorative emoji before the real release title. Remove linked emoji
    # markup before URL cleanup so the URL cannot consume the rest of the name.
    raw_title = re.sub(r"!?\[[^\]]*\]\(https?://[^)]+\)", " ", raw_title)
    raw_title = re.sub(r"<a\b[^>]*>.*?</a>", " ", raw_title, flags=re.IGNORECASE | re.DOTALL)
    # The caller frequently passes an absolute file path, not just its filename.
    title = raw_title.rsplit("/", 1)[-1]
    stem, ext = splitext(title)
    if ext and len(ext) <= 9:
        title = stem

    title = re.sub(r"https?://\S+", " ", title)
    title = re.sub(r"\b(?:t|telegram)\.me/\S+", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\[[^\]]*\]", " ", title)
    title = re.sub(r"[\[\](){}]", " ", title)
    title = _SITE_PREFIX.sub("", title)
    title = re.sub(r"\bwww\S*", " ", title, flags=re.IGNORECASE)
    title = title.replace("_", " ")
    title = title.replace(".", " ")
    title = re.sub(r"\s+", " ", title).strip()
    # Ignore leading decorative emoji/symbols (including Telegram custom
    # emoji placeholders) without stripping letters from the actual title.
    title = re.sub(r"^[^\w\s]+", "", title, flags=re.UNICODE).strip()

    # For episode releases, everything after SxxExxx is release noise. Keep
    # the show name only (e.g. My Hero Academia Vigilantes).
    for pattern in (
        # Common release spelling: "My Hero Academia S01 E05 720p".
        # The season marker is sufficient to end the title even when the
        # episode marker is separated by whitespace.
        r"(?<!\w)S0*\d{1,2}(?:[ ._-]*E0*\d{1,3})?(?!\w)",
        r"\bSeason\s+\d{1,2}\b",
        r"(?<!\w)(?:E|EP|EPISODE)\s?\d{1,4}(?!\w)",
    ):
        marker = re.search(pattern, title, re.IGNORECASE)
        if marker:
            head = title[: marker.start()].strip()
            tail = title[marker.end() :].strip()
            title = head if len(head) > 1 else tail
            return _final_clean(title), None

    # A release year is the boundary between the real title and release
    # metadata. Use the first year (not the last), so language tags, codecs,
    # uploader names, and other trailing text can never become part of the
    # catalogue search title. Example: "Salaar Part 1 Ceasefire 2023 BRRip
    # Hi Te Ta Ml Kn 720p X264 AVC HE-AAC 2.0 ESub CineVood.mkv" becomes
    # title="Salaar Part 1 Ceasefire", year="2023".
    year_match = re.search(r"\b(?:19|20)\d{2}\b", title)
    if year_match:
        head = title[: year_match.start()].strip()
        tail = title[year_match.end() :].strip()
        return _final_clean(head if len(head) > 1 else tail), year_match.group(0)

    # If there is no year/episode marker, stop at the first technical release
    # token so encodes, languages, codecs, and uploader decorations are not
    # mistakenly sent to IMDb as part of the title.
    technical = re.search(
        r"\b(?:\d{3,4}p|4k|2160p|1080p|720p|480p|WEB[\s._-]*DL|WEB[\s._-]*RIP|"
        r"BLU[\s._-]*RAY|BD[\s._-]*RIP|BR[\s._-]*RIP|HDRIP|HDTV|HEVC|H[.]?26[45]|"
        r"X26[45]|XVID|DIVX|AAC(?:[\s._-]*\d(?:[.]\d)?)*|AC3|EAC3|DTS|TRUEHD|"
        r"MULTI[\s._-]*SUBS?|ESUBS?|MSUBS?|SUBBED|DUBBED)\b",
        title,
        re.IGNORECASE,
    )
    if technical and technical.start() > 1:
        title = title[: technical.start()].strip()
    title = re.sub(r"[~|]+.*$", " ", title).strip()
    return _final_clean(title), None


def _pick_image(images, as_doc):
    backdrops = images.get("backdrops", [])
    posters = images.get("posters", [])
    en_backdrops = [i for i in backdrops if i.get("iso_639_1") == "en"]
    no_lang_backdrops = [i for i in backdrops if i.get("iso_639_1") is None]
    en_posters = [i for i in posters if i.get("iso_639_1") == "en"]
    no_lang_posters = [i for i in posters if i.get("iso_639_1") is None]

    # Keep artwork orientation correct: document thumbnails use portrait
    # posters; media thumbnails use landscape backdrops only.
    if as_doc:
        order = [en_posters, no_lang_posters, posters]
    else:
        order = [en_backdrops, no_lang_backdrops, backdrops]

    for group in order:
        if group:
            # TMDb can return several language variants/resolutions. Choose
            # the largest artwork in the preferred orientation group.
            best = max(
                group,
                key=lambda item: (int(item.get("width") or 0) * int(item.get("height") or 0), int(item.get("vote_count") or 0)),
            )
            return best["file_path"]
    return None


def _tmdb_token():
    return str(Config.TMDB_ACCESS_TOKEN or "").strip()


async def get_tmdb_poster_link(title, year=None, as_doc=False):
    token = _tmdb_token()
    if not token:
        return None

    headers = {"accept": "application/json"}
    params = {
        "query": title,
        "include_adult": "false",
        "language": "en-US",
        "page": "1",
    }
    if len(token) < 50:
        params["api_key"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(3):
        try:
            async with AsyncSession() as client:
                resp = await client.get(TMDB_SEARCH_URL, params=params, headers=headers, timeout=15)
                if resp.status_code == 401:
                    LOGGER.warning("TMDb authentication failed, check TMDB_ACCESS_TOKEN")
                    return None
                if resp.status_code >= 500:
                    await sleep(2)
                    continue
                if resp.status_code != 200:
                    LOGGER.warning(f"TMDb returned {resp.status_code} for '{title}'")
                    return None

                results = [
                    r
                    for r in resp.json().get("results", [])
                    if r.get("media_type") != "person"
                ]
                if not results:
                    LOGGER.info(f"No TMDb results for '{title}'")
                    return None

                target = _normalize_title(title)

                def result_score(result):
                    names = (
                        result.get("title"),
                        result.get("original_title"),
                        result.get("name"),
                        result.get("original_name"),
                    )
                    best = max(
                        (SequenceMatcher(None, target, _normalize_title(name)).ratio()
                         for name in names if name),
                        default=0.0,
                    )
                    date = result.get("release_date") or result.get("first_air_date") or ""
                    if year and date.startswith(year):
                        best += 0.15
                    return best

                results.sort(key=result_score, reverse=True)
                first = results[0]
                if result_score(first) < 0.68:
                    LOGGER.info("No close TMDb title match for '%s'", title)
                    return None
                media_type = first.get("media_type", "movie")

                img_params = {"include_image_language": "en,null"}
                if len(token) < 50:
                    img_params["api_key"] = token
                img_resp = await client.get(
                    f"https://api.themoviedb.org/3/{media_type}/{first['id']}/images",
                    params=img_params,
                    headers=headers,
                )
                if img_resp.status_code == 200:
                    path = _pick_image(img_resp.json(), as_doc)
                    if path:
                        return f"{TMDB_IMAGE_URL}{path}"

                backdrop = first.get("backdrop_path")
                poster = first.get("poster_path")
                fallback = poster if as_doc else backdrop
                if fallback:
                    return f"{TMDB_IMAGE_URL}{fallback}"
                return None
        except Exception as e:
            LOGGER.warning(f"TMDb request failed ({attempt + 1}/3): {e}")
            await sleep(1)
    return None



def _guess_title_locale(title):
    """Guess an IMDb search locale from common Unicode writing-system ranges."""
    codes = [ord(char) for char in str(title or "")]
    # Script-specific ranges are checked before shared CJK ideographs.
    checks = (
        ((0x3040, 0x30FF), "ja"),
        ((0xAC00, 0xD7AF), "ko"),
        ((0x0900, 0x097F), "hi"),
        ((0x0980, 0x09FF), "bn"),
        ((0x0A00, 0x0A7F), "pa"),
        ((0x0A80, 0x0AFF), "gu"),
        ((0x0B00, 0x0B7F), "or"),
        ((0x0B80, 0x0BFF), "ta"),
        ((0x0C00, 0x0C7F), "te"),
        ((0x0C80, 0x0CFF), "kn"),
        ((0x0D00, 0x0D7F), "ml"),
        ((0x0D80, 0x0DFF), "si"),
        ((0x0600, 0x06FF), "ar"),
        ((0x0400, 0x052F), "ru"),
        ((0x0E00, 0x0E7F), "th"),
        ((0x0590, 0x05FF), "he"),
        ((0x0370, 0x03FF), "el"),
        ((0x0530, 0x058F), "hy"),
        ((0x10A0, 0x10FF), "ka"),
        ((0x1000, 0x109F), "my"),
        ((0x1200, 0x137F), "am"),
    )
    for (lower, upper), locale in checks:
        if any(lower <= code <= upper for code in codes):
            return locale
    if any(0x4E00 <= code <= 0x9FFF for code in codes):
        return "zh"
    return "en"


def _normalize_title(value):
    """Normalize titles without discarding non-Latin scripts or accents."""
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


async def get_imdb_thumbnail_link(title, year=None, as_doc=False):
    """Return IMDb portrait artwork for documents or a landscape backdrop for media."""
    from .bot_utils import sync_to_async

    cache_key = (title.casefold(), str(year or ""), bool(as_doc))
    if cache_key in _imdb_image_cache:
        return _imdb_image_cache[cache_key]

    try:
        from imdbinfo import get_media_gallery, get_movie, search_title

        search_year = int(year) if str(year or "").isdigit() else None
        target = _normalize_title(title)
        locales = ["en"]
        guessed_locale = _guess_title_locale(title)
        if guessed_locale != "en":
            locales.append(guessed_locale)

        # Search the default catalogue locale and, when the filename uses a
        # recognizable non-Latin script, retry in that language as well.
        candidates = []
        seen_ids = set()
        for locale in locales:
            try:
                search_result = await sync_to_async(
                    search_title, title, year=search_year, locale=locale
                )
                for item in list(getattr(search_result, "titles", None) or []):
                    item_id = (
                        getattr(item, "imdbId", None)
                        or getattr(item, "imdb_id", None)
                        or getattr(item, "id", None)
                    )
                    identity = str(item_id or getattr(item, "title", ""))
                    if identity not in seen_ids:
                        seen_ids.add(identity)
                        candidates.append(item)
            except Exception as search_exc:
                LOGGER.debug("IMDb %s title search failed for '%s': %s", locale, title, search_exc)
        if not candidates:
            _imdb_image_cache[cache_key] = None
            return None

        def candidate_score(candidate):
            candidate_title = getattr(candidate, "title", "")
            normalized = _normalize_title(candidate_title)
            similarity = SequenceMatcher(None, target, normalized).ratio()
            if normalized == target:
                similarity += 0.50
            elif normalized and (normalized in target or target in normalized):
                similarity += 0.20
            kind = str(getattr(candidate, "kind", "") or "").casefold()
            if "series" in kind or "miniseries" in kind:
                similarity += 0.08
            elif "episode" in kind:
                similarity -= 0.12
            candidate_year = str(getattr(candidate, "year", "") or "")
            if search_year and candidate_year == str(search_year):
                similarity += 0.15
            elif search_year and candidate_year.isdigit():
                similarity -= min(abs(int(candidate_year) - search_year) * 0.01, 0.10)
            return similarity

        candidates.sort(key=candidate_score, reverse=True)
        # Do not attach artwork from an unrelated IMDb result just because the
        # search API returned something. Exact/close localized title matches win.
        for candidate in candidates[:5]:
            if candidate_score(candidate) < 0.68:
                continue
            imdb_id = (
                getattr(candidate, "imdbId", None)
                or getattr(candidate, "imdb_id", None)
                or getattr(candidate, "id", None)
            )
            if not imdb_id:
                continue

            cover_url = getattr(candidate, "cover_url", None)
            if as_doc and cover_url:
                _imdb_image_cache[cache_key] = cover_url
                return cover_url

            gallery = await sync_to_async(get_media_gallery, str(imdb_id), locale="en")
            # get_media_gallery documents its return type as "MediaGallery
            # or list" - handle both an items-wrapper object and a plain
            # iterable of media items.
            _gallery_items = getattr(gallery, "items", None)
            if _gallery_items is None and isinstance(gallery, (list, tuple)):
                _gallery_items = gallery
            items = list(_gallery_items or [])

            def image_shape(item):
                try:
                    width = int(getattr(item, "width", None) or 0)
                    height = int(getattr(item, "height", None) or 0)
                except (TypeError, ValueError):
                    width, height = 0, 0
                image_type = str(getattr(item, "type", "") or "").casefold()
                return width, height, image_type

            # Choose the largest image of the correct orientation, not simply
            # the first gallery item (which may be a cast portrait or logo).
            if as_doc:
                portrait_items = []
                for item in items:
                    width, height, image_type = image_shape(item)
                    if getattr(item, "url", None) and (
                        (height > width > 0)
                        or "poster" in image_type
                        or "portrait" in image_type
                    ):
                        portrait_items.append(item)
                if portrait_items:
                    best = max(
                        portrait_items,
                        key=lambda item: image_shape(item)[0] * image_shape(item)[1],
                    )
                    _imdb_image_cache[cache_key] = best.url
                    return best.url
                if cover_url:
                    _imdb_image_cache[cache_key] = cover_url
                    return cover_url
            else:
                landscape_items = []
                for item in items:
                    width, height, image_type = image_shape(item)
                    is_landscape = width > height > 0
                    is_wide_type = any(
                        tag in image_type
                        for tag in ("backdrop", "landscape", "still", "frame")
                    )
                    if getattr(item, "url", None) and (is_landscape or is_wide_type):
                        landscape_items.append(item)
                if landscape_items:
                    best = max(
                        landscape_items,
                        key=lambda item: image_shape(item)[0] * image_shape(item)[1],
                    )
                    _imdb_image_cache[cache_key] = best.url
                    return best.url

            # Portrait cover fallback is valid only for document mode. A video
            # without a backdrop falls through to other landscape sources/frame.
            if as_doc:
                detail = await sync_to_async(get_movie, str(imdb_id), locale="en")
                detail_cover = getattr(detail, "cover_url", None) if detail else None
                if detail_cover:
                    _imdb_image_cache[cache_key] = detail_cover
                    return detail_cover

        _imdb_image_cache[cache_key] = None
        return None
    except Exception as exc:
        LOGGER.debug("IMDb auto-thumbnail lookup failed for '%s': %s", title, exc)
        return None


def _public_json(url, params=None, payload=None):
    """Small no-key metadata adapter for public catalogue APIs."""
    import json as _json
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    data = None if payload is None else _json.dumps(payload).encode("utf-8")
    headers = {"User-Agent": "CDML-X/1.0 (thumbnail metadata)", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers)
    with urlopen(req, timeout=12) as response:
        return _json.loads(response.read().decode("utf-8", "replace"))


def _public_catalogue_image(title, year=None, as_doc=False):
    """Use keyless catalogues only when their title match is sufficiently close."""
    import re as _re

    target = _normalize_title(title)
    anime_hint = bool(_re.search(r"\b(anime|ova|ona|season|episode|s\d{1,2}e\d{1,3})\b", title, _re.I))

    def score(value):
        candidate = _normalize_title(value)
        if not candidate:
            return 0.0
        if candidate == target:
            return 1.0
        return SequenceMatcher(None, target, candidate).ratio()

    # Jikan is useful for anime document posters. Never use its portrait cover
    # as a landscape video backdrop.
    try:
        data = _public_json("https://api.jikan.moe/v4/anime", {"q": title, "limit": 8, "sfw": "true"})
        results = data.get("data") or []
        results.sort(
            key=lambda item: max(
                score(item.get("title")),
                score(item.get("title_english")),
                *(score(alias) for alias in (item.get("title_synonyms") or [])),
            ),
            reverse=True,
        )
        if results:
            item = results[0]
            match_score = max(
                score(item.get("title")),
                score(item.get("title_english")),
                *(score(alias) for alias in (item.get("title_synonyms") or [])),
            )
            if match_score >= (0.58 if anime_hint else 0.72) and as_doc:
                images = item.get("images") or {}
                jpg = images.get("jpg") or images.get("webp") or {}
                url = jpg.get("large_image_url") or jpg.get("image_url")
                if url:
                    return url
    except Exception as exc:
        LOGGER.debug("Jikan thumbnail lookup failed: %s", exc)

    # AniList provides a wide banner for videos and a portrait cover for documents.
    try:
        query = "query ($search: String) { Media(search: $search, type: ANIME, isAdult: false) { title { romaji english native } coverImage { extraLarge large } bannerImage } }"
        data = _public_json(
            "https://graphql.anilist.co",
            payload={"query": query, "variables": {"search": title}},
        )
        media = ((data.get("data") or {}).get("Media") or {})
        names = media.get("title") or {}
        match_score = max(score(names.get("romaji")), score(names.get("english")), score(names.get("native")))
        if match_score >= (0.58 if anime_hint else 0.72):
            cover = media.get("coverImage") or {}
            url = (cover.get("extraLarge") or cover.get("large")) if as_doc else media.get("bannerImage")
            if url:
                return url
    except Exception as exc:
        LOGGER.debug("AniList thumbnail lookup failed: %s", exc)

    # TVmaze supports live-action, animation, and web series. For media output,
    # use an episode still (landscape); for documents, use the series poster.
    try:
        data = _public_json("https://api.tvmaze.com/search/shows", {"q": title})
        candidates = data or []
        candidates.sort(key=lambda item: score((item.get("show") or {}).get("name")), reverse=True)
        for candidate in candidates[:4]:
            show = candidate.get("show") or {}
            if score(show.get("name")) < 0.72:
                continue
            if as_doc:
                image = show.get("image") or {}
                url = image.get("original") or image.get("medium")
                if url:
                    return url
            else:
                show_id = show.get("id")
                if show_id:
                    episodes = _public_json(f"https://api.tvmaze.com/shows/{int(show_id)}/episodes")
                    stills = []
                    for episode in episodes or []:
                        image = episode.get("image") or {}
                        url = image.get("original") or image.get("medium")
                        if url:
                            stills.append(url)
                    if stills:
                        return stills[0]
    except Exception as exc:
        LOGGER.debug("TVmaze thumbnail lookup failed: %s", exc)
    return None

async def get_auto_thumbnail(filename, as_doc=False):
    from .media_utils import download_image_thumb

    title, year = clean_poster_title(filename)
    if len(title) < 2:
        return None

    key = (title, year, bool(as_doc))
    poster_url = _poster_cache.get(key)
    if not poster_url:
        # IMDb is the primary no-token source. The full release filename is
        # cleaned above, e.g. "My Hero Academia Vigilantes S01E12 ..." becomes
        # only "My Hero Academia Vigilantes" before search.
        # Prefer established IMDb artwork, then keyless public catalogues.
        poster_url = await get_imdb_thumbnail_link(title, year, as_doc)
        if not poster_url:
            poster_url = await __import__("asyncio").to_thread(
                _public_catalogue_image, title, year, as_doc
            )
        # TMDb remains an optional final source when the owner configured a token.
        if not poster_url:
            poster_url = await get_tmdb_poster_link(title, year, as_doc)
        if not poster_url:
            return None
        if len(_poster_cache) > 200:
            _poster_cache.clear()
        _poster_cache[key] = poster_url

    LOGGER.info("Auto thumbnail selected for '%s' (mode=%s)", title, "portrait" if as_doc else "landscape")
    return await download_image_thumb(poster_url) or None
