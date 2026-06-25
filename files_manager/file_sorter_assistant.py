#!/usr/bin/env python3
"""
File Sorter Assistant
Интерактивный сортировщик файлов с помощью локальной LLM llama.cpp / LM Studio
Python 3.12 | OpenAI-compatible /v1/chat/completions | JSON Mode
"""

import os
import sys
import json
import time
import shutil
import requests
import re
import configparser
import subprocess
import tarfile
import zipfile
import queue
import threading
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from dataclasses import dataclass, field

# ─────────────────────────────────────────────
#  Конфигурация
# ─────────────────────────────────────────────

LLAMACPP_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL_NAME = "gemma-4"

TEMPERATURE = 0.1
MAX_TOKENS = 32768
REQUEST_TIMEOUT = 120

MAX_DEPTH = 5
MAX_RECOMMENDATIONS = 5
MAX_ARCHIVE_SCAN_ENTRIES = 5000
MAX_ARCHIVE_PREVIEW_ENTRIES = 12
MAX_DUPLICATE_ARCHIVE_DETAIL_ENTRIES = 120
MAX_DUPLICATE_ARCHIVE_REPORT_FILES = 24
MAX_DUPLICATE_ARCHIVE_UNIQUE_PATHS = 18
MAX_NESTED_ARCHIVE_BYTES = 900 * 1024 * 1024
MAX_NESTED_ARCHIVES_PER_PARENT = 2
MIN_RECOMMENDATION_CONFIDENCE = 0.30
STRONG_LEAD_CONFIDENCE_GAP = 0.45
STRONG_LEAD_AUTO_SELECT_SECONDS = 3
DUPLICATE_SIMILARITY_THRESHOLD = 0.88
DUPLICATE_REVIEW_FOLDER = "_duplicates_review"
LLM_DUPLICATE_MAX_FILES = 300
LLM_DUPLICATE_MAX_GROUPS = 30
LLM_DUPLICATE_MAX_HINT_GROUPS = 40

DEFAULT_AUTO_SELECT_SECONDS = 60

# Если True — при авто-выборе папка будет создана без вопроса.
AUTO_CREATE_FOLDER_ON_TIMEOUT = True

# Если True — при авто-выборе файл будет перемещен без подтверждения.
AUTO_MOVE_WITHOUT_CONFIRMATION = True

# Небольшой запас на случай, если пользователь нажал клавишу ровно на границе таймаута.
TIMEOUT_INPUT_GRACE_SECONDS = 1.0

SCRIPT_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
CONFIG_PATH = SCRIPT_DIR / "file_sorter_assistant.ini"


# ─────────────────────────────────────────────
#  Архитектура состояний
# ─────────────────────────────────────────────


@dataclass
class SortSession:
    root: Path | None = None
    dest: Path | None = None
    tree_str: str = ""
    flat_paths: list[str] = field(default_factory=list)

    # 0 = автовыбор отключен
    auto_select_seconds: int = DEFAULT_AUTO_SELECT_SECONDS

    @property
    def target_dir(self) -> Path | None:
        return self.dest if self.dest else self.root


state = SortSession()


@dataclass
class ArchiveInspection:
    inspected: bool = False
    supported: bool = False
    archive_type: str = ""
    entries_scanned: int = 0
    total_entries: int | None = None
    truncated: bool = False
    preview_entries: list[str] = field(default_factory=list)
    has_init_py: bool = False
    has_blend: bool = False
    has_texture: bool = False
    has_uproject: bool = False
    has_uplugin: bool = False
    classification: str | None = None
    reason: str = ""
    error: str = ""


@dataclass
class ArchiveEntryDetail:
    path: str
    size: int | None = None
    modified: str = ""


# ─────────────────────────────────────────────
#  Вспомогательные функции путей
# ─────────────────────────────────────────────


def normalize_rel_folder(folder: str) -> str | None:
    """
    Нормализует путь папки от LLM:
    - убирает пробелы;
    - заменяет \\ на /;
    - схлопывает двойные слеши;
    - убирает слеши в начале и конце;
    - запрещает абсолютные пути;
    - запрещает выход через ..
    """
    if not isinstance(folder, str):
        return None

    folder = folder.strip().replace("\\", "/")
    folder = re.sub(r"/+", "/", folder)
    folder = folder.strip("/")

    if not folder or folder == ".":
        return None

    parts = folder.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None

    # Запрет Windows-дисков вида C:/...
    if re.match(r"^[a-zA-Z]:", folder):
        return None

    # Запрет абсолютных путей
    if Path(folder).is_absolute():
        return None

    return folder


def safe_join_base(base: Path, rel_folder: str) -> Path | None:
    """
    Безопасно соединяет base + rel_folder.
    Не дает LLM выйти за пределы base через ../ или абсолютный путь.
    """
    normalized = normalize_rel_folder(rel_folder)
    if not normalized:
        return None

    base_resolved = base.resolve()
    dest = (base_resolved / normalized).resolve()

    if dest != base_resolved and base_resolved not in dest.parents:
        return None

    return dest


def path_exists_in_base(base: Path, rel_path: str) -> bool:
    dest = safe_join_base(base, rel_path)
    return bool(dest and dest.is_dir())


# ─────────────────────────────────────────────
#  INI config
# ─────────────────────────────────────────────


def load_config() -> None:
    if not CONFIG_PATH.exists():
        return

    config = configparser.ConfigParser()
    config.read(CONFIG_PATH, encoding="utf-8")

    paths = config["paths"] if config.has_section("paths") else {}

    root_raw = paths.get("root", "").strip()
    dest_raw = paths.get("dest", "").strip()

    if root_raw:
        root_path = Path(root_raw).expanduser()
        if root_path.is_dir():
            state.root = root_path.resolve()
        else:
            print(f"  [!] root из ini недоступен: {root_path}")

    if dest_raw:
        dest_path = Path(dest_raw).expanduser()
        if dest_path.is_dir():
            state.dest = dest_path.resolve()
        else:
            print(f"  [!] dest из ini недоступен: {dest_path}")

    settings = config["settings"] if config.has_section("settings") else {}

    timeout_raw = settings.get("auto_select_seconds", "").strip()
    if timeout_raw:
        try:
            value = int(timeout_raw)
            state.auto_select_seconds = max(0, value)
        except ValueError:
            print(f"  [!] Некорректный auto_select_seconds в ini: {timeout_raw}")


def save_config() -> None:
    config = configparser.ConfigParser()

    config["paths"] = {
        "root": str(state.root) if state.root else "",
        "dest": str(state.dest) if state.dest else "",
    }

    config["settings"] = {
        "auto_select_seconds": str(state.auto_select_seconds),
    }

    try:
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            config.write(f)
    except OSError as e:
        print(f"  [ОШИБКА] Не удалось сохранить ini: {e}")


# ─────────────────────────────────────────────
#  Дерево папок
# ─────────────────────────────────────────────


def build_tree(base_dir: Path, max_depth: int = MAX_DEPTH) -> tuple[str, list[str]]:
    """
    Строит дерево папок и плоский список относительных путей.
    Игнорирует символические ссылки для предотвращения рекурсивных петель.
    """
    lines: list[str] = []
    flat_paths: list[str] = []

    def _walk(current: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return

        try:
            entries = sorted(
                [e for e in current.iterdir() if e.is_dir() and not e.is_symlink()],
                key=lambda e: e.name.lower(),
            )
        except (PermissionError, OSError):
            return

        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{entry.name}/")

            rel_path = str(entry.relative_to(base_dir)).replace("\\", "/")
            rel_path = normalize_rel_folder(rel_path)

            if rel_path:
                flat_paths.append(rel_path)

            extension = "    " if is_last else "│   "
            _walk(entry, prefix + extension, depth + 1)

    lines.append(f"{base_dir.name}/")
    _walk(base_dir, "", 1)

    return "\n".join(lines), sorted(set(flat_paths))


def refresh_folder_cache() -> bool:
    target = state.target_dir

    if not target:
        print("  [!] Сначала установите root или dest.")
        return False

    if not target.is_dir():
        print(f"  [!] Папка назначения недоступна: {target}")
        return False

    print(f"  [scan] Сканирование структуры: {target} до {MAX_DEPTH} уровней...")
    state.tree_str, state.flat_paths = build_tree(target, MAX_DEPTH)
    print(f"  [scan] Найдено папок: {len(state.flat_paths)}")

    return True


def print_tree_numbered(flat_paths: list[str]) -> None:
    print()

    for i, path in enumerate(flat_paths, 1):
        depth = path.count("/")
        indent = "  " * depth
        name = path.split("/")[-1]
        print(f"  {i:>4}. {indent}{name}/  [{path}]")

    print()


# ─────────────────────────────────────────────
#  Общие вспомогательные функции
# ─────────────────────────────────────────────


def hr(char: str = "─", width: int = 60) -> str:
    return char * width


def format_size(size_bytes: int) -> str:
    value = float(size_bytes)

    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{value:.1f} TB"


# ─────────────────────────────────────────────
#  Быстрый просмотр архивов без распаковки
# ─────────────────────────────────────────────

ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".7z",
    ".rar",
)

TEXTURE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".tga",
    ".tif",
    ".tiff",
    ".exr",
    ".hdr",
    ".bmp",
    ".webp",
    ".dds",
}


def archive_suffix(path: Path) -> str | None:
    name = path.name.lower()

    for suffix in ARCHIVE_SUFFIXES:
        if name.endswith(suffix):
            return suffix

    return None


def normalize_archive_entry(name: str) -> str | None:
    normalized = name.replace("\\", "/").strip("/")

    if not normalized or normalized.endswith("/"):
        return None

    parts = normalized.split("/")

    if any(part in ("", ".", "..") for part in parts):
        return None

    return normalized


def limited_archive_entries(entries: list[str]) -> tuple[list[str], bool]:
    normalized = []

    for entry in entries:
        path = normalize_archive_entry(entry)

        if path:
            normalized.append(path)

    return normalized[:MAX_ARCHIVE_SCAN_ENTRIES], len(normalized) > MAX_ARCHIVE_SCAN_ENTRIES


def limited_archive_entry_details(entries: list[ArchiveEntryDetail]) -> tuple[list[ArchiveEntryDetail], bool]:
    normalized: list[ArchiveEntryDetail] = []

    for entry in entries:
        path = normalize_archive_entry(entry.path)

        if path:
            normalized.append(ArchiveEntryDetail(path=path, size=entry.size, modified=entry.modified))

    return normalized[:MAX_ARCHIVE_SCAN_ENTRIES], len(normalized) > MAX_ARCHIVE_SCAN_ENTRIES


def format_zip_datetime(date_time: tuple[int, int, int, int, int, int]) -> str:
    try:
        return f"{date_time[0]:04d}-{date_time[1]:02d}-{date_time[2]:02d} {date_time[3]:02d}:{date_time[4]:02d}"
    except Exception:
        return ""


def format_timestamp(ts: float | int | None) -> str:
    if not ts:
        return ""

    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


def read_zip_entries(file_path: Path) -> tuple[list[str], int | None, bool]:
    with zipfile.ZipFile(file_path) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]

    entries, truncated = limited_archive_entries(names)
    return entries, len(names), truncated


def read_zip_entry_details(file_path: Path) -> tuple[list[ArchiveEntryDetail], int | None, bool]:
    with zipfile.ZipFile(file_path) as archive:
        infos = [
            ArchiveEntryDetail(
                path=info.filename,
                size=info.file_size,
                modified=format_zip_datetime(info.date_time),
            )
            for info in archive.infolist()
            if not info.is_dir()
        ]

    entries, truncated = limited_archive_entry_details(infos)
    return entries, len(infos), truncated


def read_tar_entries(file_path: Path) -> tuple[list[str], int | None, bool]:
    entries: list[str] = []
    truncated = False

    with tarfile.open(file_path, mode="r:*") as archive:
        for member in archive:
            if member.isfile():
                path = normalize_archive_entry(member.name)

                if path:
                    entries.append(path)

            if len(entries) >= MAX_ARCHIVE_SCAN_ENTRIES:
                truncated = True
                break

    return entries, None, truncated


def read_tar_entry_details(file_path: Path) -> tuple[list[ArchiveEntryDetail], int | None, bool]:
    entries: list[ArchiveEntryDetail] = []
    truncated = False

    with tarfile.open(file_path, mode="r:*") as archive:
        for member in archive:
            if member.isfile():
                path = normalize_archive_entry(member.name)

                if path:
                    entries.append(
                        ArchiveEntryDetail(
                            path=path,
                            size=member.size,
                            modified=format_timestamp(member.mtime),
                        )
                    )

            if len(entries) >= MAX_ARCHIVE_SCAN_ENTRIES:
                truncated = True
                break

    return entries, None, truncated


def candidate_archive_tools(names: list[str], common_relative_paths: list[str]) -> list[str]:
    candidates: list[str] = []

    for name in names:
        path = shutil.which(name)

        if path:
            candidates.append(path)

    roots = [
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("LocalAppData", ""),
    ]

    for root in roots:
        if not root:
            continue

        for rel_path in common_relative_paths:
            path = Path(root) / rel_path

            if path.is_file():
                candidates.append(str(path))

    unique: list[str] = []

    for path in candidates:
        if path not in unique:
            unique.append(path)

    return unique


def run_archive_tool(args: list[str], timeout: int) -> tuple[subprocess.CompletedProcess[str] | None, str]:
    startupinfo = None
    creationflags = 0

    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        return completed, ""
    except Exception as e:
        return None, str(e)


def parse_7z_slt_output(output: str, archive_name: str) -> tuple[list[str], int | None, bool]:
    entries: list[str] = []

    for line in output.splitlines():
        if not line.startswith("Path = "):
            continue

        path = normalize_archive_entry(line[len("Path = ") :])

        if path and path != archive_name:
            entries.append(path)

        if len(entries) >= MAX_ARCHIVE_SCAN_ENTRIES:
            return entries, None, True

    return entries, len(entries), False


def parse_7z_slt_details(output: str, archive_name: str) -> tuple[list[ArchiveEntryDetail], int | None, bool]:
    entries: list[ArchiveEntryDetail] = []
    current: dict[str, str] = {}

    def flush_current() -> None:
        if not current:
            return

        path = normalize_archive_entry(current.get("Path", ""))

        if not path or path == archive_name:
            current.clear()
            return

        attributes = current.get("Attributes", "")
        size_raw = current.get("Size", "")

        if "D" in attributes and not size_raw:
            current.clear()
            return

        try:
            size = int(size_raw) if size_raw else None
        except ValueError:
            size = None

        modified = current.get("Modified", "")[:16]
        entries.append(ArchiveEntryDetail(path=path, size=size, modified=modified))
        current.clear()

    for line in output.splitlines():
        if not line.strip():
            flush_current()
            continue

        if " = " not in line:
            continue

        key, value = line.split(" = ", 1)

        if key == "Path" and current:
            flush_current()

        current[key] = value

        if len(entries) >= MAX_ARCHIVE_SCAN_ENTRIES:
            return entries, None, True

    flush_current()
    return entries[:MAX_ARCHIVE_SCAN_ENTRIES], len(entries), len(entries) > MAX_ARCHIVE_SCAN_ENTRIES


