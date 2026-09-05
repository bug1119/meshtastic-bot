#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "meshtastic",
#     "paho-mqtt",
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

import warnings

# urllib3 v2 prints a NotOpenSSLWarning on import when the interpreter is
# linked against LibreSSL instead of OpenSSL, which macOS system Python is.
# Nothing here makes an HTTPS request through urllib3 - it arrives as a
# dependency of meshtastic - so the warning is noise on every start with
# nothing to act on. Filtered by message rather than by category, so a
# different urllib3 warning would still be seen.
#
# It has to sit above the dependency check rather than down with the other
# imports. That check imports meshtastic.ble_interface, and that is the first
# thing to pull urllib3 in, so a filter installed further down is installed
# after the warning has already printed - which is exactly what it did.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

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
import collections
import functools
import math
import os
import secrets
import signal
import subprocess
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
import meshtastic.util
from meshtastic.protobuf import config_pb2

import lora_params

RULES_FILE = Path(__file__).parent / "rules.txt"

DEFAULT_RULES = """\
# Auto-reply rules, grouped by channel.
#
#   [channel]           the channel the rules below it apply to, given either
#                       as its Meshtastic name ([EDGE_ATS]) or its index
#                       ([#0] - useful for the unnamed primary channel).
#                       [*] applies to every channel except those listed
#                       under [!exclude].
#   [!exclude]          channels [*] must NOT fire on, one per line, by name or
#                       as #index. Blanket rules are how a bot becomes a
#                       nuisance on a busy public channel; an excluded channel
#                       answers only from rules written for it explicitly.
#                       Direct messages are unaffected - a DM is addressed to
#                       you, so [*] still applies to it.
#   [DM]                rules for direct messages. Tried before the channel the
#                       DM arrived on, so a keyword can be answered differently
#                       in private, or left out here and shared with the
#                       channel. A reply to a DM goes back as a DM.
#   keyword=reply text  one rule per line. The whole message must equal the
#                       keyword exactly, case included - only surrounding
#                       whitespace is ignored. So A=Alpha answers "A" but not
#                       "a" and not "AAA". The reply text is taken literally,
#                       so do not quote it.
#   %variable%          replaced in the reply text. Only these names, and only
#                       in a reply - a name not on the list is left as it was
#                       written, so a stray percent sign is harmless:
#                         %my_pos%     this station, "lat,lon"
#                         %their_pos%  the sender, "lat,lon"
#                         %my_lat% %my_lon% %their_lat% %their_lon%
#                         %dist%       distance between the two
#                       A value that cannot be determined reads "--". Our end
#                       is --here if it was given, otherwise our own GPS fix;
#                       the other end is whatever position that node last
#                       broadcast. See the gps rule under [*].
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

# Busy or public channels, where a blanket [*] rule would answer everybody.
# Delete a line to let [*] apply there again.
[!exclude]
SignalTest
Emergency!
LongFast
MediumFast
MeshTW

[DM]
ping=pong (private)

[EDGE_ATS]
ping=pong
help=指令: ping
# Both ends and the distance between them. The bracket line already
# carries dist=, but not the coordinates it was computed from.
gps=我 %my_pos% · 你 %their_pos% · %dist%
"""

BROADCAST_ADDR = "^all"

# rules.txt section that applies to every channel.
ALL_CHANNELS = "*"

# rules.txt section for direct messages. A channel actually named "DM"
# would share it, which is accepted as the price of a readable name.
DM_SECTION = "DM"

# rules.txt section listing channels that [*] must not apply to, one per line
# rather than as keyword=reply pairs. The "!" makes it unmistakably not a
# channel name - Meshtastic names do not start with one, and "!" already reads
# as "not a channel" here because node ids wear it.
EXCLUDE_SECTION = "!exclude"

# Device-list items are keyed "<transport>:<address>", mirroring the "kind:key"
# scheme the targets list already uses, so one list can hold both transports
# and connect_device knows which interface class to build.
TRANSPORT_BLE = "ble"
TRANSPORT_TCP = "tcp"
TRANSPORT_SERIAL = "serial"

# What the detached copy of ourselves must be told to come up headless.
# bot_dual.py has a UI to suppress and so passes --server; bot_server.py is
# nothing but the server and has no such flag. Keeping it in one place is what
# lets detached_argv() be shared between them unchanged.
HEADLESS_FLAGS = ["--server"]

# Meshtastic's socket API port. Firmware only accepts one TCP client at a
# time and force-closes the previous one, so connecting here kicks off a
# phone/desktop app already talking to the same node over WiFi.
DEFAULT_TCP_PORT = 4403

# paho-mqtt is wanted only by --mqtt, so it is deliberately absent from
# _REQUIRED_MODULES above: a machine that installed the packages by hand and
# never asks for the bridge should not be refused a start over one it will not
# import. It is in the uv header all the same, so the normal route has it.
MQTT_MODULE = ("paho.mqtt.client", "paho-mqtt")

# The client proxy's topic layout, mirrored from the firmware's cryptTopic
# (src/mqtt/MQTT.h): "<root>/2/e/<channel>/<node>" carries the encrypted mesh
# packets, "<root>/2/map/" the map reports. Only the first has downlink traffic
# worth having - the node feeds everything a client hands back into
# onReceiveProto(), which decodes a ServiceEnvelope, and a MapReport is not
# one, so subscribing to the map topic would only earn it decode errors.
MQTT_ENVELOPE_PATH = "/2/e/"

# Direct messages are gatewayed under this pseudo-channel rather than any of
# the node's own, so it has to be subscribed to explicitly - waiting to see the
# node publish on it means having missed the DM that would have taught us.
MQTT_PKI_CHANNEL = "PKI"

# Substituted for whatever the node left blank, because the firmware
# substitutes exactly these (default_mqtt_* in src/mesh/Default.h). An
# untouched MQTT config has to mean the same thing here as it does there.
MQTT_DEFAULT_ADDRESS = "mqtt.meshtastic.org"
MQTT_DEFAULT_USERNAME = "meshdev"
MQTT_DEFAULT_PASSWORD = "large4cats"
MQTT_DEFAULT_ROOT = "msh"

# The device has no port setting: the firmware picks one from tls_enabled alone
# (PubSubConfig in src/mqtt/MQTT.cpp). A "host:port" address still overrides,
# which is the only way to name a broker on some other port.
MQTT_PORT = 1883
MQTT_TLS_PORT = 8883


def load_rules() -> dict[str, dict[str, str]]:
    """Re-read rules.txt every call so edits take effect without restarting the bot.

    Returns {section: {keyword: reply}}. A section is a channel name, "#<index>",
    ALL_CHANNELS, DM_SECTION, or EXCLUDE_SECTION. Rules appearing before the
    first [header] land in ALL_CHANNELS, which is what keeps a flat pre-sections
    file working.

    EXCLUDE_SECTION holds channel names as keys with empty values, since its
    lines are a list rather than pairs - see excluded_channels().
    """
    if not RULES_FILE.exists():
        RULES_FILE.write_text(DEFAULT_RULES, encoding="utf-8")

    rules: dict[str, dict[str, str]] = {}
    section = ALL_CHANNELS
    for raw_line in RULES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # A bare "#0" under [!exclude] is a channel index, not a comment.
            # It has to be: the primary channel is normally unnamed, so its
            # index is the only way to name it at all - and the primary is
            # usually the public one you most want excluded. Anything else
            # beginning with "#" stays a comment, "# 主頻道" included.
            if not (section == EXCLUDE_SECTION and line[1:].isdigit()):
                continue
            # Normalised, so "#00" and "#0" mean the same channel.
            line = f"#{int(line[1:])}"
        # Checked before the "=" test so a header is never mistaken for a rule.
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip() or ALL_CHANNELS
            rules.setdefault(section, {})
            continue
        if "=" not in line:
            # [!exclude] is a list of channels, not keyword=reply pairs, so it
            # is the one section where a bare line means something. Everywhere
            # else a line without "=" is a typo, and skipping it quietly is the
            # long-standing behaviour.
            if section == EXCLUDE_SECTION:
                rules.setdefault(section, {})[line] = ""
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


# How long to wait for a node to hand over its configuration before giving up
# on a connect. The library defaults every transport to 300s, and a node that
# accepts the connection but never finishes the handshake then buys five
# minutes of silence - long enough to look like a hang rather than a failure,
# which is exactly how it presented. The Apple client bounds the same step at
# 120s; this is tighter still, because a failed attempt here costs only a retry
# from the reconnect loop.
CONNECT_TIMEOUT_SECS = 90


def open_interface(transport: str, address: str):
    """Build and connect the meshtastic interface for one transport.

    Shared by the TUI and the one-shot --wifi path so both resolve addresses and
    pick interface classes identically.
    """
    if transport == TRANSPORT_TCP:
        hostname, port = _split_host_port(address)
        return meshtastic.tcp_interface.TCPInterface(
            hostname=hostname, portNumber=port, timeout=CONNECT_TIMEOUT_SECS
        )
    if transport == TRANSPORT_SERIAL:
        return meshtastic.serial_interface.SerialInterface(
            devPath=address, timeout=CONNECT_TIMEOUT_SECS
        )
    return meshtastic.ble_interface.BLEInterface(
        address=address, timeout=CONNECT_TIMEOUT_SECS
    )


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


