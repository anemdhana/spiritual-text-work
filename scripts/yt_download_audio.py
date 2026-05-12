from __future__ import annotations

"""yt_download_audio.py — Download YouTube audio and convert to configured quality.

Quality presets are aligned with MediaFileUtils audioCodecOptionsFor values:
- COMPACT_SIZE
- COMPACT_SIZE_SPEECH
- COMPACT_SIZE_MUSIC
- COMPACT_MUSIC_INSTRUMENTAL
- WHATSAPP
- MUSIC_CONCERT
- YOUTUBE_UPLOAD

Configuration source:
    config/media-config.properties

Usage:
    python scripts/yt_download_audio.py
    python scripts/yt_download_audio.py --video-id Py8Z7D15JYo
    python scripts/yt_download_audio.py --video-id Py8Z7D15JYo --quality COMPACT_SIZE_SPEECH
    python scripts/yt_download_audio.py --video-id Py8Z7D15JYo --start 00:05:00 --end 00:30:00
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

_DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "media-config.properties"
_SPRING_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MULTI_DASH = re.compile(r"-{2,}")

QUALITY_PRESETS_M4A = {
    "COMPACT_SIZE": "-c:a aac -b:a 80k -ar 44100 -ac 2",
    "COMPACT_SIZE_SPEECH": "-c:a aac -b:a 48k -ar 32000 -ac 1",
    "COMPACT_SIZE_MUSIC": "-c:a aac -b:a 72k -ar 44100 -ac 2",
    "COMPACT_MUSIC_INSTRUMENTAL": "-c:a aac -b:a 72k -ar 44100 -ac 2",
    "WHATSAPP": "-c:a aac -b:a 96k -ar 44100 -ac 2",
    "MUSIC_CONCERT": "-c:a aac -b:a 256k -ar 48000 -ac 2",
    "YOUTUBE_UPLOAD": "-c:a aac -b:a 192k -ar 44100 -ac 2",
}

QUALITY_PRESETS_MP3 = {
    "COMPACT_SIZE": "-c:a libmp3lame -b:a 80k -ar 44100",
    "COMPACT_SIZE_SPEECH": "-c:a libmp3lame -b:a 48k -ar 32000 -ac 1",
    "COMPACT_SIZE_MUSIC": "-c:a libmp3lame -b:a 72k -ar 44100 -ac 2",
    "COMPACT_MUSIC_INSTRUMENTAL": "-c:a libmp3lame -b:a 72k -ar 44100 -ac 2",
    "WHATSAPP": "-c:a libmp3lame -b:a 96k -ar 44100",
    "MUSIC_CONCERT": "-af replaygain=track -c:a libmp3lame -b:a 256k",
    "YOUTUBE_UPLOAD": "-c:a libmp3lame -b:a 192k",
}


def load_properties(props_path: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in props_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    return props


def resolve_placeholders(value: str, props: dict[str, str]) -> str:
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "user.home":
            return str(Path.home())
        return props.get(key, os.environ.get(key, m.group(0)))

    return _SPRING_PLACEHOLDER.sub(_replace, value)


def resolve_all(props: dict[str, str]) -> dict[str, str]:
    return {k: resolve_placeholders(v, props) for k, v in props.items()}


def resolve_tool(tools_dir: Path, name: str) -> str:
    for candidate in (tools_dir / name, tools_dir / f"{name}.exe"):
        if candidate.exists():
            return str(candidate)
    return name


def resolve_ytdlp(tools_dir: Path) -> list[str]:
    """Return command prefix for yt-dlp; prefers pip module to avoid PyInstaller issues."""
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        pass
    return [resolve_tool(tools_dir, "yt-dlp")]


def slugify(title: str, max_len: int = 120) -> str:
    slug = _UNSAFE_CHARS.sub("-", title)
    slug = _MULTI_DASH.sub("-", slug).strip("- ")
    return slug[:max_len]


def fetch_video_title(video_id: str, ytdlp: list[str], logger: logging.Logger) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        result = subprocess.run(
            [*ytdlp, "--print", "title", "--no-playlist", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            title = result.stdout.strip().splitlines()[0]
            logger.info("  Title: %s", title)
            return title
        logger.debug("yt-dlp title stderr: %s", result.stderr.strip())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Title fetch error: %s", exc)
    return ""


def download_raw_audio(video_id: str, tmp_dir: Path, ytdlp: list[str], logger: logging.Logger) -> Path | None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_template = str(tmp_dir / "%(id)s.%(ext)s")
    cmd = [*ytdlp, "--no-playlist", "-f", "bestaudio", "-o", out_template, url]

    logger.info("[1/2] Downloading raw audio …")
    logger.debug("  cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("yt-dlp failed (exit %d):\n%s", result.returncode, result.stderr.strip())
        return None

    for ext_glob in ("*.m4a", "*.webm", "*.opus", "*.mp4", "*.ogg", "*.mp3"):
        found = list(tmp_dir.glob(ext_glob))
        if found:
            logger.info("  Raw audio: %s", found[0].name)
            return found[0]

    logger.error("yt-dlp succeeded but no audio file was found in %s", tmp_dir)
    return None


def codec_options_for(quality: str, output_format: str) -> str:
    if output_format == "mp3":
        return QUALITY_PRESETS_MP3[quality]
    return QUALITY_PRESETS_M4A[quality]


def convert_audio(
    source_audio: Path,
    target_audio: Path,
    quality: str,
    output_format: str,
    start_time: str,
    end_time: str,
    ffmpeg: str,
    logger: logging.Logger,
) -> bool:
    cmd = [ffmpeg, "-y"]
    if start_time:
        cmd += ["-ss", start_time]
    if end_time:
        cmd += ["-to", end_time]

    cmd += ["-i", str(source_audio)]
    cmd += shlex.split(codec_options_for(quality, output_format))

    if output_format == "m4a":
        cmd += ["-movflags", "+faststart"]
    else:
        cmd += ["-f", "mp3"]

    cmd += [str(target_audio)]

    logger.info("[2/2] Converting to %s (%s) …", quality, output_format)
    logger.debug("  cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("ffmpeg failed (exit %d):\n%s", result.returncode, result.stderr[-2000:])
        return False

    logger.info("  Output: %s", target_audio)
    return True


def validate_audio_with_ffprobe(audio_file: Path, ffprobe: str, logger: logging.Logger) -> bool:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_file),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("ffprobe validation failed: %s", result.stderr.strip())
        return False

    values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not values:
        logger.error("ffprobe returned no output for %s", audio_file)
        return False

    duration = values[0] if len(values) >= 1 else "unknown"
    size = values[1] if len(values) >= 2 else "unknown"
    logger.info("  Validated with ffprobe: duration=%s sec, size=%s bytes", duration, size)
    return True


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download YouTube audio and convert using quality presets.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default=str(_DEFAULT_CONFIG), help="Properties config path")
    p.add_argument("--video-id", dest="video_id", default="", help="YouTube video ID")
    p.add_argument("--quality", default="", help="Quality preset name")
    p.add_argument("--output-format", default="", help="m4a or mp3")
    p.add_argument("--start", default="", help="Start time HH:MM:SS")
    p.add_argument("--end", default="", help="End time HH:MM:SS")
    p.add_argument("--verbose", action="store_true", help="Enable DEBUG logs")
    return p


def main() -> int:
    args = build_arg_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("yt_download_audio")

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        return 1

    raw_props = load_properties(config_path)
    cfg = resolve_all(raw_props)

    def prop(key: str, cli_val: str, fallback: str = "") -> str:
        if cli_val:
            return cli_val
        return cfg.get(key, fallback)

    tools_dir = Path(prop("tools_dir", "")).expanduser()
    media_dir = Path(prop("media_dir", str(Path.home() / "media-files"))).expanduser()
    media_dir.mkdir(parents=True, exist_ok=True)

    ytdlp = resolve_ytdlp(tools_dir)
    ffmpeg = resolve_tool(tools_dir, "ffmpeg")
    ffprobe = resolve_tool(tools_dir, "ffprobe")

    video_id = prop("videoId", args.video_id).strip()
    url_match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", video_id)
    if url_match:
        video_id = url_match.group(1)
    if not video_id:
        logger.error("No video ID provided. Set videoId in config or pass --video-id.")
        return 1

    quality = prop("quality", args.quality, "COMPACT_SIZE_SPEECH").strip().upper()
    output_format = prop("output_format", args.output_format, "m4a").strip().lower()
    start_time = prop("startTime", args.start).strip()
    end_time = prop("endTime", args.end).strip()

    if quality not in QUALITY_PRESETS_M4A:
        logger.error("Invalid quality '%s'. Allowed: %s", quality, ", ".join(QUALITY_PRESETS_M4A.keys()))
        return 1
    if output_format not in {"m4a", "mp3"}:
        logger.error("Invalid output format '%s'. Allowed: m4a, mp3", output_format)
        return 1

    logger.info("Video ID    : %s", video_id)
    logger.info("Quality     : %s", quality)
    logger.info("Output fmt  : %s", output_format)
    logger.info("Start       : %s", start_time or "(none)")
    logger.info("End         : %s", end_time or "(none)")

    with tempfile.TemporaryDirectory(prefix="ytdl_") as tmp_str:
        tmp_dir = Path(tmp_str)

        title = fetch_video_title(video_id, ytdlp, logger)
        stem = slugify(title) if title else video_id

        source_audio = download_raw_audio(video_id, tmp_dir, ytdlp, logger)
        if source_audio is None:
            return 1

        out_name = f"{stem}-{quality.lower()}.{output_format}"
        output_audio = media_dir / out_name

        if not convert_audio(
            source_audio=source_audio,
            target_audio=output_audio,
            quality=quality,
            output_format=output_format,
            start_time=start_time,
            end_time=end_time,
            ffmpeg=ffmpeg,
            logger=logger,
        ):
            return 1

        if not validate_audio_with_ffprobe(output_audio, ffprobe, logger):
            return 1

    logger.info("Done: %s", output_audio)
    return 0


if __name__ == "__main__":
    sys.exit(main())
