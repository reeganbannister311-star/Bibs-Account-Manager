#!/usr/bin/env python3
"""
Initialize a fresh farm_manager.db with the correct schema.
Run this once before starting the app for the first time.
"""
import os
import sqlite3

_BOTDATA = os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData")
DB_PATH = os.path.join(_BOTDATA, "farm_manager.db")

def init_db():
    os.makedirs(_BOTDATA, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    # Accounts
    c.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            pin TEXT,
            totp TEXT,
            username TEXT,
            jagex_account BOOLEAN DEFAULT 1,
            session_id TEXT,
            character_id TEXT,
            proxy_id INTEGER,
            category TEXT DEFAULT 'Uncategorized',
            status TEXT DEFAULT 'Offline',
            notes TEXT,
            display_name TEXT,
            membership BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (proxy_id) REFERENCES proxies(id)
        )
    """)

    # Proxies
    c.execute("""
        CREATE TABLE IF NOT EXISTS proxies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT,
            password TEXT,
            protocol TEXT DEFAULT 'http',
            active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Scripts
    c.execute("""
        CREATE TABLE IF NOT EXISTS scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT DEFAULT 'Misc',
            params TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Launch history
    c.execute("""
        CREATE TABLE IF NOT EXISTS launch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            script_name TEXT,
            pid INTEGER,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            status TEXT DEFAULT 'Running',
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        )
    """)

    # Script queue entries
    c.execute("""
        CREATE TABLE IF NOT EXISTS script_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            position INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Pending',
            current_script_idx INTEGER DEFAULT 0,
            mode TEXT DEFAULT 'parallel',
            batch_group INTEGER DEFAULT 0,
            batch_position INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        )
    """)

    # Queue script sequence
    c.execute("""
        CREATE TABLE IF NOT EXISTS queue_scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_id INTEGER NOT NULL,
            script_name TEXT NOT NULL,
            world INTEGER DEFAULT 420,
            stop_condition TEXT DEFAULT 'Process Exit',
            stop_value TEXT DEFAULT '',
            order_index INTEGER DEFAULT 0,
            repeat INTEGER DEFAULT 1,
            completed_runs INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Pending',
            pid INTEGER,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            FOREIGN KEY (queue_id) REFERENCES script_queue(id) ON DELETE CASCADE
        )
    """)

    # Cached OSRS hiscore stats
    c.execute("""
        CREATE TABLE IF NOT EXISTS account_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL UNIQUE,
            username TEXT NOT NULL,
            overall_lvl INTEGER DEFAULT 0,
            overall_xp INTEGER DEFAULT 0,
            attack INTEGER DEFAULT 1,
            defence INTEGER DEFAULT 1,
            strength INTEGER DEFAULT 1,
            hitpoints INTEGER DEFAULT 10,
            ranged INTEGER DEFAULT 1,
            prayer INTEGER DEFAULT 1,
            magic INTEGER DEFAULT 1,
            cooking INTEGER DEFAULT 1,
            woodcutting INTEGER DEFAULT 1,
            fletching INTEGER DEFAULT 1,
            fishing INTEGER DEFAULT 1,
            firemaking INTEGER DEFAULT 1,
            crafting INTEGER DEFAULT 1,
            smithing INTEGER DEFAULT 1,
            mining INTEGER DEFAULT 1,
            herblore INTEGER DEFAULT 1,
            agility INTEGER DEFAULT 1,
            thieving INTEGER DEFAULT 1,
            slayer INTEGER DEFAULT 1,
            farming INTEGER DEFAULT 1,
            runecrafting INTEGER DEFAULT 1,
            hunter INTEGER DEFAULT 1,
            construction INTEGER DEFAULT 1,
            quest_points INTEGER DEFAULT 0,
            combat_lvl REAL DEFAULT 3.0,
            wealth TEXT DEFAULT '0',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
    """)

    # Profiles
    c.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            queue_mode TEXT DEFAULT 'parallel',
            batch_size INTEGER DEFAULT 0,
            stagger_delay INTEGER DEFAULT 5,
            category_filter TEXT DEFAULT '',
            status_filter TEXT DEFAULT '',
            search_filter TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Profile account associations
    c.execute("""
        CREATE TABLE IF NOT EXISTS profile_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            UNIQUE(profile_id, account_id)
        )
    """)

    # Profile script templates
    c.execute("""
        CREATE TABLE IF NOT EXISTS profile_scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            script_name TEXT NOT NULL,
            world INTEGER DEFAULT 420,
            stop_condition TEXT DEFAULT 'Process Exit',
            stop_value TEXT DEFAULT '',
            order_index INTEGER DEFAULT 0,
            repeat INTEGER DEFAULT 1,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    """)

    # Ban log
    c.execute("""
        CREATE TABLE IF NOT EXISTS ban_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            detected_by TEXT DEFAULT 'status_check',
            previous_category TEXT,
            notes TEXT,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        )
    """)

    conn.commit()
    conn.close()
    print(f"[INIT] Database created at: {DB_PATH}")
    print("[INIT] You can now run the farm manager and add accounts + proxies via the UI.")

if __name__ == "__main__":
    init_db()
