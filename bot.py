#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "meshtastic",
#     "pypubsub",
#     "textual",
# ]
# ///
"""Meshtastic monitor - three-pane interactive TUI (BLE or WiFi/TCP).

Layout (all visible at once, k9s/ranger style):
    +-----------+-----------+---------------------------+
    | Devices   | Channels  |  Messages for the         |
    | (BLE)     | & Nodes   |  selected channel/node    |
    +-----------+           |  + send box               |
    | This      |           |                           |
    | device's  |           |                           |
    | status    |           |                           |
    +-----------+-----------+---------------------------+

Flow:
  1. Left pane (top) - scans for Meshtastic BLE peripherals and, when --host
     was given, lists that TCP node alongside them, each row marked with its
     transport; pick one to connect. --host connects immediately rather than
     waiting out the ~10s BLE scan, which keeps running so a BLE device can
     still be picked afterwards.
     Left pane (bottom) - once connected, shows the connected
     device's own status: firmware version, role/preset/region/slot/
     frequency, uptime, last-heard signal quality, GPS fix.
  2. Middle pane - once connected, lists this node's channels AND all known
     mesh nodes. Pick a channel to broadcast on it, or a node to send it a
     direct message.
  3. Right pane - live message log for whichever target is selected (each
     target keeps its own scrollback), plus an input box to send text.
     Every channel and node DM is received, recorded, and browsable/sendable
     as normal - keyword auto-reply (see rules.txt) is the only thing scoped
     down, firing only where rules were actually written: a channel with a
     section of its own, or a direct message once [DM] exists.

Usage - the shebang hands the script to `uv run --script`, which resolves the
dependencies declared at the top of this file into a cached environment, so
there is no virtualenv to create or activate:
    ./bot.py                          # scan and connect over BLE
    ./bot.py --host Meshtastic.local  # connect over WiFi/TCP
    ./bot.py --host 192.168.0.247:4403

Without uv, invoke an interpreter that already has the dependencies - note
that `python3 bot.py` bypasses the shebang, so a bare system python3 will
fail the dependency check below:
    /path/to/venv/bin/python bot.py --host 192.168.0.247

Keyboard: Left/Right cycle devices -> channels/nodes -> messages. Up/Down keep
their normal per-widget meaning (move the list cursor in the device/target
lists, scroll the log) - use Tab/Shift+Tab to reach the status pane.

Auto-reply rules live in rules.txt next to this script, grouped by channel:

    [EDGE_ATS]          the channel these rules apply to - either its name, or
    ping=pong           its index as [#0] for the usually-unnamed primary.
    help=指令: ping      [*] applies to every channel.

    [#0]
    ping=pong

A channel's own rules are checked before [*], and the first keyword whose text
equals the whole message - case included, surrounding whitespace ignored -
wins. Reply text is taken literally, so do not quote it. Only messages from
other nodes are answered, once each. Rules written before the first [header] apply to
every channel, which keeps a flat pre-sections file working, but will also
auto-reply on public channels, so prefer an explicit header.

Blank lines and lines starting with # are ignored. The file is re-read on
every incoming message, so edits take effect immediately - no restart needed.
On connect, the bot logs which channels it will reply on and flags sections
that match no channel on this node.

The Meshtastic phone/desktop app must be disconnected from the device first,
on either transport: BLE allows one connected client at a time, and the
firmware's socket API accepts a single TCP client and force-closes the previous
one, so connecting here will kick an app already talking to that node.

TCP is the way in to a node whose Bluetooth is off - notably MUI/TFT boards,
where enabling BLE puts the device UI into programming mode.
"""

# Deferred annotation evaluation, so the `X | None` unions below stay strings
# at runtime instead of being evaluated. Without this the module fails to
# import on Python 3.9, which parses PEP 604 unions but cannot evaluate them.
from __future__ import annotations

import importlib
import sys

# Check dependencies up front so a missing package fails fast with a plain
# "pip install X" hint instead of a raw ImportError/traceback from somewhere
# deep inside textual/meshtastic. meshtastic.ble_interface is checked
# specifically (not just "meshtastic") since it pulls in bleak, which is a
# separate, occasionally-missing install.
_REQUIRED_MODULES = [
    ("textual", "textual"),
    ("pubsub", "pypubsub"),
    ("meshtastic.ble_interface", "meshtastic"),
]
_missing_packages = []
for _module_name, _pip_name in _REQUIRED_MODULES:
    try:
        importlib.import_module(_module_name)
    except ImportError:
        if _pip_name not in _missing_packages:
            _missing_packages.append(_pip_name)
if _missing_packages:
    print("缺少必要的 Python 套件。", file=sys.stderr)
    print("    ./bot.py            # 讓 shebang 交給 uv 自動備環境", file=sys.stderr)
    print(
        f"    pip install {' '.join(_missing_packages)}"
        "    # 或手動裝進當前的 interpreter",
        file=sys.stderr,
    )
    sys.exit(1)

import argparse
import atexit
import datetime
import math
import threading
import time
import unicodedata
from pathlib import Path

from pubsub import pub
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Label, ListItem, ListView, RichLog

import meshtastic
import meshtastic.ble_interface
import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic.protobuf import config_pb2

import lora_params

RULES_FILE = Path(__file__).parent / "rules.txt"

DEFAULT_RULES = """\
# Auto-reply rules, grouped by channel.
#
#   [channel]           the channel the rules below it apply to, given either
#                       as its Meshtastic name ([EDGE_ATS]) or its index
#                       ([#0] - useful for the unnamed primary channel).
#                       [*] applies to every channel.
#   [DM]                rules for direct messages. Tried before the channel the
#                       DM arrived on, so a keyword can be answered differently
#                       in private, or left out here and shared with the
#                       channel. A reply to a DM goes back as a DM.
#   keyword=reply text  one rule per line. The whole message must equal the
#                       keyword exactly, case included - only surrounding
#                       whitespace is ignored. So A=Alpha answers "A" but not
#                       "a" and not "AAA". The reply text is taken literally,
#                       so do not quote it.
#
# The first matching rule wins, and a channel's own rules are checked before
# [*]. Blank lines and lines starting with # are ignored.
#
# Messages from other nodes are answered, and so are ones typed on this device,
# so a keyword can be tried without a second radio. Anything starting with
# "BOT: " is never answered - that is what stops a reply drawing a reply. Each
# message is answered at most once, even if the mesh redelivers it.
#
# Rules placed before the first [channel] header apply to EVERY channel, which
# is how the old flat format keeps working - but that will auto-reply on public
# channels too, so prefer an explicit header.

[DM]
ping=pong (private)

[EDGE_ATS]
ping=pong
help=指令: ping
"""

BROADCAST_ADDR = "^all"

# rules.txt section that applies to every channel.
ALL_CHANNELS = "*"

# rules.txt section for direct messages. A channel actually named "DM"
# would share it, which is accepted as the price of a readable name.
DM_SECTION = "DM"

# Device-list items are keyed "<transport>:<address>", mirroring the "kind:key"
# scheme the targets list already uses, so one list can hold both transports
# and connect_device knows which interface class to build.
TRANSPORT_BLE = "ble"
TRANSPORT_TCP = "tcp"
TRANSPORT_SERIAL = "serial"

# Meshtastic's socket API port. Firmware only accepts one TCP client at a
# time and force-closes the previous one, so connecting here kicks off a
# phone/desktop app already talking to the same node over WiFi.
DEFAULT_TCP_PORT = 4403


