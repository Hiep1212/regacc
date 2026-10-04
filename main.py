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

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

# Thư mục lưu profile (fingerprint json + user-data Chrome)
BASE_DIR = Path(__file__).parent
PROFILES_DIR = BASE_DIR / "profiles"
PROFILES_DIR.mkdir(exist_ok=True)

# Chrome PORTABLE riêng của tool (không đụng Chrome hệ thống bạn đang lướt web)
PORTABLE_CHROME_DIR = BASE_DIR / "chrome"
PORTABLE_CHROME_EXE = PORTABLE_CHROME_DIR / "chrome.exe"

# Extension OMOcaptcha (tự giải FunCaptcha của Roblox). Load sẵn cho mọi browser;
# content script chỉ chạy trên iframe arkose/funcaptcha nên không ảnh hưởng khi
# không có captcha. Cần api_key trong omocaptcha/configs.json (đã có sẵn).
CAPTCHA_EXT_DIR = BASE_DIR / "omocaptcha"
USE_CAPTCHA_EXT = True     # False nếu muốn tắt tự giải captcha

# ---- Lưới cửa sổ: 5 tab, 3 cột x 2 hàng (3 trên, 2 dưới) -------------------
GRID_COLS = 3
GRID_ROWS = 2
NUM_TABS = 5

# FAST_MODE: True = nhanh (giảm delay, bỏ warm-up). Bật bằng: python main.py --fast
FAST_MODE = False

# MINIMIZED: minimize thì chuột/phím thật không chạy -> KHÔNG điền được form.
# Nên tắt. Thay vào đó mở cửa sổ NHỎ NHẤT có thể (visible để điền được).
MINIMIZED = False
# SMALL_WINDOW: False = chia đều màn (3 trên 2 dưới, to như cũ). True = cửa sổ nhỏ.
SMALL_WINDOW = False
MIN_W, MIN_H = 500, 400

# BLOCK_IMAGES: chặn ảnh/font để load nhanh hơn. False = GIỮ ảnh (không chặn),
# chỉ chặn tracking/video nền -> vẫn nhanh mà ảnh/captcha hiển thị bình thường.
BLOCK_IMAGES = False

# SPOOF_FINGERPRINT: True = mỗi profile là một THIẾT BỊ khác (CPU/RAM/card
# đồ họa + nhiễu canvas/audio riêng) -> Arkose không thấy hàng loạt acc từ
# cùng một máy -> ít wave hơn. Đặt False để A/B so sánh.
SPOOF_FINGERPRINT = True


def nap(a: float, b: float) -> None:
    """Nghỉ ngẫu nhiên a..b giây. Ở FAST_MODE thì rút ngắn còn ~1/4."""
    if FAST_MODE:
        a, b = a * 0.25, b * 0.25
    time.sleep(random.uniform(a, b))


def get_screen_size() -> tuple[int, int]:
    """Kích thước màn hình LOGIC (vd 1536x864 ở DPI 125%).

    Chrome đặt cửa sổ theo tọa độ LOGIC. NHƯNG pyautogui/uiautomation khi import
    tự SetProcessDPIAware -> GetSystemMetrics trả PIXEL VẬT LÝ (1920). Nên phải
    CHIA cho DPI scale để về logic. Dùng GetDpiForSystem.
    """
    try:
        user32 = ctypes.windll.user32
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        # lấy DPI scale (96 = 100%, 120 = 125%, 144 = 150%)
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
            scale = dpi / 96.0
        except Exception:
            scale = 1.0
        # nếu w > 1600 nhưng scale > 1 -> đang là pixel vật lý -> quy về logic
        if scale > 1.01:
            w = int(w / scale)
            h = int(h / scale)
        return w, h
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
    """Trả (x, y, w, h) cho ô 'slot'.

    SMALL_WINDOW: mỗi cửa sổ NHỎ NHẤT (MIN_W x MIN_H), xếp cạnh nhau từ góc
    trên-trái theo lưới -> gọn, ít che màn. Ngược lại: chia đều màn (bản gốc).
    """
    cols, rows, cell_w, cell_h, sh = grid_layout()
    col = slot % cols
    row = (slot // cols) % rows
    if SMALL_WINDOW:
        w, h = MIN_W, MIN_H
        x = col * w
        y = row * h
        return x, y, w, h
    x = col * cell_w
    y = row * cell_h
    return x, y, cell_w, cell_h

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
_driver_patched = False   # chỉ patch chromedriver 1 lần / lần chạy


def patch_chromedriver_cdc() -> None:
    """XÓA chuỗi 'cdc_...' trong chromedriver.exe -> ẩn dấu vết chromedriver.

    Arkose/DataDome check các biến window.cdc_* (do chromedriver bơm vào) để
    nhận diện bot -> tăng số wave captcha. undetected-chromedriver patch sẵn;
    selenium thường thì KHÔNG -> phải tự patch. Thay 'cdc_'+22 ký tự bằng chuỗi
    random cùng độ dài (không đổi kích thước file). Chạy 1 lần mỗi lần khởi động
    (selenium có thể tải lại driver mới -> cần patch lại)."""
    global _driver_patched
    if _driver_patched:
        return
    try:
        import string
        from selenium.webdriver.common.selenium_manager import SeleniumManager
        sm = SeleniumManager()
        info = sm.binary_paths(["--browser", "chrome"])
        drv_path = info.get("driver_path")
        if not drv_path or not Path(drv_path).exists():
            return
        data = Path(drv_path).read_bytes()
        if not re.search(rb"cdc_[a-zA-Z0-9]{22}", data):
            _driver_patched = True         # đã sạch (đã patch trước đó)
            return

        def _repl(m):
            n = len(m.group(0))
            return "".join(random.choice(string.ascii_letters)
                           for _ in range(n)).encode()

        new = re.sub(rb"cdc_[a-zA-Z0-9]{22}", _repl, data)
        if len(new) == len(data):
            Path(drv_path).write_bytes(new)
            print(f"[*] Đã patch chromedriver (ẩn cdc_): {drv_path}")
        _driver_patched = True
    except Exception as exc:
        print(f"[!] Không patch được chromedriver: {exc}")


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


# Mẫu JS spoof fingerprint. Placeholder được thay theo TỪNG profile trong
# build_stealth_js() -> mỗi tài khoản trông như một MÁY KHÁC nhau.
_FP_SPOOF_TMPL = r"""
(function () {
    'use strict';
    try {
        if (window.__fpPatched) return;
        window.__fpPatched = true;

        var SEED  = __SEED__;
        var CORES = __CORES__;
        var MEM   = __MEM__;
        var GLV   = "__GLVENDOR__";
        var GLR   = "__GLRENDERER__";
        var SPOOF = __SPOOF__;   // false = chỉ dọn dấu vết bot, KHÔNG đổi thiết bị

        // PRNG ổn định theo seed: CÙNG profile luôn ra CÙNG nhiễu. Fingerprint
        // đổi liên tục giữa 2 lần đo cũng là cờ bot, nên bắt buộc phải ổn định.
        function rnd(i) {
            var x = Math.sin(SEED * 9301 + i * 49297 + 12345) * 233280;
            return x - Math.floor(x);
        }
        function def(o, p, v) {
            try {
                Object.defineProperty(o, p, {
                    get: function () { return v; }, configurable: true });
            } catch (e) {}
        }
        // Bọc Proxy -> fn.toString() vẫn trả '[native code]'. Wrapper thường sẽ
        // lòi source ra khi web soi toString -> lộ ngay.
        function wrap(orig, handler) {
            try { return new Proxy(orig, { apply: handler }); } catch (e) { return orig; }
        }

        // ---- 1) Dọn dấu vết chromedriver ----
        try {
            var ks = Object.getOwnPropertyNames(window);
            for (var i = 0; i < ks.length; i++) {
                var k = ks[i];
                if (k.indexOf('cdc_') >= 0 || k.indexOf('$chrome_asyncScriptInfo') >= 0) {
                    try { delete window[k]; } catch (e) { window[k] = undefined; }
                }
            }
        } catch (e) {}
        def(navigator, 'webdriver', false);

        if (SPOOF) {
        // ---- 2) Phần cứng RIÊNG cho từng profile ----
        // Đây là chỗ quan trọng nhất: nếu mọi acc cùng một cấu hình máy thì
        // Arkose thấy 1 thiết bị đẻ ra hàng loạt acc -> hạ điểm tin cậy -> nhiều wave.
        def(navigator, 'hardwareConcurrency', CORES);
        def(navigator, 'deviceMemory', MEM);
        // KHÔNG đụng navigator.languages: để nguyên tiếng Việt thật của máy.
        // Ép en-US ở đây sẽ chỏi với header Accept-Language (vi) mà Chrome gửi,
        // tạo ra đúng cái mâu thuẫn cần tránh. Thực tế đo được: vi cho captcha
        // NHẸ hơn en-US.

        // ---- 3) Card đồ họa riêng theo profile (đều là D3D11/Windows nên khớp UA) ----
        try {
            var glPatch = function (t, self, a) {
                var p = a[0];
                if (p === 37445) return GLV;   // UNMASKED_VENDOR_WEBGL
                if (p === 37446) return GLR;   // UNMASKED_RENDERER_WEBGL
                return Reflect.apply(t, self, a);
            };
            if (window.WebGLRenderingContext) {
                WebGLRenderingContext.prototype.getParameter =
                    wrap(WebGLRenderingContext.prototype.getParameter, glPatch);
            }
            if (window.WebGL2RenderingContext) {
                WebGL2RenderingContext.prototype.getParameter =
                    wrap(WebGL2RenderingContext.prototype.getParameter, glPatch);
            }
        } catch (e) {}

        // ---- 4) Canvas: nhiễu ±1 trên VÀI pixel, ổn định theo seed ----
        // WeakSet để MỖI canvas chỉ nhiễu ĐÚNG 1 LẦN. Nếu nhiễu tích luỹ mỗi lần
        // gọi toDataURL thì hash đổi liên tục -> bất ổn -> lộ.
        try {
            var noised = new WeakSet();
            var addNoise = function (cv) {
                try {
                    if (!cv || noised.has(cv) || !cv.width || !cv.height) return;
                    noised.add(cv);
                    var ctx = cv.getContext('2d');
                    if (!ctx) return;
                    for (var i = 0; i < 6; i++) {
                        var x = Math.floor(rnd(i * 3) * cv.width);
                        var y = Math.floor(rnd(i * 3 + 1) * cv.height);
                        var d = ctx.getImageData(x, y, 1, 1);
                        var ch = i % 3;
                        // +1 hoặc -1 (255 ≡ -1 mod 256): mắt thường không thấy
                        d.data[ch] = (d.data[ch] + (rnd(i * 3 + 2) > 0.5 ? 1 : 255)) % 256;
                        // BẮT BUỘC đụng kênh alpha: pixel trong suốt (alpha=0) bị
                        // PNG chuẩn hoá RGB về 0 -> nhiễu RGB bị NUỐT MẤT. Phần lớn
                        // canvas fingerprint là vùng trong suốt nên không sửa alpha
                        // thì rất hay mất nhiễu -> 2 profile ra cùng hash.
                        d.data[3] = (d.data[3] === 0) ? 1 : (d.data[3] - 1);
                        ctx.putImageData(d, x, y);
                    }
                } catch (e) {}
            };
            HTMLCanvasElement.prototype.toDataURL = wrap(
                HTMLCanvasElement.prototype.toDataURL,
                function (t, self, a) { addNoise(self); return Reflect.apply(t, self, a); });
            HTMLCanvasElement.prototype.toBlob = wrap(
                HTMLCanvasElement.prototype.toBlob,
                function (t, self, a) { addNoise(self); return Reflect.apply(t, self, a); });
            if (window.CanvasRenderingContext2D) {
                CanvasRenderingContext2D.prototype.getImageData = wrap(
                    CanvasRenderingContext2D.prototype.getImageData,
                    function (t, self, a) {
                        try { addNoise(self.canvas); } catch (e) {}
                        return Reflect.apply(t, self, a);
                    });
            }
        } catch (e) {}

        // ---- 5) Audio: nhiễu 1e-7, không nghe thấy nhưng đủ đổi hash ----
        try {
            if (window.AudioBuffer) {
                var done = new WeakSet();
                AudioBuffer.prototype.getChannelData = wrap(
                    AudioBuffer.prototype.getChannelData,
                    function (t, self, a) {
                        var out = Reflect.apply(t, self, a);
                        try {
                            if (out && !done.has(out)) {
                                done.add(out);
                                for (var i = 0; i < out.length; i += 337) {
                                    out[i] = out[i] + (rnd(i) - 0.5) * 1e-7;
                                }
                            }
                        } catch (e) {}
                        return out;
                    });
            }
        } catch (e) {}
        }   // hết khối SPOOF

        // ---- 6) chrome.runtime + permissions ----
        try {
            if (!window.chrome) window.chrome = {};
            if (!window.chrome.runtime) window.chrome.runtime = {};
        } catch (e) {}
        try {
            var pq = navigator.permissions && navigator.permissions.query;
            if (pq) {
                navigator.permissions.query = wrap(pq, function (t, self, a) {
                    try {
                        var p = a[0];
                        // typeof check: có build Notification không tồn tại ->
                        // đọc thẳng Notification.permission sẽ ném lỗi -> lộ.
                        if (p && p.name === 'notifications' &&
                            typeof Notification !== 'undefined') {
                            return Promise.resolve({ state: Notification.permission });
                        }
                    } catch (e) {}
                    return Reflect.apply(t, self, a);
                });
            }
        } catch (e) {}

    } catch (e) {}
})();

// ==== CHẶN PASSKEY / WebAuthn ====
// IIFE RIÊNG, cố ý KHÔNG nằm chung try với phần fingerprint ở trên: chỉ cần
// một dòng spoof nào đó ném lỗi là cả khối trên nhảy xuống catch, đoạn này sẽ
// bị bỏ qua -> hộp thoại Windows lại hiện.
//
// Vì sao phải chặn: form đăng ký MỚI của Roblox tới bước 2 là gọi WebAuthn ->
// Windows Hello bật hộp thoại "Lưu mã khoá của bạn". Đó là hộp thoại NATIVE
// của Windows, Selenium KHÔNG đóng được, nó chặn cứng cả browser.
// Khai báo máy không có platform authenticator -> Roblox chỉ còn mời đặt mật
// khẩu. (Máy bàn không có Windows Hello cũng y hệt nên không phải dấu hiệu bot.)
(function () {
    try {
        if (window.PublicKeyCredential) {
            PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable =
                function () { return Promise.resolve(false); };
            PublicKeyCredential.isConditionalMediationAvailable =
                function () { return Promise.resolve(false); };
        }
    } catch (e) {}
    try {
        // Phòng khi Roblox cứ gọi bất chấp: trả lỗi y như người dùng bấm Huỷ,
        // KHÔNG để hộp thoại Windows kịp hiện.
        var cr = navigator.credentials;
        var deny = function () {
            return Promise.reject(new DOMException(
                'The operation either timed out or was not allowed.',
                'NotAllowedError'));
        };
        if (cr && cr.create) {
            var oc = cr.create.bind(cr);
            cr.create = function (o) { return (o && o.publicKey) ? deny() : oc(o); };
        }
        if (cr && cr.get) {
            var og = cr.get.bind(cr);
            cr.get = function (o) { return (o && o.publicKey) ? deny() : og(o); };
        }
    } catch (e) {}
})();
"""


def _js_str(v: str) -> str:
    """Escape chuỗi để nhúng an toàn vào JS trong dấu nháy kép."""
    return str(v).replace('\\', '\\\\').replace('"', '\\"')


def build_stealth_js(fp: dict) -> str:
    """JS anti-detect chạy TRƯỚC khi trang tải.

    LUÔN làm: dọn dấu vết chromedriver (cdc_), navigator.webdriver=false,
    bảo đảm chrome.runtime, vá permissions.query.

    Khi SPOOF_FINGERPRINT bật: đổi CPU/RAM/card đồ họa + thêm nhiễu
    canvas/audio RIÊNG theo từng profile. Đây là phần quan trọng nhất -
    nếu mọi acc dùng chung một fingerprint thì Arkose thấy 1 thiết bị đẻ ra
    hàng loạt acc -> hạ điểm tin cậy -> BẮT GIẢI NHIỀU WAVE. Nhiễu sinh từ
    seed cố định của profile nên ỔN ĐỊNH giữa các lần đo (đổi liên tục cũng
    là cờ bot).

    KHÔNG đụng tới UA/screen/timezone ở đây: UA thật là Windows Chrome nên
    card đồ họa chọn cũng toàn D3D11/Windows -> không mâu thuẫn. Timezone
    xử lý riêng bằng CDP cho khớp IP VPN.
    """
    ram = fp.get("ram_gb", 8)
    # navigator.deviceMemory chỉ nhận 0.25/0.5/1/2/4/8 -> chặn trần ở 8
    dev_mem = 8 if ram >= 8 else (4 if ram >= 4 else 2)
    return (_FP_SPOOF_TMPL
            .replace("__SEED__", str(int(fp.get("canvas_noise", 1))))
            .replace("__CORES__", str(int(fp.get("cpu_cores", 8))))
            .replace("__MEM__", str(dev_mem))
            .replace("__GLVENDOR__", _js_str(fp.get("webgl_vendor", "")))
            .replace("__GLRENDERER__", _js_str(fp.get("webgl_renderer", "")))
            .replace("__SPOOF__", "true" if SPOOF_FINGERPRINT else "false"))


def create_browser(name: str, fp: dict, chrome_version: int | None,
                   slot: int = 0) -> webdriver.Chrome:
    user_data = PROFILES_DIR / f"{name}_chrome"

    # Ô lưới cho cửa sổ này (2 hàng x 5 cột)
    x, y, w, h = grid_rect(slot)

    options = Options()
    # BiDi + webextension: nạp ext bằng driver.webextension.install() (Chrome mới
    # CHẶN --load-extension dù có DisableLoadExtensionCommandLineSwitch, nên phải
    # dùng đường BiDi này -> đã kiểm chứng nạp được).
    options.enable_bidi = True
    options.enable_webextensions = True
    # Ẩn cờ automation (thay cho undetected-chromedriver)
    options.add_experimental_option("excludeSwitches",
                                    ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)
    if USE_CAPTCHA_EXT and (CAPTCHA_EXT_DIR / "manifest.json").exists():
        _ensure_captcha_power_on()   # bật power_on để ext tự chạy (key có sẵn)
    options.add_argument(f"--user-data-dir={user_data}")   # cookie/login riêng
    # KHÔNG ép user-agent / lang giả -> dùng UA THẬT của Chrome máy (nhất quán,
    # không mâu thuẫn với fingerprint thật -> Arkose tin hơn -> ít wave).
    options.add_argument(f"--window-position={x},{y}")
    options.add_argument(f"--window-size={w},{h}")
    # KHÔNG dùng headless: extension OMOcaptcha không chạy được ở headless ->
    # không giải được captcha. Luôn hiện cửa sổ (giống Gen Roblox Browser).
    options.add_argument("--disable-blink-features=AutomationControlled")
    # WebRTC: KHÔNG xóa window.RTCPeerConnection bằng JS nữa - trình duyệt thật
    # LUÔN có WebRTC, thiếu hẳn API là cờ bot còn rõ hơn cả lộ IP. Thay bằng cờ
    # native của Chrome: WebRTC chỉ dùng interface public (IP VPN), không đụng
    # IP nội bộ/thật -> API còn nguyên mà vẫn không rò IP.
    options.add_argument(
        "--force-webrtc-ip-handling-policy=default_public_interface_only")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--enable-webgl")
    options.add_argument("--use-gl=angle")
    options.add_argument("--ignore-certificate-errors")
    # --- BROWSER MỚI TINH KHÔI: không dùng dữ liệu cũ, không sync, không dấu vết ---
    options.add_argument("--no-service-autorun")
    options.add_argument("--disable-sync")                    # không đồng bộ tài khoản
    options.add_argument("--disable-background-networking")   # không ping ngầm
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-domain-reliability")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--no-pings")
    options.add_argument("--disable-save-password-bubble")
    # NGÔN NGỮ: GIỮ NGUYÊN tiếng Việt thật của máy, KHÔNG ép en-US.
    # Đã thử thực tế: để en-US thì captcha còn NẶNG hơn để vi. Giữ vi cũng khiến
    # mọi tầng tự khớp nhau sẵn (header Accept-Language, Intl locale,
    # navigator.languages đều vi) -> không có chỗ vênh nào để lộ.
    # --- TĂNG TỐC: bớt việc nền, render nhẹ hơn ---
    # (KHÔNG dùng --disable-extensions vì cần nạp extension giải captcha)
    # (KHÔNG dùng --disable-notifications: cờ này XÓA LUÔN window.Notification,
    #  mà trình duyệt thật thì LUÔN có -> thiếu nó là cờ bot. Chặn bằng prefs
    #  bên dưới là đủ: API vẫn còn, permission = denied như máy đã từ chối.)
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-hang-monitor")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--mute-audio")
    # GOM TẤT CẢ --disable-features vào MỘT lần (nếu tách nhiều lần Chrome chỉ
    # nhận lần cuối -> các flag trước bị mất tác dụng, đây là bug cũ đã sửa).
    options.add_argument(
        "--disable-features="
        "IsolateOrigins,site-per-process,"       # tăng trust (không tách origin)
        "PasswordManagerEnabled,"                # không hỏi lưu mật khẩu
        "AutofillEnableAccountWalletStorage,"
        "OptimizationHints,MediaRouter,Translate,"
        "InterestFeedContentSuggestions,"
        "AutomationControlled,"                  # ẩn thêm cờ automation
        "DisableLoadExtensionCommandLineSwitch,"  # cho phép --load-extension chạy
        "CalculateNativeWinOcclusion"            # bớt Chrome tự đưa cửa sổ lên trước
    )
    options.page_load_strategy = "eager"     # không chờ tải hết mới chạy (vẫn tải ảnh)
    options.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.password_manager_leak_detection": False,
        "profile.default_content_setting_values.notifications": 2,
    })
    if fp.get("proxy"):
        options.add_argument(f"--proxy-server=http://{fp['proxy']}")

    # Dùng chrome.exe PORTABLE riêng của tool nếu có (anti-detect, tách biệt)
    portable = ensure_portable_chrome()
    if portable is not None:
        options.binary_location = str(portable)

    patch_chromedriver_cdc()          # ẩn dấu vết cdc_ TRƯỚC khi tạo driver
    driver = webdriver.Chrome(
        options=options, service=Service(log_output=subprocess.DEVNULL))

    # Ép timezone KHỚP nước VPN đang dùng. Ép ở tầng trình duyệt (CDP Emulation)
    # nên Intl/Date đều nhất quán - không phải hook JS nên không có dấu vết.
    if _vpn_timezone:
        try:
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                                   {"timezoneId": _vpn_timezone})
        except Exception as exc:
            print(f"[!] Không ép được timezone {_vpn_timezone}: {exc}")

    # NẠP extension OMOcaptcha NGAY BÂY GIỜ (trước khi mở trang CreateAccount).
    # Lúc này chưa có iframe funcaptcha nào -> ext + service worker sẵn sàng. Khi
    # iframe funcaptcha xuất hiện SAU (do bấm Sign Up), content script sẽ inject
    # vào frame MỚI đó -> tự giải, KHÔNG cần reload trang (reload sẽ mất captcha).
    if USE_CAPTCHA_EXT and (CAPTCHA_EXT_DIR / "manifest.json").exists():
        try:
            ext_path = str(CAPTCHA_EXT_DIR.resolve())
            res = driver.webextension.install(path=ext_path)
            print(f"[*] Đã nạp extension OMOcaptcha lúc mở: {res}")
        except Exception as exc:
            print(f"[!] Lỗi nạp extension OMOcaptcha: {exc}")

    # KHÔNG spoof timezone/geo/language nữa -> dùng THẬT của máy (nhất quán với
    # IP VPN). Chỉ tiêm JS chống WebRTC leak để không lộ IP thật.
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": build_stealth_js(fp)})
    except Exception:
        pass
    # Tiêm sniffer API signup TRƯỚC khi trang tải -> bắt được response thật
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": SNIFFER_JS})
    except Exception:
        pass
    # Tiêm hook đọc WAVE + VARIANT của FunCaptcha. PHẢI tiêm TRƯỚC khi mở trang:
    # request /fc/gfct/ bắn ngay lúc captcha load, tiêm sau là hụt.
    #  - Đường CHÍNH: BiDi addPreloadScript -> theo chuẩn áp cho MỌI browsing
    #    context, kể cả iframe Arkose khác origin (OOPIF). CDP thường KHÔNG chui
    #    được vào OOPIF nên đây mới là đường tin cậy.
    #  - Đường DỰ PHÒNG: CDP addScriptToEvaluateOnNewDocument (frame chính).
    # Cài 2 lần vô hại: JS có cờ __arkHookInstalled chống cài trùng.
    ark_ok = False
    try:
        driver.script.add_preload_script(f"() => {{ {ARKOSE_JS} }}")
        ark_ok = True
    except Exception as exc:
        print(f"[!] BiDi addPreloadScript lỗi ({exc}) -> dùng CDP thay thế.")
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": ARKOSE_JS})
    except Exception:
        pass
    if not ark_ok:
        # CDP không chắc tới được iframe Arkose -> bật auto-attach cho OOPIF
        try:
            driver.execute_cdp_cmd("Target.setAutoAttach",
                                   {"autoAttach": True,
                                    "waitForDebuggerOnStart": False,
                                    "flatten": True})
        except Exception:
            pass
    # TĂNG TỐC LOAD: chặn tài nguyên NẶNG không cần cho form.
    # KHÔNG chặn script chính (React cần) và iframe Arkose.
    try:
        # tracking/analytics luôn chặn (vô hại, chỉ làm chậm)
        blocked = [
            "*google-analytics*", "*googletagmanager*", "*doubleclick*",
            "*/scribe*", "*sentry*", "*.hotjar.com*", "*fullstory*",
            "*.mp4", "*.webm", "*.avi",                    # video nền
        ]
        if BLOCK_IMAGES:                                    # chặn thêm ảnh + font
            blocked += [
                "*.woff", "*.woff2", "*.ttf", "*.otf",
                "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg",
            ]
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": blocked})
    except Exception:
        pass
    # Đặt đúng ô lưới
    print(f"[*] Cửa sổ ô #{slot}: pos=({x},{y}) size=({w}x{h})")
    set_window_grid(driver, x, y, w, h)
    # MỞ NGẦM: thu nhỏ ngay -> không nhảy lên màn (extension vẫn chạy)
    if MINIMIZED:
        try:
            win = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
            driver.execute_cdp_cmd("Browser.setWindowBounds", {
                "windowId": win["windowId"],
                "bounds": {"windowState": "minimized"},
            })
        except Exception:
            pass
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


