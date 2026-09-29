"""Shared paths, ledger schema, and text helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

SOURCE_LABELS = {
    "sidecar": "外挂字幕",
    "embedded": "内嵌字幕",
    "asr": "机器听写",
}
STATUS_LABELS = {
    "candidate": "待决定",
    "learning": "在背",
    "known": "已认识",
    "ignored": "忽略",
}
STATUSES = ("candidate", "learning", "known", "ignored")
WORD_LIMIT = 25
PHRASE_LIMIT = 10
MIN_ZIPF = 2.0
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
USER_MARKER = "# --- user ---"
_TOOL_CACHE: dict[str, str] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    source TEXT NOT NULL,
    language TEXT,
    warning TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    item_key TEXT UNIQUE NOT NULL,
    item_type TEXT NOT NULL,
    zh TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    sentence TEXT NOT NULL,
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    surface TEXT NOT NULL,
    UNIQUE(video_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_occ_video ON occurrences(video_id);
CREATE INDEX IF NOT EXISTS idx_occ_item ON occurrences(item_id);
"""


class ToolMissing(RuntimeError):
    pass


class NoText(RuntimeError):
    pass


class AsrUnavailable(RuntimeError):
    pass


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        skill = parent / ".cursor" / "skills" / "video-vocab"
        if skill.exists():
            return parent
    raise RuntimeError("找不到项目根目录")


def project_root(explicit: Path | None) -> Path:
    if explicit:
        return explicit.resolve()
    return repo_root()


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r'[<>:"/\\|?*]+', "-", name)
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-.")
    name = re.sub(r"[^\w.-]+", "-", name, flags=re.UNICODE)
    name = re.sub(r"-{2,}", "-", name).strip("-.")
    return name or "video"


def slug_for(path: Path) -> str:
    import hashlib

    resolved = str(path.resolve())
    base = slugify(path.stem)
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:6]
    return f"{base}-{digest}"


def data_dir(root: Path) -> Path:
    path = root / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def transcript_path(root: Path, slug: str) -> Path:
    return data_dir(root) / "transcripts" / f"{slug}.json"


def candidates_path(root: Path, slug: str) -> Path:
    return data_dir(root) / "candidates" / f"{slug}.json"


def known_lemmas_path(root: Path) -> Path:
    return root / "data" / "known_lemmas.txt"


def known_phrases_path(root: Path) -> Path:
    return root / "data" / "known_phrases.txt"


def phrase_list_path(root: Path) -> Path:
    return root / "data" / "phrases.txt"


def ledger_path(root: Path) -> Path:
    return root / "data" / "ledger.sqlite"


def cards_dir(root: Path) -> Path:
    path = root / "cards"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def load_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    items: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        items.append(stripped.lower())
    return items


def add_line(path: Path, text: str) -> None:
    text = text.strip().lower()
    if not text or text.startswith("#"):
        return
    current = load_list(path) if path.exists() else []
    if text in current:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n"):
        prefix = "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(prefix + text + "\n")


def remove_line(path: Path, text: str) -> None:
    if not path.exists():
        return
    target = text.strip().lower()
    kept: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.lower() == target and not stripped.startswith("#"):
            continue
        kept.append(line)
    payload = "\n".join(kept)
    if payload:
        payload += "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")


def soften(text: str) -> str:
    return (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def clean_caption(text: str) -> str:
    text = soften(text).replace("♪", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\{[^}]*\}", " ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:[A-Z][A-Z0-9 .]{0,20}:\s*)+", "", text)
    text = re.sub(r"^[-–—]\s+", "", text).strip()
    return text


def normalize_text(text: str) -> str:
    text = soften(text).lower()
    text = re.sub(r"[^a-z0-9']+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clip_sentence(text: str, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def fmt_time(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@lru_cache(maxsize=20000)
def lemma_of(word: str) -> str:
    from simplemma import lemmatize

    token = word.lower()
    try:
        lemma = lemmatize(token, lang="en")
    except Exception:
        return token
    lemma = (lemma or token).lower()
    if not re.fullmatch(r"[a-z]+(?:'[a-z]+)?", lemma):
        return token
    return lemma


def zipf_of(word: str) -> float:
    from wordfreq import zipf_frequency

    return float(zipf_frequency(word, "en"))


def find_tool(name: str) -> str:
    cached = _TOOL_CACHE.get(name)
    if cached:
        return cached
    found = shutil.which(name)
    if found:
        _TOOL_CACHE[name] = found
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    direct = [
        Path(local) / "Microsoft" / "WinGet" / "Links" / f"{name}.exe",
        Path(r"C:\ffmpeg\bin") / f"{name}.exe",
    ]
    program_files = os.environ.get("ProgramFiles", "")
    if program_files:
        direct.append(Path(program_files) / "ffmpeg" / "bin" / f"{name}.exe")
    for path in direct:
        if path.exists():
            _TOOL_CACHE[name] = str(path)
            return str(path)
    packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
    if packages.exists():
        for path in packages.rglob(f"{name}.exe"):
            _TOOL_CACHE[name] = str(path)
            return str(path)
    raise ToolMissing(
        f"找不到 {name}。请安装 FFmpeg 后重开终端："
        " winget install --id Gyan.FFmpeg -e"
    )


def run_tool(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )


def connect(root: Path, create: bool = True) -> sqlite3.Connection | None:
    path = ledger_path(root)
    if not path.exists() and not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    if create:
        conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def compose_known_file(seed: list[str], previous: list[str] | None) -> str:
    extras: list[str] = []
    if previous:
        seed_set = set(seed)
        extras = [word for word in previous if word not in seed_set]
    lines = [
        "# Seed of about 6000 high-frequency English lemmas. You can edit this file.",
        "# Regenerating with --force keeps extra lines that are not part of the new seed.",
        *seed,
    ]
    if extras:
        lines.append(USER_MARKER)
        lines.extend(extras)
    return "\n".join(lines) + "\n"
