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
"""Exercise every flag of all three programs against real hardware.

test_rules.py covers the logic and needs nothing plugged in. This covers the
things only a real run reaches: that each flag is actually wired to something,
that a connection which cannot succeed gives up cleanly instead of hanging, that
the TUI survives being handed each transport, and that --daemon detaches and
then stops on a plain kill. It also samples RSS while connected, which is where
the figures in the README come from.

It exists because a flag can parse perfectly and still crash the moment it is
used: `--ble` on the TUI raised KeyError('ble') while drawing the first frame,
for want of one entry in a lookup table, and nothing in the unit suite could see
it.

    ./test_params_live.py                  # finds a BLE node by scanning
    ./test_params_live.py --node Bug2_1ca6 # or name one
    ./test_params_live.py --quick          # skip the slow connected cases

Needs one Meshtastic node advertising over BLE. A node already connected to a
phone usually is not advertising - disconnect the phone app first.

rules.txt is emptied for the duration and restored afterwards, so nothing here
transmits on the mesh. --wifi is only checked for its argument handling: it
writes to a device and reboots it, which is not something a test should do
behind your back.

--mqtt is the same kind of flag and gets the same treatment: its parsing and
its clean-failure path are always checked, but actually bridging the node to
its broker republishes the mesh to whatever address the device names - the
public one, for an untouched config - so the connected case needs --mqtt-live
before it will run.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable

# Whether the interpreter running this file can import paho, which is what the
# programs' --mqtt guard checks. Declared in the header above, so the shebang
# route always has it; a bare `python3 test_params_live.py` may not, and that
# changes what --mqtt is expected to do rather than making it untestable.
try:
    import paho.mqtt.client  # noqa: F401
    HAVE_PAHO = True
except ImportError:
    HAVE_PAHO = False

# How long to hold a connected run. A BLE connect takes ~25-30s before config
# sync lands, so anything much shorter tests the wrong thing.
HOLD_SECONDS = 70
SAMPLE_EVERY = 2

results: list[tuple[str, bool, str]] = []
memory: dict[str, int] = {}


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print(f"         {detail}", flush=True)


def run(
    args,
    timeout,
    expect_rc=None,
    expect_out=(),
    expect_absent=(),
    name=None,
):
    """Run to completion; check the exit code and output markers."""
    name = name or " ".join(args)
    try:
        proc = subprocess.run(
            [PY, *args], cwd=HERE, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        record(name, False, f"timed out after {timeout}s")
        return
    blob = proc.stdout + proc.stderr
    problems = []
    if expect_rc is not None and proc.returncode != expect_rc:
        problems.append(f"rc={proc.returncode}, wanted {expect_rc}")
    problems += [f"missing {n!r}" for n in expect_out if n not in blob]
    problems += [f"unexpected {n!r}" for n in expect_absent if n in blob]
    head = " | ".join(line.strip() for line in blob.strip().splitlines()[:2])[:140]
    record(name, not problems, "; ".join(problems) or f"rc={proc.returncode} {head}")


def rss_kib(pid):
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return int(out) if out.isdigit() else None


def _on_pty(args):
    """Launch under a pseudo-terminal, so Textual runs for real.

    Textual will not start without a tty, and the master end has to be drained
    or the child blocks once the pty buffer fills.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    proc = subprocess.Popen(
        [PY, *args],
        cwd=HERE,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=dict(os.environ, TERM="xterm-256color"),
    )
    os.close(slave)
    chunks: list[bytes] = []

    def drain():
        try:
            while True:
                data = os.read(master, 65536)
                if not data:
                    break
                chunks.append(data)
        except OSError:
            pass

    threading.Thread(target=drain, daemon=True).start()
    return proc, chunks


def hold(args, name, seconds=HOLD_SECONDS, tui=False, expect_out=(), key=None):
    """Run for `seconds` sampling RSS, then SIGTERM and check it behaved."""
    if tui:
        proc, chunks = _on_pty(args)
    else:
        proc = subprocess.Popen(
            [PY, *args],
            cwd=HERE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        chunks = None

    samples = []
    started = time.time()
    while time.time() - started < seconds:
        if proc.poll() is not None:
            break
        value = rss_kib(proc.pid)
        # Skip the moment before the interpreter is up, which reads as a few MiB
        # and would drag a "starting" figure down to something meaningless.
        if value and value > 4096:
            samples.append(value)
        time.sleep(SAMPLE_EVERY)

    alive = proc.poll() is None
    proc.terminate()
    try:
        proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    if chunks is not None:
        blob = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", b"".join(chunks)).decode(
            "utf-8", errors="replace"
        )
    else:
        blob = proc.stdout.read() if proc.stdout else ""

    problems = []
    if not alive:
        problems.append(f"exited early rc={proc.returncode}")
    problems += [f"missing {n!r}" for n in expect_out if n not in blob]
    peak = max(samples) if samples else 0
    if key:
        memory[key] = peak
    record(
        name,
        not problems,
        "; ".join(problems)
        or f"survived {seconds}s, peak RSS {peak / 1024:.0f} MiB ({len(samples)} samples)",
    )


