from __future__ import annotations

"""azure_transcribe.py — Transcribe local audio with Azure Speech to Text.

Configuration is driven by config/media-config.properties.

Usage:
    python scripts/azure_transcribe.py --audio-file /path/to/audio.m4a
    python scripts/azure_transcribe.py --config config/media-config.properties
    python scripts/azure_transcribe.py --audio-file /path/to/audio.m4a --lang te-IN

Output:
    <media_dir>/<audio-stem>.azure.transcript.txt
    [HH:MM:SS.mmm -> HH:MM:SS.mmm] Speaker Name: text

Dependencies:
    pip install azure-cognitiveservices-speech
    Tools (ffmpeg) configured via tools_dir in media-config.properties
"""

import argparse
import logging
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


_DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "media-config.properties"
_SPRING_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


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
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "user.home":
            return str(Path.home())
        return props.get(key, os.environ.get(key, match.group(0)))

    return _SPRING_PLACEHOLDER.sub(_replace, value)


def resolve_all(props: dict[str, str]) -> dict[str, str]:
    return {key: resolve_placeholders(value, props) for key, value in props.items()}


def resolve_tool(tools_dir: Path, name: str) -> str:
    for candidate in (tools_dir / name, tools_dir / f"{name}.exe"):
        if candidate.exists():
            return str(candidate)
    return name


def resolve_secret(cfg: dict[str, str], direct_key: str, env_key_name: str) -> tuple[str, str]:
    direct_value = cfg.get(direct_key, "").strip()
    if direct_value and not _SPRING_PLACEHOLDER.fullmatch(direct_value):
        return direct_value, direct_key

    env_var_name = cfg.get(env_key_name, "").strip()
    if env_var_name:
        env_value = os.environ.get(env_var_name, "").strip()
        if env_value:
            return env_value, f"env:{env_var_name}"

    return "", env_var_name


def parse_ts(ts: str) -> float:
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


_SPEAKER_PATTERNS = [
    re.compile(r"^>>\s*(?P<speaker>[^:<>\[\]]+?)\s*:\s*(?P<text>.+)$"),
    re.compile(r"^\[(?P<speaker>[A-Z][^\[\]]{1,40}?)\]\s*:\s*(?P<text>.+)$"),
    re.compile(r"^\[(?P<speaker>[A-Z][^\[\]]{1,40}?)\]\s+(?P<text>.+)$"),
    re.compile(r"^(?P<speaker>[A-Z][A-Za-z .'-]{1,30}):\s+(?P<text>\S.+)$"),
]


def detect_speaker(text: str, current_speaker: str) -> tuple[str, str]:
    for pat in _SPEAKER_PATTERNS:
        match = pat.match(text.strip())
        if match:
            return match.group("speaker").strip(), match.group("text").strip()
    return current_speaker, text.strip()


