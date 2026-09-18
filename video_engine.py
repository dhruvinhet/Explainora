import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterable, List, Optional

from tts_engine import audio_duration_seconds


FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
VIDEO_WIDTH = int(os.environ.get("VIDEO_WIDTH", "1280"))
VIDEO_HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "720"))
VIDEO_FPS = int(os.environ.get("VIDEO_FPS", "30"))
VIDEO_CRF = int(os.environ.get("VIDEO_CRF", "20"))

# Optional override. On Windows this can be e.g.
# BROWSER_BIN=C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe
BROWSER_BIN = os.environ.get("BROWSER_BIN", "").strip()


def _run(cmd, error_label: str = "Command"):
    completed = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        stdout = completed.stdout.decode("utf-8", errors="replace")
        details = stderr.strip() or stdout.strip()
        raise RuntimeError(
            f"{error_label} failed ({completed.returncode}):\n{details}"
        )
    return completed


def _find_browser() -> Optional[str]:
    """Find a Chromium-family browser for SVG -> PNG rendering.

    Windows normally already has Microsoft Edge, so this avoids Cairo/GTK DLL
    installation completely. BROWSER_BIN may be used to override detection.
    """
    if BROWSER_BIN:
        configured = Path(os.path.expandvars(BROWSER_BIN)).expanduser()
        if configured.exists():
            return str(configured)
        found = shutil.which(BROWSER_BIN)
        if found:
            return found
        raise RuntimeError(
            f"BROWSER_BIN is set but was not found: {BROWSER_BIN}"
        )

    # PATH first.
    for name in (
        "msedge",
        "msedge.exe",
        "chrome",
        "chrome.exe",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        found = shutil.which(name)
        if found:
            return found

    # Standard Windows install locations.
    env = os.environ
    roots = [
        env.get("PROGRAMFILES(X86)"),
        env.get("PROGRAMFILES"),
        env.get("LOCALAPPDATA"),
    ]
    relative_candidates = [
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Google/Chrome/Application/chrome.exe"),
    ]

    for root in roots:
        if not root:
            continue
        for relative in relative_candidates:
            candidate = Path(root) / relative
            if candidate.exists():
                return str(candidate)

    return None


def _svg_to_png_with_ffmpeg(svg_path: Path, output_png: Path) -> None:
    """Use FFmpeg's librsvg decoder when available."""
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(svg_path),
        "-frames:v", "1",
        "-vf", f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}",
        str(output_png),
    ]
    _run(cmd, error_label="FFmpeg SVG renderer")


def _svg_to_png_with_cairosvg(svg_text: str, output_png: Path) -> None:
    """Use CairoSVG when the installed FFmpeg build cannot decode SVG."""
    import cairosvg

    cairosvg.svg2png(
        bytestring=svg_text.encode("utf-8"),
        write_to=str(output_png),
        output_width=VIDEO_WIDTH,
        output_height=VIDEO_HEIGHT,
    )
    if not output_png.exists() or output_png.stat().st_size == 0:
        raise RuntimeError("CairoSVG exited without producing a PNG")


