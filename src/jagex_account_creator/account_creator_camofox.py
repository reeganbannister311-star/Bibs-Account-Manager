import base64
import random
import re
import threading
import time
from datetime import timedelta
from pathlib import Path

import pyotp
import wreq
from imap_tools import AND, MailBox
from loguru import logger
from wreq.blocking import Client

from . import models, utils
from .camofox_client import CamofoxClient
from .gproxy import GProxy



class ElementNotFoundError(Exception):
    """Raised when a required element cannot be found."""
    pass


class RegistrationError(Exception):
    """An error that occurred during account registration."""
    pass


class AccountCreatorCamofox:
    """Account creator using camofox-browser API instead of Chrome"""

    _REGISTRATION_URL = "https://account.jagex.com/en-GB/login/registration-start"
    _MANAGEMENT_URL = "https://account.jagex.com/en-GB/manage/profile"

    def __init__(
        self,
        wreq_client: Client,
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
        camofox_base_url: str = "http://localhost:9377",
        camofox_path: str = None,
    ) -> None:
        self.run_id = run_id or utils.generate_string(include_punctuation=False)
        self.logger = logger.bind(module="AccountCreatorCamofox", uid=self.run_id)

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
            self.wreq_client = wreq_client or utils.setup_wreq_client(
                user_agent=self.user_agent,
                timeout_seconds=self.element_wait_timeout,
            )
            if self.proxy:
                self.wreq_proxy = self.proxy.to_wreq()
            else:
                self.wreq_proxy = None

        # Initialize camofox client with auto-start capability
        self.camofox_client = CamofoxClient(
            base_url=camofox_base_url, 
            timeout=element_wait_timeout,
            camofox_path=camofox_path,
            auto_start=camofox_path is not None
        )
        self.logger.info(f"Initialized camofox client at {camofox_base_url}")

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
                    # Use email subject/title instead of parsing HTML
                    mail_subject = email.subject
                    self.logger.debug(f"Email subject: {mail_subject}")
                    # Extract code from subject (first word/token)
                    if mail_subject:
                        code = mail_subject.split()[0].strip()
                        self.logger.debug(f"Returning verification code from subject: {code}")
                        return code
                time.sleep(0.5)
        raise RegistrationError("Timed out waiting for registration code.")

    def _get_verification_code_gmail_web(
        self, gmail_web_details: models.GmailWebDetails, email_username: str, timeout_seconds: int = 120
    ) -> str:
        """Gets the verification code from Gmail using a browser to log into Gmail web."""
        self.logger.info("Opening Gmail browser to retrieve verification code...")

        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")

        gmail_driver = uc.Chrome(options=options)
        gmail_driver.set_page_load_timeout(30)

        try:
            gmail_driver.get("https://gmail.com")
            time.sleep(3)

            current_url = gmail_driver.current_url
            if "inbox" in current_url or "mail.google.com/mail" in current_url:
                self.logger.info("Already logged into Gmail")
            else:
                self.logger.info("Logging into Gmail...")
                try:
                    email_input = WebDriverWait(gmail_driver, 10).until(
                        EC.presence_of_element_located((By.ID, "identifierId"))
                    )
                    email_input.clear()
                    email_input.send_keys(gmail_web_details.email)
                    gmail_driver.find_element(By.ID, "identifierNext").click()
                    time.sleep(2)
                except Exception:
                    pass

                try:
                    password_input = WebDriverWait(gmail_driver, 10).until(
                        EC.presence_of_element_located((By.NAME, "Passwd"))
                    )
                    password_input.clear()
                    password_input.send_keys(gmail_web_details.password)
                    gmail_driver.find_element(By.ID, "passwordNext").click()
                    time.sleep(5)
                except Exception as e:
                    raise RegistrationError(f"Gmail login failed: {e}")

                time.sleep(5)

            search_url = f"https://mail.google.com/mail/u/0/#search/to:{email_username}"
            gmail_driver.get(search_url)
            self.logger.info(f"Searching Gmail for emails to: {email_username}")
            time.sleep(6)

            timeout = time.monotonic() + timeout_seconds
            check_count = 0
            while time.monotonic() < timeout:
                check_count += 1
                try:
                    if check_count > 1:
                        gmail_driver.refresh()
                        time.sleep(4)

                    page_text = gmail_driver.find_element(By.TAG_NAME, "body").text
                    lines = page_text.split('\n')
                    for line in lines:
                        line_upper = line.upper()
                        if "JAGEX" in line_upper or "VERIFY" in line_upper or "CODE" in line_upper:
                            code_match = re.search(r"\b[A-Z0-9]{4,8}\b", line_upper)
                            if code_match:
                                code = code_match.group(0)
                                self.logger.info(f"Found verification code in Gmail: {code}")
                                return code

                    for line in lines:
                        if email_username.lower() in line.lower():
                            idx = lines.index(line)
                            for offset in range(-3, 4):
                                check_idx = idx + offset
                                if 0 <= check_idx < len(lines):
                                    code_match = re.search(r"\b[A-Z0-9]{4,8}\b", lines[check_idx].upper())
                                    if code_match:
                                        code = code_match.group(0)
                                        self.logger.info(f"Found verification code: {code}")
                                        return code
                except Exception as e:
                    self.logger.debug(f"Gmail web check error: {e}")
                time.sleep(5)

            raise RegistrationError("Timed out waiting for verification code in Gmail web.")
        finally:
            try:
                gmail_driver.quit()
            except Exception:
                pass

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
        self.logger.debug(f"Response: {get_email_resp}")
        get_email_resp.raise_for_status()

        sid_token = get_email_resp.json()["sid_token"]

        # Guerrilla Mail API has an issue with the case of our username
        # when getting email via the API, even though it seems fine on the site..
        email_username = email_username.lower()

        self.logger.debug(f"Sending request to set Guerrilla Mail email to: {email_username}.")
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
        self.logger.debug(f"Response: {set_email_resp}")
        set_email_resp.raise_for_status()

        if email_username not in set_email_resp.json()["email_addr"]:
            raise RegistrationError("Failed to set account email on Guerrilla Mail.")

        timeout = time.monotonic() + timeout_seconds
        while time.monotonic() < timeout:
            self.logger.debug("Sending request to check our email.")
            check_email_resp = self.wreq_client.get(
                url=guerrilla_mail_api_url,
                query={"f": "check_email", "sid_token": sid_token, "seq": 0},
                proxy=self.wreq_proxy,
            )
            self.logger.debug(f"Response: {check_email_resp}")
            check_email_resp.raise_for_status()

            for email in check_email_resp.json()["list"]:
                if email["mail_from"] != "no-reply@contact.jagex.com":
                    continue
                mail_subject: str = email["mail_subject"]
                code = mail_subject.split()[0]
                self.logger.debug(f"Returning verification code: {code}")
                return code
            time.sleep(1)
        raise RegistrationError("Timed out waiting for registration code.")

    def _get_verification_code_xitroo(self, account_email: str, timeout_seconds: int = 60) -> str:
        """Get account verification code via xitroo temp mail api."""
        self.logger.debug("Getting verification code via xitroo.")
        timeout = time.monotonic() + timeout_seconds
        while time.monotonic() < timeout:
            self.logger.debug("Sending request to check our email.")
            check_email_resp = self.wreq_client.get(
                url="https://api.xitroo.com/v1/mails",
                query={
                    "locale": "en",
                    "mailAddress": account_email,
                    "mailsPerPage": "1",
                    "minTimestamp": str(time.time() - timeout_seconds),
                    "maxTimestamp": str(time.time() + timeout_seconds),
                },
                proxy=self.wreq_proxy,
            )
            self.logger.debug(f"Response: {check_email_resp}")
            check_email_resp.raise_for_status()

            emails = check_email_resp.json().get("mails", [])
            self.logger.debug(f"Emails: {len(emails)}")
            for email in emails:
                if email["from"] != "Jagex <no-reply@contact.jagex.com>":
                    continue
                mail_subject: str = base64.b64decode(email["subject"]).decode("utf-8")
                code = mail_subject.split()[0]
                self.logger.debug(f"Returning verification code: {code}")
                return code
            time.sleep(1)
        raise RegistrationError("Timed out waiting for registration code.")

    def _find_and_click_text(self, tab_id: str, user_id: str, search_text: str, retry_count: int = 3) -> str:
        """Find element by text and click it"""
        for attempt in range(retry_count):
            try:
                ref = self.camofox_client.wait_for_element(tab_id, user_id, search_text, timeout=10)
                if ref:
                    self.logger.info(f"Found element with text '{search_text}' with ref: {ref}")
                    self.camofox_client.click(tab_id, user_id, ref=ref)
                    time.sleep(0.5)  # Wait for click to register
                    return ref
                else:
                    self.logger.warning(f"Attempt {attempt + 1}: Could not find element with text '{search_text}'")
                    time.sleep(1)
            except Exception as e:
                self.logger.warning(f"Attempt {attempt + 1}: Error finding/clicking element: {e}")
                time.sleep(1)
        
        raise ElementNotFoundError(f"Could not find element with text '{search_text}' after {retry_count} attempts")

    def _find_and_type_text(self, tab_id: str, user_id: str, search_text: str, typing_text: str, retry_count: int = 3) -> str:
        """Find input field by text and type into it"""
        for attempt in range(retry_count):
            try:
                ref = self.camofox_client.wait_for_element(tab_id, user_id, search_text, timeout=10)
                if ref:
                    self.logger.info(f"Found input field with text '{search_text}' with ref: {ref}")
                    self.camofox_client.click(tab_id, user_id, ref=ref)
                    time.sleep(0.3)  # Wait for focus
                    self.camofox_client.type(tab_id, user_id, text=typing_text, ref=ref, press_enter=False)
                    time.sleep(0.3)  # Wait for typing to complete
                    return ref
                else:
                    self.logger.warning(f"Attempt {attempt + 1}: Could not find input field with text '{search_text}'")
                    time.sleep(1)
            except Exception as e:
                self.logger.warning(f"Attempt {attempt + 1}: Error finding/typing in element: {e}")
                time.sleep(1)
        
        raise ElementNotFoundError(f"Could not find input field with text '{search_text}' after {retry_count} attempts")

    def _submit_account_username(self, tab_id: str, user_id: str, timeout_seconds: int = 30) -> str:
        """Wrapper function to loop the username submission with camofox"""
        timeout = time.monotonic() + timeout_seconds
        while time.monotonic() < timeout:
            try:
                # Try to find the display name field
                ref = self.camofox_client.wait_for_element(tab_id, user_id, "Display name", timeout=5)
                if not ref:
                    self.logger.warning("Couldn't find display name field, retrying...")
                    time.sleep(1)
                    continue

                account_username = utils.generate_string(include_punctuation=False, length=12)
                self.logger.info(f"Attempting to submit account username: {account_username}")

                # Clear the field by selecting all and typing
                self.camofox_client.click(tab_id, user_id, ref=ref)
                time.sleep(0.2)
                self.camofox_client.type(tab_id, user_id, text="", ref=ref)  # Clear by typing empty
                time.sleep(0.2)
                self.camofox_client.type(tab_id, user_id, text=account_username, ref=ref)
                
                # Click continue button
                continue_ref = self.camofox_client.wait_for_element(tab_id, user_id, "Continue", timeout=5)
                if continue_ref:
                    self.camofox_client.click(tab_id, user_id, ref=continue_ref)
                    time.sleep(2)  # Wait for navigation
                
                # Check if we moved past the username page
                snapshot = self.camofox_client.get_snapshot(tab_id, user_id)
                if "Display name" not in snapshot.get("snapshot", ""):
                    self.logger.info(f"Username accepted: {account_username}")
                    return account_username
                
                self.logger.debug("Retrying username submission.")
                time.sleep(1)
                
            except Exception as e:
                self.logger.warning(f"Error in username submission: {e}")
                time.sleep(1)

        raise RegistrationError("Timed out submitting account username.")

    def _handle_registration(self, tab_id: str, user_id: str) -> models.JagexAccount:
        """Do the account registration flow using camofox-browser"""
        self.logger.info("Starting registration flow with camofox-browser")

        account_birthday = models.Birthday(
            day=random.randint(1, 25),
            month=random.randint(1, 12),
            year=random.randint(1979, 2010),
        )

        base_email = None
        if self.mail_provider == models.MailProvider.IMAP and self.imap_details:
            base_email = self.imap_details.email
        elif self.mail_provider == models.MailProvider.GMAIL_WEB and self.gmail_web_details:
            base_email = self.gmail_web_details.email
        
        if base_email:
            base_username = base_email.split('@')[0]
            tag = utils.generate_string(include_punctuation=False, length=8)
            username = f"{base_username}+{tag}"
            account_email = models.Email(
                username=username,
                domain=self.account_email_domain,
            )
        else:
            account_email = models.Email(
                username=utils.generate_string(include_punctuation=False, length=12),
                domain=self.account_email_domain,
            )

        self.logger.info(f"Going to registration url: {self._REGISTRATION_URL}")
        self.camofox_client.navigate(tab_id, user_id, self._REGISTRATION_URL)
        time.sleep(3)  # Wait for page to load

        # Check if blocked with multiple variations of CF error messages
        # Add retry logic for Cloudflare blocking
        max_cf_retries = 3
        for cf_attempt in range(max_cf_retries):
            snapshot = self.camofox_client.get_snapshot(tab_id, user_id)
            snapshot_text = snapshot.get("snapshot", "").lower()
            cf_errors = [
                "sorry, you have been blocked",
                "too many requests",
                "access denied",
                "cloudflare",
                "error 1020",
                "challenge required"
            ]
            if any(msg in snapshot_text for msg in cf_errors):
                self.logger.warning(f"Cloudflare protection detected (attempt {cf_attempt + 1}/{max_cf_retries}). IP may be blocked.")
                if cf_attempt < max_cf_retries - 1:
                    self.logger.info("Waiting 10 seconds and retrying...")
                    time.sleep(10)
                    # Refresh the page
                    self.camofox_client.navigate(tab_id, user_id, self._REGISTRATION_URL)
                    time.sleep(3)
                    continue
                else:
                    self.logger.error("Cloudflare protection detected after multiple retries. IP likely blocked.")
                    raise RegistrationError("IP is blocked by CF. Try different proxy or wait longer.")
            else:
                # No Cloudflare error, proceed
                break

        # Fill in registration form
        self.logger.info("Filling registration form")
        
        # Email field
        self._find_and_type_text(tab_id, user_id, "Email address", account_email.address)
        
        # Birthday fields - try multiple approaches to find date fields
        self.logger.info("Filling birthday fields")

        # First, take a snapshot to see what's available
        snapshot = self.camofox_client.get_snapshot(tab_id, user_id)
        snapshot_text = snapshot.get("snapshot", "")
        self.logger.debug(f"Page snapshot for birthday fields: {snapshot_text[:500]}")

        # Try different variations for the birthday fields with extended timeout
        day_found = False
        month_found = False
        year_found = False

        # Try text-based search first with longer timeout
        for day_text in ["Day", "day", "Date of birth", "birth", "DOB", "date", "Date", "Birthday"]:
            try:
                ref = self.camofox_client.wait_for_element(tab_id, user_id, day_text, timeout=8)
                if ref:
                    self.logger.info(f"Found Day field with search text: {day_text}, ref: {ref}")
                    self.camofox_client.click(tab_id, user_id, ref=ref)
                    time.sleep(0.3)
                    self.camofox_client.type(tab_id, user_id, text=str(account_birthday.day), ref=ref)
                    day_found = True

                    # Try to find month and year nearby
                    for month_text in ["Month", "month"]:
                        try:
                            ref_month = self.camofox_client.wait_for_element(tab_id, user_id, month_text, timeout=3)
                            if ref_month:
                                self.camofox_client.click(tab_id, user_id, ref=ref_month)
                                time.sleep(0.3)
                                self.camofox_client.type(tab_id, user_id, text=str(account_birthday.month), ref=ref_month)
                                month_found = True
                                break
                        except:
                            continue

                    for year_text in ["Year", "year"]:
                        try:
                            ref_year = self.camofox_client.wait_for_element(tab_id, user_id, year_text, timeout=3)
                            if ref_year:
                                self.camofox_client.click(tab_id, user_id, ref=ref_year)
                                time.sleep(0.3)
                                self.camofox_client.type(tab_id, user_id, text=str(account_birthday.year), ref=ref_year)
                                year_found = True
                                break
                        except:
                            continue
                    break
            except Exception as e:
                self.logger.debug(f"Attempt with '{day_text}' failed: {e}")
                continue

        # Always initialize input_refs for fallback logic
        input_refs = []
        lines = snapshot_text.split("\n")
        for i, line in enumerate(lines):
            if any(indicator in line.lower() for indicator in ["textbox", "combobox", "number", "spinbutton", "input"]):
                import re
                match = re.search(r'\[([a-z]\d+)\]', line)
                if match:
                    # Also try to identify if this might be a date field based on nearby text
                    context = ""
                    if i > 0:
                        context += lines[i-1] + " "
                    context += line
                    if i + 1 < len(lines):
                        context += lines[i+1]

                    input_refs.append((i, match.group(1), context.lower()))

        self.logger.info(f"Found {len(input_refs)} input fields")
        
        # If text search failed, try using detected input fields
        if not day_found and len(input_refs) >= 3:
            self.logger.info("Trying to use detected input fields for birthday")
            try:
                # Sort input refs by their context to identify day/month/year fields
                # Look for keywords in the context
                day_ref = None
                month_ref = None
                year_ref = None
                
                for idx, (line_num, ref, context) in enumerate(input_refs):
                    if any(keyword in context for keyword in ["day", "day of"]):
                        day_ref = ref
                    elif any(keyword in context for keyword in ["month"]):
                        month_ref = ref
                    elif any(keyword in context for keyword in ["year"]):
                        year_ref = ref
                    elif day_ref is None and idx == 0:  # First field as fallback
                        day_ref = ref
                    elif month_ref is None and idx == 1:  # Second field as fallback
                        month_ref = ref
                    elif year_ref is None and idx == 2:  # Third field as fallback
                        year_ref = ref
                
                # Fill the fields
                if day_ref:
                    self.camofox_client.click(tab_id, user_id, ref=day_ref)
                    time.sleep(0.3)
                    self.camofox_client.type(tab_id, user_id, text=str(account_birthday.day), ref=day_ref)
                    day_found = True
                
                if month_ref:
                    self.camofox_client.click(tab_id, user_id, ref=month_ref)
                    time.sleep(0.3)
                    self.camofox_client.type(tab_id, user_id, text=str(account_birthday.month), ref=month_ref)
                    month_found = True
                
                if year_ref:
                    self.camofox_client.click(tab_id, user_id, ref=year_ref)
                    time.sleep(0.3)
                    self.camofox_client.type(tab_id, user_id, text=str(account_birthday.year), ref=year_ref)
                    year_found = True
                
                self.logger.info("Successfully filled birthday fields using input field detection")
                
            except Exception as e:
                self.logger.error(f"Failed to fill birthday fields via input detection: {e}")
        
        if not (day_found and month_found and year_found):
            self.logger.warning(f"Birthday fields not fully filled: day={day_found}, month={month_found}, year={year_found}")
        
        time.sleep(1)  # Wait for form to process
        
        # Accept terms
        self._find_and_click_text(tab_id, user_id, "I agree")
        
        # Click continue
        self._find_and_click_text(tab_id, user_id, "Continue")
        time.sleep(3)  # Wait for navigation

        # Get verification code from email using the SAME email that was used for account creation
        self.logger.info(f"Getting verification code from {self.mail_provider} for email: {account_email.address}")
        if self.mail_provider == models.MailProvider.IMAP:
            code = self._get_verification_code_imap(
                imap_details=self.imap_details, email_username=account_email.username
            )
        elif self.mail_provider == models.MailProvider.GMAIL_WEB:
            code = self._get_verification_code_gmail_web(
                gmail_web_details=self.gmail_web_details, email_username=account_email.address
            )
        elif self.mail_provider == models.MailProvider.GUERRILLA_MAIL:
            code = self._get_verification_code_guerrilla_mail(email_username=account_email.username)
        elif self.mail_provider == models.MailProvider.XITROO:
            code = self._get_verification_code_xitroo(account_email=account_email.address)
        else:
            raise RegistrationError(f"Unsupported mail provider: {self.mail_provider}")

        self.logger.info(f"Verification code retrieved for email {account_email.address}: {code}")

        self.logger.info(f"Got verification code: {code}")

        # Enter verification code
        self.logger.info("Entering verification code")

        # Wait for verification page to load
        time.sleep(2)
        snapshot = self.camofox_client.get_snapshot(tab_id, user_id)
        self.logger.debug(f"Verification page snapshot: {snapshot.get('snapshot', '')[:200]}")

        # Try multiple approaches to find and fill the verification code field
        code_entered = False
        for attempt in range(5):
            try:
                # Try to find the verification code input field with longer timeout
                ref = self.camofox_client.wait_for_element(tab_id, user_id, "Verification code", timeout=10)
                if ref:
                    self.logger.info(f"Found verification code field with ref: {ref}")
                    self.camofox_client.click(tab_id, user_id, ref=ref)
                    time.sleep(0.5)
                    self.camofox_client.type(tab_id, user_id, text=code, ref=ref, press_enter=False)
                    time.sleep(1)

                    # Click continue button
                    continue_ref = self.camofox_client.wait_for_element(tab_id, user_id, "Continue", timeout=5)
                    if continue_ref:
                        self.camofox_client.click(tab_id, user_id, ref=continue_ref)

                    code_entered = True
                    break
                else:
                    # Try alternative approach - look for any input field
                    snapshot = self.camofox_client.get_snapshot(tab_id, user_id)
                    # Look for input fields in the snapshot
                    if "input" in snapshot.get("snapshot", "").lower():
                        # Try to find input by looking for common patterns
                        for search_text in ["code", "enter", "verification", "Code", "Enter code"]:
                            ref = self.camofox_client.wait_for_element(tab_id, user_id, search_text, timeout=3)
                            if ref:
                                self.logger.info(f"Found verification field using search text: {search_text}")
                                self.camofox_client.click(tab_id, user_id, ref=ref)
                                time.sleep(0.5)
                                self.camofox_client.type(tab_id, user_id, text=code, ref=ref, press_enter=False)

                                continue_ref = self.camofox_client.wait_for_element(tab_id, user_id, "Continue", timeout=5)
                                if continue_ref:
                                    self.camofox_client.click(tab_id, user_id, ref=continue_ref)

                                code_entered = True
                                break
                        if code_entered:
                            break

                if not code_entered:
                    self.logger.warning(f"Attempt {attempt + 1}: Could not find verification code field")
                    time.sleep(2)

            except Exception as e:
                self.logger.warning(f"Attempt {attempt + 1}: Error entering verification code: {e}")
                time.sleep(2)

        if not code_entered:
            self.logger.error("Failed to enter verification code after multiple attempts")
            raise RegistrationError("Could not enter verification code")

        time.sleep(3)  # Wait for verification to process

        # Submit account username
        account_username = self._submit_account_username(tab_id, user_id)

        # Enter password
        self.logger.info("Entering password")
        self._find_and_type_text(tab_id, user_id, "Password", self.account_password)
        self._find_and_type_text(tab_id, user_id, "Confirm password", self.account_password)
        
        # Click create account button
        self._find_and_click_text(tab_id, user_id, "Create account")
        
        # Wait for completion
        self.logger.info("Waiting for registration completion")
        time.sleep(5)
        
        # Check if registration was successful
        snapshot = self.camofox_client.get_snapshot(tab_id, user_id)
        if "completed" in snapshot.get("snapshot", "").lower() or "success" in snapshot.get("snapshot", "").lower():
            self.logger.info("Registration appears to be successful")
        else:
            self.logger.warning("Registration completion status unclear")

        jagex_account = models.JagexAccount(
            email=account_email,
            username=account_username,
            password=self.account_password,
            birthday=account_birthday,
            real_ip="unknown",  # Camofox handles proxy internally
            proxy=self.proxy,
        )

        if self.set_2fa:
            self.logger.warning("2FA setup not yet implemented for camofox-browser")
            # TODO: Implement 2FA setup for camofox-browser

        self.logger.info("Registration finished")
        return jagex_account

    def register_account(self) -> models.AccountRegistrationResult:
        """Wrapper function to fully register a Jagex account using camofox-browser."""
        start_time = time.monotonic()
        
        # Generate unique user ID for this registration
        user_id = f"jagex-creator-{self.run_id}"
        session_key = f"session-{utils.generate_string(include_punctuation=False)}"
        
        self.logger.info(f"Starting camofox-browser registration with user_id: {user_id}")
        
        gproxy = GProxy(
            run_uid=self.run_id,
            upstream_proxy=self.proxy,
            allowed_hosts=["jagex", "cloudflare", "ipify"],
        )
        gproxy.start()

        success = False
        tab_id = None
        
        try:
            # Create tab with camofox-browser
            tab_data = self.camofox_client.create_tab(user_id, session_key, self._REGISTRATION_URL)
            tab_id = tab_data["tabId"]
            self.logger.info(f"Created camofox tab: {tab_id}")
            
            # Wait a bit for the tab to initialize
            time.sleep(2)
            
            account = self._handle_registration(tab_id, user_id)
            success = True
            
            return models.AccountRegistrationResult(
                jagex_account=account,
                transfer_stats=gproxy.transfer_stats,
                duration=timedelta(seconds=time.monotonic() - start_time),
            )
            
        except Exception as e:
            self.logger.error(f"Registration failed: {e}")
            raise RegistrationError(f"Registration failed: {e}")
            
        finally:
            # Cleanup
            if tab_id:
                try:
                    self.camofox_client.close_tab(tab_id, user_id)
                    self.logger.info(f"Closed camofox tab: {tab_id}")
                except Exception as e:
                    self.logger.warning(f"Error closing tab: {e}")
            
            gproxy.stop()
            self.logger.info("Registration process cleaned up")