"""
SQLite database layer for the DreamBot Farm Manager.
"""
import sqlite3
import os
import shutil
from datetime import datetime

# Store DB in DreamBot's BotData folder for persistence
_BOTDATA = os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData")
DB_PATH = os.path.join(_BOTDATA, "farm_manager.db")
_BACKUP_DIR = os.path.join(_BOTDATA, "backups")

class FarmDB:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        self._init_db()
        self._migrate()
        self._backup()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            c = conn.cursor()
            # Accounts table
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (proxy_id) REFERENCES proxies(id)
                )
            """)
            # Proxies table
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
            # Scripts table
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
            # Profiles table
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
            # Ban log table for tracking account bans with timestamps
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
            
            # Add queue_mode column to profiles table if it doesn't exist
            c.execute("PRAGMA table_info(profiles)")
            columns = [row[1] for row in c.fetchall()]
            if 'queue_mode' not in columns:
                c.execute("ALTER TABLE profiles ADD COLUMN queue_mode TEXT DEFAULT 'parallel'")
            
            conn.commit()

    def _backup(self):
        if os.path.isfile(self.db_path):
            ts = str(int(datetime.now().timestamp() * 1000))
            dest = os.path.join(_BACKUP_DIR, f"farm_manager.db.backup.{ts}")
            shutil.copy2(self.db_path, dest)
            # Keep only last 20 backups
            backups = sorted([f for f in os.listdir(_BACKUP_DIR) if f.startswith("farm_manager.db.backup.")])
            for old in backups[:-20]:
                os.remove(os.path.join(_BACKUP_DIR, old))

    def _migrate(self):
        """Add missing columns to existing tables."""
        with self._connect() as conn:
            c = conn.cursor()
            # Check accounts table columns
            c.execute("PRAGMA table_info(accounts)")
            existing = {row[1] for row in c.fetchall()}
            # Add missing columns
            if "session_id" not in existing:
                c.execute("ALTER TABLE accounts ADD COLUMN session_id TEXT")
                print("[MIGRATE] Added session_id to accounts")
            if "character_id" not in existing:
                c.execute("ALTER TABLE accounts ADD COLUMN character_id TEXT")
                print("[MIGRATE] Added character_id to accounts")
            if "display_name" not in existing:
                c.execute("ALTER TABLE accounts ADD COLUMN display_name TEXT")
                print("[MIGRATE] Added display_name to accounts")
            if "membership" not in existing:
                c.execute("ALTER TABLE accounts ADD COLUMN membership BOOLEAN DEFAULT 0")
                print("[MIGRATE] Added membership to accounts")
            # Queue migrations
            c.execute("PRAGMA table_info(script_queue)")
            sq_cols = {row[1] for row in c.fetchall()}
            if "mode" not in sq_cols:
                c.execute("ALTER TABLE script_queue ADD COLUMN mode TEXT DEFAULT 'parallel'")
                print("[MIGRATE] Added mode to script_queue")
            if "batch_group" not in sq_cols:
                c.execute("ALTER TABLE script_queue ADD COLUMN batch_group INTEGER DEFAULT 0")
                print("[MIGRATE] Added batch_group to script_queue")
            if "batch_position" not in sq_cols:
                c.execute("ALTER TABLE script_queue ADD COLUMN batch_position INTEGER DEFAULT 0")
                print("[MIGRATE] Added batch_position to script_queue")
            c.execute("PRAGMA table_info(queue_scripts)")
            qs_cols = {row[1] for row in c.fetchall()}
            if "repeat" not in qs_cols:
                c.execute("ALTER TABLE queue_scripts ADD COLUMN repeat INTEGER DEFAULT 1")
                print("[MIGRATE] Added repeat to queue_scripts")
            if "completed_runs" not in qs_cols:
                c.execute("ALTER TABLE queue_scripts ADD COLUMN completed_runs INTEGER DEFAULT 0")
                print("[MIGRATE] Added completed_runs to queue_scripts")
            # Profile migrations
            c.execute("PRAGMA table_info(profiles)")
            prof_cols = {row[1] for row in c.fetchall()}
            if "batch_size" not in prof_cols:
                c.execute("ALTER TABLE profiles ADD COLUMN batch_size INTEGER DEFAULT 0")
                print("[MIGRATE] Added batch_size to profiles")
            if "stagger_delay" not in prof_cols:
                c.execute("ALTER TABLE profiles ADD COLUMN stagger_delay INTEGER DEFAULT 5")
                print("[MIGRATE] Added stagger_delay to profiles")
            conn.commit()

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------
    def add_account(self, email, password, pin="", totp="", username="",
                    display_name="", jagex_account=1, proxy_id=None,
                    category="Uncategorized", status="Offline", notes=""):
        with self._connect() as conn:
            c = conn.cursor()
            try:
                c.execute("""
                    INSERT INTO accounts (email, password, pin, totp, username,
                        display_name, jagex_account, proxy_id, category, status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (email, password, pin, totp, username, display_name,
                      jagex_account, proxy_id, category, status, notes))
                conn.commit()
                return c.lastrowid
            except sqlite3.IntegrityError:
                return None

    def update_account(self, account_id, **kwargs):
        allowed = {"email", "password", "pin", "totp", "username",
                   "jagex_account", "session_id", "character_id",
                   "proxy_id", "category", "status", "notes", "display_name",
                   "membership"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        with self._connect() as conn:
            c = conn.cursor()
            set_clause = ", ".join(f"{k}=?" for k in fields)
            values = list(fields.values()) + [account_id]
            c.execute(f"UPDATE accounts SET {set_clause} WHERE id=?", values)
            conn.commit()
            return c.rowcount > 0

    def get_most_recent_running(self):
        """Return the most recently launched account still marked Running."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT a.* FROM accounts a
                JOIN launch_history lh ON a.id = lh.account_id
                WHERE lh.ended_at IS NULL
                ORDER BY lh.started_at DESC
                LIMIT 1
            """)
            row = c.fetchone()
            return dict(row) if row else None

    def delete_account(self, account_id):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            conn.commit()
            return c.rowcount > 0

    def clear_all_accounts(self):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM accounts")
            conn.commit()
            return c.rowcount

    def get_account(self, account_id):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
            row = c.fetchone()
            return dict(row) if row else None

    def get_account_by_email(self, email):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM accounts WHERE email=?", (email,))
            row = c.fetchone()
            return dict(row) if row else None

    def update_account_sessions(self, sessions):
        """sessions: list of dicts with 'email', 'sessionId', 'characterId' keys"""
        updated = 0
        with self._connect() as conn:
            c = conn.cursor()
            for s in sessions:
                email = s.get("email", "").strip()
                if not email:
                    continue
                sid = s.get("sessionId", "").strip() or s.get("session_id", "").strip()
                cid = s.get("characterId", "").strip() or s.get("character_id", "").strip()
                fields = []
                vals = []
                if sid:
                    fields.append("session_id=?")
                    vals.append(sid)
                if cid:
                    fields.append("character_id=?")
                    vals.append(cid)
                if not fields:
                    continue
                vals.append(email)
                c.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE email=?", vals)
                if c.rowcount > 0:
                    updated += 1
            conn.commit()
        return updated

    def list_accounts(self, category=None, status=None, search=""):
        with self._connect() as conn:
            c = conn.cursor()
            query = "SELECT * FROM accounts WHERE 1=1"
            params = []
            if category:
                query += " AND category=?"
                params.append(category)
            if status:
                query += " AND status=?"
                params.append(status)
            if search:
                query += " AND (email LIKE ? OR username LIKE ? OR notes LIKE ?)"
                like = f"%{search}%"
                params.extend([like, like, like])
            query += " ORDER BY created_at DESC"
            c.execute(query, params)
            return [dict(row) for row in c.fetchall()]

    def count_by_category(self):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT category, COUNT(*) FROM accounts GROUP BY category")
            return dict(c.fetchall())

    def count_by_status(self):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT status, COUNT(*) FROM accounts GROUP BY status")
            return dict(c.fetchall())

    def get_all_categories(self):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT DISTINCT category FROM accounts ORDER BY category")
            return [row[0] for row in c.fetchall()]

    def get_analytics(self):
        """Return various account counts for the analytics dashboard."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM accounts")
            total = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM accounts WHERE status='Running'")
            running = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM accounts WHERE status='Offline'")
            offline = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM accounts WHERE status='Banned'")
            banned = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM accounts WHERE category='Ready To Farm'")
            ready = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM accounts WHERE category='Tutorial Island'")
            tutorial = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM accounts WHERE proxy_id IS NOT NULL")
            with_proxy = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM accounts WHERE membership=1")
            p2p = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM accounts WHERE totp IS NOT NULL AND totp!=''")
            with_totp = c.fetchone()[0]
            return {
                "total": total,
                "running": running,
                "offline": offline,
                "banned": banned,
                "ready_to_farm": ready,
                "tutorial_island": tutorial,
                "with_proxy": with_proxy,
                "without_proxy": total - with_proxy,
                "p2p": p2p,
                "f2p": total - p2p,
                "with_totp": with_totp,
                "without_totp": total - with_totp,
            }

    # ------------------------------------------------------------------
    # Proxies
    # ------------------------------------------------------------------
    def add_proxy(self, host, port, username="", password="", protocol="http", active=1):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO proxies (host, port, username, password, protocol, active)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (host, port, username, password, protocol, active))
            conn.commit()
            return c.lastrowid

    def list_proxies(self, active_only=False):
        with self._connect() as conn:
            c = conn.cursor()
            if active_only:
                c.execute("SELECT * FROM proxies WHERE active=1")
            else:
                c.execute("SELECT * FROM proxies")
            return [dict(row) for row in c.fetchall()]

    def delete_proxy(self, proxy_id):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM proxies WHERE id=?", (proxy_id,))
            conn.commit()
            return c.rowcount > 0

    # ------------------------------------------------------------------
    # Scripts
    # ------------------------------------------------------------------
    def add_script(self, name, category="Misc", params="", description=""):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO scripts (name, category, params, description)
                VALUES (?, ?, ?, ?)
            """, (name, category, params, description))
            conn.commit()
            return c.lastrowid

    def list_scripts(self):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM scripts ORDER BY name")
            return [dict(row) for row in c.fetchall()]

    def delete_script(self, script_id):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM scripts WHERE id=?", (script_id,))
            conn.commit()
            return c.rowcount > 0

    # ------------------------------------------------------------------
    # Launch history
    # ------------------------------------------------------------------
    def record_launch(self, account_id, script_name, pid=None, status="Running"):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO launch_history (account_id, script_name, pid, status)
                VALUES (?, ?, ?, ?)
            """, (account_id, script_name, pid, status))
            conn.commit()
            # Update account status
            c.execute("UPDATE accounts SET status=? WHERE id=?", (status, account_id))
            conn.commit()
            return c.lastrowid

    def record_stop(self, account_id, final_status="Offline"):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE launch_history SET ended_at=?, status='Stopped'
                WHERE account_id=? AND ended_at IS NULL
            """, (datetime.now().isoformat(), account_id))
            c.execute("UPDATE accounts SET status=? WHERE id=?", (final_status, account_id))
            conn.commit()

    def update_launch_pid(self, account_id, new_pid):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE launch_history SET pid=? WHERE account_id=? AND ended_at IS NULL
            """, (new_pid, account_id))
            conn.commit()

    def get_running(self):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT a.*, lh.script_name, lh.pid, lh.started_at
                FROM accounts a
                JOIN launch_history lh ON a.id = lh.account_id
                WHERE lh.ended_at IS NULL
            """)
            return [dict(row) for row in c.fetchall()]

    def update_launch_status(self, account_id, status):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE launch_history SET status=? WHERE account_id=? AND ended_at IS NULL
            """, (status, account_id))
            c.execute("UPDATE accounts SET status=? WHERE id=?", (status, account_id))
            conn.commit()

    def cleanup_all_running(self):
        """Mark all open launch_history entries as stopped (used on startup)."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE launch_history SET ended_at=?, status='Stopped'
                WHERE ended_at IS NULL
            """, (datetime.now().isoformat(),))
            c.execute("UPDATE accounts SET status='Offline' WHERE status='Running'")
            conn.commit()
            return c.rowcount

    # ------------------------------------------------------------------
    # Script Queue
    # ------------------------------------------------------------------
    def add_queue_entry(self, account_id, position=0, mode='parallel', batch_group=0, batch_position=0):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO script_queue (account_id, position, status, mode, batch_group, batch_position)
                VALUES (?, ?, 'Pending', ?, ?, ?)
            """, (account_id, position, mode, batch_group, batch_position))
            conn.commit()
            return c.lastrowid

    def delete_queue_entry(self, queue_id):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM queue_scripts WHERE queue_id=?", (queue_id,))
            c.execute("DELETE FROM script_queue WHERE id=?", (queue_id,))
            conn.commit()

    def clear_queue(self):
        """Delete all queue entries and their scripts."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM queue_scripts")
            c.execute("DELETE FROM script_queue")
            conn.commit()

    def add_queue_script(self, queue_id, script_name, world=420, stop_condition="Process Exit", stop_value="", order_index=0, repeat=1):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO queue_scripts (queue_id, script_name, world, stop_condition, stop_value, order_index, repeat)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (queue_id, script_name, world, stop_condition, stop_value, order_index, repeat))
            conn.commit()
            return c.lastrowid

    def update_queue_script(self, script_id, script_name=None, world=None, stop_condition=None, stop_value=None, order_index=None, repeat=None, completed_runs=None):
        with self._connect() as conn:
            c = conn.cursor()
            fields = []
            vals = []
            if script_name is not None:
                fields.append("script_name=?"); vals.append(script_name)
            if world is not None:
                fields.append("world=?"); vals.append(world)
            if stop_condition is not None:
                fields.append("stop_condition=?"); vals.append(stop_condition)
            if stop_value is not None:
                fields.append("stop_value=?"); vals.append(stop_value)
            if order_index is not None:
                fields.append("order_index=?"); vals.append(order_index)
            if repeat is not None:
                fields.append("repeat=?"); vals.append(repeat)
            if completed_runs is not None:
                fields.append("completed_runs=?"); vals.append(completed_runs)
            if fields:
                vals.append(script_id)
                c.execute(f"UPDATE queue_scripts SET {','.join(fields)} WHERE id=?", vals)
                conn.commit()

    def delete_queue_script(self, script_id):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM queue_scripts WHERE id=?", (script_id,))
            conn.commit()

    def get_queue_entries(self, status=None):
        with self._connect() as conn:
            c = conn.cursor()
            if status:
                c.execute("""
                    SELECT sq.*, a.email, a.username, a.category
                    FROM script_queue sq
                    JOIN accounts a ON sq.account_id = a.id
                    WHERE sq.status=?
                    ORDER BY sq.position, sq.id
                """, (status,))
            else:
                c.execute("""
                    SELECT sq.*, a.email, a.username, a.category
                    FROM script_queue sq
                    JOIN accounts a ON sq.account_id = a.id
                    ORDER BY sq.position, sq.id
                """)
            return [dict(row) for row in c.fetchall()]

    def get_queue_scripts(self, queue_id):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT * FROM queue_scripts WHERE queue_id=? ORDER BY order_index
            """, (queue_id,))
            return [dict(row) for row in c.fetchall()]

    def get_active_queue_scripts(self):
        """Get all queue_scripts that are currently Running."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT qs.*, sq.account_id, sq.mode
                FROM queue_scripts qs
                JOIN script_queue sq ON qs.queue_id = sq.id
                WHERE qs.status = 'Running'
            """)
            return [dict(row) for row in c.fetchall()]

    def update_queue_entry_status(self, queue_id, status):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("UPDATE script_queue SET status=? WHERE id=?", (status, queue_id))
            if status == 'Running':
                c.execute("UPDATE script_queue SET started_at=? WHERE id=?", (datetime.now().isoformat(), queue_id))
            if status in ('Finished', 'Stopped'):
                c.execute("UPDATE script_queue SET ended_at=? WHERE id=?", (datetime.now().isoformat(), queue_id))
            conn.commit()

    def update_queue_entry_current_idx(self, queue_id, idx):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("UPDATE script_queue SET current_script_idx=? WHERE id=?", (idx, queue_id))
            conn.commit()

    def update_queue_entry_mode(self, queue_id, mode):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("UPDATE script_queue SET mode=? WHERE id=?", (mode, queue_id))
            conn.commit()

    def update_queue_script_status(self, script_id, status, pid=None):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("UPDATE queue_scripts SET status=? WHERE id=?", (status, script_id))
            if status == 'Running':
                c.execute("UPDATE queue_scripts SET started_at=? WHERE id=?", (datetime.now().isoformat(), script_id))
            if status in ('Finished', 'Stopped'):
                c.execute("UPDATE queue_scripts SET ended_at=? WHERE id=?", (datetime.now().isoformat(), script_id))
            if pid is not None:
                c.execute("UPDATE queue_scripts SET pid=? WHERE id=?", (pid, script_id))
            conn.commit()

    def cleanup_queue_running(self):
        """Reset stuck Running queue entries to Pending on startup."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE script_queue SET status='Pending', started_at=NULL
                WHERE status='Running'
            """)
            c.execute("""
                UPDATE queue_scripts SET status='Pending', pid=NULL, started_at=NULL, ended_at=NULL, completed_runs=0
                WHERE status='Running'
            """)
            conn.commit()
            return c.rowcount

    def get_next_pending_queue_script(self, queue_id):
        """Get the next Pending script for a queue entry."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT * FROM queue_scripts
                WHERE queue_id=? AND status='Pending'
                ORDER BY order_index LIMIT 1
            """, (queue_id,))
            row = c.fetchone()
            return dict(row) if row else None

    def reorder_queue_positions(self):
        """Recompact queue positions after deletions."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT id FROM script_queue ORDER BY position, id")
            for i, (qid,) in enumerate(c.fetchall(), start=0):
                c.execute("UPDATE script_queue SET position=? WHERE id=?", (i, qid))
            conn.commit()

    def get_queue_entries_by_batch(self, batch_group, status=None):
        """Get queue entries for a specific batch group."""
        with self._connect() as conn:
            c = conn.cursor()
            if status:
                c.execute("""
                    SELECT sq.*, a.email, a.username, a.category
                    FROM script_queue sq
                    JOIN accounts a ON sq.account_id = a.id
                    WHERE sq.batch_group=? AND sq.status=?
                    ORDER BY sq.batch_position, sq.id
                """, (batch_group, status))
            else:
                c.execute("""
                    SELECT sq.*, a.email, a.username, a.category
                    FROM script_queue sq
                    JOIN accounts a ON sq.account_id = a.id
                    WHERE sq.batch_group=?
                    ORDER BY sq.batch_position, sq.id
                """, (batch_group,))
            return [dict(row) for row in c.fetchall()]

    def count_running_in_batch(self, batch_group):
        """Count how many queue entries in a batch group are currently Running."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT COUNT(*) FROM script_queue
                WHERE batch_group=? AND status='Running'
            """, (batch_group,))
            return c.fetchone()[0]

    def get_next_pending_batch_group(self):
        """Get the lowest batch_group that has Pending entries and no Running entries."""
        with self._connect() as conn:
            c = conn.cursor()
            # Find all batch groups that have Pending entries
            c.execute("""
                SELECT DISTINCT batch_group FROM script_queue
                WHERE status='Pending'
                ORDER BY batch_group ASC
            """)
            pending_groups = [row[0] for row in c.fetchall()]
            for group in pending_groups:
                # Check if this batch group has any Running entries
                c.execute("""
                    SELECT COUNT(*) FROM script_queue
                    WHERE batch_group=? AND status='Running'
                """, (group,))
                running_count = c.fetchone()[0]
                if running_count == 0:
                    return group
            return None

    def get_pending_batch_entries(self, batch_group):
        """Get Pending entries in a batch group, ordered by batch_position."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT sq.*, a.email, a.username, a.category
                FROM script_queue sq
                JOIN accounts a ON sq.account_id = a.id
                WHERE sq.batch_group=? AND sq.status='Pending'
                ORDER BY sq.batch_position ASC
            """, (batch_group,))
            return [dict(row) for row in c.fetchall()]

    def update_queue_entry_batch(self, queue_id, batch_group, batch_position):
        """Update batch group and position for a queue entry."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE script_queue SET batch_group=?, batch_position=? WHERE id=?
            """, (batch_group, batch_position, queue_id))
            conn.commit()

    # ------------------------------------------------------------------
    # Account Stats (OSRS Hiscores)
    # ------------------------------------------------------------------
    def get_account_stats(self, account_id):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM account_stats WHERE account_id=?", (account_id,))
            row = c.fetchone()
            return dict(row) if row else None

    def get_all_account_stats(self):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM account_stats ORDER BY updated_at DESC")
            return [dict(row) for row in c.fetchall()]

    def save_account_stats(self, account_id, username, stats):
        """stats: dict with skill keys, quest_points, combat_lvl, wealth"""
        cols = [
            "account_id", "username", "overall_lvl", "overall_xp", "attack", "defence",
            "strength", "hitpoints", "ranged", "prayer", "magic", "cooking",
            "woodcutting", "fletching", "fishing", "firemaking", "crafting",
            "smithing", "mining", "herblore", "agility", "thieving", "slayer",
            "farming", "runecrafting", "hunter", "construction", "quest_points",
            "combat_lvl", "wealth"
        ]
        values = [account_id, username] + [stats.get(c, 0) for c in cols[2:]] + [datetime.now().isoformat()]
        insert_cols = cols + ["updated_at"]
        # Use excluded.* so no extra parameter placeholders are needed
        set_clause = ", ".join(f"{c}=excluded.{c}" for c in insert_cols)
        with self._connect() as conn:
            c = conn.cursor()
            c.execute(f"""
                INSERT INTO account_stats ({','.join(insert_cols)})
                VALUES ({','.join('?' for _ in insert_cols)})
                ON CONFLICT(account_id) DO UPDATE SET {set_clause}
            """, values)
            conn.commit()

    def delete_account_stats(self, account_id):
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM account_stats WHERE account_id=?", (account_id,))
            conn.commit()

    # ------------------------------------------------------------------
    # Profile Management
    # ------------------------------------------------------------------
    def create_profile(self, name, description="", queue_mode="parallel", batch_size=0, stagger_delay=5, category_filter="", status_filter="", search_filter=""):
        """Create a new profile with filters."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO profiles (name, description, queue_mode, batch_size, stagger_delay, category_filter, status_filter, search_filter, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, description, queue_mode, batch_size, stagger_delay, category_filter, status_filter, search_filter, datetime.now().isoformat()))
            conn.commit()
            return c.lastrowid

    def list_profiles(self):
        """Get all profiles."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM profiles ORDER BY name")
            return [dict(row) for row in c.fetchall()]

    def get_profile(self, profile_id):
        """Get a specific profile."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM profiles WHERE id=?", (profile_id,))
            row = c.fetchone()
            return dict(row) if row else None

    def update_profile(self, profile_id, **kwargs):
        """Update profile fields."""
        if not kwargs:
            return False
        kwargs['updated_at'] = datetime.now().isoformat()
        with self._connect() as conn:
            c = conn.cursor()
            set_clause = ", ".join(f"{k}=?" for k in kwargs.keys())
            c.execute(f"UPDATE profiles SET {set_clause} WHERE id=?", (*kwargs.values(), profile_id))
            conn.commit()
            return c.rowcount > 0

    def delete_profile(self, profile_id):
        """Delete a profile (cascades to profile_accounts and profile_scripts)."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
            conn.commit()
            return c.rowcount > 0

    def get_profile_accounts(self, profile_id):
        """Get accounts associated with a profile."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT a.* FROM accounts a
                JOIN profile_accounts pa ON a.id = pa.account_id
                WHERE pa.profile_id = ?
                ORDER BY a.email
            """, (profile_id,))
            return [dict(row) for row in c.fetchall()]

    def add_account_to_profile(self, profile_id, account_id):
        """Add an account to a profile."""
        with self._connect() as conn:
            c = conn.cursor()
            try:
                c.execute("INSERT INTO profile_accounts (profile_id, account_id) VALUES (?, ?)", (profile_id, account_id))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False  # Already exists

    def remove_account_from_profile(self, profile_id, account_id):
        """Remove an account from a profile."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM profile_accounts WHERE profile_id=? AND account_id=?", (profile_id, account_id))
            conn.commit()
            return c.rowcount > 0

    def get_accounts_by_profile_filters(self, profile):
        """Get accounts matching profile's filters."""
        category = profile.get('category_filter', '')
        status = profile.get('status_filter', '')
        search = profile.get('search_filter', '')
        
        return self.list_accounts(
            category=None if category == '' else category,
            status=None if status == '' else status,
            search=search
        )

    def get_profile_scripts(self, profile_id):
        """Get script templates for a profile."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT * FROM profile_scripts
                WHERE profile_id = ?
                ORDER BY order_index
            """, (profile_id,))
            return [dict(row) for row in c.fetchall()]

    def add_profile_script(self, profile_id, script_name, world=420, stop_condition="Process Exit", stop_value="", order_index=0, repeat=1):
        """Add a script template to a profile."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO profile_scripts 
                (profile_id, script_name, world, stop_condition, stop_value, order_index, repeat)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (profile_id, script_name, world, stop_condition, stop_value, order_index, repeat))
            conn.commit()
            return c.lastrowid

    def update_profile_script(self, script_id, **kwargs):
        """Update a profile script template."""
        if not kwargs:
            return False
        with self._connect() as conn:
            c = conn.cursor()
            set_clause = ", ".join(f"{k}=?" for k in kwargs.keys())
            c.execute(f"UPDATE profile_scripts SET {set_clause} WHERE id=?", (*kwargs.values(), script_id))
            conn.commit()
            return c.rowcount > 0

    def remove_profile_script(self, script_id):
        """Remove a script template from a profile."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM profile_scripts WHERE id=?", (script_id,))
            conn.commit()
            return c.rowcount > 0

    def create_queue_from_profile(self, profile_id, mode="parallel"):
        """Create queue entries for all accounts in a profile using profile scripts.
        If profile has batch_size > 0, accounts are grouped into batches."""
        profile = self.get_profile(profile_id)
        if not profile:
            return False

        # Get accounts (either manually selected or by filters)
        accounts = self.get_profile_accounts(profile_id)
        if not accounts:
            # Try filter-based selection
            accounts = self.get_accounts_by_profile_filters(profile)

        scripts = self.get_profile_scripts(profile_id)
        if not accounts or not scripts:
            return False

        batch_size = profile.get('batch_size', 0) or 0
        print(f"[QUEUE CREATE] Profile '{profile.get('name')}' batch_size={batch_size}, accounts={len(accounts)}, scripts={len(scripts)}")

        # Create queue entry for each account
        with self._connect() as conn:
            c = conn.cursor()
            for idx, account in enumerate(accounts):
                # Compute batch group and position (batch groups start at 1; 0 = legacy/no batch)
                if batch_size > 0:
                    batch_group = (idx // batch_size) + 1
                    batch_position = idx % batch_size
                else:
                    batch_group = 0
                    batch_position = idx

                # Create queue entry
                c.execute("""
                    INSERT INTO script_queue (account_id, status, mode, batch_group, batch_position)
                    VALUES (?, 'Pending', ?, ?, ?)
                """, (account['id'], mode, batch_group, batch_position))
                queue_id = c.lastrowid

                # Add scripts to queue
                for i, script in enumerate(scripts):
                    c.execute("""
                        INSERT INTO queue_scripts
                        (queue_id, script_name, world, stop_condition, stop_value, order_index, repeat)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (queue_id, script['script_name'], script['world'],
                          script['stop_condition'], script['stop_value'], i, script['repeat']))

            conn.commit()
            # Log batch assignments for debugging
            if batch_size > 0:
                c.execute("SELECT batch_group, COUNT(*) FROM script_queue WHERE batch_group > 0 GROUP BY batch_group ORDER BY batch_group")
                groups = c.fetchall()
                for bg, count in groups:
                    print(f"[QUEUE CREATE] Batch group {bg}: {count} accounts")
            return True

    def log_ban(self, account_id, detected_by="status_check", previous_category=None, notes=None):
        """Log when an account is detected as banned with timestamp."""
        with self._connect() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO ban_log (account_id, detected_by, previous_category, notes)
                VALUES (?, ?, ?, ?)
            """, (account_id, detected_by, previous_category, notes))
            conn.commit()
            return c.lastrowid

    def get_ban_log(self, account_id=None, limit=100):
        """Get ban log entries, optionally filtered by account_id."""
        with self._connect() as conn:
            c = conn.cursor()
            if account_id:
                c.execute("""
                    SELECT bl.*, a.email 
                    FROM ban_log bl
                    JOIN accounts a ON bl.account_id = a.id
                    WHERE bl.account_id = ?
                    ORDER BY bl.banned_at DESC
                    LIMIT ?
                """, (account_id, limit))
            else:
                c.execute("""
                    SELECT bl.*, a.email 
                    FROM ban_log bl
                    JOIN accounts a ON bl.account_id = a.id
                    ORDER BY bl.banned_at DESC
                    LIMIT ?
                """, (limit,))
            return [dict(row) for row in c.fetchall()]
