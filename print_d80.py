#!/usr/bin/env python3
"""
print_d80: Print to the DP-D80 / D80 family thermal printer (Letter/A4,
LuckPrinter SDK) directly from your computer via BLE, no app required.

EXPERIMENTAL / UNVERIFIED AGAINST HARDWARE. This was built by decompiling
Luck Jingle v2.7.16 (com.dingdang.newprint) with JADX and reading the
com.luckprinter.sdk_new.device.normal.a4.DP_D80 / BaseA4Device classes.
The control commands (enable, wakeup, paper type, feed, stop) are the same
DLE/GS/ESC commands documented in PROTOCOL.md for the DP-L1S and are very
likely correct. The bitmap format is NOT verified: the app always sends a
proprietary-compressed image for this device (native codeLihu() codec),
which was not reverse engineered here. This script instead sends a plain
uncompressed ESC/POS "GS v 0" raster image -- the same approach that works
on the DP-L1S despite it also nominally being configured for compression.
It's a reasonable bet since GS v 0 is a standard command family the
firmware likely still parses, but it has not been confirmed on real D80
hardware. Start with `info` and a small `test` print before anything big.

Usage:
  python3 print_d80.py scan                     Scan for nearby BLE printers
  python3 print_d80.py info                      Show printer info (battery, firmware, etc.)
  python3 print_d80.py test                      Print a small test pattern
  python3 print_d80.py image photo.png           Print an image file
  python3 print_d80.py image logo.png --dither   Print with Floyd-Steinberg dithering
  python3 print_d80.py text "Hello World"        Print text

Options:
  --address XX:XX:XX:XX:XX:XX   Skip scanning, connect directly
  --density 0|1|2                Print darkness (0=light, 1=normal, 2=dark)
  --dither                       Use Floyd-Steinberg dithering (better for photos)
  --invert                       Invert image (swap black/white)
  --paper 56|77|107|a4|letter    Roll paper width preset (default: a4)
  --width N                      Print width in pixels (overrides --paper)
  --copies N                     Number of copies (default: 1)

Requirements:
  pip install bleak Pillow

Based on reverse-engineered LuckPrinter SDK (com.luckprinter.sdk_new),
device class DP_D80 / BaseA4Device. See PROTOCOL.md for the DP-L1S
protocol this is adapted from -- the low-level commands are identical.
"""

import argparse
import asyncio
import sys

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Missing dependency: bleak")
    print("Install with: pip install bleak")
    sys.exit(1)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Missing dependency: Pillow")
    print("Install with: pip install Pillow")
    sys.exit(1)


# ── BLE constants ────────────────────────────────────────────────────
# Same GATT service/characteristics as the rest of the LuckPrinter family.

SERVICE_UUID     = "0000ff00-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID  = "0000ff02-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"

# Print width (pixels) at 200dpi per roll-paper-width preset, taken
# straight from BaseA4Device.getPrintWidth(). The D80 (non-H) is 200dpi;
# the "H" hardware variant (DP_D80H) is 300dpi and uses different values
# -- check with `info` (model string) if unsure which you have.
PAPER_WIDTH_PX_200DPI = {
    "56": 432,
    "77": 591,
    "107": 832,
    "a4": 1616,       # 210mm, default
    "letter": 1648,   # 216mm / 8.5in
}
DEFAULT_PAPER = "a4"
DEFAULT_WIDTH = PAPER_WIDTH_PX_200DPI[DEFAULT_PAPER]

# BaseA4Device sets endLineDot = 144 (200dpi) or 216 (300dpi H variant),
# independent of paper width.
DEFAULT_FEED = 144

CHUNK_SIZE  = 512
CHUNK_DELAY = 0.01

# BLE name prefixes the LuckJingle app matches to DP_D80/DP_D80H/PCPS_D80,
# pulled from PrinterEnum in the decompiled SDK.
SCAN_KEYWORDS = ["D80", "DP_D80", "DP-D80", "CASA-01", "PCPS_D80", "PCPS-D80"]

# BaseA4Device.lambda$print$17: setPaperType(1, 16) before sending the
# bitmap for a plain (non-label, non-tattoo) print job. 0x1F 0x80 0x01 0x10.
CMD_SET_PAPER_TYPE_NORMAL = bytes([0x1F, 0x80, 0x01, 0x10])


# ── Notification handling ────────────────────────────────────────────

received_data = []

def notification_handler(sender, data):
    received_data.append(data)


