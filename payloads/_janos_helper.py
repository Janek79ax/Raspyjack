"""
JanOS helper -- UART transport + parsers for ESP32C5 JanOS firmware.

JanOS exposes a text CLI over UART (115200 8N1). Commands are sent terminated
with "\r\n"; responses are line based. This module provides:

  - detect_janos()       auto-detect the serial port via a ping/pong handshake
  - class JanOS          open/close, send(), background line reader + callback
  - JanOS.run_until()    send a command and collect lines until a marker/timeout
  - parse_* helpers      network scan CSV, sniffer tree, BLE scan, RSSI

Usage:
    from payloads._janos_helper import detect_janos, JanOS

    port, baud = detect_janos()
    dev = JanOS(port, baud)
    dev.open()
    nets = []
    dev.run_until("Scan results printed", timeout=20, command="scan_networks",
                  on_line=lambda ln: nets.append(ln))
"""

import os
import glob
import threading
import time

try:
    import serial
except ImportError:  # pragma: no cover - environment without pyserial
    import subprocess
    subprocess.run(["pip3", "install", "--break-system-packages", "pyserial"],
                   capture_output=True, timeout=60)
    import serial


# Candidate ports, in priority order (USB serial first, then GPIO UART).
_PORTS = [
    "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2",
    "/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2",
    "/dev/ttyS0", "/dev/ttyAMA0", "/dev/ttyAMA1",
]

# JanOS runs at 115200; keep a couple of fallbacks just in case.
_BAUDS = [115200, 921600, 230400]

DEFAULT_BAUD = 115200


def _candidate_ports():
    """Return existing candidate serial ports (globbed + fixed UART nodes)."""
    found = []
    for pat in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        for p in sorted(glob.glob(pat)):
            if p not in found:
                found.append(p)
    for p in ("/dev/ttyS0", "/dev/ttyAMA0", "/dev/ttyAMA1"):
        if os.path.exists(p) and p not in found:
            found.append(p)
    # Preserve the documented priority where possible.
    ordered = [p for p in _PORTS if p in found]
    ordered += [p for p in found if p not in ordered]
    return ordered


def _ping_port(port, baud, timeout=1.2):
    """Send 'ping' on *port* and return True if 'pong' comes back."""
    try:
        ser = serial.Serial(port, baud, timeout=timeout)
    except Exception:
        return False
    try:
        time.sleep(0.2)
        ser.reset_input_buffer()
        ser.write(b"ping\r\n")
        ser.flush()
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            chunk = ser.read(128)
            if chunk:
                buf += chunk
                if b"pong" in buf.lower():
                    return True
        return False
    except Exception:
        return False
    finally:
        try:
            ser.close()
        except Exception:
            pass


def detect_janos():
    """Auto-detect the JanOS serial port via ping/pong.

    Returns (port, baud) or (None, None) when no JanOS device responds.
    """
    for port in _candidate_ports():
        for baud in _BAUDS:
            if _ping_port(port, baud):
                return port, baud
    return None, None


