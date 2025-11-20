#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
generate_md_from_sources.py

Recursively walks through a directory tree, collects all files with the
specified extensions (by default only ``.kt`` – Kotlin sources) and writes a
single Markdown document that contains:

* the relative file path as a header,
* the file contents wrapped in a fenced code block (` ```kotlin `).

The script can be used from the command line, e.g.:

    python generate_md_from_sources.py -s src/ -o all_sources.md

Author:   ваш‑имя
License:  MIT
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable, List, Set


def collect_source_files(root: Path, extensions: Set[str]) -> List[Path]:
    """
    Return a list of all files under *root* whose suffix matches one of the
    provided *extensions*.

    Parameters
    ----------
    root : Path
        Directory to start the search from.
    extensions : set[str]
        File suffixes (including leading dot, e.g. ``{'.kt', '.java'}``).

    Returns
    -------
    list[Path]
        Sorted list of matching file paths.
    """
    return sorted(
        [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in extensions]
    )


def read_file_contents(file_path: Path) -> str:
    """
    Read a text file using UTF‑8 encoding; on failure returns an empty string
    and prints a warning.

    Parameters
    ----------
    file_path : Path
        File to be read.

    Returns
    -------
    str
        Raw file contents (stripped trailing newlines are removed by the caller).
    """
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as exc:          # pragma: no cover - defensive
        print(f"⚠️  Не удалось прочитать {file_path}: {exc}")
        return ""


def generate_markdown(files: Iterable[Path], root: Path) -> str:
    """
    Build the Markdown body for a collection of files.

    Parameters
    ----------
    files : iterable[Path]
        Source file paths.
    root : Path
        Root directory (used to calculate relative paths).

    Returns
    -------
    str
        Markdown text ready to be written to disk.
    """
    md_parts: List[str] = []

    for path in files:
        rel_path = path.relative_to(root)
        content = read_file_contents(path)

        # Header with the relative file name
        md_parts.append(f"### `{rel_path}`")
        md_parts.append("")  # empty line

        # Fenced code block – hint `kotlin` enables syntax highlighting on GitHub
        md_parts.append("```kotlin")
        md_parts.append(content.rstrip())
        md_parts.append("```\n")          # trailing newline for separation

    return "\n".join(md_parts)


def parse_extensions(raw: str) -> Set[str]:
    """
    Convert a comma‑separated string of extensions into a set with leading
    dots and lowercased suffixes.

    Parameters
    ----------
    raw : str
        Raw argument from the CLI (e.g. ".kt,.java" or "kt,java").

    Returns
    -------
    set[str]
        Normalized suffix set.
    """
    return {f".{ext.lstrip('.').lower()}" for ext in raw.split(",") if ext.strip()}


def main() -> None:
    """Entry point – parse arguments and orchestrate the workflow."""
    parser = argparse.ArgumentParser(
        description="Создать Markdown‑файл, содержащий содержимое всех "
                    "исходных файлов с заданными расширениями."
    )
    parser.add_argument(
        "-s",
        "--source",
        default=".",
        help="Корневая папка для рекурсивного поиска (по умолчанию текущая).",
    )
    parser.add_argument(
        "-e",
        "--extensions",
        default=".kt",
        help="Список расширений через запятую, например '.kt,.java'. "
             "Точки не обязательны.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="source_dump.md",
        help="Имя выходного Markdown‑файла (по умолчанию source_dump.md).",
    )

    args = parser.parse_args()

    root_dir = Path(args.source).expanduser().resolve()
    extensions_set = parse_extensions(args.extensions)

    files = collect_source_files(root_dir, extensions_set)

    if not files:
        print("⚠️  Файлы с указанными расширениями не найдены.")
        return

    markdown_text = generate_markdown(files, root_dir)

    output_path = Path(args.output).expanduser().resolve()
    output_path.write_text(markdown_text, encoding="utf-8")

    print(f"✅ Markdown‑файл успешно создан: {output_path}")


if __name__ == "__main__":
    main()
