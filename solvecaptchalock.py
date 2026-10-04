import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")

import time
import json
import base64
import queue
import hashlib
import argparse
import threading
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Tái dùng phần đã CHẠY ỔN của helpsolve1.py (mở browser + nạp OMOcaptcha runtime).
# helpsolve1 dùng để giải captcha lúc JOIN GAME; file này khác ở chỗ nó xử lý
# CAPTCHA LOCK (acc bị khoá, bấm "Gỡ" ra captcha) qua account-unlock-api.
from helpsolve1 import (
    HEADERS_BASE,
    ROOT_DIR,
    PROFILES_DIR,
    get_csrf,
    build_challenge_url,
    find_browser,
    account_profile_dir,
    prepare_profile,
    create_browser,
    wait_challenge_ready,
    install_omocaptcha_extension,
    write_omocaptcha_source_key,
    check_omocaptcha_key,
    load_config,
)
# Parser cookie dùng chung (hỗ trợ cả 'user:pass:cookie' lẫn chỉ cookie).
from checkcookie import parse_account_line
# Hook đọc wave + variant FunCaptcha (học từ main.py) -> xem omo giải tới đâu.
from arkose_hook import (install_arkose_hook, read_arkose_info, format_arkose,
                         detect_captcha_kinds, format_captcha_kinds)
# Anti-detect fingerprint (học từ main.py) -> nâng trust score, GIẢM số wave.
from stealth import build_stealth_js, fresh_fingerprint, patch_chromedriver_cdc

requests.packages.urllib3.disable_warnings()

ACC_FILE_DEFAULT = ROOT_DIR / "accounts.txt"
RESULTS_FILE = ROOT_DIR / "solvecaptchalock_results.txt"
TYPES_FILE = ROOT_DIR / "captcha_types.log"

URL_AUTH = "https://users.roblox.com/v1/users/authenticated"
URL_UNLOCK = "https://apis.roblox.com/account-unlock-api/v1/unlock"
URL_MOBILE = "https://www.roblox.com/mobileapi/userinfo"


# ============================================================
#  ROBLOX API - CHECK CAPTCHA LOCK (không giải, chỉ nhận diện)
# ============================================================
def new_session(cookie):
    s = requests.Session()
    s.verify = False
    s.cookies.set(".ROBLOSECURITY", cookie.strip(), domain=".roblox.com")
    return s


def get_user_basic(session):
    """Lấy tên + id ngay cả khi acc bị khoá (authenticated trả 403).

    mobileapi thường vẫn trả được thông tin khi acc moderated.
    """
    try:
        r = session.get(URL_MOBILE, headers=HEADERS_BASE, timeout=12)
        if r.status_code == 200:
            d = r.json()
            return d.get("UserName") or "?", d.get("UserID") or 0
    except (requests.RequestException, ValueError):
        pass
    return "?", 0


