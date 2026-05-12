from __future__ import annotations

"""vlc_playlist_to_m4a.py - Merge VLC playlist tracks into a normalized M4A.

Features:
- Reads VLC XSPF playlists, including per-track start/end options when present.
- Also supports M3U/M3U8 with #EXTVLCOPT:start-time / stop-time options.
- Concatenates all resolved track clips in playlist order.
- Encodes output using MUSIC_CONCERT preset (AAC 256k, 48kHz, stereo).
- Applies final loudness normalization (ReplayGain-style listening consistency).

Output:
- <playlist_stem>.m4a in the same directory as the playlist

Usage:
    python scripts/vlc_playlist_to_m4a.py --playlist C:\\path\\to\\mylist.xspf
    python scripts/vlc_playlist_to_m4a.py --playlist C:\\path\\to\\mylist.m3u --verbose
"""

import argparse
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "media-config.properties"
_SPRING_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")

# MUSIC_CONCERT preset aligned with existing yt_download_audio.py mappings.
_MUSIC_CONCERT_CODEC = ["-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2"]


@dataclass
class Clip:
    path: Path
    start_s: float | None = None
    end_s: float | None = None


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


def parse_time_value(raw: str) -> float | None:
    val = raw.strip()
    if not val:
        return None
    val = val.replace(",", ".")

    try:
        if ":" not in val:
            sec = float(val)
            return sec if sec >= 0 else None

        parts = val.split(":")
        if len(parts) == 3:
            h = int(parts[0])
            m = int(parts[1])
            s = float(parts[2])
            sec = h * 3600 + m * 60 + s
            return sec if sec >= 0 else None
        if len(parts) == 2:
            m = int(parts[0])
            s = float(parts[1])
            sec = m * 60 + s
            return sec if sec >= 0 else None
    except ValueError:
        return None

    return None


def to_ffmpeg_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def decode_location_to_path(location: str, playlist_dir: Path) -> Path:
    loc = location.strip()
    parsed = urllib.parse.urlparse(loc)

    if parsed.scheme == "file":
        decoded = urllib.parse.unquote(parsed.path)
        if parsed.netloc and not decoded.startswith("/"):
            decoded = f"/{decoded}"
        candidate = Path(decoded)
        # file:///C:/... on Windows yields /C:/...; strip the leading slash.
        if re.match(r"^/[A-Za-z]:/", decoded):
            candidate = Path(decoded[1:])
        return candidate.resolve()

    if parsed.scheme in {"http", "https"}:
        raise ValueError(f"Remote track URL is not supported: {loc}")

    return (playlist_dir / urllib.parse.unquote(loc)).resolve()


def parse_xspf_playlist(playlist_path: Path, logger: logging.Logger) -> list[Clip]:
    tree = ET.parse(playlist_path)
    root = tree.getroot()

    ns = {
        "x": "http://xspf.org/ns/0/",
        "vlc": "http://www.videolan.org/vlc/playlist/ns/0/",
    }

    clips: list[Clip] = []
    playlist_dir = playlist_path.parent

    for track in root.findall(".//x:track", ns):
        location_el = track.find("x:location", ns)
        if location_el is None or not (location_el.text or "").strip():
            continue

        try:
            clip_path = decode_location_to_path(location_el.text or "", playlist_dir)
        except ValueError as exc:
            logger.warning("Skipping unsupported track location: %s", exc)
            continue

        start_s: float | None = None
        end_s: float | None = None

        for opt in track.findall("x:extension/vlc:option", ns):
            text = (opt.text or "").strip()
            if not text:
                continue

            if text.startswith("start-time="):
                start_s = parse_time_value(text.split("=", 1)[1])
            elif text.startswith("stop-time=") or text.startswith("end-time="):
                end_s = parse_time_value(text.split("=", 1)[1])

        clips.append(Clip(path=clip_path, start_s=start_s, end_s=end_s))

    return clips


def parse_m3u_playlist(playlist_path: Path, logger: logging.Logger) -> list[Clip]:
    lines = playlist_path.read_text(encoding="utf-8", errors="replace").splitlines()
    clips: list[Clip] = []
    playlist_dir = playlist_path.parent

    pending_start: float | None = None
    pending_end: float | None = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if line.startswith("#EXTVLCOPT:"):
            option = line[len("#EXTVLCOPT:") :]
            if option.startswith("start-time="):
                pending_start = parse_time_value(option.split("=", 1)[1])
            elif option.startswith("stop-time=") or option.startswith("end-time="):
                pending_end = parse_time_value(option.split("=", 1)[1])
            continue

        if line.startswith("#"):
            continue

        path = decode_location_to_path(line, playlist_dir)
        clips.append(Clip(path=path, start_s=pending_start, end_s=pending_end))
        pending_start = None
        pending_end = None

    return clips


def parse_playlist(playlist_path: Path, logger: logging.Logger) -> list[Clip]:
    ext = playlist_path.suffix.lower()
    if ext == ".xspf":
        return parse_xspf_playlist(playlist_path, logger)
    if ext in {".m3u", ".m3u8"}:
        return parse_m3u_playlist(playlist_path, logger)
    raise ValueError("Unsupported playlist format. Use .xspf, .m3u, or .m3u8")


