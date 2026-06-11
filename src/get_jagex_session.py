import argparse
import json
import os
import secrets
import time
import base64
import hashlib
import urllib.parse
import pathlib
import sys
import traceback
import logging
import random
import requests
import socket
import threading
import select
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    pass

try:
    import pyautogui
except ImportError:
    pyautogui = None

# === Local proxy server (handles Chrome + authenticated upstream proxies) ===
class LocalProxyServer:
    """Minimal local HTTP proxy that forwards to an upstream authenticated proxy."""

    def __init__(self, proxy_host, proxy_port, proxy_user="", proxy_pass=""):
        self.upstream = (proxy_host, proxy_port)
        self.auth = None
        if proxy_user and proxy_pass:
            creds = base64.b64encode(f"{proxy_user}:{proxy_pass}".encode()).decode()
            self.auth = f"Proxy-Authorization: Basic {creds}\r\n"
        self._sock = None
        self.port = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._conns = set()

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(5)
        self._sock.settimeout(1.0)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[LOCAL PROXY] Started on 127.0.0.1:{self.port} -> {self.upstream[0]}:{self.upstream[1]}")

    def stop(self):
        self._stop.set()
        with self._lock:
            for c in list(self._conns):
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self):
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        with self._lock:
            self._conns.add(client)
        try:
            # Do NOT set a read timeout — browser connections can be idle for a while
            data = client.recv(65536)
            if not data:
                return
            if data.startswith(b"CONNECT"):
                target = data.split()[1].decode()
                self._tunnel_connect(client, target)
            else:
                self._forward_http(client, data)
        except Exception as e:
            print(f"[LOCAL PROXY] Error: {e}")
        finally:
            with self._lock:
                self._conns.discard(client)
            try:
                client.close()
            except Exception:
                pass

    def _tunnel_connect(self, client, target):
        upstream = socket.create_connection(self.upstream, timeout=30)
        try:
            req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
            if self.auth:
                req += self.auth
            req += "\r\n"
            upstream.sendall(req.encode())
            resp = self._recv_until(upstream, b"\r\n\r\n")
            if not resp or b"200" not in resp.split(b"\r\n")[0]:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._pipe(client, upstream)
        finally:
            upstream.close()

    def _forward_http(self, client, initial_data):
        upstream = socket.create_connection(self.upstream, timeout=30)
        try:
            if self.auth:
                initial_data = initial_data.replace(
                    b"\r\n\r\n", b"\r\n" + self.auth.encode() + b"\r\n", 1
                )
            upstream.sendall(initial_data)
            self._pipe(client, upstream)
        finally:
            upstream.close()

    def _recv_until(self, sock, marker):
        buf = b""
        while marker not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return buf
            buf += chunk
        return buf

    def _pipe(self, a, b):
        sockets = [a, b]
        while not self._stop.is_set():
            readable, _, _ = select.select(sockets, [], [], 1)
            if not readable:
                continue
            for src in readable:
                dst = b if src is a else a
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)

# Make database import work regardless of cwd
_project_root = pathlib.Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from database import FarmDB

# === Logging ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("jagex-session")

# === PKCE config ===
AUTH_BASE = "https://account.jagex.com/oauth2/auth"
CLIENT_ID = "com_jagex_auth_desktop_launcher"
REDIRECT_URI = "https://secure.runescape.com/m=weblogin/launcher-redirect"
SCOPES = "openid offline gamesso.token.create user.profile.read"
PKCE_DIR = pathlib.Path.home() / "DreamBot" / "BotData"
PKCE_DIR.mkdir(parents=True, exist_ok=True)

# === PKCE helpers ===
def _pkce_file(account_db_id: int):
    return PKCE_DIR / f"pkce_run_{account_db_id}.json"

def pkce_pair():
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge

def generate_pkce_login_url(account_db_id: int):
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    params = {
        "auth_method": "",
        "login_type": "",
        "flow": "launcher",
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
    }
    login_url = AUTH_BASE + "?" + urllib.parse.urlencode(params)
    payload = {
        "created_at": int(time.time()),
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_verifier": verifier,
        "code_challenge": challenge,
        "state": state,
        "nonce": nonce,
        "login_url": login_url,
    }
    out_file = _pkce_file(account_db_id)
    out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return login_url