# ==========================================================================
# 2 FORM ĐĂNG KÝ: Roblox đang A/B nên phải nhận dạng rồi điền theo đúng kiểu
#   - "old": <select id=MonthDropdown> thường + có sẵn ô password + nút Sign Up
#   - "new": <div id=MonthDropdown> BỌC <button role=combobox> (Radix UI),
#            bước 1 chỉ có Birthday + Username + Gender + nút Continue,
#            password nằm ở BƯỚC 2.
# LƯU Ý: form mới DÙNG LẠI ĐÚNG id cũ (MonthDropdown...) nhưng là <div>, nên
# Select(el) sẽ ném UnexpectedTagNameException -> BẮT BUỘC phải phân biệt tag.
# ==========================================================================
FORM_OLD, FORM_NEW, FORM_UNKNOWN = "old", "new", "unknown"
# Biến thể thứ 3: combobox Radix GIỐNG form mới nhưng trang có thêm nút
# "Sign in" riêng ở header. Chưa mổ xẻ nên mặc định DỪNG lại để soi.
FORM_NEW_SIGNIN = "new+signin"

# True = gặp biến thể có nút "Sign in" thì DỪNG cả tool, GIỮ NGUYÊN tab đó và
#        dump cấu trúc ra signinform_dump.json để nghiên cứu.
# Đã mổ xẻ xong: nó là Radix mới + 1 bước kiểu cũ (#signup-password ngay bước 1,
# nút gửi "Create account") -> điền được rồi nên tắt.
HALT_ON_SIGNIN_FORM = False

# True  = gặp form MỚI thì DỪNG cả tool + giữ nguyên tab đó + dump cấu trúc ra
#         newform_dump.json (dùng khi Roblox lại đổi form, để soi selector mới).
# False = điền bình thường cả 2 form (mặc định, vì form mới đã làm xong).
HALT_ON_NEW_FORM = False

MONTH_FULL = {"Jan": "January", "Feb": "February", "Mar": "March",
              "Apr": "April", "May": "May", "Jun": "June", "Jul": "July",
              "Aug": "August", "Sep": "September", "Oct": "October",
              "Nov": "November", "Dec": "December"}

_VARIANT_JS = r"""
// Form CŨ nhận trước: trang cũ CŨNG có 1 combobox Radix (bộ chọn khác) nên
// không được nhận dạng form mới chỉ bằng [role=combobox].
if (document.querySelector('select#MonthDropdown, select[name="birthdayMonth"]'))
    return 'old';
if (document.querySelector(
      '#MonthDropdown [role="combobox"], #DayDropdown [role="combobox"],'
    + '#YearDropdown [role="combobox"], [data-testid^="birthday-"] [role="combobox"],'
    + '[role="combobox"][aria-label="Month"], [role="combobox"][aria-label="Day"],'
    + '[role="combobox"][aria-label="Year"]')) {

    // Có biến thể thứ 3: cũng combobox Radix nhưng trang kèm 1 nút "Sign in"
    // RIÊNG ở header. Phân biệt được vì form mới thường CHỈ có chữ "Sign in"
    // trong link cuối thẻ ("Already have an account? Sign in") - link đó nằm
    // TRONG thẻ đăng ký. Nút ở header thì nằm NGOÀI thẻ.
    const card = document.querySelector(
        '[data-testid="signup-v2-animated-card"], [data-testid="signup-v2-form-panel"],'
      + '[data-testid="signup-form-v2"], form');
    const vis = (e) => {
        const c = getComputedStyle(e);
        return c.visibility !== 'hidden' && c.display !== 'none'
            && !!(e.offsetWidth || e.offsetHeight);
    };
    const outside = [...document.querySelectorAll('a, button')].filter(e => {
        const t = (e.innerText || '').trim();
        if (!/^(sign in|đăng nhập)$/i.test(t)) return false;
        if (card && card.contains(e)) return false;   // link cuối thẻ -> bỏ
        return vis(e);
    });
    if (outside.length) return 'new+signin';
    return 'new';
}
return 'unknown';
"""


def detect_form_variant(driver) -> str:
    """Trang đang phục vụ form nào: FORM_OLD / FORM_NEW / FORM_UNKNOWN."""
    try:
        return driver.execute_script(_VARIANT_JS) or FORM_UNKNOWN
    except Exception:
        return FORM_UNKNOWN


_FIND_FIRST_JS = r"""
// PHẢI xét cả computed visibility: form mới giấu bước 1 bằng
// visibility:hidden, mà element kiểu đó VẪN có offsetWidth/Height -> chỉ đo
// kích thước là vớ nhầm ô của bước đang ẩn. visibility là thuộc tính kế thừa
// nên con của panel bị ẩn cũng tự tính ra 'hidden'.
const shown = (e) => {
    if (!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)) return false;
    const cs = getComputedStyle(e);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
};
for (const s of arguments[0]) {
    for (const e of document.querySelectorAll(s)) {
        if (shown(e)) return e;
    }
}
return null;
"""


def find_first(driver, selectors: list[str], timeout: float = 0.0):
    """Element HIỆN HÌNH đầu tiên khớp 1 trong các selector; None nếu không có.

    Dùng danh sách selector để 1 hàm chạy được trên CẢ 2 form (thử id cũ ->
    data-testid mới -> aria-label), khỏi phải viết 2 nhánh ở mọi chỗ.
    """
    deadline = time.time() + timeout
    while True:
        try:
            el = driver.execute_script(_FIND_FIRST_JS, selectors)
        except Exception:
            el = None
        if el is not None or time.time() >= deadline:
            return el
        time.sleep(0.25)


# Item trong dropdown Radix của Roblox KHÔNG phải [role=option] (đã kiểm chứng
# trên form thật: role=option đếm được 0). Nó là:
#   <button class="foundation-web-menu-item" data-radix-collection-item
#           aria-selected="false" data-state="unchecked"> <span>January</span>
# và KHÔNG có data-value -> bắt buộc khớp bằng TEXT.
_OPT_SEL = ('[data-radix-collection-item], button.foundation-web-menu-item,'
            ' [role="option"]')

_OPEN_LISTBOX_JS = r"""
const t = arguments[0], sel = arguments[1];
const id = t.getAttribute('aria-controls');
let lb = id ? document.getElementById(id) : null;
if (!lb) lb = document.querySelector('[role="listbox"][data-state="open"]')
          || document.querySelector('[role="listbox"]');
if (!lb) return null;
return lb.querySelectorAll(sel).length;
"""

