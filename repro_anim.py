"""Reproduce the scene_002 animated render to see if truncation occurs."""
import json
from pathlib import Path
from tts_engine import audio_duration_seconds
from animated_scene import render_animated_segment

SEG = Path("/home/ubuntu/simi_realtime/work/81ef2218bfcf/segments/scene_002")
OUT = Path("/tmp/repro_scene002.mp4")

svg = (SEG / "scene_animated.svg").read_text()
tl = json.loads((SEG / "timeline.json").read_text())
elements = tl["elements"]
animations = [
    {
        "t": e["appear_at_seconds"],
        "e": e["index"],
        "a": e["animation"],
        "d": e["duration_seconds"],
        "_element": {"type": e["type"], "index": e["index"], "layer": e.get("layer", 0)},
    }
    for e in elements
]

# Which wav to render with? The code passes `wav` (fixed if corrected).
for wav_name in ["narration_fixed.wav", "narration.wav"]:
    wav = SEG / wav_name
    print(f"--- rendering with {wav_name} ({audio_duration_seconds(wav):.3f}s) ---")
    if OUT.exists():
        OUT.unlink()
    try:
        render_animated_segment(
            svg, elements, animations, wav, OUT,
            narration="repro", caption_text="",
        )
        a = audio_duration_seconds(OUT)
        print(f"  segment audio: {a:.3f}s (narration {audio_duration_seconds(wav):.3f}s) "
              f"{'OK' if a >= audio_duration_seconds(wav) - 0.4 else 'TRUNCATED!'}")
    except Exception as ex:
        print("  render failed:", ex)