def extract_code_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    # Jagex may put the code in the query OR the fragment
    for source in (parsed.query, parsed.fragment):
        if source:
            params = urllib.parse.parse_qs(source)
            code = params.get("code", [None])[0]
            if code:
                return code
    raise ValueError("No 'code=' found in URL")

def exchange_code_for_token(code: str, account_db_id: int):
    out_file = _pkce_file(account_db_id)
    if not out_file.exists():
        raise FileNotFoundError(f"{out_file} not found")
    data = json.loads(out_file.read_text(encoding="utf-8"))
    payload = {
        "grant_type": "authorization_code",
        "client_id": data["client_id"],
        "redirect_uri": data["redirect_uri"],
        "code": code,
        "code_verifier": data["code_verifier"]
    }
    logger.info("[TOKEN] Exchanging code for token...")
    import requests
    response = requests.post("https://account.jagex.com/oauth2/token", data=payload)
    if response.status_code != 200:
        logger.error("[TOKEN] Failed: %s", response.text)
        raise Exception("Token exchange failed")
    token_data = response.json()
    logger.info("[TOKEN] Access token received.")
    return token_data

# === Browser (undetected_chromedriver) ===
import re

def _get_chrome_version():
    """Detect installed Chrome version for uc driver matching."""
    import subprocess
    for cmd in [
        r'reg query "HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon" /v version',
        r'reg query "HKEY_LOCAL_MACHINE\SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome" /v version',
        r'reg query "HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome" /v version',
    ]:
        try:
            out = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode()
            m = re.search(r'version\s+REG_SZ\s+(\d+)', out)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return None

def start_browser(show_browser=False, proxy=None):
    """Initialize Chrome via undetected-chromedriver (bypasses bot detection).
    Returns (driver, local_proxy) where local_proxy is a LocalProxyServer if one was started.
    """
    print("[BROWSER] Starting undetected Chrome...")
    try:
        import undetected_chromedriver as uc
    except ImportError:
        logger.error("Missing package: pip install undetected-chromedriver")
        raise

    version_main = _get_chrome_version()
    if version_main:
        print(f"[BROWSER] Detected Chrome v{version_main}, matching driver...")
    else:
        print("[BROWSER] Could not detect Chrome version, letting uc auto-detect...")

    options = uc.ChromeOptions()
    options.add_argument("--window-size=1280,900")
    # NOTE: Do NOT add --no-sandbox or --disable-dev-shm-usage — those trigger bot detection

    local_proxy = None
    if proxy:
        proto = proxy.get("protocol", "http")
        host = proxy.get("host", "")
        port = proxy.get("port", "")
        user = proxy.get("username", "")
        pwd = proxy.get("password", "")

        if user and pwd:
            # Chrome can't handle authenticated proxies directly.
            # Start a local proxy that forwards to upstream with proper auth headers.
            local_proxy = LocalProxyServer(
                proxy_host=host,
                proxy_port=port,
                proxy_user=user,
                proxy_pass=pwd,
            )
            local_proxy.start()
            time.sleep(0.3)  # give proxy a moment to start listening
            options.add_argument(f"--proxy-server=http://127.0.0.1:{local_proxy.port}")
            print(f"[BROWSER] Using local proxy -> {host}:{port}")
        else:
            proxy_str = f"{proto}://{host}:{port}"
            options.add_argument(f"--proxy-server={proxy_str}")
            print(f"[BROWSER] Using proxy: {host}:{port}")

    try:
        kwargs = {"options": options}
        if version_main:
            kwargs["version_main"] = version_main
        driver = uc.Chrome(**kwargs)
        driver.set_page_load_timeout(60)
        def _place_in_corner_and_back():
            """Resize to small corner window, keeping focus for CF."""
            import ctypes
            import ctypes.wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            SWP_NOZORDER = 0x0004

            # Get screen size
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)

            # Get our browser PID so we only manipulate our own window
            browser_pid = driver.service.process.pid if driver.service and driver.service.process else None
            if not browser_pid:
                return False

            GetWindowThreadProcessId = user32.GetWindowThreadProcessId
            GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.c_ulong)]
            GetWindowThreadProcessId.restype = ctypes.c_ulong

            # Wait up to 5s for our Chrome window to appear
            target_hwnd = None
            for _ in range(50):
                EnumWindowsProc = ctypes.WINFUNCTYPE(
                    ctypes.c_bool, ctypes.wintypes.HWND, ctypes.py_object
                )
                def cb(hwnd, _):
                    nonlocal target_hwnd
                    if target_hwnd:
                        return True
                    if user32.IsWindowVisible(hwnd):
                        cn = ctypes.create_unicode_buffer(256)
                        user32.GetClassNameW(hwnd, cn, 256)
                        if "Chrome_WidgetWin" in cn.value:
                            pid = ctypes.c_ulong(0)
                            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                            if pid.value == browser_pid:
                                target_hwnd = hwnd
                                return False  # stop enumerating
                    return True
                user32.EnumWindows(EnumWindowsProc(cb), None)
                if target_hwnd:
                    break
                time.sleep(0.1)

            if not target_hwnd:
                return False

            # Move to bottom-right, keep Z-order so CF focus is preserved
            w, h = 400, 300
            x, y = sw - w - 10, sh - h - 40
            user32.SetWindowPos(
                target_hwnd, 0, x, y, w, h,
                SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOZORDER
            )
            return True

        if not show_browser:
            try:
                if _place_in_corner_and_back():
                    print("[BROWSER] Chrome started (small corner, behind other windows)")
                else:
                    print("[BROWSER] Chrome started (window not found, leaving visible)")
            except Exception as e:
                print(f"[BROWSER] Chrome started (corner/back failed: {e})")
        else:
            print("[BROWSER] Chrome started (visible)")
        return driver, local_proxy
    except Exception as e:
        logger.error(f"Failed to initialize browser: {e}")
        raise

