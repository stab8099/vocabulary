"""Choose unknown words and verbatim phrases from a transcript."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from common import (
    MIN_ZIPF,
    PHRASE_LIMIT,
    WORD_LIMIT,
    candidates_path,
    clip_sentence,
    configure_stdio,
    connect,
    known_lemmas_path,
    known_phrases_path,
    lemma_of,
    load_list,
    normalize_text,
    phrase_list_path,
    project_root,
    read_json,
    soften,
    write_json,
    zipf_of,
)

TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
LEMMA_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")
VOWEL_RE = re.compile(r"[aeiouy]")
POSSESSIVES = ("my", "your", "his", "her", "its", "our", "their")
SOMEONES = ("someone", "somebody", "me", "you", "him", "her", "us", "them")


@dataclass(frozen=True)
class PhraseEntry:
    phrase: str
    priority: int
    end: bool


@dataclass
class Hit:
    key: str
    priority: int
    surface: str
    sentence: str
    start: float
    end: float


def load_phrases(path: Path) -> list[PhraseEntry]:
    if not path.exists():
        raise SystemExit(f"找不到短语表: {path}")
    entries: list[PhraseEntry] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        match = re.match(r"^(\d+)[ \t]+(.+?)(?:[ \t]+(end))?$", raw)
        if match:
            priority = int(match.group(1))
            phrase_raw = match.group(2).strip()
            flags = {match.group(3)} if match.group(3) else set()
        else:
            priority = 3
            phrase_raw = raw
            flags = set()
        phrase = normalize_text(phrase_raw)
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        entries.append(PhraseEntry(phrase, priority, "end" in flags))
    return entries


def expand_variants(phrase: str) -> list[str]:
    variants = [phrase]
    if "one's" in phrase:
        variants = [item.replace("one's", pronoun) for item in variants for pronoun in POSSESSIVES]
    if "someone" in phrase:
        variants = [item.replace("someone", pronoun) for item in variants for pronoun in SOMEONES]
    expanded: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        normalized = normalize_text(variant)
        if normalized and normalized not in seen:
            seen.add(normalized)
            expanded.append(normalized)
    return expanded


def original_slice(text: str, variant: str) -> str | None:
    words = variant.split()
    if not words:
        return None
    pattern = r"\b" + r"\s+".join(re.escape(word) for word in words) + r"\b"
    match = re.search(pattern, soften(text), flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0)


def find_spans(normalized: str, variant: str, end_only: bool) -> list[tuple[int, int]]:
    if end_only:
        if normalized == variant or normalized.endswith(" " + variant):
            start = len(normalized) - len(variant)
            return [(start, start + len(variant))]
        return []
    spans: list[tuple[int, int]] = []
    padded = f" {normalized} "
    needle = f" {variant} "
    cursor = 0
    while True:
        pos = padded.find(needle, cursor)
        if pos < 0:
            break
        spans.append((pos, pos + len(variant)))
        cursor = pos + len(needle)
    return spans


def match_phrases(segments: list[dict], entries: list[PhraseEntry]) -> list[Hit]:
    hits: list[Hit] = []
    for segment in segments:
        sentence = segment["text"]
        normalized = normalize_text(sentence)
        if not normalized:
            continue
        found: list[tuple[int, int, PhraseEntry, str]] = []
        for entry in entries:
            for variant in expand_variants(entry.phrase):
                surface = None
                for start, end in find_spans(normalized, variant, entry.end):
                    if surface is None:
                        surface = original_slice(sentence, variant)
                    if not surface:
                        continue
                    found.append((start, end, entry, surface))
        found.sort(key=lambda item: (-(item[1] - item[0]), -item[2].priority, item[0]))
        occupied: list[tuple[int, int]] = []
        for start, end, entry, surface in found:
            if any(not (end <= left or start >= right) for left, right in occupied):
                continue
            occupied.append((start, end))
            hits.append(
                Hit(
                    key=entry.phrase,
                    priority=entry.priority,
                    surface=surface,
                    sentence=sentence,
                    start=float(segment["start"]),
                    end=float(segment["end"]),
                )
            )
    return hits


def adjacent_windows(segments: list[dict]) -> list[dict]:
    windows: list[dict] = []
    for left, right in zip(segments, segments[1:]):
        if float(right["start"]) - float(left["end"]) <= 1.5:
            windows.append(
                {
                    "start": left["start"],
                    "end": right["end"],
                    "text": f"{left['text']} {right['text']}",
                }
            )
    return windows


def suppress_shorter_overlaps(hits: list[Hit]) -> list[Hit]:
    kept: list[Hit] = []
    for hit in hits:
        embedded = False
        for other in hits:
            if other is hit or not other.key.startswith(hit.key + " "):
                continue
            sentence = hit.sentence.lower()
            other_sentence = other.sentence.lower()
            nested = hit.start >= other.start and hit.end <= other.end
            if other.surface.lower() in sentence or sentence in other_sentence or other_sentence in sentence or nested:
                embedded = True
                break
        if not embedded:
            kept.append(hit)
    return kept


def dedupe_hits(hits: list[Hit]) -> list[dict]:
    grouped: dict[str, list[Hit]] = defaultdict(list)
    for hit in hits:
        grouped[hit.key].append(hit)
    results: list[dict] = []
    for key, group in grouped.items():
        group.sort(key=lambda item: (item.start, item.end))
        rich = [item for item in group if len(item.sentence.split()) >= 6]
        chosen = (rich or group)[0]
        results.append(
            {
                "type": "phrase",
                "key": key,
                "surface": chosen.surface,
                "sentence": clip_sentence(chosen.sentence),
                "start": chosen.start,
                "end": chosen.end,
                "count": len(group),
                "priority": max(item.priority for item in group),
            }
        )
    return results


def locate_phrase(segments: list[dict], phrase: str) -> dict | None:
    entry = PhraseEntry(normalize_text(phrase), 5, False)
    if not entry.phrase:
        return None
    found = dedupe_hits(match_phrases(segments, [entry]))
    if found:
        return found[0]
    found = dedupe_hits(match_phrases(adjacent_windows(segments), [entry]))
    return found[0] if found else None


def iter_tokens(text: str):
    previous = 0
    for match in TOKEN_RE.finditer(text):
        gap = text[previous : match.start()]
        sentence_initial = previous == 0 or bool(re.search(r"[.!?]", gap))
        previous = match.end()
        yield match.group(0), sentence_initial


def choose_occurrence(occurrences: list[dict]) -> dict:
    ordered = sorted(occurrences, key=lambda item: item["start"])
    rich = [item for item in ordered if len(item["sentence"].split()) >= 6]
    return (rich or ordered)[0]


def word_entry(lemma: str, occurrences: list[dict], zipf: float | None) -> dict:
    chosen = choose_occurrence(occurrences)
    entry = {
        "type": "word",
        "key": lemma,
        "surface": chosen["surface"],
        "sentence": clip_sentence(chosen["sentence"]),
        "start": chosen["start"],
        "end": chosen["end"],
        "count": len(occurrences),
    }
    if zipf is not None:
        entry["zipf"] = round(zipf, 3)
    return entry


def select_from_segments(
    segments: list[dict],
    *,
    known_words: set[str],
    known_phrases: set[str],
    blocked_words: set[str],
    blocked_phrases: set[str],
    tracked_words: set[str],
    tracked_phrases: set[str],
    phrases: list[PhraseEntry],
    word_limit: int = WORD_LIMIT,
    phrase_limit: int = PHRASE_LIMIT,
    min_zipf: float = MIN_ZIPF,
    lemma_fn=None,
    zipf_fn=None,
) -> dict:
    lemma_fn = lemma_fn or lemma_of
    zipf_fn = zipf_fn or zipf_of
    new_groups: dict[str, list[dict]] = defaultdict(list)
    repeat_groups: dict[str, list[dict]] = defaultdict(list)

    for segment in segments:
        sentence = segment["text"]
        for surface, sentence_initial in iter_tokens(sentence):
            lower = surface.lower()
            lemma = lemma_fn(lower)
            occurrence = {
                "surface": surface,
                "sentence": sentence,
                "start": float(segment["start"]),
                "end": float(segment["end"]),
            }
            if lemma in known_words or lower in known_words or lemma in blocked_words or lower in blocked_words:
                continue
            if lemma in tracked_words or lower in tracked_words:
                repeat_groups[lemma].append(occurrence)
                continue
            is_acronym = surface.isupper() and surface.isalpha() and len(surface) >= 2
            is_proper = (not sentence_initial) and surface[:1].isupper() and not surface.isupper()
            if is_acronym or is_proper or len(lemma) < 3 or not VOWEL_RE.search(lemma) or not LEMMA_RE.fullmatch(lemma):
                continue
            new_groups[lemma].append(occurrence)

    eligible: list[dict] = []
    for lemma, occurrences in new_groups.items():
        score = float(zipf_fn(lemma))
        if score < min_zipf:
            continue
        eligible.append(word_entry(lemma, occurrences, score))
    eligible.sort(key=lambda item: (-item["zipf"], -item["count"], item["key"]))
    words = eligible[:word_limit]
    repeats = [word_entry(lemma, occurrences, None) for lemma, occurrences in repeat_groups.items()]

    listed = {entry.phrase for entry in phrases}
    search_entries = list(phrases)
    for key in tracked_phrases:
        normalized = normalize_text(key)
        if normalized and normalized not in listed:
            search_entries.append(PhraseEntry(normalized, 0, False))
    phrase_hits = match_phrases(segments, search_entries)
    found_keys = {hit.key for hit in phrase_hits}
    missing = [entry for entry in search_entries if entry.phrase not in found_keys]
    if missing:
        phrase_hits.extend(match_phrases(adjacent_windows(segments), missing))
    phrase_items = dedupe_hits(suppress_shorter_overlaps(phrase_hits))

    new_phrases: list[dict] = []
    for item in phrase_items:
        key = item["key"]
        if key in known_phrases or key in blocked_phrases:
            continue
        if key in tracked_phrases:
            item["type"] = "phrase"
            repeats.append(item)
            continue
        new_phrases.append(item)
    new_phrases.sort(key=lambda item: (-item["priority"], item["start"], item["key"]))
    phrases_out = new_phrases[:phrase_limit]

    def chronological(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda item: (item["start"], item["key"]))

    return {
        "words": chronological(words),
        "phrases": chronological(phrases_out),
        "repeats": chronological(repeats),
    }


def ledger_sets(root: Path) -> tuple[set[str], set[str], set[str], set[str]]:
    conn = connect(root, create=False)
    blocked_words: set[str] = set()
    blocked_phrases: set[str] = set()
    tracked_words: set[str] = set()
    tracked_phrases: set[str] = set()
    if conn is None:
        return blocked_words, blocked_phrases, tracked_words, tracked_phrases
    try:
        for row in conn.execute("select item_key, item_type, status from items"):
            if row["status"] in ("known", "ignored"):
                target_words, target_phrases = blocked_words, blocked_phrases
            elif row["status"] in ("candidate", "learning"):
                target_words, target_phrases = tracked_words, tracked_phrases
            else:
                continue
            if row["item_type"] == "phrase":
                target_phrases.add(row["item_key"])
            else:
                target_words.add(row["item_key"])
    finally:
        conn.close()
    return blocked_words, blocked_phrases, tracked_words, tracked_phrases


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="按已知词表从文本里挑生词和短语。")
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--word-limit", type=int, default=WORD_LIMIT)
    parser.add_argument("--phrase-limit", type=int, default=PHRASE_LIMIT)
    parser.add_argument("--min-zipf", type=float, default=MIN_ZIPF)
    parser.add_argument("--verify")
    args = parser.parse_args(argv)
    root = project_root(args.root)
    transcript = read_json(args.transcript)
    segments = transcript.get("segments") or []
    if args.verify:
        hit = locate_phrase(segments, args.verify)
        if not hit:
            print(json.dumps({"ok": False, "phrase": args.verify}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, **hit}, ensure_ascii=False))
        return 0

    known_path = known_lemmas_path(root)
    if not known_path.exists():
        raise SystemExit(f"找不到 {known_path}。请先运行 build_known_list.py。")
    blocked_words, blocked_phrases, tracked_words, tracked_phrases = ledger_sets(root)
    selected = select_from_segments(
        segments,
        known_words=set(load_list(known_path)),
        known_phrases={normalize_text(item) for item in load_list(known_phrases_path(root))},
        blocked_words=blocked_words,
        blocked_phrases=blocked_phrases,
        tracked_words=tracked_words,
        tracked_phrases=tracked_phrases,
        phrases=load_phrases(phrase_list_path(root)),
        word_limit=args.word_limit,
        phrase_limit=args.phrase_limit,
        min_zipf=args.min_zipf,
    )
    payload = {
        "slug": transcript["slug"],
        "title": transcript.get("title") or "",
        "video_path": transcript.get("video_path") or "",
        "source": transcript.get("source") or "",
        "warning": transcript.get("warning") or "",
        **selected,
    }
    out = args.out or candidates_path(root, transcript["slug"])
    write_json(out, payload)
    print(json.dumps({"out": str(out.resolve()), **payload}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
