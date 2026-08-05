"""
smw_exit_tracker.py — live Super Mario World exit counter for OBS.

Reads the exit counter straight out of SNES RAM over USB (FXPak Pro / sd2snes
via SNI), so the hack's own logic decides what counts as an exit. Levels the
author marked as non-counting — switch palaces, bonus rooms — are filtered for
free, and keyhole/orb exits register the same as goal tape.

Two ways to run it:

1. CLI, for one-time setup on your cart:
       python smw_exit_tracker.py scan          # find the counter address
       python smw_exit_tracker.py probe F51F2E  # check counter + game mode
       python smw_exit_tracker.py watch F51F2E  # print one byte as it changes

2. OBS script (Tools -> Scripts -> +). Polls the address and rewrites the
   left-hand number of an "Exits 02/44" text source, leaving the total alone
   so another script can own it.

Requires SNI (https://github.com/alttpo/sni) running with the pak attached.

Full setup guide: see README.md

Address spaces in the FX Pak Pro map (pass these to `scan --base`):
    F50000-F6FFFF   WRAM mirror  ($7E0000-$7FFFFF)
    E00000-EFFFFF   cart SRAM    (save data)
    000000-DFFFFF   ROM
"""

import json
import re
import threading
import time

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

DEFAULT_URL = "ws://localhost:23074"  # 8080 is the deprecated legacy port
APP_NAME = "SMW Exit Overlay"

# SNI names emulator devices after the transport, not the emulator: BizHawk and
# snes9x-rr both show up as luabridge://host:port. Map what people actually type.
# Address profiles. Vanilla-base hacks keep SMW's variables in WRAM, which is
# what the FXPak mirrors and what emulators expose. Anything else goes through
# Custom addresses after a scan.
PROFILES = {
    "vanilla": {"address": "F51F2E", "gate_addr": "F50100", "arm_mode": "0A,0E"},
}

DEVICE_ALIASES = {
    "bizhawk": "luabridge",
    "snes9x": "luabridge",
    "emu": "luabridge",
    "emulator": "luabridge",
    "lua": "luabridge",
    "fxpak": "fxpakpro",
    "sd2snes": "fxpakpro",
    "pak": "fxpakpro",
}
WRAM_BASE = 0xF50000
LOWRAM_SIZE = 0x2000  # $7E:0000-$7E:1FFF covers SMW's working variables


# --------------------------------------------------------------------------
# usb2snes protocol
# --------------------------------------------------------------------------

class Usb2Snes:
    """Talks to SNI or QUsb2Snes, which front for both hardware and emulators."""

    def __init__(self, url=DEFAULT_URL, timeout=5.0, device_filter=None):
        self.url = url
        self.timeout = timeout
        self.device_filter = (device_filter or "").strip().lower()
        self.ws = None
        self.device = None
        self.devices = []

    def connect(self):
        if websocket is None:
            raise RuntimeError("websocket-client not installed for this Python")
        self.ws = websocket.create_connection(self.url, timeout=self.timeout)
        self._send("DeviceList")
        self.devices = json.loads(self.ws.recv()).get("Results", [])
        if not self.devices:
            raise RuntimeError(
                "No device found. Check that your FXPak is plugged in with a ROM "
                "running, or that your emulator's SNI/usb2snes connector is active.")

        # With both an emulator and a pak connected, "first device" is a coin
        # flip — let the user pin one by substring (e.g. "fxpak", "bizhawk").
        if self.device_filter:
            wanted = DEVICE_ALIASES.get(self.device_filter, self.device_filter)
            matches = [d for d in self.devices if wanted in d.lower()]
            if not matches:
                raise RuntimeError(
                    "No device matching %r. Found: %s\n"
                    "(Emulators appear as luabridge://... rather than by name.)"
                    % (self.device_filter, ", ".join(self.devices)))
            self.device = matches[0]
        else:
            self.device = self.devices[0]

        self._send("Attach", [self.device])
        self._send("Name", [APP_NAME])
        return self.device

    def _send(self, opcode, operands=None):
        self.ws.send(json.dumps({
            "Opcode": opcode,
            "Space": "SNES",
            "Operands": operands or [],
        }))

    def read(self, addr, size):
        """Read `size` bytes at `addr`. Replies arrive in <=1024 byte chunks."""
        self._send("GetAddress", [format(addr, "X"), format(size, "X")])
        buf = bytearray()
        while len(buf) < size:
            chunk = self.ws.recv()
            if isinstance(chunk, str):
                raise RuntimeError("Unexpected text reply: %s" % chunk[:120])
            buf.extend(chunk)
        return bytes(buf)

    def read_u8(self, addr):
        return self.read(addr, 1)[0]

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None


