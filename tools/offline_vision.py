"""Run the vision pipeline on saved screenshots and write annotated copies.

    python tools/offline_vision.py shots/*.png --out debug_offline
"""
import argparse
import glob
import os
import sys

import cv2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bobble_bot.overlay import draw  # noqa: E402
from bobble_bot.vision import Vision  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("images", nargs="+")
ap.add_argument("--out", default="debug_offline")
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
files = sorted(f for pat in a.images for f in glob.glob(pat))
for i, f in enumerate(files):
    v = Vision()  # independent frames: fresh state each time
    st = v.process(cv2.imread(f), t=float(i))
    img = draw(v.debug["img"], st, v, {"active": False})
    cv2.imwrite(os.path.join(a.out, os.path.basename(f).rsplit(".", 1)[0] + ".jpg"), img)
    p = st.player
    print(f"{os.path.basename(f):28s} {st.screen.value:9s} btn={v.debug.get('pause_btn', 0):.2f} "
          f"rc={v.debug.get('receipt', 0):.2f} card={v.debug.get('card', 0):.2f} "
          f"player={'lane %+d' % p.lane if p.found else '-':8s} obst={len(st.obstacles)} coins={len(st.targets)} "
          f"free={ {k: round(z) for k, z in st.lane_free.items()} }")
