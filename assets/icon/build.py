#!/usr/bin/env python3
"""ゆらぎ (yuragi-ime) アイコン生成スクリプト。

同じ寸法表から SVG (元データ) と .ico (複数サイズ) と確認用 preview.png を出す。
字の輪郭は src/glyph.json (手描きの「あ」を写真から取り出したもの) を読み、
全サイズ同じ輪郭を使う (手描きなので太さ違いは作らない)。
依存: Pillow のみ。  使い方: python3 build.py
"""
import io
import json
import re
import struct
from pathlib import Path
from PIL import Image, ImageDraw, ImageChops

OUT = Path(__file__).resolve().parent
SRC = OUT / "src"
INK = "#111111"      # 墨
PAPER = "#ffffff"    # 字
SIZES = [16, 20, 24, 32, 40, 48, 64, 256]
SS = 8               # スーパーサンプリング倍率
# 字の輪郭。手描きなので全サイズ同じ輪郭を使う。
GLYPH = json.loads((SRC / "glyph.json").read_text(encoding="utf-8"))
GLYPH_SMALL = GLYPH  # 16〜24px も同じ輪郭

# ---------------------------------------------------------------- 寸法表
# 図形: 手描きの字「あ」をそのまま置く。切らない、揺らさない、曲げない。
#   h  : 字の高さ      top : 字の上端 y      gx : 横の寄せ (0 で左右中央)
# 小サイズ (16/20/24) は画素格子に合わせた専用の寸法 (線がいちばんくっきり出る位置を選んである)。32 以上は 64 単位の原図を拡大縮小。
SMALL = {  # 灰色の画素がいちばん少ない位置を探した値
    "badge": {
        16: dict(h=14, top=1.375, gx=-0.375),
        20: dict(h=17, top=1.625, gx=0.25),
        24: dict(h=20.5, top=1.75, gx=0.375),
    },
    "bare": {
        16: dict(h=15.5, top=0.125, gx=0.25),
        20: dict(h=19, top=1.0, gx=0.375),
        24: dict(h=22.5, top=0.875, gx=0.375),
    },
}
MASTER = {  # 64 単位
    "badge": dict(h=52, top=6, gx=0),
    "bare": dict(h=60, top=2, gx=0),
}
BADGE_RADIUS = 0.22  # 角丸 (辺に対する比)

VARIANTS = {
    # 名前: (寸法の系統, 台座の色, 図の色)
    "yuragi": ("badge", INK, PAPER),        # 明暗両用 (既定)
    "yuragi-light": ("bare", None, INK),    # 明るいタスクバー専用
    "yuragi-dark": ("bare", None, PAPER),   # 暗いタスクバー専用
}


# ---------------------------------------------------------------- 字の輪郭
def pieces(glyph):
    """輪郭データ (M/L/Q/Z) を読む (字の単位のまま)。直線も 2 次ベジェとして持つ。

    返り値: 輪郭ごとの [始点, (制御点, 終点), (制御点, 終点), ...]
    """
    out, cur = [], []
    for cmd, args in re.findall(r"([MLQZ])([^MLQZ]*)", glyph["path"]):
        v = [float(t) for t in re.findall(r"-?[\d.]+", args)]
        if cmd == "M":
            cur, pos = [(v[0], v[1])], (v[0], v[1])
        elif cmd == "Z":
            out.append(cur)
        else:
            p2 = (v[-2], v[-1])
            p1 = (v[0], v[1]) if cmd == "Q" else ((pos[0] + p2[0]) / 2, (pos[1] + p2[1]) / 2)
            cur.append((p1, p2))
            pos = p2
    return out


PIECES = {id(GLYPH): pieces(GLYPH)}


def layout(kind, size):
    """size px のときの寸法 (px 単位) と、字の輪郭 (px 座標) を返す。字は拡大縮小と移動だけ。"""
    if size in SMALL[kind]:
        d, unit, g = SMALL[kind][size], size, GLYPH_SMALL
    else:
        d, unit, g = MASTER[kind], 64, GLYPH
    L = {n: v * size / unit for n, v in d.items()}
    L["size"] = size
    L["glyph"] = g
    k = L["h"] / g["height"]
    ox = size / 2 + L["gx"] - g["width"] * k / 2
    mv = lambda p: (p[0] * k + ox, p[1] * k + L["top"])
    L["shape"] = [[mv(c[0])] + [(mv(a), mv(b)) for a, b in c[1:]] for c in PIECES[id(g)]]
    return L


# ---------------------------------------------------------------- ラスタ
def glyph_mask(L):
    """字を塗った L 画像。輪郭どうしが重なっていてもよいよう、回転数を数えて塗る。"""
    s, n = L["size"], L["size"] * SS
    acc = Image.new("L", (n, n), 128)
    for c in L["shape"]:
        pts, p0 = [c[0]], c[0]
        for p1, p2 in c[1:]:
            pts += [tuple((1 - t) ** 2 * a + 2 * t * (1 - t) * b + t * t * e
                          for a, b, e in zip(p0, p1, p2)) for t in [i / 8 for i in range(1, 9)]]
            p0 = p2
        pts = [(x * SS, y * SS) for x, y in pts]
        area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))
        one = Image.new("L", (n, n), 0)
        ImageDraw.Draw(one).polygon(pts, fill=1)
        acc = (ImageChops.add if area > 0 else ImageChops.subtract)(acc, one)
    return acc.point(lambda v: 255 if v != 128 else 0).resize((s, s), Image.BOX)


