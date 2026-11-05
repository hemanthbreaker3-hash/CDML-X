from google.oauth2 import service_account
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from json import loads as json_loads
from logging import getLogger, ERROR
from os import path as ospath, listdir
from pickle import load as pload
from random import randrange
from re import search as re_search
from urllib.parse import parse_qs, urlparse
from tenacity import (
    retry,
    wait_exponential,
    stop_after_attempt,
    retry_if_exception_type,
)

from ....core.config_manager import Config
from ...ext_utils.links_utils import is_gdrive_id

LOGGER = getLogger(__name__)
getLogger("googleapiclient.discovery").setLevel(ERROR)


class GoogleDriveHelper:
    def __init__(self):
        self._OAUTH_SCOPE = ["https://www.googleapis.com/auth/drive"]
        self.token_path = "token.pickle"
        self.G_DRIVE_DIR_MIME_TYPE = "application/vnd.google-apps.folder"
        self.G_DRIVE_BASE_DOWNLOAD_URL = (
            "https://drive.google.com/uc?id={}&export=download"
        )
        self.G_DRIVE_DIR_BASE_DOWNLOAD_URL = "https://drive.google.com/drive/folders/{}"
        self.is_uploading = False
        self.is_downloading = False
        self.is_cloning = False
        self.sa_index = 0
        self.sa_count = 1
        self.sa_number = 100
        self.alt_auth = False
        self.service = None
        self.total_files = 0
        self.total_folders = 0
        self.file_processed_bytes = 0
        self.proc_bytes = 0
        self.total_time = 0
        self.status = None
        self.update_interval = 3
        self.use_sa = Config.USE_SERVICE_ACCOUNTS

    @property
    def speed(self):
        try:
            return self.proc_bytes / self.total_time
        except Exception:
            return 0

    @property
    def processed_bytes(self):
        return self.proc_bytes

    async def progress(self):
        if self.status is not None:
            chunk_size = (
                self.status.total_size * self.status.progress()
                - self.file_processed_bytes
            )
            self.file_processed_bytes = self.status.total_size * self.status.progress()
            self.proc_bytes += chunk_size
            self.total_time += self.update_interval

    def authorize(self):
        """Create a Drive v3 client using service-account or OAuth credentials.

        Supports global token.pickle and per-user tokens/<user_id>.pickle.
        Expired OAuth credentials are refreshed when a refresh token is present.
        """
        credentials = None
        if self.use_sa:
            accounts_dir = "accounts"
            if not ospath.isdir(accounts_dir):
                raise ValueError("Service-account mode is enabled, but accounts/ is missing.")
            json_files = sorted(
                f for f in listdir(accounts_dir) if f.lower().endswith(".json")
            )
            self.sa_number = len(json_files)
            if not json_files:
                LOGGER.error("No service account files found in accounts/")
                raise ValueError("No service account files found in accounts/")
            if self.service is None:
                self.sa_index = randrange(self.sa_number)
            elif self.sa_index >= self.sa_number:
                self.sa_index = 0
            account_path = ospath.join(accounts_dir, json_files[self.sa_index])
            LOGGER.info("Authorizing with service account %s", json_files[self.sa_index])
            credentials = service_account.Credentials.from_service_account_file(
                account_path, scopes=self._OAUTH_SCOPE
            )
        else:
            # Preserve support for the standard token and per-user token files.
            token_path = self.token_path
            if not ospath.isfile(token_path) and token_path != "token.pickle":
                LOGGER.warning("Per-user token %s is missing; checking token.pickle fallback", token_path)
                token_path = "token.pickle"
            if ospath.isfile(token_path):
                LOGGER.info("Loading Google Drive OAuth credentials from %s", token_path)
                try:
                    with open(token_path, "rb") as token_file:
                        credentials = pload(token_file)
                except Exception as exc:
                    raise ValueError(
                        f"Could not read {token_path}. Regenerate the OAuth token pickle "
                        "with the project's token generator."
                    ) from exc
            else:
                raise ValueError(
                    f"Google Drive token not found ({self.token_path} or token.pickle). "
                    "Add a valid OAuth token pickle or enable service accounts."
                )

            if credentials is not None and getattr(credentials, "expired", False):
                if getattr(credentials, "refresh_token", None):
                    try:
                        credentials.refresh(Request())
                        # Persist refreshed credentials so the next run does not reuse an expired token.
                        import pickle
                        token_dir = ospath.dirname(token_path)
                        if token_dir:
                            from os import makedirs
                            makedirs(token_dir, exist_ok=True)
                        with open(token_path, "wb") as token_file:
                            pickle.dump(credentials, token_file, protocol=pickle.HIGHEST_PROTOCOL)
                    except Exception as exc:
                        raise ValueError(
                            f"Google Drive OAuth token in {token_path} is expired and could not "
                            "be refreshed. Regenerate the token pickle and ensure OAuth access "
                            "is still authorized."
                        ) from exc

        if credentials is None or not getattr(credentials, "valid", True):
            raise ValueError(
                "Google Drive credentials are invalid. Check the OAuth token pickle or "
                "service-account JSON and regenerate credentials if needed."
            )
        return build("drive", "v3", credentials=credentials, cache_discovery=False)

    def switch_service_account(self):
        if self.sa_index == self.sa_number - 1:
            self.sa_index = 0
        else:
            self.sa_index += 1
        self.sa_count += 1
        LOGGER.info(f"Switching to {self.sa_index} index")
        self.service = self.authorize()

    def _sa_quota_switch(self, err):
        if not self.use_sa or self.sa_count >= self.sa_number:
            return False
        if not err.resp.get("content-type", "").startswith("application/json"):
            return False
        try:
            reason = json_loads(err.content)["error"]["errors"][0]["reason"]
        except Exception:
            return False
        if reason not in ("userRateLimitExceeded", "dailyLimitExceeded"):
            return False
        self.switch_service_account()
        LOGGER.info(f"Got: {reason}, Trying Again...")
        return True

    def _execute(self, build_request):
        while True:
            try:
                return build_request().execute()
            except HttpError as err:
                if not self._sa_quota_switch(err):
                    raise

    def get_id_from_url(self, link, user_id=""):
        if user_id and link.startswith("mtp:"):
            self.use_sa = False
            self.token_path = f"tokens/{user_id}.pickle"
            link = link.replace("mtp:", "", 1)
        elif link.startswith("sa:"):
            self.use_sa = True
            link = link.replace("sa:", "", 1)
        elif link.startswith("tp:"):
            self.use_sa = False
            link = link.replace("tp:", "", 1)
        if is_gdrive_id(link):
            return link
        if "folders" in link or "file" in link:
            regex = r"https:\/\/drive\.google\.com\/(?:drive(.*?)\/folders\/|file(.*?)?\/d\/)([-\w]+)"
            res = re_search(regex, link)
            if res is None:
                raise IndexError("G-Drive ID not found.")
            return res.group(3)
        parsed = urlparse(link)
        return parse_qs(parsed.query)["id"][0]

    @retry(
        wait=wait_exponential(multiplier=2, min=3, max=6),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    def set_permission(self, file_id):
        permissions = {
            "role": "reader",
            "type": "anyone",
            "value": None,
            "withLink": True,
        }
        return self._execute(
            lambda: self.service.permissions().create(
                fileId=file_id, body=permissions, supportsAllDrives=True
            )
        )

    @retry(
        wait=wait_exponential(multiplier=2, min=3, max=6),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    def add_permission_user(self, file_id, email):
        permissions = {
            "role": "reader",
            "type": "user",
            "emailAddress": email,
        }
        return self._execute(
            lambda: self.service.permissions().create(
                fileId=file_id,
                body=permissions,
                supportsAllDrives=True,
                sendNotificationEmail=False,
            )
        )

    @retry(
        wait=wait_exponential(multiplier=2, min=3, max=6),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    def get_file_metadata(self, file_id):
        return self._execute(
            lambda: self.service.files().get(
                fileId=file_id,
                supportsAllDrives=True,
                fields="name, id, mimeType, size",
            )
        )

    @retry(
        wait=wait_exponential(multiplier=2, min=3, max=6),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    def get_files_by_folder_id(self, folder_id, item_type=""):
        page_token = None
        files = []
        if not item_type:
            q = f"'{folder_id}' in parents and trashed = false"
        elif item_type == "folders":
            q = f"'{folder_id}' in parents and mimeType = '{self.G_DRIVE_DIR_MIME_TYPE}' and trashed = false"
        else:
            q = f"'{folder_id}' in parents and mimeType != '{self.G_DRIVE_DIR_MIME_TYPE}' and trashed = false"
        while True:
            response = self._execute(
                lambda: self.service.files().list(
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    q=q,
                    spaces="drive",
                    pageSize=200,
                    fields="nextPageToken, files(id, name, mimeType, size, shortcutDetails)",
                    orderBy="folder, name",
                    pageToken=page_token,
                )
            )
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if page_token is None:
                break
        return files

    @retry(
        wait=wait_exponential(multiplier=2, min=3, max=6),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    def create_directory(self, directory_name, dest_id):
        file_metadata = {
            "name": directory_name,
            "description": "Uploaded by Mirror-leech-telegram-bot",
            "mimeType": self.G_DRIVE_DIR_MIME_TYPE,
        }
        if dest_id is not None:
            file_metadata["parents"] = [dest_id]
        file = self._execute(
            lambda: self.service.files().create(
                body=file_metadata, supportsAllDrives=True
            )
        )
        file_id = file.get("id")
        if not Config.IS_TEAM_DRIVE:
            self.set_permission(file_id)
        LOGGER.info(f"Created G-Drive Folder:\nName: {file.get('name')}\nID: {file_id}")
        return file_id

    def escapes(self, estr):
        chars = ["\\", "'", '"', r"\a", r"\b", r"\f", r"\n", r"\r", r"\t"]
        for char in chars:
            estr = estr.replace(char, "\\" + char)
        return estr.strip()

    async def cancel_task(self):
        self.listener.is_cancelled = True
        if self.is_downloading:
            LOGGER.info(f"Cancelling Download: {self.listener.name}")
            await self.listener.on_download_error("Stopped by user!")
        elif self.is_cloning:
            LOGGER.info(f"Cancelling Clone: {self.listener.name}")
            await self.listener.on_upload_error(
                "your clone has been stopped and cloned data has been deleted!"
            )
        elif self.is_uploading:
            LOGGER.info(f"Cancelling Upload: {self.listener.name}")
            await self.listener.on_upload_error(
                "your upload has been stopped and uploaded data has been deleted!"
            )
