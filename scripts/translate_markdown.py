from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

import requests


_SPRING_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


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


def resolve_spring_placeholders(value: str, props: dict[str, str]) -> str:
    """Resolve ${...} placeholders from built-ins, properties, and environment."""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "user.home":
            return str(Path.home())
        return props.get(key, os.environ.get(key, match.group(0)))

    return _SPRING_PLACEHOLDER.sub(_replace, value)


def load_config(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        raise SystemExit(f"Config file not found: {config_path}")

    raw_props = load_properties(config_path)
    resolved: dict[str, str] = {}
    for key, value in raw_props.items():
        resolved[key] = resolve_spring_placeholders(value, raw_props)
    return resolved


def resolve_api_key(cfg: dict[str, str]) -> tuple[str, str]:
    """Resolve translator API key from properties or environment.

    Resolution order:
    1) translator_api_key (direct value or resolved ${ENV_VAR})
    2) environment variable named by translator_api_key_env
    """
    api_key = cfg.get("translator_api_key", "").strip()
    if api_key and not _SPRING_PLACEHOLDER.fullmatch(api_key):
        return api_key, "translator_api_key"

    env_var_name = cfg.get("translator_api_key_env", "").strip()
    if env_var_name:
        env_api_key = os.environ.get(env_var_name, "").strip()
        if env_api_key:
            return env_api_key, f"env:{env_var_name}"

    default_env_api_key = os.environ.get("AZURE_TRANSLATOR_KEY", "").strip()
    if default_env_api_key:
        return default_env_api_key, "env:AZURE_TRANSLATOR_KEY"

    return "", env_var_name


def split_langs(value: str) -> list[str]:
    langs = [item.strip() for item in value.split(",") if item.strip()]
    return langs


def sanitize_lang_for_suffix(lang: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", lang.strip())


def output_path_for_language(input_path: Path, lang: str) -> Path:
    safe_lang = sanitize_lang_for_suffix(lang)
    return input_path.with_name(f"{input_path.stem}_{safe_lang}{input_path.suffix}")


def translate_text(
    text: str,
    api_key: str,
    region: str,
    endpoint: str,
    from_lang: str,
    to_lang: str,
    timeout_s: int,
) -> str:
    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Ocp-Apim-Subscription-Region": region,
        "Content-type": "application/json",
    }

    params: dict[str, str] = {
        "api-version": "3.0",
        "to": to_lang,
    }
    if from_lang and from_lang.lower() != "auto":
        params["from"] = from_lang

    body = [{"text": text}]
    response = requests.post(
        f"{endpoint.rstrip('/')}/translate",
        params=params,
        headers=headers,
        json=body,
        timeout=timeout_s,
    )
    response.raise_for_status()

    payload = response.json()
    return payload[0]["translations"][0]["text"]


def translate_markdown_file(
    input_path: Path,
    output_path: Path,
    api_key: str,
    region: str,
    endpoint: str,
    from_lang: str,
    to_lang: str,
    timeout_s: int,
    logger: logging.Logger,
) -> None:
    in_fence = False
    fence_prefixes = ("```", "~~~")

    with input_path.open("r", encoding="utf-8") as infile, output_path.open("w", encoding="utf-8") as outfile:
        buffer: list[str] = []

        def flush_buffer() -> None:
            nonlocal buffer
            if not buffer:
                return
            text = "".join(buffer)
            try:
                translated = translate_text(
                    text=text,
                    api_key=api_key,
                    region=region,
                    endpoint=endpoint,
                    from_lang=from_lang,
                    to_lang=to_lang,
                    timeout_s=timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Translation error: %s", exc)
                translated = text
            outfile.write(translated)
            buffer = []

        for line in infile:
            stripped = line.strip()

            if any(stripped.startswith(prefix) for prefix in fence_prefixes):
                flush_buffer()
                in_fence = not in_fence
                outfile.write(line)
                continue

            if in_fence:
                outfile.write(line)
                continue

            # Keep front-matter separators and blank lines unchanged.
            if stripped == "" or stripped == "---":
                flush_buffer()
                outfile.write(line)
                continue

            buffer.append(line)

        flush_buffer()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate Markdown using Azure Translator with settings from properties file."
    )
    parser.add_argument(
        "--config",
        default="config/media-config.properties",
        help="Path to properties file (default: config/media-config.properties)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logs",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("translate_markdown")

    config_path = Path(args.config).expanduser().resolve()
    cfg = load_config(config_path)

    input_md_file = cfg.get("input_md_file", "").strip()
    if not input_md_file:
        logger.error("Missing 'input_md_file' in %s", config_path)
        return 1

    endpoint = cfg.get("translator_endpoint", "").strip()
    api_key, api_key_source = resolve_api_key(cfg)
    region = cfg.get("translator_region", "").strip()
    from_lang = cfg.get("translator_from", "").strip()
    to_langs = split_langs(cfg.get("translator_to", "").strip())
    timeout_s_str = cfg.get("translator_timeout_s", "").strip()

    if not api_key:
        env_hint = cfg.get("translator_api_key_env", "").strip()
        if env_hint:
            logger.error(
                "Translator API key is missing. Set 'translator_api_key' or set environment variable '%s'.",
                env_hint,
            )
        else:
            logger.error("Missing 'translator_api_key' in %s", config_path)
        return 1
    if not region:
        logger.error("Missing 'translator_region' in %s", config_path)
        return 1
    if not endpoint:
        logger.error("Missing 'translator_endpoint' in %s", config_path)
        return 1
    if not from_lang:
        logger.error("Missing 'translator_from' in %s", config_path)
        return 1
    if not to_langs:
        logger.error("Missing 'translator_to' in %s", config_path)
        return 1
    if not timeout_s_str:
        logger.error("Missing 'translator_timeout_s' in %s", config_path)
        return 1

    try:
        timeout_s = int(timeout_s_str)
    except ValueError:
        logger.error("Invalid translator_timeout_s value: %s", timeout_s_str)
        return 1

    input_path = Path(input_md_file).expanduser().resolve()
    if not input_path.exists():
        logger.error("Input markdown file not found: %s", input_path)
        return 1

    logger.info("Using config: %s", config_path)
    logger.info("Input file : %s", input_path)
    logger.info("API key via: %s", api_key_source)
    logger.info("From lang  : %s", from_lang)
    logger.info("To lang(s) : %s", ", ".join(to_langs))

    for to_lang in to_langs:
        output_path = output_path_for_language(input_path, to_lang)
        logger.info("Translating -> %s", to_lang)
        logger.info("Output file : %s", output_path)

        translate_markdown_file(
            input_path=input_path,
            output_path=output_path,
            api_key=api_key,
            region=region,
            endpoint=endpoint,
            from_lang=from_lang,
            to_lang=to_lang,
            timeout_s=timeout_s,
            logger=logger,
        )

        logger.info("Completed translation: %s", output_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
