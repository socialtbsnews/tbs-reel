"""Quick check that TBS Reel works on this computer (run by the build).
Checks Bangla text shaping, then makes a short reel from generated pictures."""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fribidi_loader  # noqa: E402
from PIL import Image, features  # noqa: E402

from reel_core import (ReelSettings, dims, disclaimer_card, ffmpeg_exe, headline_font, make_item,  # noqa: E402
                       make_reel, overlay, probe)

# Bangla needs complex text shaping (Raqm). Without it, vowel signs land in the wrong place.
assert features.check("raqm"), "Pillow has no Raqm text shaping: Bangla would render wrongly"
f = headline_font(60)
assert f.getlength("ক্ষ") < f.getlength("ক") + f.getlength("্") + f.getlength("ষ") - 5, "Bangla is not being shaped"
print("Bangla shaping OK (raqm", features.version("raqm"), "fribidi", features.version("fribidi"),
      "loaded from", fribidi_loader.LOADED or "the system", ")")

s = ReelSettings(date_text="25 OCTOBER | 2026", credit_text="Video: Test",
                 headline=[["অবৈধ পার্কিংয়ে ", False, False], ["‘ভিডিও মামলা’", True, True], [" and English", False, True]],
                 end_card=True, end_secs=1.0)
for lay in ("reel", "feed"):
    s.layout = lay
    assert overlay(s).size == dims(s), lay
    assert disclaimer_card("discretion", s, *dims(s)).size == dims(s), lay
s.layout = "reel"
s.disclaimer, s.disclaimer_secs = "copyright", 1.0
s.fade_in = s.fade_between = True
import fontlib  # noqa: E402
fams = fontlib.built_in()
assert any(f.bangla for f in fams.values()) and any(f.latin for f in fams.values()), fams
print("Fonts OK:", ", ".join(sorted(fams)))

d = tempfile.mkdtemp()
Image.new("RGB", (1600, 1000), (200, 80, 40)).save(os.path.join(d, "a.jpg"))
Image.new("RGB", (900, 1400), (40, 90, 200)).save(os.path.join(d, "b.png"))
clip = os.path.join(d, "c.mp4")
subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=2",
                "-f", "lavfi", "-i", "sine=d=2", "-c:v", "libx264", "-c:a", "aac", "-shortest", clip], check=True)
items = [make_item(os.path.join(d, n), 1.0) for n in ("a.jpg", "c.mp4", "b.png")]
assert all(items), items
items[1].meta.update(zoom=1.5, cx=0.3, cy=0.5, bleeps=[[0.5, 1.0]], blurs=[[0.1, 0.1, 0.4, 0.3]])
out = os.path.join(d, "reel.mp4")
make_reel(items, s, out)
info = probe(out)
print(info)
assert (info["width"], info["height"]) == dims(s) == (1080, 1920), info
assert info["has_audio"] and abs(info["duration"] - 6.0) < 0.2, info
print("TBS Reel engine OK")
