from __future__ import annotations

"""yt_transcribe.py — Transcribe local audio with faster-whisper and speaker labels.

Configuration is driven entirely by a central properties file (config/media-config.properties).

Usage:
    python scripts/yt_transcribe.py --audio-file /path/to/audio.m4a
    python scripts/yt_transcribe.py --config config/media-config.properties
    python scripts/yt_transcribe.py --audio-file /path/to/audio.m4a --start 00:05:00 --end 00:30:00

Output:
    <media_dir>/<audio-stem>.transcript.txt   — timestamped segments
    [HH:MM:SS.mmm -> HH:MM:SS.mmm] Speaker Name: text

Dependencies:
    pip install faster-whisper
    Tools (ffmpeg) configured via tools_dir in media-config.properties when clipping is needed
"""

import argparse
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Default config: <project-root>/config/media-config.properties
# (scripts/ is one level below project root)
# ---------------------------------------------------------------------------
_DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "media-config.properties"


# ---------------------------------------------------------------------------
# Properties helpers
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


def resolve_placeholders(value: str, props: dict[str, str]) -> str:
    """Resolve ${key} placeholders; supports ${user.home} built-in."""
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "user.home":
            return str(Path.home())
        return props.get(key, os.environ.get(key, m.group(0)))
    return _SPRING_PLACEHOLDER.sub(_replace, value)


def resolve_all(props: dict[str, str]) -> dict[str, str]:
    return {k: resolve_placeholders(v, props) for k, v in props.items()}


# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------

def resolve_tool(tools_dir: Path, name: str) -> str:
    """Return absolute path to a tool inside tools_dir; falls back to system PATH."""
    for candidate in (tools_dir / name, tools_dir / f"{name}.exe"):
        if candidate.exists():
            return str(candidate)
    return name  # let the OS resolve via PATH


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def parse_ts(ts: str) -> float:
    """Parse HH:MM:SS[.mmm] → seconds as float."""
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

_SPEAKER_PATTERNS = [
    re.compile(r"^>>\s*(?P<speaker>[^:<>\[\]]+?)\s*:\s*(?P<text>.+)$"),
    re.compile(r"^\[(?P<speaker>[A-Z][^\[\]]{1,40}?)\]\s*:\s*(?P<text>.+)$"),
    re.compile(r"^\[(?P<speaker>[A-Z][^\[\]]{1,40}?)\]\s+(?P<text>.+)$"),
    re.compile(r"^(?P<speaker>[A-Z][A-Za-z .'-]{1,30}):\s+(?P<text>\S.+)$"),
]


def detect_speaker(text: str, current_speaker: str) -> tuple[str, str]:
    """Return (speaker_label, cleaned_text). Falls back to current_speaker."""
    for pat in _SPEAKER_PATTERNS:
        m = pat.match(text.strip())
        if m:
            return m.group("speaker").strip(), m.group("text").strip()
    return current_speaker, text.strip()


# ---------------------------------------------------------------------------
# ffmpeg: optional preprocess for clip range or format normalization
# ---------------------------------------------------------------------------

