import os
import json
import base64
import argparse
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

URL_AUTHENTICATED = "https://users.roblox.com/v1/users/authenticated"
URL_ACCOUNT_INFO = "https://accountinformation.roblox.com/v1/birthdate"
URL_VOICE = "https://voice.roblox.com/v1/settings"
URL_MOBILE = "https://www.roblox.com/mobileapi/userinfo"  
URL_MOD_NOT_APPROVED = "https://usermoderation.roblox.com/v2/not-approved"
URL_UNLOCK = "https://apis.roblox.com/account-unlock-api/v1/unlock"
URL_REACTIVATE = "https://usermoderation.roblox.com/v1/not-approved/reactivate"


def get_csrf_token(session: requests.Session, timeout: int = 15) -> str:
    for url in ("https://auth.roblox.com/v2/logout", URL_UNLOCK):
        try:
            r = session.post(url, timeout=timeout)
            tok = r.headers.get("x-csrf-token")
            if tok:
                return tok
        except requests.RequestException:
            continue
    return ""


def reactivate_account(session: requests.Session, timeout: int = 15) -> str:
    csrf = get_csrf_token(session, timeout)
    headers = {"x-csrf-token": csrf, "Content-Type": "application/json"} if csrf else {}
    try:
        r = session.post(URL_REACTIVATE, headers=headers, json={}, timeout=timeout)
    except requests.RequestException as e:
        return f"reactivate LỖI kết nối: {e}"

    body = r.text.strip()
    if r.status_code == 200:
        return "ĐÃ GỠ thành công (reactivate OK)"
    if r.status_code in (400, 403):
        # thường là chưa hết hạn hoặc không đủ điều kiện
        return f"KHÔNG gỡ được (HTTP {r.status_code}) - có thể CHƯA hết hạn: {body[:200]}"
    return f"reactivate HTTP {r.status_code}: {body[:200]}"


def _verify_after_reactivate(session: requests.Session, timeout: int = 15) -> str:
    """Sau khi reactivate trả 200: check LẠI acc thật sự thế nào.

    reactivate trả 200 KHÔNG chắc acc live (acc faceid cũng trả 200). Gọi lại
    authenticated -> 200 = LIVE thật; 403 -> probe xem faceid hay vẫn banned.
    Trả: 'LIVE' / 'FACEID' / 'STILL_BANNED'.
    """
    import time as _t
    r = None
    for att in range(4):
        try:
            r = session.get(URL_AUTHENTICATED, timeout=timeout)
        except requests.RequestException:
            return "STILL_BANNED"
        if r.status_code in (403, 429):
            _t.sleep(1.5 * (att + 1))     # rate limit -> chờ thử lại
            continue
        break
    if r.status_code == 200:
        return "LIVE"
    if r.status_code == 403:
        analysis, _ = probe_403(session, timeout)
        if analysis["status"] == "FACEID":
            return "FACEID"
        return "STILL_BANNED"
    return "STILL_BANNED"