# --------------------------------------------------------------------------
# CLI: address discovery
# --------------------------------------------------------------------------

def warn_if_wram_unavailable(snes):
    """Detect the 0x55 filler that means the WRAM mirror isn't populated.

    On an FXPak Pro this is what SA-1 hacks look like: the pak plays them fine,
    but its FPGA can't expose their memory, so every byte reads as filler.
    """
    try:
        sample = snes.read(WRAM_BASE, 0x40)
    except Exception:
        return False
    if len(set(sample)) == 1:
        print("\n  !! Memory reads as constant 0x%02X — nothing real is being"
              " exposed here.\n" % sample[0])
        print("     On an FXPak Pro: expected for SA-1 hacks. The pak plays")
        print("     them but cannot mirror their RAM. Use an emulator instead.\n")
        print("     On an emulator: usually the wrong core. SNI's connector")
        print("     reads through BizHawk's System Bus domain, which the")
        print("     Snes9x core does not expose. Switch to BSNES under")
        print("     Config -> Cores -> SNES, reload the ROM, and restart the")
        print("     connector script. Check BizHawk's Lua console for")
        print("     'Unable to find domain: System Bus'.\n")
        return True
    return False


def snes_label(addr):
    """Human-readable SNES address for an FX Pak Pro address, where possible."""
    if WRAM_BASE <= addr < WRAM_BASE + 0x20000:
        offset = addr - WRAM_BASE
        bank = 0x7E + (offset >> 16)
        return "($%02X:%04X)" % (bank, offset & 0xFFFF)
    if 0xE00000 <= addr < 0xF00000:
        return "(SRAM +%04X)" % (addr - 0xE00000)
    return ""


def cmd_devices():
    """List everything SNI can see — hardware and emulators alike."""
    snes = Usb2Snes()
    snes.connect()
    for name in snes.devices:
        print(" ", name)
    print("\nPass part of a name to --device to pin one, e.g. --device fxpak")


def cmd_scan(base=WRAM_BASE, size=LOWRAM_SIZE, delta=1, device=None):
    """Snapshot, wait for you to clear a level, report bytes that went up by `delta`."""
    snes = Usb2Snes(device_filter=device)
    print("Connected to:", snes.connect())
    warn_if_wram_unavailable(snes)
    print("Scanning %06X-%06X" % (base, base + size - 1))

    before = snes.read(base, size)
    candidates = None

    while True:
        input("\nClear a level that SHOULD count as an exit, then press Enter... ")
        after = snes.read(base, size)
        hits = {i for i in range(size) if after[i] == (before[i] + delta) & 0xFF}
        changed = sum(1 for i in range(size) if after[i] != before[i])
        candidates = hits if candidates is None else (candidates & hits)
        before = after

        # If nothing at all moved, the region isn't live — either the game
        # doesn't use it (SA-1 relocates most of SMW's RAM) or you're reading
        # the wrong address space.
        print("%d of %d bytes changed at all" % (changed, size))
        print("%d candidate(s):" % len(candidates))
        for i in sorted(candidates)[:20]:
            print("   %06X  %-14s = %d" % (base + i, snes_label(base + i), after[i]))
        if not candidates:
            print("Nothing incremented. See the SA-1 note at the bottom of this file.")
            return
        if len(candidates) <= 3:
            print("\nNarrow enough — verify with:  python smw_exit_tracker.py watch %06X"
                  % (base + sorted(candidates)[0]))