def _split_host_port(address: str, default_port: int = DEFAULT_TCP_PORT) -> tuple[str, int]:
    """Split "host:port" into its parts, defaulting to Meshtastic's TCP port.

    Only splits a trailing ":<digits>", and only when the remainder has no
    colon of its own, so a bare IPv6 literal passes through untouched.

    `default_port` exists for the MQTT broker, whose default is 1883 or 8883
    depending on TLS. Copying this three-line split rather than parameterising
    it would mean two places to get that IPv6 case wrong.
    """
    host, sep, port = address.rpartition(":")
    if sep and port.isdigit() and ":" not in host and host:
        return host, int(port)
    return address, default_port


def mqtt_connect_failed(reason_code) -> bool:
    """Whether a paho connect callback is reporting a refusal.

    Deliberately not `if reason_code:`. paho's v2 callbacks hand over a
    ReasonCode object, which defines no __bool__ and is therefore truthy even
    for Success - so the obvious test reads every successful connect as a
    rejection. Measured against the real broker, where it logged
    "broker 拒絕連線: Success" and then relayed nothing.

    An int still works, which is what the v1 callbacks and the tests' stubs
    pass; 0 means accepted there.
    """
    is_failure = getattr(reason_code, "is_failure", None)
    if is_failure is not None:
        return bool(is_failure)
    return bool(reason_code)


def mqtt_broker_settings(mqtt_config) -> dict:
    """Where to reach the broker, read off the connected node.

    Read from the device rather than written down here because the operator
    changes it on the device - address, credentials, root topic and TLS are all
    node settings, and a bridge with its own copy would keep publishing to
    yesterday's broker.

    Blank fields fall back to what the firmware falls back to. Note that a
    blank *address* discards the stored username and password as well, which is
    what PubSubConfig does: credentials meant for a broker the operator did not
    name are credentials for the wrong broker.
    """
    address = (mqtt_config.address or "").strip()
    if address:
        username = mqtt_config.username or ""
        password = mqtt_config.password or ""
    else:
        address = MQTT_DEFAULT_ADDRESS
        username = MQTT_DEFAULT_USERNAME
        password = MQTT_DEFAULT_PASSWORD
    tls = bool(mqtt_config.tls_enabled)
    host, port = _split_host_port(address, MQTT_TLS_PORT if tls else MQTT_PORT)
    # Trailing slashes are stripped because the firmware pastes the root
    # straight onto a path that already starts with one ("msh/TW" + "/2/e/"),
    # so a root of "msh/TW/" there would give "msh/TW//2/e/" here.
    root = (mqtt_config.root or MQTT_DEFAULT_ROOT).strip().strip("/")
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "tls": tls,
        "root": root or MQTT_DEFAULT_ROOT,
    }


def excluded_channels() -> set[str]:
    """Channels listed under [!exclude], as names and/or "#index" forms.

    Read from the file on each call, like the rules themselves: editing
    rules.txt takes effect without a restart, and this has to follow the same
    rule or the exclusions would silently lag behind the rules they modify.
    """
    return set(load_rules().get(EXCLUDE_SECTION, {}))


def is_excluded(name: str | None, index: int | None) -> bool:
    """Whether channel `name`/`index` is listed under [!exclude].

    Either form matches, so a channel can be excluded by the name it is
    configured with or by its index - the same two ways a section addresses it.
    """
    excluded = excluded_channels()
    if not excluded:
        return False
    if name and name in excluded:
        return True
    return index is not None and f"#{index}" in excluded


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
        # EXCLUDE_SECTION holds channel names with empty replies, so a message
        # that happens to equal one of them would be "answered" with nothing at
        # all - the bot putting an empty line on the mesh. Nothing puts that
        # section in `sections` today; this is what makes it harmless if
        # something ever does.
        if section == EXCLUDE_SECTION:
            continue
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


def packet_node_id(packet: dict, id_key: str, num_key: str) -> str | None:
    """A node id for one end of a packet, rebuilt from the node number if needed.

    The library fills fromId/toId by looking the node number up in its node
    database, and leaves the key set to None when it is not there yet. That is
    the normal state for the first messages after connecting - the node list
    arrives as its own config phase, and over BLE it is slow enough that real
    traffic beats it - so a plain packet.get() would render an unresolved
    sender as "None" or a placeholder. The node number is in the packet
    regardless, so reconstruct the "!hex" id the library would have produced.
    """
    node_id = packet.get(id_key)
    if node_id:
        return node_id
    num = packet.get(num_key)
    if isinstance(num, int) and num:
        return f"!{num:08x}"
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

    # rxTime is the *node's* clock, and a node that has never had a GPS fix or a
    # phone connected reports 0 - which is the normal state for a bench node, so
    # this used to render as "??:??:??" on every single message. Falling back to
    # our own clock is honest: the packet is parsed as it arrives, so the two are
    # within a second of each other. Marked with "~" to say it was derived here
    # rather than reported, the same convention the status pane uses.
    rx_time = packet.get("rxTime")
    when = (
        datetime.datetime.fromtimestamp(rx_time).strftime("%H:%M:%S")
        if rx_time
        else "~" + datetime.datetime.now().strftime("%H:%M:%S")
    )

    to_id = packet_node_id(packet, "toId", "to") or BROADCAST_ADDR
    from_id = packet_node_id(packet, "fromId", "from") or "?"
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


def format_incoming_line(info: dict, sender: str | None = None, markup: bool = True) -> str:
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
        who = f"[bold]{sender}[/bold][dim]\\[{node_id}][/dim]" if markup else f"{sender}[{node_id}]"
    else:
        who = f"[bold]{node_id}[/bold]" if markup else str(node_id)
    when = f"[dim]{info['when']}[/dim]" if markup else info["when"]
    line = f"{when} {who}({info['transport']}"
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


# One row per fact the node reports about itself. Groups are contiguous in pane
# order, so a consumer can print one row per line (the pane) or fold each group
# into one wide line (the server log) without either one reordering the fields.
def local_status_rows(bot) -> list[tuple[str, str, str]]:
    """The node's own configuration and state, as (group, label, value) rows.

    Shared by the TUI's local-status pane and the server's startup block. It
    hands back rows instead of writing them because the two want different
    shapes - one row per line down a 24-column pane, against a handful of wide
    log lines - and neither should be the one that owns the field list. While
    this was pane code the server could not report any of it.

    Values are plain text: the pane adds its own emphasis and the log has none
    to add, so markup here would only have to be stripped again.

    A metric the node does not report gets no row at all rather than a blank
    one - a device without a battery has no battery row - which is what the
    pane has always done.
    """
    interface = bot.interface
    if interface is None or bot.my_id is None:
        return []

    lora = interface.localNode.localConfig.lora
    device_cfg = interface.localNode.localConfig.device
    position_cfg = interface.localNode.localConfig.position

    region = config_pb2.Config.LoRaConfig.RegionCode.Name(lora.region)
    preset = (
        config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
        if lora.use_preset
        else "自訂"
    )
    role = config_pb2.Config.DeviceConfig.Role.Name(device_cfg.role)
    gps_mode = config_pb2.Config.PositionConfig.GpsMode.Name(position_cfg.gps_mode)

    rows = [
        ("節點", "Region", region),
        ("節點", "韌體", bot.firmware_version or "查詢中..."),
        ("節點", "Role", role),
        ("無線電", "Preset", preset),
        ("無線電", "Slot", str(lora.channel_num or "(Auto)")),
    ]

    # A preset config leaves stored bandwidth at 0 and override_frequency at
    # 0.0, so both of these are reconstructed - see lora_params. A "~" marks a
    # derived value so it is not mistaken for something the node reported.
    freq = lora_params.frequency_mhz(lora)
    if lora.override_frequency:
        rows.append(("無線電", "頻率", f"{freq:.3f} MHz"))
    elif freq is not None:
        rows.append(("無線電", "頻率", f"~{freq:.3f} MHz"))
    else:
        rows.append(("無線電", "頻率", "無法推導"))

    bw = lora_params.bandwidth_khz(lora)
    if bw is None:
        rows.append(("無線電", "Bandwidth", "無法推導"))
    elif lora.use_preset:
        rows.append(("無線電", "Bandwidth", f"~{bw:g} kHz"))
    else:
        rows.append(("無線電", "Bandwidth", f"{bw:g} kHz"))

    rows.append(("無線電", "Tx Power", f"{lora.tx_power} dBm"))

    node = (interface.nodes or {}).get(bot.my_id, {})
    metrics = node.get("deviceMetrics", {})
    rows.append(("裝置", "Uptime", format_uptime(metrics.get("uptimeSeconds"))))
    battery = metrics.get("batteryLevel")
    if battery is not None:
        rows.append(("裝置", "電量", f"{battery}% {metrics.get('voltage', 0):.3f}V"))
        rows.append(("裝置", "Ch.Util", f"{metrics.get('channelUtilization', 0):.1f}%"))
    rows.append(("裝置", "OK to MQTT", "是" if lora.config_ok_to_mqtt else "否"))

    if bot.last_signal:
        snr = bot.last_signal["snr"]
        rssi = bot.last_signal["rssi"]
        rows.append(("收訊", "最近 SNR", str(snr) if snr is not None else "--"))
        rows.append(("收訊", "最近 RSSI", str(rssi) if rssi is not None else "--"))
    else:
        rows.append(("收訊", "最近收訊", "--"))

    position = node.get("position", {})
    if gps_mode == "NOT_PRESENT":
        gps_line = "無 GPS 模組"
    elif gps_mode == "DISABLED":
        gps_line = "已停用"
    elif "latitudeI" in position:
        gps_line = (
            f"已定位 ({position['latitudeI'] / 1e7:.4f}, "
            f"{position['longitudeI'] / 1e7:.4f})"
        )
    else:
        gps_line = "已啟用,尚無定位"
    rows.append(("定位", "GPS", gps_line))

    # Last, so Region keeps the top of the pane.
    transport = (bot.transport or "?").upper()
    if bot.link_down:
        # Everything above is the last known state of a node we can no longer
        # hear, so say that outright rather than leaving figures that look live.
        rows.append(("連線", "連線", f"{transport} 中斷,重連中"))
    elif bot.peer:
        rows.append(("連線", "連線", f"{transport} {bot.peer}"))
    else:
        rows.append(("連線", "連線", transport))

    return rows


