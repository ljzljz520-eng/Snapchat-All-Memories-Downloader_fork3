"""Command-line entry point: recovery first, then migrate or download."""

import argparse
import asyncio
import json
from pathlib import Path

from . import legacy as legacy_mod
from .index import IndexStore
from .models import Memory
from .pipeline import download_all
from .recovery import run_recovery


def load_memories(json_path: Path) -> list[Memory]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [Memory(**item) for item in data["Saved Media"]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Snapchat memories from data export "
        "(raw/derived two-phase with crash recovery)"
    )
    parser.add_argument(
        "json_file",
        nargs="?",
        default="json/memories_history.json",
        help="Path to memories_history.json",
    )
    parser.add_argument(
        "-o", "--output", default="./downloads", help="Output directory"
    )
    parser.add_argument(
        "-c", "--concurrent", type=int, default=40, help="Max concurrent downloads"
    )
    parser.add_argument("--no-exif", action="store_true", help="Disable EXIF metadata")
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-download even when the index already has the memory",
    )
    parser.add_argument(
        "--migrate-legacy",
        action="store_true",
        help="Register pre-upgrade flat jpg/mp4 files in place as "
        "raw_source=unknown, then exit",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    memories = load_memories(Path(args.json_file))
    index = IndexStore(output_dir).load()
    memories_by_fp = {memory.fingerprint: memory for memory in memories}
    await run_recovery(output_dir, index, memories_by_fp)

    if args.migrate_legacy:
        memories_by_stem = {memory.filename: memory for memory in memories}
        migrated = legacy_mod.migrate_legacy(
            output_dir, memories_by_stem, dict(index.entries)
        )
        if migrated:
            await index.replace_many(migrated)
            print(
                f"Migrated {len(migrated)} legacy file(s) in place "
                f"(raw_source=unknown); no files were moved or downloaded."
            )
        else:
            print("No untracked legacy files found.")
        return

    await download_all(
        memories,
        output_dir,
        max_concurrent=args.concurrent,
        add_exif=not args.no_exif,
        skip_existing=not args.no_skip_existing,
    )


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args))