def cmd_watch(addr, device=None):
    """Print the byte at `addr` whenever it changes — sanity-check before going live."""
    snes = Usb2Snes(device_filter=device)
    print("Connected to:", snes.connect())
    last = None
    while True:
        value = snes.read_u8(addr)
        if value != last:
            print(time.strftime("%H:%M:%S"), "%06X = %d" % (addr, value))
            last = value
        time.sleep(0.15)


def cmd_probe(counter_addr, gate_addr=0xF50100, device=None):
    """Show game mode and exit counter side by side, to pick a gate threshold."""
    snes = Usb2Snes(device_filter=device)
    print("Connected to:", snes.connect())
    warn_if_wram_unavailable(snes)
    print("Move between title screen, file select, overworld and a level.")
    print("Note the mode value where the counter becomes trustworthy.\n")
    last = None
    while True:
        mode = snes.read_u8(gate_addr)
        count = snes.read_u8(counter_addr)
        if (mode, count) != last:
            print("mode %02X   counter %3d" % (mode, count))
            last = (mode, count)
        time.sleep(0.1)


# --------------------------------------------------------------------------
# OBS script
# --------------------------------------------------------------------------

try:
    import obspython as obs
except ImportError:
    obs = None

settings = {
    "source": "",
    "profile": "vanilla",
    "url": DEFAULT_URL,
    "device": "",
    "address": "F51F2E",
    "gate_addr": "F50100",
    "gate_min": "0B",
    "gate_max": "1F",
    "arm_mode": "0A,0E",
    "fallback_total": 96,
    "poll_ms": 200,
}
state = {"count": None, "total": None, "run": False, "thread": None, "status": "idle"}
lock = threading.Lock()


def _poll_loop():
    """Network work stays off the OBS main thread; only the timer touches sources."""
    backoff = 1.0
    while state["run"]:
        snes = Usb2Snes(settings["url"], device_filter=settings["device"])
        try:
            snes.connect()
            backoff = 1.0
            with lock:
                state["status"] = "connected"
            addr = int(settings["address"], 16)
            gate_addr = int(settings["gate_addr"], 16) if settings["gate_addr"].strip() else None
            gate_min = int(settings["gate_min"], 16)
            gate_max = int(settings["gate_max"], 16) if settings["gate_max"].strip() else 0xFF
            arm_modes = {int(m, 16) for m in settings["arm_mode"].replace(",", " ").split()}
            pending, pending_hits = None, 0
            armed = not arm_modes
            while state["run"]:
                # Gate first: while the game mode says title screen or file
                # select, WRAM holds nothing meaningful, so don't read at all.
                # Once a file is loaded, whatever the counter says is the truth —
                # including a legitimate 0 on a brand new file.
                if gate_addr is not None:
                    mode = snes.read_u8(gate_addr)
                    # A mode outside the valid range means WRAM is not holding
                    # real game state — during a ROM load it reads 0x55 filler.
                    if mode == 0x55:
                        with lock:
                            state["status"] = "memory unavailable (SA-1 on hardware?)"
                        time.sleep(settings["poll_ms"] / 1000.0)
                        continue
                    if mode < gate_min or mode > gate_max:
                        armed = False
                        with lock:
                            state["status"] = "holding (mode %02X)" % mode
                        time.sleep(settings["poll_ms"] / 1000.0)
                        continue

                    # The title screen demo plays a real level, so it clears the
                    # gate while no file is loaded — and a freshly swapped ROM
                    # still has the previous hack's junk in the counter byte.
                    # Only trust readings once the overworld has been reached,
                    # which the demo never does.
                    if mode in arm_modes:
                        armed = True
                    if arm_modes and not armed:
                        with lock:
                            state["status"] = "waiting for overworld (mode %02X)" % mode
                        time.sleep(settings["poll_ms"] / 1000.0)
                        continue

                    with lock:
                        state["status"] = "connected (mode %02X)" % mode

                value = snes.read_u8(addr)

                # Mode and counter are separate USB round trips. If the console
                # left the game between them (e.g. dropped to the FXPak menu,
                # which reads mode 00 with junk in the counter byte), the sample
                # is torn — re-check the mode and throw it away if so.
                recheck = snes.read_u8(gate_addr) if gate_addr is not None else None
                if recheck is not None and not (gate_min <= recheck <= gate_max):
                    time.sleep(settings["poll_ms"] / 1000.0)
                    continue

                with lock:
                    last = state["count"]

                if gate_addr is None and value == 0 and last:
                    # No gate configured — fall back to the old heuristic.
                    pass
                elif last is not None and value < last:
                    # Small confirmation window rides out garbage during the
                    # fade-in while a file is still loading.
                    if value == pending:
                        pending_hits += 1
                    else:
                        pending, pending_hits = value, 1
                    if pending_hits >= 3:
                        with lock:
                            state["count"] = value
                        pending, pending_hits = None, 0
                else:
                    pending, pending_hits = None, 0
                    with lock:
                        state["count"] = value

                time.sleep(settings["poll_ms"] / 1000.0)
        except Exception as exc:
            with lock:
                state["status"] = "reconnecting: %s" % exc
            time.sleep(backoff)
            backoff = min(backoff * 2, 15.0)
        finally:
            snes.close()