def probe_403(session: requests.Session, timeout: int = 15) -> tuple:
    logs = []
    challenge_type = ""
    challenge_id = ""
    mod_status = None
    mod_data = {}

    csrf = get_csrf_token(session, timeout)
    headers = {"x-csrf-token": csrf, "Content-Type": "application/json"} if csrf else {}
    logs.append(f"[probe] csrf-token: {'có' if csrf else 'KHÔNG lấy được'}")

    # 1) POST unlock {} -> đọc challenge headers (KHÔNG đi tiếp bước mở khóa).
    #    Đây là chỗ PHÂN BIỆT (đã kiểm chứng trên acc thật):
    #      - CAPTCHA LOCK: challenge-type='captcha', metadata có unifiedCaptchaId/dataExchangeBlob.
    #      - FACE ID     : challenge-type='biometric', metadata có biometricType='personaLiveness'.
    #    metadata bị mã hoá base64 -> phải GIẢI MÃ rồi mới so khớp (nếu không sẽ vô tác dụng).
    has_captcha = False
    has_biometric = False
    try:
        r = session.post(URL_UNLOCK, headers=headers, json={}, timeout=timeout)
        challenge_type = (r.headers.get("rblx-challenge-type") or "").strip()
        challenge_id = (r.headers.get("rblx-challenge-id") or "").strip()
        challenge_meta = (r.headers.get("rblx-challenge-metadata") or "").strip()
        ctl = challenge_type.lower()
        try:
            meta_txt = base64.b64decode(
                challenge_meta + "=" * (-len(challenge_meta) % 4)
            ).decode("utf-8", "replace").lower() if challenge_meta else ""
        except (ValueError, UnicodeError):
            meta_txt = challenge_meta.lower()
        has_captcha = ("captcha" in ctl) or \
                      ("unifiedcaptchaid" in meta_txt) or ("dataexchangeblob" in meta_txt)
        has_biometric = ("biometric" in ctl) or ("personaliveness" in ctl) or \
                        ("biometrictype" in meta_txt) or ("personaliveness" in meta_txt) or \
                        ("liveness" in meta_txt)
        logs.append(f"[probe] {r.status_code} POST unlock | challenge-type='{challenge_type}' "
                    f"captcha={has_captcha} biometric={has_biometric}"
                    + (f" challenge-id='{challenge_id[:12]}...'" if challenge_id else ""))
    except requests.RequestException as e:
        logs.append(f"[probe] ERR POST unlock | {e}")

    # 2) GET usermoderation -> moderationStatus
    try:
        r2 = session.get(URL_MOD_NOT_APPROVED, headers=headers, timeout=timeout)
        body = r2.text.strip()
        logs.append(f"[probe] {r2.status_code} not-approved | {body[:250]}")
        try:
            mod_data = r2.json()
            restr = mod_data.get("restriction") or mod_data.get("moderation") or {}
            if isinstance(restr, dict):
                mod_status = restr.get("moderationStatus")
        except ValueError:
            pass
    except requests.RequestException as e:
        logs.append(f"[probe] ERR not-approved | {e}")

    analysis = classify_punishment(challenge_type, mod_status, mod_data,
                                   has_captcha, has_biometric)
    logs.append(f"[probe] -> {analysis['status']}/{analysis['kind']}: {analysis['message'][:250]}")
    return analysis, "\n".join(logs)


