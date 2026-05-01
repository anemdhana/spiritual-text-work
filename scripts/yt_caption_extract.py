from __future__ import annotations

"""yt_caption_extract.py — Download YouTube captions and extract a time-ranged transcript
with speaker labels and timestamps.

Usage:
    python yt_caption_extract.py <video_id> [--start HH:MM:SS] [--end HH:MM:SS] [OPTIONS]
    python yt_caption_extract.py --playlist-id <playlist_id> [OPTIONS]
    python yt_caption_extract.py --config config/media-config.properties

Examples:
    python yt_caption_extract.py dQw4w9WgXcQ
    python yt_caption_extract.py dQw4w9WgXcQ --start 00:02:00 --end 00:10:00 --lang hi
    python yt_caption_extract.py dQw4w9WgXcQ --lang en,hi
    python yt_caption_extract.py --playlist-id PLT6lIcOhPFQoNxf1If3b7ExFzoHiOjkbG
    python yt_caption_extract.py --config config/media-config.properties

Output format (same as timestamped transcripts in this project):
    [HH:MM:SS.mmm -> HH:MM:SS.mmm] Speaker: text

Dependencies:
    pip install yt-dlp
"""

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_ts(ts: str) -> float:
    """Parse HH:MM:SS[.mmm] or HH:MM:SS,mmm or MM:SS → seconds as float."""
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def format_ts(seconds: float) -> str:
    """Format seconds → HH:MM:SS.mmm"""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hrs = total_ms // 3_600_000
    rem = total_ms % 3_600_000
    mins = rem // 60_000
    rem %= 60_000
    secs = rem // 1000
    ms = rem % 1000
    return f"{hrs:02d}:{mins:02d}:{secs:02d}.{ms:03d}"


# ---------------------------------------------------------------------------
# Speaker detection
# ---------------------------------------------------------------------------

# Patterns tried in order:
#   1.  >> Speaker Name: text       (common YouTube multi-speaker captions)
#   2.  [SPEAKER NAME]: text        (bracket format, optional colon)
#   3.  [SPEAKER NAME] text         (bracket with no colon)
#   4.  Speaker Name: text          (plain "Capitalized Name:" prefix)
_SPEAKER_PATTERNS = [
    re.compile(r"^>>\s*(?P<speaker>[^:<>\[\]]+?)\s*:\s*(?P<text>.+)$"),
    re.compile(r"^\[(?P<speaker>[A-Z][^\[\]]{1,40}?)\]\s*:\s*(?P<text>.+)$"),
    re.compile(r"^\[(?P<speaker>[A-Z][^\[\]]{1,40}?)\]\s+(?P<text>.+)$"),
    re.compile(r"^(?P<speaker>[A-Z][A-Za-z .'-]{1,30}):\s+(?P<text>\S.+)$"),
]


def detect_speaker(text: str, current_speaker: str) -> tuple[str, str]:
    """Return (speaker_label, cleaned_text).  Falls back to current_speaker."""
    for pat in _SPEAKER_PATTERNS:
        m = pat.match(text.strip())
        if m:
            return m.group("speaker").strip(), m.group("text").strip()
    return current_speaker, text.strip()


# ---------------------------------------------------------------------------
# SRT parser  (yt-dlp converts VTT → SRT cleanly, removing word-level tags)
# ---------------------------------------------------------------------------

_SRT_TIMING = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s+-->\s+(?P<end>\d{1,2}:\d{2}:\d{2}[.,]\d{3})"
)
_HTML_TAG = re.compile(r"<[^>]+>")
_MULTI_SPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    text = _HTML_TAG.sub("", text)
    text = _MULTI_SPACE.sub(" ", text).strip()
    return text


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    """Parse an SRT/VTT file; return list of (start_s, end_s, text)."""
    content = path.read_text(encoding="utf-8", errors="replace")
    segments: list[tuple[float, float, str]] = []

    for block in re.split(r"\n\s*\n", content):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        timing_line: re.Match | None = None
        text_lines: list[str] = []

        for line in lines:
            m = _SRT_TIMING.search(line)
            if m:
                timing_line = m
                text_lines = []
            elif timing_line is not None:
                # Skip the numeric block index and metadata headers
                if re.fullmatch(r"\d+", line):
                    continue
                if line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")):
                    continue
                cleaned = _clean(line)
                if cleaned:
                    text_lines.append(cleaned)

        if timing_line and text_lines:
            start_s = parse_ts(timing_line.group("start"))
            end_s = parse_ts(timing_line.group("end"))
            text = " ".join(text_lines)
            if text:
                segments.append((start_s, end_s, text))

    return segments