def local_status_lines(bot) -> list[str]:
    """The same rows folded into one line per group, for a log.

    Sixteen timestamped lines is not a startup banner, it is a wall. One line
    per group keeps every fact greppable while matching the shape of the
    startup lines either side of it - `頻道:` and `規則:` are one category each
    too.
    """
    grouped: dict[str, list[str]] = {}
    for group, label, value in local_status_rows(bot):
        grouped.setdefault(group, []).append(f"{label}={value}")
    lines = []
    for group, parts in grouped.items():
        # A group holding one field named after itself would read
        # "連線: 連線=BLE Bug2_1ca6", so the label goes and the value stays.
        if len(parts) == 1 and parts[0].startswith(f"{group}="):
            lines.append(f"{group}: {parts[0][len(group) + 1:]}")
        else:
            lines.append(f"{group}: {' '.join(parts)}")
    return lines


def channel_label(interface, index: int) -> str:
    """`4(CLSE)` for a named channel, `4` for one without a name.

    The number leads so a log stays greppable by it, and the name follows
    because that is what anyone reading recognises - rules.txt is written in
    channel names, so a line saying only `channel:4` means going and looking
    the number up somewhere else.

    An unnamed channel gets nothing rather than a guess. The firmware does
    substitute the modem preset's display name for an empty primary when it
    builds MQTT topics, but that substitution belongs to the firmware, and
    printing it here would read as a name somebody had set.

    Guarded, because this is a label on a log line: a channel table in an
    unexpected shape must not stop a message from being handled.
    """
    try:
        for ch in interface.localNode.channels or []:
            if ch.settings and ch.index == index and ch.settings.name:
                return f"{index}({ch.settings.name})"
    except Exception:  # noqa: BLE001
        pass
    return str(index)


# Values a reply may interpolate, and nothing else. A whitelist rather than a
# format string: str.format and format_map walk attributes, so "{a.__class__}"
# in a rules file would reach into the program's own objects, and rules.txt is
# a file an operator edits casually. %name% cannot address anything that is not
# on this list.
REPLY_VARS = ("my_lat", "my_lon", "my_pos", "their_lat", "their_lon", "their_pos", "dist")

# What an unavailable value reads as. The bracket line omits a field it cannot
# fill, because there it is one of several and the payload is tight; inside a
# reply the operator wrote, a hole mid-sentence reads as a bug, so it says so
# instead - and "--" is what the node list and the status pane already use.
REPLY_VAR_MISSING = "--"