def classify_punishment(challenge_type: str, mod_status, mod_data: dict,
                        has_captcha: bool = False, has_biometric: bool = False) -> dict:
    ct = (challenge_type or "").lower()
    blob = json.dumps(mod_data).lower() if mod_data else ""

    # Lấy restriction để đọc thời hạn THẬT (endTime/durationSeconds có GIÁ TRỊ),
    # tránh bug cũ: khớp chuỗi 'duration' trúng key 'durationSeconds': null -> báo
    # nhầm mọi acc là "khóa tạm thời".
    restr = {}
    if isinstance(mod_data, dict):
        r = mod_data.get("restriction") or mod_data.get("moderation") or {}
        if isinstance(r, dict):
            restr = r
    has_real_duration = bool(restr.get("endTime")) or bool(restr.get("durationSeconds"))

    message = ""
    for key in ("punishmentMessageToUser", "message", "reason",
                "moderationMessage", "detail"):
        if isinstance(mod_data, dict) and mod_data.get(key):
            message = str(mod_data.get(key))
            break

    # 1) FACEID (ưu tiên CAO NHẤT): bước unlock trả challenge sinh trắc học
    #    (challenge-type='biometric', metadata biometricType='personaLiveness') ->
    #    acc bắt QUÉT MẶT trên phone. Đây là dấu hiệu tách bạch với captcha (đã
    #    kiểm chứng: acc face trả 'biometric', acc captcha trả 'captcha').
    if has_biometric or \
       any(k in ct for k in ("biometric", "personaliveness", "liveness", "persona")) or \
       any(k in blob for k in ("ageestimation", "facial", "faceid",
                               "liveness", "personaverification", "verifyage")):
        return {"status": "FACEID", "kind": "verify",
                "message": message or "Bị FACE ID - cần XÁC MINH/QUÉT MẶT (challenge biometric)"}

    # 2) Khóa vĩnh viễn / xóa (bằng chứng RÕ trong dữ liệu moderation) -> ban thật,
    #    ưu tiên trước captcha (acc bị xóa vẫn có thể lòi challenge captcha).
    if any(k in blob for k in ("permanent", "delete", "terminat", "poison")):
        return {"status": "BANNED", "kind": "permanent",
                "message": message or "Khóa vĩnh viễn / đã xóa"}

    # 3) Khóa TẠM THỜI THẬT: có endTime/durationSeconds CÓ GIÁ TRỊ, hoặc từ khóa
    #    suspend/temporary/expire rõ ràng. (KHÔNG dùng 'duration'/'enddate' chung
    #    chung nữa vì trúng key null.)
    if has_real_duration or any(k in blob for k in ("suspend", "temporary", "expir")):
        return {"status": "BANNED", "kind": "temporary",
                "message": message or "Khóa tạm thời"}

    # 4) CAPTCHA LOCK: acc bị khóa nhưng account-unlock-api bắt GIẢI CAPTCHA để tự
    #    mở (challenge-type='captcha'). KHÔNG phải ban thật, KHÔNG phải faceid (đã
    #    loại ở trên), KHÔNG có thời hạn cố định. Đây là dấu hiệu của accounts.txt.
    #    -> chỉ CHECK, tuyệt đối không giải captcha.
    if has_captcha or "captcha" in ct:
        return {"status": "CAPTCHA", "kind": "captcha",
                "message": message or ("Bị KHÓA CAPTCHA - gỡ bằng cách GIẢI CAPTCHA "
                                       "(không phải Face ID / không phải ban vĩnh viễn)")}

    # 5) CHỈ báo BANNED khi moderationStatus == 2 HOẶC blob có từ khóa khóa RÕ RÀNG.
    #    KHÔNG báo ban chỉ vì 'mod_data' có dữ liệu (acc live cũng có thể trả JSON
    #    rỗng/khác) -> đây là bug cũ khiến acc LIVE bị báo BANNED nhầm.
    if mod_status == 2 or "moderat" in blob or "punish" in blob or "restrict" in blob:
        return {"status": "BANNED", "kind": "unknown",
                "message": message or f"Bị khóa (moderationStatus={mod_status})"}

    # Không có bằng chứng ban rõ -> KHÔNG kết luận ban (để tầng trên xử tiếp)
    return {"status": "UNKNOWN", "kind": "unknown", "message": message}


def explain_message(msg: str) -> str:
    m = (msg or "").lower()
    if "moderat" in m:
        return "Tài khoản bị KIỂM DUYỆT/KHÓA (moderated) - thường là ban tạm thời hoặc vĩnh viễn"
    if "terminat" in m:
        return "Tài khoản bị KHÓA VĨNH VIỄN (terminated)"
    if "ban" in m:
        return "Tài khoản bị BAN"
    if "token validation failed" in m:
        return "Cookie SAI/HẾT HẠN (không phải bị khóa)"
    if "challenge" in m or "verif" in m:
        return "Cần XÁC MINH (challenge) - chưa chắc bị khóa"
    return msg or "Không rõ lý do"


def fetch_user_basic(session: requests.Session, timeout: int = 15) -> dict:
    info = {"username": None, "user_id": None, "robux": None}
    try:
        r = session.get(URL_MOBILE, timeout=timeout)
        if r.status_code == 200:
            d = r.json()
            info["username"] = d.get("UserName")
            info["user_id"] = d.get("UserID")
            info["robux"] = d.get("RobuxBalance")
    except (requests.RequestException, ValueError):
        pass
    return info


def dump_response(r: requests.Response) -> str:
    lines = [f"HTTP {r.status_code} {r.reason} | URL: {r.url}"]

    # Các header Roblox hay dùng để báo lý do
    for h in ("x-csrf-token", "rblx-challenge-type", "rblx-challenge-id",
              "x-frame-options", "www-authenticate"):
        if h in r.headers:
            lines.append(f"  {h}: {r.headers[h]}")

    body = r.text.strip()
    if len(body) > 1000:
        body = body[:1000] + "...(cắt bớt)"
    lines.append(f"  body: {body if body else '(rỗng)'}")
    return "\n".join(lines)


