"""Store what each video contributed and what the whole library is studying."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import (
    PHRASE_LIMIT,
    STATUSES,
    add_line,
    clip_sentence,
    configure_stdio,
    connect,
    known_lemmas_path,
    known_phrases_path,
    project_root,
    read_json,
    remove_line,
    transaction,
    utcnow,
)
from select_candidates import locate_phrase


def load_glosses(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    data = read_json(path)
    if isinstance(data, dict):
        data = data.get("glosses", data.get("marks", []))
    if not isinstance(data, list):
        raise SystemExit("释义文件必须是 JSON 数组。")
    glosses: dict[str, dict] = {}
    for item in data:
        key = str(item["key"]).strip()
        if key:
            glosses[key] = item
    return glosses


def normalize_entry(entry: dict) -> dict:
    key = str(entry.get("key") or "").strip()
    item_type = entry.get("type")
    sentence = clip_sentence(str(entry.get("sentence") or ""))
    if not key or item_type not in ("word", "phrase") or not sentence:
        raise SystemExit(f"候选条目不完整: {entry!r}")
    return {
        "key": key,
        "type": item_type,
        "sentence": sentence,
        "start": float(entry.get("start") or 0),
        "end": float(entry.get("end") or 0),
        "surface": str(entry.get("surface") or key),
    }


def collect_entries(candidates: dict) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    for entry in [*candidates.get("words", []), *candidates.get("phrases", []), *candidates.get("repeats", [])]:
        normalized = normalize_entry(entry)
        if normalized["key"] in seen:
            continue
        seen.add(normalized["key"])
        entries.append(normalized)
    return entries


def known_path_for(root: Path, item_type: str) -> Path:
    if item_type == "phrase":
        return known_phrases_path(root)
    return known_lemmas_path(root)


def sync_known_file(root: Path, item_type: str, key: str, previous: str, new_status: str) -> None:
    path = known_path_for(root, item_type)
    if new_status == "known":
        add_line(path, key)
    elif previous == "known":
        remove_line(path, key)


def import_video(
    root: Path,
    transcript_file: Path,
    candidates_file: Path,
    glosses_file: Path | None = None,
    allow_empty: bool = False,
) -> dict:
    transcript = read_json(transcript_file)
    candidates = read_json(candidates_file)
    if candidates.get("slug") and candidates["slug"] != transcript["slug"]:
        raise SystemExit("候选文件和文本不是同一支视频。")
    entries = collect_entries(candidates)
    glosses = load_glosses(glosses_file)
    conn = connect(root)
    assert conn is not None
    try:
        with transaction(conn):
            existing_rows = {
                row["item_key"]: row for row in conn.execute("select * from items")
            }
            invented = [
                key for key in glosses if key not in {entry["key"] for entry in entries} and key not in existing_rows
            ]
            if invented:
                raise SystemExit("释义里有文本中不存在的条目:\n" + "\n".join(invented))

            missing: list[str] = []
            ignored = 0
            kept: list[tuple[dict, str]] = []
            for entry in entries:
                gloss = glosses.get(entry["key"], {})
                existing = existing_rows.get(entry["key"])
                if gloss.get("ignore"):
                    ignored += 1
                    _ensure_item(conn, entry["key"], entry["type"], "", "ignored", True)
                    continue
                if existing is not None and existing["status"] in ("known", "ignored"):
                    continue
                zh = str(gloss.get("zh") or "").strip() or (existing["zh"] if existing is not None else "")
                if not zh and not allow_empty:
                    missing.append(entry["key"])
                    continue
                status = existing["status"] if existing is not None else "candidate"
                item_id = _ensure_item(conn, entry["key"], entry["type"], zh, status, existing is None)
                kept.append((entry, item_id))
            if missing:
                raise SystemExit("这些条目还没有中文释义:\n" + "\n".join(missing))

            slug = transcript["slug"]
            now = utcnow()
            video = conn.execute("select * from videos where slug = ?", (slug,)).fetchone()
            if video is None:
                cursor = conn.execute(
                    """
                    insert into videos (slug, title, path, source, language, warning, created_at)
                    values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        transcript.get("title") or slug,
                        transcript.get("video_path") or "",
                        transcript.get("source") or "sidecar",
                        transcript.get("language") or "",
                        transcript.get("warning") or "",
                        now,
                    ),
                )
                video_id = int(cursor.lastrowid)
            else:
                video_id = int(video["id"])
                conn.execute(
                    """
                    update videos
                    set title = ?, path = ?, source = ?, language = ?, warning = ?
                    where id = ?
                    """,
                    (
                        transcript.get("title") or video["title"],
                        transcript.get("video_path") or video["path"],
                        transcript.get("source") or video["source"],
                        transcript.get("language") or "",
                        transcript.get("warning") or "",
                        video_id,
                    ),
                )
            conn.execute("delete from occurrences where video_id = ?", (video_id,))
            for entry, item_id in kept:
                conn.execute(
                    """
                    insert into occurrences (video_id, item_id, sentence, start_sec, end_sec, surface)
                    values (?, ?, ?, ?, ?, ?)
                    on conflict(video_id, item_id) do update set
                        sentence = excluded.sentence,
                        start_sec = excluded.start_sec,
                        end_sec = excluded.end_sec,
                        surface = excluded.surface
                    """,
                    (video_id, item_id, entry["sentence"], entry["start"], entry["end"], entry["surface"]),
                )
            conn.execute(
                """
                delete from items
                where status = 'candidate'
                  and id not in (select item_id from occurrences)
                """
            )
        learning = conn.execute("select count(*) as n from items where status = 'learning'").fetchone()["n"]
        queued = conn.execute("select count(*) as n from items where status = 'candidate'").fetchone()["n"]
        return {
            "slug": slug,
            "occurrences": len(kept),
            "ignored": ignored,
            "learning": learning,
            "candidates": queued,
        }
    finally:
        conn.close()