def convert_to_compact_speech(
    raw_audio: Path,
    output_path: Path,
    codec_options: str,
    start_time: str,
    end_time: str,
    ffmpeg: str,
    logger: logging.Logger,
) -> bool:
    """Re-encode raw_audio → output_path using config audio codec options.
    Trim with start_time / end_time when provided."""
    cmd = [ffmpeg, "-y"]
    if start_time:
        cmd += ["-ss", start_time]
    if end_time:
        cmd += ["-to", end_time]
    cmd += ["-i", str(raw_audio)]
    cmd += shlex.split(codec_options)
    cmd += ["-movflags", "+faststart", str(output_path)]

    logger.info("[1/2] Preprocessing audio to m4a …")
    logger.debug("  cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("ffmpeg failed (exit %d):\n%s", result.returncode, result.stderr[-2000:])
        return False
    logger.info("  Output m4a: %s", output_path.name)
    return True


# ---------------------------------------------------------------------------
# faster-whisper transcription
# ---------------------------------------------------------------------------

def transcribe(
    audio_path: Path,
    lang: str,
    model_size: str,
    logger: logging.Logger,
) -> list[dict] | None:
    """Transcribe with faster-whisper medium model; returns list of segment dicts."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error("faster-whisper not installed. Run: pip install faster-whisper")
        return None

    primary_lang: str | None = lang
    if "," in lang or lang.lower() == "auto":
        logger.info(
            "  Language '%s' → auto-detection enabled for mixed-language input.", lang
        )
        primary_lang = None  # None = auto-detect in faster-whisper

    logger.info("[2/2] Loading Whisper model '%s' …", model_size)
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    logger.info("  Transcribing: %s", audio_path.name)

    segments_iter, info = model.transcribe(
        str(audio_path),
        language=primary_lang,
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
    )

    results = []
    duration = getattr(info, "duration", None)
    logger.info("  Collecting segments …")
    for seg in segments_iter:
        results.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
        if len(results) == 1 or len(results) % 25 == 0:
            if duration and duration > 0:
                pct = min((seg.end / duration) * 100.0, 100.0)
                logger.info(
                    "  Progress: %d segments, audio up to %s (%.1f%%)",
                    len(results),
                    format_ts(seg.end),
                    pct,
                )
            else:
                logger.info(
                    "  Progress: %d segments, audio up to %s",
                    len(results),
                    format_ts(seg.end),
                )

    logger.info("  Detected language: %s", getattr(info, "language", lang))
    logger.info("  Segments:          %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Write transcript
# ---------------------------------------------------------------------------

def write_transcript(
    segments: list[dict],
    output_path: Path,
    default_speaker: str,
    start_offset: float,
    logger: logging.Logger,
) -> None:
    """Write [HH:MM:SS.mmm -> HH:MM:SS.mmm] Speaker: text lines."""
    lines: list[str] = []
    current_speaker = default_speaker
    total = len(segments)
    logger.info("  Writing transcript lines …")

    for idx, seg in enumerate(segments, start=1):
        speaker, text = detect_speaker(seg["text"], current_speaker)
        current_speaker = speaker
        ts_start = format_ts(seg["start"] + start_offset)
        ts_end   = format_ts(seg["end"]   + start_offset)
        lines.append(f"[{ts_start} -> {ts_end}] {speaker}: {text}")
        if idx == 1 or idx % 100 == 0 or idx == total:
            logger.info("  Write progress: %d/%d lines", idx, total)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("  Transcript: %s  (%d lines)", output_path, len(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Transcribe local audio with faster-whisper.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--config", default=str(_DEFAULT_CONFIG),
        help=f"Properties config file (default: config/media-config.properties)",
    )
    p.add_argument("--start",      default="",  help="Start time HH:MM:SS")
    p.add_argument("--end",        default="",  help="End time HH:MM:SS")
    p.add_argument("--lang",       default="",  help="Language code(s) e.g. te,en")
    p.add_argument("--model",      default="",  help="Whisper model size (default: medium)")
    p.add_argument("--speaker",    default="",  help="Default speaker label")
    p.add_argument("--audio-file", dest="audio_file", default="", help="Local audio file path")
    p.add_argument("--verbose",    action="store_true", help="Enable DEBUG logging")
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = build_arg_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("yt_transcribe")

    # ── Load config ──────────────────────────────────────────────────────────
    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        logger.error("Expected at: %s", _DEFAULT_CONFIG)
        return 1

    raw_props = load_properties(config_path)
    cfg = resolve_all(raw_props)
    logger.info("Config: %s", config_path)

    def prop(key: str, cli_val: str, fallback: str = "") -> str:
        if cli_val:
            return cli_val
        return cfg.get(key, fallback)

    # ── Resolve paths ────────────────────────────────────────────────────────
    tools_dir = Path(prop("tools_dir", "")).expanduser()
    media_dir = Path(prop("media_dir", str(Path.home() / "media-files"))).expanduser()
    media_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = resolve_tool(tools_dir, "ffmpeg")

    # ── Input resolution: local audio only ──────────────────────────────────
    raw_audio_file = args.audio_file or cfg.get("audio_file", "")
    if not raw_audio_file:
        logger.error("No local audio input found. Set audio_file in config or pass --audio-file.")
        return 1

    audio_file_path = Path(raw_audio_file.strip('"').strip("'")).expanduser().resolve()
    if not audio_file_path.exists():
        logger.error("Local audio file not found: %s", audio_file_path)
        return 1
    logger.info("Audio file: %s", audio_file_path)

    start_raw  = prop("startTime", args.start).strip()
    end_raw    = prop("endTime",   args.end).strip()
    lang       = prop("lang",      args.lang,    "te,en")
    model_size = prop("model",     args.model,   "medium")
    speaker    = prop("speaker",   args.speaker, "Speaker 1")
    codec_opts = cfg.get("audio_codec", "-c:a aac -b:a 48k -ar 32000 -ac 1")
    out_suffix = cfg.get("output_suffix", ".transcript")

    logger.info("  Start    : %s", start_raw or "(none)")
    logger.info("  End      : %s", end_raw   or "(none)")
    logger.info("  Language : %s", lang)
    logger.info("  Model    : %s", model_size)
    logger.info("  Speaker  : %s", speaker)
    logger.info("  Codec    : %s", codec_opts)

    start_offset = parse_ts(start_raw) if start_raw else 0.0

    # ── Preprocess (optional) → transcribe ──────────────────────────────────
    with tempfile.TemporaryDirectory(prefix="yttranscribe_") as tmp_str:
        if audio_file_path.suffix.lower() != ".m4a" or start_raw or end_raw:
            tmp_dir = Path(tmp_str)
            m4a_path = tmp_dir / (audio_file_path.stem + ".transcribe.m4a")
            if not convert_to_compact_speech(
                audio_file_path, m4a_path, codec_opts, start_raw, end_raw, ffmpeg, logger
            ):
                return 1
        else:
            m4a_path = audio_file_path
            logger.info("[1/2] Using local audio as-is: %s", m4a_path.name)

        segments = transcribe(m4a_path, lang, model_size, logger)
        if segments is None:
            return 1

        transcript_path = media_dir / (m4a_path.stem + out_suffix + ".txt")
        write_transcript(segments, transcript_path, speaker, start_offset, logger)

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
