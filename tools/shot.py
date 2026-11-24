#!/usr/bin/env python3
"""shot.py - render docs/screenshot.png from the app's own geometry.

Reads the vertex buffer that nota_screen_list actually emits, samples the
baked alpha atlas for glyph quads and flat-fills solid ones. This is a
software renderer for one verification purpose: if the screenshot looks
wrong, the app is wrong. It found three real bugs during development - a
clobbered glyph index, a texel format mismatch and a repeated title line.

Usage:  python3 tools/shot.py [--out docs/screenshot.png]
Requires Pillow and the same NOTA_CC environment as the tests.
"""
import argparse
import os
import sys, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
import test_ui as T
from PIL import Image

elf, syms = T.build()
fx = T.Fx(elf, syms)
T.seed_notes(fx, [b"Groceries\nmilk, eggs, coffee, bread",
                  b"Ship the thing\nfinish the save path and cut a build",
                  b"Reading list\nThe Design of Everyday Things"])
# the renderer needs a viewport: positions are device pixels, divided by u_proj.
# nota_gl_frame resets the vertex count - without it the capture accumulates
# every quad ever emitted and rows overlap.
fx.call("nota_ui_layout", 1080, 2160)
fx.call("nota_gl_frame", 1080, 2160)
fx.call("nota_ui_set_screen", 0)
fx.call("nota_screen_list")

# font atlas: 208x320, 16x20 cells, GL_LUMINANCE_ALPHA (2 bytes per texel)
atlas_addr = fx.sym("nota_font_atlas")
raw = fx.rd(atlas_addr, 208*320)
atlas = Image.frombytes("L", (208, 320), raw)   # 8-bit coverage per texel

W, H = 1080, 2160
img = Image.new("RGBA", (W, H), (18, 18, 20, 255))

st = fx.sym("nota_gl")
nv = struct.unpack("<I", fx.rd(st + 0x28, 4))[0]
base = fx.sym("nota_gl_vbuf")
quads = []
for i in range(nv // 6):
    v = [struct.unpack("<8f", fx.rd(base + (i*6+k)*32, 32)) for k in range(6)]
    quads.append(v)

for v in quads:
    xs=[p[0] for p in v]; ys=[p[1] for p in v]
    x0,y0,x1,y1 = min(xs),min(ys),max(xs),max(ys)
    r,g,b,a = [int(round(c*255)) for c in v[0][4:8]]
    if x1-x0 < 0.5 or y1-y0 < 0.5: continue
    us=[p[2] for p in v]; vs=[p[3] for p in v]
    u0,u1 = min(us),max(us); vv0,vv1 = min(vs),max(vs)
    if u1-u0 > 1e-6 or vv1-vv0 > 1e-6:
        # textured: crop the atlas cell region and scale it into place
        sx0,sy0 = int(u0*208), int(vv0*320)
        sx1,sy1 = max(int(u1*208),sx0+1), max(int(vv1*320),sy0+1)
        cell = atlas.crop((sx0,sy0,sx1,sy1)).resize(
            (max(int(x1-x0),1), max(int(y1-y0),1)), Image.NEAREST)
        # tint by the vertex colour, alpha from the glyph's alpha channel
        tint = Image.new("RGBA", cell.size, (r,g,b,255))
        glyph_alpha = cell
        layer = Image.new("RGBA", cell.size, (0,0,0,0))
        layer.paste(tint, (0,0), glyph_alpha)
        img.alpha_composite(layer, (int(x0), int(y0)))
    else:
        card = Image.new("RGBA", (max(int(x1-x0),1), max(int(y1-y0),1)), (r,g,b,a))
        img.alpha_composite(card, (int(x0), int(y0)))

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="docs/screenshot.png")
a = ap.parse_args()
os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
img.convert("RGB").save(a.out)
print("wrote %s (%d quads)" % (a.out, len(quads)))