def badge_mask(size):
    n = size * SS
    m = Image.new("L", (n, n), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, n - 1, n - 1], radius=BADGE_RADIUS * n, fill=255)
    return m.resize((size, size), Image.BOX)


def render(variant, size):
    kind, bg, fg = VARIANTS[variant]
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if bg:
        img = Image.composite(Image.new("RGBA", (size, size), bg), img, badge_mask(size))
    g = glyph_mask(layout(kind, size))
    return Image.composite(Image.new("RGBA", (size, size), fg), img, g) if bg else \
        Image.merge("RGBA", (*Image.new("RGB", (size, size), fg).split(), g))


# ---------------------------------------------------------------- ICO
def dib(img):
    """32bpp BMP (XOR) + 1bpp AND マスク。rc.exe / 旧ローダでも確実に読める形。"""
    w, h = img.size
    px = img.tobytes("raw", "BGRA")
    rows = [px[y * w * 4:(y + 1) * w * 4] for y in range(h)][::-1]
    alpha = img.getchannel("A")
    stride = ((w + 31) // 32) * 4
    mask = b""
    for y in range(h - 1, -1, -1):
        bits = bytearray(stride)
        for x in range(w):
            if alpha.getpixel((x, y)) == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bytes(bits)
    head = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(px) + len(mask), 0, 0, 0, 0)
    return head + b"".join(rows) + mask


def write_ico(path, images):
    blobs = []
    for im in images:
        if im.width >= 256:
            b = io.BytesIO()
            im.save(b, "PNG", optimize=True)
            blobs.append(b.getvalue())
        else:
            blobs.append(dib(im))
    off = 6 + 16 * len(images)
    out = struct.pack("<HHH", 0, 1, len(images))
    for im, blob in zip(images, blobs):
        out += struct.pack("<BBBBHHII", im.width % 256, im.height % 256, 0, 0, 1, 32, len(blob), off)
        off += len(blob)
    path.write_bytes(out + b"".join(blobs))


# ---------------------------------------------------------------- SVG
def f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def svg(variant, size):
    kind, bg, fg = VARIANTS[variant]
    L = layout(kind, size)
    s = L["size"]
    d = " ".join(f"M{f(c[0][0])},{f(c[0][1])} "
                 + " ".join(f"Q{f(a[0])},{f(a[1])} {f(b[0])},{f(b[1])}" for a, b in c[1:]) + " Z"
                 for c in L["shape"])
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s} {s}" width="{s}" height="{s}">',
         f"  <title>ゆらぎ — {variant} ({'画素合わせ' if size in SMALL[kind] else '原図'})</title>",
         f"  <!-- 字の輪郭: {L['glyph']['font']}。線を約 25% 太らせてベクタ化。形はそのまま -->"]
    if bg:
        o.append(f'  <rect width="{s}" height="{s}" rx="{f(BADGE_RADIUS * s)}" fill="{bg}"/>')
    o += [f'  <path fill="{fg}" d="{d}"/>', "</svg>", ""]
    return "\n".join(o)


# ---------------------------------------------------------------- 確認用シート
def preview(rendered):
    pad, zoom = 16, 8
    show = [16, 20, 24, 32, 48]
    colw = pad + sum(s + pad for s in show) + sum(s * zoom + pad for s in (16, 20, 24)) + 256 + pad
    rowh = 256 + pad * 2
    grounds = [("#f3f3f3", "light"), ("#1c1c1c", "dark")]
    sheet = Image.new("RGB", (colw, rowh * len(rendered) * 2), "#808080")
    y = 0
    for name, imgs in rendered.items():
        for ground, _ in grounds:
            sheet.paste(Image.new("RGB", (colw, rowh), ground), (0, y))
            x = pad
            for s in show:
                sheet.paste(imgs[s], (x, y + pad), imgs[s])
                x += s + pad
            for s in (16, 20, 24):
                big = imgs[s].resize((s * zoom,) * 2, Image.NEAREST)
                sheet.paste(big, (x, y + pad), big)
                x += s * zoom + pad
            sheet.paste(imgs[256], (x, y + pad), imgs[256])
            y += rowh
    sheet.save(OUT / "preview.png")


def main():
    SRC.mkdir(exist_ok=True)
    rendered = {}
    for name, (kind, _, _) in VARIANTS.items():
        rendered[name] = {s: render(name, s) for s in SIZES}
        write_ico(OUT / f"{name}.ico", [rendered[name][s] for s in SIZES])
        (SRC / f"{name}.svg").write_text(svg(name, 64), encoding="utf-8")
        for s in SMALL[kind]:
            (SRC / f"{name}-{s}px.svg").write_text(svg(name, s), encoding="utf-8")
    preview(rendered)


if __name__ == "__main__":
    main()
