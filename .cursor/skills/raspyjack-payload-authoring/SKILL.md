---
name: raspyjack-payload-authoring
description: Author RaspyJack payloads for Raspberry Pi LCD/WebUI operation. Use when creating, editing, reviewing, or explaining payloads under payloads/, especially payload UI screens, GPIO/WebUI input, Linux command interaction, WiFi monitor/AP mode, external USB WiFi adapters, GPS, Bluetooth, NFC, SDR, loot storage, and cleanup.
---

# RaspyJack Payload Authoring

Use this skill whenever the user asks to create, modify, review, or explain a
RaspyJack payload. RaspyJack payloads are standalone Python scripts launched from
`payloads/` by `raspyjack.py`; each payload temporarily owns the LCD, buttons,
Linux tools, and any external hardware it starts.

## First Read

Before changing or generating a payload, inspect these files in the repository:

- `payloads/examples/_payload_template.py` for the current starter structure.
- `payloads/examples/example_show_buttons.py` for minimal input behavior.
- `payloads/_input_helper.py` for WebUI + GPIO button handling.
- `payloads/_display_helper.py` for scaled drawing.
- `payloads/_keyboard_helper.py` for text entry.
- `payloads/_iface_helper.py` for WiFi/Ethernet interface selection.
- A nearby payload in the same category for local conventions.

For WiFi, also inspect `payloads/wifi/deauth.py` and
`payloads/utilities/wifi_dongle_installer.py`. For GPS, inspect
`payloads/_gps_helper.py`. For JanOS, inspect `payloads/_janos_helper.py`. For
SDR, inspect `payloads/sdr/_sdr_core.py`.

## Payload Registration

- Put executable payloads in `payloads/<category>/<name>.py`.
- Do not prefix executable payload filenames with `_`; that hides them from the
  launcher.
- Helper modules may start with `_` and are imported by payloads, not launched.
- The menu is filesystem-discovered; no registry edit is required.
- Optional icons go in `menu_icons.json` under `payloads`, keyed by the display
  label with a leading space.
- Payloads should write durable artifacts under `loot/<PayloadName>/` or an
  existing category-specific loot folder.

Current categories include `reconnaissance`, `wifi`, `network`, `credentials`,
`bluetooth`, `usb`, `exfiltration`, `evasion`, `remote_access`, `nfc_rfid`,
`sdr`, `ai`, `utilities`, `hardware`, `games`, and `examples`.

## Runtime Contract

RaspyJack launches payloads through `exec_payload()`:

1. The main UI writes `/dev/shm/rj_payload_state.json`.
2. The main UI sets `screen_lock` and clears the LCD.
3. The payload runs as a blocking `python3` subprocess.
4. stdout and stderr are appended to `loot/payload.log`.
5. On exit, RaspyJack reinitializes GPIO/LCD and redraws the menu.

Design payloads as cooperative foreground programs:

- Only one payload should own the LCD/buttons at a time.
- The parent menu cannot kill a payload directly during normal operation.
- WebUI Stop sends a virtual `KEY3`; the payload must poll input and exit.
- Every payload must treat `KEY3` as exit/back.
- Avoid launching sibling payloads with unmanaged subprocesses; use RaspyJack
  launch paths or explicit scheduler/automation utilities when needed.

## Screen Handling

Use a 128-base layout even on larger displays:

- Create frames at the active LCD size.
- Draw with `payloads._display_helper.ScaledDraw`.
- Use `scaled_font()` for readable fonts across 128x128, 240x240, and Cardputer
  style displays.
- Keep coordinates in 0..127 base units when using `ScaledDraw`.
- Push frames with the LCD driver's show-image method; the WebUI mirror updates
  automatically from LCD frames.

Recommended screen structure:

- Header: short payload title and state.
- Body: the current list, dashboard, progress, or result.
- Footer: controls, especially `KEY3: Exit` or `KEY3: Cancel`.
- Use green for normal/primary text, cyan for headers/info, yellow for warnings,
  red for destructive/errors, white for secondary text, black background.
- Truncate labels aggressively; LCD width is limited.
- Redraw when state changes or at a controlled refresh rate, not in a tight loop.

For multi-screen payloads, use explicit states such as `idle`, `scan`,
`select`, `run`, `results`, and `error`. Each state should define its controls
and draw function. Keep user feedback visible during long operations.

Games may render at a fixed timestep and can use held-button input, but still
need `KEY3` exit and cleanup.

## Input Handling