def daemon_case(program, extra, node):
    """--daemon must detach, keep working, and stop on a plain kill."""
    log = HERE / f".params_live_{program}.log"
    log.unlink(missing_ok=True)
    started = time.time()
    proc = subprocess.run(
        [PY, program, *extra, "--ble", node, "--daemon", "--log", str(log), "--heartbeat", "20"],
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=180,
    )
    parent_took = time.time() - started
    pid = next((int(t) for t in proc.stderr.replace("=", " ").split() if t.isdigit()), None)

    # Long enough for the child to connect and sync over BLE.
    time.sleep(50)
    text = log.read_text(encoding="utf-8") if log.exists() else ""

    problems = []
    if proc.returncode != 0:
        problems.append(f"parent rc={proc.returncode}")
    if parent_took > 10:
        problems.append(f"parent blocked for {parent_took:.1f}s")
    if pid is None:
        problems.append("no pid printed")
    if "設定同步完成" not in text:
        problems.append("child never synced")

    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            problems.append("child died")
    if alive:
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 25
        stopped = False
        while time.time() < deadline:
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except OSError:
                stopped = True
                break
        if not stopped:
            os.kill(pid, signal.SIGKILL)
            problems.append("needed SIGKILL")
    log.unlink(missing_ok=True)
    record(
        f"{program} --daemon",
        not problems,
        "; ".join(problems)
        or f"parent {parent_took:.1f}s rc=0, pid {pid} synced, stopped on SIGTERM",
    )


# How long to give one candidate node to reach config sync before moving on.
# A BLE connect takes ~25-30s when it works at all.
PROBE_SECONDS = 45


def scan_names():
    print("scanning for BLE nodes (~10s)...", flush=True)
    sys.path.insert(0, str(HERE))
    import meshtastic.ble_interface

    try:
        devices = meshtastic.ble_interface.BLEInterface.scan()
    except Exception as exc:  # noqa: BLE001
        print(f"scan failed: {exc}", file=sys.stderr)
        return []
    for device in devices:
        print(f"  found {device.name}")
    return [device.name for device in devices]