class JanOS:
    """Serial transport for a JanOS device with a background line reader."""

    def __init__(self, port, baud=DEFAULT_BAUD, line_term="\r\n"):
        self.port = port
        self.baud = baud
        self.line_term = line_term
        self._ser = None
        self._reader = None
        self._stop = threading.Event()
        self._cb_lock = threading.Lock()
        self._line_cb = None
        self._line_buf = ""

    # -- connection ---------------------------------------------------------

    def open(self):
        """Open the serial port and start the reader thread."""
        self._ser = serial.Serial(self.port, self.baud, timeout=0.1)
        time.sleep(0.2)
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self):
        """Stop the reader thread and close the port."""
        self._stop.set()
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=2)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def is_open(self):
        return self._ser is not None and getattr(self._ser, "is_open", False)

    # -- send ---------------------------------------------------------------

    def send(self, cmd):
        """Send a command line (terminator appended automatically)."""
        if not self._ser:
            return
        try:
            self._ser.write((cmd + self.line_term).encode("utf-8", "replace"))
            self._ser.flush()
        except Exception:
            pass

    def stop_all(self):
        """Send the universal JanOS 'stop' command."""
        self.send("stop")

    # -- line callback ------------------------------------------------------

    def set_line_callback(self, fn):
        """Register a callback fn(line) invoked for every received line."""
        with self._cb_lock:
            self._line_cb = fn

    def clear_line_callback(self):
        with self._cb_lock:
            self._line_cb = None

    def _emit(self, line):
        with self._cb_lock:
            cb = self._line_cb
        if cb:
            try:
                cb(line)
            except Exception:
                pass

    def _read_loop(self):
        """Accumulate bytes into lines and dispatch them to the callback."""
        while not self._stop.is_set():
            try:
                data = self._ser.read(256)
            except Exception:
                break
            if not data:
                continue
            text = data.decode("utf-8", "replace")
            for ch in text:
                if ch in "\r\n":
                    if self._line_buf:
                        self._emit(self._line_buf)
                        self._line_buf = ""
                else:
                    self._line_buf += ch
                    if len(self._line_buf) > 1024:
                        # Avoid unbounded growth on a stuck stream.
                        self._emit(self._line_buf)
                        self._line_buf = ""

    # -- request/response ---------------------------------------------------

    def run_until(self, marker, timeout=15.0, command=None, on_line=None,
                  idle_timeout=None):
        """Send *command* (optional) and collect lines until *marker* appears.

        marker       substring that signals completion (None = until timeout).
        timeout      hard upper bound in seconds.
        command      optional command to send before collecting.
        on_line      optional callback fn(line) for each received line.
        idle_timeout optional seconds of silence after which we give up early.

        Returns the list of collected lines.
        """
        collected = []
        done = threading.Event()
        last_rx = [time.time()]

        def _collector(line):
            last_rx[0] = time.time()
            collected.append(line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass
            if marker and marker in line:
                done.set()

        self.set_line_callback(_collector)
        try:
            if command is not None:
                self.send(command)
            deadline = time.time() + timeout
            while time.time() < deadline and not done.is_set():
                if idle_timeout and (time.time() - last_rx[0]) > idle_timeout:
                    break
                time.sleep(0.05)
        finally:
            self.clear_line_callback()
        return collected


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_network_csv(line):
    """Parse a scan_networks CSV line.

    Format: "index","SSID","","BSSID","channel","security","RSSI","band"
    Returns a dict or None if the line is not a network row.
    """
    if not line or line[0] != '"':
        return None
    fields = []
    p = 0
    n = len(line)
    while p < n and len(fields) < 8:
        if line[p] == '"':
            p += 1
            start = p
            while p < n and line[p] != '"':
                p += 1
            fields.append(line[start:p])
            if p < n:
                p += 1  # skip closing quote
            # skip comma separator
            if p < n and line[p] == ',':
                p += 1
        else:
            p += 1
    if len(fields) < 8:
        return None
    try:
        index = int(fields[0])
    except ValueError:
        return None
    try:
        rssi = int(fields[6])
    except ValueError:
        rssi = -99
    return {
        "index": index,
        "ssid": fields[1] or "<hidden>",
        "bssid": fields[3],
        "channel": fields[4],
        "security": fields[5],
        "rssi": rssi,
        "band": fields[7],
    }


def parse_sniffer_tree(lines):
    """Parse show_sniffer_results output into a list of APs with clients.

    AP lines have no leading space: "SSID, CHn: K"
    Client MAC lines have a leading space: " AA:BB:CC:DD:EE:FF"

    Returns list of dicts: {ssid, channel, count, clients:[mac,...]}.
    """
    aps = []
    current = None
    for raw in lines:
        if not raw:
            continue
        if "No APs with clients found" in raw:
            return []
        if raw[0] in (" ", "\t"):
            mac = raw.strip()
            if current is not None and mac:
                current["clients"].append(mac)
            continue
        # AP header line: "SSID, CHn: K"
        stripped = raw.strip()
        if not stripped or ":" not in stripped or "," not in stripped:
            continue
        ssid_part, rest = stripped.rsplit(",", 1)
        ssid = ssid_part.strip()
        channel = ""
        count = 0
        rest = rest.strip()  # "CHn: K"
        if ":" in rest:
            ch_token, cnt_token = rest.split(":", 1)
            ch_token = ch_token.strip()
            if ch_token.upper().startswith("CH"):
                channel = ch_token[2:].strip()
            else:
                channel = ch_token
            try:
                count = int(cnt_token.strip())
            except ValueError:
                count = 0
        current = {"ssid": ssid, "channel": channel, "count": count,
                   "clients": []}
        aps.append(current)
    return aps


def parse_bt_scan_line(line):
    """Parse a one-time scan_bt result row.

    Format: "  1. 09:2B:56:0E:1C:E0  RSSI: -82 dBm  Name: Foo"
    Returns dict {index, mac, rssi, name} or None.
    """
    if not line:
        return None
    s = line.strip()
    # Must start with "<n>. "
    dot = s.find(". ")
    if dot < 1 or not s[:dot].isdigit():
        return None
    try:
        index = int(s[:dot])
    except ValueError:
        return None
    rest = s[dot + 2:].strip()
    parts = rest.split()
    if not parts:
        return None
    mac = parts[0].strip()
    if mac.count(":") != 5:
        return None
    rssi = parse_rssi(rest)
    name = ""
    npos = rest.find("Name:")
    if npos >= 0:
        name = rest[npos + 5:].strip()
    return {"index": index, "mac": mac,
            "rssi": rssi if rssi is not None else -99, "name": name}


def parse_bt_track(line):
    """Parse a continuous scan_bt <MAC> tracking line.

    Format: "F1:2E:71:9F:C8:68  RSSI: -93 dBm  Name: Forerunner 935"
    Returns dict {mac, rssi, name} or None.
    """
    if not line or "RSSI:" not in line:
        return None
    s = line.strip()
    parts = s.split()
    if not parts or parts[0].count(":") != 5:
        return None
    mac = parts[0]
    rssi = parse_rssi(s)
    name = ""
    npos = s.find("Name:")
    if npos >= 0:
        name = s[npos + 5:].strip()
    return {"mac": mac, "rssi": rssi if rssi is not None else -99, "name": name}


def parse_rssi(line):
    """Extract the integer after 'RSSI:' from a line. Returns int or None."""
    if not line:
        return None
    pos = line.find("RSSI:")
    if pos < 0:
        return None
    tail = line[pos + 5:].strip()
    num = ""
    for ch in tail:
        if ch == '-' and not num:
            num += ch
        elif ch.isdigit():
            num += ch
        else:
            break
    if num in ("", "-"):
        return None
    try:
        return int(num)
    except ValueError:
        return None