def load_rules() -> dict[str, dict[str, str]]:
    """Re-read rules.txt every call so edits take effect without restarting the bot.

    Returns {section: {keyword: reply}}. A section is a channel name, "#<index>",
    or ALL_CHANNELS. Rules appearing before the first [header] land in
    ALL_CHANNELS, which is what keeps a flat pre-sections file working.
    """
    if not RULES_FILE.exists():
        RULES_FILE.write_text(DEFAULT_RULES, encoding="utf-8")

    rules: dict[str, dict[str, str]] = {}
    section = ALL_CHANNELS
    for raw_line in RULES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Checked before the "=" test so a header is never mistaken for a rule.
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip() or ALL_CHANNELS
            rules.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        keyword, _, reply = line.partition("=")
        keyword = keyword.strip()
        if keyword:
            rules.setdefault(section, {})[keyword] = reply.strip()
    return rules


def _split_device_key(device_key: str) -> tuple[str, str]:
    """Split a "<transport>:<address>" device-list key.

    A bare address (no prefix) is treated as BLE so an older saved key, or a
    caller that has not been updated, still connects the way it used to.
    Partitioning from the left keeps IPv6 literals and "host:port" intact.
    """
    transport, sep, address = device_key.partition(":")
    if not sep:
        return TRANSPORT_BLE, device_key
    if transport not in (TRANSPORT_BLE, TRANSPORT_TCP, TRANSPORT_SERIAL):
        return TRANSPORT_BLE, device_key
    return transport, address


# Content width of the local-status pane. CJK glyphs occupy two terminal cells,
# so a line that looks short in source can still wrap and strand a word on its
# own row - measure with display_width() before writing a variable-length line.
STATUS_PANE_WIDTH = 24


def display_width(text: str) -> int:
    """Terminal cells `text` occupies, counting East Asian wide/fullwidth as 2."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


# IUGG mean Earth radius. Node separations here are tens of kilometres at most,
# where a spherical model is well inside the error of a LoRa-reported position.
EARTH_RADIUS_M = 6371008.8


def node_position(node: dict) -> tuple[float, float] | None:
    """(latitude, longitude) in degrees from a nodeDB entry, or None.

    Meshtastic stores degrees as integers scaled by 1e7. None covers three cases
    that all mean "no usable fix": no position dict at all, a position dict
    carrying only a timestamp (what a node with GPS enabled but no fix reports),
    and an exact 0,0 - which is a placeholder in practice, not a real position in
    the Gulf of Guinea.
    """
    position = node.get("position") or {}
    lat_i, lon_i = position.get("latitudeI"), position.get("longitudeI")
    if lat_i is None or lon_i is None:
        return None
    if lat_i == 0 and lon_i == 0:
        return None
    return lat_i / 1e7, lon_i / 1e7


def open_interface(transport: str, address: str):
    """Build and connect the meshtastic interface for one transport.

    Shared by the TUI and the one-shot --wifi path so both resolve addresses and
    pick interface classes identically.
    """
    if transport == TRANSPORT_TCP:
        hostname, port = _split_host_port(address)
        return meshtastic.tcp_interface.TCPInterface(hostname=hostname, portNumber=port)
    if transport == TRANSPORT_SERIAL:
        return meshtastic.serial_interface.SerialInterface(devPath=address)
    return meshtastic.ble_interface.BLEInterface(address=address)


def set_wifi(transport: str, address: str, enable: bool) -> int:
    """Turn the node's WiFi on or off, then exit. Returns a process exit code.

    Deliberately not a TUI action: it reboots the device, and when switching off
    over TCP it severs the very link it travelled over.
    """
    if transport == TRANSPORT_TCP and not enable:
        print(
            "注意: 正在透過 WiFi 關閉 WiFi - 這條連線會斷,而且無法再用 WiFi 開回來。\n"
            "      要開回來需要 USB (--port) 或裝置螢幕上長按 WLAN 按鈕。",
            file=sys.stderr,
        )

    print(f"連線 {transport}:{address} ...")
    try:
        iface = open_interface(transport, address)
    except Exception as e:  # noqa: BLE001
        print(f"連線失敗: {e}", file=sys.stderr)
        return 1

    try:
        node = iface.localNode
        before = node.localConfig.network.wifi_enabled
        owner = (iface.getMyUser() or {}).get("longName")
        print(f"節點: {owner}  目前 wifi_enabled={before}")
        if before == enable:
            print(f"已經是 {'開' if enable else '關'},不做任何變更")
            return 0

        node.localConfig.network.wifi_enabled = enable
        node.writeConfig("network")
        print(f"已設定 wifi_enabled={enable},裝置會重新開機後生效")
        if not enable:
            print("提醒: 之後要開回來只能用 USB (--port) 或裝置螢幕長按 WLAN 按鈕")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"設定失敗: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            iface.close()
        except Exception:  # noqa: BLE001
            pass


def parse_latlon(text: str) -> tuple[float, float]:
    """Parse "lat,lon" for --here, raising argparse's error type on bad input."""
    parts = text.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected LAT,LON - got {text!r}")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected two numbers - got {text!r}") from None
    if not -90 <= lat <= 90:
        raise argparse.ArgumentTypeError(f"latitude out of range: {lat}")
    if not -180 <= lon <= 180:
        raise argparse.ArgumentTypeError(f"longitude out of range: {lon}")
    return lat, lon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon pairs, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def format_distance(metres: float | None) -> str:
    """Compact distance for the node list; "--" when it cannot be computed."""
    if metres is None:
        return "--"
    if metres < 1000:
        return f"{metres:.0f}m"
    return f"{metres / 1000:.1f}km"


def _split_host_port(address: str) -> tuple[str, int]:
    """Split "host:port" into its parts, defaulting to Meshtastic's TCP port.

    Only splits a trailing ":<digits>", and only when the remainder has no
    colon of its own, so a bare IPv6 literal passes through untouched.
    """
    host, sep, port = address.rpartition(":")
    if sep and port.isdigit() and ":" not in host and host:
        return host, int(port)
    return address, DEFAULT_TCP_PORT


def find_reply(text: str, sections: list[str]) -> str | None:
    """Reply for `text` if some rule's keyword matches it exactly.

    The whole message must equal the whole keyword, case included. Only
    surrounding whitespace is ignored.

    Both halves of that are deliberate. This used to be a case-insensitive
    substring test, which a short-keyword rules file makes unusable: with the
    NATO alphabet (A=Alpha ... Z=Zulu) almost any message contains some single
    letter, so "hello" answered "Echo" and "Bravo" answered "Alpha". Matching
    case as well means "a" no longer triggers the rule written as "A".

    The caller passes the channel's own sections before ALL_CHANNELS, so a
    channel-specific rule beats a catch-all one for the same keyword.
    """
    message = text.strip()
    if not message:
        return None
    rules = load_rules()
    for section in sections:
        for keyword, reply in rules.get(section, {}).items():
            if keyword.strip() == message:
                return reply
    return None