def parse_403_detail(r: requests.Response) -> str:
    detail = "Bị từ chối (403)"
    try:
        data = r.json()
        errs = data.get("errors") or []
        if errs:
            msgs = []
            for e in errs:
                msg = e.get("message", "")
                ud = e.get("userFacingMessage", "")
                # message gốc + giải thích tiếng Việt
                part = explain_message(msg)
                if msg:
                    part += f"  [gốc: \"{msg}\"]"
                if ud and ud != msg:
                    part += f" [hiển thị: \"{ud}\"]"
                msgs.append(part)
            return " ; ".join(msgs)
    except (ValueError, json.JSONDecodeError):
        pass

    # Không phải JSON -> đoán theo nội dung text
    txt = r.text.lower()
    if "terminat" in txt:
        return "403: Tài khoản bị khóa vĩnh viễn (terminated)"
    if "ban" in txt:
        return "403: Tài khoản bị ban"
    if "moderat" in txt:
        return "403: Tài khoản bị kiểm duyệt (moderated)"
    if "challenge" in r.headers.get("rblx-challenge-type", "").lower() or \
       "rblx-challenge-type" in r.headers:
        return "403: Yêu cầu xác minh (challenge) - có thể cần verify, chưa chắc bị khóa"
    return detail


def make_session(cookie: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36",
        "Accept": "application/json",
    })
    s.cookies.set(".ROBLOSECURITY", cookie.strip(), domain=".roblox.com")
    return s