def expand_reply_vars(reply: str, info: dict) -> str:
    """Substitute %my_pos% and friends. Unknown names are left alone.

    Left alone rather than blanked, so a reply that happens to contain a
    percent sign survives, and a typo shows up as itself instead of vanishing.
    """
    if "%" not in reply:
        return reply

    def coord(value):
        return f"{value:.4f}" if value is not None else REPLY_VAR_MISSING

    def pair(pos):
        if pos is None:
            return REPLY_VAR_MISSING
        return f"{pos[0]:.4f},{pos[1]:.4f}"

    mine, theirs = info.get("my_pos"), info.get("their_pos")
    values = {
        "my_lat": coord(mine[0] if mine else None),
        "my_lon": coord(mine[1] if mine else None),
        "my_pos": pair(mine),
        "their_lat": coord(theirs[0] if theirs else None),
        "their_lon": coord(theirs[1] if theirs else None),
        "their_pos": pair(theirs),
        "dist": format_distance(info.get("distance_m")),
    }
    for name in REPLY_VARS:
        reply = reply.replace(f"%{name}%", values[name])
    return reply


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

    The reply text passes through expand_reply_vars() first, so a rule can ask
    for the positions this already used to compute dist - see REPLY_VARS.
    """
    reply = expand_reply_vars(reply, info)
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


class ReplyEngine:
    """The rules engine, shared by the TUI and the headless server.

    Everything here works without a widget - deciding and sending replies,
    resolving the far end, pacing reconnects - so both front ends inherit it
    unchanged and a rule that works in one works in the other. Implementors
    provide `interface`, `my_id`, `here`, `tcp_host`, `history`, `_replied_ids`,
    `sent_auto_count`, `firmware_version` and `last_signal`; the TUI has them as
    UI state, the server keeps its own.
    """

    # Whether log lines this class produces are Rich markup. True for the TUI,
    # which renders them in a RichLog; False for the server, whose output is a
    # plain stream where the tags would be printed literally.
    MARKUP = True

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

    # How long a link may claim to be up while delivering nothing at all before
    # it is treated as dead. connection.lost only fires when the transport
    # itself gives up, and a BLE link can stop carrying packets one way while
    # staying nominally connected: an observed run sat at 71 packets for 18
    # minutes with reconnects at 0, silently dropping everything sent to it,
    # while the MQTT bridge on the same link kept pushing 40 messages a minute
    # in the other direction.
    #
    # Comfortably longer than the library's 300s heartbeat interval, because
    # that heartbeat's own reply is traffic: a mesh with nothing to say still
    # produces a packet every five minutes, so a quiet night cannot trip this.
    STALE_LINK_SECS = 420
    # How often to ask. Cheap - one subtraction - so the cost of asking often
    # is nothing next to the cost of noticing late.
    STALE_CHECK_SECS = 30

    def _note_packet(self) -> None:
        """Record that the link just delivered something. Called for every
        packet, of any kind, before anything decides whether to care about it."""
        self.last_packet_at = time.monotonic()

    def _link_is_stale(self, now: float) -> bool:
        """Whether the link is up on paper but has delivered nothing for too long.

        False while there is no link, while one is already known to be down, and
        while shutting down - all three have their own handling, and a stale
        check firing on top would start a second reconnect.
        """
        if self.interface is None or self.link_down or self._closing:
            return False
        if self.last_packet_at is None:
            return False
        return (now - self.last_packet_at) > self.STALE_LINK_SECS

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

    def _my_position(self) -> tuple[float, float] | None:
        """This station's position: --here if given, else our own fix."""
        if self.here:
            return self.here
        if self.interface is None:
            return None
        return node_position((self.interface.nodes or {}).get(self.my_id, {}))

    def _position_of(self, node_id: str | None) -> tuple[float, float] | None:
        """A node's last broadcast position, or None if it has not sent one."""
        if not node_id or self.interface is None:
            return None
        return node_position((self.interface.nodes or {}).get(node_id, {}))

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

        [!exclude] applies to channel messages only; see below.
        """
        kind, key = target
        if kind == "channel":
            return self._channel_sections(key)
        if channel is None:
            return [DM_SECTION, ALL_CHANNELS]
        # [!exclude] is about broadcast traffic: it stops the bot answering
        # everyone on a busy channel. A direct message is addressed to us
        # personally, so [*] still applies to it even when the channel it
        # happened to travel on is excluded.
        sections = self._channel_sections(channel)
        if ALL_CHANNELS not in sections:
            sections.append(ALL_CHANNELS)
        return [DM_SECTION] + sections

    def _channel_sections(self, index: int) -> list[str]:
        """rules.txt sections that apply to channel `index`, most specific first.

        [*] is left out for a channel listed under [!exclude]. It is the blanket
        default, and a blanket default on a busy public channel is how a bot
        becomes a nuisance - so an excluded channel answers only from rules
        written for it by name or index.
        """
        sections = []
        name = self._channel_name(index)
        if name:
            sections.append(name)
        sections.append(f"#{index}")
        if not is_excluded(name, index):
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
            # Both ends, for a rule that wants to say where they are rather
            # than only how far apart. Same sources dist uses: --here or our
            # own fix for this end, the sender's broadcast for the other.
            "my_pos": self._my_position(),
            "their_pos": self._position_of(from_id),
        }
        full_reply = build_reply_text(reply, info_like)
        # Reported rather than raised. on_receive runs on the library's
        # publishing thread, so an exception here escapes into that thread and
        # is swallowed - the reply simply does not happen and nothing says so,
        # which is indistinguishable from the rules not matching. A BLE write
        # can fail on its own (a link whose notifications never subscribed, a
        # node that went away mid-exchange) and that has to look like a
        # failure, not like silence.
        try:
            if kind == "channel":
                interface.sendText(full_reply, channelIndex=key)
            else:
                interface.sendText(full_reply, destinationId=key)
        except Exception as exc:  # noqa: BLE001
            where = f"channel:{key}" if kind == "channel" else f"node:{key}"
            note = f"  -> 回覆送出失敗 ({where}): {type(exc).__name__}: {exc}"
            # Returned rather than logged here, matching how the success line
            # travels: the caller owns the log, and the TUI needs markup where
            # the server must not have it.
            return f"[red]{note}[/red]" if self.MARKUP else note
        self.sent_auto_count += 1

        # The sent text carries literal brackets and a newline. For the TUI the
        # brackets have to be escaped, since RichLog reads the line as markup and
        # would otherwise parse "[12:34:56 ...]" as a style tag and drop it; for
        # a plain stream they must be left exactly as sent. Either way the
        # newline is folded, so one reply stays one log line.
        flat = full_reply.replace("\n", " ")
        if self.MARKUP:
            reply_line = f"[yellow]  -> auto-reply: {flat.replace('[', chr(92) + '[')}[/yellow]"
        else:
            reply_line = f"  -> auto-reply: {flat}"
        self.history.setdefault(target, []).append(reply_line)
        return reply_line


class MeshtasticTUI(ReplyEngine, App):
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
        ble_address: str | None = None,
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
        if ble_address:
            self.explicit_targets.append((TRANSPORT_BLE, ble_address))
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
        # When the link last delivered a packet. None until the first one, so
        # the staleness check cannot fire on a link that has yet to say
        # anything - a fresh connect gets its config download in first.
        self.last_packet_at: float | None = None
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
        self.set_interval(self.STALE_CHECK_SECS, self._check_stale_link)
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

    # Row mark per transport. Every transport that can reach explicit_targets
    # needs an entry: a missing one is a KeyError raised while drawing the first
    # frame, which is how --ble arrived - added as a target without being given
    # a mark here.
    DEVICE_MARKS = {TRANSPORT_TCP: "◆", TRANSPORT_SERIAL: "▣", TRANSPORT_BLE: "●"}

    def _rebuild_device_list(self, ble_devices: list) -> None:
        """Redraw the device pane from both sources: the targets named on the
        command line, then whatever the BLE scan found.

        Always rebuilt from scratch rather than appended to, so the named rows
        survive a rescan and a repeated scan cannot duplicate a BLE row.
        """
        listview = self.query_one("#device-list", ListView)
        listview.clear()
        for transport, address in self.explicit_targets:
            shown = address.rsplit("/", 1)[-1] if transport == TRANSPORT_SERIAL else address
            mark = self.DEVICE_MARKS.get(transport, "•")
            listview.append(
                ListItem(
                    Label(f"{mark} {shown}  [dim]{transport.upper()}[/dim]"),
                    name=f"{transport}:{address}",
                )
            )
        # A --ble target is already a row above, and the scan behind it finds the
        # same node again - list it once, not twice.
        named = {address for transport, address in self.explicit_targets if transport == TRANSPORT_BLE}
        for d in ble_devices:
            if d.name in named:
                continue
            listview.append(
                ListItem(
                    Label(f"{self.DEVICE_MARKS[TRANSPORT_BLE]} {d.name}  [dim]BLE[/dim]"),
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

    def _check_stale_link(self) -> None:
        """Treat a link that has gone quiet for too long as lost.

        connection.lost never fires for this case: the transport still believes
        it is connected, so nothing else here would ever notice. See
        ReplyEngine.STALE_LINK_SECS for what "too long" is and why.
        """
        now = time.monotonic()
        if not self._link_is_stale(now):
            return
        idle = int(now - self.last_packet_at)
        self._log_system(f"[red]{idle} 秒沒有收到任何封包,視為斷線[/red]")
        self._link_lost()

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
        self.last_packet_at = time.monotonic()
        self.peer = self._describe_peer(interface, transport)
        where = f" ({self.peer})" if self.peer else ""
        self._log_system(f"[green]{transport.upper()} 已連線{where}[/green],等待設定同步...")

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
            if section == EXCLUDE_SECTION:
                # Reported as its own line, and checked for typos: an entry that
                # matches no channel here excludes nothing, and looks identical
                # to a working one until the bot answers where it should not.
                listed = sorted(section_rules)
                unknown = [c for c in listed if c not in known]
                self._log_system(f"[*] 不適用於這些頻道: {', '.join(listed)}")
                if unknown:
                    self._log_system(
                        f"[yellow][!exclude] 的 {', '.join(unknown)} 對不上這台的任何頻道,"
                        f"沒有排除到任何東西[/yellow]"
                    )
            elif section == ALL_CHANNELS:
                excluded = sorted(rules.get(EXCLUDE_SECTION, {}))
                if excluded:
                    self._log_system(
                        f"{count} 條規則適用於「所有頻道」,但已排除 "
                        f"{len(excluded)} 個: {', '.join(excluded)}"
                    )
                else:
                    self._log_system(
                        f"[yellow]{count} 條規則適用於「所有頻道」,包含公共頻道 - "
                        f"建議改用 [頻道名]、[#index] 或 [!exclude] 限定[/yellow]"
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
        self.last_signal = {
            "snr": snr,
            "rssi": rssi,
            "from_id": packet_node_id(packet, "fromId", "from") or "?",
        }

    def _render_local_status(self) -> None:
        log = self.query_one("#local-status-log", RichLog)
        log.clear()
        rows = local_status_rows(self)
        if not rows:
            log.write("[dim]尚未連線[/dim]")
            return
        for _group, label, value in rows:
            if label != "連線":
                log.write(f"[bold]{label}:[/bold] {value}")
                continue
            # The address is variable length, and a long IP or a hostname
            # fallback overflows the pane and wraps mid-value, so it drops to
            # its own indented row instead of breaking wherever the terminal
            # decides. Here rather than in local_status_rows() because a width
            # is a property of the pane, not of the node.
            if self.link_down:
                log.write(f"[bold]連線:[/bold] [red]{value}[/red]")
            elif display_width(f"連線: {value}") <= STATUS_PANE_WIDTH:
                log.write(f"[bold]連線:[/bold] {value}")
            else:
                log.write(f"[bold]連線:[/bold] {(self.transport or '?').upper()}")
                log.write(f"  {self.peer}")

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
        self._note_packet()
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


class _BoundedHistory(dict):
    """The server's stand-in for the TUI's message history.

    ReplyEngine records every reply it sends so the TUI can redraw a pane you
    switch back to. The server has no pane, but it also has no natural end - it
    is meant to stay up for weeks - so an unbounded list per target would be a
    slow leak. Each target gets a short deque instead; the recording code is
    unchanged, since deque.append works the same way.
    """

    LIMIT = 50

    def setdefault(self, key, default=None):
        if key not in self:
            super().__setitem__(key, collections.deque(maxlen=self.LIMIT))
        return self[key]


def _isolated(method):
    """Wrap a bridge callback so an exception cannot escape the thread that called it.

    Neither direction runs on a thread of our own: the uplink arrives on
    meshtastic's publishing thread, the downlink on paho's network thread. An
    exception loose in either kills a thread the bot needs for work that has
    nothing to do with MQTT - lose the publishing thread and the bot stops
    answering messages at all. MQTT is a side channel and has to fail like one.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._note_error(method.__name__, exc)
        return None

    return wrapper


