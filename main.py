import ctypes
import json
import random
import re
import subprocess
import sys
import threading
import time
import winreg
from datetime import date
from pathlib import Path

# Khóa để nhiều thread ghi file an toàn
_file_lock = threading.Lock()

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

# Thư mục lưu profile (fingerprint json + user-data Chrome)
BASE_DIR = Path(__file__).parent
PROFILES_DIR = BASE_DIR / "profiles"
PROFILES_DIR.mkdir(exist_ok=True)

# Chrome PORTABLE riêng của tool (không đụng Chrome hệ thống bạn đang lướt web)
PORTABLE_CHROME_DIR = BASE_DIR / "chrome"
PORTABLE_CHROME_EXE = PORTABLE_CHROME_DIR / "chrome.exe"

# ---- Lưới cửa sổ: 5 tab, hàng trên 3 - hàng dưới 2 -------------------------
# Màn 1536 logic / 3 cột = 512px mỗi cột > 524? gần bằng -> xếp khít, đẹp.
GRID_COLS = 3
GRID_ROWS = 2
NUM_TABS = 5


def get_screen_size() -> tuple[int, int]:
    """Kích thước màn hình LOGIC (vd 1536x864 ở DPI 125%).

    Chrome đặt cửa sổ (--window-position/size, CDP bounds) theo tọa độ LOGIC này,
    nên KHÔNG gọi SetProcessDPIAware.
    """
    try:
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return 1536, 864


def grid_layout() -> tuple[int, int, int, int, int]:
    """Lưới GRID_COLS x GRID_ROWS. Ô rộng = màn // cột (>=512px nên không đè)."""
    sw, sh = get_screen_size()
    sh -= 48                            # chừa taskbar (logic)
    cols, rows = GRID_COLS, GRID_ROWS
    cell_w = sw // cols
    cell_h = sh // rows
    return cols, rows, cell_w, cell_h, sh


def grid_rect(slot: int) -> tuple[int, int, int, int]:
    """Trả (x, y, w, h) cho ô 'slot'. Các ô sát nhau, xếp khít."""
    cols, rows, w, h, _ = grid_layout()
    col = slot % cols
    row = (slot // cols) % rows
    x = col * w
    y = row * h
    return x, y, w, h

# ---- Kho giá trị để random fingerprint ------------------------------------
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]
WINDOW_SIZES = [(1920, 1080), (1600, 900), (1536, 864), (1440, 900), (1366, 768)]
LANGS = ["en-US", "en-GB"]
TIMEZONES = ["America/New_York", "Europe/London", "Asia/Ho_Chi_Minh", "Asia/Singapore"]
# Toạ độ geo khớp timezone (để geolocation không lệch timezone -> không lộ VPN)
TZ_GEO = {
    "America/New_York": (40.7128, -74.0060),
    "Europe/London": (51.5074, -0.1278),
    "Asia/Ho_Chi_Minh": (10.7769, 106.7009),
    "Asia/Singapore": (1.3521, 103.8198),
}
CPU_CORES = [4, 6, 8, 12, 16]
RAM_GB = [4, 8, 16, 32]
# Cặp WebGL vendor/renderer thật của card đồ họa phổ biến
WEBGL_PAIRS = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]


# ==========================================================================
# FINGERPRINT: sinh mới hoặc nạp lại từ file (cố định theo profile)
# ==========================================================================
def load_or_create_fingerprint(name: str) -> dict:
    """Nạp fingerprint của profile; nếu chưa có thì sinh mới rồi lưu lại."""
    fp_path = PROFILES_DIR / f"{name}.json"
    if fp_path.exists():
        fp = json.loads(fp_path.read_text(encoding="utf-8"))
        print(f"[*] Nạp fingerprint đã lưu cho profile '{name}'.")
        return fp

    width, height = random.choice(WINDOW_SIZES)
    vendor, renderer = random.choice(WEBGL_PAIRS)
    fp = {
        "user_agent": random.choice(UA_LIST),
        "width": width,
        "height": height,
        "language": random.choice(LANGS),
        "timezone": random.choice(TIMEZONES),
        "cpu_cores": random.choice(CPU_CORES),
        "ram_gb": random.choice(RAM_GB),
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        # "hạt giống" để nhiễu Canvas/WebGL ổn định theo profile
        "canvas_noise": random.randint(1, 1_000_000),
        "proxy": None,  # sẽ gán sau khi tìm được proxy sống
    }
    fp_path.write_text(json.dumps(fp, indent=2), encoding="utf-8")
    print(f"[*] Sinh fingerprint MỚI cho profile '{name}' -> {fp_path.name}")
    return fp


def save_fingerprint(name: str, fp: dict) -> None:
    (PROFILES_DIR / f"{name}.json").write_text(
        json.dumps(fp, indent=2), encoding="utf-8"
    )


