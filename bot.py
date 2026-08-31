#!/usr/bin/env python3
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
     down, firing only on channels that have rules of their own, and never
     on direct messages.

Usage:
    python3 bot.py                          # scan and connect over BLE
    python3 bot.py --host Meshtastic.local  # connect over WiFi/TCP
    python3 bot.py --host 192.168.0.247:4403

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
appears in the message (case-insensitive substring) wins. Reply text is taken
literally - do not quote it. Rules written before the first [header] apply to
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
    print("缺少必要的 Python 套件,請先安裝:", file=sys.stderr)
    print(f"    pip3 install {' '.join(_missing_packages)}", file=sys.stderr)
    sys.exit(1)

import argparse
import atexit
import datetime
import math
import threading
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
#   keyword=reply text  one rule per line, case-insensitive substring match
#                       on keyword. The reply text is taken literally, so do
#                       not quote it.
#
# The first matching rule wins, and a channel's own rules are checked before
# [*]. Blank lines and lines starting with # are ignored.
#
# Rules placed before the first [channel] header apply to EVERY channel, which
# is how the old flat format keeps working - but that will auto-reply on public
# channels too, so prefer an explicit header.

[EDGE_ATS]
ping=pong
help=指令: ping
"""

BROADCAST_ADDR = "^all"

# rules.txt section that applies to every channel.
ALL_CHANNELS = "*"

# Device-list items are keyed "<transport>:<address>", mirroring the "kind:key"
# scheme the targets list already uses, so one list can hold both transports
# and connect_device knows which interface class to build.
TRANSPORT_BLE = "ble"
TRANSPORT_TCP = "tcp"

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
    if transport not in (TRANSPORT_BLE, TRANSPORT_TCP):
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
    """First rule whose keyword appears in `text`, searching `sections` in order.

    The caller passes the channel's own sections before ALL_CHANNELS, so a
    channel-specific rule beats a catch-all one for the same keyword.
    """
    lowered = text.lower()
    rules = load_rules()
    for section in sections:
        for keyword, reply in rules.get(section, {}).items():
            if keyword.lower() in lowered:
                return reply
    return None


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
        "from_id": from_id,
        "to_id": to_id,
        "target": target,
        "when": when,
        "transport": "MQTT" if packet.get("viaMqtt") else "LoRa",
        "snr": packet.get("rxSnr"),
        "rssi": packet.get("rxRssi"),
    }


def format_incoming_line(info: dict) -> str:
    """Render a parsed incoming message as one RichLog line."""
    line = f"[dim]{info['when']}[/dim] [bold]{info['from_id']}[/bold] ({info['transport']}"
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


def build_reply_text(reply: str, info: dict) -> str:
    """Append the collected message info to a keyword reply, compact (LoRa payload limit)."""
    bits = [info["when"], f"via={info['transport']}"]
    if info["snr"] is not None:
        bits.append(f"snr={info['snr']}")
    if info["rssi"] is not None:
        bits.append(f"rssi={info['rssi']}")
    # Omitted rather than sent as "--" when unknown: the payload limit is tight,
    # and a field that is usually blank is worse than no field.
    if info.get("distance_m") is not None:
        bits.append(f"dist={format_distance(info['distance_m'])}")
    return f"{reply} | {' '.join(bits)} from={info['from_id']}"


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

    def __init__(self, tcp_host: str | None = None, here: tuple[float, float] | None = None) -> None:
        super().__init__()
        # Reference position for node distances, from --here. Local only - it is
        # never sent to the device or broadcast to the mesh.
        self.here = here
        # Set from --host. When present the bot talks TCP to that one node and
        # never scans BLE, since there is nothing to discover.
        self.tcp_host = tcp_host
        self.interface = None
        self.transport: str | None = None
        self.peer: str | None = None  # resolved address of the far end
        self.my_id: str | None = None
        self.target: tuple[str, str | int] | None = None  # ("channel", idx) or ("node", id)
        self.history: dict[tuple, list[str]] = {}
        self.firmware_version: str | None = None
        self.last_signal: dict | None = None
        self.scanning = False

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

    def on_mount(self) -> None:
        pub.subscribe(self.on_receive, "meshtastic.receive")
        pub.subscribe(self.on_config_synced, "meshtastic.connection.established")
        self.action_rescan()
        self.set_interval(5.0, self._render_local_status)

    def on_unmount(self) -> None:
        pub.unsubscribe(self.on_receive, "meshtastic.receive")
        pub.unsubscribe(self.on_config_synced, "meshtastic.connection.established")
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
        if self.tcp_host and self.interface is None:
            # --host is an explicit request for that node, so connect now
            # rather than making the user wait out the BLE scan. The scan still
            # runs, so a BLE device can be picked afterwards.
            self._on_device_selected(f"{TRANSPORT_TCP}:{self.tcp_host}")
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
        if self.tcp_host:
            listview.append(
                ListItem(
                    Label(f"◆ {self.tcp_host}  [dim]TCP[/dim]"),
                    name=f"{TRANSPORT_TCP}:{self.tcp_host}",
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
        elif len(devices) == 1 and not self.tcp_host and self.interface is None:
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
            if transport == TRANSPORT_TCP:
                hostname, port = _split_host_port(address)
                interface = meshtastic.tcp_interface.TCPInterface(hostname=hostname, portNumber=port)
            else:
                interface = meshtastic.ble_interface.BLEInterface(address=address)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._log_system, f"[red]連線失敗: {e}[/red]")
            return
        self.call_from_thread(self._connected, interface, transport)

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
        if self.peer:
            if display_width(f"連線: {transport} {self.peer}") <= STATUS_PANE_WIDTH:
                log.write(f"[bold]連線:[/bold] {transport} {self.peer}")
            else:
                log.write(f"[bold]連線:[/bold] {transport}")
                log.write(f"  {self.peer}")
        else:
            log.write(f"[bold]連線:[/bold] {transport}")

    # ---- pane 2: channels & nodes -----------------------------------------

    def _populate_targets(self) -> None:
        listview = self.query_one("#target-list", ListView)
        listview.clear()

        for ch in self.interface.localNode.channels or []:
            if not ch.settings:
                continue
            name = ch.settings.name or f"(unnamed #{ch.index})"
            listview.append(
                ListItem(Label(f"# {name}"), name=f"channel:{ch.index}")
            )

        nodes = self.interface.nodes or {}
        # --here wins over the node's own fix: it is there precisely because this
        # device has none, and an operator-supplied position is not worth
        # second-guessing when it disagrees.
        here = self.here or node_position(nodes.get(self.my_id, {}))
        with_position = 0

        for node_id, node in nodes.items():
            if node_id == self.my_id:
                continue
            user = node.get("user", {})
            label = user.get("shortName") or user.get("longName") or node_id
            there = node_position(node)
            if there is not None:
                with_position += 1
            distance = haversine_m(*here, *there) if (here and there) else None
            listview.append(
                ListItem(
                    Label(f"@ {label} [dim]{node_id}[/dim] {format_distance(distance)}"),
                    name=f"node:{node_id}",
                )
            )

        self._log_system(f"載入 {len(listview.children)} 個頻道/node")
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

    def on_receive(self, packet, interface) -> None:
        self._track_signal(packet)

        info = parse_incoming(packet, self.my_id)
        if info is None:
            return
        line = format_incoming_line(info)
        self.history.setdefault(info["target"], []).append(line)

        def update_ui():
            if info["target"] == self.target:
                self.query_one("#log", RichLog).write(line)
            kind, key = info["target"]
            self._log_system(f"收到訊息 {kind}:{key} from={info['from_id']}")

        self.call_from_thread(update_ui)

        reply_line = self._maybe_auto_reply(
            interface,
            info["target"],
            info["text"],
            when=info["when"],
            from_id=info["from_id"],
            transport=info["transport"],
            snr=info["snr"],
            rssi=info["rssi"],
        )
        if reply_line:
            def update_ui2():
                if info["target"] == self.target:
                    self.query_one("#log", RichLog).write(reply_line)

            self.call_from_thread(update_ui2)

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
    ) -> str | None:
        """Send + record the keyword auto-reply for `text` if its channel has a
        matching rule in rules.txt. Shared by on_receive (messages
        that arrived over the mesh) and on_input_submitted (messages typed
        into the send box on this connected device) so both paths get
        monitored/replied-to identically. Returns the reply line to display,
        or None if nothing was sent - the caller decides how to write it to
        the log, since on_receive needs call_from_thread and
        on_input_submitted (already on the UI thread) doesn't.
        """
        kind, key = target
        # Rules are per-channel, so DMs are recorded but never auto-replied to.
        if kind != "channel":
            return None
        reply = find_reply(text, self._channel_sections(key))
        if reply is None:
            return None
        info_like = {
            "when": when,
            "transport": transport,
            "snr": snr,
            "rssi": rssi,
            "from_id": from_id,
            "distance_m": self._distance_to(from_id),
        }
        full_reply = build_reply_text(reply, info_like)
        if kind == "channel":
            interface.sendText(full_reply, channelIndex=key)
        else:
            interface.sendText(full_reply, destinationId=key)

        reply_line = f"[yellow]  -> auto-reply: {full_reply}[/yellow]"
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

        # Messages sent from this device (not just ones received over the
        # mesh) also get checked against rules.txt on the watched channel.
        reply_line = self._maybe_auto_reply(
            self.interface, self.target, text, when=now, from_id=self.my_id or "me", transport="LoRa"
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
    args = parser.parse_args()
    MeshtasticTUI(tcp_host=args.host, here=args.here).run()


if __name__ == "__main__":
    main()
