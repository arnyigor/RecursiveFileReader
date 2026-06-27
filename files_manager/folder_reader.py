from pathlib import Path
from datetime import datetime

# =========================
# НАСТРОЙКИ
# =========================

PATHS = [
    r"e:\Unreal",
    r"f:\3D\UE",
]

OUTPUT_FILE = "files_report.txt"

RECURSIVE = True

# Если True — показывать в отчете пропущенные папки
SHOW_EXCLUDED_DIRS = True

# Если True — считать размер UE-проекта целиком.
# Для скорости лучше оставить False.
CALCULATE_PROJECT_SIZE = False

# Папки, которые скрипт НЕ будет читать вообще
EXCLUDED_DIR_NAMES = {
    ".git",
    ".idea",
    ".gradle",
    ".vs",
    "__pycache__",

    # Unreal Engine / heavy cache folders
    "VaultCache",
    "DerivedDataCache",
    "Intermediate",
    "Saved",
    "Binaries",
    ".vs",
}


# =========================
# ЛОГИКА
# =========================

def normalize_name(name: str) -> str:
    return name.lower()


EXCLUDED_DIR_NAMES_NORMALIZED = {
    normalize_name(name) for name in EXCLUDED_DIR_NAMES
}


def is_excluded_dir(path: Path) -> bool:
    return path.is_dir() and normalize_name(path.name) in EXCLUDED_DIR_NAMES_NORMALIZED


def bytes_to_mb(bytes_count: int) -> float:
    return bytes_count / (1024 * 1024)


def file_size_mb(path: Path) -> float:
    return bytes_to_mb(path.stat().st_size)


def is_unreal_project_dir(folder: Path) -> bool:
    """
    UE проект считаем найденным, если внутри есть:
    - хотя бы один .uproject
    - папка Content

    Если это UE проект — дальше внутрь не заходим.
    """
    if not folder.is_dir():
        return False

    has_content = (folder / "Content").is_dir()
    has_uproject = any(folder.glob("*.uproject"))

    return has_content and has_uproject


def folder_size_mb(folder: Path) -> float:
    """
    Подсчет размера папки с учетом исключений.
    Используется только если CALCULATE_PROJECT_SIZE = True.
    """
    total = 0
    stack = [folder]

    while stack:
        current = stack.pop()

        try:
            for item in current.iterdir():
                try:
                    if item.is_dir():
                        if is_excluded_dir(item):
                            continue
                        stack.append(item)

                    elif item.is_file():
                        total += item.stat().st_size

                except (PermissionError, FileNotFoundError):
                    pass

        except (PermissionError, FileNotFoundError):
            pass

    return bytes_to_mb(total)


def add_line(lines: list[str], text: str):
    lines.append(text)


def scan_folder(folder: Path, lines: list[str], stats: dict):
    try:
        items = sorted(folder.iterdir(), key=lambda p: str(p).lower())
    except PermissionError:
        add_line(lines, f"[NO ACCESS] {folder}")
        return
    except FileNotFoundError:
        add_line(lines, f"[MISSING] {folder}")
        return

    for item in items:
        try:
            if item.is_dir():
                if is_excluded_dir(item):
                    stats["excluded_dirs"] += 1

                    if SHOW_EXCLUDED_DIRS:
                        add_line(lines, f"[EXCLUDED DIR] {item}")

                    continue

                if is_unreal_project_dir(item):
                    stats["ue_projects"] += 1

                    if CALCULATE_PROJECT_SIZE:
                        mb = folder_size_mb(item)
                        stats["total_mb"] += mb
                        add_line(lines, f"[UE PROJECT] {item} | {mb:.2f} MB")
                    else:
                        add_line(lines, f"[UE PROJECT] {item} | size skipped")

                    continue

                if RECURSIVE:
                    scan_folder(item, lines, stats)

            elif item.is_file():
                mb = file_size_mb(item)
                stats["files"] += 1
                stats["total_mb"] += mb
                add_line(lines, f"[FILE] {item} | {mb:.2f} MB")

        except PermissionError:
            add_line(lines, f"[NO ACCESS] {item}")
        except FileNotFoundError:
            add_line(lines, f"[MISSING] {item}")


def main():
    lines = []

    stats = {
        "files": 0,
        "ue_projects": 0,
        "excluded_dirs": 0,
        "total_mb": 0.0,
    }

    add_line(lines, "FILES REPORT")
    add_line(lines, f"Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    add_line(lines, f"Recursive: {RECURSIVE}")
    add_line(lines, f"Calculate UE project size: {CALCULATE_PROJECT_SIZE}")
    add_line(lines, "")
    add_line(lines, "Excluded folders:")
    for name in sorted(EXCLUDED_DIR_NAMES, key=str.lower):
        add_line(lines, f"- {name}")

    for raw_path in PATHS:
        root = Path(raw_path)

        add_line(lines, "")
        add_line(lines, "=" * 120)
        add_line(lines, f"ROOT: {root}")
        add_line(lines, "=" * 120)

        if not root.exists():
            add_line(lines, "Папка не найдена")
            continue

        if not root.is_dir():
            add_line(lines, "Это не папка")
            continue

        if is_excluded_dir(root):
            stats["excluded_dirs"] += 1
            add_line(lines, f"[EXCLUDED DIR] {root}")
            continue

        if is_unreal_project_dir(root):
            stats["ue_projects"] += 1

            if CALCULATE_PROJECT_SIZE:
                mb = folder_size_mb(root)
                stats["total_mb"] += mb
                add_line(lines, f"[UE PROJECT] {root} | {mb:.2f} MB")
            else:
                add_line(lines, f"[UE PROJECT] {root} | size skipped")

            continue

        scan_folder(root, lines, stats)

    add_line(lines, "")
    add_line(lines, "=" * 120)
    add_line(lines, "SUMMARY")
    add_line(lines, "=" * 120)
    add_line(lines, f"Files: {stats['files']}")
    add_line(lines, f"UE projects: {stats['ue_projects']}")
    add_line(lines, f"Excluded dirs: {stats['excluded_dirs']}")
    add_line(lines, f"Total file size: {stats['total_mb']:.2f} MB")

    output_path = Path(OUTPUT_FILE)
    output_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"Готово. Отчет сохранен: {output_path.resolve()}")


if __name__ == "__main__":
    main()