def fresh_fingerprint() -> dict:
    """Sinh fingerprint MỚI HOÀN TOÀN (không lưu file) cho mỗi lần chạy.

    Mỗi giá trị random -> mỗi browser là một thiết bị khác, tinh khôi.
    """
    width, height = random.choice(WINDOW_SIZES)
    vendor, renderer = random.choice(WEBGL_PAIRS)
    return {
        "user_agent": random.choice(UA_LIST),
        "width": width,
        "height": height,
        "language": random.choice(LANGS),
        "timezone": random.choice(TIMEZONES),
        "cpu_cores": random.choice(CPU_CORES),
        "ram_gb": random.choice(RAM_GB),
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        "canvas_noise": random.randint(1, 1_000_000),
        "proxy": None,
    }


def wipe_profile(name: str) -> None:
    """Xóa SẠCH mọi dấu vết profile: user-data Chrome + file fingerprint."""
    import shutil
    for target in (PROFILES_DIR / f"{name}_chrome", PROFILES_DIR / f"{name}.json"):
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        except Exception:
            pass


# ==========================================================================
# CHROME
# ==========================================================================
def get_chrome_major_version() -> int | None:
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"Software\Google\Chrome\BLBeacon")
            version, _ = winreg.QueryValueEx(key, "version")
            winreg.CloseKey(key)
            return int(version.split(".")[0])
        except Exception:
            pass
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        try:
            out = subprocess.check_output([path, "--version"],
                                          stderr=subprocess.DEVNULL, timeout=10)
            m = re.search(r"(\d+)\.", out.decode(errors="ignore"))
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return None


def find_system_chrome() -> Path | None:
    """Tìm thư mục Application của Chrome hệ thống (để copy làm bản portable)."""
    import os
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application",
    ]
    for d in candidates:
        if (d / "chrome.exe").exists():
            return d
    return None


def ensure_portable_chrome() -> Path | None:
    """Đảm bảo có chrome.exe PORTABLE riêng trong d:\\regroblox\\chrome.

    Lần đầu: copy toàn bộ Chrome hệ thống sang thư mục tool. Các lần sau dùng lại.
    Trả về đường dẫn chrome.exe portable, hoặc None nếu không tạo được.
    """
    if PORTABLE_CHROME_EXE.exists():
        return PORTABLE_CHROME_EXE

    src = find_system_chrome()
    if src is None:
        print("[!] Không tìm thấy Chrome hệ thống để tạo bản portable.")
        return None

    import shutil
    print(f"[*] Lần đầu: tạo Chrome portable riêng cho tool từ {src} ...")
    print("    (chỉ chạy 1 lần, hơi lâu vì copy Chrome)")
    try:
        PORTABLE_CHROME_DIR.mkdir(parents=True, exist_ok=True)
        # Chrome hay để file trong thư mục con <version>/ -> copy cả cây
        shutil.copytree(src, PORTABLE_CHROME_DIR, dirs_exist_ok=True)
        if PORTABLE_CHROME_EXE.exists():
            print(f"[*] Đã tạo Chrome portable: {PORTABLE_CHROME_EXE}")
            return PORTABLE_CHROME_EXE
    except Exception as exc:
        print(f"[!] Lỗi tạo Chrome portable: {exc}")
    return None