# ── Image processing ─────────────────────────────────────────────────
# Identical to print.py -- same dithering/threshold/bit-packing logic,
# the image pipeline doesn't depend on which printer model it targets.

def floyd_steinberg_dither(img):
    img = img.convert('L')
    width, height = img.size
    pixels = [float(img.getpixel((x, y))) for y in range(height) for x in range(width)]

    for y in range(height):
        for x in range(width):
            idx = y * width + x
            old_val = pixels[idx]
            new_val = 255.0 if old_val >= 128 else 0.0
            pixels[idx] = new_val
            error = old_val - new_val

            if x + 1 < width:
                pixels[idx + 1] += error * 7 / 16
            if y + 1 < height:
                if x > 0:
                    pixels[(y + 1) * width + (x - 1)] += error * 3 / 16
                pixels[(y + 1) * width + x] += error * 5 / 16
                if x + 1 < width:
                    pixels[(y + 1) * width + (x + 1)] += error * 1 / 16

    result = Image.new('L', (width, height))
    for y in range(height):
        for x in range(width):
            val = max(0, min(255, int(pixels[y * width + x])))
            result.putpixel((x, y), val)

    return result


def prepare_image(source, width_px=DEFAULT_WIDTH, dither=False, invert=False):
    if isinstance(source, str):
        img = Image.open(source)
    else:
        img = source

    img = img.convert('L')

    if img.width != width_px:
        ratio = width_px / img.width
        new_height = int(img.height * ratio)
        img = img.resize((width_px, new_height), Image.LANCZOS)

    if invert:
        from PIL import ImageOps
        img = ImageOps.invert(img)

    if dither:
        img = floyd_steinberg_dither(img)

    return img


def image_to_bitmap(img, width_px=DEFAULT_WIDTH):
    width_bytes = (width_px + 7) // 8
    height_px = img.height
    pixels = list(img.getdata())

    bitmap = bytearray(width_bytes * height_px)

    for y in range(height_px):
        for xb in range(width_bytes):
            byte_val = 0
            for bit in range(8):
                x = xb * 8 + bit
                if x < width_px:
                    if pixels[y * width_px + x] < 128:
                        byte_val |= (128 >> bit)
            bitmap[y * width_bytes + xb] = byte_val

    return bytes(bitmap), width_bytes, height_px


