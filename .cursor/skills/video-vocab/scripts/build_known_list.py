"""Build the editable list of about 6000 known English lemmas."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from common import (
    compose_known_file,
    configure_stdio,
    known_lemmas_path,
    lemma_of,
    load_list,
    project_root,
    zipf_of,
)

TOKEN_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")


def generate_seed_lemmas(limit: int = 6000) -> list[str]:
    from wordfreq import top_n_list

    lemmas: list[str] = []
    seen: set[str] = set()
    for token in top_n_list("en", 80000):
        if not TOKEN_RE.fullmatch(token):
            continue
        lemma = lemma_of(token)
        if lemma in seen or not TOKEN_RE.fullmatch(lemma):
            continue
        seen.add(lemma)
        lemmas.append(lemma)
        if len(lemmas) >= limit:
            return lemmas
    raise SystemExit(f"只收集到 {len(lemmas)} 个词条，不足 {limit}")


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="生成大约 6000 个高频英语词条，作为「假设已认识」的词表。")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=6000)
    args = parser.parse_args(argv)
    root = project_root(args.root)
    path = known_lemmas_path(root)
    if path.exists() and not args.force:
        print(f"{path} 已存在。要重新生成请加 --force。你额外加上的词会被保留。")
        return 0
    seed = generate_seed_lemmas(args.limit)
    previous = load_list(path) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(compose_known_file(seed, previous), encoding="utf-8", newline="\n")
    last = seed[-1]
    print(f"wrote {len(seed)} lemmas to {path} ; last={last} ; zipf={zipf_of(last):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
