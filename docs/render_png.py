#!/usr/bin/env python3
"""Rasterise message-flow.html to message-flow.png.

The PNG is what the README embeds - GitHub sanitises inline SVG, so a remote
web font never loads there and the Chinese labels would fall back to whatever
the viewer has. A PNG bakes the type in.

Uses the Chrome that is already installed rather than pulling a second browser
down through Playwright. The SVG is wrapped in a page sized to exactly its
viewBox, which makes a viewport screenshot identical to an element screenshot -
no cropping guesswork - and --force-device-scale-factor doubles the pixels.

    ./render_png.py            # 2x, the README size
    ./render_png.py 3          # 3x, for print

Chrome refuses to load a local file over file:// in some configurations, so the
page is served over loopback for the duration and the server is shut down after.
"""

import functools
import http.server
import re
import socketserver
import struct
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "message-flow.html"
OUT = HERE / "message-flow.png"
RASTER = HERE / "_raster.html"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def build_raster_page():
    """Write a wrapper page holding just the SVG at its exact pixel size."""
    html = SRC.read_text(encoding="utf-8")

    svg = re.search(r"<svg\b.*?</svg>", html, re.S)
    if not svg:
        sys.exit(f"no <svg> found in {SRC.name}")
    markup = svg.group(0)

    box = re.search(r'viewBox="0 0 (\d+) (\d+)"', markup)
    if not box:
        sys.exit("the <svg> has no viewBox to size the page from")
    width, height = int(box.group(1)), int(box.group(2))

    fonts = re.search(r'<link href="(https://fonts\.googleapis\.com[^"]+)"', html)
    if not fonts:
        sys.exit("no Google Fonts link to carry over - the type would fall back")

    sized = re.sub(r"<svg\b", f'<svg width="{width}" height="{height}"', markup, count=1)
    RASTER.write_text(
        '<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">'
        f'<link href="{fonts.group(1)}" rel="stylesheet">'
        "<style>html,body{margin:0;padding:0;background:#f5f5f5;}svg{display:block;}</style>"
        "</head><body>" + sized + "</body></html>",
        encoding="utf-8",
    )
    return width, height


def main():
    scale = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    if not CHROME.exists():
        sys.exit(f"Chrome not found at {CHROME}")

    width, height = build_raster_page()
    print(f"viewBox {width}x{height} -> {width * scale}x{height * scale} px at {scale}x")

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        """Same server, without an access log line per request - Chrome also
        asks for /favicon.ico, and a 404 for it is not news."""

        def log_message(self, *_args):
            pass

    handler = functools.partial(QuietHandler, directory=str(HERE))
    # Port 0 lets the OS pick a free one, so a rerun cannot collide with itself.
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        OUT.unlink(missing_ok=True)
        result = subprocess.run(
            [
                str(CHROME),
                "--headless",
                "--disable-gpu",
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
                f"--force-device-scale-factor={scale}",
                f"--window-size={width},{height}",
                # Lets the web fonts finish before the frame is captured; without
                # it the CJK glyphs can rasterise with fallback metrics.
                "--virtual-time-budget=10000",
                f"--screenshot={OUT}",
                f"http://127.0.0.1:{port}/_raster.html",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        server.shutdown()

    RASTER.unlink(missing_ok=True)

    if not OUT.exists():
        print(result.stderr[-800:], file=sys.stderr)
        sys.exit("Chrome produced no PNG")

    data = OUT.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit("output is not a PNG")
    got_w, got_h = struct.unpack(">II", data[16:24])
    print(f"wrote {OUT.name}: {got_w}x{got_h} px, {len(data) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
