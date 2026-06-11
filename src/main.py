import sys, os, subprocess, glob, time, json, queue
import logging
from datetime import datetime
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QMessageBox, QFileDialog, QGroupBox, QHeaderView, QTabWidget,
    QDialog, QDialogButtonBox, QTextEdit, QComboBox, QSpinBox, QCheckBox,
    QListWidget, QListWidgetItem, QMenu, QAbstractItemView, QFormLayout,
    QProgressDialog, QRadioButton, QInputDialog
)
from PyQt6.QtCore import Qt, QSettings, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QAction, QColor
import psutil

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAS_WEBENGINE = True
except ImportError:
    HAS_WEBENGINE = False
from database import FarmDB
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import threading

# Logging setup — console only (FileHandlers fail when another instance is running)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

APP_NAME = "Bibs Farm Manager"
APP_VERSION = "2.0.0"
CATEGORIES = ["All Accounts","Uncategorized","Tutorial Island","Quested","Ready To Farm","Banned","Finished","Mule"]
STATUSES = ["Offline","Running","Tutorial Island","Questing","Farming","Banned","Finished"]

def _find_jar():
    paths = [
        os.path.expandvars(r"%LOCALAPPDATA%\DreamBot\Client.jar"),
        os.path.join(os.path.expanduser("~"),"DreamBot","Client.jar"),
        os.path.join(os.path.expanduser("~"),"DreamBot","Launcher.jar"),
        os.path.join(os.path.expanduser("~"),"DreamBot","Launcher","Launcher.jar"),
        os.path.join(os.path.expanduser("~"),"IdeaProjects","DreamBotBootstrapper","libs","Client.jar"),
        os.path.join(os.path.expanduser("~"),"IdeaProjects","PSCBootstrapper","libs","Client.jar"),
    ] + glob.glob(os.path.join(os.path.expanduser("~"),"**","Client.jar"),recursive=True)
    for p in paths:
        if os.path.isfile(p): return os.path.normpath(p)
    return ""

DEFAULT_JAR = _find_jar()