def _tick():
    """Runs on the OBS main thread. Rewrites only the left half of 'Exits xx/yy'."""
    with lock:
        count = state["count"]
    if count is None:
        return

    source = obs.obs_get_source_by_name(settings["source"])
    if source is None:
        return
    try:
        data = obs.obs_source_get_settings(source)
        current = obs.obs_data_get_string(data, "text") or ""
        obs.obs_data_release(data)

        match = re.search(r"(\d+)\s*/\s*(\d+)", current)
        total = int(match.group(2)) if match else settings["fallback_total"]

        # If the total changed, the kaizoff script just swapped hacks on us.
        # The held count belongs to the old hack — drop it and let whatever
        # that script wrote stand until a fresh gated read comes in.
        with lock:
            previous_total = state.get("total")
            state["total"] = total
            if previous_total is not None and total != previous_total:
                state["count"] = None
                return

        width = max(2, len(str(total)))
        new_text = "Exits %0*d/%0*d" % (width, count, width, total)

        if new_text != current.strip():
            update = obs.obs_data_create()
            obs.obs_data_set_string(update, "text", new_text)
            obs.obs_source_update(source, update)
            obs.obs_data_release(update)
    finally:
        obs.obs_source_release(source)


def script_description():
    return ("Polls the SMW exit counter from an FXPak Pro via SNI/QUsb2Snes and keeps "
            "the exits text source live. Run this file from a terminal with 'scan' "
            "to find the counter address for your hack first.")