def read_7z_entries(file_path: Path) -> tuple[list[str], int | None, bool]:
    py7zr_error = ""

    try:
        import py7zr  # type: ignore

        with py7zr.SevenZipFile(file_path, mode="r") as archive:
            names = [
                info.filename
                for info in archive.list()
                if getattr(info, "is_file", False)
            ]

        entries, truncated = limited_archive_entries(names)
        return entries, len(names), truncated
    except ImportError:
        pass
    except Exception as e:
        py7zr_error = f"py7zr: {e}"

    tools = candidate_archive_tools(
        names=["7z", "7za", "7z.exe", "7za.exe"],
        common_relative_paths=[
            "7-Zip/7z.exe",
            "7-Zip/7za.exe",
            "NanaZip/NanaZipC.exe",
        ],
    )

    if not tools:
        suffix = f"; {py7zr_error}" if py7zr_error else ""
        raise RuntimeError(
            "для .7z не найден py7zr или 7z/7za в PATH/Program Files"
            f"{suffix}"
        )

    errors: list[str] = [py7zr_error] if py7zr_error else []

    for exe in tools:
        completed = subprocess.run(
            [exe, "l", "-slt", str(file_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

        if completed.returncode == 0:
            return parse_7z_slt_output(completed.stdout, file_path.name)

        errors.append(completed.stderr.strip() or completed.stdout.strip() or exe)

    raise RuntimeError("; ".join(errors))


def read_7z_entry_details(file_path: Path) -> tuple[list[ArchiveEntryDetail], int | None, bool]:
    py7zr_error = ""

    try:
        import py7zr  # type: ignore

        with py7zr.SevenZipFile(file_path, mode="r") as archive:
            infos = []

            for info in archive.list():
                if not getattr(info, "is_file", False):
                    continue

                size = getattr(info, "uncompressed", None)
                modified = getattr(info, "lastwritetime", None)
                modified_text = modified.strftime("%Y-%m-%d %H:%M") if modified else ""
                infos.append(ArchiveEntryDetail(path=info.filename, size=size, modified=modified_text))

        entries, truncated = limited_archive_entry_details(infos)
        return entries, len(infos), truncated
    except ImportError:
        pass
    except Exception as e:
        py7zr_error = f"py7zr: {e}"

    tools = candidate_archive_tools(
        names=["7z", "7za", "7z.exe", "7za.exe"],
        common_relative_paths=[
            "7-Zip/7z.exe",
            "7-Zip/7za.exe",
            "NanaZip/NanaZipC.exe",
        ],
    )

    if not tools:
        suffix = f"; {py7zr_error}" if py7zr_error else ""
        raise RuntimeError(
            "для .7z не найден py7zr или 7z/7za в PATH/Program Files"
            f"{suffix}"
        )

    errors: list[str] = [py7zr_error] if py7zr_error else []

    for exe in tools:
        completed = subprocess.run(
            [exe, "l", "-slt", str(file_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )

        if completed.returncode == 0:
            return parse_7z_slt_details(completed.stdout, file_path.name)

        errors.append(completed.stderr.strip() or completed.stdout.strip() or exe)

    raise RuntimeError("; ".join(errors))


def read_rar_entries_with_rarfile(file_path: Path) -> tuple[list[str], int | None, bool] | None:
    try:
        import rarfile  # type: ignore
    except ImportError:
        return None

    with rarfile.RarFile(file_path) as archive:
        names = [info.filename for info in archive.infolist() if not info.isdir()]

    entries, truncated = limited_archive_entries(names)
    return entries, len(names), truncated


def read_rar_entry_details_with_rarfile(file_path: Path) -> tuple[list[ArchiveEntryDetail], int | None, bool] | None:
    try:
        import rarfile  # type: ignore
    except ImportError:
        return None

    with rarfile.RarFile(file_path) as archive:
        infos = [
            ArchiveEntryDetail(
                path=info.filename,
                size=getattr(info, "file_size", None),
                modified=format_zip_datetime(info.date_time) if getattr(info, "date_time", None) else "",
            )
            for info in archive.infolist()
            if not info.isdir()
        ]

    entries, truncated = limited_archive_entry_details(infos)
    return entries, len(infos), truncated


def parse_unrar_list_output(output: str) -> tuple[list[str], int | None, bool]:
    entries: list[str] = []
    seen: set[str] = set()

    for line in output.splitlines():
        path = normalize_archive_entry(line)

        if not path:
            continue

        # UnRAR/WinRAR can list directories in bare mode. Keep likely files only.
        if "." not in Path(path).name:
            continue

        if path in seen:
            continue

        seen.add(path)
        entries.append(path)

        if len(entries) >= MAX_ARCHIVE_SCAN_ENTRIES:
            return entries, None, True

    return entries, len(entries), False


def parse_unrar_technical_details(output: str) -> tuple[list[ArchiveEntryDetail], int | None, bool]:
    entries: list[ArchiveEntryDetail] = []
    current: dict[str, str] = {}

    key_map = {
        "name": "path",
        "path": "path",
        "pathname": "path",
        "file name": "path",
        "size": "size",
        "file size": "size",
        "modified": "modified",
        "mtime": "modified",
        "date": "modified",
    }

    def flush_current() -> None:
        if not current:
            return

        path = normalize_archive_entry(current.get("path", ""))

        if not path:
            current.clear()
            return

        size_raw = current.get("size", "")
        size_match = re.search(r"\d+", size_raw.replace(" ", ""))

        try:
            size = int(size_match.group(0)) if size_match else None
        except ValueError:
            size = None

        entries.append(
            ArchiveEntryDetail(
                path=path,
                size=size,
                modified=current.get("modified", "")[:16],
            )
        )
        current.clear()

    for raw_line in output.splitlines():
        line = raw_line.strip()

        if not line:
            flush_current()
            continue

        if ":" in line:
            raw_key, value = line.split(":", 1)
        elif "=" in line:
            raw_key, value = line.split("=", 1)
        else:
            continue

        key = key_map.get(raw_key.strip().lower())

        if not key:
            continue

        if key == "path" and current.get("path"):
            flush_current()

        current[key] = value.strip()

        if len(entries) >= MAX_ARCHIVE_SCAN_ENTRIES:
            return entries, None, True

    flush_current()

    if not entries:
        raise RuntimeError("technical list не содержит распознанных файлов")

    return entries[:MAX_ARCHIVE_SCAN_ENTRIES], len(entries), len(entries) > MAX_ARCHIVE_SCAN_ENTRIES


def read_rar_entry_details_with_bare_list(file_path: Path, exe: str) -> tuple[list[ArchiveEntryDetail], int | None, bool]:
    completed, error = run_archive_tool([exe, "lb", str(file_path)], timeout=20)

    if completed is None:
        raise RuntimeError(error or exe)

    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or exe)

    names, total_entries, truncated = parse_unrar_list_output(completed.stdout)
    return [ArchiveEntryDetail(path=name) for name in names], total_entries, truncated


def read_rar_entries(file_path: Path) -> tuple[list[str], int | None, bool]:
    errors: list[str] = []

    try:
        rarfile_result = read_rar_entries_with_rarfile(file_path)
    except Exception as e:
        rarfile_result = None
        errors.append(f"rarfile: {e}")

    if rarfile_result is not None:
        return rarfile_result

    seven_zip_tools = candidate_archive_tools(
        names=["7z", "7za", "7z.exe", "7za.exe"],
        common_relative_paths=["7-Zip/7z.exe", "7-Zip/7za.exe"],
    )

    for exe in seven_zip_tools:
        completed, error = run_archive_tool([exe, "l", "-slt", str(file_path)], timeout=15)

        if completed is None:
            errors.append(error or exe)
            continue

        if completed.returncode == 0:
            return parse_7z_slt_output(completed.stdout, file_path.name)

        errors.append(completed.stderr.strip() or completed.stdout.strip() or exe)

    rar_tools = candidate_archive_tools(
        names=["unrar", "rar", "UnRAR.exe", "Rar.exe"],
        common_relative_paths=[
            "WinRAR/UnRAR.exe",
            "WinRAR/Rar.exe",
        ],
    )

    for exe in rar_tools:
        completed, error = run_archive_tool([exe, "lb", str(file_path)], timeout=15)

        if completed is None:
            errors.append(error or exe)
            continue

        if completed.returncode == 0:
            return parse_unrar_list_output(completed.stdout)

        errors.append(completed.stderr.strip() or completed.stdout.strip() or exe)

    details = "; ".join(errors)
    suffix = f": {details}" if details else ""
    raise RuntimeError(
        "для .rar не найден rarfile, 7z/7za или WinRAR/UnRAR в PATH/Program Files"
        f"{suffix}"
    )


def read_rar_entry_details(file_path: Path) -> tuple[list[ArchiveEntryDetail], int | None, bool]:
    errors: list[str] = []

    try:
        rarfile_result = read_rar_entry_details_with_rarfile(file_path)
    except Exception as e:
        rarfile_result = None
        errors.append(f"rarfile: {e}")

    if rarfile_result is not None:
        return rarfile_result

    seven_zip_tools = candidate_archive_tools(
        names=["7z", "7za", "7z.exe", "7za.exe"],
        common_relative_paths=["7-Zip/7z.exe", "7-Zip/7za.exe"],
    )

    for exe in seven_zip_tools:
        completed, error = run_archive_tool([exe, "l", "-slt", str(file_path)], timeout=20)

        if completed is None:
            errors.append(error or exe)
            continue

        if completed.returncode == 0:
            return parse_7z_slt_details(completed.stdout, file_path.name)

        errors.append(completed.stderr.strip() or completed.stdout.strip() or exe)

    rar_tools = candidate_archive_tools(
        names=["unrar", "rar", "UnRAR.exe", "Rar.exe"],
        common_relative_paths=[
            "WinRAR/UnRAR.exe",
            "WinRAR/Rar.exe",
        ],
    )

    for exe in rar_tools:
        completed, error = run_archive_tool([exe, "lt", str(file_path)], timeout=20)

        if completed is None:
            errors.append(error or exe)
            continue

        if completed.returncode == 0:
            try:
                return parse_unrar_technical_details(completed.stdout)
            except Exception as e:
                errors.append(f"{exe} lt parse: {e}")
        else:
            errors.append(completed.stderr.strip() or completed.stdout.strip() or exe)

    for exe in rar_tools:
        try:
            return read_rar_entry_details_with_bare_list(file_path, exe)
        except Exception as e:
            errors.append(f"{exe} lb: {e}")

    raise RuntimeError("; ".join(errors) or "не удалось прочитать подробности rar")


def classify_archive_entries(entries: list[str]) -> tuple[str | None, str]:
    lower_entries = [entry.lower() for entry in entries]
    basenames = [Path(entry).name.lower() for entry in lower_entries]

    has_init_py = "__init__.py" in basenames
    has_blend = any(name.endswith(".blend") for name in lower_entries)
    has_texture = any(Path(name).suffix.lower() in TEXTURE_SUFFIXES for name in lower_entries)
    has_uproject = any(name.endswith(".uproject") for name in lower_entries)
    has_uplugin = any(name.endswith(".uplugin") for name in lower_entries)
    has_uasset = any(name.endswith(".uasset") for name in lower_entries)
    has_hda = any(name.endswith((".hda", ".hdalc", ".hdanc")) for name in lower_entries)
    has_unitypackage = any(name.endswith(".unitypackage") for name in lower_entries)
    has_adobe_extension = any(
        name.endswith((".zxp", ".ccx", ".jsx", ".jsxbin", ".atn", ".abr"))
        for name in lower_entries
    )

    if has_uproject:
        return "ue_project", "найден .uproject внутри архива"

    if has_uplugin:
        return "ue_plugin", "найден .uplugin внутри архива"

    if has_hda:
        return "houdini_asset", "найдены Houdini Digital Assets (.hda)"

    if has_unitypackage:
        return "unity_package", "найден .unitypackage внутри архива"

    if has_adobe_extension:
        return "adobe_extension", "найдены Adobe/Photoshop extension файлы"

    if has_uasset:
        return "ue_content", "найдены Unreal Engine content файлы (.uasset)"

    if has_init_py:
        return "blender_addon", "найден __init__.py внутри архива"

    if has_blend and has_texture:
        return "blender_assets", "найдены .blend файл и текстуры"

    if has_blend:
        return "blender_assets", "найден .blend файл"

    return None, ""


def read_archive_entry_details(file_path: Path) -> tuple[list[ArchiveEntryDetail], int | None, bool]:
    suffix = archive_suffix(file_path)

    if suffix == ".zip":
        return read_zip_entry_details(file_path)

    if suffix in {".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"}:
        return read_tar_entry_details(file_path)

    if suffix == ".7z":
        return read_7z_entry_details(file_path)

    if suffix == ".rar":
        return read_rar_entry_details(file_path)

    raise RuntimeError("неподдерживаемый архив")


def archive_detail_key_file(entry: ArchiveEntryDetail) -> bool:
    path = entry.path.lower()
    name = Path(path).name
    suffix = Path(path).suffix.lower()

    if name in {"__init__.py", "blender_manifest.toml", "package.json", "manifest.json"}:
        return True

    if suffix in {
        ".blend",
        ".py",
        ".json",
        ".toml",
        ".txt",
        ".md",
        ".exe",
        ".dll",
        ".hda",
        ".uplugin",
        ".uproject",
        ".unitypackage",
        ".zxp",
        ".ccx",
    }:
        return True

    return False


def format_archive_entry_detail(entry: ArchiveEntryDetail) -> str:
    size = format_size(entry.size) if entry.size is not None else "?"
    modified = entry.modified or "?"
    return f"{entry.path} | size={size} | modified={modified}"


def nested_archive_entries(entries: list[ArchiveEntryDetail]) -> list[ArchiveEntryDetail]:
    nested = [
        entry
        for entry in entries
        if archive_suffix(Path(entry.path))
    ]
    return sorted(nested, key=lambda entry: entry.size or 0, reverse=True)[:MAX_NESTED_ARCHIVES_PER_PARENT]


def safe_nested_output_path(tmp_dir: Path, entry_path: str) -> Path:
    name = Path(entry_path.replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._")

    if not name:
        name = "nested_archive"

    return tmp_dir / name


def extract_nested_archive(parent_path: Path, entry_path: str, output_path: Path) -> None:
    suffix = archive_suffix(parent_path)

    if suffix == ".zip":
        with zipfile.ZipFile(parent_path) as archive:
            with archive.open(entry_path) as src, output_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        return

    if suffix in {".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"}:
        with tarfile.open(parent_path, mode="r:*") as archive:
            member = archive.getmember(entry_path)
            src = archive.extractfile(member)

            if src is None:
                raise RuntimeError("tar member cannot be extracted as file")

            with src, output_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        return

    if suffix == ".rar":
        import rarfile  # type: ignore

        with rarfile.RarFile(parent_path) as archive:
            with archive.open(entry_path) as src, output_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        return

    if suffix == ".7z":
        import py7zr  # type: ignore

        with py7zr.SevenZipFile(parent_path, mode="r") as archive:
            archive.extract(path=output_path.parent, targets=[entry_path])

        extracted_path = output_path.parent / entry_path

        if not extracted_path.exists():
            raise RuntimeError("py7zr did not extract requested nested archive")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted_path), str(output_path))
        return

    raise RuntimeError("parent archive type does not support nested extraction")


def format_nested_archive_report(parent_path: Path, nested_entry: ArchiveEntryDetail) -> tuple[str, list[ArchiveEntryDetail]]:
    if nested_entry.size is not None and nested_entry.size > MAX_NESTED_ARCHIVE_BYTES:
        return (
            f"nested_archive: {nested_entry.path} | size={format_size(nested_entry.size)} | skipped=too_large",
            [],
        )

    try:
        with tempfile.TemporaryDirectory(prefix="file_sorter_nested_") as tmp:
            tmp_dir = Path(tmp)
            nested_path = safe_nested_output_path(tmp_dir, nested_entry.path)
            extract_nested_archive(parent_path, nested_entry.path, nested_path)
            entries, total_entries, truncated = read_archive_entry_details(nested_path)
    except Exception as e:
        return f"nested_archive: {nested_entry.path} | scan_error={e}", []

    known_sizes = [entry.size for entry in entries if entry.size is not None]
    paths = [entry.path for entry in entries]
    classification, reason = classify_archive_entries(paths)
    sample = entries[:MAX_DUPLICATE_ARCHIVE_REPORT_FILES]
    key_entries = [entry for entry in entries if archive_detail_key_file(entry)][:MAX_DUPLICATE_ARCHIVE_REPORT_FILES]
    report = "\n".join(
        [
            f"nested_archive: {nested_entry.path}",
            f"  entries_scanned: {len(entries)}",
            f"  total_entries: {total_entries if total_entries is not None else 'unknown'}",
            f"  truncated: {truncated}",
            f"  uncompressed_size_scanned: {format_size(sum(known_sizes)) if known_sizes else 'unknown'}",
            f"  classification: {classification or 'unknown'}",
            f"  classification_reason: {reason or 'no strong deterministic signal'}",
            "  key_files:",
            *[f"    - {format_archive_entry_detail(entry)}" for entry in key_entries],
            "  sample_files:",
            *[f"    - {format_archive_entry_detail(entry)}" for entry in sample],
        ]
    )

    prefixed_entries = [
        ArchiveEntryDetail(
            path=f"{nested_entry.path}::{entry.path}",
            size=entry.size,
            modified=entry.modified,
        )
        for entry in entries
    ]
    return report, prefixed_entries


def format_duplicate_archive_deep_report(file_id: int, file_path: Path) -> tuple[str, list[ArchiveEntryDetail]]:
    if not archive_suffix(file_path):
        return f"[{file_id}] {file_path.name}\narchive: no\n", []

    try:
        entries, total_entries, truncated = read_archive_entry_details(file_path)
    except Exception as e:
        return (
            f"[{file_id}] {file_path.name}\n"
            f"archive: yes\n"
            f"deep_scan_error: {e}\n"
        ), []

    paths = [entry.path for entry in entries]
    classification, reason = classify_archive_entries(paths)
    known_sizes = [entry.size for entry in entries if entry.size is not None]
    total_size = sum(known_sizes)
    extension_counts: dict[str, int] = {}
    modified_values = [entry.modified for entry in entries if entry.modified]

    for entry in entries:
        suffix = Path(entry.path).suffix.lower() or "<no_ext>"
        extension_counts[suffix] = extension_counts.get(suffix, 0) + 1

    ext_summary = ", ".join(
        f"{suffix}:{count}"
        for suffix, count in sorted(extension_counts.items(), key=lambda item: (-item[1], item[0]))[:12]
    )
    largest_entries = sorted(entries, key=lambda entry: entry.size or 0, reverse=True)[
        :MAX_DUPLICATE_ARCHIVE_REPORT_FILES
    ]
    key_entries = [
        entry
        for entry in entries
        if archive_detail_key_file(entry)
    ][:MAX_DUPLICATE_ARCHIVE_REPORT_FILES]
    sample_entries = entries[:MAX_DUPLICATE_ARCHIVE_REPORT_FILES]
    nested_reports: list[str] = []
    nested_detail_entries: list[ArchiveEntryDetail] = []

    for nested_entry in nested_archive_entries(entries):
        nested_report, nested_entries = format_nested_archive_report(file_path, nested_entry)
        nested_reports.append(nested_report)
        nested_detail_entries.extend(nested_entries)

    parts = [
        f"[{file_id}] {file_path.name}",
        "archive: yes",
        f"type: {archive_suffix(file_path).lstrip('.')}",
        f"entries_scanned: {len(entries)}",
        f"total_entries: {total_entries if total_entries is not None else 'unknown'}",
        f"truncated: {truncated}",
        f"uncompressed_size_scanned: {format_size(total_size) if known_sizes else 'unknown'}",
        f"modified_range: {min(modified_values) if modified_values else '?'} .. {max(modified_values) if modified_values else '?'}",
        f"classification: {classification or 'unknown'}",
        f"classification_reason: {reason or 'no strong deterministic signal'}",
        f"extension_counts: {ext_summary or 'none'}",
        "largest_files:",
        *[f"  - {format_archive_entry_detail(entry)}" for entry in largest_entries],
        "key_files:",
        *[f"  - {format_archive_entry_detail(entry)}" for entry in key_entries],
        "sample_files:",
        *[f"  - {format_archive_entry_detail(entry)}" for entry in sample_entries],
    ]

    if nested_reports:
        parts.extend(
            [
                "nested_archives:",
                *nested_reports,
            ]
        )

    return "\n".join(parts) + "\n", entries + nested_detail_entries


def format_archive_structure_comparison(details_by_id: dict[int, list[ArchiveEntryDetail]], id_to_path: dict[int, Path]) -> str:
    if len(details_by_id) < 2:
        return "not enough archive details for comparison"

    path_sets = {
        file_id: {entry.path.lower() for entry in entries}
        for file_id, entries in details_by_id.items()
    }
    basename_sets = {
        file_id: {Path(entry.path.lower()).name for entry in entries}
        for file_id, entries in details_by_id.items()
    }
    all_path_sets = list(path_sets.values())
    common_paths = set.intersection(*all_path_sets) if all_path_sets else set()
    lines = [
        f"common_exact_paths_count: {len(common_paths)}",
        "common_exact_paths_sample:",
        *[f"  - {path}" for path in sorted(common_paths)[:MAX_DUPLICATE_ARCHIVE_UNIQUE_PATHS]],
    ]

    for file_id, paths in path_sets.items():
        other_paths: set[str] = set()
        other_basenames: set[str] = set()

        for other_id, other_set in path_sets.items():
            if other_id != file_id:
                other_paths |= other_set
                other_basenames |= basename_sets[other_id]

        unique_paths = paths - other_paths
        common_basenames = basename_sets[file_id] & other_basenames
        lines.extend(
            [
                f"[{file_id}] {id_to_path[file_id].name}",
                f"  unique_exact_paths_count: {len(unique_paths)}",
                "  unique_exact_paths_sample:",
                *[f"    - {path}" for path in sorted(unique_paths)[:MAX_DUPLICATE_ARCHIVE_UNIQUE_PATHS]],
                f"  common_basenames_count: {len(common_basenames)}",
                "  common_basenames_sample:",
                *[f"    - {name}" for name in sorted(common_basenames)[:MAX_DUPLICATE_ARCHIVE_UNIQUE_PATHS]],
            ]
        )

    return "\n".join(lines)


def format_duplicate_archives_deep_review(archive_ids: list[int], id_to_path: dict[int, Path]) -> str:
    reports: list[str] = []
    details_by_id: dict[int, list[ArchiveEntryDetail]] = {}

    for file_id in archive_ids:
        report, entries = format_duplicate_archive_deep_report(file_id, id_to_path[file_id])
        reports.append(report)

        if entries:
            details_by_id[file_id] = entries

    comparison = format_archive_structure_comparison(details_by_id, id_to_path)
    return "\n\n".join(reports + ["Archive structure comparison:", comparison])


def inspect_archive(file_path: Path) -> ArchiveInspection:
    suffix = archive_suffix(file_path)

    if not suffix:
        return ArchiveInspection()

    info = ArchiveInspection(inspected=True, supported=True, archive_type=suffix.lstrip("."))

    try:
        if suffix == ".zip":
            entries, total_entries, truncated = read_zip_entries(file_path)
        elif suffix in {".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"}:
            entries, total_entries, truncated = read_tar_entries(file_path)
        elif suffix == ".7z":
            entries, total_entries, truncated = read_7z_entries(file_path)
        elif suffix == ".rar":
            entries, total_entries, truncated = read_rar_entries(file_path)
        else:
            entries, total_entries, truncated = [], None, False
    except Exception as e:
        info.supported = False
        info.error = str(e)
        return info

    lower_entries = [entry.lower() for entry in entries]
    basenames = [Path(entry).name.lower() for entry in lower_entries]

    info.entries_scanned = len(entries)
    info.total_entries = total_entries
    info.truncated = truncated
    info.preview_entries = entries[:MAX_ARCHIVE_PREVIEW_ENTRIES]
    info.has_init_py = "__init__.py" in basenames
    info.has_blend = any(name.endswith(".blend") for name in lower_entries)
    info.has_texture = any(Path(name).suffix.lower() in TEXTURE_SUFFIXES for name in lower_entries)
    info.has_uproject = any(name.endswith(".uproject") for name in lower_entries)
    info.has_uplugin = any(name.endswith(".uplugin") for name in lower_entries)
    info.classification, info.reason = classify_archive_entries(entries)

    return info


def print_archive_inspection(info: ArchiveInspection) -> None:
    if not info.inspected:
        return

    if not info.supported:
        print(f"  [archive] Не удалось быстро прочитать архив: {info.error}")
        return

    total = f" из {info.total_entries}" if info.total_entries is not None else ""
    suffix = " (лимит сканирования)" if info.truncated else ""

    print(f"  [archive] {info.archive_type}: просмотрено {info.entries_scanned}{total} файлов{suffix}")

    signals: list[str] = []

    if info.has_init_py:
        signals.append("__init__.py")
    if info.has_blend:
        signals.append(".blend")
    if info.has_texture:
        signals.append("textures")
    if info.has_uproject:
        signals.append(".uproject")
    if info.has_uplugin:
        signals.append(".uplugin")
    if info.classification == "houdini_asset":
        signals.append(".hda")
    if info.classification == "ue_content":
        signals.append(".uasset")
    if info.classification == "unity_package":
        signals.append(".unitypackage")
    if info.classification == "adobe_extension":
        signals.append("Adobe extension")

    if signals:
        print(f"  [archive] Признаки: {', '.join(signals)}")

    if info.classification:
        print(f"  [archive] Быстрый вывод: {info.classification} — {info.reason}")

    if info.preview_entries:
        print("  [archive] Первые файлы:")

        for entry in info.preview_entries:
            print(f"        - {entry}")


def get_files_in_root(root: Path) -> list[Path]:
    try:
        return sorted(
            [p for p in root.iterdir() if p.is_file() and not p.is_symlink()],
            key=lambda p: p.name.lower(),
        )
    except (PermissionError, OSError) as e:
        print(f"  [ОШИБКА] Не удалось прочитать root: {e}")
        return []


def resolve_destination(dest_folder: Path, filename: str) -> Path:
    dest = dest_folder / filename

    if not dest.exists():
        return dest

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1

    while True:
        new_name = f"{stem} ({counter}){suffix}"
        dest = dest_folder / new_name

        if not dest.exists():
            return dest

        counter += 1


# ─────────────────────────────────────────────
#  Поиск дублей и похожих версий в root
# ─────────────────────────────────────────────

VERSION_PATTERNS = (
    r"\bv?\d+(?:[._-]\d+){1,4}[a-z]?\b",
    r"\bv\d+\b",
    r"\bver(?:sion)?[._ -]*\d+\b",
    r"\brev(?:ision)?[._ -]*\d+\b",
    r"\bbuild[._ -]*\d+\b",
    r"\bcopy\b",
    r"\bcopie\b",
    r"\bкопия\b",
    r"\bduplicate\b",
    r"\bfinal\b",
    r"\bold\b",
    r"\bnew\b",
)

VERSION_EXTRACT_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:v|ver(?:sion)?[._ -]*)?(\d+(?:[._-]\d+){0,5}[a-z]?)(?=$|[^a-z0-9])"
)

COMPACT_VERSION_RE = re.compile(r"(?i)(?<=[a-z])(\d{3})(?:[a-z]*)$")
COMPACT_DROP_WORDS = (
    "enterprise",
    "professional",
    "portable",
    "repack",
    "crack",
    "release",
    "latest",
    "win",
    "windows",
    "x64",
    "x86",
)
DUPLICATE_GENERIC_NAME_TOKENS = {
    "procedural",
    "prcdrl",
    "addon",
    "addons",
    "asset",
    "assets",
    "pack",
    "preset",
    "presets",
    "plugin",
    "plugins",
    "texture",
    "textures",
    "material",
    "materials",
}


def remove_known_suffix(filename: str) -> str:
    lower_name = filename.lower()
    suffix = archive_suffix(Path(filename))

    if suffix:
        return filename[: -len(suffix)]

    return filename[: -len(Path(lower_name).suffix)] if Path(lower_name).suffix else filename


def normalize_duplicate_key(file_path: Path) -> str:
    name = remove_known_suffix(file_path.name).lower()
    name = re.sub(r"[\[\]{}()]+", " ", name)
    name = VERSION_EXTRACT_RE.sub(" ", name)

    for pattern in VERSION_PATTERNS:
        name = re.sub(pattern, " ", name, flags=re.IGNORECASE)

    name = re.sub(r"[_+\-.]+", " ", name)
    name = re.sub(r"\b(?:x64|x86|win64|win32|windows|macos|linux)\b", " ", name)
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        return remove_known_suffix(file_path.name).lower().strip()

    return name


def duplicate_extension_key(file_path: Path) -> str:
    if archive_suffix(file_path):
        return "<archive>"

    return file_path.suffix.lower()


def duplicate_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0

    ratio = SequenceMatcher(None, left, right).ratio()
    left_tokens = set(left.split())
    right_tokens = set(right.split())

    if not left_tokens or not right_tokens:
        return ratio

    shorter_tokens = left_tokens if len(left_tokens) <= len(right_tokens) else right_tokens
    longer_tokens = right_tokens if len(left_tokens) <= len(right_tokens) else left_tokens

    if len(shorter_tokens) >= 2 and shorter_tokens <= longer_tokens:
        return 0.92

    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return max(ratio, jaccard)


def compact_duplicate_name(file_path: Path) -> str:
    name = remove_known_suffix(file_path.name).lower()
    name = VERSION_EXTRACT_RE.sub(" ", name)
    for pattern in VERSION_PATTERNS:
        name = re.sub(pattern, " ", name, flags=re.IGNORECASE)

    compact = re.sub(r"[^a-z0-9]+", "", name)
    compact = COMPACT_VERSION_RE.sub("", compact)

    for token in COMPACT_DROP_WORDS:
        compact = compact.replace(token, "")

    return compact


def compact_duplicate_skeleton(compact: str) -> str:
    return re.sub(r"[aeiouy]+", "", compact)


def duplicate_files_look_related(left: Path, right: Path) -> bool:
    left_key = normalize_duplicate_key(left)
    right_key = normalize_duplicate_key(right)

    if duplicate_similarity(left_key, right_key) >= DUPLICATE_SIMILARITY_THRESHOLD:
        return True

    left_compact = compact_duplicate_name(left)
    right_compact = compact_duplicate_name(right)

    if not left_compact or not right_compact:
        return False

    shorter_compact = left_compact if len(left_compact) <= len(right_compact) else right_compact

    if (
        shorter_compact not in DUPLICATE_GENERIC_NAME_TOKENS
        and (left_compact in right_compact or right_compact in left_compact)
    ):
        return True

    if SequenceMatcher(None, left_compact, right_compact).ratio() >= 0.88:
        return True

    left_skeleton = compact_duplicate_skeleton(left_compact)
    right_skeleton = compact_duplicate_skeleton(right_compact)

    if len(left_skeleton) < 4 or len(right_skeleton) < 4:
        return False

    shorter_skeleton = left_skeleton if len(left_skeleton) <= len(right_skeleton) else right_skeleton

    if (
        shorter_skeleton not in DUPLICATE_GENERIC_NAME_TOKENS
        and (left_skeleton in right_skeleton or right_skeleton in left_skeleton)
    ):
        return True

    return SequenceMatcher(None, left_skeleton, right_skeleton).ratio() >= 0.86


def compact_version_tuple(name: str) -> tuple[int, ...] | None:
    compact = re.sub(r"[^a-z0-9]+", "", name.lower())
    match = COMPACT_VERSION_RE.search(compact)

    if not match:
        return None

    return tuple(int(digit) for digit in match.group(1))


def normalize_version_tuple(version: tuple[int, ...] | None) -> tuple[int, ...]:
    if not version:
        return ()

    normalized = tuple(version)

    while normalized and normalized[-1] == 0:
        normalized = normalized[:-1]

    return normalized


def duplicate_version_tuple(file_path: Path) -> tuple[int, ...] | None:
    name = remove_known_suffix(file_path.name).lower()
    matches = list(VERSION_EXTRACT_RE.finditer(name))

    best: tuple[int, ...] | None = None

    for match in matches:
        raw = match.group(1)
        parts = re.findall(r"\d+", raw)

        if not parts:
            continue

        version = tuple(int(part) for part in parts)

        if best is None or version > best:
            best = version

    if best is not None:
        return best

    return compact_version_tuple(name)


def duplicate_modified_day(file_path: Path) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(file_path.stat().st_mtime))
    except OSError:
        return "0000-00-00"


