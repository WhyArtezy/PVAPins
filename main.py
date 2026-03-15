#!/usr/bin/env python3
"""
PVAPins - WhatsApp Mexico
Multi-App · Multi-Thread · Target OTP
"""

import requests
import time
import sys
import re
import threading
from datetime import datetime
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.console import Group
from rich import box

# ─── LOAD CONFIG ───────────────────────────────────────────────────────────────
def load_config(path: str = "config.txt") -> dict:
    cfg = {}
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.split("#")[0].strip()  # hapus komentar inline
                    cfg[key] = val
    except FileNotFoundError:
        print(f"[ERROR] {path} tidak ditemukan.")
        sys.exit(1)
    return cfg

_cfg = load_config()

API_KEY         = _cfg.get("API_KEY", "")
COUNTRY         = _cfg.get("COUNTRY", "Mexico")
POLL_DELAY      = int(_cfg.get("POLL_DELAY", 5))
POLL_MAX        = int(_cfg.get("POLL_MAX", 180))
ORDER_DELAY     = float(_cfg.get("ORDER_DELAY", 0.3))
RETRY_DELAY     = int(_cfg.get("RETRY_DELAY", 10))
RETRY_MAX       = int(_cfg.get("RETRY_MAX", 60))
MAX_REQ_PER_MIN = int(_cfg.get("MAX_REQ_PER_MIN", 60))
MAX_NEW_PER_MIN = int(_cfg.get("MAX_NEW_PER_MIN", 3))
MAX_LOG         = int(_cfg.get("MAX_LOG", 12))
TG_TOKEN        = _cfg.get("TG_TOKEN", "")
TG_CHAT_ID      = _cfg.get("TG_CHAT_ID", "")

BASE_URL = "https://api.pvapins.com/user/api"

WA_APPS = {
    "1":  "Whatsapp1",
    "2":  "Whatsapp9",
    "3":  "Whatsapp24",
    "4":  "Whatsapp33",
    "5":  "Whatsapp42",
    "6":  "Whatsapp56",
    "7":  "Whatsapp57",
    "8":  "Whatsapp58",
    "9":  "Whatsapp60",
}

console = Console(highlight=False)

# ─── SHARED STATE ──────────────────────────────────────────────────────────────
lock        = threading.Lock()
done_event  = threading.Event()
results     = []
active_nums = {}
log_lines   = []
stats       = {"retry": 0, "cancel": 0, "collected": 0}

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def now() -> str:
    return datetime.now().strftime("%H:%M:%S")

