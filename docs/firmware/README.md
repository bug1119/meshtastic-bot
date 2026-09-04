# Heltec V4 TFT 韌體:上游 bug 與修法

這台節點(`BUG1119` / `!1d7e2212`,Heltec V4 TFT)曾經**每 55 秒重開一次**,`rebootCount` 一個下午從 198 爬到 267。原因是 `meshtastic/device-ui` 裡的四個 bug —— **全部都是上游的**,不是本地改壞的。

後來又找到兩個,都不在 device-ui:一個是鈴聲每次重播都重複 attach 蜂鳴器那支腳,把 log 洗到滿(在 `meshtastic-firmware` 修掉了);一個是 device-ui 不認識 MQTT proxy 的封包,只是雜訊(沒修)。合計六個。

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

## 另外兩個

這兩個不在上面的四個裡,因為它們不在 device-ui 裡:一個在 `meshtastic-firmware`(已修),一個是 device-ui 的 log 雜訊(沒修)。兩個都沒有回饋上游。

### 鈴聲每次重播都重複 attach 同一支腳,每 25ms 噴一對錯誤

**症狀** 裝置看起來像壞掉但**其實沒有重開**。serial 上每 ~70ms 噴一對錯誤,而且不會停:

```
[E][esp32-hal-ledc.c:213] ledcAttachChannel(): Pin 6 is already attached to LEDC (channel 1, resolution 10)
[E][esp32-hal-ledc.c:328] ledcAttach(): No free timers available for freq=1000, resolution=10
```

實測 12 分鐘的抓取裡,**1128 行有 1128 行是這個** —— 也就是 100%。`[Router]`、MQTT 那些真正有用的記錄全被沖掉,所以第一眼會以為裝置死了。

**成因** —— 我第一次的判斷是錯的,而錯的方式很典型:我照著錯誤訊息的字面讀。

第二行寫「No free timers available」,所以我推論是 TFT 背光(`LGFX_PIN_BL=21`)先把 LEDC timer 佔走、蜂鳴器(`PIN_BUZZER=6`)拿不到。**那不是原因。** 讀了核心的實作才看清楚(`esp32-hal-ledc.c:211`):

```c
ledc_channel_handle_t *bus = perimanGetPinBus(pin, ESP32_BUS_TYPE_LEDC);
if (bus != NULL) {
    log_e("Pin %u is already attached to LEDC (channel %u, resolution %u)", ...);
    return false;                      // ← 對「已經 attach 的腳」直接拒絕
}
```

錯誤第一行自己就說了 **pin 6 已經 attach 成功、在 channel 1** —— 蜂鳴器**拿到了** channel。「No free timers」是 wrapper 在 per-pin 拒絕之後印的次要訊息,跟背光無關。

真正的缺陷在鈴聲函式庫(`NonBlockingRTTTL`):

```c
void toneSetup(uint8_t pin) {
  ledcAttach(pin, 1000, LEDC_RESOLUTION);   // 無條件 attach,freq=1000 resolution=10
}
```

`toneSetup()` 由 `begin()` 呼叫,而 `begin()` 每次重播都會呼叫它。這支腳在那之前**一定已經被佔住** —— `buzz.cpp` 的開機音效用 `tone()` 用過它,之後每次重播都是我們自己。所以每次 `begin()` 都固定噴那一對錯誤。

而 `ExternalNotificationModule::runOnce()` 的這一段:

```cpp
if (rtttl::isPlaying()) { rtttl::play(); }
else if (isNagging && !Throttle::deadlinePassed(nagCycleCutoff)) {
    rtttl::begin(config.device.buzzer_gpio, rtttlConfig.ringtone);
}
delay = EXT_NOTIFICATION_FAST_THREAD_MS;   // 25
```

歌曲沒在播的時候,這條分支就以 **25ms** 的間隔一直跑。所以一對錯誤變成一秒好幾對,而 nag 是每則訊息 15 秒 —— 這片 mesh 有 114 個節點在線,nag 幾乎不會結束。

**修法**(`src/modules/ExternalNotificationModule.cpp`):重播之前先放掉那支腳。

```cpp
} else if (isNagging && !Throttle::deadlinePassed(nagCycleCutoff)) {
    ledcDetach(config.device.buzzer_gpio);
    rtttl::begin(config.device.buzzer_gpio, rtttlConfig.ringtone);
}
```

`ledcDetach()` 對「沒有 attach 的腳」也會印一行,所以在完全沒人用過那支腳的情況下,開機時會多一行 —— 用一行換掉幾千行。