def duplicate_file_score(file_path: Path) -> tuple[tuple[int, ...], str, int]:
    version = normalize_version_tuple(duplicate_version_tuple(file_path))

    try:
        size = file_path.stat().st_size
    except OSError:
        size = 0

    return version, duplicate_modified_day(file_path), size


def format_version_tuple(version: tuple[int, ...] | None) -> str:
    if not version:
        return "-"

    return ".".join(str(part) for part in version)


def format_duplicate_version(file_path: Path) -> str:
    raw_version = duplicate_version_tuple(file_path)
    comparable_version = normalize_version_tuple(raw_version)

    if not raw_version:
        return "-"

    raw_text = format_version_tuple(raw_version)
    comparable_text = format_version_tuple(comparable_version)

    if raw_text == comparable_text:
        return raw_text

    return f"{raw_text} (= {comparable_text})"


def suggest_duplicate_keep(group: list[Path]) -> tuple[int, list[int], str]:
    scored = [(i, file_path, duplicate_file_score(file_path)) for i, file_path in enumerate(group, 1)]
    keep_index, keep_path, keep_score = max(scored, key=lambda item: item[2])
    delete_indexes = [i for i, _, _ in scored if i != keep_index]

    versions = [score[0] for _, _, score in scored if score[0]]

    if versions and len(set(versions)) > 1:
        reason = (
            f"оставить более новую версию {format_version_tuple(keep_score[0])}: "
            f"{keep_path.name}"
        )
    else:
        days = [score[1] for _, _, score in scored]

        if len(set(days)) > 1:
            reason = f"версии одинаковы/не найдены, оставить более свежий файл: {keep_path.name}"
        else:
            reason = f"дата модификации в один день, оставить файл большего размера: {keep_path.name}"

    return keep_index, delete_indexes, reason


