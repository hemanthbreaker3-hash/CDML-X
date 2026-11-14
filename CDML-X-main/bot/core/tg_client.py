import os
import re
import time
from pathlib import Path
from ast import literal_eval
from wzgram import Client
from pyrogram import enums
from pyrogram.errors import FloodWait, PeerIdInvalid, ChannelInvalid
from asyncio import Lock, gather, sleep
from hashlib import sha256
from inspect import signature
from contextlib import suppress

from .. import LOGGER, bot_loop
from .config_manager import Config

# Pyrogram's file-backed SQLite sessions must live in a real, writable path.
# The old VPS path (/usr/src/app) exists in some containers but not on Ubuntu VPS.
SESSION_STORAGE_DIR = Path(
    os.environ.get("WZGRAM_SESSION_DIR", str(Path(__file__).resolve().parents[2] / ".sessions"))
).expanduser().resolve()
SESSION_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
try:
    SESSION_STORAGE_DIR.chmod(0o700)
except OSError:
    pass

_DB_PARTITION_SALT = b"wzmlx_v3_db_partition_salt"

#  MiB = 2_097_152_000 bytes  (~2.00 GB)
#   Premium           : 4000 MiB = 4_194_304_000 bytes  (~4.00 GB)
# Using decimal 4_000_000_000 caused ~3.8 GiB files to split into
# 3.73 GB + ~80 MB remainder. 4000 MiB keeps them as ONE file.
TELEGRAM_PREMIUM_MAX_BYTES = 4_194_304_000   # 4000 MiB — Telegram Premium max
PREMIUM_PART_BYTES = 4_194_304_000           # split size when Premium session is active
BOT_MAX_BYTES = 2_097_152_000                # 2000 MiB — bot / non-premium max
PREMIUM_MAX_BYTES = TELEGRAM_PREMIUM_MAX_BYTES
PREMIUM_CHUNK_BYTES = PREMIUM_PART_BYTES


def db_partition_id(bot_id):
    raw = sha256(_DB_PARTITION_SALT + str(bot_id).encode("utf-8")).hexdigest()
    return f"p_{raw[:24]}"


