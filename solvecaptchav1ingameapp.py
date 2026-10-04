import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")

import json
import os
import re
import time
import base64
import shutil
import threading
import requests
import urllib.parse
import random
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import psutil
from DrissionPage import Chromium

try:
    import winreg
except ImportError:
    winreg = None

requests.packages.urllib3.disable_warnings()

# ============================================================
#  PATHS & CONSTANTS
# ============================================================
ROOT_DIR = Path(__file__).parent.resolve()
ACC_FILE = ROOT_DIR / "accounts.txt"
RESULTS_FILE = ROOT_DIR / "captcha_results.txt"
WEBVIEW_PROFILE_DIR = ROOT_DIR / "roblox_webview_profile"

def get_omocaptcha_dir():
    """Tự động tìm thư mục extension omocaptcha (ưu tiên ROOT_DIR, sau đó đến D:\regroblox)."""
    candidates = [
        ROOT_DIR / "omocaptcha",
        Path(r"D:\regroblox\omocaptcha"),
        Path(r"C:\regroblox\omocaptcha"),
    ]
    for p in candidates:
        if (p / "manifest.json").exists():
            return p
    return ROOT_DIR / "omocaptcha"

OMOCAPTCHA_DIR = get_omocaptcha_dir()
OMO_CONFIG = OMOCAPTCHA_DIR / "configs.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

HEADERS_BASE = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://www.roblox.com",
    "Referer": "https://www.roblox.com/",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "Accept-Language": "en-US,en;q=0.9",
}

POPULAR_PLACES = [
    {"placeId": 2753915549, "name": "Blox Fruits"},
]

# ============================================================
#  OMOCAPTCHA UTILITIES (Đồng bộ với helpsolve1.py)
# ============================================================
def read_omocaptcha_key():
    """Đọc api_key hiện hành trong configs.json của extension."""
    cfg_file = get_omocaptcha_dir() / "configs.json"
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        return str(data.get("api_key", "")).strip()
    except Exception:
        return ""

def write_omocaptcha_source_key(api_key=None):
    """Cập nhật key và bật cờ power_on trong configs.json."""
    cfg_file = get_omocaptcha_dir() / "configs.json"
    if not cfg_file.exists():
        return
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        if api_key:
            data["api_key"] = str(api_key).strip()
        data["power_on"] = True
        cfg_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  [!] Lỗi ghi configs.json: {e}")

def check_omocaptcha_key():
    """Kiểm tra số dư/tính khả dụng của key OMOcaptcha."""
    key = read_omocaptcha_key()
    if not key:
        return False, "configs.json không có api_key"
    try:
        r = requests.post(
            "https://api.omocaptcha.com/v2/getBalance",
            json={"clientKey": key},
            timeout=15,
            verify=False,
        )
        data = r.json()
    except Exception as e:
        return True, f"Không kiểm tra được key ({e}) - tiếp tục chạy"
    if data.get("errorId"):
        return False, f"{data.get('errorCode')} - {data.get('errorDescription')}"
    return True, f"Key OK, Số dư: {data.get('balance')}"

def reset_stale_omocaptcha_storage(profile_dir):
    """Xóa chrome.storage cũ của extension trong profile nếu key bị lệch."""
    key = read_omocaptcha_key()
    if not key:
        return
    store = Path(profile_dir) / "EBWebView" / "Default" / "Local Extension Settings"
    if not store.is_dir():
        store = Path(profile_dir) / "Default" / "Local Extension Settings"
    if not store.is_dir():
        return

    for ext_dir in store.iterdir():
        if not ext_dir.is_dir():
            continue
        blob = b""
        try:
            for f in ext_dir.iterdir():
                if f.is_file():
                    blob += f.read_bytes()
        except OSError:
            continue
        found = set(re.findall(rb"OMO_[A-Z0-9]{20,}", blob))
        if not found or found == {key.encode()}:
            continue
        shutil.rmtree(ext_dir, ignore_errors=True)
        print(f"  [*] Đã làm mới key OMO cũ trong profile {Path(profile_dir).name}")

# ============================================================
#  ROBLOX API
# ============================================================
def get_csrf(session):
    try:
        r = session.post("https://auth.roblox.com/v2/logout", headers=HEADERS_BASE, timeout=10)
        return r.headers.get("x-csrf-token", "")
    except Exception:
        return ""

