import time
import requests
import subprocess
import socket
import threading
from loguru import logger
from typing import Optional, Dict, Any

class CamofoxClient:
    """Client for interacting with camofox-browser API"""
    
    # Class-level variables for thread-safe startup
    _startup_lock = threading.Lock()
    _is_starting = False
    _startup_complete = False
    
    def __init__(self, base_url: str = "http://localhost:9377", timeout: int = 60, camofox_path: str = None, auto_start: bool = True):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.logger = logger.bind(component="CamofoxClient")
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.camofox_path = camofox_path
        self.camofox_process = None
        
        # Auto-start camofox if enabled and not running (with thread safety)
        if auto_start:
            self._ensure_camofox_running()
    
    def _is_camofox_running(self) -> bool:
        """Check if camofox server is running by checking if the port is open"""
        try:
            # Extract port from base_url (default 9377)
            port = 9377
            if ":" in self.base_url:
                port = int(self.base_url.split(":")[-1])
            
            # Try to connect to the port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', port))
            sock.close()
            return result == 0
        except Exception as e:
            self.logger.debug(f"Error checking if camofox is running: {e}")
            return False
    
    def _is_camofox_ready(self) -> bool:
        """Check if camofox server is ready to handle requests by making a simple HTTP request"""
        try:
            # Try to get the OpenAPI spec which should be available when server is ready
            response = self.session.get(
                f"{self.base_url}/openapi.json",
                timeout=5
            )
            return response.status_code == 200
        except Exception as e:
            self.logger.debug(f"Camofox not ready yet: {e}")
            return False
    
    def _ensure_camofox_running(self) -> None:
        """Ensure camofox is running with thread-safe startup"""
        if self._is_camofox_running():
            self.logger.debug("Camofox is already running")
            return
        
        # Use class-level lock to prevent multiple threads from starting camofox simultaneously
        with CamofoxClient._startup_lock:
            # Double-check after acquiring lock (another thread might have started it)
            if self._is_camofox_running():
                self.logger.debug("Camofox was started by another thread")
                CamofoxClient._startup_complete = True
                return
            
            if CamofoxClient._is_starting:
                # Another thread is already starting camofox, wait for it to complete
                self.logger.info("Another thread is starting camofox, waiting...")
                max_wait = 60  # seconds
                wait_interval = 1
                waited = 0
                while waited < max_wait:
                    if CamofoxClient._startup_complete:
                        self.logger.info("Camofox startup completed by another thread")
                        return
                    time.sleep(wait_interval)
                    waited += wait_interval
                self.logger.error("Timeout waiting for camofox startup by another thread")
                return
            
            # This thread will start camofox
            CamofoxClient._is_starting = True
            try:
                self._start_camofox()
                CamofoxClient._startup_complete = True
            finally:
                CamofoxClient._is_starting = False
    
    def _start_camofox(self) -> None:
        """Start the camofox server"""
        if not self.camofox_path:
            self.logger.warning("No camofox_path provided, skipping auto-start")
            return
        
        try:
            self.logger.info(f"Starting camofox server at {self.camofox_path}")
            
            # Start camofox as a subprocess
            self.camofox_process = subprocess.Popen(
                ["npm", "start"],
                cwd=self.camofox_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True
            )
            
            # Wait for camofox to start and be ready
            max_wait = 60  # seconds (increased to allow for full initialization)
            wait_interval = 2
            waited = 0
            
            while waited < max_wait:
                if self._is_camofox_running():
                    self.logger.debug("Camofox port is open, checking if ready...")
                    # Additional check to ensure camofox is actually ready to handle requests
                    if self._is_camofox_ready():
                        self.logger.info("Camofox server started successfully and is ready")
                        return
                    else:
                        self.logger.debug(f"Camofox port is open but not ready yet... ({waited}/{max_wait}s)")
                else:
                    self.logger.debug(f"Waiting for camofox to start... ({waited}/{max_wait}s)")
                time.sleep(wait_interval)
                waited += wait_interval
            
            self.logger.error("Camofox server failed to start within timeout")
            
        except Exception as e:
            self.logger.error(f"Failed to start camofox: {e}")
            CamofoxClient._startup_complete = False
    
    def stop_camofox(self) -> None:
        """Stop the camofox server if it was started by this client"""
        if self.camofox_process:
            try:
                self.logger.info("Stopping camofox server")
                self.camofox_process.terminate()
                self.camofox_process.wait(timeout=10)
                self.logger.info("Camofox server stopped")
                CamofoxClient._startup_complete = False
            except Exception as e:
                self.logger.error(f"Failed to stop camofox: {e}")
            finally:
                self.camofox_process = None

    def create_tab(self, user_id: str, session_key: str, url: str = None, retry_count: int = 8) -> Dict[str, Any]:
        """Create a new browser tab with retry logic and exponential backoff"""
        for attempt in range(retry_count):
            try:
                payload = {"userId": user_id, "sessionKey": session_key}
                if url:
                    payload["url"] = url

                response = self.session.post(
                    f"{self.base_url}/tabs",
                    json=payload,
                    timeout=self.timeout
                )

                # Handle 503 errors specifically with longer backoff
                if response.status_code == 503:
                    self.logger.warning(f"Attempt {attempt + 1}/{retry_count}: Server busy (503), retrying...")
                    if attempt < retry_count - 1:
                        # Exponential backoff: 2, 4, 8, 16, 32 seconds
                        backoff_time = min(2 ** attempt, 32)
                        self.logger.info(f"Waiting {backoff_time}s before retry...")
                        time.sleep(backoff_time)
                        continue
                    else:
                        raise Exception("Server busy after multiple retries")

                response.raise_for_status()
                data = response.json()
                self.logger.info(f"Created tab: {data.get('tabId')} for user: {user_id}")
                return data
            except Exception as e:
                self.logger.warning(f"Attempt {attempt + 1}/{retry_count}: Failed to create tab: {e}")
                if attempt < retry_count - 1:
                    # Exponential backoff for general errors
                    backoff_time = min(2 ** attempt, 16)
                    self.logger.info(f"Waiting {backoff_time}s before retry...")
                    time.sleep(backoff_time)
                else:
                    self.logger.error(f"Failed to create tab after {retry_count} attempts: {e}")
                    raise
    
    def get_snapshot(self, tab_id: str, user_id: str, retry_count: int = 3) -> Dict[str, Any]:
        """Get accessibility snapshot of the current page with retry logic"""
        for attempt in range(retry_count):
            try:
                response = self.session.get(
                    f"{self.base_url}/tabs/{tab_id}/snapshot",
                    params={"userId": user_id},
                    timeout=self.timeout
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                self.logger.warning(f"Attempt {attempt + 1}/{retry_count}: Failed to get snapshot: {e}")
                if attempt < retry_count - 1:
                    backoff_time = min(2 ** attempt, 10)
                    self.logger.info(f"Waiting {backoff_time}s before retry...")
                    time.sleep(backoff_time)
                else:
                    self.logger.error(f"Failed to get snapshot after {retry_count} attempts: {e}")
                    raise
    
    def click(self, tab_id: str, user_id: str, ref: str = None, selector: str = None) -> Dict[str, Any]:
        """Click an element by ref or selector"""
        try:
            payload = {"userId": user_id}
            if ref:
                payload["ref"] = ref
            if selector:
                payload["selector"] = selector
            
            response = self.session.post(
                f"{self.base_url}/tabs/{tab_id}/click",
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.error(f"Failed to click element: {e}")
            raise
    
    def type(self, tab_id: str, user_id: str, text: str, ref: str = None, selector: str = None, press_enter: bool = False) -> Dict[str, Any]:
        """Type text into an element"""
        try:
            payload = {"userId": user_id, "text": text, "pressEnter": press_enter}
            if ref:
                payload["ref"] = ref
            if selector:
                payload["selector"] = selector
            
            response = self.session.post(
                f"{self.base_url}/tabs/{tab_id}/type",
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.error(f"Failed to type text: {e}")
            raise
    
    def navigate(self, tab_id: str, user_id: str, url: str) -> Dict[str, Any]:
        """Navigate to a URL"""
        try:
            payload = {"userId": user_id, "url": url}
            response = self.session.post(
                f"{self.base_url}/tabs/{tab_id}/navigate",
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.error(f"Failed to navigate: {e}")
            raise
    
    def scroll(self, tab_id: str, user_id: str, direction: str = "down", amount: int = 500) -> Dict[str, Any]:
        """Scroll the page"""
        try:
            payload = {"userId": user_id, "direction": direction, "amount": amount}
            response = self.session.post(
                f"{self.base_url}/tabs/{tab_id}/scroll",
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.error(f"Failed to scroll: {e}")
            raise
    
    def close_tab(self, tab_id: str, user_id: str, retry_count: int = 3) -> Dict[str, Any]:
        """Close a tab with retry logic"""
        for attempt in range(retry_count):
            try:
                response = self.session.delete(
                    f"{self.base_url}/tabs/{tab_id}",
                    params={"userId": user_id},
                    timeout=self.timeout
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                self.logger.warning(f"Attempt {attempt + 1}/{retry_count}: Failed to close tab: {e}")
                if attempt < retry_count - 1:
                    backoff_time = min(2 ** attempt, 10)
                    self.logger.info(f"Waiting {backoff_time}s before retry...")
                    time.sleep(backoff_time)
                else:
                    self.logger.error(f"Failed to close tab after {retry_count} attempts: {e}")
                    raise
    
    def find_ref_by_text(self, snapshot: Dict[str, str], search_text: str) -> Optional[str]:
        """Find an element ref by searching for text in the snapshot"""
        snapshot_text = snapshot.get("snapshot", "")
        lines = snapshot_text.split("\n")
        
        # Try exact match first
        for i, line in enumerate(lines):
            if search_text.lower() in line.lower():
                # Extract ref from line like: "button 'Continue' [e1]" or "textbox 'Day' [e5]"
                import re
                match = re.search(r'\[([a-z]\d+)\]', line)
                if match:
                    return match.group(1)
                
                # If no ref in current line, check next line (input fields often follow labels)
                if i + 1 < len(lines):
                    next_line = lines[i + 1]
                    match = re.search(r'\[([a-z]\d+)\]', next_line)
                    if match:
                        return match.group(1)
                    
                    # Check next few lines as well
                    for j in range(2, 4):
                        if i + j < len(lines):
                            next_line = lines[i + j]
                            match = re.search(r'\[([a-z]\d+)\]', next_line)
                            if match:
                                return match.group(1)
        
        # Try partial matches and alternative patterns
        search_variations = [
            search_text,
            search_text.split()[0] if " " in search_text else search_text,  # First word
            search_text[:4],  # First 4 characters
            search_text[:3],  # First 3 characters
        ]
        
        for variation in search_variations:
            for i, line in enumerate(lines):
                if variation.lower() in line.lower():
                    import re
                    match = re.search(r'\[([a-z]\d+)\]', line)
                    if match:
                        return match.group(1)
                    if i + 1 < len(lines):
                        next_line = lines[i + 1]
                        match = re.search(r'\[([a-z]\d+)\]', next_line)
                        if match:
                            return match.group(1)
        
        # Fallback: look for any input element if searching for form fields
        if any(word in search_text.lower() for word in ["day", "month", "year", "date", "birth"]):
            import re
            for line in lines:
                if any(field_type in line.lower() for field_type in ["textbox", "combobox", "number"]):
                    match = re.search(r'\[([a-z]\d+)\]', line)
                    if match:
                        return match.group(1)
        
        return None
    
    def find_ref_by_selector_hint(self, snapshot: Dict[str, str], hints: list) -> Optional[str]:
        """Find an element ref by searching for selector hints in the snapshot"""
        snapshot_text = snapshot.get("snapshot", "")
        
        for hint in hints:
            ref = self.find_ref_by_text(snapshot, hint)
            if ref:
                return ref
        
        return None
    
    def wait_for_element(self, tab_id: str, user_id: str, search_text: str, timeout: int = 30) -> Optional[str]:
        """Wait for an element to appear in the snapshot"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                snapshot = self.get_snapshot(tab_id, user_id)
                ref = self.find_ref_by_text(snapshot, search_text)
                if ref:
                    return ref
                time.sleep(1)
            except Exception as e:
                self.logger.debug(f"Error waiting for element: {e}")
                time.sleep(1)
        
        self.logger.error(f"Timeout waiting for element: {search_text}")
        return None