**附帶效果:蜂鳴器現在真的會響。** 之前 attach 被拒絕,音調其實根本沒送到腳上。如果你不想聽到聲音,關掉 external notification(下面那條指令)而不是靠這個 bug。

**設定層的繞法**(不想重刷韌體就用這個):

```sh
meshtastic --port /dev/cu.usbmodem2101 --set external_notification.enabled false
```

實測:`esp32-hal-ledc` 錯誤從佔滿 100% 的 log 降到 **0**,`[Router]` 記錄在 100 秒內回來 76 行,MQTT 橋接不受影響。裝置會重開一次才生效。

⚠️ **但那是設定,會被改回來。** 從裝置畫面或 app 把 external notification 開回去,舊韌體上迴圈就回來。韌體修過的版本不會。

### device-ui 不處理 `mqttClientProxyMessage`

**症狀** 開了 MQTT client proxy 之後,每一筆被代送的訊息都噴一行:

```
ERROR | [DeviceUI] unhandled fromRadio packet variant: 14
```

**成因** `FromRadio` 的欄位 14 就是 `mqttClientProxyMessage`(用 `mesh_pb2.FromRadio.DESCRIPTOR.fields_by_number` 查得到)。device-ui 跟著 radio 的 fromRadio 流走,遇到它不認識的 variant 就以 `LOG_ERROR` 記一筆。

**不會崩,只是雜訊** —— 但頻率跟 MQTT 上行一樣,在忙的 mesh 上就是持續的假錯誤。它也讓真正的錯誤更難被看到。

**修法** device-ui 對這個 variant 應該安靜地忽略(它本來就不是給 UI 的),而不是記成錯誤。沒有改。

---

## 要升級或重刷之前,先看這個

**patch 不能套在已經編好的韌體上。** 它們是 device-ui 的 source patch,一定要重編。

**刷任何官方版都會把 Bug 1 和 Bug 2 帶回來。** 這不是「舊版才有」的問題 —— 查過上游最新的 `9c97e42`(比這裡的基底多 23 個 commit):

| | 上游 `9c97e42` 的狀態 |
|---|---|
| `PNGdecoder.cpp` 的 `MALLOC_CAP_32BIT` | **還在** —— 開地圖仍會 reboot |
| `addOrUpdateMap` 的 null 容器 | **還在** —— 仍會每 55 秒 StoreProhibited |

中文字型當然也會一起消失。

**好消息:五個 patch 全部乾淨套用在上游最新版上**(逐一 `git apply --check` 驗過),所以要跟上游就是:

```sh
git clone https://github.com/meshtastic/device-ui.git && cd device-ui
git am /path/to/patches/*.patch
```

然後照〈[怎麼重現這個 build](#怎麼重現這個-build)〉重編。

⚠️ **中文字型是這裡最脆的一環。** 那個 commit 是 352,135 行工具產物,只存在本機的 `feat/cjk-only` 分支,**沒有機器外備份,產生它的流程也沒有記錄下來**。五個修正有 patch 備份,字型沒有。要重建就得重跑一次字型產生,而怎麼跑目前只在當時的記憶裡。


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

## Patch 檔

五個修正的完整 patch(含 commit message 裡的推理)在 **[`patches/`](patches/)**,`git format-patch` 產出,共 20KB。

全部驗證過能**乾淨套用在官方原版 `e1de01e` 上**,而且彼此獨立 —— 可以只挑要的:

```sh
git clone https://github.com/meshtastic/device-ui.git && cd device-ui
git checkout e1de01e
git am /path/to/patches/*.patch
```

中文字型那個 commit **沒有做成 patch** —— 它是 352,135 行由工具產生的字符資料,是產物不是原始碼。它跟這五個修正也完全獨立(只動 `generated/`,修正只動 `source/`)。

---

## 還沒解決的

- **六個都沒有回饋上游。** 前四個是 `meshtastic/device-ui` 的問題,別的 TFT 使用者遲早會踩到,尤其 Bug 2(開地圖必炸)—— 而且[上游最新版仍未修](#要升級或重刷之前先看這個)。patch 檔是現成的發 PR 材料。
- **蜂鳴器那個只用設定繞過,韌體沒改。** 從裝置畫面把 external notification 開回去,錯誤迴圈就回來。
- **中文字型沒有機器外備份。** 352,135 行的工具產物只在本機的 `feat/cjk-only`,產生流程也沒有記錄 —— 這是目前最脆的一環,比任何一個 bug 都值得先處理。
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