class MqttProxy:
    """The broker half of Meshtastic's MQTT client proxy.

    A node with mqtt.proxy_to_client_enabled never contacts a broker itself: it
    hands every publish to whichever client is attached and expects that client
    to own the connection. Over BLE that is the only arrangement there is -
    ESP32 firmware starts Bluetooth only when WiFi is unavailable
    (src/platform/esp32/main-esp32.cpp), so a BLE-attached node has no network
    by construction and its MQTT traffic goes nowhere unless something on this
    side carries it. The phone apps do carry it; nothing here did, so the
    node's uplink was quietly dropped on the floor.

    Opt-in, behind --mqtt. Starting by default would take a mesh the operator
    may well consider private and begin republishing it to whatever broker the
    device names - which for an untouched config is the public one.

    Holds the bot rather than an interface, because a link reconnect replaces
    the interface object and a downlink has to reach the current one.
    """

    # How long to give the broker disconnect before leaving without it, for the
    # reason ServerBot.CLOSE_TIMEOUT exists: a teardown that can block is a
    # process that needs SIGKILL. Shorter than the interface's, this being the
    # least important of the things a shutdown is waiting on.
    STOP_TIMEOUT = 3

    # Mirrors the firmware's own choices. It publishes through Arduino's
    # PubSubClient, which only does QoS 0, and subscribes at QoS 1
    # (MQTT::sendSubscriptions).
    PUBLISH_QOS = 0
    SUBSCRIBE_QOS = 1

    # Distinct failure lines to keep before going quiet. A broker rejecting
    # every publish would otherwise write the same line hundreds of times a
    # minute; the heartbeat's error count is what shows it is still happening.
    ERROR_LINE_LIMIT = 20

    def __init__(self, bot, client_factory=None) -> None:
        self._bot = bot
        # Injected so the tests can drive both directions without a broker.
        # Left None it is paho, imported on first use rather than at module
        # import: a bot started without --mqtt must not need the package.
        self._client_factory = client_factory
        self._client = None
        self._settings: dict | None = None
        self._wanted: set = set()
        # Guards _wanted and the connected flag together. Topics are added from
        # meshtastic's publishing thread as the node reveals them, while paho's
        # thread walks the set to re-subscribe on connect - without this, one
        # can be iterating it while the other adds, and the loser of that race
        # is a subscription that quietly never happens.
        self._lock = threading.Lock()
        self._downlink_wanted = False
        self._stopped = threading.Event()
        self.connected = False
        self.up_count = 0
        self.down_count = 0
        self.error_count = 0
        self._reported_errors: set = set()
        # Whether the current outage has already been reported. One line per
        # state change means one line for the outage, not one per retry.
        self._reported_down = False
        # Generated on first use and kept, so reconnects present the same id
        # instead of leaving a trail of half-open sessions on the broker.
        self._client_id_cached: str | None = None

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Connect to the broker the node names, and start relaying.

        Called from on_config_synced, the first moment both halves are known:
        moduleConfig.mqtt arrives with the config download and so does the
        channel list the downlink topics are built from. A link reconnect syncs
        again, so this has to be safe to call repeatedly - the later calls only
        refresh the subscriptions, since the channels may have been edited while
        the link was down.
        """
        interface = self._bot.interface
        config = interface.localNode.moduleConfig.mqtt
        self._settings = mqtt_broker_settings(config)

        if self._client is not None:
            self._refresh_wanted_topics()
            return

        if not config.enabled:
            self._bot.log("MQTT: 節點的 mqtt.enabled 是關的,不啟動橋接")
            return
        # Without this the node keeps its MQTT traffic to itself and there is
        # nothing to relay. Worth saying plainly: --mqtt was asked for, and the
        # fix is a device setting rather than anything on this side.
        if not config.proxy_to_client_enabled:
            self._bot.log(
                "MQTT: 節點的 mqtt.proxy_to_client_enabled 是關的,不啟動橋接"
                "(節點不會把 MQTT 交給 client)"
            )
            return

        self._refresh_wanted_topics()
        try:
            self._client = self._build_client(self._settings)
        except Exception as exc:  # noqa: BLE001
            # A broker that cannot even be set up must not stop the bot: it
            # keeps answering messages, just without the bridge.
            self._bot.log(f"MQTT 橋接啟動失敗,略過: {exc}")
            self._client = None
            return

        pub.subscribe(self.on_proxy_message, "meshtastic.mqttclientproxymessage")
        scheme = "mqtts" if self._settings["tls"] else "mqtt"
        self._bot.log(
            f"MQTT 橋接啟動: {scheme}://{self._settings['host']}:{self._settings['port']}"
            f" root={self._settings['root']} 下行 topic {len(self._wanted)} 個"
        )
        threading.Thread(target=self._supervise, daemon=True).start()

    def stop(self) -> None:
        """Disconnect from the broker, but not at any price.

        On a side thread with a deadline, exactly as ServerBot._shutdown treats
        interface.close(): a stop that can hang is a process that needs
        SIGKILL, and the bridge is the least important thing being torn down.
        """
        if self._client is None:
            return
        self._stopped.set()
        try:
            pub.unsubscribe(self.on_proxy_message, "meshtastic.mqttclientproxymessage")
        except Exception:  # noqa: BLE001
            # Unsubscribing is only tidiness by this point - the relay already
            # refuses work because _stopped is set.
            pass
        stopper = threading.Thread(target=self._disconnect_quietly, daemon=True)
        stopper.start()
        stopper.join(self.STOP_TIMEOUT)
        if stopper.is_alive():
            self._bot.log(f"MQTT 中斷逾時 ({self.STOP_TIMEOUT}s),不再等待")

    def _disconnect_quietly(self) -> None:
        try:
            self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            self._bot.log(f"MQTT 中斷時出錯,忽略: {exc}")

    def heartbeat_fragment(self) -> str:
        """The MQTT counters, for the heartbeat line.

        Volume belongs here and nowhere else. This mesh moves hundreds of
        packets a minute and most of them get gatewayed, so a log line per
        relayed message would bury every line worth reading; one periodic count
        says the same thing and leaves the log readable.
        """
        text = (
            f" MQTT {'已連線' if self.connected else '未連線'}"
            f" 上行 {self.up_count} 下行 {self.down_count}"
        )
        if self.error_count:
            text += f" 錯誤 {self.error_count}"
        return text

    # ---- broker connection ------------------------------------------------

    def _build_client(self, settings: dict):
        """A paho client aimed at `settings`, wired up but not yet connected.

        Connecting is left to _supervise, on a thread of its own: this runs on
        meshtastic's publishing thread, where a blocking connect to an
        unreachable broker would stop the bot answering messages because MQTT
        is down - the one thing a side channel must never do.

        The client id is the node's own with a random suffix - see client_id
        for why the bare node id, which is what the firmware presents when it
        connects directly (connectPubSub), cannot be used here.
        """
        client = self._new_client(settings)
        if settings["tls"]:
            # Default verification, unlike the firmware's setInsecure(): an
            # ESP32 has no CA bundle to check against and this machine does, so
            # there is no reason to accept any certificate offered. A private
            # broker with a self-signed certificate is refused here and says so
            # in the log rather than failing silently.
            client.tls_set()
        if settings["username"]:
            client.username_pw_set(settings["username"], settings["password"])
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _new_client(self, settings: dict):
        if self._client_factory is not None:
            return self._client_factory(settings)
        paho = importlib.import_module(MQTT_MODULE[0])
        # reconnect_on_failure off because _supervise owns the retries, using
        # the same backoff table the link reconnect uses. Leaving paho's own
        # retry on as well would put two schedules on one socket.
        return paho.Client(
            callback_api_version=paho.CallbackAPIVersion.VERSION2,
            client_id=self.client_id(),
            reconnect_on_failure=False,
        )

    def client_id(self) -> str:
        """A broker-unique client id: the node's own, plus a random suffix.

        MQTT requires client ids to be unique. A connection presenting an id
        already in use evicts the one holding it, and the two then take turns
        evicting each other for as long as both run. Using the bare node id
        means exactly that whenever a second copy of this bot points at the
        same node - another machine, or a forgotten one left running here.

        The failure is one-sided and so does not look like a connection
        problem at all. Publishes are fire-and-forget and mostly still land,
        while the subscription dies with every eviction, so the node uplinks
        steadily and receives nothing. A real capture showed 3349 uplinks
        against 9 downlinks over 19 hours, with the downlink count frozen from
        the 5-hour mark on and the link never once reported as down.

        The node id stays as the prefix so a broker's connection log still
        names the node. The suffix comes from the cached value, so reconnects
        keep the id they had rather than opening a fresh session each time.
        """
        if self._client_id_cached is None:
            base = self._bot.my_id or "meshtastic-bot"
            self._client_id_cached = f"{base}-{secrets.token_hex(2)}"
        return self._client_id_cached

    def _supervise(self) -> None:
        """Keep the broker connection up for as long as the bot is, pacing the
        attempts with ReplyEngine's table - the same table the link reconnect
        uses, and for the same reason: quick enough that a blip recovers at
        once, then a steady poll rather than hammering a broker that is off.

        loop_forever() returns instead of reconnecting, because the client was
        built with reconnect_on_failure off, so this loop sees every drop.
        """
        attempt = 0
        while not self._stopped.is_set():
            attempt += 1
            if self._stopped.wait(ReplyEngine._reconnect_delay(attempt)):
                return
            try:
                self._client.connect(
                    self._settings["host"], self._settings["port"], keepalive=60
                )
            except Exception as exc:  # noqa: BLE001
                self._note_outage(f"連不上 broker: {exc}")
                continue
            # A connect that worked starts the table over, so the next outage
            # retries quickly instead of inheriting the last one's 30 seconds.
            attempt = 0
            try:
                self._client.loop_forever()
            except Exception as exc:  # noqa: BLE001
                self._note_outage(f"broker 連線出錯: {exc}")

    def _note_outage(self, detail: str) -> None:
        """Report an outage once, not once per retry.

        A broker that is down stays down for hours, and the retry pacing is
        already known from the code. The heartbeat is what says it is still
        down, so repeating the line here would only push the useful lines off
        the screen.
        """
        self.connected = False
        if self._reported_down:
            return
        self._reported_down = True
        self._bot.log(f"MQTT {detail},持續重連中(不影響自動回覆)")

    # ---- subscriptions ----------------------------------------------------

    def _refresh_wanted_topics(self) -> None:
        """Work out which broker topics this node has downlink use for.

        The firmware cannot tell us. MQTT::sendSubscriptions() only runs on the
        path where it opened the socket itself, so in proxy mode it never sends
        a subscription list and the choice is the client's to make.

        Two sources, because neither covers all of it. A channel with a name
        gives its topic away directly. The usually-unnamed primary does not -
        the firmware substitutes the modem preset's display name for the empty
        one ("MediumFast", "LongFast", ...), a value the config we can read does
        not carry - so that one is learned from the topics the node publishes
        on, which spell it out. Mirroring the firmware's preset table here would
        be one more copy of a firmware table to keep in step, and this repo
        already carries one of those.

        PKI comes in as soon as any channel takes downlink, matching the
        firmware's own filter: direct messages arrive under that pseudo-channel
        and no channel name reveals it.
        """
        self._downlink_wanted = False
        for channel in self._bot.interface.localNode.channels or []:
            if not channel.settings or not channel.settings.downlink_enabled:
                continue
            self._downlink_wanted = True
            if channel.settings.name:
                self._want(self._channel_topic(channel.settings.name))
        if self._downlink_wanted:
            self._want(self._channel_topic(MQTT_PKI_CHANNEL))

    def _channel_topic(self, channel_id: str) -> str:
        """The broker topic carrying downlink for one channel.

        The node publishes to "<root>/2/e/<channel>/<node>"; the same channel's
        downlink is that with the publishing node wildcarded.
        """
        return f"{self._settings['root']}{MQTT_ENVELOPE_PATH}{channel_id}/+"

    def _want(self, topic: str) -> None:
        """Add `topic` to the subscription set, sending it now if connected."""
        with self._lock:
            if topic in self._wanted:
                return
            self._wanted.add(topic)
            # Under the same lock as the flag it reads: a topic added in the
            # instant between "not connected yet" and on_connect's sweep would
            # otherwise be subscribed by neither.
            if self.connected:
                self._client.subscribe(topic, qos=self.SUBSCRIBE_QOS)

    def _learn_from_uplink(self, topic: str) -> None:
        """Subscribe to the channel a published topic names.

        This is what covers the unnamed primary - see _refresh_wanted_topics.
        Nothing is learned until some channel wants downlink at all: the node
        discards an envelope whose channel has downlink_enabled off
        (onReceiveProto filters on exactly that flag), so subscribing to one
        would spend link bandwidth to have it thrown away.
        """
        if not self._downlink_wanted:
            return
        prefix = f"{self._settings['root']}{MQTT_ENVELOPE_PATH}"
        if not topic.startswith(prefix):
            return
        channel_id = topic[len(prefix) :].split("/")[0]
        if channel_id:
            self._want(f"{prefix}{channel_id}/+")

    # ---- relay ------------------------------------------------------------

    @_isolated
    def on_proxy_message(self, proxymessage, interface) -> None:
        """Radio -> broker. Subscribed to meshtastic.mqttclientproxymessage,
        which the library publishes and then does nothing else with.

        The payload is a oneof: protobuf-encoded envelopes arrive as `data`,
        the JSON variant as `text`. Reading the wrong arm of a union gives
        whichever bytes happen to alias it, so it is selected explicitly.
        """
        if self._client is None or self._stopped.is_set():
            return
        if proxymessage.WhichOneof("payload_variant") == "text":
            payload = proxymessage.text.encode("utf-8")
        else:
            payload = proxymessage.data
        self._client.publish(
            proxymessage.topic,
            payload,
            qos=self.PUBLISH_QOS,
            retain=bool(proxymessage.retained),
        )
        self.up_count += 1
        self._learn_from_uplink(proxymessage.topic)

    @_isolated
    def _on_message(self, client, userdata, message) -> None:
        """Broker -> radio. The node decodes and filters it from here
        (onReceiveProto), so nothing is inspected on the way through."""
        interface = self._bot.interface
        if interface is None or self._stopped.is_set() or self._bot.link_down:
            return
        interface.sendMqttClientProxyMessage(message.topic, message.payload)
        self.down_count += 1

    @_isolated
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        # A refusal is where a wrong username lands, and it deserves a line
        # of its own: the alternative is a bridge that looks up and moves
        # nothing. See mqtt_connect_failed for why this is not `if reason_code`.
        if mqtt_connect_failed(reason_code):
            self.connected = False
            self._note_error("connect", f"broker 拒絕連線: {reason_code}")
            return
        self._reported_down = False
        with self._lock:
            self.connected = True
            # A clean session starts with no subscriptions at all, so the whole
            # set is re-sent on every connect rather than left to the broker.
            for topic in sorted(self._wanted):
                client.subscribe(topic, qos=self.SUBSCRIBE_QOS)
        self._bot.log(
            f"MQTT 已連線 {self._settings['host']}:{self._settings['port']}"
            f",訂閱 {len(self._wanted)} 個下行 topic"
        )

    @_isolated
    def _on_disconnect(
        self, client, userdata, flags=None, reason_code=None, properties=None
    ) -> None:
        if self._stopped.is_set():
            return
        self._note_outage(f"連線中斷 ({reason_code})")

    # ---- errors -----------------------------------------------------------

    def _note_error(self, where: str, exc) -> None:
        """Count every failure, log each distinct one once.

        The count is what the heartbeat reports; the line is what tells you
        which failure it is. Repeating the line for a fault that recurs per
        message would drown the log - the same reason relayed messages are
        counted rather than logged.
        """
        self.error_count += 1
        signature = f"{where}: {exc}"
        if signature in self._reported_errors:
            return
        if len(self._reported_errors) >= self.ERROR_LINE_LIMIT:
            return
        self._reported_errors.add(signature)
        self._bot.log(f"MQTT {signature}(不影響自動回覆)")


class ServerBot(ReplyEngine):
    """Headless auto-reply server: the same rules engine with no UI.

    Reply behaviour is inherited from ReplyEngine rather than reimplemented, so
    a rule that answers in the TUI answers here identically. What differs is
    everything around it - one timestamped line per event on a stream instead of
    panes, and a plain blocking wait instead of Textual's event loop, so the
    process can be detached and left running as a service.
    """

    MARKUP = False

    def __init__(
        self,
        here: tuple[float, float] | None = None,
        heartbeat: int = 600,
        mqtt: bool = False,
        out=None,
    ) -> None:
        self.here = here
        self.heartbeat = heartbeat
        # Resolved at write time rather than captured here, so daemonising after
        # construction still lands in the redirected stream.
        self._out = out
        self.interface = None
        self.transport: str | None = None
        self.peer: str | None = None
        self.tcp_host: str | None = None
        self.my_id: str | None = None
        # Read by local_status_rows(), which the TUI feeds from a worker and a
        # packet hook; here the version arrives with the config download and
        # the signal stays None until something is heard.
        self.firmware_version: str | None = None
        self.last_signal: dict | None = None
        self.history = _BoundedHistory()
        self._replied_ids: dict = {}
        self.sent_auto_count = 0
        self.received_count = 0
        self.started_at = time.monotonic()
        # As in the TUI's status bar: text messages are rare, everything else is
        # not, so this is the number that shows the link is still carrying
        # something. In a heartbeat line it is the difference between "quiet
        # mesh" and "we stopped hearing anything".
        self.packet_count = 0
        self.connected_key: str | None = None
        self.link_down = False
        self._closing = False
        self.reconnect_attempt = 0
        self.reconnect_total = 0
        # See MeshtasticTUI.__init__ - same meaning, same reason.
        self.last_packet_at: float | None = None
        self._stopped = threading.Event()
        # None unless --mqtt was given, and checked for None at every use
        # rather than swapped for a do-nothing stand-in: "is the bridge on" is
        # a thing the heartbeat and the shutdown both have to ask.
        self.mqtt = MqttProxy(self) if mqtt else None

    # ---- output -----------------------------------------------------------

    def log(self, text: str) -> None:
        """One timestamped line, flushed.

        Full dates, not the bare clock the TUI shows: this output is normally a
        file read days later, where "14:07:03" alone does not say which day.
        Flushed per line for the same reason - a log being tailed is no use if
        it arrives a block at a time.
        """
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} {text}", file=self._out or sys.stdout, flush=True)

    # ---- link -------------------------------------------------------------

    def _adopt(self, interface, transport: str) -> None:
        self.interface = interface
        self.transport = transport
        self.last_packet_at = time.monotonic()
        self.peer = self._describe_peer(interface, transport)
        where = f" ({self.peer})" if self.peer else ""
        self.log(f"{transport.upper()} 已連線{where},等待設定同步...")

    def on_connection_lost(self, interface) -> None:
        """Start reconnecting, for the reason the TUI does the same: the
        library's reader thread ends for good on an error, so without this the
        server sits there believing it is connected and hearing nothing."""
        if self._closing or interface is not self.interface or self.link_down:
            return
        self.link_down = True
        self.reconnect_attempt = 0
        self.log("連線中斷,自動重連中...")
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _reconnect_loop(self) -> None:
        """Reopen the dropped link, backing off between attempts, for as long as
        the process is up. The usual causes - a node reboot, a WiFi blip, its
        single TCP slot taken by a phone - all clear on their own, and a server
        that gives up needs attention at exactly the moment it should not.

        Waits on the stop event rather than sleeping, so shutdown during a
        30-second backoff is immediate.
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
            if self._stopped.wait(self._reconnect_delay(attempt)):
                return
            if self._closing or not self.link_down:
                return
            self.log(f"重連中(第 {attempt} 次)...")
            try:
                interface = open_interface(transport, address)
            except Exception as e:  # noqa: BLE001
                self.log(f"重連失敗: {e}")
                continue
            self.link_down = False
            self._adopt(interface, transport)
            return

    # ---- pubsub handlers --------------------------------------------------

    def _stale_watchdog(self) -> None:
        """Poll for a link that is up but no longer delivering.

        A thread of its own because the main loop waits on the heartbeat
        interval, which is ten minutes by default and never with --heartbeat 0 -
        neither is a rate at which to notice a dead link.
        """
        while not self._stopped.wait(self.STALE_CHECK_SECS):
            now = time.monotonic()
            if not self._link_is_stale(now):
                continue
            idle = int(now - self.last_packet_at)
            self.log(f"{idle} 秒沒有收到任何封包,視為斷線")
            self.on_connection_lost(self.interface)

    def on_config_synced(self, interface, topic=pub.AUTO_TOPIC) -> None:
        # Adopted here as well as in _adopt(). The library publishes
        # connection.established from its own thread while open_interface() is
        # still constructing the interface, so this can arrive before run() has
        # had the chance to assign self.interface - and _known_channel_sections
        # below reads it. The interface that published is ours by definition.
        self.interface = interface
        my_user = interface.getMyUser() or {}
        self.my_id = my_user.get("id")
        name = my_user.get("longName") or my_user.get("shortName") or "?"
        self.log(f"設定同步完成: {name} {self.my_id}")
        # The node hands over its DeviceMetadata during the config download, so
        # this is already in hand - no admin round-trip, unlike the TUI, which
        # has to ask for it and therefore asks on a worker thread.
        metadata = getattr(interface, "metadata", None)
        self.firmware_version = getattr(metadata, "firmware_version", None) or "未知"
        # Contained, on the same reasoning as the MQTT callbacks: this is a
        # banner. A field some firmware reports in a shape we did not expect
        # must not cost the operator the whole server - and this runs on the
        # library's own thread, where an exception loose here would take the
        # rest of the connection setup with it.
        try:
            for line in local_status_lines(self):
                self.log(line)
        except Exception as exc:  # noqa: BLE001
            self.log(f"本機狀態讀取失敗: {type(exc).__name__}: {exc}")
        channels = [
            f"#{ch.index}"
            + (f" {ch.settings.name}" if ch.settings.name else "")
            for ch in (interface.localNode.channels or [])
            if ch.settings
        ]
        self.log(f"頻道: {', '.join(channels) if channels else '(無)'}")
        rules = load_rules()
        known = self._known_channel_sections()
        # [!exclude] is not a rules section - counting its channels as "5 rules"
        # among the others reads as though five keywords were loaded.
        summary = ", ".join(
            f"[{sec}]={len(r)}" for sec, r in rules.items() if sec != EXCLUDE_SECTION
        )
        self.log(f"規則: {summary if summary else '(無)'}")

        excluded = sorted(rules.get(EXCLUDE_SECTION, {}))
        if excluded:
            self.log(f"[*] 不適用於這些頻道: {', '.join(excluded)}")
            # An entry matching no channel here excludes nothing, and looks
            # exactly like one that works - the line is in the file and reads
            # correctly. LongFast and MediumFast are modem preset names rather
            # than channel names, so this fires more often than you would think.
            missing = [c for c in excluded if c not in known]
            if missing:
                self.log(
                    f"注意: [!exclude] 的 {', '.join(missing)} 對不上這台的任何頻道,"
                    "沒有排除到任何東西"
                )

        # A section aimed at a channel this node does not have will never fire,
        # and a typo looks exactly like that. [DM] and [!exclude] are selected
        # by other means, so neither belongs here - reporting a working
        # exclusion as dead sends you hunting a typo that is not there, which is
        # exactly what this did.
        unknown = sorted(set(rules) - known - {DM_SECTION, EXCLUDE_SECTION})
        if unknown:
            self.log(
                "注意: 這些區段對應不到本機頻道,不會生效: "
                + ", ".join(unknown)
            )

        # Last, and inside its own guard: everything above is what the bot is
        # actually for, and a bridge that cannot start must not cost the
        # startup report that tells you the rules loaded.
        if self.mqtt is not None:
            try:
                self.mqtt.start()
            except Exception as exc:  # noqa: BLE001
                self.log(f"MQTT 橋接啟動失敗,略過: {exc}")

    def on_receive(self, packet, interface) -> None:
        # Before the text filter, for the reason the TUI does the same.
        self.packet_count += 1
        self._note_packet()
        info = parse_incoming(packet, self.my_id)
        if info is None:
            return
        sender = node_label(interface.nodes or {}, info["from_id"])
        kind, key = info["target"]
        label = channel_label(interface, key) if kind == "channel" else key
        self.log(f"{kind}:{label} {format_incoming_line(info, sender, markup=False)}")
        if info["from_id"] != self.my_id:
            self.received_count += 1
        if not self._should_auto_reply(info):
            return
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
            self.log(reply_line)

    # ---- lifecycle --------------------------------------------------------

    def _heartbeat_line(self) -> str:
        state = "連線中斷" if self.link_down else "已連線"
        line = (
            f"[心跳] {state} 執行 {format_elapsed(time.monotonic() - self.started_at)}"
            f" 封包 {self.packet_count}"
            f" 收訊 {self.received_count}"
            f" 自動回覆 {self.sent_auto_count}"
            f" 重連 {self.reconnect_total}"
        )
        # Appended rather than interleaved so the columns a running server is
        # read by stay where they were when --mqtt is off.
        if self.mqtt is not None:
            line += self.mqtt.heartbeat_fragment()
        return line

    # How long to give the interface to close before leaving without it. BLE
    # teardown on macOS can block indefinitely; measured against a real node,
    # SIGTERM woke the wait immediately and then close() never returned, so the
    # process needed SIGKILL. Whatever the library is waiting for, a server told
    # to stop has to stop.
    CLOSE_TIMEOUT = 5

    def stop(self, *_) -> None:
        """Unblock run(). Safe from a signal handler or another thread."""
        self._closing = True
        self._stopped.set()

    def _close_quietly(self, interface) -> None:
        try:
            interface.close()
        except Exception as exc:  # noqa: BLE001
            self.log(f"關閉介面時出錯,忽略: {exc}")

    def _shutdown(self, interface) -> bool:
        """Close the interface, but not at any price. True if it really closed.

        On a side thread with a deadline, because close() is the call that was
        observed hanging. The thread is a daemon, so a close that never finishes
        cannot keep the process alive either.

        The return value matters because close() is also where meshtastic
        unregisters the atexit handler it installed for client.disconnect. A
        close that never finished never got there, so a normal exit would still
        run that handler and hang - the deadline would have bounded the waiting
        and nothing else. The caller uses this to leave without atexit.
        """
        self._closing = True
        # Before the interface, so the relay stops handing it work while it is
        # being torn down - and bounded in its own right, since a broker socket
        # can hang exactly like a BLE one.
        if self.mqtt is not None:
            self.mqtt.stop()
        closer = threading.Thread(
            target=self._close_quietly, args=(interface,), daemon=True
        )
        closer.start()
        closer.join(self.CLOSE_TIMEOUT)
        if closer.is_alive():
            self.log(f"介面關閉逾時 ({self.CLOSE_TIMEOUT}s),不再等待")
            return False
        return True

    def run(self, transport: str, address: str) -> int:
        """Connect and serve until stopped. Returns a process exit code."""
        pub.subscribe(self.on_receive, "meshtastic.receive")
        pub.subscribe(self.on_config_synced, "meshtastic.connection.established")
        pub.subscribe(self.on_connection_lost, "meshtastic.connection.lost")

        if transport == TRANSPORT_TCP:
            self.tcp_host = address
        # Handlers before the connect, not after it. A BLE connect takes about
        # half a minute, and until these are installed a Ctrl-C in that window
        # is a plain KeyboardInterrupt inside the library - which registers
        # client.disconnect with atexit (ble_interface.py) and only unregisters
        # it in close(). Interpreter shutdown then calls that handler, it
        # dispatches onto an asyncio loop that is going away, and the process
        # hangs until SIGKILL. That is the whole of the "sometimes": the
        # handler only exists once the client object does.
        #
        # Nothing is built yet here, so leaving hard is the correct response -
        # and os._exit is the point, because it skips the atexit handler that
        # does the hanging.
        def _abort_before_connect(*_):
            self.log("連線建立中被中斷,直接離開")
            sys.stdout.flush()
            if self._out:
                self._out.flush()
            os._exit(130)

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, _abort_before_connect)

        self.log(f"連線 {transport}:{address} ...")
        try:
            interface = open_interface(transport, address)
        except Exception as e:  # noqa: BLE001
            self.log(f"連線失敗: {e}")
            return 1
        self.connected_key = f"{transport}:{address}"
        self._adopt(interface, transport)
        threading.Thread(target=self._stale_watchdog, daemon=True).start()

        # There is state worth closing now, so swap the hard abort for the
        # graceful path.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self.stop)
        # The library drives everything from its own reader and publishing
        # threads, so there is nothing for this one to do but wait - on an event
        # rather than a bare sleep, so a signal ends it at once instead of after
        # the current interval. heartbeat=0 waits indefinitely, logging only
        # what actually happens.
        interval = self.heartbeat if self.heartbeat > 0 else None
        while not self._stopped.wait(interval):
            self.log(self._heartbeat_line())

        self.log("停止中...")
        closed = self._shutdown(interface)
        self.log(f"已停止。{self._heartbeat_line()}")
        if not closed:
            # The last line is out; leave before atexit can run the disconnect
            # that the timed-out close() never unregistered.
            sys.stdout.flush()
            if self._out:
                self._out.flush()
            os._exit(0)
        return 0