Always use `payloads._input_helper.get_button()` for interactive payloads.

- It checks WebUI virtual buttons before physical GPIO.
- It applies display flip mapping from `gui_conf.json`.
- It includes debounce behavior shared across payloads.
- It allows WebUI Stop to work because Stop sends `KEY3`.

Use button roles consistently:

- `UP`, `DOWN`, `LEFT`, `RIGHT`: navigation, scrolling, cursor movement, value
  adjustment.
- `OK`: select, confirm, start, pause, or activate.
- `KEY1`: mode switch or primary secondary action.
- `KEY2`: backspace, stop sub-action, reset, or alternate action.
- `KEY3`: exit/back/cancel.

For text input, use `payloads._keyboard_helper.lcd_keyboard()` instead of
inventing a new keyboard. Choose the narrowest charset that fits the prompt:
`full`, `alpha`, `digits`, `ip`, `hex`, `mac`, or `url`.

For smoother movement in games, use held-button support from `_input_helper`.
For advanced long-press/double-click behavior, consider `input_events.py`, but
keep the payload's visible controls simple.

## Linux Interaction

Payloads often run Linux tools. Treat tool usage as part of the payload lifecycle:

- Detect missing tools early and show a clear LCD error.
- Log command output to stdout/stderr or a payload-specific loot log.
- Use timeouts for subprocess calls that can hang.
- Prefer explicit argument lists over shell strings where practical.
- If shell features are required, keep quoting and input validation strict.
- Keep long-running commands in worker threads or managed subprocesses so the UI
  loop can still poll `KEY3`.
- Track every process started by the payload and stop it during cleanup.
- Restore kernel, firewall, NetworkManager, Bluetooth, gpsd, or SDR state that
  the payload changes.

Common Linux resources:

- Network state: `ip`, `iw`, `nmcli`, `rfkill`, `sysctl`, `iptables`, `nft`.
- Capture: `tcpdump`, `tshark`, Scapy, raw AF_PACKET sockets.
- WiFi attacks: `airmon-ng`, `airodump-ng`, `aireplay-ng`, `hcxdumptool`,
  `hcxpcapngtool`, `reaver`, `bully`, `hostapd`, `dnsmasq`.
- Bluetooth: BlueZ, `bluetoothctl`, `hciconfig`, `hcitool`, `l2ping`, Bleak,
  bluepy.
- GPS: `gpsd`, `gpsmon`, NMEA serial devices.
- SDR: `rtl_test`, `rtl_sdr`, `rtl_fm`, `rtl_433`, `dump1090`, `multimon-ng`,
  optional SoapySDR/HackRF tooling.
- NFC: PN532 helper modules and installed MIFARE/NFC tools.

## WiFi and External USB Adapters

Assume the onboard Pi WiFi is for management/WebUI, not attacks. WiFi attack
payloads should prefer an external USB WiFi adapter.

Use `_iface_helper` for interface discovery:

- `list_interfaces("wifi")` returns WiFi interfaces with driver, IP, state,
  onboard flag, AP support, and monitor support.
- `select_interface(..., iface_type="wifi", require_monitor=True)` should be the
  default for monitor/injection payloads.
- USB WiFi appears before onboard WiFi in selectors.
- Do not hardcode `wlan1` unless the payload is intentionally limited and clearly
  documents that assumption.

Adapter policy:

- Monitor/injection payloads need a monitor-capable external adapter.
- AP/captive portal payloads need an adapter with AP/master mode support.
- Evil twin workflows are most reliable with two adapters: one AP-mode adapter
  and one monitor/injection adapter.
- Realtek out-of-tree drivers may not report monitor mode in `iw phy`; rely on
  `_iface_helper` known-driver fallback.
- RTL8192CU/RTL8188CUS style adapters can be unreliable for injection even when
  monitor mode appears to work.

Monitor mode lifecycle:

1. Unblock WiFi with rfkill if needed.
2. Prefer per-interface NetworkManager unmanagement.
3. Stop per-interface wpa_supplicant interference.
4. Bring the interface down.
5. Switch it to monitor mode with `iw`.
6. Bring it up and verify with `iw dev`.
7. Accept either the original name or an `ifacemon` name if airmon-ng renamed it.
8. Set channels explicitly during capture or injection.
9. On exit, stop monitor mode, restore managed mode, re-manage the interface, and
   restart NetworkManager only when necessary.

Avoid `airmon-ng check kill` unless the payload deliberately accepts that it may
break management WiFi and WebUI connectivity. Prefer targeted per-interface
cleanup.