_PICK_OPTION_JS = r"""
const t = arguments[0], wants = arguments[1], sel = arguments[2];
const wrapId = arguments[3], value = arguments[4];
const id = t.getAttribute('aria-controls');
let lb = id ? document.getElementById(id) : null;
if (!lb) lb = document.querySelector('[role="listbox"][data-state="open"]')
          || document.querySelector('[role="listbox"]');
if (!lb) return null;
const opts = [...lb.querySelectorAll(sel)];
if (!opts.length) return null;
const norm = (s) => (s || '').trim().toLowerCase();

// 1) data-value (phòng bản Radix khác) rồi tới text: khớp hẳn -> bỏ số 0 đầu
//    -> bắt đầu bằng ("Jan" phải ăn được option ghi "January").
for (const w of wants) {
    const nw = norm(w);
    for (const o of opts) if (norm(o.getAttribute('data-value')) === nw) return o;
    for (const o of opts) if (norm(o.innerText) === nw) return o;
    for (const o of opts) if (norm(o.innerText).replace(/^0+/, '') === nw.replace(/^0+/, '')) return o;
    for (const o of opts) if (norm(o.innerText).startsWith(nw)) return o;
}

// 2) KHỚP THEO VỊ TRÍ - đường cứu khi trang đổi ngôn ngữ.
//    Roblox trả trang theo IP nên gặp /vi/CreateAccount là tháng ghi
//    "Tháng 1" chứ không phải "January" -> mọi cách so chữ ở trên đều trượt.
//    Radix luôn kèm 1 <select> ẩn giữ value THẬT ("Jan"/"01"/"1999") và item
//    trong listbox xếp ĐÚNG THỨ TỰ option của select đó -> lấy theo index là
//    xong, khỏi quan tâm ngôn ngữ.
const wrap = document.getElementById(wrapId);
const hidden = wrap ? wrap.querySelector('select') : null;
if (hidden) {
    const real = [...hidden.options].filter(o => o.value !== '');
    const idx = real.findIndex(o => o.value === value);
    if (idx >= 0 && idx < opts.length) return opts[idx];
}
return null;
"""


def _pick_combobox(driver, el_id: str, value: str, label: str,
                   wants: list[str]) -> bool:
    """Chọn 1 giá trị trên dropdown Radix của form MỚI (button role=combobox).

    Radix không phải <select>: phải BẤM mở, chờ listbox render (thường ở portal
    cuối body), rồi bấm đúng [role=option]. Danh sách năm dài nên phải
    scrollIntoView option trước khi bấm, không thì bấm trượt ra ngoài viewport.
    """
    trigger = find_first(driver, [
        f'#{el_id} [role="combobox"]',
        f'[data-testid="birthday-{label.lower()}"] [role="combobox"]',
        f'[role="combobox"][aria-label="{label}"]',
    ], timeout=5)
    if trigger is None:
        print(f"[!] {label}: không thấy combobox của form mới.")
        return False

    # 5 lượt, mỗi lượt chờ listbox tới 6s: dropdown Năm có 95 option nên render
    # chậm hẳn so với Tháng/Ngày - đo thực tế có lần phải tới lượt thứ 3.
    for attempt in range(1, 6):
        try:
            real_click(driver, trigger)
        except Exception:
            try:
                trigger.click()
            except Exception:
                pass

        def _count(rounds: int) -> int:
            """Chờ listbox render (qua portal nên hơi trễ). Trả số option."""
            for _ in range(rounds):
                try:
                    k = driver.execute_script(
                        _OPEN_LISTBOX_JS, trigger, _OPT_SEL) or 0
                except Exception:
                    k = 0
                if k:
                    return k
                time.sleep(0.15)
            return 0

        n = _count(40)
        if not n:
            # real_click NUỐT hết lỗi CDP nên nó KHÔNG BAO GIỜ raise -> không
            # thể dựa vào try/except để biết chuột có bấm thật không. Phải tự
            # kiểm listbox rồi bấm lại bằng đường khác, nếu không sẽ lặp đủ 5
            # lượt mà chẳng bấm được cái nào.
            for fallback in (lambda: trigger.click(),
                             lambda: driver.execute_script(
                                 "arguments[0].click();", trigger)):
                try:
                    fallback()
                except Exception:
                    continue
                n = _count(14)
                if n:
                    break
        if not n:
            print(f"    [!] {label}: mở lần {attempt} chưa thấy option, thử lại")
            continue

        try:
            opt = driver.execute_script(_PICK_OPTION_JS, trigger, wants,
                                        _OPT_SEL, el_id, value)
        except Exception:
            opt = None
        if opt is None:
            print(f"    [!] {label}: có {n} option nhưng không khớp {wants}")
            continue

        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", opt)
            time.sleep(random.uniform(0.1, 0.25))
            real_click(driver, opt)
        except Exception:
            try:
                opt.click()
            except Exception:
                pass

        time.sleep(random.uniform(0.2, 0.45))
        # chọn xong Radix bỏ data-placeholder và ghi text vào trigger
        try:
            ok = driver.execute_script(
                "const t=arguments[0];"
                "return !t.hasAttribute('data-placeholder') &&"
                "       (t.innerText||'').trim().length > 0;", trigger)
        except Exception:
            ok = False
        if ok:
            return True
        print(f"    [!] {label}: bấm xong mà chưa thấy đổi, thử lại {attempt}")

    print(f"[!] {label}: không chọn được '{value}' trên form mới.")
    return False


def _select_dropdown(driver, wait, el_id: str, value: str, label: str) -> bool:
    """Chọn 1 dropdown ngày sinh. Tự nhận form cũ (<select>) hay mới (Radix)."""
    # Form mới dùng LẠI id cũ nhưng là <div> -> phải xem tag thật mới biết.
    try:
        tag = driver.execute_script(
            "const e = document.getElementById(arguments[0]);"
            "return e ? e.tagName.toLowerCase() : null;", el_id)
    except Exception:
        tag = None

    if tag != "select":
        wants = [value]
        if label == "Month":
            wants.append(MONTH_FULL.get(value, value))
        elif label == "Day":
            wants.append(value.lstrip("0") or value)
        return _pick_combobox(driver, el_id, value, label, wants)

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
# USERNAME: tạo random -> lọc offline -> check API -> gõ -> KIỂM TRA LẠI
# ==========================================================================
# Số tên tối đa thử trong 1 lần điền (mỗi tên = 1 lần gọi API).
USERNAME_MAX_TRIES = 40
# Gõ tên vào ô mà giá trị trong ô SAI thì gõ lại tối đa mấy lần.
USERNAME_TYPE_RETRIES = 3
# Số vòng "gõ xong -> Roblox báo lỗi -> đổi tên khác gõ lại".
USERNAME_ROUNDS = 4
# API lỗi mạng/timeout thì thử lại CÙNG tên đó mấy lần (không phí tên).
USERNAME_API_RETRIES = 2
# Chờ Roblox validate xong sau khi blur (giây) trước khi kết luận "sạch".
USERNAME_ERROR_WAIT = 3.0
# TRẦN THỜI GIAN cho CẢ khâu tìm + điền username (giây). Không có nó thì worst
# case = 40 tên x 3 lần gọi x 15s timeout x 4 vòng ~ 2 TIẾNG treo 1 tab.
USERNAME_TIME_BUDGET = 90.0
# TRẦN THỜI GIAN cho cả 1 acc, tính từ lúc mở trang tới lúc bấm gửi form.
# Quá giờ -> tắt tab làm acc khác, KHÔNG để tab treo vô hạn.
ACCOUNT_FILL_BUDGET = 240.0

# Mã trả về của username_validate()
UV_OK = 0          # hợp lệ, chưa ai dùng
UV_TAKEN = 1       # đã có người dùng
UV_FILTERED = 2    # bị lọc (không phù hợp)
UV_LENGTH = 3      # sai độ dài / ký tự
UV_TRANSIENT = -1  # lỗi mạng/timeout/429 -> thử LẠI cùng tên
UV_BADREQ = -2     # birthday sai -> hỏng luồng, dừng luôn cho nhanh

# Roblox: 3..20 ký tự, chữ/số, tối đa 1 gạch dưới, không ở đầu/cuối.
_RE_USERNAME_OK = re.compile(r"^(?=.{3,20}$)[A-Za-z0-9]+(?:_[A-Za-z0-9]+)?$")


def username_locally_valid(name: str) -> bool:
    """Lọc OFFLINE trước khi tốn 1 lần gọi API.

    Chỉ chặn thứ Roblox chắc chắn từ chối (độ dài / ký tự / gạch dưới).
    Tên qua được hàm này VẪN có thể bị API từ chối (đã dùng / bị lọc).
    """
    return bool(name) and bool(_RE_USERNAME_OK.match(name))


def random_username() -> str:
    """Tạo username 10..14 ký tự, ĐỌC ĐƯỢC (không phải tên người).

    Ghép các âm tiết phụ-âm + nguyên-âm phát âm được rồi thêm số ở cuối.
    """
    consonants = "bcdfghjklmnprstvwz"
    vowels = "aeiou"

    # Ghép 3-4 âm tiết cho đủ dài (mỗi âm tiết ~2-3 ký tự)
    syllables = random.randint(3, 4)
    name = ""
    for _ in range(syllables):
        name += random.choice(consonants) + random.choice(vowels)
        if random.random() < 0.35:
            name += random.choice(consonants)

    # Chữ hoa/thường ngẫu nhiên (đa số thường cho dễ đọc)
    name = "".join(c.upper() if random.random() < 0.25 else c for c in name)

    # Thêm 2-3 chữ số ở cuối
    name += "".join(random.choice("0123456789")
                    for _ in range(random.randint(2, 3)))

    # Đảm bảo độ dài 10..14
    while len(name) < 10:
        name += random.choice(vowels) + random.choice("0123456789")
    if len(name) > 14:
        name = name[:14]
    return name


_UV_JS = """
const done = arguments[arguments.length - 1];
const u = arguments[0], b = arguments[1];
const loc = (location.pathname.match(/^\\/([a-z]{2})\\//) || [])[1] || 'en';
const urlLocale = loc === 'vi' ? 'vi_vn' : 'en_us';
const url = 'https://auth.roblox.com/v1/usernames/validate'
    + '?request.username=' + encodeURIComponent(u)
    + '&request.birthday=' + encodeURIComponent(b)
    + '&request.context=Signup'
    + '&urlLocale=' + urlLocale;
fetch(url, {credentials: 'include', headers: {'accept':'application/json'}})
    .then(r => r.text().then(t => done(r.status + '|' + t)))
    .catch(e => done('0|ERR:' + e));
"""


def _uv_once(driver, username: str, birthday: str) -> tuple[int, str]:
    """1 lần gọi API validate. Trả (mã UV_*, thông báo của Roblox)."""
    try:
        driver.set_script_timeout(15)
        raw = driver.execute_async_script(_UV_JS, username, birthday)
    except Exception as exc:
        return UV_TRANSIENT, f"script lỗi: {exc}"

    if not raw or "|" not in raw:
        return UV_TRANSIENT, "không có phản hồi"
    status_txt, _, body = raw.partition("|")
    try:
        status = int(status_txt)
    except ValueError:
        status = 0

    # Mạng hỏng / bị chặn / rate-limit / server lỗi -> thử lại CÙNG tên.
    if status == 0 or status == 429 or status >= 500:
        return UV_TRANSIENT, f"HTTP {status}"

    try:
        data = json.loads(body)
    except Exception:
        return UV_TRANSIENT, f"HTTP {status}, body không phải JSON"

    # Dạng lỗi: {"errors":[{"code":2,"message":"A valid birthday ... required."}]}
    # KHÁC hẳn dạng thường {"code":0,...} -> phải tách, không thì hiểu nhầm
    # thành "tên đã dùng" và đốt sạch 40 tên.
    errs = data.get("errors")
    if isinstance(errs, list) and errs:
        msg = str(errs[0].get("message", "")) or f"HTTP {status}"
        return UV_BADREQ, msg

    code = data.get("code")
    if code is None:
        return UV_TRANSIENT, f"HTTP {status}, thiếu field code"
    return int(code), str(data.get("message", ""))


def username_validate(driver, username: str,
                      birthday: str) -> tuple[int, str]:
    """Hỏi Roblox xem tên dùng được không (KHÔNG nhập gì vào form).

    Trả (mã UV_*, thông báo). Gọi y hệt form thật (cùng birthday, context
    Signup, cùng locale) nên kết quả khớp với lúc bấm Sign Up.

    Lỗi mạng/timeout/429 KHÔNG tính là "tên xấu": thử lại chính tên đó
    USERNAME_API_RETRIES lần rồi mới bỏ cuộc.
    """
    code, msg = UV_TRANSIENT, "chưa gọi"
    for i in range(USERNAME_API_RETRIES + 1):
        code, msg = _uv_once(driver, username, birthday)
        if code != UV_TRANSIENT:
            return code, msg
        if i < USERNAME_API_RETRIES:
            time.sleep(random.uniform(0.6, 1.4) * (i + 1))
    return code, msg


_last_mouse = {}   # nhớ vị trí chuột cuối theo id(driver) để di mượt


def _elem_center(driver, element) -> tuple[float, float]:
    """Toạ độ tâm element trên viewport (px), có lệch ngẫu nhiên như người thật."""
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center',inline:'center'});", element)
    time.sleep(random.uniform(0.15, 0.4))
    r = driver.execute_script(
        "const b=arguments[0].getBoundingClientRect();"
        "return {x:b.left, y:b.top, w:b.width, h:b.height};", element)
    # không click chính giữa mà lệch nhẹ (người thật không bao giờ trúng tâm)
    px = r["x"] + r["w"] * random.uniform(0.35, 0.65)
    py = r["y"] + r["h"] * random.uniform(0.35, 0.65)
    return px, py


def real_move_to(driver, px, py) -> None:
    """Di chuột tới (px,py) theo đường cong Bezier mượt (chuột thật qua CDP)."""
    did = id(driver)
    w = int(driver.execute_script("return window.innerWidth") or 1000)
    h = int(driver.execute_script("return window.innerHeight") or 800)
    cx, cy = _last_mouse.get(did, (w // 2, h // 2))
    ctrlx = (cx + px) / 2 + random.randint(-80, 80)
    ctrly = (cy + py) / 2 + random.randint(-80, 80)
    steps = random.randint(12, 26)
    for i in range(1, steps + 1):
        t = i / steps
        mx = (1-t)**2*cx + 2*(1-t)*t*ctrlx + t*t*px
        my = (1-t)**2*cy + 2*(1-t)*t*ctrly + t*t*py
        try:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                                   {"type": "mouseMoved", "x": mx, "y": my})
        except Exception:
            pass
        time.sleep(random.uniform(0.004, 0.02))
    _last_mouse[did] = (px, py)


def real_click(driver, element) -> None:
    """Di chuột tới element rồi NHẤN CHUỘT THẬT (không phải element.click())."""
    px, py = _elem_center(driver, element)
    real_move_to(driver, px, py)
    time.sleep(random.uniform(0.05, 0.2))          # dừng nhẹ trước khi bấm
    for tp in ("mousePressed", "mouseReleased"):
        try:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": tp, "x": px, "y": py,
                "button": "left", "clickCount": 1,
            })
        except Exception:
            pass
        time.sleep(random.uniform(0.03, 0.09))     # thời gian giữ nút


def real_type(driver, text: str) -> None:
    """Gõ từng ký tự bằng KEY EVENT THẬT qua CDP (không dùng send_keys tổng hợp).

    Nhịp gõ biến thiên, thỉnh thoảng khựng như người đang nghĩ.
    """
    for ch in text:
        try:
            driver.execute_cdp_cmd("Input.dispatchKeyEvent",
                                   {"type": "keyDown", "text": ch})
            driver.execute_cdp_cmd("Input.dispatchKeyEvent",
                                   {"type": "keyUp", "text": ch})
        except Exception:
            pass
        if FAST_MODE:
            time.sleep(random.uniform(0.01, 0.04))  # gõ nhanh
            continue
        d = random.uniform(0.06, 0.19)
        if random.random() < 0.14:                 # thỉnh thoảng khựng
            d += random.uniform(0.25, 0.7)
        time.sleep(d)


def type_slowly(element, text: str) -> None:
    """(giữ tương thích) gõ từng ký tự bằng send_keys với nhịp người."""
    for ch in text:
        element.send_keys(ch)
        d = random.uniform(0.05, 0.18)
        if random.random() < 0.12:
            d += random.uniform(0.2, 0.5)
        time.sleep(d)


def human_mouse_wiggle(driver, moves: int = 6) -> None:
    """Di chuột theo ĐƯỜNG CONG mượt (Bezier) như người thật -> tăng trust Arkose.

    Con người không nhảy thẳng từ điểm A -> B; Arkose theo dõi quỹ đạo chuột.
    """
    try:
        w = int(driver.execute_script("return window.innerWidth") or 500)
        h = int(driver.execute_script("return window.innerHeight") or 500)
        did = id(driver)
        cx, cy = _last_mouse.get(did, (w // 2, h // 2))

        for _ in range(moves):
            tx = random.randint(5, w - 5)
            ty = random.randint(5, h - 5)
            # điểm điều khiển cho đường cong Bezier bậc 2
            ctrlx = (cx + tx) / 2 + random.randint(-60, 60)
            ctrly = (cy + ty) / 2 + random.randint(-60, 60)
            steps = random.randint(8, 18)
            for i in range(1, steps + 1):
                t = i / steps
                # nội suy Bezier bậc 2
                px = (1-t)**2*cx + 2*(1-t)*t*ctrlx + t*t*tx
                py = (1-t)**2*cy + 2*(1-t)*t*ctrly + t*t*ty
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": px, "y": py,
                })
                time.sleep(random.uniform(0.004, 0.02))
            cx, cy = tx, ty
            time.sleep(random.uniform(0.05, 0.25))
        _last_mouse[did] = (cx, cy)
    except Exception:
        pass