def check_cookie(cookie: str, timeout: int = 15, do_reactivate: bool = False) -> dict:
    """
    Trả về dict mô tả trạng thái cookie.
    Nếu do_reactivate=True và acc bị warn/ban tạm: thử gọi reactivate
    (Roblox tự từ chối nếu chưa hết hạn). Mặc định CHỈ check, không đổi gì.
    {
        "status": "LIVE" | "DEAD" | "BANNED" | "FACEID" | "CAPTCHA" | "ERROR",
        "username": str | None,
        "user_id": int | None,
        "detail": str,
    }
    """
    result = {
        "status": "DEAD",
        "username": None,
        "user_id": None,
        "detail": "",
        "raw": "",          # log chi tiết khi bị từ chối (403...)
    }

    if not cookie or len(cookie.strip()) < 50:
        result["status"] = "DEAD"
        result["detail"] = "Cookie rỗng hoặc quá ngắn"
        return result

    session = make_session(cookie)

    # 1) Kiểm tra đăng nhập cơ bản.
    #    RETRY khi gặp 403/429: chạy nhiều luồng dễ bị RATE LIMIT (Roblox trả 403
    #    tạm thời) -> KHÔNG phải ban. Retry với delay -> nếu ra 200 thì LIVE.
    import time as _t
    r = None
    for att in range(4):
        try:
            r = session.get(URL_AUTHENTICATED, timeout=timeout)
        except requests.RequestException as e:
            result["status"] = "ERROR"
            result["detail"] = f"Lỗi kết nối: {e}"
            return result
        if r.status_code in (403, 429):
            # có thể rate limit -> chờ rồi thử lại
            _t.sleep(1.5 * (att + 1))
            continue
        break   # 200/401/khác -> dừng retry

    if r.status_code == 401:
        result["status"] = "DEAD"
        result["detail"] = "Không xác thực được (401) - cookie hết hạn/sai"
        return result

    if r.status_code == 403:
        # 403 ở đây thường là banned/moderated hoặc cần verify
        detail = parse_403_detail(r)
        result["raw"] = dump_response(r)

        # Cố lấy tên tài khoản dù bị khóa (mobileapi thường vẫn trả)
        info = fetch_user_basic(session)
        result["username"] = info["username"]
        result["user_id"] = info["user_id"]

        # "Token Validation Failed" thực ra là cookie chết, không phải ban
        if "cookie sai/hết hạn" in detail.lower():
            result["status"] = "DEAD"
            result["detail"] = detail
            return result

        # Phân tích trang /not-approved để phân biệt BANNED vs FACEID
        analysis, log = probe_403(session)
        result["raw"] += "\n" + log

        if analysis["status"] == "FACEID":
            # FACEID: KHÔNG reactivate (reactivate không gỡ được faceid)
            result["status"] = "FACEID"
            result["detail"] = "Cần XÁC MINH tuổi/khuôn mặt (Face ID) - chưa bị ban: " + analysis["message"]
        elif analysis["status"] == "CAPTCHA":
            # CAPTCHA LOCK: acc gỡ được bằng cách GIẢI CAPTCHA (không phải faceid,
            # không phải ban). KHÔNG reactivate (reactivate không gỡ captcha), và
            # theo yêu cầu: CHỈ CHECK, tuyệt đối không tự giải captcha.
            result["status"] = "CAPTCHA"
            result["detail"] = analysis["message"]
        elif analysis["status"] == "BANNED":
            kind_vi = {
                "permanent": "KHÓA VĨNH VIỄN",
                "temporary": "KHÓA TẠM THỜI",
                "unknown": "BỊ KHÓA (không rõ loại)",
            }.get(analysis["kind"], "BỊ KHÓA")
            result["status"] = "BANNED"
            result["detail"] = f"{kind_vi}: {analysis['message']}"

            # CHỈ reactivate cho BANNED (không phải faceid/vĩnh viễn).
            if do_reactivate and analysis["kind"] != "permanent":
                res_msg = reactivate_account(session, timeout)
                result["detail"] += f"  |  [reactivate] {res_msg}"
                result["raw"] += f"\n[reactivate] {res_msg}"
                if "thành công" in res_msg:
                    # VERIFY LẠI: reactivate trả 200 KHÔNG chắc acc đã live
                    # (acc faceid cũng trả 200). Check lại authenticated + probe.
                    verify = _verify_after_reactivate(session, timeout)
                    if verify == "LIVE":
                        result["status"] = "REACTIV"
                        result["detail"] = "ĐÃ REACTIVATE thành công - acc LIVE lại"
                    elif verify == "FACEID":
                        result["status"] = "FACEID"
                        result["detail"] = ("Reactivate trả 200 nhưng acc VẪN cần "
                                            "Face ID -> không phải live")
                    else:
                        # vẫn 403/không rõ -> giữ BANNED
                        result["detail"] += "  |  [verify] acc vẫn chưa live sau reactivate"
        else:
            # KHÔNG rõ ban (analysis=UNKNOWN). 403 có thể do challenge tạm/rate
            # limit chứ chưa chắc ban. THỬ verify lại xem acc có live không.
            verify = _verify_after_reactivate(session, timeout)
            if verify == "LIVE":
                result["status"] = "LIVE"
                result["detail"] = "Cookie hợp lệ (403 tạm thời, verify lại LIVE)"
            elif verify == "FACEID":
                result["status"] = "FACEID"
                result["detail"] = "Cần Face ID"
            else:
                # verify vẫn không live + không có bằng chứng ban rõ -> UNKNOWN,
                # KHÔNG khẳng định ban để tránh báo ban nhầm.
                result["status"] = "UNKNOWN"
                result["detail"] = f"403 không rõ lý do (chưa chắc ban): {detail}"
        return result
    elif r.status_code != 200:
        result["status"] = "ERROR"
        result["detail"] = f"HTTP {r.status_code} không mong đợi"
        return result

    # Lấy thông tin user nếu có
    try:
        data = r.json()
        result["username"] = data.get("name")
        result["user_id"] = data.get("id")
    except (ValueError, json.JSONDecodeError):
        pass

    # Tới đây cookie xác thực OK -> LIVE (mặc định)
    result["status"] = "LIVE"
    result["detail"] = "Cookie hợp lệ"

    # 2) Phân biệt BANNED: thử đọc thông tin tài khoản
    try:
        r2 = session.get(URL_ACCOUNT_INFO, timeout=timeout)
        if r2.status_code == 403:
            # Có thể bị moderated/banned dù authenticated trả 200 (hiếm)
            txt = r2.text.lower()
            if "ban" in txt or "moderat" in txt or "terminat" in txt:
                result["status"] = "BANNED"
                result["detail"] = parse_403_detail(r2)
                result["raw"] = dump_response(r2)
                return result
    except requests.RequestException:
        pass

    # 3) Kiểm tra Face ID / verification qua voice settings
    #    Khi tài khoản bật xác minh khuôn mặt, voice settings thường yêu cầu
    #    verification và trả cờ tương ứng.
    try:
        r3 = session.get(URL_VOICE, timeout=timeout)
        if r3.status_code == 200:
            vd = r3.json()
            # Quét toàn bộ phản hồi để tìm cờ liên quan xác minh khuôn mặt
            txt = json.dumps(vd).lower()
            if "faceid" in txt or "face_id" in txt or "facialverification" in txt:
                result["status"] = "FACEID"
                result["detail"] = "Tài khoản bật xác minh khuôn mặt (Face ID)"
                return result
    except (requests.RequestException, ValueError):
        pass

    return result