def _ensure_item(conn, key: str, item_type: str, zh: str, status: str, overwrite_status: bool) -> int:
    now = utcnow()
    row = conn.execute("select * from items where item_key = ?", (key,)).fetchone()
    if row is None:
        cursor = conn.execute(
            """
            insert into items (item_key, item_type, zh, status, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (key, item_type, zh or "", status, now, now),
        )
        return int(cursor.lastrowid)
    assignments: list[str] = []
    values: list[object] = []
    if zh:
        assignments.append("zh = ?")
        values.append(zh)
    if overwrite_status and status != row["status"]:
        assignments.append("status = ?")
        values.append(status)
    if assignments:
        assignments.append("updated_at = ?")
        values.append(now)
        values.append(row["id"])
        conn.execute(f"update items set {', '.join(assignments)} where id = ?", values)
    return int(row["id"])


def set_status(root: Path, key: str, status: str) -> dict:
    if status not in STATUSES:
        raise SystemExit(f"不认识的状态: {status}")
    conn = connect(root)
    assert conn is not None
    try:
        row = conn.execute("select * from items where item_key = ?", (key,)).fetchone()
        if row is None:
            raise SystemExit(f"账本里没有这条: {key}")
        previous = row["status"]
        with transaction(conn):
            conn.execute(
                "update items set status = ?, updated_at = ? where id = ?",
                (status, utcnow(), row["id"]),
            )
        sync_known_file(root, row["item_type"], key, previous, status)
        return {"key": key, "status": status, "previous": previous}
    finally:
        conn.close()


def import_marks(root: Path, path: Path) -> dict:
    data = read_json(path)
    marks = data if isinstance(data, list) else data.get("marks", [])
    if not isinstance(marks, list):
        raise SystemExit("标记文件格式不对。")
    conn = connect(root)
    assert conn is not None
    planned: list[tuple[object, str]] = []
    missing: list[str] = []
    try:
        for mark in marks:
            key = str(mark["key"]).strip()
            status = mark["status"]
            if status not in STATUSES:
                raise SystemExit(f"不认识的状态: {status}")
            row = conn.execute("select * from items where item_key = ?", (key,)).fetchone()
            if row is None:
                missing.append(key)
                continue
            planned.append((row, status))
        if missing:
            raise SystemExit("账本里没有这些条目:\n" + "\n".join(missing))
        with transaction(conn):
            now = utcnow()
            for row, status in planned:
                conn.execute(
                    "update items set status = ?, updated_at = ? where id = ?",
                    (status, now, row["id"]),
                )
        for row, status in planned:
            sync_known_file(root, row["item_type"], row["item_key"], row["status"], status)
        return {"updated": len(planned)}
    finally:
        conn.close()


def add_phrase(root: Path, transcript_file: Path, phrase: str, zh: str, phrase_limit: int = PHRASE_LIMIT) -> dict:
    zh = zh.strip()
    if not zh:
        raise SystemExit("补充短语需要 --zh。")
    transcript = read_json(transcript_file)
    hit = locate_phrase(transcript.get("segments") or [], phrase)
    if not hit:
        raise SystemExit(f"原文里没有这个短语: {phrase}")
    conn = connect(root)
    assert conn is not None
    try:
        with transaction(conn):
            video = conn.execute("select * from videos where slug = ?", (transcript["slug"],)).fetchone()
            if video is None:
                raise SystemExit("请先导入这支视频，再补充短语。")
            key = hit["key"]
            row = conn.execute("select * from items where item_key = ?", (key,)).fetchone()
            if row is not None and row["status"] in ("known", "ignored"):
                raise SystemExit("这条短语已经标成认识或忽略，不会再加入。")
            already = conn.execute(
                """
                select o.id from occurrences o
                join items i on i.id = o.item_id
                where o.video_id = ? and i.item_key = ?
                """,
                (video["id"], key),
            ).fetchone()
            if already is None:
                count = conn.execute(
                    """
                    select count(*) as n from occurrences o
                    join items i on i.id = o.item_id
                    where o.video_id = ? and i.item_type = 'phrase' and i.status != 'ignored'
                    """,
                    (video["id"],),
                ).fetchone()["n"]
                if count >= phrase_limit:
                    raise SystemExit(f"这支视频的短语已经有 {phrase_limit} 条。")
            item_id = _ensure_item(
                conn,
                key,
                "phrase",
                zh,
                row["status"] if row is not None else "candidate",
                row is None,
            )
            conn.execute(
                """
                insert into occurrences (video_id, item_id, sentence, start_sec, end_sec, surface)
                values (?, ?, ?, ?, ?, ?)
                on conflict(video_id, item_id) do update set
                    sentence = excluded.sentence,
                    start_sec = excluded.start_sec,
                    end_sec = excluded.end_sec,
                    surface = excluded.surface
                """,
                (video["id"], item_id, hit["sentence"], hit["start"], hit["end"], hit["surface"]),
            )
        return {"key": key, "sentence": hit["sentence"], "start": hit["start"]}
    finally:
        conn.close()


def list_items(root: Path, video_slug: str | None = None, statuses: set[str] | None = None) -> dict:
    conn = connect(root, create=False)
    if conn is None:
        return {"videos": 0, "learning_words": 0, "learning_phrases": 0, "candidates": 0, "items": []}
    try:
        status_filter = statuses or {"learning", "candidate"}
        rows = conn.execute(
            """
            select i.*, o.sentence, o.start_sec, o.surface, v.slug as video_slug, v.title as video_title
            from items i
            left join occurrences o on o.item_id = i.id
            left join videos v on v.id = o.video_id
            order by i.item_key, o.start_sec
            """
        ).fetchall()
        grouped: dict[str, dict] = {}
        for row in rows:
            if row["status"] not in status_filter:
                continue
            item = grouped.get(row["item_key"])
            if item is None:
                item = {
                    "key": row["item_key"],
                    "type": row["item_type"],
                    "zh": row["zh"],
                    "status": row["status"],
                    "videos": [],
                }
                grouped[row["item_key"]] = item
            if row["video_slug"] and (video_slug is None or row["video_slug"] == video_slug):
                item["videos"].append(
                    {
                        "slug": row["video_slug"],
                        "title": row["video_title"],
                        "sentence": row["sentence"],
                        "start": row["start_sec"],
                        "surface": row["surface"],
                    }
                )
        if video_slug:
            grouped = {key: item for key, item in grouped.items() if item["videos"]}
        video_count = conn.execute("select count(*) as n from videos").fetchone()["n"]
        learning_words = conn.execute(
            "select count(*) as n from items where status = 'learning' and item_type = 'word'"
        ).fetchone()["n"]
        learning_phrases = conn.execute(
            "select count(*) as n from items where status = 'learning' and item_type = 'phrase'"
        ).fetchone()["n"]
        candidates = conn.execute("select count(*) as n from items where status = 'candidate'").fetchone()["n"]
        return {
            "videos": video_count,
            "learning_words": learning_words,
            "learning_phrases": learning_phrases,
            "candidates": candidates,
            "items": list(grouped.values()),
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="记录每支视频和全部视频的单词与短语。")
    parser.add_argument("--root", type=Path)
    sub = parser.add_subparsers(dest="cmd", required=True)

    import_cmd = sub.add_parser("import-video")
    import_cmd.add_argument("--transcript", type=Path, required=True)
    import_cmd.add_argument("--candidates", type=Path, required=True)
    import_cmd.add_argument("--glosses", type=Path)
    import_cmd.add_argument("--allow-empty-gloss", action="store_true")

    phrase_cmd = sub.add_parser("add-phrase")
    phrase_cmd.add_argument("--transcript", type=Path, required=True)
    phrase_cmd.add_argument("--phrase", required=True)
    phrase_cmd.add_argument("--zh", required=True)
    phrase_cmd.add_argument("--phrase-limit", type=int, default=PHRASE_LIMIT)

    mark_cmd = sub.add_parser("mark")
    mark_cmd.add_argument("--key", required=True)
    mark_cmd.add_argument("--status", required=True, choices=STATUSES)

    marks_cmd = sub.add_parser("import-marks")
    marks_cmd.add_argument("path", type=Path)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--video")
    list_cmd.add_argument("--status", default="learning,candidate")

    args = parser.parse_args(argv)
    root = project_root(args.root)
    if args.cmd == "import-video":
        result = import_video(root, args.transcript, args.candidates, args.glosses, args.allow_empty_gloss)
    elif args.cmd == "add-phrase":
        result = add_phrase(root, args.transcript, args.phrase, args.zh, args.phrase_limit)
    elif args.cmd == "mark":
        result = set_status(root, args.key.strip(), args.status)
    elif args.cmd == "import-marks":
        result = import_marks(root, args.path)
    else:
        statuses = {item.strip() for item in args.status.split(",") if item.strip()}
        result = list_items(root, args.video, statuses)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
