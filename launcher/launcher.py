#!/usr/bin/env python3
"""
Bibs Account Manager Launcher
Checks GitHub for latest release, downloads updates, installs DreamBot script,
and launches the manager.
"""
import os
import sys
import json
import shutil
import zipfile
import subprocess
import urllib.request
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────
GITHUB_API = "https://api.github.com/repos/reeganbannister311-star/Bibs-Account-Manager/releases/latest"
ZIP_ASSET_NAME = "BibsAccountManager.zip"
JAR_ASSET_NAME = "BankCacheBootstrapper.jar"
EXE_NAME = "BibsAccountManager.exe"

# Paths relative to launcher.exe
LAUNCHER_DIR = Path(__file__).parent.resolve()
VERSION_FILE = LAUNCHER_DIR / "version.txt"
APP_DIR = LAUNCHER_DIR / "App"
EXE_PATH = APP_DIR / EXE_NAME

# DreamBot scripts folder
DREAMBOT_SCRIPTS = Path(os.path.expandvars(r"%USERPROFILE%\DreamBot\Scripts"))

# ── Helpers ────────────────────────────────────────────────────────
def read_local_version() -> str:
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "0.0.0"


def write_local_version(tag: str) -> None:
    VERSION_FILE.write_text(tag)


def fetch_latest_release() -> dict | None:
    req = urllib.request.Request(
        GITHUB_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "BibsLauncher/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[LAUNCHER] Failed to check GitHub release: {e}")
        return None


def find_asset(assets: list, name: str) -> dict | None:
    for a in assets:
        if a.get("name") == name:
            return a
    return None


def download_file(url: str, dest: Path, desc: str = "file") -> bool:
    print(f"[LAUNCHER] Downloading {desc} ...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BibsLauncher/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
        print(f"[LAUNCHER] Downloaded {desc} -> {dest}")
        return True
    except Exception as e:
        print(f"[LAUNCHER] Download failed for {desc}: {e}")
        return False


def extract_zip(zip_path: Path, dest_dir: Path) -> bool:
    print(f"[LAUNCHER] Extracting {zip_path.name} ...")
    try:
        if dest_dir.exists():
            # Remove old App folder contents to avoid stale files
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(dest_dir)
        print(f"[LAUNCHER] Extracted to {dest_dir}")
        return True
    except Exception as e:
        print(f"[LAUNCHER] Extract failed: {e}")
        return False


def install_jar(jar_path: Path) -> bool:
    print(f"[LAUNCHER] Installing DreamBot script ...")
    try:
        DREAMBOT_SCRIPTS.mkdir(parents=True, exist_ok=True)
        dest = DREAMBOT_SCRIPTS / JAR_ASSET_NAME
        shutil.copy2(jar_path, dest)
        print(f"[LAUNCHER] Installed JAR -> {dest}")
        return True
    except Exception as e:
        print(f"[LAUNCHER] JAR install failed: {e}")
        return False


def launch_manager() -> None:
    if not EXE_PATH.exists():
        print(f"[LAUNCHER] ERROR: Manager not found at {EXE_PATH}")
        input("Press Enter to exit...")
        sys.exit(1)

    print(f"[LAUNCHER] Starting {EXE_PATH.name} ...")
    subprocess.Popen([str(EXE_PATH)], cwd=str(APP_DIR))
    sys.exit(0)


# ── Main ───────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  Bibs Account Manager Launcher")
    print("=" * 60)

    local_version = read_local_version()
    print(f"[LAUNCHER] Installed version: {local_version}")

    release = fetch_latest_release()
    if not release:
        print("[LAUNCHER] Could not reach GitHub. Launching existing install ...")
        launch_manager()

    remote_tag = release.get("tag_name", "0.0.0").lstrip("v")
    print(f"[LAUNCHER] Latest release: v{remote_tag}")

    needs_update = remote_tag != local_version
    if not needs_update:
        print("[LAUNCHER] Already up to date.")
        launch_manager()

    print(f"[LAUNCHER] Update available: {local_version} -> {remote_tag}")
    assets = release.get("assets", [])

    # 1) Download & extract manager ZIP
    zip_asset = find_asset(assets, ZIP_ASSET_NAME)
    if not zip_asset:
        print(f"[LAUNCHER] WARNING: Asset '{ZIP_ASSET_NAME}' not found in release.")
    else:
        zip_path = LAUNCHER_DIR / ZIP_ASSET_NAME
        if download_file(zip_asset["browser_download_url"], zip_path, ZIP_ASSET_NAME):
            if extract_zip(zip_path, APP_DIR):
                zip_path.unlink(missing_ok=True)

    # 2) Download & install DreamBot JAR
    jar_asset = find_asset(assets, JAR_ASSET_NAME)
    if not jar_asset:
        print(f"[LAUNCHER] WARNING: Asset '{JAR_ASSET_NAME}' not found in release.")
    else:
        jar_path = LAUNCHER_DIR / JAR_ASSET_NAME
        if download_file(jar_asset["browser_download_url"], jar_path, JAR_ASSET_NAME):
            install_jar(jar_path)
            jar_path.unlink(missing_ok=True)

    # 3) Bump version
    write_local_version(remote_tag)
    print(f"[LAUNCHER] Updated to v{remote_tag}")

    launch_manager()


if __name__ == "__main__":
    main()