# === Cloudflare click ===
def click_cloudflare_button(image_path="Cloudflarebutton.png", timeout=8, confidence=0.6):
    if pyautogui is None:
        logger.warning("pyautogui not installed, skipping Cloudflare click")
        return False

    if not os.path.isfile(image_path):
        candidates = [
            image_path,
            os.path.join(os.path.dirname(__file__), image_path),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), image_path),
        ]
        found = next((p for p in candidates if os.path.isfile(p)), None)
        if found:
            image_path = found
        else:
            print(f"[CF] WARNING: {image_path} not found")
            return False

    pyautogui.FAILSAFE = True
    start = time.time()
    print(f"[CF] Scanning for CF button ({image_path})...")
    while time.time() - start < timeout:
        try:
            loc = pyautogui.locateOnScreen(image_path, confidence=confidence)
            if loc:
                x, y = pyautogui.center(loc)
                x += random.randint(-3, 3)
                y += random.randint(-3, 3)
                pyautogui.moveTo(x, y, duration=random.uniform(0.4, 0.8), tween=pyautogui.easeInOutQuad)
                time.sleep(random.uniform(0.2, 0.5))
                pyautogui.click()
                print("[CF] Button clicked")
                return True
        except Exception:
            pass
        time.sleep(0.5)
    print("[CF] No button found")
    return False

# === Login flow (Selenium API) ===
def _is_cloudflare_blocking(driver):
    """Check if page is still showing a Cloudflare challenge."""
    url = driver.current_url.lower()
    src = driver.page_source.lower()
    return (
        "challenges.cloudflare.com" in url
        or "cf-challenge" in src
        or "verify you are human" in src
        or "are you a robot?" in src
    )

