import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")

import json
import os
import re
import time
import base64
import shutil
import subprocess
import threading
import queue
import requests
import urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Selenium (giong tool rejoin roblox.py) - nap extension runtime bang webextension.install
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException

requests.packages.urllib3.disable_warnings()

# ============================================================
#  PATHS (Tự động theo vị trí file .py)
# ============================================================
ROOT_DIR = Path(__file__).parent.resolve()
PROFILES_DIR = ROOT_DIR / "chrome_profiles"
ACC_FILE = ROOT_DIR / "accounts.txt" # Đã sửa thành accounts.txt
CONFIG_FILE = ROOT_DIR / "config.json"
RESULTS_FILE = ROOT_DIR / "captcha_results.txt"

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
                return {"type": ctype, "id": cid, "metadata_b64": meta,
                        "metadata": metadata, "place": name}
            print("ok")
        except Exception as e:
            print(f"err: {e}")
    return None

def verify_captcha_solved(cookie):
    """Kiem tra captcha da duoc giai chua bang cach goi lai join-game."""
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
                return False  # Van con captcha
            return True  # Khong con captcha hoac da join dc
        except Exception:
            return False
    return False

def build_challenge_url(info):
    params = {
        "generic-challenge-type": "captcha",
        "app-type": "windows",
        "generic-challenge-id": info["id"],
        "challenge-metadata-json": info["metadata_b64"],
        "challenge-type": "generic",
        "dark-mode": "false",
    }
    return "https://www.roblox.com/challenge/cdn/hybrid?" + urllib.parse.urlencode(params)

