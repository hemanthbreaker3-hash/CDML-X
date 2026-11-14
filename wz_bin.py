"""Fallback and resolver for WZML-X / CDML-X binary names (`wz_bin`).

Works across:
1. Docker images (`mysterysd/wzmlx:wzadv` + `mysterysd/wzmlx:m-tools`) where a
   compiled `wz_bin*.so` extension or custom binary names may exist.
2. Direct VPS / systemd / virtualenv deployments where standard Debian/Ubuntu
   binaries (`aria2c`, `qbittorrent-nox`, `ffmpeg`, `rclone`, `sabnzbdplus`)
   are installed.
"""

from importlib.machinery import ExtensionFileLoader
from pathlib import Path
from shutil import which
import site
import sys

_DEFAULT_BINS = (
    ("aria2c", "xaria2c"),
    ("qbittorrent-nox", "xqbittorrent-nox", "qbit"),
    ("ffmpeg", "xffmpeg"),
    ("rclone", "xrclone"),
    ("sabnzbdplus", "xsabnzbdplus", "SABnzbd.py"),
)

_NATIVE_BIN_NAME = None


def _load_native_wz_bin():
    global _NATIVE_BIN_NAME
    if _NATIVE_BIN_NAME is not None:
        return _NATIVE_BIN_NAME
    search_dirs = []
    with_site = getattr(site, "getsitepackages", None)
    if callable(with_site):
        try:
            search_dirs.extend(with_site())
        except Exception:
            pass
    for p in sys.path:
        if p and "site-packages" in p and p not in search_dirs:
            search_dirs.append(p)
    for d in search_dirs:
        dp = Path(d)
        if not dp.is_dir():
            continue
        for so_file in sorted(dp.glob("wz_bin*.so")):
            try:
                loader = ExtensionFileLoader("_wz_bin_native", str(so_file))
                mod = loader.load_module()
                fn = getattr(mod, "bin_name", None)
                if callable(fn):
                    _NATIVE_BIN_NAME = fn
                    return _NATIVE_BIN_NAME
            except Exception:
                continue
    _NATIVE_BIN_NAME = False
    return _NATIVE_BIN_NAME


def bin_name(idx: int) -> str:
    native_fn = _load_native_wz_bin()
    if callable(native_fn):
        try:
            candidate = str(native_fn(idx) or "").strip()
            if candidate and which(candidate):
                return candidate
        except Exception:
            pass
    if 0 <= idx < len(_DEFAULT_BINS):
        candidates = _DEFAULT_BINS[idx]
        for candidate in candidates:
            if which(candidate):
                return candidate
        return candidates[0]
    return "unknown"