def go_to_login(driver, login_url):
    """Navigate to login, handle CF quickly. Returns False if CF still blocking (banned)."""
    logger.info("[NAVIGATE] Login page")
    print(f"[NAVIGATE] URL: {login_url[:120]}...")

    # Retry navigation up to 3 times (uc can timeout on first attempt)
    for attempt in range(1, 4):
        try:
            driver.get(login_url)
            print(f"[NAVIGATE] Page loaded (attempt {attempt})")
            break
        except Exception as e:
            print(f"[NAVIGATE] Attempt {attempt} failed: {e}")
            if attempt == 3:
                raise
            time.sleep(2)

    # Wait a moment for page to start loading before checking state
    time.sleep(1)

    # Give undetected-chromedriver time to auto-solve CF (up to 10s)
    print("[CF] Checking for challenge...")
    for i in range(20):
        time.sleep(0.5)
        if not _is_cloudflare_blocking(driver):
            print("[CF] Challenge cleared automatically")
            break
    else:
        # Still blocked — try pyautogui click (give it 8s to find the button)
        print("[CF] Still blocking, trying pyautogui click...")
        click_cloudflare_button(timeout=8)
        # After click, wait up to 10s for CF to clear
        print("[CF] Waiting for challenge to clear after click...")
        for _ in range(20):
            time.sleep(0.5)
            if not _is_cloudflare_blocking(driver):
                print("[CF] Challenge cleared after click")
                break
        else:
            print("[CF] Still blocked after click — account likely banned")
            return False

    # Normal account flow — dismiss cookie banner if present
    try:
        wait = WebDriverWait(driver, 5)
        cookie_btn = wait.until(EC.element_to_be_clickable((By.ID, "CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll")))
        cookie_btn.click()
        logger.info("[COOKIE] Banner dismissed")
    except Exception:
        pass
    return True

def fill_email(driver, email):
    wait = WebDriverWait(driver, 20)
    email_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type=email]")))
    email_input.click()
    email_input.clear()
    email_input.send_keys(email)
    continue_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type=submit]")))
    continue_btn.click()
    logger.info("[EMAIL] Filled")

def fill_password(driver, password):
    wait = WebDriverWait(driver, 20)
    pwd_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type=password]")))
    pwd_input.click()
    pwd_input.clear()
    pwd_input.send_keys(password)
    continue_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type=submit]")))
    continue_btn.click()
    logger.info("[PASSWORD] Filled")

def fill_otp(driver, otp_secret):
    if not otp_secret:
        return
    try:
        import pyotp
    except ImportError:
        logger.error("pyotp not installed, cannot fill OTP")
        raise
    
    # Wait for OTP page to load (no hardcoded sleep)
    wait = WebDriverWait(driver, 20)
    
    # Try to click "Continue with TOTP" button if present
    try:
        otp_input_first = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='continue-with-totp-mfa']")))
        otp_input_first.click()
        time.sleep(1)  # Brief pause for page transition
    except Exception:
        # If the button doesn't exist, we're probably already on the OTP input page
        pass
    
    # Generate and submit OTP code
    code = pyotp.TOTP(otp_secret).now()
    print(f"[OTP] Generated code: {code}")
    
    # Wait for OTP input field and fill it
    otp_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type=text], input[name=code]")))
    otp_input.click()
    otp_input.clear()
    otp_input.send_keys(code)
    
    # Submit the OTP
    submit_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type=submit]")))
    submit_btn.click()
    logger.info(f"[OTP] Submitted: {code}")