def validate_clips(clips: list[Clip], logger: logging.Logger) -> list[Clip]:
    valid: list[Clip] = []

    for idx, clip in enumerate(clips, start=1):
        if not clip.path.exists():
            logger.warning("[%d] Missing file, skipping: %s", idx, clip.path)
            continue

        if clip.end_s is not None and clip.start_s is not None and clip.end_s <= clip.start_s:
            logger.warning(
                "[%d] Invalid range start>=end, skipping: %s (start=%s end=%s)",
                idx,
                clip.path,
                clip.start_s,
                clip.end_s,
            )
            continue

        valid.append(clip)

    return valid


def build_filter_complex(clips: list[Clip]) -> str:
    parts: list[str] = []

    for i, clip in enumerate(clips):
        chain = f"[{i}:a]"

        atrim_parts: list[str] = []
        if clip.start_s is not None:
            atrim_parts.append(f"start={to_ffmpeg_seconds(clip.start_s)}")
        if clip.end_s is not None:
            atrim_parts.append(f"end={to_ffmpeg_seconds(clip.end_s)}")

        if atrim_parts:
            chain += f"atrim={':'.join(atrim_parts)},"

        chain += f"asetpts=PTS-STARTPTS[a{i}]"
        parts.append(chain)

    concat_inputs = "".join(f"[a{i}]" for i in range(len(clips)))
    parts.append(f"{concat_inputs}concat=n={len(clips)}:v=0:a=1[cat]")

    # loudnorm provides ReplayGain-like perceived loudness consistency.
    parts.append("[cat]loudnorm=I=-16:LRA=11:TP=-1.5[norm]")

    return ";".join(parts)


def merge_playlist(
    clips: list[Clip],
    output_file: Path,
    ffmpeg: str,
    ffprobe: str,
    logger: logging.Logger,
) -> bool:
    cmd: list[str] = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    for clip in clips:
        cmd += ["-i", str(clip.path)]

    filter_complex = build_filter_complex(clips)

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[norm]",
        *_MUSIC_CONCERT_CODEC,
        "-movflags",
        "+faststart",
        str(output_file),
    ]

    logger.info("Merging %d tracks -> %s", len(clips), output_file)
    logger.debug("ffmpeg cmd: %s", " ".join(cmd))
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    last_progress_log = time.monotonic()
    assert process.stderr is not None
    for line in process.stderr:
        msg = line.strip()
        if not msg:
            continue

        # Keep normal mode readable while still showing ffmpeg progress over long runs.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("ffmpeg: %s", msg)
            continue

        if "time=" in msg or "size=" in msg or "speed=" in msg:
            now = time.monotonic()
            if now - last_progress_log >= 2.0:
                logger.info("ffmpeg: %s", msg)
                last_progress_log = now

    return_code = process.wait()
    if return_code != 0:
        logger.error("ffmpeg failed (exit %d). Re-run with --verbose for detailed ffmpeg logs.", return_code)
        return False

    probe_cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(output_file),
    ]
    probe = subprocess.run(probe_cmd, capture_output=True, text=True)
    if probe.returncode == 0:
        vals = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        if vals:
            duration = vals[0] if len(vals) >= 1 else "unknown"
            size = vals[1] if len(vals) >= 2 else "unknown"
            logger.info("Output validated: duration=%s sec, size=%s bytes", duration, size)

    return True


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Read VLC playlist tracks, apply optional clip times, and produce one "
            "MUSIC_CONCERT-quality M4A with final loudness normalization."
        )
    )
    p.add_argument("--playlist", required=True, help="Path to VLC playlist (.xspf/.m3u/.m3u8)")
    p.add_argument("--config", default=str(_DEFAULT_CONFIG), help="Properties config path")
    p.add_argument("--verbose", action="store_true", help="Enable DEBUG logs")
    return p


def main() -> int:
    args = build_arg_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("vlc_playlist_to_m4a")

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        return 1

    raw_props = load_properties(config_path)
    cfg = resolve_all(raw_props)

    tools_dir = Path(cfg.get("tools_dir", "")).expanduser()
    ffmpeg = resolve_tool(tools_dir, "ffmpeg")
    ffprobe = resolve_tool(tools_dir, "ffprobe")

    playlist_path = Path(args.playlist).expanduser().resolve()
    if not playlist_path.exists():
        logger.error("Playlist file not found: %s", playlist_path)
        return 1

    try:
        clips = parse_playlist(playlist_path, logger)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to parse playlist: %s", exc)
        return 1

    if not clips:
        logger.error("No tracks found in playlist: %s", playlist_path)
        return 1

    clips = validate_clips(clips, logger)
    if not clips:
        logger.error("No valid tracks available after validation.")
        return 1

    output_file = playlist_path.with_suffix(".m4a")

    for idx, clip in enumerate(clips, start=1):
        logger.info(
            "[%d] %s (start=%s end=%s)",
            idx,
            clip.path,
            "none" if clip.start_s is None else to_ffmpeg_seconds(clip.start_s),
            "none" if clip.end_s is None else to_ffmpeg_seconds(clip.end_s),
        )

    ok = merge_playlist(clips, output_file, ffmpeg, ffprobe, logger)
    if not ok:
        return 1

    logger.info("Done: %s", output_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
