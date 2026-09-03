#!/usr/bin/env python3
"""Regenerate bot_server.py from bot_dual.py by removing the UI.

There are three files and they are not three codebases:

    bot.py         the original monitor, left alone
    bot_dual.py    the monitor plus a headless --server mode  <- edit this one
    bot_server.py  generated: bot_dual.py with the UI removed

Run this after changing bot_dual.py. Only what must differ differs - the
dependency list, the imports, the module docstring and main() - so every shared
function stays byte-identical, and test_rules.py compares them with
inspect.getsource() so a hand-edit to bot_server.py fails the suite.

    ./make_bot_server.py && ./test_rules.py
"""

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DUAL = HERE / "bot_dual.py"
OUT = HERE / "bot_server.py"

lines = DUAL.read_text(encoding="utf-8").splitlines(keepends=True)


def _bounds(name: str, kind: str) -> tuple[int, int]:
    """Line range of a top-level def/class, including the comments above it."""
    prefix = f"{kind} {name}"
    start = next(i for i, line in enumerate(lines) if line.startswith(prefix))
    while start > 0 and lines[start - 1].startswith("#"):
        start -= 1
    end = start + 1
    while end < len(lines):
        line = lines[end]
        # A definition ends at the next thing starting in column 0 that is not a
        # continuation bracket.
        if line.strip() and not line[0].isspace() and not line.startswith((")", "]", "}")):
            break
        end += 1
    return start, end


def drop(name: str, kind: str = "def") -> str:
    global lines
    start, end = _bounds(name, kind)
    head = lines[start].rstrip()
    while end < len(lines) and lines[end].strip() == "":
        end += 1
    lines = lines[:start] + lines[end:]
    return head


print("removing:")
print("  " + drop("MeshtasticTUI", "class"))
for name in ("display_width", "format_uptime", "set_wifi"):
    print("  " + drop(name))

source = "".join(lines)


def sub(old: str, new: str, count: int = 1) -> None:
    global source
    found = source.count(old)
    assert found == count, f"expected {count} of {old[:70]!r}, found {found}"
    source = source.replace(old, new, count)


# STATUS_PANE_WIDTH existed only for display_width.
sub(
    """# Content width of the local-status pane. CJK glyphs occupy two terminal cells,
# so a line that looks short in source can still wrap and strand a word on its
# own row - measure with display_width() before writing a variable-length line.
STATUS_PANE_WIDTH = 24


""",
    "",
)

# No widgets, no width measuring, no LoRa tables: the status pane was the only
# thing that needed any of them.
sub(
    """from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Label, ListItem, ListView, RichLog

""",
    "",
)
sub("\nimport lora_params\n", "")
sub("import unicodedata\n", "")

# Nobody should have to install a UI toolkit to run a headless server.
sub('#     "textual",\n', "")
required = re.search(r"_REQUIRED_MODULES = \[.*?\]\n", source, re.S).group(0)
assert "textual" in required, "expected textual in _REQUIRED_MODULES"
source = source.replace(
    required,
    "\n".join(line for line in required.splitlines() if "textual" not in line) + "\n",
    1,
)

sub(
    '''# bot_dual.py has a UI to suppress and so passes --server; bot_server.py is
# nothing but the server and has no such flag. Keeping it in one place is what
# lets detached_argv() be shared between them unchanged.
HEADLESS_FLAGS = ["--server"]''',
    '''# Nothing to add: this file is only ever the server, so the background copy
# needs no flag to suppress a UI. See bot_dual.py, which passes --server here.
HEADLESS_FLAGS: list[str] = []''',
)

# The module docstring describes a three-pane TUI that is no longer here.
first = source.index('"""')
second = source.index('"""', first + 3) + 3
source = (
    source[:first]
    + '''"""Meshtastic auto-reply server - headless, no UI.

Reads rules.txt and answers matching messages on the mesh, logging one line per
event to stdout. This is bot.py's rules engine with the three-pane monitor taken
out: same rules.txt, same exact-match semantics, same [DM] / [channel] / [*]
section precedence, so a rule tested in the UI behaves identically here.

GENERATED from bot_dual.py by make_bot_server.py - do not edit by hand. Change
bot_dual.py and regenerate; test_rules.py compares the shared functions of the
two files and fails if they differ.

    ./bot_server.py --port /dev/cu.usbmodem2101
    ./bot_server.py --host 192.168.0.247 --daemon --log ~/bot.log
    ./bot_server.py --ble Meshtastic_1a2b --heartbeat 0

With no target it lists what it can see and reads a number, then serves. With
--daemon it relaunches itself detached once the device has been chosen, so the
picking still happens on your terminal and nothing else does. Stop it with
SIGTERM (plain kill, or Ctrl-C in the foreground).
"""'''
    + source[second:]
)


def drop_arg(flag: str) -> None:
    """Remove one parser.add_argument block by its flag."""
    global source
    needle = f'    parser.add_argument(\n        "{flag}",\n'
    start = source.index(needle)
    end = source.index("\n    )\n", start) + len("\n    )\n")
    source = source[:start] + source[end:]


