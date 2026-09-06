# meshtastic-firmware 這一側的四個 patch

`~/git/meshtastic-firmware` 的 `build/cjk-crashfix` 分支,相對於 `origin/develop`。
**這四個 commit 沒有推到任何遠端** —— 那個 repo 只有官方的 `origin`,而這些是本地改動,所以這裡是它們唯一的機器外副本。

共 **187 行新增、36 行刪除、13 個檔案**,26 KB。

| Patch | 內容 | 是誰的 |
|---|---|---|
| `0001` | System 畫面把 Ver 和 Up 併成一行 | 自製功能 |
| `0002` | 讓手機 app 在 MUI 機種上能透過 WiFi 連 | 自製功能 |
| `0003` | build 設定:指向本地 CJK device-ui、stock 分區 | **不要送上游** |
| `0004` | 重播鈴聲前先放掉蜂鳴器腳位;版本 tag | bug 修正 |

## 套用

```sh
git clone https://github.com/meshtastic/firmware.git && cd firmware
git checkout develop
git am /path/to/patches/firmware/*.patch
```

⚠️ **`0003` 只在原本那台機器上有意義。** 它把 `platformio.ini` 的 device-ui 依賴改成絕對路徑 symlink:

```ini
symlink:///Users/josephyu/git/device-ui/.claude/worktrees/cjk-only
```

**那個路徑刻意保留原樣沒有改成 `<you>`** —— 這些 patch 存在的目的是還原那個 build,改掉就套不回去了。(這個 repo 的 commit 歷史本來就帶著同一個使用者名稱。)

換機器的話就把那行改成你自己的 device-ui 路徑,或還原成註解裡的官方 zip 依賴。

## `0004` 兩件事一起

一個 commit 裡有兩樣東西,因為它們是同一輪工作:

**蜂鳴器** —— `NonBlockingRtttl::begin()` 呼叫的 `toneSetup()` 是無條件 `ledcAttach`,而核心對已經 attach 的腳直接拒絕。呼叫端以 25ms 間隔重試,結果一對錯誤佔滿整個 log(12 分鐘抓取裡 1128 行全是它)。修法是重播前先 `ledcDetach`。實機驗證:錯誤 1128 → **0**。

**版本 tag** —— `MESHTASTIC_BUILD_TAG` 讓同一個 commit 的兩份 build(有/無中文字型)報出不同版本字串。`firmware_version` 是 `char[18]`,所以 tag 上限 3 字元,超過會**明確報錯而不是被截斷**;而那個檢查刻意放在 `readProps()` 的 `try` 外面 —— 裡面那個裸的 `except:` 會吞掉它,結果會編出一個版本字串錯誤的韌體。

## 相關

- device-ui 的五個 crash 修正:[`../`](../)
- 中文字型的重建配方:[`../../fonts/`](../../fonts/)
- 來龍去脈:[`../../README.md`](../../README.md)