def format_elapsed(seconds: float) -> str:
    """Seconds as H:MM:SS, for how long this program has been running.

    Deliberately not format_uptime, which reports the *node's* uptime: that one
    stops at minutes and says "--" when the figure is missing, both right for a
    value polled every five seconds and sometimes absent. This one ticks every
    second and always has a value, so it shows seconds and starts at 0:00:00.

    Hours are not wrapped into days either: this runs for days at a time and
    "51:03:12" reads unambiguously, where "2d 3:03:12" needs a moment's
    arithmetic to compare against a log timestamp.
    """
    total = int(max(seconds, 0))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def parse_incoming(packet: dict, my_id: str | None) -> dict | None:
    """Return a dict of the fields we care about for a text-message packet, or None.

    target is a (kind, key) tuple: ("channel", index) for a broadcast, or
    ("node", other_party_id) for a direct message - key is always the *other*
    side of the conversation, whether this packet was incoming or our own
    outgoing echo.
    """
    decoded = packet.get("decoded")
    if not decoded or decoded.get("portnum") != "TEXT_MESSAGE_APP":
        return None

    rx_time = packet.get("rxTime")
    when = (
        datetime.datetime.fromtimestamp(rx_time).strftime("%H:%M:%S") if rx_time else "??:??:??"
    )

    to_id = packet.get("toId", BROADCAST_ADDR)
    from_id = packet.get("fromId", "?")
    if to_id == BROADCAST_ADDR:
        target = ("channel", packet.get("channel", 0))
    else:
        other = to_id if from_id == my_id else from_id
        target = ("node", other)

    return {
        "text": decoded.get("text", ""),
        # Kept for a DM too: a direct message still travels on a channel, and
        # its rules fall back to that channel's when [DM] has no match.
        "channel": packet.get("channel", 0),
        "from_id": from_id,
        "to_id": to_id,
        "target": target,
        "when": when,
        "transport": "MQTT" if packet.get("viaMqtt") else "LoRa",
        "snr": packet.get("rxSnr"),
        "rssi": packet.get("rxRssi"),
        "id": packet.get("id"),
    }


def node_label(nodes: dict, node_id: str) -> str:
    """Readable name for a node id: its short name, else long name, else the id.

    Names arrive as separate NodeInfo packets, so a node we have heard a message
    from may not have one yet - hence the fall back to the raw id rather than
    showing nothing.
    """
    user = (nodes.get(node_id) or {}).get("user") or {}
    return user.get("shortName") or user.get("longName") or node_id


def format_incoming_line(info: dict, sender: str | None = None) -> str:
    """Render a parsed incoming message as one RichLog line.

    Shaped "12:34:56 Bug2[!f2dcbabe](LoRa snr=6.5 rssi=-92): ping" - name, then
    its node id in brackets, then the transport and signal. The id is not
    repeated when no name resolved, since the name has already fallen back to it.

    The brackets are escaped: RichLog reads this as markup, where "[" opens a
    style tag, so an unescaped "[!f2dcbabe]" would be parsed as one instead of
    printed.
    """
    node_id = info["from_id"]
    if sender and sender != node_id:
        who = f"[bold]{sender}[/bold][dim]\\[{node_id}][/dim]"
    else:
        who = f"[bold]{node_id}[/bold]"
    line = f"[dim]{info['when']}[/dim] {who}({info['transport']}"
    if info["snr"] is not None:
        line += f" snr={info['snr']}"
    if info["rssi"] is not None:
        line += f" rssi={info['rssi']}"
    line += f"): {info['text']}"
    return line


def format_uptime(seconds: int | None) -> str:
    if not seconds:
        return "--"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}"
    return f"{hours:02d}:{minutes:02d}"


BOT_REPLY_PREFIX = "BOT: "


def build_reply_text(reply: str, info: dict) -> str:
    """Two lines: the rule's reply, then the message details in brackets.

        BOT: pong
        [12:34:56 from=Bug2 rx=LoRa snr=6.5 rssi=-92 dist=842m]

    Fields that cannot be determined are left out rather than sent as "--": the
    text payload is tight, and a field that is usually blank costs bytes on
    every reply while telling the recipient nothing.

    "from" carries the sender's name when one is known, matching the message
    pane, and falls back to the node id.

    The "BOT: " prefix and the bracketed detail line also keep the bot from
    answering itself: matching is exact, so a reply can never equal a keyword.
    """
    who = info.get("from_name") or info["from_id"]
    # "rx" rather than "via": every field here describes the message being
    # answered, not the reply, and rx reads as reception - matching rxSnr and
    # rxRssi, which is where snr and rssi come from.
    bits = [info["when"], f"from={who}", f"rx={info['transport']}"]
    if info["snr"] is not None:
        bits.append(f"snr={info['snr']}")
    if info["rssi"] is not None:
        bits.append(f"rssi={info['rssi']}")
    if info.get("distance_m") is not None:
        bits.append(f"dist={format_distance(info['distance_m'])}")
    return f"{BOT_REPLY_PREFIX}{reply}\n[{' '.join(bits)}]"


