"""Regression checks for the shared 16:9 visual/video coordinate system."""

import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

# Make the intended production dimensions explicit before importing modules
# whose constants are resolved at startup.
os.environ["VIDEO_WIDTH"] = "1280"
os.environ["VIDEO_HEIGHT"] = "720"

import video_engine  # noqa: E402
import visual_engine  # noqa: E402
from quality_report import write_canvas_evaluation  # noqa: E402


def _plan():
    return visual_engine.normalize_visual_plan(
        {
            "canvas": {"width": 1280, "height": 720},
            "elements": [
                {
                    "type": "rect",
                    "x": 100,
                    "y": 90,
                    "width": 1080,
                    "height": 540,
                    "fill": "#ffffff",
                    "stroke": "#222222",
                    "stroke_width": 3,
                },
                {
                    "type": "text",
                    "x": 640,
                    "y": 170,
                    "text": "Shared 16:9 canvas",
                    "font_size": 48,
                    "font_weight": "bold",
                    "align": "center",
                    "fill": "#111111",
                },
            ],
        }
    )


def test_visual_and_raster_config_share_dimensions() -> None:
    assert (visual_engine.CANVAS_WIDTH, visual_engine.CANVAS_HEIGHT) == (1280, 720)
    assert (video_engine.VIDEO_WIDTH, video_engine.VIDEO_HEIGHT) == (1280, 720)
    plan = _plan()
    assert plan["canvas"] == {"width": 1280, "height": 720}
    svg, _ = visual_engine.render_visual_plan(plan)
    assert 'width="1280" height="720"' in svg
    assert 'viewBox="0 0 1280 720"' in svg


def _write_blank_1280x720_png(path: Path) -> None:
    video_engine.subprocess.run(
        [
            os.environ.get("FFMPEG_BIN", "ffmpeg"),
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=white:s=1280x720",
            "-frames:v", "1", str(path),
        ],
        check=True,
    )


def test_rasterizer_uses_configured_1280x720_dimensions() -> None:
    captured = []
    real_run = video_engine._run
    video_engine._run = lambda command, error_label="": captured.append(command)
    try:
        video_engine._svg_to_png_with_ffmpeg(Path("scene.svg"), Path("scene.png"))
    finally:
        video_engine._run = real_run
    assert captured and "scale=1280:720" in captured[0]
    source = (ROOT / "video_engine.py").read_text(encoding="utf-8")
    assert 'f"--window-size={VIDEO_WIDTH},{VIDEO_HEIGHT}"' in source


def test_dimension_probe_reads_1280x720_png() -> None:
    with tempfile.TemporaryDirectory(prefix="simi_canvas_test_") as tmp:
        png = Path(tmp) / "scene.png"
        _write_blank_1280x720_png(png)
        dimensions = video_engine.subprocess.run(
            [
                os.environ.get("FFPROBE_BIN", "ffprobe"),
                "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
                str(png),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert dimensions == "1280x720"


def test_evaluation_json_reports_problem_one_pass() -> None:
    with tempfile.TemporaryDirectory(prefix="simi_canvas_report_") as tmp:
        root = Path(tmp)
        job_dir = root / "work" / "abc123abc123"
        output_dir = root / "outputs" / "abc123abc123"
        scenes = job_dir / "scenes"
        scenes.mkdir(parents=True)
        plan = _plan()
        svg, _ = visual_engine.render_visual_plan(plan)
        (scenes / "scene_001.visual_plan.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )
        (scenes / "scene_001.svg").write_text(svg, encoding="utf-8")
        _write_blank_1280x720_png(scenes / "scene_001.png")

        # A PNG is sufficient for ffprobe's dimension check in this focused
        # test; production passes final.mp4 through the same probe helper.
        path = write_canvas_evaluation(
            job_id="abc123abc123",
            mode="batch",
            job_dir=job_dir,
            output_dir=output_dir,
            final_video=scenes / "scene_001.png",
            request_data={"prompt": "generic test"},
        )
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["summary"]["problem_1_pass"] is True
        assert report["configured_video"]["is_16_9"] is True
        assert report["scenes"][0]["canvas"] == {
            "width": 1280,
            "height": 720,
            "aspect_ratio": round(1280 / 720, 6),
            "matches_configured_video": True,
        }


def test_both_players_expose_evaluation_download() -> None:
    index = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    stream = (ROOT / "templates" / "stream_player.html").read_text(encoding="utf-8")
    assert 'id="evaluationLink"' in index and "evaluation_url" in index
    assert 'id="evaluationLink"' in stream and "evaluation_url" in stream


if __name__ == "__main__":
    test_visual_and_raster_config_share_dimensions()
    test_rasterizer_uses_configured_1280x720_dimensions()
    test_dimension_probe_reads_1280x720_png()
    test_evaluation_json_reports_problem_one_pass()
    test_both_players_expose_evaluation_download()
    print("Shared 16:9 canvas + evaluation JSON: PASS")
