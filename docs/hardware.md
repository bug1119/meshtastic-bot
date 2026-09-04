# 硬體與韌體層面

這一份是**裝置**那一側的行為,不是 bot 的行為 —— 三種連線方式在硬體上的差別、
韌體的認證機制,以及 MQTT 為什麼在某些板子上會安靜地失敗。

bot 本身的用法在 [README](../README.md);Heltec V4 TFT 的韌體修改在
[docs/firmware](firmware/README.md)。

---

## 三種傳輸的取捨

| | 優點 | 限制 |
|---|---|---|
| **BLE** | 有配對 PIN | 一次只能一個 client。**在 MUI/TFT 機種上開藍牙會讓螢幕進 programming mode**,等於失去畫面 |
| **TCP** | 跨房間、不用線 | **沒有加密、沒有密碼**(見下方安全性)。韌體一次只收一條 TCP 連線,新連線會踢掉舊的 |
| **serial** | 最可靠,WiFi/藍牙都關掉時唯一的路 | 要接線 |

## ⚠️ 安全性:TCP 連線沒有加密也沒有密碼

這點值得單獨說明,因為它不直觀:

- **傳輸沒有加密** —— Meshtastic 的 socket API 是明文的 protobuf,整條路徑沒有 TLS
- **沒有任何認證** —— 韌體裡有 per-connection 認證機制(`MESHTASTIC_PHONEAPI_ACCESS_CONTROL`),但在 MUI/TFT 機種上**編譯不進去**(與 MUI 必需的 `USE_PACKET_API` 互斥)
- **本機連線等於完整管理權** —— 任何能連到 `<node>:4403` 的人,都能讀全部訊息與設定、改設定、以你的身分發訊息
- 而且 mDNS 會主動廣播節點,不用掃描就找得到

**仍然受保護的是 LoRa 空中傳輸本身**(頻道 PSK、DM 用 PKC)。API 這條連線是那層加密**之外**的明文視窗。

建議:節點放在可信的 LAN,**不要把 4403 對外 port forward**,不用的時候用 `--wifi off` 關掉。

### WiFi 開關

```sh
./bot.py --port /dev/cu.usbmodem2101 --wifi off         # 關
./bot.py --port /dev/cu.usbmodem2101 --wifi on          # 開
```

**WiFi 關掉之後只有 USB 或裝置螢幕能開回來** —— 因為開 WiFi 這個指令沒辦法透過 WiFi 下,而 MUI 機種的藍牙預設是關的。裝置螢幕上的方式是 **home 畫面的 WLAN 按鈕長按**(短按無效)。

用 `--host` 關 WiFi 會先警告,因為那會切斷你自己正在用的連線。兩種方式都會讓裝置重開機才生效。

## 換算到實際裝置

| 裝置 | 怎麼連 | `proxy_to_client_enabled` | `--mqtt` |
|---|---|---|---|
| **GAT562 30S**(nRF52840) | 藍牙 / USB | **必須開** | **必須加** |
| Heltec V4(ESP32-S3) | 藍牙 | 必須開 | 必須加 |
| Heltec V4 | WiFi(`--host`),或 USB 而 WiFi 開著 | **關著** | **不用加** |
| Heltec V4 | WiFi 開著但節點連不到 broker | 開 | 加(刻意繞經這台機器) |

**nRF52 這類裝置沒有選擇。** `HAS_WIFI 1` 只出現在 `src/platform/esp32/` 和 `src/platform/portduino/`,nRF52 沒有定義,所以 `HAS_NETWORKING` 是 0 —— 而 `MQTT::publish()` 的直連那支是包在 `#if HAS_NETWORKING` 裡的,在這塊板子上**根本沒被編進去**。proxy 沒開就是 `return false`,不存在第二條路。

這跟 Heltec V4 不一樣:Heltec 是「走藍牙所以**現在**沒網路」(執行期的結果),nRF52 是「這塊板子**永遠**不可能有網路」(編譯期就決定)。

**而 nRF52 會講。** 正因為它的 `HAS_NETWORKING` 是 0,`MQTT::isValidConfig()` 的 `#else` 分支**有**編進去,所以設定錯了會拿到 `LOG_ERROR` 加一則 ERROR 級的 ClientNotification,直接推給連著的 client:

> `Invalid MQTT config: proxy_to_client_enabled must be enabled on nodes that do not have a network`

這句診斷**ESP32 看不到、nRF52 看得到** —— 正好跟直覺相反,原因在後面的〈[為什麼韌體不會警告你](#為什麼韌體不會警告你)〉。

（順帶一提,`MESHTASTIC_EXCLUDE_MQTT` 只掛在 `MESHTASTIC_MINIMIZE_BUILD` 底下,而那行預設是註解掉的,所以 nRF52 的標準 build **有**帶 MQTT 模組。跑第三方 fork 的話值得自己確認一次。)

## 為什麼韌體不會警告你

節點沒有網路、proxy 又關著的時候,MQTT **連嘗試都不會嘗試**(`src/mqtt/MQTT.cpp`):

```c
bool wantsLink()
{
    return hasChannelorMapReport &&
           (moduleConfig.mqtt.proxy_to_client_enabled || isConnectedToNetwork());
}
```

封包則落到 `else` 那支,進一個深度 16 的佇列,滿了就丟最舊的:

```
LOG_INFO("MQTT not connected, queue packet");
LOG_WARN("MQTT queue is full, discard oldest");
```

韌體其實**有**一句話正是為這個情況寫的:

> `Invalid MQTT config: proxy_to_client_enabled must be enabled on nodes that do not have a network`

**但有 WiFi 硬體的板子看不到它。** 它在 `#if HAS_NETWORKING` 的 `#else` 分支裡,而 `HAS_NETWORKING` 是 `HAS_WIFI || HAS_ETHERNET`(`src/configuration.h`)—— **編譯期**的板子能力,不是執行期「現在有沒有連上」。Heltec V4 有 WiFi 硬體,所以那句錯誤根本沒被編進去;走的是另一支,它只在 `isConnectedToNetwork()` 為真時去測 broker 通不通,而走 BLE 時這個是假,於是**什麼都不做,設定照存,零錯誤零警告**。

也就是說:那句診斷只給沒有 WiFi 硬體的板子看,而 ESP32 全系列都有。設定看起來完全正常、訊息就是不會出去,也沒有任何一行 log 說為什麼 —— 這是設定 MQTT 時最花時間的一個坑。

