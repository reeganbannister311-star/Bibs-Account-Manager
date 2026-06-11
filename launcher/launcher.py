#!/usr/bin/env python3
"""
Bibs Farm Manager Launcher — GUI Edition
Auto-updater with dark theme, progress bar, and status logging.
"""
import os
import sys
import json
import shutil
import zipfile
import subprocess
import threading
import urllib.request
from pathlib import Path

# ── Try tkinter, fallback to console ────────────────────────────────
try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    GUI_MODE = True
except ImportError:
    GUI_MODE = False

# ── Configuration ──────────────────────────────────────────────────
GITHUB_API = "https://api.github.com/repos/reeganbannister311-star/Bibs-Account-Manager/releases/latest"
ZIP_ASSET_NAME = "BibsAccountManager.zip"
JAR_ASSET_NAME = "BankCacheBootstrapper.jar"
EXE_NAME = "BibsAccountManager.exe"

LAUNCHER_DIR = Path(__file__).parent.resolve()
VERSION_FILE = LAUNCHER_DIR / "version.txt"
APP_DIR = LAUNCHER_DIR / "App"
EXE_PATH = APP_DIR / EXE_NAME
DREAMBOT_SCRIPTS = Path(os.path.expandvars(r"%USERPROFILE%\DreamBot\Scripts"))


def find_exe() -> Path | None:
    """Find the manager EXE, handling subfolders from ZIP extraction."""
    # Direct path first
    if EXE_PATH.exists():
        return EXE_PATH
    # Search recursively inside App/
    if APP_DIR.exists():
        for p in APP_DIR.rglob(EXE_NAME):
            return p
    return None

# ── Colors ───────────────────────────────────────────────────────
BG = "#1e1e2e"
FG = "#cdd6f4"
ACCENT = "#89b4fa"
SUCCESS = "#a6e3a1"
WARN = "#f9e2af"
ERROR = "#f38ba8"

# ── Helpers ────────────────────────────────────────────────────────
def read_local_version() -> str:
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return "0.0.0"


def write_local_version(tag: str) -> None:
    VERSION_FILE.write_text(tag)


