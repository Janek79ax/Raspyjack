#!/usr/bin/env python3
"""
RaspyJack JanOS C5 -- WiFi/BLE control for ESP32C5 (JanOS firmware over UART)
============================================================================

Drives a JanOS device connected over USB/UART serial. Auto-detects the port
with a ping/pong handshake, then offers a menu:

  - Deauth          scan_networks -> multi-select -> select_networks + start_deauth -> stop
  - Blackout        start_blackout (scans + deauths everything) -> stop
  - Net Observer    start_sniffer + show_sniffer_results -> AP/client tree
  - BLE Locate      scan_bt list -> pick one -> scan_bt <MAC> -> RSSI hot/cold meter

Controls:
  MENU:      UP/DN=Nav  OK=Open  KEY3=Exit
  SELECT:    UP/DN=Nav  OK=Toggle  KEY1=Rescan  KEY2=Deauth  KEY3=Back
  ATTACK:    KEY2=Stop  KEY3=Back
  OBSERVER:  UP/DN=Scroll  KEY3=Stop/Back
  BLE LIST:  UP/DN=Nav  OK=Track  KEY1=Rescan  KEY3=Back
  BLE TRACK: KEY3=Stop/Back
"""

import os
import sys
import time
import signal
import threading

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44
import LCD_Config  # noqa: F401  (imported for side-effect parity with other payloads)
from PIL import Image
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button
from payloads._janos_helper import (
    detect_janos, JanOS,
    parse_network_csv, parse_sniffer_tree,
    parse_bt_scan_line, parse_bt_track, parse_rssi,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PINS = {
    "UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
    "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16,
}

SCAN_TIMEOUT = 20          # seconds to wait for "Scan results printed"
BLE_SCAN_TIMEOUT = 14      # JanOS BLE scan runs ~10s
OBS_REFRESH = 2.5          # seconds between show_sniffer_results polls

# Colors (base-128 drawing)
CLR_GREEN = "#00FF00"
CLR_RED = "#FF3333"
CLR_YELLOW = "#FFCC00"
CLR_CYAN = "#00CCFF"
CLR_WHITE = "#FFFFFF"
CLR_GRAY = "#888888"
CLR_DARK = "#111111"
CLR_BLUE = "#3366FF"
CLR_BG_MENU = "#001a33"
CLR_BG_SCAN = "#003300"
CLR_BG_ATK = "#330000"
CLR_BG_BLE = "#1a0033"

# ---------------------------------------------------------------------------
# LCD / GPIO setup
# ---------------------------------------------------------------------------
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
for _pin in PINS.values():
    GPIO.setup(_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

LCD = LCD_1in44.LCD()
LCD.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
WIDTH, HEIGHT = LCD.width, LCD.height

canvas = Image.new("RGB", (WIDTH, HEIGHT), "black")
draw = ScaledDraw(canvas)

FNT_LG = scaled_font(11)
FNT_MD = scaled_font(9)
FNT_SM = scaled_font(8)
FNT_XS = scaled_font(7)

# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------
_running = True


def _signal_handler(signum, frame):
    global _running
    _running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _refresh():
    LCD.LCD_ShowImage(canvas, 0, 0)


def _clear():
    draw.rectangle((0, 0, 128, 128), fill="black")


def _header(text, color, bg):
    draw.rectangle((0, 0, 128, 12), fill=bg)
    draw.line((0, 12, 128, 12), fill=color)
    w = draw.textbbox((0, 0), text, font=FNT_MD)[2]
    draw.text(((128 - w) // 2, 1), text, font=FNT_MD, fill=color)


def _footer(text, color=CLR_GRAY):
    draw.rectangle((0, 116, 128, 128), fill=CLR_DARK)
    draw.line((0, 116, 128, 116), fill=CLR_GRAY)
    draw.text((2, 117), text, font=FNT_XS, fill=color)


def _fmt_time(seconds):
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m:02d}:{s:02d}"


def draw_status(msg, color=CLR_GREEN, title="JanOS C5"):
    _clear()
    _header(title, CLR_YELLOW, CLR_BG_MENU)
    words = msg.split()
    lines = []
    cur = ""
    for w in words:
        if len(cur + w) <= 20:
            cur += w + " "
        else:
            if cur:
                lines.append(cur.strip())
            cur = w + " "
    if cur:
        lines.append(cur.strip())
    y = 40
    for ln in lines[:6]:
        draw.text((4, y), ln, font=FNT_SM, fill=color)
        y += 12
    _refresh()


def _signal_color(rssi):
    if rssi >= -55:
        return CLR_GREEN
    if rssi >= -70:
        return CLR_YELLOW
    return CLR_RED


def _signal_bars(rssi):
    if rssi >= -50:
        return "||||"
    if rssi >= -60:
        return "||| "
    if rssi >= -70:
        return "||  "
    if rssi >= -80:
        return "|   "
    return ".   "


def _press_release(name):
    """Block until the given button is released (simple debounce)."""
    while get_button(PINS, GPIO) == name:
        time.sleep(0.04)

# ---------------------------------------------------------------------------
# Connect to JanOS
# ---------------------------------------------------------------------------

def connect():
    """Detect and open the JanOS serial port. Returns a JanOS instance or None."""
    draw_status("Searching for JanOS device on serial ports...", CLR_CYAN)
    port, baud = detect_janos()
    if not port:
        return None
    draw_status(f"Found JanOS at {os.path.basename(port)} @ {baud}", CLR_GREEN)
    dev = JanOS(port, baud)
    try:
        dev.open()
    except Exception:
        return None
    time.sleep(0.3)
    return dev


def get_version(dev):
    """Query firmware version. Returns string or 'unknown'."""
    lines = dev.run_until("JanOS version", timeout=2.0, command="version")
    for ln in lines:
        if "JanOS version" in ln:
            return ln.split("JanOS version:", 1)[-1].strip()
    return "unknown"

# ---------------------------------------------------------------------------
# MAIN MENU
# ---------------------------------------------------------------------------
MENU_ITEMS = [
    ("Deauth", "deauth"),
    ("Blackout", "blackout"),
    ("Net Observer", "observer"),
    ("BLE Locate", "ble"),
]


def draw_menu(idx, port_name, version):
    _clear()
    _header("JanOS C5", CLR_CYAN, CLR_BG_MENU)
    for i, (label, _) in enumerate(MENU_ITEMS):
        y = 18 + i * 16
        if i == idx:
            draw.rectangle((2, y - 2, 126, y + 12), fill="#102030")
            draw.text((8, y), label, font=FNT_MD, fill=CLR_GREEN)
        else:
            draw.text((8, y), label, font=FNT_MD, fill=CLR_WHITE)
    draw.text((4, 96), f"{port_name}  v{version}"[:24], font=FNT_XS, fill=CLR_GRAY)
    _footer("OK:Open  KEY3:Exit")
    _refresh()


def run_menu(dev, port_name, version):
    """Top-level menu loop. Returns the chosen action key or None to exit."""
    idx = 0
    draw_menu(idx, port_name, version)
    while _running:
        btn = get_button(PINS, GPIO)
        if btn == "UP":
            _press_release("UP")
            idx = (idx - 1) % len(MENU_ITEMS)
            draw_menu(idx, port_name, version)
        elif btn == "DOWN":
            _press_release("DOWN")
            idx = (idx + 1) % len(MENU_ITEMS)
            draw_menu(idx, port_name, version)
        elif btn == "OK":
            _press_release("OK")
            return MENU_ITEMS[idx][1]
        elif btn == "KEY3":
            _press_release("KEY3")
            return None
        else:
            time.sleep(0.05)
    return None

# ---------------------------------------------------------------------------
# Generic scan-in-thread runner with animated screen
# ---------------------------------------------------------------------------

def run_scan_with_anim(dev, command, marker, timeout, title, parse_fn):
    """Run a scan command in a worker thread, animate, return parsed list.

    parse_fn(line) -> item or None ; collected items are returned.
    Returns (items, cancelled).
    """
    result = []
    finished = threading.Event()

    def _worker():
        def _on_line(ln):
            item = parse_fn(ln)
            if item is not None:
                result.append(item)
        dev.run_until(marker, timeout=timeout, command=command, on_line=_on_line)
        finished.set()

    t = threading.Thread(target=_worker, daemon=True)
    start = time.time()
    t.start()

    while not finished.is_set() and _running:
        btn = get_button(PINS, GPIO)
        if btn == "KEY3":
            _press_release("KEY3")
            dev.stop_all()
            finished.set()
            t.join(timeout=2)
            return result, True
        elapsed = time.time() - start
        _draw_scanning(title, elapsed, timeout, len(result))
        time.sleep(0.15)

    t.join(timeout=2)
    return result, False


def _draw_scanning(title, elapsed, timeout, found):
    dots = "." * (int(elapsed * 2) % 4)
    _clear()
    _header(title, CLR_GREEN, CLR_BG_SCAN)
    draw.text((16, 38), f"Scanning{dots}", font=FNT_MD, fill=CLR_GREEN)
    draw.text((16, 54), f"Found: {found}", font=FNT_SM, fill=CLR_YELLOW)
    pct = min(1.0, elapsed / timeout) if timeout > 0 else 0
    draw.rectangle((14, 74, 114, 80), outline=CLR_GRAY)
    bar_w = int(100 * pct)
    if bar_w > 0:
        draw.rectangle((14, 74, 14 + bar_w, 80), fill=CLR_GREEN)
    _footer("KEY3: Cancel")
    _refresh()

# ---------------------------------------------------------------------------
# DEAUTH
# ---------------------------------------------------------------------------

def draw_select(nets, idx, selected):
    _clear()
    _header(f"SELECT  {len(nets)} APs", CLR_GREEN, CLR_BG_SCAN)
    rows = 7
    row_h = 13
    scroll = max(0, idx - rows // 2)
    scroll = min(scroll, max(0, len(nets) - rows))
    for i in range(rows):
        ni = scroll + i
        if ni >= len(nets):
            break
        net = nets[ni]
        y = 15 + i * row_h
        is_cur = ni == idx
        is_sel = net["index"] in selected
        if is_cur:
            draw.rectangle((0, y - 1, 128, y + row_h - 2), fill="#1a1a2e")
        chk = "[x]" if is_sel else "[ ]"
        draw.text((1, y), chk, font=FNT_XS, fill=CLR_GREEN if is_sel else CLR_GRAY)
        ssid = net["ssid"][:10]
        draw.text((18, y), ssid, font=FNT_XS, fill=CLR_WHITE if is_cur else CLR_GRAY)
        draw.text((78, y), f"c{net['channel']}", font=FNT_XS, fill=CLR_CYAN)
        draw.text((98, y), _signal_bars(net["rssi"]), font=FNT_XS,
                  fill=_signal_color(net["rssi"]))
    _footer(f"{len(selected)}sel K1:Scan K2:Deauth")
    _refresh()


def draw_attack(targets, stats, elapsed, title="DEAUTH"):
    _clear()
    _header(title, CLR_RED, CLR_BG_ATK)
    if targets is None:
        draw.text((4, 16), "All networks", font=FNT_SM, fill=CLR_RED)
    elif len(targets) == 1:
        draw.text((4, 16), targets[0]["ssid"][:16], font=FNT_SM, fill=CLR_RED)
    else:
        draw.text((4, 16), f"{len(targets)} targets", font=FNT_SM, fill=CLR_RED)
    draw.text((92, 16), _fmt_time(elapsed), font=FNT_SM, fill=CLR_WHITE)
    draw.line((4, 28, 124, 28), fill=CLR_RED)

    draw.text((4, 32), "RX lines:", font=FNT_XS, fill=CLR_GRAY)
    draw.text((4, 42), str(stats["lines"]), font=FNT_LG, fill=CLR_RED)

    last = stats.get("last", "")
    if last:
        draw.text((4, 60), last[:22], font=FNT_XS, fill=CLR_YELLOW)

    if targets:
        ly = 74
        draw.line((4, ly - 2, 124, ly - 2), fill=CLR_GRAY)
        for t in targets[:4]:
            draw.text((6, ly), f"c{t['channel']:>3} {t['ssid'][:13]}",
                      font=FNT_XS, fill=CLR_RED)
            ly += 9
        if len(targets) > 4:
            draw.text((6, ly), f"+{len(targets) - 4} more", font=FNT_XS, fill=CLR_GRAY)

    _footer("KEY2:Stop  KEY3:Back")
    _refresh()


def _run_continuous(dev, command, targets, title):
    """Start a continuous JanOS attack and render an activity dashboard.

    Returns when the user stops (KEY2) or backs out (KEY3).
    """
    stats = {"lines": 0, "last": ""}

    def _on_line(ln):
        stats["lines"] += 1
        s = ln.strip()
        if s:
            stats["last"] = s

    dev.set_line_callback(_on_line)
    dev.send(command)
    start = time.time()
    try:
        while _running:
            btn = get_button(PINS, GPIO)
            if btn in ("KEY2", "KEY3"):
                _press_release(btn)
                break
            draw_attack(targets, stats, time.time() - start, title)
            time.sleep(0.2)
    finally:
        dev.clear_line_callback()
        dev.stop_all()
        time.sleep(0.3)


def feature_deauth(dev):
    while _running:
        nets, cancelled = run_scan_with_anim(
            dev, "scan_networks", "Scan results printed",
            SCAN_TIMEOUT, "WiFi SCAN", parse_network_csv)
        if cancelled:
            return
        # Dedupe by index, sort by RSSI descending
        seen = {}
        for n in nets:
            seen[n["index"]] = n
        nets = sorted(seen.values(), key=lambda n: n["rssi"], reverse=True)
        if not nets:
            draw_status("No networks found.", CLR_RED, "DEAUTH")
            time.sleep(2)
            return

        idx = 0
        selected = set()
        draw_select(nets, idx, selected)
        rescan = False
        while _running and not rescan:
            btn = get_button(PINS, GPIO)
            if btn == "UP":
                _press_release("UP")
                idx = (idx - 1) % len(nets)
                draw_select(nets, idx, selected)
            elif btn == "DOWN":
                _press_release("DOWN")
                idx = (idx + 1) % len(nets)
                draw_select(nets, idx, selected)
            elif btn == "OK":
                _press_release("OK")
                ni = nets[idx]["index"]
                if ni in selected:
                    selected.discard(ni)
                else:
                    selected.add(ni)
                draw_select(nets, idx, selected)
            elif btn == "KEY1":
                _press_release("KEY1")
                rescan = True
            elif btn == "KEY2":
                _press_release("KEY2")
                if not selected:
                    draw_status("No targets selected!", CLR_RED, "DEAUTH")
                    time.sleep(1.5)
                    draw_select(nets, idx, selected)
                    continue
                ordered = sorted(selected)
                targets = [n for n in nets if n["index"] in selected]
                dev.run_until(None, timeout=1.5,
                              command="select_networks " + " ".join(str(i) for i in ordered),
                              idle_timeout=0.6)
                _run_continuous(dev, "start_deauth", targets, "DEAUTH")
                draw_select(nets, idx, selected)
            elif btn == "KEY3":
                _press_release("KEY3")
                return
            else:
                time.sleep(0.05)
        # loop back for rescan

# ---------------------------------------------------------------------------
# BLACKOUT
# ---------------------------------------------------------------------------

def feature_blackout(dev):
    _clear()
    _header("BLACKOUT", CLR_RED, CLR_BG_ATK)
    draw.text((4, 22), "Scans + deauths", font=FNT_SM, fill=CLR_WHITE)
    draw.text((4, 32), "EVERYTHING nearby", font=FNT_SM, fill=CLR_RED)
    draw.text((4, 52), "KEY2  Start", font=FNT_SM, fill=CLR_GREEN)
    draw.text((4, 64), "KEY3  Back", font=FNT_SM, fill=CLR_GRAY)
    _footer("KEY2:Start  KEY3:Back")
    _refresh()
    while _running:
        btn = get_button(PINS, GPIO)
        if btn == "KEY2":
            _press_release("KEY2")
            _run_continuous(dev, "start_blackout", None, "BLACKOUT")
            return
        elif btn == "KEY3":
            _press_release("KEY3")
            return
        else:
            time.sleep(0.05)

# ---------------------------------------------------------------------------
# NETWORK OBSERVER
# ---------------------------------------------------------------------------

def draw_observer(aps, scroll, packets, age):
    _clear()
    total_clients = sum(a["count"] for a in aps)
    _header(f"OBSERVER {len(aps)}AP", CLR_CYAN, CLR_BG_MENU)

    # Build flat display rows: AP line then its client lines.
    rows = []
    for ap in aps:
        rows.append(("ap", ap))
        for mac in ap["clients"]:
            rows.append(("cl", mac))

    visible = 8
    row_h = 12
    scroll = max(0, min(scroll, max(0, len(rows) - visible)))
    for i in range(visible):
        ri = scroll + i
        if ri >= len(rows):
            break
        kind, data = rows[ri]
        y = 15 + i * row_h
        if kind == "ap":
            name = data["ssid"][:14] or "<hidden>"
            draw.text((2, y), name, font=FNT_XS, fill=CLR_GREEN)
            draw.text((92, y), f"c{data['channel']}", font=FNT_XS, fill=CLR_CYAN)
            draw.text((112, y), str(data["count"]), font=FNT_XS, fill=CLR_YELLOW)
        else:
            draw.text((10, y), data[:20], font=FNT_XS, fill=CLR_GRAY)

    if not rows:
        draw.text((10, 50), "Listening...", font=FNT_SM, fill=CLR_GRAY)

    _footer(f"{total_clients}cl {age:.0f}s ago  K3:Stop")
    _refresh()


def feature_observer(dev):
    dev.run_until(None, timeout=1.0, command="start_sniffer", idle_timeout=0.5)
    aps = []
    scroll = 0
    last_poll = 0.0
    last_update = time.time()
    draw_observer(aps, scroll, 0, 0)
    try:
        while _running:
            btn = get_button(PINS, GPIO)
            if btn == "KEY3":
                _press_release("KEY3")
                break
            elif btn == "UP":
                _press_release("UP")
                scroll = max(0, scroll - 1)
                draw_observer(aps, scroll, 0, time.time() - last_update)
            elif btn == "DOWN":
                _press_release("DOWN")
                scroll += 1
                draw_observer(aps, scroll, 0, time.time() - last_update)

            now = time.time()
            if now - last_poll >= OBS_REFRESH:
                last_poll = now
                lines = dev.run_until(None, timeout=1.2,
                                      command="show_sniffer_results",
                                      idle_timeout=0.5)
                data_lines = [ln for ln in lines
                              if "Sniffer packet count" not in ln]
                parsed = parse_sniffer_tree(data_lines)
                if parsed:
                    aps = parsed
                last_update = time.time()
                draw_observer(aps, scroll, 0, 0)
            time.sleep(0.05)
    finally:
        dev.stop_all()
        time.sleep(0.3)

# ---------------------------------------------------------------------------
# BLE LOCATE
# ---------------------------------------------------------------------------

def draw_ble_list(devs, idx):
    _clear()
    _header(f"BLE  {len(devs)} dev", CLR_BLUE, CLR_BG_BLE)
    rows = 7
    row_h = 13
    scroll = max(0, idx - rows // 2)
    scroll = min(scroll, max(0, len(devs) - rows))
    for i in range(rows):
        ni = scroll + i
        if ni >= len(devs):
            break
        d = devs[ni]
        y = 15 + i * row_h
        is_cur = ni == idx
        if is_cur:
            draw.rectangle((0, y - 1, 128, y + row_h - 2), fill="#1a1a2e")
        label = d["name"] if d["name"] else d["mac"]
        draw.text((2, y), label[:15], font=FNT_XS,
                  fill=CLR_WHITE if is_cur else CLR_GRAY)
        draw.text((96, y), str(d["rssi"]), font=FNT_XS,
                  fill=_signal_color(d["rssi"]))
    _footer("OK:Track K1:Scan K3:Back")
    _refresh()


def draw_ble_track(target, rssi, history, elapsed):
    _clear()
    _header("BLE LOCATE", CLR_BLUE, CLR_BG_BLE)
    label = target["name"] if target["name"] else target["mac"]
    draw.text((4, 15), label[:20], font=FNT_SM, fill=CLR_WHITE)
    draw.text((4, 25), target["mac"], font=FNT_XS, fill=CLR_GRAY)

    if rssi is None:
        draw.text((20, 60), "no signal...", font=FNT_MD, fill=CLR_GRAY)
        _footer("KEY3: Stop")
        _refresh()
        return

    color = _signal_color(rssi)
    # Big RSSI number
    draw.text((30, 38), f"{rssi}", font=FNT_LG, fill=color)
    draw.text((84, 44), "dBm", font=FNT_XS, fill=CLR_GRAY)

    # Hot/cold proximity bar: map -100..-30 dBm to 0..100%
    pct = max(0.0, min(1.0, (rssi + 100) / 70.0))
    draw.rectangle((10, 60, 118, 72), outline=CLR_GRAY)
    bar_w = int(108 * pct)
    if bar_w > 0:
        draw.rectangle((10, 60, 10 + bar_w, 72), fill=color)
    hint = "HOT - very close" if pct > 0.75 else \
           "warm - closer" if pct > 0.5 else \
           "cool - far" if pct > 0.25 else "COLD - far away"
    draw.text((10, 76), hint, font=FNT_XS, fill=color)

    # Sparkline of recent RSSI history
    if len(history) > 1:
        base_y = 108
        x0 = 10
        step = max(1, 108 // max(1, len(history)))
        for i, hv in enumerate(history[-30:]):
            hp = max(0.0, min(1.0, (hv + 100) / 70.0))
            h = int(22 * hp)
            x = x0 + i * 3
            if x > 118:
                break
            draw.line((x, base_y, x, base_y - h), fill=_signal_color(hv))

    _footer(f"{_fmt_time(elapsed)}  KEY3: Stop")
    _refresh()


def feature_ble(dev):
    while _running:
        devs, cancelled = run_scan_with_anim(
            dev, "scan_bt", "total devices",
            BLE_SCAN_TIMEOUT, "BLE SCAN", parse_bt_scan_line)
        if cancelled:
            return
        # Dedupe by MAC, keep strongest RSSI, sort by RSSI desc
        by_mac = {}
        for d in devs:
            ex = by_mac.get(d["mac"])
            if ex is None or d["rssi"] > ex["rssi"]:
                by_mac[d["mac"]] = d
        devs = sorted(by_mac.values(), key=lambda d: d["rssi"], reverse=True)
        if not devs:
            draw_status("No BLE devices found.", CLR_RED, "BLE")
            time.sleep(2)
            return

        idx = 0
        draw_ble_list(devs, idx)
        rescan = False
        while _running and not rescan:
            btn = get_button(PINS, GPIO)
            if btn == "UP":
                _press_release("UP")
                idx = (idx - 1) % len(devs)
                draw_ble_list(devs, idx)
            elif btn == "DOWN":
                _press_release("DOWN")
                idx = (idx + 1) % len(devs)
                draw_ble_list(devs, idx)
            elif btn == "KEY1":
                _press_release("KEY1")
                rescan = True
            elif btn == "OK":
                _press_release("OK")
                _ble_track(dev, devs[idx])
                draw_ble_list(devs, idx)
            elif btn == "KEY3":
                _press_release("KEY3")
                return
            else:
                time.sleep(0.05)


def _ble_track(dev, target):
    state = {"rssi": None}
    history = []

    def _on_line(ln):
        info = parse_bt_track(ln)
        if info and info["mac"].upper() == target["mac"].upper():
            state["rssi"] = info["rssi"]
            history.append(info["rssi"])
            if len(history) > 60:
                del history[0]
        else:
            r = parse_rssi(ln)
            if r is not None and target["mac"].upper() in ln.upper():
                state["rssi"] = r
                history.append(r)
                if len(history) > 60:
                    del history[0]

    dev.set_line_callback(_on_line)
    dev.send(f"scan_bt {target['mac']}")
    start = time.time()
    try:
        while _running:
            btn = get_button(PINS, GPIO)
            if btn == "KEY3":
                _press_release("KEY3")
                break
            draw_ble_track(target, state["rssi"], history, time.time() - start)
            time.sleep(0.2)
    finally:
        dev.clear_line_callback()
        dev.stop_all()
        time.sleep(0.3)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dev = connect()
    if dev is None:
        draw_status("JanOS not found. Check USB/UART cable & power. KEY3=Exit",
                    CLR_RED)
        while _running:
            if get_button(PINS, GPIO) == "KEY3":
                break
            time.sleep(0.1)
        LCD.LCD_Clear()
        GPIO.cleanup()
        return

    try:
        version = get_version(dev)
        port_name = os.path.basename(dev.port)
        while _running:
            action = run_menu(dev, port_name, version)
            if action is None:
                break
            dev.stop_all()  # reset firmware state before entering a feature
            time.sleep(0.2)
            if action == "deauth":
                feature_deauth(dev)
            elif action == "blackout":
                feature_blackout(dev)
            elif action == "observer":
                feature_observer(dev)
            elif action == "ble":
                feature_ble(dev)
    finally:
        try:
            dev.stop_all()
        except Exception:
            pass
        dev.close()
        draw_status("Disconnected.", CLR_YELLOW)
        time.sleep(0.8)
        LCD.LCD_Clear()
        GPIO.cleanup()


if __name__ == "__main__":
    main()