def find_duplicate_groups(files: list[Path]) -> list[list[Path]]:
    buckets: dict[str, list[tuple[str, Path]]] = {}

    for file_path in files:
        key = duplicate_extension_key(file_path)
        buckets.setdefault(key, []).append((normalize_duplicate_key(file_path), file_path))

    groups: list[list[Path]] = []

    for items in buckets.values():
        pending = items[:]

        while pending:
            key, file_path = pending.pop(0)
            group = [(key, file_path)]
            changed = True

            while changed:
                changed = False
                rest: list[tuple[str, Path]] = []
                group_keys = [item_key for item_key, _ in group]

                for candidate_key, candidate_path in pending:
                    if any(
                        duplicate_similarity(candidate_key, group_key)
                        >= DUPLICATE_SIMILARITY_THRESHOLD
                        for group_key in group_keys
                    ) or any(
                        duplicate_files_look_related(candidate_path, group_path)
                        for _, group_path in group
                    ):
                        group.append((candidate_key, candidate_path))
                        changed = True
                    else:
                        rest.append((candidate_key, candidate_path))

                pending = rest

            if len(group) > 1:
                paths = sorted(
                    [path for _, path in group],
                    key=lambda path: (
                        normalize_duplicate_key(path),
                        -path.stat().st_mtime if path.exists() else 0,
                        path.name.lower(),
                    ),
                )
                groups.append(paths)

    groups.sort(key=lambda group: (len(group), group[0].name.lower()), reverse=True)
    return groups


def format_mtime(file_path: Path) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(file_path.stat().st_mtime))
    except OSError:
        return "unknown"


def print_duplicate_group(group: list[Path], index: int, total: int) -> None:
    common_key = normalize_duplicate_key(group[0])
    keep_index, delete_indexes, reason = suggest_duplicate_keep(group)

    print(f"\n{hr('═')}")
    print(f"  Дубли/версии: группа {index}/{total}")
    print(f"  Ключ         : {common_key}")
    print(f"{hr('═')}")

    for i, file_path in enumerate(group, 1):
        try:
            size = format_size(file_path.stat().st_size)
        except OSError:
            size = "unknown"

        print(
            f"  [{i:>2}] {file_path.name}\n"
            f"       size: {size} | modified: {format_mtime(file_path)} | "
            f"version: {format_duplicate_version(file_path)} | "
            f"key: {normalize_duplicate_key(file_path)}"
        )

    delete_arg = " ".join(str(index) for index in delete_indexes)
    print(f"\n  [suggest] d {delete_arg}")
    print(f"        └─ {reason}")
    print(f"        └─ Быстро применить: a")


def parse_duplicate_numbers(raw: str, max_index: int) -> list[int]:
    numbers: list[int] = []

    for token in re.split(r"[,\s]+", raw.strip()):
        if not token:
            continue

        if not token.isdigit():
            return []

        value = int(token)

        if value < 1 or value > max_index:
            return []

        if value not in numbers:
            numbers.append(value)

    return numbers


def duplicate_review_dir(root: Path) -> Path:
    return root / DUPLICATE_REVIEW_FOLDER


def move_duplicate_files(root: Path, files: list[Path]) -> int:
    review_dir = duplicate_review_dir(root)
    moved = 0

    try:
        review_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"  [ОШИБКА] Не удалось создать {review_dir}: {e}")
        return 0

    for file_path in files:
        if not file_path.exists():
            continue

        dest_path = resolve_destination(review_dir, file_path.name)

        try:
            shutil.move(str(file_path), str(dest_path))
            print(f"  [✓] Перемещено: {file_path.name} → {DUPLICATE_REVIEW_FOLDER}/{dest_path.name}")
            moved += 1
        except OSError as e:
            print(f"  [ОШИБКА] {file_path.name}: {e}")

    return moved


def delete_duplicate_files(files: list[Path]) -> int:
    deleted = 0

    for file_path in files:
        if not file_path.exists():
            continue

        try:
            file_path.unlink()
            print(f"  [✓] Удалено: {file_path.name}")
            deleted += 1
        except OSError as e:
            print(f"  [ОШИБКА] {file_path.name}: {e}")

    return deleted


def parse_json_object_response(raw: str) -> dict | None:
    if not raw:
        return None

    text = raw.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("```", "")
    text = re.sub(r",\s*([\]}])", r"\1", text)
    text = repair_json_like_text(text)

    decoder = json.JSONDecoder()
    idx = 0

    while idx < len(text):
        brace_idx = text.find("{", idx)

        if brace_idx == -1:
            break

        try:
            obj, end = decoder.raw_decode(text[brace_idx:])
        except json.JSONDecodeError:
            idx = brace_idx + 1
            continue

        if isinstance(obj, dict):
            return obj

        idx = brace_idx + max(end, 1)

    return None


def duplicate_file_record(file_id: int, file_path: Path) -> dict:
    try:
        size_bytes = file_path.stat().st_size
    except OSError:
        size_bytes = 0

    version = duplicate_version_tuple(file_path)
    comparable_version = normalize_version_tuple(version)

    return {
        "id": file_id,
        "name": file_path.name,
        "size": format_size(size_bytes),
        "size_bytes": size_bytes,
        "modified": format_mtime(file_path),
        "extension_group": duplicate_extension_key(file_path),
        "normalized_key": normalize_duplicate_key(file_path),
        "detected_version": format_version_tuple(version),
        "comparable_version": format_version_tuple(comparable_version),
    }


def build_llm_duplicate_prompt(files: list[Path]) -> tuple[str, dict[int, Path]]:
    id_to_path = {i: file_path for i, file_path in enumerate(files, 1)}
    records = [duplicate_file_record(i, file_path) for i, file_path in id_to_path.items()]
    lines = []

    for record in records:
        lines.append(
            (
                f"[{record['id']}] {record['name']} | size={record['size']} "
                f"({record['size_bytes']}) | modified={record['modified']} | "
                f"ext_group={record['extension_group']} | detected_version={record['detected_version']} | "
                f"comparable_version={record['comparable_version']} | "
                f"key={record['normalized_key']}"
            )
        )

    prompt = "\n".join(lines)
    return prompt, id_to_path


def duplicate_hint_tokens(file_path: Path) -> list[str]:
    spaced_name = re.sub(r"(?<=[a-zа-яё])(?=[A-ZА-ЯЁ])", " ", remove_known_suffix(file_path.name))
    key = normalize_duplicate_key(Path(spaced_name))
    compact = compact_duplicate_name(file_path)
    tokens = [
        token
        for token in re.findall(r"[a-zа-яё0-9]+", key.lower())
        if len(token) >= 4
    ]

    stop_words = {
        "setup",
        "installer",
        "install",
        "latest",
        "release",
        "repack",
        "portable",
        "professional",
        "enterprise",
        "windows",
        "win64",
        "win32",
        "crack",
        "only",
        "part",
        "procedural",
    }
    tokens = [token for token in tokens if token not in stop_words]

    if compact and len(compact) >= 5:
        tokens.append(compact)

    skeleton = compact_duplicate_skeleton(compact)

    if len(skeleton) >= 5:
        tokens.append(skeleton)

    tokens = [token for token in tokens if token not in stop_words]
    return list(dict.fromkeys(tokens))


def multipart_archive_part(file_path: Path) -> tuple[str, str] | None:
    name = file_path.name.lower()
    patterns = (
        r"(.+?)(?:[._ -])part[\s._-]*(\d+)(?:\.rar|\.zip|\.7z)$",
        r"(.+?\.7z)\.(\d{3})$",
        r"(.+?)(?:\.zip|\.rar|\.7z)\.(\d{3})$",
        r"(.+?)\.r(\d{2,3})$",
        r"(.+?)\.z(\d{2,3})$",
        r"(.+?)(?:[._ -])(\d{3})$",
    )
    match = None

    for pattern in patterns:
        match = re.search(pattern, name, flags=re.IGNORECASE)

        if match:
            break

    if not match:
        return None

    base = re.sub(r"[\W_]+", " ", match.group(1)).strip()
    number = str(int(match.group(2)))
    return base, number


def all_same_multipart_set(ids: list[int], id_to_path: dict[int, Path]) -> bool:
    parts = [multipart_archive_part(id_to_path[file_id]) for file_id in ids]

    if not parts or any(part is None for part in parts):
        return False

    bases = {part[0] for part in parts if part is not None}
    numbers = {part[1] for part in parts if part is not None}

    return len(bases) == 1 and len(numbers) == len(ids)


def group_contains_multipart_set_parts(ids: list[int], id_to_path: dict[int, Path]) -> bool:
    by_base: dict[str, set[str]] = {}

    for file_id in ids:
        if file_id not in id_to_path:
            continue

        part = multipart_archive_part(id_to_path[file_id])

        if not part:
            continue

        base, number = part
        by_base.setdefault(base, set()).add(number)

    return any(len(numbers) > 1 for numbers in by_base.values())


def build_llm_duplicate_hints(id_to_path: dict[int, Path]) -> str:
    hint_groups: list[tuple[str, list[int], str]] = []
    seen_signatures: set[tuple[int, ...]] = set()

    def add_hint(label: str, ids: list[int], source: str) -> None:
        clean_ids = sorted(dict.fromkeys(file_id for file_id in ids if file_id in id_to_path))

        if len(clean_ids) < 2:
            return

        if all_same_multipart_set(clean_ids, id_to_path) or group_contains_multipart_set_parts(clean_ids, id_to_path):
            return

        signature = tuple(clean_ids)

        if signature in seen_signatures:
            return

        seen_signatures.add(signature)
        hint_groups.append((label, clean_ids, source))

    path_to_id = {file_path: file_id for file_id, file_path in id_to_path.items()}

    for group in find_duplicate_groups(list(id_to_path.values())):
        add_hint(
            normalize_duplicate_key(group[0]) or "local-similarity",
            [path_to_id[path] for path in group if path in path_to_id],
            "local_similarity",
        )

    token_buckets: dict[str, list[int]] = {}

    for file_id, file_path in id_to_path.items():
        for token in duplicate_hint_tokens(file_path):
            token_buckets.setdefault(token, []).append(file_id)

    for token, ids in sorted(token_buckets.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(ids) < 2:
            continue

        add_hint(token, ids, "shared_name_token")

    for left_id, left_path in id_to_path.items():
        related_ids = [left_id]

        for right_id, right_path in id_to_path.items():
            if right_id <= left_id:
                continue

            if duplicate_files_look_related(left_path, right_path):
                related_ids.append(right_id)

        if len(related_ids) > 1:
            add_hint(compact_duplicate_name(left_path) or normalize_duplicate_key(left_path), related_ids, "fallback_similarity")

    lines: list[str] = []

    for index, (label, ids, source) in enumerate(hint_groups[:LLM_DUPLICATE_MAX_HINT_GROUPS], 1):
        members = ", ".join(f"[{file_id}] {id_to_path[file_id].name}" for file_id in ids)
        lines.append(f"{index}. {label} | source={source} | ids={ids} | files={members}")

    return "\n".join(lines) if lines else "none"


def parse_llm_rejected_duplicate_hints(data: dict) -> list[dict]:
    rejected: list[dict] = []

    for raw_item in data.get("rejected_hints", []):
        if not isinstance(raw_item, dict):
            continue

        ids: list[int] = []

        for value in raw_item.get("ids", []):
            try:
                file_id = int(value)
            except Exception:
                continue

            if file_id not in ids:
                ids.append(file_id)

        reason = str(raw_item.get("reason", "")).strip()

        if ids and reason:
            rejected.append({"ids": ids, "reason": reason})

    return rejected


def ask_llm_duplicate_groups(files: list[Path]) -> tuple[list[dict], dict[int, Path], list[dict]]:
    prompt, id_to_path = build_llm_duplicate_prompt(files)
    hints = build_llm_duplicate_hints(id_to_path)
    hint_count = 0 if hints == "none" else hints.count("\n") + 1
    print(f"  [dup-llm] Широких кандидатов для LLM: {hint_count}")

    system_prompt = f"""\
You are a cautious duplicate-file review assistant.
You receive files from one folder. Find possible duplicate files, older versions, renamed variants, or product-family version conflicts.

Return ONLY one JSON object:
{{
  "groups": [
    {{
      "title": "short group name",
      "confidence": 0.0,
      "keep": 1,
      "delete": [2, 3],
      "structure_override": false,
      "analysis": "detailed Russian comparison of archive structure, filenames, sizes and dates",
      "reason": "short Russian reason"
    }}
  ],
  "rejected_hints": [
    {{
      "ids": [1, 2],
      "reason": "short Russian reason why this broad hint is not a duplicate/version group"
    }}
  ]
}}

Rules:
1. Return review candidates, not automatic deletions. It is acceptable to include medium-confidence candidates for manual review.
2. Extract and compare versions yourself from the filename. Do not rely only on detected_version; it is a weak hint.
3. Version patterns can be diverse: v1.2.6, 1.2.6, 126 meaning 1.2.6 for compact product names, 2025.3.3, 20.0.5.17637, R25, beta/build/release suffixes.
4. Treat trailing zero version parts as equal: 2.2.6.0 equals 2.2.6. Use comparable_version for that comparison.
5. Use filename, size, modified date, extension group, normalized key, detected_version, comparable_version, spelling variants, and abbreviations.
6. Include all visible versions of the same product family in one group; do not ignore a newer version candidate.
7. Priority for keep/delete: newest comparable_version first; if versions are equal or absent, newer modified date; if modified date is the same day, larger size.
8. Never keep an older version only because its archive contents look larger, fuller, or more reliable. If the newer file is a different product, omit the group instead.
9. A typo, abbreviation, or compact spelling in the filename does not make a newer version worse by itself.
10. Compare file size only by the numeric size_bytes field. Do not claim a file is smaller/larger unless size_bytes proves it.
11. Multipart archives like part1/part2 are usually a set, not duplicates. Do not mark one part for deletion unless the same part is duplicated.
12. A plugin/addon/preset for an application is not a duplicate of the host application itself.
13. Broad candidate hints are only hints. You may split a hint into smaller groups.
14. Hints with source=local_similarity are strong review candidates. Return them as groups unless they are clearly different products.
15. For every broad candidate hint: either return a group, or add a rejected_hints item with a concrete reason.
16. If files look like the same product line but you are not fully sure, return a lower-confidence group instead of rejecting it.
17. Never suggest deleting all files in a group.
18. Return at most {LLM_DUPLICATE_MAX_GROUPS} groups.
19. "delete" contains file IDs that are candidates for manual deletion. No action will be automatic.
20. In reason, mention the version/name comparison you used.
"""

    raw = call_llm_raw(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Files:\n{prompt}\n\nBroad candidate hints:\n{hints}"},
        ]
    )

    if not raw:
        return [], id_to_path, []

    data = parse_json_object_response(raw)

    if not data:
        print("  [ОШИБКА] LLM не вернула JSON с groups.")
        print(f"  [DEBUG RAW]:\n{raw[:1000]}...\n")
        return [], id_to_path, []

    if not isinstance(data.get("groups"), list):
        data["groups"] = []

    groups: list[dict] = []
    used_delete_ids: set[int] = set()
    rejected_hints = parse_llm_rejected_duplicate_hints(data)

    for raw_group in data["groups"]:
        if not isinstance(raw_group, dict):
            continue

        try:
            keep_id = int(raw_group.get("keep"))
        except Exception:
            continue

        if keep_id not in id_to_path:
            continue

        delete_ids: list[int] = []

        for value in raw_group.get("delete", []):
            try:
                file_id = int(value)
            except Exception:
                continue

            if file_id == keep_id or file_id not in id_to_path or file_id in used_delete_ids:
                continue

            if file_id not in delete_ids:
                delete_ids.append(file_id)

        if not delete_ids:
            continue

        if group_contains_multipart_set_parts([keep_id, *delete_ids], id_to_path):
            continue

        used_delete_ids.update(delete_ids)

        try:
            confidence = max(0.0, min(1.0, float(raw_group.get("confidence", 0.0))))
        except Exception:
            confidence = 0.0

        groups.append(
            {
                "title": str(raw_group.get("title", "")).strip() or normalize_duplicate_key(id_to_path[keep_id]),
                "confidence": confidence,
                "keep": keep_id,
                "delete": delete_ids,
                "structure_override": bool(raw_group.get("structure_override", False)),
                "analysis": str(raw_group.get("analysis", "")).replace('"', "'").strip(),
                "reason": str(raw_group.get("reason", "")).replace('"', "'").strip(),
            }
        )

        if len(groups) >= LLM_DUPLICATE_MAX_GROUPS:
            break

    return groups, id_to_path, rejected_hints


def format_duplicate_archive_detail(file_id: int, file_path: Path) -> str:
    if not archive_suffix(file_path):
        return f"[{file_id}] {file_path.name}\narchive: no\n"

    info = inspect_archive(file_path)

    if not info.supported:
        return (
            f"[{file_id}] {file_path.name}\n"
            f"archive: yes\n"
            f"scan_error: {info.error}\n"
        )

    entries = "\n".join(f"  - {entry}" for entry in info.preview_entries)
    extension_counts: dict[str, int] = {}

    for entry in info.preview_entries:
        suffix = Path(entry).suffix.lower() or "<no_ext>"
        extension_counts[suffix] = extension_counts.get(suffix, 0) + 1

    ext_summary = ", ".join(
        f"{suffix}:{count}"
        for suffix, count in sorted(extension_counts.items(), key=lambda item: (-item[1], item[0]))
    )
    classification = info.classification or "unknown"
    reason = info.reason or "no strong deterministic signal"

    return (
        f"[{file_id}] {file_path.name}\n"
        f"archive: yes\n"
        f"type: {info.archive_type}\n"
        f"entries_scanned: {info.entries_scanned}\n"
        f"classification: {classification}\n"
        f"classification_reason: {reason}\n"
        f"sample_extension_counts: {ext_summary or 'none'}\n"
        f"sample_entries:\n{entries}\n"
    )


