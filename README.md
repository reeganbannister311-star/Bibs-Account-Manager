# Bibs DreamBot Manager

A Python GUI app to bulk-add Jagex accounts into [DreamBot](https://dreambot.org).

## Features
- Paste accounts in `email:password:totp` format
- Modern dark-themed PyQt6 GUI
- **Direct Mode**: writes straight to DreamBot's `accounts.json`
- **UI Automation Mode**: opens DreamBot and types accounts in via `pyautogui`
- Remembers your DreamBot path

## Setup
1. Double-click `start.bat` — it will create a virtual environment, install dependencies, and launch.
2. Or manually:
   ```
   pip install -r requirements.txt
   python src/main.py
   ```

## How to use
1. Paste your accounts into the left panel (one per line, `email:password:totp`).
2. Click **Parse Accounts**.
3. Set your **DreamBot Client.jar** path if it wasn't found automatically.
4. Choose your injection mode:
   - **Direct** — overwrites `accounts.json` (restart DreamBot to load).
   - **UI Automation** — opens DreamBot and types them in. Make sure the **Add Account** dialog is focused.
5. Click **Add Accounts to DreamBot**.

## Account Format
```
email1@gmail.com:password1:123456
email2@gmail.com:password2:654321
```
- TOTP is optional but recommended for Jagex accounts.
- You can leave TOTP blank if you don't use it: `email:password:`

## Notes
- DreamBot data is stored in `%LOCALAPPDATA%\DreamBot\accounts.json`.
- If DreamBot changes its JSON schema, update the `db_accounts` structure in `src/main.py`.