# === Consent + Game Session ===
def get_game_session(driver, original_id_token: str):
    # Step 1: Build consent URL with a different client_id
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    params = {
        "client_id": "1fddee4e-b100-4f4e-b2b0-097f9088f9d2",
        "redirect_uri": "http://localhost",
        "scope": "openid offline",
        "state": state,
        "nonce": nonce,
        "prompt": "consent",
        "response_type": "id_token code",
        "response_mode": "fragment",
        "id_token_hint": original_id_token
    }
    consent_url = AUTH_BASE + "?" + urllib.parse.urlencode(params)
    logger.info(f"[CONSENT] Navigating to consent URL...")

    # Step 2: Navigate to consent URL with browser
    try:
        driver.get(consent_url)
    except Exception as e:
        logger.warning(f"[CONSENT] Navigation error (expected if localhost not listening): {e}")

    # Step 3: Wait for redirect to localhost and extract new id_token from fragment
    start_wait = time.time()
    new_id_token = None
    while time.time() - start_wait < 30:
        url = driver.current_url
        if "localhost" in url:
            parsed = urllib.parse.urlparse(url)
            fragment = parsed.fragment
            if fragment:
                qs = urllib.parse.parse_qs(fragment)
                new_id_token = qs.get("id_token", [None])[0]
                if new_id_token:
                    logger.info("[CONSENT] Got new id_token from fragment")
                    break
            # Also try full URL query params (some browsers may put it there)
            qs = urllib.parse.parse_qs(parsed.query)
            new_id_token = qs.get("id_token", [None])[0]
            if new_id_token:
                break
        time.sleep(0.5)

    if not new_id_token:
        # Fallback: try reading from page JS
        try:
            fragment = driver.execute_script("return location.hash.substring(1)")
            if fragment:
                qs = urllib.parse.parse_qs(fragment)
                new_id_token = qs.get("id_token", [None])[0]
        except Exception:
            pass

    if not new_id_token:
        raise Exception("Failed to get new id_token from consent flow")

    # Step 4: Use new id_token for game session API
    logger.info("[GAME-SESSION] Creating game session with consent id_token...")
    resp = requests.post(
        "https://auth.jagex.com/game-session/v1/sessions",
        headers={"content-type": "application/json", "accept": "application/json"},
        json={"idToken": new_id_token},
        timeout=30
    )
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"raw": resp.text}
    logger.info(f"[GAME-SESSION] Status: {resp.status_code}")
    if resp.status_code == 200 and "sessionId" in data:
        session_id = data["sessionId"]
        logger.info(f"[SESSION_ID] {session_id}")
        return session_id
    else:
        logger.error(f"[GAME-SESSION] Failed: {json.dumps(data, indent=2)}")
        raise Exception("Failed to get game session")