def validate_llm_duplicate_groups(data: dict, id_to_path: dict[int, Path]) -> list[dict]:
    if not isinstance(data.get("groups"), list):
        return []

    groups: list[dict] = []
    used_delete_ids: set[int] = set()

    for raw_group in data["groups"]:
        if not isinstance(raw_group, dict):
            continue

        try:
            keep_id = int(raw_group.get("keep"))
        except Exception:
            continue

        if keep_id not in id_to_path:
            continue

        delete_ids: list[int] = []

        for value in raw_group.get("delete", []):
            try:
                file_id = int(value)
            except Exception:
                continue

            if file_id == keep_id or file_id not in id_to_path or file_id in used_delete_ids:
                continue

            if file_id not in delete_ids:
                delete_ids.append(file_id)

        if not delete_ids:
            continue

        if group_contains_multipart_set_parts([keep_id, *delete_ids], id_to_path):
            continue

        used_delete_ids.update(delete_ids)

        try:
            confidence = max(0.0, min(1.0, float(raw_group.get("confidence", 0.0))))
        except Exception:
            confidence = 0.0

        groups.append(
            {
                "title": str(raw_group.get("title", "")).strip() or normalize_duplicate_key(id_to_path[keep_id]),
                "confidence": confidence,
                "keep": keep_id,
                "delete": delete_ids,
                "structure_override": bool(raw_group.get("structure_override", False)),
                "analysis": str(raw_group.get("analysis", "")).replace('"', "'").strip(),
                "reason": str(raw_group.get("reason", "")).replace('"', "'").strip(),
            }
        )

        if len(groups) >= LLM_DUPLICATE_MAX_GROUPS:
            break

    return groups


def expand_llm_duplicate_groups(groups: list[dict], id_to_path: dict[int, Path]) -> list[dict]:
    expanded: list[dict] = []
    consumed_delete_ids: set[int] = set()

    for group in groups:
        group_ids = [group["keep"], *group["delete"]]
        related_ids = set(group_ids)

        for file_id, file_path in id_to_path.items():
            if file_id in related_ids:
                continue

            if any(duplicate_files_look_related(file_path, id_to_path[group_id]) for group_id in group_ids):
                related_ids.add(file_id)

        if len(related_ids) < 2:
            continue

        if group_contains_multipart_set_parts(sorted(related_ids), id_to_path):
            continue

        candidates = sorted(related_ids)
        keep_id = max(candidates, key=lambda file_id: duplicate_file_score(id_to_path[file_id]))
        delete_ids = [
            file_id
            for file_id in candidates
            if file_id != keep_id and file_id not in consumed_delete_ids
        ]

        if not delete_ids:
            continue

        consumed_delete_ids.update(delete_ids)
        added_ids = sorted(set(candidates) - set(group_ids))
        added_text = f"; добавлены похожие ID {added_ids}" if added_ids else ""

        expanded.append(
            {
                **group,
                "llm_keep": group["keep"],
                "llm_delete": group["delete"],
                "keep": keep_id,
                "delete": delete_ids,
                "fallback_added_ids": added_ids,
                "reason": f"{group['reason']}{added_text}",
            }
        )

    return expanded


def group_file_ids(group: dict) -> set[int]:
    return {int(file_id) for file_id in [group["keep"], *group["delete"]]}


def prune_duplicate_outlier_ids(
    keep_id: int,
    delete_ids: list[int],
    id_to_path: dict[int, Path],
) -> tuple[list[int], list[int]]:
    keep_path = id_to_path[keep_id]
    kept_delete_ids: list[int] = []
    outlier_ids: list[int] = []

    for file_id in delete_ids:
        file_path = id_to_path[file_id]

        if duplicate_files_look_related(file_path, keep_path):
            kept_delete_ids.append(file_id)
        else:
            outlier_ids.append(file_id)

    return kept_delete_ids, outlier_ids


def enforce_llm_duplicate_priority(
    refined_groups: list[dict],
    source_groups: list[dict],
    id_to_path: dict[int, Path],
    include_source_candidates: bool = False,
) -> list[dict]:
    fixed: list[dict] = []
    used_delete_ids: set[int] = set()

    for group in refined_groups:
        candidate_ids = group_file_ids(group)

        if include_source_candidates:
            for source_group in source_groups:
                source_ids = group_file_ids(source_group)

                if candidate_ids & source_ids:
                    candidate_ids |= source_ids

        candidate_ids = {
            file_id
            for file_id in candidate_ids
            if file_id in id_to_path and id_to_path[file_id].exists()
        }

        if len(candidate_ids) < 2:
            continue

        if group_contains_multipart_set_parts(sorted(candidate_ids), id_to_path):
            continue

        structure_override = bool(group.get("structure_override", False))
        old_keep = group["keep"]
        keep_id = old_keep if structure_override and old_keep in candidate_ids else max(
            candidate_ids,
            key=lambda file_id: duplicate_file_score(id_to_path[file_id]),
        )
        delete_ids = [
            file_id
            for file_id in sorted(candidate_ids)
            if file_id != keep_id and file_id not in used_delete_ids
        ]
        original_delete_ids = delete_ids[:]
        delete_ids, outlier_ids = prune_duplicate_outlier_ids(keep_id, delete_ids, id_to_path)

        if not delete_ids and original_delete_ids:
            delete_ids = original_delete_ids
            outlier_ids = []

        used_delete_ids.update(delete_ids)
        reason = group["reason"]

        if structure_override:
            reason = (
                f"{reason}; выбор LLM оставлен по deep-анализу структуры архива "
                f"(structure_override=true)"
            )
        elif keep_id != old_keep:
            reason = (
                f"{reason}; исправлено правилом приоритета: оставить "
                f"{id_to_path[keep_id].name}, потому что у него выше версия/дата/размер по правилу выбора"
            )

        if outlier_ids:
            outlier_names = ", ".join(f"[{file_id}] {id_to_path[file_id].name}" for file_id in outlier_ids)
            reason = f"{reason}; исключены нерелевантные кандидаты: {outlier_names}"

        fixed.append(
            {
                **group,
                "keep": keep_id,
                "delete": delete_ids,
                "reason": reason,
            }
        )

    return fixed


def format_duplicate_rule_report(groups: list[dict], id_to_path: dict[int, Path]) -> str:
    reports: list[str] = []

    for index, group in enumerate(groups, 1):
        candidate_ids = sorted(group_file_ids(group))
        scored = [
            (file_id, id_to_path[file_id], duplicate_file_score(id_to_path[file_id]))
            for file_id in candidate_ids
            if file_id in id_to_path and id_to_path[file_id].exists()
        ]

        if len(scored) < 2:
            continue

        keep_id, keep_path, keep_score = max(scored, key=lambda item: item[2])
        versions = [score[0] for _, _, score in scored if score[0]]
        days = [score[1] for _, _, score in scored]

        if versions and len(set(versions)) > 1:
            criterion = "newest comparable_version"
        elif len(set(days)) > 1:
            criterion = "same/absent comparable_version, newest modified date"
        else:
            criterion = "same comparable_version and same modified day, largest size"

        delete_ids = [file_id for file_id, _, _ in scored if file_id != keep_id]
        rows = [
            (
                f"  [{file_id}] {path.name} | comparable_version={format_version_tuple(score[0])} | "
                f"modified_day={score[1]} | size_bytes={score[2]}"
            )
            for file_id, path, score in scored
        ]
        reports.append(
            "\n".join(
                [
                    f"Group {index}:",
                    f"strict_keep={keep_id} ({keep_path.name})",
                    f"strict_delete={delete_ids}",
                    f"strict_criterion={criterion}",
                    "candidates:",
                    *rows,
                ]
            )
        )

    return "\n\n".join(reports)


def refine_llm_duplicate_groups_with_archives(
    groups: list[dict],
    id_to_path: dict[int, Path],
) -> list[dict]:
    involved_ids: list[int] = []

    for group in groups:
        for file_id in [group["keep"], *group["delete"]]:
            if file_id not in involved_ids:
                involved_ids.append(file_id)

    archive_ids = [
        file_id
        for file_id in involved_ids
        if archive_suffix(id_to_path[file_id])
    ]
    has_fallback_added = any(group.get("fallback_added_ids") for group in groups)

    if not archive_ids and not has_fallback_added:
        return groups

    if archive_ids:
        print(f"  [LLM] Быстро читаю содержимое архивов кандидатов: {len(archive_ids)}")

    if has_fallback_added:
        print("  [LLM] Проверяю fallback-кандидатов по полному контексту группы...")

    original_groups = json.dumps(
        {
            "groups": groups,
        },
        ensure_ascii=False,
        indent=2,
    )
    archive_details = format_duplicate_archives_deep_review(archive_ids, id_to_path)
    candidate_details = "\n".join(
        (
            f"[{record['id']}] {record['name']} | size={record['size']} "
            f"({record['size_bytes']}) | modified={record['modified']} | "
            f"ext_group={record['extension_group']} | detected_version={record['detected_version']} | "
            f"comparable_version={record['comparable_version']} | "
            f"key={record['normalized_key']}"
        )
        for record in (
            duplicate_file_record(file_id, id_to_path[file_id])
            for file_id in involved_ids
        )
    )
    rule_report = format_duplicate_rule_report(groups, id_to_path)

    system_prompt = f"""\
You are a cautious duplicate-file review assistant.
You already proposed duplicate groups. Now review them using candidate file details and detailed archive structure reports when available.

Return ONLY one JSON object:
{{
  "groups": [
    {{
      "title": "short group name",
      "confidence": 0.0,
      "keep": 1,
      "delete": [2, 3],
      "structure_override": false,
      "analysis": "detailed Russian comparison of archive structure, filenames, sizes and dates",
      "reason": "short Russian reason"
    }}
  ]
}}

Rules:
1. Keep a group only if files are likely the same product/asset/tool or older/newer versions.
2. If archive contents clearly differ in product type, ecosystem, or main files, omit that group.
3. Use archive sample entries to compare internal structure and main file types.
4. fallback_added_ids are only weak local candidates; confirm or reject them yourself.
5. Extract and compare versions yourself from filenames. Version patterns can be diverse: v1.2.6, 126 meaning 1.2.6 for compact product names, 2025.3.3, 20.0.5.17637, R25, beta/build/release suffixes.
6. Treat trailing zero version parts as equal: 2.2.6.0 equals 2.2.6. Use comparable_version for that comparison.
7. Include all visible versions of the same product family in one group; do not ignore a newer version candidate.
8. Baseline priority for keep/delete: newest comparable_version first; if versions are equal or absent, newer modified date; if modified date is the same day, larger size.
9. Deep archive structure can override the baseline when it proves the baseline winner only has redundant junk, duplicate copies, download-site files, cache/backup files, or renamed duplicate content.
10. A typo, abbreviation, or compact spelling in the filename does not make a newer version worse by itself.
11. Archive structure reports are evidence from archive catalogs; do not call a file "full" or "only installer" unless the listed entries clearly prove it.
12. Compare file size only by the numeric size_bytes field. Do not claim a file is smaller/larger unless size_bytes proves it.
13. Multipart archives like part1/part2 are usually a set, not duplicates. Do not mark one part for deletion unless the same part is duplicated.
14. A plugin/addon/preset for an application is not a duplicate of the host application itself.
15. If a report contains nested_archives, treat their nested contents as part of that candidate. Do not call an outer archive empty/broken just because the useful files are inside a nested archive.
16. Treat the Strict local rule report as a baseline recommendation, not a command. You may override strict_keep if detailed archive structure proves another file is cleaner or equivalent.
17. If one archive is a superset of another, decide whether the extra files are valuable main content or removable noise. Common noise examples: duplicate filenames with suffixes like copy/backup/old/blend1, CGDownload/html/url/readme ads, metadata, cache, temp files, thumbnails.
18. If core content is identical and the only meaningful difference is redundant/noise files, prefer the cleaner archive even if it is smaller or older.
19. If extra files are real assets, source files, presets, textures, examples, or newer main content, prefer the fuller archive.
20. If internal evidence is insufficient to tell whether extra files are noise or valuable content, keep the baseline winner but state uncertainty.
21. Set structure_override=true only when archive structure evidence is strong enough to override the baseline version/date/size winner.
22. Never suggest deleting all files in a group.
23. No action will be automatic; user will confirm manually.
24. Return at most {LLM_DUPLICATE_MAX_GROUPS} groups.
25. Fill "analysis" with a careful Russian comparison: external filenames, versions, archive file counts, internal key files, nested archive contents, internal sizes/dates, common paths, unique paths, whether extras look useful or redundant/noise, and uncertainty.
26. In reason, give the short final recommendation.
"""

    raw = call_llm_raw(
        [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Original candidate groups:\n{original_groups}\n\n"
                    f"Candidate file details:\n{candidate_details}\n\n"
                    f"Baseline local rule report:\n{rule_report}\n\n"
                    f"Detailed archive structure reports:\n{archive_details}"
                ),
            },
        ]
    )

    if not raw:
        return groups

    data = parse_json_object_response(raw)

    if not data:
        print("  [LLM] Не удалось уточнить группы по содержимому архивов. Использую первичный список.")
        return groups

    refined = validate_llm_duplicate_groups(data, id_to_path)

    if not refined:
        print("  [LLM] После проверки архивов подходящих дублей не осталось.")
        return []

    return enforce_llm_duplicate_priority(refined, groups, id_to_path)


def print_llm_duplicate_group(group: dict, id_to_path: dict[int, Path], index: int, total: int) -> None:
    keep_path = id_to_path[group["keep"]]

    print(f"\n{hr('═')}")
    print(f"  LLM дубли: группа {index}/{total}")
    print(f"  Название : {group['title']}")
    print(f"  Уверенность: {group['confidence']:.0%}")
    print(f"{hr('═')}")
    print(f"  Оставить: [{group['keep']}] {keep_path.name}")
    print(
        f"       size: {format_size(keep_path.stat().st_size)} | "
        f"modified: {format_mtime(keep_path)} | "
        f"version: {format_duplicate_version(keep_path)}"
    )
    print("  К удалению:")

    for file_id in group["delete"]:
        file_path = id_to_path[file_id]
        print(
            f"    [{file_id}] {file_path.name}\n"
            f"         size: {format_size(file_path.stat().st_size)} | "
            f"modified: {format_mtime(file_path)} | "
            f"version: {format_duplicate_version(file_path)}"
        )

    analysis = str(group.get("analysis", "")).strip()

    if analysis:
        print("\n  Анализ LLM:")

        for line in analysis.splitlines():
            print(f"    {line}")

    print(f"  Причина: {group['reason']}")


def print_rejected_duplicate_hints(rejected_hints: list[dict], id_to_path: dict[int, Path]) -> None:
    if not rejected_hints:
        return

    print("\n  [dup-llm] Отклоненные широкие кандидаты:")

    for index, item in enumerate(rejected_hints, 1):
        ids = [file_id for file_id in item["ids"] if file_id in id_to_path]

        if not ids:
            continue

        names = ", ".join(f"[{file_id}] {id_to_path[file_id].name}" for file_id in ids)
        print(f"    {index}. {names}")
        print(f"       └─ {item['reason']}")


def print_llm_keep_selection(group: dict, id_to_path: dict[int, Path]) -> None:
    keep_path = id_to_path[group["keep"]]
    delete_files = [id_to_path[file_id] for file_id in group["delete"] if id_to_path[file_id].exists()]

    print(f"  [dup] Теперь оставить: [{group['keep']}] {keep_path.name}")
    print("  [dup] Остальные по текущему выбору:")

    for file_path in delete_files:
        print(f"    - {file_path.name}")


