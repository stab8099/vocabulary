"""Tests for transcript extraction, candidate selection, the ledger, and cards."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".cursor" / "skills" / "video-vocab" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_known_list  # noqa: E402
import common  # noqa: E402
import extract_text  # noqa: E402
import ledger  # noqa: E402
import render_cards  # noqa: E402
import select_candidates  # noqa: E402
from extract_text import EmbeddedResult  # noqa: E402
from select_candidates import PhraseEntry  # noqa: E402


SRT = """1
00:00:01,000 --> 00:00:03,500
JOHN: I look forward to the serendipity of this quiet afternoon.

2
00:00:04,000 --> 00:00:05,000
[Music]

3
00:00:05,200 --> 00:00:07,000
I was running with Harry.
"""


class SelectionTests(unittest.TestCase):
    def test_lemma_running(self):
        self.assertEqual(common.lemma_of("running"), "run")
        self.assertEqual(common.lemma_of("was"), "be")

    def test_filters_names_acronyms_and_known_lemmas(self):
        segments = [
            {"start": 0, "end": 2, "text": "I was running with Harry yesterday and serendipity arrived."},
            {"start": 3, "end": 5, "text": "The NASA office called."},
            {"start": 6, "end": 8, "text": "Serendipity matters here."},
        ]

        def zipf(word: str) -> float:
            return {"serendipity": 3.2, "harry": 4.0, "nasa": 4.0, "run": 6.0}.get(word, 0.0)

        result = select_candidates.select_from_segments(
            segments,
            known_words={"i", "be", "run", "with", "and", "arrive", "the", "office", "call", "matter", "here", "yesterday"},
            known_phrases=set(),
            blocked_words=set(),
            blocked_phrases=set(),
            tracked_words=set(),
            tracked_phrases=set(),
            phrases=[],
            lemma_fn=common.lemma_of,
            zipf_fn=zipf,
        )
        keys = [item["key"] for item in result["words"]]
        self.assertIn("serendipity", keys)
        self.assertNotIn("run", keys)
        self.assertNotIn("harry", keys)
        self.assertNotIn("nasa", keys)
        serendipity = next(item for item in result["words"] if item["key"] == "serendipity")
        self.assertEqual(serendipity["count"], 2)

    def test_word_cap_keeps_higher_frequency_unknowns(self):
        names = [f"qua{a}{b}" for a in "abcdefghijklmnopqrstuvwxyz" for b in "abc"][:40]
        ranks = {name: 5 - index * 0.05 for index, name in enumerate(names)}
        result = select_candidates.select_from_segments(
            [{"start": 0, "end": 4, "text": " ".join(names)}],
            known_words=set(),
            known_phrases=set(),
            blocked_words=set(),
            blocked_phrases=set(),
            tracked_words={names[0]},
            tracked_phrases=set(),
            phrases=[],
            lemma_fn=lambda word: word,
            zipf_fn=lambda word: ranks.get(word, 0.0),
        )
        self.assertEqual({item["key"] for item in result["words"]}, set(names[1:26]))
        self.assertEqual([item["key"] for item in result["repeats"]], [names[0]])

    def test_phrases_are_verbatim_and_capped(self):
        phrases = [
            PhraseEntry("look forward to", 5, False),
            PhraseEntry("for good", 5, True),
            PhraseEntry("make up one's mind", 5, False),
            PhraseEntry("end up", 5, False),
            PhraseEntry("end up with", 4, False),
        ]
        segments = [
            {"start": 0, "end": 2, "text": "I look forward to it."},
            {"start": 3, "end": 4, "text": "Thanks for good."},
            {"start": 5, "end": 7, "text": "This is for good reasons today."},
            {"start": 8, "end": 10, "text": "Please make up your mind now."},
            {"start": 11, "end": 13, "text": "We end up with nothing special today."},
        ]
        result = select_candidates.select_from_segments(
            segments,
            known_words={"i", "it", "thanks", "this", "is", "reasons", "today", "please", "now", "we", "nothing", "special"},
            known_phrases=set(),
            blocked_words=set(),
            blocked_phrases=set(),
            tracked_words=set(),
            tracked_phrases=set(),
            phrases=phrases,
            zipf_fn=lambda word: 0.0,
        )
        found = {item["key"]: item for item in result["phrases"]}
        self.assertIn("look forward to", found)
        self.assertEqual(found["for good"]["sentence"], "Thanks for good.")
        self.assertNotIn("reasons", found["for good"]["sentence"])
        self.assertEqual(found["make up one's mind"]["surface"].lower(), "make up your mind")
        self.assertIn("end up with", found)
        self.assertNotIn("end up", found)

    def test_phrase_crossing_a_short_gap(self):
        hit = select_candidates.locate_phrase(
            [
                {"start": 0, "end": 1, "text": "I look"},
                {"start": 1.2, "end": 2.4, "text": "forward to dinner tonight"},
            ],
            "look forward to",
        )
        self.assertIsNotNone(hit)
        self.assertIn("forward to", hit["sentence"])

    def test_phrase_limit_drops_lower_priority(self):
        entries = [PhraseEntry(f"kind{i} token", 20 - i, False) for i in range(12)]
        text = " ".join(f"kind{i} token" for i in range(12))
        result = select_candidates.select_from_segments(
            [{"start": 0, "end": 3, "text": text}],
            known_words=set(),
            known_phrases=set(),
            blocked_words=set(),
            blocked_phrases=set(),
            tracked_words=set(),
            tracked_phrases=set(),
            phrases=entries,
            phrase_limit=10,
            zipf_fn=lambda word: 0.0,
        )
        self.assertEqual({item["key"] for item in result["phrases"]}, {f"kind{i} token" for i in range(10)})

    def test_seed_file_and_real_selector(self):
        known_path = common.known_lemmas_path(ROOT)
        self.assertTrue(known_path.exists(), "run build_known_list.py first")
        known = set(common.load_list(known_path))
        self.assertGreaterEqual(len(known), 6000)
        self.assertEqual(common.load_list(known_path)[0], "the")
        self.assertIn("run", known)
        phrases = select_candidates.load_phrases(common.phrase_list_path(ROOT))
        self.assertTrue(any(entry.phrase == "look forward to" for entry in phrases))
        self.assertTrue(any(entry.phrase == "for good" and entry.end for entry in phrases))
        running = select_candidates.select_from_segments(
            [{"start": 0, "end": 2, "text": "I was running with friends and I look forward to tomorrow."}],
            known_words=known,
            known_phrases=set(),
            blocked_words=set(),
            blocked_phrases=set(),
            tracked_words=set(),
            tracked_phrases=set(),
            phrases=phrases,
        )
        keys = [item["key"] for item in running["words"]]
        self.assertNotIn("run", keys)
        self.assertNotIn("running", keys)
        self.assertIn("look forward to", [item["key"] for item in running["phrases"]])
        self.assertLessEqual(len(keys), 25)

        from wordfreq import top_n_list, zipf_frequency

        target = None
        for word in top_n_list("en", 50000):
            if not word.isalpha() or len(word) < 5:
                continue
            lemma = common.lemma_of(word)
            if lemma in known or word in known:
                continue
            if zipf_frequency(lemma, "en") < 2:
                continue
            target = lemma
            break
        self.assertIsNotNone(target)
        found = select_candidates.select_from_segments(
            [{"start": 0, "end": 2, "text": f"We discussed {target} yesterday."}],
            known_words=known,
            known_phrases=set(),
            blocked_words=set(),
            blocked_phrases=set(),
            tracked_words=set(),
            tracked_phrases=set(),
            phrases=[],
        )
        self.assertIn(target, [item["key"] for item in found["words"]])

    def test_compose_keeps_user_words(self):
        text = common.compose_known_file(["the", "run"], ["the", "serendipity"])
        self.assertIn(common.USER_MARKER, text)
        self.assertEqual(text.splitlines().count("the"), 1)
        self.assertIn("serendipity", text)


class ExtractTests(unittest.TestCase):
    def test_sidecar_beats_embedded_and_cleans_captions(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            video = folder / "clip.mp4"
            video.write_bytes(b"not a video")
            (folder / "clip.srt").write_text(SRT, encoding="utf-8")

            def explode(path):
                raise AssertionError("sidecar should win before embedded subtitles are read")

            result = extract_text.extract_segments(video, allow_asr=False, read_embedded=explode)
        self.assertEqual(result.source, "sidecar")
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.segments[0]["text"], "I look forward to the serendipity of this quiet afternoon.")
        self.assertNotIn("Harry", result.segments[0]["text"])
        self.assertIn("Harry", result.segments[1]["text"])

    def test_asr_stub_and_refusals(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "bare.mp4"
            video.write_bytes(b"not a video")
            with self.assertRaises(common.ToolMissing):
                extract_text.extract_segments(
                    video,
                    read_embedded=lambda path: EmbeddedResult(ffmpeg_missing=True),
                )
            with self.assertRaises(common.NoText):
                extract_text.extract_segments(
                    video,
                    allow_asr=False,
                    read_embedded=lambda path: EmbeddedResult(),
                )
            result = extract_text.extract_segments(
                video,
                read_embedded=lambda path: EmbeddedResult(),
                transcribe=lambda path, model: ([{"start": 0.0, "end": 1.0, "text": "Serendipity is rare."}], "zh"),
            )
        self.assertEqual(result.source, "asr")
        self.assertEqual(result.language, "zh")
        self.assertIn("不是英语", result.warning)

    def test_whisper_import_message(self):
        try:
            imported = extract_text.import_whisper_model()
        except common.AsrUnavailable as exc:
            self.assertIn("requirements-asr.txt", str(exc))
            return
        self.assertTrue(callable(imported))

    def test_embedded_subtitle_when_ffmpeg_exists(self):
        try:
            ffmpeg = common.find_tool("ffmpeg")
        except common.ToolMissing:
            self.skipTest("ffmpeg missing")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            srt = folder / "line.srt"
            srt.write_text(
                "1\n00:00:00,200 --> 00:00:01,200\nSerendipity matters.\n\n",
                encoding="utf-8",
            )
            video = folder / "embedded.mp4"
            completed = common.run_tool(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x240:d=2",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=44100:cl=mono",
                    "-i",
                    str(srt),
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-map",
                    "2:s",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-c:s",
                    "mov_text",
                    "-t",
                    "2",
                    str(video),
                ]
            )
            if completed.returncode != 0 or not video.exists():
                self.skipTest(completed.stderr[-400:])
            result = extract_text.extract_segments(video, allow_asr=False)
        self.assertEqual(result.source, "embedded")
        self.assertEqual(result.segments[0]["text"], "Serendipity matters.")


class LedgerCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copy(common.phrase_list_path(ROOT), common.data_dir(self.root) / "phrases.txt")
        common.known_lemmas_path(self.root).write_text("run\nthe\n", encoding="utf-8")
        self.transcript = {
            "slug": "clip-abc123",
            "title": "Clip",
            "video_path": "C:/videos/clip.mp4",
            "source": "sidecar",
            "language": "en",
            "warning": "",
            "segments": [
                {"start": 1, "end": 3, "text": "I look forward to serendipity."},
                {"start": 4, "end": 6, "text": "We discussed <b>bad</b> serendipity yesterday."},
            ],
        }
        self.candidates = {
            "slug": "clip-abc123",
            "words": [
                {
                    "type": "word",
                    "key": "serendipity",
                    "surface": "serendipity",
                    "sentence": "We discussed <b>bad</b> serendipity yesterday.",
                    "start": 4,
                    "end": 6,
                    "count": 2,
                    "zipf": 3.1,
                }
            ],
            "phrases": [
                {
                    "type": "phrase",
                    "key": "look forward to",
                    "surface": "look forward to",
                    "sentence": "I look forward to serendipity.",
                    "start": 1,
                    "end": 3,
                    "count": 1,
                    "priority": 5,
                }
            ],
            "repeats": [],
        }

    def tearDown(self):
        self.temp.cleanup()

    def write_inputs(self):
        transcript_path = self.root / "transcript.json"
        candidates_path = self.root / "candidates.json"
        common.write_json(transcript_path, self.transcript)
        common.write_json(candidates_path, self.candidates)
        return transcript_path, candidates_path

    def test_import_requires_real_glosses(self):
        transcript_path, candidates_path = self.write_inputs()
        glosses = self.root / "gloss.json"
        common.write_json(glosses, [{"key": "serendipity", "zh": "意外之喜"}, {"key": "not-in-video", "zh": "不存在"}])
        with self.assertRaises(SystemExit):
            ledger.import_video(self.root, transcript_path, candidates_path, glosses)
        conn = common.connect(self.root, create=False)
        self.assertIsNotNone(conn)
        try:
            self.assertEqual(conn.execute("select count(*) as n from items").fetchone()["n"], 0)
        finally:
            conn.close()

    def test_cards_ledger_and_known_list(self):
        transcript_path, candidates_path = self.write_inputs()
        glosses = self.root / "gloss.json"
        common.write_json(
            glosses,
            [{"key": "serendipity", "zh": "意外之喜"}, {"key": "look forward to", "zh": "期待"}],
        )
        imported = ledger.import_video(self.root, transcript_path, candidates_path, glosses)
        self.assertEqual(imported["occurrences"], 2)
        listed = ledger.list_items(self.root)
        self.assertEqual({item["key"] for item in listed["items"]}, {"serendipity", "look forward to"})
        rendered = render_cards.render(self.root)
        study = Path(rendered["videos"][0]["html"]).read_text(encoding="utf-8")
        index = Path(rendered["index"]).read_text(encoding="utf-8")
        self.assertIn("serendipity", study)
        self.assertIn("意外之喜", study)
        self.assertIn("期待", index)
        self.assertNotIn("<b>bad</b>", study)
        self.assertIn("\\u003cb", study)
        ledger.set_status(self.root, "serendipity", "learning")
        ledger.set_status(self.root, "look forward to", "known")
        self.assertIn("look forward to", common.load_list(common.known_phrases_path(self.root)))
        again = select_candidates.select_from_segments(
            self.transcript["segments"],
            known_words=set(common.load_list(common.known_lemmas_path(self.root))),
            known_phrases={common.normalize_text(item) for item in common.load_list(common.known_phrases_path(self.root))},
            blocked_words=set(),
            blocked_phrases={"look forward to"},
            tracked_words={"serendipity"},
            tracked_phrases=set(),
            phrases=select_candidates.load_phrases(common.phrase_list_path(self.root)),
            zipf_fn=lambda word: 3.5 if word == "serendipity" else 0.0,
        )
        self.assertEqual(again["words"], [])
        self.assertEqual(again["phrases"], [])
        self.assertEqual(again["repeats"][0]["key"], "serendipity")
        render_cards.render(self.root)
        raw_payload = Path(rendered["index"]).read_text(encoding="utf-8").split('id="payload" type="application/json">', 1)[1].split("</script>", 1)[0]
        overview = json.loads(raw_payload)
        learning = [item for item in overview["items"] if item["status"] == "learning"]
        self.assertEqual([item["key"] for item in learning], ["serendipity"])


if __name__ == "__main__":
    unittest.main()
