# DP-D80 / D80 family BLE protocol notes

Reverse-engineered from Luck Jingle v2.7.16 (`com.dingdang.newprint`), decompiled
with JADX. **Not yet verified against real hardware** — see [print_d80.py](print_d80.py).

Unlike the DP-L1S (see [PROTOCOL.md](PROTOCOL.md)), the D80 is handled by a
different branch of the LuckPrinter SDK class hierarchy:

```
BaseDevice
└── BaseNormalDevice
    └── BaseA4Device (A4/Letter paper printers, 200 or 300 dpi)
        ├── DP_D80          (this printer, 200dpi)
        │   ├── DP_D80H     (300dpi hardware variant)
        │   └── PCPS_D80    (rebrand; only differs in density range 0-3 vs 0-2)
        └── ~90 other A4-family models (DP_A4, DP_A80, DP_L80, MT80, TPA46, ...)
```

## BLE identity

Same GATT layout as the rest of the family: service `ff00`, write char `ff02`,
notify char `ff01`.

The app matches these BLE advertised-name prefixes to `DP_D80`/`DP_D80H`/`PCPS_D80`
(from the SDK's `PrinterEnum`):

| Name prefix | Maps to |
|---|---|
| `DP_D80_` / `DP-D80_` / `D80-` / `D80_` / `CASA-01_` | `DP_D80` (200dpi) |
| `DP_D80H_` | `DP_D80H` (300dpi) |
| `PCPS_D80_` / `PCPS-D80_` | `PCPS_D80` (200dpi, density 0-3) |

Check which one you have with `python3 print_d80.py info` — the "Model"
query (`10 FF 20 F0`) returns the printer's own model string.

## Confirmed-identical commands (same as DP-L1S)

These are defined in `BaseNormalDevice`/`BaseDevice`, not overridden by `DP_D80`,
so they should carry over directly:

| Command | Description |
|---|---|
| `10 FF F1 03` | Enable printer |
| 12× `00` | Wake up |
| `10 FF 10 00 [n]` | Set density (0-2, or 0-3 on `PCPS_D80`) |
| `1B 4A [n]` | Feed paper by n dots |
| `10 FF F1 45` | Stop print job → wait for `AA` or `OK` |
| `10 FF 20 F0` / `F1` / `F2` | Model / version / serial |
| `10 FF 50 F1` | Battery |
| `10 FF 40` | Status bitfield |

## What's different from the DP-L1S flow

1. **Paper type must be set before the bitmap.** `BaseA4Device`'s plain-print
   path calls `setPaperType(1, 16)` → `1F 80 01 10` right after wakeup, which
   the pocket-printer flow never does.
2. **`endLineDot` is fixed at 144** (200dpi) or **216** (300dpi/`DP_D80H`),
   regardless of paper width — set unconditionally in `BaseA4Device`'s
   constructor, overriding `BaseNormalDevice`'s width-based default.
3. **Print width depends on a roll-paper-width setting**, not a fixed constant.
   From `BaseA4Device.getPrintWidth()` (200dpi / 300dpi):

   | Roll width | 200dpi px | 300dpi px |
   |---|---|---|
   | 56mm | 432 | 648 |
   | 77mm | 591 | 887 |
   | 107mm | 832 | 1248 |
   | 210mm (A4, default) | 1616 | 2400 |
   | 216mm (Letter) | 1648 | 2496 |

4. **The app always sends the bitmap compressed** (`DP_D80` sets
   `compress=true, compressWay=1` in its constructor → `PrinterImageProcessor
   .getBitmapByteArrayCompress()` → native `Compress.codeLihu()` from
   `libcompress.so`). That native codec was **not** reverse-engineered — it's
   compiled ARM/x86 code, not something JADX can turn back into readable
   logic without a disassembler (Ghidra) and a lot more effort.

## The open bet: raw GS v 0 instead of LIHU compression

`print_d80.py` sides-steps the native compression entirely and sends a plain,
uncompressed ESC/POS `GS v 0` raster image — the exact same header/bit-packing
format `print.py` uses for the DP-L1S:

```
1D 76 30 [mode] [wL] [wH] [hL] [hH] + 1bpp MSB-first bitmap
```

This is the same bet the original DP-L1S project made and it paid off there
(that device is *also* configured with `compress=true` in the SDK, yet the
plain raster command works fine — the compression is a bandwidth optimization
the app chooses to use, not something the firmware exclusively accepts). Given
the D80 shares the same command family (`10 FF F1 03`, wakeup, `1B 4A`,
`10 FF F1 45`), there's a reasonable chance its firmware also still parses
standard `GS v 0`. **This is unconfirmed** — it needs a real print test.

If it doesn't work (nothing prints, garbage prints, or it times out), the
fallback options, roughly in order of effort:
- Capture a real print job's BLE traffic (Android "Bluetooth HCI snoop log"
  while printing from Luck Jingle) and compare the actual bytes sent against
  the LIHU-compressed structure below, to reverse it from real samples
  instead of from the compiled code.
- Disassemble `libcompress.so` (extract it from the APK's `lib/<abi>/`
  folder, open in Ghidra/IDA, find `codeLihu`) to recover the exact RLE/LZ
  scheme.

## Compressed format, if it turns out to be required

For reference, in case the raw path fails and someone picks up the native
reversing work — the compressed frame layout (`getBitmapByteArrayCompress`,
`isNewCommand()` is `false` for this device so the 2-byte prefix is `1F 10`):

```
1F 10 [wBytesHi] [wBytesLo] [heightHi] [heightLo] [len0] [len1] [len2] [len3] + codeLihu(bitmap)
```

Note the width/height bytes here are **big-endian**, unlike the little-endian
order in the uncompressed `GS v 0` header — easy to trip over if adapting code
between the two paths. `len0..len3` is the compressed payload length as a
4-byte big-endian int (`PrinterUtil.intToByteArray4`).