def probe_lock(session, csrf):
    """Phân loại trạng thái acc dựa trên bước account-unlock.

    Trả dict:
      {"status": "clean"}                         acc bình thường (authenticated 200)
      {"status": "dead"}                          cookie hết hạn (401)
      {"status": "faceid"}                        khoá FACE ID (challenge biometric) -> KHÔNG giải
      {"status": "captcha", "id", "metadata_b64", "csrf"}   CAPTCHA LOCK -> giải được
      {"status": "banned"}                        403 nhưng không ra captcha/faceid
      {"status": "other", "code"}                 http khác
    """
    try:
        r = session.get(URL_AUTH, headers=HEADERS_BASE, timeout=15)
    except requests.RequestException as e:
        return {"status": "other", "code": f"conn:{e}"}

    if r.status_code == 200:
        return {"status": "clean"}
    if r.status_code == 401:
        return {"status": "dead"}
    if r.status_code != 403:
        return {"status": "other", "code": r.status_code}

    # 403 -> POST unlock {} để đọc loại challenge (captcha hay biometric).
    headers = {**HEADERS_BASE, "x-csrf-token": csrf}
    try:
        ru = session.post(URL_UNLOCK, headers=headers, json={}, timeout=15)
    except requests.RequestException as e:
        return {"status": "other", "code": f"unlock:{e}"}

    # csrf hết hạn -> lấy token mới rồi thử lại 1 lần
    new_csrf = ru.headers.get("x-csrf-token", "")
    if ru.status_code == 403 and new_csrf and new_csrf != csrf:
        headers["x-csrf-token"] = new_csrf
        try:
            ru = session.post(URL_UNLOCK, headers=headers, json={}, timeout=15)
        except requests.RequestException:
            pass

    ctype = (ru.headers.get("rblx-challenge-type") or "").strip().lower()
    cid = (ru.headers.get("rblx-challenge-id") or "").strip()
    meta_b64 = (ru.headers.get("rblx-challenge-metadata") or "").strip()

    meta_txt = ""
    if meta_b64:
        try:
            meta_txt = base64.b64decode(
                meta_b64 + "=" * (-len(meta_b64) % 4)
            ).decode("utf-8", "replace").lower()
        except (ValueError, UnicodeError):
            meta_txt = meta_b64.lower()

    is_bio = ("biometric" in ctype) or ("personaliveness" in meta_txt) or \
             ("biometrictype" in meta_txt) or ("liveness" in meta_txt)
    is_cap = ("captcha" in ctype) or ("unifiedcaptchaid" in meta_txt) or \
             ("dataexchangeblob" in meta_txt)

    if is_bio:
        return {"status": "faceid"}
    if is_cap and cid:
        # renderNativeChallenge=true -> captcha NATIVE cua Roblox (loai "v2"
        # rieng, Roblox tu render anh, KHONG qua Arkose) -> omo FunCaptcha
        # KHONG giai duoc. false/thieu -> FunCaptcha (Arkose) -> omo giai duoc.
        native = "rendernativechallenge\":true" in meta_txt.replace(" ", "")
        return {"status": "captcha", "id": cid, "metadata_b64": meta_b64,
                "native": native, "csrf": headers.get("x-csrf-token", csrf)}
    return {"status": "banned"}


def verify_unlocked(cookie):
    """Acc đã được gỡ chưa: authenticated trả 200 -> đã LIVE lại."""
    s = new_session(cookie)
    try:
        r = s.get(URL_AUTH, headers=HEADERS_BASE, timeout=15)
        return r.status_code == 200
    except requests.RequestException:
        return False


def read_solved_token(browser):
    """Đọc token FunCaptcha ĐÃ GIẢI THẬT từ browser (arkose hook đã bắt ở
    /fc/gt2/ + cập nhật khi giải xong). Trả token hoặc None nếu chưa có."""
    if browser is None:
        return None
    try:
        browser.switch_to.default_content()   # token nằm ở frame TOP (hook postMessage lên)
    except Exception:
        pass
    try:
        return browser.execute_script(
            "return (window.__arkoseInfo && window.__arkoseInfo.token) || null;")
    except Exception:
        return None


def _build_solved_metadata(info, token):
    """Ghép metadata nộp lại: giữ unifiedCaptchaId/dataExchangeBlob gốc, nhét
    captchaToken = token ĐÃ GIẢI thật (không bịa)."""
    unified = info["id"]
    dxb = ""
    try:
        orig = json.loads(base64.b64decode(
            info["metadata_b64"] + "=" * (-len(info["metadata_b64"]) % 4)))
        unified = orig.get("unifiedCaptchaId", unified)
        dxb = orig.get("dataExchangeBlob", "")
    except (ValueError, KeyError):
        pass
    meta = {"unifiedCaptchaId": unified, "captchaToken": token}
    if dxb:
        meta["dataExchangeBlob"] = dxb
    return base64.b64encode(json.dumps(meta).encode("utf-8")).decode("ascii")


def try_complete_unlock(cookie, info, browser=None):
    """Hoàn tất gỡ khoá bằng TOKEN ĐÃ GIẢI THẬT lấy từ browser.

    omo giải xong -> token FunCaptcha hợp lệ nằm ở window.__arkoseInfo.token.
    Nộp lại account-unlock kèm rblx-challenge-* + metadata chứa captchaToken thật.
    KHÔNG có token (chưa giải xong) -> chỉ kiểm tra acc đã live lại chưa.
    Trả True nếu unlock 200/201 hoặc acc đã live.
    """
    token = read_solved_token(browser)
    if not token:
        # chưa giải xong -> không nộp token rỗng (nộp rỗng luôn fail); chỉ verify
        return verify_unlocked(cookie)

    meta_b64 = _build_solved_metadata(info, token)
    s = new_session(cookie)
    csrf = get_csrf(s)
    headers = {
        **HEADERS_BASE,
        "x-csrf-token": csrf,
        "rblx-challenge-id": info["id"],
        "rblx-challenge-type": "captcha",
        "rblx-challenge-metadata": meta_b64,
    }
    try:
        r = s.post(URL_UNLOCK, headers=headers, json={}, timeout=15)
        print(f"    [unlock] nộp token -> HTTP {r.status_code} {r.text[:120]}")
        if r.status_code in (200, 201):
            return True
    except requests.RequestException as e:
        print(f"    [unlock] lỗi: {e}")
    return verify_unlocked(cookie)