class MeshtasticTUI(App):
    """Three-pane Meshtastic BLE monitor: devices | channels & nodes | messages."""

    CSS = """
    #main-row { height: 1fr; }
    #devices-pane, #targets-pane { width: 28; border: solid $accent; }
    #messages-pane { border: solid $accent; }
    #status-pane { height: 8; border: solid $warning; }
    ListView { height: 1fr; }
    RichLog { height: 1fr; }
    #devices-pane #device-list { height: 1fr; }
    #devices-pane #local-status-log { height: 2fr; }
    .pane-title { text-style: bold; background: $accent 20%; padding: 0 1; }
    #status-pane .pane-title { background: $warning 30%; }
    #status-bar { height: 1; background: $accent 30%; padding: 0 1; }
    """

    BINDINGS = [
        Binding("r", "rescan", "Rescan devices"),
        Binding("q", "quit", "Quit"),
        # Left/Right cycle devices -> channels/nodes -> messages. priority=True so
        # they win even when the log pane has focus (RichLog binds arrow keys for
        # its own scrolling); action_focus_pane special-cases the send box so
        # typing still moves the text cursor instead of switching panes. Up/down
        # are deliberately left alone everywhere - ListView uses them to move the
        # list cursor and RichLog uses them to scroll, both of which matter more
        # than a pane jump. Tab/Shift+Tab (Textual default) still cycle through
        # every focusable widget including the status pane.
        Binding("left", "focus_pane(-1)", "上一欄", show=False, priority=True),
        Binding("right", "focus_pane(1)", "下一欄", show=False, priority=True),
    ]

    def __init__(
        self,
        tcp_host: str | None = None,
        serial_port: str | None = None,
        here: tuple[float, float] | None = None,
    ) -> None:
        super().__init__()
        # Reference position for node distances, from --here. Local only - it is
        # never sent to the device or broadcast to the mesh.
        self.here = here
        self.tcp_host = tcp_host
        self.serial_port = serial_port
        # Targets named on the command line, listed above the BLE scan results
        # and connected to without waiting for the ~10s scan. TCP first so the
        # existing --host behaviour is unchanged when both are given.
        self.explicit_targets: list[tuple[str, str]] = []
        if tcp_host:
            self.explicit_targets.append((TRANSPORT_TCP, tcp_host))
        if serial_port:
            self.explicit_targets.append((TRANSPORT_SERIAL, serial_port))
        self.interface = None
        self.transport: str | None = None
        self.peer: str | None = None  # resolved address of the far end
        self.my_id: str | None = None
        self.target: tuple[str, str | int] | None = None  # ("channel", idx) or ("node", id)
        self.history: dict[tuple, list[str]] = {}
        # Targets that received a message while you were looking somewhere
        # else, shown in bold in the targets pane. Keyed "kind:key" to match
        # the ListItem names, so a row can be found without rebuilding it.
        self.unread: set[str] = set()
        # Each target row's markup *without* the unread bold. Kept so the bold
        # can be switched on and off without recomputing the label - a node row
        # carries a distance that costs a haversine over the node table.
        self._target_markup: dict[str, str] = {}
        self.firmware_version: str | None = None
        self.last_signal: dict | None = None
        self.scanning = False
        # The device key currently connected, so a dropped link can be
        # reopened without asking the user to pick the device again.
        self.connected_key: str | None = None
        # Whether the link is down and a reconnect is in flight. Kept separate
        # from self.interface, which stays set: the status pane and history
        # still describe the node we were talking to.
        self.link_down = False
        # Set while quitting, so the close we perform ourselves is not mistaken
        # for the link dropping.
        self._closing = False
        # Status bar figures. monotonic, so the uptime cannot jump if the system
        # clock is corrected under us. Received counts text messages from other
        # nodes - our own echo would otherwise make sending one message read as
        # both sent and received. Typed and auto-reply are kept apart so the bar
        # can show how much of the traffic the bot generated by itself.
        self.started_at = time.monotonic()
        # Every packet the radio handed us, of any kind. The library publishes
        # meshtastic.receive.<kind> under the meshtastic.receive topic we
        # subscribe to, so this sees position/nodeinfo/telemetry too - which is
        # the point: those flow constantly while text messages are rare, so
        # this is the figure that shows the link is alive.
        self.packet_count = 0
        self.received_count = 0
        self.sent_typed_count = 0
        self.sent_auto_count = 0
        # Reconnects, split two ways because they answer different questions:
        # which attempt the current outage is on ("is it still trying?"), and
        # how many attempts there have been all session ("has the link been
        # flapping while I was away?").
        self.reconnect_attempt = 0
        self.reconnect_total = 0
        # Packet ids already auto-replied to, oldest first. The mesh redelivers
        # packets (rebroadcast, MQTT bridge), so the same message can arrive more
        # than once; this holds it to one reply each. Bounded, so a long session
        # cannot grow it without limit.
        self._replied_ids: dict = {}

    def compose(self) -> ComposeResult:
        # min_width=1 on every RichLog below: RichLog defaults to min_width=78,
        # meaning it lays text out for at least 78 columns even in a much
        # narrower pane and then clips whatever doesn't fit, instead of
        # wrapping. min_width=1 makes it wrap at the pane's actual width.
        with Vertical():
            with Horizontal(id="main-row"):
                with Vertical(id="devices-pane"):
                    yield Label("裝置", classes="pane-title")
                    yield ListView(id="device-list")
                    yield Label("本機狀態", classes="pane-title")
                    yield RichLog(
                        id="local-status-log", wrap=True, highlight=True, markup=True, min_width=1
                    )
                with Vertical(id="targets-pane"):
                    yield Label("頻道 / Node", classes="pane-title")
                    yield ListView(id="target-list")
                with Vertical(id="messages-pane"):
                    yield Label("訊息", classes="pane-title", id="messages-title")
                    yield RichLog(id="log", wrap=True, highlight=True, markup=True, min_width=1)
                    yield Input(
                        placeholder="選一個頻道/node 才能送訊息...", id="send-box", disabled=True
                    )
            with Vertical(id="status-pane"):
                yield Label("狀態", classes="pane-title")
                yield RichLog(
                    id="status-log", wrap=True, highlight=True, markup=True, min_width=1
                )
            # One fixed line at the very bottom. A Label rather than another
            # RichLog: this is replaced wholesale every second, not appended to.
            yield Label(id="status-bar")

    def on_mount(self) -> None:
        pub.subscribe(self.on_receive, "meshtastic.receive")
        pub.subscribe(self.on_config_synced, "meshtastic.connection.established")
        pub.subscribe(self.on_connection_lost, "meshtastic.connection.lost")
        self.action_rescan()
        self.set_interval(5.0, self._render_local_status)
        # Every second, so the uptime actually ticks rather than jumping in
        # five-second steps.
        self.set_interval(1.0, self._render_status_bar)
        self._render_status_bar()

    def on_unmount(self) -> None:
        # Before unsubscribing: close() below makes the library publish
        # connection.lost, and until this is set that looks like the link
        # failing on its own.
        self._closing = True
        pub.unsubscribe(self.on_receive, "meshtastic.receive")
        pub.unsubscribe(self.on_config_synced, "meshtastic.connection.established")
        pub.unsubscribe(self.on_connection_lost, "meshtastic.connection.lost")
        if self.interface:
            # BLEInterface registers its own atexit hook (self._exit_handler)
            # that also calls the same no-timeout disconnect - if the
            # background close() below hasn't reached its own
            # atexit.unregister() yet by the time the process starts shutting
            # down, that hook fires *again* during interpreter shutdown and
            # hangs there instead, which is what surfaced as an
            # "Exception ignored in atexit callback" KeyboardInterrupt
            # traceback on exit. Unregistering it here first - synchronous,
            # no BLE I/O, can't hang - closes that race outright.
            exit_handler = getattr(self.interface, "_exit_handler", None)
            if exit_handler is not None:
                try:
                    atexit.unregister(exit_handler)
                except Exception:  # noqa: BLE001
                    pass

            # interface.close() disconnects over BLE with no timeout, so a
            # slow/stuck CoreBluetooth disconnect (or an exception from an
            # already-dropped link) would otherwise hang or crash the app
            # right as it's quitting. Fire-and-forget in a daemon thread so
            # Ctrl+Q/Q always exits immediately regardless of how that goes -
            # the device notices the dropped link on its own either way.
            def close_quietly() -> None:
                try:
                    self.interface.close()
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=close_quietly, daemon=True).start()

    # ---- pane 1: devices -------------------------------------------------

    def action_rescan(self) -> None:
        # Guard against overlapping scans: BLEInterface.scan() takes ~10s, and
        # without this a manual "R" press (or the empty-result auto-retry in
        # _populate_devices) while one is still running would start a second
        # one and land both scans' results in the list.
        if self.scanning:
            self._log_system("已在掃描中,請稍候...")
            return
        self.scanning = True
        # Show the TCP row straight away so it is pickable during the scan;
        # _populate_devices rebuilds the list once the BLE results land.
        self._rebuild_device_list([])
        if self.explicit_targets and self.interface is None:
            # A target named on the command line is an explicit request, so
            # connect now rather than making the user wait out the BLE scan. The
            # scan still runs, so a BLE device can be picked afterwards.
            transport, address = self.explicit_targets[0]
            self._on_device_selected(f"{transport}:{address}")
        self.scan_devices()

    @work(thread=True)
    def scan_devices(self) -> None:
        try:
            devices = meshtastic.ble_interface.BLEInterface.scan()
        except Exception as e:  # noqa: BLE001
            self.scanning = False
            self.call_from_thread(self._log_system, f"[red]掃描失敗: {e}[/red]")
            return
        self.call_from_thread(self._populate_devices, devices)

    def _rebuild_device_list(self, ble_devices: list) -> None:
        """Redraw the device pane from both sources: the --host TCP node, then
        whatever the BLE scan found.

        Always rebuilt from scratch rather than appended to, so the TCP row
        survives a rescan and a repeated scan cannot duplicate a BLE row.
        """
        listview = self.query_one("#device-list", ListView)
        listview.clear()
        marks = {TRANSPORT_TCP: "◆", TRANSPORT_SERIAL: "▣"}
        for transport, address in self.explicit_targets:
            shown = address.rsplit("/", 1)[-1] if transport == TRANSPORT_SERIAL else address
            listview.append(
                ListItem(
                    Label(f"{marks[transport]} {shown}  [dim]{transport.upper()}[/dim]"),
                    name=f"{transport}:{address}",
                )
            )
        for d in ble_devices:
            listview.append(
                ListItem(
                    Label(f"● {d.name}  [dim]BLE[/dim]"),
                    name=f"{TRANSPORT_BLE}:{d.name}",
                )
            )

    def _populate_devices(self, devices: list) -> None:
        self.scanning = False
        self._rebuild_device_list(devices)
        listview = self.query_one("#device-list", ListView)

        if not listview.children:
            # Only reachable without --host, so the retry cannot spin forever
            # on a list that always holds the TCP row.
            if self.interface is None:
                self._log_system("沒找到裝置,自動重新掃描...")
                self.action_rescan()
            else:
                self._log_system("沒找到裝置,按 R 重新掃描")
            return

        if not devices:
            self._log_system("沒掃到 BLE 裝置,按 R 重新掃描")
        elif len(devices) == 1 and not self.explicit_targets and self.interface is None:
            self._log_system(f"只找到一個裝置,自動連線: {devices[0].name}")
            listview.index = 0
            self._on_device_selected(f"{TRANSPORT_BLE}:{devices[0].name}")

    def _on_device_selected(self, device_key: str) -> None:
        transport, address = _split_device_key(device_key)
        self._log_system(f"連線到 {address} ({transport.upper()})...")
        self.connect_device(device_key)

    @work(thread=True)
    def connect_device(self, device_key: str) -> None:
        transport, address = _split_device_key(device_key)
        try:
            interface = open_interface(transport, address)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._log_system, f"[red]連線失敗: {e}[/red]")
            return
        self.connected_key = device_key
        self.call_from_thread(self._connected, interface, transport)

    # ---- losing and regaining the link -------------------------------------

    # Seconds between reconnect attempts. Quick at first so a momentary WiFi
    # blip recovers almost immediately, then settling into a steady poll rather
    # than hammering a node that is powered off or rebooting.
    RECONNECT_DELAYS = (1, 2, 5, 10, 30)

    @classmethod
    def _reconnect_delay(cls, attempt: int) -> int:
        """Seconds to wait before reconnect attempt `attempt`, counting from 1.

        Anything past the end of the table repeats the last value, so the
        retries continue at a fixed interval instead of growing without bound.
        """
        index = min(max(attempt, 1), len(cls.RECONNECT_DELAYS)) - 1
        return cls.RECONNECT_DELAYS[index]

    def on_connection_lost(self, interface) -> None:
        """The library's reader thread has given up on the link.

        Without this the app goes on claiming to be connected while receiving
        nothing at all. A TCP timeout arrives as OSError inside
        StreamInterface.__reader, and its `finally` calls _disconnected(),
        ending the reader for good; the library only reopens the socket by
        itself when the peer closes cleanly (recv returning b""), never after
        an error. So every message sent after a timeout was silently lost -
        the pane kept saying "已連線" and simply stopped filling.

        Published from the library's own publishing thread, hence the hand-off.
        """
        # Neither of these is our link going down: an interface we have already
        # moved off, or the close we perform ourselves while quitting.
        if self._closing or interface is not self.interface:
            return
        self.call_from_thread(self._link_lost)

    def _link_lost(self) -> None:
        """Say the link dropped, and start trying to get it back."""
        # The same outage can be reported more than once (the reader's finally
        # and an explicit close, say); only the first starts a reconnect.
        if self.link_down:
            return
        self.link_down = True
        self.reconnect_attempt = 0
        self._log_system("[red]連線中斷,自動重連中...[/red]")
        self._render_local_status()
        self.reconnect_loop()

    @work(thread=True)
    def reconnect_loop(self) -> None:
        """Reopen the link that dropped, backing off between attempts.

        Keeps trying for as long as the app is up: the usual causes - a WiFi
        blip, the node rebooting, its single TCP slot taken by a phone app -
        all clear on their own, and a monitor that gives up needs babysitting
        at exactly the moment it should not.
        """
        key = self.connected_key
        if key is None:
            return
        transport, address = _split_device_key(key)
        attempt = 0
        while not self._closing and self.link_down:
            attempt += 1
            self.reconnect_attempt = attempt
            self.reconnect_total += 1
            time.sleep(self._reconnect_delay(attempt))
            # The app may have quit, or a device been picked by hand, while
            # this thread was asleep.
            if self._closing or not self.link_down:
                return
            self.call_from_thread(self._log_system, f"重連中(第 {attempt} 次)...")
            try:
                interface = open_interface(transport, address)
            except Exception as e:  # noqa: BLE001
                self.call_from_thread(self._log_system, f"[yellow]重連失敗: {e}[/yellow]")
                continue
            self.call_from_thread(self._relinked, interface, transport)
            return

    def _relinked(self, interface, transport: str) -> None:
        """Adopt a reconnected interface.

        _connected does the rest, and the library follows up with
        connection.established, so on_config_synced rebuilds the channel and
        node lists - which also picks up any node first heard while the link
        was down. self.history survives, so nothing already logged is lost.
        """
        self.link_down = False
        self._connected(interface, transport)

    def _connected(self, interface, transport: str = TRANSPORT_BLE) -> None:
        # This fires as soon as the raw link is up. Channels/nodes/myUser are
        # not populated yet at this point - that's a separate, slightly later
        # config-sync phase signalled by "meshtastic.connection.established"
        # (see on_config_synced below). Don't populate the targets pane here.
        self.interface = interface
        self.transport = transport
        self.peer = self._describe_peer(interface, transport)
        where = f" ({self.peer})" if self.peer else ""
        self._log_system(f"[green]{transport.upper()} 已連線{where}[/green],等待設定同步...")

    def _describe_peer(self, interface, transport: str) -> str | None:
        """Address of the far end, for the status pane.

        TCP asks the socket for the resolved peer, so connecting by a hostname
        like Meshtastic.local still reports the IP actually reached. The port is
        only shown when it is not the default, to keep the narrow pane readable.
        BLE keeps its address on the inner client rather than the interface.
        """
        if transport == TRANSPORT_TCP:
            sock = getattr(interface, "socket", None)
            try:
                host, port = sock.getpeername()[:2]
            except Exception:  # noqa: BLE001
                # Socket already gone, or a stub without getpeername - fall back
                # to whatever we were asked to connect to.
                return getattr(interface, "hostname", None) or self.tcp_host
            return host if port == DEFAULT_TCP_PORT else f"{host}:{port}"
        return getattr(getattr(interface, "client", None), "address", None)

    def on_config_synced(self, interface, topic=pub.AUTO_TOPIC) -> None:
        # Fires on meshtastic's own pubsub thread, not Textual's - hop back.
        self.call_from_thread(self._config_synced, interface)

    def _config_synced(self, interface) -> None:
        my_user = interface.getMyUser() or {}
        self.my_id = my_user.get("id")
        self._log_system(f"[green]設定同步完成[/green] (my id: {self.my_id})")
        self._report_rule_coverage()
        self._populate_targets()
        self._render_local_status()
        self.fetch_metadata()

    def _distance_to(self, node_id: str | None) -> float | None:
        """Distance from this station to `node_id`, or None if either end lacks
        a position.

        Returns None for our own id too: a reply to a message typed here would
        otherwise carry a trivially true "dist=0m".
        """
        if not node_id or node_id == self.my_id or self.interface is None:
            return None
        nodes = self.interface.nodes or {}
        here = self.here or node_position(nodes.get(self.my_id, {}))
        there = node_position(nodes.get(node_id, {}))
        if here is None or there is None:
            return None
        return haversine_m(*here, *there)

    def _channel_name(self, index: int) -> str | None:
        """The channel's configured name, or None if it has none (the primary
        channel is typically unnamed - address it as [#0] in rules.txt)."""
        for ch in self.interface.localNode.channels or []:
            if ch.index == index and ch.settings:
                return ch.settings.name or None
        return None

    def _reply_sections(self, target: tuple, channel: int | None) -> list[str]:
        """rules.txt sections to search for `target`, most specific first.

        A channel message uses that channel's sections. A direct message tries
        [DM] first and then falls back to the sections of the channel it arrived
        on, so a rule can be written for private messages specifically or simply
        shared with the channel. `channel` is None when it is not known - a DM
        typed here, where the target is a node and no packet was received - and
        then only [DM] and [*] apply.
        """
        kind, key = target
        if kind == "channel":
            return self._channel_sections(key)
        if channel is None:
            return [DM_SECTION, ALL_CHANNELS]
        return [DM_SECTION] + self._channel_sections(channel)

    def _channel_sections(self, index: int) -> list[str]:
        """rules.txt sections that apply to channel `index`, most specific first."""
        sections = []
        name = self._channel_name(index)
        if name:
            sections.append(name)
        sections.append(f"#{index}")
        sections.append(ALL_CHANNELS)
        return sections

    def _known_channel_sections(self) -> set[str]:
        """Every section name that would match one of this node's channels, so
        _config_synced can warn about rules aimed at a channel that is not
        configured here (a typo'd name, or a channel on another node)."""
        known = {ALL_CHANNELS}
        for ch in self.interface.localNode.channels or []:
            if not ch.settings:
                continue
            known.add(f"#{ch.index}")
            if ch.settings.name:
                known.add(ch.settings.name)
        return known

    def _report_rule_coverage(self) -> None:
        """Log which channels rules.txt will auto-reply on, and flag anything
        suspicious - sections that match no channel here, and catch-all rules
        that would fire on public channels."""
        rules = load_rules()
        if not rules:
            self._log_system("[yellow]rules.txt 沒有任何規則,bot 不會自動回覆[/yellow]")
            return

        known = self._known_channel_sections()
        for section, section_rules in sorted(rules.items()):
            if not section_rules:
                continue
            count = len(section_rules)
            if section == ALL_CHANNELS:
                self._log_system(
                    f"[yellow]{count} 條規則適用於「所有頻道」,包含公共頻道 - "
                    f"建議改用 [頻道名] 或 [#index] 限定[/yellow]"
                )
            elif section == DM_SECTION:
                # [DM] is selected by the target being a node, not by matching
                # a channel name, so it has to skip the "matches no channel"
                # branch below - which would otherwise report working DM rules
                # as dead and send you looking for a typo that is not there.
                # A channel genuinely named DM shares the section (see
                # DM_SECTION), and is then covered as well.
                also = " (以及同名的頻道)" if section in known else ""
                self._log_system(f"自動回覆私訊{also}: {count} 條規則")
            elif section in known:
                self._log_system(f"自動回覆頻道 [{section}]: {count} 條規則")
            else:
                self._log_system(
                    f"[red]rules.txt 的 [{section}] 對不上這台的任何頻道,該區規則不會生效[/red]"
                )

    # ---- pane 1b: local device status -------------------------------------

    @work(thread=True)
    def fetch_metadata(self) -> None:
        # getMetadata() blocks on an admin round-trip over BLE and can be slow
        # (or, on some firmware, never resolve) - run it off the UI thread and
        # just leave the version as "查詢中..." if it never comes back.
        try:
            self.interface.localNode.getMetadata()
        except Exception:  # noqa: BLE001
            pass
        metadata = self.interface.metadata
        version = metadata.firmware_version if metadata else None
        self.call_from_thread(self._metadata_fetched, version)

    def _metadata_fetched(self, version: str | None) -> None:
        self.firmware_version = version or "未知"
        self._render_local_status()

    def _track_signal(self, packet: dict) -> None:
        # Tracks reception quality of whatever this radio last heard from
        # anyone on the mesh - a node can't measure its own transmit signal,
        # so this is the closest honest proxy for "how well is my radio
        # currently hearing the mesh", refreshed on every packet regardless
        # of the EDGE_ATS-only monitoring/reply restriction above.
        snr = packet.get("rxSnr")
        rssi = packet.get("rxRssi")
        if snr is None and rssi is None:
            return
        self.last_signal = {"snr": snr, "rssi": rssi, "from_id": packet.get("fromId", "?")}

    def _render_local_status(self) -> None:
        log = self.query_one("#local-status-log", RichLog)
        log.clear()
        if self.interface is None or self.my_id is None:
            log.write("[dim]尚未連線[/dim]")
            return

        lora = self.interface.localNode.localConfig.lora
        device_cfg = self.interface.localNode.localConfig.device
        position_cfg = self.interface.localNode.localConfig.position

        region = config_pb2.Config.LoRaConfig.RegionCode.Name(lora.region)
        preset = (
            config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
            if lora.use_preset
            else "自訂"
        )
        role = config_pb2.Config.DeviceConfig.Role.Name(device_cfg.role)
        gps_mode = config_pb2.Config.PositionConfig.GpsMode.Name(position_cfg.gps_mode)

        log.write(f"[bold]Region:[/bold] {region}")
        log.write(f"[bold]韌體:[/bold] {self.firmware_version or '查詢中...'}")
        log.write(f"[bold]Role:[/bold] {role}")
        log.write(f"[bold]Preset:[/bold] {preset}")
        log.write(f"[bold]Slot:[/bold] {lora.channel_num or '(Auto)'}")
        # A preset config leaves stored bandwidth at 0 and override_frequency at
        # 0.0, so both of these are reconstructed - see lora_params. A "~" marks
        # a derived value so it is not mistaken for something the node reported.
        freq = lora_params.frequency_mhz(lora)
        if lora.override_frequency:
            log.write(f"[bold]頻率:[/bold] {freq:.3f} MHz")
        elif freq is not None:
            log.write(f"[bold]頻率:[/bold] ~{freq:.3f} MHz")
        else:
            log.write("[bold]頻率:[/bold] 無法推導")
        bw = lora_params.bandwidth_khz(lora)
        if bw is None:
            log.write("[bold]Bandwidth:[/bold] 無法推導")
        elif lora.use_preset:
            log.write(f"[bold]Bandwidth:[/bold] ~{bw:g} kHz")
        else:
            log.write(f"[bold]Bandwidth:[/bold] {bw:g} kHz")
        log.write(f"[bold]Tx Power:[/bold] {lora.tx_power} dBm")

        node = (self.interface.nodes or {}).get(self.my_id, {})
        metrics = node.get("deviceMetrics", {})
        log.write(f"[bold]Uptime:[/bold] {format_uptime(metrics.get('uptimeSeconds'))}")
        battery = metrics.get("batteryLevel")
        if battery is not None:
            log.write(f"[bold]電量:[/bold] {battery}% {metrics.get('voltage', 0):.3f}V")
            log.write(f"[bold]Ch.Util:[/bold] {metrics.get('channelUtilization', 0):.1f}%")
        log.write(f"[bold]OK to MQTT:[/bold] {'是' if lora.config_ok_to_mqtt else '否'}")

        if self.last_signal:
            snr = self.last_signal["snr"]
            rssi = self.last_signal["rssi"]
            log.write(f"[bold]最近 SNR:[/bold] {snr if snr is not None else '--'}")
            log.write(f"[bold]最近 RSSI:[/bold] {rssi if rssi is not None else '--'}")
        else:
            log.write("[bold]最近收訊:[/bold] --")

        position = node.get("position", {})
        if gps_mode == "NOT_PRESENT":
            gps_line = "無 GPS 模組"
        elif gps_mode == "DISABLED":
            gps_line = "已停用"
        elif "latitudeI" in position:
            gps_line = f"已定位 ({position['latitudeI'] / 1e7:.4f}, {position['longitudeI'] / 1e7:.4f})"
        else:
            gps_line = "已啟用,尚無定位"
        log.write(f"[bold]GPS:[/bold] {gps_line}")
        # Last so Region keeps the top of the pane. The address is variable
        # length - a long IP or a hostname fallback overflows the pane and wraps
        # mid-value, so drop it to its own indented row instead of letting the
        # terminal break it in an arbitrary place.
        transport = (self.transport or "?").upper()
        if self.link_down:
            # The rest of this pane is the last known state of a node we can no
            # longer hear, so say that outright rather than leaving figures on
            # screen that look live.
            log.write(f"[bold]連線:[/bold] [red]{transport} 中斷,重連中[/red]")
        elif self.peer:
            if display_width(f"連線: {transport} {self.peer}") <= STATUS_PANE_WIDTH:
                log.write(f"[bold]連線:[/bold] {transport} {self.peer}")
            else:
                log.write(f"[bold]連線:[/bold] {transport}")
                log.write(f"  {self.peer}")
        else:
            log.write(f"[bold]連線:[/bold] {transport}")

    # ---- pane 2: channels & nodes -----------------------------------------

    @staticmethod
    def _target_key(target: tuple[str, str | int]) -> str:
        """A target's row name, matching what _add_target assigns and
        _on_target_selected parses back."""
        kind, key = target
        return f"{kind}:{key}"

    def _styled_target(self, key: str) -> str:
        """A row's markup, bolded while the target has unread messages."""
        markup = self._target_markup[key]
        return f"[bold]{markup}[/bold]" if key in self.unread else markup

    def _add_target(self, listview: ListView, key: str, markup: str) -> None:
        """Append one target row, remembering its unstyled markup."""
        self._target_markup[key] = markup
        listview.append(ListItem(Label(self._styled_target(key)), name=key))

    def _restyle_target(self, key: str) -> None:
        """Repaint one target row after its unread state changed.

        Best-effort by design: the list is built once, on config sync, so a
        node heard for the first time afterwards has no row yet. Its key stays
        in self.unread regardless, and _add_target picks the bold up if the
        list is ever rebuilt.
        """
        if key not in self._target_markup:
            return
        for item in self.query_one("#target-list", ListView).query(ListItem):
            if item.name == key:
                item.query_one(Label).update(self._styled_target(key))
                return

    def _mark_unread(self, target: tuple[str, str | int]) -> None:
        """Bold a target's row because a message arrived for it.

        Skips the selected target: its messages are already being written into
        the log in front of you, so marking it unread would be a lie that only
        clears when you switch away and back.
        """
        if target == self.target:
            return
        key = self._target_key(target)
        self.unread.add(key)
        self._restyle_target(key)

    def _clear_unread(self, target: tuple[str, str | int]) -> None:
        """Drop a target's unread bold, on becoming the selected target."""
        key = self._target_key(target)
        if key in self.unread:
            self.unread.discard(key)
            self._restyle_target(key)

    def _populate_targets(self) -> None:
        listview = self.query_one("#target-list", ListView)
        listview.clear()
        self._target_markup.clear()

        for ch in self.interface.localNode.channels or []:
            if not ch.settings:
                continue
            name = ch.settings.name or f"(unnamed #{ch.index})"
            self._add_target(listview, f"channel:{ch.index}", f"# {name}")

        nodes = self.interface.nodes or {}
        # --here wins over the node's own fix: it is there precisely because this
        # device has none, and an operator-supplied position is not worth
        # second-guessing when it disagrees.
        here = self.here or node_position(nodes.get(self.my_id, {}))
        with_position = 0

        for node_id, node in nodes.items():
            if node_id == self.my_id:
                continue
            label = node_label(nodes, node_id)
            there = node_position(node)
            if there is not None:
                with_position += 1
            distance = haversine_m(*here, *there) if (here and there) else None
            self._add_target(
                listview,
                f"node:{node_id}",
                f"@ {label} [dim]{node_id}[/dim] {format_distance(distance)}",
            )

        # Counted from what was just added, not len(listview.children): the
        # clear() above queues the old rows for removal but they are still in
        # .children at this point, while the new ones mount immediately - so on
        # a repopulate (which only happens after a reconnect) the widget count
        # reads as old + new. The rows themselves end up correct; it was only
        # this number that doubled.
        self._log_system(f"載入 {len(self._target_markup)} 個頻道/node")
        # Distance needs a position at both ends, so say which end is missing
        # rather than leaving a column of "--" unexplained.
        if here is None:
            self._log_system("[yellow]本機無定位,無法計算距離(可用 --here LAT,LON 指定)[/yellow]")
        elif with_position == 0:
            self._log_system("[yellow]沒有節點回報位置,無法計算距離[/yellow]")
        else:
            self._log_system(f"{with_position} 個節點有位置,可計算距離")

    def _on_target_selected(self, target_key: str) -> None:
        kind, _, key = target_key.partition(":")
        if kind == "channel":
            key = int(key)
        self.target = (kind, key)
        self._clear_unread(self.target)
        self.query_one("#messages-title", Label).update(f"訊息 - {kind}:{key}")
        self.query_one("#send-box", Input).disabled = False
        self._redraw_log()
        self._log_system(f"切換監控目標: {kind}:{key}")

    def _redraw_log(self) -> None:
        log = self.query_one("#log", RichLog)
        log.clear()
        for line in self.history.get(self.target, []):
            log.write(line)

    # ---- pane 3: messages ---------------------------------------------------

    def _log_system(self, line: str) -> None:
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.query_one("#status-log", RichLog).write(f"[dim]{now}[/dim] {line}")

    # ---- the fixed bottom bar ----------------------------------------------

    def _status_bar_text(self) -> str:
        """The bottom line: how long this has been up, and how much has passed
        through it.

        Kept apart from the widget write so the wording and arithmetic can be
        checked without a running app. The sent figure is the total, with the
        auto-reply share in brackets - "發 7 (自動 5)" means five of those seven
        were the bot answering, not five on top of seven.
        """
        sent = self.sent_typed_count + self.sent_auto_count
        if self.link_down:
            # Which attempt this outage is on - the figure worth watching while
            # it is down.
            link = f"   [red]重連中 第 {self.reconnect_attempt} 次[/red]"
        elif self.reconnect_total:
            # A different figure: attempts across the whole session, so a link
            # that has been flapping is visible after it recovered.
            link = f"   [dim]重連 {self.reconnect_total} 次[/dim]"
        else:
            link = ""
        return (
            f"[bold]執行[/bold] {format_elapsed(time.monotonic() - self.started_at)}"
            f"   [bold]封包[/bold] {self.packet_count}"
            f"   [bold]收[/bold] {self.received_count}"
            f"   [bold]發[/bold] {sent} ([dim]自動 {self.sent_auto_count}[/dim])"
            f"{link}"
        )

    def _render_status_bar(self) -> None:
        self.query_one("#status-bar", Label).update(self._status_bar_text())

    def on_receive(self, packet, interface) -> None:
        # Counted first, ahead of the text-message filter below - a packet is a
        # packet whatever its portnum, and non-text ones are most of the traffic.
        self.packet_count += 1
        self._track_signal(packet)

        info = parse_incoming(packet, self.my_id)
        if info is None:
            return
        sender = node_label(interface.nodes or {}, info["from_id"])
        line = format_incoming_line(info, sender)
        self.history.setdefault(info["target"], []).append(line)
        # Counted here, on the library's thread: the status bar only ever reads
        # these, and the one-second tick renders them on the app thread.
        if info["from_id"] != self.my_id:
            self.received_count += 1

        def update_ui():
            if info["target"] == self.target:
                self.query_one("#log", RichLog).write(line)
            self._mark_unread(info["target"])
            kind, key = info["target"]
            # The status pane keeps the raw id alongside the name: it is the
            # diagnostic view, and names are neither unique nor always present.
            who = sender if sender == info["from_id"] else f"{sender} ({info['from_id']})"
            self._log_system(f"收到訊息 {kind}:{key} from={who}")

        self.call_from_thread(update_ui)

        reply_line = None
        if self._should_auto_reply(info):
            reply_line = self._maybe_auto_reply(
                interface,
                info["target"],
                info["text"],
                when=info["when"],
                from_id=info["from_id"],
                transport=info["transport"],
                snr=info["snr"],
                rssi=info["rssi"],
                channel=info["channel"],
            )
        if reply_line:
            def update_ui2():
                if info["target"] == self.target:
                    self.query_one("#log", RichLog).write(reply_line)

            self.call_from_thread(update_ui2)

    # How many recent packet ids to remember for the once-per-message rule.
    # Comfortably more than a mesh can redeliver in the window that matters,
    # while staying trivial in memory.
    REPLIED_ID_LIMIT = 256

    def _should_auto_reply(self, info: dict) -> bool:
        """Whether this *received* packet earns an auto-reply.

        Three gates.

        Never answer a bot reply. Every reply starts with BOT_REPLY_PREFIX, so
        this is what stops the bot answering itself into an endless exchange. It
        is deliberately an explicit check rather than relying on the fact that
        exact matching cannot match a two-line reply - that holds today, but it
        is a subtle property to leave a loop resting on.

        Never answer our own echo. Text typed here is answered directly from
        on_input_submitted, so if the radio also echoes it back, replying again
        would double up.

        And only once per packet, since the mesh redelivers the same message via
        rebroadcast or the MQTT bridge.
        """
        if info["text"].lstrip().startswith(BOT_REPLY_PREFIX):
            return False

        if info["from_id"] == self.my_id:
            return False

        packet_id = info.get("id")
        if packet_id is None:
            # Nothing to deduplicate on. Answering is the lesser failure - a
            # duplicate reply beats silently ignoring a real message.
            return True
        if packet_id in self._replied_ids:
            return False

        self._replied_ids[packet_id] = True
        while len(self._replied_ids) > self.REPLIED_ID_LIMIT:
            self._replied_ids.pop(next(iter(self._replied_ids)))
        return True

    def _maybe_auto_reply(
        self,
        interface,
        target: tuple,
        text: str,
        when: str,
        from_id: str,
        transport: str,
        snr: float | None = None,
        rssi: int | None = None,
        channel: int | None = None,
    ) -> str | None:
        """Send + record the keyword auto-reply for `text` if a rule matches it
        exactly.

        `channel` is the index the message arrived on. It only matters for a
        direct message, whose rules fall back to that channel's - see
        _reply_sections.

        Called only from on_receive, and only once the _should_auto_reply gates
        have passed. Returns the reply line to display, or None if nothing was
        sent - the caller writes it to the log itself, since on_receive is on
        meshtastic's thread and needs call_from_thread.
        """
        kind, key = target
        reply = find_reply(text, self._reply_sections(target, channel))
        if reply is None:
            return None
        info_like = {
            "when": when,
            "transport": transport,
            "snr": snr,
            "rssi": rssi,
            "from_id": from_id,
            "from_name": node_label(interface.nodes or {}, from_id),
            "distance_m": self._distance_to(from_id),
        }
        full_reply = build_reply_text(reply, info_like)
        if kind == "channel":
            interface.sendText(full_reply, channelIndex=key)
        else:
            interface.sendText(full_reply, destinationId=key)
        self.sent_auto_count += 1

        # The sent text carries literal brackets and a newline; the log line has
        # to escape the brackets, since RichLog reads it as markup and would
        # otherwise parse "[12:34:56 ...]" as a style tag and drop it.
        shown = full_reply.replace("[", "\\[").replace("\n", " ")
        reply_line = f"[yellow]  -> auto-reply: {shown}[/yellow]"
        self.history.setdefault(target, []).append(reply_line)
        return reply_line

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self.target is None:
            return
        event.input.value = ""
        now = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[dim]{now}[/dim] [bold cyan]me[/bold cyan]: {text}"
        self.history.setdefault(self.target, []).append(line)
        self.query_one("#log", RichLog).write(line)

        kind, key = self.target
        if kind == "channel":
            self.interface.sendText(text, channelIndex=key)
        else:
            self.interface.sendText(text, destinationId=key)
        self.sent_typed_count += 1
        self._render_status_bar()

        # Text typed here is answered too, so a keyword can be tried without a
        # second radio. Done directly rather than waiting for the radio to echo
        # the packet back, because sendText publishes nothing locally and whether
        # an echo arrives at all is firmware behaviour. _should_auto_reply drops
        # our own echo if one does turn up, so this cannot answer twice, and it
        # refuses anything starting with BOT_REPLY_PREFIX, so a reply typed or
        # echoed back cannot start a loop.
        if not text.lstrip().startswith(BOT_REPLY_PREFIX):
            reply_line = self._maybe_auto_reply(
                self.interface, self.target, text,
                when=now, from_id=self.my_id or "me", transport="LoRa",
            )
            if reply_line:
                self.query_one("#log", RichLog).write(reply_line)

    # ---- pane navigation (arrow keys) ------------------------------------

    def _focused_pane_index(self) -> int | None:
        focused = self.focused
        focused_id = focused.id if focused is not None else None
        if focused_id == "device-list":
            return 0
        if focused_id == "target-list":
            return 1
        if focused_id in ("log", "send-box"):
            return 2
        return None

    def _focus_pane(self, index: int) -> None:
        index = max(0, min(index, 2))
        if index == 0:
            self.query_one("#device-list", ListView).focus()
        elif index == 1:
            self.query_one("#target-list", ListView).focus()
        else:
            send_box = self.query_one("#send-box", Input)
            if not send_box.disabled:
                send_box.focus()
            else:
                self.query_one("#log", RichLog).focus()

    def action_focus_pane(self, delta: int) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            # This binding has priority, so it would otherwise steal left/right
            # away from the send box's own text-cursor movement while typing.
            if delta < 0:
                focused.action_cursor_left()
            else:
                focused.action_cursor_right()
            return
        current = self._focused_pane_index()
        if current is None:
            return
        self._focus_pane(current + delta)

    # ---- shared list dispatch -------------------------------------------

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "device-list":
            self._on_device_selected(event.item.name)
        elif event.list_view.id == "target-list":
            self._on_target_selected(event.item.name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Meshtastic monitor TUI. Connects over BLE by default; "
        "pass --host to talk to a node over WiFi/TCP instead.",
    )
    parser.add_argument(
        "--host",
        metavar="HOST[:PORT]",
        help="also offer this node over TCP, e.g. Meshtastic.local or "
        f"192.168.0.247. Connected to immediately; BLE is still scanned. "
        f"Port defaults to {DEFAULT_TCP_PORT}.",
    )
    parser.add_argument(
        "--here",
        metavar="LAT,LON",
        type=parse_latlon,
        help="this station's position, used to show each node's distance. Only "
        "needed when the connected node has no GPS fix. Stays local - it is "
        "never sent to the device or the mesh.",
    )
    parser.add_argument(
        "--port",
        metavar="PATH",
        help="also offer this node over USB serial, e.g. /dev/cu.usbmodem2101. "
        "The only transport that still works once the node's WiFi is off and its "
        "Bluetooth is disabled, which is the normal state on MUI/TFT boards.",
    )
    parser.add_argument(
        "--wifi",
        choices=("on", "off"),
        help="turn the node's WiFi on or off, then exit without starting the UI. "
        "Needs --port or --host. The device reboots to apply it, and switching it "
        "off over --host severs that link - only USB or the device's own WLAN "
        "button can switch it back on.",
    )
    args = parser.parse_args()

    if args.wifi:
        if args.port:
            transport, address = TRANSPORT_SERIAL, args.port
        elif args.host:
            transport, address = TRANSPORT_TCP, args.host
        else:
            parser.error("--wifi needs a target: --port /dev/... (or --host, which cannot switch WiFi back on)")
        sys.exit(set_wifi(transport, address, args.wifi == "on"))

    MeshtasticTUI(tcp_host=args.host, serial_port=args.port, here=args.here).run()


if __name__ == "__main__":
    main()