def _svg_to_png_with_browser(svg_text: str, output_png: Path) -> None:
    """Fallback renderer using installed Edge/Chrome/Chromium."""
    browser = _find_browser()
    if not browser:
        raise RuntimeError(
            "No Chromium browser was found. Install Microsoft Edge/Google Chrome "
            "or set BROWSER_BIN to its executable path."
        )

    html_text = f"""<!doctype html>
<html>
<head>
<meta charset=\"utf-8\">
<style>
html, body {{ margin:0; padding:0; width:{VIDEO_WIDTH}px; height:{VIDEO_HEIGHT}px; overflow:hidden; background:white; }}
svg {{ display:block; width:{VIDEO_WIDTH}px !important; height:{VIDEO_HEIGHT}px !important; }}
</style>
</head>
<body>{svg_text}</body>
</html>
"""

    errors = []
    # Chromium can occasionally exit zero without producing a screenshot when
    # many independent headless processes start together. Each attempt gets an
    # isolated profile and writes to a private staging file; only a verified,
    # non-empty PNG is moved into the scene path.
    for attempt in range(1, 4):
        with tempfile.TemporaryDirectory(
            prefix="simi_browser_",
            ignore_cleanup_errors=(os.name == "nt"),
        ) as tmp:
            tmp_path = Path(tmp)
            html_path = tmp_path / "scene.html"
            profile_path = tmp_path / "profile"
            staged_png = tmp_path / "scene.png"
            html_path.write_text(html_text, encoding="utf-8")

            cmd = [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--no-first-run",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--window-size={VIDEO_WIDTH},{VIDEO_HEIGHT}",
                f"--user-data-dir={profile_path}",
                f"--screenshot={staged_png}",
                html_path.resolve().as_uri(),
            ]
            if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
                cmd.insert(1, "--no-sandbox")

            try:
                completed = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=45,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"attempt {attempt}: timed out")
            else:
                stderr = completed.stderr.decode("utf-8", errors="replace").strip()
                if completed.returncode != 0:
                    errors.append(
                        f"attempt {attempt}: exit {completed.returncode}: {stderr}"
                    )
                elif not staged_png.exists() or staged_png.stat().st_size == 0:
                    errors.append(
                        f"attempt {attempt}: browser exited successfully but produced no PNG"
                        + (f": {stderr}" if stderr else "")
                    )
                else:
                    shutil.copyfile(staged_png, output_png)
                    return
        if attempt < 3:
            time.sleep(0.15 * attempt)

    raise RuntimeError(
        f"Browser SVG renderer failed using {browser}:\n" + "\n".join(errors)
    )


def svg_to_png(svg_text: str, output_png: Path) -> Path:
    """Rasterize an SVG using the first available local renderer.

    Primary path: FFmpeg + librsvg.
    Fallbacks: CairoSVG, then installed Edge/Chrome/Chromium.
    """
    output_png = Path(output_png).resolve()
    output_png.parent.mkdir(parents=True, exist_ok=True)

    errors = []

    with tempfile.TemporaryDirectory(prefix="simi_svg_") as tmp:
        svg_path = Path(tmp) / "scene.svg"
        svg_path.write_text(svg_text, encoding="utf-8")

        try:
            _svg_to_png_with_ffmpeg(svg_path, output_png)
        except Exception as exc:
            errors.append(str(exc))
            output_png.unlink(missing_ok=True)

            try:
                _svg_to_png_with_cairosvg(svg_text, output_png)
            except Exception as cairo_exc:
                errors.append(f"CairoSVG renderer failed: {cairo_exc}")
                output_png.unlink(missing_ok=True)

                try:
                    _svg_to_png_with_browser(svg_text, output_png)
                except Exception as browser_exc:
                    errors.append(str(browser_exc))

    if not output_png.exists() or output_png.stat().st_size == 0:
        raise RuntimeError(
            "Could not rasterize SVG.\n\n"
            "Tried:\n- FFmpeg SVG decoder\n- CairoSVG\n"
            "- Edge/Chrome/Chromium fallback\n\n"
            + "\n\n".join(errors)
            + "\n\nInstall CairoSVG or an FFmpeg build with SVG/librsvg support, "
              "or set BROWSER_BIN to msedge.exe/chrome.exe."
        )

    return output_png


def render_scene_video(
    image_path: Path,
    audio_path: Path,
    output_mp4: Path,
    caption_text: str = "",
    music_path: Optional[Path] = None,
    captions: Optional[bool] = None,
) -> Path:
    """Encode one static scene with narration; optional captions and bed.

    Same feature set as fast_render.render_scene_video_fast: burnt-in phrase
    captions (CAPTIONS_ENABLED env default, overridable per-job via the
    captions bool) and a ducked background music bed.
    """
    output_mp4 = Path(output_mp4)
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    duration = audio_duration_seconds(audio_path)
    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:white,"
        "format=yuv420p"
    )
    from fast_render import (
        MUSIC_TRACK_PATH,
        caption_chunks_for_scene,
        _drawtext_filter,
        _music_filter,
        _captions_enabled,
    )
    chunks = []
    caption_tmp = None
    captions_enabled = captions if captions is not None else _captions_enabled()
    if captions_enabled and caption_text:
        chunks = caption_chunks_for_scene(caption_text, duration)
        if chunks:
            caption_tmp = tempfile.TemporaryDirectory(prefix="simi_captions_")
            vf += "," + _drawtext_filter(
                chunks, textfile_dir=Path(caption_tmp.name)
            )

    use_music = music_path and Path(music_path).exists()
    if not use_music and MUSIC_TRACK_PATH:
        use_music = Path(MUSIC_TRACK_PATH).exists()
        if use_music:
            music_path = Path(MUSIC_TRACK_PATH)

    cmd = [
        FFMPEG_BIN, "-y",
        "-loop", "1",
        "-framerate", str(VIDEO_FPS),
        "-i", str(image_path),
        "-i", str(audio_path),
    ]
    if use_music:
        cmd += ["-i", str(music_path)]
    cmd += [
        "-t", f"{duration:.6f}",
        "-filter_complex",
        (
            f"[0:v]{vf}[v];[1:a]loudnorm=I=-16:TP=-1.5:LRA=7[speech];"
            + (f"[{2 if use_music else 1}:a]{_music_filter(duration)}[music];" if use_music else "")
            + ("[speech][music]amix=inputs=2:duration=first:dropout_transition=0[a]"
               if use_music else "[speech]anull[a]")
        ),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(VIDEO_CRF),
        "-r", str(VIDEO_FPS),
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "1",
        "-shortest",
        str(output_mp4),
    ]
    try:
        _run(cmd, error_label="FFmpeg scene render")
    finally:
        if caption_tmp is not None:
            caption_tmp.cleanup()
    return output_mp4