def connects(name):
    """Whether `name` actually reaches config sync.

    Advertising is not the same as accepting a connection: a node already paired
    to a phone shows up in a scan and then refuses. Without this check the rest
    of the run reports four failures that say nothing about the code.
    """
    proc = subprocess.Popen(
        [PY, "bot_server.py", "--ble", name, "--heartbeat", "0"],
        cwd=HERE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + PROBE_SECONDS
    seen = []
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            line = proc.stdout.readline()
            if not line:
                break
            seen.append(line)
            if "設定同步完成" in line:
                return True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    tail = "".join(seen).strip().splitlines()
    print(f"    {name} did not connect: {tail[-1][:90] if tail else 'no output'}")
    return False


def find_node():
    """The first scanned node that will actually talk to us."""
    for name in scan_names():
        print(f"  probing {name} (up to {PROBE_SECONDS}s)...", flush=True)
        if connects(name):
            return name
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--node", help="BLE node name; scanned for when omitted")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="only the cases that need no connection (fast, no hardware)",
    )
    parser.add_argument(
        "--mqtt-live",
        action="store_true",
        help="also bridge the node to its real broker for one connected run. "
        "Off by default: it republishes this mesh to whatever address the "
        "device names, which is not a side effect a test should have.",
    )
    args = parser.parse_args()

    node = None
    if not args.quick:
        node = args.node or find_node()
        if node is None:
            print(
                "no BLE node would accept a connection - rerun with --quick, or "
                "check the node is advertising and not already connected to a "
                "phone (a scan finds it either way; only one of those works)",
                file=sys.stderr,
            )
            return 2
        print(f"using {node}\n")

    rules = HERE / "rules.txt"
    backup = HERE / ".rules.txt.params-live-backup"
    had_rules = rules.exists()
    if had_rules:
        shutil.copy(rules, backup)
    rules.write_text(
        "# emptied by test_params_live.py - nothing here may transmit\n", encoding="utf-8"
    )

    try:
        print("=== --help ===")
        for program, flags in (
            (
                "bot.py",
                ["--host", "--port", "--ble", "--list", "--server", "--daemon", "--log", "--heartbeat", "--wifi", "--mqtt"],
            ),
            ("bot_server.py", ["--host", "--port", "--ble", "--list", "--daemon", "--log", "--heartbeat", "--mqtt"]),
        ):
            run([program, "--help"], 120, expect_rc=0, expect_out=flags, name=f"{program} --help")

        print("\n=== arguments that must be rejected ===")
        run(["bot.py", "--wifi", "on"], 120, expect_rc=2,
            expect_out=["--wifi needs a target"], name="bot.py --wifi with no target")
        run(["bot.py", "--daemon", "--port", "/dev/null"], 120, expect_rc=2,
            name="bot.py --daemon without --server")
        run(["bot.py", "--mqtt", "--port", "/dev/null"], 120, expect_rc=2,
            name="bot.py --mqtt without --server")
        run(["bot.py", "--here", "not-a-coord"], 120, expect_rc=2, name="--here nonsense")
        run(["bot_server.py", "--here", "999,999"], 120, expect_rc=2, name="--here out of range")
        run(["bot_server.py", "--ble", "X", "--heartbeat", "abc"], 120, expect_rc=2,
            name="--heartbeat not a number")
        run(["bot_server.py", "--nonsense"], 120, expect_rc=2, name="unknown flag")
        run(["bot_server.py", "--server"], 120, expect_rc=2,
            name="bot_server.py has no --server")

        print("\n=== unreachable targets must fail cleanly, not hang ===")
        run(["bot_server.py", "--port", "/dev/cu.does-not-exist"], 300, expect_rc=1,
            expect_out=["連線失敗"], expect_absent=["Traceback"],
            name="bot_server.py --port that does not exist")
        run(["bot.py", "--server", "--port", "/dev/cu.does-not-exist"], 300, expect_rc=1,
            expect_out=["連線失敗"], expect_absent=["Traceback"],
            name="bot.py --server --port that does not exist")
        # --mqtt must not change how a failed connect ends: the bridge only
        # starts at config sync, so this never reaches a broker. Which of the
        # two endings is correct depends on the interpreter running this file,
        # and both are worth pinning - the whole point of checking paho up
        # front is that it fails here rather than half a minute into a BLE
        # connect, in the background, where nobody sees it.
        if HAVE_PAHO:
            run(["bot_server.py", "--mqtt", "--port", "/dev/cu.does-not-exist"], 300, expect_rc=1,
                expect_out=["連線失敗"], expect_absent=["Traceback", "MQTT"],
                name="bot_server.py --mqtt --port that does not exist")
        else:
            run(["bot_server.py", "--mqtt", "--port", "/dev/cu.does-not-exist"], 300, expect_rc=2,
                expect_out=["paho-mqtt"], expect_absent=["Traceback", "連線"],
                name="bot_server.py --mqtt without paho installed")

        print("\n=== --list ===")
        for program in ("bot_server.py", "bot.py"):
            run([program, "--list"], 300, expect_rc=0,
                expect_out=["BLE 節點", "USB serial"], name=f"{program} --list")

        if args.quick:
            print("\n(--quick: skipping the connected cases)")
        else:
            print(f"\n=== connected to {node} ===")
            hold(["bot_server.py", "--ble", node, "--heartbeat", "20"],
                 "bot_server.py --ble", expect_out=["已連線", "設定同步完成", "[心跳]"],
                 key="bot_server.py")
            hold(["bot.py", "--server", "--ble", node, "--heartbeat", "20"],
                 "bot.py --server --ble", expect_out=["已連線", "設定同步完成", "[心跳]"],
                 key="bot.py --server")
            if args.mqtt_live:
                # The only case here that talks to anything but the node, and
                # the only one that puts this mesh on a public broker - hence
                # the separate flag. "MQTT 已連線" rather than just "MQTT":
                # every refusal line starts with MQTT too.
                hold(["bot_server.py", "--ble", node, "--mqtt", "--heartbeat", "20"],
                     "bot_server.py --ble --mqtt",
                     expect_out=["設定同步完成", "MQTT 橋接啟動", "MQTT 已連線", "[心跳] "],
                     key="bot_server.py --mqtt")
            else:
                print("  (--mqtt: skipped, needs --mqtt-live)")
            # "已連線" as well as the pane title: a TUI whose connection failed
            # stays up showing the device list, and its RSS would then be
            # reported as a connected figure when it is nothing of the kind.
            hold(["bot.py", "--ble", node, "--here", "25.0339,121.5645"],
                 "bot.py --ble --here (TUI)", tui=True,
                 expect_out=["裝置", "已連線"], key="bot.py (TUI)")

            print("\n=== --daemon ===")
            daemon_case("bot_server.py", [], node)
            daemon_case("bot.py", ["--server"], node)
    finally:
        if had_rules:
            shutil.copy(backup, rules)
            backup.unlink()
        else:
            rules.unlink(missing_ok=True)
        print("\nrules.txt restored")

    failed = [name for name, ok, _ in results if not ok]
    print("\n" + "=" * 62)
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    for name in failed:
        print(f"  FAILED: {name}")
    if memory:
        print("\npeak RSS (MiB):")
        for key, value in memory.items():
            print(f"  {key:28} {value / 1024:.0f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