def get_user_info(session):
    try:
        r = session.get("https://users.roblox.com/v1/users/authenticated", headers=HEADERS_BASE, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return d.get("name", "?"), d.get("id", 0)
    except Exception:
        pass
    return "?", 0

def trigger_captcha(session, csrf):
    headers = {**HEADERS_BASE, "x-csrf-token": csrf}
    for place in POPULAR_PLACES:
        pid, name = place["placeId"], place["name"]
        print(f"      {name}...", end=" ")
        try:
            r = session.post(
                "https://gamejoin.roblox.com/v1/join-game",
                headers=headers,
                json={"placeId": pid, "isTeleport": False},
                timeout=15,
            )
            nc = r.headers.get("x-csrf-token", "")
            if nc and r.status_code == 403:
                headers["x-csrf-token"] = nc
                r = session.post(
                    "https://gamejoin.roblox.com/v1/join-game",
                    headers=headers,
                    json={"placeId": pid, "isTeleport": False},
                    timeout=15,
                )

            ctype = r.headers.get("rblx-challenge-type", "")
            cid = r.headers.get("rblx-challenge-id", "")
            meta = r.headers.get("rblx-challenge-metadata", "")

            if cid and ctype == "captcha":
                print("CAPTCHA!")
                metadata = None
                try:
                    metadata = json.loads(base64.b64decode(meta))
                except Exception:
                    pass
                return {
                    "type": ctype,
                    "id": cid,
                    "metadata_b64": meta,
                    "metadata": metadata,
                    "place": name,
                }
            print("ok")
        except Exception as e:
            print(f"err: {e}")
    return None

def verify_captcha_solved(cookie):
    """Kiểm tra captcha đã được giải xong chưa bằng cách gọi lại join-game."""
    session = requests.Session()
    session.verify = False
    session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

    csrf = get_csrf(session)
    if not csrf:
        return False

    headers = {**HEADERS_BASE, "x-csrf-token": csrf}
    for place in POPULAR_PLACES:
        pid = place["placeId"]
        try:
            r = session.post(
                "https://gamejoin.roblox.com/v1/join-game",
                headers=headers,
                json={"placeId": pid, "isTeleport": False},
                timeout=15,
            )
            nc = r.headers.get("x-csrf-token", "")
            if nc and r.status_code == 403:
                headers["x-csrf-token"] = nc
                r = session.post(
                    "https://gamejoin.roblox.com/v1/join-game",
                    headers=headers,
                    json={"placeId": pid, "isTeleport": False},
                    timeout=15,
                )

            ctype = r.headers.get("rblx-challenge-type", "")
            cid = r.headers.get("rblx-challenge-id", "")

            if cid and ctype == "captcha":
                return False
            return True
        except Exception:
            return False
    return False

# ============================================================
#  ROBLOX APP & WEBVIEW2 CONTROLLER
# ============================================================
def resolve_roblox_player_beta_path():
    """Tìm đường dẫn RobloxPlayerBeta.exe trên Windows."""
    def _parse_exe(command_value):
        raw = os.path.expandvars((command_value or "").strip())
        m = re.match(r'^"([^"]+?RobloxPlayerBeta\.exe)"', raw, re.IGNORECASE)
        if m:
            return m.group(1) if os.path.exists(m.group(1)) else None
        m = re.match(r'^([^\s]+RobloxPlayerBeta\.exe)', raw, re.IGNORECASE)
        if m:
            return m.group(1) if os.path.exists(m.group(1)) else None
        return None

    if winreg:
        for hive, key in [
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Classes\roblox-player\shell\open\command"),
            (winreg.HKEY_CLASSES_ROOT, r"roblox-player\shell\open\command"),
        ]:
            try:
                with winreg.OpenKey(hive, key) as k:
                    value, _ = winreg.QueryValueEx(k, None)
                    path = _parse_exe(value)
                    if path:
                        return path
            except Exception:
                pass

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        base = Path(local_appdata) / "Roblox" / "Versions"
        if base.exists():
            candidates = sorted(
                base.glob("version-*/RobloxPlayerBeta.exe"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for cand in candidates:
                if cand.exists():
                    return str(cand)
    return None

def get_auth_ticket_api(cookie, max_retries=3):
    headers = {
        "Cookie": f".ROBLOSECURITY={cookie}",
        "User-Agent": "Roblox/WinInet",
        "Content-Type": "application/json",
        "Origin": "https://www.roblox.com",
        "Referer": "https://www.roblox.com/games",
    }
    token = None
    for _ in range(max_retries):
        try:
            r = requests.post("https://auth.roblox.com/v1/authentication-ticket", headers=headers, data="{}", timeout=15)
            token = r.headers.get("x-csrf-token")
            if token:
                break
        except Exception:
            pass
        time.sleep(2)
    if not token:
        return None

    headers["x-csrf-token"] = token
    for _ in range(max_retries):
        try:
            r2 = requests.post("https://auth.roblox.com/v1/authentication-ticket", headers=headers, data="{}", timeout=15)
            if r2.status_code == 200:
                ticket = r2.headers.get("rbx-authentication-ticket")
                if ticket:
                    return ticket
        except Exception:
            pass
        time.sleep(2)
    return None

def build_join_uri(ticket, place_id):
    launch_time = int(time.time())
    browser_tracker = random.randint(100000, 999999)
    return (
        f"roblox-player:1+launchmode:play"
        f"+gameinfo:{urllib.parse.quote(ticket)}"
        f"+launchtime:{launch_time}"
        f"+placelauncherurl:https://assetgame.roblox.com/game/PlaceLauncher.ashx"
        f"?request=RequestGame&placeId={place_id}"
        f"+browsertrackerid:{browser_tracker}"
        f"+robloxLocale:en_us+gameLocale:en_us"
    )

def get_roblox_pids():
    pids = set()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == "robloxplayerbeta.exe":
                pids.add(proc.info["pid"])
        except Exception:
            pass
    return pids

def find_new_roblox_pid(old_pids, timeout=25):
    start = time.time()
    while time.time() - start < timeout:
        new_pids = get_roblox_pids() - old_pids
        if new_pids:
            candidate = max(new_pids)
            time.sleep(2)
            if psutil.pid_exists(candidate):
                return candidate
            old_pids = old_pids | {candidate}
        time.sleep(0.3)
    return None

def _all_roblox_related_pids():
    roblox_pids = set()
    webview_pids = set()
    for proc in psutil.process_iter(["pid", "name", "ppid"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name in ("robloxplayerbeta.exe", "robloxcrashhandler.exe"):
                roblox_pids.add(proc.info["pid"])
            elif name == "msedgewebview2.exe":
                webview_pids.add((proc.info["pid"], proc.info.get("ppid")))
        except Exception:
            pass
    result = set(roblox_pids)
    changed = True
    while changed:
        changed = False
        for wp, ppid in webview_pids:
            if wp not in result and ppid in result:
                result.add(wp)
                changed = True
    return result

def kill_roblox_pid(pid, before_pids=None):
    to_kill = set()
    if pid:
        try:
            if psutil.pid_exists(pid):
                proc = psutil.Process(pid)
                for child in proc.children(recursive=True):
                    to_kill.add(child.pid)
                to_kill.add(pid)
        except Exception:
            pass
    try:
        current = _all_roblox_related_pids()
        if before_pids is not None:
            to_kill |= (current - set(before_pids))
        elif pid:
            to_kill |= {p for p in current if p == pid}
    except Exception:
        pass

    procs_killed = []
    for p in to_kill:
        try:
            pr = psutil.Process(p)
            pr.kill()
            procs_killed.append(pr)
        except Exception:
            pass
    try:
        psutil.wait_procs(procs_killed, timeout=5)
    except Exception:
        pass

def _port_is_free(port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0
    except Exception:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass

_debug_port_lock = threading.Lock()
_debug_port_cursor = [9300 + random.randint(0, 200)]

def next_debug_port():
    with _debug_port_lock:
        for _ in range(500):
            port = _debug_port_cursor[0]
            _debug_port_cursor[0] += 1
            if _port_is_free(port):
                return port
        return _debug_port_cursor[0]

def _clean_webview_locks(profile_dir):
    """Xóa các file lock của WebView2 (giữ nguyên session/cookie theo chuẩn helpsolve.py)."""
    profile_dir = Path(profile_dir)
    lock_names = ["lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"]
    roots = [profile_dir, profile_dir / "EBWebView"]
    for root in roots:
        for name in lock_names:
            target = root / name
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists() or target.is_symlink():
                    target.unlink()
            except Exception:
                pass

def enable_webview2_extensions_registry():
    """Bật hỗ trợ Extension cho WebView2 trong Windows Registry."""
    if not winreg:
        return
    for sub in [
        r"Software\Policies\Microsoft\Edge\WebView2",
        r"Software\Microsoft\Edge\WebView2",
    ]:
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub) as key:
                winreg.SetValueEx(key, "AreBrowserExtensionsEnabled", 0, winreg.REG_DWORD, 1)
        except Exception:
            pass

def launch_roblox_and_connect(cookie, place_id, debug_port, timeout=25):
    """Khởi động Roblox App với extension OMOcaptcha nạp vào WebView2."""
    before_related = _all_roblox_related_pids()
    exe_path = resolve_roblox_player_beta_path()
    if not exe_path:
        print("  [!] Không tìm thấy RobloxPlayerBeta.exe!")
        return None, None, None, before_related

    ticket = get_auth_ticket_api(cookie)
    if not ticket:
        print("  [!] Không lấy được auth ticket!")
        return None, None, None, before_related

    join_uri = build_join_uri(ticket, place_id)
    before_pids = get_roblox_pids()

    ext_dir = get_omocaptcha_dir()
    ext_path = str(ext_dir.resolve())

    # Kích hoạt hỗ trợ extension cho WebView2
    enable_webview2_extensions_registry()

    env = os.environ.copy()
    env["WEBVIEW2_ARE_BROWSER_EXTENSIONS_ENABLED"] = "1"
    
    # Nạp extension OMOcaptcha vào WebView2
    wv_args = (
        f"--remote-debugging-port={debug_port} "
        f"--remote-allow-origins=* "
        f"--disable-blink-features=AutomationControlled "
        f'--load-extension="{ext_path}" '
        f'--disable-extensions-except="{ext_path}"'
    )
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = wv_args

    try:
        WEBVIEW_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _clean_webview_locks(WEBVIEW_PROFILE_DIR)
        reset_stale_omocaptcha_storage(WEBVIEW_PROFILE_DIR)
        env["WEBVIEW2_USER_DATA_FOLDER"] = str(WEBVIEW_PROFILE_DIR)
    except Exception:
        pass

    print(f"  [*] Khởi động Roblox app + Extension OMOcaptcha (debug port {debug_port})...")
    subprocess.Popen([exe_path, join_uri], env=env)

    pid = find_new_roblox_pid(before_pids, timeout=timeout)
    if not pid:
        print("  [!] Không thấy Roblox process mới mở!")
        return None, None, None, before_related

    print(f"  [*] Roblox PID={pid}. Chờ web2view captcha xuất hiện...")

    # Chờ WebView2 load trang challenge
    browser = None
    captcha_tab = None
    wait_start = time.time()
    while time.time() - wait_start < 90:
        if not psutil.pid_exists(pid):
            print("  [!] Roblox process đã thoát trước khi captcha kịp hiện.")
            return pid, None, None, before_related
        try:
            r = requests.get(f"http://127.0.0.1:{debug_port}/json", timeout=5)
            targets = r.json()
            if any("roblox.com/challenge" in (t.get("url") or "") for t in targets):
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        print("  [!] Hết 90s mà web2view chưa load trang captcha.")
        return pid, None, None, before_related

    # Kết nối DrissionPage để giữ phiên mà không can thiệp hay click trang
    for _ in range(10):
        try:
            browser = Chromium(addr_or_opts=f"127.0.0.1:{debug_port}")
            for tid in browser.tab_ids:
                tab = browser.get_tab(tid)
                if "roblox.com/challenge" in (tab.url or ""):
                    captcha_tab = tab
                    break
            if captcha_tab:
                break
        except Exception:
            pass
        time.sleep(1)

    if not captcha_tab:
        print("  [!] Không tìm thấy tab captcha trong web2view.")
        return pid, browser, None, before_related

    print("  [+] Đã kết nối vào web2view chứa captcha!")
    return pid, browser, captcha_tab, before_related

# ============================================================
#  SOLVE ACCOUNT
# ============================================================
def solve_account(result, index, total):
    """Mở Roblox App với Extension OMOcaptcha, chờ extension tự giải xong (không giới hạn 180s)."""
    user = result["username"]
    cookie = result.get("cookie", "")
    place_id = POPULAR_PLACES[0]["placeId"]

    dp = next_debug_port()
    pid, browser, captcha_tab, before_related = launch_roblox_and_connect(cookie, place_id, dp)

    if not pid:
        kill_roblox_pid(pid, before_related)
        return False

    solved = False
    try:
        print(f"  [*] Roblox app đã mở với Extension OMOcaptcha. Đang chờ giải captcha cho {user}...")

        # Vòng lặp chờ vô hạn cho tới khi giải xong hoặc Roblox đóng
        while True:
            time.sleep(5)

            # Nếu Roblox process tự tắt nghĩa là đã qua captcha hoặc người dùng tắt
            if not psutil.pid_exists(pid):
                solved = verify_captcha_solved(cookie)
                break

            if verify_captcha_solved(cookie):
                print(f"  [+] THÀNH CÔNG! OMOcaptcha đã giải xong cho {user}!")
                solved = True
                break

    except Exception as e:
        print(f"  [!] Lỗi trong quá trình giải: {e}")
        solved = verify_captcha_solved(cookie)
    finally:
        try:
            if browser:
                browser.quit()
        except Exception:
            pass
        kill_roblox_pid(pid, before_related)

    status = "SOLVED" if solved else "FAILED"
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{user}|{result.get('uid', 0)}|{status}\n")

    return solved

# ============================================================
#  CORE ENGINE
# ============================================================
def parse_acc_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    return {"cookie": line}

def ensure_acc_file():
    if not ACC_FILE.exists():
        ACC_FILE.write_text(
            "# Dán Cookie vào đây (Mỗi dòng 1 Cookie):\n"
            "_|WARNING:-DO-NOT-SHARE-...\n",
            encoding="utf-8",
        )
        print(f"[!] Đã tạo file {ACC_FILE}. Vui lòng điền Cookie vào!")
        return False
    return True

def load_accounts():
    lines = ACC_FILE.read_text(encoding="utf-8").splitlines()
    accounts = []
    for line in lines:
        acc = parse_acc_line(line)
        if acc:
            accounts.append(acc)
    return accounts

def check_account(acc, index, total):
    cookie = acc["cookie"]
    short_cookie = cookie[:30] + "..." if len(cookie) > 30 else cookie

    print(f"\n[{index}/{total}] Kiểm tra Cookie | {short_cookie}")
    session = requests.Session()
    session.verify = False
    session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

    csrf = get_csrf(session)
    if not csrf:
        return {"status": "error", "reason": "csrf_failed"}

    username, uid = get_user_info(session)
    if uid == 0:
        return {"status": "error", "reason": "invalid_cookie"}

    print(f"  [+] Tài khoản: {username} (ID: {uid})")
    info = trigger_captcha(session, csrf)
    if not info:
        print(f"  [OK] {username} Không bị captcha")
        return {"status": "clean", "username": username, "uid": uid}

    print(f"  [!] {username} BỊ CAPTCHA trên {info['place']}")
    return {
        "status": "captcha",
        "username": username,
        "uid": uid,
        "captcha_info": info,
        "cookie": cookie,
    }

def main():
    print("=" * 60)
    print("  Roblox App Captcha Solver - OMOcaptcha Auto")
    print("=" * 60)

    if not resolve_roblox_player_beta_path():
        print("[!] Không tìm thấy RobloxPlayerBeta.exe trên máy!")
        return

    ext_dir = get_omocaptcha_dir()
    manifest_file = ext_dir / "manifest.json"
    if not manifest_file.exists():
        print(f"[!] KHÔNG tìm thấy Extension OMOcaptcha tại: {ext_dir}")
        return

    write_omocaptcha_source_key(None)
    ok, msg = check_omocaptcha_key()
    print(f"[*] OMOcaptcha Status: {msg}")
    if not ok:
        print("[!] Key OMOcaptcha không hợp lệ, vui lòng kiểm tra lại configs.json.")
        return

    if not ensure_acc_file():
        return

    accounts = load_accounts()
    if not accounts:
        print(f"[!] Không tìm thấy tài khoản hợp lệ trong {ACC_FILE}!")
        return

    counters = {"clean": 0, "error": 0, "captcha": 0, "solved": 0, "failed": 0}
    count_lock = threading.Lock()
    solve_lock = threading.Lock()

    def check_and_solve(acc, index, total):
        result = check_account(acc, index, total)
        if result["status"] == "clean":
            with count_lock:
                counters["clean"] += 1
        elif result["status"] == "error":
            with count_lock:
                counters["error"] += 1
        elif result["status"] == "captcha":
            with count_lock:
                counters["captcha"] += 1

            # Khóa tuần tự khi giải captcha trên Roblox App
            with solve_lock:
                ok = solve_account(result, index, total)
                with count_lock:
                    if ok:
                        counters["solved"] += 1
                    else:
                        counters["failed"] += 1
                print("  [*] Chờ 15s trước khi chuyển sang tài khoản kế tiếp...")
                time.sleep(15)

    max_workers = min(10, len(accounts)) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(check_and_solve, acc, i, len(accounts)) for i, acc in enumerate(accounts, 1)]
        for fut in as_completed(futures):
            pass

    print(f"\n{'=' * 40}")
    print(f"  KẾT QUẢ CUỐI CÙNG:")
    print(f"    Clean  : {counters['clean']}")
    print(f"    Solved : {counters['solved']}")
    print(f"    Failed : {counters['failed']}")
    print(f"    Error  : {counters['error']}")
    print(f"{'=' * 40}")

if __name__ == "__main__":
    main()
