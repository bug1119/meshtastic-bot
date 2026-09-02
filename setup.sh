#!/usr/bin/env bash
#
# Bootstrap this bot on a fresh machine (written for macOS; works on Linux too).
#
# uv is the ONLY prerequisite. bot.py's shebang is `uv run --script`, and the
# packages it needs are declared inline at the top of bot.py (PEP 723), so uv
# resolves and caches the environment itself - there is no virtualenv to create
# and nothing to activate. That is also why this script is short: it installs
# uv, warms the cache so the first real run is instant, and checks the one
# thing it cannot fix for you (uv being on the PATH of your interactive shell,
# which the shebang needs at run time).
#
# Safe to re-run: every step is a no-op once satisfied.

set -euo pipefail

cd "$(dirname "$0")"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$1" >&2; }

# ---------------------------------------------------------------- 1. install uv
#
# Homebrew is preferred when present, since a Mac that has brew is easier to
# keep updated that way. Otherwise fall back to Astral's standalone installer,
# which drops uv in ~/.local/bin and needs no admin rights.

say "檢查 uv"
if command -v uv >/dev/null 2>&1; then
	echo "已安裝: $(command -v uv) ($(uv --version))"
elif command -v brew >/dev/null 2>&1; then
	echo "用 Homebrew 安裝 uv..."
	brew install uv
else
	echo "沒有 Homebrew,改用 Astral 官方 installer(裝到 ~/.local/bin,不需要 sudo)..."
	curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# The standalone installer cannot change the PATH of this already-running
# shell, so pick the new binary up directly before using it below.
if ! command -v uv >/dev/null 2>&1; then
	for candidate in "$HOME/.local/bin" /opt/homebrew/bin /usr/local/bin; do
		if [ -x "$candidate/uv" ]; then
			export PATH="$candidate:$PATH"
			break
		fi
	done
fi

if ! command -v uv >/dev/null 2>&1; then
	warn "uv 裝完了卻找不到。請手動確認安裝位置後重跑這個 script。"
	exit 1
fi

UV_DIR="$(dirname "$(command -v uv)")"

# --------------------------------------------------------- 2. executable bits
#
# git tracks the exec bit, so a normal clone already has these. Restore them
# anyway for the cases that lose it: a zip download, a copy over a filesystem
# without the bit, or `git config core.fileMode false` on a shared volume.

say "確認執行權限"
chmod +x bot.py test_rules.py
echo "bot.py / test_rules.py 可執行"

# -------------------------------------------------------- 3. warm the uv cache
#
# --help exits before any device I/O, so this resolves and downloads the
# dependency set without touching Bluetooth, the network, or a serial port.
# Doing it now means the first real run does not stall on a cold cache.

say "預先備好套件環境(下載 + cache,第一次可能要一兩分鐘)"
uv run --script bot.py --help >/dev/null
echo "完成"

# ------------------------------------------------------------ 4. smoke test
#
# test_rules.py needs no hardware, so a green run here proves the whole
# toolchain works on this machine before you go looking for a node.

say "跑測試"
uv run --script test_rules.py | tail -1

# ------------------------------------------------------------------ 5. PATH
#
# This is the one thing that has to be checked rather than done. `./bot.py`
# works by handing the file to uv via the shebang, and that is resolved through
# the PATH of whatever shell you launch it from. If uv is not on that PATH,
# `./bot.py` fails with "env: uv: No such file or directory" even though this
# script just used uv successfully.
#
# Ask a fresh login shell rather than testing $PATH here: step 1 may have
# prepended the directory to this script's own PATH, which says nothing about
# what your normal shell sees.

say "檢查 PATH"
# Discard the login shell's own output: sourcing a profile commonly emits
# prompt/terminal-title escape sequences, which would otherwise be printed
# here as garbage in front of the next message.
if [ -n "${SHELL:-}" ] && "$SHELL" -lc 'command -v uv' >/dev/null 2>&1; then
	echo "登入 shell 找得到 uv,./bot.py 可以直接跑"
else
	warn "你的登入 shell 找不到 uv($UV_DIR 不在 PATH 上)。"
	warn "./bot.py 會噴 'env: uv: No such file or directory'。請加上這行:"
	case "$(basename "${SHELL:-zsh}")" in
	zsh) profile="~/.zshrc" ;;
	bash) profile="~/.bash_profile" ;;
	*) profile="你的 shell profile" ;;
	esac
	printf '\n    echo '"'"'export PATH="%s:$PATH"'"'"' >> %s\n\n' "$UV_DIR" "$profile"
	warn "加完後開一個新的終端機,或 source 一次該檔案。"
fi

say "可以開始用了"
cat <<'USAGE'
    ./bot.py                                # 掃描並連 BLE
    ./bot.py --host Meshtastic.local        # 走 WiFi (TCP 4403)
    ./bot.py --port /dev/cu.usbmodem2101    # 走 USB serial

第一次用 BLE 時 macOS 會跳藍牙權限請求 —— 要允許你的終端機程式(Terminal /
iTerm / VS Code),否則掃不到任何裝置。另外節點一次只接受一個 client,所以
先把 Meshtastic 手機／桌面 App 從該節點斷開。
USAGE
