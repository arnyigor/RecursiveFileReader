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


def read_zip_entries(file_path: Path) -> tuple[list[str], int | None, bool]:
    with zipfile.ZipFile(file_path) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]

    entries, truncated = limited_archive_entries(names)
    return entries, len(names), truncated


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


def read_7z_entries(file_path: Path) -> tuple[list[str], int | None, bool]:
    tools = candidate_archive_tools(
        names=["7z", "7za", "7z.exe", "7za.exe"],
        common_relative_paths=[
            "7-Zip/7z.exe",
            "7-Zip/7za.exe",
            "NanaZip/NanaZipC.exe",
        ],
    )

    if not tools:
        raise RuntimeError("для .7z не найден 7z/7za в PATH или Program Files")

    errors: list[str] = []

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


def read_rar_entries_with_rarfile(file_path: Path) -> tuple[list[str], int | None, bool] | None:
    try:
        import rarfile  # type: ignore
    except ImportError:
        return None

    with rarfile.RarFile(file_path) as archive:
        names = [info.filename for info in archive.infolist() if not info.isdir()]

    entries, truncated = limited_archive_entries(names)
    return entries, len(names), truncated


def parse_unrar_list_output(output: str) -> tuple[list[str], int | None, bool]:
    entries: list[str] = []

    for line in output.splitlines():
        path = normalize_archive_entry(line)

        if not path:
            continue

        entries.append(path)

        if len(entries) >= MAX_ARCHIVE_SCAN_ENTRIES:
            return entries, None, True

    return entries, len(entries), False


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

    rar_tools = candidate_archive_tools(
        names=["unrar", "rar", "winrar", "UnRAR.exe", "Rar.exe", "WinRAR.exe"],
        common_relative_paths=[
            "WinRAR/UnRAR.exe",
            "WinRAR/Rar.exe",
            "WinRAR/WinRAR.exe",
        ],
    )

    for exe in rar_tools:
        completed = subprocess.run(
            [exe, "lb", str(file_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

        if completed.returncode == 0:
            return parse_unrar_list_output(completed.stdout)

        errors.append(completed.stderr.strip() or completed.stdout.strip() or exe)

    details = "; ".join(errors)
    suffix = f": {details}" if details else ""
    raise RuntimeError(
        "для .rar не найден rarfile, 7z/7za или WinRAR/UnRAR в PATH/Program Files"
        f"{suffix}"
    )


def classify_archive_entries(entries: list[str]) -> tuple[str | None, str]:
    lower_entries = [entry.lower() for entry in entries]
    basenames = [Path(entry).name.lower() for entry in lower_entries]

    has_init_py = "__init__.py" in basenames
    has_blend = any(name.endswith(".blend") for name in lower_entries)
    has_texture = any(Path(name).suffix.lower() in TEXTURE_SUFFIXES for name in lower_entries)
    has_uproject = any(name.endswith(".uproject") for name in lower_entries)
    has_uplugin = any(name.endswith(".uplugin") for name in lower_entries)

    if has_init_py:
        return "blender_addon", "найден __init__.py внутри архива"

    if has_uproject:
        return "ue_project", "найден .uproject внутри архива"

    if has_uplugin:
        return "ue_plugin", "найден .uplugin внутри архива"

    if has_blend and has_texture:
        return "blender_assets", "найдены .blend файл и текстуры"

    if has_blend:
        return "blender_assets", "найден .blend файл"

    return None, ""


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
#  Ввод с таймаутом
# ─────────────────────────────────────────────


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
        choice = input(prompt).strip().lower()
        return choice, False

    print(prompt, end="", flush=True)

    if os.name == "nt":
        print(
            "\n  [i] Windows/PyCharm: автовыбор отключен для надежного ввода."
        )
        choice = input("  Введите выбор и нажмите Enter: ").strip().lower()

        if choice == "":
            return "__manual__", False

        return choice, False

    else:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)

        if ready:
            choice = sys.stdin.readline().strip().lower()

            if choice == "":
                return "__manual__", False

            return choice, False

        print()
        return default, True


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

IMPORTANT:
28. Known standalone VFX/DCC tools and generators must not be classified as UE assets unless the filename explicitly contains UE/Unreal markers.
29. EmberGen, EmbrGen, LiquiGen, GeoGen, Houdini, Blender, Maya, ZBrush, Substance, Marvelous Designer, World Creator, Gaea, SpeedTree are standalone software/tools unless filename explicitly says they are UE/Unreal assets or plugins.
30. If Archive quick scan says __init__.py was found, strongly prefer Blender add-on folders.
31. If Archive quick scan says .blend and textures were found, strongly prefer Blender asset/scene/props folders.
32. If Archive quick scan says .uproject or .uplugin was found, strongly prefer Unreal Engine folders.
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

        if isinstance(obj, dict) and isinstance(obj.get("recommendations"), list):
            return obj

        idx = brace_idx + max(end, 1)

    print(
        "  [ОШИБКА] В ответе LLM не найден валидный JSON-объект с ключом 'recommendations'."
    )
    print(f"  [DEBUG RAW]:\n{text[:1000]}...\n")

    return None


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
        answer = input(prompt).strip().lower()

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
    path_str = input(prompt_msg).strip()

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
                        input("  [-1] удалить, [s] пропустить, [q] выход: ")
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
                    input("  Номер папки, [-1] удалить, [s] пропустить, [q] выход: ")
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

            print("\n  Рекомендации LLM:")

            for i, r in enumerate(recs, 1):
                filled = int(r["confidence"] * 10)
                conf_bar = "█" * filled + "░" * (10 - filled)
                status = "✓" if r["exists"] else "✚ новая"

                print(
                    f"  [{i:>2}] {r['folder']}/  "
                    f"{conf_bar} {r['confidence']:.0%} │ {status}\n"
                    f"        └─ {r['reason']}"
                )

            valid_choices = {str(i) for i in range(1, len(recs) + 1)}
            valid_choices.update({"0", "-1", "s", "q"})

            if state.auto_select_seconds > 0 and not manual_mode_for_file:
                prompt = (
                    f"\n  Выбор [1-{len(recs)}], [0] другие, [-1] удалить, "
                    f"[s] пропустить, [q] выход "
                    f"(авто 1 через {state.auto_select_seconds} сек, Enter = ручной режим): "
                )

                choice, timed_out = timed_choice(
                    prompt=prompt,
                    timeout_seconds=state.auto_select_seconds,
                    default="1",
                    valid_choices=valid_choices,
                )

                choice = (choice or "").strip().lower()

                if choice == "__manual__":
                    manual_mode_for_file = True
                    choice = (
                        input(
                            f"  Ручной выбор [1-{len(recs)}], [0] другие, [-1] удалить, "
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
                    f"\n  Выбор [1-{len(recs)}], [0] другие, [-1] удалить, "
                    f"[s] пропустить, [q] выход: "
                )
                choice = input(prompt).strip().lower()
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
                for r in recs:
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

            if choice.isdigit() and 1 <= int(choice) <= len(recs):
                chosen = recs[int(choice) - 1]

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
            line = input("\n> ").strip()

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