def apply_llm_duplicate_group(group: dict, id_to_path: dict[int, Path], root: Path) -> tuple[str, int]:
    candidate_ids = sorted(group_file_ids(group))
    current_group = {
        **group,
        "delete": [
            file_id
            for file_id in group["delete"]
            if file_id in id_to_path and id_to_path[file_id].exists()
        ],
    }

    def set_manual_keep(file_id: int) -> bool:
        if file_id not in candidate_ids or file_id not in id_to_path or not id_to_path[file_id].exists():
            print(f"  [!] ID {file_id} нет в этой группе или файл уже недоступен.")
            return False

        current_group["keep"] = file_id
        current_group["delete"] = [
            candidate_id
            for candidate_id in candidate_ids
            if candidate_id != file_id
            and candidate_id in id_to_path
            and id_to_path[candidate_id].exists()
        ]
        current_group["reason"] = f"выбор пользователя: оставить {id_to_path[file_id].name}"
        print_llm_keep_selection(current_group, id_to_path)
        return True

    print("  Команды: d=удалить предложенные, m=переместить предложенные, k ID=выбрать что оставить, km ID=выбрать и переместить остальные, s=пропустить, q=выйти")

    while True:
        delete_files = [
            id_to_path[file_id]
            for file_id in current_group["delete"]
            if file_id in id_to_path and id_to_path[file_id].exists()
        ]
        choice = read_input("\n  duplicates_llm [d/m/k ID/km ID/s/q]> ").strip().lower()

        if choice == "q":
            return "quit", 0

        if choice == "s":
            print("  [dup] Группа пропущена.")
            return "done", 0

        if choice.startswith("k ") or choice.startswith("keep "):
            parts = choice.split(maxsplit=1)

            if len(parts) != 2 or not parts[1].isdigit():
                print("  [!] Формат: k ID")
                continue

            set_manual_keep(int(parts[1]))
            continue

        if choice.startswith("km "):
            parts = choice.split(maxsplit=1)

            if len(parts) != 2 or not parts[1].isdigit():
                print("  [!] Формат: km ID")
                continue

            if not set_manual_keep(int(parts[1])):
                continue

            delete_files = [
                id_to_path[file_id]
                for file_id in current_group["delete"]
                if file_id in id_to_path and id_to_path[file_id].exists()
            ]

            if ask_yes_no(
                f"  Переместить {len(delete_files)} файл(ов) в {DUPLICATE_REVIEW_FOLDER}/? [y/n]: "
            ):
                moved = move_duplicate_files(root, delete_files)
                return ("done", 1) if moved else ("done", 0)

            continue

        if choice == "m":
            if ask_yes_no(
                f"  Переместить {len(delete_files)} файл(ов) в {DUPLICATE_REVIEW_FOLDER}/? [y/n]: "
            ):
                moved = move_duplicate_files(root, delete_files)
                return ("done", 1) if moved else ("done", 0)

            continue

        if choice == "d":
            print("  Будут удалены:")

            for file_path in delete_files:
                print(f"    - {file_path.name}")

            if ask_yes_no("  Удалить предложенные LLM файлы безвозвратно? [y/n]: "):
                deleted = delete_duplicate_files(delete_files)
                return ("done", 1) if deleted else ("done", 0)

            continue

        print("  [!] Введите d, m, k ID, km ID, s или q.")


def apply_duplicate_command(
    choice: str,
    group: list[Path],
    root: Path,
) -> tuple[str, int]:
    choice = choice.strip().lower()

    if not choice:
        return "continue", 0

    if choice == "q":
        return "quit", 0

    if choice == "s":
        print("  [dup] Группа пропущена.")
        return "done", 0

    if choice == "a":
        _, delete_indexes, _ = suggest_duplicate_keep(group)
        choice = "d " + " ".join(str(index) for index in delete_indexes)

    parts = choice.split(maxsplit=1)
    command = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    if command == "k":
        numbers = parse_duplicate_numbers(args, len(group))

        if len(numbers) != 1:
            print("  [!] Формат: k N")
            return "continue", 0

        keep_index = numbers[0]
        selected = [
            file_path
            for i, file_path in enumerate(group, 1)
            if i != keep_index
        ]

        if not selected:
            print("  [!] Нечего перемещать.")
            return "continue", 0

        print(f"  Оставляем: {group[keep_index - 1].name}")

        if ask_yes_no(
            f"  Переместить остальные {len(selected)} файл(ов) в {DUPLICATE_REVIEW_FOLDER}/? [y/n]: "
        ):
            moved = move_duplicate_files(root, selected)
            return ("done", 1) if moved else ("continue", 0)

        return "continue", 0

    if command == "m":
        numbers = parse_duplicate_numbers(args, len(group))

        if not numbers:
            print("  [!] Формат: m N ...")
            return "continue", 0

        selected = [group[number - 1] for number in numbers]

        if ask_yes_no(
            f"  Переместить выбранные {len(selected)} файл(ов) в {DUPLICATE_REVIEW_FOLDER}/? [y/n]: "
        ):
            moved = move_duplicate_files(root, selected)
            return ("done", 1) if moved else ("continue", 0)

        return "continue", 0

    if command == "d":
        numbers = parse_duplicate_numbers(args, len(group))

        if not numbers:
            print("  [!] Формат: d N ...")
            return "continue", 0

        selected = [group[number - 1] for number in numbers]
        print("  Будут удалены:")

        for file_path in selected:
            print(f"    - {file_path.name}")

        if ask_yes_no("  Удалить выбранные файлы безвозвратно? [y/n]: "):
            deleted = delete_duplicate_files(selected)
            return ("done", 1) if deleted else ("continue", 0)

        return "continue", 0

    print("  [!] Неизвестная команда. Используйте k/m/d/s/q.")
    return "continue", 0


# ─────────────────────────────────────────────
#  Ввод
# ─────────────────────────────────────────────

_stdin_queue: queue.Queue[str | None] | None = None
_stdin_reader_started = False
_stdin_reader_lock = threading.Lock()


def ensure_stdin_reader() -> queue.Queue[str | None]:
    global _stdin_queue, _stdin_reader_started

    if _stdin_queue is None:
        _stdin_queue = queue.Queue()

    if os.name != "nt":
        return _stdin_queue

    with _stdin_reader_lock:
        if _stdin_reader_started:
            return _stdin_queue

        def reader() -> None:
            while True:
                try:
                    line = sys.stdin.readline()
                except UnicodeDecodeError:
                    try:
                        raw_line = sys.stdin.buffer.readline()
                    except Exception:
                        _stdin_queue.put(None)
                        break

                    if raw_line == b"":
                        _stdin_queue.put(None)
                        break

                    line = raw_line.decode("utf-8", errors="replace")
                except Exception:
                    _stdin_queue.put(None)
                    break

                if line == "":
                    _stdin_queue.put(None)
                    break

                _stdin_queue.put(line.rstrip("\r\n"))

        thread = threading.Thread(target=reader, name="stdin-reader", daemon=True)
        thread.start()
        _stdin_reader_started = True

    return _stdin_queue


def read_input(prompt: str = "") -> str:
    if os.name != "nt":
        return input(prompt)

    stdin_queue = ensure_stdin_reader()
    print(prompt, end="", flush=True)
    line = stdin_queue.get()

    if line is None:
        try:
            return input()
        except UnicodeDecodeError:
            raw_line = sys.stdin.buffer.readline()

            if raw_line == b"":
                raise EOFError

            return raw_line.decode("utf-8", errors="replace").rstrip("\r\n")

    return line


def read_input_timeout(prompt: str, timeout_seconds: float) -> str | None:
    if os.name != "nt":
        import select

        print(prompt, end="", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)

        if ready:
            return sys.stdin.readline().strip()

        return None

    stdin_queue = ensure_stdin_reader()
    print(prompt, end="", flush=True)

    try:
        line = stdin_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        return None

    if line is None:
        raise EOFError

    return line


def timed_choice(
    prompt: str,
    timeout_seconds: int,
    default: str,
    valid_choices: set[str],
) -> tuple[str, bool]:
    """
    Меню с таймаутом.

    Поведение:
    - 1/2/3/4/5/0/s/q вводятся через Enter.
    - -1 поддерживается для удаления.
    - Enter отменяет таймер и переводит выбор в ручной режим.
    - Если ничего не нажато до таймаута, возвращается default.

    Возвращает:
      (choice, timed_out)

    Спец-значение:
      "__manual__" = пользователь нажал Enter, нужно спросить обычным input без таймера.
    """

    if timeout_seconds <= 0:
        choice = read_input(prompt).strip().lower()
        return choice, False

    choice = read_input_timeout(prompt, timeout_seconds)

    if choice is None:
        print()
        return default, True

    choice = choice.strip().lower()

    if choice == "":
        return "__manual__", False

    return choice, False


# ─────────────────────────────────────────────
#  LLM интеграция
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a precise file organization assistant. Your only job is to suggest which existing folder a given file should be moved to.

Return ONLY one valid JSON object. No markdown. No comments. No explanations outside JSON.

STRICT JSON schema:
{
  "recommendations": [
    {
      "folder": "<relative/path>",
      "exists": true,
      "confidence": 0.0,
      "reason": "<short reason in Russian>"
    }
  ]
}

GENERAL RULES:
1. List up to 5 folders. Return 5 folders whenever at least 5 plausible existing folders are available.
2. Sort by confidence descending.
3. "folder" must be a relative path from the destination root.
4. Do NOT start folder with slash.
5. Do NOT end folder with slash.
6. Do NOT use backslashes.
7. Do NOT use "..".
8. Prefer existing folders from the provided tree.
9. Suggest a new folder only if no existing folder is suitable.
10. "reason" must be short Russian text.
11. Do not use double quotes inside the reason string.

SELECTION LOGIC:
12. Prefer the most specific semantically matching existing folder over generic folders.
13. Prefer domain-specific folders over generic folders.
14. Generic folders such as unsorted, misc, other, unknown, temp, downloads, general, files are only last resort.
15. If choosing a generic/unsorted folder, confidence must be <= 0.35 unless absolutely no other folder is relevant.
16. Do not invent a new folder when a reasonably matching existing folder is visible.
17. Do not infer Unreal Engine only because a file is related to games, VFX, 3D, assets, effects, fire, smoke, rain, atmosphere, or simulation.
18. Classify as Unreal Engine only if the filename or folder context clearly contains UE, UE4, UE5, Unreal, Engine plugin markers, uplugin, project, template, marketplace, or an explicit Unreal version marker.
19. If filename contains UE, UE4, UE5, Unreal, Unity, Blender, Maya, Houdini, classify by that ecosystem first.
20. If filename looks like standalone software, application installer, tool, generator, simulator, editor, renderer, converter, utility, or versioned app archive, prefer Software/tool folders over engine asset folders.
21. If filename suggests plugin/tool/editor extension for a specific ecosystem, prefer plugin/tool folders.
22. If filename suggests content assets, scenes, environments, maps, landscapes, interiors, vegetation, weather, sky, clouds, rain, snow, fog, atmosphere, water, fire, smoke, VFX, FX, particles, prefer content/environment/VFX folders over plugin folders.
23. If filename suggests characters, creatures, animals, humans, monsters, prefer character/creature folders.
24. If filename suggests animations, mocap, rigs, poses, locomotion, prefer animation folders.
25. If filename suggests templates, starter projects, samples, demo projects, prefer template/project folders.
26. If filename suggests materials, shaders, textures, surfaces, prefer materials/textures folders.
27. If confidence between a specific folder and generic folder is close, choose the specific folder.

EVIDENCE PRIORITY:
28. Read the filename first. The filename often contains product name, ecosystem, content type, and version.
29. Use Archive quick scan second, as confirmation or clarification when filename is ambiguous.
30. Hard manifest files inside archives are strong evidence: .uproject, .uplugin, .unitypackage, .hda, __init__.py for Blender add-ons.
31. Dominant internal file extensions are medium evidence: .uasset, .blend, textures, .jsx/.zxp/.ccx.
32. Do not let generic internal folders such as assets, content, source, textures override an explicit ecosystem or product type in the filename.
33. If filename and archive contents conflict, prefer hard manifest files; otherwise prefer filename and mention the conflict in reason.