# ============================================================
#  CHECK 1 ACC
# ============================================================
def check_account(cookie, index, total):
    short = cookie.strip()[:30] + "..."
    print(f"\n[{index}/{total}] Check | {short}")

    s = new_session(cookie)
    csrf = get_csrf(s)
    if not csrf:
        print("  [!] Không lấy được csrf")
        return {"status": "error", "cookie": cookie}

    username, uid = get_user_basic(s)
    lock = probe_lock(s, csrf)
    st = lock["status"]

    if st == "clean":
        print(f"  [OK] {username} ({uid}) - KHÔNG bị khoá")
        return {"status": "clean", "username": username, "uid": uid}
    if st == "dead":
        print(f"  [x] Cookie hết hạn/sai")
        return {"status": "dead"}
    if st == "faceid":
        print(f"  [FACE] {username} ({uid}) - dính FACE ID -> BỎ QUA (không giải)")
        return {"status": "faceid", "username": username, "uid": uid}
    if st == "banned":
        print(f"  [BAN] {username} ({uid}) - 403 không ra captcha (ban/khoá khác)")
        return {"status": "banned", "username": username, "uid": uid}
    if st != "captcha":
        print(f"  [?] {username} ({uid}) - {lock.get('code')}")
        return {"status": "other"}

    if lock.get("native"):
        print(f"  [CAPTCHA-V2] {username} ({uid}) - captcha NATIVE Roblox "
              f"(renderNativeChallenge=true) -> omo KHÔNG giải được, mở browser để tự soi")
    else:
        print(f"  [CAPTCHA] {username} ({uid}) - FunCaptcha/Arkose -> mở browser cho omo giải")
    # uid có thể = 0 (acc khoá không lộ id) -> tạo id ổn định từ cookie để mỗi acc
    # có profile browser RIÊNG, tránh nhiều cửa sổ dùng chung 1 thư mục 'unknown_0'.
    prof_uid = uid or hashlib.md5(cookie.encode("utf-8")).hexdigest()[:10]
    return {
        "status": "captcha",
        "username": username,
        "uid": prof_uid,
        "cookie": cookie,
        "native": lock.get("native", False),
        "captcha_info": {"id": lock["id"], "metadata_b64": lock["metadata_b64"]},
    }


