#!/usr/bin/env python3
"""
PVAPins - WhatsApp Mexico | Multi-App · Multi-Thread · Target OTP
- Sisa nomor TIDAK di-reject saat target tercapai
- Auto cancel (tanpa reject) jika 15 menit tidak dapat OTP
"""

import requests
import time
import sys
import re
import threading
from datetime import datetime
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.text import Text
from rich.live import Live
from rich.table import Table
from rich.console import Group
from rich import box

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BASE_URL    = "https://api.pvapins.com/user/api"
COUNTRY     = "mexico"
API_KEY     = "69b65a9dcf0d9598859"
POLL_DELAY  = 5           # detik antar polling
POLL_MAX    = 180         # 180 × 5s = 15 menit (auto cancel)
ORDER_DELAY = 0.3         # detik jeda sebelum request get_number

WA_APPS = {
    "1": "Whatsapp1",
    "2": "Whatsapp9",
    "3": "Whatsapp24",
    "4": "Whatsapp42",
    "5": "Whatsapp60",
}

APP_COLORS = {
    "Whatsapp1":  "green",
    "Whatsapp9":  "bright_green",
    "Whatsapp24": "cyan",
    "Whatsapp42": "blue",
    "Whatsapp60": "magenta",
}

console = Console()

# ─── SHARED STATE ──────────────────────────────────────────────────────────────
lock          = threading.Lock()
done_event    = threading.Event()
results       = []
thread_status = {}

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
    if isinstance(data, dict):
        num = str(data.get("data", ""))
        if data.get("code") == 100 or num.isdigit():
            return num
        return None
    if isinstance(data, str) and data.isdigit():
        return data
    return None

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

    def setstatus(number, status, step, elapsed=""):
        with lock:
            thread_status[tid] = {
                "app": app, "number": number or "-",
                "status": status, "step": step,
                "elapsed": elapsed,
                "ts": datetime.now().strftime("%H:%M:%S"),
            }

    while not done_event.is_set():
        setstatus(None, "[yellow]Ambil nomor...[/yellow]", 0)
        time.sleep(ORDER_DELAY)
        number = get_number(key, app)
        if not number:
            setstatus(None, "[red]Gagal nomor[/red]", 0)
            time.sleep(10)
            continue

        setstatus(number, "[cyan]Polling OTP[/cyan]", 0)
        got_otp = False
        start_ts = time.time()

        for i in range(1, POLL_MAX + 1):
            # ── Target tercapai: berhenti tanpa reject ──
            if done_event.is_set():
                elapsed = int(time.time() - start_ts)
                setstatus(number, "[dim]Stop (target done)[/dim]", i,
                          f"{elapsed}s")
                return

            time.sleep(POLL_DELAY)
            elapsed = int(time.time() - start_ts)
            mins, secs = divmod(elapsed, 60)
            elapsed_str = f"{mins:02d}:{secs:02d}"

            otp, full_msg = poll_sms(key, number, app)

            if otp:
                with lock:
                    existing = {r["number"] for r in results}
                    if number not in existing and len(results) < target:
                        results.append({
                            "otp": otp, "number": number,
                            "msg": full_msg, "tid": tid,
                            "app": app,
                            "ts": datetime.now().strftime("%H:%M:%S"),
                        })
                        if len(results) >= target:
                            done_event.set()

                setstatus(number, f"[bold green]OTP: {otp}[/bold green]",
                          i, elapsed_str)
                got_otp = True
                break

            # ── Timeout 15 menit: cancel tanpa reject, coba nomor baru ──
            if i >= POLL_MAX:
                setstatus(number, "[red]15 menit! Auto cancel[/red]",
                          i, elapsed_str)
                # Tidak panggil reject_number — biarkan nomor expired sendiri
                time.sleep(2)
                break

            setstatus(number, f"[cyan]Poll {i}/{POLL_MAX}[/cyan]",
                      i, elapsed_str)

        if not got_otp and not done_event.is_set():
            # Lanjut coba nomor baru
            time.sleep(2)