class TgClient:
    _lock = Lock()
    _hlock = Lock()
    _ulock = Lock()
    _slock = Lock()

    bot = None
    user = None
    helper_bots = {}
    helper_loads = {}
    helper_users = {}
    helper_user_loads = {}
    stream_bots = {}
    stream_loads = {}
    user_bots = {}
    personal_users = {}
    _personal_locks = {}

    BNAME = ""
    ID = 0
    PARTITION = ""
    IS_PREMIUM_USER = False
    TELEGRAM_PREMIUM_MAX_BYTES = TELEGRAM_PREMIUM_MAX_BYTES
    PREMIUM_PART_BYTES = PREMIUM_PART_BYTES
    BOT_MAX_BYTES = BOT_MAX_BYTES
    PREMIUM_MAX_BYTES = PREMIUM_MAX_BYTES
    PREMIUM_CHUNK_BYTES = PREMIUM_CHUNK_BYTES
    MAX_SPLIT_SIZE = BOT_MAX_BYTES

    @staticmethod
    def sanitize_proxy(proxy):
        if not isinstance(proxy, dict) or not proxy:
            return None
        p = dict(proxy)
        scheme = str(p.get("scheme") or "").lower()
        if not scheme:
            scheme = "socks5"
        elif scheme in ("socks5h", "socks4a"):
            scheme = scheme[:-1]
        p["scheme"] = scheme
        if not p.get("hostname") or not p.get("port"):
            return None
        try:
            p["port"] = int(p["port"])
        except (ValueError, TypeError):
            return None
        return p

    @staticmethod
    async def warm_peer_cache(client, limit=250):
        """Warm in-memory MTProto peer storage from the account's recent dialogs."""
        if client is None or not hasattr(client, "get_dialogs"):
            return
        try:
            count = 0
            async for _ in client.get_dialogs(limit=limit):
                count += 1
                if count >= limit:
                    break
        except Exception as exc:
            LOGGER.debug("Dialog cache warm-up skipped/partial for client: %s", exc)

    @classmethod
    async def ensure_peer(cls, client, chat_id, fallback_client=None):
        """Ensure `client` can resolve `chat_id` in its MTProto peer table."""
        if client is None or not chat_id:
            return False

        target = chat_id
        if isinstance(target, str):
            stripped = target.strip()
            if stripped.lstrip("-").isdigit():
                target = int(stripped)
            else:
                target = stripped

        # Self peer shortcut for user accounts
        me = getattr(client, "me", None)
        if me and isinstance(target, int) and target == getattr(me, "id", None):
            with suppress(Exception):
                await client.resolve_peer("me")
                return True

        try:
            await client.resolve_peer(target)
            return True
        except Exception:
            pass

        try:
            await client.get_chat(target)
            return True
        except Exception:
            pass

        # If target is a public @username or fallback_client knows its @username, resolve via username
        if fallback_client is not None and fallback_client is not client:
            try:
                fb_chat = await fallback_client.get_chat(target)
                uname = getattr(fb_chat, "username", None)
                if uname:
                    await client.get_chat(f"@{uname.lstrip('@')}")
                    return True
            except Exception:
                pass

        # Warm dialogs for user sessions and retry resolution
        if isinstance(target, int) and hasattr(client, "get_dialogs"):
            try:
                async for dialog in client.get_dialogs():
                    peer_chat = getattr(dialog, "chat", None)
                    if peer_chat and peer_chat.id == target:
                        return True
            except Exception as exc:
                LOGGER.debug("Could not scan dialogs for %s: %s", target, exc)

            try:
                await client.resolve_peer(target)
                return True
            except Exception:
                pass

        return False

    @classmethod
    def is_client_premium(cls, client):
        if client is None:
            return False
        me = getattr(client, "me", None)
        if me is not None and getattr(me, "is_premium", None) is not None:
            return bool(me.is_premium)
        if client is cls.user:
            return bool(cls.IS_PREMIUM_USER)
        return bool(getattr(client, "_is_premium_cached", False))

    @classmethod
    def has_premium_session(cls, user_id=None, user_dict=None):
        if cls.user is not None and cls.is_client_premium(cls.user):
            return True
        for huser in (cls.helper_users or {}).values():
            if cls.is_client_premium(huser):
                return True
        if user_id is not None:
            try:
                p_cli = cls.personal_users.get(int(user_id))
                if p_cli is not None and cls.is_client_premium(p_cli):
                    return True
            except (TypeError, ValueError):
                pass
        return False

    @classmethod
    def max_split_size_for_user(cls, user_id=None, user_dict=None):
        if cls.has_premium_session(user_id=user_id, user_dict=user_dict):
            return TELEGRAM_PREMIUM_MAX_BYTES
        return cls.MAX_SPLIT_SIZE

    @staticmethod
    def _install_fast_upload(client):
        """Accelerate large file uploads using parallel MTProto media sessions on wzgram[fast]."""
        import os
        import inspect
        from asyncio import Lock as AsyncLock, Queue, create_task, gather, to_thread
        from pyrogram import StopTransmission, raw
        from pyrogram.errors import FloodWait
        from pyrogram.session import Session

        try:
            from pyrogram.errors import FloodPremiumWait
        except ImportError:
            FloodPremiumWait = FloodWait

        orig_save_file = getattr(client, "save_file", None)
        if not callable(orig_save_file) or getattr(client, "_wz_fast_upload_installed", False):
            return client

        client._fast_upload_sessions = []
        client._fast_upload_lock = AsyncLock()

        async def _close_media_sessions():
            async with client._fast_upload_lock:
                sessions = list(client._fast_upload_sessions)
                client._fast_upload_sessions.clear()
            for sess in sessions:
                with suppress(Exception):
                    await sess.stop()

        async def _replace_media_session(bad_sess):
            async with client._fast_upload_lock:
                if bad_sess in client._fast_upload_sessions:
                    client._fast_upload_sessions.remove(bad_sess)
                with suppress(Exception):
                    await bad_sess.stop()
                dc_id = await client.storage.dc_id()
                auth_key = await client.storage.auth_key()
                test_mode = await client.storage.test_mode()
                if not auth_key:
                    return None
                new_sess = Session(client, dc_id, auth_key, test_mode, is_media=True)
                await new_sess.start()
                client._fast_upload_sessions.append(new_sess)
                return new_sess

        async def _get_media_sessions(pool_size=4):
            async with client._fast_upload_lock:
                alive = [
                    s
                    for s in client._fast_upload_sessions
                    if getattr(s, "is_started", None) and s.is_started.is_set()
                ]
                client._fast_upload_sessions = alive
                if len(alive) >= pool_size:
                    return alive[:pool_size]
                dc_id = await client.storage.dc_id()
                auth_key = await client.storage.auth_key()
                test_mode = await client.storage.test_mode()
                if not auth_key:
                    return []
                while len(client._fast_upload_sessions) < pool_size:
                    sess = Session(client, dc_id, auth_key, test_mode, is_media=True)
                    await sess.start()
                    client._fast_upload_sessions.append(sess)
                return list(client._fast_upload_sessions[:pool_size])

        async def _fast_save_file(path, file_id=None, file_part=0, progress=None, progress_args=()):
            if (
                not getattr(Config, "USE_HYPER", True)
                or file_id is not None
                or file_part != 0
                or not isinstance(path, (str, os.PathLike))
            ):
                return await orig_save_file(
                    path, file_id=file_id, file_part=file_part, progress=progress, progress_args=progress_args
                )
            file_path = os.fspath(path)
            try:
                if not os.path.isfile(file_path):
                    return await orig_save_file(
                        path, file_id=file_id, file_part=file_part, progress=progress, progress_args=progress_args
                    )
                file_size = os.path.getsize(file_path)
            except OSError:
                return await orig_save_file(
                    path, file_id=file_id, file_part=file_part, progress=progress, progress_args=progress_args
                )

            # Use multi-session parallel big-file upload for files >= 10 MiB
            if file_size < 10 * 1024 * 1024:
                return await orig_save_file(
                    path, file_id=file_id, file_part=file_part, progress=progress, progress_args=progress_args
                )

            try:
                pool_size = 3 if file_size < 256 * 1024 * 1024 else 4
                sessions = await _get_media_sessions(pool_size=pool_size)
                if not sessions:
                    return await orig_save_file(
                        path, file_id=file_id, file_part=file_part, progress=progress, progress_args=progress_args
                    )
            except Exception as exc:
                LOGGER.debug("Fast upload session pool fallback to native save_file: %s", exc)
                return await orig_save_file(
                    path, file_id=file_id, file_part=file_part, progress=progress, progress_args=progress_args
                )

            part_size = 512 * 1024
            total_parts = (file_size + part_size - 1) // part_size
            fid = client.rnd_id()
            queue = Queue()
            for part_idx in range(total_parts):
                queue.put_nowait(part_idx)

            uploaded_bytes = 0
            last_progress_ts = 0.0
            cancelled = False
            failed_exc = None
            fd = os.open(file_path, os.O_RDONLY)

            async def _worker(worker_id):
                nonlocal uploaded_bytes, last_progress_ts, cancelled, failed_exc
                sess = sessions[worker_id % len(sessions)]
                while not cancelled and failed_exc is None:
                    try:
                        part_idx = queue.get_nowait()
                    except Exception:
                        break
                    offset = part_idx * part_size
                    try:
                        chunk = await to_thread(os.pread, fd, part_size, offset)
                        if not chunk:
                            break
                        for attempt in range(5):
                            if cancelled or failed_exc is not None:
                                break
                            try:
                                rpc = raw.functions.upload.SaveBigFilePart(
                                    file_id=fid,
                                    file_part=part_idx,
                                    file_total_parts=total_parts,
                                    bytes=chunk,
                                )
                                await sess.invoke(rpc, sleep_threshold=getattr(client, "sleep_threshold", 60))
                                break
                            except (FloodWait, FloodPremiumWait) as fw:
                                wait_s = max(1, int(getattr(fw, "value", 1)))
                                await sleep(wait_s + 1)
                            except Exception as req_err:
                                if attempt >= 4:
                                    raise req_err
                                await sleep(min(2 ** attempt, 4))
                                replacement = await _replace_media_session(sess)
                                if replacement is not None:
                                    sess = replacement
                        if cancelled or failed_exc is not None:
                            break
                        uploaded_bytes = min(file_size, uploaded_bytes + len(chunk))
                        now = time.monotonic()
                        if callable(progress) and (
                            uploaded_bytes >= file_size or (now - last_progress_ts) >= 0.2
                        ):
                            last_progress_ts = now
                            try:
                                res = progress(uploaded_bytes, file_size, *progress_args)
                                if inspect.isawaitable(res):
                                    await res
                            except StopTransmission:
                                cancelled = True
                                break
                            except Exception:
                                pass
                    except StopTransmission:
                        cancelled = True
                        break
                    except Exception as worker_err:
                        failed_exc = worker_err
                        break
                    finally:
                        queue.task_done()

            num_workers = min(total_parts, len(sessions) * 3)
            workers = [create_task(_worker(i)) for i in range(num_workers)]
            try:
                await gather(*workers, return_exceptions=True)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

            if cancelled:
                raise StopTransmission
            if failed_exc is not None:
                LOGGER.warning("Fast upload worker error (%s); retrying with native wzgram save_file", failed_exc)
                await _close_media_sessions()
                return await orig_save_file(
                    path, file_id=file_id, file_part=file_part, progress=progress, progress_args=progress_args
                )

            if callable(progress):
                with suppress(StopTransmission, Exception):
                    res = progress(file_size, file_size, *progress_args)
                    if inspect.isawaitable(res):
                        await res

            return raw.types.InputFileBig(
                id=fid,
                parts=total_parts,
                name=os.path.basename(file_path),
            )

        client._orig_save_file = orig_save_file
        client._close_fast_upload_sessions = _close_media_sessions
        client.save_file = _fast_save_file
        client._wz_fast_upload_installed = True
        return client

    @classmethod
    def wztgClient(cls, *args, proxy=None, **kwargs):
        kwargs["api_id"] = Config.TELEGRAM_API
        kwargs["api_hash"] = Config.TELEGRAM_HASH
        kwargs["proxy"] = (Config.TG_PROXY if proxy is None else proxy) or None
        kwargs["parse_mode"] = enums.ParseMode.HTML
        # Use persistent SQLite storage in a dedicated writable directory.
        # Do not point sessions at a container-only absolute path.
        kwargs.setdefault("workdir", str(SESSION_STORAGE_DIR))
        kwargs["in_memory"] = False
        for param, value in {
            "max_concurrent_transmissions": int(os.environ.get("WZGRAM_MAX_CONCURRENT", "2")),
            "skip_updates": False,
        }.items():
            if param in signature(Client.__init__).parameters:
                kwargs[param] = value
        return Client(*args, **kwargs)

    @classmethod
    def _parse_proxies(cls, raw):
        if not raw:
            return []
        proxies = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                proxies.append(None)
                continue
            try:
                parsed = literal_eval(line)
                proxies.append(cls.sanitize_proxy(parsed) if isinstance(parsed, dict) else None)
            except (ValueError, SyntaxError):
                proxies.append(None)
        return proxies

    @classmethod
    async def _retry_hclient(cls, no, b_token, delay, proxy=None):
        await sleep(delay)
        try:
            hbot = cls.wztgClient(
                f"WZ-HBot{no}",
                bot_token=b_token,
                no_updates=True,
                proxy=proxy,
            )
            await hbot.start()
            LOGGER.info(f"Helper Bot [@{hbot.me.username}] Started!")
            cls.helper_bots[no], cls.helper_loads[no] = hbot, 0
        except FloodWait as e:
            LOGGER.warning(f"Helper Bot{no} FloodWait: Retrying in {e.value}s...")
            bot_loop.create_task(cls._retry_hclient(no, b_token, e.value, proxy))
        except Exception as e:
            LOGGER.error(f"Failed to start helper bot {no} from HELPER_TOKENS. {e}")

    @classmethod
    async def start_hclient(cls, no, b_token, proxy=None):
        try:
            hbot = cls.wztgClient(
                f"WZ-HBot{no}",
                bot_token=b_token,
                no_updates=True,
                proxy=proxy,
            )
            await hbot.start()
            LOGGER.info(f"Helper Bot [@{hbot.me.username}] Started!")
            cls.helper_bots[no], cls.helper_loads[no] = hbot, 0
        except FloodWait as e:
            LOGGER.warning(
                f"Helper Bot{no} FloodWait: Retrying in {e.value}s (non-blocking)..."
            )
            bot_loop.create_task(cls._retry_hclient(no, b_token, e.value, proxy))
        except Exception as e:
            LOGGER.error(f"Failed to start helper bot {no} from HELPER_TOKENS. {e}")
            cls.helper_bots.pop(no, None)

    @classmethod
    async def start_helper_bots(cls):
        if not Config.HELPER_TOKENS:
            return
        tokens = list(dict.fromkeys(t.strip() for t in re.split(r"[,\s]+", str(Config.HELPER_TOKENS)) if t.strip()))
        if not tokens:
            return
        LOGGER.info("Generating helper client from HELPER_TOKENS")
        bot_proxies = cls._parse_proxies(Config.HELPER_BOT_PROXIES)
        async with cls._hlock:
            await gather(
                *(
                    cls.start_hclient(
                        no,
                        b_token,
                        bot_proxies[no - 1]
                        if bot_proxies and no - 1 < len(bot_proxies)
                        else None,
                    )
                    for no, b_token in enumerate(tokens, start=1)
                )
            )

    @classmethod
    async def _retry_sclient(cls, no, b_token, delay, proxy=None):
        await sleep(delay)
        try:
            sbot = cls.wztgClient(
                f"WZ-SBot{no}",
                bot_token=b_token,
                no_updates=True,
                proxy=proxy,
            )
            await sbot.start()
            LOGGER.info(f"Stream Bot [@{sbot.me.username}] Started!")
            cls.stream_bots[no], cls.stream_loads[no] = sbot, 0
        except FloodWait as e:
            LOGGER.warning(f"Stream Bot{no} FloodWait: Retrying in {e.value}s...")
            bot_loop.create_task(cls._retry_sclient(no, b_token, e.value, proxy))
        except Exception as e:
            LOGGER.error(f"Failed to start stream bot {no} from STREAM_TOKENS. {e}")

    @classmethod
    async def start_sclient(cls, no, b_token, proxy=None):
        try:
            sbot = cls.wztgClient(
                f"WZ-SBot{no}",
                bot_token=b_token,
                no_updates=True,
                proxy=proxy,
            )
            await sbot.start()
            LOGGER.info(f"Stream Bot [@{sbot.me.username}] Started!")
            cls.stream_bots[no], cls.stream_loads[no] = sbot, 0
        except FloodWait as e:
            LOGGER.warning(
                f"Stream Bot{no} FloodWait: Retrying in {e.value}s (non-blocking)..."
            )
            bot_loop.create_task(cls._retry_sclient(no, b_token, e.value, proxy))
        except Exception as e:
            LOGGER.error(f"Failed to start stream bot {no} from STREAM_TOKENS. {e}")
            cls.stream_bots.pop(no, None)

    @classmethod
    async def start_stream_bots(cls):
        if not Config.STREAM_TOKENS:
            return
        tokens = [t.strip() for t in re.split(r"[,\s]+", Config.STREAM_TOKENS) if t.strip()]
        if not tokens:
            return
        LOGGER.info("Generating stream client from STREAM_TOKENS")
        async with cls._slock:
            await gather(
                *(
                    cls.start_sclient(no, b_token)
                    for no, b_token in enumerate(tokens, start=1)
                )
            )

    @classmethod
    async def _retry_huser(cls, no, session_string, delay, proxy=None):
        await sleep(delay)
        try:
            huser = cls.wztgClient(
                f"WZ-HUser{no}",
                session_string=session_string,
                sleep_threshold=60,
                no_updates=True,
                proxy=proxy,
            )
            await huser.start()
            me = huser.me or await huser.get_me()
            huser._is_premium_cached = bool(getattr(me, "is_premium", False))
            uname = me.username or me.first_name
            LOGGER.info(f"Helper User [{uname}] Started! Premium={huser._is_premium_cached}")
            cls.helper_users[no], cls.helper_user_loads[no] = huser, 0
            if huser._is_premium_cached and not cls.IS_PREMIUM_USER:
                cls.IS_PREMIUM_USER = True
                cls.MAX_SPLIT_SIZE = TELEGRAM_PREMIUM_MAX_BYTES
            bot_loop.create_task(cls.warm_peer_cache(huser))
        except FloodWait as e:
            LOGGER.warning(f"Helper User{no} FloodWait: Retrying in {e.value}s...")
            bot_loop.create_task(cls._retry_huser(no, session_string, e.value, proxy))
        except Exception as e:
            LOGGER.error(f"Failed to start helper user {no} from HELPER_STRINGS. {e}")

    @classmethod
    async def start_huser(cls, no, session_string, proxy=None):
        try:
            huser = cls.wztgClient(
                f"WZ-HUser{no}",
                session_string=session_string,
                sleep_threshold=60,
                no_updates=True,
                proxy=proxy,
            )
            await huser.start()
            me = huser.me or await huser.get_me()
            huser._is_premium_cached = bool(getattr(me, "is_premium", False))
            uname = me.username or me.first_name
            LOGGER.info(f"Helper User [{uname}] Started! Premium={huser._is_premium_cached}")
            cls.helper_users[no], cls.helper_user_loads[no] = huser, 0
            if huser._is_premium_cached and not cls.IS_PREMIUM_USER:
                cls.IS_PREMIUM_USER = True
                cls.MAX_SPLIT_SIZE = TELEGRAM_PREMIUM_MAX_BYTES
            bot_loop.create_task(cls.warm_peer_cache(huser))
        except FloodWait as e:
            LOGGER.warning(
                f"Helper User{no} FloodWait: Retrying in {e.value}s (non-blocking)..."
            )
            bot_loop.create_task(cls._retry_huser(no, session_string, e.value, proxy))
        except Exception as e:
            LOGGER.error(f"Failed to start helper user {no} from HELPER_STRINGS. {e}")
            cls.helper_users.pop(no, None)

    @classmethod
    async def start_helper_users(cls):
        if not Config.HELPER_STRINGS:
            return
        sessions = [s.strip() for s in re.split(r"[,\s]+", Config.HELPER_STRINGS) if s.strip()]
        if not sessions:
            return
        LOGGER.info("Generating helper client from HELPER_STRINGS")
        user_proxies = cls._parse_proxies(Config.HELPER_USER_PROXIES)
        async with cls._ulock:
            await gather(
                *(
                    cls.start_huser(
                        no,
                        session_string,
                        user_proxies[no - 1]
                        if user_proxies and no - 1 < len(user_proxies)
                        else None,
                    )
                    for no, session_string in enumerate(sessions, start=1)
                )
            )

    @classmethod
    async def start_bot(cls):
        LOGGER.info("Generating client from BOT_TOKEN")
        cls.ID = Config.BOT_TOKEN.split(":", 1)[0]
        cls.PARTITION = db_partition_id(cls.ID)
        cls.bot = cls.wztgClient(
            f"WZ-Bot{cls.ID}",
            bot_token=Config.BOT_TOKEN,
            workdir=str(SESSION_STORAGE_DIR),
        )
        while True:
            try:
                await cls.bot.start()
                break
            except FloodWait as e:
                LOGGER.warning(f"FloodWait: Sleeping for {e.value} seconds...")
                await sleep(e.value)
        cls.BNAME = cls.bot.me.username
        cls.ID = Config.BOT_TOKEN.split(":", 1)[0]
        LOGGER.info(f"WZ Bot : [@{cls.BNAME}] Started!")

    @classmethod
    def _sync_split_size_with_premium(cls):
        if cls.IS_PREMIUM_USER:
            cls.MAX_SPLIT_SIZE = TELEGRAM_PREMIUM_MAX_BYTES
            if getattr(Config, "LEECH_SPLIT_SIZE", 0) in (
                0,
                None,
                2_000_000_000,
                BOT_MAX_BYTES,
                4_000_000_000,
            ) or int(getattr(Config, "LEECH_SPLIT_SIZE", 0) or 0) > TELEGRAM_PREMIUM_MAX_BYTES:
                Config.LEECH_SPLIT_SIZE = TELEGRAM_PREMIUM_MAX_BYTES
        else:
            cls.MAX_SPLIT_SIZE = BOT_MAX_BYTES
            if (
                not getattr(Config, "LEECH_SPLIT_SIZE", 0)
                or int(getattr(Config, "LEECH_SPLIT_SIZE", 0) or 0) > BOT_MAX_BYTES
                or int(getattr(Config, "LEECH_SPLIT_SIZE", 0) or 0) == 2_000_000_000
            ):
                Config.LEECH_SPLIT_SIZE = BOT_MAX_BYTES

    @classmethod
    async def _retry_user(cls, delay):
        await sleep(delay)
        try:
            cls.user = cls.wztgClient(
                "WZ-User",
                session_string=Config.USER_SESSION_STRING.strip(),
                sleep_threshold=60,
                no_updates=True,
            )
            await cls.user.start()
            me = cls.user.me or await cls.user.get_me()
            cls.IS_PREMIUM_USER = bool(getattr(me, "is_premium", False))
            cls.user._is_premium_cached = cls.IS_PREMIUM_USER
            cls._sync_split_size_with_premium()
            uname = me.username or me.first_name
            LOGGER.info(
                "WZ User : [%s] Started! Premium=%s upload_limit=%s bytes",
                uname, cls.IS_PREMIUM_USER, cls.MAX_SPLIT_SIZE,
            )
            bot_loop.create_task(cls.warm_peer_cache(cls.user))
        except FloodWait as e:
            LOGGER.warning(f"User client FloodWait: Retrying in {e.value}s...")
            bot_loop.create_task(cls._retry_user(e.value))
        except Exception as e:
            LOGGER.error(f"Failed to start client from USER_SESSION_STRING. {e}")
            cls.IS_PREMIUM_USER = False
            cls.MAX_SPLIT_SIZE = BOT_MAX_BYTES
            cls.user = None

    @classmethod
    async def start_user(cls):
        session_str = str(getattr(Config, "USER_SESSION_STRING", "") or "").strip()
        if session_str:
            LOGGER.info("Generating client from USER_SESSION_STRING")
            try:
                if cls.user is not None:
                    with suppress(Exception):
                        if hasattr(cls.user, "_close_fast_upload_sessions"):
                            await cls.user._close_fast_upload_sessions()
                        await cls.user.stop()
                cls.user = cls.wztgClient(
                    "WZ-User",
                    session_string=session_str,
                    sleep_threshold=60,
                    no_updates=True,
                )
                await cls.user.start()
                me = cls.user.me or await cls.user.get_me()
                cls.IS_PREMIUM_USER = bool(getattr(me, "is_premium", False))
                cls.user._is_premium_cached = cls.IS_PREMIUM_USER
                cls._sync_split_size_with_premium()
                uname = me.username or me.first_name
                LOGGER.info(
                    "WZ User : [%s] Started! Premium=%s upload_limit=%s bytes",
                    uname, cls.IS_PREMIUM_USER, cls.MAX_SPLIT_SIZE,
                )
                bot_loop.create_task(cls.warm_peer_cache(cls.user))
            except FloodWait as e:
                LOGGER.warning(
                    f"User client FloodWait: Retrying in {e.value}s (non-blocking)..."
                )
                bot_loop.create_task(cls._retry_user(e.value))
            except Exception as e:
                LOGGER.error(f"Failed to start client from USER_SESSION_STRING. {e}")
                cls.IS_PREMIUM_USER = False
                cls.MAX_SPLIT_SIZE = BOT_MAX_BYTES
                cls.user = None
        else:
            if cls.user is not None:
                with suppress(Exception):
                    if hasattr(cls.user, "_close_fast_upload_sessions"):
                        await cls.user._close_fast_upload_sessions()
                    await cls.user.stop()
            cls.user = None
            cls.IS_PREMIUM_USER = any(
                cls.is_client_premium(u) for u in (cls.helper_users or {}).values()
            )
            cls._sync_split_size_with_premium()

    @classmethod
    async def ensure_user_client(cls):
        """Ensure TgClient.user is connected when USER_SESSION_STRING is configured."""
        session_str = str(getattr(Config, "USER_SESSION_STRING", "") or "").strip()
        if not session_str:
            return None
        if cls.user is not None and getattr(cls.user, "is_connected", False):
            return cls.user
        async with cls._ulock:
            if cls.user is not None and getattr(cls.user, "is_connected", False):
                return cls.user
            await cls.start_user()
            return cls.user

    @classmethod
    async def get_personal_user(cls, user_id, session_string):
        """Return a cached per-user Telegram user client for the optional personal session."""
        if not session_string or not str(session_string).strip():
            return None
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return None
        session_clean = str(session_string).strip()
        existing = cls.personal_users.get(user_id)
        if (
            existing
            and getattr(existing, "is_connected", False)
            and getattr(existing, "_session_str_hash", None) == sha256(session_clean.encode("utf-8")).hexdigest()
        ):
            return existing
        lock = cls._personal_locks.setdefault(user_id, Lock())
        async with lock:
            existing = cls.personal_users.get(user_id)
            sess_hash = sha256(session_clean.encode("utf-8")).hexdigest()
            if (
                existing
                and getattr(existing, "is_connected", False)
                and getattr(existing, "_session_str_hash", None) == sess_hash
            ):
                return existing
            if existing:
                with suppress(Exception):
                    if hasattr(existing, "_close_fast_upload_sessions"):
                        await existing._close_fast_upload_sessions()
                    await existing.stop()
                cls.personal_users.pop(user_id, None)
            name = f"WZ-PUser-{user_id}"
            client = cls.wztgClient(
                name,
                session_string=session_clean,
                sleep_threshold=60,
                no_updates=True,
            )
            try:
                await client.start()
                me = await client.get_me()
                client.me = me
                client._is_premium_cached = bool(getattr(me, "is_premium", False))
                client._session_str_hash = sess_hash
                cls.personal_users[user_id] = client
                LOGGER.info(
                    "Personal user session [%s] started as @%s (Premium=%s)",
                    user_id,
                    me.username or me.first_name,
                    client._is_premium_cached,
                )
                bot_loop.create_task(cls.warm_peer_cache(client))
                return client
            except Exception as e:
                LOGGER.error(f"Failed to start personal user session [{user_id}]: {e}")
                with suppress(Exception):
                    if hasattr(client, "_close_fast_upload_sessions"):
                        await client._close_fast_upload_sessions()
                    await client.stop()
                cls.personal_users.pop(user_id, None)
                return None

    @classmethod
    async def stop_personal_user(cls, user_id):
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return
        client = cls.personal_users.pop(user_id, None)
        if client:
            with suppress(Exception):
                if hasattr(client, "_close_fast_upload_sessions"):
                    await client._close_fast_upload_sessions()
                await client.stop()

    @classmethod
    async def reset_personal_user(cls, user_id):
        await cls.stop_personal_user(user_id)
        cls._personal_locks.pop(int(user_id), None)

    @classmethod
    async def get_user_bots(cls, user_id, bot_tokens):
        """Start valid user bots and cache them by token, not list position.

        Index-based cache keys can accidentally reuse Bot 2's live client as
        Bot 1 after a token is deleted/reordered. Token fingerprints keep each
        connected client tied to the exact token that created it.
        """
        if not isinstance(bot_tokens, list):
            return {}
        user_id = int(user_id)
        res = {}
        valid_keys = set()
        async with cls._hlock:
            for idx, raw_token in enumerate(bot_tokens):
                token = raw_token.strip() if isinstance(raw_token, str) else ""
                if not token or ":" not in token or len(token) < 20:
                    continue
                fingerprint = sha256(token.encode("utf-8")).hexdigest()
                key = (user_id, fingerprint)
                valid_keys.add(key)
                cli = cls.user_bots.get(key)
                if cli is not None and getattr(cli, "is_connected", False):
                    res[idx] = cli
                    continue
                if cli is not None:
                    cls.user_bots.pop(key, None)
                    with suppress(Exception):
                        if hasattr(cli, "_close_fast_upload_sessions"):
                            await cli._close_fast_upload_sessions()
                        await cli.stop()
                try:
                    cli = cls.wztgClient(
                        f"WZ-UserBot-{user_id}-{fingerprint[:12]}",
                        bot_token=token,
                        no_updates=True,
                    )
                    await cli.start()
                    cls.user_bots[key] = cli
                    res[idx] = cli
                except Exception as e:
                    LOGGER.error(
                        "Failed to start user bot token %s for user %s: %s",
                        idx + 1, user_id, e,
                    )
            # Stop stale cached clients when tokens are removed or replaced.
            for key, cli in list(cls.user_bots.items()):
                if key[0] == user_id and key not in valid_keys:
                    cls.user_bots.pop(key, None)
                    with suppress(Exception):
                        if hasattr(cli, "_close_fast_upload_sessions"):
                            await cli._close_fast_upload_sessions()
                        await cli.stop()
        return res

    @classmethod
    async def stop(cls):
        async with cls._lock:
            clients = []
            for c in [cls.bot, cls.user, *cls.helper_bots.values(), *cls.helper_users.values(), *cls.stream_bots.values(), *cls.personal_users.values()]:
                if c and hasattr(c, "_close_fast_upload_sessions"):
                    with suppress(Exception):
                        await c._close_fast_upload_sessions()
            if cls.bot:
                clients.append(cls.bot.stop())
                cls.bot = None
            if cls.user:
                clients.append(cls.user.stop())
                cls.user = None
            if cls.helper_bots:
                clients.extend(h_bot.stop() for h_bot in cls.helper_bots.values())
                cls.helper_bots = {}
            if cls.helper_users:
                clients.extend(h_user.stop() for h_user in cls.helper_users.values())
                cls.helper_users = {}
            if cls.stream_bots:
                clients.extend(s_bot.stop() for s_bot in cls.stream_bots.values())
                cls.stream_bots = {}
            if cls.personal_users:
                clients.extend(p_user.stop() for p_user in cls.personal_users.values())
                cls.personal_users = {}
            if clients:
                await gather(*clients, return_exceptions=True)
            LOGGER.info("All Client(s) stopped")

    @classmethod
    async def reload(cls):
        async with cls._lock:
            for c in [cls.bot, cls.user, *cls.helper_bots.values(), *cls.helper_users.values(), *cls.stream_bots.values()]:
                if c and hasattr(c, "_close_fast_upload_sessions"):
                    with suppress(Exception):
                        await c._close_fast_upload_sessions()
            await cls.bot.restart()
            if cls.user:
                await cls.user.restart()
                bot_loop.create_task(cls.warm_peer_cache(cls.user))
            if cls.helper_bots:
                await gather(*[h_bot.restart() for h_bot in cls.helper_bots.values()])
            if cls.helper_users:
                await gather(
                    *[h_user.restart() for h_user in cls.helper_users.values()]
                )
            if cls.stream_bots:
                await gather(*[s_bot.restart() for s_bot in cls.stream_bots.values()])
            LOGGER.info("All Client(s) restarted")