def create_test_image(width_px=DEFAULT_WIDTH):
    """Small test strip -- deliberately short so a failed first attempt
    doesn't waste a lot of paper."""
    height = 260
    img = Image.new('L', (width_px, height), 255)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, width_px - 1, height - 1], outline=0, width=2)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 42)
        font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except Exception:
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 42)
            font_sm = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 20)
        except Exception:
            font = ImageFont.load_default()
            font_sm = font

    draw.text((width_px // 2, 25), "D80 TEST PRINT", anchor="mt", fill=0, font=font)
    draw.text((width_px // 2, 80), f"{width_px}px wide", anchor="mt", fill=0, font=font)

    for x in range(20, width_px - 20):
        grey = int((x - 20) / (width_px - 40) * 255)
        draw.line([(x, 140), (x, 180)], fill=grey)
    draw.text((width_px // 2, 185), "gradient (use --dither for smooth)", anchor="mt", fill=0, font=font_sm)

    draw.line([(10, 10), (width_px - 10, 10)], fill=0, width=1)
    draw.line([(10, height - 10), (width_px - 10, height - 10)], fill=0, width=1)
    draw.line([(10, 10), (10, height - 10)], fill=0, width=1)
    draw.line([(width_px - 10, 10), (width_px - 10, height - 10)], fill=0, width=1)

    return img


def create_text_image(text, width_px=DEFAULT_WIDTH, font_size=64):
    fonts_to_try = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]

    font = None
    for font_path in fonts_to_try:
        try:
            font = ImageFont.truetype(font_path, font_size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    dummy = Image.new('L', (1, 1))
    draw = ImageDraw.Draw(dummy)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    padding = 30
    img_height = text_h + padding * 2

    img = Image.new('L', (width_px, img_height), 255)
    draw = ImageDraw.Draw(img)

    x = (width_px - text_w) // 2
    y = padding
    draw.text((x, y), text, fill=0, font=font)

    return img


# ── Protocol commands ─────────────────────────────────────────────────

def build_gs_v_0(bitmap_data, width_bytes, height_px, mode=0):
    """Standard ESC/POS GS v 0 raster image command, uncompressed."""
    header = bytes([
        0x1D, 0x76, 0x30,
        mode & 0x03,
        width_bytes % 256,
        width_bytes // 256,
        height_px % 256,
        height_px // 256,
    ])
    return header + bitmap_data


# ── BLE communication ─────────────────────────────────────────────────

async def ble_send_chunked(client, data, label="data"):
    total = len(data)
    sent = 0
    while sent < total:
        chunk = data[sent:sent + CHUNK_SIZE]
        await client.write_gatt_char(WRITE_CHAR_UUID, chunk, response=False)
        sent += len(chunk)
        await asyncio.sleep(CHUNK_DELAY)
    print(f"  Sent {label}: {total:,} bytes ({(total + CHUNK_SIZE - 1) // CHUNK_SIZE} chunks)")


async def ble_command(client, cmd_bytes, label="cmd", wait=0.3):
    await client.write_gatt_char(WRITE_CHAR_UUID, cmd_bytes, response=False)
    await asyncio.sleep(wait)


async def find_printer(address=None, timeout=8.0):
    if address:
        print(f"  Using address: {address}")
        return address

    print("  Scanning for printers...")
    devices = await BleakScanner.discover(timeout=timeout)

    for d in devices:
        name = (d.name or "").upper()
        if any(kw.upper() in name for kw in SCAN_KEYWORDS):
            print(f"  Found: {d.name} ({d.address})")
            return d.address

    print("  Printer not found. Nearby BLE devices:")
    for d in sorted(devices, key=lambda x: x.rssi or -999, reverse=True):
        if d.name:
            print(f"    {d.name:30s}  {d.address}  RSSI={d.rssi}")
    print("\n  Use --address XX:XX:XX:XX:XX:XX to connect manually.")
    return None


# ── Commands ──────────────────────────────────────────────────────────

async def cmd_scan(args):
    print("Scanning for BLE devices...\n")
    devices = await BleakScanner.discover(timeout=10.0)

    printers = []
    others = []
    for d in sorted(devices, key=lambda x: x.rssi or -999, reverse=True):
        if not d.name:
            continue
        name_upper = d.name.upper()
        is_printer = any(kw.upper() in name_upper for kw in SCAN_KEYWORDS)
        (printers if is_printer else others).append(d)

    if printers:
        print("Printers found:")
        for d in printers:
            print(f"  * {d.name:30s}  {d.address}  RSSI={d.rssi}")
    else:
        print("No known printers found.")

    if others:
        print(f"\nOther BLE devices ({len(others)}):")
        for d in others[:15]:
            print(f"    {d.name:30s}  {d.address}  RSSI={d.rssi}")


async def cmd_info(args):
    address = await find_printer(args.address)
    if not address:
        return

    async with BleakClient(address, timeout=15.0) as client:
        await client.start_notify(NOTIFY_CHAR_UUID, notification_handler)
        await asyncio.sleep(0.3)

        queries = [
            ("Model",    bytes([0x10, 0xFF, 0x20, 0xF0])),
            ("Version",  bytes([0x10, 0xFF, 0x20, 0xF1])),
            ("Serial",   bytes([0x10, 0xFF, 0x20, 0xF2])),
            ("Battery",  bytes([0x10, 0xFF, 0x50, 0xF1])),
            ("Density",  bytes([0x10, 0xFF, 0x11])),
            ("Status",   bytes([0x10, 0xFF, 0x40])),
        ]

        print("\nPrinter Information:")
        print("-" * 40)
        print("  (Model/Version help confirm whether this is a plain D80,")
        print("   the 300dpi D80H, or a PCPS_D80 rebrand -- protocol details")
        print("   differ slightly between them, see the module docstring.)\n")

        for label, cmd in queries:
            received_data.clear()
            await ble_command(client, cmd, wait=0.5)
            if received_data:
                raw = received_data[-1]
                try:
                    text = raw.decode('ascii', errors='ignore').strip('\x00')
                    if text and text.isprintable():
                        print(f"  {label:12s}: {text}")
                    elif label == "Battery" and len(raw) >= 2:
                        print(f"  {label:12s}: {raw[1]}%")
                    elif label == "Status" and len(raw) >= 1:
                        status = raw[0]
                        flags = []
                        if status & 0x01: flags.append("printing")
                        if status & 0x02: flags.append("cover open")
                        if status & 0x04: flags.append("no paper")
                        if status & 0x08: flags.append("low battery")
                        if status & 0x50: flags.append("overheating")
                        if status & 0x20: flags.append("charging")
                        print(f"  {label:12s}: {', '.join(flags) if flags else 'ready'}")
                    else:
                        print(f"  {label:12s}: {raw.hex()}")
                except Exception:
                    print(f"  {label:12s}: {raw.hex()}")
            else:
                print(f"  {label:12s}: no response")

        await client.stop_notify(NOTIFY_CHAR_UUID)


async def cmd_print(args, img):
    address = await find_printer(args.address)
    if not address:
        return

    width_px = args.width
    bitmap_data, width_bytes, height_px = image_to_bitmap(img, width_px)
    gs_command = build_gs_v_0(bitmap_data, width_bytes, height_px)

    print(f"  Image: {width_bytes * 8}x{height_px} px")
    print(f"  Data:  {len(gs_command):,} bytes")

    copies = getattr(args, 'copies', 1) or 1

    async with BleakClient(address, timeout=15.0) as client:
        await client.start_notify(NOTIFY_CHAR_UUID, notification_handler)
        await asyncio.sleep(0.3)

        if args.density is not None:
            density = max(0, min(3, args.density))
            print(f"  Setting density: {density}")
            await ble_command(client, bytes([0x10, 0xFF, 0x10, 0x00, density]))

        for copy in range(copies):
            if copies > 1:
                print(f"\n  Copy {copy + 1}/{copies}")

            # Enable printer
            await ble_command(client, bytes([0x10, 0xFF, 0xF1, 0x03]))

            # Wake up
            await ble_command(client, bytes(12))

            # A4 device: set paper type before the bitmap (BaseA4Device
            # print flow does this; the pocket printers don't).
            await ble_command(client, CMD_SET_PAPER_TYPE_NORMAL)

            # Send bitmap
            print("  Printing...")
            await ble_send_chunked(client, gs_command, "bitmap")
            await asyncio.sleep(0.5)

            # Feed paper (endLineDot)
            await ble_command(client, bytes([0x1B, 0x4A, DEFAULT_FEED]))

            # Stop print job
            await ble_command(client, bytes([0x10, 0xFF, 0xF1, 0x45]), wait=2.0)

        await client.stop_notify(NOTIFY_CHAR_UUID)

    print("  Done!")


async def cmd_test(args):
    print("Printing test pattern...")
    img = create_test_image(args.width)
    if args.dither:
        img = floyd_steinberg_dither(img)
    await cmd_print(args, img)


async def cmd_image(args):
    print(f"Printing: {args.file}")
    img = prepare_image(args.file, args.width, args.dither, args.invert)
    await cmd_print(args, img)


async def cmd_text(args):
    print(f"Printing text: {args.text!r}")
    font_size = getattr(args, 'font_size', 64) or 64
    img = create_text_image(args.text, args.width, font_size)
    await cmd_print(args, img)


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Print to DP-D80 / D80-family thermal printer via BLE (experimental)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument('--address', '-a', help='Printer BLE address (skip scanning)')
    parser.add_argument('--paper', choices=list(PAPER_WIDTH_PX_200DPI.keys()), default=DEFAULT_PAPER,
                        help=f'Roll paper width preset (default: {DEFAULT_PAPER})')
    parser.add_argument('--width', '-w', type=int, default=None,
                        help='Print width in pixels (overrides --paper)')
    parser.add_argument('--density', '-d', type=int, choices=[0, 1, 2, 3],
                        default=None, help='Print density (0=light .. 2/3=dark, depends on model)')
    parser.add_argument('--dither', action='store_true',
                        help='Use Floyd-Steinberg dithering')
    parser.add_argument('--invert', action='store_true',
                        help='Invert colours')
    parser.add_argument('--copies', '-c', type=int, default=1,
                        help='Number of copies (default: 1)')

    subparsers = parser.add_subparsers(dest='command', help='Command')

    subparsers.add_parser('scan', help='Scan for BLE printers')
    subparsers.add_parser('info', help='Show printer info')
    subparsers.add_parser('test', help='Print test pattern')

    img_parser = subparsers.add_parser('image', help='Print an image')
    img_parser.add_argument('file', help='Image file to print (PNG, JPG, etc.)')

    txt_parser = subparsers.add_parser('text', help='Print text')
    txt_parser.add_argument('text', help='Text to print')
    txt_parser.add_argument('--font-size', type=int, default=64,
                            help='Font size in pixels (default: 64)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.width is None:
        args.width = PAPER_WIDTH_PX_200DPI[args.paper]

    commands = {
        'scan': cmd_scan,
        'info': cmd_info,
        'test': cmd_test,
        'image': cmd_image,
        'text': cmd_text,
    }

    asyncio.run(commands[args.command](args))


if __name__ == '__main__':
    main()