# ─── LIVE TABLE ────────────────────────────────────────────────────────────────
def make_table(balance: str, selected_apps: list, target: int) -> Panel:
    with lock:
        snap_st  = dict(thread_status)
        snap_res = list(results)
        collected = len(snap_res)

    # ── Thread table ──
    t = Table(
        box=box.SIMPLE_HEAD, show_header=True,
        header_style="bold cyan", expand=True, padding=(0, 1),
    )
    t.add_column("TID",     width=4,  justify="center")
    t.add_column("App",     width=12)
    t.add_column("Nomor",   width=16)
    t.add_column("Status",  min_width=20)
    t.add_column("Elapsed", width=8,  justify="center")
    t.add_column("Poll",    width=14, justify="center")
    t.add_column("Waktu",   width=10, justify="right")

    for tid in sorted(snap_st):
        s   = snap_st[tid]
        app = s.get("app", "-")
        col = APP_COLORS.get(app, "white")
        step = s["step"]
        filled = int(step / POLL_MAX * 12) if POLL_MAX else 0
        bar = f"[{col}]" + "#" * filled + f"[/{col}][dim]" + "-" * (12 - filled) + "[/dim]"
        t.add_row(
            str(tid),
            f"[{col}]{app}[/{col}]",
            s["number"],
            s["status"],
            s.get("elapsed", "-"),
            bar,
            s["ts"],
        )

    # ── Hasil OTP table ──
    r = Table(
        box=box.SIMPLE_HEAD, show_header=True,
        header_style="bold green", expand=True, padding=(0, 1),
    )
    r.add_column("#",      width=4,  justify="center")
    r.add_column("App",    width=12)
    r.add_column("Nomor",  width=16)
    r.add_column("OTP",    width=10, style="bold bright_green")
    r.add_column("SMS",    min_width=30)
    r.add_column("Waktu",  width=10, justify="right")

    for idx, res in enumerate(snap_res, 1):
        col = APP_COLORS.get(res["app"], "green")
        r.add_row(
            str(idx),
            f"[{col}]{res['app']}[/{col}]",
            f"+{res['number']}",
            res["otp"],
            (res["msg"] or "")[:50],
            res["ts"],
        )
    for i in range(collected + 1, target + 1):
        r.add_row(
            str(i), "[dim]-[/dim]", "[dim]-[/dim]",
            "[dim]...[/dim]", "[dim]Menunggu...[/dim]", "[dim]-[/dim]",
        )

    # ── Progress bar ──
    bar_len  = 20
    done_len = int(collected / target * bar_len) if target else 0
    prog = (
        "[green]" + "#" * done_len + "[/green]"
        + "[dim]" + "-" * (bar_len - done_len) + "[/dim]"
    )

    apps_label = "  ".join(
        f"[{APP_COLORS.get(a,'white')}]{a}[/{APP_COLORS.get(a,'white')}]"
        for a in selected_apps
    )

    header = (
        f"[bold cyan]PVAPins WhatsApp Mexico[/bold cyan]  "
        f"[dim]|[/dim]  [green]${balance}[/green]  "
        f"[dim]|[/dim]  {apps_label}  "
        f"[dim]|[/dim]  [bold]{collected}[/bold][dim]/{target}[/dim]  {prog}  "
        f"[dim]| timeout: 15 mnt | no-reject[/dim]"
    )

    body = Group(
        Panel(t, title="[cyan]Thread Status[/cyan]",
              border_style="dim cyan", padding=(0, 1)),
        Panel(r, title=f"[green]Hasil OTP  ({collected}/{target})[/green]",
              border_style="green", padding=(0, 1)),
    )
    return Panel(body, title=header, border_style="cyan", padding=(0, 1))

# ─── PICK APPS ─────────────────────────────────────────────────────────────────
def pick_apps() -> list:
    console.print()
    console.print("  [bold cyan]Pilih App WhatsApp:[/bold cyan]")
    for k, v in WA_APPS.items():
        col = APP_COLORS.get(v, "white")
        console.print(f"    [{col}][{k}][/{col}] {v}")
    console.print("    [dim][6][/dim] Semua (1 + 9 + 24 + 42 + 60)")
    console.print()

    raw = Prompt.ask(
        "  [cyan]Pilih[/cyan] (contoh: 5  atau  1,3,5  atau  6)",
        default="5",
    ).strip()

    if raw == "6":
        return list(WA_APPS.values())

    chosen = []
    for part in re.split(r"[,\s]+", raw):
        if part in WA_APPS:
            app = WA_APPS[part]
            if app not in chosen:
                chosen.append(app)
    return chosen if chosen else ["Whatsapp60"]

# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    console.clear()
    console.print(Panel(
        Text(
            "PVAPins  -  WhatsApp Mexico  |  Multi-App · Multi-Thread · Target OTP",
            justify="center", style="bold cyan",
        ),
        subtitle="[dim]No-Reject  ·  Auto-Cancel 15 menit[/dim]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()

    key = Prompt.ask("  [cyan]API Key[/cyan]", default=API_KEY).strip()

    console.print("\n[dim]  Cek saldo...[/dim]")
    balance = check_balance(key)
    if balance is None:
        console.print("[red]  x API key tidak valid.[/red]")
        sys.exit(1)
    console.print(f"  Balance  :  [bold green]${balance}[/bold green]")

    selected_apps = pick_apps()
    console.print(
        "  App      :  " +
        "  ".join(
            f"[{APP_COLORS.get(a,'white')}]{a}[/{APP_COLORS.get(a,'white')}]"
            for a in selected_apps
        )
    )

    target = IntPrompt.ask(
        "\n  [cyan]Jumlah OTP yang ingin dikumpulkan[/cyan]",
        default=1,
    )
    target = max(1, target)
    console.print(f"  Target   :  [bold]{target} OTP[/bold]")

    n_per_app = IntPrompt.ask(
        f"  [cyan]Thread per app[/cyan] (×{len(selected_apps)} app)",
        default=2,
    )
    n_per_app     = max(1, min(n_per_app, 5))
    total_threads = n_per_app * len(selected_apps)
    console.print(
        f"  Thread   :  [bold]{total_threads}[/bold]  "
        f"[dim]({n_per_app} × {len(selected_apps)} app)[/dim]"
    )
    console.print(
        "  Timeout  :  [bold]15 menit[/bold] per nomor  "
        "[dim](auto cancel, tanpa reject)[/dim]"
    )
    console.print(
        f"  Order delay :  [bold]{ORDER_DELAY}s[/bold]  "
        "[dim](jeda sebelum request nomor baru)[/dim]"
    )

    console.print(f"\n  [dim]Memulai {total_threads} thread...[/dim]\n")
    time.sleep(0.5)

    # Reset state
    done_event.clear()
    results.clear()
    thread_status.clear()

    # Init status
    tid = 1
    for app in selected_apps:
        for _ in range(n_per_app):
            thread_status[tid] = {
                "app": app, "number": "-",
                "status": "[dim]Init[/dim]", "step": 0,
                "elapsed": "-",
                "ts": datetime.now().strftime("%H:%M:%S"),
            }
            tid += 1

    # Jalankan thread
    threads = []
    tid = 1
    for app in selected_apps:
        for _ in range(n_per_app):
            t = threading.Thread(
                target=worker,
                args=(tid, key, app, target, (tid - 1) * 1.0),
                daemon=True,
            )
            threads.append(t)
            t.start()
            tid += 1

    # Live dashboard
    with Live(
        make_table(balance, selected_apps, target),
        refresh_per_second=2,
        console=console,
    ) as live:
        while not done_event.is_set():
            live.update(make_table(balance, selected_apps, target))
            time.sleep(0.5)
        time.sleep(1.5)
        live.update(make_table(balance, selected_apps, target))

    # Summary
    console.print()
    lines = []
    for idx, res in enumerate(results, 1):
        col = APP_COLORS.get(res["app"], "green")
        lines.append(
            f"  [{col}][{idx:>2}][/{col}]"
            f"  OTP [bold bright_green]{res['otp']}[/bold bright_green]"
            f"  +{res['number']}"
            f"  [{col}]{res['app']}[/{col}]"
            f"  [dim]{res['ts']}[/dim]"
        )
    console.print(Panel(
        "\n".join(lines),
        title=f"[bold green]Selesai!  {len(results)}/{target} OTP Terkumpul[/bold green]",
        subtitle="[dim]Nomor tidak di-reject — tetap aktif di akun PVAPins[/dim]",
        border_style="green",
        padding=(0, 2),
    ))

    for t in threads:
        t.join(timeout=5)

    console.print()
    if Confirm.ask("  Order lagi?", default=False):
        main()
    else:
        console.print("\n  [dim]Selesai.[/dim]\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n\n  [dim]Dibatalkan.[/dim]\n")
        sys.exit(0)