def fmt_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def fetch_latest_release() -> tuple[dict | None, str]:
    """Fetch latest release. Returns (release_dict, error_message)."""
    req = urllib.request.Request(
        GITHUB_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "BibsLauncher/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()), ""
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "No releases found on GitHub. Publish a release first."
        return None, f"GitHub returned HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def find_asset(assets: list, name: str) -> dict | None:
    for a in assets:
        if a.get("name") == name:
            return a
    return None


def download_file(url: str, dest: Path, progress_cb=None) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BibsLauncher/1.0"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            chunk_size = 262144  # 256KB chunks
            data = b""
            downloaded = 0
            last_pct = -1
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                data += chunk
                downloaded += len(chunk)
                if total and progress_cb:
                    pct = int(downloaded / total * 100)
                    if pct != last_pct:
                        last_pct = pct
                        progress_cb(pct)
            dest.write_bytes(data)
        return True
    except Exception:
        return False


def extract_zip(zip_path: Path, dest_dir: Path) -> bool:
    try:
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(dest_dir)
        return True
    except Exception:
        return False


def install_jar(jar_path: Path) -> bool:
    try:
        DREAMBOT_SCRIPTS.mkdir(parents=True, exist_ok=True)
        dest = DREAMBOT_SCRIPTS / JAR_ASSET_NAME
        shutil.copy2(jar_path, dest)
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════
class LauncherGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Bibs Farm Manager Launcher")
        self.root.geometry("500x340")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(str(LAUNCHER_DIR / "icon.ico"))
        except Exception:
            pass

        # ── Header ─────────────────────────────────────────────
        header = tk.Frame(root, bg=BG)
        header.pack(pady=(20, 10))

        self.title_lbl = tk.Label(
            header, text="BIBS FARM MANAGER",
            font=("Segoe UI", 18, "bold"),
            fg=ACCENT, bg=BG
        )
        self.title_lbl.pack()

        self.sub_lbl = tk.Label(
            header, text="Auto-Updater & Launcher",
            font=("Segoe UI", 10), fg=FG, bg=BG
        )
        self.sub_lbl.pack()

        # ── Status ─────────────────────────────────────────────
        status_frame = tk.Frame(root, bg=BG)
        status_frame.pack(pady=10, fill=tk.X, padx=30)

        self.status_lbl = tk.Label(
            status_frame, text="Checking for updates...",
            font=("Segoe UI", 11), fg=FG, bg=BG, anchor="w"
        )
        self.status_lbl.pack(fill=tk.X)

        self.detail_lbl = tk.Label(
            status_frame, text="",
            font=("Segoe UI", 9), fg="#6c7086", bg=BG, anchor="w"
        )
        self.detail_lbl.pack(fill=tk.X, pady=(2, 0))

        # ── Progress ───────────────────────────────────────────
        prog_frame = tk.Frame(root, bg=BG)
        prog_frame.pack(pady=10, fill=tk.X, padx=30)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TProgressbar", thickness=14, background=ACCENT, troughcolor="#313244")

        self.progress = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X)

        # ── Version info ───────────────────────────────────────
        self.ver_lbl = tk.Label(
            root, text=f"Installed: v{read_local_version()}",
            font=("Segoe UI", 9), fg="#6c7086", bg=BG
        )
        self.ver_lbl.pack(side=tk.BOTTOM, pady=15)

        # ── Start worker ───────────────────────────────────────
        self.after_id = None
        threading.Thread(target=self._worker, daemon=True).start()

    # ── UI helpers ─────────────────────────────────────────
    def set_status(self, text: str, color: str = FG):
        self.root.after(0, lambda: self.status_lbl.config(text=text, fg=color))

    def set_detail(self, text: str):
        self.root.after(0, lambda: self.detail_lbl.config(text=text))

    def set_progress(self, value: int):
        self.root.after(0, lambda: self.progress.config(value=value))

    def set_version(self, text: str):
        self.root.after(0, lambda: self.ver_lbl.config(text=text))

    def finish(self):
        if self.after_id:
            self.root.after_cancel(self.after_id)
        self.root.after(0, self.root.destroy)

    # ── Worker thread ──────────────────────────────────────
    def _worker(self):
        local_version = read_local_version()
        self.set_version(f"Installed: v{local_version}")
        self.set_status("Checking GitHub for updates...")

        release, err = fetch_latest_release()
        if not release:
            self.set_status("Update check failed", ERROR)
            self.set_detail(err or "No internet connection")
            if EXE_PATH.exists():
                self.root.after(3000, self._launch)
            else:
                self.root.after(3000, lambda: self._fatal(
                    "No release found and no local install.\n"
                    "Publish a release on GitHub or place the app in the App/ folder."
                ))
            return

        remote_tag = release.get("tag_name", "0.0.0").lstrip("v")
        self.set_version(f"Installed: v{local_version}  |  Latest: v{remote_tag}")

        if remote_tag == local_version and EXE_PATH.exists():
            self.set_status("Already up to date!", SUCCESS)
            self.set_progress(100)
            self.root.after(1500, self._launch)
            return

        self.set_status(f"Update available: v{local_version} → v{remote_tag}", WARN)
        assets = release.get("assets", [])

        # 1) Download & extract ZIP
        zip_asset = find_asset(assets, ZIP_ASSET_NAME)
        if zip_asset:
            size = zip_asset.get("size", 0)
            size_str = fmt_size(size) if size else ""
            self.set_detail(f"Downloading {ZIP_ASSET_NAME} {size_str}...")
            zip_path = LAUNCHER_DIR / ZIP_ASSET_NAME
            ok = download_file(
                zip_asset["browser_download_url"], zip_path,
                progress_cb=lambda p: self.set_progress(p // 2)
            )
            if ok:
                self.set_detail("Extracting manager files...")
                self.set_progress(50)
                if extract_zip(zip_path, APP_DIR):
                    zip_path.unlink(missing_ok=True)
                    self.set_progress(60)

        # 2) Download & install JAR
        jar_asset = find_asset(assets, JAR_ASSET_NAME)
        if jar_asset:
            jsize = jar_asset.get("size", 0)
            jsize_str = fmt_size(jsize) if jsize else ""
            self.set_detail(f"Downloading {JAR_ASSET_NAME} {jsize_str}...")
            jar_path = LAUNCHER_DIR / JAR_ASSET_NAME
            ok = download_file(
                jar_asset["browser_download_url"], jar_path,
                progress_cb=lambda p: self.set_progress(60 + p // 3)
            )
            if ok:
                self.set_detail("Installing DreamBot script...")
                install_jar(jar_path)
                jar_path.unlink(missing_ok=True)
                self.set_progress(95)

        write_local_version(remote_tag)
        self.set_status(f"Updated to v{remote_tag}! Launching...", SUCCESS)
        self.set_progress(100)
        self.root.after(1500, self._launch)

    def _fatal(self, msg: str):
        messagebox.showerror("Launcher Error", msg)
        self.root.destroy()

    def _launch(self):
        exe = find_exe()
        if not exe:
            listing = ""
            if APP_DIR.exists():
                items = [p.name for p in APP_DIR.iterdir()]
                listing = f"\n\nContents of {APP_DIR}:\n  " + "\n  ".join(items[:20])
            self._fatal(
                f"Manager executable not found:\n{EXE_PATH}{listing}\n\n"
                "The ZIP may have extracted into a subfolder.\n"
                "Run the launcher from its installed folder (not from a ZIP/RAR)."
            )
            return

        self.set_status("Launching Farm Manager...", SUCCESS)
        self.set_detail("")
        subprocess.Popen([str(exe)], cwd=str(exe.parent))
        self.root.destroy()


# ═══════════════════════════════════════════════════════════════════
#  Console fallback
# ═══════════════════════════════════════════════════════════════════
def console_main():
    print("=" * 60)
    print("  Bibs Farm Manager Launcher")
    print("=" * 60)

    local_version = read_local_version()
    print(f"[LAUNCHER] Installed version: {local_version}")

    release, err = fetch_latest_release()
    if not release:
        print(f"[LAUNCHER] {err or 'Could not reach GitHub'}")
        exe = find_exe()
        if exe:
            print("[LAUNCHER] Launching existing install ...")
        else:
            print(f"[LAUNCHER] ERROR: No release found and no local install.")
            print("[LAUNCHER] Run from the install folder (not from ZIP/RAR).")
            input("Press Enter to exit...")
            sys.exit(1)
    else:
        remote_tag = release.get("tag_name", "0.0.0").lstrip("v")
        print(f"[LAUNCHER] Latest release: v{remote_tag}")

        if remote_tag != local_version:
            print(f"[LAUNCHER] Update available: {local_version} -> {remote_tag}")
            assets = release.get("assets", [])

            zip_asset = find_asset(assets, ZIP_ASSET_NAME)
            if zip_asset:
                zip_path = LAUNCHER_DIR / ZIP_ASSET_NAME
                if download_file(zip_asset["browser_download_url"], zip_path):
                    if extract_zip(zip_path, APP_DIR):
                        zip_path.unlink(missing_ok=True)

            jar_asset = find_asset(assets, JAR_ASSET_NAME)
            if jar_asset:
                jar_path = LAUNCHER_DIR / JAR_ASSET_NAME
                if download_file(jar_asset["browser_download_url"], jar_path):
                    install_jar(jar_path)
                    jar_path.unlink(missing_ok=True)

            write_local_version(remote_tag)
            print(f"[LAUNCHER] Updated to v{remote_tag}")
        else:
            print("[LAUNCHER] Already up to date.")

    exe = find_exe()
    if not exe:
        print(f"[LAUNCHER] ERROR: Manager not found at {EXE_PATH}")
        print("[LAUNCHER] Run from the install folder (not from ZIP/RAR).")
        input("Press Enter to exit...")
        sys.exit(1)

    print(f"[LAUNCHER] Starting {exe.name} ...")
    subprocess.Popen([str(exe)], cwd=str(exe.parent))
    sys.exit(0)


# ═══════════════════════════════════════════════════════════════════
#  Entry
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if GUI_MODE:
        root = tk.Tk()
        app = LauncherGUI(root)
        root.mainloop()
    else:
        console_main()