def build_stealth_js(fp: dict) -> str:
    """JS tiêm vào MỌI trang trước khi tải: ép fingerprint cố định theo profile."""
    return f"""
    // --- Ẩn dấu hiệu automation ---
    Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
    window.chrome = {{ runtime: {{}} }};

    // --- Phần cứng cố định theo profile ---
    Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {fp['cpu_cores']}}});
    Object.defineProperty(navigator, 'deviceMemory', {{get: () => {fp['ram_gb']}}});
    Object.defineProperty(navigator, 'language', {{get: () => '{fp['language']}'}});
    Object.defineProperty(navigator, 'languages', {{get: () => ['{fp['language']}', 'en']}});
    Object.defineProperty(navigator, 'platform', {{get: () => 'Win32'}});

    // --- WebGL spoof (vendor/renderer cố định) ---
    (function() {{
        const getParam = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {{
            if (p === 37445) return '{fp['webgl_vendor']}';    // UNMASKED_VENDOR
            if (p === 37446) return '{fp['webgl_renderer']}';  // UNMASKED_RENDERER
            return getParam.call(this, p);
        }};
        if (window.WebGL2RenderingContext) {{
            const getParam2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(p) {{
                if (p === 37445) return '{fp['webgl_vendor']}';
                if (p === 37446) return '{fp['webgl_renderer']}';
                return getParam2.call(this, p);
            }};
        }}
    }})();

    // --- Canvas spoof: thêm nhiễu cố định theo profile (seed = canvas_noise) ---
    (function() {{
        let seed = {fp['canvas_noise']};
        function rnd() {{ seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; }}
        const toBlob = HTMLCanvasElement.prototype.toBlob;
        const toDataURL = HTMLCanvasElement.prototype.toDataURL;
        const getImageData = CanvasRenderingContext2D.prototype.getImageData;
        function noisify(canvas) {{
            try {{
                const ctx = canvas.getContext('2d');
                if (!ctx) return;
                const img = getImageData.call(ctx, 0, 0, canvas.width, canvas.height);
                for (let i = 0; i < img.data.length; i += 4) {{
                    img.data[i]   ^= (rnd() * 2) | 0;
                    img.data[i+1] ^= (rnd() * 2) | 0;
                    img.data[i+2] ^= (rnd() * 2) | 0;
                }}
                ctx.putImageData(img, 0, 0);
            }} catch (e) {{}}
        }}
        HTMLCanvasElement.prototype.toDataURL = function() {{
            noisify(this); return toDataURL.apply(this, arguments);
        }};
        HTMLCanvasElement.prototype.toBlob = function() {{
            noisify(this); return toBlob.apply(this, arguments);
        }};
    }})();

    // --- CHỐNG WebRTC LEAK: chặn lộ IP thật qua STUN/ICE ---
    (function() {{
        const block = function() {{ throw new Error('disabled'); }};
        try {{ window.RTCPeerConnection = undefined; }} catch(e) {{}}
        try {{ window.webkitRTCPeerConnection = undefined; }} catch(e) {{}}
        try {{ window.RTCDataChannel = undefined; }} catch(e) {{}}
        try {{
            navigator.mediaDevices && (navigator.mediaDevices.getUserMedia = block);
        }} catch(e) {{}}
    }})();

    // --- Ẩn các dấu hiệu automation còn sót (plugins, permissions) ---
    (function() {{
        // plugins/mimeTypes giả cho giống trình duyệt thật
        Object.defineProperty(navigator, 'plugins', {{
            get: () => [1,2,3,4,5].map(i => ({{name: 'Plugin '+i}}))
        }});
        // permissions.query không lộ 'denied' bất thường
        const origQuery = window.navigator.permissions &&
                          window.navigator.permissions.query;
        if (origQuery) {{
            window.navigator.permissions.query = (p) =>
                p && p.name === 'notifications'
                    ? Promise.resolve({{state: Notification.permission}})
                    : origQuery(p);
        }}
    }})();
    """


def create_browser(name: str, fp: dict, chrome_version: int | None,
                   slot: int = 0) -> uc.Chrome:
    user_data = PROFILES_DIR / f"{name}_chrome"

    # Ô lưới cho cửa sổ này (2 hàng x 5 cột)
    x, y, w, h = grid_rect(slot)

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={user_data}")   # cookie/login riêng
    options.add_argument(f"--user-agent={fp['user_agent']}")
    # đặt sẵn vị trí + size lúc mở (CDP sẽ ép lại chính xác sau)
    options.add_argument(f"--window-position={x},{y}")
    options.add_argument(f"--window-size={w},{h}")
    options.add_argument(f"--lang={fp['language']}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    # --- BROWSER MỚI TINH KHÔI: không dùng dữ liệu cũ, không sync, không dấu vết ---
    options.add_argument("--no-service-autorun")
    options.add_argument("--disable-sync")                    # không đồng bộ tài khoản
    options.add_argument("--disable-background-networking")   # không ping ngầm
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-domain-reliability")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--no-pings")
    # Tắt popup "Save password?" / lời nhắc lưu mật khẩu của Chrome
    options.add_argument("--disable-save-password-bubble")
    options.add_argument("--disable-features=PasswordManagerEnabled,"
                        "AutofillEnableAccountWalletStorage,"
                        "OptimizationHints,MediaRouter,Translate")
    options.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.password_manager_leak_detection": False,
    })
    if fp.get("proxy"):
        options.add_argument(f"--proxy-server=http://{fp['proxy']}")

    # Dùng chrome.exe PORTABLE riêng của tool nếu có (anti-detect, tách biệt)
    portable = ensure_portable_chrome()
    if portable is not None:
        options.binary_location = str(portable)

    driver = uc.Chrome(options=options, use_subprocess=True,
                       version_main=chrome_version)

    # Ép timezone khớp
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                           {"timezoneId": fp["timezone"]})
    # Spoof GEO khớp timezone (không lộ VPN qua geolocation)
    lat, lon = TZ_GEO.get(fp["timezone"], (0.0, 0.0))
    try:
        driver.execute_cdp_cmd("Emulation.setGeolocationOverride",
                               {"latitude": lat, "longitude": lon, "accuracy": 100})
    except Exception:
        pass
    # Ép Accept-Language header khớp ngôn ngữ
    try:
        driver.execute_cdp_cmd("Network.setExtraHTTPHeaders",
                               {"headers": {"Accept-Language": fp["language"]}})
    except Exception:
        pass
    # Tiêm stealth JS (webdriver, WebGL, Canvas, WebRTC leak...) vào mọi trang
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                           {"source": build_stealth_js(fp)})
    # Đặt đúng ô lưới
    print(f"[*] Cửa sổ ô #{slot}: pos=({x},{y}) size=({w}x{h})")
    set_window_grid(driver, x, y, w, h)
    return driver


