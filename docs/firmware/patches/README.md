# 五個 device-ui patch

`git format-patch` 產出的完整 patch,含 commit message 裡的推理。成因與量測見[上一層的 README](../README.md)。

## 套用

基底是 `meshtastic/device-ui` 的 **`e1de01e`**(`feat: async loading of remote map tiles in background task`,#367)。

```sh
git clone https://github.com/meshtastic/device-ui.git
cd device-ui
git checkout e1de01e
git am /path/to/patches/*.patch
```

`git am` 會保留 commit message 和作者資訊。只想要程式碼改動就用 `git apply`。

**五個都驗證過能乾淨套用在 `e1de01e` 上**,而且彼此獨立 —— 可以只挑要的。

## 內容

| Patch | 修什麼 | 症狀 |
|---|---|---|
| `0001` | `updatePosition` 對沒有面板的節點解參考 | 潛在;跟下面那個同款,但不是實際會炸的那個 |
| `0002` | `addOrUpdateMap` 在 null 容器上建圖片 | **每 55 秒 `StoreProhibited`**,自我維持的迴圈 |
| `0003` | `initPNGDecoder` 用了 `MALLOC_CAP_32BIT` | **一按地圖就 reboot**;影響任何開地圖的 TFT 裝置 |
| `0004` | 訊息 log 上限 100kB 太大 | 開機還原 38.7 秒 + LVGL heap 耗盡後 crash |
| `0005` | `LogRotate::init` 不套用調小後的上限 | 上限改小後顯示 369% 卻不修剪 |

`0002` 和 `0003` 是實際會讓裝置無法使用的兩個。`0004` 的數值(16kB / 8 檔 / 2048)是針對這台調的 —— 換算方式見上一層 README。

## 為什麼沒有中文字型的 patch

那個 commit 是 **352,135 行**,全是 `generated/ui_240x320/ui_font_noto_sans_tc_{12,14,16,20}.c` 裡由工具產生的字符資料。做成 patch 會是 10MB+ 的文字檔,而且它是**產物不是原始碼** —— 該版控的是產生它的步驟,不是產生出來的位元組。

它也跟這五個修正**完全獨立**:字型 commit 只動 `generated/` 底下的檔案,五個修正只動 `source/` 底下的,所以要中文顯示與要修 crash 是兩件可以分開做的事。

## 這五個都是上游的 bug

不是本地改壞的。`meshtastic/device-ui` 的其他 TFT 使用者遲早會踩到,尤其 `0003` —— 註解寫的意圖(對齊到 32 bit)跟 flag 的實際語意(只能 32 bit 存取)不是同一件事,而它在**任何人打開地圖時**都會炸。

**這些還沒回饋上游。** patch 放在這裡就是為了要發的時候有現成材料。