def script_properties():
    props = obs.obs_properties_create()

    picker = obs.obs_properties_add_list(
        props, "source", "Exits text source",
        obs.OBS_COMBO_TYPE_EDITABLE, obs.OBS_COMBO_FORMAT_STRING)
    sources = obs.obs_enum_sources()
    if sources:
        for src in sources:
            if "text" in obs.obs_source_get_unversioned_id(src):
                name = obs.obs_source_get_name(src)
                obs.obs_property_list_add_string(picker, name, name)
        obs.source_list_release(sources)

    profile = obs.obs_properties_add_list(
        props, "profile", "Hack type",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_list_add_string(profile, "Vanilla-base hack (WRAM)", "vanilla")
    obs.obs_property_list_add_string(profile, "Custom addresses", "custom")
    obs.obs_property_set_modified_callback(profile, _profile_changed)

    obs.obs_properties_add_text(props, "url", "SNI WebSocket URL", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "device", "Device filter (blank = first found)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "address", "Counter address (hex)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "gate_addr", "Game mode address (hex, blank = off)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "gate_min", "Minimum game mode (hex)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "gate_max", "Maximum game mode (hex)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, "arm_mode", "Arm on game modes (hex, comma-separated)", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_int(props, "fallback_total", "Fallback total exits", 1, 999, 1)
    obs.obs_properties_add_int(props, "poll_ms", "Poll interval (ms)", 50, 2000, 50)
    obs.obs_properties_add_text(props, "status", "Status", obs.OBS_TEXT_INFO)
    return props


def _profile_changed(props, prop, data):
    """Fill the address fields when a profile is picked, and hide them unless
    the user explicitly wants to hand-edit."""
    choice = obs.obs_data_get_string(data, "profile")
    preset = PROFILES.get(choice)
    if preset:
        for key, value in preset.items():
            obs.obs_data_set_string(data, key, value)
    custom = preset is None
    for key in ("address", "gate_addr", "gate_min", "gate_max", "arm_mode"):
        target = obs.obs_properties_get(props, key)
        if target:
            obs.obs_property_set_visible(target, custom)
    return True


def script_defaults(data):
    obs.obs_data_set_default_string(data, "profile", "vanilla")
    obs.obs_data_set_default_string(data, "url", DEFAULT_URL)
    obs.obs_data_set_default_string(data, "device", "")
    obs.obs_data_set_default_string(data, "address", "F51F2E")
    obs.obs_data_set_default_string(data, "gate_addr", "F50100")
    obs.obs_data_set_default_string(data, "gate_min", "0B")
    obs.obs_data_set_default_string(data, "gate_max", "1F")
    obs.obs_data_set_default_string(data, "arm_mode", "0A,0E")
    obs.obs_data_set_default_int(data, "fallback_total", 96)
    obs.obs_data_set_default_int(data, "poll_ms", 200)


def script_update(data):
    settings["source"] = obs.obs_data_get_string(data, "source")
    settings["profile"] = obs.obs_data_get_string(data, "profile") or "vanilla"
    settings["url"] = obs.obs_data_get_string(data, "url")
    settings["device"] = obs.obs_data_get_string(data, "device")
    settings["address"] = obs.obs_data_get_string(data, "address").replace("$", "").strip()
    settings["gate_addr"] = obs.obs_data_get_string(data, "gate_addr").replace("$", "").strip()
    settings["gate_min"] = obs.obs_data_get_string(data, "gate_min").replace("$", "").strip() or "0"
    settings["gate_max"] = obs.obs_data_get_string(data, "gate_max").replace("$", "").strip()
    settings["arm_mode"] = obs.obs_data_get_string(data, "arm_mode").replace("$", "").strip()
    settings["fallback_total"] = obs.obs_data_get_int(data, "fallback_total")
    settings["poll_ms"] = obs.obs_data_get_int(data, "poll_ms")

    # A named profile overrides whatever is sitting in the address boxes, so a
    # stale hand-edit can't quietly survive a profile switch.
    preset = PROFILES.get(settings["profile"])
    if preset:
        settings.update(preset)

    script_unload()
    with lock:
        state["count"] = None
        state["total"] = None
    state["run"] = True
    state["thread"] = threading.Thread(target=_poll_loop, daemon=True)
    state["thread"].start()
    obs.timer_add(_tick, 250)


def script_unload():
    state["run"] = False
    if obs:
        obs.timer_remove(_tick)
    thread = state.get("thread")
    if thread and thread.is_alive():
        thread.join(timeout=2.0)
    state["thread"] = None


# --------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Find/watch the SMW exit counter over usb2snes")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list devices SNI can see")

    scan = sub.add_parser("scan", help="diff-scan for the counter byte")
    scan.add_argument("--base", default=hex(WRAM_BASE))
    scan.add_argument("--size", default=hex(LOWRAM_SIZE))
    scan.add_argument("--delta", type=int, default=1)

    watch = sub.add_parser("watch", help="print a byte whenever it changes")
    watch.add_argument("address")

    probe = sub.add_parser("probe", help="show game mode alongside the counter")
    probe.add_argument("address", help="counter address, e.g. F51F2E")
    probe.add_argument("--gate", default="F50100")

    for sub_parser in (scan, watch, probe):
        sub_parser.add_argument("--device", default=None,
                                help="substring of the device name, e.g. fxpak or bizhawk")

    args = parser.parse_args()
    if args.cmd == "devices":
        cmd_devices()
    elif args.cmd == "scan":
        cmd_scan(int(args.base, 16), int(args.size, 16), args.delta, args.device)
    elif args.cmd == "probe":
        cmd_probe(int(args.address, 16), int(args.gate, 16), args.device)
    else:
        cmd_watch(int(args.address, 16), args.device)