def find_browser(config_browser_path=""):
    """Lay duong dan browser."""
    candidates = []
    if config_browser_path:
        candidates.append(config_browser_path)

    env_browser = os.environ.get("BROWSER_PATH", "").strip()
    if env_browser:
        candidates.append(env_browser)

    candidates.extend([
        r"%LocalAppData%\Google\Chrome for Testing\Application\chrome.exe",
        r"%ProgramFiles%\Google\Chrome for Testing\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome for Testing\Application\chrome.exe",
        r"%LocalAppData%\Chromium\Application\chrome.exe",
        r"%ProgramFiles%\Chromium\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Chromium\Application\chrome.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ])

    for p in candidates:
        p = os.path.expandvars(str(p).strip().strip('"'))
        if p and os.path.exists(p):
            return p
    return ""

def sanitize_profile_name(value):
    value = re.sub(r"[^\w.-]+", "_", str(value or "").strip())
    value = value.strip("._")
    return value or "unknown"

def account_profile_dir(result):
    user = sanitize_profile_name(result.get("username", "unknown"))
    uid = sanitize_profile_name(result.get("uid", "0"))
    return PROFILES_DIR / f"{user}_{uid}"

def prepare_profile(profile_dir):
    """Xoa session cu + setup prefs."""
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    targets = [
        default_dir / "Sessions",
        default_dir / "Last Session",
        default_dir / "Last Tabs",
        profile_dir / "Last Session",
        profile_dir / "Last Tabs",
    ]
    for target in targets:
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        except Exception:
            pass

    # Doi api_key trong configs.json -> phai xoa cho extension luu key CU,
    # neu khong profile cu van dung key cu (API key does not exist).
    reset_stale_omocaptcha_storage(profile_dir)


def write_omocaptcha_source_key(api_key):
    """Ghi api_key vao omocaptcha/configs.json + bat power_on (giong tool rejoin)."""
    config_path = ROOT_DIR / "omocaptcha" / "configs.json"
    if not config_path.exists():
        return
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if api_key:
            data["api_key"] = str(api_key).strip()
        data["power_on"] = True
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        print(f"  [!] Loi ghi configs.json: {e}")


OMO_CONFIG = ROOT_DIR / "omocaptcha" / "configs.json"


def read_omocaptcha_key():
    """Doc api_key dang dung trong omocaptcha/configs.json."""
    try:
        data = json.loads(OMO_CONFIG.read_text(encoding="utf-8"))
        return str(data.get("api_key", "")).strip()
    except (OSError, ValueError):
        return ""


def reset_stale_omocaptcha_storage(profile_dir):
    """Xoa chrome.storage cua extension omo trong profile NEU key luu trong do
    KHAC key hien tai o configs.json.

    Extension chi doc configs.json DUNG 1 LAN cho moi profile (no set flag
    'initialized' vao chrome.storage.local roi khong doc file nua). Nen doi
    api_key trong configs.json ma khong xoa cho luu nay -> profile cu VAN GUI
    KEY CU len server -> loi 'API key does not exist' / ERROR_KEY_DOES_NOT_EXIST.
    """
    key = read_omocaptcha_key()
    if not key:
        return
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
            continue          # chua co key nao / da dung key moi -> de nguyen
        shutil.rmtree(ext_dir, ignore_errors=True)
        print(f"  [*] Xoa key OMO cu trong profile {Path(profile_dir).name} "
              f"-> extension se nap lai key moi tu configs.json")


def check_omocaptcha_key():
    """Hoi server omo: key trong configs.json con song + con tien khong?

    Tra (ok, message). Loi mang -> coi nhu ok (khong chan tool chay).
    """
    key = read_omocaptcha_key()
    if not key:
        return False, "configs.json khong co api_key"
    try:
        r = requests.post("https://api.omocaptcha.com/v2/getBalance",
                          json={"clientKey": key}, timeout=20, verify=False)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        return True, f"khong check duoc key ({e}) - cu chay thu"
    if data.get("errorId"):
        return False, f"{data.get('errorCode')} - {data.get('errorDescription')}"
    return True, f"key OK, balance {data.get('balance')}"


def create_browser(browser_path, profile_dir, window_pos=None):
    """Tao SELENIUM Chrome (giong tool rejoin) - extension nap RUNTIME sau khi mo."""
    profile_path = str(Path(profile_dir).resolve())

    options = Options()
    if browser_path:
        options.binary_location = str(browser_path)
    options.page_load_strategy = "none"
    options.enable_bidi = True                    # bat BiDi
    options.enable_webextensions = True           # cho phep webextension.install
    options.add_argument(f"--user-data-dir={profile_path}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-session-crashed-bubble")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches",
                                    ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--window-size=450,600")
    if window_pos:
        options.add_argument(f"--window-position={window_pos[0]},{window_pos[1]}")

    driver = webdriver.Chrome(
        options=options, service=Service(log_output=subprocess.DEVNULL))
    driver.set_page_load_timeout(60)
    return driver


# ============================================================
#  NAP EXTENSION OMOcaptcha (tu giai captcha, khong API)
# ============================================================
def wait_challenge_ready(driver, timeout=45):
    """Cho trang captcha render ON DINH roi moi nap extension (giong tool rejoin).

    Nap extension khi trang chua on dinh -> trang reload lien tuc. Cho toi khi
    URL la /challenge + co nut Start / iframe captcha thi moi coi la san sang.
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            url = driver.current_url or ""
        except Exception:
            url = ""
        try:
            txt = driver.execute_script(
                "return document.body ? document.body.innerText : ''") or ""
        except Exception:
            txt = ""
        has_url = "challenge" in url.lower()
        has_start = any(m in txt for m in ("Bắt đầu", "Câu đố", "Start", "Verify",
                                           "Bảo vệ", "Protect", "not a bot",
                                           "con người"))
        has_widget = False
        try:
            has_widget = bool(driver.execute_script("""
                return !!document.querySelector(
                  'iframe[src*="arkose"],iframe[src*="funcaptcha"],'
                  +'iframe[src*="challenge"]');
            """))
        except Exception:
            pass
        if has_url and (has_start or has_widget):
            print("  [*] Trang captcha da san sang.")
            return True
        time.sleep(0.5)
    print(f"  [!] Trang captcha chua ro sau {timeout}s.")
    return False


def install_omocaptcha_extension(driver):
    """NAP extension omocaptcha RUNTIME (sau khi captcha da render).

    Cach cua tool rejoin: driver.webextension.install(path=...) -> nap dung luc
    dinh captcha. Extension se tu dong giai FunCaptcha.
    """
    ext_path = str((ROOT_DIR / "omocaptcha").resolve())
    if not (ROOT_DIR / "omocaptcha" / "manifest.json").exists():
        print(f"  [!] KHONG thay omocaptcha/manifest.json tai: {ext_path}")
        return False
    try:
        driver.webextension.install(path=ext_path)
        print(f"  [*] Da nap extension OMOcaptcha (runtime): {ext_path}")
        return True
    except Exception as e:
        print(f"  [!] Loi nap extension: {e}")
        return False


# (Da BO giai API - chi dung extension OMOcaptcha tu giai)


# (Da BO login_and_open_captcha - dung DrissionPage cu, khong dung nua)

# (Da BO create_spy_extension_auto, WebhookHandler, webhook server, overlay -
#  chi dung extension OMOcaptcha tu giai, khong tao gi rieng, khong tiem gi vao trang)

# ============================================================
#  CONFIG & ACC FILE
# ============================================================


def load_config():
    if not CONFIG_FILE.exists():
        default = {"threads": 1, "solveBrowsers": 3, "browserPath": ""}
        CONFIG_FILE.write_text(json.dumps(default, indent=4, ensure_ascii=False), encoding="utf-8")
        return default

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    cfg.setdefault("threads", 1)
    cfg.setdefault("solveBrowsers", 3)
    cfg.setdefault("browserPath", "")
    return cfg

def parse_acc_line(line):
    """Chi doc moi Cookie, khong can user hay pass"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    return {
        "cookie": line, # Lấy cả dòng làm cookie
    }

def ensure_acc_file():
    if not ACC_FILE.exists():
        ACC_FILE.write_text(
            "# Vui long dan Cookie vao day (Moi dong 1 Cookie):\n"
            "_|WARNING:-DO-NOT-SHARE-...\n",
            encoding="utf-8",
        )
        print(f"[!] Da tao file {ACC_FILE}. Vui long dien Cookie vao!")
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

# ============================================================
#  CHECK & SOLVE
# ============================================================

def check_account(acc, index, total):
    cookie = acc["cookie"]
    short_cookie = cookie[:30] + "..." if len(cookie) > 30 else cookie

    print(f"\n[{index}/{total}] Dang check Cookie | {short_cookie}")
    
    session = requests.Session()
    session.verify = False
    session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

    csrf = get_csrf(session)
    if not csrf:
        return {"status": "error", "reason": "csrf_failed"}

    # TỰ ĐỘNG LẤY TÊN NICK VÀ ID TỪ COOKIE ĐÂY NÀY
    username, uid = get_user_info(session)
    if uid == 0:
        return {"status": "error", "reason": "invalid_cookie"}
        
    print(f"  [+] Đã nhận diện acc: {username} (ID: {uid})")

    info = trigger_captcha(session, csrf)
    # ... (Phần bên dưới của hàm này giữ nguyên y như cũ) ...
    if not info:
        print(f"  [OK] {username} Khong bi captcha")
        return {"status": "clean", "username": username, "uid": uid}

    print(f"  [!] {username} dính CAPTCHA tren {info['place']}")
    return {
        "status": "captcha",
        "username": username,
        "uid": uid,
        "captcha_info": info,
        "cookie": cookie,
    }

def solve_account(result, index, total, browser_path, window_pos=None):
    info = result["captcha_info"]
    user = result["username"]
    cookie = result.get("cookie", "")
    profile_dir = account_profile_dir(result)

    url = build_challenge_url(info)
    prepare_profile(profile_dir)

    browser = None
    solved = False
    try:
        browser = create_browser(browser_path, profile_dir, window_pos)

        # 1. Set cookie qua CDP (giong tool rejoin)
        print("  [*] Dang set cookie...")
        try:
            browser.execute_cdp_cmd("Network.enable", {})
            browser.execute_cdp_cmd("Network.setCookie", {
                "url": "https://www.roblox.com/",
                "name": ".ROBLOSECURITY",
                "value": cookie,
                "domain": ".roblox.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            })
        except Exception as e:
            print(f"  [!] Loi set cookie: {e}")
            return False

        # 2. Mo trang captcha
        print("  [*] Dang mo TRUC TIEP trang Captcha...")
        try:
            browser.execute_cdp_cmd("Page.enable", {})
            browser.execute_cdp_cmd("Page.navigate", {"url": url})
        except Exception:
            try:
                browser.get(url)
            except TimeoutException:
                pass

        # 3. CHO trang captcha render ON DINH (tranh nap extension qua som -> reload)
        wait_challenge_ready(browser, timeout=45)
        time.sleep(2)   # them 2s cho on dinh han

        # 4. NAP EXTENSION OMOcaptcha RUNTIME -> extension TU GIAI (khong API)
        install_omocaptcha_extension(browser)
        print(f"  [*] OMOcaptcha (extension) dang tu giai cho {user}...")

        # 5. Cho extension giai xong (goi lai join-game de kiem tra)
        max_wait = 180
        start_time = time.time()
        while time.time() - start_time < max_wait:
            time.sleep(5)
            try:
                _ = browser.current_window_handle
            except Exception:
                solved = verify_captcha_solved(cookie)
                break
            if verify_captcha_solved(cookie):
                print(f"  [+] XONG! Da giai captcha xong cho {user}!")
                solved = True
                break

        if not solved:
            print(f"  [!] Het {max_wait}s ma chua giai xong. Bo qua {user}.")

    except Exception as e:
        print(f"  [!] Loi khi giai: {e}")
        solved = verify_captcha_solved(cookie)
    finally:
        if browser:
            try:
                browser.quit()
            except Exception:
                pass

    status = "SOLVED" if solved else "FAILED"
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{user}|{result['uid']}|{info['id']}|{status}|{url}\n")
    return solved

def main():
    print("=" * 60)
    print("  Roblox Captcha Solver - OMOcaptcha Auto")
    print("=" * 60)
    # OMOcaptcha tu giai - nap runtime khi dinh captcha (giong tool rejoin)
    if not (ROOT_DIR / "omocaptcha" / "manifest.json").exists():
        print("[!] KHONG thay thu muc 'omocaptcha' co manifest.json!")
        return
    write_omocaptcha_source_key(None)   # bat power_on (key da co san trong configs.json)
    ok, msg = check_omocaptcha_key()
    print(f"[*] OMOcaptcha: {msg}")
    if not ok:
        print("[!] Key omo khong dung duoc -> sua 'api_key' trong "
              "omocaptcha/configs.json roi chay lai.")
        return
    print("[+] Se nap extension OMOcaptcha de tu dong giai captcha!")

    cfg = load_config()
    solve_browsers = max(1, cfg.get("solveBrowsers", 3))
    browser_path = find_browser(str(cfg.get("browserPath", "")).strip())
    
    if not browser_path:
        print("[!] Khong tim thay duong dan Chrome/Chromium!")
        return

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    if not ensure_acc_file():
        return

    accounts = load_accounts()
    if not accounts:
        print(f"[!] Khong tim thay account hop le nao trong {ACC_FILE}!")
        return

    counters = {"clean": 0, "error": 0, "captcha": 0, "solved": 0, "failed": 0}
    count_lock = threading.Lock()

    WINDOW_W = 460
    slot_queue = queue.Queue()
    for s in range(solve_browsers):
        slot_queue.put((s * WINDOW_W, 0))

    def check_and_solve(acc, index, total):
        result = check_account(acc, index, total)
        if result["status"] == "clean":
            with count_lock: counters["clean"] += 1
        elif result["status"] == "error":
            with count_lock: counters["error"] += 1
        elif result["status"] == "captcha":
            with count_lock: counters["captcha"] += 1
            
            slot_pos = slot_queue.get()
            try:
                ok = solve_account(result, index, total, browser_path, window_pos=slot_pos)
                with count_lock:
                    if ok: counters["solved"] += 1
                    else: counters["failed"] += 1
            finally:
                slot_queue.put(slot_pos) 

    max_workers = solve_browsers + 5
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(check_and_solve, acc, i, len(accounts)) for i, acc in enumerate(accounts, 1)]
        for fut in as_completed(futures):
            pass

    print(f"\n{'=' * 40}\n  KET QUA CUOI CUNG:\n    Clean: {counters['clean']} | Solved: {counters['solved']} | Failed: {counters['failed']} | Error: {counters['error']}\n{'=' * 40}")

if __name__ == "__main__":
    main()