def warm_up_page(driver) -> None:
    """'Sống' trên trang một lúc trước khi điền form: cuộn, di chuột, dừng đọc.

    Arkose cộng điểm trust theo thời gian tương tác & hành vi tự nhiên trên trang.
    Vào thẳng đăng ký ngay lập tức = dấu hiệu bot rõ ràng.
    """
    try:
        # dừng "đọc" trang vài giây
        time.sleep(random.uniform(1.5, 3.0))
        human_mouse_wiggle(driver, random.randint(4, 7))
        # cuộn xuống rồi lên như đang xem trang
        for _ in range(random.randint(2, 4)):
            driver.execute_script(
                f"window.scrollBy(0, {random.randint(120, 400)});")
            time.sleep(random.uniform(0.4, 1.1))
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(random.uniform(0.5, 1.2))
        human_mouse_wiggle(driver, random.randint(3, 6))
    except Exception:
        pass


def clear_field(driver, box) -> None:
    """XÓA SẠCH ô input React (Ctrl+A + Delete + set value rỗng + dispatch event).

    box.clear() thường KHÔNG xóa được ô React -> gõ tên mới bị nối vào tên cũ.
    """
    try:
        # 1) chọn hết rồi xóa bằng phím (React nhận)
        box.send_keys(Keys.CONTROL, "a")
        time.sleep(0.05)
        box.send_keys(Keys.DELETE)
        time.sleep(0.05)
    except Exception:
        pass
    try:
        # 2) ép value rỗng + dispatch input/change để React cập nhật state
        driver.execute_script("""
            const el = arguments[0];
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, '');
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
        """, box)
    except Exception:
        pass


def _find_free_username(driver, birthday: str, tried: set[str],
                        deadline: float) -> str | None:
    """Quay số tên tới khi API xác nhận CHƯA DÙNG (UV_OK). None nếu chịu thua."""
    for attempt in range(1, USERNAME_MAX_TRIES + 1):
        if time.time() > deadline:
            print(f"[!] Hết giờ tìm username ({USERNAME_TIME_BUDGET:.0f}s).")
            return None
        name = random_username()
        if name in tried:
            continue
        tried.add(name)

        # Lọc offline trước -> khỏi tốn 1 vòng mạng cho tên chắc chắn hỏng.
        if not username_locally_valid(name):
            print(f"    #{attempt} '{name}' -> sai định dạng (lọc offline)")
            continue

        code, msg = username_validate(driver, name, birthday)
        if code == UV_OK:
            print(f"[*] Username '{name}' CHƯA DÙNG -> chọn.")
            return name
        if code == UV_BADREQ:
            # Birthday hỏng thì tên nào cũng trượt -> dừng ngay, đừng đốt 40 tên.
            print(f"[!] API từ chối yêu cầu (birthday='{birthday}'): {msg}")
            return None
        print(f"    #{attempt} '{name}' -> code={code} ({msg}), thử tên khác")

    print(f"[!] Thử {USERNAME_MAX_TRIES} tên đều không dùng được.")
    return None


def _type_username(driver, box, name: str) -> bool:
    """Gõ name vào ô và ĐẢM BẢO giá trị trong ô đúng y hệt. True nếu khớp.

    Gõ bằng CDP đôi khi rơi ký tự đầu (ô chưa kịp focus) hoặc clear_field
    không ăn -> tên bị nối đôi. Cả 2 ca đều làm Roblox báo "không hợp lệ /
    đã sử dụng" dù API vừa bảo tên sạch. Nên phải đọc lại value mà so.
    """
    for attempt in range(1, USERNAME_TYPE_RETRIES + 1):
        real_click(driver, box)
        time.sleep(random.uniform(0.15, 0.35))
        clear_field(driver, box)
        time.sleep(random.uniform(0.05, 0.15))

        if attempt == 1:
            real_type(driver, name)          # gõ như người (giữ anti-detect)
        else:
            for ch in name:                  # dự phòng: send_keys từng ký tự
                box.send_keys(ch)
                time.sleep(random.uniform(0.03, 0.09))

        typed = driver.execute_script("return arguments[0].value;", box) or ""
        if typed == name:
            return True
        print(f"    [!] Gõ hụt lần {attempt}: ô đang là '{typed}' "
              f"thay vì '{name}' -> gõ lại")

    return False


def fill_username(driver, birthday: str) -> str | None:
    """Chọn username chưa dùng, gõ vào ô, rồi KIỂM TRA LẠI Roblox có chửi không.

    Vòng lặp mỗi lượt:
      1. API validate -> lấy tên code 0 (chưa dùng, không bị lọc)
      2. Gõ vào ô + đọc lại value cho chắc gõ đúng
      3. Validate LẠI đúng chuỗi đang nằm trong ô
      4. Blur -> chờ Roblox validate -> đọc lỗi dưới ô
    Sạch cả 4 bước mới trả về; dính lỗi thì đổi tên khác làm lại.
    """
    wait = WebDriverWait(driver, 20)
    try:
        box = wait.until(EC.element_to_be_clickable((By.ID, "signup-username")))
    except Exception:
        # dự phòng theo name nếu id đổi
        box = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "input[name='signupUsername'], input#signup-username")))

    tried: set[str] = set()
    deadline = time.time() + USERNAME_TIME_BUDGET
    for rnd in range(1, USERNAME_ROUNDS + 1):
        if time.time() > deadline:
            print(f"[!] Hết giờ khâu username ({USERNAME_TIME_BUDGET:.0f}s)"
                  f" ở vòng {rnd}.")
            return None
        chosen = _find_free_username(driver, birthday, tried, deadline)
        if not chosen:
            return None

        if not _type_username(driver, box, chosen):
            print(f"[!] Vòng {rnd}: không gõ nổi '{chosen}' vào ô -> đổi tên.")
            continue

        # Kiểm tra ĐÚNG chuỗi trong ô (không phải chuỗi mình định gõ).
        actual = driver.execute_script("return arguments[0].value;", box) or ""
        code, msg = username_validate(driver, actual, birthday)
        if code != UV_OK:
            print(f"[!] Vòng {rnd}: '{actual}' trong ô bị API từ chối "
                  f"(code={code} {msg}) -> đổi tên.")
            continue

        # Bắt Roblox tự validate rồi đọc lỗi nó hiện dưới ô.
        _blur_field(driver, "signup-username")
        err = wait_username_error(driver)
        if err:
            print(f"[!] Vòng {rnd}: Roblox báo lỗi ô username: {err} -> đổi tên.")
            continue

        print(f"[*] Đã điền username: {chosen} (API sạch + form không báo lỗi)")
        return chosen

    print(f"[!] {USERNAME_ROUNDS} vòng đều bị Roblox từ chối username.")
    return None


_USERNAME_ERR_JS = r"""
const el = document.getElementById('signup-username')
        || document.querySelector("input[name='signupUsername']");
if (!el) return '';

// CHỈ đọc lỗi THUỘC VỀ ô username. Quét cả trang sẽ dính chữ gợi ý của ô
// mật khẩu ("... ít nhất 8 ký tự") -> lúc nào cũng tưởng lỗi.
const cands = [];

// 1) phần tử validation riêng của field (Roblox đặt id theo mẫu này)
['signup-usernameInputValidation', 'signup-usernameValidation'].forEach(id => {
    const n = document.getElementById(id);
    if (n) cands.push(n);
});

// 2) phần tử mà chính input trỏ tới qua aria-describedby / aria-errormessage
['aria-describedby', 'aria-errormessage'].forEach(a => {
    (el.getAttribute(a) || '').split(/\s+/).forEach(id => {
        const n = id && document.getElementById(id);
        if (n) cands.push(n);
    });
});

// 3) leo lên .form-group gần nhất rồi lấy text lỗi TRONG khối đó thôi
// form MỚI không có .form-group cũng không có *InputValidation; lỗi nằm
// trong [data-testid=text-input-wrapper] và input được gắn aria-invalid.
let box = el.closest('.form-group, .form-field, .input-group,'
                   + '[data-testid="text-input-wrapper"]');
if (!box) { box = el; for (let i = 0; i < 4 && box.parentElement; i++) box = box.parentElement; }
if (box) {
    box.querySelectorAll(
        '[class*="error" i],[class*="validation" i],[class*="invalid" i],'
        + '.text-error,.form-control-label,small,span,p').forEach(n => {
        if (n.children.length === 0) cands.push(n);
    });
}

let texts = [];
cands.forEach(n => {
    // bỏ qua phần tử đang bị ẩn (Roblox render sẵn rồi mới hiện)
    const cs = window.getComputedStyle(n);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    const t = (n.innerText || n.textContent || '').trim();
    if (t) texts.push(t);
});

const patterns = [
  'đã được sử dụng', 'đã dùng', 'already in use', 'is taken', 'already taken',
  'not appropriate', 'không phù hợp', 'không hợp lệ', 'inappropriate',
  'có thể dài từ', 'must be between', 'can be 3 to 20', 'ký tự',
  'letters, numbers', 'chữ và số', 'try another', 'thử tên khác'
];
for (const t of texts) {
    const low = t.toLowerCase();
    for (const p of patterns) { if (low.includes(p)) return t; }
}

// 4) không khớp chữ nào nhưng input bị đánh dấu invalid -> vẫn coi là lỗi
if (el.getAttribute('aria-invalid') === 'true'
    || /\b(error|invalid)\b/i.test(el.className)) {
    return texts.length ? texts[0] : 'field bị đánh dấu invalid';
}
return '';
"""


def username_error_text(driver) -> str:
    """Text lỗi Roblox đang hiện DƯỚI Ô USERNAME ('' nếu không có lỗi)."""
    try:
        return (driver.execute_script(_USERNAME_ERR_JS) or "").strip()
    except Exception:
        return ""


def wait_username_error(driver, timeout: float = USERNAME_ERROR_WAIT) -> str:
    """Chờ Roblox validate xong sau khi blur rồi đọc lỗi.

    Trả text lỗi ngay khi lỗi hiện ra; '' nếu hết giờ mà vẫn sạch.
    Roblox validate bằng 1 call mạng nên lỗi hiện TRỄ vài trăm ms -> đọc
    ngay lập tức sẽ luôn thấy "sạch" rồi bấm Sign Up và toang.
    """
    deadline = time.time() + timeout
    while True:
        err = username_error_text(driver)
        if err:
            return err
        if time.time() >= deadline:
            return ""
        time.sleep(0.2)


def username_field_has_error(driver) -> bool:
    """(giữ tương thích) Có lỗi dưới ô username không."""
    return bool(username_error_text(driver))


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
    real_click(driver, box)                      # di chuột + click chuột THẬT
    time.sleep(random.uniform(0.2, 0.5))
    real_type(driver, pwd)                       # gõ bằng key event THẬT
    typed = driver.execute_script("return arguments[0].value;", box)
    if typed == pwd:
        print("[*] Đã điền mật khẩu xong.")
        return pwd
    # dự phòng nếu React chưa nhận
    box.clear()
    box.send_keys(pwd)
    if driver.execute_script("return arguments[0].value;", box) == pwd:
        return pwd
    print(f"[!] Mật khẩu vào ô không khớp.")
    return None


# ==========================================================================
# GENDER: random chọn Nam / Nữ (2 nút)
# ==========================================================================
_GENDER_V2_JS = r"""
// Form mới: nút giới tính KHÔNG có id, chỉ có chữ Female/Male + icon
// .icon-regular-head-male|female. Chọn xong nút đổi aria-pressed="true".
const want = arguments[0];        // 'Male' | 'Female'
const words = arguments[1] || []; // các chữ đồng nghĩa theo ngôn ngữ trang
const icon = 'icon-regular-head-' + want.toLowerCase();
const ok = (b) => {
    const cs = getComputedStyle(b);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
};
const all = [...document.querySelectorAll('button')].filter(ok);

// ICON TRƯỚC: class icon-regular-head-male|female KHÔNG đổi theo ngôn ngữ,
// còn chữ thì trang /vi/ ghi "Nam"/"Nữ" -> so chữ tiếng Anh là trượt.
for (const b of all) if (b.querySelector('[class*="' + icon + '"]')) return b;
for (const b of all) {
    const t = (b.innerText || '').trim().toLowerCase();
    if (words.some(w => t === String(w).toLowerCase())) return b;
}
return null;
"""


def select_gender(driver) -> str | None:
    """Random chọn giới tính. Chạy được cả form cũ (id) lẫn form mới (text)."""
    choice = random.choice(["Nam", "Nữ"])
    en = "Male" if choice == "Nam" else "Female"
    print(f"[*] Chọn giới tính (random): {choice}")

    # --- form CŨ: có #MaleButton / #FemaleButton ---
    try:
        el = WebDriverWait(driver, 4).until(
            EC.element_to_be_clickable((By.ID, f"{en}Button")))
        real_click(driver, el)
        selected = driver.execute_script(
            "return arguments[0].querySelector('.gender-selected') !== null;", el)
        if not selected:
            el.click()
        print(f"[*] Đã chọn giới tính: {choice}")
        return choice
    except Exception:
        pass

    # --- form MỚI: tìm theo chữ/icon, xác nhận bằng aria-pressed ---
    try:
        words = (["Male", "Nam"] if en == "Male"
                 else ["Female", "Nữ", "Nu"])
        for attempt in range(1, 4):
            btn = driver.execute_script(_GENDER_V2_JS, en, words)
            if btn is None:
                print("[!] Không thấy nút giới tính nào.")
                return None
            real_click(driver, btn)
            time.sleep(random.uniform(0.25, 0.5))
            if driver.execute_script(
                    "return arguments[0].getAttribute('aria-pressed') === 'true';", btn):
                print(f"[*] Đã chọn giới tính: {choice}")
                return choice
            try:
                btn.click()                      # dự phòng
            except Exception:
                pass
            time.sleep(0.3)
            if driver.execute_script(
                    "return arguments[0].getAttribute('aria-pressed') === 'true';", btn):
                print(f"[*] Đã chọn giới tính: {choice}")
                return choice
            print(f"    [!] bấm giới tính lần {attempt} chưa ăn, thử lại")
    except Exception as exc:
        print(f"[!] Không chọn được giới tính: {exc}")
    return None


# ==========================================================================
# SIGN UP: bấm nút đăng ký
# ==========================================================================
# JS hook fetch + XHR để bắt response API /signup. Phải tiêm TRƯỚC khi trang
# tải (qua CDP addScriptToEvaluateOnNewDocument) mới bắt kịp, vì Roblox override
# fetch/XHR ngay khi load.
SNIFFER_JS = r"""
(function() {
    if (window.__snifferInstalled) return;
    window.__snifferInstalled = true;
    window.__signupResp = null;
    const isSignup = (u) => typeof u === 'string' && /\/v\d\/signup/i.test(u);
    const rec = (o) => { window.__signupResp = o; };

    const of = window.fetch;
    if (of) window.fetch = function(...a) {
        const url = (a[0] && a[0].url) || a[0];
        return of.apply(this, a).then(res => {
            if (isSignup(url)) {
                const hdrs = {};
                res.headers.forEach((v,k)=>hdrs[k]=v);
                res.clone().text().then(t => {
                    rec({kind:'fetch', status: res.status, body: t, url: String(url), headers: hdrs});
                }).catch(()=>{});
            }
            return res;
        }).catch(err => {
            if (isSignup(url)) rec({kind:'fetch-ERR', status: 0, body: String(err), url: String(url)});
            throw err;
        });
    };

    const oo = XMLHttpRequest.prototype.open;
    const os = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(m, u) {
        this.__u = u; this.__m = m; return oo.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
        const self = this;
        const grab = (ev) => {
            if (!isSignup(self.__u)) return;
            rec({kind:'xhr-'+ev, status: self.status, body: self.responseText,
                 url: String(self.__u), method: self.__m,
                 allHeaders: (function(){try{return self.getAllResponseHeaders();}catch(e){return '';}})()});
        };
        self.addEventListener('load', () => grab('load'));
        self.addEventListener('error', () => grab('error'));
        self.addEventListener('abort', () => grab('abort'));
        self.addEventListener('timeout', () => grab('timeout'));
        return os.apply(this, arguments);
    };
})();
"""


