import re
from pathlib import Path

from redblue.video.captions import WordTiming, align, chunk, even_timings, get_transcriber
from redblue.video.render import build_ass
from redblue.video.schemas import Beat, Script


def test_even_timings_cover_duration_weighted_by_length():
    t = even_timings("a extraordinarily b", 3.0)
    assert t[0].start == 0 and abs(t[-1].end - 3.0) < 0.01
    assert (t[1].end - t[1].start) > 3 * (t[0].end - t[0].start)


def test_align_keeps_script_spelling_with_audio_timing():
    heard = [
        WordTiming("It's", 0.1, 0.3),
        WordTiming("two", 0.35, 0.5),
        WordTiming("times", 0.5, 0.8),
        WordTiming("faster,", 0.85, 1.3),
        WordTiming("says", 1.5, 1.7),
        WordTiming("Intel.", 1.7, 2.1),
    ]
    out = align("It's 2x faster, says Intel.", heard, 2.5)
    assert [w.word for w in out] == ["It's", "2x", "faster,", "says", "Intel."]
    two_x = out[1]
    assert two_x.start == 0.3 and two_x.end == 0.85  # spans "two times"
    assert out[2].start == 0.85 and out[-1].end == 2.1
    assert all(a.end <= b.start + 1e-9 for a, b in zip(out, out[1:], strict=False))


def test_align_handles_missing_and_extra_words():
    heard = [
        WordTiming("hello", 0.0, 0.4),
        WordTiming("uh", 0.4, 0.6),
        WordTiming("world", 0.7, 1.1),
    ]
    out = align("hello big world today", heard, 2.0)
    assert [w.word for w in out] == ["hello", "big", "world", "today"]
    assert 0.4 <= out[1].start < out[1].end <= 0.7  # interpolated between neighbours
    assert out[3].start >= 1.1
    assert align("no audio", [], 2.0) == even_timings("no audio", 2.0)


def test_chunk_breaks_on_punctuation_pauses_and_size():
    words = [
        WordTiming(w, i * 0.2, i * 0.2 + 0.18)
        for i, w in enumerate("one two three four five six. seven".split())
    ]
    words.append(WordTiming("eight", 3.0, 3.2))  # long pause before it
    groups = [[w.word for w in g] for g in chunk(words)]
    assert groups == [["one", "two", "three", "four"], ["five", "six."], ["seven"], ["eight"]]


def test_ass_uses_word_timings_with_karaoke_tags():
    s = Script(
        title="t",
        hook="h",
        cta="c",
        beats=[
            Beat(narration="Hello {\\pos(1,1)} world", visual_query="q", seconds=2),
            Beat(narration="Second beat here", visual_query="q", seconds=2),
        ],
    )
    words = [
        [
            WordTiming("Hello", 0.5, 0.9),
            WordTiming("{\\pos(1,1)}", 0.9, 1.0),
            WordTiming("world", 1.0, 1.4),
        ],
        None,
    ]
    ass = build_ass(s, [2.0, 2.0], 1080, 1920, words)
    events = [line for line in ass.splitlines() if line.startswith("Dialogue: 1")]
    assert events[0].startswith("Dialogue: 1,0:00:00.50,")  # starts when speech starts
    assert "{\\kf40}Hello" in events[0] and "{\\kf40}world" in events[0]
    assert "\\pos" not in ass.split("[Events]")[1]  # user text can't inject override tags
    assert re.search(r"Dialogue: 1,0:00:02\.00,", "\n".join(events))  # beat 2 offset


def test_get_transcriber_falls_back_when_unavailable():
    assert get_transcriber("even") is None
    # A model name that can't be downloaded/loaded (offline or bogus) → None, not a crash.
    assert get_transcriber("whisper", model="definitely-not-a-model-xyz") is None


class FakeWhisper:
    def __init__(self):
        self.calls = []

    def words(self, audio: Path):
        self.calls.append(audio.name)
        return [WordTiming("Everyone's", 0.05, 0.4), WordTiming("talking", 0.4, 0.8)]


def test_render_passes_word_timings_to_captions(tmp_path, monkeypatch):
    """Regression: render() must hand each beat's Whisper timings to build_ass."""
    import redblue.video.render as R

    seen = {}
    real = R.build_ass

    def spy(script, durations, width, height, words=None):
        seen["words"] = words
        return real(script, durations, width, height, words)

    monkeypatch.setattr(R, "build_ass", spy)
    s = Script(
        title="t",
        hook="h",
        cta="c",
        beats=[
            Beat(narration="Hello world", visual_query="q", seconds=2),
            Beat(narration="Bye", visual_query="q", seconds=2),
        ],
    )
    timed = [WordTiming("Hello", 0.2, 0.6), WordTiming("world", 0.6, 1.0)]
    R.render(
        s, [R.BeatMedia(words=timed), R.BeatMedia()], tmp_path / "out.mp4", width=320, height=568
    )
    assert seen["words"] == [timed, None]