def render_scene_video_sequence(
    image_paths: Iterable[Path],
    image_durations: Iterable[float],
    audio_path: Path,
    output_mp4: Path,
    caption_text: str = "",
    captions: Optional[bool] = None,
) -> Path:
    """Encode cumulative static beat states against one unchanged narration."""
    images = [Path(path).resolve() for path in image_paths]
    durations = [max(0.05, float(value)) for value in image_durations]
    if len(images) < 2 or len(images) != len(durations):
        raise ValueError("Static beat sequence requires matching image/duration rows.")
    output_mp4 = Path(output_mp4)
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    total = audio_duration_seconds(audio_path)
    scale = total / max(1e-6, sum(durations))
    durations = [value * scale for value in durations]
    concat_file = output_mp4.with_suffix(".stills.txt")
    lines: List[str] = []
    for path, duration in zip(images, durations):
        safe = str(path).replace("'", "'\\''")
        lines.extend([f"file '{safe}'", f"duration {duration:.6f}"])
    safe_last = str(images[-1]).replace("'", "'\\''")
    lines.append(f"file '{safe_last}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    from fast_render import _captions_enabled, _drawtext_filter, caption_chunks_for_scene
    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:white,"
        f"fps={VIDEO_FPS},format=yuv420p"
    )
    caption_tmp = None
    captions_enabled = captions if captions is not None else _captions_enabled()
    if captions_enabled and caption_text:
        chunks = caption_chunks_for_scene(caption_text, total)
        if chunks:
            caption_tmp = tempfile.TemporaryDirectory(prefix="simi_captions_")
            vf += "," + _drawtext_filter(chunks, textfile_dir=Path(caption_tmp.name))
    cmd = [
        FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-i", str(audio_path), "-t", f"{total:.6f}", "-filter_complex",
        f"[0:v]{vf}[v];[1:a]loudnorm=I=-16:TP=-1.5:LRA=7[a]", "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(VIDEO_CRF),
        "-r", str(VIDEO_FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-ac", "1", "-shortest", str(output_mp4),
    ]
    try:
        _run(cmd, error_label="FFmpeg static beat sequence render")
    finally:
        concat_file.unlink(missing_ok=True)
        if caption_tmp is not None:
            caption_tmp.cleanup()
    return output_mp4


def concat_scene_videos(scene_videos: Iterable[Path], output_mp4: Path) -> Path:
    scene_videos = [Path(p).resolve() for p in scene_videos]
    if not scene_videos:
        raise ValueError("No scene videos to concatenate.")

    output_mp4 = Path(output_mp4)
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    concat_file = output_mp4.with_suffix(".concat.txt")

    lines: List[str] = []
    for path in scene_videos:
        # FFmpeg concat demuxer quoting.
        safe = str(path).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    try:
        # Scene segments use identical codecs/settings, so stream copy is fast.
        cmd = [
            FFMPEG_BIN, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_mp4),
        ]
        _run(cmd, error_label="FFmpeg concat")
    finally:
        concat_file.unlink(missing_ok=True)
    return output_mp4


def write_manifest(path: Path, manifest: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