def get_accounts(session_id: str):
    import requests
    headers = {
        "Authorization": f"Bearer {session_id}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    resp = requests.get("https://auth.jagex.com/game-session/v1/accounts", headers=headers, timeout=30)
    data = resp.json()
    if isinstance(data, list) and len(data) > 0:
        data = data[0]
    account_id = data.get("accountId")
    display_name = data.get("displayName")
    logger.info(f"[ACCOUNT] ID={account_id}, Name={display_name}")
    return account_id, display_name

# === Main ===
def main(account_db_id: int, show_browser=False, proxy=None):
    db = FarmDB()
    acc = db.get_account(account_db_id)
    if not acc:
        raise Exception(f"Account {account_db_id} not found in DB")

    email = acc["email"]
    password = acc["password"]
    totp = acc.get("totp", "")

    print(f"\n[SESSION] ===== Starting session grab for {email} =====")
    if proxy:
        print(f"[SESSION] Using proxy: {proxy.get('host')}:{proxy.get('port')}")
    logger.info(f"[START] Getting session for {email}")
    print("[SESSION] Generating PKCE login URL...")
    login_url = generate_pkce_login_url(account_db_id)
    print(f"[SESSION] Login URL generated")
    print("[SESSION] Starting browser...")
    driver, local_proxy = start_browser(show_browser=show_browser, proxy=proxy)

    try:
        print("[SESSION] Navigating to login page...")
        cf_ok = go_to_login(driver, login_url)
        if not cf_ok:
            print("[SESSION] Account appears banned (Cloudflare blocking)")
            db.update_account(account_db_id, category="Banned")
            print("[SESSION] Marked as Banned, skipping...")
            raise Exception("Account banned - Cloudflare challenge")

        print("[SESSION] Filling email...")
        fill_email(driver, email)
        print("[SESSION] Filling password...")
        fill_password(driver, password)
        if totp:
            print("[SESSION] Filling OTP...")
            fill_otp(driver, totp)
            print("[SESSION] OTP submitted")
        else:
            print("[SESSION] No TOTP configured")

        logger.info("[WAIT] Waiting for redirect...")
        print("[SESSION] Waiting for Jagex redirect (up to 90s)...")
        start_wait = time.time()
        redirect_url = ""
        while time.time() - start_wait < 90:
            url = driver.current_url
            if "code=" in url or "id_token=" in url:
                redirect_url = url
                print(f"[SESSION] Redirect detected at t={int(time.time()-start_wait)}s")
                break
            if int(time.time() - start_wait) % 10 == 0:
                print(f"[SESSION] Still waiting for redirect... ({int(time.time()-start_wait)}s elapsed, current url: {url[:80]}...)")
            time.sleep(0.5)
        if not redirect_url:
            print("[SESSION] ERROR: Timeout waiting for redirect after 90s")
            raise Exception("Timeout waiting for redirect")
        logger.info(f"[SUCCESS] Redirect: {redirect_url}")
        print("[SESSION] Redirect received, extracting auth code...")

        code = extract_code_from_url(redirect_url)
        print("[SESSION] Exchanging code for token...")
        token_data = exchange_code_for_token(code, account_db_id)
        id_token = token_data.get("id_token")
        if not id_token:
            print("[SESSION] ERROR: No id_token in token response")
            raise Exception("No id_token received")
        print("[SESSION] Token received")

        print("[SESSION] Getting game session...")
        session_id = get_game_session(driver, id_token)
        print(f"[SESSION] Game session obtained: {session_id[:20]}...")
        print("[SESSION] Fetching account info...")
        account_id, display_name = get_accounts(session_id)
        print(f"[SESSION] Account: id={account_id}, name={display_name}")

        if session_id and account_id:
            # Retry DB write with jitter — multiple subprocesses may hit the DB at once
            import sqlite3, random
            ok = False
            for attempt in range(5):
                try:
                    db.update_account(
                        account_db_id,
                        session_id=session_id,
                        character_id=account_id,
                        username=display_name or acc.get("username", ""),
                        display_name=display_name or acc.get("display_name", "")
                    )
                    ok = True
                    break
                except sqlite3.OperationalError as dbe:
                    print(f"[DB] Write locked (attempt {attempt+1}/5): {dbe}")
                    time.sleep(random.uniform(0.2, 0.8))
            if not ok:
                raise Exception("Failed to write session to database after 5 retries")
            logger.info(f"[DB] Updated account {account_db_id} with session_id and character_id")
            print(f"\n=== SUCCESS ===")
            print(f"session_id  : {session_id}")
            print(f"character_id: {account_id}")
            print(f"display_name: {display_name}")
            sys.stdout.flush()
            return session_id, account_id
        else:
            print("[SESSION] ERROR: Missing session_id or account_id")
            raise Exception("Missing session_id or account_id")

    except Exception as e:
        print(f"[SESSION] ERROR in main flow: {e}")
        traceback.print_exc()
        raise

    finally:
        print("[SESSION] Closing browser...")
        try:
            # Use a thread with timeout to avoid quit() hanging indefinitely
            import threading
            _driver = driver
            _quit_err = [None]
            def _do_quit():
                try:
                    _driver.quit()
                except Exception as qe:
                    _quit_err[0] = qe
            t = threading.Thread(target=_do_quit, daemon=True)
            t.start()
            t.join(timeout=8)
            if t.is_alive():
                print("[SESSION] Browser quit() timed out, forcing close...")
                try:
                    _driver.close()
                except Exception:
                    pass
                try:
                    _driver.service.process.kill()
                except Exception:
                    pass
            else:
                print("[SESSION] Browser closed")
            if _quit_err[0]:
                logger.warning(f"[SHUTDOWN] {_quit_err[0]}")
        except Exception as e:
            logger.warning(f"[SHUTDOWN] {e}")
        if local_proxy:
            try:
                print("[SESSION] Stopping local proxy...")
                local_proxy.stop()
                print("[SESSION] Local proxy stopped")
            except Exception as e:
                logger.warning(f"[SHUTDOWN] Proxy stop error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--show-browser", action="store_true", help="Show browser window instead of off-screen")
    parser.add_argument("--proxy", type=str, default="", help="JSON string with proxy config")
    args = parser.parse_args()
    proxy = None
    if args.proxy:
        try:
            proxy = json.loads(args.proxy)
        except Exception:
            pass
    try:
        main(args.account_id, show_browser=args.show_browser, proxy=proxy)
    except Exception as e:
        logger.error(f"FAILED: {e}")
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