# ------------------------------------------------------------------
# OSRS stats helpers (thread-safe)
# ------------------------------------------------------------------
def _do_fetch_osrs_stats_raw(account_id, username):
    """Fetch OSRS hiscores and return (stats_dict, error_msg) tuple.
    Uses the same JSON endpoint RuneLite uses: services.runescape.com
    """
    if not username or not username.strip():
        return None, "no username"
    import urllib.request
    import urllib.error
    import urllib.parse
    import ssl
    url = f"https://services.runescape.com/m=hiscore_oldschool/index_lite.json?player={urllib.parse.quote(username.strip())}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "RuneLite/1.0",
            "Accept": "application/json",
            "Accept-Language": "en-US",
        }
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            data = resp.read().decode("utf-8")
            if not data or data.strip().startswith("<"):
                return None, "bad response (html)"
            payload = json.loads(data)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return None, f"URL error: {e.reason}"
    except json.JSONDecodeError as e:
        return None, f"bad json: {e}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if not payload or "skills" not in payload:
        return None, "missing skills field"
    name_map = {
        "Overall": "overall_lvl",
        "Attack": "attack",
        "Defence": "defence",
        "Strength": "strength",
        "Hitpoints": "hitpoints",
        "Ranged": "ranged",
        "Prayer": "prayer",
        "Magic": "magic",
        "Cooking": "cooking",
        "Woodcutting": "woodcutting",
        "Fletching": "fletching",
        "Fishing": "fishing",
        "Firemaking": "firemaking",
        "Crafting": "crafting",
        "Smithing": "smithing",
        "Mining": "mining",
        "Herblore": "herblore",
        "Agility": "agility",
        "Thieving": "thieving",
        "Slayer": "slayer",
        "Farming": "farming",
        "Runecrafting": "runecrafting",
        "Hunter": "hunter",
        "Construction": "construction",
    }
    stats = {}
    for sk in payload.get("skills", []):
        key = name_map.get(sk.get("name", ""))
        if key:
            stats[key] = sk.get("level", 1)
            if key == "overall_lvl":
                stats["overall_xp"] = sk.get("xp", 0)
    if "overall_lvl" not in stats:
        return None, "missing overall level"
    attack = stats.get("attack", 1)
    defence = stats.get("defence", 1)
    strength = stats.get("strength", 1)
    hitpoints = stats.get("hitpoints", 10)
    ranged = stats.get("ranged", 1)
    magic = stats.get("magic", 1)
    prayer = stats.get("prayer", 1)
    base = 0.25 * (defence + hitpoints + prayer // 2)
    melee = 0.325 * (attack + strength)
    range_val = 0.325 * (ranged * 1.5)
    mage_val = 0.325 * (magic * 1.5)
    combat = base + max(melee, range_val, mage_val)
    stats["combat_lvl"] = round(combat, 2)
    # Quest points live in the activities array (name="Quests" or "Quest Points")
    qp = 0
    for act in payload.get("activities", []):
        name = (act.get("name") or "").lower()
        if "quest" in name:
            qp = act.get("score", 0)
            break
    stats["quest_points"] = qp
    stats["wealth"] = "0"
    return stats, None


class StatsFetchWorker(QThread):
    """Background thread that fetches OSRS stats without blocking the GUI.
    DB writes happen in the main thread via account_done signal.
    """
    progress = pyqtSignal(str)
    account_done = pyqtSignal(int, str, dict)  # account_id, username, stats
    not_found = pyqtSignal(int, str)            # account_id, username (HTTP 404)
    finished = pyqtSignal(int, int, int)       # fetched, failed, skipped
    error = pyqtSignal(str)

    def __init__(self, accounts):
        super().__init__()
        self.accounts = accounts

    def run(self):
        try:
            # Only fetch accounts with a real OSRS display name.
            # Skip Jagex logins (contain '+') unless display_name is explicitly set.
            to_fetch = []
            skipped = 0
            for a in self.accounts:
                dn = (a.get("display_name") or "").strip()
                un = (a.get("username") or "").strip()
                if dn:
                    to_fetch.append(a)
                elif un and "+" not in un:
                    # username looks like an OSRS name, not a Jagex login
                    to_fetch.append(a)
                else:
                    skipped += 1
            total = len(to_fetch)
            fetched = 0
            failed = 0
            errors = []
            for acc in to_fetch:
                uname = (acc.get("display_name") or acc.get("username") or "").strip()
                self.progress.emit(f"Fetching {uname}... ({fetched + failed + 1}/{total})")
                stats, err = _do_fetch_osrs_stats_raw(acc["id"], uname)
                if stats:
                    self.account_done.emit(acc["id"], uname, stats)
                    fetched += 1
                else:
                    failed += 1
                    errors.append(f"{uname}: {err}")
                    if err and "404" in err:
                        self.not_found.emit(acc["id"], uname)
                time.sleep(0.8)
            if errors:
                self.error.emit("\n".join(errors))
            self.finished.emit(fetched, failed, skipped)
        except Exception:
            import traceback
            self.error.emit(traceback.format_exc())


class _WebhookHandler(BaseHTTPRequestHandler):
    """Receives bot-stats and location POSTs from DreamBot scripts."""
    db = None  # injected by main()
    heatmap_data = None  # injected by main()
    heatmap_lock = None  # injected by main()

    def log_message(self, fmt, *args):
        logging.debug("[WEBHOOK] " + fmt % args)

    def do_POST(self):
        if self.path == "/api/webhook/bot-stats":
            self._handle_bot_stats()
        elif self.path == "/api/location":
            self._handle_location()
        elif self.path == "/api/location/clear":
            self._handle_location_clear()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_bot_stats(self):
        try:
            clen = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(clen).decode("utf-8")
            data = json.loads(body)
            display_name = (data.get("account") or "").strip()
            if display_name and self.db:
                acc = self.db.get_most_recent_running()
                if acc:
                    current_cat = acc.get("category") or ""
                    if current_cat not in ("Banned", "Finished"):
                        self.db.update_account(acc["id"], display_name=display_name, category="Ready To Farm")
                        logging.info("[WEBHOOK] Updated account %d display_name to '%s', category=Ready To Farm", acc["id"], display_name)
                    else:
                        self.db.update_account(acc["id"], display_name=display_name)
                        logging.info("[WEBHOOK] Updated account %d display_name to '%s' (category=%s preserved)", acc["id"], display_name, current_cat)
            self.send_response(200)
            self.end_headers()
        except Exception as e:
            logging.error("[WEBHOOK ERROR] %s", e)
            self.send_response(500)
            self.end_headers()

    def _handle_location(self):
        try:
            clen = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(clen).decode("utf-8")
            data = json.loads(body)
            account_id = data.get("account_id")
            display_name = (data.get("display_name") or str(account_id)).strip()
            x = data.get("x")
            y = data.get("y")
            z = data.get("z", 0)
            activity = (data.get("activity") or "Unknown").strip()

            if account_id is not None and x is not None and y is not None:
                if self.heatmap_data is not None and self.heatmap_lock is not None:
                    with self.heatmap_lock:
                        self.heatmap_data[display_name] = {
                            "display_name": display_name,
                            "x": int(x),
                            "y": int(y),
                            "z": int(z),
                            "activity": activity,
                            "last_update": time.time(),
                        }
                    logging.debug("[HEATMAP] Location update: %s at (%s, %s) — %s", display_name, x, y, activity)
                else:
                    logging.debug("[HEATMAP] Received location but heatmap not initialized")
            self.send_response(200)
            self.end_headers()
        except Exception as e:
            logging.debug("[HEATMAP WEBHOOK ERROR] %s", e)
            self.send_response(500)
            self.end_headers()

    def _handle_location_clear(self):
        try:
            if self.heatmap_data is not None and self.heatmap_lock is not None:
                with self.heatmap_lock:
                    self.heatmap_data.clear()
                logging.debug("[HEATMAP] Cleared all locations")
            self.send_response(200)
            self.end_headers()
        except Exception as e:
            logging.debug("[HEATMAP CLEAR ERROR] %s", e)
            self.send_response(500)
            self.end_headers()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    pass


class MainWindow(QMainWindow):
    creator_log_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.creator_log_signal.connect(self._creator_log_slot)
        self.db = FarmDB()
        self.settings = QSettings("BibsFarm","Settings")
        self.selected_ids = []
        self.current_cat = "All Accounts"
        self.search_text = ""
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setMinimumSize(1400,800)
        # Heatmap: bot location tracking (thread-safe dict) — init BEFORE webhook
        self.heatmap_lock = threading.Lock()
        self.heatmap_data = {}  # account_id -> {display_name, x, y, z, activity, last_update}
        self.heatmap_html_path = os.path.join(os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData"), "heatmap_map.html")
        os.makedirs(os.path.dirname(self.heatmap_html_path), exist_ok=True)
        # Start webhook server to receive display names and bot locations from scripts
        _WebhookHandler.db = self.db
        _WebhookHandler.heatmap_data = self.heatmap_data
        _WebhookHandler.heatmap_lock = self.heatmap_lock
        self._webhook_srv = ThreadedHTTPServer(("127.0.0.1", 8767), _WebhookHandler)
        threading.Thread(target=self._webhook_srv.serve_forever, daemon=True).start()
        logging.info("[WEBHOOK] Listening on 127.0.0.1:8767")
        self.resize(1600,900)
        self._theme()
        self._ensure_webengine()
        self._build_ui()
        self._load_settings()
        # Clean up stale entries from previous sessions
        cleaned = self.db.cleanup_all_running()
        if cleaned:
            print(f"[STARTUP] Cleaned up {cleaned} stale launch entries")
        qcleaned = self.db.cleanup_queue_running()
        if qcleaned:
            print(f"[STARTUP] Cleaned up {qcleaned} stale queue entries")
        self._refresh_all()
        # Track log files and done files for active queue scripts (for script-finish detection)
        self._queue_logs = {}
        self._queue_done_files = {}
        self._bootstrapper_pids = set()  # PIDs launched with bank cache bootstrapper
        self._last_sequential_stop = 0
        # Batch queue state
        self._batch_stagger_timer = QTimer(self)
        self._batch_stagger_timer.setSingleShot(True)
        self._batch_stagger_timer.timeout.connect(self._on_batch_stagger_timer)
        self._current_batch_group = None
        self._batch_stagger_delay = 5  # seconds between launches in a batch
        self._batch_launching = False  # prevent re-entrant launches
        self._last_batch_launch_time = 0
        # Profile runner (direct execution without creating queue entries)
        self._profile_runner_active = False
        self._profile_runner_accounts = []   # list of account dicts
        self._profile_runner_scripts = []    # list of script dicts
        self._profile_runner_mode = "parallel"
        self._profile_runner_batch_size = 0
        self._profile_runner_stagger = 5
        self._profile_runner_current_idx = 0
        self._profile_runner_batch_start_idx = 0
        self._profile_runner_running = {}   # account_id -> {pid, script_name, log_file, done_file, start_time}
        self._profile_runner_name = ""
        self._profile_runner_in_batch = False  # True while stagger timer may fire for current batch
        self._profile_runner_wait_pid_aid = None  # account ID waiting for real PID resolution before next launch
        self._profile_runner_wait_pid_start = 0  # timestamp when PID wait began
        self._profile_runner_stagger_timer = QTimer(self)
        self._profile_runner_stagger_timer.setSingleShot(True)
        self._profile_runner_stagger_timer.timeout.connect(self._on_profile_stagger_timer)
        self._profile_runner_stepping = False  # re-entry guard for _run_profile_step
        self.heatmap_timer = QTimer(self)
        self.heatmap_timer.timeout.connect(self._refresh_heatmap)
        self.heatmap_timer.start(5000)
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(5000)
        # Stats auto-fetch timer (30 minutes)
        self._stats_last_fetch = 0
        self._stats_fetching = False
        self.stats_timer = QTimer(self)
        self.stats_timer.timeout.connect(self._stats_auto_fetch)
        self.stats_timer.start(30 * 60 * 1000)
        # Run initial status check after UI loads (5 seconds)
        QTimer.singleShot(5000, self._stats_auto_fetch)

    def _theme(self):
        self.setStyleSheet("""
            QMainWindow{background:#1e1e1e}QWidget{background:#1e1e1e;color:#e0e0e0}
            QTableWidget{background:#252526;alternate-background-color:#2d2d30;gridline-color:#3e3e42;border:1px solid #3e3e42;color:#e0e0e0}
            QTableWidget::item:selected{background:#094771;color:#fff}
            QHeaderView::section{background:#2d2d30;color:#e0e0e0;padding:6px;border:1px solid #3e3e42}
            QPushButton{background:#0e639c;color:white;border:none;padding:6px 14px;border-radius:3px}
            QPushButton:hover{background:#1177bb}QPushButton:pressed{background:#094771}
            QPushButton:disabled{background:#3e3e42;color:#888}
            QLineEdit,QTextEdit,QComboBox,QSpinBox{background:#3c3c3c;color:#e0e0e0;border:1px solid #555;padding:4px;border-radius:2px}
            QGroupBox{border:1px solid #3e3e42;margin-top:8px;padding-top:8px}
            QListWidget{background:#252526;border:1px solid #3e3e42}
            QListWidget::item{padding:6px;color:#e0e0e0}
            QListWidget::item:selected{background:#094771;color:#fff}
            QMenu{background:#2d2d30;border:1px solid #3e3e42;color:#e0e0e0}
            QMenu::item:selected{background:#094771}
            QLabel{color:#e0e0e0}
        """)

    def _ensure_webengine(self):
        """Auto-install PyQt6-WebEngine if missing. Tries same-process import first; restarts only as last resort."""
        global HAS_WEBENGINE
        if HAS_WEBENGINE:
            return
        try:
            import PyQt6.QtWebEngineWidgets
            HAS_WEBENGINE = True
            return
        except ImportError:
            pass  # WebEngine not available, will try auto-install

        # Prevent infinite restart loops — only auto-install once per launch chain
        if os.environ.get("_FM_WEBENGINE_RETRY"):
            return

        logging.info("[HEATMAP] PyQt6-WebEngine not found — auto-installing...")
        try:
            # Force-reinstall both PyQt6 and PyQt6-WebEngine to fix version mismatches
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", "PyQt6", "PyQt6-WebEngine"],
                capture_output=True, text=True, timeout=180
            )
            if result.returncode == 0:
                logging.info("[HEATMAP] PyQt6-WebEngine installed. Trying import...")
                import importlib
                import importlib.util
                importlib.invalidate_caches()
                try:
                    import PyQt6.QtWebEngineWidgets
                    HAS_WEBENGINE = True
                    logging.info("[HEATMAP] WebEngine ready.")
                    return
                except ImportError:
                    logging.warning("[HEATMAP] Import still failing, restarting...")
                    os.environ["_FM_WEBENGINE_RETRY"] = "1"
                    os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                logging.warning(f"[HEATMAP] Install failed (exit {result.returncode}).")
        except Exception as e:
            logging.warning(f"[HEATMAP] Auto-install error: {e}")

    def _build_ui(self):
        cw = QWidget()
        self.setCentralWidget(cw)
        ml = QHBoxLayout(cw)
        ml.setContentsMargins(8,8,8,8)
        ml.setSpacing(8)

        # Sidebar
        sb = QWidget()
        sb.setFixedWidth(200)
        sbl = QVBoxLayout(sb)
        sbl.setContentsMargins(0,0,0,0)
        t = QLabel(APP_NAME)
        t.setFont(QFont("Segoe UI",14,QFont.Weight.Bold))
        sbl.addWidget(t)
        sbl.addWidget(QLabel("Categories"))
        self.cat_list = QListWidget()
        self.cat_list.setMaximumHeight(280)
        self.cat_list.itemClicked.connect(self._cat_click)
        sbl.addWidget(self.cat_list)
        sbl.addWidget(QLabel("Overview"))
        self.ov = QLabel("Accounts: 0\nRunning: 0\nBanned: 0")
        self.ov.setStyleSheet("color:#aaa;padding:4px")
        sbl.addWidget(self.ov)
        sbl.addStretch()
        sbl.addWidget(QLabel("DB: farm.db"))
        ml.addWidget(sb)

        # Center — tabbed
        self.tabs = QTabWidget()
        tabs = self.tabs
        tabs.setStyleSheet("QTabWidget::pane{border:1px solid #3e3e42}QTabBar::tab{background:#2d2d30;padding:8px 16px}QTabBar::tab:selected{background:#094771}")

        # Tab 1: Accounts
        acc_tab = QWidget()
        cl = QVBoxLayout(acc_tab)
        cl.setContentsMargins(0,0,0,0)
        cl.setSpacing(8)

        tb = QHBoxLayout()
        self.btn_add = QPushButton("+ Add Account")
        self.btn_add.clicked.connect(self._add_dlg)
        tb.addWidget(self.btn_add)
        self.btn_bulk = QPushButton("Bulk Import")
        self.btn_bulk.clicked.connect(self._bulk_dlg)
        tb.addWidget(self.btn_bulk)
        self.btn_launch = QPushButton("Launch")
        self.btn_launch.setStyleSheet("background:#388e3c")
        self.btn_launch.clicked.connect(self._launch_sel)
        tb.addWidget(self.btn_launch)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setStyleSheet("background:#d32f2f")
        self.btn_stop.clicked.connect(self._stop_sel)
        tb.addWidget(self.btn_stop)
        self.btn_del = QPushButton("Delete")
        self.btn_del.setStyleSheet("background:#555")
        self.btn_del.clicked.connect(self._del_sel)
        tb.addWidget(self.btn_del)
        self.btn_jag = QPushButton("Get Jagex Session")
        self.btn_jag.setStyleSheet("background:#e65100")
        self.btn_jag.clicked.connect(self._get_jagex_session)
        tb.addWidget(self.btn_jag)
        self.btn_fetch_dn = QPushButton("Fetch Display Names")
        self.btn_fetch_dn.setStyleSheet("background:#00695c")
        self.btn_fetch_dn.clicked.connect(self._batch_fetch_display_names)
        tb.addWidget(self.btn_fetch_dn)
        self.cb_show_browser = QCheckBox("Show Browser")
        self.cb_show_browser.setToolTip("Show the Chrome browser window during session grabbing")
        self.cb_show_browser.stateChanged.connect(lambda: self.settings.setValue("show_browser", self.cb_show_browser.isChecked()))
        tb.addWidget(self.cb_show_browser)
        self.cb_session_proxies = QCheckBox("Use Proxies for Sessions")
        self.cb_session_proxies.setToolTip("Rotate proxies when grabbing Jagex sessions (disable = no proxy, 1 at a time)")
        self.cb_session_proxies.setChecked(True)
        self.cb_session_proxies.stateChanged.connect(lambda: self.settings.setValue("session_use_proxies", self.cb_session_proxies.isChecked()))
        tb.addWidget(self.cb_session_proxies)
        self.cb_bank_cache = QCheckBox("Bank Cache")
        self.cb_bank_cache.setToolTip("When enabled, launches a wrapper script that runs the target script then dumps bank cache before closing the client")
        self.cb_bank_cache.stateChanged.connect(lambda: self.settings.setValue("use_bank_cache", self.cb_bank_cache.isChecked()))
        tb.addWidget(self.cb_bank_cache)
        self.spin_session_max = QSpinBox()
        self.spin_session_max.setRange(1, 8)
        self.spin_session_max.setValue(4)
        self.spin_session_max.setToolTip("Max concurrent browsers for session grabbing")
        self.spin_session_max.setSuffix(" concurrent")
        self.spin_session_max.setFixedWidth(110)
        self.spin_session_max.valueChanged.connect(lambda: self.settings.setValue("session_max_concurrent", self.spin_session_max.value()))
        tb.addWidget(QLabel("Max:"))
        tb.addWidget(self.spin_session_max)
        tb.addStretch()
        tb.addWidget(QLabel("Search:"))
        self.se = QLineEdit()
        self.se.setPlaceholderText("Email, username, notes...")
        self.se.setFixedWidth(200)
        self.se.textChanged.connect(self._search)
        tb.addWidget(self.se)
        tb.addWidget(QLabel("Filter:"))
        self.fc = QComboBox()
        self.fc.addItem("All Categories")
        self.fc.addItems(CATEGORIES[1:])
        self.fc.currentTextChanged.connect(self._filter)
        tb.addWidget(self.fc)
        cl.addLayout(tb)

        self.tbl = QTableWidget()
        self.tbl.setColumnCount(10)
        self.tbl.setHorizontalHeaderLabels(["ID","Email","Password","PIN","TOTP","Display Name","Category","Status","Proxy","Notes"])
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self._ctx_menu)
        self.tbl.itemSelectionChanged.connect(self._sel_change)
        cl.addWidget(self.tbl)
        self.status = QLabel("Ready")
        self.status.setStyleSheet("color:#aaa;padding:4px")
        cl.addWidget(self.status)
        tabs.addTab(acc_tab, "Accounts")

        # Tab 2: Script Queue
        queue_tab = QWidget()
        ql = QVBoxLayout(queue_tab)
        ql.setContentsMargins(0,0,0,0)
        ql.setSpacing(8)

        qtb = QHBoxLayout()
        self.btn_qadd = QPushButton("+ Add to Queue")
        self.btn_qadd.clicked.connect(self._queue_add_dlg)
        qtb.addWidget(self.btn_qadd)
        self.btn_qdel = QPushButton("Remove")
        self.btn_qdel.setStyleSheet("background:#555")
        self.btn_qdel.clicked.connect(self._queue_remove)
        qtb.addWidget(self.btn_qdel)
        self.btn_qstart = QPushButton("Start Queue")
        self.btn_qstart.setStyleSheet("background:#388e3c")
        self.btn_qstart.clicked.connect(self._queue_start)
        qtb.addWidget(self.btn_qstart)
        self.btn_qstop = QPushButton("Stop Queue")
        self.btn_qstop.setStyleSheet("background:#d32f2f")
        self.btn_qstop.clicked.connect(self._queue_stop)
        qtb.addWidget(self.btn_qstop)
        qtb.addStretch()
        ql.addLayout(qtb)

        # Profile management section
        profile_section = QGroupBox("Profile Management")
        profile_layout = QVBoxLayout(profile_section)
        
        # Profile controls
        profile_ctrl = QHBoxLayout()
        self.btn_new_profile = QPushButton("New Profile")
        self.btn_new_profile.setStyleSheet("background:#388e3c")
        self.btn_new_profile.clicked.connect(self._new_profile_dlg)
        profile_ctrl.addWidget(self.btn_new_profile)
        self.btn_edit_profile = QPushButton("Edit Profile")
        self.btn_edit_profile.clicked.connect(self._edit_profile_dlg)
        profile_ctrl.addWidget(self.btn_edit_profile)
        self.btn_delete_profile = QPushButton("Delete Profile")
        self.btn_delete_profile.setStyleSheet("background:#d32f2f")
        self.btn_delete_profile.clicked.connect(self._delete_profile)
        profile_ctrl.addWidget(self.btn_delete_profile)
        self.btn_queue_from_profile = QPushButton("Create Queue from Profile")
        self.btn_queue_from_profile.setStyleSheet("background:#e65100")
        self.btn_queue_from_profile.clicked.connect(self._queue_from_profile)
        profile_ctrl.addWidget(self.btn_queue_from_profile)
        profile_ctrl.addStretch()
        profile_layout.addLayout(profile_ctrl)

        # Profile list
        self.profile_tbl = QTableWidget()
        self.profile_tbl.setColumnCount(7)
        self.profile_tbl.setHorizontalHeaderLabels(["ID","Name","Description","Mode","Batch","Accounts","Scripts"])
        self.profile_tbl.horizontalHeader().setStretchLastSection(True)
        self.profile_tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.profile_tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.profile_tbl.setAlternatingRowColors(True)
        self.profile_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.profile_tbl.setMaximumHeight(200)
        self.profile_tbl.itemSelectionChanged.connect(self._profile_sel_change)
        profile_layout.addWidget(self.profile_tbl)
        
        # Profile action buttons
        profile_btn_layout = QHBoxLayout()
        self.btn_new_profile = QPushButton("New Profile")
        self.btn_new_profile.clicked.connect(self._new_profile_dlg)
        profile_btn_layout.addWidget(self.btn_new_profile)
        
        self.btn_edit_profile = QPushButton("Edit Profile")
        self.btn_edit_profile.clicked.connect(self._edit_profile_dlg)
        self.btn_edit_profile.setEnabled(False)
        profile_btn_layout.addWidget(self.btn_edit_profile)
        
        self.btn_delete_profile = QPushButton("Delete Profile")
        self.btn_delete_profile.clicked.connect(self._delete_profile)
        self.btn_delete_profile.setEnabled(False)
        profile_btn_layout.addWidget(self.btn_delete_profile)
        
        self.btn_start_profile_queue = QPushButton("Start Queue from Profile")
        self.btn_start_profile_queue.setStyleSheet("background:#4CAF50")
        self.btn_start_profile_queue.clicked.connect(self._start_profile_queue_direct)
        self.btn_start_profile_queue.setEnabled(False)
        profile_btn_layout.addWidget(self.btn_start_profile_queue)
        
        profile_layout.addLayout(profile_btn_layout)

        
        self.qtbl = QTableWidget()
        self.qtbl.setColumnCount(8)
        self.qtbl.setHorizontalHeaderLabels(["ID","Account","Status","Mode","Batch","Current Script","Scripts","Created"])
        self.qtbl.horizontalHeader().setStretchLastSection(True)
        self.qtbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.qtbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.qtbl.setAlternatingRowColors(True)
        self.qtbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.qtbl.itemSelectionChanged.connect(self._queue_sel_change)
        self.qtbl.doubleClicked.connect(self._queue_edit_scripts)
        ql.addWidget(self.qtbl)

        # Script sequence editor for selected queue entry
        qed = QGroupBox("Script Sequence (double-click row to edit)")
        qel = QVBoxLayout(qed)
        self.qslbl = QLabel("Select a queue entry to view/edit scripts")
        self.qslbl.setStyleSheet("color:#aaa")
        qel.addWidget(self.qslbl)
        self.qs_tbl = QTableWidget()
        self.qs_tbl.setColumnCount(6)
        self.qs_tbl.setHorizontalHeaderLabels(["#","Script","World","Stop Condition","Value","Repeats"])
        self.qs_tbl.horizontalHeader().setStretchLastSection(True)
        self.qs_tbl.setAlternatingRowColors(True)
        self.qs_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        qel.addWidget(self.qs_tbl)

        ql.addWidget(profile_section)
        ql.addWidget(qed)
        tabs.addTab(queue_tab, "Script Queue")

        # Tab 3: Proxies
        proxy_tab = QWidget()
        pl = QVBoxLayout(proxy_tab)
        pl.setContentsMargins(0,0,0,0)
        pl.setSpacing(8)

        ptb = QHBoxLayout()
        self.btn_proxy_add = QPushButton("+ Add Proxy")
        self.btn_proxy_add.setStyleSheet("background:#388e3c")
        self.btn_proxy_add.clicked.connect(self._proxy_add_dlg)
        ptb.addWidget(self.btn_proxy_add)
        self.btn_proxy_bulk = QPushButton("Bulk Import")
        self.btn_proxy_bulk.setStyleSheet("background:#e65100")
        self.btn_proxy_bulk.clicked.connect(self._proxy_bulk_import_dlg)
        ptb.addWidget(self.btn_proxy_bulk)
        self.btn_proxy_file = QPushButton("Import from File")
        self.btn_proxy_file.setStyleSheet("background:#00695c")
        self.btn_proxy_file.clicked.connect(self._proxy_import_file)
        ptb.addWidget(self.btn_proxy_file)
        self.btn_proxy_del = QPushButton("Delete")
        self.btn_proxy_del.setStyleSheet("background:#d32f2f")
        self.btn_proxy_del.clicked.connect(self._proxy_delete)
        ptb.addWidget(self.btn_proxy_del)
        ptb.addStretch()
        self.proxy_count_lbl = QLabel("Active: 0 | Total: 0")
        self.proxy_count_lbl.setStyleSheet("color:#aaa")
        ptb.addWidget(self.proxy_count_lbl)
        pl.addLayout(ptb)

        self.proxy_tbl = QTableWidget()
        self.proxy_tbl.setColumnCount(6)
        self.proxy_tbl.setHorizontalHeaderLabels(["ID","Host","Port","Protocol","Username","Active"])
        self.proxy_tbl.horizontalHeader().setStretchLastSection(True)
        self.proxy_tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.proxy_tbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.proxy_tbl.setAlternatingRowColors(True)
        self.proxy_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        pl.addWidget(self.proxy_tbl)
        tabs.addTab(proxy_tab, "Proxies")

        # Tab 4: Account Settings
        settings_tab = QWidget()
        sl = QVBoxLayout(settings_tab)
        sl.setContentsMargins(0,0,0,0)
        sl.setSpacing(8)

        stb = QHBoxLayout()
        stb.addWidget(QLabel("Select Action:"))
        self.action_combo = QComboBox()
        self.action_combo.addItems([
            "Select Action", "Export", "Start New Task", "Kill Instance",
            "Assign Proxy", "Clear Proxy", "Change Category", "Remove Notes",
            "Check Account Status", "Check Membership", "Toggle Membership",
            "Verify Email", "Change Password",
            "Enable Authenticator", "Disable Authenticator",
            "Redeem Membership Code", "Collect Membership Codes",
            "Appeal Ban", "Sync Ban History"
        ])
        self.action_combo.setMinimumWidth(220)
        stb.addWidget(self.action_combo)
        self.btn_action_go = QPushButton("Go")
        self.btn_action_go.setStyleSheet("background:#388e3c;padding:6px 20px")
        self.btn_action_go.clicked.connect(self._settings_action_go)
        stb.addWidget(self.btn_action_go)
        stb.addStretch()
        stb.addWidget(QLabel("Search:"))
        self.set_search = QLineEdit()
        self.set_search.setPlaceholderText("Email, username, notes...")
        self.set_search.setFixedWidth(200)
        self.set_search.textChanged.connect(self._settings_refresh_table)
        stb.addWidget(self.set_search)
        sl.addLayout(stb)

        self.set_tbl = QTableWidget()
        self.set_tbl.setColumnCount(9)
        self.set_tbl.setHorizontalHeaderLabels(["ID","Email","Password","PIN","TOTP","Display Name","Category","Status","Proxy","Notes"])
        self.set_tbl.horizontalHeader().setStretchLastSection(True)
        self.set_tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.set_tbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.set_tbl.setAlternatingRowColors(True)
        self.set_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.set_tbl.itemSelectionChanged.connect(self._settings_sel_change)
        sl.addWidget(self.set_tbl)
        tabs.addTab(settings_tab, "Account Settings")

        # Tab 4: Account Analytics
        analytics_tab = QWidget()
        al = QVBoxLayout(analytics_tab)
        al.setContentsMargins(0,0,0,0)
        al.setSpacing(8)

        # Metric cards row
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.card_total = self._make_card("Total Accounts", "0", "#1e88e5")
        cards.addWidget(self.card_total)
        self.card_running = self._make_card("Running", "0", "#43a047")
        cards.addWidget(self.card_running)
        self.card_banned = self._make_card("Banned", "0", "#e53935")
        cards.addWidget(self.card_banned)
        self.card_ready = self._make_card("Ready To Farm", "0", "#fb8c00")
        cards.addWidget(self.card_ready)
        self.card_proxy = self._make_card("With Proxy", "0", "#8e24aa")
        cards.addWidget(self.card_proxy)
        self.card_f2p = self._make_card("F2P", "0", "#00acc1")
        cards.addWidget(self.card_f2p)
        self.card_p2p = self._make_card("P2P", "0", "#00897b")
        cards.addWidget(self.card_p2p)
        cards.addStretch()
        al.addLayout(cards)

        # Filter panel
        fp = QGroupBox("Filters")
        fl = QHBoxLayout(fp)
        fl.setSpacing(12)
        fl.addWidget(QLabel("Status:"))
        self.afs_status = QComboBox()
        self.afs_status.addItem("All")
        self.afs_status.addItems(STATUSES)
        self.afs_status.currentTextChanged.connect(self._analytics_refresh)
        fl.addWidget(self.afs_status)
        fl.addWidget(QLabel("Category:"))
        self.afs_category = QComboBox()
        self.afs_category.addItem("All")
        self.afs_category.addItems(CATEGORIES[1:])
        self.afs_category.currentTextChanged.connect(self._analytics_refresh)
        fl.addWidget(self.afs_category)
        self.afs_proxy = QCheckBox("Has Proxy")
        self.afs_proxy.stateChanged.connect(self._analytics_refresh)
        fl.addWidget(self.afs_proxy)
        self.afs_p2p = QCheckBox("P2P")
        self.afs_p2p.stateChanged.connect(self._analytics_refresh)
        fl.addWidget(self.afs_p2p)
        self.afs_f2p = QCheckBox("F2P")
        self.afs_f2p.stateChanged.connect(self._analytics_refresh)
        fl.addWidget(self.afs_f2p)
        self.afs_totp = QCheckBox("Has TOTP")
        self.afs_totp.stateChanged.connect(self._analytics_refresh)
        fl.addWidget(self.afs_totp)
        self.afs_search = QLineEdit()
        self.afs_search.setPlaceholderText("Search email, username, notes...")
        self.afs_search.setFixedWidth(220)
        self.afs_search.textChanged.connect(self._analytics_refresh)
        fl.addWidget(self.afs_search)
        fl.addStretch()
        al.addWidget(fp)

        # Filtered results table
        self.atbl = QTableWidget()
        self.atbl.setColumnCount(10)
        self.atbl.setHorizontalHeaderLabels(["ID","Email","Password","PIN","TOTP","Display Name","Category","Status","GP","Notes"])
        self.atbl.horizontalHeader().setStretchLastSection(True)
        self.atbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.atbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.atbl.setAlternatingRowColors(True)
        self.atbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        al.addWidget(self.atbl)
        tabs.addTab(analytics_tab, "Account Analytics")

        # Tab 5: Account Stats (OSRS Hiscores)
        stats_tab = QWidget()
        stl = QVBoxLayout(stats_tab)
        stl.setContentsMargins(0,0,0,0)
        stl.setSpacing(8)

        sttb = QHBoxLayout()
        self.btn_stats_refresh = QPushButton("Refresh Stats")
        self.btn_stats_refresh.setStyleSheet("background:#388e3c")
        self.btn_stats_refresh.clicked.connect(self._stats_refresh_btn)
        sttb.addWidget(self.btn_stats_refresh)
        self.stats_lbl = QLabel("Stats auto-fetched every 30 min")
        self.stats_lbl.setStyleSheet("color:#aaa;padding:4px")
        sttb.addWidget(self.stats_lbl)
        sttb.addStretch()
        sttb.addWidget(QLabel("Search:"))
        self.stats_search = QLineEdit()
        self.stats_search.setPlaceholderText("Username...")
        self.stats_search.setFixedWidth(200)
        self.stats_search.textChanged.connect(self._stats_refresh_table)
        sttb.addWidget(self.stats_search)
        stl.addLayout(sttb)

        self.stats_tbl = QTableWidget()
        self.stats_tbl.setColumnCount(14)
        self.stats_tbl.setHorizontalHeaderLabels(["","Display Name","TTL","QP","CB","GP","Plat","Wealth","Category","Notes","Bans","Status","P2P","Instance"])
        self.stats_tbl.horizontalHeader().setStretchLastSection(True)
        self.stats_tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.stats_tbl.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.stats_tbl.setAlternatingRowColors(True)
        self.stats_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        stl.addWidget(self.stats_tbl)
        tabs.addTab(stats_tab, "Account Stats")

        # Tab 6: Heatmap (Bot Locations)
        heatmap_tab = QWidget()
        hm_layout = QHBoxLayout(heatmap_tab)
        hm_layout.setContentsMargins(0, 0, 0, 0)
        _hm_ok = HAS_WEBENGINE
        if _hm_ok:
            try:
                from PyQt6.QtWebEngineWidgets import QWebEngineView
                from PyQt6.QtCore import QUrl
                # Sidebar: bot list
                hm_sidebar = QWidget()
                hm_sidebar.setFixedWidth(280)
                hm_sbl = QVBoxLayout(hm_sidebar)
                hm_sbl.setContentsMargins(8, 8, 8, 8)
                hm_title = QLabel("Active Bots")
                hm_title.setStyleSheet("font-weight:bold;color:#ff9800;padding-bottom:4px;border-bottom:1px solid #3e3e42")
                hm_sbl.addWidget(hm_title)
                self.btn_clear_sel = QPushButton("Show Full Map")
                self.btn_clear_sel.setStyleSheet("background:#555;color:#e0e0e0;padding:4px;border-radius:3px")
                self.btn_clear_sel.setToolTip("Deselect all bots and stop auto-panning")
                self.btn_clear_sel.clicked.connect(self._heatmap_clear_selection)
                hm_sbl.addWidget(self.btn_clear_sel)
                self.heatmap_list = QListWidget()
                self.heatmap_list.setStyleSheet("background:#252526;border:1px solid #3e3e42;color:#e0e0e0")
                self.heatmap_list.itemClicked.connect(self._heatmap_item_clicked)
                hm_sbl.addWidget(self.heatmap_list)
                hm_layout.addWidget(hm_sidebar)
                # Map: explv loaded directly (no iframe)
                self.heatmap_view = QWebEngineView()
                # Inject a user script BEFORE page JS runs to capture the Leaflet map instance
                try:
                    from PyQt6.QtWebEngineCore import QWebEngineScript, QWebEngineProfile
                    profile = self.heatmap_view.page().profile()
                    capture_script = """
                    (function() {
                        var checkL = setInterval(function() {
                            if (window.L && window.L.map) {
                                clearInterval(checkL);
                                var orig = window.L.map;
                                window.L.map = function() {
                                    var result = orig.apply(this, arguments);
                                    window._captured_map = result;
                                    return result;
                                };
                            }
                        }, 10);
                    })();
                    """
                    script = QWebEngineScript()
                    script.setName("leaflet-capture")
                    script.setSourceCode(capture_script)
                    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
                    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
                    profile.scripts().insert(script)
                except Exception as e:
                    print(f"[HEATMAP] Failed to inject capture script: {e}")
                self.heatmap_view.load(QUrl("https://explv.github.io/?centreX=3200&centreY=3200&centreZ=0&zoom=2"))
                hm_layout.addWidget(self.heatmap_view, stretch=1)
            except Exception as e:
                print(f"[HEATMAP] Failed to create QWebEngineView: {e}")
                _hm_ok = False
        if not _hm_ok:
            hm_warn = QLabel(
                "Heatmap requires PyQt6-WebEngine.\n\n"
                "The manager tried to auto-install it on launch but it may have failed.\n\n"
                "Install it manually with:\n"
                "  pip install --upgrade --force-reinstall PyQt6 PyQt6-WebEngine\n\n"
                "Then restart the manager."
            )
            hm_warn.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hm_warn.setStyleSheet("color:#ff9800;font-size:14px;padding:40px")
            hm_layout.addWidget(hm_warn)
        tabs.addTab(heatmap_tab, "Heatmap")

        # Tab 7: AI Agent
        ai_tab = QWidget()
        ai_layout = QVBoxLayout(ai_tab)
        ai_layout.setContentsMargins(0,0,0,0)
        ai_layout.setSpacing(8)

        # AI Agent header
        ai_header = QHBoxLayout()
        ai_title = QLabel("AI Assistant")
        ai_title.setStyleSheet("font-weight:bold;color:#4CAF50;font-size:16px;padding:8px")
        ai_header.addWidget(ai_title)
        ai_header.addStretch()
        ai_status = QLabel("Ready")
        ai_status.setStyleSheet("color:#aaa;padding:4px")
        ai_header.addWidget(ai_status)
        ai_layout.addLayout(ai_header)

        # Quick action buttons
        ai_actions = QHBoxLayout()
        ai_actions.addWidget(QLabel("Quick Actions:"))
        
        self.btn_ai_build_queue = QPushButton("Build Script Queue")
        self.btn_ai_build_queue.setStyleSheet("background:#388e3c;color:white;padding:6px 12px")
        self.btn_ai_build_queue.clicked.connect(self._ai_build_queue)
        ai_actions.addWidget(self.btn_ai_build_queue)
        
        self.btn_ai_analyze_accounts = QPushButton("Analyze Accounts")
        self.btn_ai_analyze_accounts.setStyleSheet("background:#00695c;color:white;padding:6px 12px")
        self.btn_ai_analyze_accounts.clicked.connect(self._ai_analyze_accounts)
        ai_actions.addWidget(self.btn_ai_analyze_accounts)
        
        self.btn_ai_recommend_scripts = QPushButton("Recommend Scripts")
        self.btn_ai_recommend_scripts.setStyleSheet("background:#e65100;color:white;padding:6px 12px")
        self.btn_ai_recommend_scripts.clicked.connect(self._ai_recommend_scripts)
        ai_actions.addWidget(self.btn_ai_recommend_scripts)
        
        ai_actions.addStretch()
        ai_layout.addLayout(ai_actions)

        # Chat interface
        chat_widget = QWidget()
        chat_layout = QVBoxLayout(chat_widget)
        chat_layout.setContentsMargins(8,8,8,8)
        
        # Chat history
        self.ai_chat_history = QTextEdit()
        self.ai_chat_history.setReadOnly(True)
        self.ai_chat_history.setStyleSheet("""
            QTextEdit {
                background-color: #252526;
                border: 1px solid #3e3e42;
                color: #e0e0e0;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 12px;
                padding: 8px;
            }
        """)
        chat_layout.addWidget(self.ai_chat_history)
        
        # Input area
        input_layout = QHBoxLayout()
        self.ai_input = QLineEdit()
        self.ai_input.setPlaceholderText("Ask the AI assistant...")
        self.ai_input.setStyleSheet("""
            QLineEdit {
                background-color: #2d2d30;
                border: 1px solid #3e3e42;
                color: #e0e0e0;
                padding: 8px;
                font-size: 12px;
            }
        """)
        self.ai_input.returnPressed.connect(self._ai_send_message)
        input_layout.addWidget(self.ai_input)
        
        self.btn_ai_send = QPushButton("Send")
        self.btn_ai_send.setStyleSheet("background:#4CAF50;color:white;padding:6px 16px")
        self.btn_ai_send.clicked.connect(self._ai_send_message)
        input_layout.addWidget(self.btn_ai_send)
        
        chat_layout.addLayout(input_layout)
        ai_layout.addWidget(chat_widget)

        tabs.addTab(ai_tab, "AI Agent")

        # Tab: Account Creator
        creator_tab = QWidget()
        crl = QVBoxLayout(creator_tab)
        crl.setContentsMargins(0,0,0,0)
        crl.setSpacing(8)

        # Toolbar
        ctb = QHBoxLayout()
        ctb.addWidget(QLabel("Accounts:"))
        self.spin_creator_count = QSpinBox()
        self.spin_creator_count.setRange(1, 100)
        self.spin_creator_count.setValue(5)
        self.spin_creator_count.setSuffix(" to create")
        self.spin_creator_count.setFixedWidth(120)
        ctb.addWidget(self.spin_creator_count)

        ctb.addWidget(QLabel("Provider:"))
        self.cb_creator_provider = QComboBox()
        self.cb_creator_provider.addItems(["Guerrilla Mail", "Xitroo", "IMAP", "Gmail Web"])
        self.cb_creator_provider.currentTextChanged.connect(self._on_creator_provider_changed)
        ctb.addWidget(self.cb_creator_provider)

        self.cb_creator_proxies = QCheckBox("Use Proxies")
        self.cb_creator_proxies.setChecked(True)
        ctb.addWidget(self.cb_creator_proxies)

        self.cb_creator_2fa = QCheckBox("Enable 2FA")
        self.cb_creator_2fa.setChecked(True)
        ctb.addWidget(self.cb_creator_2fa)

        self.cb_creator_headless = QCheckBox("Headless")
        ctb.addWidget(self.cb_creator_headless)

        self.btn_creator_start = QPushButton("Start")
        self.btn_creator_start.setStyleSheet("background:#388e3c")
        self.btn_creator_start.clicked.connect(self._start_account_creation)
        ctb.addWidget(self.btn_creator_start)

        self.btn_creator_stop = QPushButton("Stop")
        self.btn_creator_stop.setStyleSheet("background:#d32f2f")
        self.btn_creator_stop.clicked.connect(self._stop_account_creation)
        self.btn_creator_stop.setEnabled(False)
        ctb.addWidget(self.btn_creator_stop)
        ctb.addStretch()
        crl.addLayout(ctb)

        # Provider settings
        self.creator_settings = QGroupBox("Provider Settings")
        csl = QVBoxLayout(self.creator_settings)
        csl.setContentsMargins(8,12,8,8)
        csl.setSpacing(6)

        # Guerrilla Mail settings
        self.creator_guerrilla_widget = QWidget()
        guerrilla_l = QFormLayout(self.creator_guerrilla_widget)
        self.creator_guerrilla_domain = QLineEdit("gmail.com")
        guerrilla_l.addRow("Domain:", self.creator_guerrilla_domain)
        csl.addWidget(self.creator_guerrilla_widget)

        # Xitroo settings
        self.creator_xitroo_widget = QWidget()
        xitroo_l = QFormLayout(self.creator_xitroo_widget)
        self.creator_xitroo_domain = QLineEdit("gmail.com")
        xitroo_l.addRow("Domain:", self.creator_xitroo_domain)
        csl.addWidget(self.creator_xitroo_widget)

        # IMAP settings
        self.creator_imap_widget = QWidget()
        imap_l = QFormLayout(self.creator_imap_widget)
        self.creator_imap_server = QLineEdit("imap.gmail.com")
        self.creator_imap_port = QLineEdit("993")
        self.creator_imap_email = QLineEdit()
        self.creator_imap_password = QLineEdit()
        self.creator_imap_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.creator_imap_domain = QLineEdit("gmail.com")
        imap_l.addRow("IMAP Server:", self.creator_imap_server)
        imap_l.addRow("IMAP Port:", self.creator_imap_port)
        imap_l.addRow("Email:", self.creator_imap_email)
        imap_l.addRow("Password:", self.creator_imap_password)
        imap_l.addRow("Domain:", self.creator_imap_domain)
        csl.addWidget(self.creator_imap_widget)

        # Gmail Web settings
        self.creator_gmail_widget = QWidget()
        gmail_l = QFormLayout(self.creator_gmail_widget)
        self.creator_gmail_email = QLineEdit()
        self.creator_gmail_domain = QLineEdit("gmail.com")
        gmail_l.addRow("Gmail Address:", self.creator_gmail_email)
        gmail_l.addRow("Domain:", self.creator_gmail_domain)
        csl.addWidget(self.creator_gmail_widget)

        crl.addWidget(self.creator_settings)

        # Log area
        self.creator_log = QTextEdit()
        self.creator_log.setReadOnly(True)
        self.creator_log.setPlaceholderText("Account creation progress will appear here...")
        crl.addWidget(self.creator_log)

        tabs.addTab(creator_tab, "Account Creator")
        self._on_creator_provider_changed("Guerrilla Mail")

        tabs.currentChanged.connect(self._refresh_all)
        ml.addWidget(tabs, stretch=1)

        # Right panel
        dp = QGroupBox("Details")
        dl = QVBoxLayout(dp)
        dl.setContentsMargins(8,16,8,8)
        self.dl = QLabel("Select an account")
        self.dl.setWordWrap(True)
        dl.addWidget(self.dl)
        r = QHBoxLayout()
        r.addWidget(QLabel("DreamBot JAR:"))
        self.jar = QLineEdit()
        self.jar.setPlaceholderText("Path to Client.jar")
        r.addWidget(self.jar)
        b = QPushButton("Browse...")
        b.clicked.connect(self._browse)
        r.addWidget(b)
        dl.addLayout(r)
        lg = QGroupBox("Quick Launch")
        ll = QVBoxLayout(lg)
        ll.addWidget(QLabel("Script:"))
        self.sc = QComboBox()
        self.sc.setEditable(False)
        self.sc.addItems(self._discover_scripts())
        ll.addWidget(self.sc)
        ll.addWidget(QLabel("World:"))
        self.ws = QSpinBox()
        self.ws.setRange(301,570)
        self.ws.setValue(420)
        ll.addWidget(self.ws)
        ql = QPushButton("Launch Selected with Script")
        ql.clicked.connect(self._quick_launch)
        ll.addWidget(ql)
        dl.addWidget(lg)
        dl.addStretch()
        dp.setFixedWidth(280)
        ml.addWidget(dp)

    def _refresh_all(self, idx=None):
        """Refresh only the UI elements for the currently visible tab to avoid lag."""
        self._refresh_sidebar()
        self._refresh_overview()

        current = ""
        if hasattr(self, "tabs"):
            try:
                current = self.tabs.tabText(self.tabs.currentIndex())
            except Exception:
                pass

        if current == "Accounts":
            self._refresh_table()
        elif current == "Script Queue":
            self._refresh_queue_table()
            self._refresh_profiles_table()
        elif current == "Proxies":
            self._refresh_proxies_table()
        elif current == "Account Settings":
            self._settings_refresh_table()
        elif current == "Account Analytics":
            self._analytics_refresh()
        elif current == "Account Stats":
            self._stats_refresh_table()
        elif current == "Heatmap":
            self._refresh_heatmap()
        elif current == "AI Agent":
            self._refresh_ai_agent()

    def _refresh_sidebar(self):
        self.cat_list.clear()
        counts = self.db.count_by_category()
        total = sum(counts.values()) if counts else 0
        for cat in CATEGORIES:
            cnt = counts.get(cat,0) if cat != "All Accounts" else total
            it = QListWidgetItem(f"{cat}  ({cnt})")
            it.setData(Qt.ItemDataRole.UserRole, cat)
            self.cat_list.addItem(it)
        for i in range(self.cat_list.count()):
            if self.cat_list.item(i).data(Qt.ItemDataRole.UserRole) == self.current_cat:
                self.cat_list.item(i).setSelected(True)
                break

    def _refresh_table(self):
        self.tbl.blockSignals(True)
        cat = None if self.current_cat == "All Accounts" else self.current_cat
        rows = self.db.list_accounts(category=cat, search=self.search_text)
        self.tbl.setRowCount(len(rows))
        for r, acc in enumerate(rows):
            self.tbl.setItem(r,0,QTableWidgetItem(str(acc["id"])))
            self.tbl.setItem(r,1,QTableWidgetItem(acc["email"]))
            self.tbl.setItem(r,2,QTableWidgetItem(acc["password"]))
            self.tbl.setItem(r,3,QTableWidgetItem(acc.get("pin","")))
            self.tbl.setItem(r,4,QTableWidgetItem(acc.get("totp","")))
            self.tbl.setItem(r,5,QTableWidgetItem(acc.get("display_name","")))
            self.tbl.setItem(r,6,QTableWidgetItem(acc.get("category","Uncategorized")))
            st = acc.get("status","Offline")
            si = QTableWidgetItem(st)
            if st == "Running": si.setBackground(QColor("#1b5e20"))
            elif st == "Banned": si.setBackground(QColor("#b71c1c"))
            self.tbl.setItem(r,7,si)
            pid = acc.get("proxy_id")
            pt = ""
            if pid:
                for p in self.db.list_proxies():
                    if p["id"] == pid: pt = f"{p['host']}:{p['port']}"; break
            self.tbl.setItem(r,8,QTableWidgetItem(pt))
            self.tbl.setItem(r,9,QTableWidgetItem(acc.get("notes","")))
        self.tbl.blockSignals(False)
        self._sel_change()
        self.status.setText(f"Showing {len(rows)} account(s)")

    def _refresh_overview(self):
        c = self.db.count_by_status()
        t = self.db.list_accounts()
        self.ov.setText(f"Accounts: {len(t)}\nRunning: {c.get('Running',0)}\nBanned: {c.get('Banned',0)}")

    def _make_card(self, title, value, color):
        w = QGroupBox(title)
        w.setStyleSheet(f"QGroupBox{{background:{color};color:#fff;border-radius:6px;padding:8px;font-weight:bold}}QLabel{{background:transparent}}")
        wl = QVBoxLayout(w)
        wl.setContentsMargins(8,4,8,4)
        lbl = QLabel(value)
        lbl.setFont(QFont("Segoe UI",18,QFont.Weight.Bold))
        lbl.setStyleSheet("background:transparent;color:#fff")
        wl.addWidget(lbl)
        w._lbl = lbl
        w.setMinimumWidth(140)
        return w

    def _get_account_gp(self, display_name):
        """Read bank cache JSON for display_name and return formatted GP amount."""
        if not display_name:
            return "-"
        safe = display_name.replace(" ", "_")
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in safe)
        path = os.path.join(os.path.expanduser("~"), "CascadeProjects", "bibs-dreambot-manager", "BankCache", f"{safe}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("items", []):
                if item and item.get("id") == 995:
                    amt = item.get("amount", 0)
                    return self._format_gp(amt)
            return "0"
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return "-"

    def _format_gp(self, amount):
        """Format GP amount to human-readable string (e.g. 18662 -> 18.6k)."""
        if amount >= 1_000_000_000:
            return f"{amount/1_000_000_000:.1f}b"
        elif amount >= 1_000_000:
            return f"{amount/1_000_000:.1f}m"
        elif amount >= 1_000:
            return f"{amount/1_000:.1f}k"
        else:
            return str(amount)

    def _analytics_refresh(self):
        # Update metric cards
        stats = self.db.get_analytics()
        self.card_total._lbl.setText(str(stats["total"]))
        self.card_running._lbl.setText(str(stats["running"]))
        self.card_banned._lbl.setText(str(stats["banned"]))
        self.card_ready._lbl.setText(str(stats["ready_to_farm"]))
        self.card_proxy._lbl.setText(f"{stats['with_proxy']} / {stats['without_proxy']}")
        self.card_p2p._lbl.setText(str(stats['p2p']))
        self.card_f2p._lbl.setText(str(stats['f2p']))

        # Apply filters and populate analytics table
        status = self.afs_status.currentText()
        category = self.afs_category.currentText()
        search = self.afs_search.text().strip()
        has_proxy = self.afs_proxy.isChecked()
        has_p2p = self.afs_p2p.isChecked()
        has_f2p = self.afs_f2p.isChecked()
        has_totp = self.afs_totp.isChecked()

        rows = self.db.list_accounts(
            category=None if category == "All" else category,
            status=None if status == "All" else status,
            search=search
        )

        # Apply checkbox filters in Python
        filtered = []
        proxies = {p["id"]: p for p in self.db.list_proxies()}
        for acc in rows:
            if has_proxy and not acc.get("proxy_id"):
                continue
            if has_p2p and not acc.get("membership"):
                continue
            if has_f2p and acc.get("membership"):
                continue
            if has_totp and not (acc.get("totp") or "").strip():
                continue
            filtered.append(acc)

        self.atbl.blockSignals(True)
        self.atbl.setRowCount(len(filtered))
        for r, acc in enumerate(filtered):
            self.atbl.setItem(r,0,QTableWidgetItem(str(acc["id"])))
            self.atbl.setItem(r,1,QTableWidgetItem(acc["email"]))
            self.atbl.setItem(r,2,QTableWidgetItem(acc["password"]))
            self.atbl.setItem(r,3,QTableWidgetItem(acc.get("pin","")))
            self.atbl.setItem(r,4,QTableWidgetItem(acc.get("totp","")))
            self.atbl.setItem(r,5,QTableWidgetItem(acc.get("display_name","")))
            self.atbl.setItem(r,6,QTableWidgetItem(acc.get("category","Uncategorized")))
            st = acc.get("status","Offline")
            si = QTableWidgetItem(st)
            if st == "Running": si.setBackground(QColor("#1b5e20"))
            elif st == "Banned": si.setBackground(QColor("#b71c1c"))
            self.atbl.setItem(r,7,si)
            gp = self._get_account_gp(acc.get("display_name",""))
            self.atbl.setItem(r,8,QTableWidgetItem(gp))
            self.atbl.setItem(r,9,QTableWidgetItem(acc.get("notes","")))
        self.atbl.blockSignals(False)

    def _refresh_queue_table(self):
        self.qtbl.blockSignals(True)
        rows = self.db.get_queue_entries()
        self.qtbl.setRowCount(len(rows))
        for r, q in enumerate(rows):
            self.qtbl.setItem(r, 0, QTableWidgetItem(str(q["id"])))
            nick = q.get("username", "") or q["email"].split("@")[0]
            self.qtbl.setItem(r, 1, QTableWidgetItem(nick))
            st = q.get("status", "Pending")
            si = QTableWidgetItem(st)
            if st == "Running": si.setBackground(QColor("#1b5e20"))
            elif st == "Finished": si.setBackground(QColor("#0d47a1"))
            self.qtbl.setItem(r, 2, si)
            mode = q.get("mode", "parallel")
            self.qtbl.setItem(r, 3, QTableWidgetItem(mode))
            # Batch group
            bg = q.get("batch_group", 0)
            bp = q.get("batch_position", 0)
            batch_text = f"{bg}-{bp}" if bg > 0 else "-"
            bi = QTableWidgetItem(batch_text)
            if bg > 0 and st == "Running":
                bi.setBackground(QColor("#1b5e20"))
            self.qtbl.setItem(r, 4, bi)
            # Get current script
            scripts = self.db.get_queue_scripts(q["id"])
            current_idx = q.get("current_script_idx", 0)
            current = ""
            if current_idx < len(scripts):
                s = scripts[current_idx]
                current = f"{s['script_name']} ({s.get('completed_runs',0)}/{s.get('repeat',1)})"
            self.qtbl.setItem(r, 5, QTableWidgetItem(current))
            script_list = ", ".join([f"{s['script_name']} x{s.get('repeat',1)}" for s in scripts])
            self.qtbl.setItem(r, 6, QTableWidgetItem(script_list))
            self.qtbl.setItem(r, 7, QTableWidgetItem(str(q.get("created_at", ""))))
        self.qtbl.blockSignals(False)
        self._queue_sel_change()

    def _queue_sel_change(self):
        rows = self.qtbl.selectionModel().selectedRows()
        if not rows:
            self.qslbl.setText("Select a queue entry to view scripts")
            self.qs_tbl.setRowCount(0)
            return
        row = rows[0].row()
        qid = int(self.qtbl.item(row, 0).text())
        scripts = self.db.get_queue_scripts(qid)
        self.qs_tbl.setRowCount(len(scripts))
        for r, s in enumerate(scripts):
            self.qs_tbl.setItem(r, 0, QTableWidgetItem(str(s["order_index"] + 1)))
            self.qs_tbl.setItem(r, 1, QTableWidgetItem(s["script_name"]))
            self.qs_tbl.setItem(r, 2, QTableWidgetItem(str(s["world"])))
            self.qs_tbl.setItem(r, 3, QTableWidgetItem(s["stop_condition"]))
            self.qs_tbl.setItem(r, 4, QTableWidgetItem(s["stop_value"]))
            self.qs_tbl.setItem(r, 5, QTableWidgetItem(str(s.get("repeat", 1))))
        acc = self.db.get_queue_entries()[row] if row < len(self.db.get_queue_entries()) else None
        nick = (acc.get("username","") or acc["email"].split("@")[0]) if acc else ""
        self.qslbl.setText(f"Scripts for {nick} (Queue #{qid})")

    def _queue_add_dlg(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Add to Script Queue")
        dlg.setMinimumSize(700, 500)
        ly = QVBoxLayout(dlg)

        # Mode selector (applies to all entries created in this dialog)
        mode_form = QFormLayout()
        mode_combo = QComboBox()
        mode_combo.addItems(["parallel", "sequential"])
        mode_combo.setToolTip("Parallel = run alongside other queue entries. Sequential = wait for previous entry to finish.")
        mode_form.addRow("Mode:", mode_combo)
        ly.addLayout(mode_form)

        # Pre-fetch all non-running accounts for filtering
        all_non_running = [acc for acc in self.db.list_accounts() if acc["status"] != "Running"]

        # Category filter for account selection
        cat_ly = QHBoxLayout()
        cat_ly.addWidget(QLabel("Account Category Filter:"))
        cat_filter = QComboBox()
        cat_filter.addItem("All Categories")
        cat_filter.addItems(CATEGORIES[1:])
        cat_ly.addWidget(cat_filter)
        cat_ly.addStretch()
        ly.addLayout(cat_ly)

        # Helper: build an account combo filtered by category
        def _acc_combo_widget(category="All Categories", current_aid=None):
            cb = QComboBox()
            for acc in all_non_running:
                if category != "All Categories" and acc.get("category", "Uncategorized") != category:
                    continue
                nick = acc.get("username","") or acc["email"].split("@")[0]
                cb.addItem(nick, acc["id"])
                if current_aid and acc["id"] == current_aid:
                    cb.setCurrentIndex(cb.count() - 1)
            return cb

        def refresh_account_combos():
            cat = cat_filter.currentText()
            for r in range(seq_tbl.rowCount()):
                old = seq_tbl.cellWidget(r, 0)
                if old:
                    aid = old.currentData()
                    new_cb = _acc_combo_widget(cat, aid)
                    seq_tbl.setCellWidget(r, 0, new_cb)

        cat_filter.currentTextChanged.connect(refresh_account_combos)

        # Script sequence table - now includes Account per row
        ly.addWidget(QLabel("Script Sequence (each row = one account + script):"))
        seq_tbl = QTableWidget()
        seq_tbl.setColumnCount(7)
        seq_tbl.setHorizontalHeaderLabels(["Account","Script","World","Stop Condition","Value","Repeats",""])
        seq_tbl.horizontalHeader().setStretchLastSection(True)
        seq_tbl.setAlternatingRowColors(True)
        seq_tbl.setRowCount(1)
        seq_tbl.setCellWidget(0, 0, _acc_combo_widget())
        seq_tbl.setCellWidget(0, 1, self._script_combo_widget())
        seq_tbl.setCellWidget(0, 2, self._world_spin_widget())
        seq_tbl.setCellWidget(0, 3, self._stop_combo_widget())
        seq_tbl.setCellWidget(0, 4, QLineEdit())
        rep = QSpinBox(); rep.setRange(1, 100); rep.setValue(1); rep.setFixedWidth(60)
        seq_tbl.setCellWidget(0, 5, rep)
        del_btn = QPushButton("X")
        del_btn.setStyleSheet("background:#b71c1c;padding:2px 8px")
        del_btn.clicked.connect(lambda: self._remove_seq_row(seq_tbl, del_btn))
        seq_tbl.setCellWidget(0, 6, del_btn)
        ly.addWidget(seq_tbl)

        def add_row():
            cat = cat_filter.currentText()
            r = seq_tbl.rowCount()
            seq_tbl.insertRow(r)
            seq_tbl.setCellWidget(r, 0, _acc_combo_widget(cat))
            seq_tbl.setCellWidget(r, 1, self._script_combo_widget())
            seq_tbl.setCellWidget(r, 2, self._world_spin_widget())
            seq_tbl.setCellWidget(r, 3, self._stop_combo_widget())
            seq_tbl.setCellWidget(r, 4, QLineEdit())
            rep = QSpinBox(); rep.setRange(1, 100); rep.setValue(1); rep.setFixedWidth(60)
            seq_tbl.setCellWidget(r, 5, rep)
            db = QPushButton("X")
            db.setStyleSheet("background:#b71c1c;padding:2px 8px")
            db.clicked.connect(lambda: self._remove_seq_row(seq_tbl, db))
            seq_tbl.setCellWidget(r, 6, db)

        add_btn = QPushButton("+ Add Script Row")
        add_btn.clicked.connect(add_row)
        ly.addWidget(add_btn)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        ly.addWidget(bb)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # Group rows by account, preserving order of first appearance
        from collections import OrderedDict
        groups = OrderedDict()  # aid -> list of rows
        for r in range(seq_tbl.rowCount()):
            acc_w = seq_tbl.cellWidget(r, 0)
            scw = seq_tbl.cellWidget(r, 1)
            if not acc_w or not scw:
                continue
            aid = acc_w.currentData()
            if aid is None:
                continue
            if aid not in groups:
                groups[aid] = []
            groups[aid].append(r)

        if not groups:
            return

        mode = mode_combo.currentText()
        pos = len(self.db.get_queue_entries())
        created = 0
        for aid, rows in groups.items():
            # For each account, check if already queued
            existing = [q for q in self.db.get_queue_entries() if q["account_id"] == aid]
            if existing:
                for e in existing:
                    self.db.delete_queue_entry(e["id"])
            qid = self.db.add_queue_entry(aid, position=pos, mode=mode)
            pos += 1
            for i, r in enumerate(rows):
                scw = seq_tbl.cellWidget(r, 1)
                wsw = seq_tbl.cellWidget(r, 2)
                stw = seq_tbl.cellWidget(r, 3)
                vle = seq_tbl.cellWidget(r, 4)
                rep_w = seq_tbl.cellWidget(r, 5)
                repeat = rep_w.value() if rep_w else 1
                self.db.add_queue_script(qid, scw.currentText(), wsw.value(), stw.currentText(), vle.text(), order_index=i, repeat=repeat)
            created += 1
        self._refresh_queue_table()
        self.status.setText(f"Added {created} queue entry(s)")

    def _acc_combo_widget(self, exclude_running=True):
        cb = QComboBox()
        for acc in self.db.list_accounts():
            if exclude_running and acc["status"] == "Running":
                continue
            nick = acc.get("username","") or acc["email"].split("@")[0]
            cb.addItem(nick, acc["id"])
        return cb

    # Manual mapping for script names that don't match JAR filenames or have typos
    SCRIPT_NAME_MAP = {
        "bibs-tut": "Bibs Tut",
        "bibs_tut": "Bibs Tut",
        "bibstut": "Bibs Tut",
        "bibs-slayer": "Bibs Slayer",
        "bibs_slayer": "Bibs Slayer",
        "bibsslayer": "Bibs Slayer",
        "bibs-fishing": "Bibs Fishing (DreamBot)",
        "bibs_fishing": "Bibs Fishing (DreamBot)",
        "bibsfishing": "Bibs Fishing (DreamBot)",
        "bibs-combat": "Bibs Combat Script",
        "bibs_combat": "Bibs Combat Script",
        "bibscombat": "Bibs Combat Script",
    }

    def _discover_scripts(self):
        """Scan DreamBot Scripts directories and read cached script list (includes purchased SDN scripts)."""
        found = set()
        # 1) DreamBot's cached script list — includes ALL available scripts (free + purchased)
        cache_path = os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData\.cache\scripts.dat")
        official_names = {}
        if os.path.isfile(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        name = line.strip()
                        if name:
                            found.add(name)
                            official_names[name.lower()] = name
            except Exception as e:
                logging.warning("[DISCOVER] Failed to read scripts.dat: %s", e)
        # 2) Local JAR files in Scripts directories
        # If a JAR name case-insensitively matches an official DreamBot name, use the official one
        script_dirs = [
            os.path.expandvars(r"%USERPROFILE%\DreamBot\Scripts"),
            os.path.expandvars(r"%LOCALAPPDATA%\DreamBot\Scripts"),
            os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData\Scripts"),
            os.path.join(os.path.expanduser("~"), "DreamBot", "Scripts"),
            os.path.join(os.path.expanduser("~"), "DreamBot", "BotData", "Scripts"),
        ]
        for d in script_dirs:
            if not os.path.isdir(d):
                continue
            for root, _, files in os.walk(d):
                for f in files:
                    if f.lower().endswith(".jar"):
                        name = f[:-4]  # strip .jar
                        lower = name.lower()
                        if lower in self.SCRIPT_NAME_MAP:
                            found.add(self.SCRIPT_NAME_MAP[lower])
                        elif lower in official_names:
                            found.add(official_names[lower])
                        else:
                            found.add(name)
        # 3) Fallback hardcoded names
        fallback = {"BibsAccountAdder","Bibs Tut","Bibs Slayer","Bibs Fishing (DreamBot)","Bibs NPC Hunter (DreamBot)","Universal Combat Script","Universal Combat V2"}
        found.update(fallback)
        return sorted(found, key=str.lower)

    def _script_combo_widget(self):
        cb = QComboBox()
        cb.setEditable(False)
        cb.addItems(self._discover_scripts())
        return cb

    def _world_spin_widget(self):
        sb = QSpinBox()
        sb.setRange(301, 570)
        sb.setValue(420)
        return sb

    def _stop_combo_widget(self):
        cb = QComboBox()
        cb.addItems(["Process Exit", "Script End", "Timer (minutes)", "Level (skill:level)"])
        return cb

    def _remove_seq_row(self, tbl, btn):
        for r in range(tbl.rowCount()):
            if tbl.cellWidget(r, tbl.columnCount() - 1) == btn:
                tbl.removeRow(r)
                break

    def _queue_remove(self):
        rows = self.qtbl.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "None", "Select queue entries to remove.")
            return
        for idx in sorted([r.row() for r in rows], reverse=True):
            qid = int(self.qtbl.item(idx, 0).text())
            self.db.delete_queue_entry(qid)
        self.db.reorder_queue_positions()
        self._refresh_queue_table()
        self.qs_tbl.setRowCount(0)
        self.qslbl.setText("Select a queue entry to view scripts")

    def _queue_edit_scripts(self):
        rows = self.qtbl.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        qid = int(self.qtbl.item(row, 0).text())
        entry = self.db.get_queue_entries()[row] if row < len(self.db.get_queue_entries()) else None
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit Script Sequence (Queue #{qid})")
        dlg.setMinimumSize(650, 450)
        ly = QVBoxLayout(dlg)
        # Account label
        if entry:
            nick = entry.get("username","") or entry["email"].split("@")[0]
            ly.addWidget(QLabel(f"<b>Account:</b> {nick}"))
        # Mode selector
        mode_form = QFormLayout()
        mode_combo = QComboBox()
        mode_combo.addItems(["parallel", "sequential"])
        if entry:
            mode_combo.setCurrentText(entry.get("mode", "parallel"))
        mode_form.addRow("Mode:", mode_combo)
        ly.addLayout(mode_form)
        ly.addWidget(QLabel("Scripts in order — click Save to update:"))
        seq_tbl = QTableWidget()
        seq_tbl.setColumnCount(6)
        seq_tbl.setHorizontalHeaderLabels(["Script","World","Stop Condition","Value","Repeats",""])
        seq_tbl.horizontalHeader().setStretchLastSection(True)
        scripts = self.db.get_queue_scripts(qid)
        seq_tbl.setRowCount(len(scripts))
        for r, s in enumerate(scripts):
            scb = self._script_combo_widget()
            scb.setCurrentText(s["script_name"])
            seq_tbl.setCellWidget(r, 0, scb)
            wsb = self._world_spin_widget()
            wsb.setValue(s["world"])
            seq_tbl.setCellWidget(r, 1, wsb)
            stc = self._stop_combo_widget()
            stc.setCurrentText(s["stop_condition"])
            seq_tbl.setCellWidget(r, 2, stc)
            le = QLineEdit(s["stop_value"])
            seq_tbl.setCellWidget(r, 3, le)
            rep = QSpinBox(); rep.setRange(1, 100); rep.setValue(s.get("repeat", 1)); rep.setFixedWidth(60)
            seq_tbl.setCellWidget(r, 4, rep)
            db = QPushButton("X")
            db.setStyleSheet("background:#b71c1c;padding:2px 8px")
            db.clicked.connect(lambda _, b=db: self._remove_seq_row(seq_tbl, b))
            seq_tbl.setCellWidget(r, 5, db)
        ly.addWidget(seq_tbl)

        def add_row():
            r = seq_tbl.rowCount()
            seq_tbl.insertRow(r)
            seq_tbl.setCellWidget(r, 0, self._script_combo_widget())
            seq_tbl.setCellWidget(r, 1, self._world_spin_widget())
            seq_tbl.setCellWidget(r, 2, self._stop_combo_widget())
            seq_tbl.setCellWidget(r, 3, QLineEdit())
            rep = QSpinBox(); rep.setRange(1, 100); rep.setValue(1); rep.setFixedWidth(60)
            seq_tbl.setCellWidget(r, 4, rep)
            db = QPushButton("X")
            db.setStyleSheet("background:#b71c1c;padding:2px 8px")
            db.clicked.connect(lambda _, b=db: self._remove_seq_row(seq_tbl, b))
            seq_tbl.setCellWidget(r, 5, db)

        add_btn = QPushButton("+ Add Script")
        add_btn.clicked.connect(add_row)
        ly.addWidget(add_btn)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        # Explicitly wire Save button clicked to accept (PyQt6 safety)
        save_btn = bb.button(QDialogButtonBox.StandardButton.Save)
        if save_btn:
            save_btn.clicked.connect(dlg.accept)
        ly.addWidget(bb)

        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        result = dlg.exec()
        if result != QDialog.DialogCode.Accepted:
            dlg.deleteLater()
            return

        # Update mode
        if entry:
            self.db.update_queue_entry_mode(qid, mode_combo.currentText())
        # Delete old scripts and re-add
        for s in scripts:
            self.db.delete_queue_script(s["id"])
        for r in range(seq_tbl.rowCount()):
            scw = seq_tbl.cellWidget(r, 0)
            wsw = seq_tbl.cellWidget(r, 1)
            stw = seq_tbl.cellWidget(r, 2)
            vle = seq_tbl.cellWidget(r, 3)
            rep_w = seq_tbl.cellWidget(r, 4)
            repeat = rep_w.value() if rep_w else 1
            if scw:
                self.db.add_queue_script(qid, scw.currentText(), wsw.value(), stw.currentText(), vle.text(), order_index=r, repeat=repeat)
        dlg.deleteLater()
        self._queue_sel_change()
        self._refresh_queue_table()

    def _queue_start(self):
        pending = [q for q in self.db.get_queue_entries() if q["status"] == "Pending"]
        print(f"[QUEUE START] Pending entries: {len(pending)}")
        if not pending:
            QMessageBox.information(self, "Empty", "No pending queue entries.")
            return
        self._process_queue_step()
        # Quick retry: if nothing launched (all still Pending), retry after 3s
        QTimer.singleShot(3000, self._retry_queue_if_stuck)

    def _queue_stop(self):
        active = self.db.get_active_queue_scripts()
        stopped = 0
        for s in active:
            pid = s.get("pid")
            if pid:
                try: psutil.Process(pid).terminate()
                except psutil.NoSuchProcess: pass
            self.db.update_queue_script_status(s["id"], "Stopped")
            self.db.update_queue_entry_status(s["queue_id"], "Stopped")
            self.db.record_stop(s["account_id"])
            self._queue_logs.pop(s["id"], None)
            done_file = self._queue_done_files.pop(s["id"], None)
            if done_file and os.path.isfile(done_file):
                try: os.remove(done_file)
                except: pass
            stopped += 1
        if stopped > 0:
            self._last_sequential_stop = time.time()
        self._refresh_all()
        self._refresh_queue_table()
        self.status.setText(f"Stopped {stopped} queue scripts")

    def _retry_queue_if_stuck(self):
        """Retry launch if queue entries are still Pending with nothing Running."""
        pending = [q for q in self.db.get_queue_entries() if q["status"] == "Pending"]
        running = [q for q in self.db.get_queue_entries() if q["status"] == "Running"]
        if pending and not running and not self._batch_launching and not self._batch_stagger_timer.isActive():
            print(f"[RETRY] {len(pending)} entries still pending, retrying launch...")
            self._process_queue_step()

    def _on_batch_stagger_timer(self):
        """Timer callback: safely advance batch after stagger delay."""
        self._batch_launching = False
        self._process_queue_step()

    def _process_queue_step(self):
        """Launch the next pending script in queue entries. Supports batch mode with staggered launches."""
        print(f"[PROCESS QUEUE] batch_launching={self._batch_launching}, stagger_active={self._batch_stagger_timer.isActive()}")
        # Re-entry guard for batch mode
        if self._batch_launching:
            print("[BATCH] Launch already in progress, skipping _process_queue_step call")
            return

        # Also skip if stagger timer is about to fire (batch in progress)
        if self._batch_stagger_timer.isActive():
            print("[BATCH] Stagger timer active, skipping _process_queue_step call")
            return

        # Check if there are any Pending entries at all
        all_pending = [q for q in self.db.get_queue_entries() if q["status"] == "Pending"]
        print(f"[PROCESS QUEUE] Pending entries: {len(all_pending)}")
        if not all_pending:
            return

        # Determine execution mode from pending entries
        has_batches = any(q.get("batch_group", 0) > 0 for q in all_pending)
        print(f"[PROCESS QUEUE] Batch mode: {has_batches}")
        if has_batches:
            self._process_batch_queue_step()
        else:
            self._process_legacy_queue_step()

    def _process_legacy_queue_step(self):
        """Original queue logic for non-batch entries (batch_group=0)."""
        pending = [q for q in self.db.get_queue_entries() if q["status"] == "Pending"]
        if not pending:
            return
        running_entries = [q for q in self.db.get_queue_entries() if q["status"] == "Running"]
        sequential_running = any((q.get("mode", "parallel") == "sequential") for q in running_entries)
        launched_this_pass = False

        # Check account conflicts - prevent same account from running in multiple queues
        running_account_ids = set()
        for running in running_entries:
            running_account_ids.add(running["account_id"])

        for entry in pending:
            # Skip if account is already running in another queue
            if entry["account_id"] in running_account_ids:
                continue

            if entry.get("mode", "parallel") == "sequential" and (sequential_running or launched_this_pass):
                continue
            # Sequential cooldown: wait 10s after stopping so DreamBot releases the slot
            if entry.get("mode", "parallel") == "sequential":
                elapsed = time.time() - self._last_sequential_stop
                if self._last_sequential_stop and elapsed < 10:
                    print(f"[QUEUE] Sequential cooldown: waiting {10 - int(elapsed)}s before next launch")
                    continue
            if self._launch_queue_entry(entry):
                launched_this_pass = True
                if entry.get("mode", "parallel") == "sequential":
                    break
        self._refresh_all()
        self._refresh_queue_table()

    def _process_batch_queue_step(self):
        """Batch queue logic: launch groups in parallel with stagger, wait for batch to finish before next."""
        # Re-entry guard + minimum interval guard
        elapsed = time.time() - self._last_batch_launch_time
        print(f"[BATCH STEP] elapsed={elapsed:.1f}s since last launch")
        if elapsed < 2:
            print(f"[BATCH] Too soon since last launch ({elapsed:.1f}s), waiting...")
            return

        # Find which batch group is currently running
        active_batches = set()
        running = [q for q in self.db.get_queue_entries() if q["status"] == "Running"]
        for r in running:
            bg = r.get("batch_group", 0)
            if bg > 0:
                active_batches.add(bg)

        print(f"[BATCH STEP] Active batches: {active_batches}, Running entries: {len(running)}")

        # If a batch is running, don't start a new batch yet
        if active_batches:
            current_batch = min(active_batches)
            pending_in_batch = self.db.get_pending_batch_entries(current_batch)
            print(f"[BATCH STEP] Batch {current_batch} has {len(pending_in_batch)} pending entries")
            if pending_in_batch:
                print(f"[BATCH] Batch {current_batch} active, {len(pending_in_batch)} pending remaining.")
                self._launch_batch_accounts(current_batch)
            else:
                print(f"[BATCH] Batch {current_batch} fully launched. Waiting for all to finish before next batch.")
            return

        # No active batch - find next pending batch group
        next_batch = self.db.get_next_pending_batch_group()
        print(f"[BATCH STEP] Next pending batch group: {next_batch}")
        if next_batch is None:
            print("[BATCH] No pending batches found.")
            return

        print(f"[BATCH] Starting batch group {next_batch}")
        self._current_batch_group = next_batch
        self._launch_batch_accounts(next_batch)

    def _launch_batch_accounts(self, batch_group):
        """Launch exactly ONE Pending account in a batch group, then schedule next via timer."""
        if self._batch_launching:
            print("[BATCH] Already launching, skipping.")
            return

        pending_in_batch = self.db.get_pending_batch_entries(batch_group)
        print(f"[BATCH] _launch_batch_accounts(batch={batch_group}): {len(pending_in_batch)} pending")
        if not pending_in_batch:
            return

        self._batch_launching = True
        self._last_batch_launch_time = time.time()

        entry = pending_in_batch[0]  # launch the first pending entry only
        launched = False
        try:
            launched = self._launch_queue_entry(entry)
        except Exception as e:
            print(f"[BATCH] _launch_queue_entry CRASHED for entry {entry['id']}: {e}")
            import traceback
            traceback.print_exc()
        if launched:
            remaining = self.db.get_pending_batch_entries(batch_group)
            if remaining:
                delay_ms = self._batch_stagger_delay * 1000
                print(f"[BATCH] Launched 1, {len(remaining)} remaining. Next in {self._batch_stagger_delay}s (batch {batch_group})")
                self._batch_stagger_timer.start(delay_ms)
            else:
                print(f"[BATCH] All accounts in batch {batch_group} launched. Waiting for batch to finish...")
                self._batch_launching = False
        else:
            print(f"[BATCH] Launch failed for entry {entry['id']}, will retry next cycle")
            self._batch_launching = False

        self._refresh_all()
        self._refresh_queue_table()

    def _launch_queue_entry(self, entry):
        """Launch a single queue entry. Returns True if launched successfully."""
        qid = entry["id"]
        aid = entry["account_id"]
        print(f"[LAUNCH ENTRY] qid={qid} aid={aid}")
        next_script = self.db.get_next_pending_queue_script(qid)
        if not next_script:
            print(f"[LAUNCH ENTRY] qid={qid} no pending scripts, marking Finished")
            self.db.update_queue_entry_status(qid, "Finished")
            return False

        # Ensure session if needed
        acc = self.db.get_account(aid)
        if not acc:
            print(f"[LAUNCH ENTRY] qid={qid} account {aid} not found")
            self.db.update_queue_entry_status(qid, "Stopped")
            return False
        sid = (acc.get("session_id") or "").strip()
        cid = (acc.get("character_id") or "").strip()
        if not sid or not cid:
            self.status.setText(f"Getting session for queue entry {qid}...")
            QApplication.processEvents()
            try:
                ok = self._ensure_session(aid, quiet=True)
            except Exception as e:
                print(f"[LAUNCH ENTRY] qid={qid} _ensure_session crashed: {e}")
                ok = False
            if not ok:
                print(f"[LAUNCH ENTRY] qid={qid} session fetch failed, marking Stopped")
                self.db.update_queue_entry_status(qid, "Stopped")
                return False
            acc = self.db.get_account(aid)
            if not acc:
                self.db.update_queue_entry_status(qid, "Stopped")
                return False
            sid = (acc.get("session_id") or "").strip()
            cid = (acc.get("character_id") or "").strip()
            if not sid or not cid:
                print(f"[LAUNCH ENTRY] qid={qid} still no session after fetch, marking Stopped")
                self.db.update_queue_entry_status(qid, "Stopped")
                return False

        # Launch the script
        java = self._find_java()
        path = os.path.expandvars(self.jar.text())
        if not os.path.isfile(path):
            QMessageBox.critical(self, "Not Found", f"JAR not found:\n{path}")
            return False
        script = next_script["script_name"]

        # Validate script name against DreamBot's known list
        discovered = self._discover_scripts()
        discovered_set = set(discovered)
        if script not in discovered_set:
            lower_script = script.lower()
            if lower_script in self.SCRIPT_NAME_MAP:
                corrected = self.SCRIPT_NAME_MAP[lower_script]
                print(f"[QUEUE FIX] Mapped script name '{script}' -> '{corrected}'")
                script = corrected
            else:
                lower_map = {n.lower(): n for n in discovered}
                if lower_script in lower_map:
                    corrected = lower_map[lower_script]
                    print(f"[QUEUE FIX] Auto-corrected script name '{script}' -> '{corrected}'")
                    script = corrected
                else:
                    print(f"[QUEUE ERROR] Script '{script}' not found in DreamBot. Skipping queue entry {qid}.")
                    self.status.setText(f"Script '{script}' not recognized — check queue setup")
                    self.db.update_queue_entry_status(qid, "Stopped")
                    self.db.update_queue_script_status(next_script["id"], "Stopped")
                    return False

        world = next_script["world"]
        nick = (acc.get("username") or "").strip() or acc["email"].split("@")[0]

        # Build proxy JVM args if account has a proxy
        proxy_args = []
        proxy_id = acc.get("proxy_id")
        if proxy_id:
            p = self.db.list_proxies()
            for px in p:
                if px["id"] == proxy_id:
                    proto = px.get("protocol", "http")
                    phost = px["host"]
                    pport = px["port"]
                    puser = px.get("username", "")
                    ppass = px.get("password", "")
                    if proto == "socks5":
                        proxy_args.append(f"-DsocksProxyHost={phost}")
                        proxy_args.append(f"-DsocksProxyPort={pport}")
                        if puser and ppass:
                            proxy_args.append(f"-Djava.net.socks.username={puser}")
                            proxy_args.append(f"-Djava.net.socks.password={ppass}")
                    else:
                        proxy_args.append(f"-Dhttp.proxyHost={phost}")
                        proxy_args.append(f"-Dhttp.proxyPort={pport}")
                        proxy_args.append(f"-Dhttps.proxyHost={phost}")
                        proxy_args.append(f"-Dhttps.proxyPort={pport}")
                        if puser and ppass:
                            proxy_args.append(f"-Dhttp.proxyUser={puser}")
                            proxy_args.append(f"-Dhttp.proxyPassword={ppass}")
                            proxy_args.append(f"-Dhttps.proxyUser={puser}")
                            proxy_args.append(f"-Dhttps.proxyPassword={ppass}")
                    break

        # Bank cache bootstrapper wrapping
        actual_script = script.strip()
        use_bootstrapper = getattr(self, "cb_bank_cache", None) and self.cb_bank_cache.isChecked()
        if use_bootstrapper and actual_script:
            proxy_args.append(f"-Dbankcache.target.script={actual_script}")
            proxy_args.append(f"-Dbankcache.account.name={nick}")
            try:
                import tempfile
                fb_script = os.path.join(tempfile.gettempdir(), "bankcache_target_script.txt")
                with open(fb_script, "w", encoding="utf-8") as f:
                    f.write(actual_script)
                fb_name = os.path.join(tempfile.gettempdir(), "bankcache_account_name.txt")
                with open(fb_name, "w", encoding="utf-8") as f:
                    f.write(nick)
            except Exception:
                pass
            actual_script = "BibsTheKing"
        cmd = [java] + proxy_args + ["-Xmx512M", "-jar", os.path.abspath(path), "-script", actual_script]
        if world: cmd.extend(["-world", str(world)])
        if sid:
            cmd.append(f"-sessionId={sid}")
            if cid: cmd.append(f"-characterId={cid}")
            if nick: cmd.append(f"-displayName={nick}")
        else:
            cmd.extend([f"-accountUsername={acc['email']}", f"-accountPassword={acc['password']}"])
            if acc.get("pin"): cmd.append(f"-accountPin={acc['pin']}")
            if acc.get("totp"): cmd.append(f"-accountTotp={acc['totp']}")
        log_dir = os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData\logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"queue_{qid}_{next_script['id']}_{datetime.now().strftime('%H%M%S')}.log")
        done_file = os.path.join(log_dir, f"done_{qid}_{next_script['id']}.txt")
        if os.path.isfile(done_file):
            try: os.remove(done_file)
            except: pass
        cmd.append(f"-doneFile={done_file}")
        try:
            # Kill any existing DreamBot client for this account before launching next
            for r in self.db.get_running():
                if r["id"] == aid:
                    pid = r.get("pid")
                    if pid and psutil.pid_exists(pid):
                        try:
                            psutil.Process(pid).terminate()
                        except psutil.NoSuchProcess:
                            pass
                    self.db.record_stop(aid)
                    break
            else:
                self.db.record_stop(aid)
            with open(log_file, "w") as out:
                out.write(f"CMD: {' '.join(cmd)}\n\n")
                out.flush()
                proc = subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(path)),
                                      stdout=out, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            self.db.record_launch(aid, script, pid=proc.pid, status="Running")
            self.db.update_queue_entry_status(qid, "Running")
            self.db.update_queue_script_status(next_script["id"], "Running", pid=proc.pid)
            self.db.update_queue_entry_current_idx(qid, next_script["order_index"])
            self._queue_logs[next_script["id"]] = log_file
            self._queue_done_files[next_script["id"]] = done_file
            if getattr(self, "cb_bank_cache", None) and self.cb_bank_cache.isChecked():
                self._bootstrapper_pids.add(proc.pid)
                print(f"[BANK CACHE] Tracking bootstrapper PID {proc.pid} for account {aid}")
            QTimer.singleShot(10000, lambda lp=proc.pid, a=aid, qs=next_script["id"]: self._resolve_real_pid(lp, a, qs))
            print(f"[LAUNCH ENTRY] qid={qid} launched OK, PID={proc.pid}, script={actual_script}")
            return True
        except Exception as e:
            print(f"[LAUNCH ENTRY] qid={qid} launch FAILED: {e}")
            self.db.update_queue_entry_status(qid, "Stopped")
            return False

    def _resolve_real_pid(self, launcher_pid, account_id, queue_script_id=None, attempt=1):
        """Replace launcher PID with the actual DreamBot client PID."""
        real_pid = None
        acc = self.db.get_account(account_id)
        nick = (acc.get("username") or "").strip().lower() if acc else ""
        email = (acc.get("email") or "").strip().lower() if acc else ""
        # First try children of launcher
        try:
            launcher = psutil.Process(launcher_pid)
            children = launcher.children(recursive=True)
            for child in children:
                try:
                    if child.is_running():
                        exe = (child.exe() or "").lower()
                        if "java" in exe or "javaw" in exe:
                            real_pid = child.pid
                            break
                except:
                    pass
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            print(f"[PID] Error resolving children: {e}")

        # If launcher dead or no child found, scan all java/openjdk processes
        if not real_pid:
            candidates = []
            now = time.time()
            for proc in psutil.process_iter(attrs=["pid", "name", "exe", "cmdline", "create_time"]):
                try:
                    pinfo = proc.info
                    name = (pinfo.get("name") or "").lower()
                    exe = (pinfo.get("exe") or "").lower()
                    if "java" in name or "javaw" in name or "openjdk" in name or "java" in exe:
                        # Skip if already tracked by another profile runner account
                        already_tracked = False
                        for other_aid, other_info in self._profile_runner_running.items():
                            if other_aid != account_id and other_info.get("pid") == proc.pid:
                                already_tracked = True
                                break
                        if already_tracked:
                            continue
                        cmd = " ".join(pinfo.get("cmdline") or []).lower()
                        score = 0
                        if nick and nick in cmd:
                            score += 10
                        if email and email in cmd:
                            score += 10
                        if str(launcher_pid) in cmd:
                            score += 5
                        # Only consider processes started in the last 2 minutes
                        create_time = pinfo.get("create_time", 0)
                        if now - create_time > 120:
                            continue
                        candidates.append((score, create_time, proc.pid))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if candidates:
                # Pick highest score, then newest create_time
                candidates.sort(key=lambda x: (-x[0], -x[1]))
                real_pid = candidates[0][2]

        if real_pid and real_pid != launcher_pid:
            # Extra guard: ensure this PID isn't already tracked by another profile runner account
            for other_aid, other_info in self._profile_runner_running.items():
                if other_aid != account_id and other_info.get("pid") == real_pid:
                    print(f"[PID] PID {real_pid} already tracked by account {other_aid}, retrying for {account_id}")
                    max_attempts = 10 if account_id in self._profile_runner_running else 4
                    if attempt < max_attempts:
                        QTimer.singleShot(10000, lambda: self._resolve_real_pid(launcher_pid, account_id, queue_script_id, attempt + 1))
                    return
            try:
                self.db.update_launch_pid(account_id, real_pid)
                if queue_script_id:
                    self.db.update_queue_script_status(queue_script_id, "Running", pid=real_pid)
                # Update bootstrapper tracking if launcher was tracked
                if launcher_pid in self._bootstrapper_pids:
                    self._bootstrapper_pids.discard(launcher_pid)
                    self._bootstrapper_pids.add(real_pid)
                # Update profile runner tracking if account is managed by it
                if account_id in self._profile_runner_running:
                    self._profile_runner_running[account_id]["pid"] = real_pid
                    self._profile_runner_running[account_id]["real_pid_resolved"] = True
                    print(f"[PID] Profile runner {launcher_pid} -> {real_pid} (account={account_id})")
                    # If batch mode is waiting for this account's PID, continue to next launch
                    if getattr(self, "_profile_runner_wait_pid_aid", None) == account_id:
                        self._profile_runner_wait_pid_aid = None
                        delay_ms = self._profile_runner_stagger * 1000
                        print(f"[PROFILE RUNNER] PID resolved for account {account_id}, scheduling next in {self._profile_runner_stagger}s")
                        self._profile_runner_stagger_timer.start(delay_ms)
                else:
                    print(f"[PID] {launcher_pid} -> {real_pid} (account={account_id})")
                return
            except Exception as e:
                print(f"[PID] DB update failed: {e}")

        max_attempts = 10 if account_id in self._profile_runner_running else 4
        if attempt < max_attempts:
            QTimer.singleShot(10000, lambda: self._resolve_real_pid(launcher_pid, account_id, queue_script_id, attempt + 1))

    def _cat_click(self, item):
        self.current_cat = item.data(Qt.ItemDataRole.UserRole)
        self._refresh_table()

    def _search(self, text):
        self.search_text = text.strip()
        self._refresh_table()

    def _filter(self, text):
        self.current_cat = "All Accounts" if text == "All Categories" else text
        self._refresh_sidebar(); self._refresh_table()

    def _sel_change(self):
        sel = self.tbl.selectedItems()
        self.selected_ids = []
        if sel:
            for row in set(i.row() for i in sel):
                it = self.tbl.item(row,0)
                if it: self.selected_ids.append(int(it.text()))
        if len(self.selected_ids) == 1:
            self._show_details(self.db.get_account(self.selected_ids[0]))
        elif len(self.selected_ids) > 1:
            self.dl.setText(f"{len(self.selected_ids)} accounts selected")
        else:
            self.dl.setText("Select an account")

    def _show_details(self, acc):
        if not acc: return
        self.dl.setText("<br>".join([
            f"<b>ID:</b> {acc['id']}", f"<b>Email:</b> {acc['email']}",
            f"<b>Password:</b> {acc['password']}", f"<b>PIN:</b> {acc.get('pin','N/A')}",
            f"<b>TOTP:</b> {acc.get('totp','N/A')}",
            f"<b>Display Name:</b> {acc.get('display_name','N/A')}",
            f"<b>Category:</b> {acc.get('category','Uncategorized')}",
            f"<b>Status:</b> {acc.get('status','Offline')}",
            f"<b>Notes:</b> {acc.get('notes','N/A')}",
            f"<b>Created:</b> {acc.get('created_at','')}"
        ]))

    def _ctx_menu(self, pos):
        m = QMenu(self)
        m.addAction("Edit", self._edit_sel)
        sc = QMenu("Set Category", self)
        for cat in CATEGORIES[1:]:
            a = QAction(cat, self)
            a.triggered.connect(lambda c, cat=cat: self._set_cat(cat))
            sc.addAction(a)
        m.addMenu(sc)
        ss = QMenu("Set Status", self)
        for s in STATUSES:
            a = QAction(s, self)
            a.triggered.connect(lambda c, s=s: self._set_status(s))
            ss.addAction(a)
        m.addMenu(ss)
        m.exec(self.tbl.viewport().mapToGlobal(pos))

    def _add_dlg(self):
        dlg = AccountDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            if self.db.add_account(**data):
                self._refresh_all(); self.status.setText(f"Added: {data['email']}")
            else:
                QMessageBox.warning(self,"Duplicate","Email already exists.")

    def _bulk_dlg(self):
        dlg = BulkDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            accs = dlg.get_accounts()
            added = skipped = 0
            for a in accs:
                if self.db.add_account(**a): added += 1
                else: skipped += 1
            self._refresh_all()
            self.status.setText(f"Added {added}, skipped {skipped}")
            QMessageBox.information(self,"Done",f"Added: {added}\nSkipped: {skipped}")

    def _edit_sel(self):
        if not self.selected_ids: return
        acc = self.db.get_account(self.selected_ids[0])
        if not acc: return
        dlg = AccountDialog(self, acc)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.db.update_account(acc["id"], **dlg.get_data())
            self._refresh_all()

    def _set_cat(self, cat):
        if not self.selected_ids: return
        for i in self.selected_ids: self.db.update_account(i, category=cat)
        self._refresh_all()
        self.status.setText(f"Category set: {cat}")

    def _set_status(self, st):
        if not self.selected_ids: return
        for i in self.selected_ids: self.db.update_account(i, status=st)
        self._refresh_all()

    def _del_sel(self):
        if not self.selected_ids:
            QMessageBox.information(self,"None","Select accounts to delete.")
            return
        if QMessageBox.question(self,"Delete",f"Delete {len(self.selected_ids)} account(s)?",
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            for i in self.selected_ids: self.db.delete_account(i)
            self._refresh_all()
            self.status.setText(f"Deleted {len(self.selected_ids)}")

    def _clear_all(self):
        if QMessageBox.question(self,"Clear All","Delete ALL accounts from the farm database?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            n = self.db.clear_all_accounts()
            self._refresh_all()
            self.status.setText(f"Cleared {n} accounts")

    def _push_to_dreambot(self):
        if not self.selected_ids:
            QMessageBox.information(self, "None", "Select accounts to push to DreamBot.")
            return
        db_path = os.path.expandvars(r"%LOCALAPPDATA%\DreamBot\accounts.json")
        existing = []
        if os.path.isfile(db_path):
            try:
                import json
                with open(db_path, "r") as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
        # Build lookup by email
        by_email = {(e.get("username") or "").strip(): e for e in existing if isinstance(e, dict)}
        pushed = updated = 0
        for aid in self.selected_ids:
            acc = self.db.get_account(aid)
            if not acc: continue
            email = acc["email"].strip()
            pwd = acc["password"].strip()
            if not email or not pwd:
                continue
            nick = (acc.get("username") or "").strip() or email.split("@")[0]
            pin = acc.get("pin", "").strip()
            totp = acc.get("totp", "").strip()
            entry = {
                "username": email,
                "password": pwd,
                "type": "JAGEX",
                "nickname": nick,
            }
            if pin:
                entry["pin"] = pin
            if totp:
                entry["totp"] = totp
                entry["token"] = totp  # compatibility for older DreamBot versions
            by_email[email] = entry
            if email in by_email and email != list(by_email.keys())[list(by_email.values()).index(entry)]:
                pass  # already counted
            pushed += 1
        # Write back
        try:
            import json
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            with open(db_path, "w") as f:
                json.dump(list(by_email.values()), f, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to write accounts.json:\n{str(e)}")
            return
        self._refresh_all()
        QMessageBox.information(self, "Pushed", f"Pushed {pushed} account(s) to DreamBot.\nRestart DreamBot to see them.")

    def _launch_sel(self):
        if not self.selected_ids:
            QMessageBox.information(self,"None","Select accounts to launch.")
            return
        dlg = LaunchDialog(self, len(self.selected_ids), self._discover_scripts())
        if dlg.exec() == QDialog.DialogCode.Accepted:
            sn, w = dlg.get_params()
            self._do_launch(self.selected_ids, sn, w)

    def _quick_launch(self):
        if not self.selected_ids:
            QMessageBox.information(self,"None","Select accounts to launch.")
            return
        self._do_launch(self.selected_ids, self.sc.currentText(), self.ws.value())

    def _find_java(self):
        for name in ["javaw","java"]:
            for p in os.environ.get("PATH","").split(os.pathsep):
                jp = os.path.join(p, name + ".exe")
                if os.path.isfile(jp): return jp
        for base in [os.path.expandvars(r"%PROGRAMFILES%\Java"), os.path.expandvars(r"%PROGRAMFILES(X86)%\Java")]:
            if os.path.isdir(base):
                for root, _, files in os.walk(base):
                    for name in ["javaw.exe","java.exe"]:
                        if name in files: return os.path.join(root, name)
        return "java"

    def _do_launch(self, ids, script, world):
        # Validate script name against DreamBot's known list (with manual + case-insensitive fallback)
        discovered = self._discover_scripts()
        discovered_set = set(discovered)
        if script not in discovered_set:
            lower_script = script.lower()
            # 1) Check manual name mapping first
            if lower_script in self.SCRIPT_NAME_MAP:
                corrected = self.SCRIPT_NAME_MAP[lower_script]
                print(f"[LAUNCH FIX] Mapped script name '{script}' -> '{corrected}'")
                script = corrected
            else:
                # 2) Try case-insensitive match
                lower_map = {n.lower(): n for n in discovered}
                if lower_script in lower_map:
                    corrected = lower_map[lower_script]
                    print(f"[LAUNCH FIX] Auto-corrected script name '{script}' -> '{corrected}'")
                    script = corrected
                else:
                    QMessageBox.critical(self, "Invalid Script",
                        f"Script '{script}' is not recognized by DreamBot.\n\n"
                        f"Please select from the dropdown list.")
                    return
        path = os.path.expandvars(self.jar.text())
        if not os.path.isfile(path):
            QMessageBox.critical(self,"Not Found",f"JAR not found:\n{path}")
            return
        # Validate it's actually a JAR file (ZIP format)
        try:
            sz = os.path.getsize(path)
            if sz == 0:
                QMessageBox.critical(self,"Invalid JAR",f"JAR file is empty (0 bytes):\n{path}")
                return
            with open(path, "rb") as f:
                header = f.read(4)
            if header[:2] != b"PK":
                QMessageBox.critical(self,"Invalid JAR",f"File is not a valid JAR/ZIP archive:\n{path}\nHeader: {header.hex()}")
                return
        except Exception as e:
            QMessageBox.critical(self,"JAR Check Error",f"Failed to read JAR file:\n{path}\nError: {e}")
            return
        java = self._find_java()
        launched = 0
        errors = []
        log_dir = os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData\logs")
        os.makedirs(log_dir, exist_ok=True)
        for aid in ids:
            acc = self.db.get_account(aid)
            if not acc: continue
            email = acc["email"].strip()
            pwd = acc["password"].strip()
            if not email or not pwd:
                continue
            nick = (acc.get("username") or "").strip() or email.split("@")[0]
            sid = (acc.get("session_id") or "").strip()
            cid = (acc.get("character_id") or "").strip()
            # Auto-get session if missing (sequential — one at a time)
            if not sid or not cid:
                self.status.setText(f"Getting session for {nick} before launch...")
                ok = self._ensure_session(aid, quiet=True)
                if not ok:
                    errors.append(f"{nick}: Failed to get Jagex session")
                    continue
                # Refresh account data after session getter
                acc = self.db.get_account(aid)
                sid = (acc.get("session_id") or "").strip()
                cid = (acc.get("character_id") or "").strip()
            # Build proxy JVM args if account has a proxy
            proxy_args = []
            proxy_id = acc.get("proxy_id")
            if proxy_id:
                p = self.db.list_proxies()
                for px in p:
                    if px["id"] == proxy_id:
                        proto = px.get("protocol", "http")
                        phost = px["host"]
                        pport = px["port"]
                        puser = px.get("username", "")
                        ppass = px.get("password", "")
                        
                        if proto == "socks5":
                            proxy_args.append(f"-DsocksProxyHost={phost}")
                            proxy_args.append(f"-DsocksProxyPort={pport}")
                            # Add authentication for SOCKS5 if credentials provided
                            if puser and ppass:
                                proxy_args.append(f"-Djava.net.socks.username={puser}")
                                proxy_args.append(f"-Djava.net.socks.password={ppass}")
                        else:
                            # HTTP/HTTPS proxy with authentication support
                            proxy_args.append(f"-Dhttp.proxyHost={phost}")
                            proxy_args.append(f"-Dhttp.proxyPort={pport}")
                            proxy_args.append(f"-Dhttps.proxyHost={phost}")
                            proxy_args.append(f"-Dhttps.proxyPort={pport}")
                            # Add authentication for HTTP/HTTPS if credentials provided
                            if puser and ppass:
                                # For HTTP proxy authentication
                                proxy_args.append(f"-Dhttp.proxyUser={puser}")
                                proxy_args.append(f"-Dhttp.proxyPassword={ppass}")
                                proxy_args.append(f"-Dhttps.proxyUser={puser}")
                                proxy_args.append(f"-Dhttps.proxyPassword={ppass}")
                        break
            # Bank cache bootstrapper wrapping
            actual_script = script.strip()
            use_bootstrapper = getattr(self, "cb_bank_cache", None) and self.cb_bank_cache.isChecked()
            if use_bootstrapper and actual_script:
                proxy_args.append(f"-Dbankcache.target.script={actual_script}")
                proxy_args.append(f"-Dbankcache.account.name={nick}")
                # Write fallback files in case JVM properties don't survive launcher process spawn
                try:
                    import tempfile
                    fb_script = os.path.join(tempfile.gettempdir(), "bankcache_target_script.txt")
                    with open(fb_script, "w", encoding="utf-8") as f:
                        f.write(actual_script)
                    fb_name = os.path.join(tempfile.gettempdir(), "bankcache_account_name.txt")
                    with open(fb_name, "w", encoding="utf-8") as f:
                        f.write(nick)
                except Exception:
                    pass
                actual_script = "BibsTheKing"
            cmd = [java] + proxy_args + ["-Xmx512M", "-jar", os.path.abspath(path)]
            if actual_script: cmd.extend(["-script", actual_script])
            if world: cmd.extend(["-world", str(world)])
            # Prefer session ID if available (skips login flow)
            if sid:
                cmd.append(f"-sessionId={sid}")
                if cid:
                    cmd.append(f"-characterId={cid}")
                if nick:
                    cmd.append(f"-displayName={nick}")
            else:
                # Fall back to raw credentials
                cmd.extend([f"-accountUsername={email}", f"-accountPassword={pwd}"])
                if acc.get("pin"):
                    cmd.append(f"-accountPin={acc['pin']}")
                if acc.get("totp"):
                    cmd.append(f"-accountTotp={acc['totp']}")
            log_file = os.path.join(log_dir, f"launch_{nick}_{datetime.now().strftime('%H%M%S')}.log")
            # Create done file for script end detection
            done_file = os.path.join(log_dir, f"quick_done_{aid}_{datetime.now().strftime('%H%M%S')}.txt")
            cmd.append(f"-doneFile={done_file}")
            # Store done file for monitoring
            self._queue_done_files[aid] = done_file
            try:
                # Clean up any old running entries for this account first
                self.db.record_stop(aid)
                with open(log_file, "w") as out:
                    out.write(f"CMD: {' '.join(cmd)}\n\n")
                    out.flush()
                    proc = subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(path)),
                                          stdout=out, stderr=subprocess.STDOUT,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
                self.db.record_launch(aid, script, pid=proc.pid, status="Running")
                if getattr(self, "cb_bank_cache", None) and self.cb_bank_cache.isChecked():
                    self._bootstrapper_pids.add(proc.pid)
                    print(f"[BANK CACHE] Tracking bootstrapper PID {proc.pid} for quick launch account {aid}")
                QTimer.singleShot(10000, lambda lp=proc.pid, a=aid: self._resolve_real_pid(lp, a))
                launched += 1
                # 10s delay between launches to avoid overwhelming the system
                if aid != ids[-1]:
                    time.sleep(10)
            except Exception as e:
                errors.append(f"{nick}: {e}")
                break
        self._refresh_all()
        self.status.setText(f"Launched {launched} with {script}")
        if errors:
            QMessageBox.critical(self, "Launch Error", "\n".join(errors))

    def _stop_sel(self):
        if not self.selected_ids:
            QMessageBox.information(self,"None","Select accounts to stop.")
            return
        stopped = 0
        for r in self.db.get_running():
            if r["id"] in self.selected_ids:
                pid = r.get("pid")
                if pid:
                    try: psutil.Process(pid).terminate(); stopped += 1
                    except psutil.NoSuchProcess: pass
                self.db.record_stop(r["id"])
        self._refresh_all()
        self.status.setText(f"Stopped {stopped}")

    def _sync_account_status(self):
        """Scan running java/openjdk processes and sync account Running/Offline status."""
        changed = False
        # Map running java processes by cmdline keywords
        proc_map = {}  # pid -> cmd
        for proc in psutil.process_iter(attrs=["pid", "name", "exe", "cmdline"]):
            try:
                pinfo = proc.info
                name = (pinfo.get("name") or "").lower()
                exe = (pinfo.get("exe") or "").lower()
                if "java" not in name and "javaw" not in name and "openjdk" not in name and "java" not in exe:
                    continue
                cmd = " ".join(pinfo.get("cmdline") or []).lower()
                proc_map[pinfo["pid"]] = cmd
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Check all accounts
        for acc in self.db.list_accounts():
            aid = acc["id"]
            nick = (acc.get("username") or "").strip().lower()
            display = (acc.get("display_name") or "").strip().lower()
            email = acc["email"].strip().lower()
            email_prefix = email.split("@")[0] if "@" in email else email
            # Find matching running process
            matched_pid = None
            for pid, cmd in proc_map.items():
                if (nick and nick in cmd) or (display and display in cmd) or (email and email in cmd) or (email_prefix and email_prefix in cmd):
                    matched_pid = pid
                    break
            current_status = acc.get("status", "Offline")
            if matched_pid and current_status != "Running":
                # Account is running but DB says Offline — fix it
                self.db.update_account(aid, status="Running")
                # Ensure launch_history open entry exists with this PID
                running = self.db.get_running()
                found = any(r["id"] == aid for r in running)
                if not found:
                    self.db.record_launch(aid, acc.get("script_name") or "Unknown", pid=matched_pid, status="Running")
                else:
                    self.db.update_launch_pid(aid, matched_pid)
                changed = True
            elif not matched_pid and current_status == "Running":
                # Account not actually running but DB says Running — only stop if grace expired
                running_entry = None
                for r in self.db.get_running():
                    if r["id"] == aid:
                        running_entry = r
                        break
                if running_entry:
                    started = running_entry.get("started_at")
                    if started:
                        try:
                            if (datetime.now() - datetime.fromisoformat(started)).total_seconds() < 60:
                                continue
                        except: pass
                    self.db.record_stop(aid)
                    changed = True
        return changed

    def _finish_queue_script(self, qs):
        """Mark a queue script as finished, kill its process, and advance the queue."""
        aid = qs["account_id"]
        qid = qs["queue_id"]
        # Kill the actual Java process if still alive
        pid = qs.get("pid")
        if pid and psutil.pid_exists(pid):
            try:
                psutil.Process(pid).terminate()
                print(f"[QUEUE] Terminated PID {pid} for account {aid}")
            except psutil.NoSuchProcess:
                pass
        # Track cooldown for sequential mode so next launch waits 10s
        if qs.get("mode", "parallel") == "sequential":
            self._last_sequential_stop = time.time()
        # Close the launch history entry so account is no longer "Running"
        self.db.record_stop(aid)
        new_completed = (qs.get("completed_runs") or 0) + 1
        self.db.update_queue_script(qs["id"], completed_runs=new_completed)
        repeat = qs.get("repeat", 1) or 1
        if new_completed < repeat:
            self.db.update_queue_script_status(qs["id"], "Pending", pid=None)
            self.db.update_queue_entry_status(qid, "Pending")
        else:
            self.db.update_queue_script_status(qs["id"], "Finished")
            next_script = self.db.get_next_pending_queue_script(qid)
            if next_script:
                self.db.update_queue_entry_status(qid, "Pending")
            else:
                self.db.update_queue_entry_status(qid, "Finished")
        self._queue_logs.pop(qs["id"], None)
        done_file = self._queue_done_files.pop(qs["id"], None)
        if done_file and os.path.isfile(done_file):
            try: os.remove(done_file)
            except: pass
        return True

    def _check_done_file(self, qs):
        """Check if the script created a done-file signaling completion."""
        done_file = self._queue_done_files.get(qs["id"])
        if done_file and os.path.isfile(done_file):
            print(f"[QUEUE] Script {qs['script_name']} finished (done file found: {done_file})")
            return self._finish_queue_script(qs)
        return False

    def _check_script_log_finished(self, qs):
        """Read the script's log file looking for script-completion indicators."""
        log_file = self._queue_logs.get(qs["id"])
        if not log_file or not os.path.isfile(log_file):
            return False
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 50 * 1024))
                content = f.read().lower()
            stop_phrases = [
                "script stopped", "script finished", "stopping script",
                "[stopped]", "script end", "script complete",
                "onstop called", "script has ended", "script terminated",
                "donefile created", "done file created",
                "stopped bibs tut",
                "script stop triggered", "script stop condition met",
                "stop condition reached", "runtime reached", "target level reached",
                "items collected reached", "script stop",
            ]
            for phrase in stop_phrases:
                if phrase in content:
                    print(f"[QUEUE] Script {qs['script_name']} finished (log match: '{phrase}')")
                    return self._finish_queue_script(qs)
            # Debug: show last line of log if no match
            last_lines = content.strip().split("\n")[-3:]
            if last_lines and last_lines[-1].strip():
                print(f"[QUEUE] Log tail for {qs['script_name']}: {last_lines[-1][:120]}")
        except Exception as e:
            print(f"[QUEUE] Log check error for {qs['script_name']}: {e}")
        return False

    def _poll(self):
        changed = False
        queue_changed = False
        grace = 60  # seconds — DreamBot launcher may exit quickly
        now = datetime.now()
        # First sync status by scanning actual processes
        if self._sync_account_status():
            changed = True
        # Check regular launches (for non-process-matched or edge cases)
        for r in self.db.get_running():
            pid = r.get("pid")
            if pid:
                if not psutil.pid_exists(pid):
                    # Grace period: don't mark finished if just launched
                    started = r.get("started_at")
                    if started:
                        try:
                            if (now - datetime.fromisoformat(started)).total_seconds() < grace:
                                continue
                        except: pass
                    script = (r.get("script_name") or "").lower()
                    if "tut" in script or "tutorial" in script:
                        self.db.record_stop(r["id"], final_status="Offline")
                        acc = self.db.get_account(r["id"])
                        current_cat = (acc.get("category") or "") if acc else ""
                        # Only upgrade Tutorial Island → Ready To Farm; never stomp on other categories
                        if current_cat == "Tutorial Island":
                            self.db.update_account(r["id"], category="Ready To Farm")
                    else:
                        self.db.record_stop(r["id"])
                    changed = True
        # Check queue scripts — PID death OR log-based finish
        for qs in self.db.get_active_queue_scripts():
            pid = qs.get("pid")
            finished = False
            if pid and not psutil.pid_exists(pid):
                # Cross-check: real PID may exist in launch_history even if queue_scripts wasn't updated
                account_has_real_process = False
                for r in self.db.get_running():
                    if r["id"] == qs["account_id"]:
                        real_pid = r.get("pid")
                        if real_pid and psutil.pid_exists(real_pid):
                            self.db.update_queue_script_status(qs["id"], "Running", pid=real_pid)
                            account_has_real_process = True
                        break
                if account_has_real_process:
                    continue
                # Grace period
                started = qs.get("started_at")
                if started:
                    try:
                        if (now - datetime.fromisoformat(started)).total_seconds() < grace:
                            continue
                    except: pass
                if pid in self._bootstrapper_pids:
                    self._bootstrapper_pids.discard(pid)
                self._finish_queue_script(qs)
                queue_changed = True
            else:
                # Java process still alive
                if pid in self._bootstrapper_pids:
                    # Bootstrapper wrapper is managing script lifecycle internally;
                    # skip log/done detection and only rely on process death
                    pass
                else:
                    if self._check_done_file(qs):
                        queue_changed = True
                    elif self._check_script_log_finished(qs):
                        queue_changed = True
        # Check quick launches for script completion
        for r in self.db.get_running():
            aid = r["id"]
            pid = r.get("pid")
            # If this is a bootstrapper-launched client, skip log/done detection
            if pid and pid in self._bootstrapper_pids:
                if not psutil.pid_exists(pid):
                    self._bootstrapper_pids.discard(pid)
                    self.db.record_stop(aid)
                    changed = True
                continue

            # Check if this quick launch has a done file
            done_file = self._queue_done_files.get(aid)
            quick_finished = False
            if done_file and os.path.isfile(done_file):
                print(f"[QUICK LAUNCH] Script finished for account {aid} (done file)")
                quick_finished = True
            else:
                # Check log file for script end indicators
                log_dir = os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData\logs")
                log_files = [f for f in os.listdir(log_dir) if f.startswith(f"launch_{r.get('username', r['email'].split('@')[0])}_")]
                if log_files:
                    log_file = os.path.join(log_dir, log_files[-1])  # Use latest log
                    try:
                        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                            f.seek(0, 2)
                            size = f.tell()
                            f.seek(max(0, size - 50 * 1024))
                            content = f.read().lower()
                        stop_phrases = [
                            "script stopped", "script finished", "stopping script",
                            "[stopped]", "script end", "script complete",
                            "onstop called", "script has ended", "script terminated",
                            "donefile created", "done file created",
                            "stopped bibs tut", "script stop triggered",
                            "script stop condition met", "stop condition reached",
                            "runtime reached", "target level reached",
                            "items collected reached", "script stop",
                        ]
                        for phrase in stop_phrases:
                            if phrase in content:
                                print(f"[QUICK LAUNCH] Script finished for account {aid} (log match: '{phrase}')")
                                quick_finished = True
                                break
                    except Exception as e:
                        pass  # Ignore log reading errors

            if quick_finished:
                # Clean up done file
                if done_file and os.path.isfile(done_file):
                    self._queue_done_files.pop(aid, None)
                    try: os.remove(done_file)
                    except: pass
                # Terminate the process
                if pid and psutil.pid_exists(pid):
                    try:
                        psutil.Process(pid).terminate()
                    except psutil.NoSuchProcess:
                        pass
                self.db.record_stop(aid)
                changed = True

        # Advance queue ONLY if a queue script actually finished
        if queue_changed:
            self._process_queue_step()
            changed = True

        # Retry stuck Pending entries: if there are Pending queue entries but nothing Running,
        # and we're not in the middle of a batch launch, try to start them.
        pending_entries = [q for q in self.db.get_queue_entries() if q["status"] == "Pending"]
        running_entries = [q for q in self.db.get_queue_entries() if q["status"] == "Running"]
        if pending_entries and not running_entries and not self._batch_launching and not self._batch_stagger_timer.isActive():
            print(f"[POLL] Found {len(pending_entries)} pending entries with nothing running — retrying launch")
            self._process_queue_step()
            changed = True

        # Profile runner polling
        if self._poll_profile_runner():
            changed = True

        if changed:
            self._refresh_all()

    def _browse(self):
        p,_ = QFileDialog.getOpenFileName(self,"Select JAR","","JAR files (*.jar);;All files (*.*)")
        if p: self.jar.setText(p)

    def _load_settings(self):
        p = self.settings.value("jar","")
        p = os.path.expandvars(p) if p else ""
        # Validate saved path is actually a valid JAR
        valid = False
        if p and os.path.isfile(p):
            try:
                with open(p, "rb") as f:
                    header = f.read(4)
                if header[:2] == b"PK" and os.path.getsize(p) > 0:
                    valid = True
            except Exception:
                pass
        if not valid:
            p = DEFAULT_JAR if DEFAULT_JAR else ""
        self.jar.setText(p)
        # Load show-browser checkbox state
        show = self.settings.value("show_browser", False)
        self.cb_show_browser.setChecked(show in (True, "true", "True", "1", 1))
        # Load session-proxy settings
        use_proxies = self.settings.value("session_use_proxies", True)
        self.cb_session_proxies.setChecked(use_proxies in (True, "true", "True", "1", 1))
        # Load bank cache checkbox state
        use_bank_cache = self.settings.value("use_bank_cache", False)
        self.cb_bank_cache.setChecked(use_bank_cache in (True, "true", "True", "1", 1))
        max_conc = self.settings.value("session_max_concurrent", 4)
        try:
            self.spin_session_max.setValue(int(max_conc))
        except Exception:
            pass

    def _push_to_dreambot(self):
        if not self.selected_ids:
            QMessageBox.information(self, "None", "Select accounts to push to DreamBot.")
            return
        db_path = os.path.expandvars(r"%LOCALAPPDATA%\DreamBot\accounts.json")
        existing = []
        if os.path.isfile(db_path):
            try:
                import json
                with open(db_path, "r") as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
        by_email = {(e.get("username") or "").strip(): e for e in existing if isinstance(e, dict)}
        pushed = 0
        for aid in self.selected_ids:
            acc = self.db.get_account(aid)
            if not acc:
                continue
            email = acc["email"].strip()
            pwd = acc["password"].strip()
            if not email or not pwd:
                continue
            nick = (acc.get("username") or "").strip() or email.split("@")[0]
            pin = acc.get("pin", "").strip()
            totp = acc.get("totp", "").strip()
            entry = {
                "username": email,
                "password": pwd,
                "type": "JAGEX",
                "nickname": nick,
            }
            if pin:
                entry["pin"] = pin
            if totp:
                entry["totp"] = totp
                entry["token"] = totp
            by_email[email] = entry
            pushed += 1
        try:
            import json
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            with open(db_path, "w") as f:
                json.dump(list(by_email.values()), f, indent=2)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to write accounts.json:\n{str(e)}")
            return
        self._refresh_all()
        QMessageBox.information(self, "Pushed", f"Pushed {pushed} account(s) to DreamBot.\nLaunch DreamBot to load them into Account Manager.")

    def _extract_sessions(self):
        """Launch SessionExtractor script to pull session IDs from DreamBot AccountManager."""
        java = self._find_java()
        path = os.path.expandvars(self.jar.text())
        if not os.path.isfile(path):
            QMessageBox.critical(self, "Not Found", f"JAR not found:\n{path}")
            return
        log_dir = os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData\logs")
        os.makedirs(log_dir, exist_ok=True)
        sessions_file = os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData\sessions.json")
        # Remove old sessions.json to know when fresh one is written
        if os.path.isfile(sessions_file):
            os.remove(sessions_file)
        cmd = [java, "-Xmx512M", "-jar", os.path.abspath(path),
               "-script", "Session Extractor"]
        log_file = os.path.join(log_dir, f"extract_sessions_{datetime.now().strftime('%H%M%S')}.log")
        try:
            with open(log_file, "w") as out:
                out.write(f"CMD: {' '.join(cmd)}\n\n")
                out.flush()
                proc = subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(path)),
                                      stdout=out, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            # Wait up to 60s for the script to finish and write sessions.json
            proc.wait(timeout=60)
        except Exception as e:
            QMessageBox.critical(self, "Extract Error", f"Failed to run SessionExtractor:\n{str(e)}")
            return
        # Read the extracted sessions
        if not os.path.isfile(sessions_file):
            QMessageBox.information(self, "No Sessions", "SessionExtractor did not write sessions.json.\nMake sure accounts are saved in DreamBot Account Manager.")
            return
        try:
            import json
            with open(sessions_file, "r") as f:
                sessions = json.load(f)
            if not isinstance(sessions, list):
                QMessageBox.critical(self, "Bad Format", "sessions.json is not a list.")
                return
            updated = self.db.update_account_sessions(sessions)
            self._refresh_all()
            QMessageBox.information(self, "Sessions Extracted", f"Updated {updated} account(s) with session/character IDs.\nYou can now launch with session IDs.")
        except Exception as e:
            QMessageBox.critical(self, "Read Error", f"Failed to read sessions.json:\n{str(e)}")

    def _show_copyable_error(self, title, message):
        """Show an error dialog with selectable/copyable text."""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QPushButton
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(700, 400)
        layout = QVBoxLayout(dlg)
        text = QTextEdit(dlg)
        text.setPlainText(message)
        text.setReadOnly(True)
        text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(text)
        btn = QPushButton("OK", dlg)
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn)
        dlg.exec()

    def _launch_session_getter(self, aid, show_browser=False, proxy=None):
        """Launch get_jagex_session.py. Returns (job_dict, error_msg_or_None)."""
        import queue, threading, io, contextlib, traceback, subprocess, re

        # Check packages
        missing = []
        for pkg in [("requests","requests"),("pyotp","pyotp"),("pyautogui","pyautogui"),("undetected_chromedriver","undetected-chromedriver")]:
            mod, pip_name = pkg
            try:
                __import__(mod)
            except ImportError:
                missing.append(pip_name)
        if missing:
            msg = "Missing packages:\n" + "\n".join(f"  pip install {p}" for p in missing)
            print(f"[SESSION] {msg}")
            return None, msg

        # Check Chrome is installed
        chrome_found = False
        for cmd in [
            r'reg query "HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon" /v version',
            r'reg query "HKEY_LOCAL_MACHINE\SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome" /v version',
            r'reg query "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome" /v version',
        ]:
            try:
                out = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode()
                if re.search(r'version\s+REG_SZ\s+(\d+)', out):
                    chrome_found = True
                    break
            except Exception:
                pass
        for chrome_path in [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]:
            if os.path.isfile(chrome_path):
                chrome_found = True
                break
        if not chrome_found:
            return None, "Google Chrome is not installed.\n\nThe session grabber requires Chrome.\nDownload it from: https://www.google.com/chrome/"

        log_dir = os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData\logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"session_getter_{aid}_{datetime.now().strftime('%H%M%S')}.log")

        out_q = queue.Queue()
        err_q = queue.Queue()

        class _QueueIO:
            def __init__(self, q):
                self.q = q
                self._buf = ""
            def write(self, s):
                self._buf += s
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    if line:
                        self.q.put(line)
            def flush(self):
                if self._buf:
                    self.q.put(self._buf)
                    self._buf = ""

        # Frozen (PyInstaller) mode: run in-process via thread
        if getattr(sys, 'frozen', False):
            try:
                import get_jagex_session
            except Exception as e:
                return None, f"Failed to import session module: {e}"

            def _run_session():
                try:
                    with contextlib.redirect_stdout(_QueueIO(out_q)), contextlib.redirect_stderr(_QueueIO(err_q)):
                        get_jagex_session.main(aid, show_browser=show_browser, proxy=proxy)
                    job["retcode"] = 0
                except Exception as e:
                    err_q.put(f"EXCEPTION: {e}")
                    err_q.put(traceback.format_exc())
                    job["retcode"] = 1
                finally:
                    job["done"] = True

            job = {
                "aid": aid,
                "thread": threading.Thread(target=_run_session, daemon=True),
                "out_q": out_q,
                "err_q": err_q,
                "start_t": time.time(),
                "log_file": log_file,
                "out_lines": [],
                "err_lines": [],
                "status": "running",
                "retcode": None,
                "done": False,
            }
            job["thread"].start()
            return job, None

        # Non-frozen: use subprocess
        script_path = os.path.join(os.path.dirname(__file__), "get_jagex_session.py")
        if not os.path.isfile(script_path):
            return None, f"Script not found: {script_path}"

        cmd = [sys.executable, script_path, "--account-id", str(aid)]
        if show_browser:
            cmd.append("--show-browser")
        if proxy:
            import json
            cmd.extend(["--proxy", json.dumps(proxy)])
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )

        def _reader(pipe, q):
            try:
                for line in iter(pipe.readline, ''):
                    q.put(line.rstrip())
            finally:
                pipe.close()
        t_out = threading.Thread(target=_reader, args=(proc.stdout, out_q), daemon=True)
        t_err = threading.Thread(target=_reader, args=(proc.stderr, err_q), daemon=True)
        t_out.start()
        t_err.start()

        job = {
            "aid": aid,
            "proc": proc,
            "t_out": t_out,
            "t_err": t_err,
            "out_q": out_q,
            "err_q": err_q,
            "start_t": time.time(),
            "log_file": log_file,
            "out_lines": [],
            "err_lines": [],
            "status": "running",
        }
        return job, None

    def _poll_session_job(self, job, quiet=False):
        """Poll a running session getter to completion. Returns True on success."""
        import queue
        out_q = job["out_q"]
        err_q = job["err_q"]
        start_t = job["start_t"]
        log_file = job["log_file"]
        aid = job["aid"]
        out_lines = job["out_lines"]
        err_lines = job["err_lines"]

        def _is_running():
            if "thread" in job:
                return job["thread"].is_alive()
            return job["proc"].poll() is None

        def _drain():
            for q, lines in [(out_q, out_lines), (err_q, err_lines)]:
                while True:
                    try:
                        lines.append(q.get_nowait())
                    except queue.Empty:
                        break

        while _is_running():
            QApplication.processEvents()
            while True:
                try:
                    line = out_q.get_nowait()
                    out_lines.append(line)
                    print(f"[SESSION-{aid}] {line}")
                except queue.Empty:
                    break
            while True:
                try:
                    line = err_q.get_nowait()
                    err_lines.append(line)
                    print(f"[SESSION-{aid}-ERR] {line}")
                except queue.Empty:
                    break
            if time.time() - start_t > 120:
                if "proc" in job:
                    job["proc"].kill()
                    job["t_out"].join(timeout=2)
                    job["t_err"].join(timeout=2)
                _drain()
                stdout = "\n".join(out_lines)
                stderr = "\n".join(err_lines)
                with open(log_file, "w") as f:
                    f.write("TIMEOUT after 120s\n\nSTDOUT:\n" + stdout + "\n\nSTDERR:\n" + stderr)
                if not quiet:
                    self._show_copyable_error("Timeout", f"Session getter timed out after 120s.\nLog: {log_file}")
                job["status"] = "timeout"
                return False
            time.sleep(0.05)

        if "proc" in job:
            job["t_out"].join(timeout=2)
            job["t_err"].join(timeout=2)
        _drain()
        stdout = "\n".join(out_lines)
        stderr = "\n".join(err_lines)
        retcode = job["proc"].returncode if "proc" in job else job.get("retcode", 1)
        with open(log_file, "w") as f:
            f.write(f"EXIT CODE: {retcode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}")

        got_success = "=== SUCCESS ===" in stdout
        if retcode == 0 or got_success:
            self._refresh_all()
            if not quiet:
                acc = self.db.get_account(aid)
                output = stdout + "\n" + stderr
                self._show_auto_info("Session OK", f"Jagex session obtained for {acc['email']}.\nYou can now launch with session ID.\n\nLog:\n{output[-500:]}", 2000)
            job["status"] = "done_ok"
            return True
        else:
            if not quiet:
                output = stdout + "\n" + stderr
                self._show_copyable_error("Session Failed", f"Exit code: {retcode}\n\nLog: {log_file}\n\n{output[-1000:]}")
            job["status"] = "done_fail"
            return False

    def _ensure_session(self, aid, quiet=False, log_prefix="", force=False):
        """Get Jagex session for account. Returns True on success. Sequential-safe."""
        acc = self.db.get_account(aid)
        if not acc:
            return False
        if not force and acc.get("session_id") and acc.get("character_id"):
            print(f"[SESSION] Account {aid} already has session (use force=True to re-grab)")
            return True

        if not quiet:
            self.status.setText(f"Getting Jagex session for {acc.get('username','')}...")

        # Use account's assigned proxy if available
        proxy = None
        if acc.get("proxy_id"):
            for p in self.db.list_proxies(active_only=True):
                if p["id"] == acc["proxy_id"]:
                    proxy = p
                    break

        job, err = self._launch_session_getter(aid, show_browser=self.cb_show_browser.isChecked(), proxy=proxy)
        if job is None:
            if not quiet:
                self._show_copyable_error("Session Failed", err)
            return False

        try:
            return self._poll_session_job(job, quiet=quiet)
        except Exception as e:
            with open(job["log_file"], "w") as f:
                import traceback
                f.write(f"EXCEPTION: {e}\n\n{traceback.format_exc()}")
            if not quiet:
                self._show_copyable_error("Error", f"{e}\n\nLog: {job['log_file']}")
            return False

    def _show_auto_info(self, title, message, duration_ms=2000):
        """Show an info dialog that auto-dismisses after duration_ms."""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel
        from PyQt6.QtCore import QTimer
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(450, 150)
        layout = QVBoxLayout(dlg)
        lbl = QLabel(message)
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        timer = QTimer(dlg)
        timer.setSingleShot(True)
        timer.timeout.connect(dlg.accept)
        timer.start(duration_ms)
        dlg.exec()

    def _drain_job_queues(self, job):
        """Drain stdout/stderr queues for a running job."""
        import queue
        while True:
            try:
                line = job["out_q"].get_nowait()
                job["out_lines"].append(line)
            except queue.Empty:
                break
        while True:
            try:
                line = job["err_q"].get_nowait()
                job["err_lines"].append(line)
            except queue.Empty:
                break

    def _is_job_running(self, job):
        """Return True if the job (subprocess or thread) is still running."""
        if "thread" in job:
            return job["thread"].is_alive() and not job.get("done")
        return job["proc"].poll() is None

    def _kill_job(self, job):
        """Kill a subprocess job. Threads can't be force-killed."""
        if "proc" in job:
            try:
                job["proc"].kill()
            except Exception:
                pass
            try:
                job["t_out"].join(timeout=2)
                job["t_err"].join(timeout=2)
            except Exception:
                pass

    def _get_job_retcode(self, job):
        """Get return code: proc.returncode or thread retcode."""
        if "proc" in job:
            return job["proc"].returncode
        return job.get("retcode", 1)

    def _get_jagex_session(self):
        if not self.selected_ids:
            QMessageBox.information(self, "None", "Select account(s) to get Jagex session.")
            return

        use_proxies = getattr(self, "cb_session_proxies", None)
        use_proxies = use_proxies.isChecked() if use_proxies else True
        max_conc = getattr(self, "spin_session_max", None)
        max_conc = max_conc.value() if max_conc else 4

        proxies = self.db.list_proxies(active_only=True) if use_proxies else []
        concurrent = max(1, min(len(proxies), max_conc)) if use_proxies else 1
        print(f"[SESSION BATCH] Use proxies: {use_proxies} | Active proxies: {len(proxies)} | Max concurrent: {max_conc} -> concurrency = {concurrent}")

        # Build list of accounts that actually need sessions
        needs_session = []
        for aid in self.selected_ids:
            acc = self.db.get_account(aid)
            if not acc:
                continue
            if acc.get("session_id") and acc.get("character_id"):
                continue
            needs_session.append(aid)

        if not needs_session:
            QMessageBox.information(self, "Sessions", "All selected accounts already have sessions.")
            return

        total = len(needs_session)
        progress = QProgressDialog("Starting session grab...", "Cancel", 0, total, self)
        progress.setWindowTitle("Getting Jagex Sessions")
        progress.setMinimumWidth(450)
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()

        success = 0
        failed = 0
        completed = 0
        proxy_idx = 0

        while needs_session:
            if progress.wasCanceled():
                break

            # Launch up to `concurrent` jobs
            batch = []
            for _ in range(concurrent):
                if not needs_session:
                    break
                aid = needs_session.pop(0)
                acc = self.db.get_account(aid)
                nick = acc.get("username", "") or acc["email"].split("@")[0]

                # Assign proxy round-robin (each browser gets a different proxy)
                proxy = None
                if proxies:
                    proxy = proxies[proxy_idx % len(proxies)]
                    proxy_idx += 1

                label = (
                    f"Launching {nick}...\n"
                    f"Completed: {completed}/{total} | Success: {success} | Failed: {failed}"
                )
                progress.setLabelText(label)
                QApplication.processEvents()

                job, err = self._launch_session_getter(
                    aid, show_browser=self.cb_show_browser.isChecked(), proxy=proxy
                )
                if job is None:
                    failed += 1
                    completed += 1
                    progress.setValue(completed)
                    print(f"[SESSION BATCH] Failed to launch session getter for {nick}: {err}")
                    continue
                batch.append({"aid": aid, "job": job, "nick": nick})

            # Poll batch until all jobs finish
            while batch and any(b["job"]["status"] == "running" for b in batch):
                if progress.wasCanceled():
                    for b in batch:
                        if b["job"]["status"] == "running":
                            self._kill_job(b["job"])
                            b["job"]["status"] = "canceled"
                    break

                QApplication.processEvents()
                for b in batch:
                    if b["job"]["status"] != "running":
                        continue
                    self._drain_job_queues(b["job"])
                    if self._is_job_running(b["job"]):
                        # Still running — check timeout
                        if time.time() - b["job"]["start_t"] > 120:
                            self._kill_job(b["job"])
                            b["job"]["status"] = "timeout"
                            failed += 1
                            completed += 1
                            progress.setValue(completed)
                            print(f"[SESSION BATCH] Timeout for {b['nick']}")
                    else:
                        # Finished — collect remaining output
                        self._drain_job_queues(b["job"])
                        stdout = "\n".join(b["job"]["out_lines"])
                        stderr = "\n".join(b["job"]["err_lines"])
                        retcode = self._get_job_retcode(b["job"])
                        with open(b["job"]["log_file"], "w") as f:
                            f.write(f"EXIT CODE: {retcode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}")
                        got_success = "=== SUCCESS ===" in stdout
                        if retcode == 0 or got_success:
                            b["job"]["status"] = "done_ok"
                            success += 1
                            # Post-success hooks
                            try:
                                self._fetch_display_name(b["aid"])
                                self._check_membership(b["aid"])
                            except Exception:
                                pass
                            try:
                                acc = self.db.get_account(b["aid"])
                                if acc and acc.get("display_name"):
                                    cat = acc.get("category", "")
                                    if cat not in ("Banned", "Finished"):
                                        self.db.update_account(b["aid"], category="Ready To Farm")
                            except Exception:
                                pass
                        else:
                            b["job"]["status"] = "done_fail"
                            failed += 1
                            print(f"[SESSION BATCH] Failed for {b['nick']} (exit {retcode})")
                        completed += 1
                        progress.setValue(completed)
                time.sleep(0.05)

        progress.close()
        self._refresh_all()
        self.status.setText(f"Sessions: {success} OK, {failed} failed out of {total}")
        if failed > 0:
            QMessageBox.information(self, "Batch Done", f"Success: {success}\nFailed: {failed}")
        else:
            self._show_auto_info("Batch Done", f"All {success} session(s) grabbed successfully.", 2000)

    # ------------------------------------------------------------------
    # Account Creator
    # ------------------------------------------------------------------
    def _on_creator_provider_changed(self, text):
        self.creator_guerrilla_widget.setVisible(text == "Guerrilla Mail")
        self.creator_xitroo_widget.setVisible(text == "Xitroo")
        self.creator_imap_widget.setVisible(text == "IMAP")
        self.creator_gmail_widget.setVisible(text == "Gmail Web")

    def _creator_log_slot(self, message):
        self.creator_log.append(message)
        sb = self.creator_log.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def _creator_log_append(self, message):
        self.creator_log_signal.emit(message)

    def _start_account_creation(self):
        self.btn_creator_start.setEnabled(False)
        self.btn_creator_stop.setEnabled(True)
        self.creator_log.clear()
        self._creator_log_append("[ACCOUNT CREATOR] Starting...")
        self._creator_stop_event = threading.Event()

        # Capture all Qt widget values on the main thread (Qt is not thread-safe)
        provider_text = self.cb_creator_provider.currentText()
        count = self.spin_creator_count.value()
        use_proxies = self.cb_creator_proxies.isChecked()
        set_2fa = self.cb_creator_2fa.isChecked()
        headless = self.cb_creator_headless.isChecked()

        # Provider-specific settings
        imap_server = self.creator_imap_server.text()
        imap_port = self.creator_imap_port.text()
        imap_email = self.creator_imap_email.text()
        imap_password = self.creator_imap_password.text()
        imap_domain = self.creator_imap_domain.text()
        gmail_email = self.creator_gmail_email.text()
        gmail_domain = self.creator_gmail_domain.text()
        guerrilla_domain = self.creator_guerrilla_domain.text()
        xitroo_domain = self.creator_xitroo_domain.text()

        t = threading.Thread(
            target=self._creator_thread_worker,
            args=(
                provider_text, count, use_proxies, set_2fa, headless,
                imap_server, imap_port, imap_email, imap_password, imap_domain,
                gmail_email, gmail_domain,
                guerrilla_domain, xitroo_domain,
            ),
            daemon=True,
        )
        t.start()

    def _stop_account_creation(self):
        self._creator_log_append("[ACCOUNT CREATOR] Stop requested...")
        if hasattr(self, "_creator_stop_event"):
            self._creator_stop_event.set()

    def _creator_thread_worker(
        self,
        provider_text, count, use_proxies, set_2fa, headless,
        imap_server, imap_port, imap_email, imap_password, imap_domain,
        gmail_email, gmail_domain,
        guerrilla_domain, xitroo_domain,
    ):
        try:
            from jagex_account_creator import models, utils
            from jagex_account_creator.account_creator_selenium import AccountCreatorSelenium
        except Exception as e:
            self._creator_log_append(f"[ERROR] Failed to import account creator: {e}")
            self._creator_log_append("[ERROR] Make sure all dependencies are installed (selenium, undetected-chromedriver, pyotp, imap-tools, wreq, loguru, pydantic, platformdirs).")
            def _done():
                self.btn_creator_start.setEnabled(True)
                self.btn_creator_stop.setEnabled(False)
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, _done)
            return

        provider_map = {
            "Guerrilla Mail": models.MailProvider.GUERRILLA_MAIL,
            "Xitroo": models.MailProvider.XITROO,
            "IMAP": models.MailProvider.IMAP,
            "Gmail Web": models.MailProvider.GMAIL_WEB,
        }

        mail_provider = provider_map.get(provider_text, models.MailProvider.GUERRILLA_MAIL)

        proxies = []
        if use_proxies:
            db_proxies = self.db.list_proxies(active_only=True)
            for p in db_proxies:
                proxies.append(models.Proxy(
                    ip=p["host"],
                    port=p["port"],
                    username=p.get("username") or None,
                    password=p.get("password") or None,
                ))

        imap_details = None
        gmail_web_details = None
        domains = ["gmail.com"]

        if mail_provider == models.MailProvider.IMAP:
            imap_details = models.IMAPDetails(
                ip=imap_server,
                port=int(imap_port or 993),
                email=imap_email,
                password=imap_password,
            )
            domains = [imap_domain]
        elif mail_provider == models.MailProvider.GMAIL_WEB:
            gmail_web_details = models.GmailWebDetails(
                email=gmail_email,
            )
            domains = [gmail_domain]
        elif mail_provider == models.MailProvider.GUERRILLA_MAIL:
            domains = [guerrilla_domain]
        elif mail_provider == models.MailProvider.XITROO:
            domains = [xitroo_domain]

        created = 0
        failed = 0

        for i in range(count):
            if self._creator_stop_event.is_set():
                self._creator_log_append("[ACCOUNT CREATOR] Stopped by user.")
                break

            proxy = None
            if proxies:
                proxy = proxies[i % len(proxies)]

            self._creator_log_append(f"[{i+1}/{count}] Creating account...")
            try:
                ac = AccountCreatorSelenium(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                    element_wait_timeout=30,
                    cache_update_threshold=0.3,
                    enable_dev_tools=False,
                    account_email_domain=utils.get_account_domain(domains=domains),
                    account_password="",
                    mail_provider=mail_provider,
                    run_id=f"fm-{i+1}",
                    proxy=proxy,
                    set_2fa=set_2fa,
                    use_headless_browser=headless,
                    imap_details=imap_details,
                    gmail_web_details=gmail_web_details,
                    use_proxy_for_temp_mail=bool(proxy),
                )
                result = ac.register_account()
                account = result.jagex_account

                # Save to Farm Manager DB
                proxy_id = None
                if proxy:
                    db_proxies = self.db.list_proxies(active_only=True)
                    for p in db_proxies:
                        if p["host"] == proxy.ip and p["port"] == proxy.port:
                            proxy_id = p["id"]
                            break

                aid = self.db.add_account(
                    email=account.email.address,
                    password=account.password,
                    pin="",
                    totp=account.tfa.setup_key if account.tfa else "",
                    username=account.username,
                    display_name="",
                    jagex_account=1,
                    proxy_id=proxy_id,
                    category="Ready To Farm",
                    status="Offline",
                    notes=f"Created by Account Creator | Birthday: {account.birthday.day}/{account.birthday.month}/{account.birthday.year}",
                )
                if aid:
                    totp_part = account.tfa.setup_key if account.tfa else ""
                    line = f"{account.email.address}:{account.password}:{totp_part}"
                    self._creator_log_append(f"[{i+1}/{count}] Created: {line}")
                    # Append to accounts_created.txt
                    try:
                        import pathlib
                        accounts_file = os.path.join(os.path.expanduser("~"), "DreamBot", "BotData", "accounts_created.txt")
                        pathlib.Path(accounts_file).parent.mkdir(parents=True, exist_ok=True)
                        with open(accounts_file, "a", encoding="utf-8") as f:
                            f.write(line + "\n")
                    except Exception as file_err:
                        self._creator_log_append(f"[{i+1}/{count}] Warning: could not write to accounts file: {file_err}")
                    created += 1
                else:
                    self._creator_log_append(f"[{i+1}/{count}] DB duplicate? {account.email.address}")
                    failed += 1

            except Exception as e:
                self._creator_log_append(f"[{i+1}/{count}] Failed: {e}")
                failed += 1

        self._creator_log_append(f"[ACCOUNT CREATOR] Done. Created: {created}, Failed: {failed}")
        def _done():
            self._refresh_sidebar()
            self._refresh_overview()
            self._refresh_table()
            self.btn_creator_start.setEnabled(True)
            self.btn_creator_stop.setEnabled(False)
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, _done)

    def _create_heatmap_html(self):
        """Generate the heatmap HTML file with explv map iframe + bot sidebar."""
        html = '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
