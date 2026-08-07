# SMW Exit Tracker

Live exit counter for Super Mario World hack streams, reading the game's own exit total straight out of SNES memory — on real hardware or in an emulator.

Built for vanilla-base hacks. ROMs using enhancement chips like SA-1 aren't supported; count those by hand.

Point it at an OBS text source and your exit count updates the moment you clear a level — no hotkeys, no manual editing between attempts, no counting on stream.

```
Exits 07/44
```

## Why read RAM instead of watching the screen

The obvious approach is detecting the COURSE CLEAR banner with image matching. That runs into two problems fast: keyhole and orb exits never show a banner at all, and plenty of levels aren't supposed to count — switch palaces, bonus rooms, whatever the author flagged.

Reading the game's own exit counter sidesteps both. The ROM already knows which goals increment the total, so the filtering is exact and needs no per-hack configuration. Clear a non-counting switch palace and the number simply doesn't move.

## What you need

Everyone needs these:

| | |
|---|---|
| **Software** | [SNI](https://github.com/alttpo/sni/releases) — bridges your SNES (real or emulated) to your PC |
| **Python** | 3.8+, plus `websocket-client` |
| **OBS** | Any recent version with Python scripting configured |

Plus **one** of:

| | |
|---|---|
| **Hardware** | FXPak Pro or sd2snes with a USB cable, in any SNES — Super Nt, original console, whatever |
| **Emulator** | BizHawk or snes9x-rr (via SNI's Lua connector), or RetroArch with a bsnes-mercury core |

SNI presents both the same way, so the setup below is nearly identical either route.

## Setup

### 1. Install SNI

Download the latest release from [github.com/alttpo/sni](https://github.com/alttpo/sni/releases), unzip it anywhere, and run `sni.exe`. It lives in your system tray and needs no configuration. Leave it running whenever you stream.

**On hardware:** plug the FXPak into your PC over USB and boot a hack.

**On emulator:** connect your emulator to SNI. BizHawk and snes9x-rr use SNI's `Connector.lua` script (bundled with SNI, in its `lua` folder) — open it via the emulator's Lua console while your ROM is running. RetroArch needs network commands enabled in its settings; SNI finds it from there.

Either way, confirm SNI can see it:

```
python smw_exit_tracker.py devices
```

If both an emulator and a pak show up, note part of the name you want — you'll pin it with `--device` on the CLI and the "Device filter" box in OBS. Otherwise the script takes whatever it finds first, which is fine for a single device.

Note that SNI names emulators after the connection type, so BizHawk shows up as something like `luabridge://127.0.0.1:62748` rather than "BizHawk". You can still type `--device bizhawk` — the script maps the common names (`bizhawk`, `snes9x`, `fxpak`, `sd2snes`) to what SNI actually reports.

### 2. Install the Python dependency

Important: OBS uses its own Python installation, which is often *not* the one on your PATH. In OBS, go to **Tools → Scripts → Python Settings** and note the path shown there. Install using that exact interpreter:

```
C:\Python312\python.exe -m pip install websocket-client
```

If that fails with a permissions error, either run the terminal as Administrator or add `--user`.

### 3. Find your counter address

The address the counter lives at can vary between hacks, so confirm it on your own setup. With the hack running and SNI up:

```
python smw_exit_tracker.py scan
```

(Add `--device fxpak` or `--device bizhawk` if you have more than one connected.)

It snapshots memory, waits for you to clear a level, then lists every byte that went up by exactly one. Clear two or three levels and the list narrows to a single address — usually `F51F2E`.

Confirm it with:

```
python smw_exit_tracker.py probe F51F2E
```

This prints the counter alongside the game mode as you play. Move between the title screen, file select, overworld, and a level. You should see the counter hold your real total and tick up when you clear something.

### 4. Load the OBS script

**Tools → Scripts → +**, pick `smw_exit_tracker.py`, then fill in:

- **Exits text source** — the text source to update
- **Hack type** — leave on *Vanilla-base*, which sets the addresses for you. *Custom addresses* lets you hand-edit them if `scan` found something unusual.
- **Device filter** — leave blank for a single device; set to `bizhawk` or `fxpak` if both are connected

The other settings have working defaults. Make sure your text source contains something in `07/44` form; the script rewrites only the left number and leaves the total untouched, so a separate script can manage that half.

Load a save file and the count should appear.

## Settings

| Setting | Default | What it does |
|---|---|---|
| Exits text source | — | Which OBS text source to update |
| Hack type | Vanilla-base | Preset addresses; choose Custom to edit them by hand |
| SNI WebSocket URL | `ws://localhost:23074` | Where SNI listens |
| Device filter | blank | Substring of a device name; blank uses the first found |
| Counter address | `F51F2E` | The exit counter byte |
| Game mode address | `F50100` | Byte telling us what the game is doing |
| Minimum game mode | `0B` | Below this, no file is loaded |
| Maximum game mode | `1F` | Above this, the value isn't a real mode |
| Arm on game modes | `0A,0E` | Comma-separated; one must be seen before trusting reads |
| Fallback total | `96` | Used if the text source has no `/total` |
| Poll interval | `200` ms | How often to read while idle |
| Level-end trigger address | `F51493` | End-of-level timer; fires the fast-poll burst |
| Trigger fires above | `0E` | The timer is set high then counts down |
| Burst poll interval | `50` ms | Read rate for a few seconds after a level ends |
| Burst duration | `5` s | How long to keep polling fast |
| Arm after N seconds | `10` | Fallback arming for hacks with no overworld |
| ROM title address | `007FC0` | Identifies the loaded hack for per-hack counts |

## Streaming checklist

1. Game running — console with the FXPak plugged in, or your emulator with its connector script active
2. SNI running in the tray
3. OBS open — the script starts polling by itself

That's the whole routine. Consider adding SNI to your Windows startup so step 2 stops being something to remember.

## Emulator support

SNI supports emulators through two routes:

- **Lua Bridge** — BizHawk and snes9x-rr, via SNI's `Connector.lua`
- **RetroArch** — with a bsnes-mercury core and network commands enabled

**MesenCE is not supported.** SNI has no driver for it, and Mesen's Lua API doesn't provide the socket support a bridge connector would need. If you want a Mesen-family emulator to work with SNI, that request belongs upstream with SNI or MesenCE, not here.

## Troubleshooting

**Nothing appears at all.** First check the Script Log — most causes name themselves there, including an unset text source.

**Still nothing.** Run `python smw_exit_tracker.py devices` — if your console or emulator isn't listed, the problem is between SNI and the game, not this script. On emulator, that usually means the connector Lua script isn't running. Then try `probe`, which fails with a clear error, unlike the OBS script, which quietly retries.

**Wrong device attached.** With an emulator and a pak both connected, set the Device filter to part of the name you want.

**Everything reads as `0x55`.** On an emulator, this is the wrong core. SNI's connector reads BizHawk's System Bus memory domain, which the Snes9x core doesn't expose — switch to BSNES under Config → Cores → SNES, reload the ROM, and restart `Connector.lua`. BizHawk's Lua console shows `Unable to find domain: System Bus` when this is the problem. On hardware, it means the ROM uses an enhancement chip the pak can't mirror — not supported.

**Script won't load in OBS.** Almost always `websocket-client` installed to the wrong Python. Recheck the interpreter path in Tools → Scripts → Python Settings.

**Number doesn't move after a clear.** Re-clearing a level you've already beaten doesn't add an exit — the game only counts each one once. Test on a level you haven't cleared on that save file.

**Number is stuck.** Check the Script Log (Tools → Scripts → Script Log) — it reports what the script is doing every time that changes: whether it's connected, which game mode it sees, whether it's armed, and every write it makes. `waiting to arm` for more than about ten seconds means the arming fallback is disabled or set too high.

**Wrong number briefly on hack swap.** Should be handled, but if it persists, the total in your text source may not have changed — the swap detection keys off that. Clearing and resetting the counter address in the script settings forces a reset.

**Using QUsb2Snes instead of SNI.** Set the URL to `ws://localhost:8080`. Note that SNI disables port 8080 by default, so if you're on SNI, stick with 23074.

## How it works

Every poll, the script reads two bytes over USB: a game mode byte and the exit counter. Several guards decide whether the reading is trustworthy:

- **Range check.** During a ROM load, WRAM reads as `0x55` filler. A mode outside the valid range means the memory isn't holding real game state.
- **Re-check after reading.** The two bytes are separate USB round trips. If the console left the game in between, the sample is torn and gets thrown away.
- **Drop confirmation.** A decrease has to persist across several reads before it's accepted, riding out garbage during a file load.
- **Swap detection.** The ROM's internal title identifies the loaded hack. On a swap, that hack's last known count is restored from a small `exit_cache.json` beside the script, then confirmed by a live read — so switching back and forth doesn't lose your place. If the title can't be read, a change in the text source's total is used as a fallback signal.
- **Instant updates.** SMW's end-of-level timer fires the moment a level ends, so the script switches to fast polling for a few seconds around each clear. The counter still decides: a level the hack doesn't count produces a burst of reads and no change on screen, never a number that has to be taken back.
- **Arming.** Readings are only trusted after a file is genuinely loaded — signalled by the overworld or file-load game modes, or by sustained normal play for hacks that have neither. This keeps the title screen demo and freshly swapped ROMs from being read as real values.

Console resets hold the last known value until a file is loaded again, rather than flashing zero on stream.

## Credits

Built on the usb2snes protocol via [SNI](https://github.com/alttpo/sni) by jsd1982.

## License

MIT — see [LICENSE](LICENSE).
