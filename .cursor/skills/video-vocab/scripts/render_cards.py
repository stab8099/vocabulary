"""Write self-contained HTML flashcards and the all-video overview."""

from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from pathlib import Path

from common import (
    SOURCE_LABELS,
    cards_dir,
    configure_stdio,
    connect,
    project_root,
)

HERE = Path(__file__).resolve().parent


def embed_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def page_html(title: str, payload: dict) -> str:
    css = (HERE / "card.css").read_text(encoding="utf-8")
    script = (HERE / "card.js").read_text(encoding="utf-8").replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
{css}
</style>
</head>
<body>
<main id="app"></main>
<script id="payload" type="application/json">{embed_json(payload)}</script>
<script>
{script}
</script>
</body>
</html>
"""


def empty_index() -> dict:
    return {
        "mode": "index",
        "videos": [],
        "items": [],
        "stats": {"videos": 0, "learning_words": 0, "learning_phrases": 0, "candidates": 0},
    }


def build_payloads(conn) -> tuple[dict, list[tuple[str, dict]]]:
    videos = [dict(row) for row in conn.execute("select * from videos order by created_at, id")]
    items = [dict(row) for row in conn.execute("select * from items order by item_key")]
    occurrences = [
        dict(row)
        for row in conn.execute(
            """
            select o.*, v.slug as video_slug, v.title as video_title, v.source as video_source
            from occurrences o
            join videos v on v.id = o.video_id
            order by o.start_sec, o.id
            """
        )
    ]
    item_by_id = {item["id"]: item for item in items}
    occ_by_item: dict[int, list[dict]] = defaultdict(list)
    occ_by_video: dict[str, list[dict]] = defaultdict(list)
    for occurrence in occurrences:
        occ_by_item[occurrence["item_id"]].append(occurrence)
        occ_by_video[occurrence["video_slug"]].append(occurrence)

    index_items = []
    for item in items:
        if item["status"] not in ("candidate", "learning"):
            continue
        videos_for = []
        for occurrence in occ_by_item.get(item["id"], []):
            videos_for.append(
                {
                    "slug": occurrence["video_slug"],
                    "title": occurrence["video_title"],
                    "sentence": occurrence["sentence"],
                    "start": occurrence["start_sec"],
                    "end": occurrence["end_sec"],
                    "surface": occurrence["surface"],
                    "source": occurrence["video_source"],
                }
            )
        index_items.append(
            {
                "key": item["item_key"],
                "type": item["item_type"],
                "zh": item["zh"],
                "status": item["status"],
                "videos": videos_for,
            }
        )

    video_cards = []
    study_pages: list[tuple[str, dict]] = []
    for video in videos:
        learning = 0
        candidates = 0
        study_items = []
        for occurrence in occ_by_video.get(video["slug"], []):
            item = item_by_id[occurrence["item_id"]]
            if item["status"] == "learning":
                learning += 1
            elif item["status"] == "candidate":
                candidates += 1
            else:
                continue
            study_items.append(
                {
                    "key": item["item_key"],
                    "type": item["item_type"],
                    "zh": item["zh"],
                    "status": item["status"],
                    "sentence": occurrence["sentence"],
                    "start": occurrence["start_sec"],
                    "end": occurrence["end_sec"],
                    "surface": occurrence["surface"],
                }
            )
        study_items.sort(key=lambda item: (item["start"], item["key"]))
        video_cards.append(
            {
                "slug": video["slug"],
                "title": video["title"],
                "source": video["source"],
                "source_label": SOURCE_LABELS.get(video["source"], video["source"]),
                "warning": video["warning"] or "",
                "path": video["path"],
                "learning": learning,
                "candidates": candidates,
            }
        )
        study_pages.append(
            (
                video["slug"],
                {
                    "mode": "study",
                    "video": video_cards[-1],
                    "items": study_items,
                },
            )
        )

    learning_words = sum(1 for item in items if item["status"] == "learning" and item["item_type"] == "word")
    learning_phrases = sum(1 for item in items if item["status"] == "learning" and item["item_type"] == "phrase")
    candidate_count = sum(1 for item in items if item["status"] == "candidate")
    index = {
        "mode": "index",
        "videos": video_cards,
        "items": index_items,
        "stats": {
            "videos": len(videos),
            "learning_words": learning_words,
            "learning_phrases": learning_phrases,
            "candidates": candidate_count,
        },
    }
    return index, study_pages


def render(root: Path) -> dict:
    folder = cards_dir(root)
    conn = connect(root, create=False)
    if conn is None:
        index_path = folder / "index.html"
        index_path.write_text(page_html("全部视频 · 单词卡", empty_index()), encoding="utf-8", newline="\n")
        return {"index": str(index_path.resolve()), "videos": [], "learning": 0, "candidates": 0}
    try:
        index, study_pages = build_payloads(conn)
    finally:
        conn.close()
    expected = {"index.html"}
    index_path = folder / "index.html"
    index_path.write_text(page_html("全部视频 · 单词卡", index), encoding="utf-8", newline="\n")
    video_outputs = []
    for slug, payload in study_pages:
        filename = f"{slug}.html"
        expected.add(filename)
        path = folder / filename
        title = f"{payload['video']['title']} · 单词卡"
        path.write_text(page_html(title, payload), encoding="utf-8", newline="\n")
        video_outputs.append({"slug": slug, "html": str(path.resolve())})
    for path in folder.glob("*.html"):
        if path.name not in expected:
            path.unlink()
    return {
        "index": str(index_path.resolve()),
        "videos": video_outputs,
        "learning": index["stats"]["learning_words"] + index["stats"]["learning_phrases"],
        "candidates": index["stats"]["candidates"],
    }


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="生成单词卡和总览页。")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    result = render(project_root(args.root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
