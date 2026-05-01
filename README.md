# spiritual-text-work

Python project for:
- downloading YouTube audio with selectable quality presets, and
- transcribing local audio files with timestamped speaker-style output.

Core workflow:
1. Download best available YouTube audio using yt-dlp
2. Convert with ffmpeg based on quality preset (COMPACT_SIZE, COMPACT_SIZE_SPEECH, etc.)
3. Transcribe local audio with faster-whisper medium model
4. Write transcript lines in this format:
   [HH:MM:SS.mmm -> HH:MM:SS.mmm] Speaker: text

## Project Structure

- config/media-config.properties
  Central configuration for tools path, media directory, input values, quality, and transcription settings.
- scripts/yt_download_audio.py
  Download script with quality preset support.
- scripts/yt_transcribe.py
  Transcription script (local audio input only).
- scripts/yt_caption_extract.py
  YouTube caption extraction script with speaker detection and timestamping.
- scripts/run_batch.ps1
  PowerShell batch runner for multiple downloads with quality/format overrides.
- requirements.txt
  Python dependencies.
- pyproject.toml
  Project metadata.

## Requirements

- Python 3.11+
- Windows tools installed in configured tools_dir:
  - yt-dlp.exe
  - ffmpeg.exe
  - ffprobe.exe
- Network access for YouTube download

Default tools path in config:
C:\Users\dhana\pssm-music-playlist-tools

## Setup

From project root:

1) Create virtual environment
python -m venv .venv

2) Activate
.venv\Scripts\Activate.ps1

3) Install dependencies
pip install -r requirements.txt

## Configuration

Edit config/media-config.properties.

Important fields:
- tools_dir
- media_dir
- videoId
- quality
- output_format
- audio_file
- startTime
- endTime
- audio_codec
- model
- lang
- speaker
- output_suffix

Notes:
- Download quality presets use Java-aligned mappings from MediaFileUtils:
  COMPACT_SIZE, COMPACT_SIZE_SPEECH, COMPACT_SIZE_MUSIC,
  COMPACT_MUSIC_INSTRUMENTAL, WHATSAPP, MUSIC_CONCERT, YOUTUBE_UPLOAD.
- Transcription script always uses local audio input (audio_file in config or --audio-file).
- startTime and endTime are optional for both scripts.

## Download Usage Examples (Quality-Based)

Run downloader with config values:
python scripts/yt_download_audio.py

Override video and quality:
python scripts/yt_download_audio.py --video-id Py8Z7D15JYo --quality COMPACT_SIZE_SPEECH

Download as mp3 with specific quality:
python scripts/yt_download_audio.py --video-id Py8Z7D15JYo --quality WHATSAPP --output-format mp3

Download clipped section only:
python scripts/yt_download_audio.py --video-id Py8Z7D15JYo --start 00:05:00 --end 00:20:00

Validate and save output in media_dir:
python scripts/yt_download_audio.py --video-id Py8Z7D15JYo --quality COMPACT_SIZE_MUSIC

## Transcription Usage Examples (Local Audio Only)

Run with config-provided audio_file:
python scripts/yt_transcribe.py

Run with explicit audio file:
python scripts/yt_transcribe.py --audio-file C:\Users\dhana\media-files\sample.m4a

Use clip range before transcription:
python scripts/yt_transcribe.py --audio-file C:\Users\dhana\media-files\sample.m4a --start 00:01:00 --end 00:10:00

Mixed-language transcription:
python scripts/yt_transcribe.py --audio-file C:\Users\dhana\media-files\sample.m4a --lang te,en

Verbose logs:
python scripts/yt_transcribe.py --audio-file C:\Users\dhana\media-files\sample.m4a --verbose

## Caption Extraction Usage Examples (YouTube Captions)

Download and extract captions as timestamped transcript from YouTube (alternative to transcription).

Run with config settings:
python scripts/yt_caption_extract.py --config config/media-config.properties

Extract specific video ID:
python scripts/yt_caption_extract.py dQw4w9WgXcQ

Extract with clipped time range:
python scripts/yt_caption_extract.py dQw4w9WgXcQ --start 00:05:00 --end 00:20:00

Extract with specific language:
python scripts/yt_caption_extract.py dQw4w9WgXcQ --lang hi

Extract multiple languages:
python scripts/yt_caption_extract.py dQw4w9WgXcQ --lang te,en

Extract entire playlist:
python scripts/yt_caption_extract.py --playlist-id PLT6lIcOhPFQoNxf1If3b7ExFzoHiOjkbG

Custom output path (single video only):
python scripts/yt_caption_extract.py dQw4w9WgXcQ --output my_captions.txt

Disable rolling-window deduplication:
python scripts/yt_caption_extract.py dQw4w9WgXcQ --no-dedup

Keep temporary caption files for inspection:
python scripts/yt_caption_extract.py dQw4w9WgXcQ --keep-tmp

Notes:
- Captions are downloaded using yt-dlp (same as audio download).
- Attempts manual captions first, falls back to auto-generated.
- Converts to SRT format and parses to timestamped lines.
- Detects speaker labels from caption text patterns (>>, [], "Name:").
- Falls back to config speaker value if no speaker detected.
- Rolling-window deduplication removes duplicate cues from YouTube auto-captions.
- Output format matches transcripts: [HH:MM:SS.mmm -> HH:MM:SS.mmm] Speaker: text

## Batch Processing Multiple Video IDs

Use the helper script for batch downloading.

Example 1: batch download with config quality
.\scripts\run_batch.ps1 -VideoIds Py8Z7D15JYo,C38Ov5g5e7c

Example 2: override quality/format in batch
.\scripts\run_batch.ps1 -VideoIds Py8Z7D15JYo,C38Ov5g5e7c -Quality COMPACT_SIZE_MUSIC -OutputFormat m4a

Example 3: jobs CSV with per-row start/end
# CSV columns: videoId,startTime,endTime
.\scripts\run_batch.ps1 -JobsCsv .\jobs.csv -StopOnError

Tip:
- Keep shared defaults in config/media-config.properties.
- For transcription batch, call yt_transcribe.py repeatedly with different --audio-file values.

## Output Files

Generated audio and transcripts are written to media_dir from config.

Typical outputs:
- <title>-<quality>.m4a or .mp3
- <audio-stem>.transcript.txt

Transcript line format:
[00:00:12.340 -> 00:00:16.120] Speaker 1: Example text

## Troubleshooting

1) Error: Config file not found
- Ensure config/media-config.properties exists.
- Or pass --config with a valid path.

2) Error: yt-dlp or ffmpeg not found
- Verify tools_dir points to folder containing executables.
- Or add tools to PATH.

3) Error: No local audio input found
- Set audio_file in config.
- Or pass --audio-file to yt_transcribe.py.

4) Error: faster-whisper not installed
- Run: pip install -r requirements.txt

5) Slow transcription on CPU
- medium model is heavier than tiny/base.
- Keep medium for better quality; use smaller model only if speed is critical.

## Quick Start

1) Set videoId and quality in config/media-config.properties
2) Activate .venv
3) Download:
python scripts/yt_download_audio.py
4) Set audio_file in config/media-config.properties (or pass --audio-file)
5) Transcribe:
python scripts/yt_transcribe.py
