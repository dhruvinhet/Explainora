"""Direct and pytest-compatible regressions for A/V and word synchronization."""

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from animated_scene import narrate_align_animations  # noqa: E402
from tts_engine import _extract_transcription_words  # noqa: E402


def test_top_level_whisper_words_are_extracted() -> None:
    response = SimpleNamespace(
        words=[
            SimpleNamespace(word=" Torque", start=0.1, end=0.4),
            SimpleNamespace(word="omega", start=4.8, end=5.2),
        ],
        segments=[],
    )
    assert _extract_transcription_words(response) == [
        {"word": "Torque", "start": 0.1, "end": 0.4},
        {"word": "omega", "start": 4.8, "end": 5.2},
    ]


def test_unanchored_animations_span_spoken_word_timeline() -> None:
    animations = [
        {"e": 0, "t": 0.1, "d": 0.4, "a": "fade"},
        {"e": 1, "t": 0.5, "d": 0.4, "a": "fade"},
        {"e": 2, "t": 1.0, "d": 0.4, "a": "fade"},
    ]
    words = [
        {"word": "first", "start": 0.0, "end": 0.3},
        {"word": "middle", "start": 5.0, "end": 5.4},
        {"word": "last", "start": 9.2, "end": 9.6},
    ]
    aligned = narrate_align_animations(
        animations, 10.0, "first middle last", words=words
    )
    assert aligned[0]["t"] < 0.5
    assert 4.0 < aligned[1]["t"] < 6.0
    assert aligned[2]["t"] > 7.5


def test_owned_label_never_appears_before_its_visual_object() -> None:
    animations = [
        {"e": 0, "t": 6.0, "d": 0.6, "a": "pop", "_element": {"id": "current"}},
        {"e": 1, "t": 0.1, "d": 0.3, "a": "fade", "_element": {
            "id": "current_label", "owner_id": "current", "type": "text"
        }},
    ]
    aligned = narrate_align_animations(animations, 10.0)
    by_index = {row["e"]: row for row in aligned}
    assert by_index[1]["t"] > by_index[0]["t"]


def test_finalization_has_no_audio_only_tempo_stretch() -> None:
    source = (ROOT / "segment_pipeline.py").read_text(encoding="utf-8")
    finalization = source[source.index("# ---- final assembly"):]
    assert '"-filter:a"' not in finalization
    assert "atempo=" not in finalization


def test_player_defers_refresh_while_reviewing() -> None:
    source = (ROOT / "templates" / "stream_player.html").read_text(
        encoding="utf-8"
    )
    assert "reviewingEarlier" in source
    assert "if (!force && reviewingEarlier) return false" in source
    assert "video.addEventListener('seeking'" in source
    assert "checkNewSceneFallback(true)" in source


if __name__ == "__main__":
    test_top_level_whisper_words_are_extracted()
    test_unanchored_animations_span_spoken_word_timeline()
    test_finalization_has_no_audio_only_tempo_stretch()
    test_player_defers_refresh_while_reviewing()
    print("A/V + word synchronization regressions: PASS")
