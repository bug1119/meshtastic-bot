# Heltec V4 TFT 韌體:四個上游 device-ui bug 與修法

這台節點(`BUG1119` / `!1d7e2212`,Heltec V4 TFT)曾經**每 55 秒重開一次**,`rebootCount` 一個下午從 198 爬到 267。原因是 `meshtastic/device-ui` 裡的四個 bug —— **全部都是上游的**,不是本地改壞的。

這份文件記錄:每個 bug 的症狀、解出來的 backtrace、真正的成因、修法,以及**怎麼自己解 backtrace**(這是整件事裡最值得帶走的一招)。

---

## 最重要的一件事:先解 backtrace

我在解位址之前**猜錯了兩輪**,浪費的時間遠超過解位址需要的 30 秒。

ESP32 panic 會印出這樣一串:

```
Guru Meditation Error: Core 0 panic'ed (StoreProhibited). Exception was unhandled.
Backtrace: 0x4201e3e3:0x3fcc7440 0x420436ff:0x3fcc7460 0x4207b421:0x3fcc7480 ...
```

那些位址不用猜,可以直接查:

```sh
ELF=~/git/meshtastic-firmware/.claude/worktrees/cjk-fix/.pio/build/heltec-v4-tft/firmware-*.elf
A2L=~/.platformio/packages/toolchain-xtensa-esp32s3/bin/xtensa-esp32s3-elf-addr2line

$A2L -pfiaC -e $ELF 0x4201e3e3 0x420436ff 0x4207b421 0x4207ebdd
```

輸出:

```
0x4201e3e3: lv_obj_class_create_obj
0x420436ff: lv_image_create
0x4207b421: TFTView_320x240::addOrUpdateMap(unsigned long, long, long)
0x4207ebdd: TFTView_320x240::updatePosition(...)
```

**ELF 必須是刷進裝置的那一份**,版本要對得上 `--info` 回報的 `firmwareVersion`。`.pio/build/` 會同時留著多個版本的 ELF,挑對的那個。

`-pfiaC` 分別是:一行一筆、印函式名、展開 inline、印位址、C++ 名稱還原。

沒有對應的 ELF 就重編一次同一棵樹 —— 位址是可重現的。

---

## Bug 1:`addOrUpdateMap` 在不存在的地圖容器上建圖片

**症狀** 每 55 秒 `StoreProhibited`,而且是自我維持的迴圈。

**Backtrace**

```
lv_obj_class_create_obj
lv_image_create
TFTView_320x240::addOrUpdateMap
TFTView_320x240::updatePosition
ViewController::packetReceived
ViewController::receive
```

**成因** `source/graphics/TFT/TFTView_320x240.cpp`:

```c
lv_obj_t *img = lv_image_create(objects.raw_map_panel);   // parent 是 null
```

地圖畫面是**延遲建立**的 —— 沒開過地圖,`raw_map_panel` 和 `map` 都是 null。但 position 封包照樣進來,而 LVGL 對 parent 不做檢查就解參考。

同一個函式下面**已經有兩處 `if (map)` 檢查**,寫的人知道地圖可能不存在;那些檢查只是跑在崩掉的那一行之後。

**為什麼會變成迴圈** 重開機後地圖必然是「沒開過」的狀態,而這片 mesh 有 100+ 個節點在廣播位置,所以開機後幾十秒內必然湊齊條件。崩了又重開,又回到起點。

**修法** 在函式開頭同時守住 `map` 和它的容器:

```c
if (!map || !objects.raw_map_panel) {
    return;
}
```

**取捨** 第一次打開地圖之前,新節點不會建立標記 —— 該節點下次廣播位置時才會出現。比每分鐘重開好得多。

---

## Bug 2:`initPNGDecoder` 把 PNG 物件放進「只能 32 bit 存取」的記憶體

**症狀** **一按地圖按鈕就 reboot。**

**Backtrace**

```
initPNGDecoder()
TFTView_320x240::loadMap()
TFTView_320x240::ui_event_MapButton()
lv_event_send / indev_proc_release
```