def set_window_grid(driver, x: int, y: int, w: int, h: int) -> None:
    """Ép cửa sổ về đúng ô (x,y,w,h) qua CDP để lách giới hạn min-width 500px.

    Ép LẶP nhiều lần vì Chrome hay bung cửa sổ về mức tối thiểu ~500px.
    In ra size thật Chrome trả về để dễ chẩn đoán nếu còn lệch.
    """
    try:
        win = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        wid = win["windowId"]
    except Exception as exc:
        print(f"    [!] getWindowForTarget lỗi: {exc}")
        return

    # đảm bảo không maximized
    try:
        driver.execute_cdp_cmd("Browser.setWindowBounds",
                               {"windowId": wid,
                                "bounds": {"windowState": "normal"}})
    except Exception:
        pass

    cur = {}
    for _ in range(5):
        try:
            driver.execute_cdp_cmd("Browser.setWindowBounds", {
                "windowId": wid,
                "bounds": {"left": x, "top": y, "width": w, "height": h},
            })
            cur = driver.execute_cdp_cmd(
                "Browser.getWindowBounds", {"windowId": wid})["bounds"]
            if abs(cur.get("width", 0) - w) <= 6 and abs(cur.get("left", 0) - x) <= 6:
                return
        except Exception:
            pass
        time.sleep(0.15)
    # còn lệch -> báo con số thật để chẩn đoán
    print(f"    [grid] muốn (x={x},y={y},w={w},h={h}) | Chrome trả "
          f"(x={cur.get('left')},y={cur.get('top')},w={cur.get('width')},h={cur.get('height')})")


def random_birthday_18plus() -> tuple[str, str, int]:
    """Sinh ngày sinh ngẫu nhiên đảm bảo tuổi >= 18 (từ 18 đến 45 tuổi).

    Returns: (tên tháng viết tắt như 'Jan', ngày dạng '1'..'28', năm).
    """
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    month = random.choice(months)
    day = f"{random.randint(1, 28):02d}"      # '01'..'28' (Roblox value có số 0 đầu)
    this_year = date.today().year
    year = random.randint(this_year - 30, this_year - 19)  # tuổi ~19..30 (chắc chắn 18+)
    return month, day, year


def _select_dropdown(driver, wait, el_id: str, value: str, label: str) -> bool:
    """Chọn 1 dropdown theo VALUE (option value), có fallback theo text/index."""
    try:
        el = wait.until(EC.presence_of_element_located((By.ID, el_id)))
        sel = Select(el)
        # 1) Thử theo value (Month có value "Jan", Day/Year value = số)
        try:
            sel.select_by_value(value)
            return True
        except Exception:
            pass
        # 2) Thử theo visible text
        try:
            sel.select_by_visible_text(value)
            return True
        except Exception:
            pass
        # 3) Fallback: dò trong danh sách option, khớp value hoặc text
        for opt in sel.options:
            ov = (opt.get_attribute("value") or "").strip()
            ot = (opt.text or "").strip()
            if ov == value or ot == value or ot.startswith(value):
                sel.select_by_value(ov)
                return True
        print(f"[!] {label}: không tìm thấy '{value}' trong dropdown.")
        return False
    except Exception as exc:
        print(f"[!] {label}: lỗi khi chọn -> {exc}")
        return False


MONTH_NUM = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
             "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
             "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}


def fill_birthday(driver) -> str | None:
    """Tự chọn Month/Day/Year (18+). Trả về birthday 'YYYY-MM-DD' nếu xong, None nếu lỗi."""
    month, day, year = random_birthday_18plus()
    print(f"[*] Điền ngày sinh (18+): {month} / {day} / {year}")
    wait = WebDriverWait(driver, 20)

    ok_m = _select_dropdown(driver, wait, "MonthDropdown", month, "Month")
    time.sleep(random.uniform(0.25, 0.5))
    ok_d = _select_dropdown(driver, wait, "DayDropdown", day, "Day")
    time.sleep(random.uniform(0.25, 0.5))
    ok_y = _select_dropdown(driver, wait, "YearDropdown", str(year), "Year")
    time.sleep(random.uniform(0.25, 0.5))

    if ok_m and ok_d and ok_y:
        print("[*] Đã điền ngày sinh xong (18+).")
        return f"{year}-{MONTH_NUM[month]}-{day}"   # định dạng API cần
    print("[!] Chưa điền đủ ngày sinh.")
    return None


# ==========================================================================
# USERNAME: tạo random -> check API Roblox chưa dùng -> gõ TỪNG KÝ TỰ
# ==========================================================================
def random_username() -> str:
    """Tạo username 7..11 ký tự, chỉ chữ (hoa/thường) và số, không ký tự đặc biệt."""
    import string
    length = random.randint(8, 11)              # 8..11 cho chắc chắn > 7
    first = random.choice(string.ascii_letters)  # ký tự đầu là chữ
    rest = "".join(random.choice(string.ascii_letters + string.digits)
                   for _ in range(length - 1))
    return first + rest


