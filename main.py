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
from rich.prompt import Prompt, Confirm, IntPrompt

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL     = "https://api.pvapins.com/user/api"
COUNTRY      = "mexico"
API_KEY      = "69b65a9dcf0d9598859"
POLL_DELAY   = 5
POLL_MAX     = 180    # 180 × 5s = 15 menit
ORDER_DELAY  = 0.3
RETRY_DELAY  = 10
RETRY_MAX    = 60

WA_APPS = {
    "1": "Whatsapp1",
    "2": "Whatsapp9",
    "3": "Whatsapp24",
    "4": "Whatsapp42",
    "5": "Whatsapp60",
}

console = Console(highlight=False)

# ─── SHARED STATE ──────────────────────────────────────────────────────────────
lock        = threading.Lock()
done_event  = threading.Event()
results     = []
log_lines   = []          # log global (thread-safe)
MAX_LOG     = 200

# ─── LOG ───────────────────────────────────────────────────────────────────────
def now() -> str:
    return datetime.now().strftime("%H:%M:%S")

def log(msg: str):
    line = f"[dim]{now()}[/dim]  {msg}"
    with lock:
        log_lines.append(line)
        if len(log_lines) > MAX_LOG:
            log_lines.pop(0)
    console.print(line)

# ─── API ───────────────────────────────────────────────────────────────────────
def api_get(endpoint: str, params: dict):
    try:
        r = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return r.text.strip()
    except requests.exceptions.RequestException:
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
        return None, "timeout/koneksi"
    if isinstance(data, dict):
        num = str(data.get("data", ""))
        if data.get("code") == 100 or num.isdigit():
            return num, None
        return None, (num[:40] if num else "error tidak diketahui")
    if isinstance(data, str):
        if data.isdigit():
            return data, None
        return None, data[:40]
    return None, "respon tidak dikenal"

def poll_sms(key: str, number: str, app: str):
    data = api_get("get_sms.php", {
        "customer": key, "number": number, "country": COUNTRY, "app": app,
    })
    raw = None
    if isinstance(data, dict):
        raw = str(data.get("data", ""))
        if any(x in raw.lower() for x in ["not", "expired", "error"]):
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
    tag = f"[cyan]T{tid:02d}[/cyan] [dim]{app}[/dim]"
    fail_count = 0

    while not done_event.is_set():
        time.sleep(ORDER_DELAY)
        number, err = get_number(key, app)

        if not number:
            fail_count += 1
            wait = min(RETRY_DELAY * fail_count, RETRY_MAX)
            log(f"{tag}  [red]gagal nomor[/red]  [dim]{err} — retry {wait}s[/dim]")
            time.sleep(wait)
            continue

        fail_count = 0
        log(f"{tag}  [green]+{number}[/green]  polling OTP...")
        start_ts = time.time()

        for i in range(1, POLL_MAX + 1):
            if done_event.is_set():
                elapsed = int(time.time() - start_ts)
                log(f"{tag}  [dim]+{number} stop (target done) {elapsed}s[/dim]")
                return

            time.sleep(POLL_DELAY)
            elapsed = int(time.time() - start_ts)
            mins, secs = divmod(elapsed, 60)
            otp, full_msg = poll_sms(key, number, app)

            if otp:
                with lock:
                    existing = {r["number"] for r in results}
                    if number not in existing and len(results) < target:
                        results.append({
                            "otp": otp, "number": number,
                            "msg": full_msg, "tid": tid,
                            "app": app,
                            "ts": now(),
                        })
                        collected = len(results)
                        if collected >= target:
                            done_event.set()

                log(
                    f"{tag}  [green]+{number}[/green]  "
                    f"[bold green]OTP {otp}[/bold green]  "
                    f"[dim]{mins:02d}:{secs:02d}  ({collected}/{target})[/dim]"
                )
                return

            if i >= POLL_MAX:
                log(f"{tag}  [yellow]+{number}[/yellow]  [dim]15 mnt timeout — cancel[/dim]")
                break

            if i % 6 == 0:   # log setiap 30 detik saja agar tidak spam
                log(f"{tag}  [dim]+{number} poll {i}/{POLL_MAX}  {mins:02d}:{secs:02d}[/dim]")

        time.sleep(2)

# ─── PICK APPS ─────────────────────────────────────────────────────────────────
def pick_apps() -> list:
    console.print()
    for k, v in WA_APPS.items():
        console.print(f"  [cyan]{k}[/cyan]  {v}")
    console.print(f"  [cyan]6[/cyan]  Semua")
    console.print()

    raw = Prompt.ask("  pilih app", default="5").strip()
    if raw == "6":
        return list(WA_APPS.values())
    chosen = []
    for p in re.split(r"[,\s]+", raw):
        if p in WA_APPS and WA_APPS[p] not in chosen:
            chosen.append(WA_APPS[p])
    return chosen or ["Whatsapp60"]

# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    console.clear()
    console.rule("[bold cyan]PVAPins  WhatsApp Mexico[/bold cyan]")
    console.print()

    key = Prompt.ask("  api key", default=API_KEY).strip()

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

    console.print(f"  target   {target} otp")
    console.print(f"  thread   {total}  ({n_per} × {len(selected)} app)")
    console.print(f"  timeout  15 menit  (no reject)")
    console.print()
    console.rule()
    console.print()

    done_event.clear()
    results.clear()

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

    # Tunggu sampai selesai
    done_event.wait()
    time.sleep(1.5)

    for t in threads:
        t.join(timeout=5)

    # Summary
    console.print()
    console.rule("[bold green]selesai[/bold green]")
    for idx, res in enumerate(results, 1):
        console.print(
            f"  [dim]{idx:>2}[/dim]  "
            f"[bold green]{res['otp']}[/bold green]  "
            f"+{res['number']}  "
            f"[dim]{res['app']}  {res['ts']}[/dim]"
        )
    console.rule()
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