# JS hook fetch + XHR trong MỌI frame để đọc SỐ WAVE + VARIANT của FunCaptcha
# từ response /fc/gfct/ của Arkose. Tiêm qua BiDi addPreloadScript (áp cho cả
# iframe khác origin) nên chui được vào iframe Arkose; dữ liệu bắn về frame TOP
# bằng postMessage -> Python đọc window.__arkoseInfo ở frame chính.
ARKOSE_JS = r"""

(function () {
    'use strict';
    try {
        // Chỉ cài 1 LẦN cho MỖI frame. CDP addScriptToEvaluateOnNewDocument tiêm
        // vào mọi frame (kể cả iframe Arkose khác origin) -> phải có cờ chống cài lại.
        if (window.__arkHookInstalled) return;
        window.__arkHookInstalled = true;

        var TAG = '__ARK_SNIFF__';   // khoá riêng của tool, không đụng protocol Arkose
        var GT  = { 2: 'AudioMode', 3: 'TileGame', 4: 'MatchGame', 101: 'AudioGame' };

        // Kho dữ liệu của frame này. Ở frame TOP đây chính là chỗ Python đọc:
        //   driver.execute_script("return window.__arkoseInfo")
        var S = window.__arkoseInfo = {
            captcha:    false,  // đã thấy dấu hiệu Arkose chưa
            waves:      null,   // TỔNG số wave phải giải (game_data.waves)
            variant:    null,   // tên biến thể game (game_variant / instruction_string)
            gameType:   null,   // 2=AudioMode 3=TileGame 4=MatchGame 101=AudioGame
            gameName:   null,   // tên chữ của gameType
            waveSrc:    null,   // lấy waves từ nguồn nào (để chẩn đoán độ tin cậy)
            variantSrc: null,   // lấy variant từ nguồn nào
            answered:   0,      // số wave đã chấm (đếm response /fc/ca/)
            solved:     null,   // Arkose báo giải xong chưa
            token:      null,   // token /fc/gt2/ (chứa r=, meta=, at=, ag=, surl=)
            url:        null,   // url bắt được gfct
            raw:        null,   // 4000 ký tự đầu body gfct (chẩn đoán khi parse hỏng)
            ts:         0,      // mốc thời gian cập nhật cuối
            gfctCount:  0,      // số lần NẠP challenge (chỉ để xem log, KHÔNG
                                // suy ra đúng/sai: Arkose nạp lại vì nhiều lý do)
            log:        []      // lịch sử (1 challenge có thể load lại nhiều lần)
        };

        // ---------------------------------------------------------------- tiện ích
        function n(v)   { v = parseInt(v, 10); return (isFinite(v) && v > 0) ? v : null; }
        function s(v)   { return (typeof v === 'string' && v.length) ? v : null; }
        function len(v) { return (v && typeof v.length === 'number' && v.length > 0) ? v.length : null; }
        // DOM chỉ được ghi đè khi CHƯA có số liệu từ network (network luôn thắng)
        function domOk() { return S.waveSrc === null || S.waveSrc.slice(0, 4) === 'dom:'; }

        // Gộp dữ liệu mới vào kho rồi BẮN THẲNG lên frame TOP (qua được cross-origin)
        function push(p) {
            try {
                for (var k in p) { if (p[k] !== null && p[k] !== undefined) S[k] = p[k]; }
                S.captcha = true;
                S.ts = Date.now();
                S.log.push({ t: S.ts, w: S.waves, v: S.variant, g: S.gameType, src: S.waveSrc });
                if (S.log.length > 30) S.log.shift();
                if (window.top && window.top !== window) {
                    var m = {}; m[TAG] = 1; m.d = S;
                    window.top.postMessage(m, '*');
                }
            } catch (e) {}
        }

        // Nhận dữ liệu từ iframe con (kể cả khác origin) -> gộp vào kho frame này
        window.addEventListener('message', function (ev) {
            try {
                var d = ev.data;
                if (!d || typeof d !== 'object') return;

                // (a) message của CHÍNH TOOL -> gộp, và chuyển tiếp nếu đây là frame giữa
                if (d[TAG] === 1 && d.d) {
                    var o = d.d, ch = false;
                    for (var k in o) {
                        if (k === 'log') continue;
                        if (o[k] !== null && o[k] !== undefined && S[k] !== o[k]) { S[k] = o[k]; ch = true; }
                    }
                    S.captcha = true; S.ts = Date.now();
                    if (ch) {
                        S.log.push({ t: S.ts, w: S.waves, v: S.variant, g: S.gameType, src: S.waveSrc });
                        if (S.log.length > 30) S.log.shift();
                    }
                    if (window.top !== window) { try { window.top.postMessage(d, '*'); } catch (e2) {} }
                    return;
                }

                // (b) NGHE LỎM message nội bộ Arkose: game-core bắn
                //     parent.postMessage({msg:'game_type', data:<gameType>}, '*')
                //     lên frame enforcement -> hook của ta ở frame đó bắt được miễn phí.
                if (d.msg === 'game_type' && d.data !== undefined && S.gameType === null) {
                    var g = n(d.data);
                    if (g !== null) push({ gameType: g, gameName: GT[g] || ('type' + g) });
                }
            } catch (e) {}
        }, false);

        // -------------------------------------------------- phân loại URL cần bắt
        function kindOf(u) {
            try {
                if (!u) return null;
                u = String(u);
                // chặn sớm: URL không phải Arkose thì bỏ qua ngay, không tốn gì
                if (!/arkoselabs\.com|funcaptcha\.com|\/fc\//i.test(u)) return null;
                if (/\/fc\/gfct\b/i.test(u))            return 'gfct';  // <- nguồn chính
                if (/\/fc\/ca\b/i.test(u))              return 'ca';    // chấm đáp án
                if (/\/fc\/gt2\/public_key\//i.test(u)) return 'gt2';   // token phiên
                if (/\/fc\/(gc|api|a)\b/i.test(u))      return 'ping';  // chỉ đánh dấu có captcha
                return null;
            } catch (e) { return null; }
        }

        // ---------------- /fc/gfct/ : nguồn CHÍNH của waves + variant ----------------
        // Đối chiếu đúng cách ec-game-core tự đọc (đã dịch ngược bundle 1.22.0):
        //   totalRounds = game_data.waves ?? 1
        //   variant     = game_data.game_variant  (TileGame/AudioGame)
        //   variant     = game_data.instruction_string (MatchGame + nhánh default)
        function onGfct(o, txt, url) {
            var gd = (o && o.game_data) || {};
            var cg = gd.customGUI || {};
            var w = null, ws = null, v = null, vs = null, gt = n(gd.gameType);

            // WAVES - xếp theo độ tin cậy giảm dần
            if      ((w = n(gd.waves)) !== null)                      ws = 'game_data.waves';
            else if ((w = len(o && o.audio_challenge_urls)) !== null) ws = 'audio_challenge_urls.length';
            else if ((w = len(cg._challenge_imgs)) !== null)          ws = 'customGUI._challenge_imgs.length';
            else if ((w = len(cg._challenge_layouts)) !== null)       ws = 'customGUI._challenge_layouts.length';

            // VARIANT - xếp theo độ tin cậy giảm dần
            if      ((v = s(gd.game_variant)) !== null)               vs = 'game_data.game_variant';
            else if ((v = s(gd.instruction_string)) !== null)         vs = 'game_data.instruction_string';
            else {
                // string_table có key dạng "4.instructions-hopscotch_highsec"
                try {
                    var st = (o && o.string_table) || {};
                    for (var k in st) {
                        var km = /^(?:\d+|audio_game)\.instructions-(.+)$/.exec(k);
                        if (km) { v = km[1]; vs = 'string_table:' + k; break; }
                    }
                } catch (e) {}
            }

            // Body không parse được JSON (bọc / obscure mode) -> quét thô bằng regex
            if (w === null || v === null || gt === null) {
                try {
                    var m1;
                    if (w  === null && (m1 = /"waves"\s*:\s*(\d+)/.exec(txt)))                  { w  = n(m1[1]); ws = 'regex:waves'; }
                    if (v  === null && (m1 = /"game_variant"\s*:\s*"([^"]+)"/.exec(txt)))       { v  = m1[1];    vs = 'regex:game_variant'; }
                    if (v  === null && (m1 = /"instruction_string"\s*:\s*"([^"]+)"/.exec(txt))) { v  = m1[1];    vs = 'regex:instruction_string'; }
                    if (gt === null && (m1 = /"gameType"\s*:\s*(\d+)/.exec(txt)))               { gt = n(m1[1]); }
                } catch (e) {}
            }

            // game-core mặc định totalRounds = 1 khi thiếu waves -> bắt chước y hệt
            if (w === null && gt !== null) { w = 1; ws = 'default:game-core=1'; }

            push({
                waves: w, waveSrc: ws, variant: v, variantSrc: vs,
                gameType: gt, gameName: (gt !== null ? (GT[gt] || ('type' + gt)) : null),
                url: String(url || ''),
                answered: 0,                                   // challenge mới -> reset bộ đếm
                gfctCount: (S.gfctCount || 0) + 1,             // lần nạp thứ mấy
                raw: String(txt == null ? '' : txt).slice(0, 4000)
            });
        }

        // ---- /fc/ca/ : mỗi response = 1 wave đã chấm -> chặn dưới cho waves + tiến độ
        function onCa(o, txt) {
            try {
                var sv = null;
                if (o && typeof o.solved === 'boolean')     sv = o.solved;
                else if (/"solved"\s*:\s*true/.test(txt))   sv = true;
                else if (/"solved"\s*:\s*false/.test(txt))  sv = false;

                var p = { answered: (S.answered || 0) + 1 };
                if (sv !== null) p.solved = sv;
                // KHÔNG suy ra "trả lời sai" từ body /fc/ca/ nữa. Body luôn chứa
                // các field đếm kiểu "wrong":0 / "incorrect_tries" nên mọi cách
                // dò chữ đều báo sai cả khi giải ĐÚNG. solved===false cũng vô
                // dụng: giữa chừng challenge nó luôn false, chỉ wave cuối mới true.
                if (S.waves === null || S.waves < p.answered) { p.waves = p.answered; p.waveSrc = 'count:/fc/ca/'; }
                push(p);
            } catch (e) {}
        }

        // ---- /fc/gt2/public_key/ : token phiên (r=, meta=, at=, ag=, surl=) ----
        function onGt2(o, txt) {
            try {
                var tk = (o && typeof o.token === 'string') ? o.token : null;
                if (!tk) { var m = /"token"\s*:\s*"([^"]+)"/.exec(txt); tk = m ? m[1] : null; }
                if (tk) push({ token: String(tk).slice(0, 400) });
            } catch (e) {}
        }

        function handle(kind, txt, url) {
            try {
                if (!kind) return;
                if (kind === 'ping') { push({}); return; }     // chỉ đánh dấu "đang có captcha"
                if (!txt) return;
                var o = null;
                try { o = JSON.parse(txt); } catch (e) { o = null; }
                if      (kind === 'gfct') onGfct(o, txt, url);
                else if (kind === 'ca')   onCa(o, txt);
                else if (kind === 'gt2')  onGt2(o, txt);
            } catch (e) {}
        }

        // -------------------------------------------------------------- hook fetch
        // Dùng Proxy: Function.prototype.toString trên Proxy vẫn trả [native code]
        // -> khó bị Arkose soi ra là đã bị vá. Trả về ĐÚNG promise gốc, chỉ quan sát
        // trên một nhánh .then() riêng và chỉ đọc BẢN CLONE -> không đụng body của trang.
        try {
            var _fetch = window.fetch;
            if (typeof _fetch === 'function' && typeof Proxy === 'function') {
                window.fetch = new Proxy(_fetch, {
                    apply: function (t, self, a) {
                        var p = Reflect.apply(t, self, a);     // gọi nguyên bản TRƯỚC
                        try {
                            var a0 = a[0];
                            var u = (a0 && typeof a0 === 'object' && a0.url)
                                  ? a0.url : String(a0 == null ? '' : a0);
                            var kind = kindOf(u);
                            if (kind && p && typeof p.then === 'function') {
                                p.then(function (res) {
                                    try {
                                        res.clone().text().then(
                                            function (tx) { handle(kind, tx, u); },
                                            function () {}
                                        );
                                    } catch (e) {}
                                }, function () {});            // nuốt lỗi, không tạo unhandled rejection
                            }
                        } catch (e) {}
                        return p;                              // response đi qua NGUYÊN VẸN
                    }
                });
            }
        } catch (e) {}

        // ---------------------------------------------------------------- hook XHR
        // Arkose dùng cả axios/XHR (chế độ LiteJS) nên bắt buộc hook cả hai đường.
        // Dùng addEventListener('load') -> KHÔNG giẫm lên onload/onreadystatechange của trang.
        try {
            var XP = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
            if (XP && typeof Proxy === 'function') {
                var _open = XP.open, _send = XP.send;

                XP.open = new Proxy(_open, {
                    apply: function (t, self, a) {
                        try {
                            self.__arkU = String(a[1] == null ? '' : a[1]);
                            self.__arkK = kindOf(self.__arkU);
                        } catch (e) {}
                        return Reflect.apply(t, self, a);
                    }
                });

                XP.send = new Proxy(_send, {
                    apply: function (t, self, a) {
                        try {
                            if (self.__arkK && !self.__arkB) {
                                self.__arkB = true;            // 1 request chỉ gắn 1 listener
                                self.addEventListener('load', function () {
                                    try {
                                        var tx = null, rt = '';
                                        try { rt = self.responseType || ''; } catch (e) {}
                                        try {
                                            // responseText NÉM LỖI khi responseType là json/blob/arraybuffer
                                            if (rt === '' || rt === 'text')       tx = self.responseText;
                                            else if (rt === 'json')               tx = JSON.stringify(self.response);
                                            else if (typeof self.response === 'string') tx = self.response;
                                        } catch (e) { tx = null; }
                                        handle(self.__arkK, tx, self.__arkU);
                                    } catch (e) {}
                                }, false);
                            }
                        } catch (e) {}
                        return Reflect.apply(t, self, a);
                    }
                });
            }
        } catch (e) {}

        // ------------------------------------------- DỰ PHÒNG CUỐI: đọc DOM trong frame Arkose
        // Chỉ chạy trong frame Arkose (frame Roblox không tốn gì). Dùng khi hook bị
        // tiêm MUỘN và đã lỡ mất lần /fc/gfct/ của challenge đang hiển thị.
        try {
            if (/arkoselabs\.com|funcaptcha\.com/i.test(location.hostname)
                || /\/fc\//i.test(location.pathname)) {
                var ticks = 0;
                var iv = setInterval(function () {
                    try {
                        if (++ticks > 180) { clearInterval(iv); return; }   // tối đa ~3 phút
                        if (!document.body) return;

                        // game-core render ĐÚNG 1 ô .progress-section-container cho MỖI wave
                        if (domOk()) {
                            var secs = document.querySelectorAll('.progress-section-container').length;
                            if (secs > 0 && S.waves !== secs) {
                                push({ waves: secs, waveSrc: 'dom:.progress-section-container' });
                            }
                        }
                        // hoặc lấy từ dòng chữ hướng dẫn "... (1 of 6)"
                        if (domOk() && S.waves === null) {
                            var el = document.querySelector(
                                '.match-game .text, .challenge-instructions-container, #game_children_text');
                            var tx = el ? (el.textContent || '').trim() : '';
                            var mm = /\((?:\s*\d+\s*(?:of|\/)\s*)?(\d+)\s*\)\s*$/i.exec(tx);
                            if (mm) push({ waves: n(mm[1]), waveSrc: 'dom:text' });
                        }
                        // chỉ đoán được HỌ game qua selector, không ra tên variant
                        if (S.gameType === null) {
                            if (document.querySelector('.match-game')) {
                                push({ gameType: 4, gameName: 'MatchGame',
                                       variantSrc: S.variantSrc || 'dom:.match-game' });
                            } else if (document.querySelector(
                                       '#game_challengeItem_image, .challenge-instructions-container')) {
                                push({ gameType: 3, gameName: 'TileGame',
                                       variantSrc: S.variantSrc || 'dom:tile' });
                            }
                        }
                        if (S.waves !== null && S.variant !== null) clearInterval(iv);
                    } catch (e) {}
                }, 1000);
            }
        } catch (e) {}
    } catch (e) {}
})();
"""


def install_signup_sniffer(driver) -> None:
    """Tiêm lại sniffer (dự phòng, phần chính đã tiêm qua CDP lúc mở browser)."""
    try:
        driver.execute_script(SNIFFER_JS)
    except Exception:
        pass


def read_signup_response(driver):
    """Đọc response API signup đã bắt được (hoặc None)."""
    try:
        return driver.execute_script("return window.__signupResp;")
    except Exception:
        return None


