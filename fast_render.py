"""
fast_render.py — Cheap, fast FFmpeg rendering for static explainer scenes.

The old path (libx264 preset medium, 30 fps, CRF 20) re-encodes hundreds of
identical frames for visuals that never move. This module replaces it for the
real-time pipeline with:

- Configurable encoder preset (default: veryfast) and CRF.
- Configurable fps for static scenes (default 15, still smooth for SVG art).
- A `-loop 1` still-image encode plus a separate `concat` with stream copy,
  exactly like before, so the player can still hand off a final MP4.
- Optional burnt-in phrase captions (caption_chunks = list of (phrase, t_start, t_end)).
- Optional music bed: a full-video background track ducked under the narration.

Environment:
  FINAL_ENCODER_PRESET=veryfast   (default veryfast)
  STATIC_SCENE_FPS=15             (default 15)
  VIDEO_CRF=24                    (default 24 for flat SVG graphics; old value 20)
  VIDEO_WIDTH=1280                (default 1280; use 1920 for 1080p)
  VIDEO_HEIGHT=720                (default 720; use 1080 for 1080p)
  CAPTIONS_ENABLED=0              (default 0; set 1 to burn in phrase captions)
  CAPTIONS_FONT_SIZE=26           (default 26)
  CAPTIONS_MAX_PHRASE_WORDS=14    (max words per caption line)
  MUSIC_TRACK_PATH=               (if set, used as the background bed)
  MUSIC_VOLUME=0.12               (bed volume relative to narration; default 0.12)
"""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tts_engine import audio_duration_seconds, FFMPEG_BIN


# Camera layer on by default (env CAMERA=1/0). Static scenes use ffmpeg
# zoompan; animated scenes apply an SVG-space camera in animated_scene.
CAMERA_ENABLED = str(os.environ.get("CAMERA", "1")).strip().lower() not in {
    "0", "false", "no", "off",
}

