from functools import wraps
from importlib import import_module
from time import time
from uuid import uuid4

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError
from pymongo.server_api import ServerApi

from ... import LOGGER, qbit_options, rss_dict, user_data
from ...core.config_manager import Config
from ...core.tg_client import TgClient, db_partition_id


def _bot_id():
    if TgClient.ID:
        return str(TgClient.ID)
    return Config.BOT_TOKEN.split(":", 1)[0]


def _expired(doc):
    exp = doc.get("exp")
    return bool(exp) and time() >= exp


def _part():
    if not TgClient.PARTITION:
        TgClient.PARTITION = db_partition_id(_bot_id())
    return TgClient.PARTITION


def _new_uuid():
    return uuid4().hex


def _safe_db(default_return=None):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            if self._return or self.db is None:
                return default_return() if callable(default_return) else default_return
            try:
                return await func(self, *args, **kwargs)
            except Exception as e:
                LOGGER.warning(f"MongoDB operation {func.__name__} failed ({e}); skipping.")
                return default_return() if callable(default_return) else default_return

        return wrapper

    return decorator


class DbManager:
    def __init__(self):
        self._return = True
        self._conn = None
        self.db = None

    async def connect(self):
        try:
            if self._conn is not None:
                try:
                    await self._conn.close()
                except Exception:
                    pass
            database_url = str(Config.DATABASE_URL or "").strip()
            if not database_url:
                self.db = None
                self._return = True
                self._conn = None
                LOGGER.error("DATABASE_URL is empty; MongoDB persistence is disabled.")
                return
            if not database_url.startswith(("mongodb://", "mongodb+srv://")):
                raise ValueError("DATABASE_URL must start with mongodb:// or mongodb+srv://")
            self._conn = AsyncMongoClient(
                database_url,
                server_api=ServerApi("1"),
                serverSelectionTimeoutMS=6000,
                connectTimeoutMS=6000,
                socketTimeoutMS=10000,
            )
            await self._conn.admin.command("ping")
            self.db = self._conn.wzmlx
            self._return = False
            LOGGER.info("Connected to MongoDB successfully.")
        except (PyMongoError, Exception) as e:
            LOGGER.warning(
                f"MongoDB connection failed or timed out ({e}). Continuing with local config.py settings in memory."
            )
            if self._conn is not None:
                try:
                    await self._conn.close()
                except Exception:
                    pass
            self.db = None
            self._return = True
            self._conn = None


    async def flush_all(self):
        """Persist all in-memory user and RSS records before shutdown/restart."""
        if self._return or self.db is None:
            LOGGER.warning("Skipping database flush: MongoDB is not connected.")
            return False
        failed = False
        for user_id in list(user_data):
            try:
                await self.update_user_data(user_id)
            except Exception as exc:
                failed = True
                LOGGER.error("Could not persist user data for %s: %s", user_id, exc)
        try:
            await self.rss_update_all()
        except Exception as exc:
            failed = True
            LOGGER.error("Could not persist RSS data: %s", exc)
        if failed:
            LOGGER.error("Database flush completed with errors; check the preceding logs.")
            return False
        LOGGER.info("Database flush completed: user settings and RSS data saved.")
        return True

    async def disconnect(self):
        self._return = True
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
        self._conn = None
        self.db = None

    @_safe_db()
    async def update_deploy_config(self):
        settings = import_module("config")
        config_file = {
            key: value.strip() if isinstance(value, str) else value
            for key, value in vars(settings).items()
            if not key.startswith("__")
        }
        await self.db.settings.deployConfig.replace_one(
            {"_id": _part()}, config_file, upsert=True
        )

    @_safe_db()
    async def update_config(self, dict_):
        await self.db.settings.config.update_one(
            {"_id": _part()}, {"$set": dict_}, upsert=True
        )

    @_safe_db()
    async def update_aria2(self, key, value):
        await self.db.settings.aria2c.update_one(
            {"_id": _part()}, {"$set": {key: value}}, upsert=True
        )

    @_safe_db()
    async def update_qbittorrent(self, key, value):
        await self.db.settings.qbittorrent.update_one(
            {"_id": _part()}, {"$set": {key: value}}, upsert=True
        )

    @_safe_db()
    async def save_qbit_settings(self):
        await self.db.settings.qbittorrent.update_one(
            {"_id": _part()}, {"$set": qbit_options}, upsert=True
        )

    @_safe_db()
    async def update_private_file(self, path):
        db_path = path.replace(".", "__")
        if await aiopath.exists(path):
            async with aiopen(path, "rb+") as pf:
                pf_bin = await pf.read()
            await self.db.settings.files.update_one(
                {"_id": _part()}, {"$set": {db_path: pf_bin}}, upsert=True
            )
            if path == "config.py":
                await self.update_deploy_config()
        else:
            await self.db.settings.files.update_one(
                {"_id": _part()}, {"$unset": {db_path: ""}}, upsert=True
            )

    @_safe_db()
    async def update_nzb_config(self):
        if not await aiopath.exists("configs/sabnzbd/SABnzbd.ini"):
            return
        async with aiopen("configs/sabnzbd/SABnzbd.ini", "rb+") as pf:
            nzb_conf = await pf.read()
        await self.db.settings.nzb.replace_one(
            {"_id": _part()}, {"SABnzbd__ini": nzb_conf}, upsert=True
        )

    @_safe_db()
    async def update_user_data(self, user_id):
        data = user_data.get(user_id, {})
        data = data.copy()
        for key in (
            "THUMBNAIL",
            "RCLONE_CONFIG",
            "TOKEN_PICKLE",
            "USER_COOKIE_FILE",
            "INTRO_SUB_IMAGE",
        ):
            data.pop(key, None)
        pipeline = [
            {
                "$replaceRoot": {
                    "newRoot": {
                        "$mergeObjects": [
                            data,
                            {
                                "$arrayToObject": {
                                    "$filter": {
                                        "input": {"$objectToArray": "$$ROOT"},
                                        "as": "field",
                                        "cond": {
                                            "$in": [
                                                "$$field.k",
                                                [
                                                    "THUMBNAIL",
                                                    "RCLONE_CONFIG",
                                                    "TOKEN_PICKLE",
                                                    "USER_COOKIE_FILE",
                                                    "INTRO_SUB_IMAGE",
                                                ],
                                            ]
                                        },
                                    }
                                }
                            },
                        ]
                    }
                }
            }
        ]
        await self.db.users[_part()].update_one({"_id": user_id}, pipeline, upsert=True)

    @_safe_db()
    async def update_user_doc(self, user_id, key, path=""):
        if path:
            async with aiopen(path, "rb+") as doc:
                doc_bin = await doc.read()
            await self.db.users[_part()].update_one(
                {"_id": user_id}, {"$set": {key: doc_bin}}, upsert=True
            )
        else:
            await self.db.users[_part()].update_one(
                {"_id": user_id}, {"$unset": {key: ""}}, upsert=True
            )

    @_safe_db()
    async def rss_update_all(self):
        for user_id in list(rss_dict.keys()):
            await self.db.rss[_part()].replace_one(
                {"_id": user_id}, rss_dict[user_id], upsert=True
            )

    @_safe_db()
    async def rss_update(self, user_id):
        await self.db.rss[_part()].replace_one(
            {"_id": user_id}, rss_dict[user_id], upsert=True
        )

    @_safe_db()
    async def rss_delete(self, user_id):
        await self.db.rss[_part()].delete_one({"_id": user_id})

    @_safe_db()
    async def add_incomplete_task(
        self, cid, link, tag, command="", user_id=0, reply_to_msg_id=0, dump_msg_id=0
    ):
        await self.db.tasks[_part()].update_one(
            {"link": link},
            {
                "$setOnInsert": {
                    "_id": _new_uuid(),
                    "cid": cid,
                    "tag": tag,
                    "link": link,
                    "command": command,
                    "user_id": user_id,
                    "reply_to_msg_id": reply_to_msg_id,
                    "dump_msg_id": dump_msg_id,
                }
            },
            upsert=True,
        )

    @_safe_db()
    async def update_task_dump_msg(self, link, dump_chat, dump_msg_id):
        await self.db.tasks[_part()].update_one(
            {"link": link},
            {"$set": {"dump_msg_id": dump_msg_id, "dump_chat": dump_chat}},
        )

    @_safe_db()
    async def add_stream(
        self, token, chat_id, msg_id, poster=None, exp=None, pl=None, pi=0
    ):
        doc = {"cid": int(chat_id), "mid": int(msg_id)}
        if isinstance(poster, str):
            doc["purl"] = poster
        elif poster:
            doc["pcid"] = int(poster[0])
            doc["pmid"] = int(poster[1])
        if exp:
            doc["exp"] = int(exp)
        if pl:
            doc["pl"] = pl
            doc["pi"] = int(pi)
        await self.db.streams[_part()].update_one(
            {"_id": token},
            {"$set": doc},
            upsert=True,
        )

    @_safe_db(default_return=None)
    async def get_stream_art(self, token):
        doc = await self.db.streams[_part()].find_one({"_id": token})
        if not doc:
            return None
        if _expired(doc):
            await self.rm_stream(token)
            return None
        if doc.get("purl"):
            return ("url", doc["purl"])
        if doc.get("pcid") and doc.get("pmid"):
            return ("tg", (doc["pcid"], doc["pmid"]))
        return None

    @_safe_db(default_return=None)
    async def get_stream_nav(self, token):
        doc = await self.db.streams[_part()].find_one({"_id": token})
        if doc and _expired(doc):
            await self.rm_stream(token)
            return None
        if doc and doc.get("pl"):
            return (doc["pl"], int(doc.get("pi") or 0))
        owner = await self.db.playlists[_part()].find_one({"items": token})
        if not owner:
            return None
        if _expired(owner):
            await self.rm_playlist(owner["_id"])
            return None
        items = owner.get("items") or []
        return (owner["_id"], items.index(token) if token in items else 0)

    @_safe_db(default_return=None)
    async def find_stream(self, chat_id, msg_id):
        doc = await self.db.streams[_part()].find_one(
            {"cid": int(chat_id), "mid": int(msg_id), "exp": {"$exists": False}}
        )
        return doc["_id"] if doc else None

    @_safe_db(default_return=None)
    async def get_stream(self, token):
        doc = await self.db.streams[_part()].find_one({"_id": token})
        if not doc:
            return None
        if _expired(doc):
            await self.rm_stream(token)
            return None
        return (doc["cid"], doc["mid"])

    @_safe_db()
    async def rm_stream(self, token):
        await self.db.streams[_part()].delete_one({"_id": token})

    @_safe_db()
    async def add_playlist(self, token, name, items, poster=None, exp=None):
        doc = {"name": name or "", "items": list(items)}
        doc["purl"] = poster if isinstance(poster, str) else None
        doc["pcid"] = int(poster[0]) if poster and not isinstance(poster, str) else None
        doc["pmid"] = int(poster[1]) if poster and not isinstance(poster, str) else None
        if exp:
            doc["exp"] = int(exp)
        await self.db.playlists[_part()].update_one(
            {"_id": token},
            {"$set": doc},
            upsert=True,
        )

    @_safe_db(default_return=None)
    async def get_playlist(self, token):
        doc = await self.db.playlists[_part()].find_one({"_id": token})
        if not doc:
            return None
        if _expired(doc):
            await self.rm_playlist(token)
            return None
        return {
            "name": doc.get("name") or "",
            "items": doc.get("items") or [],
            "purl": doc.get("purl"),
            "pcid": doc.get("pcid"),
            "pmid": doc.get("pmid"),
        }

    @_safe_db()
    async def rm_playlist(self, token):
        await self.db.playlists[_part()].delete_one({"_id": token})

    @_safe_db()
    async def save_plugin(self, name, state):
        doc = {k: v for k, v in (state or {}).items() if k != "_id"}
        await self.db.plugins[_part()].update_one(
            {"_id": name}, {"$set": doc}, upsert=True
        )

    @_safe_db(default_return=dict)
    async def get_plugins(self):
        out = {}
        async for doc in self.db.plugins[_part()].find({}):
            name = doc.pop("_id")
            out[name] = doc
        return out

    @_safe_db()
    async def set_plugin_enabled(self, name, enabled):
        await self.db.plugins[_part()].update_one(
            {"_id": name}, {"$set": {"enabled": bool(enabled)}}, upsert=True
        )

    @_safe_db()
    async def save_plugin_config(self, name, config):
        await self.db.plugins[_part()].update_one(
            {"_id": name}, {"$set": {"config": config or {}}}, upsert=True
        )

    @_safe_db()
    async def rm_plugin(self, name):
        await self.db.plugins[_part()].delete_one({"_id": name})

    @_safe_db(default_return=list)
    async def get_pm_uids(self):
        return [doc["_id"] async for doc in self.db.pm_users[_part()].find({})]

    @_safe_db()
    async def set_pm_users(self, user_id):
        if not bool(await self.db.pm_users[_part()].find_one({"_id": user_id})):
            await self.db.pm_users[_part()].insert_one({"_id": user_id})
            LOGGER.info(f"New PM User Added : {user_id}")

    @_safe_db()
    async def rm_pm_user(self, user_id):
        await self.db.pm_users[_part()].delete_one({"_id": user_id})

    @_safe_db()
    async def rm_complete_task(self, link):
        await self.db.tasks[_part()].delete_one({"link": link})

    @_safe_db(default_return=dict)
    async def get_incomplete_tasks(self):
        notifier_dict = {}
        if await self.db.tasks[_part()].find_one():
            rows = self.db.tasks[_part()].find({})
            async for row in rows:
                link = row.get("link") or row.get("_id")
                if not link:
                    continue
                cid = row["cid"]
                tag = row["tag"]
                task_data = {
                    "link": link,
                    "command": row.get("command", ""),
                    "user_id": row.get("user_id", 0),
                    "reply_to_msg_id": row.get("reply_to_msg_id", 0),
                    "dump_msg_id": row.get("dump_msg_id", 0),
                    "dump_chat": row.get("dump_chat", 0),
                }
                if cid in notifier_dict:
                    if tag in notifier_dict[cid]:
                        notifier_dict[cid][tag].append(task_data)
                    else:
                        notifier_dict[cid][tag] = [task_data]
                else:
                    notifier_dict[cid] = {tag: [task_data]}
        return notifier_dict

    @_safe_db()
    async def drop_incomplete_tasks(self):
        await self.db.tasks[_part()].drop()

    @_safe_db()
    async def trunc_table(self, name):
        await self.db[name][_part()].drop()


database = DbManager()