def read_arkose_info(driver) -> dict:
    """Đọc thông tin FunCaptcha mà hook đã bắt (wave, variant, loại game...).

    Hook chạy trong iframe Arkose đã postMessage dữ liệu lên frame TOP, nên chỉ
    cần đọc ở frame chính, KHÔNG phải switch vào iframe (switch_to.frame lúc
    extension đang bấm dễ gây 'Something went wrong').

    Chỉ lấy các trường gọn - bỏ 'raw' (4KB) và 'log' để không kéo payload nặng
    qua mỗi vòng poll.
    """
    js = """
    const s = window.__arkoseInfo;
    if (!s) return null;
    return {captcha:s.captcha, waves:s.waves, variant:s.variant,
            gameType:s.gameType, gameName:s.gameName,
            waveSrc:s.waveSrc, variantSrc:s.variantSrc,
            answered:s.answered, solved:s.solved,
            gfctCount:s.gfctCount};
    """
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    try:
        d = driver.execute_script(js)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def arkose_wave_variant(driver) -> tuple[int | None, str | None]:
    """Trả (số wave, tên variant) của captcha hiện tại; None nếu chưa bắt được."""
    d = read_arkose_info(driver)
    w = d.get("waves")
    v = d.get("variant") or d.get("gameName")
    w = int(w) if isinstance(w, (int, float)) and w > 0 else None
    v = v if isinstance(v, str) and v else None
    return w, v


def _wait_enabled(driver, el, timeout: float = 20.0) -> bool:
    """Chờ nút hết disabled.

    Nút của form mới bị khoá cho tới khi Roblox validate XONG (username phải
    qua 1 lượt gọi mạng). Bấm sớm là click rơi vào hư không mà KHÔNG báo lỗi
    gì cả -> tưởng đã bấm rồi ngồi chờ mãi.
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            if not driver.execute_script("return !!arguments[0].disabled;", el):
                return True
        except Exception:
            return False
        time.sleep(0.3)
    return False


def click_signup(driver) -> bool:
    """Gửi form: form cũ = #signup-button, form mới = nút Continue."""
    try:
        install_signup_sniffer(driver)           # bắt response API để chẩn đoán
    except Exception:
        pass

    btn = find_first(driver, [
        "#signup-button",                                    # form cũ
        'button[type="submit"][aria-label="Continue"]',      # form mới 2 bước
        'button[type="submit"][aria-label="Create account"]',  # biến thể 1 bước
        # Chốt chặn cuối: nút submit BẤT KỲ trong thẻ đăng ký. Cần vì aria-label
        # bị dịch theo ngôn ngữ trang (/vi/ ghi "Tiếp tục"/"Tạo tài khoản").
        '[data-testid="signup-form-v2"] button[type="submit"]',
        'form button[type="submit"]',
    ], timeout=15)
    if btn is None:
        print("[!] Không thấy nút gửi form (Sign Up / Continue).")
        return False

    if not _wait_enabled(driver, btn, timeout=25):
        print("[!] Nút gửi form vẫn bị khoá sau 25s -> form chưa hợp lệ.")
        return False

    try:
        time.sleep(random.uniform(0.6, 1.4))     # do dự như người thật
        real_click(driver, btn)
        print("[*] Đã bấm nút gửi form.")
        return True
    except Exception as exc:
        print(f"[!] Không bấm được nút gửi form: {exc}")
        return False


def fill_password_v2(driver) -> str | None:
    """BƯỚC 2 của form mới: đặt mật khẩu rồi bấm 'Add password'.

    Luồng đã đo trên form thật:
      Continue -> panel bước 2 hiện (bước 1 bị giấu bằng visibility:hidden)
      -> gõ mật khẩu -> 'Add password' bỏ disabled -> bấm
      -> API signup trả 403 'Challenge is required' -> iframe Arkose hiện
      -> giải xong -> chuyển /home + có .ROBLOSECURITY.
    """
    box = find_first(driver, [
        "#signup-v2-password",
        '[data-testid="password-auth-method"] input[type="password"]',
        'input[autocomplete="new-password"]',
    ], timeout=25)
    if box is None:
        print("[!] Bước 2: không thấy ô mật khẩu.")
        return None

    pwd = random_password()
    print(f"[*] Bước 2 - điền mật khẩu: {pwd}")
    real_click(driver, box)
    time.sleep(random.uniform(0.2, 0.5))
    real_type(driver, pwd)
    if driver.execute_script("return arguments[0].value;", box) != pwd:
        clear_field(driver, box)
        box.send_keys(pwd)
    if driver.execute_script("return arguments[0].value;", box) != pwd:
        print("[!] Bước 2: mật khẩu vào ô không khớp.")
        return None

    btn = find_first(driver, [
        'button[aria-label="Add password"]',
        '[data-testid="password-auth-method"] button[type="submit"]',
    ], timeout=10)
    if btn is None:
        print("[!] Bước 2: không thấy nút 'Add password'.")
        return None
    if not _wait_enabled(driver, btn, timeout=25):
        print("[!] Bước 2: nút 'Add password' vẫn khoá sau 25s.")
        return None

    time.sleep(random.uniform(0.4, 1.0))
    real_click(driver, btn)
    print("[*] Đã bấm 'Add password'.")
    return pwd


def has_step1_password(driver) -> bool:
    """Trang có ô mật khẩu NGAY bước 1 không?

    Roblox đang chạy ÍT NHẤT 3 biến thể, nên đừng đoán theo tên form nữa mà
    hỏi thẳng DOM:
      - form cũ            : <select> + #signup-password  -> CÓ
      - form mới 2 bước    : Radix, mật khẩu ở bước 2     -> KHÔNG
      - form mới 1 bước    : Radix + #signup-password     -> CÓ
        (biến thể có nút "Sign in" ở header, nút gửi ghi "Create account")
    """
    return find_first(driver, [
        "#signup-password",
        'input[name="signupPassword"]',
    ]) is not None


def wait_step2(driver, timeout: float = 25.0) -> bool:
    """Chờ panel bước 2 của form mới hiện ra sau khi bấm Continue.

    Phải đo bằng computed visibility: Roblox giấu bước 1 bằng
    visibility:hidden, mà element kiểu đó VẪN có offsetWidth -> đo kích thước
    thì bước nào cũng thấy 'đang hiện'.
    """
    # Biến thể 1 bước KHÔNG có panel này trong DOM -> thoát ngay, khỏi phí 25s.
    try:
        if not driver.execute_script(
                'return !!document.querySelector('
                '"[data-testid=\'signup-v2-add-auth-method-panel\']")'
                ' || !!document.getElementById("signup-v2-password");'):
            return False
    except Exception:
        return False

    js = """
    const p = document.querySelector('[data-testid="signup-v2-add-auth-method-panel"]');
    const i = document.getElementById('signup-v2-password');
    if (!p || !i) return false;
    return getComputedStyle(p).visibility === 'visible'
        && getComputedStyle(i).visibility === 'visible';
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            if driver.execute_script(js):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


# ==========================================================================
# COOKIE / CAPTCHA / LƯU ACCOUNT
# ==========================================================================
ACCOUNTS_FILE = BASE_DIR / "account.txt"

# Captcha <= MAX_WAVES  -> để extension OMOcaptcha giải.
# Captcha >  MAX_WAVES  -> TẮT tab ngay, làm acc khác (đỡ đốt credit + thời gian).
# Đặt 0 để TẮT ngưỡng (giải mọi wave).
MAX_WAVES = 7

# ĐÃ BỎ cơ chế "giải sai -> tắt tab". Không có cách nào đọc được đúng/sai từ
# /fc/ca/: body luôn có field đếm ("wrong":0, incorrect_tries...) nên dò chữ
# báo SAI cả khi giải ĐÚNG, còn gfctCount>=2 thì Arkose nạp lại vì nhiều lý do
# chứ không riêng gì trả lời sai -> toàn tắt nhầm tab đang giải ngon.
# Giờ cứ để extension giải tới cùng; hết 3 phút không xong thì wait_login_and_save
# tự bỏ. Chặn tốn credit đã có ngưỡng MAX_WAVES ở trên lo.

# Thống kê wave để ĐO hiệu quả anti-detect: {số_wave: số_lần gặp}
_wave_stats: dict[int, int] = {}
_wave_lock = threading.Lock()


def record_wave(w: int) -> None:
    """Ghi nhận 1 lần gặp captcha w wave (để in thống kê cuối phiên)."""
    with _wave_lock:
        _wave_stats[w] = _wave_stats.get(w, 0) + 1


def wave_summary() -> str:
    """Chuỗi tóm tắt phân bố wave + trung bình (thấp hơn = anti-detect tốt hơn)."""
    with _wave_lock:
        if not _wave_stats:
            return "chưa gặp captcha nào"
        total = sum(_wave_stats.values())
        avg = sum(w * c for w, c in _wave_stats.items()) / total
        parts = [f"{w} wave x{c}" for w, c in sorted(_wave_stats.items())]
        return f"{', '.join(parts)}  |  TB={avg:.1f} wave / {total} captcha"


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

    ƯU TIÊN hook /fc/gfct/ (số wave CHÍNH XÁC, đọc từ game_data.waves ngay khi
    captcha vừa load). Chỉ khi hook chưa bắt được mới dò DOM như cũ (kém tin cậy:
    text '1 of N' chỉ hiện sau khi đã vào màn giải).
    """
    info = read_arkose_info(driver)
    if info.get("captcha"):
        w = info.get("waves")
        if isinstance(w, (int, float)) and w > 0:
            return int(w)
        return 0            # có captcha nhưng chưa đọc được số wave

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


def read_wave_in_frames(driver) -> int:
    """Đọc số wave bằng cách SWITCH vào từng iframe Arkose (đọc được cross-origin).

    Tìm text kiểu '1 of 3', '1/3'. Trả về TỔNG số wave (số sau), 0 nếu chưa thấy.
    """
    pat = re.compile(r"(\d+)\s*(?:of|/)\s*(\d+)", re.I)

    def read_here() -> int:
        try:
            body = driver.find_element(By.TAG_NAME, "body").text or ""
            m = pat.search(body)
            if m:
                return int(m.group(2))
        except Exception:
            pass
        return 0

    def recurse(depth: int) -> int:
        if depth > 4:
            return 0
        w = read_here()
        if w:
            return w
        try:
            frames = driver.find_elements(By.TAG_NAME, "iframe")
        except Exception:
            frames = []
        for fr in frames:
            try:
                driver.switch_to.frame(fr)
                w = recurse(depth + 1)
                driver.switch_to.parent_frame()
                if w:
                    return w
            except Exception:
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    pass
        return 0

    try:
        driver.switch_to.default_content()
        return recurse(0)
    except Exception:
        return 0
    finally:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass


def captcha_present(driver) -> bool:
    """Captcha THẬT SỰ HIỂN THỊ chưa? (không phải iframe Arkose ẩn 0x0 Roblox
    load sẵn từ đầu -> tránh báo nhầm ở tab không dính captcha).

    Quét cả text frame chính LẪN mọi iframe con truy cập được (text 'Bảo vệ tài
    khoản' nằm trong iframe challenge -> frame chính không thấy). So text đã BỎ
    DẤU để không phụ thuộc encoding/dấu tiếng Việt.
    """
    js = r"""
    // 1) iframe arkose/funcaptcha/challenge HIỂN THỊ (bỏ iframe ẩn 0x0)
    const sel = '#arkose-iframe, iframe[src*="arkoselabs"], iframe[src*="funcaptcha"],'
      + 'iframe[src*="/challenge/"], iframe[src*="challenge/cdn"],'
      + 'iframe[src*="arkose"], iframe[src*="/fc/"],'
      + '[id*="FunCaptcha"], [class*="funcaptcha"], iframe[title*="captcha" i],'
      + 'iframe[title*="Verification" i]';
    const els = document.querySelectorAll(sel);
    for (const el of els) {
        const r = el.getBoundingClientRect();
        if (el.offsetParent !== null && r.width > 100 && r.height > 100) return true;
    }
    // 2) URL đang ở trang challenge (đôi khi captcha chiếm cả trang, không iframe)
    if (/\/challenge\//i.test(location.href)) return true;

    // bỏ dấu tiếng Việt -> so khớp không phụ thuộc dấu/encoding
    const strip = s => (s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase();
    const pat = /(bao ve tai khoan|bat dau cau do|giai quyet thu thach|con nguoi that|verifying you|protect your account|not a bot|start puzzle|solve this puzzle|verify you are human)/;

    // 3) text frame chính
    if (pat.test(strip(document.body ? document.body.innerText : ''))) return true;

    // 4) text trong MỌI iframe con truy cập được (same-origin/about:blank)
    const frames = document.querySelectorAll('iframe');
    for (const f of frames) {
        try {
            const d = f.contentDocument;
            if (!d || !d.body) continue;
            if (pat.test(strip(d.body.innerText))) return true;
        } catch(e) {}
    }
    return false;
    """
    try:
        return bool(driver.execute_script(js))
    except Exception:
        return False


def _ensure_captcha_power_on() -> None:
    """Bật power_on + funcaptcha.isActive trong omocaptcha/configs.json (giống
    helpsolve1). Đảm bảo ext tự chạy giải captcha khi được nạp. Key đã có sẵn."""
    cfg_path = CAPTCHA_EXT_DIR / "configs.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        changed = False
        if not cfg.get("power_on"):
            cfg["power_on"] = True
            changed = True
        if isinstance(cfg.get("funcaptcha"), dict) and not cfg["funcaptcha"].get("isActive"):
            cfg["funcaptcha"]["isActive"] = True
            changed = True
        if changed:
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            print("[*] Đã bật power_on/funcaptcha trong omocaptcha/configs.json.")
    except Exception as exc:
        print(f"[!] Không đọc/ghi được configs.json: {exc}")


def click_into_captcha(driver) -> bool:
    """Tìm và bấm nút 'Start Puzzle' trong iframe Arkose (cross-origin).

    Switch vào từng iframe, tìm nút, rồi click bằng CHUỘT THẬT qua CDP (Arkose
    bỏ qua click tổng hợp). Toạ độ nút được cộng dồn offset của các iframe cha.
    """
    btn_sel = (
        "button[aria-label*='Start Puzzle' i], button[aria-label*='Verify' i],"
        "button[aria-label*='Begin' i], button[data-theme*='verifyButton' i],"
        "button[data-theme*='home' i], #home_children_button, button.button, button"
    )

    def cdp_click(px, py):
        for tp in ("mouseMoved", "mousePressed", "mouseReleased"):
            try:
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": tp, "x": px, "y": py,
                    "button": "left", "clickCount": 1,
                })
            except Exception:
                pass
            time.sleep(0.05)

    def find_and_click(off_x, off_y) -> bool:
        # tìm nút trong frame hiện tại
        try:
            btns = driver.find_elements(By.CSS_SELECTOR, btn_sel)
        except Exception:
            btns = []
        for b in btns:
            try:
                txt = (b.text or "") + (b.get_attribute("aria-label") or "")
                if not b.is_displayed():
                    continue
                # ưu tiên nút có chữ Start/Verify/Puzzle; nếu không thì bỏ 'button' chung
                if not re.search(r"start|puzzle|verify|begin", txt, re.I):
                    continue
                r = b.rect
                px = off_x + r["x"] + r["width"] / 2
                py = off_y + r["y"] + r["height"] / 2
                # thử cả element.click() lẫn chuột thật
                try:
                    b.click()
                except Exception:
                    pass
                cdp_click(px, py)
                return True
            except Exception:
                continue
        return False

    def recurse(depth, off_x, off_y) -> bool:
        if depth > 4:
            return False
        if find_and_click(off_x, off_y):
            return True
        try:
            frames = driver.find_elements(By.TAG_NAME, "iframe")
        except Exception:
            frames = []
        for fr in frames:
            try:
                fr_rect = fr.rect
                driver.switch_to.frame(fr)
                if recurse(depth + 1, off_x + fr_rect["x"], off_y + fr_rect["y"]):
                    return True
                driver.switch_to.parent_frame()
            except Exception:
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    pass
        return False

    clicked = False
    try:
        driver.switch_to.default_content()
        clicked = recurse(0, 0, 0)
    except Exception:
        pass
    finally:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
    print("[*] Đã bấm Start Puzzle." if clicked
          else "[!] Chưa bấm được Start Puzzle.")
    return clicked


