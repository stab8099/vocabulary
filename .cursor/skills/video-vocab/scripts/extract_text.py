"""Extract an English transcript from a video or a subtitle file."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from common import (
    AsrUnavailable,
    NoText,
    ToolMissing,
    clean_caption,
    configure_stdio,
    find_tool,
    project_root,
    run_tool,
    slug_for,
    transcript_path,
    write_json,
)

SUB_EXTS = {".srt", ".vtt", ".ass", ".ssa"}
MEDIA_HINTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"}
TEXT_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text", "srt", "hdmv_text_subtitle"}
BITMAP_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}


@dataclass
class EmbeddedResult:
    segments: list[dict] | None = None
    bitmap_only: bool = False
    ffmpeg_missing: bool = False


@dataclass
class ExtractResult:
    slug: str
    title: str
    video_path: str
    source: str
    language: str
    warning: str
    segments: list[dict]


def load_subtitle_file(path: Path) -> list[dict]:
    try:
        import pysubs2
    except ImportError as exc:
        raise SystemExit("缺少 pysubs2。请先运行 pip install -r requirements.txt") from exc
    subs = None
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp936"):
        try:
            subs = pysubs2.load(str(path), encoding=encoding)
            break
        except Exception as exc:
            last_error = exc
    if subs is None:
        raise SystemExit(f"无法读取字幕 {path}: {last_error}")
    segments: list[dict] = []
    for event in subs:
        raw = event.plaintext if hasattr(event, "plaintext") else event.text
        text = clean_caption(raw)
        if not text or not re.search(r"[A-Za-z]", text):
            continue
        segments.append(
            {"start": round(event.start / 1000.0, 3), "end": round(event.end / 1000.0, 3), "text": text}
        )
    if not segments:
        raise NoText(f"字幕里没有可用的英文句子: {path}")
    return segments


def find_sidecar(video: Path) -> Path | None:
    candidates: list[Path] = []
    for ext in (".srt", ".vtt", ".ass", ".ssa"):
        candidates.append(video.with_suffix(ext))
        candidates.append(Path(str(video) + ext))
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None


def read_embedded_subtitles(video: Path) -> EmbeddedResult:
    try:
        ffprobe = find_tool("ffprobe")
        ffmpeg = find_tool("ffmpeg")
    except ToolMissing:
        return EmbeddedResult(ffmpeg_missing=True)
    probe = run_tool(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream=index,codec_name:stream_tags=language",
            "-of",
            "json",
            str(video),
        ]
    )
    if probe.returncode != 0:
        return EmbeddedResult()
    try:
        payload = json.loads(probe.stdout or "{}")
    except json.JSONDecodeError:
        return EmbeddedResult()
    streams = payload.get("streams") or []
    if not streams:
        return EmbeddedResult()
    text_streams = [stream for stream in streams if (stream.get("codec_name") or "") in TEXT_CODECS]
    bitmap_streams = [stream for stream in streams if (stream.get("codec_name") or "") in BITMAP_CODECS]
    if not text_streams and bitmap_streams:
        return EmbeddedResult(bitmap_only=True)

    def sort_key(stream: dict) -> tuple[int, int]:
        lang = ((stream.get("tags") or {}).get("language") or "").lower()
        english = lang in {"en", "eng", "en-us", "en-gb"}
        return (0 if english else 1, int(stream.get("index") or 0))

    ordered = sorted(text_streams or streams, key=sort_key)
    for stream in ordered:
        codec = stream.get("codec_name") or ""
        if codec in BITMAP_CODECS:
            continue
        index = stream.get("index")
        if index is None:
            continue
        with tempfile.TemporaryDirectory() as temp_dir:
            dest = Path(temp_dir) / "embedded.srt"
            extracted = run_tool(
                [ffmpeg, "-y", "-i", str(video), "-map", f"0:{index}", "-f", "srt", str(dest)]
            )
            if extracted.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
                continue
            try:
                segments = load_subtitle_file(dest)
            except NoText:
                continue
            except SystemExit as exc:
                if "pysubs2" in str(exc):
                    raise
                continue
            if segments:
                return EmbeddedResult(segments=segments)
    if bitmap_streams and not text_streams:
        return EmbeddedResult(bitmap_only=True)
    return EmbeddedResult()


def import_whisper_model():
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AsrUnavailable(
            "没有字幕，需要本机听写，但还没安装听写库。"
            "请运行：.venv/Scripts/python -m pip install -r requirements-asr.txt"
        ) from exc
    return WhisperModel


def transcribe_media(path: Path, model_size: str = "small", loader=None) -> tuple[list[dict], str]:
    if loader is None:
        from common import repo_root

        whisper_model = import_whisper_model()
        download_root = repo_root() / "models" / "whisper"
        download_root.mkdir(parents=True, exist_ok=True)

        def loader(whisper_model=whisper_model, download_root=download_root, model_size=model_size):
            return whisper_model(
                model_size,
                device="cpu",
                compute_type="int8",
                download_root=str(download_root),
            )

    model = loader()
    raw_segments, info = model.transcribe(
        str(path),
        vad_filter=False,
        beam_size=5,
        condition_on_previous_text=False,
    )
    segments: list[dict] = []
    for segment in raw_segments:
        text = clean_caption(segment.text)
        if not text:
            continue
        segments.append(
            {
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "text": text,
            }
        )
    language = getattr(info, "language", "") or ""
    if not segments:
        raise NoText("听写没有得到文字。")
    return segments, language


def extract_segments(
    video: Path,
    subtitle: Path | None = None,
    *,
    allow_asr: bool = True,
    asr_model: str = "small",
    read_embedded=None,
    transcribe=None,
) -> ExtractResult:
    if not video.exists():
        raise NoText(f"找不到文件: {video}")
    slug = slug_for(video)
    title = video.stem
    resolved = str(video.resolve())
    if video.suffix.lower() in SUB_EXTS and subtitle is None:
        return ExtractResult(slug, title, resolved, "sidecar", "en", "", load_subtitle_file(video))
    if subtitle is not None:
        if not subtitle.exists():
            raise NoText(f"找不到字幕: {subtitle}")
        return ExtractResult(slug, title, resolved, "sidecar", "en", "", load_subtitle_file(subtitle))
    sidecar = find_sidecar(video)
    if sidecar is not None:
        return ExtractResult(slug, title, resolved, "sidecar", "en", "", load_subtitle_file(sidecar))

    reader = read_embedded or read_embedded_subtitles
    embedded = reader(video)
    if embedded.ffmpeg_missing:
        raise ToolMissing(
            "没有外挂字幕，也找不到 ffmpeg，所以没法检查视频里的内嵌字幕。"
            "请安装 FFmpeg 后重开终端： winget install --id Gyan.FFmpeg -e"
        )
    if embedded.segments:
        return ExtractResult(slug, title, resolved, "embedded", "en", "", embedded.segments)
    if not allow_asr:
        if embedded.bitmap_only:
            raise NoText("只有图片字幕，读不出文字。去掉 --no-asr 并安装 requirements-asr.txt 后可以听写。")
        raise NoText("没有找到可用字幕。去掉 --no-asr 并安装 requirements-asr.txt 后可以听写。")

    warning = ""
    if embedded.bitmap_only:
        warning = "内嵌字幕是图片，已改为机器听写。请对照原句检查。"
    engine = transcribe or transcribe_media
    segments, language = engine(video, asr_model)
    if language and language != "en":
        extra = f"听写识别到的语言是 {language}，不是英语。卡片可能不可靠。"
        warning = f"{warning} {extra}".strip()
    if not warning:
        warning = "这些句子来自机器听写。如果不对，请在卡片上点忽略。"
    return ExtractResult(slug, title, resolved, "asr", language or "", warning, segments)


def payload_from(result: ExtractResult) -> dict:
    return {
        "slug": result.slug,
        "title": result.title,
        "video_path": result.video_path,
        "source": result.source,
        "language": result.language,
        "warning": result.warning,
        "segments": result.segments,
    }


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description="从视频或字幕抽出带时间点的英文文本。")
    parser.add_argument("video", type=Path)
    parser.add_argument("--subtitle", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--no-asr", action="store_true")
    parser.add_argument("--asr-model", default="small")
    args = parser.parse_args(argv)
    root = project_root(args.root)
    try:
        result = extract_segments(
            args.video,
            args.subtitle,
            allow_asr=not args.no_asr,
            asr_model=args.asr_model,
        )
    except ToolMissing as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except NoText as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except AsrUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 4
    out = args.out or transcript_path(root, result.slug)
    payload = payload_from(result)
    write_json(out, payload)
    if result.warning:
        print(result.warning, file=sys.stderr)
    summary = {
        "slug": result.slug,
        "title": result.title,
        "source": result.source,
        "language": result.language,
        "warning": result.warning,
        "segments": len(result.segments),
        "out": str(out.resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
