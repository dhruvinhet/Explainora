"""Direct and pytest-compatible regression checks for Windows Unicode rendering."""

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import animated_scene  # noqa: E402
from fast_render import _drawtext_filter, caption_chunks_for_scene  # noqa: E402
from tts_engine import FFMPEG_BIN  # noqa: E402


UNICODE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">
<rect width="1280" height="720" fill="white"/>
<text x="100" y="200" font-size="54">τ Δt changes ω — wheel&apos;s speed</text>
</svg>"""
UNICODE_CAPTION = "Torque τ times Δt changes the wheel's angular speed ω."


def test_unicode_svg_and_captions_render_on_windows() -> None:
    with tempfile.TemporaryDirectory(prefix="simi_unicode_test_") as tmp:
        root = Path(tmp)

        # Exercise the former CP1252 failure while stubbing only the external
        # SVG decoder. The subprocess must receive an intact UTF-8 temp file.
        real_run = animated_scene.subprocess.run

        def inspect_svg(command, *args, **kwargs):
            svg_path = next(Path(value) for value in command if str(value).endswith(".svg"))
            decoded = svg_path.read_text(encoding="utf-8")
            assert "τ" in decoded and "ω" in decoded
            Path(command[-1]).write_bytes(b"png placeholder")
            return subprocess.CompletedProcess(command, 0)

        animated_scene.subprocess.run = inspect_svg
        try:
            animated_scene.rasterize_svg(UNICODE_SVG, root / "frame.png")
        finally:
            animated_scene.subprocess.run = real_run

        # Exercise the exact FFmpeg parser boundary. Caption text stays in
        # UTF-8 files and does not appear inline in the filter expression.
        chunks = caption_chunks_for_scene(UNICODE_CAPTION, 1.0)
        caption_dir = root / "captions"
        drawtext = _drawtext_filter(chunks, textfile_dir=caption_dir)
        assert "textfile=" in drawtext
        assert "wheel's" not in drawtext
        stored = " ".join(
            path.read_text(encoding="utf-8") for path in caption_dir.glob("*.txt")
        )
        assert "τ" in stored and "ω" in stored and "wheel's" in stored

        output = root / "caption.mp4"
        subprocess.run(
            [
                FFMPEG_BIN,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x180:d=0.2",
                "-vf",
                drawtext,
                "-frames:v",
                "1",
                str(output),
            ],
            check=True,
            timeout=15,
        )
        assert output.stat().st_size > 0


if __name__ == "__main__":
    test_unicode_svg_and_captions_render_on_windows()
    print("Unicode SVG + FFmpeg captions: PASS")