def list_devices() -> int:
    """Print every node we could connect to right now, then exit.

    resolve_server_target() already scans, but only as a step towards asking
    which one you want. This answers "what is out there" on its own - which is
    what you actually need before writing a --ble name into a launchd plist, or
    when a node has stopped appearing and you want to know whether it is
    advertising at all.

    Printed as the flags you would pass, so a line can go straight onto a
    command line.
    """
    print("掃描 BLE(約 10 秒)...", file=sys.stderr)
    try:
        devices = meshtastic.ble_interface.BLEInterface.scan()
    except Exception as e:  # noqa: BLE001
        print(f"BLE 掃描失敗: {e}", file=sys.stderr)
        return 1

    # scan() filters on Meshtastic's service UUID, so everything here is a node
    # rather than every bluetooth thing in the room.
    print(f"BLE 節點 ({len(devices)}):")
    for device in devices:
        address = getattr(device, "address", "") or ""
        print(f"  --ble {device.name}" + (f"    {address}" if address else ""))
    if not devices:
        print("  (沒有節點在廣播 - 已經連上手機的節點通常就不廣播了)")

    # findPorts prefers known USB-serial vendor ids and falls back to listing
    # everything not blacklisted, so this is "likely a node", not "any port".
    ports = meshtastic.util.findPorts(True)
    print(f"\nUSB serial ({len(ports)}):")
    for port in ports:
        print(f"  --port {port}")
    if not ports:
        print("  (沒有接上的裝置)")
    return 0