def username_available(driver, username: str, birthday: str) -> bool:
    """Gọi API validate NGAY TRONG BROWSER (cùng IP/cookie) -> code 0 là dùng được.

    https://auth.roblox.com/v1/usernames/validate
    Trả về True nếu tên hợp lệ & chưa ai dùng.
    """
    js = """
    const done = arguments[arguments.length - 1];
    const u = arguments[0], b = arguments[1];
    const url = 'https://auth.roblox.com/v1/usernames/validate'
        + '?request.username=' + encodeURIComponent(u)
        + '&request.birthday=' + encodeURIComponent(b)
        + '&urlLocale=en_us';
    fetch(url, {credentials: 'include'})
        .then(r => r.json())
        .then(d => done(JSON.stringify(d)))
        .catch(e => done('ERR:' + e));
    """
    try:
        driver.set_script_timeout(20)
        raw = driver.execute_async_script(js, username, birthday)
        if raw.startswith("ERR:"):
            print(f"    [check] lỗi gọi API: {raw}")
            return False
        data = json.loads(raw)
        code = data.get("code")
        msg = data.get("message", "")
        print(f"    [check] '{username}' -> code={code} msg='{msg}'")
        return code == 0            # 0 = hợp lệ & chưa dùng
    except Exception as exc:
        print(f"    [check] exception: {exc}")
        return False


def type_slowly(element, text: str) -> None:
    """Gõ từng ký tự một (không paste), delay biến thiên như người thật."""
    for ch in text:
        element.send_keys(ch)
        # đa số nhanh, thỉnh thoảng khựng lâu hơn -> giống nhịp gõ người thật
        d = random.uniform(0.05, 0.18)
        if random.random() < 0.12:
            d += random.uniform(0.2, 0.5)
        time.sleep(d)


def human_mouse_wiggle(driver, moves: int = 6) -> None:
    """Di chuột ngẫu nhiên vài lần (giảm điểm nghi ngờ bot -> ít captcha hơn)."""
    try:
        w = driver.execute_script("return window.innerWidth") or 500
        h = driver.execute_script("return window.innerHeight") or 500
        for _ in range(moves):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": random.randint(5, int(w) - 5),
                "y": random.randint(5, int(h) - 5),
            })
            time.sleep(random.uniform(0.05, 0.2))
    except Exception:
        pass


def fill_username(driver, birthday: str) -> str | None:
    """Tìm username chưa dùng rồi gõ từ từ vào ô Username. Trả về tên đã dùng."""
    wait = WebDriverWait(driver, 20)
    try:
        box = wait.until(EC.element_to_be_clickable((By.ID, "signup-username")))
    except Exception:
        # dự phòng theo name nếu id đổi
        box = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "input[name='signupUsername'], input#signup-username")))

    for attempt in range(1, 21):                 # thử tối đa 20 tên
        name = random_username()
        print(f"[*] Thử username #{attempt}: {name}")
        if username_available(driver, name, birthday):
            print(f"[*] '{name}' còn trống -> gõ vào form...")
            # Focus vào ô: scroll tới + click (React chỉ nhận khi ô được focus)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
            time.sleep(0.3)
            box.click()
            time.sleep(0.3)
            box.send_keys("")                    # đảm bảo đã focus
            type_slowly(box, name)               # gõ từng ký tự
            typed = driver.execute_script("return arguments[0].value;", box)
            if typed == name:
                print(f"[*] Đã điền username: {name}")
                return name
            print(f"[!] Gõ vào ô không khớp (ô đang là '{typed}'), thử lại tên khác...")
            box.clear()
        else:
            print("    (đã có người dùng / không hợp lệ, thử tên khác)")
    print("[!] Thử 20 tên đều không được.")
    return None


# ==========================================================================
# PASSWORD: prefix 'No1' + 7..10 ký tự (chữ hoa/thường + số)
# ==========================================================================
def random_password() -> str:
    """Mật khẩu: luôn bắt đầu 'No1', theo sau 7..10 ký tự gồm chữ (hoa/thường) và số."""
    import string
    length = random.randint(7, 10)
    body = "".join(random.choice(string.ascii_letters + string.digits)
                   for _ in range(length))
    return "No1" + body


def fill_password(driver) -> str | None:
    """Tạo mật khẩu rồi gõ TỪNG KÝ TỰ vào ô Password của Roblox."""
    pwd = random_password()
    wait = WebDriverWait(driver, 20)
    try:
        box = wait.until(EC.element_to_be_clickable((By.ID, "signup-password")))
    except Exception:
        box = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "input[name='signupPassword'], input#signup-password")))

    print(f"[*] Điền mật khẩu: {pwd}")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", box)
    time.sleep(0.3)
    box.click()
    time.sleep(0.3)
    box.send_keys("")
    type_slowly(box, pwd)                        # gõ từng ký tự, không paste
    typed = driver.execute_script("return arguments[0].value;", box)
    if typed == pwd:
        print("[*] Đã điền mật khẩu xong.")
        return pwd
    print(f"[!] Mật khẩu vào ô không khớp (ô đang là '{typed}').")
    return None


