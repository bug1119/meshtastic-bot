#!/usr/bin/env bash
# Rebuild the Traditional Chinese fonts for the Heltec V4 TFT build.
#
# The four generated .c files are 16.4 MB and 352,135 lines. This script plus
# codepoints.txt is 30 KB and produces them exactly - lv_font_conv writes the
# options it was given into the header of what it produces, so the recipe was
# recoverable from the output itself.
#
#   ./regenerate.sh /path/to/device-ui/generated/ui_240x320
#
# Needs lv_font_conv (npm i -g lv_font_conv) and NotoSansTC-Regular.otf, which
# is a Google font under the SIL Open Font License - fonts.google.com/noto/
# specimen/NotoSansTC, or the fonts-noto-cjk package on most distributions.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-.}"
font="${FONT:-NotoSansTC-Regular.otf}"

if ! command -v lv_font_conv >/dev/null; then
    echo "lv_font_conv not found: npm i -g lv_font_conv" >&2
    exit 1
fi
if [ ! -f "$font" ]; then
    echo "$font not found - set FONT=/path/to/NotoSansTC-Regular.otf" >&2
    exit 1
fi

# One list, four sizes: the four fonts cover the same 4827 codepoints, which is
# why only one copy is stored. Verified when the list was extracted.
range="$(paste -sd, - < "$here/codepoints.txt")"

mkdir -p "$out"
for size in 12 14 16 20; do
    name="ui_font_noto_sans_tc_${size}"
    echo "  $name ($size px)"
    lv_font_conv \
        --bpp 4 \
        --size "$size" \
        --no-compress \
        --font "$font" \
        --range "$range" \
        --format lvgl \
        --lv-font-name "$name" \
        -o "$out/$name.c"
done

echo
echo "wrote 4 fonts to $out"
echo "Byte-identical output is not guaranteed across lv_font_conv versions;"
echo "what matters is that the glyph set and metrics match."
