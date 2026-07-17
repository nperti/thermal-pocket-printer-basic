# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Reverse-engineered driver for the DP-L1S thermal pocket printer (sold as "Crafts & Co 3128" and other rebrands), letting users print over BLE without the official "Luck Jingle" Android app. The protocol was extracted by decompiling the app's APK with JADX and reading the LuckPrinter SDK's `PrinterImageProcessor` and `BaseNormalDevice` classes, then verified against real hardware.

Two independent, self-contained implementations of the same protocol live side by side:

- **`print.py`** — Python CLI (bleak + Pillow) for automation/batch jobs.
- **`index.html`** — single-file web app (vanilla JS, Web Bluetooth API) hosted via GitHub Pages, no build step.

There is no shared code between them; the BLE command sequence and bitmap encoding are duplicated in each. When changing protocol behavior (e.g. a new command, a timing fix, a bitmap encoding tweak), **update both places** and keep `PROTOCOL.md` in sync as the third source of truth.

There is a second, separate device family: **`print_d80.py`** targets the DP-D80/D80H/PCPS_D80 (Letter/A4 printers), documented in [PROTOCOL_D80.md](PROTOCOL_D80.md). It is a different branch of the LuckPrinter SDK (`BaseA4Device`, not `BaseNormalDevice`) with a different print-width scheme and an extra "set paper type" command. The app compresses D80 images with a proprietary native codec (`Compress.codeLihu`) that was not reverse-engineered; `print_d80.py` instead sends a plain uncompressed `GS v 0` raster like the DP-L1S does — confirmed working against a real D80 (200dpi, model `DYD80`). Not yet exercised: label/tag modes, the 300dpi `DP_D80H` variant, and `PCPS_D80` rebrands. Note also that BLE chunk size must be clamped to the connection's negotiated ATT MTU (`client.mtu_size - 3`) rather than assuming a fixed 512 bytes — the WinRT backend on Windows throws otherwise.

## Commands

No build/lint/test tooling exists in this repo (no package.json, no test suite, no linter config).

```bash
pip install bleak Pillow                      # deps for the CLI

python3 print.py scan                          # discover nearby BLE printers
python3 print.py info                          # battery, firmware, model, status
python3 print.py test                          # print built-in test pattern
python3 print.py image photo.png --dither       # print an image (Floyd-Steinberg dithering)
python3 print.py text "Hello World" [--label]   # print text, optionally on label paper
```

The web app has no build step — open `index.html` directly (or serve statically) in Chrome/Edge/Opera. Web Bluetooth does not work in Firefox/Safari, and Windows has limited support, so testing changes generally requires macOS/Linux/ChromeOS with a paired printer nearby.

There is no automated test suite; validating protocol changes requires physical hardware. When making BLE/protocol changes, note in your summary that they are untested against hardware unless you (or the user) actually ran them against the printer.

## Architecture: the protocol, not the code

The interesting complexity here is the BLE/ESC-POS protocol, documented fully in [PROTOCOL.md](PROTOCOL.md). Both `print.py` and `index.html` follow the same sequence, just in different languages:

1. Connect to BLE service `ff00`; write commands to characteristic `ff02`; listen for responses on `ff01`. The printer broadcasts as `C&Co 3128_BLE` and does **not** advertise service UUIDs, so it must be found by name prefix, not service filter.
2. `10 FF F1 03` — enable printer (Lujiang-specific "enable mode 3").
3. 12 null bytes — wake up.
4. Optional: `10 FF 10 00 [0|1|2]` — set density (light/normal/dark).
5. `1D 76 30 [m] [wL wH] [hL hH]` + raw bitmap bytes — ESC/POS `GS v 0` raster image, 384px wide (48 bytes/row), 1 bit/pixel, MSB-first, dark pixel = `1`.
6. Feed/position:
   - Normal paper: `1B 4A [n]` (feed n dots).
   - Label/sticker paper: wrap the image between `1F 11 51` (start) / `1D 0C` (advance to next label) / `1F 11 50` (end) instead of a plain feed, to use the gap sensor.
7. `10 FF F1 45` — stop job, wait for `AA`/`OK` response.

Key implementation details that both clients must respect:
- **Chunking differs by transport**: the web app sends 100-byte chunks with 50ms delays (Web Bluetooth MTU limits); the Python CLI uses 512-byte chunks with 10ms delays since native BLE has a larger effective MTU. Don't copy one client's chunk size into the other.
- **Dithering**: both `print.py` (`floyd_steinberg_dither`) and `index.html` (`updatePreview`/`canvasToBitmap`) implement Floyd-Steinberg dithering independently in their own language — same algorithm, kept in sync manually.
- **Image → bitmap**: grayscale → resize to print width (384px, preserving aspect ratio) → optional invert → optional dither → threshold at 128 → pack 8 pixels/byte MSB-first. This logic exists once per client (`image_to_bitmap`/`prepare_image` in Python; `canvasToBitmap`/`updatePreview` in JS).

The printer is one member of the LuckPrinter SDK's `BaseNormalDevice` family (159+ models across `DP_*`, `LuckP_*`, `MiniPocketPrinter`, etc. — see the class hierarchy in PROTOCOL.md). Protocol changes should stay generic to `BaseNormalDevice` behavior where possible, since other rebranded printers in the family may reuse this code.
