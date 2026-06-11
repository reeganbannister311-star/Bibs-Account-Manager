import base64
import concurrent.futures
import os
import random
import re
import string
import select
import socket
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Optional

import pyotp
from imap_tools import AND, MailBox
from loguru import logger
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException

from . import models, utils



class ElementNotFoundError(Exception):
    """Raised when a required element cannot be found."""
    pass


class RegistrationError(Exception):
    """An error that occurred during account registration."""
    pass


class AccountCreatorSelenium:
    """Account creator using Selenium with Chrome in incognito mode"""

    _REGISTRATION_URL = "https://account.jagex.com/en-GB/login/registration-start"
    _MANAGEMENT_URL = "https://account.jagex.com/en-GB/manage/profile"

    _gmail_driver = None
    _gmail_lock = threading.Lock()

    def __init__(
        self,
        user_agent: str,
        element_wait_timeout: int,
        cache_update_threshold: float,
        enable_dev_tools: bool,
        account_email_domain: str,
        account_password: str,
        mail_provider: models.MailProvider,
        run_id: str | None = None,
        proxy: models.Proxy | None = None,
        set_2fa: bool = False,
        use_headless_browser: bool = False,
        imap_details: models.IMAPDetails | None = None,
        gmail_web_details: models.GmailWebDetails | None = None,
        use_proxy_for_temp_mail: bool = True,
    ) -> None:
        self.run_id = run_id or utils.generate_string(include_punctuation=False)
        self.logger = logger.bind(module="AccountCreatorSelenium", uid=self.run_id)

        self.user_agent = user_agent
        self.enable_dev_tools = enable_dev_tools
        self.use_headless_browser = use_headless_browser
        self.element_wait_timeout = element_wait_timeout

        self.proxy = proxy
        self.account_email_domain = account_email_domain
        self.account_password = account_password
        self.set_2fa = set_2fa

        self.mail_provider = mail_provider
        if self.mail_provider == models.MailProvider.IMAP:
            self.imap_details = imap_details
        elif self.mail_provider == models.MailProvider.GMAIL_WEB:
            self.gmail_web_details = gmail_web_details
        else:
            self.use_proxy_for_temp_mail = use_proxy_for_temp_mail
            self.wreq_client = utils.setup_wreq_client(
                user_agent=self.user_agent,
                timeout_seconds=self.element_wait_timeout,
            )
            if self.proxy:
                self.wreq_proxy = self.proxy.to_wreq()
            else:
                self.wreq_proxy = None

        # Initialize Selenium WebDriver (will be created per account for fresh incognito session)
        self.driver = None

    def _get_chrome_version(self) -> int | None:
        """Auto-detect installed Chrome major version on Windows"""
        import subprocess
        import re

        # Try registry first
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\Google\Chrome\BLBeacon") as key:
                        version, _ = winreg.QueryValueEx(key, "version")
                        major = int(version.split(".")[0])
                        self.logger.debug(f"Detected Chrome version from registry: {major}")
                        return major
                except Exception:
                    continue
        except Exception:
            pass

        # Try Chrome executable paths
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for path in chrome_paths:
            try:
                result = subprocess.run(
                    [path, "--version"],
                    capture_output=True, text=True, timeout=5, check=False
                )
                match = re.search(r"Chrome\s+(\d+)", result.stdout)
                if match:
                    major = int(match.group(1))
                    self.logger.debug(f"Detected Chrome version from executable: {major}")
                    return major
            except Exception:
                continue

        return None

    class _ProxyForwarder:
        """Tiny local HTTP proxy that forwards to an upstream authenticated proxy.
        Chrome doesn't support user:pass@ in --proxy-server, so we run a local
        proxy that Chrome connects to, and this forwarder handles the auth."""

        def __init__(self, proxy_ip: str, proxy_port: int, proxy_user: str | None, proxy_pass: str | None) -> None:
            self.proxy_ip = proxy_ip
            self.proxy_port = proxy_port
            self.proxy_user = proxy_user or ""
            self.proxy_pass = proxy_pass or ""
            self.local_port: int | None = None
            self._server: socket.socket | None = None
            self._running = False

        def start(self) -> int:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("127.0.0.1", 0))
            self._server.listen(5)
            self.local_port = self._server.getsockname()[1]
            self._running = True
            threading.Thread(target=self._run, daemon=True).start()
            return self.local_port

        def _run(self) -> None:
            while self._running:
                try:
                    client, _ = self._server.accept()
                    threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
                except OSError:
                    break

        def _handle_client(self, client: socket.socket) -> None:
            # Read CONNECT request from Chrome
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = client.recv(4096)
                if not chunk:
                    client.close()
                    return
                req += chunk

            # Parse target from first line: "CONNECT host:port HTTP/1.1"
            try:
                first_line = req.split(b"\r\n")[0].decode()
                target = first_line.split()[1]  # "host:port"
            except Exception:
                client.send(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                client.close()
                return

            # Connect to upstream proxy
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.settimeout(30)
            try:
                upstream.connect((self.proxy_ip, self.proxy_port))
            except Exception:
                client.send(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                client.close()
                return

            # Send CONNECT to upstream with Basic auth
            auth = base64.b64encode(f"{self.proxy_user}:{self.proxy_pass}".encode()).decode()
            connect = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
            upstream.send(connect.encode())

            # Read upstream response
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = upstream.recv(4096)
                if not chunk:
                    client.close()
                    upstream.close()
                    return
                resp += chunk

            if b"200" not in resp.split(b"\r\n")[0]:
                client.send(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                client.close()
                upstream.close()
                return

            # Tell Chrome tunnel is established
            client.send(b"HTTP/1.1 200 Connection Established\r\n\r\n")

            # Bidirectional tunnel
            try:
                while True:
                    readable, _, _ = select.select([client, upstream], [], [], 30)
                    if client in readable:
                        data = client.recv(8192)
                        if not data:
                            break
                        upstream.send(data)
                    if upstream in readable:
                        data = upstream.recv(8192)
                        if not data:
                            break
                        client.send(data)
            except Exception:
                pass
            finally:
                client.close()
                upstream.close()

        def stop(self) -> None:
            self._running = False
            if self._server:
                try:
                    self._server.close()
                except Exception:
                    pass

    def _init_driver(self):
        """Initialize undetected Chrome WebDriver with stealth patches"""
        options = uc.ChromeOptions()

        # NOTE: Do NOT use --incognito — uc stealth works best with normal profile.
        # Incognito changes fingerprints and makes Cloudflare MORE likely to challenge.
        # options.add_argument("--incognito")

        # NOTE: Do NOT set custom --user-agent — uc auto-generates a realistic one.
        # Mismatched UA is a detection signal.
        # options.add_argument(f"--user-agent={self.user_agent}")

        # Headless mode if requested (uc recommends against headless for CF)
        if self.use_headless_browser:
            options.add_argument("--headless=new")
        else:
            options.add_argument("--window-size=900,650")
            options.add_argument("--window-position=800,30")

        # Proxy settings if provided
        self._proxy_forwarder = None
        if self.proxy:
            if self.proxy.username and self.proxy.password:
                # Chrome doesn't support user:pass@ in --proxy-server
                # Start a tiny local proxy that forwards with auth
                self.logger.debug(f"Starting local proxy forwarder for {self.proxy.ip}:{self.proxy.port}")
                forwarder = self._ProxyForwarder(
                    self.proxy.ip, self.proxy.port,
                    self.proxy.username, self.proxy.password
                )
                local_port = forwarder.start()
                self._proxy_forwarder = forwarder
                options.add_argument(f"--proxy-server=http://127.0.0.1:{local_port}")
                self.logger.debug(f"Local proxy forwarder running on 127.0.0.1:{local_port}")
            else:
                options.add_argument(f"--proxy-server=http://{self.proxy.ip}:{self.proxy.port}")

        # Dev tools if requested
        if self.enable_dev_tools:
            options.add_argument("--auto-open-devtools-for-tabs")

        try:
            chrome_version = self._get_chrome_version()
            if chrome_version:
                self.logger.info(f"Using Chrome version {chrome_version} for undetected-chromedriver")
                self.driver = uc.Chrome(options=options, version_main=chrome_version)
            else:
                self.logger.info("Auto-detecting Chrome version for undetected-chromedriver")
                self.driver = uc.Chrome(options=options)

            self.logger.info("Undetected Chrome WebDriver initialized successfully")
            return True
        except Exception as e:
            self.logger.error(f"Failed to initialize WebDriver: {e}")
            return False

    def _quit_driver(self):
        """Quit the WebDriver and clean up resources"""
        if self.driver:
            try:
                self.driver.quit()
                self.logger.info("WebDriver closed successfully")
            except Exception as e:
                self.logger.warning(f"Error closing WebDriver: {e}")
            finally:
                self.driver = None
        if self._proxy_forwarder:
            try:
                self._proxy_forwarder.stop()
                self.logger.debug("Local proxy forwarder stopped")
            except Exception as e:
                self.logger.debug(f"Error stopping proxy forwarder: {e}")
            finally:
                self._proxy_forwarder = None

    def _get_verification_code_imap(
        self, imap_details: models.IMAPDetails, email_username: str, timeout_seconds: int = 30
    ) -> str:
        """Gets the verification code from catch all email via imap."""
        self.logger.debug("Getting account verification code via imap.")
        email_query = AND(to=email_username, seen=False)
        with MailBox(imap_details.ip, imap_details.port).login(
            imap_details.email, imap_details.password
        ) as mailbox:
            timeout = time.monotonic() + timeout_seconds
            while time.monotonic() < timeout:
                emails = mailbox.fetch(email_query)
                for email in emails:
                    mail_subject = email.subject
                    self.logger.debug(f"Email subject: {mail_subject}")
                    if mail_subject:
                        # Try alphanumeric code first, then digits, then first word
                        code_match = re.search(r"\b[A-Z0-9]{4,8}\b", mail_subject.upper())
                        if code_match:
                            code = code_match.group(0)
                        else:
                            digits = re.findall(r"\d+", mail_subject)
                            if digits:
                                code = digits[0]
                            else:
                                code = mail_subject.split()[0].strip() if mail_subject.split() else ""
                        self.logger.debug(f"Returning verification code from subject: {code}")
                        return code
                time.sleep(0.5)
        raise RegistrationError("Timed out waiting for registration code.")

    def _gmail_email_matches(self, expected_email: str) -> bool:
        """Check if the currently logged-in Gmail email matches expected_email.
        Uses JavaScript to extract the email from Gmail's internal data structures."""
        if not expected_email:
            return True
        try:
            email_lower = expected_email.lower()
            # Try multiple JS methods to find the logged-in email
            found_email = AccountCreatorSelenium._gmail_driver.execute_script("""
                // Method 1: Gmail global data (most reliable)
                if (window.WIZ_global_data && window.WIZ_global_data.Sn) {
                    return window.WIZ_global_data.Sn;
                }
                // Method 2: OG tag
                var meta = document.querySelector('meta[itemprop=\"email\"]');
                if (meta) return meta.content;
                // Method 3: Profile menu
                var profileEl = document.querySelector('[data-tooltip=\"Google Account\"] img, [aria-label*=\"Google Account\"] img, [data-hovercard-id]');
                if (profileEl && profileEl.alt) {
                    var match = profileEl.alt.match(/[\\w.-]+@[\\w.-]+\\.\\w+/);
                    if (match) return match[0];
                }
                // Method 4: Search all text on page for email pattern
                var bodyText = document.body ? document.body.innerText : '';
                var emailMatch = bodyText.match(/([\\w.-]+@[\\w.-]+\\.\\w+)/);
                if (emailMatch) return emailMatch[0];
                // Method 5: Page title
                var titleMatch = document.title.match(/([\\w.-]+@[\\w.-]+\\.\\w+)/);
                if (titleMatch) return titleMatch[0];
                return null;
            """)
            if found_email:
                self.logger.debug(f"Detected Gmail email in profile: {found_email}")
                match = found_email.lower() == email_lower
                if match:
                    return True
                # If emails differ but we're on Gmail, log warning but still assume match
                # to avoid unnecessary profile clearing
                self.logger.warning(f"Gmail email mismatch: expected={email_lower}, found={found_email.lower()} — assuming OK to avoid profile wipe")
                return True
            # If we can't find an email but we're on a Google/Gmail page, assume match
            current_url = AccountCreatorSelenium._gmail_driver.current_url
            if "mail.google.com" in current_url or "gmail" in current_url:
                self.logger.info("On Gmail page but could not verify email — assuming match")
                return True
            return False
        except Exception as e:
            self.logger.debug(f"Email check failed: {e}")
            return False


    def _extract_email_parts(self, raw_email: str) -> tuple[str, str]:
        """Extract username and domain from an email string.
        Handles malformed input like 'Harpnotsgmail.com' -> ('Harpnots', 'gmail.com')."""
        if '@' in raw_email:
            parts = raw_email.rsplit('@', 1)
            return parts[0], parts[1]
        # Try to detect domain without @
        known_domains = ['gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'protonmail.com']
        raw_lower = raw_email.lower()
        for domain in known_domains:
            if raw_lower.endswith(domain):
                username = raw_email[:-len(domain)]
                self.logger.warning(f"Email '{raw_email}' missing @ — reconstructed as '{username}@{domain}'")
                return username, domain
        # Can't detect domain — use config default
        self.logger.warning(f"Email '{raw_email}' missing @ and unknown domain — using as-is with domain '{self.account_email_domain}'")
        return raw_email, self.account_email_domain

    def _clear_gmail_profile(self, profile_dir: Path) -> None:
        """Close Gmail browser and delete the profile directory."""
        try:
            AccountCreatorSelenium._gmail_driver.quit()
        except Exception:
            pass
        AccountCreatorSelenium._gmail_driver = None
        try:
            import shutil
            if profile_dir.exists():
                shutil.rmtree(profile_dir)
                self.logger.info(f"Deleted old Gmail profile: {profile_dir}")
        except Exception:
            pass

    def _generate_human_username(self) -> str:
        """Generate a human-like display name: FirstNameLastName + random digits."""
        first_names = [
            "James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph",
            "Thomas", "Charles", "Daniel", "Matthew", "Anthony", "Mark", "Donald", "Steven",
            "Paul", "Andrew", "Kenneth", "Joshua", "Kevin", "Brian", "George", "Edward",
            "Ronald", "Timothy", "Jason", "Jeffrey", "Ryan", "Jacob", "Gary", "Nicholas",
            "Eric", "Jonathan", "Stephen", "Larry", "Justin", "Scott", "Brandon", "Benjamin",
            "Samuel", "Gregory", "Frank", "Alexander", "Raymond", "Patrick", "Jack", "Dennis",
            "Jerry", "Tyler", "Aaron", "Jose", "Adam", "Nathan", "Henry", "Douglas",
            "Zachary", "Peter", "Kyle", "Walter", "Ethan", "Jeremy", "Harold", "Keith",
            "Christian", "Roger", "Noah", "Gerald", "Carl", "Terry", "Sean", "Austin",
            "Arthur", "Lawrence", "Jesse", "Dylan", "Bryan", "Joe", "Jordan", "Billy",
            "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica",
            "Sarah", "Karen", "Nancy", "Lisa", "Betty", "Margaret", "Sandra", "Ashley",
            "Kimberly", "Emily", "Donna", "Michelle", "Dorothy", "Carol", "Amanda", "Melissa",
            "Deborah", "Stephanie", "Rebecca", "Laura", "Sharon", "Cynthia", "Kathleen", "Amy",
            "Shirley", "Angela", "Helen", "Anna", "Brenda", "Pamela", "Nicole", "Emma",
            "Samantha", "Katherine", "Christine", "Debra", "Rachel", "Catherine", "Carolyn", "Janet",
            "Ruth", "Maria", "Heather", "Diane", "Virginia", "Julie", "Joyce", "Victoria",
            "Olivia", "Kelly", "Christina", "Lauren", "Joan", "Evelyn", "Megan", "Cheryl"
        ]
        last_names = [
            "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
            "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
            "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
            "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
            "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
            "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
            "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner", "Diaz", "Parker",
            "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris", "Morales", "Murphy",
            "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan", "Cooper", "Peterson", "Bailey",
            "Reed", "Kelly", "Howard", "Ramos", "Kim", "Cox", "Ward", "Richardson", "Watson",
            "Brooks", "Chavez", "Wood", "James", "Bennett", "Gray", "Mendoza", "Ruiz",
            "Hughes", "Price", "Alvarez", "Castillo", "Sanders", "Patel", "Myers", "Long",
            "Ross", "Foster", "Jimenez", "Powell", "Jenkins", "Perry", "Russell", "Sullivan"
        ]
        first = random.choice(first_names)
        last = random.choice(last_names)
        suffix = random.randint(1, 999)
        return f"{first}{last}{suffix}"

    def _init_gmail_driver(self) -> None:
        """Open Gmail browser. If not logged in, waits for the user to sign in manually.
        If a different email is saved in the profile, clears it and forces fresh login.
        The browser is reused across accounts — only recreated if crashed or wrong email."""
        expected_email = self.gmail_web_details.email if self.gmail_web_details else ""
        profile_dir = Path("gmail_profile")

        with AccountCreatorSelenium._gmail_lock:
            if AccountCreatorSelenium._gmail_driver is not None:
                try:
                    # Just check if browser is alive by getting URL
                    current_url = AccountCreatorSelenium._gmail_driver.current_url
                    # If we're on any Google/Gmail page and email matches, just refresh inbox
                    if "google.com" in current_url or "gmail" in current_url:
                        if self._gmail_email_matches(expected_email):
                            self.logger.info("Gmail browser already open — refreshing inbox")
                            AccountCreatorSelenium._gmail_driver.get("https://mail.google.com/mail/u/0/#inbox")
                            time.sleep(3)
                            return
                        else:
                            self.logger.info("Gmail profile has a different login — clearing profile for fresh login...")
                            self._clear_gmail_profile(profile_dir)
                    else:
                        # Browser is on some non-Gmail page, navigate to Gmail
                        self.logger.info("Gmail browser open but not on Gmail — navigating to inbox")
                        AccountCreatorSelenium._gmail_driver.get("https://mail.google.com/mail/u/0/#inbox")
                        time.sleep(3)
                        if self._gmail_email_matches(expected_email):
                            return
                        self._clear_gmail_profile(profile_dir)
                except Exception:
                    # Browser may have crashed, recreate
                    self.logger.warning("Gmail browser stale, recreating...")
                    try:
                        AccountCreatorSelenium._gmail_driver.quit()
                    except Exception:
                        pass
                    AccountCreatorSelenium._gmail_driver = None

            if not self.gmail_web_details:
                raise RegistrationError("Gmail web details not configured")

            self.logger.info("Opening Gmail browser — please sign in manually...")
            self.logger.info("========================================")
            self.logger.info("  A Chrome window will open for Gmail.")
            self.logger.info("  Please sign into your Gmail account.")
            self.logger.info("  The script will continue automatically.")
            self.logger.info("========================================")

            profile_dir.mkdir(exist_ok=True)

            options = uc.ChromeOptions()
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument(f"--user-agent={self.user_agent}")
            options.add_argument(f"--user-data-dir={profile_dir.resolve()}")
            options.add_argument("--window-size=800,600")
            options.add_argument("--window-position=50,30")

            AccountCreatorSelenium._gmail_driver = uc.Chrome(options=options)
            AccountCreatorSelenium._gmail_driver.set_page_load_timeout(30)

        # Open Gmail sign-in page
        AccountCreatorSelenium._gmail_driver.get("https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fmail.google.com&flowName=GlifWebSignIn&flowEntry=ServiceLogin")
        time.sleep(5)

        # Check if already logged in (persistent profile)
        current_url = AccountCreatorSelenium._gmail_driver.current_url
        if "inbox" in current_url or "mail.google.com/mail" in current_url:
            if self._gmail_email_matches(expected_email):
                self.logger.info("Already logged into Gmail (persistent profile)")
                return
            else:
                self.logger.info("Gmail profile has a different login — clearing and restarting...")
                self._clear_gmail_profile(profile_dir)
                # Recreate Chrome with fresh profile
                with AccountCreatorSelenium._gmail_lock:
                    profile_dir.mkdir(exist_ok=True)
                    opts = uc.ChromeOptions()
                    opts.add_argument("--no-sandbox")
                    opts.add_argument("--disable-dev-shm-usage")
                    opts.add_argument("--disable-blink-features=AutomationControlled")
                    opts.add_argument(f"--user-agent={self.user_agent}")
                    opts.add_argument(f"--user-data-dir={profile_dir.resolve()}")
                    opts.add_argument("--window-size=800,600")
                    opts.add_argument("--window-position=50,30")
                    AccountCreatorSelenium._gmail_driver = uc.Chrome(options=opts)
                    AccountCreatorSelenium._gmail_driver.set_page_load_timeout(30)
                # Re-navigate to sign-in page
                AccountCreatorSelenium._gmail_driver.get("https://accounts.google.com/v3/signin/identifier?continue=https%3A%2F%2Fmail.google.com&flowName=GlifWebSignIn&flowEntry=ServiceLogin")
                time.sleep(5)

        # Wait for user to sign in manually
        self.logger.info("Waiting for you to sign into Gmail...")
        self.logger.info("Please complete the sign-in. The script will detect it automatically.")
        wait_start = time.monotonic()
        max_wait = 300  # 5 minutes

        while time.monotonic() - wait_start < max_wait:
            current_url = AccountCreatorSelenium._gmail_driver.current_url
            page_title = AccountCreatorSelenium._gmail_driver.title.lower()

            self.logger.debug(f"Gmail wait loop: url={current_url}, title={page_title}")

            # PRIMARY: Check for actual Gmail inbox elements (most reliable)
            if "mail.google.com" in current_url:
                try:
                    # Look for Compose button or inbox container — proves we're IN Gmail
                    gmail_elements = AccountCreatorSelenium._gmail_driver.find_elements(
                        By.CSS_SELECTOR, "div[role='main'], div[aria-label='Primary'], .aeH, .T-I-KE"
                    )
                    if gmail_elements and any(el.is_displayed() for el in gmail_elements):
                        self.logger.info("Gmail inbox loaded — login confirmed")
                        time.sleep(3)
                        return
                except Exception:
                    pass

            # SECONDARY: Check page title for Gmail inbox (but be strict)
            if "inbox" in page_title:
                if "sign in" not in page_title and "login" not in page_title:
                    self.logger.info(f"Gmail inbox title detected. Continuing...")
                    time.sleep(3)
                    return

            # Google account management page
            if "myaccount.google.com" in current_url:
                self.logger.info("Google account page detected, redirecting to Gmail...")
                AccountCreatorSelenium._gmail_driver.get("https://mail.google.com")
                time.sleep(6)
                return

            # If we're on mail.google.com but couldn't find inbox elements, keep waiting
            # (don't falsely assume login is complete)
            if "mail.google.com" in current_url:
                self.logger.info("On Gmail domain but inbox not loaded yet, waiting...")
                time.sleep(3)
                continue

            # Still on sign-in page — keep waiting
            time.sleep(3)

        raise RegistrationError("Timed out waiting for manual Gmail login (5 minutes).")

    def _quit_gmail_driver(self) -> None:
        """Close the shared Gmail browser."""
        with AccountCreatorSelenium._gmail_lock:
            if AccountCreatorSelenium._gmail_driver:
                try:
                    AccountCreatorSelenium._gmail_driver.quit()
                    self.logger.info("Gmail browser closed")
                except Exception as e:
                    self.logger.debug(f"Error closing Gmail browser: {e}")
                finally:
                    AccountCreatorSelenium._gmail_driver = None

    def _get_verification_code_gmail_web(
        self, gmail_web_details: models.GmailWebDetails, email_username: str, timeout_seconds: int = 120
    ) -> str:
        """Gets the verification code from the already-open Gmail browser.
        Searches Gmail for 'jagex' and extracts the first 5 characters of the email subject."""
        if AccountCreatorSelenium._gmail_driver is None:
            raise RegistrationError("Gmail browser not initialized. Call _init_gmail_driver first.")

        self.logger.info("Searching Gmail for 'jagex' emails...")

        # Navigate to inbox first and refresh to clear any stale state from previous accounts
        AccountCreatorSelenium._gmail_driver.get("https://mail.google.com/mail/u/0/#inbox")
        time.sleep(3)

        # Clear any search query by reloading plain inbox
        AccountCreatorSelenium._gmail_driver.get("https://mail.google.com/mail/u/0")
        time.sleep(2)
        AccountCreatorSelenium._gmail_driver.refresh()
        time.sleep(3)

        search_url = "https://mail.google.com/mail/u/0/#search/jagex"
        AccountCreatorSelenium._gmail_driver.get(search_url)
        time.sleep(6)

        timeout = time.monotonic() + timeout_seconds
        check_count = 0
        while time.monotonic() < timeout:
            check_count += 1
            try:
                # Always refresh to avoid stale/cached search results (old emails)
                if check_count > 1:
                    AccountCreatorSelenium._gmail_driver.refresh()
                    time.sleep(4)

                # Strategy A: Try clicking the newest email in search results and read its subject
                # This avoids matching old emails further down the list
                try:
                    # Gmail search results: first visible email row is the newest
                    # Try common selectors for the first email row
                    first_email = None
                    for sel in ["tr.zA", "tr.yW", ".zA", ".yW", "[role='main'] tr", "table.F cf tr"]:
                        try:
                            rows = AccountCreatorSelenium._gmail_driver.find_elements(By.CSS_SELECTOR, sel)
                            if rows:
                                first_email = rows[0]
                                break
                        except Exception:
                            continue

                    if first_email:
                        first_email.click()
                        self.logger.info("Clicked first (newest) email in Gmail search results")
                        time.sleep(3)

                        # Read the email subject from the opened email view
                        # Gmail subjects are typically in h2 with data-legacy-thread-id or similar
                        subject_text = ""
                        for subj_sel in ["h2.hP", ".ha h2", "[role='main'] h2", "h2[data-thread-perm-id]", ".aY"]:
                            try:
                                subj_el = AccountCreatorSelenium._gmail_driver.find_element(By.CSS_SELECTOR, subj_sel)
                                subject_text = subj_el.text.strip()
                                if subject_text:
                                    break
                            except Exception:
                                continue

                        if subject_text:
                            self.logger.info(f"Opened newest email subject: {subject_text}")
                            # Extract code from subject using same patterns
                            for pattern in [
                                r"\b([A-Z0-9]{5})\b",
                                r"\b([A-Z0-9]{4,8})\b",
                            ]:
                                m = re.search(pattern, subject_text.upper())
                                if m:
                                    code = m.group(1)
                                    if code.upper() != "JAGEX":
                                        self.logger.info(f"Found verification code from newest opened email: {code}")
                                        return code

                        # Go back to search results for next attempt
                        AccountCreatorSelenium._gmail_driver.back()
                        time.sleep(2)
                except Exception as e:
                    self.logger.debug(f"Gmail click-first-email strategy failed: {e}")

                # Strategy B: Scan page text (fallback — may match old emails if Strategy A failed)
                page_text = AccountCreatorSelenium._gmail_driver.find_element(By.TAG_NAME, "body").text
                lines = page_text.split('\n')

                # Strategy 1: Match the exact Jagex pattern:
                # "XXXXX is your Jagex verification code" where XXXXX is the code
                for line in lines:
                    line_stripped = line.strip()
                    line_lower = line_stripped.lower()
                    if "is your jagex verification code" in line_lower:
                        first_word = line_stripped.split()[0] if line_stripped.split() else ""
                        if len(first_word) == 5 and first_word.isalnum():
                            self.logger.info(f"Found Jagex verification code: {first_word}")
                            return first_word

                # Strategy 2: Any line containing "jagex" — first word is the code
                for line in lines:
                    line_stripped = line.strip()
                    line_upper = line_stripped.upper()
                    if "JAGEX" in line_upper:
                        first_word = line_stripped.split()[0] if line_stripped.split() else ""
                        if len(first_word) == 5 and first_word.isalnum() and first_word.upper() != "JAGEX":
                            self.logger.info(f"Found Jagex verification code in subject: {first_word}")
                            return first_word

                # Strategy 3: Regex for "CODE is your" or "CODE - Jagex" patterns
                for line in lines:
                    line_lower = line.lower()
                    if "jagex" in line_lower:
                        # Match "AB12C is your..." or "AB12C - ..." patterns
                        code_match = re.search(r"\b([A-Z0-9]{5})\b", line)
                        if code_match:
                            code = code_match.group(1)
                            if code.upper() != "JAGEX":
                                self.logger.info(f"Found verification code via regex: {code}")
                                return code

            except Exception as e:
                self.logger.debug(f"Gmail web check attempt {check_count} error: {e}")

            self.logger.info(f"Gmail check {check_count}: no Jagex code yet, waiting...")
            time.sleep(5)

        raise RegistrationError("Timed out waiting for verification code in Gmail web.")

    def _get_verification_code_guerrilla_mail(
        self, email_username: str, timeout_seconds: int = 30
    ) -> str:
        """Get the verification code for the jagex account from a temp Guerrilla Mail email."""
        guerrilla_mail_api_url = "https://api.guerrillamail.com/ajax.php"
        self.logger.debug("Getting account verification code via Guerrilla mail.")

        get_email_resp = self.wreq_client.get(
            url=guerrilla_mail_api_url,
            query={"f": "get_email_address", "lang": "en"},
            proxy=self.wreq_proxy,
        )
        get_email_resp.raise_for_status()
        sid_token = get_email_resp.json()["sid_token"]

        email_username = email_username.lower()

        set_email_resp = self.wreq_client.get(
            url=guerrilla_mail_api_url,
            query={
                "f": "set_email_user",
                "email_user": email_username,
                "lang": "en",
                "sid_token": sid_token,
            },
            proxy=self.wreq_proxy,
        )
        set_email_resp.raise_for_status()

        if email_username not in set_email_resp.json()["email_addr"]:
            raise RegistrationError("Failed to set account email on Guerrilla Mail.")

        # Capture existing mail IDs so we don't reuse an old code
        existing_ids = set()
        try:
            initial_resp = self.wreq_client.get(
                url=guerrilla_mail_api_url,
                query={"f": "check_email", "sid_token": sid_token, "seq": 0},
                proxy=self.wreq_proxy,
            )
            initial_resp.raise_for_status()
            for email in initial_resp.json().get("list", []):
                existing_ids.add(email.get("mail_id"))
            self.logger.info(f"Guerrilla Mail: {len(existing_ids)} existing email(s) recorded, will ignore these")
        except Exception as e:
            self.logger.warning(f"Guerrilla Mail: could not record existing emails: {e}")

        timeout = time.monotonic() + timeout_seconds
        while time.monotonic() < timeout:
            check_email_resp = self.wreq_client.get(
                url=guerrilla_mail_api_url,
                query={"f": "check_email", "sid_token": sid_token, "seq": 0},
                proxy=self.wreq_proxy,
            )
            check_email_resp.raise_for_status()

            # Collect all NEW Jagex emails, then pick the most recent (highest mail_id)
            new_jagex_emails = []
            for email in check_email_resp.json().get("list", []):
                if email.get("mail_from") != "no-reply@contact.jagex.com":
                    continue
                if email.get("mail_id") in existing_ids:
                    continue
                new_jagex_emails.append(email)

            if new_jagex_emails:
                # Sort by mail_id descending (newest first) and take the first
                new_jagex_emails.sort(key=lambda e: int(e.get("mail_id", 0)), reverse=True)
                newest_email = new_jagex_emails[0]
                mail_subject: str = newest_email["mail_subject"]
                self.logger.info(f"Using newest Guerrilla Mail (id={newest_email.get('mail_id')}) subject: {mail_subject}")
                # Try alphanumeric code first, then digits, then first word
                code_match = re.search(r"\b[A-Z0-9]{4,8}\b", mail_subject.upper())
                if code_match:
                    code = code_match.group(0)
                else:
                    digits = re.findall(r"\d+", mail_subject)
                    if digits:
                        code = digits[0]
                    else:
                        code = mail_subject.split()[0] if mail_subject.split() else ""
                self.logger.info(f"Returning verification code from newest email: {code}")
                return code
            time.sleep(1)
        raise RegistrationError("Timed out waiting for registration code.")

    def _get_verification_code_xitroo(self, account_email: str, timeout_seconds: int = 120) -> str:
        """Get account verification code via xitroo temp mail api."""
        self.logger.info(f"Getting verification code via xitroo for {account_email}")

        # Determine the max timestamp of existing emails so we only pick NEW ones
        max_existing_timestamp = 0
        try:
            initial_resp = self.wreq_client.get(
                url="https://api.xitroo.com/v1/mails",
                query={
                    "locale": "com",
                    "mailAddress": account_email,
                    "mailsPerPage": "20",
                    "minTimestamp": "0",
                    "maxTimestamp": str(int(time.time())),
                },
            )
            initial_resp.raise_for_status()
            for email in initial_resp.json().get("mails", []):
                ts = email.get("timestamp", 0)
                if isinstance(ts, (int, float)) and ts > max_existing_timestamp:
                    max_existing_timestamp = int(ts)
            self.logger.info(f"Xitroo: max existing timestamp = {max_existing_timestamp}, will only use newer emails")
        except Exception as e:
            self.logger.warning(f"Xitroo: could not determine existing timestamp baseline: {e}")

        timeout = time.monotonic() + timeout_seconds
        attempt = 0
        while time.monotonic() < timeout:
            attempt += 1
            try:
                # Try WITHOUT proxy first — xitroo API may block or be slow via proxy
                check_email_resp = self.wreq_client.get(
                    url="https://api.xitroo.com/v1/mails",
                    query={
                        "locale": "com",
                        "mailAddress": account_email,
                        "mailsPerPage": "20",
                        "minTimestamp": "0",
                        "maxTimestamp": str(int(time.time())),
                    },
                )
                check_email_resp.raise_for_status()
            except Exception as e:
                self.logger.warning(f"Xitroo request without proxy failed (attempt {attempt}): {e}")
                # Fallback: try with proxy
                try:
                    check_email_resp = self.wreq_client.get(
                        url="https://api.xitroo.com/v1/mails",
                        query={
                            "locale": "com",
                            "mailAddress": account_email,
                            "mailsPerPage": "20",
                            "minTimestamp": "0",
                            "maxTimestamp": str(int(time.time())),
                        },
                        proxy=self.wreq_proxy,
                    )
                    check_email_resp.raise_for_status()
                except Exception as e2:
                    self.logger.warning(f"Xitroo request with proxy also failed (attempt {attempt}): {e2}")
                    time.sleep(3)
                    continue

            try:
                resp_json = check_email_resp.json()
            except Exception as e:
                self.logger.warning(f"Xitroo returned invalid JSON (attempt {attempt}): {e} — raw: {check_email_resp.text[:200]}")
                time.sleep(3)
                continue

            emails = resp_json.get("mails", [])
            self.logger.info(f"Xitroo returned {len(emails)} email(s) (attempt {attempt})")

            # Filter to only NEW Jagex emails (timestamp > baseline), then sort newest first
            new_jagex_emails = []
            for email in emails:
                ts = email.get("timestamp", 0)
                if isinstance(ts, (int, float)) and int(ts) <= max_existing_timestamp:
                    continue
                mail_from = email.get("from", "")
                if "jagex" not in mail_from.lower() and "no-reply@contact.jagex.com" not in mail_from.lower():
                    continue
                new_jagex_emails.append(email)

            if new_jagex_emails:
                # Sort by timestamp descending (newest first)
                new_jagex_emails.sort(key=lambda e: int(e.get("timestamp", 0)), reverse=True)
                newest_email = new_jagex_emails[0]
                try:
                    mail_subject: str = base64.b64decode(newest_email["subject"]).decode("utf-8")
                except Exception:
                    mail_subject = newest_email.get("subject", "")
                self.logger.info(f"Using newest Xitroo email (ts={newest_email.get('timestamp')}) subject: {mail_subject}")
                # Extract verification code from subject — try alphanumeric first (e.g. ABC123, 123456)
                code_match = re.search(r"\b[A-Z0-9]{4,8}\b", mail_subject.upper())
                if code_match:
                    code = code_match.group(0)
                    self.logger.info(f"Extracted verification code from newest subject: {code}")
                    return code
                # Fallback 1: digits only
                digits = re.findall(r"\d+", mail_subject)
                if digits:
                    code = digits[0]
                    self.logger.info(f"Extracted numeric verification code from newest subject: {code}")
                    return code
                # Fallback 2: first word of subject
                code = mail_subject.split()[0] if mail_subject.split() else ""
                if code:
                    self.logger.info(f"Returning verification code from newest subject: {code}")
                    return code
            time.sleep(2)
        raise RegistrationError("Timed out waiting for registration code.")

    def _find_and_type_text(self, text_to_find: str, text_to_type: str, max_attempts: int = 3) -> None:
        """Find an input element by label/placeholder and type into it"""
        for attempt in range(max_attempts):
            try:
                element = None

                # Strategy 1: input with exact placeholder
                try:
                    element = WebDriverWait(self.driver, 2).until(
                        EC.presence_of_element_located((By.XPATH, f"//input[@placeholder='{text_to_find}']"))
                    )
                except Exception:
                    pass

                # Strategy 2: input with partial placeholder
                if element is None:
                    try:
                        element = WebDriverWait(self.driver, 2).until(
                            EC.presence_of_element_located((By.XPATH, f"//input[contains(@placeholder,'{text_to_find}')]"))
                        )
                    except Exception:
                        pass

                # Strategy 3: label text -> associated input (by @for or sibling)
                if element is None:
                    try:
                        element = WebDriverWait(self.driver, 2).until(
                            EC.presence_of_element_located((By.XPATH,
                                f"//label[contains(text(),'{text_to_find}')]//following::input[1] | "
                                f"//label[contains(text(),'{text_to_find}')]/following-sibling::input | "
                                f"//label[contains(.,'{text_to_find}')]//following::input[1]"
                            ))
                        )
                    except Exception:
                        pass

                # Strategy 4: input by name/type attribute for common fields
                if element is None:
                    attr_map = {
                        "email address": "email",
                        "email": "email",
                        "password": "password",
                    }
                    lower = text_to_find.lower()
                    if lower in attr_map:
                        try:
                            element = WebDriverWait(self.driver, 2).until(
                                EC.presence_of_element_located((By.XPATH, f"//input[@type='{attr_map[lower]}']"))
                            )
                        except Exception:
                            pass

                # Strategy 5: any input with aria-label or title containing text
                if element is None:
                    try:
                        element = WebDriverWait(self.driver, 2).until(
                            EC.presence_of_element_located((By.XPATH,
                                f"//input[@aria-label='{text_to_find}' or contains(@aria-label,'{text_to_find}') or "
                                f"@title='{text_to_find}' or contains(@title,'{text_to_find}')]"
                            ))
                        )
                    except Exception:
                        pass

                if element is None:
                    self.logger.warning(f"Could not find input element for '{text_to_find}', attempt {attempt + 1}/{max_attempts}")
                    time.sleep(2)
                    continue

                # Scroll to element and wait for interactability
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                WebDriverWait(self.driver, 3).until(EC.element_to_be_clickable(element))

                # Click to focus, clear, then type
                element.click()
                element.clear()
                element.send_keys(text_to_type)
                time.sleep(0.5)
                self.logger.info(f"Successfully typed into '{text_to_find}'")
                return

            except Exception as e:
                self.logger.warning(f"Error typing into '{text_to_find}': {e}, attempt {attempt + 1}/{max_attempts}")
                time.sleep(2)

        raise ElementNotFoundError(f"Could not find or type into element: {text_to_find}")

    def _select_dropdown(self, name_hint: str, value: str, max_attempts: int = 3) -> None:
        """Select an option from a <select> dropdown by label/name hint"""
        from selenium.webdriver.support.ui import Select

        for attempt in range(max_attempts):
            try:
                element = None
                # Strategy 1: select by label text
                try:
                    element = WebDriverWait(self.driver, 2).until(
                        EC.presence_of_element_located((By.XPATH,
                            f"//label[contains(.,'{name_hint}')]//following::select[1] | "
                            f"//label[contains(text(),'{name_hint}')]/following-sibling::select"
                        ))
                    )
                except Exception:
                    pass

                # Strategy 2: select by name/id containing hint
                if element is None:
                    try:
                        element = WebDriverWait(self.driver, 2).until(
                            EC.presence_of_element_located((By.XPATH,
                                f"//select[contains(@name,'{name_hint.lower()}') or contains(@id,'{name_hint.lower()}') or "
                                f"contains(@name,'{name_hint}') or contains(@id,'{name_hint}')]"
                            ))
                        )
                    except Exception:
                        pass

                # Strategy 3: any select that follows a nearby label
                if element is None:
                    try:
                        element = WebDriverWait(self.driver, 2).until(
                            EC.presence_of_element_located((By.XPATH,
                                f"//select[./preceding-sibling::*[contains(text(),'{name_hint}')] or "
                                f"./ancestor::*[contains(text(),'{name_hint}')]]"
                            ))
                        )
                    except Exception:
                        pass

                if element is None:
                    self.logger.warning(f"Could not find dropdown '{name_hint}', attempt {attempt + 1}/{max_attempts}")
                    time.sleep(2)
                    continue

                select = Select(element)
                select.select_by_visible_text(value)
                time.sleep(0.5)
                self.logger.info(f"Selected '{value}' in dropdown '{name_hint}'")
                return

            except Exception as e:
                self.logger.warning(f"Error selecting dropdown '{name_hint}': {e}, attempt {attempt + 1}/{max_attempts}")
                time.sleep(2)

        raise ElementNotFoundError(f"Could not select dropdown: {name_hint}")

    def _find_and_click(self, text_to_find: str, max_attempts: int = 3) -> None:
        """Find a clickable element by visible text and click it"""
        for attempt in range(max_attempts):
            try:
                element = None

                # Strategy 1: button with exact text
                try:
                    element = WebDriverWait(self.driver, 2).until(
                        EC.element_to_be_clickable((By.XPATH, f"//button[contains(text(), '{text_to_find}')]"))
                    )
                except Exception:
                    pass

                # Strategy 2: input[type=submit] or input[type=button]
                if element is None:
                    try:
                        element = WebDriverWait(self.driver, 2).until(
                            EC.element_to_be_clickable((By.XPATH,
                                f"//input[@value='{text_to_find}' or contains(@value,'{text_to_find}')]"
                            ))
                        )
                    except Exception:
                        pass

                # Strategy 3: any clickable element with text
                if element is None:
                    try:
                        element = WebDriverWait(self.driver, 2).until(
                            EC.element_to_be_clickable((By.XPATH, f"//*[contains(text(), '{text_to_find}')]"))
                        )
                    except Exception:
                        pass

                # Strategy 4: label click (for checkboxes/radio)
                if element is None:
                    try:
                        element = WebDriverWait(self.driver, 2).until(
                            EC.element_to_be_clickable((By.XPATH, f"//label[contains(.,'{text_to_find}')]"))
                        )
                    except Exception:
                        pass

                if element is None:
                    self.logger.warning(f"Could not find clickable element '{text_to_find}', attempt {attempt + 1}/{max_attempts}")
                    time.sleep(2)
                    continue

                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                element.click()
                time.sleep(0.5)
                self.logger.info(f"Successfully clicked '{text_to_find}'")
                return

            except Exception as e:
                self.logger.warning(f"Error clicking '{text_to_find}': {e}, attempt {attempt + 1}/{max_attempts}")
                time.sleep(2)

        raise ElementNotFoundError(f"Could not find clickable element: {text_to_find}")

    def _check_for_cloudflare(self) -> bool:
        """Check if Cloudflare hard-blocked us"""
        try:
            page_source = self.driver.page_source.lower()
            blocked = (
                "sorry, you have been blocked" in page_source
                and "you are unable to access this website" in page_source
            )
            if blocked:
                self.logger.warning("Cloudflare hard-block page detected")
            return blocked
        except Exception as e:
            self.logger.warning(f"Error checking for Cloudflare: {e}")
            return False

    def _handle_cloudflare(self, max_retries: int = 3) -> bool:
        """Handle Cloudflare protection with retries"""
        if self.driver is None:
            self.logger.error("WebDriver is None in _handle_cloudflare")
            return False
        for attempt in range(max_retries):
            # Wait for the page to finish loading before checking
            try:
                WebDriverWait(self.driver, 10).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass
            time.sleep(10)

            if not self._check_for_cloudflare():
                self.logger.info("No Cloudflare protection detected")
                return True

            self.logger.warning(f"Cloudflare protection detected (attempt {attempt + 1}/{max_retries})")

            if attempt < max_retries - 1:
                wait_time = 15 + (attempt * 5)  # 15, 20, 25 seconds
                self.logger.info(f"Waiting {wait_time} seconds before retry...")
                time.sleep(wait_time)

                # Refresh the page
                self.driver.refresh()
                time.sleep(8)
            else:
                self.logger.error("Cloudflare protection detected after maximum retries")
                return False

        return False

    def _handle_robot_checkbox(self, timeout: int = 3) -> bool:
        """CF challenge check — just waits briefly for uc auto-resolve."""
        self.logger.info("Checking for robot verification...")
        challenge_texts = ["checking your browser", "please wait", "just a moment",
                           "verifying you are human", "verify you are human",
                           "attention required", "cf-challenge-running"]
        page_source = self.driver.page_source.lower()
        if not any(t in page_source for t in challenge_texts):
            return True
        self.logger.info("CF text present — waiting for auto-resolve...")
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            time.sleep(1)
            page_source = self.driver.page_source.lower()
            if not any(t in page_source for t in challenge_texts):
                self.logger.info("CF cleared")
                return True
        self.logger.info("CF still present — proceeding")
        return True

    def _click_checkbox_humanlike(self, element) -> None:
        """Click a checkbox using native click / ActionChains / JS to mimic human behavior"""
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(random.uniform(0.3, 0.8))

        # Attempt 1: Native element.click() — often most reliable for CF event listeners
        try:
            element.click()
            self.logger.info("Clicked checkbox via native element.click()")
        except Exception:
            # Attempt 2: ActionChains (more human-like movement)
            try:
                ActionChains(self.driver) \
                    .move_to_element(element) \
                    .pause(random.uniform(0.1, 0.4)) \
                    .click() \
                    .perform()
                self.logger.info("Clicked checkbox via ActionChains")
            except Exception:
                # Fallback 3: JS click
                self.driver.execute_script("arguments[0].click();", element)
                self.logger.info("Clicked checkbox via JS fallback")

        time.sleep(random.uniform(0.5, 1.0))
        # Verify state
        try:
            if element.is_selected():
                self.logger.info("Checkbox is now checked")
            else:
                self.logger.warning("Checkbox click did not result in checked state")
        except Exception:
            pass

    def _handle_cloudflare_challenge(self, timeout: int = 45) -> bool:
        """Detect and wait for Cloudflare interactive challenges (turnstile, waiting room, etc.)"""
        self.logger.info(f"Checking for Cloudflare interactive challenge (timeout: {timeout}s)")
        start = time.monotonic()

        # Strong indicators = actual blocking challenge is in progress
        strong_challenge_indicators = [
            "cf-challenge-running",
            "cf-turnstile",
            "challenge-form",
            "cf-bubbles",
            "please wait",
            "just a moment",
            "checking your browser",
            "verifying you are human",
            "verify you are human",
            "attention required",
        ]

        # Weak indicators = may be passive page elements (e.g. embedded turnstile on a functional form)
        weak_challenge_indicators = [
            "challenge-platform",
            "turnstile",
            "ray id",
            "ddos-guard",
        ]

        while time.monotonic() - start < timeout:
            # Try to click any robot checkbox that may have appeared
            self._handle_robot_checkbox(timeout=5)

            try:
                page_source = self.driver.page_source.lower()
                current_url = self.driver.current_url

                # Hard block (same as _check_for_cloudflare)
                hard_blocked = (
                    "sorry, you have been blocked" in page_source
                    and "you are unable to access this website" in page_source
                )
                if hard_blocked:
                    self.logger.warning("Cloudflare hard-block detected on challenge check")
                    return False

                # =====================================================================
                # FIRST: check if we're already on a known good / functional page.
                # This MUST come before challenge detection because Jagex embeds
                # passive CF elements (challenge-platform, turnstile) on working pages.
                # =====================================================================
                on_jagex_page = "registration-start" in current_url or "account.jagex.com" in current_url
                if on_jagex_page:
                    # Verification code page (email verification step)
                    try:
                        code_field = self.driver.find_element(By.ID, "registration-verify-form-code-input")
                        if code_field and code_field.is_displayed():
                            self.logger.info("Verification code page detected — no active challenge")
                            return True
                    except Exception:
                        pass

                    # Password page
                    try:
                        pwd_field = self.driver.find_element(By.ID, "registration-start-form--field-password")
                        if pwd_field and pwd_field.is_displayed():
                            self.logger.info("Password page detected — no active challenge")
                            return True
                    except Exception:
                        pass

                    # Email registration page
                    try:
                        email_field = self.driver.find_element(By.ID, "registration-start-form--field-email")
                        if email_field and email_field.is_displayed():
                            self.logger.info("Registration page detected — no active challenge")
                            return True
                    except Exception:
                        pass

                # Management / profile page (2FA setup path)
                if "manage" in current_url or "profile" in current_url:
                    try:
                        mgmt_indicators = self.driver.find_elements(
                            By.CSS_SELECTOR, "[data-testid='mfa-enable-totp-button'], h1, [class*='profile']"
                        )
                        if mgmt_indicators:
                            self.logger.info("Management page detected — no active challenge")
                            return True
                    except Exception:
                        pass

                # =====================================================================
                # SECOND: detect ACTIVE blocking challenges only
                # =====================================================================
                has_strong_challenge = any(ind in page_source for ind in strong_challenge_indicators)

                # Visible challenge iframes
                cf_iframes = self.driver.find_elements(
                    By.XPATH,
                    "//iframe[contains(@src,'challenges.cloudflare') or contains(@src,'turnstile')]"
                )
                has_active_iframe = any(
                    iframe.is_displayed()
                    for iframe in cf_iframes
                )

                # Visible CF overlay elements
                cf_waiting = self.driver.find_elements(
                    By.XPATH, "//*[contains(@id,'cf-') or contains(@class,'cf-')]"
                )
                has_visible_cf_elements = any(
                    elem.is_displayed()
                    for elem in cf_waiting
                )

                has_active_challenge = has_strong_challenge or has_active_iframe or has_visible_cf_elements

                if has_active_challenge:
                    strong_found = [i for i in strong_challenge_indicators if i in page_source]
                    self.logger.info(
                        f"Active Cloudflare challenge detected (strong: {strong_found}, "
                        f"iframe: {has_active_iframe}, visible_cf: {has_visible_cf_elements}) — "
                        f"waiting... ({int(time.monotonic() - start)}s elapsed)"
                    )
                    time.sleep(5)
                    continue

                # =====================================================================
                # THIRD: weak indicators only — page is likely functional, but
                # do a short grace period in case a challenge is still loading.
                # =====================================================================
                has_weak_challenge = any(ind in page_source for ind in weak_challenge_indicators)
                if has_weak_challenge:
                    weak_found = [i for i in weak_challenge_indicators if i in page_source]
                    # Only wait a brief grace period for weak indicators
                    if time.monotonic() - start < 8:
                        self.logger.info(
                            f"Weak CF indicators only ({weak_found}) — brief grace period... "
                            f"({int(time.monotonic() - start)}s elapsed)"
                        )
                        time.sleep(2)
                        continue
                    else:
                        self.logger.info(
                            f"Weak CF indicators persist but grace period exceeded — treating as clear"
                        )
                        return True

                # No indicators at all
                self.logger.info("No Cloudflare challenge detected")
                return True

            except Exception as e:
                self.logger.warning(f"Error during CF challenge check: {e}")
                time.sleep(3)

        self.logger.error(f"Cloudflare challenge did not resolve within {timeout} seconds")
        return False

    def register_account(self) -> models.AccountRegistrationResult:
        """Register a new Jagex account using Selenium"""
        transfer_stats = models.TransferStats(bytes_sent=0, bytes_received=0)
        start_time = time.monotonic()
        
        try:
            self.logger.info("Starting Selenium-based registration with Chrome incognito mode")
            
            # Initialize fresh WebDriver for each account (clean incognito session)
            if not self._init_driver():
                raise RegistrationError("Failed to initialize WebDriver")
            
            # If using Gmail Web Login, open Gmail browser first and log in
            if self.mail_provider == models.MailProvider.GMAIL_WEB:
                self._init_gmail_driver()
            
            # Navigate to registration page
            self.logger.info(f"Navigating to registration URL: {self._REGISTRATION_URL}")
            self.driver.get(self._REGISTRATION_URL)
            time.sleep(5)

            if self.driver is None:
                raise RegistrationError("WebDriver is None after initialization")

            # IMMEDIATE: check for rate limit page before doing any CF/robot work
            page_source_lower = self.driver.page_source.lower()
            if "too many requests" in page_source_lower or "rate limited" in page_source_lower:
                self.logger.error("Jagex rate limit detected immediately after page load — aborting")
                raise RegistrationError("Rate limited by Jagex: Too many requests")

            # Handle Cloudflare protection
            if not self._handle_cloudflare():
                raise RegistrationError("IP is blocked by Cloudflare. Try different proxy or wait longer.")

            # Quick rate limit re-check after CF handling
            page_source_lower = self.driver.page_source.lower()
            if "too many requests" in page_source_lower or "rate limited" in page_source_lower:
                self.logger.error("Jagex rate limit detected after CF handling — aborting")
                raise RegistrationError("Rate limited by Jagex: Too many requests")

            # Handle robot verification checkbox if present (quick check, don't wait too long)
            self._handle_robot_checkbox(timeout=10)

            # Dismiss cookie consent banner if present
            self._dismiss_cookie_banner()

            # Generate account details
            base_email = None
            if self.mail_provider == models.MailProvider.IMAP and self.imap_details:
                base_email = self.imap_details.email
            elif self.mail_provider == models.MailProvider.GMAIL_WEB and self.gmail_web_details:
                base_email = self.gmail_web_details.email
            
            if base_email:
                # Use Gmail plus addressing: base_username+randomtag@domain
                # Robust: reconstruct email even if @ is missing
                base_username, domain = self._extract_email_parts(base_email)
                tag = utils.generate_string(include_punctuation=False, length=8)
                username = f"{base_username}+{tag}"
                account_email = models.Email(
                    username=username,
                    domain=domain
                )
            else:
                account_email = models.Email(
                    username=utils.generate_string(include_punctuation=False),
                    domain=self.account_email_domain
                )
            account_email_address = account_email.address
            
            self.logger.info(f"Creating account with email: {account_email_address}")

            # Check for rate limit / blocked pages before filling form
            page_source_check = self.driver.page_source.lower()
            if "too many requests" in page_source_check or "rate limited" in page_source_check:
                self.logger.error("Jagex rate limit detected ('Too many requests') — aborting this attempt")
                raise RegistrationError("Rate limited by Jagex: Too many requests")

            # Fill in registration form
            self.logger.info("Filling registration form")

            try:
                # Email field
                self._find_and_type_text("Email address", account_email_address)
                time.sleep(1)
                
                # Birthday fields
                self.logger.info("Filling birthday fields")
                day = random.randint(1, 28)
                month = random.randint(1, 12)
                year = random.randint(1990, 2005)
                month_names = [
                    "January", "February", "March", "April", "May", "June",
                    "July", "August", "September", "October", "November", "December"
                ]

                def _fill_input_by_attr(attr: str, keywords: list[str], value: str, month_name: str | None = None) -> bool:
                    """Find an input/select by name/id attribute and fill it"""
                    for kw in keywords:
                        try:
                            element = WebDriverWait(self.driver, 2).until(
                                EC.presence_of_element_located((By.XPATH,
                                    f"//input[@{attr}='{kw}' or contains(@{attr},'{kw}')] | "
                                    f"//select[@{attr}='{kw}' or contains(@{attr},'{kw}')]"
                                ))
                            )
                            if element:
                                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                                WebDriverWait(self.driver, 2).until(EC.element_to_be_clickable(element))
                                element.click()
                                if element.tag_name.lower() == "select":
                                    from selenium.webdriver.support.ui import Select
                                    Select(element).select_by_visible_text(month_name or value)
                                else:
                                    element.clear()
                                    # For number inputs, always use numeric value, not month name
                                    if element.get_attribute("type") == "number":
                                        element.send_keys(value)
                                    else:
                                        element.send_keys(month_name or value)
                                time.sleep(0.5)
                                self.logger.info(f"Filled birthday field by @{attr}='{kw}'")
                                return True
                        except Exception:
                            continue
                    return False

                def _fill_by_exact_id(id_value: str, value: str, month_name: str | None = None) -> bool:
                    """Find an input/select by exact id and fill it"""
                    try:
                        element = WebDriverWait(self.driver, 2).until(
                            EC.presence_of_element_located((By.ID, id_value))
                        )
                        if element:
                            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                            WebDriverWait(self.driver, 2).until(EC.element_to_be_clickable(element))
                            element.click()
                            if element.tag_name.lower() == "select":
                                from selenium.webdriver.support.ui import Select
                                Select(element).select_by_visible_text(month_name or value)
                            else:
                                element.clear()
                                if element.get_attribute("type") == "number":
                                    element.send_keys(value)
                                else:
                                    element.send_keys(month_name or value)
                            time.sleep(0.5)
                            self.logger.info(f"Filled birthday field by exact id='{id_value}'")
                            return True
                    except Exception:
                        pass
                    return False

                def _fill_birthday_field(hints: list[str], value: str, month_name: str | None = None) -> None:
                    """Try to fill a single birthday field with multiple strategies"""
                    # Strategy 1: Jagex-specific exact IDs
                    jagex_ids = {
                        "day": "registration-start-form--field-day",
                        "month": "registration-start-form--field-month",
                        "year": "registration-start-form--field-year",
                    }
                    for key, jid in jagex_ids.items():
                        if key in [h.lower() for h in hints]:
                            if _fill_by_exact_id(jid, value, month_name):
                                return

                    # Strategy 2: direct name/id attribute search
                    name_keywords = [h.lower() for h in hints] + ["dob-" + h.lower() for h in hints] + ["birthday-" + h.lower() for h in hints]
                    if _fill_input_by_attr("name", name_keywords, value, month_name):
                        return
                    if _fill_input_by_attr("id", name_keywords, value, month_name):
                        return

                    # Strategy 3: dropdown by label text
                    for hint in hints:
                        try:
                            self._select_dropdown(hint, month_name or value)
                            return
                        except ElementNotFoundError:
                            continue

                    # Strategy 4: text input by placeholder/label
                    for hint in hints:
                        try:
                            self._find_and_type_text(hint, value)
                            return
                        except ElementNotFoundError:
                            continue

                    # Strategy 5: brute-force — find all inputs/selects near a "Date of birth" label
                    try:
                        dob_label = self.driver.find_element(By.XPATH,
                            "//*[contains(text(),'Date of birth') or contains(text(),'Birthday') or contains(text(),'DOB')]"
                        )
                        if dob_label:
                            nearby = self.driver.find_elements(By.XPATH,
                                "//input[preceding::*[contains(text(),'Date of birth')]] | "
                                "//select[preceding::*[contains(text(),'Date of birth')]] | "
                                "//input[following::*[contains(text(),'Date of birth')]] | "
                                "//select[following::*[contains(text(),'Date of birth')]]"
                            )
                            # Try to find by position among nearby inputs
                            for el in nearby:
                                try:
                                    el.click()
                                    el.clear()
                                    el.send_keys(value)
                                    time.sleep(0.5)
                                    self.logger.info(f"Filled birthday field via brute-force near DOB label")
                                    return
                                except Exception:
                                    continue
                    except Exception:
                        pass

                    self.logger.warning(f"Could not fill birthday field with hints {hints}")

                _fill_birthday_field(["Day", "day", "DD", "dd"], str(day).zfill(2))
                _fill_birthday_field(["Month", "month", "MM", "mm"], str(month).zfill(2), month_names[month - 1])
                _fill_birthday_field(["Year", "year", "YYYY", "yyyy"], str(year))

                time.sleep(1)

                # Step 1: Click "I agree" checkbox (Jagex specific ID)
                self.logger.info("Checking terms acceptance checkbox")
                try:
                    agree_checkbox = WebDriverWait(self.driver, 3).until(
                        EC.element_to_be_clickable((By.ID, "registration-start-accept-agreements"))
                    )
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", agree_checkbox)
                    agree_checkbox.click()
                    self.logger.info("Terms checkbox checked")
                    time.sleep(1)
                except Exception as e:
                    self.logger.warning(f"Could not check terms checkbox by ID: {e}")
                    # Fallback: try text-based find
                    try:
                        self._find_and_click("I agree")
                        time.sleep(0.5)
                    except:
                        try:
                            self._find_and_click("terms")
                            time.sleep(0.5)
                        except:
                            self.logger.warning("Could not find terms checkbox, may not be required")

                # Step 2: Click Continue button (Jagex specific ID) — with retry
                self.logger.info("Clicking Continue button")
                continue_clicked = False
                for c_attempt in range(3):
                    try:
                        # Re-find the button each attempt (it may become stale)
                        continue_btn = WebDriverWait(self.driver, 3).until(
                            EC.element_to_be_clickable((By.ID, "registration-start-form--continue-button"))
                        )
                        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", continue_btn)
                        # Try native click first, then JS click
                        try:
                            continue_btn.click()
                        except Exception:
                            self.driver.execute_script("arguments[0].click();", continue_btn)
                        self.logger.info("Continue button clicked")
                        continue_clicked = True
                        break
                    except Exception as e:
                        self.logger.debug(f"Continue click attempt {c_attempt+1} failed: {e}")
                        time.sleep(1)
                if not continue_clicked:
                    self.logger.warning("Could not click Continue by ID, trying text fallback...")
                    try:
                        self._find_and_click("Continue")
                        continue_clicked = True
                    except Exception:
                        self.logger.warning("Could not find Continue button")
                if not continue_clicked:
                    self.logger.error("CRITICAL: Continue button could not be clicked — registration may fail")

                # Start polling xitroo for verification code BEFORE waiting for page transition
                # so the email is already retrieved when the page loads
                xitroo_code = None
                if self.mail_provider == models.MailProvider.XITROO:
                    self.logger.info("Starting xitroo email polling early...")
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(self._get_verification_code_xitroo, account_email_address)
                        # Wait for page transition while also polling xitroo in background
                        self.logger.info("Waiting for verification code page to load...")
                        time.sleep(10)
                        try:
                            WebDriverWait(self.driver, 15).until(
                                lambda d: d.execute_script("return document.readyState") == "complete"
                            )
                        except Exception:
                            pass

                        # Handle robot verification checkbox if present on the new page
                        self._handle_robot_checkbox(timeout=10)

                        # Check for Cloudflare interactive challenge
                        cf_challenge_resolved = self._handle_cloudflare_challenge(timeout=45)
                        if not cf_challenge_resolved:
                            raise RegistrationError("Cloudflare challenge blocked after Continue. Try different proxy.")

                        # Dismiss cookie banner again if it reappeared on new page
                        self._dismiss_cookie_banner()

                        # Wait for verification code input field
                        try:
                            WebDriverWait(self.driver, 20).until(
                                EC.presence_of_element_located((By.ID, "registration-verify-form-code-input"))
                            )
                            self.logger.info("Verification code page loaded")
                        except Exception:
                            self.logger.warning("Verification code field did not appear within 20s, attempting anyway")

                        # Now retrieve the xitroo code (it may already be ready)
                        try:
                            xitroo_code = future.result(timeout=60)
                        except concurrent.futures.TimeoutError:
                            self.logger.warning("Xitroo polling timed out")
                        except Exception as e:
                            self.logger.warning(f"Xitroo polling error: {e}")

                else:
                    # Non-xitroo path: just wait for page transition
                    self.logger.info("Waiting for verification code page to load...")
                    time.sleep(10)
                    try:
                        WebDriverWait(self.driver, 15).until(
                            lambda d: d.execute_script("return document.readyState") == "complete"
                        )
                    except Exception:
                        pass

                    # Handle robot verification checkbox if present on the new page
                    self._handle_robot_checkbox(timeout=10)

                    # Check for Cloudflare interactive challenge
                    cf_challenge_resolved = self._handle_cloudflare_challenge(timeout=45)
                    if not cf_challenge_resolved:
                        raise RegistrationError("Cloudflare challenge blocked after Continue. Try different proxy.")

                    # Dismiss cookie banner again if it reappeared on new page
                    self._dismiss_cookie_banner()

                    # Wait for verification code input field
                    try:
                        WebDriverWait(self.driver, 20).until(
                            EC.presence_of_element_located((By.ID, "registration-verify-form-code-input"))
                        )
                        self.logger.info("Verification code page loaded")
                    except Exception:
                        self.logger.warning("Verification code field did not appear within 20s, attempting anyway")

                # Get verification code from email provider
                self.logger.info("Retrieving verification code from email...")
                if xitroo_code:
                    verification_code = xitroo_code
                    self.logger.info(f"Using pre-fetched xitroo verification code")
                elif self.mail_provider == models.MailProvider.IMAP:
                    verification_code = self._get_verification_code_imap(
                        self.imap_details, account_email_address
                    )
                elif self.mail_provider == models.MailProvider.GUERRILLA_MAIL:
                    verification_code = self._get_verification_code_guerrilla_mail(
                        account_email.username
                    )
                elif self.mail_provider == models.MailProvider.XITROO:
                    verification_code = self._get_verification_code_xitroo(
                        account_email_address
                    )
                elif self.mail_provider == models.MailProvider.GMAIL_WEB:
                    verification_code = self._get_verification_code_gmail_web(
                        self.gmail_web_details, account_email_address
                    )
                else:
                    raise RegistrationError(f"Unsupported mail provider: {self.mail_provider}")

                self.logger.info(f"Verification code received: {verification_code}")

                # Enter verification code into exact Jagex field
                self.logger.info("Entering verification code")
                try:
                    code_field = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.ID, "registration-verify-form-code-input"))
                    )
                    code_field.click()
                    code_field.clear()
                    code_field.send_keys(verification_code)
                    self.logger.info("Verification code entered")
                except Exception:
                    # Fallback: try by name attribute
                    try:
                        code_field = WebDriverWait(self.driver, 3).until(
                            EC.element_to_be_clickable((By.NAME, "code"))
                        )
                        code_field.click()
                        code_field.clear()
                        code_field.send_keys(verification_code)
                        self.logger.info("Verification code entered via name='code'")
                    except Exception:
                        self._find_and_type_text("code", verification_code)
                time.sleep(1)

                # Submit verification
                self.logger.info("Submitting verification code")
                try:
                    verify_btn = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[@type='submit']"))
                    )
                    verify_btn.click()
                    self.logger.info("Verify button clicked")
                except Exception:
                    self._find_and_click("Verify")
                time.sleep(3)

                # Check if a username/display name page appears next
                chosen_username = account_email.username  # default fallback
                self.logger.info("Checking for username page after verification...")
                try:
                    username_field = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "input#displayName"))
                    )
                    if username_field:
                        self.logger.info("Username page detected after verification")
                        # Generate a clean username (alphanumeric, no + or special chars)
                        username = self._generate_human_username()
                        chosen_username = username
                        filled = False

                        # Attempt 1: ActionChains with fresh element re-find for value check
                        for attempt in range(3):
                            try:
                                # Target the INPUT element specifically (span also has id=displayName)
                                field = self.driver.find_element(By.CSS_SELECTOR, "input#displayName")
                                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", field)
                                time.sleep(0.5)
                                field.click()
                                time.sleep(0.3)
                                field.send_keys(username)
                                time.sleep(0.5)
                                # Re-find element before reading value to avoid stale reference
                                field = self.driver.find_element(By.CSS_SELECTOR, "input#displayName")
                                actual = field.get_attribute("value")
                                if actual and actual == username:
                                    self.logger.info(f"Username filled via send_keys (attempt {attempt+1}): {username}")
                                    filled = True
                                    break
                                else:
                                    self.logger.warning(f"send_keys attempt {attempt+1}: value='{actual}', expected='{username}'")
                                    # Clear and retry
                                    field.clear()
                            except Exception as e:
                                self.logger.warning(f"send_keys attempt {attempt+1} failed: {e}")
                                time.sleep(1)

                        # Attempt 2: JS with React _valueTracker bypass (no el.select())
                        if not filled:
                            for attempt in range(3):
                                try:
                                    self.driver.execute_script(
                                        """
                                        var el = document.querySelector("input#displayName");
                                        if (!el) return false;
                                        el.focus();
                                        // Clear using setSelectionRange + delete for React compatibility
                                        el.setSelectionRange(0, el.value.length);
                                        document.execCommand('delete');
                                        el.value = '';
                                        // Bypass React value tracker
                                        var tracker = el._valueTracker;
                                        if (tracker) tracker.setValue('');
                                        var text = arguments[0];
                                        for (var i = 0; i < text.length; i++) {
                                            var c = text[i];
                                            el.value += c;
                                            el.dispatchEvent(new Event('input', {bubbles: true}));
                                        }
                                        el.dispatchEvent(new Event('change', {bubbles: true}));
                                        el.blur();
                                        return true;
                                        """,
                                        username
                                    )
                                    time.sleep(0.5)
                                    field = self.driver.find_element(By.CSS_SELECTOR, "input#displayName")
                                    actual = field.get_attribute("value")
                                    if actual and actual == username:
                                        self.logger.info(f"Username filled via JS (attempt {attempt+1}): {username}")
                                        filled = True
                                        break
                                    else:
                                        self.logger.warning(f"JS attempt {attempt+1}: value='{actual}', expected='{username}'")
                                except Exception as e:
                                    self.logger.warning(f"JS attempt {attempt+1} failed: {e}")
                                    time.sleep(1)

                        if not filled:
                            self.logger.error("Could not fill displayName after all attempts")
                        else:
                            self.logger.info(f"DisplayName successfully set to: {username}")

                        # Click Continue to proceed to password page
                        time.sleep(2)
                        for attempt in range(3):
                            try:
                                cont_btn = WebDriverWait(self.driver, 5).until(
                                    EC.element_to_be_clickable((By.XPATH, "//button[@type='submit']"))
                                )
                                cont_btn.click()
                                self.logger.info("Continue button clicked on username page")
                                break
                            except Exception:
                                try:
                                    self._find_and_click("Continue")
                                    break
                                except Exception:
                                    if attempt == 2:
                                        self.logger.warning("Could not click Continue on username page")
                                    time.sleep(1)
                        time.sleep(3)
                except Exception:
                    self.logger.info("No username page after verification, continuing...")

                # Check if a password page appears next
                self.logger.info("Checking for password page after verification...")
                try:
                    # Detect password page by presence of password input(s)
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
                    )
                    self.logger.info("Password page detected after verification")

                    # Generate a random password for this account
                    account_password = (
                        "".join(random.choices(string.ascii_uppercase, k=2)) +
                        "".join(random.choices(string.ascii_lowercase, k=4)) +
                        "".join(random.choices(string.digits, k=4)) +
                        "".join(random.choices("!@#$%^&*", k=2))
                    )
                    account_password = "".join(random.sample(account_password, len(account_password)))
                    self.account_password = account_password
                    self.logger.info(f"Generated random password for this account")

                    # Fill ALL password input fields on the page with the same password
                    password_inputs = self.driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
                    for idx, pwd_input in enumerate(password_inputs):
                        try:
                            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", pwd_input)
                            time.sleep(0.3)
                            pwd_input.click()
                            time.sleep(0.2)
                            pwd_input.clear()
                            pwd_input.send_keys(account_password)
                            self.logger.info(f"Filled password field {idx + 1}/{len(password_inputs)}")
                            time.sleep(0.5)
                        except Exception as e:
                            self.logger.warning(f"Failed to fill password field {idx + 1}: {e}")

                    # Check the "I agree" checkbox
                    time.sleep(1)
                    try:
                        agree_checkbox = WebDriverWait(self.driver, 5).until(
                            EC.element_to_be_clickable((By.ID, "registration-password-third-party"))
                        )
                        if not agree_checkbox.is_selected():
                            agree_checkbox.click()
                            self.logger.info("I agree checkbox checked")
                        else:
                            self.logger.info("I agree checkbox already checked")
                    except Exception as e:
                        self.logger.warning(f"Could not check I agree checkbox: {e}")

                    # Click Create account button
                    time.sleep(1)
                    for attempt in range(3):
                        try:
                            create_btn = WebDriverWait(self.driver, 5).until(
                                EC.element_to_be_clickable((By.ID, "registration-password-form--create-account-button"))
                            )
                            create_btn.click()
                            self.logger.info("Create account button clicked")
                            break
                        except Exception:
                            try:
                                # Fallback: any submit button on the page
                                create_btn = WebDriverWait(self.driver, 3).until(
                                    EC.element_to_be_clickable((By.XPATH, "//button[@type='submit']"))
                                )
                                create_btn.click()
                                self.logger.info("Create account button clicked (fallback)")
                                break
                            except Exception:
                                if attempt == 2:
                                    self.logger.warning("Could not click Create account button")
                                time.sleep(1)
                    self.logger.info("Registration form submitted")
                    time.sleep(3)
                except Exception:
                    self.logger.info("No password page after verification, continuing...")
                
                # Wait for account creation to complete
                time.sleep(5)
                
                # Check for success indicators
                page_source = self.driver.page_source.lower()
                success_indicators = ["account created", "registration successful", "welcome", "success"]
                if any(indicator in page_source for indicator in success_indicators):
                    self.logger.success("Account registration appears successful")
                else:
                    self.logger.warning("Could not confirm account creation success")
                
                # Set up 2FA if requested
                tfa_result = None
                if self.set_2fa:
                    self.logger.info("Setting up 2FA")
                    try:
                        tfa_result = self._setup_2fa(account_email_address)
                    except Exception as e:
                        self.logger.warning(f"Failed to set up 2FA: {e}")
                    if not tfa_result:
                        self.logger.error("2FA was requested but could not be set up — account will not be saved")
                        raise RegistrationError("2FA setup failed — account creation aborted")

                # Build birthday
                birthday = models.Birthday(day=day, month=month, year=year)

                # Try to get real IP (best effort)
                real_ip = self._get_real_ip()

                # Create jagex account object
                jagex_account = models.JagexAccount(
                    email=account_email,
                    username=chosen_username,
                    password=self.account_password,
                    birthday=birthday,
                    real_ip=real_ip,
                    tfa=tfa_result,
                )

                duration = time.monotonic() - start_time
                line = f"{jagex_account.email.address}:{jagex_account.password}:{jagex_account.tfa.setup_key if jagex_account.tfa else ''}"
                self.logger.success(f"Account created successfully: {line} ({duration:.2f}s)")

                return models.AccountRegistrationResult(
                    jagex_account=jagex_account,
                    transfer_stats=transfer_stats,
                    duration=timedelta(seconds=duration),
                )
                
            except ElementNotFoundError as e:
                self.logger.error(f"Required element not found: {e}")
                raise RegistrationError(f"Registration failed: {e}")
            except Exception as e:
                self.logger.error(f"Error during registration: {e}")
                self.logger.error(traceback.format_exc())
                raise RegistrationError(f"Registration failed: {e}")

        except RegistrationError:
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error during registration: {e}")
            self.logger.error(traceback.format_exc())
            raise RegistrationError(f"Registration failed: {e}")
        finally:
            # Clean up Jagex WebDriver (Gmail browser stays open for reuse)
            self._quit_driver()

    def _get_real_ip(self) -> str:
        """Get the real/public IP address (best effort)"""
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://api.ipify.org?format=text",
                headers={"User-Agent": self.user_agent},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.read().decode("utf-8").strip()
        except Exception as e:
            self.logger.warning(f"Could not determine real IP: {e}")
            return "unknown"

    def _dismiss_cookie_banner(self) -> None:
        """Try to dismiss cookie consent banners (best effort, no error on failure)"""
        cookie_button_texts = [
            "accept all cookies",
            "accept all",
            "accept cookies",
            "allow all",
            "allow all cookies",
            "agree to all",
            "i agree",
            "accept",
            "allow",
            "agree",
            "got it",
            "ok",
        ]
        try:
            # Strategy 1: Common IDs used by cookie providers (OneTrust, Cookiebot, etc.)
            cookie_ids = [
                "onetrust-accept-btn-handler",
                "onetrust-banner-sdk",
                "CybotCookiebotDialogBodyButtonAccept",
                "CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
                "CybotCookiebotDialogBodyLevelButtonAccept",
                "cookiebanner-accept",
                "accept-cookie-banner",
                "consent-accept",
                "truste-consent-button",
                "save-all-purposes",
            ]
            for cid in cookie_ids:
                try:
                    btn = WebDriverWait(self.driver, 1).until(
                        EC.element_to_be_clickable((By.ID, cid))
                    )
                    if btn:
                        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                        self.driver.execute_script("arguments[0].click();", btn)
                        self.logger.info(f"Cookie banner dismissed via ID '{cid}'")
                        time.sleep(1)
                        return
                except Exception:
                    pass

            # Strategy 2: Common class-based cookie buttons
            cookie_classes = [
                "onetrust-accept-btn-handler",
                "cc-accept",
                "cc-allow",
                "cookie-banner-accept",
                "accept-cookies",
                "cookie-accept",
                "CybotCookiebotDialogBodyButton",
            ]
            for cclass in cookie_classes:
                try:
                    btns = self.driver.find_elements(By.CLASS_NAME, cclass)
                    if btns:
                        btn = btns[0]
                        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                        self.driver.execute_script("arguments[0].click();", btn)
                        self.logger.info(f"Cookie banner dismissed via class '{cclass}'")
                        time.sleep(1)
                        return
                except Exception:
                    pass

            # Strategy 3: Common cookie banner button texts (with JS click to bypass overlays)
            for text in cookie_button_texts:
                try:
                    button = WebDriverWait(self.driver, 1).until(
                        EC.presence_of_element_located((By.XPATH,
                            f"//button[contains(text(),'{text}')] | "
                            f"//a[contains(text(),'{text}')] | "
                            f"//input[@value='{text}' or contains(@value,'{text}')] | "
                            f"//div[@role='button' and contains(text(),'{text}')] | "
                            f"//span[contains(text(),'{text}')]/ancestor::button[1] | "
                            f"//span[contains(text(),'{text}')]/ancestor::a[1]"
                        ))
                    )
                    if button:
                        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", button)
                        self.driver.execute_script("arguments[0].click();", button)
                        self.logger.info(f"Cookie banner dismissed via text '{text}'")
                        time.sleep(1)
                        return
                except Exception:
                    continue

            # Strategy 4: cookie banner shadow DOM or iframe approaches
            try:
                iframes = self.driver.find_elements(By.XPATH, "//iframe[contains(@src,'cookie') or contains(@src,'consent') or contains(@id,'cookie') or contains(@id,'consent')]")
                for iframe in iframes:
                    self.driver.switch_to.frame(iframe)
                    for text in cookie_button_texts:
                        try:
                            btn = self.driver.find_element(By.XPATH, f"//*[contains(text(),'{text}')]")
                            if btn:
                                self.driver.execute_script("arguments[0].click();", btn)
                                self.logger.info(f"Cookie banner dismissed in iframe via '{text}'")
                                time.sleep(1)
                                self.driver.switch_to.default_content()
                                return
                        except Exception:
                            continue
                    self.driver.switch_to.default_content()
            except Exception:
                pass

            # Strategy 5: Remove banner from DOM via JavaScript (last resort)
            self._remove_overlay_elements()

            self.logger.info("No cookie banner found to dismiss")
        except Exception as e:
            self.logger.debug(f"Cookie banner dismissal error (non-critical): {e}")

    def _remove_overlay_elements(self) -> None:
        """Remove known cookie/consent banners and overlays from the DOM via JavaScript"""
        try:
            self.driver.execute_script("""
                var selectors = [
                    '#CybotCookiebotDialog',
                    '#onetrust-banner-sdk',
                    '#onetrust-consent-sdk',
                    '.cookie-banner',
                    '.cookie-consent',
                    '.cc-banner',
                    '.ot-sdk-show-settings',
                    '#truste-consent-track',
                    '#cookieConsent',
                    '#cookie-banner',
                    '.cookie-modal',
                    '.consent-banner',
                    '.gdpr-banner'
                ];
                selectors.forEach(function(sel) {
                    var el = document.querySelector(sel);
                    if (el) { el.remove(); }
                });
                // Also remove fixed-position overlays that block clicks
                document.querySelectorAll('div').forEach(function(el) {
                    var style = window.getComputedStyle(el);
                    if (style.position === 'fixed' && style.zIndex > 1000) {
                        // Check if it covers most of the viewport
                        var rect = el.getBoundingClientRect();
                        if (rect.height > window.innerHeight * 0.3 && rect.width > window.innerWidth * 0.5) {
                            // Check if it contains cookie-related text
                            var text = el.innerText.toLowerCase();
                            if (text.includes('cookie') || text.includes('consent') || text.includes('privacy')) {
                                el.remove();
                            }
                        }
                    }
                });
            """)
            self.logger.info("Overlay/cookie banner removal script executed")
        except Exception as e:
            self.logger.debug(f"Overlay removal error (non-critical): {e}")

    def _setup_2fa(self, account_email: str) -> models.TwoFactorAuth | None:
        """Set up 2FA for the account using the Jagex authenticator flow."""
        for attempt in range(2):
            try:
                if attempt > 0:
                    self.logger.info("Retrying 2FA setup...")
                    time.sleep(3)
                result = self._setup_2fa_once(account_email)
                if result:
                    return result
            except Exception as e:
                self.logger.warning(f"2FA setup attempt {attempt + 1} failed: {e}")
        self.logger.warning("2FA setup failed after all retries")
        return None

    def _setup_2fa_once(self, account_email: str) -> models.TwoFactorAuth | None:
        """Single attempt at 2FA setup."""
        try:
            # Navigate to account management
            self.logger.info(f"Navigating to account management: {self._MANAGEMENT_URL}")
            self.driver.get(self._MANAGEMENT_URL)
            time.sleep(5)

            # Handle Cloudflare hard-block check
            if not self._handle_cloudflare():
                self.logger.warning("Could not bypass Cloudflare for 2FA setup")
                return None

            # Handle interactive Cloudflare challenge — functional page check comes FIRST
            cf_challenge_resolved = self._handle_cloudflare_challenge(timeout=45)
            if not cf_challenge_resolved:
                self.logger.warning("Cloudflare challenge blocked 2FA setup")
                return None

            # Wait for management page to fully render its content
            self.logger.info("Waiting for management page content to load...")
            for _ in range(10):
                try:
                    ready = self.driver.execute_script("return document.readyState") == "complete"
                    has_content = len(self.driver.find_elements(By.TAG_NAME, "body")) > 0
                    if ready and has_content:
                        # Check if we have ANY visible interactive elements
                        visible_count = len([e for e in self.driver.find_elements(By.TAG_NAME, "button") if e.is_displayed()])
                        visible_count += len([e for e in self.driver.find_elements(By.TAG_NAME, "a") if e.is_displayed()])
                        if visible_count > 0:
                            self.logger.info(f"Management page loaded with {visible_count} visible interactive elements")
                            break
                except Exception:
                    pass
                time.sleep(1)
            else:
                self.logger.warning("Management page content may still be loading, proceeding anyway...")

            # Dismiss cookie banners
            self._dismiss_cookie_banner()
            time.sleep(1)

            # --- Click enable TOTP button ---
            self.logger.info("Looking for enable TOTP button...")
            enable_btn = None
            for selector in [
                "[data-testid='mfa-enable-totp-button']",
                "[data-testid='enable-totp-button']",
                "button[id*='totp']",
                "button[id*='mfa']",
                "a[href*='authenticator']",
            ]:
                try:
                    candidates = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for candidate in candidates:
                        if candidate.is_displayed():
                            enable_btn = candidate
                            self.logger.info(f"Found enable TOTP button with selector: {selector}")
                            break
                    if enable_btn:
                        break
                except Exception:
                    continue

            if not enable_btn:
                # The element may be a <div> or <span> with click handlers, not a <button>.
                # Search by text content and also try JavaScript-based discovery.
                self.logger.warning("Standard selectors failed — trying text/JS-based discovery...")
                try:
                    # Strategy: find element whose text contains "Enable", "Set up", "Add"
                    # near an "Authenticator" or "2FA" heading
                    for text_kw in ["Enable", "Set up", "Add", "Turn on", "Activate"]:
                        xpath = (
                            f"//*[contains(text(),'{text_kw}') or contains(.,'{text_kw}')]"
                            f"[self::button or self::a or self::div or self::span]"
                        )
                        candidates = self.driver.find_elements(By.XPATH, xpath)
                        for candidate in candidates:
                            if candidate.is_displayed():
                                # Check if it's near an Authenticator section
                                try:
                                    parent = candidate.find_element(By.XPATH, "ancestor::*[contains(.,'Authenticator') or contains(.,'2FA') or contains(.,'app')]")
                                    if parent:
                                        enable_btn = candidate
                                        self.logger.info(f"Found enable element by text '{text_kw}' near Authenticator section")
                                        break
                                except Exception:
                                    # If no parent match, accept it as fallback
                                    enable_btn = candidate
                                    self.logger.info(f"Found enable element by text '{text_kw}' (fallback)")
                                    break
                        if enable_btn:
                            break
                except Exception as e:
                    self.logger.debug(f"Text-based search failed: {e}")

            if not enable_btn:
                # Last resort: use JavaScript to find clickable elements
                self.logger.warning("DOM search failed — trying JavaScript discovery...")
                try:
                    js_candidates = self.driver.execute_script("""
                        const results = [];
                        const keywords = ['enable', 'set up', 'add', 'turn on', 'activate'];
                        const all = document.querySelectorAll('button, a, div, span, [role="button"]');
                        for (const el of all) {
                            const text = (el.textContent || el.innerText || '').toLowerCase().trim();
                            if (keywords.some(kw => text.includes(kw))) {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    results.push({tag: el.tagName, text: el.textContent.trim().substring(0,60)});
                                }
                            }
                        }
                        return results;
                    """)
                    self.logger.warning(f"JS-discovered clickable elements: {js_candidates[:10]}")
                except Exception:
                    pass
                self.logger.warning("Could not find enable TOTP button — 2FA setup skipped")
                return None

            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", enable_btn)
            time.sleep(0.5)
            # Use JS click as fallback since it works even when Selenium click is blocked
            try:
                enable_btn.click()
                self.logger.info("Enable TOTP button clicked (native)")
            except Exception:
                self.driver.execute_script("arguments[0].click();", enable_btn)
                self.logger.info("Enable TOTP button clicked (JS fallback)")

            # Poll for 2FA setup UI to load (up to 15s) — sometimes takes longer
            self.logger.info("Waiting for authenticator setup UI to load...")
            show_secret = None
            poll_start = time.monotonic()
            while time.monotonic() - poll_start < 15:
                # --- Try selectors for "Show secret" button ---
                for selector in [
                    "#authentication-setup-show-secret",
                    "[data-testid='authentication-setup-show-secret']",
                    "button[id*='show-secret']",
                    "button[id*='secret']",
                    "[data-testid*='show']",
                    "[data-testid*='secret']",
                ]:
                    try:
                        candidates = self.driver.find_elements(By.CSS_SELECTOR, selector)
                        for candidate in candidates:
                            if candidate.is_displayed():
                                show_secret = candidate
                                self.logger.info(f"Found show-secret button with selector: {selector}")
                                break
                        if show_secret:
                            break
                    except Exception:
                        continue
                if show_secret:
                    break

                # Also try text-based keywords each poll cycle
                for text_kw in ["Show", "Reveal", "View", "Display", "Can't scan", "Manual", "Text"]:
                    try:
                        xpath = f"//*[contains(text(),'{text_kw}') or contains(.,'{text_kw}')][self::button or self::a or self::div or self::span]"
                        candidates = self.driver.find_elements(By.XPATH, xpath)
                        for candidate in candidates:
                            if candidate.is_displayed():
                                try:
                                    parent = candidate.find_element(By.XPATH, "ancestor::*[contains(.,'Authenticator') or contains(.,'secret') or contains(.,'setup')]")
                                    if parent:
                                        show_secret = candidate
                                        self.logger.info(f"Found show-secret element by text '{text_kw}'")
                                        break
                                except Exception:
                                    show_secret = candidate
                                    self.logger.info(f"Found show-secret element by text '{text_kw}' (fallback)")
                                    break
                            if show_secret:
                                break
                        if show_secret:
                            break
                    except Exception:
                        continue

                if show_secret:
                    break
                time.sleep(1)

            if not show_secret:
                self.logger.warning("Show secret button not found after 15s — 2FA setup incomplete")
                return None

            try:
                show_secret.click()
                self.logger.info("Show secret button clicked (native)")
            except Exception:
                self.driver.execute_script("arguments[0].click();", show_secret)
                self.logger.info("Show secret button clicked (JS fallback)")
            time.sleep(3)

            # --- Extract the secret key text ---
            self.logger.info("Extracting secret key...")
            secret_text = None
            for selector in [
                "#authentication-setup-secret-key",
                "[data-testid='authentication-setup-secret-key']",
            ]:
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if el.is_displayed():
                        secret_text = el.text.strip()
                        if secret_text:
                            break
                except Exception:
                    continue

            if not secret_text:
                self.logger.warning("Could not extract 2FA secret key")
                return None

            self.logger.info(f"Secret key extracted: {secret_text}")
            time.sleep(1)

            # --- Click QR button (switches back to QR/manual entry view) ---
            self.logger.info("Looking for QR button...")
            qr_btn = None
            for selector in [
                "[data-testid='authenticator-setup-qr-button']",
                "[data-testid='qr-button']",
                "button[id*='qr']",
            ]:
                try:
                    candidates = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for candidate in candidates:
                        if candidate.is_displayed():
                            qr_btn = candidate
                            break
                    if qr_btn:
                        break
                except Exception:
                    continue

            if qr_btn:
                qr_btn.click()
                time.sleep(2)
            else:
                self.logger.warning("QR button not found — continuing with manual entry")

            # --- Generate TOTP code from the secret ---
            totp = pyotp.TOTP(secret_text)
            totp_code = totp.now()
            self.logger.info(f"Generated TOTP code: {totp_code}")

            # --- Enter the TOTP code ---
            self.logger.info("Entering TOTP code...")
            code_input = None
            for selector in [
                ".css-19bq5da",
                "input[id*='code']",
                "input[placeholder*='code']",
                "input[type='text']",
            ]:
                try:
                    candidates = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for candidate in candidates:
                        if candidate.is_displayed():
                            code_input = candidate
                            break
                    if code_input:
                        break
                except Exception:
                    continue

            if not code_input:
                self.logger.warning("TOTP code input not found")
                return None

            code_input.clear()
            for char in totp_code:
                code_input.send_keys(char)
                time.sleep(0.12)
            time.sleep(1)

            # --- Click submit button ---
            self.logger.info("Looking for submit button...")
            submit_btn = None
            for selector in [
                "[data-testid='authentication-setup-qr-code-submit-button']",
                "[data-testid='submit-button']",
                "button[type='submit']",
                "button[id*='submit']",
            ]:
                try:
                    candidates = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for candidate in candidates:
                        if candidate.is_displayed():
                            submit_btn = candidate
                            break
                    if submit_btn:
                        break
                except Exception:
                    continue

            if not submit_btn:
                self.logger.warning("Submit button not found — 2FA may already be set up")
                # Even if we can't submit, return the secret so it's not lost
                return models.TwoFactorAuth(setup_key=secret_text, backup_codes=[])

            submit_btn.click()
            time.sleep(3)

            self.logger.success("2FA successfully linked")

            # Generate backup codes (best effort)
            backup_codes: list[str] = []
            try:
                backup_els = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='backup-code']")
                backup_codes = [el.text.strip() for el in backup_els if el.text.strip()]
                if backup_codes:
                    self.logger.info(f"Extracted {len(backup_codes)} backup codes")
            except Exception:
                pass

            return models.TwoFactorAuth(setup_key=secret_text, backup_codes=backup_codes)

        except Exception as e:
            self.logger.warning(f"Error setting up 2FA: {e}")
            return None
