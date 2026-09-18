import json
import os

from dotenv import load_dotenv

load_dotenv()

from director import create_video_plan
from visual_engine import (
    create_visual_plan,
    render_visual_plan,
)


PROMPT = """
Explain how binary search works to a beginner,
with a concrete example.
""".strip()

DURATION = 120
AUDIENCE = "beginner"


print("\n")
print("=" * 80)
print("DIRECTOR TEST")
print("=" * 80)

plan = create_video_plan(
    PROMPT,
    DURATION,
    AUDIENCE,
)

print(
    json.dumps(
        plan,
        indent=2,
        ensure_ascii=False,
    )
)


print("\n")
print("=" * 80)
print("FIRST SCENE VISUAL TEST")
print("=" * 80)

first_scene = plan["scenes"][0]

print("\nVISUAL PROMPT:")
print(
    first_scene["visual_prompt"]
)


visual_plan = create_visual_plan(
    first_scene["visual_prompt"]
)


print("\nVISUAL JSON:")
print(
    json.dumps(
        visual_plan,
        indent=2,
        ensure_ascii=False,
    )
)


svg, icons = render_visual_plan(
    visual_plan
)


with open(
    "model_test.svg",
    "w",
    encoding="utf-8",
) as file:
    file.write(svg)


print("\nSaved:")
print("model_test.svg")