**成因** `source/util/PNGdecoder.cpp`(上游 commit `bcb327f`,PR #353):

```c
// force allocation into zero-wait-state Internal SRAM (aligned to 32-bit words)
pngBuffer = heap_caps_malloc(sizeof(PNG), MALLOC_CAP_INTERNAL | MALLOC_CAP_32BIT);
...
png = ::new (pngBuffer) PNG();     // ← StoreProhibited
```

**`MALLOC_CAP_32BIT` 的意思不是「對齊到 32 bit」,而是「這塊記憶體只能用 32 bit 存取」。** 在 ESP32-S3 上它可能回傳 IRAM,而對 IRAM 做**任何位元組寫入都會 fault**。`PNG()` 建構子只要有 byte 寫入就炸。

註解寫的意圖(對齊)跟用的 flag 的實際語意(存取寬度限制)不是同一件事。

**修法** 換成 `MALLOC_CAP_8BIT` —— 那才是「位元組可定址的內部 SRAM」,而且它的配置本來就是字組對齊的:

```c
pngBuffer = heap_caps_malloc(sizeof(PNG), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
```

這個 bug 影響**任何會打開地圖畫面的 TFT 裝置**,不限這一台。

---

## Bug 3:訊息 log 上限對這個裝置太大

**症狀** 兩個:每次開機花 **38.7 秒**還原訊息;以及還原到後段時 `StoreProhibited`。

**Backtrace**(還原時崩的那個)

```
lv_obj_class_create_obj
lv_obj_create
TFTView_320x240::newMessage
TFTView_320x240::restoreMessage
ViewController::restoreTextMessages
```

**成因** `LogRotate` 預設 100kB / 25 檔。實測:

```
58331 bytes 存了 788 則訊息  →  每則平均 74 bytes
100kB 的預設值 → 約 1400 則
```

太多的原因有兩個:

1. **還原是「一個 UI tick 一則」**(`ViewController::runOnce` → `restoreTextMessages`),788 則就是 38.7 秒。
2. **每則訊息都會建立 LVGL 物件**,788 則把 LVGL 的 heap 用盡 → `newMessageContainer()` 回傳 null → `lv_obj_create(null)` 崩掉。

**這也解釋了為什麼它是「逐漸」壞掉的:** 訊息 log **跨重開機累積,而且不會自己縮小**,所以是慢慢爬到上限的,不是突然壞。

**修法** 在呼叫端(而非函式庫預設值)把上限調小 —— 這是使用者的政策選擇:

```c
// source/graphics/common/ViewController.cpp
log(persistentFS, logDir, sizeof(LogMessage), 16384, 8, 2048)
//                                            ↑總量  ↑檔數 ↑單檔
```

**換算**:想要 N 則就設 `N × 74` bytes,並讓 `maxFiles × maxFileSize` 等於同一個數。

| 想要 | maxSize | maxFiles | maxFileSize |
|---|---|---|---|
| ~100 則 | 7400 | 8 | 925 |
| ~220 則(目前) | 16384 | 8 | 2048 |
| ~400 則 | 29600 | 12 | 2467 |

⚠️ `maxFileSize` 別低於約 1000 —— `init()` 會比 `currentSize > maxFileSize - maxLen`,而 `maxLen = sizeof(LogMessage)` 約 250 bytes。

**實際容量會在 190–220 則之間來回**,因為修剪是**整檔刪除**的(一次砍掉最舊的約 28 則),不是一則一則刪。

---

## Bug 4:`LogRotate::init` 不會套用調小後的上限

**症狀** 上限改成 16kB 之後,裝置回報 **369%** 卻什麼都沒刪:

```
LogRotate: found 16 log files using 60564 bytes (369%).
Logging to /messages/log_000016.log
```

(`60564 ÷ 16384 = 369%` —— 新上限確實生效了,只是沒修剪。)

**成因** `source/util/LogRotate.cpp`,修剪被包在一個條件裡:

```c
if (currentSize > c_maxFileSize - c_maxLen) {
    while ((numFiles > c_maxFiles || totalSize >= c_maxSize) && removeLog())
        ;
    ...
}
```

`currentSize` 是**當前檔案**的大小。剛才它新建了 `log_000016.log`,所以 `currentSize` 很小、條件不成立 → 不修剪。要等到有足夠流量把那個檔案填滿、`write()` 觸發輪替才會縮。

**修法** 把修剪搬到條件外面,每次開機都套用上限:

```c
while ((numFiles > c_maxFiles || totalSize >= c_maxSize) && removeLog())
    ;

if (currentSize > c_maxFileSize - c_maxLen) {
    // 原本的輪替邏輯
}
```

迴圈和結束條件都是原本就有的(`write()` 也用同一組),只是拿掉了外面那層 guard。

---

## 修完的量測結果

| | 修正前 | 修正後 |
|---|---|---|
| panic 頻率 | 每 55 秒 | **0**(240 秒觀測窗) |
| 開機還原 | 788 則 / 38.7 秒 | **176 則 / 19.6 秒** |
| 訊息上限 | 100kB / 約 1400 則 | 16kB / 約 220 則 |
| 開地圖 | 立刻 reboot | 正常 |
| `rebootCount` | 198 → 309(一天) | 穩定 |

驗證方式:240 秒的 serial 抓取,期間收到 **137 個 position 封包**(這正是以前每次都炸掉的觸發條件)而沒有任何 panic。

---

## 怎麼重現這個 build

**它只在原本那台機器上編得起來** —— `platformio.ini` 用絕對路徑 symlink 指向本地的 device-ui。

```
~/git/device-ui        分支 feat/cjk-only     ← 四個修正 + 中文字型
~/git/meshtastic-firmware  分支 build/cjk-crashfix  ← build 設定
```

firmware 的 `platformio.ini`:

```ini
[device-ui_base]
lib_deps =
	symlink:///Users/<you>/git/device-ui/.claude/worktrees/cjk-only
	# 要回到官方版就還原這行:
	# https://github.com/meshtastic/device-ui/archive/<sha>.zip
```

編與刷:

```sh
cd ~/git/meshtastic-firmware/.claude/worktrees/cjk-fix
pio run -e heltec-v4-tft            # 增量約 75 秒
pio run -e heltec-v4-tft -t upload  # 約 42 秒,6.26 MB
```

Flash 用到 **95.5%**(6,260,075 / 6,553,600 bytes)—— 中文字型很吃空間,實務上塞不進第二份 OTA 映像。

---

## 兩個設定上的陷阱

**1. `meshtastic --set` 會吃掉純數字開頭的 `0`**

```sh
meshtastic --set network.wifi_psk 0123456789   # 存進去變成 123456789
```

CLI 對純數字做型別轉換。症狀是 WiFi 關聯得上但卡在 `Reason: 15 - 4WAY_HANDSHAKE_TIMEOUT`(密碼錯誤的特徵)。任何以 `0` 開頭的純數字設定都要改用 Python API:

```python
node.localConfig.network.wifi_psk = "0123456789"   # 明確字串
node.writeConfig("network")
```

寫完一定用 `--info` 讀回來比對長度。

**2. WiFi 與藍牙在 ESP32 上互斥,而 WiFi 贏**

`src/platform/esp32/main-esp32.cpp:107`:

```c
if (!isWifiAvailable() && config.bluetooth.enabled == true) {
    nimbleBluetooth->setup();      // 只有 WiFi 不可用時才啟動 BLE
}
```

WiFi 開著,藍牙**永遠不會啟動**,而且 ESP32 會直接釋放 BT 記憶體(`Released BTDM memory`),**不可逆,要重開機才能反轉**。

連帶影響 MQTT:走藍牙就等於節點沒有自己的網路,MQTT 只能靠 `proxy_to_client_enabled`,而那需要**連著的 client 真的去做 broker 連線**(手機 app 有內建;`bot_server.py` 沒有)。

另外 **device-ui 的畫面上有藍牙開關,會蓋掉 CLI 的設定** —— 要開藍牙請從螢幕上按。

---

## 還沒解決的

- **這四個修正沒有回饋上游。** 全都是 `meshtastic/device-ui` 的問題,別的 TFT 使用者遲早會踩到,尤其 Bug 2(開地圖必炸)。
- **`updatePosition` 附近還有約兩打同樣的 `nodes[...]` 寫法** —— `operator[]` 對 `unordered_map` 會插入 null 再被解參考。目前會炸的那個已經擋掉,其餘沒動:一次改 24 處是另一件工程。
- **長時間穩定性只觀測到 240 秒。** 需要跑數小時再對一次 `rebootCount` 才算真的確認。

---

## 我在這件事上錯了兩輪,值得記著

| 輪次 | 做法 | 結果 |
|---|---|---|
| 1 | 憑「PC 位址相近 + 崩在 position 封包後 + 程式碼形狀」推論成因 | ❌ 猜的那一行確實有同樣毛病,但不是崩的原因 |
| 2 | 關掉 `displaymode` 想繞過,然後宣稱「沒效」 | ⚠️ 沒等夠久就下結論,還白拿掉了畫面 |
| 3 | **解 backtrace** | ✅ 一次就中 |

第一輪的錯不在推論方向(觸發路徑其實猜對了),而在**沒有把位址解成函式名就動手改**。那一步只要 30 秒,而前兩輪加起來浪費的時間遠超過它。

**下次遇到 ESP32 panic:先 addr2line,再想。**