def convert_audio_for_azure(
    raw_audio: Path,
    output_path: Path,
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
    cmd += [
        "-i",
        str(raw_audio),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(output_path),
    ]

    logger.info("[1/2] Preprocessing audio to mono 16k WAV for Azure Speech …")
    logger.debug("  cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("ffmpeg failed (exit %d):\n%s", result.returncode, result.stderr[-2000:])
        return False
    logger.info("  Output wav: %s", output_path.name)
    return True


def transcribe_with_azure(
    audio_path: Path,
    speech_key: str,
    speech_region: str,
    speech_language: str,
    logger: logging.Logger,
) -> list[dict] | None:
    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError:
        logger.error(
            "azure-cognitiveservices-speech not installed. Run: pip install azure-cognitiveservices-speech"
        )
        return None

    logger.info("[2/2] Connecting to Azure Speech …")
    logger.info("  Speech language: %s", speech_language)
    logger.info("  Transcribing: %s", audio_path.name)

    speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
    speech_config.speech_recognition_language = speech_language
    audio_config = speechsdk.audio.AudioConfig(filename=str(audio_path))
    recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)

    results: list[dict] = []
    done = {"value": False}

    def on_recognized(evt: speechsdk.SpeechRecognitionEventArgs) -> None:
        result = evt.result
        if result.reason != speechsdk.ResultReason.RecognizedSpeech:
            return

        offset_seconds = result.offset / 10_000_000
        duration_seconds = result.duration / 10_000_000
        results.append(
            {
                "start": offset_seconds,
                "end": offset_seconds + duration_seconds,
                "text": result.text.strip(),
            }
        )

    def on_canceled(evt: speechsdk.SpeechRecognitionCanceledEventArgs) -> None:
        logger.error("Azure Speech canceled: %s", evt.result.cancellation_details.reason)
        error_details = evt.result.cancellation_details.error_details
        if error_details:
            logger.error("Azure Speech error details: %s", error_details)
        done["value"] = True

    def on_session_stopped(_: object) -> None:
        done["value"] = True

    recognizer.recognized.connect(on_recognized)
    recognizer.canceled.connect(on_canceled)
    recognizer.session_stopped.connect(on_session_stopped)

    recognizer.start_continuous_recognition()
    while not done["value"]:
        import time

        time.sleep(0.2)
    recognizer.stop_continuous_recognition()

    logger.info("  Segments: %d", len(results))
    return results


def write_transcript(
    segments: list[dict],
    output_path: Path,
    default_speaker: str,
    start_offset: float,
    logger: logging.Logger,
) -> None:
    lines: list[str] = []
    current_speaker = default_speaker

    for seg in segments:
        speaker, text = detect_speaker(seg["text"], current_speaker)
        current_speaker = speaker
        ts_start = format_ts(seg["start"] + start_offset)
        ts_end = format_ts(seg["end"] + start_offset)
        lines.append(f"[{ts_start} -> {ts_end}] {speaker}: {text}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("  Transcript: %s  (%d lines)", output_path, len(lines))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe local audio with Azure Speech to Text.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=str(_DEFAULT_CONFIG),
        help="Properties config file (default: config/media-config.properties)",
    )
    parser.add_argument("--start", default="", help="Start time HH:MM:SS")
    parser.add_argument("--end", default="", help="End time HH:MM:SS")
    parser.add_argument("--lang", default="", help="Azure speech locale, e.g. te-IN")
    parser.add_argument("--speaker", default="", help="Default speaker label")
    parser.add_argument("--audio-file", dest="audio_file", default="", help="Local audio file path")
    parser.add_argument(
        "--preprocess",
        action="store_true",
        help="Convert audio to mono 16k WAV before sending to Azure Speech (use only if direct input fails)",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("azure_transcribe")

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

    media_dir = Path(prop("media_dir", str(Path.home() / "media-files"))).expanduser()
    media_dir.mkdir(parents=True, exist_ok=True)

    raw_audio_file = args.audio_file or cfg.get("audio_file", "")
    if not raw_audio_file:
        logger.error("No local audio input found. Set audio_file in config or pass --audio-file.")
        return 1

    audio_file_path = Path(raw_audio_file.strip('"').strip("'")).expanduser().resolve()
    if not audio_file_path.exists():
        logger.error("Local audio file not found: %s", audio_file_path)
        return 1
    logger.info("Audio file: %s", audio_file_path)

    start_raw = prop("startTime", args.start).strip()
    end_raw = prop("endTime", args.end).strip()
    speech_language = prop("azure_speech_lang", args.lang, "te-IN")
    speaker = prop("speaker", args.speaker, "Speaker 1")
    out_suffix = cfg.get("azure_output_suffix", ".azure.transcript")

    speech_key, key_source = resolve_secret(cfg, "azure_speech_key", "azure_speech_key_env")
    speech_region = cfg.get("azure_speech_region", "").strip()
    if not speech_key:
        env_hint = cfg.get("azure_speech_key_env", "").strip()
        if env_hint:
            logger.error(
                "Azure Speech key is missing. Set 'azure_speech_key' or environment variable '%s'.",
                env_hint,
            )
        else:
            logger.error("Missing 'azure_speech_key' in %s", config_path)
        return 1
    if not speech_region:
        logger.error("Missing 'azure_speech_region' in %s", config_path)
        return 1

    logger.info("  Start    : %s", start_raw or "(none)")
    logger.info("  End      : %s", end_raw or "(none)")
    logger.info("  Language : %s", speech_language)
    logger.info("  Speaker  : %s", speaker)
    logger.info("  Key via  : %s", key_source)

    start_offset = parse_ts(start_raw) if start_raw else 0.0

    # Azure Speech SDK requires WAV/PCM; convert automatically for any other format.
    needs_wav = audio_file_path.suffix.lower() != ".wav" or args.preprocess
    if needs_wav:
        tools_dir = Path(prop("tools_dir", "")).expanduser()
        ffmpeg = resolve_tool(tools_dir, "ffmpeg")
        import tempfile
        with tempfile.TemporaryDirectory(prefix="azuretranscribe_") as tmp_str:
            tmp_dir = Path(tmp_str)
            wav_path = tmp_dir / (audio_file_path.stem + ".azure.wav")
            if not convert_audio_for_azure(audio_file_path, wav_path, start_raw, end_raw, ffmpeg, logger):
                return 1
            segments = transcribe_with_azure(wav_path, speech_key, speech_region, speech_language, logger)
            if segments is None:
                return 1
            transcript_path = media_dir / (audio_file_path.stem + out_suffix + ".txt")
            write_transcript(segments, transcript_path, speaker, start_offset, logger)
    else:
        logger.info("[1/2] WAV input detected, passing directly to Azure Speech")
        segments = transcribe_with_azure(audio_file_path, speech_key, speech_region, speech_language, logger)
        if segments is None:
            return 1
        transcript_path = media_dir / (audio_file_path.stem + out_suffix + ".txt")
        write_transcript(segments, transcript_path, speaker, start_offset, logger)

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())