# ==========================================================================
# GENDER: random chọn Nam / Nữ (2 nút)
# ==========================================================================
def select_gender(driver) -> str | None:
    """Random bấm nút giới tính Nam (#MaleButton) hoặc Nữ (#FemaleButton)."""
    choice = random.choice(["Nam", "Nữ"])
    btn_id = "MaleButton" if choice == "Nam" else "FemaleButton"
    print(f"[*] Chọn giới tính (random): {choice}")

    wait = WebDriverWait(driver, 15)
    try:
        el = wait.until(EC.element_to_be_clickable((By.ID, btn_id)))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(random.uniform(0.25, 0.5))
        el.click()
        # Kiểm tra đã chọn (nút được chọn có class 'gender-selected' bên trong)
        selected = driver.execute_script(
            "return arguments[0].querySelector('.gender-selected') !== null;", el)
        print(f"[*] Đã chọn giới tính: {choice}" +
              ("" if selected else " (đã bấm)"))
        return choice
    except Exception as exc:
        print(f"[!] Không chọn được giới tính: {exc}")
        return None


# ==========================================================================
# SIGN UP: bấm nút đăng ký
# ==========================================================================
def click_signup(driver) -> bool:
    """Bấm nút Sign Up (#signup-button) để gửi form đăng ký."""
    wait = WebDriverWait(driver, 15)
    try:
        btn = wait.until(EC.element_to_be_clickable((By.ID, "signup-button")))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(random.uniform(0.4, 0.8))
        btn.click()
        print("[*] Đã bấm Sign Up.")
        return True
    except Exception as exc:
        print(f"[!] Không bấm được Sign Up: {exc}")
        return False


# ==========================================================================
# COOKIE / CAPTCHA / LƯU ACCOUNT
# ==========================================================================
ACCOUNTS_FILE = BASE_DIR / "account.txt"


def get_roblosecurity(driver) -> str | None:
    """Lấy cookie .ROBLOSECURITY. Chỉ có khi đã đăng nhập/tạo acc thành công."""
    try:
        c = driver.get_cookie(".ROBLOSECURITY")
        if c and c.get("value"):
            return c["value"]
    except Exception:
        pass
    return None


def detect_captcha_wave(driver) -> int | None:
    """Phát hiện FunCaptcha/Arkose và đọc số 'wave' (số ảnh phải giải).

    Trả về số wave nếu thấy captcha; None nếu không có captcha.
    """
    js = r"""
    // Có captcha Arkose/FunCaptcha đang hiển thị không?
    const hasArkose =
        document.querySelector('#arkose-iframe, iframe[src*="arkoselabs"], iframe[src*="funcaptcha"], [id*="FunCaptcha"], [class*="funcaptcha"], iframe[title*="captcha" i]') !== null;
    if (!hasArkose) return JSON.stringify({captcha:false, wave:null});

    // Đọc số wave: dò đệ quy MỌI iframe truy cập được (Arkose lồng nhiều tầng).
    function readWave(doc) {
        try {
            const txt = (doc.body ? doc.body.innerText : '') || '';
            // các dạng: "1 of 3", "1/3", "Wave 1 of 3", "Round 1 of 3"
            let m = txt.match(/(?:wave|round)?\s*(\d+)\s*(?:of|\/)\s*(\d+)/i);
            if (m) return parseInt(m[2]);
            // đôi khi tổng wave nằm trong aria-label / thuộc tính
            const el = doc.querySelector('[aria-label*="of" i], [class*="progress"]');
            if (el) {
                const m2 = (el.getAttribute('aria-label')||el.innerText||'').match(/(\d+)\s*(?:of|\/)\s*(\d+)/i);
                if (m2) return parseInt(m2[2]);
            }
        } catch(e) {}
        return null;
    }
    function scan(doc, depth) {
        if (!doc || depth > 4) return null;
        let w = readWave(doc);
        if (w) return w;
        const frames = doc.querySelectorAll('iframe');
        for (const f of frames) {
            try {
                const cd = f.contentDocument;
                if (cd) { const r = scan(cd, depth+1); if (r) return r; }
            } catch(e) {}
        }
        return null;
    }
    let wave = scan(document, 0);
    return JSON.stringify({captcha:true, wave:wave});
    """
    try:
        data = json.loads(driver.execute_script(js))
        if data.get("captcha"):
            return data.get("wave") or 0    # 0 = có captcha nhưng chưa đọc được số wave
        return None
    except Exception:
        return None


def captcha_present(driver) -> bool:
    """Có iframe captcha Arkose/FunCaptcha đang hiển thị không?"""
    js = r"""
    return document.querySelector(
      '#arkose-iframe, iframe[src*="arkoselabs"], iframe[src*="funcaptcha"],'
      + '[id*="FunCaptcha"], [class*="funcaptcha"], iframe[title*="captcha" i]'
    ) !== null;
    """
    try:
        return bool(driver.execute_script(js))
    except Exception:
        return False