VIDEO_WIDTH = int(os.environ.get("VIDEO_WIDTH", "1280"))
VIDEO_HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "720"))
# Unified fps: all segments MUST share the same frame rate for the
# stream-copy concat used by the live assembler (mixed fps breaks the video
# timeline and shortens it). Override with STATIC_SCENE_FPS if desired.
SCENE_FPS = max(1, int(os.environ.get("STATIC_SCENE_FPS", "12")))
PRESET = os.environ.get("FINAL_ENCODER_PRESET", "veryfast").strip() or "veryfast"
CRF = int(os.environ.get("VIDEO_CRF", "24"))
CAPTIONS_ENABLED = os.environ.get("CAPTIONS_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}


def _captions_enabled() -> bool:
    """Read a per-job override without writing it into the environment."""
    from job_runtime import current_runtime

    runtime = current_runtime()
    if runtime is not None and runtime.captions is not None:
        return runtime.captions
    return os.environ.get("CAPTIONS_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }
CAPTIONS_FONT_SIZE = int(os.environ.get("CAPTIONS_FONT_SIZE", "26"))
CAPTIONS_MAX_PHRASE_WORDS = int(os.environ.get("CAPTIONS_MAX_PHRASE_WORDS", "14"))
MUSIC_TRACK_PATH = os.environ.get("MUSIC_TRACK_PATH", "").strip()
MUSIC_VOLUME = float(os.environ.get("MUSIC_VOLUME", "0.12"))


def _run(cmd, error_label="FFmpeg"):
    import subprocess
    result = subprocess.run(cmd, capture_output=True, timeout=None)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"{error_label} failed (code {result.returncode}):\n{stderr}")
    return result


def split_phrases(text: str, max_words: int = CAPTIONS_MAX_PHRASE_WORDS) -> List[str]:
    """Split narration into displayable caption phrases (no LLM needed)."""
    words = str(text or "").split()
    phrases: List[str] = []
    chunk: List[str] = []
    for w in words:
        chunk.append(w)
        if len(chunk) >= max_words:
            phrases.append(" ".join(chunk))
            chunk = []
    if chunk:
        phrases.append(" ".join(chunk))
    return phrases


def caption_chunks_for_scene(
    narration: str, duration: float, image_size: Optional[Tuple[int, int]] = None
) -> List[Tuple[str, float, float]]:
    """Timed caption phrases distributed across the scene duration.

    Deterministic: narration is written by us, so phrases can be evenly
    spaced by word count (proportional timing). Returns
    [(phrase, t_start, t_end), ...].
    """
    phrases = split_phrases(narration)
    if not phrases or duration <= 0:
        return []
    # Guard: split_phrases caps at CAPTIONS_MAX_PHRASE_WORDS (14) words, but a
    # 14-word phrase at size 26 on 1280px can still exceed one line (~100 chars).
    # Any phrase wider than 11 words is broken into screen-sized sub-phrases so
    # each caption fits on the screen.
    screen_phrases: List[str] = []
    for p in phrases:
        words = p.split()
        if len(words) <= 11:
            screen_phrases.append(p)
            continue
        for i in range(0, len(words), 11):
            screen_phrases.append(" ".join(words[i:i + 11]))
    phrases = screen_phrases
    word_times = [len(p.split()) / max(1, sum(len(p.split()) for p in phrases)) for p in phrases]
    chunks: List[Tuple[str, float, float]] = []
    t = 0.0
    for p, share in zip(phrases, word_times):
        seg = max(0.4, share * duration)
        t_end = min(t + seg, duration)
        if t_end - t < 0.4 and t < duration:
            t_end = min(t + 0.4, duration)
        chunks.append((p, round(max(0.0, t), 3), round(min(t_end, duration), 3)))
        t = t_end
    return chunks


def _wrap_caption_phrase(phrase: str, max_chars_per_line: int = 88) -> str:
    """Wrap a caption phrase so it never renders past the frame edges.

    At CAPTIONS_FONT_SIZE=26 on 1280px width, DejaVu Sans renders roughly
    14-15px per character, so a safe single line is ~88 chars. Longer phrases
    are broken on word boundaries into at most two lines so they cannot spill
    off the right edge of the screen.
    """
    words = phrase.split()
    lines: List[str] = []
    current: List[str] = []
    for w in words:
        candidate = " ".join(current + [w]) if current else w
        if len(candidate) <= max_chars_per_line:
            current.append(w)
        else:
            if current:
                lines.append(" ".join(current))
            current = [w]
    if current:
        lines.append(" ".join(current))
    return "\\N".join(lines)


def _drawtext_filter(
    chunks: List[Tuple[str, float, float]], font_size: int = CAPTIONS_FONT_SIZE,
    textfile_dir: Optional[Path] = None,
) -> str:
    """Build a Unicode-safe FFmpeg drawtext filtergraph.

    With textfile_dir, caption contents are stored as UTF-8 files so
    apostrophes, punctuation, and mathematical symbols never become part of
    FFmpeg's filtergraph syntax.
    """
    # A font must exist; fall back to DejaVu Sans which ships on most Linux.
    import shutil
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not shutil.os.path.isfile(font):
        font = ""
    parts = []
    if textfile_dir is not None:
        textfile_dir = Path(textfile_dir)
        textfile_dir.mkdir(parents=True, exist_ok=True)
    for index, (phrase, t0, t1) in enumerate(chunks):
        text = _wrap_caption_phrase(phrase)
        enable = f"between(t\\,{t0:.3f}\\,{t1:.3f})"
        box = "box=1:boxcolor=black@0.55:boxborderw=8"
        if textfile_dir is not None:
            text_path = (textfile_dir / f"caption_{index:03d}.txt").resolve()
            text_path.write_text(text.replace("\\N", "\n"), encoding="utf-8")
            filter_path = text_path.as_posix().replace(":", "\\:").replace("'", "\\'")
            source = f"textfile='{filter_path}'"
        else:
            escaped = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")
            source = f"text='{escaped}'"
        spec = (
            f"drawtext={source}:fontsize={font_size}:fontcolor=white"
            f":x=(w-text_w)/2:y=h-60-text_h:{box}:enable='{enable}'"
        )
        if font:
            spec = f"drawtext=fontfile='{font}':" + spec[len("drawtext="):]
        parts.append(spec)
    return ",".join(parts)


def _music_filter(duration: float) -> str:
    """Background bed filter: loop track, trim to duration, duck under narration.

    The bed is mixed at low volume BEFORE the narration is layered, so speech
    always dominates. If no track is configured, returns an empty string and
    the caller skips music handling.
    """
    return (
        f"loop=loop=-1:size=32000:start=0,atrim=0:{duration:.6f},"
        f"volume={MUSIC_VOLUME:.4f},afade=t=out:st={max(0.0, duration - 1.0):.3f}:d=1.0"
    )


def render_scene_video_fast(
    image_path: Path,
    audio_path: Path,
    output_mp4: Path,
    caption_text: str = "",
    music_path: Optional[Path] = None,
    captions: Optional[bool] = None,
    camera_spec: Optional[Dict[str, Any]] = None,
) -> Path:
    """Encode one static scene with its narration, cheaply.

    caption_text, when given with CAPTIONS_ENABLED (module default) or a
    per-job captions override (True/False), is burnt into the video as timed
    phrase captions. music_path (or MUSIC_TRACK_PATH) adds a ducked background
    bed.
    """
    output_mp4 = Path(output_mp4)
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    duration = audio_duration_seconds(audio_path)

    # feature D: camera motion for static scenes (ffmpeg zoompan on the PNG —
    # no re-rasterization, cheap). Animated scenes apply their own SVG-space
    # camera in animated_scene.render_animated_segment.
    _zp = None
    from camera_engine import camera_enabled
    if camera_spec and camera_enabled():
        from camera_engine import zoompan_filter
        _zp = zoompan_filter(duration, camera_spec, VIDEO_WIDTH, VIDEO_HEIGHT,
                             fps=SCENE_FPS)

    vf = (
        (_zp + ",") if _zp else ""
        + f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:white,"
        "format=yuv420p"
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

    cmd = [FFMPEG_BIN, "-y", "-loop", "1", "-framerate", str(SCENE_FPS),
           "-i", str(image_path), "-i", str(audio_path)]
    # zoompan needs an input framerate to derive the output frame count ('d' is
    # computed in frames), so set it on the input loop stream when used.
    if _zp:
        cmd += ["-framerate", str(SCENE_FPS)]
        cmd = [cmd[0], "-y"] + [a for a in cmd[2:]]
        # result: ffmpeg -y -loop 1 -framerate F -i img -framerate F -i audio
    if use_music:
        cmd += ["-i", str(music_path)]
    cmd += [
        "-t", f"{duration:.6f}",
        "-filter_complex",
        (
            f"[0:v]{vf}[v];[1:a]loudnorm=I=-16:TP=-1.5:LRA=7[speech];"
            + (f"[2:a]{_music_filter(duration)}[music];" if use_music else "")
            + ("[speech][music]amix=inputs=2:duration=first:dropout_transition=0[a]"
               if use_music else "[speech]anull[a]")
        ),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264",
        "-preset", PRESET,
        "-crf", str(CRF),
        "-r", str(SCENE_FPS),
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "1",
        "-shortest",
        str(output_mp4),
    ]
    try:
        _run(cmd, error_label="FFmpeg fast scene render")
    finally:
        if caption_tmp is not None:
            caption_tmp.cleanup()
    return output_mp4


def concat_scene_videos_fast(scene_videos: Iterable[Path], output_mp4: Path) -> Path:
    """Stream-copy concatenate scene segments (same codec/settings)."""
    scene_videos = [Path(p).resolve() for p in scene_videos]
    if not scene_videos:
        raise ValueError("No scene videos to concatenate.")
    output_mp4 = Path(output_mp4)
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    concat_file = output_mp4.with_suffix(".concat.txt")
    lines = [f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for p in scene_videos]
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        cmd = [
            FFMPEG_BIN, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_mp4),
        ]
        _run(cmd, error_label="FFmpeg fast concat")
    finally:
        concat_file.unlink(missing_ok=True)
    return output_mp4
