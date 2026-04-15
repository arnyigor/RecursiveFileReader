#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
generate_md_from_sources.py

Recursively walks through a directory tree, collects all files with the
specified extensions and writes a single Markdown document with statistics.

Features:
- Shows file size and line count for each file
- Generates summary statistics (total files, lines, size)
- Adds generation timestamp
- Supports directory exclusion
- Progress indication for large codebases
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple


def human_readable_size(size_bytes: int) -> str:
    """Convert bytes to human readable format."""
    if size_bytes == 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size_bytes)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def count_lines(content: str) -> int:
    """Count total lines in content."""
    return len(content.splitlines())


def get_language_tag(extension: str) -> str:
    """Map file extension to GitHub markdown language tag."""
    mapping = {
        ".kt": "kotlin",
        ".java": "java",
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".cpp": "cpp",
        ".c": "c",
        ".h": "c",
        ".hpp": "cpp",
        ".cs": "csharp",
        ".rb": "ruby",
        ".php": "php",
        ".swift": "swift",
        ".md": "markdown",
        ".json": "json",
        ".xml": "xml",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".sql": "sql",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "zsh",
        ".ps1": "powershell",
        ".html": "html",
        ".css": "css",
        ".scss": "scss",
        ".sass": "sass",
        ".less": "less",
    }
    return mapping.get(extension.lower(), "")


def collect_source_files(
    root: Path, extensions: Set[str], exclude_dirs: Set[str]
) -> List[Path]:
    """Return sorted list of files matching extensions, excluding specified dirs.

    Uses os.walk instead of rglob — allows pruning excluded directories
    before descending, which is significantly faster on large trees.
    """
    import mimetypes

    files: List[Path] = []

    def _is_text_file(path: Path) -> bool:
        mime, _ = mimetypes.guess_type(str(path))
        return mime is None or mime.startswith("text/")

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place — os.walk won't descend into them
        dirnames[:] = [d for d in dirnames if d.lower() not in exclude_dirs]
        for filename in filenames:
            p = Path(dirpath) / filename
            if p.suffix.lower() not in extensions:
                continue
            if not _is_text_file(p):
                continue
            files.append(p)

    return sorted(files)


def read_file_contents(file_path: Path) -> Tuple[str, int, int]:
    """
    Read file and return content, size in bytes, and line count.
    Returns empty string and zeros on error.
    """
    try:
        size = file_path.stat().st_size
        content = file_path.read_text(encoding="utf-8", errors="replace")
        lines = count_lines(content)
        return content, size, lines
    except Exception as exc:
        print(f"⚠️  Не удалось прочитать {file_path}: {exc}")
        return "", 0, 0


def generate_markdown(
    files: Iterable[Path], root: Path, verbose: bool = False
) -> Tuple[str, Dict[str, any]]:
    """
    Build the Markdown body and return statistics.

    Returns:
        Tuple of (markdown_text, stats_dict)
    """
    md_parts: List[str] = []
    stats = {"total_files": 0, "total_lines": 0, "total_size": 0, "languages": set()}

    # Generation header
    md_parts.append("# Source Code Documentation")
    md_parts.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    md_parts.append(f"\nRoot directory: `{root}`")
    md_parts.append("\n---\n")

    file_list = list(files)

    for idx, path in enumerate(file_list, 1):
        rel_path = path.relative_to(root)
        content, size, lines = read_file_contents(path)

        if size == 0 and not content:
            continue

        stats["total_files"] += 1
        stats["total_lines"] += lines
        stats["total_size"] += size
        stats["languages"].add(path.suffix.lower())

        lang_tag = get_language_tag(path.suffix)
        size_str = human_readable_size(size)

        # Header with metadata
        md_parts.append(f"### `{rel_path}`")
        md_parts.append(
            f"**Size:** {size_str} | **Lines:** {lines} | **Type:** {path.suffix[1:].upper()}"
        )
        md_parts.append("")

        # Code block
        md_parts.append(f"```{lang_tag}")
        md_parts.append(content.rstrip())
        md_parts.append("```\n")

        if verbose and idx % 10 == 0:
            print(f"  Processed {idx}/{len(file_list)} files...")

    # Insert summary at the top (after title)
    summary = [
        "## Summary Statistics\n",
        f"- **Total Files:** {stats['total_files']}",
        f"- **Total Lines:** {stats['total_lines']:,}",
        f"- **Total Size:** {human_readable_size(stats['total_size'])}",
        f"- **Languages:** {', '.join(sorted(stats['languages']))}",
        f"- **Average File Size:** {human_readable_size(stats['total_size'] // max(stats['total_files'], 1))}",
        "\n---\n",
    ]

    # Insert summary after the header (index 0 is title, 1-3 are metadata)
    md_parts = md_parts[:4] + summary + md_parts[4:]

    return "\n".join(md_parts), stats


def parse_extensions(raw: str) -> Set[str]:
    """Convert comma-separated string to set of lowercase extensions with dots."""
    return {f".{ext.lstrip('.').lower()}" for ext in raw.split(",") if ext.strip()}


def parse_exclusions(raw: str) -> Set[str]:
    """Parse comma-separated exclusion list."""
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Создать Markdown‑документ с исходным кодом и статистикой.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  %(prog)s -s ./src -e .kt,.java -o docs.md
  %(prog)s -s . -e .py --exclude venv,__pycache__,.git -v
        """,
    )
    parser.add_argument(
        "-s",
        "--source",
        default=".",
        help="Корневая папка для поиска (по умолчанию текущая).",
    )
    parser.add_argument(
        "-e",
        "--extensions",
        default=".kt,.java,.py",
        help="Расширения через запятую (по умолчанию: .kt,.java,.py).",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="source_dump.md",
        help="Имя выходного файла (по умолчанию source_dump.md).",
    )
    parser.add_argument(
        "--exclude",
        default="",
        help="Исключить папки (через запятую): node_modules,venv,__pycache__",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Показывать прогресс обработки."
    )

    args = parser.parse_args()

    root_dir = Path(args.source).expanduser().resolve()
    if not root_dir.exists():
        print(f"❌ Ошибка: Папка {root_dir} не существует.")
        return

    extensions_set = parse_extensions(args.extensions)
    exclude_set = parse_exclusions(args.exclude)

    if args.verbose:
        print(f"🔍 Сканирование {root_dir}...")
        print(f"   Расширения: {', '.join(extensions_set)}")
        if exclude_set:
            print(f"   Исключения: {', '.join(exclude_set)}")

    files = collect_source_files(root_dir, extensions_set, exclude_set)

    if not files:
        print("⚠️  Файлы с указанными расширениями не найдены.")
        return

    if args.verbose:
        print(f"📁 Найдено файлов: {len(files)}")

    markdown_text, stats = generate_markdown(files, root_dir, args.verbose)

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown_text, encoding="utf-8")

    print(f"✅ Создан: {output_path}")
    print(
        f"   Файлов: {stats['total_files']} | Строк: {stats['total_lines']:,} | Размер: {human_readable_size(stats['total_size'])}"
    )


if __name__ == "__main__":
    main()
