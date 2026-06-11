#!/usr/bin/env python3
"""
Build script to package BibsAccountManager into a portable ZIP.
Usage: python build_release.py
Output: dist/BibsAccountManager.zip (ready to upload to GitHub releases)
"""
import os
import sys
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist"
EXE_NAME = "BibsAccountManager.exe"

def clean():
    for d in [BUILD_DIR, DIST_DIR]:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

def build():
    clean()
    print("[BUILD] Installing PyInstaller...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    print("[BUILD] Building EXE with PyInstaller...")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--name", "BibsAccountManager",
        "--distpath", str(DIST_DIR / "BibsAccountManager"),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        "--hidden-import", "jagex_account_creator",
        "--hidden-import", "jagex_account_creator.models",
        "--hidden-import", "jagex_account_creator.utils",
        "--hidden-import", "jagex_account_creator.account_creator_selenium",
        "--hidden-import", "jagex_account_creator.account_creator",
        "--hidden-import", "jagex_account_creator.account_creator_camofox",
        "--hidden-import", "jagex_account_creator.camofox_client",
        "--hidden-import", "jagex_account_creator.gproxy",
        str(PROJECT_ROOT / "src" / "main.py"),
    ]
    subprocess.check_call(cmd)

    print("[BUILD] Copying additional files...")
    app_dir = DIST_DIR / "BibsAccountManager"
    for item in ["README.md", "requirements.txt"]:
        src = PROJECT_ROOT / item
        if src.exists():
            shutil.copy2(src, app_dir)

    # Ensure database directory exists
    (app_dir / "data").mkdir(exist_ok=True)

    print("[BUILD] Creating ZIP...")
    zip_path = DIST_DIR / "BibsAccountManager.zip"
    shutil.make_archive(str(DIST_DIR / "BibsAccountManager"), "zip", str(app_dir))

    print(f"[BUILD] Done: {zip_path}")
    print("[BUILD] Upload this ZIP to your GitHub release.")

if __name__ == "__main__":
    build()
