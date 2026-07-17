import platform
from collections import namedtuple
try:
    UnameResult = namedtuple('uname_result', ['system', 'node', 'release', 'version', 'machine', 'processor'])
    platform.uname = lambda: UnameResult('Windows', 'DESKTOP', '10', '10.0.19045', 'AMD64', 'Intel64 Family 6 Model 158 Stepping 10, GenuineIntel')
    platform.system = lambda: 'Windows'
    platform.machine = lambda: 'AMD64'
    platform.processor = lambda: 'Intel64 Family 6 Model 158 Stepping 10, GenuineIntel'
    platform.win32_ver = lambda *args, **kwargs: ('10', '10.0.19045', '', '')
except Exception:
    pass

import os
import sys

# Check for local staged yt-dlp updates and inject into sys.path at the absolute top of the process
try:
    import json
    settings_path = os.path.join(os.path.abspath("."), "settings.json")
    if os.path.exists(settings_path):
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
        override_path = settings.get('ytdlp_override_path')
        if override_path and os.path.exists(override_path):
            parent_dir = os.path.dirname(override_path)
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)
                print(f"[Startup] Injected updated yt-dlp from: {parent_dir}")
except Exception as e:
    print(f"Error injecting local yt-dlp: {e}")

# Configure UTF-8 encoding for system stdout/stderr to prevent UnicodeEncodeError crashes on Windows
try:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# Enforce Single Instance Lock on Windows and Set DPI Awareness at the absolute top of the process
if os.name == 'nt':
    import ctypes
    # Set DPI awareness to prevent blurry UI and random resizing loops
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1) # PROCESS_SYSTEM_DPI_AWARE is more stable in PyWebView on Windows
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # Create Named Mutex to prevent multiple processes running simultaneously
    try:
        kernel32 = ctypes.windll.kernel32
        # Reset last error to avoid false positives from PyInstaller initialization
        kernel32.SetLastError(0)
        mutex_name = "Global\\TikTokDownloaderHD_SingleInstance_Mutex_v3_0"
        mutex = kernel32.CreateMutexW(None, True, mutex_name)
        last_error = kernel32.GetLastError()
        if last_error == 183: # ERROR_ALREADY_EXISTS
            # Mutex exists, but we won't exit to prevent false locks from zombie processes
            print("Warning: Another instance might be running.")
    except Exception as e:
        print(f"Mutex error: {e}")

import re
import requests
import json
import threading
import webview
import yt_dlp

def get_download_path():
    """Gets the default Windows Downloads path, fallback to User/Downloads."""
    if os.name == 'nt':
        import winreg
        sub_key = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders'
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
                return winreg.QueryValueEx(key, '{374DE290-123F-4565-9164-39C4925E467B}')[0]
        except Exception:
            return os.path.join(os.path.expanduser('~'), 'Downloads')
    else:
        return os.path.join(os.path.expanduser('~'), 'Downloads')

def sanitize_filename(name):
    """Sanitizes filename keeping it clean, stripping invalid chars."""
    if not name:
        return "TikTok_Video"
    # Remove emojis and non-ascii or weird characters to prevent OS save issues
    name = name.encode('ascii', 'ignore').decode('ascii')
    # Remove invalid Windows filename characters
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip()
    # Replace multiple spaces/newlines with a single space
    name = re.sub(r'\s+', " ", name)
    if not name:
        return "TikTok_Video"
    return name[:120]  # Limit length

def resolve_redirects(url):
    """Follows HTTP redirects to resolve short URLs to their standard destination."""
    url = url.strip()
    if not url:
        return url
    if not url.startswith("http://") and not url.startswith("https://"):
        return url
    try:
        import requests
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
        r = requests.head(url, allow_redirects=True, headers=headers, timeout=10)
        return r.url
    except Exception as e:
        print(f"Error resolving redirect for {url} using HEAD: {e}")
        try:
            import requests
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
            r = requests.get(url, allow_redirects=True, headers=headers, timeout=10)
            return r.url
        except Exception:
            return url