IMPORTANT:
34. Known standalone VFX/DCC tools and generators must not be classified as UE assets unless the filename explicitly contains UE/Unreal markers.
35. EmberGen, EmbrGen, LiquiGen, GeoGen, Houdini, Blender, Maya, ZBrush, Substance, Marvelous Designer, World Creator, Gaea, SpeedTree are standalone software/tools unless filename explicitly says they are UE/Unreal assets or plugins.
36. If Archive quick scan says __init__.py was found and filename does not clearly indicate assets/scenes, strongly prefer Blender add-on folders.
37. If Archive quick scan says .blend and textures were found and no add-on marker is present, strongly prefer Blender asset/scene/props folders.
38. If Archive quick scan says .uproject or .uplugin was found, strongly prefer Unreal Engine folders.
39. If Archive quick scan says .hda was found, strongly prefer Houdini folders.
"""

GENERIC_FOLDER_TOKENS = {
    "unsorted",
    "misc",
    "other",
    "unknown",
    "temp",
    "tmp",
    "general",
    "downloads",
    "files",
}


def is_generic_folder(folder: str) -> bool:
    normalized = normalize_rel_folder(folder)
    if not normalized:
        return True

    parts = re.split(r"[/_\-\s]+", normalized.lower())
    return any(part in GENERIC_FOLDER_TOKENS for part in parts)


def apply_generic_folder_penalty(recs: list[dict]) -> list[dict]:
    """
    Штрафует generic/unsorted папки.
    Они должны побеждать только если других нормальных вариантов нет.
    """
    if not recs:
        return recs

    has_specific = any(not is_generic_folder(r["folder"]) for r in recs)

    for r in recs:
        if is_generic_folder(r["folder"]):
            if has_specific:
                r["confidence"] = min(float(r["confidence"]), 0.34)
            else:
                r["confidence"] = min(float(r["confidence"]), 0.45)

    recs.sort(key=lambda x: x["confidence"], reverse=True)
    return recs


def prepare_recommendations_for_choice(recs: list[dict]) -> tuple[list[dict], int | None]:
    filtered = [
        r
        for r in recs
        if float(r.get("confidence", 0.0)) >= MIN_RECOMMENDATION_CONFIDENCE
    ]

    if not filtered and recs:
        filtered = [recs[0]]

    filtered.sort(key=lambda x: x["confidence"], reverse=True)

    if len(filtered) >= 2:
        lead_gap = float(filtered[0]["confidence"]) - float(filtered[1]["confidence"])

        if lead_gap > STRONG_LEAD_CONFIDENCE_GAP:
            return [filtered[0]], STRONG_LEAD_AUTO_SELECT_SECONDS

    return filtered[:MAX_RECOMMENDATIONS], None


# ─────────────────────────────────────────────
#  Deterministic override: standalone VFX/DCC tools
# ─────────────────────────────────────────────

STANDALONE_TOOL_KEYWORDS = {
    "embergen",
    "embrgen",
    "liquigen",
    "geogen",
    "houdini",
    "blender",
    "maya",
    "zbrush",
    "substance",
    "designer",
    "painter",
    "marvelous",
    "gaea",
    "worldcreator",
    "world creator",
    "speedtree",
}


ENGINE_MARKERS = {
    "ue",
    "ue4",
    "ue5",
    "unreal",
    "unity",
    "blender",
}


def normalize_filename_tokens(filename: str) -> list[str]:
    name = filename.lower()
    name = re.sub(r"[_\-.+()\[\]]+", " ", name)
    return [p for p in name.split() if p]


def filename_contains_engine_marker(filename: str) -> bool:
    tokens = normalize_filename_tokens(filename)
    joined = " ".join(tokens)

    # UE54, UE55, UE56, UE57, UE5.6, UE_5_6 и похожие варианты
    if re.search(r"\bue\s*5?\s*\d+", joined):
        return True

    if re.search(r"\bue[45]?\d*\b", joined):
        return True

    if "unreal" in tokens:
        return True

    if "unity" in tokens:
        return True

    return False


def filename_contains_standalone_tool(filename: str) -> bool:
    name = filename.lower().replace("_", " ").replace("-", " ")

    compact = re.sub(r"[^a-z0-9]+", "", name)

    for keyword in STANDALONE_TOOL_KEYWORDS:
        compact_keyword = re.sub(r"[^a-z0-9]+", "", keyword.lower())

        if compact_keyword and compact_keyword in compact:
            return True

    return False


def find_best_existing_folder_by_keywords(
    flat_paths: list[str],
    preferred_tokens: list[str],
    forbidden_tokens: list[str] | None = None,
) -> str | None:
    forbidden_tokens = forbidden_tokens or []

    best_path: str | None = None
    best_score = -1

    for path in flat_paths:
        path_low = path.lower()

        if any(token.lower() in path_low for token in forbidden_tokens):
            continue

        score = 0

        for token in preferred_tokens:
            if token.lower() in path_low:
                score += 10

        # Чем короче путь, тем лучше для общей категории Software.
        score -= path_low.count("/")

        if score > best_score:
            best_score = score
            best_path = path

    if best_score <= 0:
        return None

    return best_path


def apply_standalone_tool_override(
    filename: str,
    recs: list[dict],
    flat_paths: list[str],
) -> list[dict]:
    """
    Если файл похож на standalone software/tool, а явного UE/Unreal маркера нет,
    не позволяем LLM отправить его в UE_Assets.
    """
    if not filename_contains_standalone_tool(filename):
        return recs

    if filename_contains_engine_marker(filename):
        return recs

    software_folder = find_best_existing_folder_by_keywords(
        flat_paths=flat_paths,
        preferred_tokens=["software", "tools", "apps", "programs", "program", "софт"],
        forbidden_tokens=["plugins", "plugin"],
    )

    if not software_folder:
        software_folder = find_best_existing_folder_by_keywords(
            flat_paths=flat_paths,
            preferred_tokens=["software"],
        )

    if not software_folder:
        return recs

    # Убираем явно плохие UE asset guesses.
    filtered: list[dict] = []

    for r in recs:
        folder_low = r["folder"].lower()

        is_bad_engine_asset_guess = (
            "ue/" in folder_low or folder_low.startswith("ue")
        ) and (
            "asset" in folder_low
            or "environment" in folder_low
            or "template" in folder_low
        )

        if is_bad_engine_asset_guess:
            continue

        filtered.append(r)

    override = {
        "folder": software_folder,
        "exists": True,
        "confidence": 0.95,
        "reason": "Похоже на standalone VFX/DCC инструмент, а не на UE ассет",
    }

    result = [override]

    for r in filtered:
        if r["folder"] != software_folder:
            result.append(r)

    result.sort(key=lambda x: x["confidence"], reverse=True)

    return result[:MAX_RECOMMENDATIONS]


def apply_archive_override(
    archive_info: ArchiveInspection | None,
    recs: list[dict],
    flat_paths: list[str],
) -> list[dict]:
    if not archive_info or not archive_info.classification:
        return recs

    classification = archive_info.classification

    if classification == "blender_addon":
        folder = find_best_existing_folder_by_keywords(
            flat_paths=flat_paths,
            preferred_tokens=["blender", "addon", "addons"],
            forbidden_tokens=["ue", "unreal", "unity"],
        )
        reason = "В архиве найден __init__.py, это признак Blender add-on"
    elif classification == "blender_assets":
        folder = find_best_existing_folder_by_keywords(
            flat_paths=flat_paths,
            preferred_tokens=["blender", "asset", "assets", "scene", "scenes", "props", "texture"],
            forbidden_tokens=["ue", "unreal", "unity"],
        )
        reason = f"Содержимое архива: {archive_info.reason}"
    elif classification == "ue_project":
        folder = find_best_existing_folder_by_keywords(
            flat_paths=flat_paths,
            preferred_tokens=["ue", "unreal", "project", "projects", "assets"],
            forbidden_tokens=["blender", "unity"],
        )
        reason = "В архиве найден .uproject, это Unreal Engine проект"
    elif classification == "ue_plugin":
        folder = find_best_existing_folder_by_keywords(
            flat_paths=flat_paths,
            preferred_tokens=["ue", "unreal", "plugin", "plugins"],
            forbidden_tokens=["blender", "unity"],
        )
        reason = "В архиве найден .uplugin, это Unreal Engine plugin"
    elif classification == "ue_content":
        folder = find_best_existing_folder_by_keywords(
            flat_paths=flat_paths,
            preferred_tokens=["ue", "unreal", "assets", "asset", "content"],
            forbidden_tokens=["blender", "unity"],
        )
        reason = "В архиве найдены .uasset, это Unreal Engine content"
    elif classification == "houdini_asset":
        folder = find_best_existing_folder_by_keywords(
            flat_paths=flat_paths,
            preferred_tokens=["houdini", "hda", "asset", "assets", "tools"],
            forbidden_tokens=["blender", "ue", "unreal", "unity"],
        )
        reason = "В архиве найдены Houdini Digital Assets (.hda)"
    elif classification == "unity_package":
        folder = find_best_existing_folder_by_keywords(
            flat_paths=flat_paths,
            preferred_tokens=["unity", "assets", "asset", "packages"],
            forbidden_tokens=["blender", "ue", "unreal"],
        )
        reason = "В архиве найден .unitypackage"
    elif classification == "adobe_extension":
        folder = find_best_existing_folder_by_keywords(
            flat_paths=flat_paths,
            preferred_tokens=["adobe", "photoshop", "software", "plugin", "plugins"],
            forbidden_tokens=["blender", "ue", "unreal", "unity"],
        )
        reason = "В архиве найдены Adobe/Photoshop extension файлы"
    else:
        return recs

    if not folder:
        return recs

    override = {
        "folder": folder,
        "exists": True,
        "confidence": 0.98,
        "reason": reason,
    }

    result = [override]

    for r in recs:
        if r["folder"] != folder:
            result.append(r)

    result.sort(key=lambda x: x["confidence"], reverse=True)
    return result[:MAX_RECOMMENDATIONS]


def format_archive_for_prompt(archive_info: ArchiveInspection | None) -> str:
    if not archive_info or not archive_info.inspected:
        return ""

    if not archive_info.supported:
        return f"\nArchive quick scan: failed: {archive_info.error}\n"

    signals: list[str] = []

    if archive_info.has_init_py:
        signals.append("__init__.py")
    if archive_info.has_blend:
        signals.append(".blend")
    if archive_info.has_texture:
        signals.append("textures")
    if archive_info.has_uproject:
        signals.append(".uproject")
    if archive_info.has_uplugin:
        signals.append(".uplugin")

    entries = "\n".join(f"- {entry}" for entry in archive_info.preview_entries)
    classification = archive_info.classification or "unknown"
    reason = archive_info.reason or "no strong deterministic signal"

    return (
        f"\nArchive quick scan:\n"
        f"  Type              : {archive_info.archive_type}\n"
        f"  Entries scanned   : {archive_info.entries_scanned}\n"
        f"  Signals           : {', '.join(signals) if signals else 'none'}\n"
        f"  Classification    : {classification}\n"
        f"  Classification why: {reason}\n"
        f"  Use policy        : use as evidence after filename; hard manifests can override filename\n"
        f"  Sample entries:\n{entries}\n"
    )


def build_user_message(
    filename: str,
    extension: str,
    size_bytes: int,
    tree_str: str,
    flat_paths: list[str],
    excluded: list[str] | None = None,
    archive_info: ArchiveInspection | None = None,
) -> str:
    excl_block = ""

    if excluded:
        excl_list = "\n".join(f"- {e}" for e in excluded)
        excl_block = (
            f"\nDO NOT suggest these previously rejected folders:\n{excl_list}\n"
        )

    folders_list = "\n".join(f"- {p}" for p in flat_paths)
    archive_block = format_archive_for_prompt(archive_info)

    return (
        f"File to sort:\n"
        f"  Name      : {filename}\n"
        f"  Extension : {extension if extension else 'none'}\n"
        f"  Size      : {format_size(size_bytes)}\n"
        f"{archive_block}"
        f"\nDestination folder tree:\n"
        f"```\n{tree_str}\n```\n"
        f"\nExisting folder paths:\n"
        f"```\n{folders_list}\n```\n"
        f"{excl_block}"
        f"\nTask:\n"
        f"Choose the best existing folder paths from the list above.\n"
        f"Return up to {MAX_RECOMMENDATIONS} recommendations, and prefer returning "
        f"{MAX_RECOMMENDATIONS} different options when possible.\n"
        f"Only suggest a new folder if none of the existing folder paths are suitable.\n"
    )


def call_llm_raw(messages: list[dict]) -> str | None:
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": False,
        # JSON Mode для совместимых llama.cpp / LM Studio серверов.
        # Если сервер не поддерживает response_format, ниже будет fallback.
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(LLAMACPP_URL, json=payload, timeout=REQUEST_TIMEOUT)

        # Fallback для серверов без response_format.
        if resp.status_code >= 400:
            payload.pop("response_format", None)
            resp = requests.post(LLAMACPP_URL, json=payload, timeout=REQUEST_TIMEOUT)

        resp.raise_for_status()

        data = resp.json()
        message = data["choices"][0]["message"]

        content = message.get("content", "")
        reasoning = message.get("reasoning_content", "")

        # Критично:
        # Не склеиваем reasoning_content и content.
        # Иначе JSON часто ломается из-за промежуточных рассуждений.
        if content and content.strip():
            return content.strip()

        # Fallback только если сервер почему-то положил финальный ответ в reasoning_content.
        if reasoning and reasoning.strip():
            return reasoning.strip()

        return None

    except Exception as e:
        print(f"  [ОШИБКА API LLM]: {e}")
        return None


def parse_llm_response(raw: str) -> dict | None:
    """
    Надежный парсер JSON:
    - убирает think-блоки;
    - убирает markdown fences;
    - не берет текст от первой { до последней };
    - ищет первый валидный JSON-объект с ключом recommendations.
    """
    if not raw:
        return None

    text = raw.strip()

    # Удаляем reasoning-блоки, если модель все равно вывела их в content.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(
        r"<\|channel>thought.*?<channel\|>", "", text, flags=re.DOTALL | re.IGNORECASE
    )

    # Удаляем markdown code fence.
    text = re.sub(r"```json\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("```", "")

    # Удаляем висячие запятые перед ] или }.
    text = re.sub(r",\s*([\]}])", r"\1", text)

    decoder = json.JSONDecoder()

    for candidate in (text, repair_json_like_text(text)):
        idx = 0

        while idx < len(candidate):
            brace_idx = candidate.find("{", idx)

            if brace_idx == -1:
                break

            try:
                obj, end = decoder.raw_decode(candidate[brace_idx:])
            except json.JSONDecodeError:
                idx = brace_idx + 1
                continue

            if isinstance(obj, dict) and isinstance(obj.get("recommendations"), list):
                return obj

            idx = brace_idx + max(end, 1)

    print(
        "  [ОШИБКА] В ответе LLM не найден валидный JSON-объект с ключом 'recommendations'."
    )
    print(f"  [DEBUG RAW]:\n{text[:1000]}...\n")

    return None


def repair_json_like_text(text: str) -> str:
    """
    Чинит частые мелкие ошибки локальных моделей в JSON:
    - confidence: 0.4 -> "confidence": 0.4
    - reason": "..." -> "reason": "..."
    """
    text = re.sub(
        r"(?m)([{,]\s*)(folder|exists|confidence|reason|recommendations)\s*:",
        r'\1"\2":',
        text,
    )
    text = re.sub(
        r"(?m)([{,]\s*)(folder|exists|confidence|reason|recommendations)\"\s*:",
        r'\1"\2":',
        text,
    )
    return text


def validate_and_sort_recommendations(data: dict, flat_paths: list[str]) -> list[dict]:
    recs = data.get("recommendations", [])

    if not isinstance(recs, list):
        return []

    flat_set = {p for p in (normalize_rel_folder(path) for path in flat_paths) if p}

    seen: set[str] = set()
    result: list[dict] = []

    for r in recs:
        if not isinstance(r, dict):
            continue

        folder = normalize_rel_folder(str(r.get("folder", "")))

        if not folder or folder in seen:
            continue

        seen.add(folder)

        try:
            confidence = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
        except Exception:
            confidence = 0.5

        reason = str(r.get("reason", "")).replace('"', "'").strip()

        result.append(
            {
                "folder": folder,
                "exists": folder in flat_set,
                "confidence": confidence,
                "reason": reason,
            }
        )

    result.sort(key=lambda x: x["confidence"], reverse=True)
    result = apply_generic_folder_penalty(result)

    return result[:MAX_RECOMMENDATIONS]


def ask_llm(
    file_path: Path,
    tree_str: str,
    flat_paths: list[str],
    excluded: list[str] | None = None,
    archive_info: ArchiveInspection | None = None,
) -> list[dict] | None:
    try:
        size = file_path.stat().st_size
    except OSError as e:
        print(f"  [ОШИБКА] Не удалось прочитать файл: {e}")
        return None

    def request_once(excluded_folders: list[str] | None) -> list[dict] | None:
        user_msg = build_user_message(
            filename=file_path.name,
            extension=file_path.suffix.lower(),
            size_bytes=size,
            tree_str=tree_str,
            flat_paths=flat_paths,
            excluded=excluded_folders,
            archive_info=archive_info,
        )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        raw = call_llm_raw(messages)

        if not raw:
            print("  [ОШИБКА] LLM вернула пустой ответ или запрос упал.")
            return None

        data = parse_llm_response(raw)

        if not data:
            return None

        recs = validate_and_sort_recommendations(data, flat_paths)

        if not recs:
            print(
                "  [ОШИБКА] LLM выдала JSON, но он пуст или не содержит валидных recommendations."
            )
            print(f"  [DEBUG JSON]: {data}")
            return None

        recs = apply_standalone_tool_override(
            filename=file_path.name,
            recs=recs,
            flat_paths=flat_paths,
        )

        recs = apply_archive_override(
            archive_info=archive_info,
            recs=recs,
            flat_paths=flat_paths,
        )

        if excluded_folders:
            excluded_set = {
                path
                for path in (normalize_rel_folder(folder) for folder in excluded_folders)
                if path
            }
            recs = [r for r in recs if r["folder"] not in excluded_set]

        recs = apply_generic_folder_penalty(recs)
        return recs

    recs = request_once(excluded)

    if not recs:
        print("  [ОШИБКА] LLM не вернула новых вариантов после исключения старых.")
        return None

    if len(recs) < MAX_RECOMMENDATIONS and len(flat_paths) > len(recs):
        refill_excluded = list(excluded or [])

        for r in recs:
            if r["folder"] not in refill_excluded:
                refill_excluded.append(r["folder"])

        print("  [LLM] Добираю варианты до 5...", flush=True)
        extra_recs = request_once(refill_excluded)

        if extra_recs:
            seen = {r["folder"] for r in recs}

            for r in extra_recs:
                if r["folder"] in seen:
                    continue

                recs.append(r)
                seen.add(r["folder"])

                if len(recs) >= MAX_RECOMMENDATIONS:
                    break

            recs = apply_generic_folder_penalty(recs)

    return recs


# ─────────────────────────────────────────────
#  Операции с файлами
# ─────────────────────────────────────────────


def ask_yes_no(prompt: str) -> bool:
    while True:
        answer = read_input(prompt).strip().lower()

        if answer in ("y", "yes", "д", "да"):
            return True

        if answer in ("n", "no", "н", "нет"):
            return False

        print("  [!] Введите y/n.")


def move_file(
    file_path: Path,
    dest_base: Path,
    chosen_folder: str,
    exists: bool,
    *,
    auto_confirm: bool = False,
    auto_create_folder: bool = False,
) -> bool:
    normalized_folder = normalize_rel_folder(chosen_folder)

    if not normalized_folder:
        print(f"  [ОШИБКА] Некорректный путь папки: {chosen_folder}")
        return False

    dest_dir = safe_join_base(dest_base, normalized_folder)

    if not dest_dir:
        print(f"  [ОШИБКА] Небезопасный путь папки: {chosen_folder}")
        return False

    if not dest_dir.exists():
        if not auto_create_folder:
            if not ask_yes_no(
                f"\n  Папка '{normalized_folder}' не существует. Создать? [y/n]: "
            ):
                print("  Отмена.")
                return False

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            print(f"  [✓] Папка создана: {normalized_folder}")
        except OSError as e:
            print(f"  [ОШИБКА] {e}")
            return False

    dest_path = resolve_destination(dest_dir, file_path.name)

    if dest_path.name != file_path.name:
        print(f"  [!] Файл переименован: {dest_path.name}")

    rel_output = f"{normalized_folder}/{dest_path.name}"

    if not auto_confirm:
        if not ask_yes_no(
            f"\n  Переместить '{file_path.name}' → '{rel_output}'? [y/n]: "
        ):
            print("  Отмена.")
            return False
    else:
        print(
            f"  [auto] Перемещение без подтверждения: {file_path.name} → {rel_output}"
        )

    try:
        shutil.move(str(file_path), str(dest_path))
        print(f"  [✓] Готово: {rel_output}")
        return True
    except OSError as e:
        print(f"  [ОШИБКА] {e}")
        return False


def _confirm_delete(file_path: Path) -> bool:
    if ask_yes_no(f"  Точно удалить '{file_path.name}'? [y/n]: "):
        try:
            file_path.unlink()
            print("  [✓] Удален.")
            return True
        except OSError as e:
            print(f"  [ОШИБКА] {e}")

    return False


# ─────────────────────────────────────────────
#  Команды CLI
# ─────────────────────────────────────────────


def _set_path(prompt_msg: str) -> Path | None:
    path_str = read_input(prompt_msg).strip()

    if not path_str:
        return None

    path = Path(path_str).expanduser().resolve()

    if not path.is_dir():
        print(f"  [!] Директория не существует или недоступна: {path}")
        return None

    return path


def cmd_root() -> None:
    path = _set_path("  Путь к корневой папке источник файлов: ")

    if path:
        state.root = path
        state.tree_str, state.flat_paths = "", []
        save_config()

        print(f"  [✓] Источник root: {state.root}")
        print(f"  [✓] Сохранено в: {CONFIG_PATH}")


def cmd_dest() -> None:
    path = _set_path("  Путь к папке назначения куда сортируем: ")

    if path:
        state.dest = path
        state.tree_str, state.flat_paths = "", []
        save_config()

        print(f"  [✓] Приемник dest: {state.dest}")
        print(f"  [✓] Сохранено в: {CONFIG_PATH}")


def cmd_clear_dest() -> None:
    state.dest = None
    state.tree_str, state.flat_paths = "", []
    save_config()

    print("  [✓] dest очищен. Назначение теперь совпадает с root.")
    print(f"  [✓] Сохранено в: {CONFIG_PATH}")


def cmd_timeout(args: list[str] | None = None) -> None:
    if not args:
        if state.auto_select_seconds == 0:
            print("  Текущий таймаут автовыбора: отключен")
        else:
            print(f"  Текущий таймаут автовыбора: {state.auto_select_seconds} сек")

        print()
        print("  Использование:")
        print("    timeout 60   — автовыбор через 60 секунд")
        print("    timeout 10   — автовыбор через 10 секунд")
        print("    timeout 0    — отключить автовыбор")
        return

    raw = args[0].strip()

    try:
        value = int(raw)
    except ValueError:
        print(f"  [!] Некорректное значение таймаута: {raw}")
        return

    if value < 0:
        print("  [!] Таймаут не может быть меньше 0.")
        return

    state.auto_select_seconds = value
    save_config()

    if value == 0:
        print("  [✓] Автовыбор отключен.")
    else:
        print(f"  [✓] Таймаут автовыбора установлен: {value} сек")

    print(f"  [✓] Сохранено в: {CONFIG_PATH}")


def cmd_folders() -> None:
    if not refresh_folder_cache():
        return

    if not state.flat_paths:
        print("  [!] В целевой директории нет подпапок.")
        print(f"\n{hr()}\n{state.tree_str}\n{hr()}")
    else:
        print(f"\n{hr()}\n{state.tree_str}\n{hr()}")
        print(f"  Папок: {len(state.flat_paths)}")
        print(hr())


def cmd_start() -> None:
    if not state.root:
        print("  [!] Установите источник командой: root")
        return

    if not state.root.is_dir():
        print(f"  [!] root недоступен: {state.root}")
        return

    target_dir = state.target_dir

    if not target_dir:
        print("  [!] Не задана папка назначения.")
        return

    if not target_dir.is_dir():
        print(f"  [!] dest/root недоступен: {target_dir}")
        return

    # Автосканирование при каждом start.
    if not refresh_folder_cache():
        return

    while True:
        files = get_files_in_root(state.root)

        if not files:
            print("\n  [✓] Все файлы в источнике root обработаны.")
            break

        file_path = files[0]

        try:
            file_size = file_path.stat().st_size
        except OSError as e:
            print(f"  [ОШИБКА] Не удалось прочитать файл: {e}")
            break

        print(
            f"\n{hr('═')}\n"
            f"  Файл    : {file_path.name}\n"
            f"  Размер  : {format_size(file_size)}\n"
            f"  Осталось: {len(files)}\n"
            f"{hr('═')}"
        )

        archive_info = inspect_archive(file_path)
        print_archive_inspection(archive_info)

        print("  [LLM] Инференс: анализ файла и генерация рекомендаций...")

        recs = ask_llm(file_path, state.tree_str, state.flat_paths, archive_info=archive_info)
        show_full = recs is None
        manual_mode_for_file = False
        rejected_folders: list[str] = []

        while True:
            if show_full:
                _, current_paths = build_tree(target_dir, MAX_DEPTH)
                current_paths = current_paths or state.flat_paths

                if not current_paths:
                    print("  [!] Нет доступных папок для ручного выбора.")
                    choice = (
                        read_input("  [-1] удалить, [s] пропустить, [q] выход: ")
                        .strip()
                        .lower()
                    )

                    if choice == "q":
                        return

                    if choice == "s":
                        print("  Пропущено.")
                        break

                    if choice == "-1":
                        if _confirm_delete(file_path):
                            break

                    continue

                print_tree_numbered(current_paths)
                choice = (
                    read_input("  Номер папки, [-1] удалить, [s] пропустить, [q] выход: ")
                    .strip()
                    .lower()
                )

                if choice == "q":
                    return

                if choice == "s":
                    print("  Пропущено.")
                    break

                if choice == "-1":
                    if _confirm_delete(file_path):
                        break
                    continue

                if choice.isdigit() and 1 <= int(choice) <= len(current_paths):
                    folder = current_paths[int(choice) - 1]

                    if move_file(
                        file_path,
                        target_dir,
                        folder,
                        path_exists_in_base(target_dir, folder),
                        auto_confirm=False,
                        auto_create_folder=False,
                    ):
                        state.tree_str, state.flat_paths = build_tree(
                            target_dir, MAX_DEPTH
                        )
                        break

                continue

            display_recs, forced_timeout = prepare_recommendations_for_choice(recs)

            if not display_recs:
                show_full = True
                continue

            print("\n  Рекомендации LLM:")

            if len(display_recs) < len(recs):
                if forced_timeout is not None:
                    print(
                        "  [filter] Сильный отрыв лучшего варианта: "
                        f"показываю только 1, таймер {forced_timeout} сек."
                    )
                else:
                    print(
                        f"  [filter] Скрыты варианты ниже {MIN_RECOMMENDATION_CONFIDENCE:.0%}."
                    )

            for i, r in enumerate(display_recs, 1):
                filled = int(r["confidence"] * 10)
                conf_bar = "█" * filled + "░" * (10 - filled)
                status = "✓" if r["exists"] else "✚ новая"

                print(
                    f"  [{i:>2}] {r['folder']}/  "
                    f"{conf_bar} {r['confidence']:.0%} │ {status}\n"
                    f"        └─ {r['reason']}"
                )

            valid_choices = {str(i) for i in range(1, len(display_recs) + 1)}
            valid_choices.update({"0", "-1", "s", "q"})
            effective_timeout = forced_timeout or state.auto_select_seconds

            if effective_timeout > 0 and not manual_mode_for_file:
                prompt = (
                    f"\n  Выбор [1-{len(display_recs)}], [0] другие, [-1] удалить, "
                    f"[s] пропустить, [q] выход "
                    f"(авто 1 через {effective_timeout} сек, Enter = ручной режим): "
                )

                choice, timed_out = timed_choice(
                    prompt=prompt,
                    timeout_seconds=effective_timeout,
                    default="1",
                    valid_choices=valid_choices,
                )

                choice = (choice or "").strip().lower()

                if choice == "__manual__":
                    manual_mode_for_file = True
                    choice = (
                        read_input(
                            f"  Ручной выбор [1-{len(display_recs)}], [0] другие, [-1] удалить, "
                            f"[s] пропустить, [q] выход: "
                        )
                        .strip()
                        .lower()
                    )
                    timed_out = False
                elif not timed_out:
                    manual_mode_for_file = True

                if timed_out:
                    print("  [auto] Таймаут. Выбрана рекомендация 1.")
            else:
                prompt = (
                    f"\n  Выбор [1-{len(display_recs)}], [0] другие, [-1] удалить, "
                    f"[s] пропустить, [q] выход: "
                )
                choice = read_input(prompt).strip().lower()
                timed_out = False

            if choice == "q":
                return

            if choice == "s":
                print("  Пропущено.")
                break

            if choice == "-1":
                if _confirm_delete(file_path):
                    break
                continue

            if choice == "0":
                for r in display_recs:
                    folder = r["folder"]
                    if folder not in rejected_folders:
                        rejected_folders.append(folder)

                print("  [LLM] Текущие рекомендации отменены.", flush=True)
                print("  [LLM] Запрашиваю другие варианты...", flush=True)

                new_recs = ask_llm(
                    file_path,
                    state.tree_str,
                    state.flat_paths,
                    excluded=rejected_folders,
                    archive_info=archive_info,
                )

                if new_recs:
                    recs = new_recs
                    show_full = False
                    continue

                print("  [!] Других вариантов от LLM не получено. Открываю полный список папок.")
                show_full = True
                continue

            if choice.isdigit() and 1 <= int(choice) <= len(display_recs):
                chosen = display_recs[int(choice) - 1]

                auto_confirm = bool(timed_out and AUTO_MOVE_WITHOUT_CONFIRMATION)

                auto_create_folder = bool(timed_out and AUTO_CREATE_FOLDER_ON_TIMEOUT)

                if move_file(
                    file_path,
                    target_dir,
                    chosen["folder"],
                    chosen["exists"],
                    auto_confirm=auto_confirm,
                    auto_create_folder=auto_create_folder,
                ):
                    state.tree_str, state.flat_paths = build_tree(target_dir, MAX_DEPTH)
                    break

                continue

            print("  [!] Некорректный ввод.")


def cmd_duplicates() -> None:
    if not state.root:
        print("  [!] Установите источник командой: root")
        return

    if not state.root.is_dir():
        print(f"  [!] root недоступен: {state.root}")
        return

    files = get_files_in_root(state.root)

    if not files:
        print("  [!] В root нет файлов для проверки.")
        return

    print(f"  [dup] Сканирую похожие имена в root: {state.root}")
    groups = find_duplicate_groups(files)

    if not groups:
        print("  [✓] Похожих дублей или версий не найдено.")
        return

    print(f"  [dup] Найдено групп: {len(groups)}")
    print("  Команды:")
    print("    a         — применить предложенное удаление")
    print(f"    k N       — оставить N, остальные переместить в {DUPLICATE_REVIEW_FOLDER}/")
    print(f"    m N ...   — переместить выбранные в {DUPLICATE_REVIEW_FOLDER}/")
    print("    d N ...   — удалить выбранные после подтверждения")
    print("    s         — пропустить группу")
    print("    q         — выйти")
    print("  Предложение строится по правилам: версия → дата модификации → размер.")

    processed = 0

    for group_index, original_group in enumerate(groups, 1):
        group = [file_path for file_path in original_group if file_path.exists()]

        if len(group) < 2:
            continue

        while True:
            group = [file_path for file_path in group if file_path.exists()]

            if len(group) < 2:
                break

            print_duplicate_group(group, group_index, len(groups))
            choice = read_input("\n  duplicates> ").strip().lower()
            status, processed_delta = apply_duplicate_command(
                choice,
                group,
                state.root,
            )
            processed += processed_delta

            if status == "quit":
                print(f"  [dup] Обработано групп: {processed}")
                return

            if status == "done":
                break

    print(f"\n  [✓] Проверка дублей завершена. Обработано групп: {processed}")


def cmd_duplicates_llm() -> None:
    if not state.root:
        print("  [!] Установите источник командой: root")
        return

    if not state.root.is_dir():
        print(f"  [!] root недоступен: {state.root}")
        return

    files = get_files_in_root(state.root)

    if not files:
        print("  [!] В root нет файлов для проверки.")
        return

    if len(files) > LLM_DUPLICATE_MAX_FILES:
        print(
            f"  [!] Файлов {len(files)}, в LLM будет отправлено только первых "
            f"{LLM_DUPLICATE_MAX_FILES} по имени."
        )
        files = files[:LLM_DUPLICATE_MAX_FILES]

    print(f"  [dup-llm] Отправляю LLM список файлов: {len(files)}")
    groups, id_to_path, rejected_hints = ask_llm_duplicate_groups(files)

    if not groups:
        print("  [✓] LLM не нашла уверенных кандидатов на дубли.")
        print_rejected_duplicate_hints(rejected_hints, id_to_path)
        return

    groups = expand_llm_duplicate_groups(groups, id_to_path)

    if not groups:
        print("  [✓] После расширения похожих имён дубли не подтверждены.")
        print_rejected_duplicate_hints(rejected_hints, id_to_path)
        return

    groups = refine_llm_duplicate_groups_with_archives(groups, id_to_path)

    if not groups:
        print("  [✓] После проверки содержимого архивов дубли не подтверждены.")
        print_rejected_duplicate_hints(rejected_hints, id_to_path)
        return

    print(f"  [dup-llm] Подтверждено групп: {len(groups)}")
    print("  Действия для каждой группы:")
    print("    d — удалить предложенные файлы после подтверждения")
    print(f"    m — переместить предложенные файлы в {DUPLICATE_REVIEW_FOLDER}/")
    print("    k ID — вручную выбрать файл, который оставить")
    print(f"    km ID — выбрать файл и переместить остальные в {DUPLICATE_REVIEW_FOLDER}/")
    print("    s — пропустить")
    print("    q — выйти")

    processed = 0

    for group_index, group in enumerate(groups, 1):
        live_delete_ids = [
            file_id
            for file_id in group["delete"]
            if id_to_path[file_id].exists()
        ]

        if not id_to_path[group["keep"]].exists() or not live_delete_ids:
            continue

        group = {**group, "delete": live_delete_ids}
        print_llm_duplicate_group(group, id_to_path, group_index, len(groups))
        status, processed_delta = apply_llm_duplicate_group(group, id_to_path, state.root)
        processed += processed_delta

        if status == "quit":
            print(f"  [dup-llm] Обработано групп: {processed}")
            return

    print(f"\n  [✓] LLM-проверка дублей завершена. Обработано групп: {processed}")


def cmd_status() -> None:
    print(f"\n{hr()}")
    print(f"  INI             : {CONFIG_PATH}")
    print(f"  Источник root   : {state.root or 'Не установлен'}")
    print(f"  Приемник dest   : {state.dest or 'Совпадает с root'}")

    if state.root and state.root.exists():
        print(f"  Файлов в root   : {len(get_files_in_root(state.root))}")

    target = state.target_dir

    if target:
        print(f"  Целевая папка   : {target}")

    print(f"  Папок в кэше    : {len(state.flat_paths)}")

    if state.auto_select_seconds == 0:
        print("  Автовыбор       : отключен")
    else:
        print(f"  Автовыбор       : {state.auto_select_seconds} сек")

    print(f"  Автосоздание    : {'да' if AUTO_CREATE_FOLDER_ON_TIMEOUT else 'нет'}")
    print(f"  Автоперенос     : {'да' if AUTO_MOVE_WITHOUT_CONFIRMATION else 'нет'}")
    print(hr())


def cmd_help() -> None:
    print(
        f"\n{hr()}\n"
        f"  root        — задать папку с файлами и сохранить в ini\n"
        f"  dest        — задать папку назначения и сохранить в ini\n"
        f"  clear_dest  — очистить dest, назначение будет равно root\n"
        f"  folders     — вручную пересканировать папки в dest/root\n"
        f"  start       — начать сортировку, папки сканируются автоматически\n"
        f"  duplicates  — найти похожие файлы/версии в root\n"
        f"  duplicates_llm — LLM-поиск дублей с проверкой содержимого архивов\n"
        f"  timeout N   — изменить таймаут автовыбора, 0 = отключить\n"
        f"  status      — статус\n"
        f"  help        — помощь\n"
        f"  exit        — выход\n"
        f"{hr()}"
    )


COMMANDS = {
    "root": cmd_root,
    "dest": cmd_dest,
    "clear_dest": cmd_clear_dest,
    "folders": cmd_folders,
    "start": cmd_start,
    "duplicates": cmd_duplicates,
    "duplicates_llm": cmd_duplicates_llm,
    "status": cmd_status,
    "help": cmd_help,
}


def main() -> None:
    print(f"\n{hr('═')}\n  File Sorter Assistant | Gemma 4 JSON Mode\n{hr('═')}")

    load_config()

    if state.root or state.dest:
        print("  [ini] Загружены сохраненные настройки:")
        print(f"        root   : {state.root or 'не установлен'}")
        print(f"        dest   : {state.dest or 'совпадает с root'}")

        if state.auto_select_seconds == 0:
            print("        timeout: отключен")
        else:
            print(f"        timeout: {state.auto_select_seconds} сек")

        print(f"        ini    : {CONFIG_PATH}")

    while True:
        try:
            line = read_input("\n> ").strip()

            if not line:
                continue

            parts = line.split()
            cmd = parts[0].lower()
            args = parts[1:]

            if cmd in ("exit", "quit"):
                break

            if cmd == "timeout":
                cmd_timeout(args)
                continue

            handler = COMMANDS.get(cmd)

            if handler:
                handler()
            else:
                print("  [!] Неизвестная команда. Введите 'help'.")

        except KeyboardInterrupt:
            print()
            break
        except EOFError:
            print()
            print("  [!] Консоль закрыла поток ввода (EOF). Запустите в обычном терминале или включите stdin в IDE.")
            break


if __name__ == "__main__":
    main()