def log(level: str, msg: str):
    with lock:
        log_lines.append((now(), level, msg))
        if len(log_lines) > 200:
            log_lines.pop(0)

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def tg_notify(msg: str):
    """Kirim pesan ke Telegram. Non-blocking, jalan di thread terpisah."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    def _send():
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()

# ─── RATE LIMITER ──────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, max_per_min: int):
        self._lock     = threading.Lock()
        self._interval = 60.0 / max_per_min
        self._last_ts  = 0.0

    def acquire(self):
        while True:
            with self._lock:
                elapsed = time.time() - self._last_ts
                if elapsed >= self._interval:
                    self._last_ts = time.time()
                    return
                wait = self._interval - elapsed
            time.sleep(wait)

order_limiter   = RateLimiter(MAX_REQ_PER_MIN)
success_limiter = RateLimiter(MAX_NEW_PER_MIN)

# ─── API ───────────────────────────────────────────────────────────────────────
def api_get(endpoint: str, params: dict):
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
        r.raise_for_status()
        try:
            data = r.json()
            # Debug: log raw response get_number
            if "get_number" in endpoint:
                log("INFO", f"RAW get_number: {str(data)[:120]}")
            return data
        except Exception:
            text = r.text.strip()
            if "get_number" in endpoint:
                log("INFO", f"RAW get_number text: {text[:120]}")
            return text
    except requests.exceptions.RequestException as e:
        if "get_number" in endpoint:
            log("WARNING", f"get_number exception: {str(e)[:80]}")
        return None

def check_balance(key: str):
    data = api_get("get_balance.php", {"customer": key})
    if isinstance(data, dict):
        return data.get("balance") or data.get("data")
    if isinstance(data, str) and "not found" not in data.lower():
        return data
    return None

def get_number(key: str, app: str):
    data = api_get("get_number.php", {
        "customer": key, "app": app, "country": COUNTRY,
    })
    if data is None:
        return None, ("timeout/koneksi", None)

    # Ekstrak field "data" dari response
    if isinstance(data, dict):
        raw_num = str(data.get("data", ""))
        code    = str(data.get("code", ""))
        # Sukses: code 100 atau data berisi angka
        num_clean = re.sub(r'[^\d]', '', raw_num)   # hapus +, spasi, dll
        if code == "100" or (num_clean and len(num_clean) >= 8):
            log("INFO", f"get_number raw={raw_num} code={code}")
            return num_clean, None
        err = raw_num if raw_num else "unknown error"
    elif isinstance(data, str):
        num_clean = re.sub(r'[^\d]', '', data)
        if num_clean and len(num_clean) >= 8:
            return num_clean, None
        err = data
    else:
        return None, ("unknown response", None)

    log("WARNING", f"get_number gagal raw={data}")
    m = re.search(r'after\s+(\d+)\s*s', err, re.IGNORECASE)
    return None, (err, int(m.group(1)) if m else None)

def poll_sms(key: str, number: str, app: str):
    data = api_get("get_sms.php", {
        "customer": key, "number": number, "country": COUNTRY, "app": app,
    })
    raw = None
    if isinstance(data, dict):
        raw = str(data.get("data", ""))
        # Cek pesan error spesifik — jangan pakai "not" saja karena terlalu luas
        err_phrases = ["not found", "not received", "expired", "error 102", "customer not"]
        if any(x in raw.lower() for x in err_phrases):
            return None, None
    elif isinstance(data, list) and data:
        raw = data[0].get("message", "")
    elif isinstance(data, str):
        raw = data
    if raw:
        m = re.search(r'\b(\d{4,8})\b', raw)
        if m:
            return m.group(1), raw
    return None, None

# ─── WORKER ────────────────────────────────────────────────────────────────────
def worker(tid: int, key: str, app: str, target: int, delay_start: float = 0):
    time.sleep(delay_start)
    fail_count = 0

    with lock:
        active_nums[tid] = {
            "app": app, "number": "-", "otp": "-",
            "status": "INIT", "masuk": "-", "tunggu": "-",
        }

    while not done_event.is_set():
        time.sleep(ORDER_DELAY)

        with lock:
            active_nums[tid].update({"status": "ORDER", "number": "-"})

        order_limiter.acquire()
        number, err = get_number(key, app)

        if not number:
            fail_count += 1
            with lock:
                stats["retry"] += 1
                active_nums[tid]["status"] = "RETRY"
            err_msg, suggested = err
            wait = suggested if (suggested and suggested > 0) else min(RETRY_DELAY * fail_count, RETRY_MAX)
            log("WARNING", f"T{tid:02d} {app} Gagal -> {err_msg} (retry {wait}s)")
            time.sleep(wait)
            continue

        # Log & notif LANGSUNG setelah dapat nomor — tidak blokir dulu
        fail_count = 0
        start_ts = time.time()

        with lock:
            active_nums[tid].update({
                "number": number, "status": "AKTIF",
                "masuk": now(), "tunggu": "00:00", "otp": "Menunggu...",
            })

        log("INFO", f"T{tid:02d} {app} +{number} Polling OTP...")
        tg_notify(
            f"📲 <b>Nomor Didapat</b>\n"
            f"📱 Nomor  : +{number}\n"
            f"📦 App    : {app}\n"
            f"🧵 Thread : T{tid:02d}\n"
            f"⏰ Waktu  : {now()}"
        )

        for i in range(1, POLL_MAX + 1):
            if done_event.is_set():
                with lock:
                    active_nums[tid]["status"] = "STOP"
                return

            time.sleep(POLL_DELAY)
            elapsed = int(time.time() - start_ts)
            elapsed_str = f"{elapsed // 60:02d}:{elapsed % 60:02d}"

            with lock:
                active_nums[tid]["tunggu"] = elapsed_str

            otp, full_msg = poll_sms(key, number, app)

            if otp:
                with lock:
                    existing = {r["number"] for r in results}
                    if number not in existing and len(results) < target:
                        results.append({
                            "otp": otp, "number": number, "msg": full_msg,
                            "tid": tid, "app": app, "ts": now(),
                        })
                        stats["collected"] = len(results)
                        if len(results) >= target:
                            done_event.set()
                            tg_notify(
                                f"🎯 <b>Semua Target Selesai!</b>\n"
                                f"📊 Terkumpul : {len(results)}/{target} OTP\n"
                                f"⏰ Waktu     : {now()}"
                            )
                    active_nums[tid].update({"otp": otp, "status": "OTP OK"})

                log("SUCCESS", f"T{tid:02d} {app} +{number} OTP={otp} ({elapsed_str})")
                tg_notify(
                    f"✅ <b>OTP Berhasil</b>\n"
                    f"📱 Nomor  : +{number}\n"
                    f"🔑 OTP    : <b>{otp}</b>\n"
                    f"📦 App    : {app}\n"
                    f"⏱ Waktu  : {elapsed_str}\n"
                    f"🧵 Thread : T{tid:02d}"
                )
                return

            if i >= POLL_MAX:
                with lock:
                    stats["cancel"] += 1
                    active_nums[tid]["status"] = "TIMEOUT"
                log("WARNING", f"T{tid:02d} {app} +{number} 15 mnt timeout -> cancel")
                tg_notify(
                    f"⏰ <b>Timeout Nomor</b>\n"
                    f"📱 Nomor  : +{number}\n"
                    f"📦 App    : {app}\n"
                    f"🧵 Thread : T{tid:02d}"
                )
                break

        # Throttle nomor baru: maks 3/menit — tunggu di sini sebelum order berikutnya
        if not done_event.is_set():
            success_limiter.acquire()
        time.sleep(2)

# ─── DASHBOARD ─────────────────────────────────────────────────────────────────
HEADER = """\
██████╗ ██╗   ██╗ █████╗ ██████╗ ██╗███╗   ██╗███████╗
██╔══██╗██║   ██║██╔══██╗██╔══██╗██║████╗  ██║██╔════╝
██████╔╝██║   ██║███████║██████╔╝██║██╔██╗ ██║███████╗
██╔═══╝ ╚██╗ ██╔╝██╔══██║██╔═══╝ ██║██║╚██╗██║╚════██║
██║      ╚████╔╝ ██║  ██║██║     ██║██║ ╚████║███████║
╚═╝       ╚═══╝  ╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═══╝╚══════╝"""

STATUS_STYLE = {
    "AKTIF": "green", "OTP OK": "bold green",
    "RETRY": "yellow", "TIMEOUT": "red",
    "ORDER": "cyan", "STOP": "dim", "INIT": "dim",
}

LEVEL_STYLE = {
    "WARNING": ("[yellow]WARNING[/yellow]", "[yellow]![/yellow]"),
    "INFO":    ("[cyan]INFO[/cyan]",         "[cyan]>[/cyan]"),
    "SUCCESS": ("[bold green]SUCCESS[/bold green]", "[bold green]✓[/bold green]"),
}

def make_dashboard(balance: str, target: int, selected_apps: list, total_threads: int) -> Group:
    with lock:
        snap_active  = dict(active_nums)
        snap_results = list(results)
        snap_stats   = dict(stats)
        snap_log     = list(log_lines[-MAX_LOG:])
        collected    = snap_stats["collected"]

    # Header
    header_text = Text(HEADER, style="bold cyan", justify="center")

    # Status
    done_len = int(collected / target * 20) if target else 0
    prog_bar = "[green]" + "█" * done_len + "[/green][dim]" + "░" * (20 - done_len) + "[/dim]"
    pct      = int(collected / target * 100) if target else 0

    st = Table.grid(padding=(0, 3))
    for _ in range(6): st.add_column(justify="center")
    st.add_row("[dim]SALDO[/dim]", "[dim]TARGET[/dim]", "[dim]RETRY[/dim]",
               "[dim]CANCEL[/dim]", "[dim]TIMEOUT[/dim]", "[dim]PROGRESS[/dim]")
    st.add_row(
        f"[bold yellow]{balance}[/bold yellow]",
        f"[bold cyan]{collected}/{target}[/bold cyan]",
        f"[white]{snap_stats['retry']}[/white]",
        f"[{'red' if snap_stats['cancel'] else 'white'}]{snap_stats['cancel']}[/{'red' if snap_stats['cancel'] else 'white'}]",
        f"[white]{POLL_MAX * POLL_DELAY}s[/white]",
        f"{prog_bar} [bold green]{pct}%[/bold green]",
    )
    status_panel = Panel(st, title="[bold cyan]STATUS[/bold cyan]",
                         border_style="cyan", padding=(0, 2))

    # Config
    ct = Table.grid(padding=(0, 3))
    for _ in range(6): ct.add_column()
    ct.add_row(
        f"[dim]Threads[/dim] [cyan]{total_threads}[/cyan]",
        f"[dim]Order delay[/dim] [cyan]{ORDER_DELAY}s[/cyan]",
        f"[dim]OTP poll[/dim] [cyan]{POLL_DELAY}s[/cyan]",
        f"[dim]OTP timeout[/dim] [cyan]{POLL_MAX * POLL_DELAY}s[/cyan]",
        f"[dim]No-reject[/dim] [cyan]ON[/cyan]",
        f"[dim]Limit[/dim] [cyan]req:{MAX_REQ_PER_MIN}/m · new:{MAX_NEW_PER_MIN}/m[/cyan]",
    )
    cfg_panel = Panel(ct, title="[bold cyan]KONFIGURASI AKTIF[/bold cyan]",
                      border_style="dim cyan", padding=(0, 2))

    # Nomor Aktif
    nt = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan",
               padding=(0, 1), expand=True)
    nt.add_column("#",      width=4,  justify="center")
    nt.add_column("Nomor",  width=18)
    nt.add_column("App",    width=12)
    nt.add_column("OTP",    width=12)
    nt.add_column("Status", width=10, justify="center")
    nt.add_column("Masuk",  width=10, justify="center")
    nt.add_column("Tunggu", width=8,  justify="center")

    row_num = 1

    # Hasil OTP
    for res in snap_results:
        nt.add_row(
            str(row_num),
            f"[green]+{res['number']}[/green]",
            f"[dim]{res['app']}[/dim]",
            f"[bold green]{res['otp']}[/bold green]",
            "[bold green]RECV[/bold green]",
            res["ts"], "-",
        )
        row_num += 1

    # Thread yang sedang punya nomor aktif (bukan OTP OK, bukan "-")
    otp_ok_nums = {res["number"] for res in snap_results}
    for tid in sorted(snap_active):
        s = snap_active[tid]
        if s["number"] == "-" or s["number"] in otp_ok_nums:
            continue
        col = STATUS_STYLE.get(s["status"], "white")
        nt.add_row(
            str(row_num),
            f"[cyan]+{s['number']}[/cyan]",
            f"[dim]{s['app']}[/dim]",
            f"[dim]{s['otp']}[/dim]",
            f"[{col}]{s['status']}[/{col}]",
            s["masuk"], s["tunggu"],
        )
        row_num += 1

    # Jika tidak ada nomor sama sekali, tampilkan satu baris placeholder
    if row_num == 1:
        nt.add_row("-", "[dim]menunggu nomor...[/dim]", "-", "-", "-", "-", "-")

    num_panel = Panel(nt, title="[bold cyan]NOMOR AKTIF[/bold cyan]",
                      border_style="cyan", padding=(0, 1))

    # Log
    lt = Table.grid(padding=(0, 1))
    lt.add_column(width=10)
    lt.add_column(width=2)
    lt.add_column(width=10)
    lt.add_column()
    for ts, level, msg in snap_log:
        lvl_text, icon = LEVEL_STYLE.get(level, ("[white]LOG[/white]", "-"))
        lt.add_row(f"[dim]{ts}[/dim]", icon, lvl_text, f"[dim]{msg}[/dim]")
    for _ in range(MAX_LOG - len(snap_log)):
        lt.add_row("", "", "", "")
    log_panel = Panel(lt, title="[bold cyan]LOG AKTIVITAS[/bold cyan]",
                      border_style="dim cyan", padding=(0, 1))

    # Footer
    footer = Text(
        f"  Threads:{total_threads}   Timeout:{POLL_MAX * POLL_DELAY}s   "
        f"OrderDelay:{ORDER_DELAY}s   Poll:{POLL_DELAY}s   "
        f"Apps: {'  '.join(selected_apps)}",
        style="dim cyan", justify="center",
    )

    return Group(header_text, status_panel, cfg_panel, num_panel, log_panel, footer)

# ─── PICK APPS ─────────────────────────────────────────────────────────────────
def pick_apps() -> list:
    console.print()
    for k, v in WA_APPS.items():
        console.print(f"  [cyan]{k}[/cyan]  {v}")
    console.print("  [cyan]0[/cyan]  Semua")
    console.print()
    raw = Prompt.ask("  pilih app", default="9").strip()
    if raw == "0":
        return list(WA_APPS.values())
    chosen = []
    for p in re.split(r"[,\s]+", raw):
        if p in WA_APPS and WA_APPS[p] not in chosen:
            chosen.append(WA_APPS[p])
    return chosen or ["Whatsapp60"]

# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    console.clear()
    console.rule("[bold cyan]PVAPins WhatsApp Mexico[/bold cyan]")
    console.print()

    key = Prompt.ask("  api key", default=API_KEY).strip()
    if not key:
        console.print("  [red]api key kosong[/red]")
        sys.exit(1)

    console.print()
    bal = check_balance(key)
    if bal is None:
        console.print("  [red]api key tidak valid[/red]")
        sys.exit(1)
    console.print(f"  balance  [green]{bal}[/green]")

    selected = pick_apps()
    console.print(f"  app      {', '.join(selected)}")

    target = IntPrompt.ask("\n  jumlah otp", default=1)
    target = max(1, target)

    n_per = IntPrompt.ask(f"  thread per app  (×{len(selected)})", default=2)
    n_per = max(1, min(n_per, 5))
    total = n_per * len(selected)

    console.print(f"  thread   {total}  ({n_per} × {len(selected)} app)")
    console.print()
    time.sleep(0.5)

    done_event.clear()
    results.clear()
    active_nums.clear()
    log_lines.clear()
    stats.update({"retry": 0, "cancel": 0, "collected": 0})

    threads = []
    tid = 1
    for app in selected:
        for _ in range(n_per):
            t = threading.Thread(
                target=worker,
                args=(tid, key, app, target, (tid - 1) * 1.0),
                daemon=True,
            )
            threads.append(t)
            t.start()
            tid += 1

    with Live(make_dashboard(bal, target, selected, total),
              refresh_per_second=2, console=console, screen=True) as live:
        while not done_event.is_set():
            live.update(make_dashboard(bal, target, selected, total))
            time.sleep(0.5)
        time.sleep(2)
        live.update(make_dashboard(bal, target, selected, total))

    console.clear()
    console.rule("[bold green]SELESAI[/bold green]")
    console.print()
    for idx, res in enumerate(results, 1):
        console.print(
            f"  [dim]{idx:>2}[/dim]  "
            f"[bold green]{res['otp']}[/bold green]  "
            f"+{res['number']}  "
            f"[dim]{res['app']}  {res['ts']}[/dim]"
        )
    console.print()
    console.rule()

    for t in threads:
        t.join(timeout=5)

    console.print()
    if Confirm.ask("  order lagi?", default=False):
        main()
    else:
        console.print("  [dim]selesai.[/dim]\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n  [dim]dibatalkan.[/dim]\n")
        sys.exit(0)