# --server is implied here, and --wifi is a job for bot.py.
drop_arg("--server")
drop_arg("--wifi")

sub(
    '''        description="Meshtastic monitor TUI. Connects over BLE by default; "
        "pass --host to talk to a node over WiFi/TCP instead.",''',
    '''        description="Headless Meshtastic auto-reply server: answers messages "
        "from rules.txt with no UI, one log line per event. Give it one of "
        "--port / --host / --ble to start straight away, or none to pick from "
        "a list. Stop it with SIGTERM.",''',
)
sub(
    '''        help="with --server, relaunch in the background once the device is "
        "chosen, writing to --log. Prints the pid to stop it with. The device "
        "is picked first, so it never needs a terminal of its own.",''',
    '''        help="relaunch in the background once the device is chosen, writing "
        "to --log. Prints the pid to stop it with. The device is picked first, "
        "so the background copy never needs a terminal of its own.",''',
)
sub(
    '''        help="how often --server logs a still-alive line with its counters. "
        "0 turns it off, logging only what actually happens. "
        "(default: %(default)s)",''',
    '''        help="how often to log a still-alive line with the counters. 0 turns "
        "it off, logging only what actually happens. (default: %(default)s)",''',
)
sub(
    '''        help="also offer this BLE node by name, e.g. Meshtastic_1a2b. Connected "
        "to without waiting for a scan, which is what --server needs to start "
        "unattended.",''',
    '''        help="this BLE node, by name, e.g. Meshtastic_1a2b. Connected to "
        "without waiting for a scan, which is what --daemon needs to start "
        "unattended.",''',
)
sub(
    '''        help="also offer this node over TCP, e.g. Meshtastic.local or "
        f"192.168.0.247. Connected to immediately; BLE is still scanned. "
        f"Port defaults to {DEFAULT_TCP_PORT}.",''',
    '''        help="this node over TCP, e.g. Meshtastic.local or 192.168.0.247. "
        f"Port defaults to {DEFAULT_TCP_PORT}.",''',
)
sub(
    '''        help="also offer this node over USB serial, e.g. /dev/cu.usbmodem2101. "
        "The only transport that still works once the node's WiFi is off and its "
        "Bluetooth is disabled, which is the normal state on MUI/TFT boards.",''',
    '''        help="this node over USB serial, e.g. /dev/cu.usbmodem2101. The only "
        "transport that still works once the node's WiFi is off and its "
        "Bluetooth is disabled, which is the normal state on MUI/TFT boards.",''',
)
sub(
    '''        help="this station's position, used to show each node's distance. Only "
        "needed when the connected node has no GPS fix. Stays local - it is "
        "never sent to the device or the mesh.",''',
    '''        help="this station's position, used for the dist= field in replies. "
        "Only needed when the connected node has no GPS fix. Stays local - it "
        "is never sent to the device or the mesh.",''',
)

sub(
    '''    if args.wifi:
        if args.port:
            transport, address = TRANSPORT_SERIAL, args.port
        elif args.host:
            transport, address = TRANSPORT_TCP, args.host
        else:
            parser.error("--wifi needs a target: --port /dev/... (or --host, which cannot switch WiFi back on)")
        sys.exit(set_wifi(transport, address, args.wifi == "on"))

    if args.daemon and not args.server:
        parser.error("--daemon 只能跟 --server 一起用")

    if args.server:
        target = resolve_server_target(args.host, args.port, args.ble)
        if target is None:
            sys.exit(1)
        transport, address = target
        if args.daemon:
            sys.exit(
                spawn_detached(
                    detached_argv(target, args.here, args.heartbeat), Path(args.log)
                )
            )
        sys.exit(ServerBot(here=args.here, heartbeat=args.heartbeat).run(transport, address))

    MeshtasticTUI(
        tcp_host=args.host,
        serial_port=args.port,
        here=args.here,
        ble_address=args.ble,
    ).run()''',
    '''    target = resolve_server_target(args.host, args.port, args.ble)
    if target is None:
        sys.exit(1)
    transport, address = target
    if args.daemon:
        sys.exit(
            spawn_detached(
                detached_argv(target, args.here, args.heartbeat), Path(args.log)
            )
        )
    sys.exit(ServerBot(here=args.here, heartbeat=args.heartbeat).run(transport, address))''',
)

OUT.write_text(source, encoding="utf-8")
OUT.chmod(0o755)
print(f"\nwrote {OUT.name} ({len(source.splitlines())} lines)")

# Anything left behind would be a NameError the moment that path is taken.
for banned in (
    "textual",
    "lora_params",
    "MeshtasticTUI",
    "set_wifi",
    "display_width",
    "STATUS_PANE_WIDTH",
    "unicodedata",
):
    offenders = [
        f"{n + 1}: {line.strip()[:60]}"
        for n, line in enumerate(source.splitlines())
        if banned in line and not line.strip().startswith(("#", '"', "'"))
    ]
    status = "clean" if not offenders else offenders
    print(f"  {banned:18} {status}")
