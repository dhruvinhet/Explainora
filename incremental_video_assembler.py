"""
incremental_video_assembler.py — Watchable, seekable MP4 that grows scene by scene.

Problem it solves:
The streaming player used to play isolated SVG+audio chunks in the browser,
which felt like slideshows (no pause, no seek bar, and when two scenes arrived
at once, two audio elements could overlap). A real <video> element needs one
continuous MP4 with a proper moov atom and accurate durations.

Solution:
- The pipeline calls add_scene() each time a scene's segment.mp4 finishes.
- We keep a "live" stream.mp4 built incrementally with stream-copy concat
  (all segments are encoded with identical settings, so concat is safe).
- Every new append rewrites stream.mp4 with `-movflags +faststart`, which
  moves the moov atom to the front so the browser can seek/stream it.
- scene_offsets.json records cumulative start times so the player and any
  consumer know where each scene begins in the timeline.

The browser never sees per-scene audio at all: a single HTML5 video element
plays the growing stream.mp4, giving pause/seek/volume/fullscreen for free,
and playback order is strictly sequential by construction (no collisions).
"""

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tts_engine import FFMPEG_BIN, FFPROBE_BIN, audio_duration_seconds


class IncrementalVideoAssembler:
    """Thread-safe, append-only video assembler producing one watchable MP4."""

    def __init__(self, job_dir: Path) -> None:
        self._job_dir = Path(job_dir)
        self._stream_path = self._job_dir / "stream.mp4"
        self._offsets_path = self._job_dir / "scene_offsets.json"
        self._manifest_path = self._job_dir / "manifest.json"
        self._scenes: List[Path] = []
        self._durations: List[float] = []
        # Scene workers finish out of order.  Buffer them until the next lesson
        # scene is available, then flush a contiguous prefix.  RLock lets the
        # atomic save helpers take consistent snapshots while add_scene holds
        # the assembly lock.
        self._lock = threading.RLock()
        self._pending: Dict[int, Tuple[Path, float]] = {}
        self._next_scene_id = 1
        self._version = 0

    @property
    def stream_path(self) -> Path:
        return self._stream_path

    def scene_offsets(self) -> List[dict]:
        """Cumulative start times of every scene in the live video."""
        starts = []
        acc = 0.0
        with self._lock:
            for p, d in zip(self._scenes, self._durations):
                starts.append({"scene_file": p.name, "start_seconds": round(acc, 3), "duration_seconds": round(d, 3)})
                acc += d
        return starts

    def add_scene(self, segment_mp4: Path, scene_id: Optional[int] = None) -> float:
        """Append one completed scene segment to the live video.

        Returns the cumulative duration of the video after the append,
        or -1.0 if anything went wrong.
        """
        segment_mp4 = Path(segment_mp4)
        if not segment_mp4.is_file():
            return -1.0

        duration = audio_duration_seconds(segment_mp4)
        if scene_id is None:
            try:
                scene_id = int(segment_mp4.parent.name.rsplit("_", 1)[-1])
            except (TypeError, ValueError):
                scene_id = self._next_scene_id
        with self._lock:
            self._pending[int(scene_id)] = (segment_mp4, float(duration))
            appended = False
            while self._next_scene_id in self._pending:
                path, scene_duration = self._pending.pop(self._next_scene_id)
                self._scenes.append(path)
                self._durations.append(scene_duration)
                self._next_scene_id += 1
                self._version += 1
                appended = True
            order = list(self._scenes)

            if appended:
                # Serialize the rebuild and metadata swaps. Previously two
                # completed workers could race over the same temporary file.
                self._rebuild(order)
                self._save_offsets()
                self._save_manifest()
            return sum(self._durations)

    def finish(self) -> Path:
        """Finalize: rebuild one last time (idempotent, safe to call twice)."""
        with self._lock:
            order = list(self._scenes)
        self._rebuild(order)
        self._save_offsets()
        self._save_manifest()
        return self._stream_path

    @property
    def total_seconds(self) -> float:
        with self._lock:
            return float(sum(self._durations))

    # ------------------------------------------------------------------
    @staticmethod
    def _video_fps(path: Path) -> float:
        """Read the video stream frame rate of a segment."""
        result = subprocess.run(
            [FFPROBE_BIN, "-v", "error",
             "-select_streams", "v:0", "-show_entries",
             "stream=r_frame_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        try:
            num, den = result.stdout.strip().split("/")
            return float(num) / max(1, float(den))
        except ValueError:
            return 0.0

    def _rebuild(self, scenes: List[Path]) -> None:
        if not scenes:
            return  # nothing to assemble yet; nothing is served as "no video"
        self._stream_path.parent.mkdir(parents=True, exist_ok=True)
        concat_file = self._job_dir / "stream.concat.txt"
        tmp_path = self._stream_path.with_suffix(".tmp.mp4")
        try:
            # Guard: -c copy concat with mixed frame rates corrupts the video
            # timeline (ffprobe reports a shorter stream). If scene segments
            # use different fps (e.g. an animated 4fps scene beside a static
            # fallback at another rate), re-encode them to a uniform fps first.
            fps_values = [self._video_fps(p) for p in scenes]
            if fps_values and len(set(fps_values)) > 1:
                target_fps = max(fps_values)
                scenes = self._normalize_fps(scenes, target_fps)
        except Exception as fps_err:
            print(f"Assembler fps normalization failed ({fps_err}); "
                  "continuing with raw concat (timeline may drift).")
        try:
            # ffmpeg resolves concat-file paths relative to the concat file's
            # own directory ONLY with the absolute option on recent versions;
            # safest cross-version approach: use absolute paths so the job's
            # cwd cannot corrupt resolution.
            lines = [f"file '{str(Path(p).absolute()).replace(chr(39), chr(92) + chr(39) + chr(39))}'" for p in scenes]
            concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            cmd = [
                FFMPEG_BIN, "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                "-movflags", "+faststart",
                str(tmp_path),
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode != 0:
                raise RuntimeError(
                    "FFmpeg incremental concat failed:\n"
                    + result.stderr.decode("utf-8", errors="replace")
                )
            # Atomic swap: the served path is never a half-written file.
            # The browser either sees the old version or the new complete one.
            tmp_path.replace(self._stream_path)
        finally:
            concat_file.unlink(missing_ok=True)
            tmp_path.unlink(missing_ok=True)

    def _normalize_fps(self, scenes: List[Path], target_fps: float) -> List[Path]:
        """Re-encode every scene to `target_fps` so concat-copy stays safe.

        Audio is untouched (already uniform 48k/mono from the render paths);
        only the video stream is fps-filtered and re-encoded.
        """
        normalized: List[Path] = []
        for p in scenes:
            # Every scene file is named segment.mp4. Include its parent scene
            # id so normalization cannot overwrite earlier scenes and make the
            # final concat repeat the last normalized file.
            out = self._job_dir / "normalized" / f"{p.parent.name}_{p.name}"
            out.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(p),
                 "-vf", f"fps={target_fps:.6f}",
                 "-r", str(target_fps),
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
                 "-c:a", "copy",
                 str(out)],
                check=True, timeout=120,
            )
            normalized.append(out)
        return normalized

    def _save_offsets(self) -> None:
        starts = self.scene_offsets()
        tmp = self._offsets_path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps({"total_seconds": round(sum(self._durations), 3), "scenes": starts}), encoding="utf-8")
        tmp.replace(self._offsets_path)

    # ------------------------------------------------------------------
    # manifest — live playlist of per-scene MP4s for the chunked player
    # ------------------------------------------------------------------

    def manifest(self) -> dict:
        """Ordered playlist of finished scene segments (used by the MSE player)."""
        with self._lock:
            return {
                "version": self._version,
                "total_seconds": round(sum(self._durations), 3),
                "fps_uniform": True,
                "scenes": [
                    {
                        "n": int(Path(p).parent.name.rsplit("_", 1)[-1]),
                        "file": Path(p).name,
                        "url": f"/segments/{Path(self._job_dir).name}/{Path(p).parent.name}/{Path(p).name}",
                        "start_seconds": round(sum(self._durations[:i]), 3),
                        "duration_seconds": round(float(d), 3),
                    }
                    for i, (p, d) in enumerate(zip(self._scenes, self._durations))
                ],
            }

    def _save_manifest(self) -> None:
        """Atomically persist the live scene playlist so the player can fetch
        only new scenes instead of re-downloading the whole stream.mp4."""
        tmp = self._manifest_path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(self.manifest(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._manifest_path)
