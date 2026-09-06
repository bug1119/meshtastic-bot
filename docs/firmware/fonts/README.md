# 中文字型:配方,而不是產物

Heltec V4 TFT build 的繁體中文顯示,來自四個 LVGL 字型檔:

```
generated/ui_240x320/ui_font_noto_sans_tc_{12,14,16,20}.c
```

那四個檔**共 16.4 MB、352,135 行**,而且是工具產生的。這裡存的不是它們,是**產生它們的配方** —— 30 KB,小 567 倍,而且能完整重建。

| | 大小 |
|---|---|
| 產物(4 個 `.c`) | 16,448,122 B |
| 配方(`codepoints.txt` + 這份說明 + 腳本) | 約 30,000 B |

## 為什麼配方比產物值得存

**因為配方才回答得了「想改的時候怎麼辦」。** 有 352,135 行字符資料,你可以刷回去;但想多收幾個字、想加一個字級、想換 bpp,那些位元組幫不上忙。

而且**原始字型從來沒有遺失風險** —— `NotoSansTC-Regular.otf` 是 Google 的公開字型(SIL OFL),隨時下載得到。真正沒被記錄下來的一直都只是這幾個參數。

## 重建

```sh
npm i -g lv_font_conv
# NotoSansTC-Regular.otf 放在當前目錄,或用 FONT=/path/to/it
./regenerate.sh /path/to/device-ui/generated/ui_240x320
```

實際跑的指令(四個尺寸各一次):

```sh
lv_font_conv \
  --bpp 4 \
  --size 16 \
  --no-compress \
  --font NotoSansTC-Regular.otf \
  --range <codepoints.txt 的 4827 個字碼,逗號分隔> \
  --format lvgl \
  --lv-font-name ui_font_noto_sans_tc_16 \
  -o ui_font_noto_sans_tc_16.c
```

## 這些參數是怎麼查出來的

**`lv_font_conv` 會把它收到的參數寫進產物的檔頭。** 所以配方一直都在那 16 MB 裡面,只是沒有人去讀:

```c
/*******************************************************************************
 * Size: 16 px
 * Bpp: 4
 * Opts: --bpp 4 --size 16 --no-compress --font extracted/NotoSansTC-Regular.otf
        --range 19968,19969,19971,... --format lvgl
        --lv-font-name ui_font_noto_sans_tc_16 -o ui_font_noto_sans_tc_16.c
 ******************************************************************************/
```

一開始我以為「產生流程沒有記錄下來」是這裡最大的風險。實際去讀檔頭之後發現不是 —— **工具自己記著**。

## `codepoints.txt`

4827 個 Unicode 字碼,一行一個。

**四個尺寸用的是同一份清單**(抽取時驗證過完全相同),所以只存一份。`regenerate.sh` 會把它折成 `--range` 要的逗號格式。

清單本身是當初選的常用字集合,不是完整的 CJK 區塊 —— 全部收錄會讓 flash 爆掉(現在含字型已經是 95.5%)。

## 授權

字符資料衍生自 **Noto Sans TC**,授權是 **SIL Open Font License 1.1**。重新散布編譯出的韌體時要附上該授權 —— 這也是 `docs/firmware/README.md` 裡「無中文版」那份存在的理由之一:它沒有這個義務。

## 相關

- 五個 device-ui crash 修正的 patch:[`../patches/`](../patches/)
- firmware 端(build 設定、LEDC 修正、兩個自製功能)的 patch:[`../patches/firmware/`](../patches/firmware/)
- 整件事的來龍去脈:[`../README.md`](../README.md)