def format_line(cookie: str, res: dict) -> str:
    preview = cookie.strip()[:25] + "..." if len(cookie.strip()) > 25 else cookie.strip()
    user = res.get("username") or "?"
    uid = res.get("user_id") or "?"
    return (f"[{res['status']:<7}] user={user:<20} "
            f"id={uid} | {res['detail']} ({preview})")


def parse_account_line(line: str):
    """Tách 1 dòng thành (full_line, cookie).

    Hỗ trợ 2 định dạng:
      - user:pass:cookie   (accounts.txt)  -> cookie = phần sau 2 dấu ':' đầu
      - chỉ cookie         (cookie.txt)   -> cả dòng là cookie
    Cookie Roblox bắt đầu bằng '_|WARNING:' nên có nhiều ':' -> tách đúng.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None, None
    # cookie luôn chứa '_|WARNING' -> tìm vị trí đó
    if "_|WARNING" in line:
        idx = line.index("_|WARNING")
        cookie = line[idx:].strip()
        return line, cookie
    # không có WARNING: nếu có dạng user:pass:cookie -> lấy phần thứ 3 trở đi
    parts = line.split(":", 2)
    if len(parts) == 3:
        return line, parts[2].strip()
    # còn lại: cả dòng là cookie
    return line, line


def run_file(path: str, do_reactivate: bool = False, threads: int = 30):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_lines = f.read().splitlines()
    except FileNotFoundError:
        print(f"Không tìm thấy file: {path}")
        return

    # parse -> danh sách (số_dòng_thật, full_line, cookie).
    # GIỮ số dòng thật trong file để in RÕ thread đang check cookie DÒNG NÀO
    # (chạy đa luồng nên thứ tự hoàn thành ≠ thứ tự dòng).
    accounts = []
    for lineno, ln in enumerate(raw_lines, 1):
        if not ln.strip():
            continue
        full, cookie = parse_account_line(ln)
        if cookie:
            accounts.append((lineno, full, cookie))

    if not accounts:
        print("File không có account/cookie nào.")
        return

    counts = {"LIVE": 0, "REACTIV": 0, "DEAD": 0, "BANNED": 0,
              "FACEID": 0, "CAPTCHA": 0, "UNKNOWN": 0, "ERROR": 0}
    buckets = {"live": [], "banned": [], "faceid": [], "captcha": []}
    lock = threading.Lock()          # khóa cho counts/buckets/print (đa luồng)
    done = [0]
    n = len(accounts)

    print(f"Đang check {n} account bằng {threads} luồng..."
          + (" (có gỡ warn/ban tạm)" if do_reactivate else "") + "\n")

    def worker(item):
        lineno, full, cookie = item
        res = check_cookie(cookie, do_reactivate=do_reactivate)
        st = res["status"]
        with lock:
            counts[st] = counts.get(st, 0) + 1
            done[0] += 1
            # REACTIV (gỡ warn/ban tạm) TÍNH LÀ LIVE
            if st in ("LIVE", "REACTIV"):
                buckets["live"].append(full)
            elif st == "BANNED":
                buckets["banned"].append(full)
            elif st == "FACEID":
                buckets["faceid"].append(full)
            elif st == "CAPTCHA":
                buckets["captcha"].append(full)
            # In RÕ số DÒNG của cookie này (không phải thứ tự hoàn thành) +
            # tiến độ tổng (đã xong/tổng) để dễ theo dõi khi chạy đa luồng.
            print(f"[dòng {lineno:>4}] ({done[0]}/{n} xong) " + format_line(cookie, res))

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(worker, acc) for acc in accounts]
        for _ in as_completed(futures):
            pass

    # Ghi các file .txt phân loại (live/banned/faceid/captcha), giữ nguyên format gốc.
    # QUAN TRỌNG: GHI THÊM (append) vào cuối file, KHÔNG BAO GIỜ xoá nội dung cũ.
    #   - Nội dung cũ trong file luôn được GIỮ NGUYÊN.
    #   - Chỉ thêm acc CHƯA có sẵn (bỏ qua trùng) để file không phình vì lặp.
    for name, lines in buckets.items():
        fname = f"{name}.txt"
        if not lines:
            continue                       # không có acc loại này -> không đụng tới file
        try:
            # Đọc acc đã có sẵn để tránh ghi trùng
            existing = set()
            if os.path.exists(fname):
                with open(fname, "r", encoding="utf-8") as fi:
                    for ln in fi:
                        s = ln.strip()
                        if s:
                            existing.add(s)

            new_lines = [ln for ln in lines if ln.strip() and ln.strip() not in existing]
            if not new_lines:
                print(f"[*] '{fname}': {len(lines)} acc đều đã có sẵn -> không thêm gì.")
                continue

            # Nếu file cũ chưa kết thúc bằng '\n' thì thêm 1 dòng trống trước khi nối
            need_nl = False
            if os.path.exists(fname) and os.path.getsize(fname) > 0:
                with open(fname, "rb") as fb:
                    fb.seek(-1, 2)
                    need_nl = fb.read(1) != b"\n"

            with open(fname, "a", encoding="utf-8") as fo:   # 'a' = GHI THÊM, không xoá
                if need_nl:
                    fo.write("\n")
                fo.write("\n".join(new_lines) + "\n")
            print(f"[*] Đã THÊM {len(new_lines)} acc vào '{fname}' "
                  f"(giữ nguyên {len(existing)} acc cũ)")
        except Exception as e:
            print(f"[!] Lỗi ghi file '{fname}': {e}")

    print("\n--- Tổng kết ---")
    for k, v in counts.items():
        print(f"{k:<7}: {v}")
    print(f"\n(REACTIV được tính vào file 'live'. LIVE trong file = {len(buckets['live'])})")


def main():
    parser = argparse.ArgumentParser(
        description="Roblox cookie checker (chỉ check, không hành động khác)."
    )
    parser.add_argument("cookie", nargs="?", help="Một cookie .ROBLOSECURITY")
    parser.add_argument("-f", "--file",
                        help="File acc (mỗi dòng 1 cookie, hoặc user:pass:cookie). "
                             "Mặc định: accounts.txt")
    parser.add_argument("--no-reactivate", action="store_true",
                        help="CHỈ check, không tự gỡ warn/ban tạm")
    parser.add_argument("-t", "--threads", type=int, default=30,
                        help="Số luồng check song song (mặc định 30)")
    args = parser.parse_args()

    # Mặc định tự gỡ warn/ban tạm đã hết hạn cho acc của bạn
    do_react = not args.no_reactivate
    threads = max(1, args.threads)

    if args.file:
        run_file(args.file, do_reactivate=do_react, threads=threads)
    elif args.cookie:
        res = check_cookie(args.cookie, do_reactivate=do_react)
        print(format_line(args.cookie, res))
    else:
        # Mặc định: đọc accounts.txt (mỗi dòng chỉ 1 cookie)
        run_file("accounts.txt", do_reactivate=do_react, threads=threads)


if __name__ == "__main__":
    main()
    input("\nNhấn Enter để thoát...")
