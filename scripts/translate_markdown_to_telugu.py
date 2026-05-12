from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from translate_markdown import (
    load_config,
    output_path_for_language,
    resolve_api_key,
    translate_markdown_file,
)

TARGET_LANG = "te"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate Markdown to Telugu using settings from properties file."
    )
    parser.add_argument(
        "--config",
        default="../config/media-config.properties",
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
    logger = logging.getLogger("translate_markdown_to_telugu")

    config_path = Path(args.config).expanduser().resolve()
    cfg = load_config(config_path)

    input_md_file = cfg.get("input_md_file", "").strip()
    endpoint = cfg.get("translator_endpoint", "").strip()
    region = cfg.get("translator_region", "").strip()
    from_lang = cfg.get("translator_from", "").strip()
    timeout_s_str = cfg.get("translator_timeout_s", "").strip()
    api_key, api_key_source = resolve_api_key(cfg)

    if not input_md_file:
        logger.error("Missing 'input_md_file' in %s", config_path)
        return 1
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
    if not endpoint:
        logger.error("Missing 'translator_endpoint' in %s", config_path)
        return 1
    if not region:
        logger.error("Missing 'translator_region' in %s", config_path)
        return 1
    if not from_lang:
        logger.error("Missing 'translator_from' in %s", config_path)
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

    output_path = output_path_for_language(input_path, TARGET_LANG)

    logger.info("Using config: %s", config_path)
    logger.info("Input file : %s", input_path)
    logger.info("API key via: %s", api_key_source)
    logger.info("From lang  : %s", from_lang)
    logger.info("To lang    : %s", TARGET_LANG)
    logger.info("Output file: %s", output_path)

    translate_markdown_file(
        input_path=input_path,
        output_path=output_path,
        api_key=api_key,
        region=region,
        endpoint=endpoint,
        from_lang=from_lang,
        to_lang=TARGET_LANG,
        timeout_s=timeout_s,
        logger=logger,
    )

    logger.info("Completed translation: %s", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
