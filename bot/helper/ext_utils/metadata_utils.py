from os.path import basename, splitext
from re import compile as re_compile
from pycountry import languages
from ..ext_utils.media_utils import get_streams, normalize_language_tag


class MetadataProcessor:
    # Only these global/container metadata fields may be applied. Keep in sync
    # with the FF Media Settings field list; unsupported tags are discarded.
    ALLOWED_METADATA_FIELDS = {
        "title", "writing_library", "album", "album_artist", "artist",
        "author", "channel", "channel_url", "collection", "composer",
        "contact", "comment", "date", "date_written", "description",
        "encoder_url", "encoded_by", "homepage", "lead_performer",
        "longdescription", "official-site", "official_artist_site",
        "official_audio_source_url", "official_release_site", "official_site",
        "official_source_url", "purl", "recorded_date", "released_date",
        "series", "show", "source_url", "summary", "synopsis", "url",
        "website",
    }
    _year_pattern = re_compile(r"\b(?:19|20)\d{2}\b")
    _season_episode_pattern = re_compile(r"(?i)(?:^|[ ._-])S(\d{1,2})[ ._-]*E(\d{1,3})(?:$|[ ._-])")
    _sanitize_pattern = re_compile(r'[<>:"/\\?*]')

    def __init__(self):
        self.vars = {}
        self.audio_streams = []
        self.subtitle_streams = []

    @staticmethod
    def convert_lang_code(lang_code):
        if not lang_code:
            return lang_code
        clean = normalize_language_tag(lang_code)
        if not clean:
            return "unknown"
        try:
            if len(clean) == 2:
                lang = languages.get(alpha_2=clean.lower())
            elif len(clean) == 3:
                lang = languages.get(alpha_3=clean.lower())
            else:
                lang = languages.get(name=clean.title())
            return lang.name if lang else clean
        except Exception:
            return clean


    async def extract_file_vars(self, file_path):
        fname = basename(file_path)
        bname, ext = splitext(fname)
        self.vars = {
            "filename": fname,
            "basename": bname,
            "extension": ext.lstrip("."),
            "audiolang": "unknown",
            "sublang": "none",
            "year": "",
            "season": "",
            "episode": "",
            "resolution": "",
            "quality": "",
            "width": "",
            "height": "",
            "video_codec": "",
            "audio_codec": "",
            "channels": "",
            "channel_layout": "",
            "duration": "",
        }
        self.audio_streams, self.subtitle_streams = [], []
        try:
            for s in await get_streams(file_path) or []:
                ctype = str(s.get("codec_type") or "").lower()
                slang = (s.get("tags") or {}).get("language", "unknown")
                full_lang = self.convert_lang_code(slang)
                entry = {
                    "index": s.get("index", 0),
                    "language": slang,
                    "full_language": full_lang,
                }
                if ctype == "audio":
                    self.audio_streams.append(entry)
                    if not self.vars["audio_codec"]:
                        self.vars["audio_codec"] = s.get("codec_name", "") or ""
                        self.vars["channels"] = s.get("channels", "") or ""
                        self.vars["channel_layout"] = s.get("channel_layout", "") or ""
                    if self.vars["audiolang"] == "unknown" and slang != "und":
                        self.vars["audiolang"] = full_lang
                elif ctype == "subtitle":
                    self.subtitle_streams.append(entry)
                    if self.vars["sublang"] == "none" and slang != "und":
                        self.vars["sublang"] = full_lang
                elif ctype == "video" and not bool((s.get("disposition") or {}).get("attached_pic", 0)):
                    if not self.vars["video_codec"]:
                        self.vars["video_codec"] = s.get("codec_name", "") or ""
                        self.vars["width"] = s.get("width", "") or ""
                        self.vars["height"] = s.get("height", "") or ""
                        self.vars["duration"] = s.get("duration", "") or ""
                        try:
                            height = int(self.vars["height"])
                            self.vars["resolution"] = f"{height}p" if height else ""
                            self.vars["quality"] = self.vars["resolution"]
                        except (TypeError, ValueError):
                            pass
        except Exception:
            pass
        years = self._year_pattern.findall(bname)
        if years:
            self.vars["year"] = years[-1]
        season_episode = self._season_episode_pattern.search(bname)
        if season_episode:
            self.vars["season"], self.vars["episode"] = season_episode.groups()

    @staticmethod
    def parse_string(metadata_str):
        """Parse metadata input consistently for settings and task arguments.

        ``Movie Title`` is accepted as shorthand for ``title=Movie Title``.
        Key/value pairs still support escaped pipes and multiple tags, e.g.
        ``title=Movie|artist=Studio``.
        """
        if isinstance(metadata_str, dict):
            parsed = {
                re_compile(r"\s+").sub("_", str(key).strip().lower()): str(value).strip()
                for key, value in metadata_str.items()
                if str(key).strip()
            }
            return {key: value for key, value in parsed.items() if key in MetadataProcessor.ALLOWED_METADATA_FIELDS}
        if not isinstance(metadata_str, str) or not metadata_str.strip():
            return {}
        raw = metadata_str.strip()
        if "=" not in raw:
            return {"title": raw.replace(r"\|", "|")}
        parts, current, i = [], "", 0
        while i < len(raw):
            if raw[i] == "\\" and i + 1 < len(raw) and raw[i + 1] == "|":
                current += "|"
                i += 2
            elif raw[i] == "|":
                parts.append(current)
                current = ""
                i += 1
            else:
                current += raw[i]
                i += 1
        parts.append(current)
        parsed = {}
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                # Mixed input such as ``title=Movie|Studio`` remains useful:
                # treat a bare segment as a title only when no title exists.
                if "title" not in parsed and part:
                    parsed["title"] = part
                continue
            key, value = part.split("=", 1)
            # Accept human-readable labels such as "Writing library" as well
            # as FFmpeg-style keys such as writing_library and official-site.
            key = re_compile(r"\s+").sub("_", key.strip().lower())
            value = value.strip()
            if key:
                parsed[key] = value
        return {key: value for key, value in parsed.items() if key in MetadataProcessor.ALLOWED_METADATA_FIELDS}

    @staticmethod
    def merge_dicts(default_dict, cmd_dict):
        return {**(default_dict or {}), **(cmd_dict or {})}

    def apply_vars_to_stream(
        self, metadata_dict, stream_lang=None, full_lang=None, stream_type="audio"
    ):
        if not isinstance(metadata_dict, dict):
            return {}
        vars_with_stream = self.vars.copy()
        if stream_lang and stream_lang != "unknown":
            key = "audiolang" if stream_type == "audio" else "sublang"
            vars_with_stream[key] = full_lang or self.convert_lang_code(stream_lang)
        class SafeVariables(dict):
            def __missing__(self, key):
                # Preserve unknown placeholders instead of failing an entire task.
                return "{" + str(key) + "}"

        def format_value(value):
            if not isinstance(value, str):
                return str(value)
            try:
                return value.format_map(SafeVariables(vars_with_stream))
            except (ValueError, IndexError):
                return value

        return {
            self.sanitize(k): format_value(v)
            for k, v in metadata_dict.items()
        }

    def apply_vars(self, metadata_dict):
        return self.apply_vars_to_stream(metadata_dict)

    def get_audio_metadata(self, audio_metadata_dict):
        return [
            {
                "index": s["index"],
                "metadata": self.apply_vars_to_stream(
                    audio_metadata_dict, s["language"], s["full_language"], "audio"
                ),
            }
            for s in self.audio_streams
        ]

    def get_subtitle_metadata(self, subtitle_metadata_dict):
        return [
            {
                "index": s["index"],
                "metadata": self.apply_vars_to_stream(
                    subtitle_metadata_dict,
                    s["language"],
                    s["full_language"],
                    "subtitle",
                ),
            }
            for s in self.subtitle_streams
        ]

    def sanitize(self, value):
        return self._sanitize_pattern.sub("_", str(value))[:100]

    @staticmethod
    def route_global_title_to_video(global_metadata, video_metadata, streams):
        """Avoid writing a duplicate title at container level when a video exists.

        Stream-specific metadata dictionaries independently apply configured values
        to video, audio, and subtitle streams. Attached cover art is not a video.
        """
        if not isinstance(global_metadata, dict):
            return
        title_items = [(key, value) for key, value in global_metadata.items()
                       if str(key).strip().lower() == "title"]
        if not title_items:
            return
        # Last configured generic title wins, then remove all case variants.
        title_value = title_items[-1][1]
        for key, _ in title_items:
            global_metadata.pop(key, None)
        if title_value is None or not str(title_value).strip():
            return
        has_video = any(
            str(stream.get("codec_type", "")).lower() == "video"
            and not bool((stream.get("disposition") or {}).get("attached_pic", 0))
            for stream in (streams or [])
        )
        if not has_video or not isinstance(video_metadata, dict):
            return
        # Preserve an explicit video-stream title, which takes precedence over
        # general metadata. Usually the title is already present because generic
        # user settings are copied to each stream in TaskConfig.
        if any(str(key).strip().lower() == "title" for key in video_metadata):
            return
        video_metadata["title"] = str(title_value)

    async def process_all(
        self,
        video_metadata_dict,
        audio_metadata_dict,
        subtitle_metadata_dict,
        file_path,
    ):
        await self.extract_file_vars(file_path)
        return {
            "video": (
                self.apply_vars(video_metadata_dict) if video_metadata_dict else {}
            ),
            "audio_streams": (
                self.get_audio_metadata(audio_metadata_dict)
                if audio_metadata_dict
                else []
            ),
            "subtitle_streams": (
                self.get_subtitle_metadata(subtitle_metadata_dict)
                if subtitle_metadata_dict
                else []
            ),
            "global": {},
        }

    async def process(self, metadata_dict, file_path):
        await self.extract_file_vars(file_path)
        return self.apply_vars(metadata_dict)