def deduplicate_rolling_window(segments: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Remove rolling-window overlap common in YouTube auto-generated captions.

    YouTube auto-captions often show the same sentence across multiple cues as
    new words are appended.  This function keeps only the last cue that contains
    a given chunk of text, discarding earlier partial repetitions.
    """
    if not segments:
        return segments

    result: list[tuple[float, float, str]] = []
    for start_s, end_s, text in segments:
        if result:
            prev_s, prev_e, prev_text = result[-1]
            # If previous text is a prefix of current text, replace it (rolling window)
            if text.startswith(prev_text) and text != prev_text:
                result[-1] = (prev_s, end_s, text)
                continue
            # If current text is entirely contained in previous text, skip it (exact dup)
            if prev_text.endswith(text):
                continue
        result.append((start_s, end_s, text))

    return result


# ---------------------------------------------------------------------------
# Properties loader with Spring-style placeholder resolution
# ---------------------------------------------------------------------------

def load_properties(props_path: Path) -> dict[str, str]:
    """Parse a key=value properties file; ignore blank lines and # comments."""
    props: dict[str, str] = {}
    for line in props_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    return props


_SPRING_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


def resolve_spring_placeholders(value: str, props: dict[str, str]) -> str:
    """Resolve Spring-style ${key} placeholders.

    Supported built-ins: user.home → Path.home().
    Other keys are looked up in *props* first, then os.environ.
    """
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "user.home":
            return str(Path.home())
        return props.get(key, os.environ.get(key, m.group(0)))

    return _SPRING_PLACEHOLDER.sub(_replace, value)


def load_media_config(config_path: Path) -> dict[str, str]:
    """Load media-config.properties and resolve all Spring-style placeholders."""
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")
    
    raw_props = load_properties(config_path)
    
    # Resolve placeholders for all values
    resolved: dict[str, str] = {}
    for key, value in raw_props.items():
        resolved[key] = resolve_spring_placeholders(value, raw_props)
    
    return resolved


# ---------------------------------------------------------------------------
# yt-dlp download
# ---------------------------------------------------------------------------

_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MULTI_DASH = re.compile(r"-{2,}")


def fetch_playlist_video_ids(playlist_id: str, logger: logging.Logger) -> list[str]:
    """Return the list of video IDs in a YouTube playlist via yt-dlp --flat-playlist."""
    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--no-warnings", "--print", "%(id)s", url],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            ids = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
            logger.info("Playlist %s contains %d videos", playlist_id, len(ids))
            return ids
        logger.error("yt-dlp playlist fetch failed: %s", result.stderr.strip())
    except Exception as exc:  # noqa: BLE001
        logger.error("Playlist fetch error: %s", exc)
    return []


def slugify(title: str, max_len: int = 120) -> str:
    """Convert a video title to a safe filename stem."""
    slug = _UNSAFE_FILENAME_CHARS.sub("-", title)
    slug = _MULTI_DASH.sub("-", slug).strip("- ")
    return slug[:max_len]


def fetch_video_title(video_id: str, logger: logging.Logger) -> str:
    """Return the YouTube video title via yt-dlp --print title, or '' on failure."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        result = subprocess.run(
            ["yt-dlp", "--print", "title", "--no-playlist", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            title = result.stdout.strip().splitlines()[0]
            logger.info("      Title     : %s", title)
            return title
        logger.debug("yt-dlp title fetch failed: %s", result.stderr.strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Title fetch error: %s", exc)
    return ""


def normalize_languages(lang_input: str) -> str:
    """Normalize language input to comma-separated format, stripping whitespace.
    
    Examples:
        'en' → 'en'
        'en,hi' → 'en,hi'
        'en, hi' → 'en,hi'
        'en.*,hi' → 'en.*,hi'
    """
    if not lang_input:
        return "en"
    # Split by comma, strip whitespace from each, join back
    langs = [lang.strip() for lang in lang_input.split(",")]
    # Filter out empty strings
    langs = [lang for lang in langs if lang]
    return ",".join(langs) if langs else "en"


def download_captions(
    video_id: str,
    tmp_dir: Path,
    lang: str,
    logger: logging.Logger,
) -> Path | None:
    """Download captions via yt-dlp; return path to the subtitle file, or None.
    
    The lang parameter can be a single language code or comma-separated codes
    for mixed-language content (e.g., 'en,hi').
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_template = str(tmp_dir / "%(id)s")

    # Attempt manual captions first, then auto-generated
    attempts = [
        ("manual", ["--write-sub"]),
        ("auto-generated", ["--write-auto-sub"]),
    ]

    for label, sub_flags in attempts:
        cmd = [
            "yt-dlp",
            *sub_flags,
            "--skip-download",
            "--sub-langs", lang,
            "--convert-subs", "srt",   # Convert to SRT for clean, tag-free text
            "-o", out_template,
            url,
        ]
        logger.info("Trying %s captions: %s", label, " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.debug("yt-dlp stderr: %s", result.stderr.strip())

        # Prefer SRT, fall back to VTT
        for ext_glob in ("*.srt", "*.vtt"):
            found = list(tmp_dir.glob(ext_glob))
            if found:
                logger.info("Downloaded: %s", found[0].name)
                return found[0]

    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download YouTube captions and save a timestamped transcript with speaker labels.\n\n"
            "Output format:  [HH:MM:SS.mmm -> HH:MM:SS.mmm] Speaker: text"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "video_id",
        nargs="?",
        default="",
        help="YouTube video ID (e.g. dQw4w9WgXcQ) or full URL. Optional when --config is supplied.",
    )
    parser.add_argument(
        "--config",
        default="",
        metavar="FILE",
        help="Path to media-config.properties file (e.g. config/media-config.properties).",
    )
    parser.add_argument(
        "--playlist-id",
        default="",
        metavar="PLAYLIST_ID",
        help="YouTube playlist ID. Extracts captions for every video in the playlist.",
    )
    parser.add_argument(
        "--start",
        default="",
        metavar="HH:MM:SS",
        help="Clip start time (default: beginning of video)",
    )
    parser.add_argument(
        "--end",
        default="",
        metavar="HH:MM:SS",
        help="Clip end time (default: end of video)",
    )
    parser.add_argument(
        "--lang",
        default="",
        metavar="LANG",
        help=(
            "Caption language code(s), comma-separated for mixed-language content. "
            "Examples: 'en', 'hi', 'en,hi'. "
            "If --config supplied, defaults to config value; otherwise 'en'."
        ),
    )
    parser.add_argument(
        "--speaker",
        default="",
        metavar="NAME",
        help="Default speaker label when none is detected in the caption text.",
    )
    parser.add_argument(
        "--output",
        default="",
        metavar="FILE",
        help="Output .txt path. Overrides media_dir when specified.",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Disable rolling-window deduplication (useful for manually-edited captions)",
    )
    parser.add_argument(
        "--keep-tmp",
        action="store_true",
        help="Keep the temporary download folder after extraction",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG if "--verbose" in sys.argv else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("yt_caption_extract")

    args = build_parser().parse_args()

    # ── Load and merge config file ──────────────────────────────────────────
    config_props: dict[str, str] = {}
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
        config_props = load_media_config(config_path)
        logger.info("Loaded config from: %s", config_path)
        logger.info("  media_dir: %s", config_props.get("media_dir", ""))
        logger.info("  tools_dir: %s", config_props.get("tools_dir", ""))

    def prop(key: str, cli_val: str, fallback: str = "") -> str:
        """CLI value wins; then config property; then fallback."""
        if cli_val:
            return cli_val
        return config_props.get(key, fallback)

    # ── Resolve video IDs ─────────────────────────────────────────────────
    video_ids: list[str] = []

    # Collect from CLI positional arg or config videoId
    raw_video_id = prop("videoId", args.video_id)
    if raw_video_id:
        for vid in raw_video_id.split(","):
            vid = vid.strip()
            url_match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", vid)
            if url_match:
                vid = url_match.group(1)
            if vid and vid not in video_ids:
                video_ids.append(vid)

    # Collect from CLI --playlist-id
    raw_playlist_id = prop("playlistId", args.playlist_id)
    if raw_playlist_id:
        playlist_ids = fetch_playlist_video_ids(raw_playlist_id.strip(), logger)
        for vid in playlist_ids:
            if vid not in video_ids:
                video_ids.append(vid)

    if not video_ids:
        logger.error("No video IDs supplied. Pass a video_id, --playlist-id, or use --config.")
        return 1

    start_raw = prop("startTime", args.start)
    end_raw   = prop("endTime",   args.end)
    start_s: float = parse_ts(start_raw) if start_raw else 0.0
    end_s: float   = parse_ts(end_raw)   if end_raw   else float("inf")

    if start_raw and end_raw and end_s <= start_s:
        logger.error("--end time must be after --start time.")
        return 1

    # Speaker: CLI > config > default
    resolved_speaker = prop("speaker", args.speaker, "Speaker")

    # Language: CLI > config > default "en"
    raw_lang = args.lang or prop("lang", "", "en")
    resolved_lang = normalize_languages(raw_lang)

    # ── Resolve output directory from media_dir ─────────────────────────────
    media_dir_str = prop("media_dir", "")
    if media_dir_str:
        media_dir = Path(media_dir_str).expanduser().resolve()
        logger.info("Output dir: %s", media_dir)
        media_dir.mkdir(parents=True, exist_ok=True)
    else:
        logger.info("Output dir: . (media_dir not set; using cwd)")
        media_dir = Path(".")

    # Verify yt-dlp is available
    if not shutil.which("yt-dlp"):
        logger.error("yt-dlp not found. Install it with:  pip install yt-dlp")
        return 1

    base_dir = media_dir
    end_label = "end" if end_s == float("inf") else format_ts(end_s)
    total = len(video_ids)
    logger.info("Processing %d video(s)", total)

    failed: list[str] = []
    for idx, video_id in enumerate(video_ids, 1):
        logger.info("━" * 60)
        logger.info("[%d/%d] Video ID : %s", idx, total, video_id)
        logger.info("        Language : %s", resolved_lang)
        logger.info("        Speaker  : %s", resolved_speaker)
        logger.info("        Range    : %s → %s", format_ts(start_s), end_label)

        # Output path: CLI --output only applies to single-video runs
        if args.output and total == 1:
            output_path = Path(args.output).expanduser()
        else:
            title = fetch_video_title(video_id, logger)
            stem = slugify(title) if title else video_id
            output_path = base_dir / f"{stem}.captions.txt"

        tmp_dir = Path(tempfile.mkdtemp(prefix=f"yt_cap_{video_id}_"))
        try:
            # ── Step 1: download captions ─────────────────────────────────
            caption_file = download_captions(video_id, tmp_dir, resolved_lang, logger)

            if caption_file is None:
                logger.warning("No captions found for video: %s — skipping", video_id)
                failed.append(video_id)
                continue

            # ── Step 2: parse ────────────────────────────────────────────
            logger.info("  Parsing captions: %s", caption_file.name)
            segments = parse_srt(caption_file)
            logger.info("      %d cues parsed", len(segments))

            if not args.no_dedup:
                segments = deduplicate_rolling_window(segments)
                logger.info("      %d cues after deduplication", len(segments))

            # ── Step 3: filter by time range ─────────────────────────────
            logger.info("  Filtering to range %s → %s", format_ts(start_s), end_label)
            filtered = [
                (s, e, t) for s, e, t in segments
                if e > start_s and s < end_s
            ]
            logger.info("      %d cues in range", len(filtered))

            if not filtered:
                logger.warning("No captions in range for video: %s", video_id)

            # ── Step 4: write output ─────────────────────────────────────
            logger.info("  Writing: %s", output_path)

            video_url = f"https://www.youtube.com/watch?v={video_id}"
            header_lines = [
                f"Title    : {Path(output_path.stem).name}",
                f"Source   : {video_url}",
                f"VideoID  : {video_id}",
                f"Language : {resolved_lang}",
                f"Range    : {format_ts(start_s)} → {end_label}",
                f"Segments : {len(filtered)}",
                "",
            ]

            caption_lines: list[str] = []
            current_speaker = resolved_speaker
            for seg_start, seg_end, text in filtered:
                speaker, clean_text = detect_speaker(text, current_speaker)
                current_speaker = speaker  # carry detected speaker forward
                if clean_text:
                    caption_lines.append(
                        f"[{format_ts(seg_start)} -> {format_ts(seg_end)}] {speaker}: {clean_text}"
                    )

            all_lines = header_lines + caption_lines
            output_path.write_text("\n".join(all_lines).strip() + "\n", encoding="utf-8-sig")

            logger.info("  Done. %d caption lines written.", len(caption_lines))

        finally:
            if args.keep_tmp:
                logger.info("Temp files kept at: %s", tmp_dir)
            else:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    if failed:
        logger.warning("Captions not found for %d video(s): %s", len(failed), ", ".join(failed))
    logger.info("Completed: %d/%d videos processed successfully.", total - len(failed), total)
    return 1 if len(failed) == total else 0


if __name__ == "__main__":
    sys.exit(main())