AP/captive portal lifecycle:

- Generate temporary hostapd/dnsmasq config under `/tmp` or payload loot/config.
- Stop stale hostapd/dnsmasq instances owned by the payload before starting.
- Configure IP/DHCP/DNS/firewall state explicitly.
- Display SSID, channel, client count, captured events, and stop controls.
- On exit, stop hostapd/dnsmasq, remove temporary configs, restore firewall/NAT,
  and return the adapter to a sane state.

## Other Hardware Patterns

GPS:

- Use `_gps_helper` instead of opening serial ports directly.
- Do not open the GPS TTY in multiple places while gpsd owns it.
- Release serial-getty on UART GPS devices.
- Display fix quality, satellites, latitude/longitude, and last update time.

JanOS ESP32-C5:

- Use `_janos_helper.detect_janos()` and `JanOS`.
- Treat UART as request/response with timeouts.
- Send `stop` during cleanup if a JanOS RF action is active.
- Display serial port, firmware response, selected networks, and action state.

Bluetooth:

- Prefer helper or BlueZ readiness checks before scanning.
- If the payload stops `bluetoothd`, restore it on exit.
- Do not assume multiple Bluetooth payloads can run concurrently.

NFC:

- Use the NFC helper modules rather than duplicating PN532 transport logic.
- Separate read, analyze, write, and saved-dump flows on screen.
- Warn before destructive operations such as format or overwrite.

SDR/Sub-GHz:

- Use `_sdr_core` for detection and RTL-SDR process management where applicable.
- Only one rtl_* process should own the SDR at a time.
- Kill stale rtl_* processes only when the payload is clearly starting an SDR
  session and document that behavior.
- Store captures under `loot/SDR/`.

USB gadget:

- Check that gadget mode/configfs paths exist before changing them.
- Clearly tell the user when a mode requires the USB OTG/data port.
- Restore gadget state if the payload changes it.

## Long Operations and Cancellation

Keep the UI responsive:

- Poll buttons every 50 to 100 ms in the foreground UI loop.
- Run scans, captures, servers, and brute-force tasks in a worker thread or
  managed subprocess.
- Share data through locks or queues.
- Use a stop event checked by all workers.
- `KEY3` should set the stop event, stop subprocesses, and return to menu.
- `KEY2` may stop only the current sub-action while keeping the payload open.

For subprocesses:

- Store process handles.
- Start process groups when the payload must terminate a tool tree.
- Capture output to a file or parse it incrementally.
- Apply timeouts or idle timeouts for commands that can hang.
- Clean up child processes in `finally` logic.

## Logging and Loot

- Use stdout for routine diagnostics; RaspyJack captures it in `loot/payload.log`.
- Use stderr for errors.
- Store operator-visible artifacts under `loot/<PayloadName>/` or the category
  directory already used by similar payloads.
- Include timestamps in filenames for scans, captures, and exports.
- Keep configuration under `config/<payload>/` when it should survive runs.
- Show the saved path on the LCD after important captures.

## Documentation Inside Payloads

The top docstring should include:

- One-line summary.
- Hardware requirements.
- External tool dependencies.
- Controls per state or screen.
- Output/loot paths.
- Any state changes the payload makes to WiFi, Bluetooth, GPS, SDR, firewall, or
  services.

When reviewing an existing payload, check whether the docstring still matches
the behavior.

## Review Checklist

Before considering a payload finished:

- It launches from `payloads/<category>/<name>.py` and is not hidden by `_`.
- It imports shared helpers from the repository instead of duplicating them.
- It uses `get_button()` and exits on `KEY3`.
- It uses scaled drawing or intentionally documents a game/framebuffer exception.
- It has a visible header, body, footer, and error screen.
- It keeps the UI responsive during long work.
- It validates required Linux tools and hardware.
- It selects interfaces with `_iface_helper` instead of hardcoding adapter names.
- It protects onboard WiFi from attack-mode changes unless explicitly intended.
- It restores monitor/AP/managed mode and NetworkManager state as needed.
- It stops hostapd, dnsmasq, tcpdump, airodump, rtl_*, Bluetooth, GPS, or other
  child services it started.
- It writes loot/config files to predictable project locations.
- It logs useful diagnostics to `loot/payload.log`.
- It avoids unrelated changes to `raspyjack.py`, WebUI, LCD drivers, or global
  configuration unless the user explicitly asks for them.