html,body{margin:0;padding:0;height:100%;width:100%;overflow:hidden;background:#1e1e1e;color:#e0e0e0;font-family:'Segoe UI',sans-serif;}
#container{display:flex;height:100vh;}
#sidebar{width:300px;background:#252526;border-right:1px solid #3e3e42;overflow-y:auto;padding:10px;box-sizing:border-box;}
#sidebar h3{margin:0 0 10px 0;font-size:14px;color:#aaa;border-bottom:1px solid #3e3e42;padding-bottom:6px;}
.bot-item{padding:8px;margin:6px 0;background:#2d2d30;border-radius:4px;cursor:pointer;transition:background 0.15s;}
.bot-item:hover{background:#3e3e42;}
.bot-name{font-weight:bold;color:#ff9800;font-size:13px;}
.bot-coords{color:#aaa;font-size:11px;margin-top:2px;}
.bot-activity{color:#4fc3f7;font-size:11px;margin-top:2px;}
.bot-time{color:#888;font-size:10px;margin-top:2px;}
#map-wrap{flex:1;position:relative;}
#map-frame{width:100%;height:100%;border:none;}
#status-bar{position:absolute;bottom:0;left:0;right:0;background:rgba(30,30,30,0.95);border-top:1px solid #3e3e42;padding:6px 12px;font-size:12px;color:#aaa;}
</style>
</head>
<body>
<div id="container">
<div id="sidebar">
<h3>Active Bots (<span id="bot-count">0</span>)</h3>
<div id="bot-list"></div>
</div>
<div id="map-wrap">
<iframe id="map-frame" src="https://explv.github.io/?centreX=3200&centreY=3200&centreZ=0&zoom=2"></iframe>
<div id="status-bar">Click a bot to center the map. Updates every 5s.</div>
</div>
</div>
<script>
var bots = {};
var botOrder = [];

function updateBot(account, x, y, z, activity) {
    var now = new Date().toLocaleTimeString();
    bots[account] = {x: x, y: y, z: z, activity: activity, time: now};
    if (botOrder.indexOf(account) === -1) botOrder.push(account);
    renderList();
}

function removeBot(account) {
    delete bots[account];
    var idx = botOrder.indexOf(account);
    if (idx > -1) botOrder.splice(idx, 1);
    renderList();
}

function clearBots() {
    bots = {};
    botOrder = [];
    renderList();
}

function centerMap(x, y) {
    document.getElementById('map-frame').src =
        'https://explv.github.io/?centreX=' + x + '&centreY=' + y + '&centreZ=0&zoom=4';
}

function renderList() {
    var list = document.getElementById('bot-list');
    list.innerHTML = '';
    document.getElementById('bot-count').textContent = botOrder.length;
    for (var i = 0; i < botOrder.length; i++) {
        var acc = botOrder[i];
        var b = bots[acc];
        if (!b) continue;
        var div = document.createElement('div');
        div.className = 'bot-item';
        div.innerHTML = '<div class="bot-name">' + acc + '</div>' +
                        '<div class="bot-coords">(' + b.x + ', ' + b.y + ', ' + b.z + ')</div>' +
                        '<div class="bot-activity">' + b.activity + '</div>' +
                        '<div class="bot-time">Updated: ' + b.time + '</div>';
        div.onclick = (function(x, y) {
            return function() { centerMap(x, y); };
        })(b.x, b.y);
        list.appendChild(div);
    }
}

// Expose to Python
window.pyUpdateBot = updateBot;
window.pyRemoveBot = removeBot;
window.pyClearBots = clearBots;
window.pyCenterMap = centerMap;
</script>
</body>
</html>'''
        try:
            with open(self.heatmap_html_path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            print(f"[HEATMAP] Failed to write HTML: {e}")

    def _heatmap_clear_selection(self):
        """Deselect all bots so the map stays still and shows the full view."""
        self._heatmap_selected_id = None
        self._heatmap_last_coords = None
        self.heatmap_list.clearSelection()

    def _refresh_heatmap(self):
        """Timer callback: update the heatmap sidebar list and sync all markers to the map.
        ONLY runs when the Heatmap tab is active to avoid lag on other tabs."""
        if not HAS_WEBENGINE or not hasattr(self, "heatmap_list"):
            return
        # Skip everything if we're not on the Heatmap tab
        if hasattr(self, "tabs"):
            try:
                if self.tabs.tabText(self.tabs.currentIndex()) != "Heatmap":
                    return
            except Exception:
                pass
        try:
            with self.heatmap_lock:
                data = dict(self.heatmap_data)

            # Update sidebar (skip unnamed bots like #Player2000)
            self.heatmap_list.clear()
            selected_item = None
            for bot_key, info in data.items():
                name = info.get("display_name") or str(bot_key)
                if name.startswith("#Player"):
                    continue
                x = int(info.get("x", 0))
                y = int(info.get("y", 0))
                z = int(info.get("z", 0))
                activity = info.get("activity") or "Unknown"
                item = QListWidgetItem(f"{name}\n({x}, {y}, {z}) — {activity}")
                item.setData(Qt.ItemDataRole.UserRole, {"account_id": bot_key, "x": x, "y": y, "activity": activity})
                self.heatmap_list.addItem(item)
                if getattr(self, "_heatmap_selected_id", None) == bot_key:
                    selected_item = item

            # Re-select previously selected bot in the list (but don't pan)
            if selected_item:
                self.heatmap_list.setCurrentItem(selected_item)
                curr = data.get(self._heatmap_selected_id)
                if curr:
                    self._heatmap_last_coords = {"x": int(curr.get("x", 0)), "y": int(curr.get("y", 0))}

            # Sync all markers to the map — moves pins, never pans camera
            if data and hasattr(self, "heatmap_view"):
                js_bots = []
                for bot_key, info in data.items():
                    name = info.get("display_name") or str(bot_key)
                    x = int(info.get("x", 0))
                    y = int(info.get("y", 0))
                    activity = info.get("activity") or "Unknown"
                    js_bots.append({"id": str(bot_key), "name": name, "x": x, "y": y, "activity": activity})
                self._sync_map_markers(js_bots)
        except Exception as e:
            print(f"[HEATMAP] Refresh error: {e}")

    def _heatmap_item_clicked(self, item):
        """Center the map on the clicked bot's coordinates and drop a marker."""
        if not HAS_WEBENGINE or not hasattr(self, "heatmap_view"):
            return
        coords = item.data(Qt.ItemDataRole.UserRole)
        if not coords:
            return
        account_id = coords.get("account_id")
        x, y, activity = coords["x"], coords["y"], coords.get("activity", "Unknown")
        name = item.text().split("\n")[0]
        # Track selected bot for auto-follow
        self._heatmap_selected_id = account_id
        self._heatmap_last_coords = {"x": x, "y": y}

        def _decide(ready):
            if ready == "ready":
                # Map already captured — just pan and mark without reload
                self._pan_and_mark(x, y, name, activity)
            else:
                # First load — load URL and capture map
                from PyQt6.QtCore import QUrl
                url = f"https://explv.github.io/?centreX={x}&centreY={y}&centreZ=0&zoom=4"
                self.heatmap_view.load(QUrl(url))
                self._poll_inject_marker(x, y, name, activity, attempts=10)

        self.heatmap_view.page().runJavaScript(
            "(window._captured_map && window._captured_map.setView) ? 'ready' : 'not_ready'",
            _decide
        )

    def _sync_map_markers(self, bots):
        """Inject JS to sync all bot markers onto the explv map."""
        if not bots:
            return
        import json
        bots_json = json.dumps(bots)
        js = """
        (function() {
            function findMap() {
                if (window._captured_map && window._captured_map.unproject) return window._captured_map;
                var candidates = ['map','osrsMap','leafletMap','myMap','lMap','gameMap','worldMap'];
                for (var i=0;i<candidates.length;i++) {
                    if (window[candidates[i]] && window[candidates[i]].unproject) return window[candidates[i]];
                }
                var keys = Object.keys(window);
                for (var k=0;k<keys.length;k++) {
                    try {
                        var obj = window[keys[k]];
                        if (obj && obj.unproject && typeof obj.unproject === 'function') return obj;
                    } catch(e) {}
                }
                var containers = document.querySelectorAll('.leaflet-container');
                for (var j=0;j<containers.length;j++) {
                    if (containers[j]._leaflet_map) return containers[j]._leaflet_map;
                    if (containers[j]._leaflet) return containers[j]._leaflet;
                }
                var all = document.querySelectorAll('*');
                for (var a=0;a<all.length;a++) {
                    if (all[a]._leaflet_map) return all[a]._leaflet_map;
                    if (all[a]._leaflet) return all[a]._leaflet;
                }
                if (window.L) {
                    if (L._instances) {
                        for (var id in L._instances) { if (L._instances[id].unproject) return L._instances[id]; }
                    }
                    if (L.map && L.map._instances) {
                        for (var id2 in L.map._instances) { if (L.map._instances[id2].unproject) return L.map._instances[id2]; }
                    }
                }
                var cont = document.querySelector('.leaflet-container');
                var cid = cont ? cont._leaflet_id : null;
                var allKeys = Object.keys(window);
                for (var z=0; z<allKeys.length; z++) {
                    try {
                        var wobj = window[allKeys[z]];
                        if (wobj && wobj._leaflet_id !== undefined && wobj.unproject && wobj.getCenter) {
                            if (cid === null || wobj._leaflet_id === cid) return wobj;
                        }
                    } catch(e) {}
                }
                return null;
            }
            var map = window._captured_map;
            if (!map || !map.unproject) {
                map = findMap();
                if (map) window._captured_map = map;
            }
            if (!map || !window.L) return 'WAIT: no map';
            window._bot_markers = window._bot_markers || {};
            var bots = """ + bots_json + """;
            var maxZoom = map.getMaxZoom ? map.getMaxZoom() : 11;
            var RS_OFFSET_X = 960, RS_OFFSET_Y = 6208;
            var RS_TILE_WIDTH_PX = 32, RS_TILE_HEIGHT_PX = 32;
            var MAP_HEIGHT_MAX_ZOOM_PX = 364544;
            var currentIds = [];
            for (var i = 0; i < bots.length; i++) {
                var b = bots[i];
                currentIds.push(b.id);
                var x_px = ((b.x - RS_OFFSET_X) * RS_TILE_WIDTH_PX) + (RS_TILE_WIDTH_PX / 4);
                var y_px = MAP_HEIGHT_MAX_ZOOM_PX - ((b.y - RS_OFFSET_Y) * RS_TILE_HEIGHT_PX);
                var target = map.unproject(L.point(x_px, y_px), maxZoom);
                if (window._bot_markers[b.id]) {
                    window._bot_markers[b.id].setLatLng(target);
                    window._bot_markers[b.id].setPopupContent('<b>' + b.name.replace(/'/g, '&#39;') + '</b><br>(' + b.x + ', ' + b.y + ', 0)<br><i>' + b.activity.replace(/'/g, '&#39;') + '</i>');
                } else {
                    var marker = L.marker(target).addTo(map);
                    marker.bindPopup('<b>' + b.name.replace(/'/g, '&#39;') + '</b><br>(' + b.x + ', ' + b.y + ', 0)<br><i>' + b.activity.replace(/'/g, '&#39;') + '</i>');
                    window._bot_markers[b.id] = marker;
                }
            }
            for (var id in window._bot_markers) {
                if (currentIds.indexOf(id) === -1) {
                    map.removeLayer(window._bot_markers[id]);
                    delete window._bot_markers[id];
                }
            }
            return 'OK: synced ' + bots.length + ' markers';
        })();
        """
        def _cb(result):
            msg = str(result) if result else ""
            if "WAIT:" in msg or "ERROR:" in msg:
                print(f"[HEATMAP JS] {msg}")
        self.heatmap_view.page().runJavaScript(js, _cb)

    def _pan_and_mark(self, x, y, name, activity):
        """Pan existing map to new coords and open popup (does NOT remove other markers)."""
        def _cb(result):
            msg = str(result) if result else ""
            if "ERROR:" in msg or "WAIT:" in msg:
                print(f"[HEATMAP JS] {msg}")
        js = """
        (function() {
            var map = window._captured_map;
            if (!map || !window.L) return 'ERROR: no map';
            try {
                var maxZoom = map.getMaxZoom ? map.getMaxZoom() : 11;
                var RS_OFFSET_X = 960, RS_OFFSET_Y = 6208;
                var RS_TILE_WIDTH_PX = 32, RS_TILE_HEIGHT_PX = 32;
                var MAP_HEIGHT_MAX_ZOOM_PX = 364544;
                var x_px = ((%(x)d - RS_OFFSET_X) * RS_TILE_WIDTH_PX) + (RS_TILE_WIDTH_PX / 4);
                var y_px = MAP_HEIGHT_MAX_ZOOM_PX - ((%(y)d - RS_OFFSET_Y) * RS_TILE_HEIGHT_PX);
                var target = map.unproject(L.point(x_px, y_px), maxZoom);
                map.setView(target, 4);
                return 'OK: panned';
            } catch(e) {
                return 'ERROR: ' + e.message;
            }
        })();
        """ % {"x": x, "y": y}
        self.heatmap_view.page().runJavaScript(js, _cb)

    def _poll_inject_marker(self, x, y, name, activity="Unknown", attempts=10):
        """Poll the page until the Leaflet map is ready, then pan to coords."""
        if not HAS_WEBENGINE or not hasattr(self, "heatmap_view"):
            return
        def _cb(result):
            msg = str(result) if result else ""
            print(f"[HEATMAP JS] {msg}")
            if "OK:" in msg:
                print("[HEATMAP] Map ready, panned to bot")
                return
            if attempts > 1:
                print(f"[HEATMAP] Retrying map ready check ({attempts-1} left)...")
                QTimer.singleShot(500, lambda: self._poll_inject_marker(x, y, name, activity, attempts-1))
            else:
                print("[HEATMAP] Map ready check failed after all retries")
        js = """
        (function() {
            function findMap() {
                if (window._captured_map && window._captured_map.unproject) return window._captured_map;
                var candidates = ['map','osrsMap','leafletMap','myMap','lMap','gameMap','worldMap'];
                for (var i=0;i<candidates.length;i++) {
                    if (window[candidates[i]] && window[candidates[i]].unproject) return window[candidates[i]];
                }
                var keys = Object.keys(window);
                for (var k=0;k<keys.length;k++) {
                    try {
                        var obj = window[keys[k]];
                        if (obj && obj.unproject && typeof obj.unproject === 'function') return obj;
                    } catch(e) {}
                }
                var containers = document.querySelectorAll('.leaflet-container');
                for (var j=0;j<containers.length;j++) {
                    if (containers[j]._leaflet_map) return containers[j]._leaflet_map;
                    if (containers[j]._leaflet) return containers[j]._leaflet;
                }
                var all = document.querySelectorAll('*');
                for (var a=0;a<all.length;a++) {
                    if (all[a]._leaflet_map) return all[a]._leaflet_map;
                    if (all[a]._leaflet) return all[a]._leaflet;
                }
                if (window.L) {
                    if (L._instances) {
                        for (var id in L._instances) { if (L._instances[id].unproject) return L._instances[id]; }
                    }
                    if (L.map && L.map._instances) {
                        for (var id2 in L.map._instances) { if (L.map._instances[id2].unproject) return L.map._instances[id2]; }
                    }
                }
                var cont = document.querySelector('.leaflet-container');
                var cid = cont ? cont._leaflet_id : null;
                var allKeys = Object.keys(window);
                for (var z=0; z<allKeys.length; z++) {
                    try {
                        var wobj = window[allKeys[z]];
                        if (wobj && wobj._leaflet_id !== undefined && wobj.unproject && wobj.getCenter) {
                            if (cid === null || wobj._leaflet_id === cid) return wobj;
                        }
                    } catch(e) {}
                }
                return null;
            }
            var map = findMap();
            if (!map || !window.L) {
                return 'WAIT: map=' + (map ? 'yes' : 'no') + ' L=' + (!!window.L) + ' containers=' + document.querySelectorAll('.leaflet-container').length;
            }
            window._captured_map = map;
            try {
                var maxZoom = map.getMaxZoom ? map.getMaxZoom() : 11;
                var RS_OFFSET_X = 960, RS_OFFSET_Y = 6208;
                var RS_TILE_WIDTH_PX = 32, RS_TILE_HEIGHT_PX = 32;
                var MAP_HEIGHT_MAX_ZOOM_PX = 364544;
                var x_px = ((%(x)d - RS_OFFSET_X) * RS_TILE_WIDTH_PX) + (RS_TILE_WIDTH_PX / 4);
                var y_px = MAP_HEIGHT_MAX_ZOOM_PX - ((%(y)d - RS_OFFSET_Y) * RS_TILE_HEIGHT_PX);
                var pt = map.unproject(L.point(x_px, y_px), maxZoom);
                map.setView(pt, 4);
                return 'OK: panned to ' + JSON.stringify(pt);
            } catch(e) {
                return 'ERROR: ' + e.message;
            }
        })();
        """ % {"x": x, "y": y}
        self.heatmap_view.page().runJavaScript(js, _cb)

    def _check_membership(self, aid):
        """Check membership status via OSRS hiscores — members-only skills with XP > 0 = P2P."""
        acc = self.db.get_account(aid)
        if not acc:
            return None
        username = (acc.get("display_name") or acc.get("username") or "").strip()
        if not username:
            return None
        # Fetch raw OSRS hiscores
        import urllib.request
        import urllib.error
        import urllib.parse
        import ssl
        url = f"https://services.runescape.com/m=hiscore_oldschool/index_lite.json?player={urllib.parse.quote(username)}"
        req = urllib.request.Request(url, headers={"User-Agent": "RuneLite/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Not on hiscores at all — likely Tutorial Island or very new F2P
                self.db.update_account(aid, membership=0)
                return False
            return None
        except Exception as e:
            logging.error("[MEMBERSHIP CHECK] Hiscores error for %s: %s", username, e)
            return None
        # Members-only skills: if any have XP > 0, account is (or was) P2P
        member_skill_names = {"Fletching", "Herblore", "Agility", "Thieving", "Slayer", "Farming", "Hunter", "Construction"}
        for sk in data.get("skills", []):
            if sk.get("name") in member_skill_names:
                xp = sk.get("xp", 0)
                if xp and xp > 0:
                    self.db.update_account(aid, membership=1)
                    print(f"[MEMBERSHIP] {username} is P2P ({sk['name']} XP={xp})")
                    return True
        # All member skills at 0 XP — account is F2P (or members but never trained a member skill)
        self.db.update_account(aid, membership=0)
        print(f"[MEMBERSHIP] {username} is F2P (no members-only skill XP)")
        return False

    def _fetch_display_name(self, aid):
        """Try lightweight fetch via existing session_id, fallback to full auth."""
        acc = self.db.get_account(aid)
        if not acc:
            return False, "Account not found"
        sid = (acc.get("session_id") or "").strip()
        if sid:
            try:
                import requests
                resp = requests.get(
                    "https://auth.jagex.com/game-session/v1/accounts",
                    headers={"Authorization": f"Bearer {sid}", "Accept": "application/json"},
                    timeout=15
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        data = data[0]
                    dn = (data.get("displayName") or "").strip()
                    updates = {}
                    if dn:
                        updates["display_name"] = dn
                        current_cat = acc.get("category") or ""
                        if current_cat not in ("Banned", "Finished"):
                            updates["category"] = "Ready To Farm"
                    is_member = None
                    for key in ["membership", "isMember", "membershipStatus", "activeMembership", "isMemberShip"]:
                        val = data.get(key)
                        if val is not None:
                            is_member = bool(val) if isinstance(val, (bool, int)) else str(val).lower() in ("true", "active", "1")
                            break
                    if is_member is not None:
                        updates["membership"] = 1 if is_member else 0
                    if updates:
                        self.db.update_account(aid, **updates)
                    if dn:
                        # Now that we have a display name, check membership via hiscores
                        self._check_membership(aid)
                        return True, dn
                    return False, "No displayName in response"
                elif resp.status_code in (401, 403):
                    logging.info("[FETCH DN] Session expired for account %d, falling back to full auth", aid)
                else:
                    return False, f"HTTP {resp.status_code}"
            except Exception as e:
                logging.error("[FETCH DN ERROR] %s", e)
        # Fallback: full auth flow
        ok = self._ensure_session(aid, quiet=True)
        if ok:
            self._check_membership(aid)
            acc = self.db.get_account(aid)
            dn = (acc.get("display_name") or "").strip()
            if dn:
                current_cat = acc.get("category") or ""
                if current_cat not in ("Banned", "Finished"):
                    self.db.update_account(aid, category="Ready To Farm")
            return bool(dn), dn or "Fetched but no display_name"
        return False, "Full auth failed"

    def _batch_fetch_display_names(self):
        if not self.selected_ids:
            QMessageBox.information(self, "None", "Select account(s) to fetch display names.")
            return
        success = 0
        failed = 0
        skipped = 0
        for aid in self.selected_ids:
            acc = self.db.get_account(aid)
            if not acc:
                continue
            nick = acc.get("username", "") or acc["email"].split("@")[0]
            self.status.setText(f"[{success+failed+skipped+1}/{len(self.selected_ids)}] Fetching display name for {nick}...")
            QApplication.processEvents()
            ok, msg = self._fetch_display_name(aid)
            if ok:
                success += 1
                logging.info("[FETCH DN] %s -> %s", nick, msg)
            else:
                failed += 1
                logging.warning("[FETCH DN FAILED] %s: %s", nick, msg)
                self.status.setText(f"Failed for {nick}: {msg} — continuing in 3s...")
                # Auto-continue after 3 seconds to keep batch moving
                for _ in range(30):
                    QApplication.processEvents()
                    time.sleep(0.1)
        self._refresh_all()
        self.status.setText(f"Display names: {success} OK, {failed} failed")
        QMessageBox.information(self, "Done", f"Fetched: {success}\nFailed: {failed}")

    def _import_dreambot(self):
        db_path = os.path.expandvars(r"%LOCALAPPDATA%\DreamBot\accounts.json")
        if not os.path.isfile(db_path):
            QMessageBox.information(self, "Not Found", f"DreamBot accounts.json not found at:\n{db_path}")
            return
        try:
            import json
            with open(db_path, "r") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read accounts.json:\n{str(e)}")
            return
        imported = updated = failed = 0
        for entry in data:
            if not isinstance(entry, dict):
                continue
            email = (entry.get("username") or "").strip()
            pwd = (entry.get("password") or "").strip()
            if not email or not pwd:
                continue
            totp = entry.get("totp", "").strip() or entry.get("token", "").strip()
            pin = entry.get("pin", "").strip()
            name = entry.get("nickname", "").strip()
            if not name:
                name = email.split("@")[0]
            existing = self.db.get_account_by_email(email)
            if existing:
                self.db.update_account(existing["id"], password=pwd, pin=pin, totp=totp, username=name,
                                       notes="Updated from DreamBot import")
                updated += 1
            else:
                a = {
                    "email": email, "password": pwd, "pin": pin, "totp": totp,
                    "username": name, "jagex_account": 1,
                    "category": "Uncategorized", "status": "Offline",
                    "notes": "Imported from DreamBot"
                }
                if self.db.add_account(**a):
                    imported += 1
                else:
                    failed += 1
        self._refresh_all()
        msg = f"New: {imported}\nUpdated: {updated}"
        if failed:
            msg += f"\nFailed: {failed}"
        QMessageBox.information(self, "Import Done", msg)

    # ------------------------------------------------------------------
    # Proxies Tab Methods
    # ------------------------------------------------------------------
    def _refresh_proxies_table(self):
        self.proxy_tbl.blockSignals(True)
        rows = self.db.list_proxies()
        self.proxy_tbl.setRowCount(len(rows))
        active = 0
        for r, p in enumerate(rows):
            self.proxy_tbl.setItem(r, 0, QTableWidgetItem(str(p["id"])))
            self.proxy_tbl.setItem(r, 1, QTableWidgetItem(p["host"]))
            self.proxy_tbl.setItem(r, 2, QTableWidgetItem(str(p["port"])))
            self.proxy_tbl.setItem(r, 3, QTableWidgetItem(p.get("protocol", "http")))
            self.proxy_tbl.setItem(r, 4, QTableWidgetItem(p.get("username", "")))
            act = "Yes" if p.get("active") else "No"
            ai = QTableWidgetItem(act)
            if p.get("active"):
                ai.setBackground(QColor("#1b5e20"))
                active += 1
            else:
                ai.setBackground(QColor("#555"))
            self.proxy_tbl.setItem(r, 5, ai)
        self.proxy_tbl.blockSignals(False)
        self.proxy_count_lbl.setText(f"Active: {active} | Total: {len(rows)}")

    def _proxy_add_dlg(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Add Proxy")
        dlg.setMinimumWidth(350)
        l = QVBoxLayout(dlg)
        host = QLineEdit(); host.setPlaceholderText("Host (e.g. 127.0.0.1)")
        l.addWidget(QLabel("Host:")); l.addWidget(host)
        port = QLineEdit(); port.setPlaceholderText("Port (e.g. 8080)")
        l.addWidget(QLabel("Port:")); l.addWidget(port)
        proto = QComboBox(); proto.addItems(["http", "socks5", "https"])
        l.addWidget(QLabel("Protocol:")); l.addWidget(proto)
        user = QLineEdit(); user.setPlaceholderText("Username (optional)")
        l.addWidget(QLabel("Username:")); l.addWidget(user)
        pwd = QLineEdit(); pwd.setPlaceholderText("Password (optional)")
        l.addWidget(QLabel("Password:")); l.addWidget(pwd)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            port_num = int(port.text().strip())
        except ValueError:
            QMessageBox.critical(self, "Invalid Port", "Port must be a number.")
            return
        self.db.add_proxy(
            host=host.text().strip(),
            port=port_num,
            username=user.text().strip(),
            password=pwd.text().strip(),
            protocol=proto.currentText()
        )
        self._refresh_proxies_table()
        self.status.setText("Proxy added")

    def _proxy_delete(self):
        rows = sorted({i.row() for i in self.proxy_tbl.selectedIndexes()}, reverse=True)
        if not rows:
            QMessageBox.information(self, "None", "Select proxy row(s) to delete.")
            return
        if QMessageBox.question(self, "Delete", f"Delete {len(rows)} proxy row(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        for r in rows:
            pid_item = self.proxy_tbl.item(r, 0)
            if pid_item:
                self.db.delete_proxy(int(pid_item.text()))
        self._refresh_proxies_table()
        self.status.setText(f"Deleted {len(rows)} proxy row(s)")

    def _parse_proxy_line(self, line):
        """Parse a single proxy line into a dict.

        Supported formats:
          host:port
          host:port:username:password
          protocol://host:port
          protocol://username:password@host:port
          ip:port:user:pass:socks5  (5th field = protocol override)
        """
        line = line.strip()
        if not line:
            return None

        # Comma-separated values within one line -> split and recurse
        if ',' in line and line.count(':') >= 2:
            parts = [p.strip() for p in line.split(',') if p.strip()]
            if len(parts) > 1:
                results = []
                for p in parts:
                    parsed = self._parse_proxy_line(p)
                    if parsed:
                        results.append(parsed)
                return results

        protocol = "http"
        username = ""
        password = ""

        # protocol://... format
        if '://' in line:
            proto_part, rest = line.split('://', 1)
            protocol = proto_part.lower()
            line = rest
            if protocol == "socks":
                protocol = "socks5"

        # user:pass@host:port format
        if '@' in line:
            auth, addr = line.rsplit('@', 1)
            if ':' in auth:
                username, password = auth.split(':', 1)
            line = addr

        # Split remaining by colon
        cols = line.split(':')
        if len(cols) < 2:
            return None

        host = cols[0]
        try:
            port = int(cols[1])
        except ValueError:
            return None

        # If 4 parts without @: host:port:user:pass
        if len(cols) == 4 and not username:
            username = cols[2]
            password = cols[3]

        # If 5 parts: host:port:user:pass:protocol
        if len(cols) == 5:
            username = cols[2]
            password = cols[3]
            protocol = cols[4].lower()

        if not host or not port:
            return None
        return {"host": host, "port": port, "protocol": protocol,
                "username": username, "password": password}

    def _proxy_bulk_import_dlg(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Bulk Import Proxies")
        dlg.setMinimumSize(500, 400)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel("Paste proxies (one per line):"))
        l.addWidget(QLabel("Formats: host:port | host:port:user:pass | protocol://host:port | protocol://user:pass@host:port"))
        te = QTextEdit()
        te.setPlaceholderText(
            "127.0.0.1:8080\n"
            "192.168.1.1:3128:user1:pass1\n"
            "socks5://10.0.0.1:1080\n"
            "http://user:pass@proxy.example.com:8080"
        )
        l.addWidget(te)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        text = te.toPlainText()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        added = 0
        failed = 0
        for ln in lines:
            result = self._parse_proxy_line(ln)
            if result is None:
                failed += 1
                continue
            # _parse_proxy_line can return a list if it detected comma-separated
            items = result if isinstance(result, list) else [result]
            for item in items:
                try:
                    self.db.add_proxy(
                        host=item["host"],
                        port=item["port"],
                        username=item.get("username", ""),
                        password=item.get("password", ""),
                        protocol=item.get("protocol", "http")
                    )
                    added += 1
                except Exception:
                    failed += 1
        self._refresh_proxies_table()
        msg = f"Added: {added}"
        if failed:
            msg += f"\nFailed / skipped: {failed}"
        QMessageBox.information(self, "Import Done", msg)
        self.status.setText(f"Bulk imported {added} proxy(s)")

    def _proxy_import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Proxies from File", "", "Text/CSV (*.txt *.csv);;All files (*.*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read file:\n{str(e)}")
            return

        added = 0
        failed = 0
        for ln in lines:
            result = self._parse_proxy_line(ln)
            if result is None:
                failed += 1
                continue
            items = result if isinstance(result, list) else [result]
            for item in items:
                try:
                    self.db.add_proxy(
                        host=item["host"],
                        port=item["port"],
                        username=item.get("username", ""),
                        password=item.get("password", ""),
                        protocol=item.get("protocol", "http")
                    )
                    added += 1
                except Exception:
                    failed += 1
        self._refresh_proxies_table()
        msg = f"Added: {added}"
        if failed:
            msg += f"\nFailed / skipped: {failed}"
        QMessageBox.information(self, "Import Done", msg)
        self.status.setText(f"Imported {added} proxy(s) from file")

    # ------------------------------------------------------------------
    # Account Settings Tab Methods
    # ------------------------------------------------------------------
    def _settings_refresh_table(self):
        self.set_tbl.blockSignals(True)
        search = self.set_search.text().strip()
        rows = self.db.list_accounts(search=search)
        self.set_tbl.setRowCount(len(rows))
        proxies = {p["id"]: p for p in self.db.list_proxies()}
        for r, acc in enumerate(rows):
            self.set_tbl.setItem(r,0,QTableWidgetItem(str(acc["id"])))
            self.set_tbl.setItem(r,1,QTableWidgetItem(acc["email"]))
            self.set_tbl.setItem(r,2,QTableWidgetItem(acc["password"]))
            self.set_tbl.setItem(r,3,QTableWidgetItem(acc.get("pin","")))
            self.set_tbl.setItem(r,4,QTableWidgetItem(acc.get("totp","")))
            self.set_tbl.setItem(r,5,QTableWidgetItem(acc.get("display_name","")))
            self.set_tbl.setItem(r,6,QTableWidgetItem(acc.get("category","Uncategorized")))
            st = acc.get("status","Offline")
            si = QTableWidgetItem(st)
            if st == "Running": si.setBackground(QColor("#1b5e20"))
            elif st == "Banned": si.setBackground(QColor("#b71c1c"))
            self.set_tbl.setItem(r,7,si)
            pid = acc.get("proxy_id")
            pt = ""
            if pid and pid in proxies:
                p = proxies[pid]
                pt = f"{p['host']}:{p['port']}"
            self.set_tbl.setItem(r,8,QTableWidgetItem(pt))
            self.set_tbl.setItem(r,9,QTableWidgetItem(acc.get("notes","")))
        self.set_tbl.blockSignals(False)
        self._settings_sel_change()

    def _settings_sel_change(self):
        ids = []
        for idx in self.set_tbl.selectionModel().selectedRows():
            it = self.set_tbl.item(idx.row(), 0)
            if it: ids.append(int(it.text()))
        self.settings_selected_ids = ids

    def _settings_action_go(self):
        action = self.action_combo.currentText()
        if action == "Select Action":
            return
        if not getattr(self, "settings_selected_ids", []):
            QMessageBox.information(self, "None", "Select account(s) in the Account Settings table.")
            return
        ids = self.settings_selected_ids
        handler = {
            "Export": self._act_export,
            "Start New Task": self._act_start_new_task,
            "Kill Instance": self._act_kill_instance,
            "Assign Proxy": self._act_assign_proxy,
            "Clear Proxy": self._act_clear_proxy,
            "Change Category": self._act_change_category,
            "Remove Notes": self._act_remove_notes,
            "Check Account Status": self._act_check_status,
            "Check Membership": self._act_check_membership,
            "Toggle Membership": self._act_toggle_membership,
            "Verify Email": self._act_verify_email,
            "Change Password": self._act_change_password,
            "Enable Authenticator": self._act_enable_auth,
            "Disable Authenticator": self._act_disable_auth,
            "Redeem Membership Code": self._act_redeem_membership,
            "Collect Membership Codes": self._act_collect_membership,
            "Appeal Ban": self._act_appeal_ban,
            "Sync Ban History": self._act_sync_ban,
        }.get(action)
        if handler:
            handler(ids)
        else:
            QMessageBox.information(self, "Not Implemented", f"'{action}' is not implemented yet.")

    def _act_export(self, ids):
        path,_ = QFileDialog.getSaveFileName(self, "Export Accounts", "accounts.csv", "CSV (*.csv)")
        if not path:
            return
        import csv
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ID","Email","Password","PIN","TOTP","Username","Category","Status","Proxy","Notes","Membership","Created"])
            for aid in ids:
                acc = self.db.get_account(aid)
                if not acc:
                    continue
                pid = acc.get("proxy_id")
                pt = ""
                if pid:
                    for p in self.db.list_proxies():
                        if p["id"] == pid:
                            pt = f"{p['host']}:{p['port']}"
                            break
                w.writerow([acc["id"], acc["email"], acc["password"], acc.get("pin",""),
                            acc.get("totp",""), acc.get("username",""), acc.get("category",""),
                            acc.get("status",""), pt, acc.get("notes",""),
                            "Yes" if acc.get("membership") else "No", acc.get("created_at","")])
        self.status.setText(f"Exported {len(ids)} account(s) to {path}")
        self._show_auto_info("Exported", f"{len(ids)} account(s) exported.", 2000)

    def _act_start_new_task(self, ids):
        QMessageBox.information(self, "Not Implemented", "Start New Task is not implemented yet.")

    def _act_kill_instance(self, ids):
        stopped = 0
        for r in self.db.get_running():
            if r["id"] in ids:
                pid = r.get("pid")
                if pid:
                    try: psutil.Process(pid).terminate(); stopped += 1
                    except psutil.NoSuchProcess: pass
                self.db.record_stop(r["id"])
        self._refresh_all()
        self.status.setText(f"Killed {stopped} instance(s)")

    def _act_assign_proxy(self, ids):
        proxies = self.db.list_proxies()
        if not proxies:
            QMessageBox.information(self, "No Proxies", "No proxies in database. Add proxies first.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Assign Proxy")
        dlg.setMinimumWidth(300)
        l = QVBoxLayout(dlg)
        combo = QComboBox()
        for p in proxies:
            combo.addItem(f"{p['host']}:{p['port']} ({p['protocol']})", p["id"])
        l.addWidget(QLabel("Select Proxy:"))
        l.addWidget(combo)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        proxy_id = combo.currentData()
        for aid in ids:
            self.db.update_account(aid, proxy_id=proxy_id)
        self._refresh_all()
        self.status.setText(f"Assigned proxy to {len(ids)} account(s)")

    def _act_clear_proxy(self, ids):
        for aid in ids:
            self.db.update_account(aid, proxy_id=None)
        self._refresh_all()
        self.status.setText(f"Cleared proxy from {len(ids)} account(s)")

    def _act_change_category(self, ids):
        dlg = QDialog(self)
        dlg.setWindowTitle("Change Category")
        dlg.setMinimumWidth(300)
        l = QVBoxLayout(dlg)
        combo = QComboBox()
        combo.addItems(CATEGORIES[1:])
        l.addWidget(QLabel("Select Category:"))
        l.addWidget(combo)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        cat = combo.currentText()
        for aid in ids:
            self.db.update_account(aid, category=cat)
        self._refresh_all()
        self.status.setText(f"Changed category to '{cat}' for {len(ids)} account(s)")

    def _act_remove_notes(self, ids):
        for aid in ids:
            self.db.update_account(aid, notes="")
        self._refresh_all()
        self.status.setText(f"Removed notes from {len(ids)} account(s)")

    def _act_check_status(self, ids):
        banned = 0
        active = 0
        tut = 0
        failed = 0
        for aid in ids:
            acc = self.db.get_account(aid)
            if not acc:
                continue
            dn = (acc.get("display_name") or "").strip()
            uname = dn or (acc.get("username") or "").strip()
            # No display name and username is a Jagex login -> Tutorial Island
            if not dn and ("+" in uname or not uname):
                if acc.get("category") != "Tutorial Island":
                    self.db.update_account(aid, category="Tutorial Island")
                tut += 1
                continue
            if not uname:
                failed += 1
                continue
            stats, err = _do_fetch_osrs_stats_raw(aid, uname)
            if stats:
                active += 1
                self.db.save_account_stats(aid, uname, stats)
                current_cat = acc.get("category") or ""
                # Only move to Ready To Farm if not manually set to Banned or Finished
                if current_cat not in ("Banned", "Finished"):
                    self.db.update_account(aid, status="Offline", category="Ready To Farm")
                else:
                    self.db.update_account(aid, status="Offline")
            elif err and "404" in err:
                # 404 = removed from hiscores = banned
                previous_category = acc.get("category", "Unknown")
                self.db.update_account(aid, category="Banned")
                # Log the ban with timestamp
                self.db.log_ban(aid, detected_by="manual_status_check", previous_category=previous_category, notes="404 error from OSRS hiscores")
                banned += 1
            else:
                failed += 1
        self._refresh_all()
        parts = [f"Checked {len(ids)} account(s)", f"Active: {active}", f"Tutorial Island: {tut}", f"Banned: {banned}", f"Failed: {failed}"]
        QMessageBox.information(self, "Account Status Check", "\n".join(parts))

    def _act_check_membership(self, ids):
        success = 0
        failed = 0
        for aid in ids:
            acc = self.db.get_account(aid)
            if not acc:
                continue
            nick = acc.get("display_name", "") or acc["email"].split("@")[0]
            self.status.setText(f"Checking membership for {nick}...")
            QApplication.processEvents()
            # If no display name, try fetching it first (needs session)
            if not acc.get("display_name") and not acc.get("username"):
                sid = (acc.get("session_id") or "").strip()
                if not sid:
                    ok = self._ensure_session(aid, quiet=True)
                    if not ok:
                        failed += 1
                        continue
                self._fetch_display_name(aid)
            result = self._check_membership(aid)
            if result is True:
                success += 1
            elif result is False:
                success += 1  # Successfully determined it's not a member
            else:
                failed += 1
        self._refresh_all()
        self.status.setText(f"Membership check: {success} OK, {failed} failed")
        QMessageBox.information(self, "Membership Check", f"Checked: {success}\nFailed: {failed}")

    def _act_toggle_membership(self, ids):
        toggled = 0
        for aid in ids:
            acc = self.db.get_account(aid)
            if not acc:
                continue
            new_val = 0 if acc.get("membership") else 1
            self.db.update_account(aid, membership=new_val)
            toggled += 1
        self._refresh_all()
        self.status.setText(f"Toggled membership for {toggled} account(s)")
        QMessageBox.information(self, "Done", f"Toggled membership for {toggled} account(s).")

    def _act_verify_email(self, ids):
        QMessageBox.information(self, "Not Implemented", "Verify Email is not implemented yet.")

    def _act_change_password(self, ids):
        if len(ids) > 1:
            QMessageBox.information(self, "Too Many", "Change Password only works on a single account.")
            return
        acc = self.db.get_account(ids[0])
        if not acc:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Change Password - {acc.get('username','') or acc['email']}")
        dlg.setMinimumWidth(350)
        l = QVBoxLayout(dlg)
        le = QLineEdit(acc["password"])
        l.addWidget(QLabel("New Password:"))
        l.addWidget(le)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.db.update_account(ids[0], password=le.text().strip())
        self._refresh_all()
        self.status.setText("Password updated")

    def _act_enable_auth(self, ids):
        if len(ids) > 1:
            QMessageBox.information(self, "Too Many", "Enable Authenticator only works on a single account.")
            return
        acc = self.db.get_account(ids[0])
        if not acc:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Enable Authenticator - {acc.get('username','') or acc['email']}")
        dlg.setMinimumWidth(350)
        l = QVBoxLayout(dlg)
        le = QLineEdit(acc.get("totp", ""))
        l.addWidget(QLabel("TOTP Secret:"))
        l.addWidget(le)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.db.update_account(ids[0], totp=le.text().strip())
        self._refresh_all()
        self.status.setText("Authenticator enabled")

    def _act_disable_auth(self, ids):
        for aid in ids:
            self.db.update_account(aid, totp="")
        self._refresh_all()
        self.status.setText(f"Disabled authenticator for {len(ids)} account(s)")

    def _act_redeem_membership(self, ids):
        QMessageBox.information(self, "Not Implemented", "Redeem Membership Code is not implemented yet.")

    def _act_collect_membership(self, ids):
        QMessageBox.information(self, "Not Implemented", "Collect Membership Codes is not implemented yet.")

    def _act_appeal_ban(self, ids):
        QMessageBox.information(self, "Not Implemented", "Appeal Ban is not implemented yet.")

    def _act_sync_ban(self, ids):
        QMessageBox.information(self, "Not Implemented", "Sync Ban History is not implemented yet.")

    # ------------------------------------------------------------------
    # Account Stats Tab Methods
    # ------------------------------------------------------------------
    def _stats_refresh_table(self):
        self.stats_tbl.blockSignals(True)
        search = self.stats_search.text().strip().lower()
        accounts = self.db.list_accounts()
        stats_rows = {s["account_id"]: s for s in self.db.get_all_account_stats()}
        rows = []
        missing = []
        for acc in accounts:
            uname = (acc.get("display_name") or acc.get("username") or acc["email"].split("@")[0]).lower()
            if search and search not in uname and search not in acc["email"].lower():
                continue
            s = stats_rows.get(acc["id"])
            rows.append((acc, s))
            if not s:
                missing.append(acc)
        self.stats_tbl.setRowCount(len(rows))
        for r, (acc, s) in enumerate(rows):
            # Eye button
            btn = QPushButton("\U0001F441")  # eye emoji
            btn.setStyleSheet("background:#333;color:#fff;padding:2px 6px;font-size:14px")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip("View stats")
            btn.clicked.connect(lambda _, a=acc, st=s: self._show_skill_popup(a, st))
            self.stats_tbl.setCellWidget(r, 0, btn)
            nick = acc.get("display_name") or acc.get("username", "") or acc["email"].split("@")[0]
            self.stats_tbl.setItem(r, 1, QTableWidgetItem(nick))
            if s:
                self.stats_tbl.setItem(r, 2, QTableWidgetItem(str(s.get("overall_lvl", 0))))
                self.stats_tbl.setItem(r, 3, QTableWidgetItem(str(s.get("quest_points", 0))))
                self.stats_tbl.setItem(r, 4, QTableWidgetItem(str(round(s.get("combat_lvl", 3.0), 1))))
                self.stats_tbl.setItem(r, 5, QTableWidgetItem("0"))  # GP placeholder
                self.stats_tbl.setItem(r, 6, QTableWidgetItem("0"))  # Plat placeholder
                self.stats_tbl.setItem(r, 7, QTableWidgetItem(str(s.get("wealth", "0"))))
            else:
                for col in range(2, 8):
                    self.stats_tbl.setItem(r, col, QTableWidgetItem("-"))
            self.stats_tbl.setItem(r, 8, QTableWidgetItem(acc.get("category", "Uncategorized")))
            self.stats_tbl.setItem(r, 9, QTableWidgetItem(acc.get("notes", "")))
            self.stats_tbl.setItem(r, 10, QTableWidgetItem("0"))  # Bans placeholder
            st = acc.get("status", "Offline")
            si = QTableWidgetItem(st)
            if st == "Running": si.setBackground(QColor("#1b5e20"))
            elif st == "Banned": si.setBackground(QColor("#b71c1c"))
            self.stats_tbl.setItem(r, 11, si)
            p2p_val = "Yes" if acc.get("membership") else "No"
            self.stats_tbl.setItem(r, 12, QTableWidgetItem(p2p_val))
            # Instance running check
            running = False
            for rh in self.db.get_running():
                if rh["id"] == acc["id"]:
                    running = True
                    break
            inst = QTableWidgetItem("IN GAME" if running else "OFFLINE")
            if running:
                inst.setBackground(QColor("#1b5e20"))
                inst.setForeground(QColor("#fff"))
            self.stats_tbl.setItem(r, 13, inst)
        self.stats_tbl.blockSignals(False)
        # Stats are fetched manually or every 30 min, not on every table refresh

    def _show_skill_popup(self, acc, stats):
        dlg = QDialog(self)
        nick = acc.get("username", "") or acc["email"].split("@")[0]
        dlg.setWindowTitle(f"{nick} - OSRS Stats")
        dlg.setMinimumSize(420, 520)
        l = QVBoxLayout(dlg)
        if not stats:
            l.addWidget(QLabel("No stats cached yet. Stats are fetched automatically from OSRS Hiscores."))
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
            bb.accepted.connect(dlg.accept)
            l.addWidget(bb)
            dlg.exec()
            return

        # Skill grid
        grid = QWidget()
        gl = QHBoxLayout(grid)
        gl.setSpacing(12)
        left = QVBoxLayout()
        right = QVBoxLayout()
        skills = [
            ("Attack", stats.get("attack", 1)),
            ("Defence", stats.get("defence", 1)),
            ("Strength", stats.get("strength", 1)),
            ("Hitpoints", stats.get("hitpoints", 10)),
            ("Ranged", stats.get("ranged", 1)),
            ("Prayer", stats.get("prayer", 1)),
            ("Magic", stats.get("magic", 1)),
            ("Cooking", stats.get("cooking", 1)),
            ("Woodcutting", stats.get("woodcutting", 1)),
            ("Fletching", stats.get("fletching", 1)),
            ("Fishing", stats.get("fishing", 1)),
            ("Firemaking", stats.get("firemaking", 1)),
            ("Crafting", stats.get("crafting", 1)),
            ("Smithing", stats.get("smithing", 1)),
            ("Mining", stats.get("mining", 1)),
            ("Herblore", stats.get("herblore", 1)),
            ("Agility", stats.get("agility", 1)),
            ("Thieving", stats.get("thieving", 1)),
            ("Slayer", stats.get("slayer", 1)),
            ("Farming", stats.get("farming", 1)),
            ("Runecrafting", stats.get("runecrafting", 1)),
            ("Hunter", stats.get("hunter", 1)),
            ("Construction", stats.get("construction", 1)),
        ]
        mid = len(skills) // 2
        for i, (name, val) in enumerate(skills):
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{name}:"))
            row.addStretch()
            row.addWidget(QLabel(str(val)))
            (left if i < mid else right).addLayout(row)
        left.addStretch()
        right.addStretch()
        gl.addLayout(left)
        gl.addLayout(right)
        l.addWidget(grid)

        # Summary
        sum_l = QHBoxLayout()
        sum_l.addWidget(QLabel(f"<b>Total Level:</b> {stats.get('overall_lvl', 0)}"))
        sum_l.addWidget(QLabel(f"<b>Combat:</b> {round(stats.get('combat_lvl', 3.0), 1)}"))
        sum_l.addWidget(QLabel(f"<b>Quest Points:</b> {stats.get('quest_points', 0)}"))
        sum_l.addStretch()
        l.addLayout(sum_l)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(dlg.accept)
        l.addWidget(bb)
        dlg.exec()

    def _stats_auto_fetch(self):
        """Called by 30-minute timer and startup. Full status check for all accounts."""
        if self._stats_fetching:
            return
        accounts = self.db.list_accounts()
        stats_rows = {s["account_id"]: s for s in self.db.get_all_account_stats()}
        to_fetch = []
        tut_count = 0
        for a in accounts:
            dn = (a.get("display_name") or "").strip()
            un = (a.get("username") or "").strip()
            if not dn and ("+" in un or not un):
                # No display name -> Tutorial Island
                if a.get("category") != "Tutorial Island":
                    self.db.update_account(a["id"], category="Tutorial Island")
                tut_count += 1
            elif dn or (un and "+" not in un):
                to_fetch.append(a)
        if to_fetch:
            logging.info("[AUTO CHECK] %d Tutorial Island, fetching %d accounts", tut_count, len(to_fetch))
            self._stats_fetch_missing(to_fetch)
        else:
            logging.info("[AUTO CHECK] %d Tutorial Island, nothing to fetch", tut_count)
        self._refresh_all()

    def _stats_refresh_btn(self):
        """Manual refresh button: full status check same as auto-fetch."""
        if self._stats_fetching:
            self.status.setText("Stats fetch already in progress...")
            return
        self._stats_auto_fetch()

    def _stats_fetch_missing(self, missing_accounts):
        """Launch background thread to fetch missing stats without blocking GUI."""
        self._stats_fetching = True
        to_fetch = []
        for a in missing_accounts:
            dn = (a.get("display_name") or "").strip()
            un = (a.get("username") or "").strip()
            if dn or (un and "+" not in un):
                to_fetch.append(a)
        self.status.setText(f"Fetching stats for {len(to_fetch)} accounts...")
        self._stats_worker = StatsFetchWorker(to_fetch)
        self._stats_worker.progress.connect(self._stats_on_progress)
        self._stats_worker.account_done.connect(self._stats_on_account_done)
        self._stats_worker.not_found.connect(self._stats_on_not_found)
        self._stats_worker.finished.connect(self._stats_on_finished)
        self._stats_worker.error.connect(self._stats_on_error)
        self._stats_worker.start()

    def _stats_on_progress(self, msg):
        self.stats_lbl.setText(msg)

    def _stats_on_account_done(self, account_id, username, stats):
        """Save stats to DB in the main thread. Active account = Ready To Farm."""
        try:
            self.db.save_account_stats(account_id, username, stats)
            self.db.update_account(account_id, category="Ready To Farm")
        except Exception as e:
            logging.error("[STATS SAVE ERROR] %s (id=%d): %s", username, account_id, e)
            self.status.setText(f"Stats save failed for {username}: {e}")

    def _stats_on_not_found(self, account_id, username):
        """HTTP 404 from hiscores — account removed from hiscores = banned."""
        existing = self.db.get_account_stats(account_id)
        if existing:
            logging.warning("[BAN DETECTED] Account %s (id=%d) — 404 from hiscores, previously had stats", username, account_id)
        else:
            logging.warning("[BAN DETECTED] Account %s (id=%d) — 404 from hiscores, no previous stats", username, account_id)
        ok = self.db.update_account(account_id, status="Banned", category="Banned")
        if ok:
            logging.info("[BAN UPDATED] Account %s (id=%d) marked as Banned", username, account_id)
        else:
            logging.error("[BAN FAILED] Account %s (id=%d) — DB update returned 0 rows", username, account_id)
        self.status.setText(f"Account {username} appears banned (404 from hiscores)")

    def _stats_on_finished(self, fetched, failed, skipped):
        self._stats_fetching = False
        self._stats_last_fetch = time.time()
        msg = f"Stats fetched: {fetched} OK, {failed} failed"
        if skipped:
            msg += f", {skipped} skipped (no display name)"
        self.stats_lbl.setText("Stats auto-fetched every 30 min")
        self._stats_refresh_table()
        self.status.setText(msg)

    def _stats_on_error(self, trace):
        self._stats_fetching = False
        self.status.setText("Stats fetch failed — see error.log")
        logging.error("[STATS WORKER ERROR]\n%s", trace)

    # ------------------------------------------------------------------
    # Profile Management Methods
    # ------------------------------------------------------------------
    def _refresh_profiles_table(self):
        profiles = self.db.list_profiles()
        self.profile_tbl.blockSignals(True)
        self.profile_tbl.setRowCount(len(profiles))
        for r, p in enumerate(profiles):
            self.profile_tbl.setItem(r, 0, QTableWidgetItem(str(p["id"])))
            self.profile_tbl.setItem(r, 1, QTableWidgetItem(p["name"]))
            self.profile_tbl.setItem(r, 2, QTableWidgetItem(p.get("description", "")))

            # Mode and batch
            batch_size = p.get('batch_size', 0) or 0
            if batch_size > 0:
                mode_text = "batch"
                batch_text = f"{batch_size}"
            else:
                mode_text = p.get('queue_mode', 'parallel')
                batch_text = "-"
            self.profile_tbl.setItem(r, 3, QTableWidgetItem(mode_text))
            self.profile_tbl.setItem(r, 4, QTableWidgetItem(batch_text))

            # Count accounts and scripts
            accounts = self.db.get_profile_accounts(p["id"])
            scripts = self.db.get_profile_scripts(p["id"])

            self.profile_tbl.setItem(r, 5, QTableWidgetItem(str(len(accounts))))
            self.profile_tbl.setItem(r, 6, QTableWidgetItem(str(len(scripts))))
        self.profile_tbl.blockSignals(False)
        self._profile_sel_change()

    def _profile_sel_change(self):
        rows = self.profile_tbl.selectionModel().selectedRows()
        has_selection = len(rows) > 0
        
        # Enable/disable buttons based on selection
        if hasattr(self, 'btn_edit_profile'):
            self.btn_edit_profile.setEnabled(has_selection)
        if hasattr(self, 'btn_delete_profile'):
            self.btn_delete_profile.setEnabled(has_selection)
        if hasattr(self, 'btn_start_profile_queue'):
            self.btn_start_profile_queue.setEnabled(has_selection)
        
        if not rows:
            return
        
        row = rows[0].row()
        profile_id = int(self.profile_tbl.item(row, 0).text())
        profile = self.db.get_profile(profile_id)
        
        if profile:
            accounts = self.db.get_profile_accounts(profile_id)
            scripts = self.db.get_profile_scripts(profile_id)
            
            # Update status with profile info
            details = f"Profile '{profile['name']}: {len(accounts)} accounts, {len(scripts)} scripts"
            if hasattr(self, 'status'):
                self.status.setText(details)

    def _new_profile_dlg(self):
        try:
            dialog = ProfileDialog(self)
        except Exception as e:
            import traceback
            QMessageBox.critical(self, "Profile Dialog Error", f"Failed to open New Profile dialog:\n\n{str(e)}\n\n{traceback.format_exc()}")
            return
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                data = dialog.get_data()
                # Check for duplicate profile name
                existing = [p for p in self.db.list_profiles() if p["name"].strip().lower() == data["name"].strip().lower()]
                if existing:
                    QMessageBox.warning(self, "Duplicate Name", f"A profile named '{data['name']}' already exists.\nPlease choose a different name.")
                    return
                profile_id = self.db.create_profile(
                    name=data["name"],
                    description=data["description"],
                    queue_mode=data.get("queue_mode", "parallel"),
                    batch_size=data.get("batch_size", 0),
                    stagger_delay=data.get("stagger_delay", 5),
                    category_filter=data["category_filter"],
                    status_filter=data["status_filter"],
                    search_filter=data["search_filter"]
                )

                # Add selected accounts if any
                if data["account_ids"]:
                    for aid in data["account_ids"]:
                        self.db.add_account_to_profile(profile_id, aid)

                # Add scripts if any
                for i, script in enumerate(data["scripts"]):
                    self.db.add_profile_script(
                        profile_id,
                        script["name"],
                        script.get("world", 420),
                        script.get("stop_condition", "Process Exit"),
                        script.get("stop_value", ""),
                        i,
                        script.get("repeat", 1)
                    )

                self._refresh_profiles_table()
                self.status.setText(f"Created profile: {data['name']}")
            except Exception as e:
                import traceback
                QMessageBox.critical(self, "Profile Save Error", f"Failed to save profile:\n\n{str(e)}\n\n{traceback.format_exc()}")

    def _edit_profile_dlg(self):
        rows = self.profile_tbl.selectionModel().selectedRows()
        if not rows:
            QMessageBox.warning(self, "No Selection", "Please select a profile to edit.")
            return

        row = rows[0].row()
        profile_id = int(self.profile_tbl.item(row, 0).text())
        profile = self.db.get_profile(profile_id)

        if profile:
            dialog = ProfileDialog(self, profile)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                data = dialog.get_data()

                # Check for duplicate profile name (excluding current profile)
                existing = [p for p in self.db.list_profiles() if p["id"] != profile_id and p["name"].strip().lower() == data["name"].strip().lower()]
                if existing:
                    QMessageBox.warning(self, "Duplicate Name", f"A profile named '{data['name']}' already exists.\nPlease choose a different name.")
                    return

                # Update profile
                self.db.update_profile(profile_id,
                    name=data["name"],
                    description=data["description"],
                    queue_mode=data.get("queue_mode", "parallel"),
                    batch_size=data.get("batch_size", 0),
                    stagger_delay=data.get("stagger_delay", 5),
                    category_filter=data["category_filter"],
                    status_filter=data["status_filter"],
                    search_filter=data["search_filter"]
                )

                # Update accounts (clear and re-add)
                current_accounts = {acc["id"] for acc in self.db.get_profile_accounts(profile_id)}
                new_accounts = set(data["account_ids"])

                # Remove accounts not in new list
                for aid in current_accounts - new_accounts:
                    self.db.remove_account_from_profile(profile_id, aid)

                # Add new accounts
                for aid in new_accounts - current_accounts:
                    self.db.add_account_to_profile(profile_id, aid)

                # Update scripts (clear and re-add)
                current_scripts = self.db.get_profile_scripts(profile_id)
                for script in current_scripts:
                    self.db.remove_profile_script(script["id"])

                for i, script in enumerate(data["scripts"]):
                    self.db.add_profile_script(
                        profile_id,
                        script["name"],
                        script.get("world", 420),
                        script.get("stop_condition", "Process Exit"),
                        script.get("stop_value", ""),
                        i,
                        script.get("repeat", 1)
                    )

                self._refresh_profiles_table()
                self.status.setText(f"Updated profile: {data['name']}")

    def _delete_profile(self):
        rows = self.profile_tbl.selectionModel().selectedRows()
        if not rows:
            QMessageBox.warning(self, "No Selection", "Please select a profile to delete.")
            return
        
        row = rows[0].row()
        profile_id = int(self.profile_tbl.item(row, 0).text())
        profile_name = self.profile_tbl.item(row, 1).text()
        
        reply = QMessageBox.question(self, "Confirm Delete", 
            f"Are you sure you want to delete profile '{profile_name}'?\n\nThis will remove all associated accounts and scripts.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_profile(profile_id)
            self._refresh_profiles_table()
            self.status.setText(f"Deleted profile: {profile_name}")

    def _queue_from_profile(self):
        rows = self.profile_tbl.selectionModel().selectedRows()
        if not rows:
            QMessageBox.warning(self, "No Selection", "Please select a profile to create queue from.")
            return

        row = rows[0].row()
        profile_id = int(self.profile_tbl.item(row, 0).text())
        profile_name = self.profile_tbl.item(row, 1).text()
        profile = self.db.get_profile(profile_id)

        # Ask for queue mode
        mode_dialog = QueueModeDialog(self)
        if mode_dialog.exec() == QDialog.DialogCode.Accepted:
            mode = mode_dialog.get_mode()

            # Set stagger delay from profile if available
            if profile:
                self._batch_stagger_delay = profile.get('stagger_delay', 5) or 5

            success = self.db.create_queue_from_profile(profile_id, mode)
            if success:
                self._refresh_all()
                self.status.setText(f"Created queue from profile: {profile_name} ({mode})")
                QMessageBox.information(self, "Success", f"Queue created from profile '{profile_name}'")
            else:
                QMessageBox.warning(self, "Failed", "Failed to create queue. Profile may not have accounts or scripts.")

    def _start_profile_queue_direct(self):
        """Start queue directly from selected profile using its saved queue mode"""
        rows = self.profile_tbl.selectionModel().selectedRows()
        if not rows:
            print("[PROFILE QUEUE] No profile selected")
            return

        row = rows[0].row()
        profile_id = int(self.profile_tbl.item(row, 0).text())
        profile = self.db.get_profile(profile_id)
        print(f"[PROFILE QUEUE] profile_id={profile_id} profile={profile}")

        if not profile:
            print("[PROFILE QUEUE] Profile not found")
            return

        profile_name = profile['name']
        mode = profile.get('queue_mode', 'parallel')
        stagger = profile.get('stagger_delay', 5) or 5

        # Check if profile has accounts and scripts
        accounts = self.db.get_profile_accounts(profile_id)
        scripts = self.db.get_profile_scripts(profile_id)
        print(f"[PROFILE QUEUE] accounts={len(accounts)} scripts={len(scripts)} mode={mode} batch_size={profile.get('batch_size',0)}")

        if not accounts:
            QMessageBox.warning(self, "No Accounts", f"Profile '{profile_name}' has no accounts")
            return

        if not scripts:
            QMessageBox.warning(self, "No Scripts", f"Profile '{profile_name}' has no scripts configured")
            return

        # Set batch stagger delay from profile
        self._batch_stagger_delay = stagger

        # Clear any stale queue entries so the old queue system doesn't interfere
        self.db.clear_queue()
        print(f"[PROFILE QUEUE] Cleared stale queue entries")

        # Setup direct profile runner (no queue entries created)
        self._profile_runner_active = True
        self._profile_runner_accounts = accounts
        self._profile_runner_scripts = scripts
        self._profile_runner_batch_size = profile.get('batch_size', 0) or 0
        # If batch_size is set, always treat as batch mode regardless of stored queue_mode
        if self._profile_runner_batch_size > 0:
            mode = "batch"
        self._profile_runner_mode = mode
        self._profile_runner_stagger = stagger
        self._profile_runner_current_idx = 0
        self._profile_runner_batch_start_idx = 0
        self._profile_runner_running = {}
        self._profile_runner_in_batch = False
        self._profile_runner_wait_pid_aid = None
        self._profile_runner_wait_pid_start = 0
        self._profile_runner_stepping = False
        self._profile_runner_name = profile_name

        print(f"[PROFILE RUNNER] Starting profile '{profile_name}' with {len(accounts)} accounts, mode={mode}, batch_size={self._profile_runner_batch_size}, stagger={stagger}s")
        self.status.setText(f"Running profile: {profile_name} ({mode}, {len(accounts)} accounts)")
        QMessageBox.information(self, "Success", f"Profile '{profile_name}' started with {len(accounts)} accounts\nMode: {mode}, Batch size: {self._profile_runner_batch_size}")

        # Begin launching
        self._run_profile_step()

    def _run_profile_step(self):
        """Launch next account(s) for the active profile runner based on mode."""
        if not self._profile_runner_active:
            return
        # Re-entry guard: prevent multiple simultaneous calls (e.g. from poll timer + stagger timer)
        if self._profile_runner_stepping:
            print("[PROFILE RUNNER] Step already in progress, skipping re-entrant call")
            return
        self._profile_runner_stepping = True

        try:
            accounts = self._profile_runner_accounts
            mode = self._profile_runner_mode
            batch_size = self._profile_runner_batch_size
            idx = self._profile_runner_current_idx
            if idx >= len(accounts):
                print(f"[PROFILE RUNNER] All {len(accounts)} accounts processed. Profile complete.")
                self._profile_runner_active = False
                self.status.setText(f"Profile '{self._profile_runner_name}' finished")
                return

            if mode == "batch" and batch_size > 0:
                # Launch up to batch_size accounts, staggered
                batch_start = (idx // batch_size) * batch_size
                self._profile_runner_batch_start_idx = batch_start
                print(f"[PROFILE RUNNER] Batch starting accounts {batch_start+1}-{min(batch_start+batch_size, len(accounts))} of {len(accounts)}")
                self._profile_runner_in_batch = True
                launched = self._launch_next_profile_account()
                # If launch failed, batch launch phase is done
                if not launched:
                    self._profile_runner_in_batch = False
            elif mode == "sequential":
                # Launch one account
                print(f"[PROFILE RUNNER] Sequential launch account {idx+1}/{len(accounts)}")
                self._launch_next_profile_account()
            else:
                # Parallel: launch all remaining at once
                print(f"[PROFILE RUNNER] Parallel launch remaining {len(accounts)-idx} accounts")
                while self._profile_runner_current_idx < len(accounts):
                    if not self._launch_next_profile_account():
                        break
        finally:
            self._profile_runner_stepping = False

    def _launch_next_profile_account(self):
        """Launch the next account in the profile runner sequence. Returns True if launched."""
        idx = self._profile_runner_current_idx
        accounts = self._profile_runner_accounts
        scripts = self._profile_runner_scripts
        if idx >= len(accounts):
            return False
        acc = accounts[idx]
        aid = acc["id"]
        # Use first script from profile (or all in sequence — simplified to first for now)
        script_info = scripts[0] if scripts else None
        if not script_info:
            print(f"[PROFILE RUNNER] No scripts for account {aid}")
            self._profile_runner_current_idx += 1
            return False
        launched = self._launch_profile_account(aid, script_info["script_name"], script_info.get("world", 0))
        if launched:
            self._profile_runner_current_idx += 1
            # If batch mode and more in batch, wait for PID resolution before scheduling next
            mode = self._profile_runner_mode
            batch_size = self._profile_runner_batch_size
            if mode == "batch" and batch_size > 0:
                in_batch_so_far = (self._profile_runner_current_idx - 1) % batch_size + 1
                if in_batch_so_far < batch_size and self._profile_runner_current_idx < len(accounts):
                    print(f"[PROFILE RUNNER] Launched account {aid}, waiting for real PID before next launch")
                    self._profile_runner_wait_pid_aid = aid
                    self._profile_runner_wait_pid_start = time.time()
                else:
                    # Last account in batch launched — no need to wait for next
                    self._profile_runner_wait_pid_aid = None
                    self._profile_runner_wait_pid_start = 0
                    self._profile_runner_in_batch = False
                    print(f"[PROFILE RUNNER] Batch fully launched ({in_batch_so_far}/{batch_size}), clearing in-batch flag")
            else:
                self._profile_runner_wait_pid_aid = None
                self._profile_runner_wait_pid_start = 0
        return launched

    def _on_profile_stagger_timer(self):
        """Timer callback: continue launching next account in profile batch."""
        self._run_profile_step()

    def _poll_profile_runner(self):
        """Check running profile accounts for completion, advance when batch/sequential finishes."""
        if not self._profile_runner_active:
            return False
        # Timeout: if PID resolution takes too long, skip to next account
        if self._profile_runner_wait_pid_aid:
            wait_start = getattr(self, '_profile_runner_wait_pid_start', 0)
            if time.time() - wait_start > 28:
                print(f"[PROFILE RUNNER] PID wait timeout ({int(time.time() - wait_start)}s) for account {self._profile_runner_wait_pid_aid}, moving to next")
                self._profile_runner_wait_pid_aid = None
                self._profile_runner_wait_pid_start = 0
                self._run_profile_step()
                return True
        changed = False
        finished_accounts = []
        for aid, info in list(self._profile_runner_running.items()):
            pid = info.get("pid")
            done_file = info.get("done_file")
            log_file = info.get("log_file")
            finished = False
            # Check if process died
            if pid and not psutil.pid_exists(pid):
                print(f"[PROFILE RUNNER] Account {aid} process {pid} exited")
                finished = True
            # Check done file
            elif done_file and os.path.isfile(done_file):
                print(f"[PROFILE RUNNER] Account {aid} done file detected")
                finished = True
                try: os.remove(done_file)
                except: pass
            # Check log file for script end (skip if real PID resolved — log is bootstrapper's)
            elif not info.get("real_pid_resolved") and log_file and os.path.isfile(log_file):
                try:
                    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - 50 * 1024))
                        content = f.read().lower()
                    stop_phrases = [
                        "script stopped", "script finished", "stopping script",
                        "[stopped]", "script end", "script complete",
                        "onstop called", "script has ended", "script terminated",
                        "donefile created", "done file created",
                        "stopped bibs tut", "script stop triggered",
                        "script stop condition met", "stop condition reached",
                        "runtime reached", "target level reached",
                        "items collected reached", "script stop",
                    ]
                    for phrase in stop_phrases:
                        if phrase in content:
                            print(f"[PROFILE RUNNER] Account {aid} log match: '{phrase}'")
                            finished = True
                            break
                except Exception:
                    pass
            if finished:
                # Safety timer: bootstrapper accounts can't finish before min_end_time
                min_end = info.get("min_end_time", 0)
                if min_end and time.time() < min_end:
                    print(f"[PROFILE RUNNER] Account {aid} safety timer active ({min_end - time.time():.0f}s left), ignoring early exit")
                    continue
                # Grace period: don't finish if just launched
                elapsed = time.time() - info.get("start_time", 0)
                if elapsed < 15:
                    continue
                if pid and psutil.pid_exists(pid):
                    try: psutil.Process(pid).terminate()
                    except psutil.NoSuchProcess: pass
                self.db.record_stop(aid)
                finished_accounts.append(aid)
                changed = True

        for aid in finished_accounts:
            self._profile_runner_running.pop(aid, None)

        # If nothing running and there are still accounts left, advance
        mode = self._profile_runner_mode
        if not self._profile_runner_running and self._profile_runner_current_idx < len(self._profile_runner_accounts) and self._profile_runner_current_idx > 0:
            # Don't advance if we're still launching the current batch (stagger timer active)
            if self._profile_runner_in_batch:
                print(f"[PROFILE RUNNER] Batch accounts finished but still launching remaining in batch — waiting for stagger")
                return changed
            # Don't advance if any account in the just-finished batch had an active safety timer
            # (safety timer already expired to get here, but guard against edge cases)
            if mode == "batch" and self._profile_runner_batch_size > 0:
                print(f"[PROFILE RUNNER] Batch finished, starting next batch")
                self._run_profile_step()
                changed = True
            elif mode == "sequential":
                print(f"[PROFILE RUNNER] Sequential account finished, starting next")
                self._run_profile_step()
                changed = True

        return changed

    def _launch_profile_account(self, account_id, script_name, world=0):
        """Launch an account directly for profile runner. Returns True if launched."""
        print(f"[PROFILE LAUNCH] aid={account_id} script={script_name} world={world}")
        acc = self.db.get_account(account_id)
        if not acc:
            print(f"[PROFILE LAUNCH] aid={account_id} not found")
            return False
        # Ensure session
        sid = (acc.get("session_id") or "").strip()
        cid = (acc.get("character_id") or "").strip()
        if not sid or not cid:
            self.status.setText(f"Getting session for account {account_id}...")
            QApplication.processEvents()
            try:
                ok = self._ensure_session(account_id, quiet=True)
            except Exception as e:
                print(f"[PROFILE LAUNCH] _ensure_session crashed: {e}")
                ok = False
            if not ok:
                print(f"[PROFILE LAUNCH] Session fetch failed for aid={account_id}")
                return False
            acc = self.db.get_account(account_id)
            if not acc:
                return False
            sid = (acc.get("session_id") or "").strip()
            cid = (acc.get("character_id") or "").strip()
            if not sid or not cid:
                print(f"[PROFILE LAUNCH] Still no session for aid={account_id}")
                return False

        java = self._find_java()
        path = os.path.expandvars(self.jar.text())
        if not os.path.isfile(path):
            QMessageBox.critical(self, "Not Found", f"JAR not found:\n{path}")
            return False

        # Validate script name
        discovered = self._discover_scripts()
        discovered_set = set(discovered)
        actual_script = script_name
        if actual_script not in discovered_set:
            lower_script = actual_script.lower()
            if lower_script in self.SCRIPT_NAME_MAP:
                actual_script = self.SCRIPT_NAME_MAP[lower_script]
                print(f"[PROFILE FIX] Mapped '{script_name}' -> '{actual_script}'")
            else:
                lower_map = {n.lower(): n for n in discovered}
                if lower_script in lower_map:
                    actual_script = lower_map[lower_script]
                    print(f"[PROFILE FIX] Auto-corrected '{script_name}' -> '{actual_script}'")
                else:
                    print(f"[PROFILE ERROR] Script '{script_name}' not found in DreamBot")
                    return False

        nick = (acc.get("username") or "").strip() or acc["email"].split("@")[0]

        # Build proxy JVM args
        proxy_args = []
        proxy_id = acc.get("proxy_id")
        if proxy_id:
            for px in self.db.list_proxies():
                if px["id"] == proxy_id:
                    proto = px.get("protocol", "http")
                    phost = px["host"]
                    pport = px["port"]
                    puser = px.get("username", "")
                    ppass = px.get("password", "")
                    if proto == "socks5":
                        proxy_args.append(f"-DsocksProxyHost={phost}")
                        proxy_args.append(f"-DsocksProxyPort={pport}")
                        if puser and ppass:
                            proxy_args.append(f"-Djava.net.socks.username={puser}")
                            proxy_args.append(f"-Djava.net.socks.password={ppass}")
                    else:
                        proxy_args.append(f"-Dhttp.proxyHost={phost}")
                        proxy_args.append(f"-Dhttp.proxyPort={pport}")
                        proxy_args.append(f"-Dhttps.proxyHost={phost}")
                        proxy_args.append(f"-Dhttps.proxyPort={pport}")
                        if puser and ppass:
                            proxy_args.append(f"-Dhttp.proxyUser={puser}")
                            proxy_args.append(f"-Dhttp.proxyPassword={ppass}")
                            proxy_args.append(f"-Dhttps.proxyUser={puser}")
                            proxy_args.append(f"-Dhttps.proxyPassword={ppass}")
                    break

        # Bank cache bootstrapper
        use_bootstrapper = getattr(self, "cb_bank_cache", None) and self.cb_bank_cache.isChecked()
        if use_bootstrapper and actual_script:
            proxy_args.append(f"-Dbankcache.target.script={actual_script}")
            proxy_args.append(f"-Dbankcache.account.name={nick}")
            try:
                import tempfile
                fb_script = os.path.join(tempfile.gettempdir(), "bankcache_target_script.txt")
                with open(fb_script, "w", encoding="utf-8") as f:
                    f.write(actual_script)
                fb_name = os.path.join(tempfile.gettempdir(), "bankcache_account_name.txt")
                with open(fb_name, "w", encoding="utf-8") as f:
                    f.write(nick)
            except Exception:
                pass
            actual_script = "BibsTheKing"

        cmd = [java] + proxy_args + ["-Xmx512M", "-jar", os.path.abspath(path), "-script", actual_script]
        if world: cmd.extend(["-world", str(world)])
        if sid:
            cmd.append(f"-sessionId={sid}")
            if cid: cmd.append(f"-characterId={cid}")
            if nick: cmd.append(f"-displayName={nick}")
        else:
            cmd.extend([f"-accountUsername={acc['email']}", f"-accountPassword={acc['password']}"])
            if acc.get("pin"): cmd.append(f"-accountPin={acc['pin']}")
            if acc.get("totp"): cmd.append(f"-accountTotp={acc['totp']}")

        log_dir = os.path.expandvars(r"%USERPROFILE%\DreamBot\BotData\logs")
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        log_file = os.path.join(log_dir, f"profile_{account_id}_{ts}.log")
        done_file = os.path.join(log_dir, f"profile_done_{account_id}_{ts}.txt")
        if os.path.isfile(done_file):
            try: os.remove(done_file)
            except: pass
        cmd.append(f"-doneFile={done_file}")

        try:
            # Kill existing client
            for r in self.db.get_running():
                if r["id"] == account_id:
                    pid = r.get("pid")
                    if pid and psutil.pid_exists(pid):
                        try: psutil.Process(pid).terminate()
                        except psutil.NoSuchProcess: pass
                    self.db.record_stop(account_id)
                    break
            else:
                self.db.record_stop(account_id)
            with open(log_file, "w") as out:
                out.write(f"CMD: {' '.join(cmd)}\n\n")
                out.flush()
                proc = subprocess.Popen(cmd, cwd=os.path.dirname(os.path.abspath(path)),
                                      stdout=out, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            self.db.record_launch(account_id, actual_script, pid=proc.pid, status="Running")
            if use_bootstrapper and actual_script == "BibsTheKing":
                self._bootstrapper_pids.add(proc.pid)
            self._profile_runner_running[account_id] = {
                "pid": proc.pid,
                "script_name": actual_script,
                "log_file": log_file,
                "done_file": done_file,
                "start_time": time.time(),
                "min_end_time": time.time() + 40 if (use_bootstrapper and actual_script == "BibsTheKing") else 0,
            }
            QTimer.singleShot(28000, lambda lp=proc.pid, a=account_id: self._resolve_real_pid(lp, a))
            print(f"[PROFILE LAUNCH] aid={account_id} launched OK, PID={proc.pid}, script={actual_script}")
            return True
        except Exception as e:
            print(f"[PROFILE LAUNCH] aid={account_id} launch FAILED: {e}")
            import traceback
            traceback.print_exc()
            return False

    def closeEvent(self, ev):
        self.settings.setValue("jar", self.jar.text())
        ev.accept()

    # AI Agent Methods
    def _refresh_ai_agent(self):
        """Refresh AI Agent tab with current context"""
        if hasattr(self, 'ai_chat_history'):
            if self.ai_chat_history.toPlainText().strip() == "":
                self._ai_add_message("system", "AI Assistant initialized. I can help you with:")
                self._ai_add_message("system", "• Building script queues from categories or specific accounts")
                self._ai_add_message("system", "• Analyzing account status and performance")
                self._ai_add_message("system", "• Recommending scripts based on account levels")
                self._ai_add_message("system", "• Managing proxy assignments")
                self._ai_add_message("system", "• Creating profiles for specific tasks")
                self._ai_add_message("system", "\nType your question or use the quick action buttons above!")

    def _ai_add_message(self, sender, message):
        """Add a message to the AI chat history"""
        if hasattr(self, 'ai_chat_history'):
            timestamp = datetime.now().strftime("%H:%M")
            color = "#4CAF50" if sender == "user" else "#2196F3" if sender == "ai" else "#FF9800"
            formatted_message = f'<span style="color: {color}; font-weight: bold;">[{timestamp}] {sender.title()}:</span> {message}'
            self.ai_chat_history.append(formatted_message)
            # Auto-scroll to bottom
            scrollbar = self.ai_chat_history.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _ai_send_message(self):
        """Send user message to AI agent"""
        if not hasattr(self, 'ai_input'):
            return
        
        user_message = self.ai_input.text().strip()
        if not user_message:
            return
        
        self._ai_add_message("user", user_message)
        self.ai_input.clear()
        
        # Process the message
        self._ai_process_message(user_message)

    def _ai_process_message(self, message):
        """Process user message and generate AI response"""
        message_lower = message.lower()
        
        # Get current context
        accounts = self.db.list_accounts()
        categories = self.db.count_by_category()
        total_accounts = len(accounts)
        
        # Build context information
        context = {
            'total_accounts': total_accounts,
            'categories': categories,
            'running_accounts': len([a for a in accounts if a.get('status') == 'Running']),
            'banned_accounts': len([a for a in accounts if a.get('status') == 'Banned']),
            'available_scripts': self._discover_scripts()
        }
        
        # Process different types of requests
        if any(keyword in message_lower for keyword in ['queue', 'script queue', 'build queue']):
            self._ai_handle_queue_request(message_lower, context)
        elif any(keyword in message_lower for keyword in ['analyze', 'analysis', 'status', 'account']):
            self._ai_handle_analysis_request(context)
        elif any(keyword in message_lower for keyword in ['recommend', 'script', 'suggestion']):
            self._ai_handle_script_recommendation(context)
        elif any(keyword in message_lower for keyword in ['proxy', 'proxies']):
            self._ai_handle_proxy_request(context)
        elif any(keyword in message_lower for keyword in ['profile', 'create profile']):
            self._ai_handle_profile_request(context)
        elif any(keyword in message_lower for keyword in ['ban', 'banned', 'check', 'status', 'fresh', 'all']):
            self._ai_handle_ban_check_request(message, context)
        elif any(keyword in message_lower for keyword in ['tut', 'tutorial', 'bibs tut']):
            self._ai_handle_tutorial_request(message, context)
        elif any(keyword in message_lower for keyword in ['add account', 'new account', 'create account']):
            self._ai_handle_account_management(message, context, 'add')
        elif any(keyword in message_lower for keyword in ['delete account', 'remove account']):
            self._ai_handle_account_management(message, context, 'delete')
        elif any(keyword in message_lower for keyword in ['edit account', 'update account']):
            self._ai_handle_account_management(message, context, 'edit')
        elif any(keyword in message_lower for keyword in ['add proxy', 'new proxy', 'create proxy']):
            self._ai_handle_proxy_management(message, context, 'add')
        elif any(keyword in message_lower for keyword in ['delete proxy', 'remove proxy']):
            self._ai_handle_proxy_management(message, context, 'delete')
        elif any(keyword in message_lower for keyword in ['assign proxy', 'set proxy']):
            self._ai_handle_proxy_management(message, context, 'assign')
        elif any(keyword in message_lower for keyword in ['start queue', 'run queue', 'launch queue']):
            self._ai_handle_queue_control(message, context, 'start')
        elif any(keyword in message_lower for keyword in ['stop queue', 'kill queue', 'pause queue']):
            self._ai_handle_queue_control(message, context, 'stop')
        elif any(keyword in message_lower for keyword in ['clear queue', 'empty queue', 'reset queue']):
            self._ai_handle_queue_control(message, context, 'clear')
        elif any(keyword in message_lower for keyword in ['import', 'bulk import', 'add bulk']):
            self._ai_handle_bulk_operations(message, context, 'import')
        elif any(keyword in message_lower for keyword in ['export', 'backup', 'save accounts']):
            self._ai_handle_bulk_operations(message, context, 'export')
        elif any(keyword in message_lower for keyword in ['fetch session', 'get session', 'update session']):
            self._ai_handle_session_management(message, context)
        elif any(keyword in message_lower for keyword in ['change category', 'set category', 'move to']):
            self._ai_handle_category_management(message, context)
        elif any(keyword in message_lower for keyword in ['change status', 'set status']):
            self._ai_handle_status_management(message, context)
        elif any(keyword in message_lower for keyword in ['quick launch', 'fast launch', 'start script']):
            self._ai_handle_quick_launch(message, context)
        elif any(keyword in message_lower for keyword in ['create profile', 'new profile', 'make profile']):
            self._ai_handle_profile_management(message, context, 'create')
        elif any(keyword in message_lower for keyword in ['delete profile', 'remove profile']):
            self._ai_handle_profile_management(message, context, 'delete')
        elif any(keyword in message_lower for keyword in ['analytics', 'stats', 'statistics', 'performance']):
            self._ai_handle_analytics_request(message, context)
        elif any(keyword in message_lower for keyword in ['webhook', 'start webhook', 'stop webhook']):
            self._ai_handle_webhook_control(message, context)
        elif any(keyword in message_lower for keyword in ['heatmap', 'show heatmap', 'toggle heatmap']):
            self._ai_handle_heatmap_control(message, context)
        elif any(keyword in message_lower for keyword in ['ban log', 'ban history', 'banned accounts', 'ban record']):
            self._ai_handle_ban_log_request(message, context)
        else:
            self._ai_handle_general_request(message, context)

    def _ai_handle_queue_request(self, message, context):
        """Handle queue building requests"""
        # Extract category from message
        category = None
        for cat in context['categories']:
            if cat.lower() in message:
                category = cat
                break
        
        if category:
            accounts_in_category = [a for a in self.db.list_accounts() if a.get('category') == category]
            self._ai_add_message("ai", f"Found {len(accounts_in_category)} accounts in '{category}' category.")
            self._ai_add_message("ai", f"I can create a script queue for these accounts. Available scripts: {', '.join(context['available_scripts'][:5])}...")
            self._ai_add_message("ai", "Would you like me to create a queue with a specific script? Please specify:")
            self._ai_add_message("ai", "• Script name")
            self._ai_add_message("ai", "• World (optional, default: 420)")
            self._ai_add_message("ai", "• Stop condition (Process Exit, Target Level, Runtime, Items Collected, Script End)")
            self._ai_add_message("ai", "• Stop value (if applicable)")
        else:
            self._ai_add_message("ai", "I can help you build a script queue! Please specify:")
            self._ai_add_message("ai", "• Category name (e.g., 'Main', 'Pure', 'Skiller')")
            self._ai_add_message("ai", "• Or specific accounts")
            self._ai_add_message("ai", "• Script name")
            self._ai_add_message("ai", "• Queue mode (parallel/sequential)")
            
        # List available categories
        self._ai_add_message("ai", f"Available categories: {', '.join(context['categories'].keys())}")

    def _ai_handle_analysis_request(self, context):
        """Handle account analysis requests"""
        self._ai_add_message("ai", "📊 Account Analysis:")
        self._ai_add_message("ai", f"• Total accounts: {context['total_accounts']}")
        self._ai_add_message("ai", f"• Running: {context['running_accounts']}")
        self._ai_add_message("ai", f"• Banned: {context['banned_accounts']}")
        self._ai_add_message("ai", f"• Available: {context['total_accounts'] - context['running_accounts'] - context['banned_accounts']}")
        
        self._ai_add_message("ai", "📈 Category Breakdown:")
        for cat, count in context['categories'].items():
            if cat != "All Accounts":
                self._ai_add_message("ai", f"• {cat}: {count} accounts")
        
        # Get recent activity
        running = self.db.get_running()
        if running:
            self._ai_add_message("ai", f"🚀 Currently running: {len(running)} accounts")
            for acc in running[:3]:  # Show first 3
                self._ai_add_message("ai", f"• {acc.get('email', '').split('@')[0]} - {acc.get('script', 'Unknown script')}")

    def _ai_handle_script_recommendation(self, context):
        """Handle script recommendation requests"""
        self._ai_add_message("ai", "🎯 Script Recommendations:")
        
        # Get some account stats for better recommendations
        accounts = self.db.list_accounts()
        skiller_accounts = [a for a in accounts if a.get('category') == 'Skiller']
        main_accounts = [a for a in accounts if a.get('category') == 'Main']
        pure_accounts = [a for a in accounts if a.get('category') == 'Pure']
        
        if skiller_accounts:
            self._ai_add_message("ai", f"• For {len(skiller_accounts)} skiller accounts: Woodcutting, Fishing, Mining, Cooking")
        if main_accounts:
            self._ai_add_message("ai", f"• For {len(main_accounts)} main accounts: Slayer, Bossing, Raids, Farming")
        if pure_accounts:
            self._ai_add_message("ai", f"• For {len(pure_accounts)} pure accounts: Range Guild, NMZ, Pest Control")
        
        self._ai_add_message("ai", f"Available scripts: {', '.join(context['available_scripts'][:10])}...")
        self._ai_add_message("ai", "Would you like me to create a queue with specific scripts?")

    def _ai_handle_proxy_request(self, context):
        """Handle proxy-related requests"""
        proxies = self.db.list_proxies()
        accounts_with_proxies = [a for a in self.db.list_accounts() if a.get('proxy_id')]
        
        self._ai_add_message("ai", "🌐 Proxy Analysis:")
        self._ai_add_message("ai", f"• Total proxies: {len(proxies)}")
        self._ai_add_message("ai", f"• Accounts with proxies: {len(accounts_with_proxies)}")
        self._ai_add_message("ai", f"• Accounts without proxies: {context['total_accounts'] - len(accounts_with_proxies)}")
        
        if len(accounts_with_proxies) < context['total_accounts']:
            self._ai_add_message("ai", "💡 Recommendation: Consider assigning proxies to more accounts for better security.")
        
        self._ai_add_message("ai", "Would you like me to help assign proxies to specific accounts?")

    def _ai_handle_profile_request(self, context):
        """Handle profile creation requests"""
        profiles = self.db.list_profiles()
        self._ai_add_message("ai", "📋 Profile Management:")
        self._ai_add_message("ai", f"• Existing profiles: {len(profiles)}")
        
        if profiles:
            self._ai_add_message("ai", "Current profiles:")
            for profile in profiles[:3]:  # Show first 3
                self._ai_add_message("ai", f"• {profile.get('name', 'Unnamed')} - {profile.get('description', 'No description')}")
        
        self._ai_add_message("ai", "I can help you create a new profile! Please specify:")
        self._ai_add_message("ai", "• Profile name")
        self._ai_add_message("ai", "• Target accounts or category")
        self._ai_add_message("ai", "• Scripts to include")
        self._ai_add_message("ai", "• Queue mode (parallel/sequential)")

    def _ai_handle_ban_check_request(self, message, context):
        """Handle ban check requests"""
        # Check if user wants to check all accounts
        if any(keyword in message.lower() for keyword in ['all', 'fresh', 'every']):
            self._ai_add_message("ai", "🔍 Starting fresh ban check on all accounts...")
            self._ai_add_message("ai", f"Checking {context['total_accounts']} accounts for ban status...")
            
            # Get all account IDs
            all_accounts = self.db.list_accounts()
            all_ids = [acc['id'] for acc in all_accounts]
            
            # Start the ban check process
            self._ai_add_message("ai", "⏳ This may take a while depending on the number of accounts...")
            self._ai_add_message("ai", "I'll check each account's status by looking up their hiscores data.")
            
            # Trigger the actual ban check with AI-safe method
            try:
                results = self._ai_check_status_safe(all_ids)
                self._ai_add_message("ai", f"✅ Ban check completed!")
                self._ai_add_message("ai", f"📊 Results: {results['checked']} accounts checked")
                self._ai_add_message("ai", f"• Active: {results['active']}")
                self._ai_add_message("ai", f"• Tutorial Island: {results['tut']}")
                self._ai_add_message("ai", f"• Banned: {results['banned']}")
                self._ai_add_message("ai", f"• Uncategorized: {results['uncategorized']}")
                self._ai_add_message("ai", f"• Failed: {results['failed']}")
                if results['banned'] > 0:
                    self._ai_add_message("ai", f"⚠️ Found {results['banned']} banned accounts - they've been moved to Banned category")
                if results['tut'] > 0:
                    self._ai_add_message("ai", f"🎓 Found {results['tut']} Tutorial Island accounts - they've been moved to Tutorial Island category")
                if results['uncategorized'] > 0:
                    self._ai_add_message("ai", f"📋 Found {results['uncategorized']} accounts without session IDs - they've been moved to Uncategorized category")
                    self._ai_add_message("ai", "💡 Use 'fetch session' to get Jagex sessions for these accounts")
            except Exception as e:
                self._ai_add_message("ai", f"❌ Error during ban check: {str(e)}")
        else:
            # Check for specific category
            category = None
            for cat in context['categories']:
                if cat.lower() in message.lower():
                    category = cat
                    break
            
            if category:
                accounts_in_category = [a for a in self.db.list_accounts() if a.get('category') == category]
                if accounts_in_category:
                    self._ai_add_message("ai", f"🔍 Starting ban check on {len(accounts_in_category)} accounts in '{category}' category...")
                    category_ids = [acc['id'] for acc in accounts_in_category]
                    
                    try:
                        results = self._ai_check_status_safe(category_ids)
                        self._ai_add_message("ai", f"✅ Ban check completed for {category} accounts!")
                        self._ai_add_message("ai", f"📊 Results: {results['checked']} accounts checked")
                        self._ai_add_message("ai", f"• Active: {results['active']}")
                        self._ai_add_message("ai", f"• Tutorial Island: {results['tut']}")
                        self._ai_add_message("ai", f"• Banned: {results['banned']}")
                        self._ai_add_message("ai", f"• Uncategorized: {results['uncategorized']}")
                        self._ai_add_message("ai", f"• Failed: {results['failed']}")
                        if results['uncategorized'] > 0:
                            self._ai_add_message("ai", f"📋 Found {results['uncategorized']} accounts without session IDs - they've been moved to Uncategorized category")
                    except Exception as e:
                        self._ai_add_message("ai", f"❌ Error during ban check: {str(e)}")
                else:
                    self._ai_add_message("ai", f"No accounts found in '{category}' category.")
            else:
                self._ai_add_message("ai", "🔍 Ban Check Options:")
                self._ai_add_message("ai", "• Say 'check all accounts' or 'fresh check' to check every account")
                self._ai_add_message("ai", "• Say 'check Main accounts' or 'check Pure accounts' for specific categories")
                self._ai_add_message("ai", "• I'll check hiscores data to determine if accounts are banned")
                self._ai_add_message("ai", f"Available categories: {', '.join([k for k in context['categories'].keys() if k != 'All Accounts'])}")

    def _ai_check_status_safe(self, ids):
        """AI-safe version of account status check that returns results instead of showing dialog"""
        banned = 0
        active = 0
        tut = 0
        uncategorized = 0
        failed = 0
        checked = 0
        
        for aid in ids:
            acc = self.db.get_account(aid)
            if not acc:
                continue
            checked += 1
            
            # Check if account has session ID - if not, move to Uncategorized
            session_id = acc.get("session_id", "").strip()
            if not session_id:
                if acc.get("category") != "Uncategorized":
                    self.db.update_account(aid, category="Uncategorized")
                uncategorized += 1
                continue
            
            dn = (acc.get("display_name") or "").strip()
            uname = dn or (acc.get("username") or "").strip()
            # No display name and username is a Jagex login -> Tutorial Island
            if not dn and ("+" in uname or not uname):
                if acc.get("category") != "Tutorial Island":
                    self.db.update_account(aid, category="Tutorial Island")
                tut += 1
                continue
            if not uname:
                failed += 1
                continue
            try:
                stats, err = _do_fetch_osrs_stats_raw(aid, uname)
                if stats:
                    active += 1
                    self.db.save_account_stats(aid, uname, stats)
                    current_cat = acc.get("category") or ""
                    # Only move to Ready To Farm if not manually set to Banned or Finished
                    if current_cat not in ("Banned", "Finished"):
                        self.db.update_account(aid, status="Offline", category="Ready To Farm")
                    else:
                        self.db.update_account(aid, status="Offline")
                elif err and "404" in err:
                    # 404 = removed from hiscores = banned
                    previous_category = acc.get("category", "Unknown")
                    self.db.update_account(aid, category="Banned")
                    # Log the ban with timestamp
                    self.db.log_ban(aid, detected_by="ai_status_check", previous_category=previous_category, notes="404 error from OSRS hiscores")
                    banned += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                
        self._refresh_all()
        return {
            'checked': checked,
            'active': active,
            'tut': tut,
            'banned': banned,
            'uncategorized': uncategorized,
            'failed': failed
        }

    def _ai_handle_tutorial_request(self, message, context):
        """Handle tutorial category and script requests"""
        # Check for tutorial island accounts
        tutorial_accounts = [a for a in self.db.list_accounts() if a.get('category') == 'Tutorial Island']
        
        if tutorial_accounts:
            self._ai_add_message("ai", f"🎓 Found {len(tutorial_accounts)} accounts in Tutorial Island category.")
            
            # Check if user wants to run BibsTut script
            if any(keyword in message.lower() for keyword in ['bibs tut', 'run', 'start', 'queue']):
                self._ai_add_message("ai", "🚀 I can help you create a script queue for Tutorial Island accounts with BibsTut script!")
                self._ai_add_message("ai", f"Setting up queue for {len(tutorial_accounts)} Tutorial Island accounts...")
                self._ai_add_message("ai", "Script: BibsTut")
                self._ai_add_message("ai", "Stop condition: Script End (when tutorial is completed)")
                self._ai_add_message("ai", "World: 420 (default)")
                
                # Create the queue with BibsTut script
                try:
                    # Get tutorial account IDs
                    tutorial_ids = [acc['id'] for acc in tutorial_accounts]
                    
                    # Add BibsTut script to queue for each tutorial account
                    for aid in tutorial_ids:
                        acc = self.db.get_account(aid)
                        if acc:
                            self.db.add_to_queue(aid, "BibsTut", 420, "Script End", "", 1)
                    
                    self._ai_add_message("ai", f"✅ Successfully added {len(tutorial_ids)} Tutorial Island accounts to queue with BibsTut script!")
                    self._ai_add_message("ai", "💡 You can now start the queue from the Script Queue tab.")
                    self._ai_add_message("ai", "📋 Queue Summary:")
                    self._ai_add_message("ai", f"• Accounts: {len(tutorial_ids)} Tutorial Island accounts")
                    self._ai_add_message("ai", "• Script: BibsTut")
                    self._ai_add_message("ai", "• Stop condition: Script End (tutorial completion)")
                    self._ai_add_message("ai", "• World: 420")
                    
                    # Refresh the queue display
                    self._refresh_all()
                    
                except Exception as e:
                    self._ai_add_message("ai", f"❌ Error creating tutorial queue: {str(e)}")
            else:
                self._ai_add_message("ai", "📋 Tutorial Island Accounts:")
                for i, acc in enumerate(tutorial_accounts[:5]):  # Show first 5
                    email_name = acc['email'].split('@')[0]
                    self._ai_add_message("ai", f"• {email_name} - {acc.get('status', 'Offline')}")
                
                if len(tutorial_accounts) > 5:
                    self._ai_add_message("ai", f"... and {len(tutorial_accounts) - 5} more")
                
                self._ai_add_message("ai", "")
                self._ai_add_message("ai", "💡 Available actions:")
                self._ai_add_message("ai", "• Say 'run BibsTut on tutorial accounts' to create a queue")
                self._ai_add_message("ai", "• Say 'start tutorial queue' to begin running BibsTut")
                self._ai_add_message("ai", "• I'll set up the queue with Script End condition")
        else:
            self._ai_add_message("ai", "🎓 No accounts found in Tutorial Island category.")
            self._ai_add_message("ai", "💡 To get Tutorial Island accounts:")
            self._ai_add_message("ai", "• Run a fresh status check on all accounts")
            self._ai_add_message("ai", "• Accounts that return 404 from hiscores will be marked as Tutorial Island")
            self._ai_add_message("ai", "• Then ask me to run BibsTut on them")

    def _ai_handle_general_request(self, message, context):
        """Handle general AI requests"""
        message_lower = message.lower().strip()
        
        # Handle math questions
        if any(op in message_lower for op in ['+', '-', '*', '/', '=', 'calculate', 'what is', 'solve']):
            try:
                # Simple math evaluation
                import re
                # Extract math expression
                math_expr = re.search(r'([\d+\-*/().\s]+)', message)
                if math_expr:
                    expr = math_expr.group(1).strip()
                    # Safe evaluation
                    result = eval(expr)
                    self._ai_add_message("ai", f"🧮 {expr} = {result}")
                    return
            except:
                pass
        
        # Handle general questions
        if any(q in message_lower for q in ['what', 'who', 'where', 'when', 'why', 'how', 'can you', 'are you', 'tell me']):
            self._ai_add_message("ai", f"🤔 I understand you're asking: '{message}'")
            self._ai_add_message("ai", "I'm your DreamBot Farm Manager AI assistant! I can help you manage your entire bot farm through natural language.")
            self._ai_add_message("ai", "")
            self._ai_add_message("ai", "🎮 **Farm Management Commands:**")
            self._ai_add_message("ai", "• Account Management: 'add account email:pass'")
            self._ai_add_message("ai", "• Proxy Management: 'add proxy 127.0.0.1:8080'")
            self._ai_add_message("ai", "• Queue Control: 'start queue', 'stop queue'")
            self._ai_add_message("ai", "• Ban Checks: 'check all accounts for bans'")
            self._ai_add_message("ai", "• Tutorial Management: 'run BibsTut on tutorial accounts'")
            self._ai_add_message("ai", "• Analytics: 'analytics', 'stats'")
            self._ai_add_message("ai", "• Quick Launch: 'quick launch user@gmail.com with Woodcutting'")
            self._ai_add_message("ai", "")
            self._ai_add_message("ai", "🤖 **General AI Features:**")
            self._ai_add_message("ai", "• Math calculations: '5+5', '10*3', '100/4'")
            self._ai_add_message("ai", "• Farm status: 'how many accounts do I have?'")
            self._ai_add_message("ai", "• Help: 'help', 'what can you do?'")
            self._ai_add_message("ai", "")
            self._ai_add_message("ai", "💡 **Try asking me:**")
            self._ai_add_message("ai", "• 'What's my farm status?'")
            self._ai_add_message("ai", "• 'Add account farmer@gmail.com:pass123'")
            self._ai_add_message("ai", "• 'Check all accounts for bans'")
            self._ai_add_message("ai", "• 'Start the queue'")
            self._ai_add_message("ai", "• 'What is 25 * 4?'")
        else:
            # Default response with farm management capabilities
            self._ai_add_message("ai", f"🤖 I understand: '{message}'")
            self._ai_add_message("ai", "I'm your DreamBot Farm Manager AI! I can help you manage your entire bot farm.")
            self._ai_add_message("ai", "")
            self._ai_add_message("ai", "📊 **Current Farm Status:**")
            self._ai_add_message("ai", f"• Total accounts: {context['total_accounts']}")
            self._ai_add_message("ai", f"• Currently running: {context['running_accounts']}")
            self._ai_add_message("ai", f"• Queue items: {len(self.db.get_queue())}")
            self._ai_add_message("ai", "")
            self._ai_add_message("ai", "🎮 **Quick Commands:**")
            self._ai_add_message("ai", "• 'start queue' - Launch queued scripts")
            self._ai_add_message("ai", "• 'check all accounts for bans' - Fresh status check")
            self._ai_add_message("ai", "• 'analytics' - View farm statistics")
            self._ai_add_message("ai", "• 'help' - See all commands")
            self._ai_add_message("ai", "")
            self._ai_add_message("ai", "💡 **I can also do math!** Try: '5+5' or '10*3'")

    def _ai_handle_account_management(self, message, context, action):
        """Handle account management operations"""
        if action == 'add':
            self._ai_add_message("ai", "👤 Adding new account...")
            # Extract email and password from message
            import re
            email_pattern = r'[\w\.-]+@[\w\.-]+\.\w+'
            password_pattern = r'password[:\s]+([^\s]+)'
            
            email_match = re.search(email_pattern, message)
            password_match = re.search(password_pattern, message, re.IGNORECASE)
            
            if email_match and password_match:
                email = email_match.group()
                password = password_match.group(1)
                
                try:
                    self.db.add_account(email, password)
                    self._ai_add_message("ai", f"✅ Successfully added account: {email}")
                    self._refresh_all()
                except Exception as e:
                    self._ai_add_message("ai", f"❌ Error adding account: {str(e)}")
            else:
                self._ai_add_message("ai", "📝 To add an account, please provide:")
                self._ai_add_message("ai", "Format: 'add account email:password'")
                self._ai_add_message("ai", "Example: 'add account user@gmail.com:mypass123'")
                
        elif action == 'delete':
            self._ai_add_message("ai", "⚠️ Delete account operation requires confirmation.")
            self._ai_add_message("ai", "Please specify the email address of the account to delete.")
            self._ai_add_message("ai", "Example: 'delete account user@gmail.com'")
            
        elif action == 'edit':
            self._ai_add_message("ai", "✏️ Edit account functionality - please specify:")
            self._ai_add_message("ai", "• Account email")
            self._ai_add_message("ai", "• What to change (password, category, status, etc.)")
            self._ai_add_message("ai", "• New value")
            self._ai_add_message("ai", "Example: 'edit account user@gmail.com set category Main'")

    def _ai_handle_proxy_management(self, message, context, action):
        """Handle proxy management operations"""
        if action == 'add':
            self._ai_add_message("ai", "🌐 Adding new proxy...")
            import re
            # Extract proxy info from message
            proxy_pattern = r'([\d\.]+):(\d+)(?::([^:]+):([^:]*))?'
            proxy_match = re.search(proxy_pattern, message)
            
            if proxy_match:
                host = proxy_match.group(1)
                port = proxy_match.group(2)
                username = proxy_match.group(3) or ""
                password = proxy_match.group(4) or ""
                
                try:
                    self.db.add_proxy(host, int(port), username, password, "http")
                    self._ai_add_message("ai", f"✅ Successfully added proxy: {host}:{port}")
                    self._refresh_all()
                except Exception as e:
                    self._ai_add_message("ai", f"❌ Error adding proxy: {str(e)}")
            else:
                self._ai_add_message("ai", "📝 To add a proxy, use format:")
                self._ai_add_message("ai", "'add proxy host:port' or 'add proxy host:port:user:pass'")
                self._ai_add_message("ai", "Example: 'add proxy 127.0.0.1:8080'")
                
        elif action == 'assign':
            self._ai_add_message("ai", "🔗 Proxy assignment - please specify:")
            self._ai_add_message("ai", "• Account email")
            self._ai_add_message("ai", "• Proxy (host:port)")
            self._ai_add_message("ai", "Example: 'assign proxy user@gmail.com to 127.0.0.1:8080'")

    def _ai_handle_queue_control(self, message, context, action):
        """Handle queue control operations"""
        if action == 'start':
            try:
                self._queue_start()
                self._ai_add_message("ai", "🚀 Queue started successfully!")
                self._ai_add_message("ai", "Monitoring script execution and will advance queue when scripts complete.")
            except Exception as e:
                self._ai_add_message("ai", f"❌ Error starting queue: {str(e)}")
                
        elif action == 'stop':
            try:
                running = self.db.get_running()
                if running:
                    stopped = 0
                    for r in running:
                        pid = r.get("pid")
                        if pid:
                            try:
                                psutil.Process(pid).terminate()
                                stopped += 1
                            except psutil.NoSuchProcess:
                                pass
                        self.db.record_stop(r["id"])
                    
                    self._refresh_all()
                    self._ai_add_message("ai", f"🛑 Stopped {stopped} running instances")
                else:
                    self._ai_add_message("ai", "ℹ️ No scripts are currently running")
            except Exception as e:
                self._ai_add_message("ai", f"❌ Error stopping queue: {str(e)}")
                
        elif action == 'clear':
            try:
                count = len(self.db.get_queue())
                self.db.clear_queue()
                self._refresh_all()
                self._ai_add_message("ai", f"🗑️ Cleared {count} items from queue")
            except Exception as e:
                self._ai_add_message("ai", f"❌ Error clearing queue: {str(e)}")

    def _ai_handle_bulk_operations(self, message, context, action):
        """Handle bulk import/export operations"""
        if action == 'import':
            self._ai_add_message("ai", "📥 Bulk import - please provide:")
            self._ai_add_message("ai", "• Account data in format: email:pass[:totp[:pin]]")
            self._ai_add_message("ai", "• One account per line")
            self._ai_add_message("ai", "Use the bulk import dialog in Account Settings tab for better results")
            
        elif action == 'export':
            try:
                accounts = self.db.list_accounts()
                export_data = []
                for acc in accounts:
                    line = f"{acc['email']}:{acc['password']}"
                    if acc.get('totp'):
                        line += f":{acc['totp']}"
                    if acc.get('pin'):
                        line += f":{acc['pin']}"
                    export_data.append(line)
                
                self._ai_add_message("ai", f"📤 Exported {len(export_data)} accounts")
                self._ai_add_message("ai", "First 5 accounts:")
                for i, line in enumerate(export_data[:5]):
                    self._ai_add_message("ai", f"{i+1}. {line}")
                if len(export_data) > 5:
                    self._ai_add_message("ai", f"... and {len(export_data) - 5} more")
            except Exception as e:
                self._ai_add_message("ai", f"❌ Error exporting accounts: {str(e)}")

    def _ai_handle_session_management(self, message, context):
        """Handle session management operations"""
        try:
            self._ai_add_message("ai", "🔑 Fetching Jagex sessions for all accounts...")
            self._fetch_display_names()
            self._ai_add_message("ai", "✅ Session fetch completed! Check account display names.")
        except Exception as e:
            self._ai_add_message("ai", f"❌ Error fetching sessions: {str(e)}")

    def _ai_handle_category_management(self, message, context):
        """Handle category management operations"""
        self._ai_add_message("ai", "📁 Category management - please specify:")
        self._ai_add_message("ai", "• Account(s) to move (email or 'all')")
        self._ai_add_message("ai", "• Target category")
        self._ai_add_message("ai", "Example: 'move account user@gmail.com to Main'")
        self._ai_add_message("ai", "Example: 'change category all accounts to Ready To Farm'")

    def _ai_handle_status_management(self, message, context):
        """Handle status management operations"""
        self._ai_add_message("ai", "🔄 Status management - please specify:")
        self._ai_add_message("ai", "• Account(s) to update (email or 'all')")
        self._ai_add_message("ai", "• New status (Offline, Running, etc.)")
        self._ai_add_message("ai", "Example: 'set status user@gmail.com to Offline'")

    def _ai_handle_quick_launch(self, message, context):
        """Handle quick launch operations"""
        self._ai_add_message("ai", "⚡ Quick launch - please specify:")
        self._ai_add_message("ai", "• Account email")
        self._ai_add_message("ai", "• Script name")
        self._ai_add_message("ai", "• World (optional, default: 420)")
        self._ai_add_message("ai", "Example: 'quick launch user@gmail.com with Woodcutting on world 420'")

    def _ai_handle_profile_management(self, message, context, action):
        """Handle profile management operations"""
        if action == 'create':
            self._ai_add_message("ai", "📋 Profile creation - please specify:")
            self._ai_add_message("ai", "• Profile name")
            self._ai_add_message("ai", "• Description (optional)")
            self._ai_add_message("ai", "• Target accounts or category")
            self._ai_add_message("ai", "• Scripts to include")
            self._ai_add_message("ai", "Example: 'create profile Main Farm with Main accounts and Woodcutting script'")
            
        elif action == 'delete':
            self._ai_add_message("ai", "⚠️ Profile deletion - please specify profile name")
            self._ai_add_message("ai", "Example: 'delete profile Main Farm'")

    def _ai_handle_analytics_request(self, message, context):
        """Handle analytics requests"""
        try:
            self._ai_add_message("ai", "📊 Farm Analytics:")
            self._ai_add_message("ai", f"• Total accounts: {context['total_accounts']}")
            self._ai_add_message("ai", f"• Currently running: {context['running_accounts']}")
            self._ai_add_message("ai", f"• Banned accounts: {context['banned_accounts']}")
            self._ai_add_message("ai", f"• Available for farming: {context['total_accounts'] - context['running_accounts'] - context['banned_accounts']}")
            
            # Show category breakdown
            self._ai_add_message("ai", "📈 Category Distribution:")
            for cat, count in context['categories'].items():
                if cat != "All Accounts":
                    percentage = (count / context['total_accounts']) * 100 if context['total_accounts'] > 0 else 0
                    self._ai_add_message("ai", f"• {cat}: {count} ({percentage:.1f}%)")
                    
            # Show queue status
            queue_items = len(self.db.get_queue())
            self._ai_add_message("ai", f"🚀 Queue items: {queue_items}")
            
        except Exception as e:
            self._ai_add_message("ai", f"❌ Error generating analytics: {str(e)}")

    def _ai_handle_webhook_control(self, message, context):
        """Handle webhook control operations"""
        if 'start' in message.lower() or 'enable' in message.lower():
            self._ai_add_message("ai", "🌐 Webhook server is already running on port 8767")
            self._ai_add_message("ai", "Receiving location updates from active DreamBot instances")
        else:
            self._ai_add_message("ai", "ℹ️ Webhook server cannot be stopped - it's essential for location tracking")

    def _ai_handle_heatmap_control(self, message, context):
        """Handle heatmap control operations"""
        if 'show' in message.lower() or 'enable' in message.lower():
            self._ai_add_message("ai", "🗺️ Navigate to the Heatmap tab to view live bot locations")
            self._ai_add_message("ai", "Shows real-time positions and activities of all running bots")
        else:
            self._ai_add_message("ai", "ℹ️ Heatmap is available in the Heatmap tab - switch tabs to view it")

    def _ai_handle_ban_log_request(self, message, context):
        """Handle ban log requests"""
        try:
            # Check if user wants specific account ban log
            import re
            email_pattern = r'[\w\.-]+@[\w\.-]+\.\w+'
            email_match = re.search(email_pattern, message)
            
            if email_match:
                # Get ban log for specific account
                email = email_match.group()
                account = self.db.get_account_by_email(email)
                if account:
                    ban_entries = self.db.get_ban_log(account['id'], limit=20)
                    if ban_entries:
                        self._ai_add_message("ai", f"📋 Ban History for {email}:")
                        for entry in ban_entries:
                            banned_time = entry['banned_at']
                            if isinstance(banned_time, str):
                                # Parse timestamp and format nicely
                                try:
                                    from datetime import datetime
                                    dt = datetime.fromisoformat(banned_time.replace('Z', '+00:00'))
                                    formatted_time = dt.strftime("%d:%m:%Y %H:%M:%S")
                                except:
                                    formatted_time = str(banned_time)
                            else:
                                formatted_time = str(banned_time)
                            
                            self._ai_add_message("ai", f"• {formatted_time} - Detected by: {entry['detected_by']}")
                            self._ai_add_message("ai", f"  Previous category: {entry['previous_category'] or 'Unknown'}")
                            self._ai_add_message("ai", f"  Notes: {entry['notes'] or 'No notes'}")
                            self._ai_add_message("ai", "")
                    else:
                        self._ai_add_message("ai", f"✅ No ban records found for {email}")
                else:
                    self._ai_add_message("ai", f"❌ Account {email} not found")
            else:
                # Get overall ban log
                ban_entries = self.db.get_ban_log(limit=50)
                if ban_entries:
                    self._ai_add_message("ai", f"📋 Recent Ban History (Last {len(ban_entries)} entries):")
                    self._ai_add_message("ai", "")
                    
                    for entry in ban_entries[:10]:  # Show first 10
                        banned_time = entry['banned_at']
                        if isinstance(banned_time, str):
                            try:
                                from datetime import datetime
                                dt = datetime.fromisoformat(banned_time.replace('Z', '+00:00'))
                                formatted_time = dt.strftime("%d:%m:%Y %H:%M:%S")
                            except:
                                formatted_time = str(banned_time)
                        else:
                            formatted_time = str(banned_time)
                        
                        email_name = entry['email'].split('@')[0]
                        self._ai_add_message("ai", f"• {formatted_time} - {email_name}")
                        self._ai_add_message("ai", f"  Detected by: {entry['detected_by']}")
                        self._ai_add_message("ai", f"  Previous: {entry['previous_category'] or 'Unknown'}")
                        self._ai_add_message("ai", "")
                    
                    if len(ban_entries) > 10:
                        self._ai_add_message("ai", f"... and {len(ban_entries) - 10} more entries")
                    
                    self._ai_add_message("ai", f"💡 Total logged bans: {len(ban_entries)}")
                    self._ai_add_message("ai", "💡 Ask 'ban log for email@example.com' for specific account history")
                else:
                    self._ai_add_message("ai", "✅ No ban records found in the database")
                    self._ai_add_message("ai", "💡 Ban records are created when accounts are detected as banned during status checks")
        except Exception as e:
            self._ai_add_message("ai", f"❌ Error retrieving ban log: {str(e)}")

    # Quick action methods
    def _ai_build_queue(self):
        """Quick action: Build script queue"""
        self._ai_add_message("user", "Build script queue")
        self._ai_handle_queue_request("build queue", {
            'total_accounts': len(self.db.list_accounts()),
            'categories': self.db.count_by_category(),
            'running_accounts': len([a for a in self.db.list_accounts() if a.get('status') == 'Running']),
            'banned_accounts': len([a for a in self.db.list_accounts() if a.get('status') == 'Banned']),
            'available_scripts': self._discover_scripts()
        })

    def _ai_analyze_accounts(self):
        """Quick action: Analyze accounts"""
        self._ai_add_message("user", "Analyze accounts")
        self._ai_handle_analysis_request({
            'total_accounts': len(self.db.list_accounts()),
            'categories': self.db.count_by_category(),
            'running_accounts': len([a for a in self.db.list_accounts() if a.get('status') == 'Running']),
            'banned_accounts': len([a for a in self.db.list_accounts() if a.get('status') == 'Banned']),
            'available_scripts': self._discover_scripts()
        })

    def _ai_recommend_scripts(self):
        """Quick action: Recommend scripts"""
        self._ai_add_message("user", "Recommend scripts")
        self._ai_handle_script_recommendation({
            'total_accounts': len(self.db.list_accounts()),
            'categories': self.db.count_by_category(),
            'running_accounts': len([a for a in self.db.list_accounts() if a.get('status') == 'Running']),
            'banned_accounts': len([a for a in self.db.list_accounts() if a.get('status') == 'Banned']),
            'available_scripts': self._discover_scripts()
        })

class AccountDialog(QDialog):
    def __init__(self, parent=None, account=None):
        super().__init__(parent)
        self.acc = account
        self.setWindowTitle("Edit" if account else "Add Account")
        self.setMinimumWidth(400)
        l = QVBoxLayout(self)
        self.ee = QLineEdit(); self.ee.setPlaceholderText("email")
        l.addWidget(QLabel("Email:")); l.addWidget(self.ee)
        self.pe = QLineEdit(); self.pe.setPlaceholderText("password")
        l.addWidget(QLabel("Password:")); l.addWidget(self.pe)
        self.pie = QLineEdit(); self.pie.setPlaceholderText("PIN (opt)"); self.pie.setMaxLength(4)
        l.addWidget(QLabel("PIN:")); l.addWidget(self.pie)
        self.te = QLineEdit(); self.te.setPlaceholderText("TOTP (opt)")
        l.addWidget(QLabel("TOTP:")); l.addWidget(self.te)
        self.ue = QLineEdit(); self.ue.setPlaceholderText("Username (opt)")
        l.addWidget(QLabel("Username:")); l.addWidget(self.ue)
        self.de = QLineEdit(); self.de.setPlaceholderText("Display Name (for hiscores)")
        l.addWidget(QLabel("Display Name:")); l.addWidget(self.de)
        self.cc = QComboBox(); self.cc.addItems(CATEGORIES[1:])
        l.addWidget(QLabel("Category:")); l.addWidget(self.cc)
        self.sc = QComboBox(); self.sc.addItems(STATUSES)
        l.addWidget(QLabel("Status:")); l.addWidget(self.sc)
        self.pc = QComboBox()
        self.pc.addItem("No Proxy", None)
        if hasattr(parent, "db"):
            for p in parent.db.list_proxies():
                self.pc.addItem(f"{p['host']}:{p['port']}", p["id"])
        l.addWidget(QLabel("Proxy:")); l.addWidget(self.pc)
        self.ne = QTextEdit(); self.ne.setPlaceholderText("Notes..."); self.ne.setMaximumHeight(80)
        l.addWidget(QLabel("Notes:")); l.addWidget(self.ne)
        b = QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        b.accepted.connect(self.accept); b.rejected.connect(self.reject)
        l.addWidget(b)
        if account: self._populate()

    def _populate(self):
        self.ee.setText(self.acc.get("email",""))
        self.pe.setText(self.acc.get("password",""))
        self.pie.setText(self.acc.get("pin",""))
        self.te.setText(self.acc.get("totp",""))
        self.ue.setText(self.acc.get("username",""))
        self.de.setText(self.acc.get("display_name",""))
        self.cc.setCurrentText(self.acc.get("category","Uncategorized"))
        self.sc.setCurrentText(self.acc.get("status","Offline"))
        self.ne.setPlainText(self.acc.get("notes",""))
        pid = self.acc.get("proxy_id")
        if pid:
            for i in range(self.pc.count()):
                if self.pc.itemData(i) == pid:
                    self.pc.setCurrentIndex(i)
                    break

    def get_data(self):
        return {"email":self.ee.text().strip(),"password":self.pe.text().strip(),
                "pin":self.pie.text().strip(),"totp":self.te.text().strip(),
                "username":self.ue.text().strip(),"display_name":self.de.text().strip(),
                "jagex_account":1,
                "category":self.cc.currentText(),"status":self.sc.currentText(),
                "notes":self.ne.toPlainText().strip(),
                "proxy_id":self.pc.currentData()}

class BulkDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bulk Import")
        self.setMinimumSize(500,400)
        l = QVBoxLayout(self)
        l.addWidget(QLabel("Paste accounts (one per line):"))
        l.addWidget(QLabel("Formats: email:pass | email:pass:totp | email:pass:pin:totp"))
        self.te = QTextEdit()
        self.te.setPlaceholderText("a@gmail.com:pass123\nb@gmail.com:pass456:totp\n...")
        l.addWidget(self.te)
        self.cc = QComboBox(); self.cc.addItems(CATEGORIES[1:])
        l.addWidget(QLabel("Default Category:")); l.addWidget(self.cc)
        b = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        b.accepted.connect(self.accept); b.rejected.connect(self.reject)
        l.addWidget(b)

    def get_accounts(self):
        lines = [l.strip() for l in self.te.toPlainText().strip().splitlines() if l.strip()]
        dc = self.cc.currentText()
        accs = []
        for ln in lines:
            p = ln.split(":")
            if len(p) < 2: continue
            a = {"email":p[0].strip(),"password":p[1].strip(),"pin":"","totp":"","username":"",
                 "jagex_account":1,"category":dc,"status":"Offline",
                 "notes":f"Imported {datetime.now().strftime('%Y-%m-%d %H:%M')}"}
            if len(p) == 3: a["totp"] = p[2].strip()
            elif len(p) >= 4: a["pin"] = p[2].strip(); a["totp"] = p[3].strip()
            accs.append(a)
        return accs

class LaunchDialog(QDialog):
    def __init__(self, parent, count, script_list=None):
        super().__init__(parent)
        self.setWindowTitle(f"Launch {count} Account(s)")
        self.setMinimumWidth(350)
        l = QVBoxLayout(self)
        l.addWidget(QLabel("Script:"))
        self.sc = QComboBox(); self.sc.setEditable(True)
        scripts = script_list if script_list else ["Bibs Tut","Bibs Slayer"]
        self.sc.addItems(scripts)
        l.addWidget(self.sc)
        l.addWidget(QLabel("World:"))
        self.ws = QSpinBox(); self.ws.setRange(301,570); self.ws.setValue(420)
        l.addWidget(self.ws)
        b = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        b.accepted.connect(self.accept); b.rejected.connect(self.reject)
        l.addWidget(b)

    def get_params(self):
        return self.sc.currentText(), self.ws.value()

class ProfileDialog(QDialog):
    def __init__(self, parent, profile=None):
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle("Edit Profile" if profile else "New Profile")
        self.setMinimumWidth(600)
        self.setMaximumWidth(800)
        l = QVBoxLayout(self)
        
        # Basic info
        basic_group = QGroupBox("Profile Information")
        basic_layout = QFormLayout(basic_group)
        self.name_edit = QLineEdit()
        self.desc_edit = QLineEdit()
        basic_layout.addRow("Name:", self.name_edit)
        basic_layout.addRow("Description:", self.desc_edit)
        
        # Queue mode selection
        mode_layout = QHBoxLayout()
        self.parallel_radio = QRadioButton("Parallel (all accounts at once)")
        self.sequential_radio = QRadioButton("Sequential (one at a time)")
        self.batch_radio = QRadioButton("Batch (parallel groups, staggered)")
        self.parallel_radio.setChecked(True)
        mode_layout.addWidget(self.parallel_radio)
        mode_layout.addWidget(self.sequential_radio)
        mode_layout.addWidget(self.batch_radio)
        basic_layout.addRow("Queue Mode:", mode_layout)

        # Batch settings (enabled when batch mode selected)
        batch_settings = QHBoxLayout()
        self.batch_size_spin = QSpinBox()
        self.batch_size_spin.setRange(0, 50)
        self.batch_size_spin.setValue(0)
        self.batch_size_spin.setToolTip("0 = no batching (all parallel). 5 = launch 5 accounts at a time.")
        self.batch_size_spin.setSuffix(" per batch")
        batch_settings.addWidget(QLabel("Batch Size:"))
        batch_settings.addWidget(self.batch_size_spin)

        self.stagger_spin = QSpinBox()
        self.stagger_spin.setRange(1, 30)
        self.stagger_spin.setValue(5)
        self.stagger_spin.setSuffix(" sec")
        self.stagger_spin.setToolTip("Delay between each account launch within a batch")
        batch_settings.addWidget(QLabel("Stagger:"))
        batch_settings.addWidget(self.stagger_spin)
        batch_settings.addStretch()
        basic_layout.addRow("Batch Settings:", batch_settings)

        # Update batch UI when mode changes
        self.parallel_radio.toggled.connect(lambda: self._update_batch_ui())
        self.sequential_radio.toggled.connect(lambda: self._update_batch_ui())
        self.batch_radio.toggled.connect(lambda: self._update_batch_ui())

        l.addWidget(basic_group)
        
        # Account selection
        account_group = QGroupBox("Account Selection")
        account_layout = QVBoxLayout(account_group)
        
        # Selection method
        method_layout = QHBoxLayout()
        self.use_filters_cb = QCheckBox("Use Filters (Manual Selection Disabled)")
        self.use_filters_cb.toggled.connect(self._toggle_selection_method)
        method_layout.addWidget(self.use_filters_cb)
        account_layout.addLayout(method_layout)
        
        # Filters
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Category:"))
        self.category_filter = QComboBox()
        self.category_filter.addItem("")
        self.category_filter.addItems(CATEGORIES[1:])
        self.category_filter.currentTextChanged.connect(self._load_accounts_by_filters)
        filter_layout.addWidget(self.category_filter)
        
        filter_layout.addWidget(QLabel("Status:"))
        self.status_filter = QComboBox()
        self.status_filter.addItem("")
        self.status_filter.addItems(STATUSES)
        self.status_filter.currentTextChanged.connect(self._load_accounts_by_filters)
        filter_layout.addWidget(self.status_filter)
        
        self.search_filter = QLineEdit()
        self.search_filter.setPlaceholderText("Search...")
        self.search_filter.textChanged.connect(self._load_accounts_by_filters)
        filter_layout.addWidget(QLabel("Search:"))
        filter_layout.addWidget(self.search_filter)
        
        account_layout.addLayout(filter_layout)
        
        # Account list
        self.account_list = QListWidget()
        self.account_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.account_list.setMaximumHeight(150)
        account_layout.addWidget(self.account_list)
        
        # Select all button
        self.select_all_btn = QPushButton("Select All Shown")
        self.select_all_btn.clicked.connect(self._select_all_accounts)
        account_layout.addWidget(self.select_all_btn)
        
        l.addWidget(account_group)
        
        # Script configuration
        script_group = QGroupBox("Script Configuration")
        script_layout = QVBoxLayout(script_group)
        
        self.script_list = QListWidget()
        self.script_list.setMaximumHeight(120)
        script_layout.addWidget(self.script_list)
        
        script_btn_layout = QHBoxLayout()
        self.add_script_btn = QPushButton("Add Script")
        self.add_script_btn.clicked.connect(self._add_script)
        self.remove_script_btn = QPushButton("Remove Script")
        self.remove_script_btn.clicked.connect(self._remove_script)
        script_btn_layout.addWidget(self.add_script_btn)
        script_btn_layout.addWidget(self.remove_script_btn)
        script_layout.addLayout(script_btn_layout)
        
        l.addWidget(script_group)
        
        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        l.addWidget(buttons)
        
        # Load data if editing
        if profile:
            self._load_profile_data()
        else:
            self._load_accounts()
        
        self._toggle_selection_method(self.use_filters_cb.isChecked())
    
    def _toggle_selection_method(self, use_filters):
        self.account_list.setEnabled(True)  # Always allow account selection
        self.select_all_btn.setEnabled(True)  # Always allow select all
        self.category_filter.setEnabled(use_filters)
        self.status_filter.setEnabled(use_filters)
        self.search_filter.setEnabled(use_filters)
        
        if use_filters:
            self._load_accounts_by_filters()
        else:
            self._load_accounts()
    
    def _load_accounts(self):
        self.account_list.clear()
        accounts = self.parent().db.list_accounts()
        for acc in accounts:
            # Show email without @domain part
            email_name = acc['email'].split('@')[0]
            display_name = acc.get('display_name', '')
            if display_name:
                display_text = f"{email_name} ({display_name}) [{acc.get('category', 'N/A')}]"
            else:
                display_text = f"{email_name} [{acc.get('category', 'N/A')}]"
            
            item = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, acc['id'])
            self.account_list.addItem(item)
    
    def _load_accounts_by_filters(self):
        self.account_list.clear()
        category = self.category_filter.currentText()
        status = self.status_filter.currentText()
        search = self.search_filter.text()
        
        accounts = self.parent().db.list_accounts(
            category=None if category == "" else category,
            status=None if status == "" else status,
            search=search
        )
        
        for acc in accounts:
            # Show email without @domain part
            email_name = acc['email'].split('@')[0]
            display_name = acc.get('display_name', '')
            if display_name:
                display_text = f"{email_name} ({display_name}) [{acc.get('category', 'N/A')}]"
            else:
                display_text = f"{email_name} [{acc.get('category', 'N/A')}]"
            
            item = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, acc['id'])
            self.account_list.addItem(item)
    
    def _select_all_accounts(self):
        for i in range(self.account_list.count()):
            item = self.account_list.item(i)
            item.setSelected(True)
    
    def _add_script(self):
        dialog = ScriptConfigDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            script_data = dialog.get_data()
            item = QListWidgetItem(f"{script_data['name']} - {script_data['stop_condition']}: {script_data['stop_value']}")
            item.setData(Qt.ItemDataRole.UserRole, script_data)
            self.script_list.addItem(item)
    
    def _remove_script(self):
        for item in self.script_list.selectedItems():
            self.script_list.takeItem(self.script_list.row(item))
    
    def _update_batch_ui(self):
        """Enable/disable batch settings based on selected mode."""
        is_batch = self.batch_radio.isChecked()
        self.batch_size_spin.setEnabled(is_batch)
        self.stagger_spin.setEnabled(is_batch)
        if is_batch and self.batch_size_spin.value() == 0:
            self.batch_size_spin.setValue(5)

    def _load_profile_data(self):
        self.name_edit.setText(self.profile['name'])
        self.desc_edit.setText(self.profile.get('description', ''))

        # Load queue mode
        queue_mode = self.profile.get('queue_mode', 'parallel')
        batch_size = self.profile.get('batch_size', 0) or 0
        if queue_mode == 'sequential':
            self.sequential_radio.setChecked(True)
        elif batch_size > 0:
            self.batch_radio.setChecked(True)
            self.batch_size_spin.setValue(batch_size)
        else:
            self.parallel_radio.setChecked(True)

        # Load stagger delay
        self.stagger_spin.setValue(self.profile.get('stagger_delay', 5) or 5)
        self._update_batch_ui()

        # Load filters
        self.category_filter.setCurrentText(self.profile.get('category_filter', ''))
        self.status_filter.setCurrentText(self.profile.get('status_filter', ''))
        self.search_filter.setText(self.profile.get('search_filter', ''))

        # Load accounts
        profile_accounts = self.parent().db.get_profile_accounts(self.profile['id'])
        for acc in profile_accounts:
            # Show email without @domain part
            email_name = acc['email'].split('@')[0]
            display_name = acc.get('display_name', '')
            if display_name:
                display_text = f"{email_name} ({display_name}) [{acc.get('category', 'N/A')}]"
            else:
                display_text = f"{email_name} [{acc.get('category', 'N/A')}]"

            item = QListWidgetItem(display_text)
            item.setData(Qt.ItemDataRole.UserRole, acc['id'])
            item.setSelected(True)
            self.account_list.addItem(item)

        # Load scripts
        scripts = self.parent().db.get_profile_scripts(self.profile['id'])
        for script in scripts:
            script_data = {
                'name': script['script_name'],
                'world': script['world'],
                'stop_condition': script['stop_condition'],
                'stop_value': script['stop_value'],
                'repeat': script['repeat']
            }
            item = QListWidgetItem(f"{script_data['name']} - {script_data['stop_condition']}: {script_data['stop_value']}")
            item.setData(Qt.ItemDataRole.UserRole, script_data)
            self.script_list.addItem(item)

    def get_data(self):
        account_ids = []
        if self.use_filters_cb.isChecked():
            # Use filter-based accounts
            category = self.category_filter.currentText()
            status = self.status_filter.currentText()
            search = self.search_filter.text()
            accounts = self.parent().db.list_accounts(
                category=None if category == "" else category,
                status=None if status == "" else status,
                search=search
            )
            account_ids = [acc['id'] for acc in accounts]
        else:
            # Use manually selected accounts
            for item in self.account_list.selectedItems():
                account_ids.append(item.data(Qt.ItemDataRole.UserRole))

        scripts = []
        for i in range(self.script_list.count()):
            item = self.script_list.item(i)
            scripts.append(item.data(Qt.ItemDataRole.UserRole))

        # Determine mode and batch size
        if self.sequential_radio.isChecked():
            queue_mode = 'sequential'
            batch_size = 0
        elif self.batch_radio.isChecked():
            queue_mode = 'parallel'
            batch_size = self.batch_size_spin.value()
        else:
            queue_mode = 'parallel'
            batch_size = 0

        return {
            'name': self.name_edit.text(),
            'description': self.desc_edit.text(),
            'queue_mode': queue_mode,
            'batch_size': batch_size,
            'stagger_delay': self.stagger_spin.value(),
            'category_filter': self.category_filter.currentText(),
            'status_filter': self.status_filter.currentText(),
            'search_filter': self.search_filter.text(),
            'account_ids': account_ids,
            'scripts': scripts
        }

class ScriptConfigDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Add Script")
        self.setMinimumWidth(400)
        l = QVBoxLayout(self)
        
        form = QFormLayout()
        self.script_name = QComboBox()
        self.script_name.setEditable(True)
        self.script_name.addItems(["Bibs Tut", "Bibs Slayer", "Bibs Fishing", "Bibs Woodcutting", "Bibs Combat"])
        form.addRow("Script:", self.script_name)
        
        self.world_spin = QSpinBox()
        self.world_spin.setRange(301, 570)
        self.world_spin.setValue(420)
        form.addRow("World:", self.world_spin)
        
        self.stop_condition = QComboBox()
        self.stop_condition.addItems(["Process Exit", "Target Level", "Runtime", "Items Collected", "Script End"])
        form.addRow("Stop Condition:", self.stop_condition)
        
        self.stop_value = QLineEdit()
        self.stop_value.setPlaceholderText("e.g., 30 for level, 60 for minutes")
        form.addRow("Stop Value:", self.stop_value)
        
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 100)
        self.repeat_spin.setValue(1)
        form.addRow("Repeat:", self.repeat_spin)
        
        l.addLayout(form)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        l.addWidget(buttons)
    
    def get_data(self):
        return {
            'name': self.script_name.currentText(),
            'world': self.world_spin.value(),
            'stop_condition': self.stop_condition.currentText(),
            'stop_value': self.stop_value.text(),
            'repeat': self.repeat_spin.value()
        }

class QueueModeDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Queue Mode")
        self.setMinimumWidth(300)
        l = QVBoxLayout(self)
        
        l.addWidget(QLabel("Select queue execution mode:"))
        
        self.parallel_radio = QRadioButton("Parallel (all accounts at once)")
        self.sequential_radio = QRadioButton("Sequential (one at a time)")
        self.parallel_radio.setChecked(True)
        
        l.addWidget(self.parallel_radio)
        l.addWidget(self.sequential_radio)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        l.addWidget(buttons)
    
    def get_mode(self):
        return "parallel" if self.parallel_radio.isChecked() else "sequential"

def main():
    try:
        app = QApplication(sys.argv)
        app.setApplicationName(APP_NAME)
        w = MainWindow()
        w.show()
        app.exec()
    except Exception:
        import traceback
        logging.critical("UNHANDLED CRASH\n%s", traceback.format_exc())
        raise

if __name__ == "__main__":
    main()
