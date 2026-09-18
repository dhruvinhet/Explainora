"""E2E test: simulate an LLM animation response and render an animated
segment from a real job's scene assets."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from animated_scene import (
    parse_animations_from_plan,
    render_animated_segment,
)

JOB = "209fc456d608"
scene_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1

seg_dir = Path(f"work/{JOB}/segments/scene_{scene_id:03d}")
svg = (seg_dir / "scene.svg").read_text(encoding="utf-8")
wav = seg_dir / "narration.wav"

# simulate a plausible LLM response
with open(seg_dir / "timeline.json") as f:
    timeline = json.load(f)

# build a fake element list matching the real scene's kinds.
# NOTE: this job's scene.svg may have been rendered from a fallback visual plan
# whose element order differs from the LLM plan (timeline.json). Map each
# timeline element to the SAME-TYPE SVG element in visual order so that
# animations land on the right shapes for testing.
svg_types = []
for m in re.finditer(r'<(rect|circle|ellipse|line|path|polygon|polyline|text|g)(\s[^>]*)?>', svg):
    t = m.group(1)
    if t == "g":
        tail = svg[m.end():]
        if re.match(r'\s*transform="translate\([\d\-. ]+\) scale\([\d\-.]+\)"\s*>', tail):
            continue  # layout wrapper
    svg_types.append("icon" if t == "g" else t)

# strip the background rect (the first full-size canvas rect)
if svg_types and svg_types[0] == "rect":
    svg_types = svg_types[1:]

used = []
plan_type_counts = {}
fake_elements = []
for tl in timeline["elements"]:
    kind = tl["type"]
    cnt = plan_type_counts.get(kind, 0)
    # find the cnt-th occurrence of this kind in svg_types
    idx = -1
    seen = 0
    for i, st in enumerate(svg_types):
        if st == kind:
            if seen == cnt:
                idx = i
                break
            seen += 1
    plan_type_counts[kind] = cnt + 1
    fake_elements.append({"type": kind, "_svg_idx": idx})

from video_engine import audio_duration_seconds
total = audio_duration_seconds(wav)

plan = {"canvas": {"width": 1024, "height": 768}, "elements": fake_elements, "animations": []}

anims = [
    {"t": 0.2, "e": 0, "a": "fade", "d": 0.5},
]
# add one kind-matched animation per unique element kind
seen = set()
for tl in timeline["elements"][: 16]:
    kind = tl["type"]
    if kind in seen:
        continue
    seen.add(kind)
    e_idx = fake_elements[len(anims) - 1]["_svg_idx"]
    if e_idx < 0:
        continue
    if kind in {"line", "arrow", "path", "polyline"}:
        anims.append({"t": min(1.0 + len(anims) * 0.8, total - 0.5), "e": e_idx, "a": "draw", "d": 0.8})
    elif kind == "rect":
        anims.append({"t": min(1.0 + len(anims) * 0.8, total - 0.5), "e": e_idx, "a": "wipe", "d": 0.7})
    elif kind == "text":
        anims.append({"t": min(1.0 + len(anims) * 0.8, total - 0.5), "e": e_idx, "a": "fade", "d": 0.5})
    else:
        anims.append({"t": min(1.0 + len(anims) * 0.8, total - 0.5), "e": e_idx, "a": "pop", "d": 0.5})

plan["animations"] = anims
clean = parse_animations_from_plan(plan, total)
print("scene", scene_id, "total", round(total, 2), "s, animations:", len(clean))
for a in clean:
    print("  ", a)

out = seg_dir / "segment_animated.mp4"
render_animated_segment(svg, plan["elements"], clean, wav, out)
print("wrote", out, out.stat().st_size, "bytes")