def resolve_server_target(
    tcp_host: str | None, serial_port: str | None, ble_address: str | None
) -> tuple[str, str] | None:
    """Pick the node for server mode: one named target, or a numbered choice.

    A single --host/--port/--ble is taken as the answer, which is what makes
    unattended startup possible at all. Otherwise the candidates are listed -
    named targets first, then a BLE scan - and the choice read from stdin, since
    drawing a chooser is the one thing this mode exists not to do.

    Prompts on stderr so that `bot.py --server > log` still shows them.
    """
    targets: list[tuple[str, str]] = []
    if tcp_host:
        targets.append((TRANSPORT_TCP, tcp_host))
    if serial_port:
        targets.append((TRANSPORT_SERIAL, serial_port))
    if ble_address:
        targets.append((TRANSPORT_BLE, ble_address))
    if len(targets) == 1:
        return targets[0]

    if not ble_address:
        print("掃描 BLE 裝置...", file=sys.stderr)
        try:
            for d in meshtastic.ble_interface.BLEInterface.scan():
                targets.append((TRANSPORT_BLE, d.name))
        except Exception as e:  # noqa: BLE001
            print(f"BLE 掃描失敗: {e}", file=sys.stderr)
    if not targets:
        print(
            "找不到任何裝置。用 --port / --host / --ble 指定。",
            file=sys.stderr,
        )
        return None

    for i, (transport, address) in enumerate(targets, 1):
        print(f"  {i}) {transport.upper():6} {address}", file=sys.stderr)
    while True:
        try:
            choice = input(f"選擇裝置 [1-{len(targets)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(targets):
            return targets[int(choice) - 1]
        print("請輸入清單上的編號。", file=sys.stderr)


def detached_argv(
    target: tuple[str, str], here, heartbeat: int, mqtt: bool = False
) -> list[str]:
    """The command line for the background copy of ourselves.

    The device is passed explicitly because the parent has already chosen it -
    the child must never reach the interactive picker, since it has no terminal.
    --daemon is deliberately *not* forwarded: a child that daemonised again
    would spawn another child, and so on.

    --mqtt is forwarded, because the child is the process that actually serves:
    dropping it would leave --daemon --mqtt starting a server with no bridge
    and nothing anywhere saying why.
    """
    transport, address = target
    flag = {
        TRANSPORT_TCP: "--host",
        TRANSPORT_SERIAL: "--port",
        TRANSPORT_BLE: "--ble",
    }[transport]
    argv = [
        sys.executable,
        os.path.abspath(__file__),
        *HEADLESS_FLAGS,
        flag,
        address,
        "--heartbeat",
        str(heartbeat),
    ]
    if here:
        argv += ["--here", f"{here[0]},{here[1]}"]
    if mqtt:
        argv.append("--mqtt")
    return argv


def spawn_detached(argv: list[str], log_path: Path) -> int:
    """Start `argv` in its own session, output appended to `log_path`.

    A fresh process rather than a fork. By the time this runs, importing
    meshtastic has already started its "publishing" thread - the one that hands
    every received packet to its subscribers - and fork() carries only the
    calling thread. A forked child would connect successfully and then never
    process a single message. Re-executing pays for the imports twice and gets a
    process whose threads are all actually there.

    start_new_session detaches it from the controlling terminal, so closing the
    shell does not take the server with it.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log, open(os.devnull, "rb") as devnull:
        child = subprocess.Popen(
            argv, stdin=devnull, stdout=log, stderr=log, start_new_session=True
        )
    print(f"背景執行中 pid={child.pid}, log: {log_path}", file=sys.stderr)
    print(f"停止: kill {child.pid}", file=sys.stderr)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Meshtastic monitor TUI, or a headless auto-reply server "
        "with --server. Connects over BLE by default; pass --host to talk to a "
        "node over WiFi/TCP instead.",
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
        "--ble",
        metavar="NAME",
        help="also offer this BLE node by name, e.g. Meshtastic_1a2b. Connected "
        "to without waiting for a scan, which is what --server needs to start "
        "unattended.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the nodes you could connect to right now - BLE names and USB "
        "serial ports - then exit without connecting to any of them.",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="run headless: no UI, one log line per event on stdout. Answers "
        "messages from rules.txt exactly as the UI does. With a single --port / "
        "--host / --ble it starts straight away; otherwise it lists the devices "
        "it can see and reads a number.",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="with --server, relaunch in the background once the device is "
        "chosen, writing to --log. Prints the pid to stop it with. The device "
        "is picked first, so it never needs a terminal of its own.",
    )
    parser.add_argument(
        "--log",
        metavar="PATH",
        default="meshtastic-bot.log",
        help="where --daemon writes. Appended to, never truncated. "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--heartbeat",
        metavar="SECS",
        type=int,
        default=600,
        help="how often --server logs a still-alive line with its counters. "
        "0 turns it off, logging only what actually happens. "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--mqtt",
        action="store_true",
        help="bridge the node's MQTT to its broker over this connection. A "
        "node reached over BLE has no network of its own, so its "
        "proxy_to_client MQTT traffic goes nowhere unless a client carries it. "
        "Off by default: a private mesh must not start republishing itself to "
        "a public broker because the bot was started. Broker address, "
        "credentials, root topic and TLS are read from the node.",
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

    if args.list:
        sys.exit(list_devices())

    if args.wifi:
        if args.port:
            transport, address = TRANSPORT_SERIAL, args.port
        elif args.host:
            transport, address = TRANSPORT_TCP, args.host
        else:
            parser.error("--wifi needs a target: --port /dev/... (or --host, which cannot switch WiFi back on)")
        sys.exit(set_wifi(transport, address, args.wifi == "on"))

    if args.daemon and not args.server:
        parser.error("--daemon 只能跟 --server 一起用")

    if args.mqtt and not args.server:
        parser.error("--mqtt 只能跟 --server 一起用")

    # Checked here rather than left to fail at config sync, which on BLE is
    # half a minute away and in the background by then. MQTT_MODULE explains
    # why this is not simply in _REQUIRED_MODULES.
    if args.mqtt:
        try:
            importlib.import_module(MQTT_MODULE[0])
        except ImportError:
            parser.error(
                f"--mqtt 需要 {MQTT_MODULE[1]}:pip install {MQTT_MODULE[1]}"
                f"(或用 ./{os.path.basename(__file__)} 讓 uv 自己備環境)"
            )

    if args.server:
        target = resolve_server_target(args.host, args.port, args.ble)
        if target is None:
            sys.exit(1)
        transport, address = target
        if args.daemon:
            sys.exit(
                spawn_detached(
                    detached_argv(target, args.here, args.heartbeat, args.mqtt),
                    Path(args.log),
                )
            )
        sys.exit(
            ServerBot(here=args.here, heartbeat=args.heartbeat, mqtt=args.mqtt).run(
                transport, address
            )
        )

    MeshtasticTUI(
        tcp_host=args.host,
        serial_port=args.port,
        here=args.here,
        ble_address=args.ble,
    ).run()


if __name__ == "__main__":
    main()