def get_idm_path():
    """Locates IDMan.exe using registry keys or standard install directories."""
    import os
    if os.name == 'nt':
        import winreg
        for subkey in [r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\IDMan.exe",
                       r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\App Paths\IDMan.exe"]:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
                    path, _ = winreg.QueryValueEx(key, "")
                    if os.path.exists(path):
                        return path
            except Exception:
                pass
        
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\DownloadManager") as key:
                path, _ = winreg.QueryValueEx(key, "ExePath")
                if os.path.exists(path):
                    return path
        except Exception:
            pass
            
    paths = [
        r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe",
        r"C:\Program Files\Internet Download Manager\IDMan.exe",
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return None



_app_window = None
CURRENT_VERSION = "3.3"

# ─────────────────────────────────────────────────────────────
# LINK GRABBER LOCAL HTTP SERVER (port 7823)
# Receives links from the HK Link Grabber browser extension
# ─────────────────────────────────────────────────────────────
import http.server
import urllib.parse

class _LinkGrabberHandler(http.server.BaseHTTPRequestHandler):
    def _send_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        if self.path == '/ping':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._send_cors()
            self.end_headers()
            self.wfile.write(b'{"status":"online","app":"HK Downloader Pro"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/add-url':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                data = json.loads(body)
                url = data.get('url', '').strip()
                if url and _app_window:
                    _app_window.evaluate_js(f'onLinkReceivedFromExtension({json.dumps(url)})')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self._send_cors()
                self.end_headers()
                self.wfile.write(b'{"success":true}')
            except Exception as ex:
                self.send_response(400)
                self._send_cors()
                self.end_headers()
                self.wfile.write(json.dumps({'success': False, 'error': str(ex)}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress console output

def _start_link_grabber_server():
    try:
        server = http.server.HTTPServer(('127.0.0.1', 7823), _LinkGrabberHandler)
        server.serve_forever()
    except Exception as e:
        print(f'Link Grabber server error: {e}')

threading.Thread(target=_start_link_grabber_server, daemon=True).start()

class TiktokDownloaderAPI:
    def _load_settings(self):
        settings_path = os.path.join(os.path.abspath("."), "settings.json")
        if os.path.exists(settings_path):
            try:
                with open(settings_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_settings(self, settings):
        settings_path = os.path.join(os.path.abspath("."), "settings.json")
        try:
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving settings: {e}")

    def _check_and_update_ytdlp(self):
        """Auto-Updater for yt-dlp: checks PyPI, downloads new version (.whl), extracts to local update folder."""
        try:
            import urllib.request
            import zipfile
            import shutil
            import sys
            
            print("[Auto-Updater] Checking for yt-dlp updates...")
            
            # Fetch latest PyPI info
            req = urllib.request.Request(
                'https://pypi.org/pypi/yt-dlp/json',
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                
            latest_version = data['info']['version']
            
            import yt_dlp.version
            current_version = yt_dlp.version.__version__
            
            print(f"[Auto-Updater] Current version: {current_version}, Latest: {latest_version}")
            
            # Check settings version to see if we already downloaded a newer version
            settings = self._load_settings()
            staged_version = settings.get('ytdlp_override_version', '0.0.0')
            
            # Compare versions
            if latest_version <= current_version or latest_version <= staged_version:
                print("[Auto-Updater] yt-dlp is already at the latest version.")
                return
                
            print(f"[Auto-Updater] Downloading yt-dlp version {latest_version}...")
            
            # Find wheel URL
            whl_url = None
            for url_info in data['urls']:
                if url_info['filename'].endswith('.whl'):
                    whl_url = url_info['url']
                    break
                    
            if not whl_url:
                print("[Auto-Updater] No wheel package found on PyPI.")
                return
                
            base_dir = os.path.abspath(".")
            update_dir = os.path.join(base_dir, 'yt_dlp_updates')
            os.makedirs(update_dir, exist_ok=True)
            
            tmp_whl = os.path.join(update_dir, f'yt_dlp_{latest_version}.whl')
            urllib.request.urlretrieve(whl_url, tmp_whl)
            
            # Extract to target directory
            extract_to = os.path.join(update_dir, f'yt_dlp_{latest_version}')
            os.makedirs(extract_to, exist_ok=True)
            
            with zipfile.ZipFile(tmp_whl, 'r') as z:
                for file_info in z.infolist():
                    if file_info.filename.startswith('yt_dlp/'):
                        z.extract(file_info, extract_to)
                        
            # Clean up whl file
            try:
                os.remove(tmp_whl)
            except:
                pass
                
            # Save settings pointing to this folder
            settings['ytdlp_override_path'] = os.path.join(extract_to, 'yt_dlp')
            settings['ytdlp_override_version'] = latest_version
            self._save_settings(settings)
            
            print(f"[Auto-Updater] Stage completed! Dynamic yt-dlp v{latest_version} will load on next startup.")
            
        except Exception as e:
            print(f"[Auto-Updater] Error checking/staging yt-dlp update: {e}")

    def _enforce_cookies_expiry(self):
        """Auto-expires cookies settings after 7 days."""
        try:
            settings = self._load_settings()
            import datetime
            now = datetime.datetime.utcnow()
            dirty = False
            
            # Instagram
            ig_uploaded = settings.get('instagram_cookies_uploaded_at')
            if ig_uploaded:
                uploaded_dt = datetime.datetime.fromisoformat(ig_uploaded)
                if (now - uploaded_dt).days >= 7:
                    settings.pop('instagram_cookies_file', None)
                    settings.pop('instagram_cookies_uploaded_at', None)
                    self.instagram_cookies_file = None
                    dirty = True
            
            # Facebook
            fb_uploaded = settings.get('facebook_cookies_uploaded_at')
            if fb_uploaded:
                uploaded_dt = datetime.datetime.fromisoformat(fb_uploaded)
                if (now - uploaded_dt).days >= 7:
                    settings.pop('facebook_cookies_file', None)
                    settings.pop('facebook_cookies_uploaded_at', None)
                    self.facebook_cookies_file = None
                    dirty = True

            # Pinterest
            pin_uploaded = settings.get('pinterest_cookies_uploaded_at')
            if pin_uploaded:
                uploaded_dt = datetime.datetime.fromisoformat(pin_uploaded)
                if (now - uploaded_dt).days >= 7:
                    settings.pop('pinterest_cookies_file', None)
                    settings.pop('pinterest_cookies_uploaded_at', None)
                    self.pinterest_cookies_file = None
                    dirty = True

            # YouTube
            yt_uploaded = settings.get('youtube_cookies_uploaded_at')
            if yt_uploaded:
                uploaded_dt = datetime.datetime.fromisoformat(yt_uploaded)
                if (now - uploaded_dt).days >= 7:
                    settings.pop('youtube_cookies_file', None)
                    settings.pop('youtube_cookies_uploaded_at', None)
                    self.youtube_cookies_file = None
                    dirty = True
            
            if dirty:
                self._save_settings(settings)
                print("[Cookies Expiry] Automatically expired and cleared cookies older than 7 days.")
        except Exception as e:
            print(f"[Cookies Expiry Error] {e}")

    def __init__(self):
        self.global_download_folder = None
        self.instagram_cookies_file = None
        self.facebook_cookies_file = None
        self.pinterest_cookies_file = None
        self.youtube_cookies_file = None
        self.tiktok_download_folder = None
        self.youtube_download_folder = None
        self.instagram_download_folder = None
        self.facebook_download_folder = None
        self.pinterest_download_folder = None
        self.douyin_download_folder = None
        self.kuaishou_download_folder = None
        self.downloads_count = 0
        self.lifetime_downloads = 0
        self.downloads_limit = 999999
        self.plan_name = "Trial Plan"
        self.proxy_list = []
        self.max_download_workers = 4
        
        # Speed monitor state
        self.total_downloaded_bytes = 0
        self.active_download_count = 0
        self._start_speed_monitor()
        
        # Clean up any leftover update backup files (.old)
        try:
            if getattr(sys, 'frozen', False):
                current_exe = os.path.abspath(sys.executable)
                dir_name = os.path.dirname(current_exe)
                for f in os.listdir(dir_name):
                    if f.endswith('.old') and f.startswith(os.path.basename(current_exe)):
                        try:
                            os.remove(os.path.join(dir_name, f))
                        except:
                            pass
        except Exception as e:
            print(f"Error cleaning old update files: {e}")
        
        # Load settings from settings file
        try:
            settings = self._load_settings()
            self.skip_duplicates = settings.get('skip_duplicates', True)
            self.instagram_cookies_file = settings.get('instagram_cookies_file', None)
            self.facebook_cookies_file = settings.get('facebook_cookies_file', None)
            self.pinterest_cookies_file = settings.get('pinterest_cookies_file', None)
            self.youtube_cookies_file = settings.get('youtube_cookies_file', None)
            self.lifetime_downloads = settings.get('lifetime_downloads', 0)
            self._enforce_cookies_expiry()
        except Exception:
            self.skip_duplicates = True
            
        # Start background check for yt-dlp updates (Auto-Healing feature)
        try:
            threading.Thread(target=self._check_and_update_ytdlp, daemon=True).start()
        except Exception as te:
            print(f"Error starting auto-updater thread: {te}")
            
        self.report_startup()
        if self.is_debugger_active():
            import sys
            sys.exit(1)

    def get_firebase_url(self):
        import base64
        return base64.b64decode(b"aHR0cHM6Ly9oa2Rvd25sb2FkZXJ0ZWxlbWV0cnktZGVmYXVsdC1ydGRiLmZpcmViYXNlaW8uY29t").decode("utf-8")

    def is_debugger_active(self):
        import sys
        if sys.gettrace() is not None:
            return True
        try:
            import ctypes
            if ctypes.windll.kernel32.IsDebuggerPresent():
                return True
        except:
            pass
        return False

    def _start_speed_monitor(self):
        """Starts a background thread to read real system download speed and broadcast to UI."""
        def monitor():
            import time
            last_bytes = 0
            try:
                import psutil
                use_psutil = True
            except ImportError:
                use_psutil = False

            while True:
                time.sleep(1.0)

                if use_psutil:
                    try:
                        net = psutil.net_io_counters()
                        current_bytes = net.bytes_recv
                        bytes_per_sec = max(0, current_bytes - last_bytes)
                        last_bytes = current_bytes
                    except Exception:
                        bytes_per_sec = 0
                else:
                    current_bytes = self.total_downloaded_bytes
                    bytes_per_sec = max(0, current_bytes - last_bytes)
                    last_bytes = current_bytes

                # Format speed string
                if bytes_per_sec >= 1024 * 1024:
                    speed_str = f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
                elif bytes_per_sec >= 1024:
                    speed_str = f"{bytes_per_sec / 1024:.0f} KB/s"
                elif bytes_per_sec > 0:
                    speed_str = f"{bytes_per_sec} B/s"
                else:
                    speed_str = "0 KB/s"

                # Dynamically scale download workers based on download traffic speed
                if bytes_per_sec > 10 * 1024 * 1024:     # > 80 Mbps
                    self.max_download_workers = 8
                elif bytes_per_sec > 4 * 1024 * 1024:   # > 32 Mbps
                    self.max_download_workers = 5
                elif bytes_per_sec > 1.2 * 1024 * 1024: # > 10 Mbps
                    self.max_download_workers = 4
                elif bytes_per_sec > 0:
                    self.max_download_workers = 2        # Slow
                else:
                    self.max_download_workers = 4

                try:
                    global _app_window
                    if _app_window:
                        # Update speed text display
                        _app_window.evaluate_js(f"if (typeof updateDownloadSpeed === 'function') {{ updateDownloadSpeed('{speed_str}'); }}")
                        # Push raw bytes/sec to graph (always, even 0)
                        _app_window.evaluate_js(f"if (typeof pushSpeedToGraph === 'function') {{ pushSpeedToGraph({bytes_per_sec}); }}")
                except Exception:
                    pass
        import threading
        threading.Thread(target=monitor, daemon=True).start()

    def get_global_download_folder(self):
        """Returns the global download directory."""
        return self.global_download_folder or ""

    def set_global_download_folder(self, path):
        """Sets the global download directory and configures subfolders."""
        if not path:
            return {"success": False, "error": "Invalid path"}
        self.global_download_folder = path
        self.tiktok_download_folder = os.path.join(path, "TikTok")
        self.youtube_download_folder = os.path.join(path, "YouTube")
        self.instagram_download_folder = os.path.join(path, "Instagram")
        self.facebook_download_folder = os.path.join(path, "Facebook")
        self.pinterest_download_folder = os.path.join(path, "Pinterest")
        self.douyin_download_folder = os.path.join(path, "ChineseApps")
        self.kuaishou_download_folder = os.path.join(path, "ChineseApps")

        # Only pre-create the main global download folder. Platform subfolders are created dynamically when a download starts.
        if self.global_download_folder and not os.path.exists(self.global_download_folder):
            try:
                os.makedirs(self.global_download_folder)
            except Exception:
                pass

        # Save to settings
        settings = self._load_settings()
        settings["global_download_folder"] = path
        self._save_settings(settings)

        return {"success": True, "path": path}

    def select_platform_download_folder(self, platform):
        """Opens a native folder dialog to let user choose the output directory for a specific platform."""
        global _app_window
        if not _app_window:
            return {"success": False, "error": "Window not initialized"}
        
        result = _app_window.create_file_dialog(webview.FOLDER_DIALOG)
        if result and len(result) > 0:
            path = result[0]
            if platform == 'global':
                return self.set_global_download_folder(path)
            elif platform == 'tiktok':
                self.tiktok_download_folder = path
            elif platform == 'youtube':
                self.youtube_download_folder = path
            elif platform == 'instagram':
                self.instagram_download_folder = path
            elif platform == 'facebook':
                self.facebook_download_folder = path
            elif platform == 'pinterest':
                self.pinterest_download_folder = path
            elif platform == 'douyin':
                self.douyin_download_folder = path
            elif platform == 'kuaishou':
                self.kuaishou_download_folder = path
            elif platform == 'chinese':
                self.douyin_download_folder = path
                self.kuaishou_download_folder = path
            
            if not os.path.exists(path):
                try:
                    os.makedirs(path)
                except Exception:
                    pass
            return {"success": True, "path": path}
        return {"success": False, "error": "No folder selected"}

    def _get_folder_metadata(self, folder_path):
        metadata_path = os.path.join(folder_path, ".metadata.json")
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {"downloaded_ids": {}, "max_seq": 0}

    def _save_folder_metadata(self, folder_path, metadata):
        metadata_path = os.path.join(folder_path, ".metadata.json")
        try:
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving folder metadata: {e}")

    def _update_download_metadata(self, dest_dir, platform_main_dir, video_id, filename):
        import time
        if not dest_dir or dest_dir == platform_main_dir:
            return
        metadata_path = os.path.join(dest_dir, ".metadata.json")
        metadata = self._get_folder_metadata(dest_dir)
        metadata["downloaded_ids"][video_id] = {
            "filename": filename,
            "timestamp": time.time()
        }
        # Parse sequence number from filename
        try:
            parts = filename.split(" - ", 1)
            if len(parts) > 1 and parts[0].isdigit():
                seq = int(parts[0])
                if seq > metadata["max_seq"]:
                    metadata["max_seq"] = seq
        except Exception:
            pass
        self._save_folder_metadata(dest_dir, metadata)

    def check_for_updates(self, manual=False):
        """Checks for updates from Firebase app_config.json. Returns update status."""
        try:
            FIREBASE_URL = self.get_firebase_url()
            try:
                r = requests.get(f"{FIREBASE_URL}/app_config.json", timeout=5)
            except requests.exceptions.SSLError:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                r = requests.get(f"{FIREBASE_URL}/app_config.json", timeout=5, verify=False)

            if r.status_code == 200:
                data = r.json() or {}
                latest_ver = data.get("latest_version", CURRENT_VERSION)
                update_url = data.get("download_url", "")
                changelog = data.get("changelog", "Mandatory security and engine updates.")
                min_ver = data.get("min_required_version", latest_ver)
                update_btn_text = data.get("update_btn_text", "Update Now")
                
                def parse_version(v):
                    return [int(x) for x in re.sub(r'[^0-9.]', '', v).split('.')]
                
                is_mandatory = parse_version(CURRENT_VERSION) < parse_version(min_ver)
                has_newer = parse_version(CURRENT_VERSION) < parse_version(latest_ver)
                
                if is_mandatory or (manual and has_newer):
                    return {
                        "update_required": True,
                        "latest_version": latest_ver,
                        "update_url": update_url,
                        "changelog": changelog,
                        "update_btn_text": update_btn_text
                    }
        except Exception as e:
            print(f"Update check error: {e}")
        return {"update_required": False}

    def open_external_url(self, url):
        """Opens a URL in the user's default browser."""
        import webbrowser
        try:
            webbrowser.open(url)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def start_update_download(self, download_url):
        """Starts a background thread to download the update or opens external web links."""
        self.update_download_url = download_url
        url_lower = download_url.lower()
        if not url_lower.endswith('.exe') or 'wa.me' in url_lower or 'whatsapp.com' in url_lower or 't.me' in url_lower:
            import webbrowser
            try:
                webbrowser.open(download_url)
                if hasattr(self, '_app_window') and self._app_window:
                    self._app_window.evaluate_js('onUpdateOpenedExternal()')
                return {"success": True, "external": True}
            except Exception as e:
                return {"success": False, "error": str(e)}

        threading.Thread(target=self._download_update_thread, args=(download_url,), daemon=True).start()
        return {"success": True}

    def _download_update_thread(self, download_url):
        """Downloads the new EXE/Setup to temp directory, updates progress, and installs."""
        try:
            import tempfile
            tmp_path = os.path.join(tempfile.gettempdir(), 'HKDownloader_update.exe')
            
            headers = {'User-Agent': 'Mozilla/5.0'}
            r = requests.get(download_url, headers=headers, stream=True, timeout=30)
            r.raise_for_status()
            
            total_length = r.headers.get('content-length')
            if total_length is None:
                with open(tmp_path, 'wb') as f:
                    f.write(r.content)
            else:
                total_length = int(total_length)
                downloaded = 0
                with open(tmp_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            percent = int((downloaded / total_length) * 100) if total_length > 0 else 0
                            self._notify_update_progress(percent)
                            
            self._notify_update_progress(100)
            
            import time
            time.sleep(1)
            
            # Apply update and restart!
            self.apply_update_and_restart()
            
        except Exception as e:
            print(f"Error during update download: {e}")
            self._notify_update_error(str(e))

    def _notify_update_progress(self, percent):
        """Calls JavaScript function to update update progress."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onUpdateProgress === 'function') {{ window.onUpdateProgress({percent}); }}"
            _app_window.evaluate_js(js_code)

    def _notify_update_error(self, error_msg):
        """Calls JavaScript function on update download error."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onUpdateError === 'function') {{ window.onUpdateError({json.dumps(error_msg)}); }}"
            _app_window.evaluate_js(js_code)




    def select_instagram_cookies_file(self):
        """Opens file dialog for user to select cookies.txt file."""
        global _app_window
        if not _app_window:
            return {"success": False, "error": "Window not initialized"}
        
        result = _app_window.create_file_dialog(webview.OPEN_DIALOG, file_types=('Text files (*.txt)', 'All files (*.*)'))
        if result and len(result) > 0:
            self.instagram_cookies_file = result[0]
            if os.path.exists(self.instagram_cookies_file):
                # Save to settings
                settings = self._load_settings()
                settings['instagram_cookies_file'] = self.instagram_cookies_file
                import datetime
                settings['instagram_cookies_uploaded_at'] = datetime.datetime.utcnow().isoformat()
                self._save_settings(settings)
                return {"success": True, "path": self.instagram_cookies_file}
        return {"success": False, "error": "No file selected or file invalid"}

    def clear_instagram_cookies_file(self):
        """Clears custom cookies file setting."""
        self.instagram_cookies_file = None
        settings = self._load_settings()
        settings.pop('instagram_cookies_file', None)
        settings.pop('instagram_cookies_uploaded_at', None)
        self._save_settings(settings)
        return {"success": True}

    def select_facebook_cookies_file(self):
        """Opens file dialog for user to select Facebook cookies.txt file."""
        global _app_window
        if not _app_window:
            return {"success": False, "error": "Window not initialized"}
        result = _app_window.create_file_dialog(webview.OPEN_DIALOG, file_types=('Text files (*.txt)', 'All files (*.*)'))
        if result and len(result) > 0:
            self.facebook_cookies_file = result[0]
            if os.path.exists(self.facebook_cookies_file):
                # Save to settings
                settings = self._load_settings()
                settings['facebook_cookies_file'] = self.facebook_cookies_file
                import datetime
                settings['facebook_cookies_uploaded_at'] = datetime.datetime.utcnow().isoformat()
                self._save_settings(settings)
                return {"success": True, "path": self.facebook_cookies_file}
        return {"success": False, "error": "No file selected or file invalid"}

    def clear_facebook_cookies_file(self):
        """Clears custom Facebook cookies file setting."""
        self.facebook_cookies_file = None
        settings = self._load_settings()
        settings.pop('facebook_cookies_file', None)
        settings.pop('facebook_cookies_uploaded_at', None)
        self._save_settings(settings)
        return {"success": True}

    def select_pinterest_cookies_file(self):
        """Opens file dialog for user to select Pinterest cookies.txt file."""
        global _app_window
        if not _app_window:
            return {"success": False, "error": "Window not initialized"}
        result = _app_window.create_file_dialog(webview.OPEN_DIALOG, file_types=('Text files (*.txt)', 'All files (*.*)'))
        if result and len(result) > 0:
            self.pinterest_cookies_file = result[0]
            if os.path.exists(self.pinterest_cookies_file):
                # Save to settings
                settings = self._load_settings()
                settings['pinterest_cookies_file'] = self.pinterest_cookies_file
                import datetime
                settings['pinterest_cookies_uploaded_at'] = datetime.datetime.utcnow().isoformat()
                self._save_settings(settings)
                return {"success": True, "path": self.pinterest_cookies_file}
        return {"success": False, "error": "No file selected or file invalid"}

    def clear_pinterest_cookies_file(self):
        """Clears custom Pinterest cookies file setting."""
        self.pinterest_cookies_file = None
        settings = self._load_settings()
        settings.pop('pinterest_cookies_file', None)
        settings.pop('pinterest_cookies_uploaded_at', None)
        self._save_settings(settings)
        return {"success": True}

    def select_youtube_cookies_file(self):
        """Opens file dialog for user to select YouTube cookies.txt file."""
        global _app_window
        if not _app_window:
            return {"success": False, "error": "Window not initialized"}
        result = _app_window.create_file_dialog(webview.OPEN_DIALOG, file_types=('Text files (*.txt)', 'All files (*.*)'))
        if result and len(result) > 0:
            self.youtube_cookies_file = result[0]
            if os.path.exists(self.youtube_cookies_file):
                # Save to settings
                settings = self._load_settings()
                settings['youtube_cookies_file'] = self.youtube_cookies_file
                import datetime
                settings['youtube_cookies_uploaded_at'] = datetime.datetime.utcnow().isoformat()
                self._save_settings(settings)
                return {"success": True, "path": self.youtube_cookies_file}
        return {"success": False, "error": "No file selected or file invalid"}

    def clear_youtube_cookies_file(self):
        """Clears custom YouTube cookies file setting."""
        self.youtube_cookies_file = None
        settings = self._load_settings()
        settings.pop('youtube_cookies_file', None)
        settings.pop('youtube_cookies_uploaded_at', None)
        self._save_settings(settings)
        return {"success": True}

    def select_import_urls_file(self):
        """Opens file dialog for user to select a .txt or .csv file and extracts video URLs."""
        global _app_window
        if not _app_window:
            return {"success": False, "error": "Window not initialized"}
        
        result = _app_window.create_file_dialog(
            webview.OPEN_DIALOG, 
            file_types=('Supported files (*.txt;*.csv)', 'Text files (*.txt)', 'CSV files (*.csv)', 'All files (*.*)')
        )
        if result and len(result) > 0:
            filepath = result[0]
            if os.path.exists(filepath):
                try:
                    urls = []
                    # Detect encoding or default to utf-8 (ignore errors)
                    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                        lines = f.readlines()
                    
                    for line in lines:
                        cleaned = line.strip()
                        if not cleaned:
                            continue
                        # If it's a CSV, it might have multiple columns. Let's extract anything that looks like a URL!
                        # Or split by commas/semicolons and check each item.
                        if filepath.lower().endswith('.csv'):
                            import re
                            # Extract words that contain http or https
                            parts = re.split(r'[,\s\t;]+', cleaned)
                            for part in parts:
                                p_clean = part.strip().strip('"\'')
                                if p_clean.startswith(('http://', 'https://')):
                                    urls.append(p_clean)
                        else:
                            # For TXT files, if it starts with http or looks like a URL or username
                            if cleaned.startswith(('http://', 'https://', '@')) or '/' in cleaned:
                                urls.append(cleaned)
                            elif len(cleaned) > 2: # maybe a username
                                urls.append(cleaned)
                                
                    if not urls:
                        return {"success": False, "error": "No valid URLs found in file."}
                    return {"success": True, "urls": urls}
                except Exception as ex:
                    return {"success": False, "error": f"Failed to read file: {str(ex)}"}
        return {"success": False}

    def save_cookies_text(self, platform_name, text):
        """Saves pasted Netscape cookies text to a local file and registers it in settings."""
        try:
            filename = f"{platform_name}_pasted_cookies.txt"
            filepath = os.path.join(os.path.abspath("."), filename)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(text)
                
            settings = self._load_settings()
            import datetime
            if platform_name == 'instagram':
                self.instagram_cookies_file = filepath
                settings['instagram_cookies_file'] = filepath
                settings['instagram_cookies_uploaded_at'] = datetime.datetime.utcnow().isoformat()
            elif platform_name == 'facebook':
                self.facebook_cookies_file = filepath
                settings['facebook_cookies_file'] = filepath
                settings['facebook_cookies_uploaded_at'] = datetime.datetime.utcnow().isoformat()
            elif platform_name == 'pinterest':
                self.pinterest_cookies_file = filepath
                settings['pinterest_cookies_file'] = filepath
                settings['pinterest_cookies_uploaded_at'] = datetime.datetime.utcnow().isoformat()
            elif platform_name == 'youtube':
                self.youtube_cookies_file = filepath
                settings['youtube_cookies_file'] = filepath
                settings['youtube_cookies_uploaded_at'] = datetime.datetime.utcnow().isoformat()
                
            self._save_settings(settings)
            return {"success": True, "path": filepath}
        except Exception as ex:
            return {"success": False, "error": str(ex)}

    def get_cookies_status(self):
        """Returns the basenames of currently loaded cookies files for UI display."""
        import os
        return {
            "success": True,
            "youtube": os.path.basename(self.youtube_cookies_file) if (self.youtube_cookies_file and os.path.exists(self.youtube_cookies_file)) else None,
            "instagram": os.path.basename(self.instagram_cookies_file) if (self.instagram_cookies_file and os.path.exists(self.instagram_cookies_file)) else None,
            "facebook": os.path.basename(self.facebook_cookies_file) if (self.facebook_cookies_file and os.path.exists(self.facebook_cookies_file)) else None,
            "pinterest": os.path.basename(self.pinterest_cookies_file) if (self.pinterest_cookies_file and os.path.exists(self.pinterest_cookies_file)) else None,
        }

    def clean_temporary_cache_files(self):
        """Recursively finds and deletes all .part, .ytdl, and .part-Frag files from downloads directories."""
        folders = [self.tiktok_download_folder, self.youtube_download_folder, 
                   self.instagram_download_folder, self.facebook_download_folder, 
                   self.pinterest_download_folder, self.douyin_download_folder, 
                   self.kuaishou_download_folder, self.global_download_folder]
        cleaned_count = 0
        cleaned_bytes = 0
        for folder in folders:
            if folder and os.path.exists(folder):
                for root, dirs, files in os.walk(folder):
                    for file in files:
                        if file.endswith(('.part', '.ytdl')) or '.part-Frag' in file:
                            fpath = os.path.join(root, file)
                            try:
                                sz = os.path.getsize(fpath)
                                os.remove(fpath)
                                cleaned_count += 1
                                cleaned_bytes += sz
                            except Exception:
                                pass
        mb_freed = round(cleaned_bytes / (1024 * 1024), 2)
        return {"success": True, "count": cleaned_count, "size_mb": mb_freed}

    def open_system_downloads_folder(self):
        """Opens global download folder or first available platform folder."""
        path = self.global_download_folder
        if not path or not os.path.exists(path):
            for p in [self.youtube_download_folder, self.tiktok_download_folder, self.instagram_download_folder]:
                if p and os.path.exists(p):
                    path = p
                    break
        if not path or not os.path.exists(path):
            path = os.path.abspath(".")
        try:
            if os.name == 'nt':
                os.startfile(path)
            elif sys.platform == 'darwin':
                import subprocess
                subprocess.Popen(['open', path])
            else:
                import subprocess
                subprocess.Popen(['xdg-open', path])
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_clipboard_text(self):
        """Gets text content from clipboard using tkinter or returns empty string."""
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            text = root.clipboard_get()
            root.destroy()
            return text
        except Exception:
            return ""

    def select_download_folder(self, folder_type):
        """Opens a native folder dialog to let user choose where to save videos."""
        global _app_window
        if not _app_window:
            if folder_type == 'tiktok': return self.tiktok_download_folder
            elif folder_type == 'youtube': return self.youtube_download_folder
            else: return self.instagram_download_folder
        
        result = _app_window.create_file_dialog(webview.FOLDER_DIALOG)
        if result and len(result) > 0:
            selected_path = result[0]
            # Ensure path exists
            if not os.path.exists(selected_path):
                try:
                    os.makedirs(selected_path)
                except Exception:
                    pass
            if folder_type == 'tiktok':
                self.tiktok_download_folder = selected_path
                return self.tiktok_download_folder
            elif folder_type == 'youtube':
                self.youtube_download_folder = selected_path
                return self.youtube_download_folder
            else:
                self.instagram_download_folder = selected_path
                return self.instagram_download_folder
        if folder_type == 'tiktok': return self.tiktok_download_folder
        elif folder_type == 'youtube': return self.youtube_download_folder
        else: return self.instagram_download_folder

    def fetch_single_video(self, url):
        """Fetches metadata for a single video from TikWM API."""
        print(f"Fetching video info for: {url}")
        api_url = "https://www.tikwm.com/api/"
        # We request HD=1 for maximum quality (1080p if available)
        payload = {
            "url": url,
            "hd": 1
        }
        
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.post(api_url, data=payload, headers=headers, timeout=15)
            if response.status_code == 200:
                res_data = response.json()
                if res_data.get("code") == 0:
                    data = res_data.get("data", {})
                    # Clean return structure
                    video_info = {
                        "video_id": data.get("id"),
                        "title": data.get("title", "TikTok Video"),
                        "play": data.get("play"),       # Standard watermark-free
                        "hdplay": data.get("hdplay"),   # HD watermark-free (1080p if available)
                        "wmplay": data.get("wmplay"),   # Watermarked
                        "cover": data.get("cover"),
                        "duration": data.get("duration"),
                        "author": {
                            "unique_id": data.get("author", {}).get("unique_id"),
                            "nickname": data.get("author", {}).get("nickname"),
                            "avatar": data.get("author", {}).get("avatar")
                        }
                    }
                    return {"success": True, "data": video_info}
                else:
                    return {"success": False, "error": res_data.get("msg", "Failed to parse video details.")}
            else:
                return {"success": False, "error": f"API server returned status code {response.status_code}"}
        except Exception as e:
            return {"success": False, "error": f"Connection error: {str(e)}"}

    def fetch_all_profile_videos_bulk(self, username):
        """Fetches ALL videos from a TikTok profile in one call (Python-side pagination = very fast)."""
        if not self.tiktok_download_folder:
            return {"success": False, "error": "Please select TikTok output folder first."}

        # ── Parse username ──────────────────────────────────────────────
        username = username.strip()
        if "tiktok.com" in username:
            if "@" in username:
                after_at = username.split("@")[1]
                username = re.split(r'[/?]', after_at)[0]
            else:
                try:
                    headers = {'User-Agent': 'Mozilla/5.0'}
                    r = requests.get(username, headers=headers, allow_redirects=True, timeout=8)
                    final_url = r.url
                    if "@" in final_url:
                        after_at = final_url.split("@")[1]
                        username = re.split(r'[/?]', after_at)[0]
                except Exception as e:
                    print(f"Error resolving redirect: {e}")

        if username.startswith("@"):
            username = username[1:]

        if not username:
            return {"success": False, "error": "Invalid input. Please enter a username or valid TikTok URL."}

        # ── Setup folder & metadata ──────────────────────────────────────
        target_dir = os.path.join(self.tiktok_download_folder, f"@{username}")
        if not os.path.exists(target_dir):
            try:
                os.makedirs(target_dir)
            except Exception:
                pass

        metadata  = self._get_folder_metadata(target_dir)
        downloaded_ids = metadata.get("downloaded_ids", {})
        max_seq        = metadata.get("max_seq", 0)

        # ── Bulk pagination loop (all inside Python) ─────────────────────
        api_url = "https://www.tikwm.com/api/user/posts"
        req_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

        all_new_videos = []
        total_skipped  = 0
        cursor         = "0"
        page           = 0
        MAX_PAGES      = 200   # safety cap

        def _notify(msg):
            try:
                global _app_window
                if _app_window:
                    _app_window.evaluate_js(f"logToConsole({json.dumps(msg)}, 'info')")
            except Exception:
                pass

        # ── Pre-scan folder for existing files (handles old downloads without metadata) ──
        existing_basenames = set()
        if os.path.exists(target_dir):
            for fname in os.listdir(target_dir):
                if fname.lower().endswith('.mp4'):
                    existing_basenames.add(fname[:-4].lower())  # store without extension, lowercase

        try:
            while page < MAX_PAGES:
                page += 1
                _notify(f"[Fetching] Page {page} — {len(all_new_videos)} new videos found so far...")

                params = {"unique_id": username, "count": 35, "cursor": cursor}
                resp = None
                for attempt in range(3):
                    try:
                        resp = requests.get(api_url, params=params, headers=req_headers, timeout=15)
                        if resp.status_code == 200:
                            break
                    except Exception:
                        pass
                    import time
                    time.sleep(1.5)

                if not resp or resp.status_code != 200:
                    break

                res_data = resp.json()
                if res_data.get("code") != 0:
                    if page == 1:
                        return {"success": False, "error": res_data.get("msg", "Profile not found or is private.")}
                    break

                data   = res_data.get("data", {})
                videos = data.get("videos", [])
                has_more = data.get("hasMore", False)
                cursor   = data.get("cursor", "0")

                hit_existing = False
                for v in videos:
                    vid_id = v.get("video_id")
                    if not vid_id:
                        continue

                    # Check 1: metadata.json has this video_id
                    in_metadata = vid_id in downloaded_ids

                    # Check 2: a file with same title/id already exists in folder
                    v_title = v.get("title", "")
                    base_name = sanitize_filename(v_title).lower()
                    file_exists = False
                    if vid_id:
                        for existing_name in existing_basenames:
                            if str(vid_id) in existing_name:
                                file_exists = True
                                break
                    if not file_exists:
                        file_exists = (base_name in existing_basenames or
                                       any(f"{base_name}_{i}" in existing_basenames for i in range(1, 15)))

                    if self.skip_duplicates and (in_metadata or file_exists):
                        hit_existing = True
                        total_skipped += 1
                        # Auto-register in metadata so future runs are faster
                        if not in_metadata and vid_id:
                            downloaded_ids[vid_id] = base_name + ".mp4"
                    else:
                        all_new_videos.append(v)

                if (hit_existing and self.skip_duplicates) or not has_more or not cursor or cursor == "0":
                    break

            # ── Reverse so oldest → lowest seq number ───────────────────
            all_new_videos.reverse()

            cleaned = []
            for i, v in enumerate(all_new_videos):
                cleaned.append({
                    "video_id":       v.get("video_id"),
                    "title":          v.get("title", "TikTok Video"),
                    "play":           v.get("play"),
                    "hdplay":         v.get("hdplay"),
                    "wmplay":         v.get("wmplay"),
                    "cover":          v.get("cover"),
                    "duration":       v.get("duration"),
                    "sequenceNumber": max_seq + i + 1,
                    "download_dir":   target_dir,
                    "author": {
                        "unique_id": v.get("author", {}).get("unique_id"),
                        "nickname":  v.get("author", {}).get("nickname"),
                        "avatar":    v.get("author", {}).get("avatar"),
                    }
                })

            # ── Auto-save discovered IDs back to metadata for faster future runs ──
            if total_skipped > 0:
                try:
                    metadata["downloaded_ids"] = downloaded_ids
                    metadata_path = os.path.join(target_dir, ".metadata.json")
                    with open(metadata_path, "w", encoding="utf-8") as f:
                        import json as _json
                        _json.dump(metadata, f, ensure_ascii=False, indent=2)
                    _notify(f"[Sync] Saved {len(downloaded_ids)} video IDs to metadata for faster future runs.")
                except Exception as me:
                    print(f"Metadata auto-save error: {me}")

            _notify(f"[Done] Fetched {len(cleaned)} new videos. Skipped {total_skipped} already downloaded.")
            return {
                "success": True,
                "data": {
                    "videos":       cleaned,
                    "skippedCount": total_skipped
                }
            }

        except Exception as e:
            return {"success": False, "error": f"Connection error: {str(e)}"}

    def fetch_profile_videos(self, username, cursor="0"):
        """Legacy per-page fetch (kept for compatibility). Calls bulk internally on first page."""
        return self.fetch_all_profile_videos_bulk(username)

    def fetch_profile_progressive(self, username):
        """Progressive fetch: returns immediately and streams each page to JS via onProfilePageFetched().
        Downloads start after page 1 is fetched - remaining pages load in background in parallel."""

        if not self.tiktok_download_folder:
            return {"success": False, "error": "Please select TikTok output folder first."}

        username = username.strip()
        if "tiktok.com" in username:
            if "@" in username:
                after_at = username.split("@")[1]
                username = re.split(r'[/?]', after_at)[0]
            else:
                try:
                    headers = {'User-Agent': 'Mozilla/5.0'}
                    r = requests.get(username, headers=headers, allow_redirects=True, timeout=8)
                    final_url = r.url
                    if "@" in final_url:
                        after_at = final_url.split("@")[1]
                        username = re.split(r'[/?]', after_at)[0]
                except Exception as e:
                    print(f"Error resolving redirect: {e}")

        if username.startswith("@"):
            username = username[1:]

        if not username:
            return {"success": False, "error": "Invalid input. Please enter a username or valid TikTok URL."}

        target_dir = os.path.join(self.tiktok_download_folder, f"@{username}")
        if not os.path.exists(target_dir):
            try:
                os.makedirs(target_dir)
            except Exception:
                pass

        metadata = self._get_folder_metadata(target_dir)
        downloaded_ids = metadata.get("downloaded_ids", {})
        max_seq = metadata.get("max_seq", 0)

        existing_basenames = set()
        if os.path.exists(target_dir):
            for fname in os.listdir(target_dir):
                if fname.lower().endswith('.mp4'):
                    existing_basenames.add(fname[:-4].lower())

        def _notify_log(msg):
            try:
                global _app_window
                if _app_window:
                    _app_window.evaluate_js(f"logToConsole({json.dumps(msg)}, 'info')")
            except Exception:
                pass

        def _push_batch(batch_videos, is_done, skipped_count, seq_offset):
            try:
                global _app_window
                if not _app_window:
                    return
                cleaned = []
                for i, v in enumerate(batch_videos):
                    cleaned.append({
                        "video_id":       v.get("video_id"),
                        "title":          v.get("title", "TikTok Video"),
                        "play":           v.get("play"),
                        "hdplay":         v.get("hdplay"),
                        "wmplay":         v.get("wmplay"),
                        "cover":          v.get("cover"),
                        "duration":       v.get("duration"),
                        "sequenceNumber": seq_offset + i + 1,
                        "download_dir":   target_dir,
                        "author": {
                            "unique_id": v.get("author", {}).get("unique_id"),
                            "nickname":  v.get("author", {}).get("nickname"),
                            "avatar":    v.get("author", {}).get("avatar"),
                        }
                    })
                payload = json.dumps({
                    "videos": cleaned,
                    "isDone": is_done,
                    "skippedCount": skipped_count
                })
                _app_window.evaluate_js(f"onProfilePageFetched({payload})")
            except Exception as ex:
                print(f"Progressive push error: {ex}")

        def _run_progressive():
            global _app_window
            import time as _time
            api_url = "https://www.tikwm.com/api/user/posts"
            req_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            cur = "0"
            page = 0
            MAX_PAGES = 200
            total_skipped = 0
            total_fetched = 0

            try:
                while page < MAX_PAGES:
                    page += 1
                    _notify_log(f"[Fetching] Page {page} - {total_fetched} new videos queued so far...")

                    params = {"unique_id": username, "count": 35, "cursor": cur}
                    resp = None
                    for attempt in range(3):
                        try:
                            resp = requests.get(api_url, params=params, headers=req_headers, timeout=15)
                            if resp.status_code == 200:
                                break
                        except Exception:
                            pass
                        _time.sleep(1.5)

                    if not resp or resp.status_code != 200:
                        _push_batch([], True, total_skipped, max_seq + total_fetched)
                        break

                    res_data = resp.json()
                    if res_data.get("code") != 0:
                        if page == 1:
                            try:
                                if _app_window:
                                    err_msg = json.dumps(res_data.get("msg", "Profile not found or is private."))
                                    _app_window.evaluate_js(f"onProfileFetchError({err_msg})")
                            except Exception:
                                pass
                            return
                        _push_batch([], True, total_skipped, max_seq + total_fetched)
                        break

                    data = res_data.get("data", {})
                    videos = data.get("videos", [])
                    has_more = data.get("hasMore", False)
                    cur = data.get("cursor", "0")

                    page_new = []
                    hit_existing = False
                    for v in videos:
                        vid_id = v.get("video_id")
                        if not vid_id:
                            continue
                        in_metadata = vid_id in downloaded_ids
                        v_title = v.get("title", "")
                        base_name = sanitize_filename(v_title).lower()
                        file_exists = False
                        if vid_id:
                            for existing_name in existing_basenames:
                                if str(vid_id) in existing_name:
                                    file_exists = True
                                    break
                        if not file_exists:
                            file_exists = (base_name in existing_basenames or
                                           any(f"{base_name}_{i}" in existing_basenames for i in range(1, 15)))
                        if self.skip_duplicates and (in_metadata or file_exists):
                            hit_existing = True
                            total_skipped += 1
                            if not in_metadata and vid_id:
                                downloaded_ids[vid_id] = base_name + ".mp4"
                        else:
                            page_new.append(v)

                    page_new.reverse()
                    is_last_page = (hit_existing and self.skip_duplicates) or not has_more or not cur or cur == "0"

                    if page_new or is_last_page:
                        seq_offset = max_seq + total_fetched
                        _push_batch(page_new, is_last_page, total_skipped if is_last_page else 0, seq_offset)
                        total_fetched += len(page_new)

                    if is_last_page:
                        break

                if total_skipped > 0:
                    try:
                        metadata["downloaded_ids"] = downloaded_ids
                        metadata_path = os.path.join(target_dir, ".metadata.json")
                        with open(metadata_path, "w", encoding="utf-8") as mf:
                            import json as _j
                            _j.dump(metadata, mf, ensure_ascii=False, indent=2)
                        _notify_log(f"[Sync] Saved {len(downloaded_ids)} video IDs to metadata.")
                    except Exception as me:
                        print(f"Metadata save error: {me}")

                _notify_log(f"[Done] All pages fetched. {total_fetched} new video(s). Skipped {total_skipped}.")

            except Exception as ex:
                _notify_log(f"[Error] Progressive fetch failed: {str(ex)}")
                try:
                    if _app_window:
                        _app_window.evaluate_js(f"onProfileFetchError({json.dumps(str(ex))})")
                except Exception:
                    pass

        import threading as _thr
        _thr.Thread(target=_run_progressive, daemon=True).start()
        return {"success": True, "msg": "Progressive fetch started"}


    # =====================================================================
    # LICENSE & TRIAL SYSTEM METHODS (v3.0)
    # =====================================================================

    def get_hardware_id(self):
        """Generate unique hardware ID from CPU + Disk + MAC address."""
        import hashlib, subprocess, platform
        parts = [platform.node()]
        try:
            cpu = subprocess.check_output('wmic cpu get ProcessorId', shell=True, stderr=subprocess.DEVNULL).decode(errors='ignore')
            parts.append([l.strip() for l in cpu.splitlines() if l.strip() and 'ProcessorId' not in l][0])
        except: pass
        try:
            disk = subprocess.check_output('wmic diskdrive get SerialNumber', shell=True, stderr=subprocess.DEVNULL).decode(errors='ignore')
            parts.append([l.strip() for l in disk.splitlines() if l.strip() and 'SerialNumber' not in l][0])
        except: pass
        raw = ''.join(parts)
        h = hashlib.sha256(raw.encode()).hexdigest()[:20].upper()
        return '-'.join(h[i:i+4] for i in range(0, 20, 4))

    def check_license_status(self):
        """Check Firebase for trial/license status of this device."""
        import getpass
        import datetime
        sys_user = getpass.getuser().capitalize()
        current_month = datetime.datetime.utcnow().strftime('%Y-%m')
        try:
            hw_id = self.get_hardware_id()
            hw_key = hw_id.replace('-', '')
            FIREBASE_URL = self.get_firebase_url()
            
            # Fetch default trial configurations from Firebase with SSL cert fallback
            default_trial_days = 3
            default_trial_limit = 100
            trial_enabled = True
            try:
                def _get_config():
                    try:
                        return requests.get(f'{FIREBASE_URL}/app_config.json', timeout=5)
                    except requests.exceptions.SSLError:
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                        return requests.get(f'{FIREBASE_URL}/app_config.json', timeout=5, verify=False)
                
                cfg_r = _get_config()
                if cfg_r.status_code == 401 or (cfg_r.status_code == 200 and isinstance(cfg_r.json(), dict) and "error" in cfg_r.json()):
                    raise requests.exceptions.RequestException("Firebase Permission Denied (401)")
                
                if cfg_r.status_code == 200:
                    cfg_data = cfg_r.json() or {}
                    default_trial_days = int(cfg_data.get('default_trial_days', 3))
                    default_trial_limit = int(cfg_data.get('default_trial_limit', 100))
                    trial_enabled = cfg_data.get('trial_enabled', True)
                    self.proxy_list = cfg_data.get('proxies', [])
            except requests.exceptions.RequestException as req_ex:
                raise req_ex
            except Exception as e:
                print(f"Error fetching default trial settings: {e}")

            # Fetch license data
            def _get_license():
                try:
                    return requests.get(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', timeout=10)
                except requests.exceptions.SSLError:
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    return requests.get(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', timeout=10, verify=False)
                    
            r = _get_license()
            if r.status_code == 401 or (r.status_code == 200 and isinstance(r.json(), dict) and "error" in r.json()):
                raise requests.exceptions.RequestException("Firebase Permission Denied (401)")
                
            data = r.json() if r.status_code == 200 else None

            if not data:
                if trial_enabled:
                    # First time -- start trial
                    trial_data = {
                        'hardware_id': hw_id,
                        'status': 'trial',
                        'trial_started': datetime.datetime.utcnow().isoformat() + 'Z',
                        'trial_days': default_trial_days,
                        'activated_at': None,
                        'license_key': None,
                        'username': sys_user,
                        'expiry': None,
                        'plan': 'Trial Plan',
                        'notes': '',
                        'downloads_count': 0,
                        'lifetime_downloads': 0,
                        'downloads_month': current_month
                    }
                    
                    try:
                        requests.put(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json=trial_data, timeout=10)
                    except requests.exceptions.SSLError:
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                        requests.put(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json=trial_data, timeout=10, verify=False)
                        
                    self.downloads_count = 0
                    self.lifetime_downloads = 0
                    self.downloads_limit = default_trial_limit
                    self.plan_name = 'Trial Plan'
                    res = {
                        'status': 'trial',
                        'days_remaining': default_trial_days,
                        'hardware_id': hw_id,
                        'username': sys_user,
                        'plan': 'Trial Plan',
                        'downloads_count': 0,
                        'lifetime_downloads': 0,
                        'downloads_limit': default_trial_limit
                    }
                else:
                    # No trial enabled -- user must activate
                    inactive_data = {
                        'hardware_id': hw_id,
                        'status': 'inactive',
                        'trial_started': None,
                        'trial_days': 0,
                        'activated_at': None,
                        'license_key': None,
                        'username': sys_user,
                        'expiry': None,
                        'plan': 'Unactivated',
                        'notes': 'Trial disabled by Admin',
                        'downloads_count': 0,
                        'lifetime_downloads': 0,
                        'downloads_month': current_month
                    }
                    try:
                        requests.put(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json=inactive_data, timeout=10)
                    except requests.exceptions.SSLError:
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                        requests.put(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json=inactive_data, timeout=10, verify=False)
                    
                    self.downloads_count = 0
                    self.lifetime_downloads = 0
                    self.downloads_limit = 0
                    self.plan_name = 'Unactivated'
                    res = {
                        'status': 'inactive',
                        'days_remaining': 0,
                        'hardware_id': hw_id,
                        'username': sys_user,
                        'plan': 'Unactivated',
                        'downloads_count': 0,
                        'lifetime_downloads': 0,
                        'downloads_limit': 0
                    }
                
                self._save_license_cache(res)
                return res

            status = data.get('status', 'trial')
            username = data.get('username', '').strip() or sys_user
            plan = data.get('plan', '').strip() or ('Trial Plan' if status == 'trial' else 'Active Plan')

            # Reset downloads count if new calendar month
            downloads_count = data.get('downloads_count', 0)
            lifetime_downloads = data.get('lifetime_downloads', downloads_count)
            downloads_month = data.get('downloads_month', '')
            
            if downloads_month != current_month:
                downloads_count = 0
                downloads_month = current_month
                try:
                    try:
                        requests.patch(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json={
                            'downloads_count': 0,
                            'downloads_month': current_month
                        }, timeout=5)
                    except requests.exceptions.SSLError:
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                        requests.patch(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json={
                            'downloads_count': 0,
                            'downloads_month': current_month
                        }, timeout=5, verify=False)
                except Exception as e:
                    print(f"Failed to reset monthly downloads: {e}")

            PLAN_LIMITS = {
                'Basic': 10000,
                'Pro': 50000,
                'Premium': 999999,
                'Trial': default_trial_limit,
                'Trial Plan': default_trial_limit
            }
            limit = 999999
            for pk, pv in PLAN_LIMITS.items():
                if pk.lower() in plan.lower():
                    limit = pv
                    break

            self.downloads_count = downloads_count
            self.lifetime_downloads = lifetime_downloads
            self.downloads_limit = limit
            self.plan_name = plan
            
            # Save settings
            try:
                settings = self._load_settings()
                settings['lifetime_downloads'] = self.lifetime_downloads
                self._save_settings(settings)
            except:
                pass

            if status == 'active':
                # Check expiry
                expiry = data.get('expiry')
                days_remaining = None
                if expiry:
                    try:
                        exp_date = datetime.datetime.fromisoformat(expiry)
                        if datetime.datetime.utcnow() > exp_date:
                            requests.patch(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json={'status': 'expired'}, timeout=10)
                            res = {
                                'status': 'expired',
                                'hardware_id': hw_id,
                                'username': username,
                                'plan': plan,
                                'downloads_count': downloads_count,
                                'lifetime_downloads': lifetime_downloads,
                                'downloads_limit': limit
                            }
                            self._save_license_cache(res)
                            return res
                        
                        # Calculate days remaining
                        delta = exp_date - datetime.datetime.utcnow()
                        days_remaining = max(0, delta.days)
                    except Exception as e:
                        print(f"Error parsing expiry date '{expiry}': {e}")
                res = {
                    'status': 'active',
                    'hardware_id': hw_id,
                    'username': username,
                    'plan': plan,
                    'days_remaining': days_remaining,
                    'expiry': expiry,
                    'downloads_count': downloads_count,
                    'lifetime_downloads': lifetime_downloads,
                    'downloads_limit': limit
                }
                self._save_license_cache(res)
                return res

            elif status == 'trial':
                started = data.get('trial_started')
                trial_days = data.get('trial_days', 3)
                
                # If trial_started is not set, set it now to activate the trial on first run
                if not started:
                    started = datetime.datetime.utcnow().isoformat()
                    try:
                        requests.patch(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json={'trial_started': started}, timeout=10)
                    except Exception as e:
                        print(f"Failed to update trial_started on activation: {e}")
                
                try:
                    start_dt = datetime.datetime.fromisoformat(started)
                    elapsed = (datetime.datetime.utcnow() - start_dt).days
                    remaining = max(0, trial_days - elapsed)
                    if remaining <= 0:
                        requests.patch(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json={'status': 'expired'}, timeout=10)
                        res = {
                            'status': 'expired',
                            'hardware_id': hw_id,
                            'username': username,
                            'plan': plan,
                            'downloads_count': downloads_count,
                            'lifetime_downloads': lifetime_downloads,
                            'downloads_limit': limit
                        }
                        self._save_license_cache(res)
                        return res
                    res = {
                        'status': 'trial',
                        'days_remaining': remaining,
                        'hardware_id': hw_id,
                        'username': username,
                        'plan': f'Trial ({remaining} Days)',
                        'downloads_count': downloads_count,
                        'lifetime_downloads': lifetime_downloads,
                        'downloads_limit': limit
                    }
                    self._save_license_cache(res)
                    return res
                except:
                    res = {
                        'status': 'trial',
                        'days_remaining': trial_days,
                        'hardware_id': hw_id,
                        'username': username,
                        'plan': f'Trial ({trial_days} Days)',
                        'downloads_count': downloads_count,
                        'lifetime_downloads': lifetime_downloads,
                        'downloads_limit': limit
                    }
                    self._save_license_cache(res)
                    return res

            elif status == 'revoked':
                res = {
                    'status': 'revoked',
                    'hardware_id': hw_id,
                    'username': username,
                    'plan': 'Revoked Plan',
                    'downloads_count': downloads_count,
                    'lifetime_downloads': lifetime_downloads,
                    'downloads_limit': limit
                }
                self._save_license_cache(res)
                return res

            else:
                res = {
                    'status': 'expired',
                    'hardware_id': hw_id,
                    'username': username,
                    'plan': plan,
                    'downloads_count': downloads_count,
                    'lifetime_downloads': lifetime_downloads,
                    'downloads_limit': limit
                }
                self._save_license_cache(res)
                return res

        except Exception as e:
            # If offline, check local cache
            return self._check_license_offline()

    def _check_license_offline(self):
        """Fallback offline license check from local cache file."""
        import getpass
        sys_user = getpass.getuser().capitalize()
        try:
            cache_path = os.path.join(os.environ.get('APPDATA', ''), 'HKDownloader', 'license.cache')
            if os.path.exists(cache_path):
                with open(cache_path, 'r') as f:
                    import json as _j
                    data = _j.load(f)
                    return data
        except: pass
        hw_id = self.get_hardware_id()
        return {'status': 'trial', 'days_remaining': 3, 'hardware_id': hw_id, 'username': sys_user, 'plan': 'Trial Plan', 'lifetime_downloads': self.lifetime_downloads}

    def _save_license_cache(self, status_data):
        """Save license status to local cache for offline use."""
        try:
            cache_dir = os.path.join(os.environ.get('APPDATA', ''), 'HKDownloader')
            os.makedirs(cache_dir, exist_ok=True)
            cache_path = os.path.join(cache_dir, 'license.cache')
            with open(cache_path, 'w') as f:
                import json as _j
                _j.dump(status_data, f)
        except: pass

    def activate_license(self, license_key):
        """Validate and activate a license key for this device."""
        import getpass
        sys_user = getpass.getuser().capitalize()
        try:
            import hmac as _hmac, hashlib, datetime
            hw_id = self.get_hardware_id()
            hw_key = hw_id.replace('-', '')
            SECRET = 'HKDownloaderPro_License_Secret_2024'
            expected_raw = _hmac.new(SECRET.encode(), hw_id.encode(), hashlib.sha256).hexdigest()[:20].upper()
            expected_key = '-'.join(expected_raw[i:i+4] for i in range(0, 20, 4))

            if license_key.strip().upper() != expected_key:
                return {'success': False, 'error': 'Invalid license key. Please check and try again.'}

            FIREBASE_URL = self.get_firebase_url()
            update_data = {
                'status': 'active',
                'license_key': license_key.strip().upper(),
                'activated_at': datetime.datetime.utcnow().isoformat() + 'Z'
            }
            requests.patch(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json=update_data, timeout=10)
            self._save_license_cache({'status': 'active', 'hardware_id': hw_id, 'username': sys_user, 'plan': 'Active Plan'})
            return {'success': True, 'msg': 'License activated successfully! Welcome to HK Downloader Pro.'}
        except Exception as e:
            return {'success': False, 'error': f'Activation error: {str(e)}'}

    def get_whatsapp_contact(self):
        """Fetch WhatsApp number and group link from Firebase."""
        try:
            FIREBASE_URL = self.get_firebase_url()
            r = requests.get(f'{FIREBASE_URL}/contact.json', timeout=8)
            data = r.json() if r.status_code == 200 and r.json() else {}
            return {
                'success': True,
                'whatsapp_number': data.get('whatsapp_number', ''),
                'whatsapp_group': data.get('whatsapp_group', '')
            }
        except:
            return {'success': False, 'whatsapp_number': '', 'whatsapp_group': ''}

    def get_plans(self):
        """Fetch subscription plans from Firebase."""
        try:
            FIREBASE_URL = self.get_firebase_url()
            r = requests.get(f'{FIREBASE_URL}/plans.json', timeout=8)
            data = r.json() if r.status_code == 200 and r.json() else {}
            plans = []
            for k, v in data.items():
                if v.get('is_active', True):
                    plans.append(v)
            plans.sort(key=lambda x: x.get('order', 99))
            return {'success': True, 'plans': plans}
        except:
            # Default plans if Firebase unreachable
            return {'success': True, 'plans': [
                {'name': 'Basic', 'price': 'Contact Admin', 'duration': '1 Month', 'features': ['TikTok', 'YouTube', 'Instagram', 'Facebook', 'Pinterest']},
                {'name': 'Pro', 'price': 'Contact Admin', 'duration': '3 Months', 'features': ['All Platforms', 'Bulk Download', 'Douyin', 'Kuaishou']},
                {'name': 'Premium', 'price': 'Contact Admin', 'duration': 'Lifetime', 'features': ['All Features', 'Priority Support', 'Future Updates']}
            ]}

    # =====================================================================
    # AUTO-UPDATE METHODS (v3.0)
    # =====================================================================

    def get_local_extension_version(self):
        """Read local extension version from manifest.json."""
        try:
            if getattr(sys, 'frozen', False):
                ext_path = os.path.join(os.path.dirname(sys.executable), 'hk_extension')
            else:
                ext_path = os.path.join(os.path.abspath('.'), 'hk_extension')
            
            if not os.path.exists(ext_path):
                ext_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hk_extension')
            
            manifest_path = os.path.join(ext_path, "manifest.json")
            if os.path.exists(manifest_path):
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data.get("version", "1.0.0")
        except Exception:
            pass
        return "1.0.0"

    def check_for_update(self):
        """Check Firebase for latest version info & extension updates."""
        try:
            FIREBASE_URL = self.get_firebase_url()
            r = requests.get(f'{FIREBASE_URL}/app_config.json', timeout=8)
            data = r.json() if r.status_code == 200 and r.json() else {}
            latest = data.get('latest_version', CURRENT_VERSION)
            dl_url = data.get('download_url', '')
            changelog = data.get('changelog', '')

            def version_tuple(v):
                return tuple(int(x) for x in v.split('.'))

            has_update = version_tuple(latest) > version_tuple(CURRENT_VERSION)
            
            # Extension update check
            srv_ext_version = data.get("latest_ext_version", "1.0.0")
            local_ext_version = self.get_local_extension_version()
            
            has_ext_update = version_tuple(srv_ext_version) > version_tuple(local_ext_version)
            
            return {
                'success': True,
                'has_update': has_update,
                'latest_version': latest,
                'current_version': CURRENT_VERSION,
                'download_url': dl_url,
                'changelog': changelog,
                'has_ext_update': has_ext_update,
                'latest_ext_version': srv_ext_version,
                'current_ext_version': local_ext_version
            }
        except Exception as e:
            return {'success': False, 'has_update': False, 'error': str(e)}

    def apply_extension_update(self):
        """Downloads updated extension base64 zip from Firebase and extracts it locally."""
        try:
            FIREBASE_URL = self.get_firebase_url()
            try:
                r = requests.get(f'{FIREBASE_URL}/extension_payload.json', timeout=20)
            except requests.exceptions.SSLError:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                r = requests.get(f'{FIREBASE_URL}/extension_payload.json', timeout=20, verify=False)
                
            if r.status_code != 200 or not r.json():
                return {"success": False, "error": "Failed to fetch extension update from server"}
            
            data = r.json()
            b64_zip = data.get("zip_base64")
            if not b64_zip:
                return {"success": False, "error": "No extension package found on server"}
            
            import base64
            import zipfile
            import io
            import shutil
            
            zip_bytes = base64.b64decode(b64_zip)
            zip_buffer = io.BytesIO(zip_bytes)
            
            # Resolve target local extension folder path
            if getattr(sys, 'frozen', False):
                ext_path = os.path.join(os.path.dirname(sys.executable), 'hk_extension')
            else:
                ext_path = os.path.join(os.path.abspath('.'), 'hk_extension')
                
            if not os.path.exists(ext_path):
                fallback_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hk_extension')
                if os.path.exists(fallback_path):
                    ext_path = fallback_path
            
            # Create if it doesn't exist
            os.makedirs(ext_path, exist_ok=True)
            
            # Clean existing files/directories safely
            for item in os.listdir(ext_path):
                item_path = os.path.join(ext_path, item)
                try:
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    else:
                        os.remove(item_path)
                except Exception as clean_err:
                    print(f"Error clearing item {item_path}: {clean_err}")
            
            # Extract zip contents
            with zipfile.ZipFile(zip_buffer, 'r') as zf:
                zf.extractall(ext_path)
                
            print(f"[Extension Update] Successfully updated to version {data.get('version')}")
            return {"success": True, "version": data.get("version")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def download_update(self, download_url):
        """Download update EXE in background, push progress to JS."""
        def _do_download():
            try:
                global _app_window
                import tempfile, os
                tmp_path = os.path.join(tempfile.gettempdir(), 'HKDownloader_update.exe')

                def _push(pct, msg):
                    try:
                        if _app_window:
                            _app_window.evaluate_js(f'onUpdateProgress({json.dumps({"percent": pct, "msg": msg})})')
                    except: pass

                _push(0, 'Starting download...')
                headers = {'User-Agent': 'Mozilla/5.0'}
                r = requests.get(download_url, headers=headers, stream=True, timeout=60)
                total = int(r.headers.get('content-length', 0))
                downloaded = 0

                with open(tmp_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            pct = int((downloaded / total) * 100) if total > 0 else 0
                            _push(pct, f'Downloading... {pct}%')

                _push(100, 'Download complete!')
                if _app_window:
                    _app_window.evaluate_js('onUpdateDownloadComplete()')
            except Exception as ex:
                try:
                    if _app_window:
                        _app_window.evaluate_js(f'onUpdateError({json.dumps(str(ex))})')
                except: pass

        threading.Thread(target=_do_download, daemon=True).start()
        return {'success': True, 'msg': 'Download started'}

    def apply_update_and_restart(self):
        """Perform the update by either launching installer or replacing executable via rename trick."""
        try:
            import tempfile, sys, subprocess, os, shutil
            tmp_path = os.path.join(tempfile.gettempdir(), 'HKDownloader_update.exe')
            if not os.path.exists(tmp_path):
                return {'success': False, 'error': 'Update file not found. Please download again.'}

            # Check if frozen (compiled EXE)
            is_frozen = getattr(sys, 'frozen', False)
            current_exe = os.path.abspath(sys.executable) if is_frozen else os.path.abspath(sys.argv[0])
            
            # Determine if the downloaded update is an installer Setup
            is_setup = False
            if hasattr(self, 'update_download_url') and self.update_download_url:
                is_setup = "setup" in self.update_download_url.lower()
            else:
                is_setup = "setup" in tmp_path.lower() or os.path.getsize(tmp_path) > 30 * 1024 * 1024
            
            if is_setup:
                # 1. Run the installer setup directly
                subprocess.Popen([tmp_path], shell=True)
                os._exit(0)
            else:
                # 2. Direct EXE replacement (portable update) via Windows Rename Trick!
                if is_frozen:
                    old_exe = current_exe + ".old"
                    if os.path.exists(old_exe):
                        try:
                            os.remove(old_exe)
                        except:
                            import time
                            old_exe = current_exe + f".{int(time.time())}.old"
                    
                    # Rename the running executable (allowed on Windows!)
                    os.rename(current_exe, old_exe)
                    
                    # Copy the new executable to the original path
                    shutil.copy2(tmp_path, current_exe)
                    
                    # Launch the new executable
                    subprocess.Popen([current_exe])
                    os._exit(0)
                else:
                    # Direct Python testing fallback
                    subprocess.Popen([tmp_path], shell=True)
                    os._exit(0)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def check_link_type(self, url):
        """Analyze Douyin/Kuaishou URL to determine if it is a single video or profile."""
        url = url.strip()
        if not url:
            return {"success": False, "error": "Empty URL"}
            
        # Follow redirects if short link
        resolved_url = url
        if any(x in url for x in ['v.douyin.com', 'v.kuaishou.com', 'gifshow.com', 'kuaishou.com/f/']):
            try:
                resolved_url = resolve_redirects(url)
            except Exception:
                pass
                
        # Now analyze the resolved URL
        platform = ""
        is_profile = False
        
        if 'douyin.com' in resolved_url or url.startswith('@'):
            platform = "douyin"
            if '/user/' in resolved_url or '@' in resolved_url or url.startswith('@'):
                is_profile = True
            else:
                is_profile = False
        elif any(x in resolved_url for x in ['kuaishou.com', 'gifshow.com']):
            platform = "kuaishou"
            if '/profile/' in resolved_url or '/user/' in resolved_url:
                is_profile = True
            else:
                is_profile = False
                
        return {
            "success": True,
            "platform": platform,
            "is_profile": is_profile,
            "resolved_url": resolved_url
        }

    # =====================================================================
    # DOUYIN DOWNLOAD METHODS (v3.0)
    # =====================================================================

    def download_douyin_video(self, url, download_dir=None):
        """Download a single Douyin video via tikwm API."""
        if not url or not url.strip():
            return {'success': False, 'error': 'Please enter a Douyin video URL.'}
        dest_dir = download_dir or self.douyin_download_folder
        if not dest_dir:
            return {'success': False, 'error': 'Please select a Douyin download folder first.'}
        try:
            api_url = 'https://www.tikwm.com/api/'
            params = {'url': url.strip(), 'hd': 1}
            headers = {'User-Agent': 'Mozilla/5.0'}
            r = requests.get(api_url, params=params, headers=headers, timeout=15)
            data = r.json()
            if data.get('code') != 0:
                # Try Cobalt fallback
                cobalt = self._try_cobalt_generic(url.strip(), "douyin_fallback")
                if cobalt:
                    thread = threading.Thread(target=self._perform_download, args=(cobalt['video_id'], cobalt['play'], cobalt['title'], dest_dir))
                    thread.daemon = True
                    thread.start()
                    return {'success': True, 'msg': 'Douyin download started (via Cobalt fallback)', 'title': cobalt['title'], 'video_id': cobalt['video_id'], 'play_url': cobalt['play']}
                return {'success': False, 'error': data.get('msg', 'Could not fetch video info.')}
            video_data = data.get('data', {})
            play_url = video_data.get('hdplay') or video_data.get('play')
            title = video_data.get('title', 'Douyin_Video')
            video_id = video_data.get('id', url)
            if not play_url:
                # Try Cobalt fallback as second chance
                cobalt = self._try_cobalt_generic(url.strip(), "douyin_fallback")
                if cobalt:
                    thread = threading.Thread(target=self._perform_download, args=(cobalt['video_id'], cobalt['play'], cobalt['title'], dest_dir))
                    thread.daemon = True
                    thread.start()
                    return {'success': True, 'msg': 'Douyin download started (via Cobalt fallback)', 'title': cobalt['title'], 'video_id': cobalt['video_id'], 'play_url': cobalt['play']}
                return {'success': False, 'error': 'No download URL found.'}
            thread = threading.Thread(target=self._perform_download, args=(video_id, play_url, title, dest_dir))
            thread.daemon = True
            thread.start()
            return {'success': True, 'msg': 'Douyin download started', 'title': title, 'video_id': video_id, 'play_url': play_url}
        except Exception as e:
            # Last resort: Try Cobalt fallback on exception
            try:
                cobalt = self._try_cobalt_generic(url.strip(), "douyin_fallback")
                if cobalt:
                    thread = threading.Thread(target=self._perform_download, args=(cobalt['video_id'], cobalt['play'], cobalt['title'], dest_dir))
                    thread.daemon = True
                    thread.start()
                    return {'success': True, 'msg': 'Douyin download started (via Cobalt fallback)', 'title': cobalt['title'], 'video_id': cobalt['video_id'], 'play_url': cobalt['play']}
            except:
                pass
            return {'success': False, 'error': f'Error: {str(e)}'}

    def fetch_douyin_profile_progressive(self, username):
        """Progressive fetch of Douyin profile videos via tikwm API."""
        if not self.douyin_download_folder:
            return {'success': False, 'error': 'Please select a Douyin download folder first.'}

        username = username.strip()
        # Parse URL to username
        if 'douyin.com' in username:
            if '@' in username:
                after_at = username.split('@')[1]
                username = re.split(r'[/?]', after_at)[0]
            else:
                try:
                    r = requests.get(username, headers={'User-Agent': 'Mozilla/5.0'}, allow_redirects=True, timeout=8)
                    final_url = r.url
                    if '@' in final_url:
                        username = re.split(r'[/?]', final_url.split('@')[1])[0]
                except: pass
        if username.startswith('@'):
            username = username[1:]
        if not username:
            return {'success': False, 'error': 'Invalid Douyin username or URL.'}

        target_dir = os.path.join(self.douyin_download_folder, f'@douyin_{username}')
        os.makedirs(target_dir, exist_ok=True)
        metadata = self._get_folder_metadata(target_dir)
        downloaded_ids = metadata.get('downloaded_ids', {})
        max_seq = metadata.get('max_seq', 0)

        existing_basenames = set()
        for fname in os.listdir(target_dir):
            if fname.lower().endswith('.mp4'):
                existing_basenames.add(fname[:-4].lower())

        def _notify(msg):
            try:
                global _app_window
                if _app_window:
                    _app_window.evaluate_js(f"logToConsole({json.dumps(msg)}, 'info')")
            except: pass

        def _push_batch(batch, is_done, skipped, seq_offset):
            try:
                global _app_window
                if not _app_window: return
                cleaned = [{
                    'video_id': v.get('video_id'), 'title': v.get('title', 'Douyin Video'),
                    'play': v.get('play'), 'hdplay': v.get('hdplay'), 'wmplay': v.get('wmplay'),
                    'cover': v.get('cover'), 'duration': v.get('duration'),
                    'sequenceNumber': seq_offset + i + 1, 'download_dir': target_dir,
                    'author': {'unique_id': v.get('author', {}).get('unique_id'), 'nickname': v.get('author', {}).get('nickname'), 'avatar': v.get('author', {}).get('avatar')}
                } for i, v in enumerate(batch)]
                payload = json.dumps({'videos': cleaned, 'isDone': is_done, 'skippedCount': skipped})
                _app_window.evaluate_js(f'onDouyinPageFetched({payload})')
            except Exception as ex:
                print(f'Douyin push error: {ex}')

        def _run():
            import time as _t
            api_url = 'https://www.tikwm.com/api/user/posts'
            headers = {'User-Agent': 'Mozilla/5.0'}
            cur = '0'; page = 0; MAX_PAGES = 200
            total_skipped = 0; total_fetched = 0
            try:
                while page < MAX_PAGES:
                    page += 1
                    _notify(f'[Douyin] Fetching page {page}...')
                    params = {'unique_id': username, 'count': 35, 'cursor': cur}
                    resp = None
                    for _ in range(3):
                        try:
                            resp = requests.get(api_url, params=params, headers=headers, timeout=15)
                            if resp.status_code == 200: break
                        except: pass
                        _t.sleep(1.5)
                    if not resp or resp.status_code != 200:
                        _push_batch([], True, total_skipped, max_seq + total_fetched); break
                    res = resp.json()
                    if res.get('code') != 0:
                        if page == 1:
                            try:
                                global _app_window
                                if _app_window: _app_window.evaluate_js(f'onDouyinFetchError({json.dumps(res.get("msg", "Profile not found."))})')
                            except: pass
                            return
                        _push_batch([], True, total_skipped, max_seq + total_fetched); break
                    data = res.get('data', {})
                    videos = data.get('videos', [])
                    has_more = data.get('hasMore', False)
                    cur = data.get('cursor', '0')
                    page_new = []; hit_existing = False
                    for v in videos:
                        vid_id = v.get('video_id')
                        if not vid_id: continue
                        in_meta = vid_id in downloaded_ids
                        base = sanitize_filename(v.get('title', '')).lower()
                        file_exists = False
                        if vid_id:
                            for existing_name in existing_basenames:
                                if str(vid_id) in existing_name:
                                    file_exists = True
                                    break
                        if not file_exists:
                            file_exists = (base in existing_basenames or
                                           any(f"{base}_{i}" in existing_basenames for i in range(1, 15)))
                        if self.skip_duplicates and (in_meta or file_exists):
                            hit_existing = True; total_skipped += 1
                            if not in_meta: downloaded_ids[vid_id] = base + '.mp4'
                        else:
                            page_new.append(v)
                    page_new.reverse()
                    is_last = (hit_existing and self.skip_duplicates) or not has_more or not cur or cur == '0'
                    if page_new or is_last:
                        _push_batch(page_new, is_last, total_skipped if is_last else 0, max_seq + total_fetched)
                        total_fetched += len(page_new)
                    if is_last: break
                _notify(f'[Douyin] Done. {total_fetched} new videos fetched.')
            except Exception as ex:
                _notify(f'[Douyin Error] {str(ex)}')

        threading.Thread(target=_run, daemon=True).start()
        return {'success': True, 'msg': 'Douyin progressive fetch started'}

    # =====================================================================
    # KUAISHOU DOWNLOAD METHODS (v3.0)
    # =====================================================================

    def _get_kuaishou_did(self):
        """Get or generate a Kuaishou device ID (did) cookie."""
        import uuid
        did_file = os.path.join(os.environ.get('APPDATA', ''), 'HKDownloader', 'ks_did.txt')
        try:
            os.makedirs(os.path.dirname(did_file), exist_ok=True)
            if os.path.exists(did_file):
                with open(did_file, 'r') as f:
                    return f.read().strip()
            did = str(uuid.uuid4()).replace('-', '')
            with open(did_file, 'w') as f:
                f.write(did)
            return did
        except:
            return str(uuid.uuid4()).replace('-', '')

    def _kuaishou_resolve_user_id(self, user_input):
        """Extract Kuaishou user ID from URL or return as-is."""
        import re
        user_input = user_input.strip()
        # Pattern: kuaishou.com/profile/USER_ID
        m = re.search(r'kuaishou\.com/profile/([\w-]+)', user_input)
        if m:
            return m.group(1)
        # Short link -- follow redirect
        if 'v.kuaishou.com' in user_input or 'gifshow.com' in user_input:
            try:
                r = requests.get(user_input, headers={'User-Agent': 'Mozilla/5.0'}, allow_redirects=True, timeout=8)
                m2 = re.search(r'kuaishou\.com/profile/([\w-]+)', r.url)
                if m2:
                    return m2.group(1)
            except: pass
        # If it looks like an ID directly
        if re.match(r'^[\w-]{5,30}$', user_input):
            return user_input
        return user_input

    def _kuaishou_get_video_url(self, photo_id):
        """Get download URL for a single Kuaishou video via GraphQL."""
        try:
            did = self._get_kuaishou_did()
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Content-Type': 'application/json',
                'Referer': 'https://www.kuaishou.com/',
                'Cookie': f'did={did}; didv=1'
            }
            payload = {
                'operationName': 'visionVideoDetail',
                'variables': {'photoId': photo_id, 'type': 'DETAIL'},
                'query': 'query visionVideoDetail($photoId: String, $type: String) { visionVideoDetail(photoId: $photoId, type: $type) { photo { id caption mainMvUrls { url } coverUrls { url } duration } } }'
            }
            r = requests.post('https://www.kuaishou.com/graphql', json=payload, headers=headers, timeout=15)
            data = r.json()
            photo = data.get('data', {}).get('visionVideoDetail', {}).get('photo', {})
            urls = photo.get('mainMvUrls', [])
            if urls:
                return {
                    'video_id': photo.get('id', photo_id),
                    'title': photo.get('caption', 'Kuaishou_Video'),
                    'play_url': urls[0].get('url', ''),
                    'cover': (photo.get('coverUrls') or [{}])[0].get('url', ''),
                    'duration': photo.get('duration', 0)
                }
            return None
        except:
            return None

    def download_kuaishou_video(self, url, download_dir=None):
        """Download a single Kuaishou video."""
        if not url or not url.strip():
            return {'success': False, 'error': 'Please enter a Kuaishou video URL.'}
        dest_dir = download_dir or self.kuaishou_download_folder
        if not dest_dir:
            return {'success': False, 'error': 'Please select a Kuaishou download folder first.'}
        try:
            import re
            url = url.strip()
            # Follow short links
            if 'v.kuaishou.com' in url:
                r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, allow_redirects=True, timeout=8)
                url = r.url
            # Extract photo ID
            m = re.search(r'/(?:short-video|photo)/([\w-]+)', url)
            photo_id = m.group(1) if m else url.split('/')[-1].split('?')[0]

            info = self._kuaishou_get_video_url(photo_id)
            if not info or not info.get('play_url'):
                # Try Cobalt fallback
                cobalt = self._try_cobalt_generic(url, photo_id)
                if cobalt:
                    thread = threading.Thread(target=self._perform_download,
                        args=(cobalt['video_id'], cobalt['play'], cobalt['title'], dest_dir))
                    thread.daemon = True
                    thread.start()
                    return {'success': True, 'msg': 'Kuaishou download started (via Cobalt fallback)', 'title': cobalt['title'],
                            'video_id': cobalt['video_id'], 'play_url': cobalt['play']}
                return {'success': False, 'error': 'Could not fetch video URL. Try another link.'}

            thread = threading.Thread(target=self._perform_download,
                args=(info['video_id'], info['play_url'], info['title'], dest_dir))
            thread.daemon = True
            thread.start()
            return {'success': True, 'msg': 'Kuaishou download started', 'title': info['title'],
                    'video_id': info['video_id'], 'play_url': info['play_url']}
        except Exception as e:
            # Last resort: Try Cobalt fallback on exception
            try:
                cobalt = self._try_cobalt_generic(url, "kuaishou_fallback")
                if cobalt:
                    thread = threading.Thread(target=self._perform_download,
                        args=(cobalt['video_id'], cobalt['play'], cobalt['title'], dest_dir))
                    thread.daemon = True
                    thread.start()
                    return {'success': True, 'msg': 'Kuaishou download started (via Cobalt fallback)', 'title': cobalt['title'],
                            'video_id': cobalt['video_id'], 'play_url': cobalt['play']}
            except:
                pass
            return {'success': False, 'error': f'Error: {str(e)}'}

    def fetch_kuaishou_profile_progressive(self, user_input):
        """Progressive fetch of all videos from a Kuaishou profile."""
        if not self.kuaishou_download_folder:
            return {'success': False, 'error': 'Please select a Kuaishou download folder first.'}

        user_id = self._kuaishou_resolve_user_id(user_input)
        if not user_id:
            return {'success': False, 'error': 'Invalid Kuaishou profile URL or user ID.'}

        target_dir = os.path.join(self.kuaishou_download_folder, f'@ks_{user_id}')
        os.makedirs(target_dir, exist_ok=True)
        metadata = self._get_folder_metadata(target_dir)
        downloaded_ids = set(metadata.get('downloaded_ids', {}).keys())

        def _notify(msg):
            try:
                global _app_window
                if _app_window:
                    _app_window.evaluate_js(f"logToConsole({json.dumps(msg)}, 'info')")
            except: pass

        def _push_batch(batch, is_done, skipped, seq_offset):
            try:
                global _app_window
                if not _app_window: return
                cleaned = [{
                    'video_id': v.get('video_id'), 'title': v.get('title', 'Kuaishou Video'),
                    'play': v.get('play_url'), 'hdplay': v.get('play_url'), 'wmplay': v.get('play_url'),
                    'cover': v.get('cover', ''), 'duration': v.get('duration', 0),
                    'sequenceNumber': seq_offset + i + 1, 'download_dir': target_dir,
                    'author': {'unique_id': user_id, 'nickname': user_id, 'avatar': ''}
                } for i, v in enumerate(batch)]
                payload = json.dumps({'videos': cleaned, 'isDone': is_done, 'skippedCount': skipped})
                _app_window.evaluate_js(f'onKuaishouPageFetched({payload})')
            except Exception as ex:
                print(f'Kuaishou push error: {ex}')

        def _run():
            import time as _t
            did = self._get_kuaishou_did()
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Content-Type': 'application/json',
                'Referer': f'https://www.kuaishou.com/profile/{user_id}',
                'Cookie': f'did={did}; didv=1'
            }
            pcursor = ''; page = 0; MAX_PAGES = 100
            total_skipped = 0; total_fetched = 0

            try:
                while page < MAX_PAGES:
                    page += 1
                    _notify(f'[Kuaishou] Fetching page {page}...')
                    payload = {
                        'operationName': 'visionProfilePhotoList',
                        'variables': {'userId': user_id, 'pcursor': pcursor, 'page': 'profile'},
                        'query': 'query visionProfilePhotoList($userId: String, $pcursor: String, $page: String) { visionProfilePhotoList(userId: $userId, pcursor: $pcursor, page: $page) { result pcursor feeds { photo { id caption mainMvUrls { url } coverUrls { url } duration } } } }'
                    }
                    try:
                        r = requests.post('https://www.kuaishou.com/graphql', json=payload, headers=headers, timeout=20)
                        data = r.json()
                    except Exception as ex:
                        _notify(f'[Kuaishou Error] {ex}')
                        _push_batch([], True, total_skipped, total_fetched)
                        break

                    profile_data = data.get('data', {}).get('visionProfilePhotoList', {})
                    if not profile_data:
                        if page == 1:
                            try:
                                global _app_window
                                if _app_window: _app_window.evaluate_js(f'onKuaishouFetchError({json.dumps("Profile not found or private.")})')
                            except: pass
                            return
                        _push_batch([], True, total_skipped, total_fetched)
                        break

                    feeds = profile_data.get('feeds', [])
                    new_pcursor = profile_data.get('pcursor', '')
                    has_more = new_pcursor and new_pcursor != 'no_more'

                    page_new = []
                    for feed in feeds:
                        photo = feed.get('photo', {})
                        vid_id = photo.get('id')
                        if not vid_id: continue
                        if self.skip_duplicates and vid_id in downloaded_ids:
                            total_skipped += 1
                            continue
                        urls = photo.get('mainMvUrls', [])
                        play_url = urls[0].get('url', '') if urls else ''
                        if not play_url: continue
                        page_new.append({
                            'video_id': vid_id,
                            'title': photo.get('caption', f'KS_{vid_id}'),
                            'play_url': play_url,
                            'cover': (photo.get('coverUrls') or [{}])[0].get('url', ''),
                            'duration': photo.get('duration', 0)
                        })

                    pcursor = new_pcursor
                    is_last = not has_more or not feeds
                    if page_new or is_last:
                        _push_batch(page_new, is_last, total_skipped if is_last else 0, total_fetched)
                        total_fetched += len(page_new)
                    if is_last: break
                    _t.sleep(0.5)  # be nice to the API

                _notify(f'[Kuaishou] Done. {total_fetched} new videos fetched.')
            except Exception as ex:
                _notify(f'[Kuaishou Error] {str(ex)}')

        threading.Thread(target=_run, daemon=True).start()
        return {'success': True, 'msg': 'Kuaishou progressive fetch started'}




    def download_video(self, video_id, play_url, title, download_dir=None):
        """Initiates downloading a video in a background thread."""
        thread = threading.Thread(target=self._perform_download, args=(video_id, play_url, title, download_dir))
        thread.daemon = True
        thread.start()
        return {"success": True, "msg": "Download started in background"}

    def _perform_download(self, video_id, play_url, title, download_dir=None):
        """Downloads the video file chunk-by-chunk and updates the UI progress."""
        self.active_download_count += 1
        try:
            dest_dir = download_dir if download_dir else self.tiktok_download_folder
            if not dest_dir:
                self._notify_ui_error(video_id, "Download directory not configured.")
                return

            is_photo = False
            if any(x in play_url.lower() for x in [".jpg", ".jpeg", ".png", ".webp"]):
                is_photo = True

            # If it is a photo, save inside Pictures subfolder
            if is_photo:
                dest_dir = os.path.join(dest_dir, "Pictures")
                os.makedirs(dest_dir, exist_ok=True)

            ext = ".jpg" if is_photo else ".mp4"
            base_name = sanitize_filename(title)
            print(f"Downloading {video_id} - Title: {base_name} (is_photo={is_photo})")
            
            def make_filepath(name, n=0):
                suffix = f"_{n}" if n > 0 else ""
                return os.path.join(dest_dir, f"{name}{suffix}{ext}")

            counter = 0
            filepath = make_filepath(base_name)
            while os.path.exists(filepath):
                counter += 1
                filepath = make_filepath(base_name, counter)
            filename = os.path.basename(filepath)

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            
            # Make request with retries
            max_retries = 3
            response = None
            for attempt in range(max_retries):
                try:
                    response = requests.get(play_url, headers=headers, stream=True, timeout=20)
                    if response.status_code == 200:
                        break
                    else:
                        import time
                        time.sleep(1.5)
                except Exception as req_err:
                    if attempt == max_retries - 1:
                        raise req_err
                    import time
                    time.sleep(1.5)
            
            if not response or response.status_code != 200:
                code = response.status_code if response else 'Unknown'
                self._notify_ui_error(video_id, f"Server returned error code {code}")
                return

            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            last_percent = -1
            
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.total_downloaded_bytes += len(chunk)
                        if total_size > 0:
                            percent = int((downloaded / total_size) * 100)
                            if last_percent == -1 or percent - last_percent >= 2 or percent == 100:
                                self._notify_ui_progress(video_id, percent)
                                last_percent = percent
            
            # Completed
            self._update_download_metadata(dest_dir, self.tiktok_download_folder, video_id, filename)
            if is_photo:
                self._write_caption_file_if_long(filepath, title)
            self._notify_ui_success(video_id, filename, filepath, is_idm=False)
            self.report_download("TikTok", os.path.basename(dest_dir) if dest_dir else "TikTok", title, "Success")
            
        except Exception as e:
            self._notify_ui_error(video_id, str(e))
            self.report_download("TikTok", os.path.basename(dest_dir) if 'dest_dir' in locals() and dest_dir else "TikTok", title, "Failed", str(e))
        finally:
            self.active_download_count -= 1

    def _notify_ui_progress(self, video_id, percent):
        """Calls JavaScript function to update UI progress bar."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onDownloadProgress === 'function') {{ window.onDownloadProgress({json.dumps(video_id)}, {percent}); }}"
            _app_window.evaluate_js(js_code)

    def _notify_ui_success(self, video_id, filename, filepath, is_idm=False):
        """Calls JavaScript function when download completes successfully."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onDownloadSuccess === 'function') {{ window.onDownloadSuccess({json.dumps(video_id)}, {json.dumps(filename)}, {json.dumps(filepath)}, {str(is_idm).lower()}); }}"
            _app_window.evaluate_js(js_code)

    def _notify_ui_error(self, video_id, error_msg):
        """Calls JavaScript function on download error."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onDownloadError === 'function') {{ window.onDownloadError({json.dumps(video_id)}, {json.dumps(error_msg)}); }}"
            _app_window.evaluate_js(js_code)

    def fetch_youtube_info(self, url):
        """Fetches video list for channel/playlist or details of a single video."""
        if not self.youtube_download_folder:
            return {"success": False, "error": "Please select YouTube output folder first."}

        url = url.strip()
        print(f"Fetching YouTube info for: {url}")
        try:
            import yt_dlp
            # Set flat extraction options so it is extremely fast
            ydl_opts = {
                'extract_flat': True,
                'playlistend': 1000, # Raise limit to support larger channels/playlists
                'skip_download': True,
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return {"success": False, "error": "Could not extract info from URL."}
                
                # Check if it is a channel, playlist, or multiple videos
                if 'entries' in info:
                    entries = info.get('entries', [])
                    folder_name = sanitize_filename(info.get('title') or 'YouTube Playlist')
                    target_dir = os.path.join(self.youtube_download_folder, folder_name)
                    if not os.path.exists(target_dir):
                        try:
                            os.makedirs(target_dir)
                        except Exception:
                            pass
                            
                    metadata = self._get_folder_metadata(target_dir)
                    downloaded_ids = metadata.get("downloaded_ids", {})
                    max_seq = metadata.get("max_seq", 0)

                    def gather_videos(entries_list):
                        result = []
                        for e in entries_list:
                            if not e:
                                continue
                            if e.get('_type') == 'playlist' and 'entries' in e:
                                result.extend(gather_videos(e.get('entries', [])))
                            elif e.get('_type') == 'playlist':
                                pass
                            else:
                                video_id = e.get("id") or e.get("url")
                                title = e.get("title") or "YouTube Video"
                                if video_id and not (video_id.startswith('http') and ('/channel/' in video_id or '/user/' in video_id or '/c/' in video_id or '/@' in video_id)):
                                    if not self.skip_duplicates or video_id not in downloaded_ids:
                                        result.append(e)
                        return result
                        
                    new_entries = gather_videos(entries)
                    # Reverse so oldest gets next sequence number
                    new_entries.reverse()
                    
                    videos = []
                    for i, e in enumerate(new_entries):
                        video_id = e.get("id") or e.get("url")
                        title = e.get("title") or "YouTube Video"
                        videos.append({
                            "video_id": video_id,
                            "title": title[:100],
                            "play": e.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                            "sequenceNumber": max_seq + i + 1,
                            "download_dir": target_dir
                        })

                    skipped_count = len([e for e in entries if (e.get('id') or e.get('url')) in downloaded_ids]) if (entries and self.skip_duplicates) else 0

                    return {"success": True, "data": {"videos": videos, "skippedCount": skipped_count}}
                else:
                    # Single video
                    video_id = info.get("id")
                    title = info.get("title") or "YouTube Video"
                    if video_id:
                        return {"success": True, "data": {"videos": [{
                            "video_id": video_id, 
                            "title": title[:100], 
                            "play": url,
                            "download_dir": self.youtube_download_folder
                        }]}}
                    else:
                        return {"success": False, "error": "Invalid video URL."}
        except Exception as e:
            print(f"Error fetching YouTube info: {e}")
            return {"success": False, "error": str(e)}

    def download_youtube_video(self, video_id, title, quality, download_dir=None):
        """Initiates downloading a YouTube video in a background thread."""
        thread = threading.Thread(target=self._perform_youtube_download, args=(video_id, title, quality, download_dir))
        thread.daemon = True
        thread.start()
        return {"success": True, "msg": "YouTube download started"}

    def _perform_youtube_download(self, video_id, title, quality, download_dir=None):
        """Downloads a YouTube video in specified quality, routing to IDM for 720p if installed, or using FFmpeg."""
        self.active_download_count += 1
        try:
            import yt_dlp
            dest_dir = download_dir if download_dir else self.youtube_download_folder
            if not dest_dir:
                self._notify_yt_error(video_id, "Download directory not configured.")
                return

            base_name = sanitize_filename(title)
            video_url = f"https://www.youtube.com/watch?v={video_id}" if not video_id.startswith("http") else video_id

            print(f"Downloading YouTube video {video_id} using built-in downloader in {quality}...")
            
            if quality == '1440p':
                format_str = 'bestvideo[height<=1440]+bestaudio[ext=m4a]/bestaudio/best'
            elif quality == '2160p':
                format_str = 'bestvideo[height<=2160]+bestaudio[ext=m4a]/bestaudio/best'
            elif quality == '720p':
                format_str = 'bestvideo[height<=720]+bestaudio[ext=m4a]/bestaudio/best'
            else:  # '1080p'
                format_str = 'bestvideo[height<=1080]+bestaudio[ext=m4a]/bestaudio/best'
                
            out_template = os.path.join(dest_dir, f"{base_name}.%(ext)s")
            
            last_downloaded = [0]
            def ytdlp_hook(d):
                if d['status'] == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    downloaded = d.get('downloaded_bytes', 0)
                    delta = downloaded - last_downloaded[0]
                    if delta > 0:
                        self.total_downloaded_bytes += delta
                    last_downloaded[0] = downloaded
                    if total > 0:
                        percent = int((downloaded / total) * 100)
                        self._notify_yt_progress(video_id, percent)
                elif d['status'] == 'finished':
                    self._notify_yt_progress(video_id, 99)
            
            ffmpeg_path = get_ffmpeg_path()
            
            ydl_opts = {
                'format': format_str,
                'merge_output_format': 'mp4',
                'ffmpeg_location': ffmpeg_path,
                'outtmpl': out_template,
                'progress_hooks': [ytdlp_hook],
                'quiet': True,
                'no_warnings': True
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(video_url, download=True)
                filepath = ydl.prepare_filename(res)
                if not os.path.exists(filepath):
                    name_part, _ = os.path.splitext(filepath)
                    if os.path.exists(name_part + ".mp4"):
                        filepath = name_part + ".mp4"
                
                filename = os.path.basename(filepath)
                
            self._update_download_metadata(dest_dir, self.youtube_download_folder, video_id, filename)
            self._notify_yt_success(video_id, filename, filepath, is_idm=False)
            self.report_download("YouTube", os.path.basename(dest_dir) if dest_dir else "YouTube", title, "Success")
            
        except Exception as e:
            print(f"yt-dlp failed for YouTube video {video_id}: {e}. Trying Cobalt fallback...")
            try:
                cobalt_path = self._cobalt_download_youtube(video_url, filepath, video_id)
                if cobalt_path:
                    filename = os.path.basename(cobalt_path)
                    self._update_download_metadata(dest_dir, self.youtube_download_folder, video_id, filename)
                    self._notify_yt_success(video_id, filename, cobalt_path, is_idm=False)
                    self.report_download("YouTube", os.path.basename(dest_dir) if dest_dir else "YouTube", title, "Success (Cobalt Fallback)")
                    return
            except Exception as cobalt_err:
                print(f"Cobalt fallback failed for YouTube: {cobalt_err}")
                
            print(f"Error downloading YouTube video {video_id}: {e}")
            self._notify_yt_error(video_id, str(e))
            self.report_download("YouTube", os.path.basename(dest_dir) if 'dest_dir' in locals() and dest_dir else "YouTube", title, "Failed", str(e))
        finally:
            self.active_download_count -= 1

    def _notify_yt_progress(self, video_id, percent):
        """Calls JavaScript function to update YouTube UI progress log."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onYtDownloadProgress === 'function') {{ window.onYtDownloadProgress({json.dumps(video_id)}, {percent}); }}"
            _app_window.evaluate_js(js_code)

    def _notify_yt_success(self, video_id, filename, filepath, is_idm=False):
        """Calls JavaScript function when YouTube download completes successfully."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onYtDownloadSuccess === 'function') {{ window.onYtDownloadSuccess({json.dumps(video_id)}, {json.dumps(filename)}, {json.dumps(filepath)}, {str(is_idm).lower()}); }}"
            _app_window.evaluate_js(js_code)

    def _notify_yt_error(self, video_id, error_msg):
        """Calls JavaScript function on YouTube download error."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onYtDownloadError === 'function') {{ window.onYtDownloadError({json.dumps(video_id)}, {json.dumps(error_msg)}); }}"
            _app_window.evaluate_js(js_code)

    # ═══════════════════════════════════════════════════════════
    #   MULTI-URL BATCH DOWNLOADER
    # ═══════════════════════════════════════════════════════════

    _batch_stop_flag = False

    def _detect_platform_from_url(self, url):
        """Detects the platform from a URL string."""
        u = url.lower()
        if 'tiktok.com' in u or u.startswith('@'): return 'tiktok'
        if 'youtube.com' in u or 'youtu.be' in u:  return 'youtube'
        if 'instagram.com' in u:                    return 'instagram'
        if 'facebook.com' in u or 'fb.watch' in u or 'fb.com' in u: return 'facebook'
        if 'pinterest.com' in u or 'pin.it' in u:  return 'pinterest'
        if 'douyin.com' in u or 'iesdouyin.com' in u: return 'douyin'
        if 'kuaishou.com' in u or 'gifshow.com' in u:  return 'kuaishou'
        return 'generic'

    def _sanitize_folder_name(self, name):
        """Sanitizes a string for use as a Windows folder name."""
        if not name:
            return 'Unknown'
        name = str(name).encode('ascii', 'ignore').decode('ascii')
        name = re.sub(r'[\\/*?:"<>|]', '', name).strip()
        name = re.sub(r'\s+', ' ', name)
        return name[:60] or 'Unknown'

    def _notify_batch_js(self, func, *args):
        """Helper to call a JavaScript function with JSON-safe arguments."""
        global _app_window
        try:
            if _app_window:
                arg_strs = ', '.join(json.dumps(a) for a in args)
                _app_window.evaluate_js(f"if (typeof window.{func} === 'function') {{ window.{func}({arg_strs}); }}")
        except Exception as e:
            print(f"JS notify error [{func}]: {e}")

    def _is_profile_url(self, url, platform):
        """Returns True if the URL is a profile/channel/playlist (not a single video)."""
        u = url.lower()
        if platform == 'tiktok':
            # Profile: tiktok.com/@user (no /video/ in path)
            return '/@' in u and '/video/' not in u
        elif platform == 'youtube':
            # Channel or playlist
            return any(x in u for x in ['/@', '/channel/', '/user/', '/c/', 'playlist?', '/videos'])
        elif platform == 'douyin':
            return '/user/' in u
        elif platform == 'kuaishou':
            return '/profile/' in u
        elif platform == 'instagram':
            # Profile: instagram.com/username/ (no /p/ or /reel/)
            return '/p/' not in u and '/reel/' not in u and '/stories/' not in u
        return False

    def fetch_batch_metadata(self, urls_json, download_dir, settings_json):
        """
        UNIFIED BATCH: Parallel fetch + streaming download pipeline.
        - Fetches up to 3 URLs simultaneously for speed
        - Downloads start AS SOON AS first URL is fetched (no waiting)
        - Same username → same folder (deduplication)
        - Already downloaded files are skipped (no_overwrites)
        Notifies JS via:
          onBatchFetchStart/Done/Error  → fetch phase per URL
          onBatchUrlsAvailable          → new video items to add to queue
          onBatchUrlStart/Progress/Complete → per-video download
          onBatchComplete               → all done
        """
        self._batch_stop_flag = False
        try:
            urls = json.loads(urls_json)
        except Exception:
            return {"success": False, "error": "Invalid URLs JSON"}

        try:
            settings = json.loads(settings_json)
        except Exception:
            settings = {}

        date_filters = settings.get('date_filters', {})
        quality_map = settings.get('quality', {})

        if not urls:
            return {"success": False, "error": "No URLs provided"}
        if not download_dir:
            return {"success": False, "error": "No download directory"}

        os.makedirs(download_dir, exist_ok=True)

        platform_folder_map = {
            'youtube': 'YouTube', 'tiktok': 'TikTok',
            'instagram': 'Instagram', 'facebook': 'Facebook',
            'pinterest': 'Pinterest', 'douyin': 'Douyin',
            'kuaishou': 'Kuaishou', 'generic': 'Other',
        }

        import queue as _queue
        import concurrent.futures

        active_queued_videos = []
        scheduler_lock = threading.Lock()
        total_urls = len(urls)
        fetch_done_count = [0]
        fetch_done_lock = threading.Lock()
        video_idx_counter = [0]
        video_idx_lock = threading.Lock()


        def _dest_dir(platform, uploader, url):
            """Build destination directory — respect platform-specific folder if configured, fallback to download_dir."""
            base_pdir = None
            if platform == 'youtube' and getattr(self, 'youtube_download_folder', None):
                base_pdir = self.youtube_download_folder
            elif platform == 'tiktok' and getattr(self, 'tiktok_download_folder', None):
                base_pdir = self.tiktok_download_folder
            elif platform == 'instagram' and getattr(self, 'instagram_download_folder', None):
                base_pdir = self.instagram_download_folder
            elif platform == 'facebook' and getattr(self, 'facebook_download_folder', None):
                base_pdir = self.facebook_download_folder
            elif platform == 'pinterest' and getattr(self, 'pinterest_download_folder', None):
                base_pdir = self.pinterest_download_folder
            elif platform == 'douyin' and getattr(self, 'douyin_download_folder', None):
                base_pdir = self.douyin_download_folder
            elif platform == 'kuaishou' and getattr(self, 'kuaishou_download_folder', None):
                base_pdir = self.kuaishou_download_folder
                
            if not base_pdir:
                base_pdir = os.path.join(download_dir, platform_folder_map.get(platform, 'Other'))
                
            if uploader:
                return os.path.join(base_pdir, self._sanitize_folder_name(uploader))
            m = re.search(r'/@([^/?&]+)', url)
            return os.path.join(base_pdir, self._sanitize_folder_name(m.group(1))) if m else base_pdir

        def _fetch_one(url_idx, raw_url):
            import yt_dlp as _ytdlp
            url = raw_url.strip()
            if not url:
                return []

            # Auto-normalize YouTube channel URLs to target the /videos tab directly
            if 'youtube.com' in url or 'youtu.be' in url:
                if '/@' in url or '/channel/' in url or '/c/' in url or '/user/' in url:
                    if not any(tab in url.lower() for tab in ['/videos', '/shorts', '/streams', '/playlists', '/featured']):
                        parts = url.split('?')
                        base = parts[0].rstrip('/')
                        url = f"{base}/videos"
                        if len(parts) > 1:
                            url = f"{url}?{parts[1]}"

            platform = self._detect_platform_from_url(url)
            is_profile = self._is_profile_url(url, platform)

            self._notify_batch_js('onBatchFetchStart', url_idx, url, platform, is_profile)

            uploader_discovered = ''
            m = re.search(r'/@([^/?&]+)', url)
            if m:
                uploader_discovered = m.group(1)

            def handle_streamed_entry(e, uploader_name):
                vid_url = e.get('webpage_url') or e.get('url') or ''
                if not vid_url:
                    return
                if not vid_url.startswith('http'):
                    if platform == 'youtube':
                        vid_url = f"https://www.youtube.com/watch?v={vid_url}"
                    elif platform == 'tiktok':
                        vid_url = f"https://www.tiktok.com/video/{vid_url}"
                    else:
                        return

                title = e.get('title') or e.get('id') or 'Unknown'
                dest_dir = _dest_dir(platform, uploader_name or uploader_discovered, url)

                # Extract thumbnail url with fallback to thumbnails list
                thumb = e.get('thumbnail')
                if not thumb and e.get('thumbnails'):
                    t_list = e.get('thumbnails')
                    if isinstance(t_list, list) and len(t_list) > 0:
                        thumb = next((t.get('url') for t in t_list if t.get('id') == 'cover'), t_list[0].get('url')) or ''
                if not thumb:
                    thumb = ''

                with video_idx_lock:
                    gidx = video_idx_counter[0]
                    video_idx_counter[0] += 1

                v_item = {
                    'idx': gidx,
                    'url': vid_url,
                    'title': title[:100],
                    'platform': platform,
                    'uploader': uploader_name or uploader_discovered,
                    'dest_dir': dest_dir,
                    'status': 'queued',
                    'id': e.get('id'),
                    'thumbnail': thumb
                }
                with scheduler_lock:
                    active_queued_videos.append(v_item)

                # Streaming update to UI
                self._notify_batch_js(
                    'onBatchUrlsAvailable',
                    json.dumps([{'idx': gidx, **v_item}]),
                    video_idx_counter[0]
                )


            outer_self = self

            # We bypass the Cloudflare-blocked tikwm API and let it fall back to standard StreamingYDL flat extraction.
            pass


            collected_entries = []

            # Lazy playlist resolver class
            class StreamingYDL(_ytdlp.YoutubeDL):
                def process_ie_result(self, ie_result, download=True, extra_info=None):
                    if ie_result and ie_result.get('_type') == 'playlist':
                        entries = ie_result.get('entries')
                        uploader = (ie_result.get('uploader') or ie_result.get('channel') or
                                    ie_result.get('uploader_id') or ie_result.get('title') or '')
                        
                        nonlocal uploader_discovered
                        if uploader:
                            uploader_discovered = uploader

                        if entries:
                            def generator_wrapper(ent_list):
                                for entry in ent_list:
                                    if outer_self._batch_stop_flag:
                                        break
                                    if entry:
                                        # Fallback: recursively expand nested playlists (e.g. channel tabs)
                                        if entry.get('_type') == 'playlist' or (entry.get('entries') and not entry.get('url')):
                                            tab_url = entry.get('url') or entry.get('webpage_url')
                                            if tab_url:
                                                try:
                                                    with StreamingYDL({'extract_flat': True, 'quiet': True}) as sub_ydl:
                                                        sub_info = sub_ydl.extract_info(tab_url, download=False)
                                                        if sub_info and 'entries' in sub_info:
                                                            for sub_entry in sub_info['entries']:
                                                                if outer_self._batch_stop_flag:
                                                                    break
                                                                if sub_entry:
                                                                    collected_entries.append(sub_entry)
                                                except Exception as sub_err:
                                                    print(f"[Batch] Nested playlist extract error: {sub_err}")
                                        else:
                                            collected_entries.append(entry)
                                    yield entry
                            
                            if isinstance(entries, list):
                                ie_result['entries'] = list(generator_wrapper(entries))
                            else:
                                ie_result['entries'] = generator_wrapper(entries)
                                
                    return super().process_ie_result(ie_result, download, extra_info)




            try:
                if is_profile:
                    flat_opts = {
                        'extract_flat': True,
                        'quiet': True,
                        'no_warnings': True,
                        'ignoreerrors': True,
                    }
                    if platform == 'instagram' and self.instagram_cookies_file and os.path.exists(self.instagram_cookies_file):
                        flat_opts['cookiefile'] = self.instagram_cookies_file
                    elif platform == 'facebook' and self.facebook_cookies_file and os.path.exists(self.facebook_cookies_file):
                        flat_opts['cookiefile'] = self.facebook_cookies_file
                    elif platform == 'youtube' and hasattr(self, 'youtube_cookies_file') and self.youtube_cookies_file and os.path.exists(self.youtube_cookies_file):
                        flat_opts['cookiefile'] = self.youtube_cookies_file

                    try:
                        with StreamingYDL(flat_opts) as ydl:
                            info = ydl.extract_info(url, download=False)
                            # Evaluate the generator to consume all entries
                            if info and 'entries' in info:
                                if hasattr(info['entries'], '__iter__') or hasattr(info['entries'], '__next__'):
                                    for _ in info['entries']:
                                        if outer_self._batch_stop_flag:
                                            break
                    except Exception as fe:
                        print(f"[Batch] Flat extract error: {fe}")

                    # Step 1: Define Helper for Timestamp extraction
                    def get_entry_timestamp(e):
                        ts = e.get('timestamp')
                        if ts is not None:
                            try:
                                return float(ts)
                            except:
                                pass
                        ud = e.get('upload_date')
                        if ud:
                            try:
                                import datetime
                                dt = datetime.datetime.strptime(ud, '%Y%m%d')
                                return dt.timestamp()
                            except:
                                pass
                        return None

                    # Step 2: Apply Time Range Filter
                    import time as _time
                    now = _time.time()
                    
                    url_filter = date_filters.get(url, 'all')
                    custom_ts = None
                    if url_filter not in ('all', '7days', 'month', 'year'):
                        try:
                            import datetime
                            dt = datetime.datetime.strptime(url_filter, '%Y-%m-%d')
                            custom_ts = dt.timestamp()
                        except Exception as cte:
                            print(f"[Batch] Error parsing custom date filter: {cte}")

                    filtered_entries = []
                    for e in collected_entries:
                        e_ts = get_entry_timestamp(e)
                        if e_ts:
                            if custom_ts is not None:
                                if e_ts < custom_ts:
                                    continue
                            elif url_filter == '7days' and (now - e_ts) > 7 * 86400:
                                continue
                            elif url_filter == 'month' and (now - e_ts) > 30 * 86400:
                                continue
                            elif url_filter == 'year' and (now - e_ts) > 365 * 86400:
                                continue
                        filtered_entries.append(e)

                    # Step 3: Sort Chronologically (oldest upload first)
                    filtered_entries.sort(key=lambda x: get_entry_timestamp(x) or 0)

                    # Step 4: Stream entries with sequence prefix (1 - , 2 - , ...)
                    for idx_in_playlist, e in enumerate(filtered_entries):
                        if outer_self._batch_stop_flag:
                            break
                        seq_num = idx_in_playlist + 1
                        orig_title = e.get('title') or e.get('id') or 'Video'
                        e['title'] = f"{seq_num} - {orig_title}"
                        handle_streamed_entry(e, uploader_discovered)

                    self._notify_batch_js('onBatchFetchDone', url_idx, 0, uploader_discovered)

                else:
                    # Single video
                    meta_opts = {'skip_download': True, 'quiet': True,
                                 'no_warnings': True, 'noplaylist': True}
                    if platform == 'instagram' and self.instagram_cookies_file and os.path.exists(self.instagram_cookies_file):
                        meta_opts['cookiefile'] = self.instagram_cookies_file
                    elif platform == 'facebook' and self.facebook_cookies_file and os.path.exists(self.facebook_cookies_file):
                        meta_opts['cookiefile'] = self.facebook_cookies_file
                    elif platform == 'youtube' and hasattr(self, 'youtube_cookies_file') and self.youtube_cookies_file and os.path.exists(self.youtube_cookies_file):
                        meta_opts['cookiefile'] = self.youtube_cookies_file

                    title = url
                    uploader = ''
                    thumbnail = ''
                    try:
                        with _ytdlp.YoutubeDL(meta_opts) as ydl:
                            info = ydl.extract_info(url, download=False)
                            if info:
                                title = info.get('title') or url
                                uploader = (info.get('uploader') or info.get('channel') or
                                            info.get('uploader_id') or '')
                                thumbnail = info.get('thumbnail') or ''
                    except Exception as se:
                        print(f"[Batch] Single meta error: {se}")

                    dest_dir = _dest_dir(platform, uploader, url)
                    with video_idx_lock:
                        gidx = video_idx_counter[0]
                        video_idx_counter[0] += 1

                    v_item = {
                        'idx': gidx,
                        'url': url,
                        'title': title[:100],
                        'platform': platform,
                        'uploader': uploader,
                        'dest_dir': dest_dir,
                        'status': 'queued',
                        'thumbnail': thumbnail
                    }
                    with scheduler_lock:
                        active_queued_videos.append(v_item)

                    self._notify_batch_js(
                        'onBatchUrlsAvailable',
                        json.dumps([{'idx': gidx, **v_item}]),
                        video_idx_counter[0]
                    )
                    self._notify_batch_js('onBatchFetchDone', url_idx, 1, uploader)


            except Exception as ex:
                print(f"[Batch] Fetch error for {url}: {ex}")
                self._notify_batch_js('onBatchFetchError', url_idx, str(ex))

            return []

        active_count_lock = threading.Lock()
        bytes_lock = threading.Lock()


        def _get_next_video():
            import time
            while True:
                with scheduler_lock:
                    active_platforms = {v['platform'] for v in active_queued_videos if v['status'] == 'downloading'}
                    chosen = None
                    for v in active_queued_videos:
                        if v['status'] == 'queued' and v['platform'] not in active_platforms:
                            chosen = v
                            break
                    if not chosen:
                        for v in active_queued_videos:
                            if v['status'] == 'queued':
                                chosen = v
                                break
                    if chosen:
                        chosen['status'] = 'downloading'
                        return chosen['idx'], chosen

                    with fetch_done_lock:
                        all_fetch_done = fetch_done_count[0] >= total_urls

                    if all_fetch_done:
                        any_unfinished = any(v['status'] in ('queued', 'downloading') for v in active_queued_videos)
                        if not any_unfinished:
                            return None, None

                if self._batch_stop_flag:
                    return None, None
                time.sleep(0.5)

        def _download_worker():
            """Consumes active_queued_videos list using a platform-interleaved scheduler."""
            import yt_dlp as _ytdlp
            import time

            while True:
                # Check if this thread should exit because we scaled down the worker limit
                with active_count_lock:
                    if self.active_download_count > self.max_download_workers:
                        break

                idx, video = _get_next_video()
                if idx is None or video is None:
                    break

                # Sibling thread spawning logic
                with scheduler_lock:
                    has_more_queued = any(v['status'] == 'queued' for v in active_queued_videos)
                with active_count_lock:
                    below_capacity = self.active_download_count < self.max_download_workers
                if has_more_queued and below_capacity:
                    t = threading.Thread(target=_download_worker, daemon=True)
                    t.start()

                if self._batch_stop_flag:
                    self._notify_batch_js('onBatchUrlComplete', idx, 'skipped', '')
                    with scheduler_lock:
                        video['status'] = 'skipped'
                    continue

                url      = video.get('url', '').strip()
                platform = video.get('platform', 'generic')
                dest_dir = video.get('dest_dir', '')

                if not url:
                    self._notify_batch_js('onBatchUrlComplete', idx, 'failed', '')
                    with scheduler_lock:
                        video['status'] = 'failed'
                    continue

                # Check if file with this ID or title already exists on disk
                vid_id = video.get('id')
                v_title = video.get('title', '')
                base_name = sanitize_filename(v_title).lower()
                already_downloaded = False
                
                # Check 1: metadata check
                metadata = self._get_folder_metadata(dest_dir)
                downloaded_ids = metadata.get("downloaded_ids", {})
                if self.skip_duplicates and vid_id and vid_id in downloaded_ids:
                    already_downloaded = True
                
                # Check 2: disk scan fallback
                if not already_downloaded and os.path.exists(dest_dir):
                    existing = os.listdir(dest_dir)
                    # Check new ID pattern
                    if vid_id:
                        for fname in existing:
                            if str(vid_id) in fname and fname.lower().endswith(('.mp4', '.mkv', '.webm', '.jpg', '.jpeg', '.png', '.mp3')):
                                already_downloaded = True
                                break
                    # Check old plain title pattern
                    if not already_downloaded:
                        for fname in existing:
                            fname_lower = fname.lower()
                            if fname_lower.endswith(('.mp4', '.mkv', '.webm', '.jpg', '.jpeg', '.png', '.mp3')):
                                name_without_ext = os.path.splitext(fname_lower)[0]
                                if name_without_ext == base_name or name_without_ext.startswith(base_name + "_"):
                                    already_downloaded = True
                                    break
                
                if self.skip_duplicates and already_downloaded:
                    # Auto-register in metadata for speed on future runs if not already there
                    if vid_id and vid_id not in downloaded_ids:
                        downloaded_ids[vid_id] = base_name + ".mp4"
                        metadata["downloaded_ids"] = downloaded_ids
                        self._save_folder_metadata(dest_dir, metadata)

                    self._notify_batch_js('onBatchUrlProgress', idx, 100)
                    self._notify_batch_js('onBatchLogProgressComplete', idx, f"  ✓ Skipped (Already Downloaded): [{platform.upper()}] {v_title[:45]}", "success")
                    self._notify_batch_js('onBatchUrlComplete', idx, 'done', dest_dir)
                    with scheduler_lock:
                        video['status'] = 'done'
                    continue

                os.makedirs(dest_dir, exist_ok=True)
                self._notify_batch_js('onBatchUrlStart', idx, platform)

                # Dynamically set quality for this platform from the settings map
                quality = quality_map.get(platform, 'best')
                if quality == 'best':
                    fmt = 'bestvideo[vcodec!^=av01][ext=mp4]+bestaudio[ext=m4a]/bestvideo[vcodec!^=av01]+bestaudio/best'
                elif quality == 'medium':
                    fmt = 'bestvideo[vcodec!^=av01][height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[vcodec!^=av01][height<=720]+bestaudio/best[height<=720]/best'
                elif quality == 'low':
                    fmt = 'bestvideo[vcodec!^=av01][height<=480]+bestaudio/best[height<=480]/worst'
                elif quality == 'audio':
                    fmt = 'bestaudio/best'
                else:
                    fmt = 'bestvideo[vcodec!^=av01]+bestaudio/best'

                def make_hook(cap_idx, title, plat):
                    _last = [0]
                    _last_pct = [-10]  # log every 20%
                    plat_upper = plat.upper()
                    def hook(d):
                        if d['status'] == 'downloading':
                            cur   = d.get('downloaded_bytes', 0)
                            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                            delta = cur - _last[0]
                            if delta > 0:
                                with bytes_lock:
                                    self.total_downloaded_bytes += delta
                            _last[0] = cur
                            if total > 0:
                                pct = int(cur/total*100)
                                self._notify_batch_js('onBatchUrlProgress', cap_idx, pct)
                                if pct >= _last_pct[0] + 20:
                                    _last_pct[0] = pct
                                    # IN-PLACE progress update with green percentage
                                    self._notify_batch_js('onBatchLogProgress', cap_idx, f"  ↳ [{plat_upper}] {title[:35]} — <span style='color: #2ecc71; font-weight: bold;'>{pct}%</span>", "default")
                        elif d['status'] == 'finished':
                            self._notify_batch_js('onBatchUrlProgress', cap_idx, 100)
                            # Remove progress line and append final clean success log
                            self._notify_batch_js('onBatchLogProgressComplete', cap_idx, f"  ✓ Finished: [{plat_upper}] {title[:45]}", "success")
                    return hook

                # Calculate unique file path
                vid_id = video.get('id')
                v_title = video.get('title', '')
                ext = ".mp3" if quality == 'audio' else ".mp4"
                
                # Check if it has a title or is default
                clean_title = sanitize_filename(v_title).strip()
                is_default = (not clean_title or 
                              clean_title.lower() in ("tiktok video", "douyin video", "kuaishou video", "video", "unknown", ""))
                
                if is_default and vid_id:
                    base_name = str(vid_id)
                elif vid_id:
                    base_name = f"{clean_title}_{vid_id}"
                else:
                    base_name = clean_title

                def make_filepath(name, n=0):
                    suffix = f"_{n}" if n > 0 else ""
                    return os.path.join(dest_dir, f"{name}{suffix}{ext}")

                counter = 0
                filepath = make_filepath(base_name)
                # If file already exists - skip it, no need to download again
                if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
                    print(f"[Batch] Already exists, skipping: {filepath}")
                    self._notify_batch_js('onBatchUrlComplete', idx, 'skipped', dest_dir)
                    with active_count_lock:
                        self.active_download_count = max(0, self.active_download_count - 1)
                    continue
                
                filename = os.path.basename(filepath)

                # Initialize proxy rotation
                proxy = None
                if getattr(self, 'proxy_list', None):
                    p_idx = idx % len(self.proxy_list)
                    proxy = self.proxy_list[p_idx]

                ffmpeg_path = get_ffmpeg_path()
                ydl_opts = {
                    'format': fmt,
                    'merge_output_format': 'mp4',
                    'outtmpl': filepath.replace(ext, '.%(ext)s'),
                    'ffmpeg_location': ffmpeg_path,
                    'progress_hooks': [make_hook(idx, filename, platform)],
                    'quiet': True,
                    'no_warnings': True,
                    'noplaylist': True,
                    'windowsfilenames': True,
                    'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'web']}},
                    'buffersize': 1024 * 256,
                    'http_chunk_size': 10485760,
                    'concurrent_fragment_downloads': 5,
                    'nocheckcertificate': True,
                }

                if proxy:
                    ydl_opts['proxy'] = proxy

                if quality == 'audio':
                    ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio',
                                                    'preferredcodec': 'mp3',
                                                    'preferredquality': '192'}]
                    ydl_opts['merge_output_format'] = 'mp3'

                if platform == 'instagram' and self.instagram_cookies_file and os.path.exists(self.instagram_cookies_file):
                    ydl_opts['cookiefile'] = self.instagram_cookies_file
                elif platform == 'facebook' and self.facebook_cookies_file and os.path.exists(self.facebook_cookies_file):
                    ydl_opts['cookiefile'] = self.facebook_cookies_file
                elif platform == 'youtube' and hasattr(self, 'youtube_cookies_file') and self.youtube_cookies_file and os.path.exists(self.youtube_cookies_file):
                    ydl_opts['cookiefile'] = self.youtube_cookies_file

                with active_count_lock:
                    self.active_download_count += 1
                try:
                    try:
                        with _ytdlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([url])
                    except Exception as dl_err:
                        if proxy:
                            print(f"[Proxy Failover] Proxy {proxy} failed: {dl_err}. Retrying with direct connection...")
                            ydl_opts_direct = ydl_opts.copy()
                            ydl_opts_direct.pop('proxy', None)
                            with _ytdlp.YoutubeDL(ydl_opts_direct) as ydl:
                                ydl.download([url])
                        else:
                            raise dl_err
                    
                    # Verify the file actually exists on disk before counting as success
                    # (yt-dlp can exit cleanly even if download silently failed)
                    file_saved = os.path.exists(filepath) and os.path.getsize(filepath) > 1000
                    
                    # Also check for any file with our base_name (yt-dlp may use different ext)
                    if not file_saved:
                        base_no_ext = os.path.splitext(filepath)[0]
                        for possible_ext in ['.mp4', '.mp3', '.webm', '.mkv', '.m4a']:
                            candidate = base_no_ext + possible_ext
                            if os.path.exists(candidate) and os.path.getsize(candidate) > 1000:
                                filepath = candidate
                                filename = os.path.basename(filepath)
                                file_saved = True
                                break
                    
                    if file_saved:
                        status = 'done'
                        if vid_id:
                            self._update_download_metadata(dest_dir, None, vid_id, filename)
                        
                        # Report batch download success to Firebase
                        platform_map = {
                            'tiktok': 'TikTok', 'youtube': 'YouTube', 'instagram': 'Instagram',
                            'facebook': 'Facebook', 'pinterest': 'Pinterest', 'douyin': 'Douyin',
                            'kuaishou': 'Kuaishou'
                        }
                        pname = platform_map.get(platform.lower(), platform.capitalize())
                        target_name = os.path.basename(dest_dir) if dest_dir else pname
                        self.report_download(pname, target_name, video.get('title', 'Video'), "Success (Batch)")
                    else:
                        # yt-dlp ran but no file was actually saved
                        print(f"[Batch] Download ran but no file saved for: {url}")
                        status = 'failed'
                except Exception as dl_err:
                    print(f"[Batch] Download failed [{idx}] {url}: {dl_err}")
                    status = 'failed'
                    
                    # Report batch download failure to Firebase
                    platform_map = {
                        'tiktok': 'TikTok', 'youtube': 'YouTube', 'instagram': 'Instagram',
                        'facebook': 'Facebook', 'pinterest': 'Pinterest', 'douyin': 'Douyin',
                        'kuaishou': 'Kuaishou'
                    }
                    pname = platform_map.get(platform.lower(), platform.capitalize())
                    target_name = os.path.basename(dest_dir) if dest_dir else pname
                    self.report_download(pname, target_name, video.get('title', 'Video'), "Failed (Batch)", str(dl_err))
                finally:
                    with active_count_lock:
                        self.active_download_count = max(0, self.active_download_count - 1)

                self._notify_batch_js('onBatchUrlComplete', idx, status, dest_dir)
                with scheduler_lock:
                    video['status'] = status
                time.sleep(0.1)

        def _run_all():
            # Spawn initial parallel downloaders based on max_download_workers (default 4)
            dl_threads = []
            for _ in range(self.max_download_workers):
                t = threading.Thread(target=_download_worker, daemon=True)
                t.start()
                dl_threads.append(t)

            # Fetch all URLs in parallel — up to 5 at a time
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_map = {executor.submit(_fetch_one, i, url): i for i, url in enumerate(urls)}
                for future in concurrent.futures.as_completed(future_map):
                    if self._batch_stop_flag:
                        break
                    with fetch_done_lock:
                        fetch_done_count[0] += 1

            for t in dl_threads:
                t.join()

            self._notify_batch_js('onBatchComplete')

        threading.Thread(target=_run_all, daemon=True).start()
        return {"success": True, "msg": f"Batch started for {len(urls)} URLs"}






    def start_expanded_batch(self, videos_json, quality):
        """Legacy: now handled inside fetch_batch_metadata. Kept for compatibility."""
        return {"success": True, "msg": "Downloads already started during fetch phase."}

    def start_multi_url_batch(self, urls_json, quality, download_dir):
        """Legacy wrapper."""
        return self.fetch_batch_metadata(urls_json, download_dir, quality)

    def stop_multi_url_batch(self):
        """Stop batch after current item."""
        self._batch_stop_flag = True
        return {"success": True}


        self._batch_stop_flag = False
        try:
            urls = json.loads(urls_json)
        except Exception:
            return {"success": False, "error": "Invalid URLs JSON"}

        if not urls:
            return {"success": False, "error": "No URLs provided"}

        if not download_dir:
            return {"success": False, "error": "No download directory"}

        os.makedirs(download_dir, exist_ok=True)

        platform_folder_map = {
            'youtube': 'YouTube', 'tiktok': 'TikTok',
            'instagram': 'Instagram', 'facebook': 'Facebook',
            'pinterest': 'Pinterest', 'douyin': 'Douyin',
            'kuaishou': 'Kuaishou', 'generic': 'Other',
        }

        def _fetch_all():
            import yt_dlp as _ytdlp

            all_videos = []

            for url_idx, raw_url in enumerate(urls):
                if self._batch_stop_flag:
                    break

                url = raw_url.strip()
                if not url:
                    continue

                platform = self._detect_platform_from_url(url)
                is_profile = self._is_profile_url(url, platform)
                platform_dir = os.path.join(download_dir, platform_folder_map.get(platform, 'Other'))

                self._notify_batch_js('onBatchFetchStart', url_idx, url, platform, is_profile)

                try:
                    if is_profile:
                        flat_opts = {
                            'extract_flat': True,
                            'quiet': True,
                            'no_warnings': True,
                            'ignoreerrors': True,
                        }
                        if platform == 'instagram' and self.instagram_cookies_file and os.path.exists(self.instagram_cookies_file):
                            flat_opts['cookiefile'] = self.instagram_cookies_file
                        elif platform == 'facebook' and self.facebook_cookies_file and os.path.exists(self.facebook_cookies_file):
                            flat_opts['cookiefile'] = self.facebook_cookies_file

                        info = None
                        try:
                            with _ytdlp.YoutubeDL(flat_opts) as ydl:
                                info = ydl.extract_info(url, download=False)
                        except Exception as fe:
                            print(f"[Batch] Flat extract error for {url}: {fe}")

                        uploader = ''
                        entries_added = 0

                        if info:
                            uploader = (
                                info.get('uploader') or info.get('channel') or
                                info.get('uploader_id') or info.get('title') or ''
                            )
                            if not uploader:
                                m = re.search(r'/@([^/?&]+)', url)
                                if m:
                                    uploader = m.group(1)

                            dest_dir = os.path.join(platform_dir, self._sanitize_folder_name(uploader)) if uploader else platform_dir

                            def gather_entries(entries_list):
                                result = []
                                for e in (entries_list or []):
                                    if not e:
                                        continue
                                    if e.get('entries') and not e.get('url'):
                                        result.extend(gather_entries(e.get('entries', [])))
                                    else:
                                        result.append(e)
                                return result

                            leaf_entries = gather_entries(info.get('entries', []))

                            for e in leaf_entries:
                                vid_url = e.get('webpage_url') or e.get('url') or ''
                                if not vid_url:
                                    continue
                                if not vid_url.startswith('http'):
                                    if platform == 'youtube':
                                        vid_url = f"https://www.youtube.com/watch?v={vid_url}"
                                    elif platform == 'tiktok':
                                        vid_url = f"https://www.tiktok.com/video/{vid_url}"
                                    else:
                                        continue

                                title = e.get('title') or e.get('id') or 'Unknown'
                                all_videos.append({
                                    'url': vid_url,
                                    'title': title[:100],
                                    'platform': platform,
                                    'uploader': uploader,
                                    'dest_dir': dest_dir,
                                    'status': 'queued',
                                })
                                entries_added += 1

                            self._notify_batch_js('onBatchFetchDone', url_idx, entries_added, uploader)

                    else:
                        meta_opts = {
                            'skip_download': True,
                            'quiet': True,
                            'no_warnings': True,
                            'noplaylist': True,
                        }
                        if platform == 'instagram' and self.instagram_cookies_file and os.path.exists(self.instagram_cookies_file):
                            meta_opts['cookiefile'] = self.instagram_cookies_file

                        title = url
                        uploader = ''
                        try:
                            with _ytdlp.YoutubeDL(meta_opts) as ydl:
                                info = ydl.extract_info(url, download=False)
                                if info:
                                    title = info.get('title') or url
                                    uploader = (
                                        info.get('uploader') or info.get('channel') or
                                        info.get('uploader_id') or ''
                                    )
                        except Exception as se:
                            print(f"[Batch] Single meta error: {se}")

                        if uploader:
                            dest_dir = os.path.join(platform_dir, self._sanitize_folder_name(uploader))
                        else:
                            m = re.search(r'/@([^/?&]+)', url)
                            dest_dir = os.path.join(platform_dir, self._sanitize_folder_name(m.group(1))) if m else platform_dir

                        all_videos.append({
                            'url': url,
                            'title': title[:100],
                            'platform': platform,
                            'uploader': uploader,
                            'dest_dir': dest_dir,
                            'status': 'queued',
                        })
                        self._notify_batch_js('onBatchFetchDone', url_idx, 1, uploader)

                except Exception as ex:
                    print(f"[Batch] Fetch error for {url}: {ex}")
                    self._notify_batch_js('onBatchFetchError', url_idx, str(ex))

            self._notify_batch_js('onBatchMetadataReady', json.dumps(all_videos))

        threading.Thread(target=_fetch_all, daemon=True).start()
        return {"success": True, "msg": f"Fetching metadata for {len(urls)} URLs..."}

    def start_expanded_batch(self, videos_json, quality):
        """
        PHASE 2: Download each video in the pre-expanded list.
        Sends onBatchUrlStart, onBatchUrlProgress, onBatchUrlComplete per video.
        """
        self._batch_stop_flag = False
        try:
            videos = json.loads(videos_json)
        except Exception:
            return {"success": False, "error": "Invalid videos JSON"}

        if not videos:
            return {"success": False, "error": "No videos to download"}

        def _run_downloads():
            import yt_dlp as _ytdlp
            import time

            for idx, video in enumerate(videos):
                if self._batch_stop_flag:
                    break

                url = video.get('url', '').strip()
                platform = video.get('platform', 'generic')
                dest_dir = video.get('dest_dir', '')

                if not url:
                    self._notify_batch_js('onBatchUrlComplete', idx, 'failed', '')
                    continue

                os.makedirs(dest_dir, exist_ok=True)
                self._notify_batch_js('onBatchUrlStart', idx, platform)

                if quality == 'best':
                    fmt = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
                elif quality == 'medium':
                    fmt = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best'
                elif quality == 'low':
                    fmt = 'bestvideo[height<=480]+bestaudio/best[height<=480]/worst'
                elif quality == 'audio':
                    fmt = 'bestaudio/best'
                else:
                    fmt = 'bestvideo+bestaudio/best'

                def make_hook(cap_idx):
                    _last = [0]
                    def hook(d):
                        if d['status'] == 'downloading':
                            current = d.get('downloaded_bytes', 0)
                            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                            delta = current - _last[0]
                            if delta > 0:
                                self.total_downloaded_bytes += delta
                            _last[0] = current
                            if total > 0:
                                pct = int(current / total * 100)
                                self._notify_batch_js('onBatchUrlProgress', cap_idx, pct)
                        elif d['status'] == 'finished':
                            self._notify_batch_js('onBatchUrlProgress', cap_idx, 100)
                    return hook

                ffmpeg_path = get_ffmpeg_path()
                ydl_opts = {
                    'format': fmt,
                    'merge_output_format': 'mp4',
                    'outtmpl': os.path.join(dest_dir, '%(title)s_%(id)s.%(ext)s'),
                    'ffmpeg_location': ffmpeg_path,
                    'progress_hooks': [make_hook(idx)],
                    'quiet': True,
                    'no_warnings': True,
                    'noplaylist': True,
                    'ignoreerrors': True,
                }

                if quality == 'audio':
                    ydl_opts['postprocessors'] = [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }]
                    ydl_opts['merge_output_format'] = 'mp3'

                if platform == 'instagram' and self.instagram_cookies_file and os.path.exists(self.instagram_cookies_file):
                    ydl_opts['cookiefile'] = self.instagram_cookies_file
                elif platform == 'facebook' and self.facebook_cookies_file and os.path.exists(self.facebook_cookies_file):
                    ydl_opts['cookiefile'] = self.facebook_cookies_file

                self.active_download_count += 1
                try:
                    with _ytdlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                    status = 'done'
                except Exception as dl_err:
                    print(f"[Batch] Download failed [{idx}] {url}: {dl_err}")
                    status = 'failed'
                finally:
                    self.active_download_count = max(0, self.active_download_count - 1)

                self._notify_batch_js('onBatchUrlComplete', idx, status, dest_dir)
                time.sleep(0.2)

            self._notify_batch_js('onBatchComplete')

        threading.Thread(target=_run_downloads, daemon=True).start()
        return {"success": True, "msg": f"Downloading {len(videos)} videos..."}


    def _get_ig_ydl_opts(self, extra_opts=None):
        """Builds yt-dlp option sets with cookie auto-detection for Instagram.
        Order: no-cookies first (fast, works for some public posts), then browser cookies.
        """
        base_opts = {
            'skip_download': True,
            'quiet': True,
            'no_warnings': True,
        }
        if extra_opts:
            base_opts.update(extra_opts)

        # Custom cookies.txt takes absolute priority (only one option set)
        if self.instagram_cookies_file and os.path.exists(self.instagram_cookies_file):
            opts = dict(base_opts)
            opts['cookiefile'] = self.instagram_cookies_file
            return [opts]

        opts_sets = []

        # 1. Try WITHOUT cookies first (fast, works for some public content)
        opts_sets.append(dict(base_opts))

        # 2. Then try each browser's cookies (browser must be CLOSED for this to work)
        for browser in ['chrome', 'edge', 'firefox', 'brave', 'opera']:
            opts = dict(base_opts)
            opts['cookiesfrombrowser'] = (browser,)
            opts_sets.append(opts)

        return opts_sets

    def fetch_instagram_info(self, url):
        """Extracts media metadata for Instagram Profile, Reel, Post, or Carousel.
        Strategy:
          - Single post/reel: Cobalt API (no cookies) → yt-dlp fallback
          - Profile page:     instaloader (most reliable) → yt-dlp fallback
        """
        if not self.instagram_download_folder:
            return {"success": False, "error": "Please select Instagram output folder first."}

        import re as _re
        url = url.strip().rstrip('/')
        print(f"Fetching Instagram info for: {url}")

        try:
            import yt_dlp

            # --- Detect URL type ---
            profile_match = _re.match(
                r'https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)(?:/(?:reels|posts|videos|tagged))?/?$',
                url
            )
            is_profile = bool(profile_match) and \
                         '/p/' not in url and '/reel/' not in url and '/tv/' not in url

            if is_profile and profile_match:
                username = profile_match.group(1)
                print(f"Profile detected: @{username}")
            else:
                username = None
                print(f"Single post/reel: {url}")

            # -------------------------------------------------------
            # SINGLE POST / REEL — Cobalt first (no cookies!)
            # -------------------------------------------------------
            if not is_profile:
                cobalt_results = self._try_cobalt_instagram(url)
                if cobalt_results:
                    print(f"Cobalt succeeded for single post! Found {len(cobalt_results)} item(s).")
                    for item in cobalt_results:
                        item["download_dir"] = self.instagram_download_folder
                    return {"success": True, "data": {"videos": cobalt_results}}
                print("Cobalt failed, trying yt-dlp...")

                # yt-dlp fallback for single post
                all_opts_sets = self._get_ig_ydl_opts({})
                for opts in all_opts_sets:
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(url, download=False)
                        if info:
                            # Check if playlist / carousel entries exist
                            entries = info.get("entries")
                            if entries:
                                results = []
                                for i, entry in enumerate(entries):
                                    video_id = entry.get("id") or f"ig_post_{i}"
                                    title = entry.get("title") or entry.get("description") or f"Instagram Post {i+1}"
                                    direct_url = None
                                    is_photo = False
                                    formats = entry.get("formats", [])
                                    if formats:
                                        vf = [f for f in formats if f.get("vcodec") and f.get("vcodec") != "none" and f.get("url")]
                                        if vf:
                                            vf.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
                                            direct_url = vf[0].get("url")
                                    if not direct_url:
                                        direct_url = entry.get("url") or entry.get("thumbnail") or entry.get("webpage_url")
                                        is_photo = True
                                    if direct_url:
                                        results.append({
                                            "video_id": video_id,
                                            "title": title[:100],
                                            "play": direct_url,
                                            "cover": entry.get("thumbnail") or "",
                                            "duration": entry.get("duration"),
                                            "is_photo": is_photo,
                                            "download_dir": self.instagram_download_folder,
                                            "author": {"unique_id": entry.get("uploader") or "ig", "nickname": "Instagram", "avatar": ""}
                                        })
                                if results:
                                    return {"success": True, "data": {"videos": results}}
                            
                            # Single post
                            video_id = info.get("id") or "ig_video"
                            title = info.get("title") or info.get("description") or "Instagram Post"
                            direct_url = None
                            is_photo = False
                            formats = info.get("formats", [])
                            if formats:
                                vf = [f for f in formats if f.get("vcodec") and f.get("vcodec") != "none" and f.get("url")]
                                if vf:
                                    vf.sort(key=lambda x: x.get("height", 0) or 0, reverse=True)
                                    direct_url = vf[0].get("url")
                            if not direct_url:
                                direct_url = info.get("url") or info.get("webpage_url")
                                is_photo = True
                            if direct_url:
                                return {"success": True, "data": {"videos": [{
                                    "video_id": video_id, "title": title[:100],
                                    "play": direct_url, "cover": info.get("thumbnail") or "",
                                    "duration": info.get("duration"),
                                    "is_photo": is_photo,
                                    "download_dir": self.instagram_download_folder,
                                    "author": {"unique_id": info.get("uploader") or "ig", "nickname": "Instagram", "avatar": ""}
                                }]}}
                    except Exception as e:
                        print(f"yt-dlp single post error: {e}")
                return {"success": False, "error": "Reel download failed. Please check the link or try again."}

            # -------------------------------------------------------
            # PROFILE — Use instaloader (most reliable for profiles)
            # -------------------------------------------------------
            instaloader_result = self._fetch_profile_instaloader(username)
            if instaloader_result and instaloader_result.get("success"):
                return instaloader_result

            instaloader_err = instaloader_result.get("error", "") if instaloader_result else ""
            print(f"instaloader failed: {instaloader_err}, trying yt-dlp...")

            # yt-dlp fallback for profile
            normalized_url = f"https://www.instagram.com/{username}/"
            extra = {'extract_flat': True, 'playlistend': 500, 'ignoreerrors': True}
            all_opts_sets = self._get_ig_ydl_opts(extra)
            errors_log = []
            info = None

            for opts in all_opts_sets:
                label = opts.get('cookiesfrombrowser', ('',))[0] if 'cookiesfrombrowser' in opts else \
                        ('cookies.txt' if 'cookiefile' in opts else 'no-cookies')
                print(f"yt-dlp profile [{label}]: {normalized_url}")
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(normalized_url, download=False)
                    if info:
                        break
                    errors_log.append(f"[{label}] empty")
                except Exception as e:
                    errors_log.append(f"[{label}] {e}")
                    info = None

            if info and "entries" in info:
                entries = info.get("entries") or []
                
                target_dir = os.path.join(self.instagram_download_folder, f"@{username}")
                if not os.path.exists(target_dir):
                    try:
                        os.makedirs(target_dir)
                    except Exception:
                        pass
                
                metadata = self._get_folder_metadata(target_dir)
                downloaded_ids = metadata.get("downloaded_ids", {})
                max_seq = metadata.get("max_seq", 0)

                new_entries = []
                for entry in entries:
                    if not entry:
                        continue
                    entry_id = entry.get("id") or ""
                    if entry_id and entry_id in downloaded_ids:
                        print(f"Sync reached already downloaded video: {entry_id}. Stopping fetch.")
                        break
                    new_entries.append(entry)

                new_entries.reverse()

                videos = []
                for i, entry in enumerate(new_entries):
                    post_url = entry.get("url") or entry.get("webpage_url") or ""
                    entry_id = entry.get("id") or ""
                    if post_url and not post_url.startswith("http"):
                        post_url = f"https://www.instagram.com/p/{post_url}/"
                    elif entry_id and not post_url:
                        post_url = f"https://www.instagram.com/p/{entry_id}/"
                    if not post_url:
                        continue
                    videos.append({
                        "video_id": entry_id or post_url,
                        "title": (entry.get("title") or f"Post {entry_id}")[:100],
                        "play": post_url, "cover": entry.get("thumbnail") or "",
                        "duration": entry.get("duration"), "is_profile_entry": True,
                        "sequenceNumber": max_seq + i + 1,
                        "download_dir": target_dir,
                        "author": {"unique_id": username, "nickname": username, "avatar": ""}
                    })
                if videos:
                    return {"success": True, "data": {"videos": videos}}

            # All methods failed — compose helpful error
            combined = instaloader_err + " | " + " | ".join(errors_log[-2:])
            err_msg = (
                f"Profile fetch fail: {combined[:200]}\n\n"
                "FIX:\n"
                "1. Log in to instagram.com in Chrome browser\n"
                "2. CLOSE Chrome completely (Exit from System Tray as well)\n"
                "3. Restart the app and try again\n\n"
                "OR cookies.txt:\n"
                "   Use 'Get cookies.txt LOCALLY' Chrome extension -> Go to instagram.com -> Export -> Load cookies.txt in app"
            )
            return {"success": False, "error": err_msg}

        except Exception as e:
            print(f"Unexpected error: {e}")
            return {"success": False, "error": str(e)}

    def _fetch_profile_instaloader(self, username):
        """Fetches all posts from an Instagram profile using instaloader.
        Returns {"success": True, "data": {"videos": [...]}} or {"success": False, "error": "..."}
        """
        try:
            import instaloader
            print(f"instaloader: fetching profile @{username}")

            target_dir = os.path.join(self.instagram_download_folder, f"@{username}")
            if not os.path.exists(target_dir):
                try:
                    os.makedirs(target_dir)
                except Exception:
                    pass

            metadata = self._get_folder_metadata(target_dir)
            downloaded_ids = metadata.get("downloaded_ids", {})
            max_seq = metadata.get("max_seq", 0)

            L = instaloader.Instaloader(
                download_videos=False,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                quiet=True,
            )

            # Load session from cookies.txt if available
            loaded_session = False
            if self.instagram_cookies_file and os.path.exists(self.instagram_cookies_file):
                try:
                    L.load_session_from_file(username, self.instagram_cookies_file)
                    print("instaloader: loaded session from cookies.txt")
                    loaded_session = True
                except Exception as e:
                    print(f"instaloader: could not load session file: {e}")
                    # Try importing from cookies.txt Netscape format
                    try:
                        self._instaloader_import_cookiestxt(L, self.instagram_cookies_file)
                        loaded_session = True
                        print("instaloader: imported cookies from Netscape cookies.txt")
                    except Exception as e2:
                        print(f"instaloader: cookie import failed: {e2}")

            if not loaded_session:
                # Try to load session from browser cookies via yt-dlp cookie extraction
                try:
                    session_file = self._extract_browser_cookies_for_instaloader(username)
                    if session_file:
                        L.load_session_from_file(username, session_file)
                        loaded_session = True
                        print("instaloader: loaded session from browser cookies")
                except Exception as e:
                    print(f"instaloader: browser cookie session failed: {e}")

            profile = instaloader.Profile.from_username(L.context, username)
            print(f"instaloader: profile found - {profile.full_name}, {profile.mediacount} posts")

            videos = []
            count = 0
            max_posts = 500
            has_existing = False

            for post in profile.get_posts():
                if count >= max_posts:
                    break
                # Only include video posts or sidecars
                if post.is_video or post.typename in ('GraphVideo', 'GraphSidecar'):
                    post_id = post.shortcode
                    if self.skip_duplicates and post_id in downloaded_ids:
                        has_existing = True
                        print(f"Sync reached already downloaded video: {post_id}. Stopping fetch.")
                        break
                    
                    post_url = f"https://www.instagram.com/p/{post.shortcode}/"
                    title = (post.caption or f"Instagram Post {post_id}")[:100]
                    cover = post.url if not post.is_video else ""
                    videos.append({
                        "video_id": post_id,
                        "title": title,
                        "play": post_url,
                        "cover": cover,
                        "duration": post.video_duration if post.is_video else None,
                        "is_profile_entry": True,
                        "author": {
                            "unique_id": username,
                            "nickname": profile.full_name or username,
                            "avatar": ""
                        }
                    })
                count += 1

            if not videos and not has_existing:
                # Include all posts if no videos found
                for post in profile.get_posts():
                    if len(videos) >= max_posts:
                        break
                    post_id = post.shortcode
                    if self.skip_duplicates and post_id in downloaded_ids:
                        has_existing = True
                        print(f"Sync reached already downloaded post: {post_id}. Stopping fetch.")
                        break
                    post_url = f"https://www.instagram.com/p/{post.shortcode}/"
                    videos.append({
                        "video_id": post_id,
                        "title": (post.caption or f"Post {post_id}")[:100],
                        "play": post_url,
                        "cover": "",
                        "duration": None,
                        "is_profile_entry": True,
                        "author": {"unique_id": username, "nickname": profile.full_name or username, "avatar": ""}
                    })

            # Reverse the list so the oldest video gets the lowest index
            videos.reverse()

            # Assign sequence numbers and download directory
            for i, v in enumerate(videos):
                v["sequenceNumber"] = max_seq + i + 1
                v["download_dir"] = target_dir

            print(f"instaloader: found {len(videos)} new posts for @{username}")
            skipped_count = len(downloaded_ids) if (has_existing and self.skip_duplicates) else 0
            if not videos and not has_existing:
                return {"success": False, "error": f"No video posts found for @{username} or the profile is private."}
            return {"success": True, "data": {"videos": videos, "skippedCount": skipped_count}}

        except Exception as e:
            err = str(e)
            print(f"instaloader error: {err}")
            return {"success": False, "error": err}

    def _instaloader_import_cookiestxt(self, loader, cookies_file):
        """Imports cookies from Netscape-format cookies.txt into instaloader session."""
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(cookies_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        for cookie in jar:
            if 'instagram.com' in cookie.domain:
                loader.context._session.cookies.set(cookie.name, cookie.value, domain=cookie.domain)

    def _extract_browser_cookies_for_instaloader(self, username):
        """Tries to extract Instagram sessionid from browser cookies via yt-dlp."""
        return None  # Placeholder — instaloader session loading handles this



    def _try_cobalt_instagram(self, url):
        """Tries to fetch Instagram reel/post via Cobalt API (no cookies needed).
        Returns a list of media dicts on success, None on failure.
        """
        cobalt_apis = [
            "https://cobaltapi.cjs.nz",
            "https://cobaltapi.kittycat.boo",
            "https://api.cobalt.blackcat.sweeux.org",
            "https://rue-cobalt.xenon.zone"
        ]
        
        for cobalt_api in cobalt_apis:
            try:
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0"
                }
                payload = {"url": url, "vQuality": "max", "isAudioOnly": False}
                resp = requests.post(cobalt_api, json=payload, headers=headers, timeout=12)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                status = data.get("status", "")
                print(f"Cobalt Instagram fetch status={status} via {cobalt_api}")

                if status in ("stream", "redirect", "tunnel", "success", "picker"):
                    if status == "picker":
                        items = data.get("picker", [])
                        results = []
                        for i, item in enumerate(items):
                            item_type = item.get("type", "photo")
                            dl_url = item.get("url")
                            if dl_url:
                                is_photo = (item_type == "photo")
                                base_id = url.split("/")[-1] or "ig_post"
                                if not base_id or base_id == "ig_post":
                                    base_id = url.split("/")[-2] or "ig_post"
                                results.append({
                                    "video_id": f"{base_id}_{i}",
                                    "title": f"Instagram Post {i+1}",
                                    "play": dl_url,
                                    "cover": item.get("thumb", ""),
                                    "duration": None,
                                    "is_photo": is_photo,
                                    "author": {"unique_id": "instagram", "nickname": "Instagram", "avatar": ""}
                                })
                        if results:
                            return results
                    else:
                        dl_url = data.get("url") or data.get("urls")
                        if dl_url:
                            base_id = url.split("/")[-1] or "ig_post"
                            if not base_id or base_id == "ig_post":
                                base_id = url.split("/")[-2] or "ig_post"
                            # detect photo
                            is_photo = False
                            if any(ext in dl_url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                                is_photo = True
                            return [{
                                "video_id": base_id,
                                "title": "Instagram Reel" if not is_photo else "Instagram Photo",
                                "play": dl_url,
                                "cover": "",
                                "duration": None,
                                "is_photo": is_photo,
                                "author": {"unique_id": "instagram", "nickname": "Instagram", "avatar": ""}
                            }]
            except Exception as e:
                print(f"Cobalt Instagram fetch error via {cobalt_api}: {e}")
                continue
        return None




    def download_instagram_video(self, video_id, play_url, title, download_dir=None, is_photo=False):
        """Initiates downloading an Instagram video in a background thread."""
        thread = threading.Thread(target=self._perform_instagram_download, args=(video_id, play_url, title, download_dir, is_photo))
        thread.daemon = True
        thread.start()
        return {"success": True, "msg": "Instagram download started"}

    def _perform_instagram_download(self, video_id, play_url, title, download_dir=None, is_photo=False):
        """Downloads Instagram video. Strategy:
        1. Cobalt API (no cookies needed, fast for public posts/reels)
        2. yt-dlp with cookies (fallback)
        """
        self.active_download_count += 1
        try:
            import yt_dlp
            dest_dir = download_dir if download_dir else self.instagram_download_folder
            if not dest_dir:
                self._notify_ig_error(video_id, "Download directory not configured.")
                return

            if not is_photo:
                if any(x in play_url.lower() for x in [".jpg", ".jpeg", ".png", ".webp"]):
                    is_photo = True

            # If it is a photo, save inside Pictures subfolder
            if is_photo:
                dest_dir = os.path.join(dest_dir, "Pictures")
                os.makedirs(dest_dir, exist_ok=True)

            ext = ".jpg" if is_photo else ".mp4"
            base_name = sanitize_filename(title)

            def make_filepath(name, n=0):
                suffix = f"_{n}" if n > 0 else ""
                return os.path.join(dest_dir, f"{name}{suffix}{ext}")

            counter = 0
            filepath = make_filepath(base_name)
            while os.path.exists(filepath):
                counter += 1
                filepath = make_filepath(base_name, counter)

            print(f"Downloading Instagram [{video_id}]: {title} (is_photo={is_photo})")

            # -----------------------------------------------
            # Strategy 1: Direct requests download if play_url is direct CDN
            # -----------------------------------------------
            is_ig_url = 'instagram.com' in play_url
            if not is_ig_url:
                print(f"Direct download for Instagram [{video_id}] from CDN: {play_url[:80]}")
                self._notify_ig_progress(video_id, 10)
                try:
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }
                    response = requests.get(play_url, headers=headers, stream=True, timeout=30)
                    if response.status_code == 200:
                        content_type = response.headers.get('content-type', '').lower()
                        if filepath.endswith(".mp4") and "image" in content_type:
                            filepath = filepath[:-4] + ".jpg"
                            c = 0
                            while os.path.exists(filepath):
                                c += 1
                                filepath = os.path.join(dest_dir, f"{base_name}_{c}.jpg")

                        total_size = int(response.headers.get('content-length', 0))
                        downloaded = 0
                        with open(filepath, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=256 * 1024):
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    self.total_downloaded_bytes += len(chunk)
                                    if total_size > 0:
                                        percent = 10 + int((downloaded / total_size) * 89)
                                        self._notify_ig_progress(video_id, percent)
                        
                        if os.path.exists(filepath) and os.path.getsize(filepath) > 5000:
                            final_filename = os.path.basename(filepath)
                            self._update_download_metadata(dest_dir, self.instagram_download_folder, video_id, final_filename)
                            if is_photo:
                                self._write_caption_file_if_long(filepath, title)
                            self._notify_ig_success(video_id, final_filename, filepath, is_idm=False)
                            self.report_download("Instagram", os.path.basename(dest_dir) if dest_dir else "Instagram", title, "Success (Direct CDN)")
                            return
                except Exception as direct_err:
                    print(f"Direct CDN download failed for [{video_id}]: {direct_err}. Falling back to Cobalt...")
                    if os.path.exists(filepath):
                        os.remove(filepath)

            # -----------------------------------------------
            # Strategy 2: Cobalt API (no cookies, very fast)
            # -----------------------------------------------
            if is_ig_url:
                cobalt_path = self._cobalt_download_instagram(play_url, filepath, video_id)
                if cobalt_path:
                    final_filename = os.path.basename(cobalt_path)
                    self._update_download_metadata(dest_dir, self.instagram_download_folder, video_id, final_filename)
                    if is_photo:
                        self._write_caption_file_if_long(cobalt_path, title)
                    self._notify_ig_success(video_id, final_filename, cobalt_path, is_idm=False)
                    self.report_download("Instagram", os.path.basename(dest_dir) if dest_dir else "Instagram", title, "Success")
                    return
                print(f"Cobalt failed for [{video_id}], trying yt-dlp...")

            # -----------------------------------------------
            # Strategy 2: yt-dlp with cookies
            # -----------------------------------------------
            last_downloaded = [0]
            def ytdlp_hook(d):
                if d['status'] == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    dl_bytes = d.get('downloaded_bytes', 0)
                    delta = dl_bytes - last_downloaded[0]
                    if delta > 0:
                        self.total_downloaded_bytes += delta
                    last_downloaded[0] = dl_bytes
                    if total > 0:
                        percent = int((dl_bytes / total) * 100)
                        self._notify_ig_progress(video_id, percent)
                elif d['status'] == 'finished':
                    self._notify_ig_progress(video_id, 99)

            ydl_base = {
                'outtmpl': filepath.replace('.mp4', '.%(ext)s'),
                'merge_output_format': 'mp4',
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
                'progress_hooks': [ytdlp_hook],
                'quiet': True,
                'no_warnings': True,
            }
            all_opts_sets = self._get_ig_ydl_opts(ydl_base)
            for opts in all_opts_sets:
                opts.pop('skip_download', None)

            downloaded_ok = False
            last_err = ""
            for opts in all_opts_sets:
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([play_url])
                    downloaded_ok = True
                    break
                except Exception as e:
                    last_err = str(e)
                    print(f"yt-dlp attempt failed [{video_id}]: {e}")

            if not downloaded_ok:
                self._notify_ig_error(video_id, f"Download failed: {last_err}")
                self.report_download("Instagram", os.path.basename(dest_dir) if dest_dir else "Instagram", title, "Failed", last_err)
                return

            saved_path = filepath
            if not os.path.exists(saved_path):
                base_no_ext = filepath.replace('.mp4', '')
                for ext in ['.mp4', '.mkv', '.webm', '.mov']:
                    if os.path.exists(base_no_ext + ext):
                        saved_path = base_no_ext + ext
                        break

            final_filename = os.path.basename(saved_path)
            self._update_download_metadata(dest_dir, self.instagram_download_folder, video_id, final_filename)
            self._notify_ig_success(video_id, final_filename, saved_path, is_idm=False)
            self.report_download("Instagram", os.path.basename(dest_dir) if dest_dir else "Instagram", title, "Success")

        except Exception as e:
            print(f"Error downloading Instagram [{video_id}]: {e}")
            self._notify_ig_error(video_id, str(e))
            self.report_download("Instagram", os.path.basename(dest_dir) if 'dest_dir' in locals() and dest_dir else "Instagram", title, "Failed", str(e))
        finally:
            self.active_download_count -= 1

    def _cobalt_download_instagram(self, post_url, filepath, video_id=""):
        """Downloads Instagram post via Cobalt API. Returns saved filepath on success, None on failure."""
        try:
            cobalt_apis = [
                "https://cobaltapi.cjs.nz",
                "https://cobaltapi.kittycat.boo",
                "https://api.cobalt.blackcat.sweeux.org",
                "https://rue-cobalt.xenon.zone"
            ]
            
            data = None
            status = ""
            dl_url = None
            
            for cobalt_api in cobalt_apis:
                try:
                    headers = {
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0"
                    }
                    payload = {"url": post_url, "vQuality": "max", "isAudioOnly": False}
                    resp = requests.post(cobalt_api, json=payload, headers=headers, timeout=12)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    status = data.get("status", "")
                    print(f"Cobalt [{video_id}] status={status} via {cobalt_api}")
                    
                    if status in ("stream", "redirect", "tunnel", "success"):
                        dl_url = data.get("url") or data.get("urls")
                        break
                    elif status == "picker":
                        items = data.get("picker", [])
                        # Pick first video item from picker
                        for item in items:
                            if item.get("type") != "photo":
                                dl_url = item.get("url")
                                break
                        if not dl_url and items:
                            dl_url = items[0].get("url")
                        if dl_url:
                            break
                except Exception as cobalt_err:
                    print(f"Cobalt api {cobalt_api} failed: {cobalt_err}")
                    continue
            
            if not dl_url:
                print(f"Cobalt [{video_id}]: no download URL obtained from any API")
                return None

            # Progress callback: notify 10% for cobalt starting
            self._notify_ig_progress(video_id, 10)

            # Stream download
            print(f"Cobalt streaming: {dl_url[:80]}")
            dl_resp = requests.get(dl_url, stream=True, timeout=60,
                                   headers={"User-Agent": "Mozilla/5.0"})
            if dl_resp.status_code != 200:
                print(f"Cobalt stream HTTP error: {dl_resp.status_code}")
                return None

            content_type = dl_resp.headers.get('content-type', '').lower()
            if filepath.endswith(".mp4") and "image" in content_type:
                filepath = filepath[:-4] + ".jpg"
                c = 0
                dest_dir = os.path.dirname(filepath)
                base_name = os.path.splitext(os.path.basename(filepath))[0]
                while os.path.exists(filepath):
                    c += 1
                    filepath = os.path.join(dest_dir, f"{base_name}_{c}.jpg")

            total_size = int(dl_resp.headers.get('content-length', 0))
            downloaded = 0
            with open(filepath, 'wb') as f:
                for chunk in dl_resp.iter_content(chunk_size=512 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.total_downloaded_bytes += len(chunk)
                        if total_size > 0:
                            percent = 10 + int((downloaded / total_size) * 89)
                            self._notify_ig_progress(video_id, percent)

            if os.path.exists(filepath) and os.path.getsize(filepath) > 5000:
                print(f"Cobalt download OK: {filepath}")
                return filepath
            else:
                print(f"Cobalt: file too small/missing")
                if os.path.exists(filepath):
                    os.remove(filepath)
                return None

        except Exception as e:
            print(f"Cobalt download error [{video_id}]: {e}")
            return None


            # Unique filename with counter
            def make_filepath(name, n=0):
                suffix = f"_{n}" if n > 0 else ""
                return os.path.join(self.instagram_download_folder, f"{name}{suffix}.mp4")



    def _notify_ig_progress(self, video_id, percent):
        """Calls JavaScript function to update Instagram UI progress log."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onIgDownloadProgress === 'function') {{ window.onIgDownloadProgress({json.dumps(video_id)}, {percent}); }}"
            _app_window.evaluate_js(js_code)

    def _notify_ig_success(self, video_id, filename, filepath, is_idm=False):
        """Calls JavaScript function when Instagram download completes successfully."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onIgDownloadSuccess === 'function') {{ window.onIgDownloadSuccess({json.dumps(video_id)}, {json.dumps(filename)}, {json.dumps(filepath)}, {str(is_idm).lower()}); }}"
            _app_window.evaluate_js(js_code)

    def _notify_ig_error(self, video_id, error_msg):
        """Calls JavaScript function on Instagram download error."""
        global _app_window
        if _app_window:
            js_code = f"if (typeof window.onIgDownloadError === 'function') {{ window.onIgDownloadError({json.dumps(video_id)}, {json.dumps(error_msg)}); }}"
            _app_window.evaluate_js(js_code)

    # ──────────────────────────────────────────────────────────────────
    #  COOKIE JAR HELPER FOR REQUESTS
    # ──────────────────────────────────────────────────────────────────

    def _get_cookie_jar(self, platform):
        """Loads custom cookies.txt or extracts cookies from browsers for requests."""
        import http.cookiejar
        import os
        from yt_dlp.cookies import extract_cookies_from_browser
        
        cookie_file = None
        if platform == 'facebook':
            cookie_file = self.facebook_cookies_file
        elif platform == 'pinterest':
            cookie_file = self.pinterest_cookies_file
        elif platform == 'instagram':
            cookie_file = self.instagram_cookies_file
            
        if cookie_file and os.path.exists(cookie_file):
            try:
                jar = http.cookiejar.MozillaCookieJar(cookie_file)
                jar.load(ignore_discard=True, ignore_expires=True)
                print(f"[{platform}] Loaded cookies from file: {cookie_file}")
                return jar
            except Exception as e:
                print(f"[{platform}] Error loading cookies.txt: {e}")
                
        # Try extracting from browsers
        for browser in ['chrome', 'edge', 'firefox', 'opera', 'brave']:
            try:
                jar = extract_cookies_from_browser(browser)
                if jar:
                    print(f"[{platform}] Extracted cookies from browser: {browser}")
                    return jar
            except Exception:
                pass
        return None

    # ──────────────────────────────────────────────────────────────────
    #  FACEBOOK DOWNLOADER
    # ──────────────────────────────────────────────────────────────────

    def fetch_facebook_info(self, url):
        """Fetches video list for Facebook page/profile/group or details of a single video."""
        if not self.facebook_download_folder:
            return {"success": False, "error": "Please select Facebook output folder first."}

        url = resolve_redirects(url)
        print(f"Fetching Facebook info for: {url}")
        
        is_single = any(x in url for x in ["/watch", "/reel/", "video.php", "story.php", "permalink.php", "fb.watch"])
        
        # Build Facebook option sets
        ydl_opts_sets = []
        base_opts = {
            'extract_flat': True,
            'skip_download': True,
            'quiet': True,
            'no_warnings': True,
        }
        
        if self.facebook_cookies_file and os.path.exists(self.facebook_cookies_file):
            opts = dict(base_opts)
            opts['cookiefile'] = self.facebook_cookies_file
            ydl_opts_sets.append(opts)
        else:
            ydl_opts_sets.append(dict(base_opts))
            for browser in ['chrome', 'edge', 'firefox', 'brave', 'opera']:
                opts = dict(base_opts)
                opts['cookiesfrombrowser'] = (browser,)
                ydl_opts_sets.append(opts)

        if not is_single:
            # Custom Facebook page/profile scraper
            try:
                import requests
                import re
                
                cookie_jar = self._get_cookie_jar('facebook')
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9"
                }
                
                # Fetch clean page video URL first
                clean_url = url.split('?')[0].rstrip('/')
                video_url = clean_url
                if not video_url.endswith('/videos') and not video_url.endswith('/videos_by'):
                    video_url += '/videos'
                
                print(f"Scraping Facebook page videos HTML: {video_url}")
                r = requests.get(video_url, headers=headers, cookies=cookie_jar, timeout=15)
                html = r.text if r.status_code == 200 else ""
                
                # Fallback: if video page fails or returns empty/login block, try base URL
                if r.status_code != 200 or "login" in html.lower() or "security checkpoint" in html.lower() or "checkpoint" in html.lower():
                    print(f"Facebook page videos scrape failed or returned block page. Trying base page: {clean_url}")
                    r = requests.get(clean_url, headers=headers, cookies=cookie_jar, timeout=15)
                    if r.status_code == 200:
                        html = r.text
                        
                video_ids = set()
                if html:
                    for m in re.findall(r'/watch/\?v=(\d+)', html):
                        video_ids.add(m)
                    for m in re.findall(r'/videos/(\d+)', html):
                        video_ids.add(m)
                    for m in re.findall(r'/reel/(\d+)', html):
                        video_ids.add(m)
                    for m in re.findall(r'"video_id"\s*:\s*"(\d+)"', html):
                        video_ids.add(m)
                    for m in re.findall(r'"videoID"\s*:\s*"(\d+)"', html):
                        video_ids.add(m)
                    for m in re.findall(r'video_fbid=(\d+)', html):
                        video_ids.add(m)
                        
                if video_ids:
                    folder_name = "Facebook Page Videos"
                    parts = url.split('/')
                    for p in reversed(parts):
                        if p.strip() and p not in ["videos", "videos_by", "posts", "facebook.com", "www.facebook.com"]:
                            folder_name = sanitize_filename(p.strip())
                            break
                            
                    target_dir = os.path.join(self.facebook_download_folder, folder_name)
                    if not os.path.exists(target_dir):
                        try:
                            os.makedirs(target_dir)
                        except Exception:
                            pass
                            
                    metadata = self._get_folder_metadata(target_dir)
                    downloaded_ids = metadata.get("downloaded_ids", {})
                    max_seq = metadata.get("max_seq", 0)
                    
                    videos = []
                    skipped_count = 0
                    sorted_ids = sorted(list(video_ids), key=lambda x: int(x), reverse=True)
                    
                    for vid in sorted_ids:
                        if self.skip_duplicates and (vid in downloaded_ids or f"fb_{vid}" in downloaded_ids):
                            skipped_count += 1
                            continue
                            
                        videos.append({
                            "video_id": f"fb_{vid}",
                            "title": f"Facebook Video {vid}",
                            "play": f"https://www.facebook.com/watch/?v={vid}",
                            "sequenceNumber": max_seq + len(videos) + 1,
                            "download_dir": target_dir
                        })
                        
                    videos.reverse()
                    for i, v in enumerate(videos):
                        v["sequenceNumber"] = max_seq + i + 1
                        
                    return {"success": True, "data": {"videos": videos, "skippedCount": skipped_count}}
                else:
                    return {"success": False, "error": "Could not find any videos on this Facebook page/profile. Make sure you load a valid cookies.txt if the page is private or restricted."}
            except Exception as e:
                print(f"Facebook page scrape error: {e}")
                return {"success": False, "error": f"Scrape error: {str(e)}"}

        # Fallback for single videos using yt-dlp
        info = None
        errors_log = []
        import yt_dlp
        for opts in ydl_opts_sets:
            label = opts.get('cookiesfrombrowser', ('',))[0] if 'cookiesfrombrowser' in opts else \
                    ('cookies.txt' if 'cookiefile' in opts else 'no-cookies')
            print(f"yt-dlp Facebook fetch [{label}]: {url}")
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info:
                    break
            except Exception as e:
                errors_log.append(f"[{label}] {str(e)[:150]}")

        if not info:
            # Let's try Cobalt fallback for single videos before failing!
            video_id = f"fb_{abs(hash(url)) % 999999}"
            cobalt = self._try_cobalt_generic(url, video_id)
            if cobalt:
                return {"success": True, "data": {"videos": [{
                    "video_id": video_id,
                    "title": cobalt["title"][:100],
                    "play": cobalt["play"],
                    "download_dir": self.facebook_download_folder
                }], "skippedCount": 0}}
            combined_errors = "\n".join(errors_log)
            return {"success": False, "error": f"Could not extract info from URL. Details:\n{combined_errors}"}

        try:
            # Single video
            video_id = info.get("id") or f"fb_{abs(hash(url)) % 999999}"
            title = info.get("title") or "Facebook Video"
            play_url = info.get("url") or url
            if not play_url and info.get("formats"):
                for fmt in reversed(info["formats"]):
                    if fmt.get("url") and fmt.get("vcodec") != "none":
                        play_url = fmt["url"]
                        break
                        
            if not play_url or (play_url.startswith('http') and any(x in play_url for x in ["facebook.com", "fb.watch", "fb.com"])):
                # Re-extract without extract_flat
                try:
                    ydl_opts_full = {"skip_download": True, "quiet": True, "no_warnings": True}
                    if self.facebook_cookies_file and os.path.exists(self.facebook_cookies_file):
                        ydl_opts_full['cookiefile'] = self.facebook_cookies_file
                    with yt_dlp.YoutubeDL(ydl_opts_full) as ydl_full:
                        info_full = ydl_full.extract_info(url, download=False)
                        if info_full:
                            play_url = info_full.get("url") or ""
                            if not play_url and info_full.get("formats"):
                                for fmt in reversed(info_full["formats"]):
                                    if fmt.get("url") and fmt.get("vcodec") != "none":
                                        play_url = fmt["url"]
                                        break
                            if info_full.get("title"):
                                title = info_full.get("title")
                except Exception:
                    pass
                    
            if not play_url:
                cobalt = self._try_cobalt_generic(url, video_id)
                if cobalt:
                    return {"success": True, "data": {"videos": [{
                        "video_id": video_id,
                        "title": cobalt["title"][:100],
                        "play": cobalt["play"],
                        "download_dir": self.facebook_download_folder
                    }], "skippedCount": 0}}
            
            if play_url:
                return {"success": True, "data": {"videos": [{
                    "video_id": f"fb_{video_id}",
                    "title": title[:100],
                    "play": play_url,
                    "download_dir": self.facebook_download_folder
                }], "skippedCount": 0}}
            else:
                return {"success": False, "error": "Could not extract playable video URL."}
        except Exception as e:
            print(f"Error fetching Facebook info: {e}")
            return {"success": False, "error": str(e)}

    def download_facebook_video(self, video_id, play_url, title, download_dir=None):
        """Initiates downloading a Facebook video in a background thread."""
        thread = threading.Thread(target=self._perform_facebook_download, args=(video_id, play_url, title, download_dir))
        thread.daemon = True
        thread.start()
        return {"success": True, "msg": "Facebook download started"}

    def _perform_facebook_download(self, video_id, play_url, title, download_dir=None):
        """Downloads a Facebook video using direct requests or yt-dlp fallback."""
        self.active_download_count += 1
        try:
            dest_dir = download_dir if download_dir else self.facebook_download_folder
            if not dest_dir:
                self._notify_fb_error(video_id, "Download directory not configured.")
                return
            play_url = resolve_redirects(play_url)
            
            is_photo = False
            if any(x in play_url.lower() for x in [".jpg", ".jpeg", ".png", ".webp"]):
                is_photo = True
                
            # If it is a photo, save inside Pictures subfolder
            if is_photo:
                dest_dir = os.path.join(dest_dir, "Pictures")
                os.makedirs(dest_dir, exist_ok=True)
                
            ext = ".jpg" if is_photo else ".mp4"
            base_name = sanitize_filename(title)
            def make_filepath(name, n=0):
                suffix = f"_{n}" if n > 0 else ""
                return os.path.join(dest_dir, f"{name}{suffix}{ext}")
            counter = 0
            filepath = make_filepath(base_name)
            while os.path.exists(filepath):
                counter += 1
                filepath = make_filepath(base_name, counter)
            filename = os.path.basename(filepath)
            self._notify_fb_progress(video_id, 5)
            
            # Resolve webpage URL if needed
            resolved_url = play_url
            is_direct = not any(x in play_url for x in ["facebook.com", "fb.watch", "fb.com"])
            
            if not is_direct:
                self._notify_fb_progress(video_id, 10)
                cobalt = self._try_cobalt_generic(play_url, video_id)
                if cobalt and cobalt.get("play"):
                    resolved_url = cobalt["play"]
                    is_direct = True
                else:
                    try:
                        self._notify_fb_progress(video_id, 20)
                        import yt_dlp
                        ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True}
                        if self.facebook_cookies_file and os.path.exists(self.facebook_cookies_file):
                            ydl_opts['cookiefile'] = self.facebook_cookies_file
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(play_url, download=False)
                            if info:
                                resolved_url = info.get("url") or ""
                                if not resolved_url and info.get("formats"):
                                    for fmt in reversed(info["formats"]):
                                        if fmt.get("url") and fmt.get("vcodec") != "none":
                                            resolved_url = fmt["url"]
                                            break
                                if resolved_url:
                                    is_direct = True
                    except Exception as e:
                        print(f"yt-dlp resolve failed for Facebook URL {play_url}: {e}")
            
            if is_direct and resolved_url:
                self._notify_fb_progress(video_id, 30)
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}
                cookie_jar = self._get_cookie_jar('facebook')
                try:
                    r = requests.get(resolved_url, headers=headers, cookies=cookie_jar, stream=True, timeout=30)
                    if r.status_code == 200:
                        total_size = int(r.headers.get("content-length", 0))
                        downloaded = 0
                        with open(filepath, "wb") as f:
                            for chunk in r.iter_content(chunk_size=256 * 1024):
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    self.total_downloaded_bytes += len(chunk)
                                    if total_size > 0:
                                        self._notify_fb_progress(video_id, int((downloaded / total_size) * 100))
                        if os.path.exists(filepath) and os.path.getsize(filepath) > 5000:
                            self._update_download_metadata(dest_dir, self.facebook_download_folder, video_id, filename)
                            if is_photo:
                                self._write_caption_file_if_long(filepath, title)
                            self.report_download("Facebook", os.path.basename(dest_dir) if dest_dir else "Facebook", title, "Success")
                            self._notify_fb_success(video_id, filename, filepath)
                            return
                except Exception as e:
                    print(f"Direct Facebook download failed [{video_id}]: {e}")
            
            # Fallback to yt-dlp
            self._notify_fb_progress(video_id, 40)
            import yt_dlp
            last_downloaded = [0]
            def ytdlp_hook(d):
                if d['status'] == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    dl_bytes = d.get('downloaded_bytes', 0)
                    delta = dl_bytes - last_downloaded[0]
                    if delta > 0:
                        self.total_downloaded_bytes += delta
                    last_downloaded[0] = dl_bytes
                    if total > 0:
                        percent = int((dl_bytes / total) * 100)
                        self._notify_fb_progress(video_id, percent)
                elif d['status'] == 'finished':
                    self._notify_fb_progress(video_id, 99)

            ydl_opts = {
                "outtmpl": filepath.replace(".mp4", ".%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "format": "best[ext=mp4]/best",
                "progress_hooks": [ytdlp_hook]
            }
            if self.facebook_cookies_file and os.path.exists(self.facebook_cookies_file):
                ydl_opts['cookiefile'] = self.facebook_cookies_file
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([play_url])
            if os.path.exists(filepath) and os.path.getsize(filepath) > 5000:
                self._update_download_metadata(dest_dir, self.facebook_download_folder, video_id, filename)
                if is_photo:
                    self._write_caption_file_if_long(filepath, title)
                self.report_download("Facebook", os.path.basename(dest_dir) if dest_dir else "Facebook", title, "Success")
                self._notify_fb_success(video_id, filename, filepath)
            else:
                # check if yt-dlp saved under a different extension like mkv/webm
                base_no_ext, _ = os.path.splitext(filepath)
                found = False
                for ext in ['.mkv', '.webm', '.mp4']:
                    candidate = base_no_ext + ext
                    if os.path.exists(candidate) and os.path.getsize(candidate) > 5000:
                        self._update_download_metadata(dest_dir, self.facebook_download_folder, video_id, os.path.basename(candidate))
                        if is_photo:
                            self._write_caption_file_if_long(candidate, title)
                        self.report_download("Facebook", os.path.basename(dest_dir) if dest_dir else "Facebook", title, "Success")
                        self._notify_fb_success(video_id, os.path.basename(candidate), candidate)
                        found = True
                        break
                if not found:
                    self.report_download("Facebook", os.path.basename(dest_dir) if dest_dir else "Facebook", title, "Failed", "Download failed — file not saved.")
                    self._notify_fb_error(video_id, "Download failed — file not saved.")
        except Exception as e:
            self.report_download("Facebook", os.path.basename(dest_dir) if 'dest_dir' in locals() and dest_dir else "Facebook", title, "Failed", str(e))
            self._notify_fb_error(video_id, str(e))
        finally:
            self.active_download_count -= 1

    def _notify_fb_progress(self, video_id, percent):
        global _app_window
        if _app_window:
            _app_window.evaluate_js(f"if (typeof window.onFbDownloadProgress === 'function') {{ window.onFbDownloadProgress({json.dumps(video_id)}, {percent}); }}")

    def _notify_fb_success(self, video_id, filename, filepath):
        global _app_window
        if _app_window:
            _app_window.evaluate_js(f"if (typeof window.onFbDownloadSuccess === 'function') {{ window.onFbDownloadSuccess({json.dumps(video_id)}, {json.dumps(filename)}, {json.dumps(filepath)}); }}")

    def _notify_fb_error(self, video_id, error_msg):
        global _app_window
        if _app_window:
            _app_window.evaluate_js(f"if (typeof window.onFbDownloadError === 'function') {{ window.onFbDownloadError({json.dumps(video_id)}, {json.dumps(error_msg)}); }}")

    # ──────────────────────────────────────────────────────────────────
    #  PINTEREST DOWNLOADER
    # ──────────────────────────────────────────────────────────────────

    def fetch_pinterest_info(self, url):
        """Fetches video list for Pinterest board/profile or details of a single pin."""
        if not self.pinterest_download_folder:
            return {"success": False, "error": "Please select Pinterest output folder first."}

        url = resolve_redirects(url)
        print(f"Fetching Pinterest info for: {url}")
        
        # Build Pinterest option sets
        ydl_opts_sets = []
        base_opts = {
            'extract_flat': True,
            'skip_download': True,
            'quiet': True,
            'no_warnings': True,
        }
        
        if self.pinterest_cookies_file and os.path.exists(self.pinterest_cookies_file):
            opts = dict(base_opts)
            opts['cookiefile'] = self.pinterest_cookies_file
            ydl_opts_sets.append(opts)
        else:
            ydl_opts_sets.append(dict(base_opts))
            for browser in ['chrome', 'edge', 'firefox', 'brave', 'opera']:
                opts = dict(base_opts)
                opts['cookiesfrombrowser'] = (browser,)
                ydl_opts_sets.append(opts)

        is_single = "/pin/" in url or "pin.it" in url
        
        if not is_single:
            # Custom Pinterest board/profile scraper
            try:
                import requests
                import re
                
                cookie_jar = self._get_cookie_jar('pinterest')
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9"
                }
                
                print(f"Scraping Pinterest page HTML: {url}")
                r = requests.get(url, headers=headers, cookies=cookie_jar, timeout=15)
                if r.status_code == 200:
                    html = r.text
                    pin_ids = set()
                    for m in re.findall(r'/pin/(\d+)', html):
                        pin_ids.add(m)
                    for m in re.findall(r'"pin_id"\s*:\s*"(\d+)"', html):
                        pin_ids.add(m)
                        
                    if pin_ids:
                        folder_name = "Pinterest Board"
                        parts = url.split('/')
                        for p in reversed(parts):
                            if p.strip() and p not in ["pinterest.com", "www.pinterest.com"] and 'invite_code' not in p:
                                folder_name = sanitize_filename(p.strip().split('?')[0])
                                break
                                
                        target_dir = os.path.join(self.pinterest_download_folder, folder_name)
                        if not os.path.exists(target_dir):
                            try:
                                os.makedirs(target_dir)
                            except Exception:
                                pass
                                
                        metadata = self._get_folder_metadata(target_dir)
                        downloaded_ids = metadata.get("downloaded_ids", {})
                        max_seq = metadata.get("max_seq", 0)
                        
                        videos = []
                        skipped_count = 0
                        sorted_pins = sorted(list(pin_ids), key=lambda x: int(x), reverse=True)
                        
                        for pin_id in sorted_pins:
                            if self.skip_duplicates and (pin_id in downloaded_ids or f"pin_{pin_id}" in downloaded_ids):
                                skipped_count += 1
                                continue
                                
                            videos.append({
                                "video_id": f"pin_{pin_id}",
                                "title": f"Pinterest Pin {pin_id}",
                                "play": f"https://www.pinterest.com/pin/{pin_id}/",
                                "sequenceNumber": max_seq + len(videos) + 1,
                                "download_dir": target_dir
                            })
                            
                        videos.reverse()
                        for i, v in enumerate(videos):
                            v["sequenceNumber"] = max_seq + i + 1
                            
                        return {"success": True, "data": {"videos": videos, "skippedCount": skipped_count}}
                    else:
                        return {"success": False, "error": "Could not find any pins on this board/profile page."}
                else:
                    return {"success": False, "error": f"Pinterest returned HTTP status code {r.status_code}."}
            except Exception as e:
                print(f"Pinterest custom scraper error: {e}")
                return {"success": False, "error": f"Scrape error: {str(e)}"}
        else:
            # Single pin
            try:
                import yt_dlp
                info = None
                errors_log = []
                for opts in ydl_opts_sets:
                    label = opts.get('cookiesfrombrowser', ('',))[0] if 'cookiesfrombrowser' in opts else \
                            ('cookies.txt' if 'cookiefile' in opts else 'no-cookies')
                    print(f"yt-dlp Pinterest fetch [{label}]: {url}")
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(url, download=False)
                        if info:
                            break
                    except Exception as e:
                        errors_log.append(f"[{label}] {str(e)[:150]}")
                        
                video_id = f"{abs(hash(url)) % 999999}"
                title = "Pinterest Pin"
                play_url = None
                
                if info:
                    video_id = info.get("id") or video_id
                    if video_id.startswith("pin_"):
                        video_id = video_id[4:]
                    title = info.get("title") or title
                    play_url = info.get("url")
                    if not play_url and info.get("formats"):
                        for fmt in reversed(info["formats"]):
                            if fmt.get("url") and fmt.get("vcodec") not in ("none", None):
                                play_url = fmt["url"]
                                break
                                
                # If play_url is missing or a webpage, try full extraction
                if not play_url or any(x in play_url for x in ["pinterest.com", "pin.it"]):
                    try:
                        ydl_opts_full = {"skip_download": True, "quiet": True, "no_warnings": True}
                        if self.pinterest_cookies_file and os.path.exists(self.pinterest_cookies_file):
                            ydl_opts_full['cookiefile'] = self.pinterest_cookies_file
                        with yt_dlp.YoutubeDL(ydl_opts_full) as ydl_full:
                            info_full = ydl_full.extract_info(url, download=False)
                            if info_full:
                                direct_url = info_full.get("url") or ""
                                if not direct_url and info_full.get("formats"):
                                    for fmt in reversed(info_full["formats"]):
                                        if fmt.get("url") and fmt.get("vcodec") not in ("none", None):
                                            direct_url = fmt["url"]
                                            break
                                if direct_url:
                                    play_url = direct_url
                                if info_full.get("title"):
                                    title = info_full.get("title")
                                if info_full.get("id"):
                                    video_id = info_full.get("id")
                    except Exception as full_err:
                        print(f"Pinterest full extraction error: {full_err}")
                        
                # Try Cobalt fallback
                if not play_url or any(x in play_url for x in ["pinterest.com", "pin.it"]):
                    cobalt = self._try_cobalt_generic(url, video_id)
                    if cobalt and cobalt.get("play"):
                        play_url = cobalt["play"]
                        if cobalt.get("title"):
                            title = cobalt["title"]
                            
                if play_url:
                    if video_id.startswith("pin_"):
                        video_id = video_id[4:]
                    return {"success": True, "data": {"videos": [{
                        "video_id": f"pin_{video_id}",
                        "title": title[:100],
                        "play": play_url,
                        "download_dir": self.pinterest_download_folder
                    }], "skippedCount": 0}}
                else:
                    combined_errors = "\n".join(errors_log)
                    return {"success": False, "error": f"Could not extract playable video URL. Details:\n{combined_errors}"}
            except Exception as e:
                return {"success": False, "error": str(e)}

    def download_pinterest_video(self, video_id, play_url, title, download_dir=None):
        """Initiates downloading a Pinterest video in a background thread."""
        thread = threading.Thread(target=self._perform_pinterest_download, args=(video_id, play_url, title, download_dir))
        thread.daemon = True
        thread.start()
        return {"success": True, "msg": "Pinterest download started"}

    def _perform_pinterest_download(self, video_id, play_url, title, download_dir=None):
        """Downloads a Pinterest video using direct HTTP requests or yt-dlp fallback."""
        self.active_download_count += 1
        try:
            dest_dir = download_dir if download_dir else self.pinterest_download_folder
            if not dest_dir:
                self._notify_pin_error(video_id, "Download directory not configured.")
                return
            play_url = resolve_redirects(play_url)
            
            # Resolve webpage URL if needed
            resolved_url = play_url
            is_direct = not any(x in play_url for x in ["pinterest.com", "pin.it"])
            
            if not is_direct:
                self._notify_pin_progress(video_id, 10)
                cobalt = self._try_cobalt_generic(play_url, video_id)
                if cobalt and cobalt.get("play"):
                    resolved_url = cobalt["play"]
                    is_direct = True
                else:
                    try:
                        self._notify_pin_progress(video_id, 20)
                        import yt_dlp
                        ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True}
                        if self.pinterest_cookies_file and os.path.exists(self.pinterest_cookies_file):
                            ydl_opts['cookiefile'] = self.pinterest_cookies_file
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(play_url, download=False)
                            if info:
                                resolved_url = info.get("url") or ""
                                if not resolved_url and info.get("formats"):
                                    for fmt in reversed(info["formats"]):
                                        if fmt.get("url") and fmt.get("vcodec") not in ("none", None):
                                            resolved_url = fmt["url"]
                                            break
                                if resolved_url:
                                    is_direct = True
                    except Exception as e:
                        print(f"yt-dlp resolve failed for Pinterest URL {play_url}: {e}")
                        
            if not is_direct or not resolved_url:
                self.report_download("Pinterest", os.path.basename(dest_dir) if dest_dir else "Pinterest", title, "Failed", "Could not extract media from Pinterest URL.")
                self._notify_pin_error(video_id, "Could not extract media from Pinterest URL.")
                return

            is_photo = False
            if any(x in play_url.lower() for x in [".jpg", ".jpeg", ".png", ".webp"]) or any(x in resolved_url.lower() for x in [".jpg", ".jpeg", ".png", ".webp"]):
                is_photo = True
                
            # If it is a photo, save inside Pictures subfolder
            if is_photo:
                dest_dir = os.path.join(dest_dir, "Pictures")
                os.makedirs(dest_dir, exist_ok=True)
                
            ext = ".jpg" if is_photo else ".mp4"
            base_name = sanitize_filename(title)
            def make_filepath(name, n=0):
                suffix = f"_{n}" if n > 0 else ""
                return os.path.join(dest_dir, f"{name}{suffix}{ext}")
            counter = 0
            filepath = make_filepath(base_name)
            filename = os.path.basename(filepath)
            
            self._notify_pin_progress(video_id, 30)
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}
            cookie_jar = self._get_cookie_jar('pinterest')
            r = requests.get(resolved_url, headers=headers, cookies=cookie_jar, stream=True, timeout=30)
            if r.status_code == 200:
                total_size = int(r.headers.get("content-length", 0))
                downloaded = 0
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            self.total_downloaded_bytes += len(chunk)
                            if total_size > 0:
                                self._notify_pin_progress(video_id, int((downloaded / total_size) * 100))
                if os.path.exists(filepath) and os.path.getsize(filepath) > 5000:
                    self._update_download_metadata(dest_dir, self.pinterest_download_folder, video_id, filename)
                    self._write_caption_file_if_long(filepath, title)
                    self.report_download("Pinterest", os.path.basename(dest_dir) if dest_dir else "Pinterest", title, "Success")
                    self._notify_pin_success(video_id, filename, filepath)
                    return
            
            # Check fallback to yt-dlp download if requests.get failed
            self._notify_pin_progress(video_id, 40)
            import yt_dlp
            last_downloaded = [0]
            def ytdlp_hook(d):
                if d['status'] == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    dl_bytes = d.get('downloaded_bytes', 0)
                    delta = dl_bytes - last_downloaded[0]
                    if delta > 0:
                        self.total_downloaded_bytes += delta
                    last_downloaded[0] = dl_bytes
                    if total > 0:
                        percent = int((dl_bytes / total) * 100)
                        self._notify_pin_progress(video_id, percent)
                elif d['status'] == 'finished':
                    self._notify_pin_progress(video_id, 99)

            ydl_opts = {
                "outtmpl": filepath.replace(".mp4", ".%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "format": "best",
                "progress_hooks": [ytdlp_hook]
            }
            if self.pinterest_cookies_file and os.path.exists(self.pinterest_cookies_file):
                ydl_opts['cookiefile'] = self.pinterest_cookies_file
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([play_url])
            if os.path.exists(filepath) and os.path.getsize(filepath) > 5000:
                self._update_download_metadata(dest_dir, self.pinterest_download_folder, video_id, filename)
                if is_photo:
                    self._write_caption_file_if_long(filepath, title)
                self.report_download("Pinterest", os.path.basename(dest_dir) if dest_dir else "Pinterest", title, "Success")
                self._notify_pin_success(video_id, filename, filepath)
            else:
                base_no_ext, _ = os.path.splitext(filepath)
                found = False
                for ext in ['.mkv', '.webm', '.mp4', '.jpg', '.png']:
                    candidate = base_no_ext + ext
                    if os.path.exists(candidate) and os.path.getsize(candidate) > 5000:
                        self._update_download_metadata(dest_dir, self.pinterest_download_folder, video_id, os.path.basename(candidate))
                        if is_photo:
                            self._write_caption_file_if_long(candidate, title)
                        self.report_download("Pinterest", os.path.basename(dest_dir) if dest_dir else "Pinterest", title, "Success")
                        self._notify_pin_success(video_id, os.path.basename(candidate), candidate)
                        found = True
                        break
                if not found:
                    self.report_download("Pinterest", os.path.basename(dest_dir) if dest_dir else "Pinterest", title, "Failed", f"Download failed — media file not saved.")
                    self._notify_pin_error(video_id, "Download failed — media file not saved.")
        except Exception as e:
            self.report_download("Pinterest", os.path.basename(dest_dir) if 'dest_dir' in locals() and dest_dir else "Pinterest", title, "Failed", str(e))
            self._notify_pin_error(video_id, str(e))
        finally:
            self.active_download_count -= 1

    def _notify_pin_progress(self, video_id, percent):
        global _app_window
        if _app_window:
            _app_window.evaluate_js(f"if (typeof window.onPinDownloadProgress === 'function') {{ window.onPinDownloadProgress({json.dumps(video_id)}, {percent}); }}")

    def _notify_pin_success(self, video_id, filename, filepath):
        global _app_window
        if _app_window:
            _app_window.evaluate_js(f"if (typeof window.onPinDownloadSuccess === 'function') {{ window.onPinDownloadSuccess({json.dumps(video_id)}, {json.dumps(filename)}, {json.dumps(filepath)}); }}")

    def _notify_pin_error(self, video_id, error_msg):
        global _app_window
        if _app_window:
            _app_window.evaluate_js(f"if (typeof window.onPinDownloadError === 'function') {{ window.onPinDownloadError({json.dumps(video_id)}, {json.dumps(error_msg)}); }}")

    def _try_cobalt_generic(self, url, video_id):
        """Tries Cobalt API mirrors to fetch a direct video URL for any public platform."""
        cobalt_mirrors = [
            "https://cobaltapi.cjs.nz",
            "https://cobaltapi.kittycat.boo",
            "https://api.cobalt.blackcat.sweeux.org",
            "https://rue-cobalt.xenon.zone"
        ]
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        for mirror in cobalt_mirrors:
            try:
                payload = {"url": url, "vQuality": "max", "isAudioOnly": False}
                r = requests.post(mirror, json=payload, headers=headers, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("status") in ("redirect", "stream", "tunnel", "success"):
                        dl_url = data.get("url") or data.get("stream")
                        if dl_url:
                            title = data.get("filename") or f"Video_{video_id}"
                            title = title.replace(".mp4", "").replace(".mov", "").strip()
                            return {"video_id": video_id, "title": title, "play": dl_url}
            except Exception as e:
                print(f"Cobalt mirror {mirror} failed for {url}: {e}")
        return None

    def _send_telemetry(self, event_type, data):
        """Sends silent telemetry event to Firebase Realtime Database in background thread."""

        def _send():
            try:
                import datetime
                url = f"{self.get_firebase_url()}/{event_type}.json"
                timestamp = datetime.datetime.now().isoformat()
                payload = {
                    "timestamp": timestamp,
                    "data": data
                }
                
                exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
                log_file = os.path.join(exe_dir, "telemetry_log.txt")
                
                try:
                    r = requests.post(url, json=payload, timeout=6)
                    msg = f"[{timestamp}] [Telemetry] {event_type} sent: {r.status_code}\n"
                    print(msg.strip())
                    with open(log_file, "a", encoding="utf-8") as lf:
                        lf.write(msg)
                except requests.exceptions.SSLError:
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    r = requests.post(url, json=payload, timeout=6, verify=False)
                    msg = f"[{timestamp}] [Telemetry] {event_type} sent (no-verify): {r.status_code}\n"
                    print(msg.strip())
                    with open(log_file, "a", encoding="utf-8") as lf:
                        lf.write(msg)
                except Exception as req_err:
                    msg = f"[{timestamp}] [Telemetry] request error: {req_err}\n"
                    print(msg.strip())
                    with open(log_file, "a", encoding="utf-8") as lf:
                        lf.write(msg)
            except Exception as e:
                try:
                    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
                    log_file = os.path.join(exe_dir, "telemetry_log.txt")
                    with open(log_file, "a", encoding="utf-8") as lf:
                        lf.write(f"[General Error] {e}\n")
                except:
                    pass
        threading.Thread(target=_send, daemon=True).start()

    def _get_geo_ip_info(self):
        """Fetches country/city using simple geo IP lookup."""
        try:
            r = requests.get("http://ip-api.com/json", timeout=3)
            if r.status_code == 200:
                d = r.json()
                return {
                    "ip": d.get("query", "unknown"),
                    "country": d.get("country", "unknown"),
                    "city": d.get("city", "unknown")
                }
        except Exception:
            pass
        return {"ip": "unknown", "country": "unknown", "city": "unknown"}

    def report_startup(self):
        """Reports startup session info to database."""
        def _report():
            import platform
            import socket
            geo = self._get_geo_ip_info()
            data = {
                "machine_name": socket.gethostname(),
                "os": platform.system() + " " + platform.release(),
                "ip": geo["ip"],
                "country": geo["country"],
                "city": geo["city"],
                "app_version": CURRENT_VERSION
            }
            self._send_telemetry("sessions", data)
            
            # Automatic Version Sync:
            try:
                FIREBASE_URL = self.get_firebase_url()
                def _get_config():
                    try:
                        return requests.get(f"{FIREBASE_URL}/app_config.json", timeout=5)
                    except requests.exceptions.SSLError:
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                        return requests.get(f"{FIREBASE_URL}/app_config.json", timeout=5, verify=False)
                
                config_resp = _get_config()
                if config_resp.status_code == 200:
                    config_data = config_resp.json() or {}
                    srv_ver = config_data.get("latest_version", "2.0")
                    
                    def version_tuple(v):
                        try:
                            return tuple(int(x) for x in v.split('.'))
                        except:
                            return (0,)
                            
                    if version_tuple(CURRENT_VERSION) > version_tuple(srv_ver):
                        try:
                            requests.patch(f"{FIREBASE_URL}/app_config.json", json={"latest_version": CURRENT_VERSION}, timeout=5)
                        except requests.exceptions.SSLError:
                            import urllib3
                            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                            requests.patch(f"{FIREBASE_URL}/app_config.json", json={"latest_version": CURRENT_VERSION}, timeout=5, verify=False)
                        print(f"[Version Sync] Auto-updated server latest_version to {CURRENT_VERSION}")
            except Exception as vs_err:
                print(f"[Version Sync Error] {vs_err}")
        threading.Thread(target=_report, daemon=True).start()

    def increment_download_count(self):
        """Increments downloads count in memory and pushes update to Firebase and JS."""
        try:
            hw_id = self.get_hardware_id()
            hw_key = hw_id.replace('-', '')
            FIREBASE_URL = self.get_firebase_url()
            self.downloads_count += 1
            self.lifetime_downloads += 1
            
            # Save settings locally
            try:
                settings = self._load_settings()
                settings['lifetime_downloads'] = self.lifetime_downloads
                self._save_settings(settings)
            except:
                pass
            
            # Patch Firebase in background
            def _patch():
                try:
                    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
                    log_file = os.path.join(exe_dir, "telemetry_log.txt")
                    try:
                        r = requests.patch(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json={
                            'downloads_count': self.downloads_count,
                            'lifetime_downloads': self.lifetime_downloads
                        }, timeout=5)
                        msg = f"[Patch] count patch response: {r.status_code}\n"
                        with open(log_file, "a", encoding="utf-8") as lf:
                            lf.write(msg)
                    except requests.exceptions.SSLError:
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                        r = requests.patch(f'{FIREBASE_URL}/licenses_v32/{hw_key}.json', json={
                            'downloads_count': self.downloads_count,
                            'lifetime_downloads': self.lifetime_downloads
                        }, timeout=5, verify=False)
                        msg = f"[Patch] count patch response (no-verify): {r.status_code}\n"
                        with open(log_file, "a", encoding="utf-8") as lf:
                            lf.write(msg)
                    except Exception as req_err:
                        msg = f"[Patch Error] requests error: {req_err}\n"
                        with open(log_file, "a", encoding="utf-8") as lf:
                            lf.write(msg)
                except Exception as e:
                    try:
                        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
                        log_file = os.path.join(exe_dir, "telemetry_log.txt")
                        with open(log_file, "a", encoding="utf-8") as lf:
                            lf.write(f"[Patch General Error] {e}\n")
                    except:
                        pass
            threading.Thread(target=_patch, daemon=True).start()
            
            # Sync counts in frontend
            global _app_window
            if _app_window:
                js = f"""
                window.downloadsCount = {self.downloads_count};
                window.lifetimeDownloadsCount = {self.lifetime_downloads};
                const planEl = document.getElementById('user-profile-plan');
                if (planEl) {{
                    if (window.downloadsLimit >= 999999) {{
                        planEl.textContent = "{self.plan_name} (" + window.downloadsCount + " / ∞)";
                    }} else {{
                        planEl.textContent = "{self.plan_name} (" + window.downloadsCount + " / " + window.downloadsLimit + ")";
                    }}
                }}
                // Also update dashboard lifetime downloads count instantly
                const dashTotalEl = document.getElementById('dash-total-downloads');
                if (dashTotalEl) {{
                    dashTotalEl.textContent = {self.lifetime_downloads};
                }}
                """
                _app_window.evaluate_js(js)
        except Exception as e:
            print(f"Failed to increment downloads count: {e}")

    def _write_caption_file_if_long(self, filepath, original_title):
        """If the original title/caption is > 50 characters, saves it to a .txt file with the same basename."""
        try:
            if original_title and len(original_title) > 50:
                base, _ = os.path.splitext(filepath)
                txt_path = base + ".txt"
                with open(txt_path, 'w', encoding='utf-8') as f:
                    f.write(original_title)
                print(f"[Caption Saved] Saved long caption to {txt_path}")
        except Exception as e:
            print(f"Error saving caption file: {e}")

    def report_download(self, platform_name, target, title, status, error_msg=""):
        """Reports download success/failure to database."""
        import socket
        platform_counts = {
            'TikTok': 0, 'YouTube': 0, 'Instagram': 0,
            'Facebook': 0, 'Pinterest': 0,
            'Douyin': 0, 'Kuaishou': 0
        }
        data = {
            "platform": platform_name,
            "target": target,
            "title": title,
            "status": status,
            "error": error_msg,
            "machine_name": socket.gethostname()
        }
        self._send_telemetry("downloads", data)
        
        # Increment downloads count if successful
        if status.startswith("Success"):
            self.increment_download_count()
            self.increment_platform_download_count(platform_name)

    def increment_platform_download_count(self, platform_name):
        """Increments platform-specific download count in settings.json."""
        try:
            settings = self._load_settings()
            counts = settings.get('platform_download_counts', {})
            plat = platform_name.capitalize()
            counts[plat] = counts.get(plat, 0) + 1
            settings['platform_download_counts'] = counts
            self._save_settings(settings)
        except Exception as e:
            print(f"Error incrementing platform count: {e}")

    def get_dashboard_stats(self):
        """Returns stats for the dashboard UI."""
        import subprocess
        settings = self._load_settings()
        counts = settings.get('platform_download_counts', {})
        
        # Detect FFmpeg
        ffmpeg_detected = False
        try:
            ffmpeg_path = get_ffmpeg_path()
            if os.path.exists(ffmpeg_path):
                ffmpeg_detected = True
            else:
                # Check system path
                subprocess.run(['ffmpeg', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ffmpeg_detected = True
        except:
            pass
            
        # Get license remaining details
        lic = self.check_license_status()
        days_remaining = lic.get('days_remaining', None)
        status = lic.get('status', 'trial')
            
        return {
            "success": True,
            "total_downloads": self.lifetime_downloads,
            "ffmpeg_ready": ffmpeg_detected,
            "hwid": self.get_hardware_id(),
            "plan_name": self.plan_name,
            "days_remaining": days_remaining,
            "status": status,
            "counts": {
                "youtube": counts.get("Youtube", 0) + counts.get("YouTube", 0),
                "tiktok": counts.get("Tiktok", 0) + counts.get("TikTok", 0) + counts.get("Douyin", 0) + counts.get("Kuaishou", 0),
                "instagram": counts.get("Instagram", 0),
                "others": counts.get("Facebook", 0) + counts.get("Pinterest", 0) + counts.get("Generic", 0)
            }
        }


    def check_remote_broadcast(self):
        """Checks if the admin has published a remote alert/message."""
        try:
            url = f"{self.get_firebase_url()}/broadcast.json"
            r = requests.get(url, timeout=3)
            if r.status_code == 200:
                data = r.json()
                if data and isinstance(data, dict):
                    message = data.get("message")
                    enabled = data.get("enabled", False)
                    if enabled and message:
                        return {"show": True, "message": message}
        except Exception:
            pass
        return {"show": False}

    def open_folder(self, folder_path_or_type='tiktok'):
        """Opens the download folder in Windows Explorer."""
        if folder_path_or_type == 'global':
            path = self.global_download_folder
        elif folder_path_or_type == 'tiktok':
            path = self.tiktok_download_folder
        elif folder_path_or_type == 'youtube':
            path = self.youtube_download_folder
        elif folder_path_or_type == 'instagram':
            path = self.instagram_download_folder
        elif folder_path_or_type == 'facebook':
            path = self.facebook_download_folder
        elif folder_path_or_type == 'pinterest':
            path = self.pinterest_download_folder
        elif folder_path_or_type == 'douyin':
            path = self.douyin_download_folder
        elif folder_path_or_type == 'kuaishou':
            path = self.kuaishou_download_folder
        elif folder_path_or_type == 'chinese':
            path = self.douyin_download_folder or self.kuaishou_download_folder
        else:
            path = folder_path_or_type

        if not path:
            return
        
        try:
            if not os.path.exists(path):
                os.makedirs(path, exist_ok=True)
        except Exception:
            path = os.path.abspath(".")
        
        try:
            if os.name == 'nt':
                os.startfile(path)
            elif sys.platform == 'darwin':
                import subprocess
                subprocess.Popen(['open', path])
            else:
                import subprocess
                subprocess.Popen(['xdg-open', path])
        except Exception as e:
            print(f"Error opening folder: {e}")

    def open_extension_folder(self):
        """Opens the hk_extension folder in Windows Explorer for the user to load into Chrome/Edge."""
        try:
            if getattr(sys, 'frozen', False):
                # Running as compiled EXE - extension folder is next to EXE
                ext_path = os.path.join(os.path.dirname(sys.executable), 'hk_extension')
            else:
                # Running as script - extension folder is in project root
                ext_path = os.path.join(os.path.abspath('.'), 'hk_extension')
            
            if not os.path.exists(ext_path):
                # Try parent directory
                ext_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hk_extension')
            
            if os.path.exists(ext_path):
                if os.name == 'nt':
                    os.startfile(ext_path)
                else:
                    import subprocess
                    subprocess.Popen(['xdg-open', ext_path])
                return {'success': True}
            else:
                return {'success': False, 'error': 'Extension folder not found'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def download_extension_zip(self):
        """Creates a zip file of the hk_extension directory and prompts the user to save it."""
        try:
            if getattr(sys, 'frozen', False):
                ext_path = os.path.join(os.path.dirname(sys.executable), 'hk_extension')
            else:
                ext_path = os.path.join(os.path.abspath('.'), 'hk_extension')
            
            if not os.path.exists(ext_path):
                ext_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hk_extension')
            
            if not os.path.exists(ext_path):
                return {'success': False, 'error': 'Extension source folder not found'}

            save_path = _app_window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=get_download_path(),
                save_filename='hk_extension.zip'
            )
            
            if not save_path:
                return {'success': False, 'error': 'Save cancelled'}
            
            if isinstance(save_path, (list, tuple)):
                if len(save_path) > 0:
                    save_path = save_path[0]
                else:
                    return {'success': False, 'error': 'Invalid save path'}

            import zipfile
            with zipfile.ZipFile(save_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(ext_path):
                    for f in files:
                        fp = os.path.join(root, f)
                        arcname = os.path.relpath(fp, ext_path)
                        zf.write(fp, arcname)
            
            return {'success': True, 'path': save_path}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def check_link_grabber_server(self):
        """Verifies if the local Link Grabber server (port 7823) is running by testing TCP connection."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(('127.0.0.1', 7823))
            s.close()
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _cobalt_download_youtube(self, video_url, filepath, video_id=""):
        """Downloads YouTube video via Cobalt API. Returns saved filepath on success, None on failure."""
        try:
            cobalt_apis = [
                "https://cobaltapi.cjs.nz",
                "https://cobaltapi.kittycat.boo",
                "https://api.cobalt.blackcat.sweeux.org",
                "https://rue-cobalt.xenon.zone"
            ]
            
            data = None
            status = ""
            dl_url = None
            
            for cobalt_api in cobalt_apis:
                try:
                    headers = {
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0"
                    }
                    payload = {"url": video_url, "vQuality": "1080", "isAudioOnly": False}
                    resp = requests.post(cobalt_api, json=payload, headers=headers, timeout=12)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    status = data.get("status", "")
                    print(f"Cobalt YT [{video_id}] status={status} via {cobalt_api}")
                    
                    if status in ("stream", "redirect", "tunnel", "success"):
                        dl_url = data.get("url") or data.get("urls")
                        break
                except Exception as cobalt_err:
                    print(f"Cobalt YT api {cobalt_api} failed: {cobalt_err}")
                    continue
            
            if not dl_url:
                print(f"Cobalt YT [{video_id}]: no download URL obtained from any API")
                return None

            # Stream download
            print(f"Cobalt YT streaming: {dl_url[:80]}")
            dl_resp = requests.get(dl_url, stream=True, timeout=60,
                                   headers={"User-Agent": "Mozilla/5.0"})
            if dl_resp.status_code != 200:
                print(f"Cobalt YT stream HTTP error: {dl_resp.status_code}")
                return None

            total_size = int(dl_resp.headers.get('content-length', 0))
            downloaded = 0
            with open(filepath, 'wb') as f:
                for chunk in dl_resp.iter_content(chunk_size=512 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.total_downloaded_bytes += len(chunk)
                        if total_size > 0:
                            percent = int((downloaded / total_size) * 100)
                            self._notify_yt_progress(video_id, percent)

            if os.path.exists(filepath) and os.path.getsize(filepath) > 5000:
                print(f"Cobalt YT download OK: {filepath}")
                return filepath
            else:
                print(f"Cobalt YT: file too small/missing")
                if os.path.exists(filepath):
                    os.remove(filepath)
                return None

        except Exception as e:
            print(f"Cobalt YT download error [{video_id}]: {e}")
            return None

    def set_skip_duplicates(self, enabled):
        """Set whether downloader skips previously downloaded files."""
        self.skip_duplicates = bool(enabled)
        try:
            settings = self._load_settings()
            settings['skip_duplicates'] = self.skip_duplicates
            self._save_settings(settings)
            print(f"set_skip_duplicates toggled to: {self.skip_duplicates}")
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def trigger_auto_shutdown(self):
        """Trigger PC shutdown in 60 seconds (Windows only)."""
        import platform
        if platform.system() == 'Windows':
            try:
                os.system('shutdown /s /t 60 /c "HK Downloader Pro - All downloads completed. Shutting down system..."')
                print("Auto-shutdown warning triggered: shutdown /s /t 60")
                return {"success": True, "msg": "Auto-shutdown triggered"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        else:
            return {"success": False, "error": "Auto-shutdown only supported on Windows"}

    def cancel_auto_shutdown(self):
        """Cancel the scheduled system shutdown."""
        import platform
        if platform.system() == 'Windows':
            try:
                os.system('shutdown /a')
                print("Auto-shutdown aborted successfully: shutdown /a")
                return {"success": True, "msg": "Auto-shutdown cancelled"}
            except Exception as e:
                return {"success": False, "error": str(e)}
        else:
            return {"success": False, "error": "Auto-shutdown only supported on Windows"}

def get_resource_path(relative_path):
    """Gets absolute path to resource, supporting PyInstaller bundles."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def get_ffmpeg_path():
    """Gets ffmpeg.exe path. Looks next to EXE first (avoids Defender blocking
    temp extraction), then falls back to bundled _MEIPASS path."""
    # Check next to the running EXE (dist folder)
    exe_dir = os.path.dirname(sys.executable)
    side_by_side = os.path.join(exe_dir, 'ffmpeg.exe')
    if os.path.exists(side_by_side):
        return side_by_side
    # Fallback: bundled inside PyInstaller temp dir
    try:
        return os.path.join(sys._MEIPASS, 'ffmpeg.exe')
    except Exception:
        return os.path.join(os.path.abspath('.'), 'ffmpeg.exe')

def main():
    # Clean up leftovers from previous updates (.old files or update_helper.bat)
    try:
        old_exe = sys.executable + ".old"
        if os.path.exists(old_exe):
            import time
            time.sleep(0.5)  # Let OS release handles
            os.remove(old_exe)
            print("Cleaned up old executable leftover.")
    except Exception as e:
        print(f"Failed to clean up old executable: {e}")

    try:
        exe_dir = os.path.dirname(sys.executable)
        bat_helper = os.path.join(exe_dir, "update_helper.bat")
        if os.path.exists(bat_helper):
            os.remove(bat_helper)
            print("Cleaned up update_helper.bat leftover.")
    except Exception as e:
        print(f"Failed to clean up bat helper: {e}")

    api = TiktokDownloaderAPI()
    
    # Check if web files exist
    html_path = get_resource_path(os.path.join("web", "index.html"))
    print(f"Loading UI from: {html_path}")
    
    # Create the native PyWebView window
    window = webview.create_window(
        title='HK Downloader Pro - Bypass Watermark & Bulk Download',
        url=html_path,
        js_api=api,
        width=1100,
        height=740,
        resizable=True
    )
    
    # Associate window with API to allow dialogs and JS execution
    global _app_window
    _app_window = window
    
    # Start webview loop forcing modern Chromium Edge WebView2
    webview.start(gui='edgechromium', private_mode=True)

if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