def click_into_captcha(driver) -> None:
    """Click vào giữa iframe captcha để nó BẮT ĐẦU (mới hiện số wave)."""
    js = r"""
    const f = document.querySelector(
      '#arkose-iframe, iframe[src*="arkoselabs"], iframe[src*="funcaptcha"],'
      + '[id*="FunCaptcha"], [class*="funcaptcha"], iframe[title*="captcha" i]'
    );
    if (!f) return false;
    const r = f.getBoundingClientRect();
    return JSON.stringify({x: r.left + r.width/2, y: r.top + r.height/2});
    """
    try:
        info = driver.execute_script(js)
        if not info or info is True:
            return
        pos = json.loads(info)
        # Click vào tâm iframe bằng CDP (Arkose bắt sự kiện chuột thật)
        for tp in ("mousePressed", "mouseReleased"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": tp, "x": pos["x"], "y": pos["y"],
                "button": "left", "clickCount": 1,
            })
            time.sleep(0.1)
    except Exception:
        pass


def save_account(username: str, password: str, cookie: str) -> None:
    """Ghi 1 dòng username:pass:cookie vào account.txt (an toàn đa luồng)."""
    line = f"{username}:{password}:{cookie}\n"
    with _file_lock:
        with open(ACCOUNTS_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    print(f"[*] Đã lưu account vào {ACCOUNTS_FILE.name}: {username}:{password}:<cookie {len(cookie)} ký tự>")


def wait_login_and_save(driver, username: str, password: str,
                        max_wait: int = 600) -> str:
    """Chờ đăng nhập xong rồi lưu account. Trả về kết quả dạng chuỗi:

    - "SAVED"     : lấy được cookie, đã lưu account.txt
    - "MULTIWAVE" : captcha > 1 wave -> BỎ, tắt browser bật cái khác
    - "FAIL"      : hết giờ / cửa sổ đóng, chưa lưu được
    """
    print("[*] Đang chờ đăng nhập / kiểm tra captcha...")
    start = time.time()

    # --- Bước 1: chờ vài giây xem có cookie ngay (KHÔNG captcha) ---
    for _ in range(4):
        cookie = get_roblosecurity(driver)
        if cookie:
            print("[*] KHÔNG captcha -> đã đăng nhập, lấy cookie.")
            save_account(username, password, cookie)
            return "SAVED"
        if captcha_present(driver):
            break
        time.sleep(1)

    # --- Bước 2: nếu có captcha -> chờ load xong rồi CLICK vào để hiện wave ---
    if captcha_present(driver):
        print("[!!!] CÓ CAPTCHA — chờ load rồi click vào để đọc số wave...")
        time.sleep(3)                       # chờ Arkose render
        click_into_captcha(driver)          # click vào captcha để nó bắt đầu
        time.sleep(2.5)                     # chờ wave hiện ra

        # Đọc số wave (thử vài lần vì load bất đồng bộ)
        wave = 0
        for _ in range(6):
            w = detect_captcha_wave(driver)
            if w:
                wave = w
                break
            time.sleep(1)

        if wave and wave > 1:
            print(f"[!!!] CAPTCHA {wave} WAVE (>1) -> BỎ, tắt browser bật cái khác.")
            return "MULTIWAVE"
        print(f"[!!!] CAPTCHA {wave or 1} wave -> chấp nhận. HÃY GIẢI BẰNG TAY, "
              f"chương trình sẽ tự lấy cookie khi xong.")

    # --- Bước 3: chờ tới khi có cookie (người giải captcha xong) ---
    while time.time() - start < max_wait:
        cookie = get_roblosecurity(driver)
        if cookie:
            print("[*] ĐÃ ĐĂNG NHẬP! Lấy được cookie.")
            save_account(username, password, cookie)
            return "SAVED"
        try:
            _ = driver.title
        except Exception:
            return "FAIL"
        time.sleep(3)

    print("[!] Hết thời gian chờ.")
    return "FAIL"


# ==========================================================================
def fill_one_account(driver, name: str, slot: int) -> None:
    """Điền TRỌN 1 tài khoản trên 1 cửa sổ ĐÃ MỞ SẴN (dùng cho mỗi thread)."""
    tag = f"[{name}]"
    try:
        print(f"{tag} mở trang tạo tài khoản...")
        driver.get("https://www.roblox.com/Createaccount")

        # Ép lại đúng ô lưới SAU khi load (Chrome hay bung size khi điều hướng)
        x, y, w, h = grid_rect(slot)
        set_window_grid(driver, x, y, w, h)

        human_mouse_wiggle(driver)
        birthday = fill_birthday(driver)
        human_mouse_wiggle(driver, 3)
        username = fill_username(driver, birthday) if birthday else None
        human_mouse_wiggle(driver, 3)
        password = fill_password(driver)
        human_mouse_wiggle(driver, 3)
        select_gender(driver)

        # nghỉ tự nhiên trước khi bấm đăng ký (giống người xem lại form)
        time.sleep(random.uniform(1.0, 2.2))
        human_mouse_wiggle(driver, 4)
        click_signup(driver)

        result = "FAIL"
        if username and password:
            result = wait_login_and_save(driver, username, password)
        else:
            print(f"{tag} [!] Thiếu username/password.")

        if result == "SAVED":
            print(f"{tag} [*] Đã lưu account -> đóng + xóa profile.")
        elif result == "MULTIWAVE":
            print(f"{tag} [*] Captcha >1 wave -> tắt luôn, worker bật acc khác.")
        else:  # FAIL: captcha 1 wave chờ giải tay, hoặc lỗi
            # Giữ mở tối đa để bạn giải captcha 1 wave; đóng tay để bỏ qua
            print(f"{tag} [!] Chờ bạn giải captcha (1 wave) hoặc đóng tay để bỏ.")
            while True:
                try:
                    if get_roblosecurity(driver):
                        # bạn vừa giải xong -> lưu
                        wait_login_and_save(driver, username, password, max_wait=10)
                        break
                    _ = driver.title
                    time.sleep(2)
                except Exception:
                    break
    except Exception as exc:
        print(f"{tag} [!] Lỗi: {exc}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        # XÓA SẠCH profile sau khi xong -> không để lại dấu vết (cookie/cache/history)
        time.sleep(1)
        wipe_profile(name)
        print(f"{tag} [*] Đã xóa sạch profile (browser tinh khôi).")


def parse_total_accounts() -> int:
    """Đọc số acc muốn tạo từ tham số: python main.py --100  (hoặc 100)."""
    for arg in sys.argv[1:]:
        m = re.match(r"-*(\d+)$", arg)   # '--100', '-100', '100'
        if m:
            return int(m.group(1))
    return NUM_TABS   # không truyền -> tạo đúng số tab (1 đợt)


# Bộ đếm acc đã hoàn thành + khóa, để các worker phối hợp
_done_lock = threading.Lock()
_done_count = 0
_next_id_lock = threading.Lock()
_next_id = 0


def worker_loop(slot: int, total: int, id_start: int,
                chrome_version: int | None) -> None:
    """1 worker giữ 1 ô lưới cố định, lặp: mở browser -> tạo acc -> xóa -> tiếp.

    Chạy tới khi tổng số acc hoàn thành đạt 'total'.
    """
    global _done_count, _next_id
    while True:
        # Lấy số thứ tự acc kế tiếp; dừng nếu đã đủ total
        with _next_id_lock:
            if _next_id >= total:
                return
            my_id = id_start + _next_id
            _next_id += 1

        name = f"profile_{my_id:03d}"
        tag = f"[{name}]"
        wipe_profile(name)                    # sạch trước khi mở

        driver = None
        for attempt in range(1, 4):
            try:
                fp = fresh_fingerprint()
                fp["proxy"] = None
                driver = create_browser(name, fp, chrome_version, slot)
                break
            except Exception as exc:
                print(f"{tag} [!] Mở Chrome lỗi (lần {attempt}/3): {exc}")
                time.sleep(2)
        if driver is None:
            print(f"{tag} [!] Không mở được -> bỏ, làm acc khác.")
            continue

        # Điền + xử lý captcha; hàm tự đóng browser + xóa profile khi xong
        fill_one_account(driver, name, slot)

        with _done_lock:
            _done_count += 1
            print(f"[*] Tiến độ: {_done_count}/{total} acc xong.")


def main() -> None:
    global _next_id
    total = parse_total_accounts()
    chrome_version = get_chrome_major_version()
    cols, rows, cw, ch, _ = grid_layout()
    workers = min(NUM_TABS, total)   # số tab chạy đồng thời (tối đa NUM_TABS)

    print(f"[*] Chrome version: {chrome_version}")
    print(f"[*] Sẽ tạo {total} acc, {workers} tab chạy song song "
          f"(lưới {cols} cột x {rows} hàng, ô {cw}x{ch}).")

    # id bắt đầu = số profile lớn nhất đang có + 1 (không trùng)
    existing = [p.stem for p in PROFILES_DIR.glob("profile_*")]
    nums = [int(m.group(1)) for p in existing
            if (m := re.match(r"profile_(\d+)", p))]
    id_start = (max(nums) + 1) if nums else 1
    _next_id = 0

    # Mỗi worker giữ 1 ô lưới cố định (slot 0..workers-1)
    threads = []
    for slot in range(workers):
        t = threading.Thread(target=worker_loop,
                             args=(slot, total, id_start, chrome_version),
                             daemon=False)
        t.start()
        threads.append(t)
        time.sleep(1.2)   # giãn để Chrome không tranh driver lúc khởi động

    for t in threads:
        t.join()
    print(f"[*] HOÀN TẤT: đã xử lý {total} acc.")


if __name__ == "__main__":
    main()
