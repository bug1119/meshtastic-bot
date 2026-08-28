#!/usr/bin/env python3
"""Meshtastic BLE monitor - three-pane interactive TUI.

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
  1. Left pane (top) - scans for Meshtastic BLE peripherals, pick one to
     connect. Left pane (bottom) - once connected, shows the connected
     device's own status: firmware version, role/preset/region/slot/
     frequency, uptime, last-heard signal quality, GPS fix.
  2. Middle pane - once connected, lists this node's channels AND all known
     mesh nodes. Pick a channel to broadcast on it, or a node to send it a
     direct message.
  3. Right pane - live message log for whichever target is selected (each
     target keeps its own scrollback), plus an input box to send text.
     Keyword auto-reply (see rules.txt) only fires on the WATCH_CHANNEL_NAME
     channel (EDGE_ATS by default) - other channels and node DMs are still
     browsable/sendable manually, but not auto-monitored or auto-replied to.

Usage:
    python3 bot.py

Keyboard: Left/Right cycle devices -> channels/nodes -> messages. Up/Down keep
their normal per-widget meaning (move the list cursor in the device/target
lists, scroll the log) - use Tab/Shift+Tab to reach the status pane.

Auto-reply rules live in rules.txt next to this script, one per line as
    keyword=reply text
Blank lines and lines starting with # are ignored. The file is re-read on
every incoming message, so edits take effect immediately - no restart needed.

The Meshtastic phone/desktop app must be disconnected from the device first -
BLE only allows one connected client at a time.
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

import datetime
from pathlib import Path

from pubsub import pub
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Label, ListItem, ListView, RichLog

import meshtastic
import meshtastic.ble_interface
from meshtastic.protobuf import config_pb2

RULES_FILE = Path(__file__).parent / "rules.txt"

DEFAULT_RULES = """\
# keyword=reply text (one per line, case-insensitive substring match on keyword)
ping=pong
help=指令: ping
"""

BROADCAST_ADDR = "^all"

# The bot only auto-monitors/replies on this one channel (by name, resolved to
# an index once channels are known - other channels and node DMs are ignored
# entirely by on_receive, though they're still browsable/sendable manually).
WATCH_CHANNEL_NAME = "EDGE_ATS"


def load_rules() -> dict[str, str]:
    """Re-read rules.txt every call so edits take effect without restarting the bot."""
    if not RULES_FILE.exists():
        RULES_FILE.write_text(DEFAULT_RULES, encoding="utf-8")

    rules: dict[str, str] = {}
    for raw_line in RULES_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keyword, _, reply = line.partition("=")
        keyword = keyword.strip()
        if keyword:
            rules[keyword] = reply.strip()
    return rules


def find_reply(text: str) -> str | None:
    lowered = text.lower()
    for keyword, reply in load_rules().items():
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

    def __init__(self) -> None:
        super().__init__()
        self.interface = None
        self.my_id: str | None = None
        self.target: tuple[str, str | int] | None = None  # ("channel", idx) or ("node", id)
        self.history: dict[tuple, list[str]] = {}
        self.watch_channel_index: int | None = None
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
                    yield Label("裝置 (BLE)", classes="pane-title")
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
            self.interface.close()

    # ---- pane 1: devices -------------------------------------------------

    def action_rescan(self) -> None:
        # Guard against overlapping scans: BLEInterface.scan() takes ~10s, and
        # without this, a manual "R" press (or the empty-result auto-retry
        # below) while one is still running would start a second one, and
        # since _populate_devices only appends, both scans' results would land
        # in the list - the same device showing up twice.
        if self.scanning:
            self._log_system("已在掃描中,請稍候...")
            return
        self.scanning = True
        self.query_one("#device-list", ListView).clear()
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

    def _populate_devices(self, devices: list) -> None:
        self.scanning = False
        listview = self.query_one("#device-list", ListView)
        listview.clear()
        for d in devices:
            listview.append(ListItem(Label(d.name), name=d.name))
        if not devices:
            if self.interface is None:
                self._log_system("沒找到裝置,自動重新掃描...")
                self.action_rescan()
            else:
                self._log_system("沒找到裝置,按 R 重新掃描")
        elif len(devices) == 1 and self.interface is None:
            self._log_system(f"只找到一個裝置,自動連線: {devices[0].name}")
            listview.index = 0
            self._on_device_selected(devices[0].name)

    def _on_device_selected(self, device_name: str) -> None:
        self._log_system(f"連線到 {device_name}...")
        self.connect_device(device_name)

    @work(thread=True)
    def connect_device(self, device_name: str) -> None:
        try:
            interface = meshtastic.ble_interface.BLEInterface(address=device_name)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._log_system, f"[red]連線失敗: {e}[/red]")
            return
        self.call_from_thread(self._connected, interface)

    def _connected(self, interface) -> None:
        # This fires as soon as the raw BLE link is up. Channels/nodes/myUser are
        # not populated yet at this point - that's a separate, slightly later
        # config-sync phase signalled by "meshtastic.connection.established"
        # (see on_config_synced below). Don't populate the targets pane here.
        self.interface = interface
        self._log_system("[green]BLE 已連線[/green],等待設定同步...")

    def on_config_synced(self, interface, topic=pub.AUTO_TOPIC) -> None:
        # Fires on meshtastic's own pubsub thread, not Textual's - hop back.
        self.call_from_thread(self._config_synced, interface)

    def _config_synced(self, interface) -> None:
        my_user = interface.getMyUser() or {}
        self.my_id = my_user.get("id")
        self.watch_channel_index = self._find_channel_index(WATCH_CHANNEL_NAME)
        if self.watch_channel_index is None:
            self._log_system(
                f"[red]找不到頻道 {WATCH_CHANNEL_NAME},bot 不會自動監控/回應任何頻道[/red]"
            )
        else:
            self._log_system(
                f"[green]設定同步完成[/green] (my id: {self.my_id}),"
                f"監控頻道: {WATCH_CHANNEL_NAME} (index {self.watch_channel_index})"
            )
        self._populate_targets()
        self._render_local_status()
        self.fetch_metadata()

    def _find_channel_index(self, name: str) -> int | None:
        for ch in self.interface.localNode.channels or []:
            if ch.settings and ch.settings.name == name:
                return ch.index
        return None

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

        log.write(f"[bold]韌體:[/bold] {self.firmware_version or '查詢中...'}")
        log.write(f"[bold]Role:[/bold] {role}")
        log.write(f"[bold]Preset:[/bold] {preset}")
        log.write(f"[bold]Region:[/bold] {region}")
        log.write(f"[bold]Slot:[/bold] {lora.channel_num or '(Auto)'}")
        if lora.override_frequency:
            log.write(f"[bold]頻率:[/bold] {lora.override_frequency:.3f} MHz")
        else:
            log.write("[bold]頻率:[/bold] 依 Region/Slot 自動")
        log.write(f"[bold]Bandwidth:[/bold] {lora.bandwidth} kHz")
        log.write(f"[bold]Spread Factor:[/bold] {lora.spread_factor}")
        log.write(f"[bold]Coding Rate:[/bold] {lora.coding_rate}")
        log.write(f"[bold]Tx Power:[/bold] {lora.tx_power} dBm")

        node = (self.interface.nodes or {}).get(self.my_id, {})
        metrics = node.get("deviceMetrics", {})
        log.write(f"[bold]Uptime:[/bold] {format_uptime(metrics.get('uptimeSeconds'))}")
        battery = metrics.get("batteryLevel")
        if battery is not None:
            log.write(f"[bold]電量:[/bold] {battery}%")
            log.write(f"[bold]Ch.Util:[/bold] {metrics.get('channelUtilization', 0):.1f}%")
            log.write(f"[bold]Air Tx:[/bold] {metrics.get('airUtilTx', 0):.1f}%")

        if self.last_signal:
            snr = self.last_signal["snr"]
            rssi = self.last_signal["rssi"]
            log.write(f"[bold]最近 SNR:[/bold] {snr if snr is not None else '--'}")
            log.write(f"[bold]最近 RSSI:[/bold] {rssi if rssi is not None else '--'}")
            log.write(f"[bold]來源:[/bold] {self.last_signal['from_id']}")
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

        for node_id, node in (self.interface.nodes or {}).items():
            if node_id == self.my_id:
                continue
            user = node.get("user", {})
            label = user.get("shortName") or user.get("longName") or node_id
            listview.append(ListItem(Label(f"@ {label} [dim]{node_id}[/dim]"), name=f"node:{node_id}"))

        self._log_system(f"載入 {len(listview.children)} 個頻道/node")

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
        if info["target"] != ("channel", self.watch_channel_index):
            return
        line = format_incoming_line(info)
        self.history.setdefault(info["target"], []).append(line)

        def update_ui():
            if info["target"] == self.target:
                self.query_one("#log", RichLog).write(line)
            kind, key = info["target"]
            self._log_system(f"收到訊息 {kind}:{key} from={info['from_id']}")

        self.call_from_thread(update_ui)

        reply = find_reply(info["text"])
        if reply is None:
            return
        full_reply = build_reply_text(reply, info)
        kind, key = info["target"]
        if kind == "channel":
            interface.sendText(full_reply, channelIndex=key)
        else:
            interface.sendText(full_reply, destinationId=key)

        reply_line = f"[yellow]  -> auto-reply: {full_reply}[/yellow]"
        self.history.setdefault(info["target"], []).append(reply_line)

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


if __name__ == "__main__":
    MeshtasticTUI().run()