def save_account(username: str, password: str, cookie: str) -> None:
    """Ghi 1 dòng username:pass:cookie vào account.txt (an toàn đa luồng)."""
    line = f"{username}:{password}:{cookie}\n"
    with _file_lock:
        with open(ACCOUNTS_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    print(f"[*] Đã lưu account vào {ACCOUNTS_FILE.name}: {username}:{password}:<cookie {len(cookie)} ký tự>")


def has_unknown_error(driver) -> bool:
    """Roblox báo 'Sorry! An unknown error occurred' (id=GeneralErrorText)?"""
    js = r"""
    const el = document.querySelector('#GeneralErrorText, [id*="GeneralError" i]');
    if (el && el.offsetParent !== null) return true;
    // dò theo nội dung text phòng khi id đổi
    return /unknown error occurred/i.test(document.body ? document.body.innerText : '');
    """
    try:
        return bool(driver.execute_script(js))
    except Exception:
        return False


def _blur_field(driver, el_id: str) -> None:
    """Kích hoạt input/change/blur trên ô -> React nhận đủ giá trị, validate đúng.

    Thiếu bước này, giá trị gõ vào có thể không được React ghi nhận -> signup lỗi.
    """
    js = """
    const el = document.getElementById(arguments[0]);
    if (!el) return;
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.blur();
    el.dispatchEvent(new Event('blur', {bubbles:true}));
    """
    try:
        driver.execute_script(js, el_id)
    except Exception:
        pass


def dismiss_error(driver) -> None:
    """Đóng thông báo lỗi (nút X 'dismiss general error') để thử Sign Up lại."""
    try:
        els = driver.find_elements(
            By.CSS_SELECTOR,
            "[aria-label*='dismiss' i], #GeneralErrorText, .close-button, .icon-close")
        for e in els:
            try:
                if e.is_displayed():
                    e.click()
            except Exception:
                pass
    except Exception:
        pass


def wait_login_and_save(driver, username: str, password: str,
                        max_wait: int = 180) -> str:
    """Sau khi Sign Up: chờ TỐI ĐA 3 PHÚT cho tới khi tạo xong acc (có cookie).

    - Không captcha: cookie có ngay -> lưu, SAVED.
    - Có captcha: extension OMOcaptcha tự giải -> chờ tới khi có cookie -> SAVED.
    - Unknown error: bỏ ngay (ERROR).
    - Hết 3 phút chưa có cookie -> tắt tab (FAIL).
    """
    print(f"[*] Chờ kết quả Sign Up (tối đa {max_wait}s = 3 phút)...")
    start = time.time()
    captcha_announced = False
    wave_announced = False
    wave_bailed = False
    _last_diag = 0.0

    while time.time() - start < max_wait:
        # 0) Tab khác đã báo đổi VPN -> tab này DỪNG NGAY (không đếm lỗi thêm)
        if _switch_vpn or _stop:
            print("[*] Có tín hiệu đổi VPN/dừng -> tắt tab này ngay.")
            return "FAIL"

        # 1) Có cookie = TẠO ACC XONG
        cookie = get_roblosecurity(driver)
        if cookie:
            print("[*] TẠO ACC THÀNH CÔNG -> lưu cookie.")
            save_account(username, password, cookie)
            return "SAVED"

        # 2) Unknown error -> bỏ ngay
        if has_unknown_error(driver):
            print("[!] UNKNOWN ERROR (Roblox chặn) -> tắt tab, làm acc khác.")
            return "ERROR"

        # 3) Có captcha -> extension OMOcaptcha ĐÃ nạp sẵn lúc mở Chrome, content
        #    script đã inject vào iframe funcaptcha. ĐỂ EXT TỰ LO HOÀN TOÀN: nó tự
        #    bấm Start Puzzle, tự tích ảnh, tự bấm 'Reload Challenge' khi gặp lỗi.
        #    KHÔNG tự click vào captcha (click_into_captcha) nữa -> click tay đá
        #    nhau với ext gây 'Something went wrong'. Giống helpsolve1: chỉ chờ.
        #    Song song: hook /fc/gfct/ cho biết CHÍNH XÁC số wave + variant.
        ark = read_arkose_info(driver)
        has_cap = bool(ark.get("captcha")) or captcha_present(driver)
        wv = ark.get("waves")
        wv = int(wv) if isinstance(wv, (int, float)) and wv > 0 else None
        wvar = ark.get("variant") or ark.get("gameName")

        if has_cap and not captcha_announced:
            captcha_announced = True
            print("[*] ĐÃ DETECT CAPTCHA -> để extension OMOcaptcha TỰ giải, chờ...")

        # Số wave về SAU vài trăm ms (chờ /fc/gfct/ trả lời) -> QUYẾT ĐỊNH ngay
        # khi biết: <= MAX_WAVES thì để extension giải, > MAX_WAVES thì tắt tab.
        if wv and not wave_announced:
            wave_announced = True
            record_wave(wv)                       # ghi thống kê để đo anti-detect
            print(f"[*] CAPTCHA: {wv} wave | variant={wvar or '?'}"
                  f" | game={ark.get('gameName') or '?'}"
                  f" | nguồn={ark.get('waveSrc') or '-'}")

            if MAX_WAVES and wv > MAX_WAVES:
                wave_bailed = True
                print(f"[!] {wv} wave > ngưỡng {MAX_WAVES} -> TẮT TAB ngay,"
                      f" không cho extension giải (đỡ tốn credit). Làm acc khác.")
                return "CAPTCHA"
            print(f"[*] {wv} wave <= ngưỡng {MAX_WAVES or '∞'}"
                  f" -> để extension OMOcaptcha giải.")

        # (Đã bỏ đoạn "giải sai -> tắt tab" - xem chú thích ở chỗ MAX_WAVES.)

        # LOG chẩn đoán mỗi ~6s: đang thấy gì (để biết captcha có detect được không)
        if time.time() - _last_diag > 6:
            _last_diag = time.time()
            elapsed = int(time.time() - start)
            done_w = ark.get("answered") or 0
            print(f"    [chờ {elapsed}s] cookie=no | captcha={'CÓ' if has_cap else 'không'}"
                  f" | wave={wv or '?'}{f' (đã giải {done_w})' if done_w else ''}"
                  f" | variant={wvar or '?'}"
                  f" | nguồn={ark.get('waveSrc') or '-'}")

        # tab đóng?
        try:
            _ = driver.title
        except Exception:
            return "FAIL"
        # Captcha vừa hiện nhưng CHƯA biết số wave -> poll DÀY (0.4s) để bắt
        # /fc/gfct/ và tắt tab kịp TRƯỚC khi extension kịp tốn credit. Bình
        # thường thì 2s cho nhẹ CPU.
        time.sleep(0.4 if (has_cap and not wave_announced) else 2)

    print("[!] Hết 3 phút chưa tạo xong acc -> tắt tab.")
    return "FAIL"


def wait_any_form(driver, timeout: int = 60) -> str:
    """Chờ tới khi nhận ra trang đang phục vụ form NÀO (cũ hay mới).

    PHẢI gọi TRƯỚC wait_page_ready: wait_page_ready đòi #signup-username và
    #signup-button (chỉ form CŨ có), gặp form mới sẽ chờ hết giờ rồi đóng tab
    -> không bao giờ biết là đã dính form mới.
    """
    start = time.time()
    while time.time() - start < timeout:
        v = detect_form_variant(driver)
        if v in (FORM_OLD, FORM_NEW, FORM_NEW_SIGNIN):
            return v
        time.sleep(0.5)
    return FORM_UNKNOWN


def wait_page_ready(driver, timeout: int = 30) -> bool:
    """Chờ trang Createaccount LOAD XONG HẲN mới cho điền.

    Điều kiện: readyState=complete + 3 dropdown ngày sinh + ô username HIỂN THỊ
    và ô username không disabled. Ổn định 2 lần liên tiếp cho chắc.
    """
    js = r"""
    if (document.readyState !== 'complete') return false;
    const m = document.getElementById('MonthDropdown');
    const u = document.getElementById('signup-username');
    if (!m || !u) return false;
    if (m.offsetParent === null || u.offsetParent === null) return false;
    if (u.disabled) return false;
    // Nút gửi. TUYỆT ĐỐI KHÔNG liệt kê theo aria-label: mỗi biến thể ghi một
    // kiểu ("Continue" / "Create account") và trang /vi/ còn dịch sang tiếng
    // Việt nữa. Liệt kê là gặp biến thể lạ sẽ chờ hết 60s rồi đóng tab mà
    // chưa điền gì. Chỉ cần CÓ MẶT một nút submit là đủ (lúc này nó đang
    // disabled vì form còn trống, nên không đòi bấm được).
    const b = document.getElementById('signup-button')
           || document.querySelector('form button[type="submit"]')
           || document.querySelector('button[type="submit"]');
    if (!b) return false;
    return true;
    """
    start = time.time()
    stable = 0
    while time.time() - start < timeout:
        try:
            if driver.execute_script(js):
                stable += 1
                if stable >= 2:
                    print("[*] Trang đã load xong -> bắt đầu điền.")
                    return True
            else:
                stable = 0
        except Exception:
            stable = 0
        time.sleep(0.6)
    return False


# ==========================================================================
class _AbortSignup(Exception):
    """Ném ra để DỪNG NGAY việc điền form khi có tín hiệu đổi VPN / dừng."""


def _abort_if_needed(tag: str) -> None:
    """Nếu đang cần đổi VPN (_switch_vpn) hoặc dừng (_stop) -> DỪNG điền NGAY."""
    if _switch_vpn or _stop:
        print(f"{tag} [*] Có tín hiệu đổi VPN/dừng -> ngừng điền, tắt tab.")
        raise _AbortSignup()


def fill_one_account(driver, name: str, slot: int) -> str:
    """Điền 1 tài khoản. Trả về kết quả: SAVED / ERROR / CAPTCHA / FAIL."""
    tag = f"[{name}]"
    outcome = "FAIL"
    keep_tab = False
    fill_deadline = time.time() + ACCOUNT_FILL_BUDGET

    def _over_budget(where: str) -> bool:
        """Quá giờ điền form -> bỏ tab này, KHÔNG để nó treo giữ chỗ mãi."""
        if time.time() <= fill_deadline:
            return False
        print(f"{tag} [!] Quá {ACCOUNT_FILL_BUDGET:.0f}s ở bước '{where}'"
              f" -> tắt tab, làm acc khác.")
        return True

    try:
        _abort_if_needed(tag)
        print(f"{tag} mở trang tạo tài khoản...")
        driver.get("https://www.roblox.com/Createaccount")

        # Ép lại đúng ô lưới SAU khi load (chỉ khi HIỆN cửa sổ; minimized thì bỏ)
        if not MINIMIZED:
            x, y, w, h = grid_rect(slot)
            set_window_grid(driver, x, y, w, h)

        # Roblox đang A/B 2 form -> phải biết form nào TRƯỚC khi chờ/điền.
        variant = wait_any_form(driver, timeout=60)
        print(f"{tag} [*] Form đăng ký: {variant}")
        if variant == FORM_NEW_SIGNIN and HALT_ON_SIGNIN_FORM:
            print(f"{tag} [!] Gặp biến thể FORM MỚI CÓ NÚT 'Sign in'"
                  f" -> dừng tool, giữ tab để nghiên cứu.")
            keep_tab = halt_for_new_form(driver, tag)
            return "NEWFORM"
        if variant == FORM_NEW and HALT_ON_NEW_FORM:
            keep_tab = halt_for_new_form(driver, tag)
            return "NEWFORM"
        if variant == FORM_UNKNOWN:
            print(f"{tag} [!] Không nhận ra form -> tắt tab, làm acc khác.")
            return "FAIL"
        # Không dừng nữa thì coi biến thể "Sign in" như form mới mà điền,
        # nếu không nó rơi qua CẢ HAI nhánh dưới -> không điền gì cả.
        if variant == FORM_NEW_SIGNIN:
            variant = FORM_NEW

        # CHỜ TRANG LOAD XONG HẲN mới điền (không điền khi icon còn xoay).
        # Nếu 60s (1 phút) vẫn chưa xong -> tắt tab, làm acc khác.
        if not wait_page_ready(driver, timeout=60):
            print(f"{tag} [!] Trang 1 phút chưa load xong -> tắt tab, làm acc khác.")
            return "FAIL"

        # "Sống" trên trang trước khi điền (bỏ ở FAST_MODE cho nhanh)
        if not FAST_MODE:
            warm_up_page(driver)

        _abort_if_needed(tag)
        if _over_budget("ngày sinh"):
            return "FAIL"
        birthday = fill_birthday(driver)
        nap(0.6, 1.5)
        if not FAST_MODE:
            human_mouse_wiggle(driver, 2)

        _abort_if_needed(tag)
        if _over_budget("username"):
            return "FAIL"
        # fill_username tự blur + tự kiểm tra lỗi bên trong rồi.
        username = fill_username(driver, birthday) if birthday else None
        if not username:
            # Không có tên sạch thì bấm Sign Up chắc chắn trượt, mà còn ăn
            # thêm 1 lần captcha -> tắt tab luôn, làm acc khác cho nhanh.
            print(f"{tag} [!] Không lấy được username hợp lệ -> tắt tab.")
            return "FAIL"
        nap(0.6, 1.5)
        if not FAST_MODE:
            human_mouse_wiggle(driver, 2)

        # KHÔNG đoán theo tên form nữa - hỏi thẳng DOM có ô mật khẩu ở bước 1
        # không. Roblox đang chạy 3 biến thể và còn đẻ thêm, cứ enum tên form là
        # gặp cái lai (Radix mới + 1 bước kiểu cũ) lại hỏng.
        password = None
        if has_step1_password(driver):
            _abort_if_needed(tag)
            password = fill_password(driver)
            _blur_field(driver, "signup-password")
            nap(0.6, 1.5)

        select_gender(driver)

        # nghỉ trước khi bấm đăng ký
        nap(1.2, 2.8)
        if not FAST_MODE:
            human_mouse_wiggle(driver, 3)
        _abort_if_needed(tag)                    # chốt cuối trước khi bấm signup
        if _over_budget("gửi form"):
            return "FAIL"
        if not click_signup(driver):
            print(f"{tag} [!] Không gửi được form -> tắt tab.")
            return "FAIL"

        # Chỉ biến thể 2 BƯỚC mới có panel này. wait_step2 tự thoát ngay nếu
        # trang không có nó, nên biến thể 1 bước chạy thẳng xuống chờ cookie.
        if not password:
            _abort_if_needed(tag)
            if _over_budget("bước 2"):
                return "FAIL"
            if not wait_step2(driver):
                print(f"{tag} [!] Chưa có mật khẩu mà bước 2 không hiện -> tắt tab.")
                return "FAIL"
            nap(0.5, 1.2)
            password = fill_password_v2(driver)
            if not password:
                print(f"{tag} [!] Không đặt được mật khẩu ở bước 2 -> tắt tab.")
                return "FAIL"

        result = "FAIL"
        if username and password:
            result = wait_login_and_save(driver, username, password)
        else:
            print(f"{tag} [!] Thiếu username/password.")

        outcome = result
        if result == "SAVED":
            print(f"{tag} [*] Thành công -> đóng + xóa profile.")
        elif result == "CAPTCHA":
            print(f"{tag} [*] Dính captcha -> tắt tab, worker bật acc khác.")
        elif result == "ERROR":
            print(f"{tag} [*] Roblox báo unknown error -> tắt tab, bật acc khác.")
        else:
            print(f"{tag} [!] Không thành công -> bỏ, làm acc khác.")
    except _AbortSignup:
        outcome = "FAIL"          # bị hủy do đổi VPN/dừng -> không tính lỗi nước
    except Exception as exc:
        print(f"{tag} [!] Lỗi: {exc}")
    finally:
        if keep_tab:
            # Tab form mới: GIỮ NGUYÊN cả tab lẫn profile để còn xem/thao tác.
            print(f"{tag} [*] Giữ nguyên tab form mới (không quit, không xóa profile).")
        else:
            try:
                driver.quit()
            except Exception:
                pass
            # XÓA SẠCH profile sau khi xong -> không để lại dấu vết (cookie/cache/history)
            time.sleep(1)
            wipe_profile(name)
    return outcome


def parse_total_accounts() -> int:
    """Đọc số acc muốn tạo từ tham số: python main.py --100  (hoặc 100)."""
    for arg in sys.argv[1:]:
        m = re.match(r"-*(\d+)$", arg)   # '--100', '-100', '100'
        if m:
            return int(m.group(1))
    return NUM_TABS   # không truyền -> tạo đúng số tab (1 đợt)


# Bộ đếm: đếm theo SỐ ACC THÀNH CÔNG (SAVED), + số lần đã thử
_count_lock = threading.Lock()
_success_count = 0        # số acc tạo thành công (mục tiêu = total)
_attempt_count = 0        # tổng số lần đã thử (để đặt tên, thống kê)
_stop = False             # cờ dừng khi đã đủ total
_error_country = 0        # số unknown error ở NƯỚC hiện tại (đủ ngưỡng -> đổi nước)
_switch_vpn = False       # cờ báo cần đổi VPN (worker set khi đủ lỗi)
_batch_gen = 0            # 'đời' của đợt hiện tại; thread đời cũ tự thoát, không
                          # lọt sang đợt mới sau khi đổi VPN (tránh tab lẫn lộn).

# Registry các driver ĐANG SỐNG để KILL HẾT ngay khi cần đổi VPN (không chờ mỗi
# tab tự nhận ra flag -> tránh tình trạng "điền xong rồi mới đổi").
_drivers_lock = threading.Lock()
_active_drivers: dict[int, object] = {}   # id(driver) -> driver


def _register_driver(driver) -> None:
    with _drivers_lock:
        _active_drivers[id(driver)] = driver


def _unregister_driver(driver) -> None:
    with _drivers_lock:
        _active_drivers.pop(id(driver), None)


def kill_all_drivers() -> None:
    """TẮT NGAY mọi tab/driver đang mở (dùng khi đổi VPN / dừng).

    Gọi driver.quit() cho tất cả driver đang sống -> mọi thao tác Selenium ở
    các thread khác lập tức ném exception -> tab dừng ngay, không điền tiếp.
    """
    with _drivers_lock:
        drivers = list(_active_drivers.values())
        _active_drivers.clear()
    if drivers:
        print(f"[*] KILL HẾT {len(drivers)} tab đang mở NGAY (đổi VPN/dừng).")
    for d in drivers:
        if d is _keep_alive_driver:      # tab form mới -> GIỮ LẠI, không đụng
            continue
        try:
            d.quit()
        except Exception:
            pass


# ==========================================================================
# GẶP FORM MỚI -> DỪNG TOÀN BỘ TOOL, NHƯNG GIỮ NGUYÊN TAB ĐÓ
# ==========================================================================
# Roblox đang A/B 2 form đăng ký. Form mới (Radix combobox + 2 bước) tool CHƯA
# điền được. Thay vì điền bừa rồi hỏng acc, gặp là dừng hết và ĐỂ NGUYÊN tab
# đó cho người xem/thao tác tay.
_keep_alive_driver = None            # driver được giữ lại (không bao giờ quit)
_keep_lock = threading.Lock()

NEW_FORM_DUMP = BASE_DIR / "newform_dump.json"

_NEW_FORM_DUMP_JS = r"""
const q = (s) => [...document.querySelectorAll(s)];
const desc = (e) => ({
  tag: e.tagName.toLowerCase(), id: e.id || null,
  testid: e.getAttribute('data-testid'), name: e.getAttribute('name'),
  type: e.getAttribute('type'), role: e.getAttribute('role'),
  aria: e.getAttribute('aria-label'), ph: e.getAttribute('placeholder'),
  ctrls: e.getAttribute('aria-controls'),
  txt: (e.innerText || '').trim().slice(0, 40) || null,
});
return JSON.stringify({
  url: location.href,
  ua: navigator.userAgent,
  fields: q('input, button, select, [role="combobox"], [data-testid]')
            .filter(e => e.offsetWidth || e.offsetHeight)
            .map(desc),
  html: document.body.innerHTML.slice(0, 120000),
}, null, 1);
"""


def _dump_new_form(driver) -> None:
    """Ghi cấu trúc form mới ra file để còn biết đường viết selector."""
    try:
        raw = driver.execute_script(_NEW_FORM_DUMP_JS)
        out = NEW_FORM_DUMP
        try:
            if detect_form_variant(driver) == FORM_NEW_SIGNIN:
                out = BASE_DIR / "signinform_dump.json"
        except Exception:
            pass
        out.write_text(raw, encoding="utf-8")
        print(f"[*] Đã ghi cấu trúc form ra: {out}")
    except Exception as exc:
        print(f"[!] Không dump được form mới: {exc}")


def _orphan_driver(driver) -> None:
    """BỎ RƠI driver: tool thoát hẳn mà cửa sổ Chrome VẪN SỐNG.

    Vì sao phải làm vòng vèo vậy:
      - chromedriver gom Chrome vào Job Object của Windows -> GIẾT chromedriver
        là Chrome chết theo ngay (đã thử, kể cả taskkill không /t).
      - Cờ "detach" của chromedriver cũng KHÔNG cứu được (đã thử, vẫn chết).
      - Cách chạy được: ĐỂ NGUYÊN chromedriver sống, chỉ chặn selenium gọi
        service.stop() / quit() lúc interpreter thoát.
    Đổi lại còn 1 chromedriver.exe treo lại; đóng cửa sổ Chrome đó là nó tự đi.
    """
    try:
        svc = driver.service
        svc.process = None                      # stop() không còn gì để giết
        svc.stop = lambda *a, **k: None
    except Exception:
        pass
    try:
        driver.quit = lambda *a, **k: None      # chặn quit nhầm từ chỗ khác
    except Exception:
        pass


def halt_for_new_form(driver, tag: str) -> bool:
    """Gặp form mới: bật cờ dừng toàn tool + giữ tab này sống.

    Trả True nếu tab NÀY là tab được giữ (caller không được quit/wipe nó).
    """
    global _stop, _keep_alive_driver
    with _keep_lock:
        if _keep_alive_driver is not None:
            return False              # đã giữ 1 tab rồi -> tab này đóng bình thường
        _keep_alive_driver = driver
    _unregister_driver(driver)        # kill_all_drivers() sẽ bỏ qua tab này
    _orphan_driver(driver)            # tool thoát mà tab vẫn sống
    _stop = True                      # mọi worker + vòng lặp chính dừng

    print("\n" + "=" * 68)
    print(f"{tag} [!] GẶP FORM ĐĂNG KÝ MỚI CỦA ROBLOX (Radix combobox / 2 bước).")
    print("    Tool CHƯA điền được form này -> DỪNG TOÀN BỘ.")
    print("    TAB NÀY ĐƯỢC GIỮ NGUYÊN, không đóng, không xóa profile.")
    print("=" * 68 + "\n")
    _dump_new_form(driver)
    return True

# ---- Cấu hình VPN (HMA) ----
USE_VPN = True                 # bật/tắt tự điều khiển HMA VPN
VPN_ERROR_LIMIT = 2            # 2 unknown error ở 1 nước -> đổi nước
VPN_COUNTRIES = ["Singapore", "Hồng Kông", "Nhật Bản", "Thái Lan"]  # thứ tự xoay

# Timezone tương ứng từng nước VPN. IP Singapore mà máy khai Asia/Saigon là mâu
# thuẫn LỘ NGAY đang xài VPN -> Arkose hạ điểm tin cậy -> bắt giải nhiều wave.
# Khi kết nối VPN xong, main() gán _vpn_timezone rồi create_browser ép timezone
# này qua CDP (ép ở tầng trình duyệt, không phải hook JS -> không lộ).
VPN_TIMEZONES = {
    "Singapore": "Asia/Singapore",
    "Hồng Kông": "Asia/Hong_Kong",
    "Nhật Bản":  "Asia/Tokyo",
    "Thái Lan":  "Asia/Bangkok",
}
_vpn_timezone: str | None = None      # None = dùng timezone thật của máy
VPN_REST_MINUTES = 2           # qua hết 4 nước -> tắt HMA, nghỉ 2 phút rồi lặp lại


def worker_loop(slot: int, total: int, id_start: int,
                chrome_version: int | None, my_gen: int) -> None:
    """Lặp mở browser -> tạo acc -> xóa -> tiếp. DỪNG khi:
       - đủ 'total' acc thành công (_stop), HOẶC
       - nước hiện tại bị đủ VPN_ERROR_LIMIT unknown error (_switch_vpn), HOẶC
       - đã sang đợt mới (my_gen != _batch_gen) -> thread đời cũ tự thoát.
    """
    global _success_count, _attempt_count, _stop, _error_country, _switch_vpn
    while True:
        with _count_lock:
            # đủ acc / bị dừng / cần đổi VPN / đã sang đợt mới -> worker thoát
            if _stop or _switch_vpn or _success_count >= total or my_gen != _batch_gen:
                if _success_count >= total:
                    _stop = True
                return
            _attempt_count += 1
            my_id = id_start + _attempt_count

        name = f"profile_{my_id:04d}"
        tag = f"[{name}]"
        wipe_profile(name)

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
            print(f"{tag} [!] Không mở được -> thử acc khác.")
            continue

        _register_driver(driver)               # ghi vào registry để kill-all được
        try:
            result = fill_one_account(driver, name, slot)
        finally:
            _unregister_driver(driver)         # fill_one_account đã quit() driver

        just_hit_limit = False
        with _count_lock:
            if result == "SAVED":
                _success_count += 1
                _error_country = 0         # thành công -> reset lỗi của nước này
            elif result == "ERROR" and not _switch_vpn:
                # chỉ đếm khi chưa đủ ngưỡng (tránh vượt do nhiều tab cùng lỗi)
                _error_country += 1
            done = _success_count
            tries = _attempt_count
            ecountry = _error_country
            if _success_count >= total:
                _stop = True
            # Đủ VPN_ERROR_LIMIT unknown error (TỔNG mọi tab) -> đổi VPN
            if USE_VPN and _error_country >= VPN_ERROR_LIMIT and not _switch_vpn:
                _switch_vpn = True
                just_hit_limit = True      # thread NÀY là thread chạm ngưỡng
        print(f"[*] Thành công: {done}/{total}  (đã thử {tries} lần)"
              + (f" | unknown error (nước này): {ecountry}" if ecountry else ""))

        # VỪA chạm ngưỡng lỗi -> KILL HẾT các tab đang mở NGAY (không để tab nào
        # điền tiếp / bấm signup). Gọi NGOÀI lock để không deadlock.
        if just_hit_limit:
            print(f"[!] Nước này đủ {VPN_ERROR_LIMIT} unknown error "
                  f"-> tắt HẾT tab ngay để đổi VPN.")
            kill_all_drivers()


def _run_batch(total: int, id_start: int, chrome_version, workers: int) -> None:
    """Chạy 1 ĐỢT trên nước VPN hiện tại: mở workers tab, chạy tới khi ĐỦ total,
    hoặc cần đổi VPN (_switch_vpn), hoặc bị dừng (_stop).

    QUAN TRỌNG: khi _switch_vpn bật, đã kill_all_drivers() -> vài thread có thể
    còn kẹt trong 1 call Selenium (đang chờ chromedriver trả lời) và join() lâu.
    Nên join CÓ TIMEOUT: chờ tối đa GRACE giây rồi trả về để main đổi VPN ngay,
    không đứng chờ mãi. Thread daemon còn sót sẽ tự chết khi driver đã quit.
    """
    global _batch_gen
    with _count_lock:
        _batch_gen += 1            # đợt mới -> thread đời cũ (nếu còn) tự thoát
        my_gen = _batch_gen

    threads = []
    for slot in range(workers):
        t = threading.Thread(target=worker_loop,
                             args=(slot, total, id_start, chrome_version, my_gen),
                             daemon=True)          # daemon: không chặn tiến trình
        t.start()
        threads.append(t)
        time.sleep(1.2)

    # Chờ tới khi có tín hiệu đổi VPN/dừng, HOẶC mọi thread xong.
    while any(t.is_alive() for t in threads):
        if _switch_vpn or _stop:
            break
        time.sleep(0.5)

    # Có tín hiệu -> đảm bảo mọi tab đã bị kill, rồi join có timeout để thu dọn.
    if _switch_vpn or _stop:
        kill_all_drivers()                          # kill lại cho chắc (idempotent)
        GRACE = 20                                  # chờ tối đa 20s cho thread thoát
        deadline = time.time() + GRACE
        for t in threads:
            remain = deadline - time.time()
            if remain > 0:
                t.join(timeout=remain)
        alive = sum(1 for t in threads if t.is_alive())
        if alive:
            print(f"[*] Còn {alive} thread chưa thoát sau {GRACE}s "
                  f"-> bỏ qua, đổi VPN ngay (chúng là daemon, sẽ tự chết).")
    else:
        for t in threads:                           # đợt kết thúc bình thường
            t.join()


def main() -> None:
    global FAST_MODE, MINIMIZED, _switch_vpn, _error_country, _stop
    if any(a in ("--fast", "-f") for a in sys.argv[1:]):
        FAST_MODE = True
    if any(a in ("--novpn",) for a in sys.argv[1:]):
        globals()["USE_VPN"] = False
    if any(a in ("--show",) for a in sys.argv[1:]):
        MINIMIZED = False        # --show: hiện cửa sổ (không thu nhỏ)

    total = parse_total_accounts()
    chrome_version = get_chrome_major_version()
    cols, rows, _cw, _ch, _ = grid_layout()
    workers = min(NUM_TABS, total)

    print(f"[*] Chrome version: {chrome_version} | FAST={FAST_MODE} | VPN={USE_VPN}")
    print(f"[*] Mục tiêu: {total} acc, {workers} tab (lưới {cols}x{rows}).")

    # id chạy tăng dần (không trùng), dùng biến ngoài để nhiều đợt không đè nhau
    existing = [p.stem for p in PROFILES_DIR.glob("profile_*")]
    nums = [int(m.group(1)) for p in existing
            if (m := re.match(r"profile_(\d+)", p))]
    next_id = (max(nums) + 1) if nums else 0

    # Nạp module điều khiển HMA VPN (nếu bật)
    hma = None
    if USE_VPN:
        try:
            import hma_vpn as hma
        except Exception as exc:
            print(f"[!] Không nạp được hma_vpn: {exc}. Chạy KHÔNG VPN.")
            globals()["USE_VPN"] = False

    country_idx = 0     # đang ở nước nào trong VPN_COUNTRIES

    while not _stop and _success_count < total:
        # ===== Bật/đổi VPN sang nước hiện tại =====
        if USE_VPN and hma:
            country = VPN_COUNTRIES[country_idx]
            print(f"\n===== VPN: kết nối {country} =====")
            ok = hma.hma_change_location(country)
            if not ok:
                print(f"[!] Không kết nối được {country} -> thử nước kế.")
                country_idx += 1
                if country_idx >= len(VPN_COUNTRIES):
                    country_idx = 0
                    _vpn_rest(hma)
                continue
            # Ép timezone khớp nước VPN -> browser mở sau đây sẽ dùng timezone
            # này, không còn mâu thuẫn "IP Singapore + giờ Việt Nam".
            globals()["_vpn_timezone"] = VPN_TIMEZONES.get(country)
            print(f"[*] Đã kết nối {country} (timezone -> "
                  f"{_vpn_timezone or 'giữ nguyên'}). Chờ 10s rồi bắt đầu reg...")
            time.sleep(10)

        # ===== reset cờ đổi VPN + lỗi của nước này, rồi chạy 1 đợt =====
        with _count_lock:
            _switch_vpn = False
            _error_country = 0
        _run_batch(total, next_id, chrome_version, workers)
        next_id += workers + 5   # nhích id cho đợt sau (tránh trùng tên)

        if _stop or _success_count >= total:
            break

        # ===== đợt kết thúc vì _switch_vpn (nước này lỗi đủ) -> đổi nước =====
        if USE_VPN and hma:
            print(f"[*] Nước '{VPN_COUNTRIES[country_idx]}' bị "
                  f"{VPN_ERROR_LIMIT} unknown error -> đổi nước.")
            country_idx += 1
            if country_idx >= len(VPN_COUNTRIES):
                # đã qua HẾT 4 nước -> tắt HMA, nghỉ 5 phút rồi lặp lại từ đầu
                country_idx = 0
                _vpn_rest(hma)
        else:
            break   # không VPN mà lỗi -> dừng

    print(f"[*] HOÀN TẤT: {_success_count} acc thành công / {_attempt_count} lần thử.")
    print(f"[*] Thống kê captcha: {wave_summary()}")

    # Dừng vì gặp form mới -> KHÔNG ngắt VPN (đổi IP là hỏng phiên của tab đang
    # giữ) và KHÔNG thoát tiến trình (thoát là chromedriver đóng tab luôn).
    if _keep_alive_driver is not None:
        _park_for_new_form()
        return

    if USE_VPN and hma:
        try:
            hma.hma_disconnect()
        except Exception:
            pass


def _park_for_new_form() -> None:
    """Báo tab đã được giữ. Tool THOÁT HẲN, cửa sổ Chrome vẫn ở đó.

    Không cần treo tiến trình nữa: _orphan_driver() đã cắt cleanup của selenium
    nên chromedriver sống tiếp và giữ Chrome giùm.
    """
    print("\n" + "=" * 68)
    print("[*] TOOL DỪNG vì gặp FORM ĐĂNG KÝ MỚI - và tool THOÁT LUÔN.")
    print("[*] Cửa sổ Chrome đó VẪN MỞ, cứ thoải mái xem DevTools/thao tác.")
    print(f"[*] Cấu trúc form đã ghi ra: {NEW_FORM_DUMP}")
    print("[*] Xem xong đóng cửa sổ Chrome đó bằng tay là xong.")
    print("=" * 68 + "\n")


def _vpn_rest(hma) -> None:
    """Đã qua hết 4 nước: tắt HMA hẳn, nghỉ VPN_REST_MINUTES phút rồi bật lại."""
    print(f"\n[!!!] Đã thử HẾT {len(VPN_COUNTRIES)} nước đều lỗi. "
          f"Tắt HMA + nghỉ {VPN_REST_MINUTES} phút...")
    try:
        hma.hma_disconnect()
        # tắt hẳn app HMA
        import subprocess
        subprocess.run(["taskkill", "/f", "/im", "Vpn.exe"],
                       capture_output=True)
    except Exception:
        pass
    # nghỉ, đếm ngược
    for m in range(VPN_REST_MINUTES, 0, -1):
        print(f"    ...nghỉ, còn {m} phút")
        time.sleep(60)
    print("[*] Hết giờ nghỉ -> bật lại HMA từ Singapore.")


if __name__ == "__main__":
    main()