def log_captcha_kinds(user, uid, kinds):
    """Ghi lại loại captcha đã gặp + dấu hiệu bắt được, để xem lại sau."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}|{user}|{uid}|{format_captcha_kinds(kinds)}"
    for k, sig in sorted(kinds.items()):
        line += f"\n    {k} <- {sig}"
    try:
        with open(TYPES_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ============================================================
#  GIẢI 1 ACC (mở browser + OMOcaptcha runtime, giống helpsolve1)
# ============================================================
def solve_account(result, index, total, browser_path, window_pos=None,
                  max_wait=180, use_extension=True):
    info = result["captcha_info"]
    user = result["username"]
    cookie = result["cookie"]
    profile_dir = account_profile_dir(result)
    url = build_challenge_url(info)

    prepare_profile(profile_dir)

    # Fingerprint RIÊNG cho từng acc, lưu lại để lần sau ỔN ĐỊNH (đổi liên tục = cờ bot)
    fp_path = Path(profile_dir) / "fingerprint.json"
    try:
        fp = json.loads(fp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fp = fresh_fingerprint()
        try:
            fp_path.write_text(json.dumps(fp, indent=2), encoding="utf-8")
        except OSError:
            pass

    browser = None
    solved = False
    try:
        browser = create_browser(browser_path, profile_dir, window_pos)

        # 0a) Anti-detect (ẩn webdriver/cdc_, spoof canvas/WebGL riêng) -> GIẢM wave.
        #     Tiêm TRƯỚC mọi thứ để navigator.webdriver=false ngay từ document đầu.
        try:
            browser.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                                    {"source": build_stealth_js(fp)})
        except Exception as e:
            print(f"  [!] Loi tiem stealth: {e}")

        # 0b) Hook đọc wave/variant FunCaptcha TRƯỚC khi mở trang (bắt /fc/gfct/)
        install_arkose_hook(browser)

        # 1) Set cookie qua CDP
        print("  [*] Set cookie...")
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

        # 2) Mở thẳng trang challenge captcha của phần GỠ KHOÁ
        print("  [*] Mở trang captcha (unlock)...")
        try:
            browser.execute_cdp_cmd("Page.enable", {})
            browser.execute_cdp_cmd("Page.navigate", {"url": url})
        except Exception:
            try:
                browser.get(url)
            except Exception:
                pass

        # 3) Chờ trang captcha render ổn định rồi mới nạp extension
        wait_challenge_ready(browser, timeout=45)
        time.sleep(2)

        # 4) Nạp OMOcaptcha runtime -> extension tự giải; hoặc để GIẢI TAY
        if use_extension:
            install_omocaptcha_extension(browser)
            print(f"  [*] OMOcaptcha đang tự giải cho {user}...")
        else:
            # --noextension: KHÔNG nạp omo, bạn tự giải captcha trong cửa sổ Chrome.
            # Giải xong tool tự phát hiện (verify) và đóng Chrome. Cho nhiều thời gian.
            max_wait = max(max_wait, 600)
            print(f"  [TAY] Tự GIẢI captcha trong cửa sổ Chrome cho {user}. "
                  f"Xong tool tự đóng (chờ tối đa {max_wait}s).")

        # 5) CHECK LIÊN TỤC (giống helpsolve1): cứ 5s xem đã HẾT captcha lock chưa.
        #    Mỗi vòng đẩy lại unlock (chỉ ăn sau khi OMOcaptcha giải xong) rồi kiểm
        #    tra acc live lại. Hết khoá -> ĐÓNG CHROME ngay.
        start = time.time()
        last_status = ""
        last_kinds = ""
        url_shown = False
        while time.time() - start < max_wait:
            time.sleep(5)
            # Browser bị đóng tay?
            try:
                _ = browser.current_window_handle
            except Exception:
                solved = verify_unlocked(cookie)
                break
            # Đọc tiến độ FunCaptcha: omo giải tới wave nào, game gì, có nạp lại/lỗi không
            try:
                ark = read_arkose_info(browser)
            except Exception:
                ark = {}
            if ark.get("url") and not url_shown:
                print(f"    [fc] url: {ark['url'][:110]}")
                url_shown = True
            status = format_arkose(ark)
            if status != last_status:
                print(f"    [fc] {user}: {status}")
                last_status = status
            # CHECK LIEN TUC loai captcha thuc te dang hien tren trang.
            # Hook fc chi thay FunCaptcha; neu Roblox tra reCaptcha v2 /
            # hCaptcha thi phai bat dung loai do trong configs.json moi giai.
            kinds = detect_captcha_kinds(browser)
            kstr = format_captcha_kinds(kinds)
            if kstr != last_kinds:
                print(f"    [type] {user}: {kstr}")
                log_captcha_kinds(user, result.get("uid"), kinds)
                last_kinds = kstr
            # Đã giải HẾT wave chưa? (answered >= waves, hoặc Arkose báo solved)
            aw = ark.get("waves")
            an = ark.get("answered") or 0
            done_solving = (ark.get("solved") is True) or (bool(aw) and an >= aw)
            # Giải xong -> nộp TOKEN THẬT để gỡ khoá. Chưa xong -> chỉ verify (phòng
            # khi Roblox tự hoàn tất). try_complete_unlock đọc token từ browser.
            ok = try_complete_unlock(cookie, info, browser) if done_solving \
                else verify_unlocked(cookie)
            if ok:
                print(f"  [+] ĐÃ GỠ KHOÁ {user} sau ~{int(time.time() - start)}s -> đóng Chrome.")
                solved = True
                break

        if not solved:
            try:
                final = format_arkose(read_arkose_info(browser))
            except Exception:
                final = "?"
            print(f"  [!] Hết {max_wait}s chưa gỡ {user}, đóng Chrome. Captcha cuối: {final}")

    except Exception as e:
        print(f"  [!] Lỗi khi giải: {e}")
        solved = verify_unlocked(cookie)
    finally:
        if browser:
            try:
                browser.quit()
            except Exception:
                pass

    status = "SOLVED" if solved else "FAILED"
    try:
        with open(RESULTS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{user}|{result.get('uid')}|{info['id']}|{status}\n")
    except OSError:
        pass
    return solved


# ============================================================
#  LOAD ACC
# ============================================================
def load_accounts(path):
    p = Path(path)
    if not p.exists():
        print(f"[!] Không thấy file: {p}")
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        _, cookie = parse_account_line(ln)
        if cookie:
            out.append(cookie)
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Check + gỡ CAPTCHA LOCK. Mặc định dùng OMOcaptcha tự giải.")
    ap.add_argument("--noextension", action="store_true",
                    help="KHÔNG nạp OMOcaptcha -> tự GIẢI TAY trong cửa sổ Chrome")
    ap.add_argument("--file", default=None,
                    help="File cookie đầu vào (mặc định accounts.txt; vd omo_queue.txt từ solvecaplockapi)")
    args = ap.parse_args()
    use_ext = not args.noextension
    acc_file = Path(args.file) if args.file else ACC_FILE_DEFAULT
    if not acc_file.is_absolute():
        acc_file = ROOT_DIR / acc_file

    print("=" * 60)
    mode = "OMOcaptcha Auto" if use_ext else "GIẢI TAY (--noextension)"
    print(f"  Roblox CAPTCHA LOCK solver - {mode}")
    print("=" * 60)

    if use_ext:
        if not (ROOT_DIR / "omocaptcha" / "manifest.json").exists():
            print("[!] KHÔNG thấy thư mục 'omocaptcha' có manifest.json!")
            return
        write_omocaptcha_source_key(None)   # bật power_on (key đã có trong configs.json)
        # Key omo hết hạn/bị xoá -> extension báo "API key does not exist" trong
        # từng cửa sổ. Check 1 lần ở đây, sai thì dừng luôn cho khỏi mở browser.
        ok, msg = check_omocaptcha_key()
        print(f"[*] OMOcaptcha: {msg}")
        if not ok:
            print("[!] Key omo không dùng được -> sửa 'api_key' trong "
                  "omocaptcha/configs.json rồi chạy lại.")
            return
    patch_chromedriver_cdc()                # ẩn dấu vết chromedriver (giảm wave captcha)

    cfg = load_config()
    solve_browsers = max(1, cfg.get("solveBrowsers", 3))
    solve_timeout = int(cfg.get("solveTimeout", 180))   # giây chờ omo giải mỗi acc
    browser_path = find_browser(str(cfg.get("browserPath", "")).strip())
    if not browser_path:
        print("[!] Không tìm thấy Chrome/Chromium! Cài Chrome hoặc set 'browserPath' trong config.json.")
        return
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    accounts = load_accounts(acc_file)
    if not accounts:
        print(f"[!] {acc_file} không có cookie hợp lệ.")
        return
    total = len(accounts)
    print(f"[+] Auto check {total} cookie trong {acc_file.name}\n")

    counters = {"clean": 0, "dead": 0, "faceid": 0, "banned": 0,
                "captcha": 0, "solved": 0, "failed": 0, "error": 0, "other": 0}
    clock = threading.Lock()

    # Slot vị trí cửa sổ để nhiều browser không đè lên nhau
    WINDOW_W = 460
    slots = queue.Queue()
    for i in range(solve_browsers):
        slots.put((i * WINDOW_W, 0))

    def work(cookie, index):
        res = check_account(cookie, index, total)
        st = res["status"]
        with clock:
            counters[st] = counters.get(st, 0) + 1

        if st == "captcha":
            pos = slots.get()
            try:
                ok = solve_account(res, index, total, browser_path,
                                   window_pos=pos, max_wait=solve_timeout,
                                   use_extension=use_ext)
                with clock:
                    counters["solved" if ok else "failed"] += 1
            finally:
                slots.put(pos)

    max_workers = solve_browsers + 5
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(work, c, i) for i, c in enumerate(accounts, 1)]
        for _ in as_completed(futs):
            pass

    print("\n" + "=" * 60)
    print("  KẾT QUẢ:")
    print(f"    Clean:{counters['clean']}  Captcha:{counters['captcha']}  "
          f"Solved:{counters['solved']}  Failed:{counters['failed']}")
    print(f"    FaceID:{counters['faceid']}  Banned:{counters['banned']}  "
          f"Dead:{counters['dead']}  Error:{counters['error']+counters['other']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
    try:
        input("\nNhấn Enter để thoát...")
    except EOFError:
        pass
