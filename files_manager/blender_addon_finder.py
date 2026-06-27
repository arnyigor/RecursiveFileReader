from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - handled at runtime
    yaml = None


# ============================================================
# Constants / regex
# ============================================================

VERSION_RE = re.compile(
    r"(?i)(?:^|[\s._\-()\[\]])(?:v|ver|version)?[\s._\-]*([0-9]+(?:[._\-][0-9]+){0,4})(?:$|[\s._\-()\[\]])"
)
YEAR_RE = re.compile(r"(?<!\d)(20[0-3][0-9])(?!\d)")
COPY_RE = re.compile(
    r"(?i)(copy|копия|backup|bak|old|старый|duplicate|дубликат|draft|final|latest|new|\(\d+\)|- copy)"
)
QUANT_RE = re.compile(r"(?i)\b(?:IQ\d_[A-Z0-9_]+|Q\d(?:_[A-Z0-9]+){0,3})\b")
MODEL_SIZE_RE = re.compile(r"(?i)(?<![A-Z0-9])(\d+(?:\.\d+)?B)(?![A-Z0-9])")

AI_MODEL_EXTENSIONS = {".gguf", ".safetensors", ".bin", ".onnx", ".pt", ".pth", ".ckpt"}
ARCHIVE_EXTENSIONS = {
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".xz",
    ".bz2",
    ".zst",
    ".tgz",
}
BLENDER_PROJECT_EXTENSIONS = {".blend", ".blend1", ".blend2"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".aac"}
IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".tif",
    ".tiff",
    ".exr",
    ".hdr",
    ".bmp",
    ".gif",
}
DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".md",
    ".rtf",
}
CODE_EXTENSIONS = {
    ".py",
    ".kt",
    ".kts",
    ".java",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".rs",
    ".go",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".gradle",
}
INSTALLER_EXTENSIONS = {
    ".exe",
    ".msi",
    ".iso",
    ".dmg",
    ".pkg",
    ".appx",
    ".appxbundle",
    ".msix",
    ".msixbundle",
}
TEMP_EXTENSIONS = {".tmp", ".temp", ".cache", ".log", ".bak", ".old"}
PROTECTED_REVIEW_EXTENSIONS = {
    ".blend",
    ".blend1",
    ".blend2",
    ".psd",
    ".kra",
    ".prproj",
    ".aep",
    ".veg",
    ".flp",
    ".als",
    ".docx",
    ".xlsx",
    ".pdf",
    ".gguf",
    ".safetensors",
    ".onnx",
    ".pt",
    ".pth",
    ".ckpt",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "scan": {
        "roots": [],
        "exclude_dirs": [
            ".git",
            "node_modules",
            ".gradle",
            ".idea",
            "__pycache__",
            ".venv",
            "venv",
            "$RECYCLE.BIN",
            "System Volume Information",
        ],
        "exclude_files": ["desktop.ini", "thumbs.db"],
        "max_depth": None,
        "follow_symlinks": False,
    },
    "analysis": {
        "top_largest_files": 300,
        "top_largest_dirs": 300,
        "max_files_for_llm_chunk": 200,
        "min_duplicate_size_mb": 1,
        "min_version_group_size_mb": 5,
        "hash_duplicates": False,
        "hash_min_size_mb": 1,
        "hash_max_size_gb": 20,
    },
    "llm": {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "local",
        "model": "local-model",
        "temperature": 0.2,
        "max_tokens": 4096,
        "timeout_sec": 300,
        "retries": 2,
        "max_prompt_chars": 120_000,
    },
    "output": {
        "directory": "outputs",
        "write_json": True,
        "write_markdown": True,
        "write_csv": True,
        "write_delete_plan": True,
        "write_move_plan": True,
        "central_folders": {
            "ai_models": "G:/AIModels/Models",
            "blender_addons_archives": "G:/Blender/Addons/Archives",
            "blender_addons_installed": "G:/Blender/Addons/Installed",
            "archives": "D:/Archives",
            "installers": "D:/Installers",
        },
        "quarantine_root": "G:/__cleanup_quarantine",
    },
}


# ============================================================
# Models
# ============================================================


@dataclass(slots=True)
class FileRecord:
    root: str
    path: str
    parent_path: str
    name: str
    stem: str
    normalized_stem: str
    extension: str
    size: int
    size_mb: float
    modified_time: str | None
    created_time: str | None
    depth: int
    category: str
    signals: str
    is_hidden: bool = False
    is_symlink: bool = False
    scan_id: str = ""


# ============================================================
# Utility / config / logging
# ============================================================


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_path(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def size_mb(size: int) -> float:
    return round(size / 1024 / 1024, 2)


def format_size(size: int | float) -> str:
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.2f} {units[idx]}"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_env_file(env_path: str) -> None:
    path = Path(env_path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_indexed_list(prefix: str) -> list[str]:
    result: list[str] = []
    for i in range(1, 1000):
        value = os.getenv(f"{prefix}_{i}")
        if value is None:
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            result.append(value)
    return result


def apply_env_config(config: dict[str, Any], env_path: str) -> dict[str, Any]:
    load_env_file(env_path)

    roots = env_indexed_list("ROOT_PATH")
    if roots:
        config["scan"]["roots"] = roots

    exclude_dirs = env_indexed_list("EXCLUDE_DIR")
    if exclude_dirs:
        config["scan"]["exclude_dirs"] = exclude_dirs

    out_dir = env_str("OUT_DIR")
    if out_dir:
        config["output"]["directory"] = out_dir

    llama_url = env_str("LLAMA_URL")
    if llama_url:
        config["llm"]["base_url"] = llama_url.rstrip("/")
        if config["llm"]["base_url"].endswith("/chat/completions"):
            config["llm"]["base_url"] = config["llm"]["base_url"][
                : -len("/chat/completions")
            ]
        if config["llm"]["base_url"].endswith("/v1") is False:
            config["llm"]["base_url"] = config["llm"]["base_url"].rstrip("/")

    llama_model = env_str("LLAMA_MODEL")
    if llama_model:
        config["llm"]["model"] = llama_model

    config["llm"]["timeout_sec"] = env_int(
        "REQUEST_TIMEOUT_SECONDS", int(config["llm"].get("timeout_sec", 300))
    )
    config["scan"]["follow_symlinks"] = env_bool(
        "FOLLOW_SYMLINKS", bool(config["scan"].get("follow_symlinks", False))
    )
    config["analysis"]["min_duplicate_size_mb"] = env_float(
        "MIN_FILE_SIZE_MB_FOR_LLM",
        float(config["analysis"].get("min_duplicate_size_mb", 1)),
    )
    config["analysis"]["top_largest_files"] = env_int(
        "TOP_LARGE_FILES_LIMIT",
        int(config["analysis"].get("top_largest_files", 300)),
    )
    config["analysis"]["top_largest_dirs"] = env_int(
        "TOP_LARGE_FOLDERS_LIMIT",
        int(config["analysis"].get("top_largest_dirs", 300)),
    )

    central = config["output"].setdefault("central_folders", {})
    env_targets = {
        "BLENDER_ADDONS_DIR": "blender_addons_archives",
        "BLENDER_ASSETS_DIR": "blender_assets",
        "LLM_MODELS_DIR": "ai_models",
        "ARCHIVES_DIR": "archives",
        "INSTALLERS_DIR": "installers",
    }
    for env_name, key in env_targets.items():
        value = env_str(env_name)
        if value:
            central[key] = value

    return config


def parse_scalar_yaml_value(value: str) -> Any:
    value = value.strip()
    if value == "" or value.lower() in {"null", "none", "~"}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def load_simple_yaml(text: str) -> dict[str, Any]:
    """
    Tiny fallback parser for this project's config.yaml shape.

    It supports nested mappings, scalar values and lists of scalars. If PyYAML is
    installed, PyYAML is used instead; this fallback keeps the CLI usable in a
    minimal Python environment.
    """
    tokens: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if " #" in stripped:
            stripped = stripped.split(" #", 1)[0].rstrip()
        tokens.append((indent, stripped))

    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    for idx, (indent, stripped) in enumerate(tokens):
        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if stripped.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"Invalid YAML list item: {stripped}")
            parent.append(parse_scalar_yaml_value(stripped[2:].strip()))
            continue

        key, sep, rest = stripped.partition(":")
        if not sep:
            raise ValueError(f"Invalid YAML mapping item: {stripped}")
        key = key.strip()
        rest = rest.strip()

        if rest:
            value: Any = parse_scalar_yaml_value(rest)
        else:
            next_is_list = False
            for next_indent, next_stripped in tokens[idx + 1 :]:
                if next_indent <= indent:
                    break
                next_is_list = next_stripped.startswith("- ")
                break
            value = [] if next_is_list else {}

        if isinstance(parent, dict):
            parent[key] = value
        elif isinstance(parent, list):
            item = {key: value}
            parent.append(item)
        else:
            raise ValueError(f"Invalid YAML parent for: {stripped}")

        if isinstance(value, (dict, list)):
            stack.append((indent, value))

    return root


def load_config(
    config_path: str = "config.yaml", env_path: str = ".env"
) -> dict[str, Any]:
    path = Path(config_path)
    loaded: dict[str, Any] = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if yaml is not None:
            loaded = yaml.safe_load(text) or {}
        else:
            loaded = load_simple_yaml(text)
    cfg = deep_merge(DEFAULT_CONFIG, loaded)
    cfg = apply_env_config(cfg, env_path)
    for field in ("roots", "exclude_dirs", "exclude_files"):
        value = cfg["scan"].get(field)
        if value is None or isinstance(value, dict):
            cfg["scan"][field] = []
    return cfg


def output_dirs(config: dict[str, Any]) -> dict[str, Path]:
    root = Path(config["output"]["directory"])
    dirs = {
        "root": root,
        "scans": root / "scans",
        "reports": root / "reports",
        "plans": root / "plans",
        "llm": root / "llm",
        "chunks": root / "llm" / "chunks",
        "logs": root / "logs",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    return dirs


def setup_logging(config: dict[str, Any]) -> None:
    dirs = output_dirs(config)
    log_path = dirs["logs"] / "run.log"
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def config_hash(config: dict[str, Any]) -> str:
    raw = json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def json_dump(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_csv(
    path: str | Path, rows: list[dict[str, Any]], fields: list[str] | None = None
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = sorted({k for row in rows for k in row.keys()}) if rows else []
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# Classification / versions
# ============================================================


def normalize_stem(stem: str) -> str:
    s = stem.lower()
    s = QUANT_RE.sub(" ", s)
    s = VERSION_RE.sub(" ", s)
    s = YEAR_RE.sub(" ", s)
    s = COPY_RE.sub(" ", s)
    s = re.sub(
        r"\b(final|release|latest|new|old|backup|bak|copy|draft|копия|дубликат)\b",
        " ",
        s,
    )
    s = re.sub(r"[\s._\-()\[\]{}]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_ai_model_name(name: str) -> dict[str, str | None]:
    stem = Path(name).stem
    quant = QUANT_RE.search(stem)
    model_size = MODEL_SIZE_RE.search(stem)
    lowered = stem.lower()
    model_type = None
    for token in ["instruct", "coder", "vision", "embedding", "reranker", "mtp"]:
        if token in lowered:
            model_type = token
            break
    base = QUANT_RE.sub("", stem)
    base = re.sub(r"[._\-]+", " ", base).strip()
    return {
        "model_name_hint": base or None,
        "model_size": model_size.group(1) if model_size else None,
        "quantization": quant.group(0).upper() if quant else None,
        "model_type": model_type,
    }


def classify_file(path: str, extension: str) -> tuple[str, list[str]]:
    p = normalize_path(path).lower()
    name = Path(path).name.lower()
    parts = set(Path(path).parts)
    parts_lower = {x.lower() for x in parts}
    signals: list[str] = []

    if any(
        x in parts_lower
        for x in [
            "cache",
            "caches",
            "temp",
            "tmp",
            "logs",
            "__pycache__",
            ".pytest_cache",
        ]
    ):
        signals.append("path_has_cache_temp_log_dir")
    if any(
        x in parts_lower
        for x in [
            "build",
            "out",
            "target",
            ".gradle",
            ".kotlin",
            "intermediates",
            "generated",
            "node_modules",
            "dist",
        ]
    ):
        signals.append("path_has_build_generated_dir")
    if "downloads" in parts_lower or "download" in parts_lower:
        signals.append("path_has_downloads")

    if extension in AI_MODEL_EXTENSIONS or any(
        x in name
        for x in [
            "gguf",
            "safetensors",
            "qwen",
            "llama",
            "mistral",
            "gemma",
            "deepseek",
            "sdxl",
        ]
    ):
        signals.append("ai_model_signature")
        return "ai_models", signals

    if extension in BLENDER_PROJECT_EXTENSIONS:
        signals.append("blender_project_extension")
        return "blender_projects", signals

    blender_path = any(
        x in p
        for x in ["blender", "addons", "add-ons", "scripts/addons", "scripts\\addons"]
    )
    if extension in ARCHIVE_EXTENSIONS and blender_path:
        signals.append("blender_addon_archive_signature")
        return "blender_addons", signals
    if extension == ".py" and blender_path:
        signals.append("blender_addon_script_signature")
        return "blender_addons", signals

    if "android" in p or any(
        x in parts_lower for x in ["gradle", ".gradle", "kotlin", "ksp", "kapt"]
    ):
        signals.append("android_project_signature")
        if "path_has_build_generated_dir" in signals:
            return "cache", signals
        return "android_projects", signals

    if (
        "path_has_cache_temp_log_dir" in signals
        or "path_has_build_generated_dir" in signals
        or extension in TEMP_EXTENSIONS
    ):
        return "cache" if "cache" in p or "build" in p else "temporary", signals

    if extension in INSTALLER_EXTENSIONS:
        signals.append("installer_extension")
        return "installers", signals
    if extension in ARCHIVE_EXTENSIONS:
        signals.append("archive_extension")
        return "archives", signals
    if extension in VIDEO_EXTENSIONS:
        signals.append("video_extension")
        return "video", signals
    if extension in AUDIO_EXTENSIONS:
        signals.append("audio_extension")
        return "audio", signals
    if extension in IMAGE_EXTENSIONS:
        signals.append("image_extension")
        if any(x in p for x in ["textures", "assets", "hdr", "hdri", "materials"]):
            return "game_assets", signals
        return "images", signals
    if extension in DOCUMENT_EXTENSIONS:
        signals.append("document_extension")
        return "documents", signals
    if extension in CODE_EXTENSIONS:
        signals.append("code_extension")
        if "python" in p or extension == ".py":
            return "python_projects", signals
        if "node_modules" in p or extension in {".js", ".ts", ".tsx", ".jsx"}:
            return "node_projects", signals
        return "code_projects", signals
    if "downloads" in p:
        return "downloads", signals
    return "unknown", signals


# ============================================================
# Folder-level detection (addon folders, quick stats)
# ============================================================

BLENDER_FOLDER_SIGNALS = {"blender", "addons", "add-ons"}


def is_blender_context(path_norm: str) -> bool:
    """True if any path component suggests a Blender addon location."""
    parts = {x.lower() for x in Path(path_norm).parts}
    return bool(parts & BLENDER_FOLDER_SIGNALS)


def probe_addon_folder(dir_path: Path) -> tuple[bool, list[str]]:
    """Fast probe: does this directory look like a Blender addon?

    Reads at most the first 4 KiB of ``__init__.py`` looking for the
    ``bl_info`` dict that every Blender addon must define.
    """
    signals: list[str] = []
    init_file = dir_path / "__init__.py"
    if not init_file.exists():
        return False, signals
    try:
        head = init_file.read_text(encoding="utf-8", errors="replace")[:4096]
        if "bl_info" in head:
            signals.append("folder_has_bl_info")
            return True, signals
        # Looser heuristic: register()/unregister() pair without bl_info
        if "def register()" in head and "def unregister()" in head:
            signals.append("folder_has_register_pair")
            return True, signals
    except OSError:
        pass
    return False, signals


def quick_dir_stats(
    dir_path: Path,
    follow_symlinks: bool = False,
    max_depth: int = 10,
    _depth: int = 0,
) -> tuple[int, int]:
    """Recursively count files and sum sizes without classification overhead.

    Returns ``(file_count, total_bytes)``.
    """
    file_count = 0
    total_size = 0
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=follow_symlinks):
                        st = entry.stat(follow_symlinks=follow_symlinks)
                        file_count += 1
                        total_size += st.st_size
                    elif (
                        entry.is_dir(follow_symlinks=follow_symlinks)
                        and _depth < max_depth
                    ):
                        sub_count, sub_size = quick_dir_stats(
                            Path(entry.path),
                            follow_symlinks,
                            max_depth,
                            _depth + 1,
                        )
                        file_count += 1 + sub_count
                        total_size += sub_size
                except (OSError, PermissionError):
                    pass
    except (OSError, PermissionError):
        pass
    return file_count, total_size


# ============================================================
# SQLite database
# ============================================================


class Database:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                root TEXT NOT NULL,
                path TEXT NOT NULL,
                parent_path TEXT NOT NULL,
                name TEXT NOT NULL,
                stem TEXT NOT NULL,
                normalized_stem TEXT NOT NULL,
                extension TEXT NOT NULL,
                size INTEGER NOT NULL,
                size_mb REAL NOT NULL,
                modified_time TEXT,
                created_time TEXT,
                depth INTEGER NOT NULL,
                category TEXT NOT NULL,
                signals TEXT,
                is_hidden INTEGER DEFAULT 0,
                is_symlink INTEGER DEFAULT 0,
                scan_id TEXT NOT NULL,
                partial_hash TEXT,
                sha256 TEXT
            );

            CREATE TABLE IF NOT EXISTS directories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                root TEXT NOT NULL,
                path TEXT NOT NULL,
                parent_path TEXT NOT NULL,
                depth INTEGER NOT NULL,
                direct_file_count INTEGER DEFAULT 0,
                recursive_file_count INTEGER DEFAULT 0,
                direct_size INTEGER DEFAULT 0,
                recursive_size INTEGER DEFAULT 0,
                modified_time TEXT,
                scan_id TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_runs (
                scan_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                config_hash TEXT,
                total_files INTEGER DEFAULT 0,
                total_dirs INTEGER DEFAULT 0,
                total_size INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                path TEXT NOT NULL,
                error_type TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_files_path ON files(path)",
            "CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)",
            "CREATE INDEX IF NOT EXISTS idx_files_extension ON files(extension)",
            "CREATE INDEX IF NOT EXISTS idx_files_size ON files(size)",
            "CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent_path)",
            "CREATE INDEX IF NOT EXISTS idx_files_scan_id ON files(scan_id)",
            "CREATE INDEX IF NOT EXISTS idx_files_category ON files(category)",
            "CREATE INDEX IF NOT EXISTS idx_files_norm_ext_size ON files(normalized_stem, extension, size)",
            "CREATE INDEX IF NOT EXISTS idx_dirs_scan_size ON directories(scan_id, recursive_size)",
            "CREATE INDEX IF NOT EXISTS idx_errors_scan_id ON errors(scan_id)",
        ]
        for stmt in indexes:
            self.conn.execute(stmt)

        # Migration: add category/signals columns to directories if missing
        for col_sql in (
            "ALTER TABLE directories ADD COLUMN category TEXT DEFAULT ''",
            "ALTER TABLE directories ADD COLUMN signals TEXT DEFAULT ''",
        ):
            try:
                self.conn.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # column already exists

        self.conn.commit()

    def latest_scan_id(self) -> str:
        row = self.conn.execute(
            "SELECT scan_id FROM scan_runs WHERE status = 'finished' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise RuntimeError("No finished scan found. Run: scan")
        return str(row["scan_id"])

    def start_scan(self, scan_id: str, cfg_hash: str) -> None:
        self.conn.execute(
            "INSERT INTO scan_runs(scan_id, started_at, status, config_hash) VALUES (?, ?, 'running', ?)",
            (scan_id, now_iso(), cfg_hash),
        )
        self.conn.commit()

    def finish_scan(
        self,
        scan_id: str,
        status: str,
        total_files: int,
        total_dirs: int,
        total_size: int,
        error_count: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE scan_runs
            SET finished_at = ?, status = ?, total_files = ?, total_dirs = ?, total_size = ?, error_count = ?
            WHERE scan_id = ?
            """,
            (
                now_iso(),
                status,
                total_files,
                total_dirs,
                total_size,
                error_count,
                scan_id,
            ),
        )
        self.conn.commit()

    def insert_files(self, records: list[FileRecord]) -> None:
        self.conn.executemany(
            """
            INSERT INTO files(
                root, path, parent_path, name, stem, normalized_stem, extension, size, size_mb,
                modified_time, created_time, depth, category, signals, is_hidden, is_symlink, scan_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.root,
                    r.path,
                    r.parent_path,
                    r.name,
                    r.stem,
                    r.normalized_stem,
                    r.extension,
                    r.size,
                    r.size_mb,
                    r.modified_time,
                    r.created_time,
                    r.depth,
                    r.category,
                    r.signals,
                    int(r.is_hidden),
                    int(r.is_symlink),
                    r.scan_id,
                )
                for r in records
            ],
        )

    def insert_directories(self, rows: list[dict[str, Any]]) -> None:
        self.conn.executemany(
            """
            INSERT INTO directories(
                root, path, parent_path, depth, direct_file_count, recursive_file_count,
                direct_size, recursive_size, modified_time, scan_id, category, signals
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["root"],
                    r["path"],
                    r["parent_path"],
                    r["depth"],
                    r.get("direct_file_count", 0),
                    r.get("recursive_file_count", 0),
                    r.get("direct_size", 0),
                    r.get("recursive_size", 0),
                    r.get("modified_time"),
                    r["scan_id"],
                    r.get("category", ""),
                    r.get("signals", ""),
                )
                for r in rows
            ],
        )

    def log_error(self, scan_id: str, path: str, error_type: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO errors(scan_id, path, error_type, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (scan_id, normalize_path(path), error_type, message, now_iso()),
        )


# ============================================================
# Scanner
# ============================================================


class Scanner:
    def __init__(self, db: Database, config: dict[str, Any]):
        self.db = db
        self.config = config
        scan_cfg = config["scan"]
        self.exclude_dirs = {x.lower() for x in scan_cfg.get("exclude_dirs", [])}
        self.exclude_files = {x.lower() for x in scan_cfg.get("exclude_files", [])}
        self.max_depth = scan_cfg.get("max_depth")
        self.follow_symlinks = bool(scan_cfg.get("follow_symlinks", False))
        self.batch_size = 1000

    def run(self) -> str:
        scan_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        self.db.start_scan(scan_id, config_hash(self.config))
        logging.info("[scan] started scan_id=%s", scan_id)

        total_files = 0
        total_dirs = 0
        total_size = 0
        error_count = 0
        file_batch: list[FileRecord] = []
        dir_rows: dict[str, dict[str, Any]] = {}

        try:
            roots = self.config["scan"].get("roots", [])
            if not roots:
                raise RuntimeError(
                    "No scan roots configured. Add scan.roots to config.yaml."
                )
            for root in roots:
                root_path = Path(root)
                if not root_path.exists() or not root_path.is_dir():
                    self.db.log_error(
                        scan_id,
                        root,
                        "RootError",
                        "Root not found or is not a directory",
                    )
                    error_count += 1
                    continue
                root_norm = normalize_path(root_path)
                stack: list[tuple[Path, int]] = [(root_path, 0)]

                while stack:
                    current, depth = stack.pop()
                    current_norm = normalize_path(current)
                    if self.max_depth is not None and depth > int(self.max_depth):
                        continue
                    try:
                        stat = current.stat()
                        modified = datetime.fromtimestamp(stat.st_mtime).isoformat(
                            timespec="seconds"
                        )
                    except OSError:
                        modified = None
                    dir_rows.setdefault(
                        current_norm,
                        {
                            "root": root_norm,
                            "path": current_norm,
                            "parent_path": normalize_path(current.parent),
                            "depth": depth,
                            "direct_file_count": 0,
                            "recursive_file_count": 0,
                            "direct_size": 0,
                            "recursive_size": 0,
                            "modified_time": modified,
                            "scan_id": scan_id,
                            "category": "",
                            "signals": "",
                        },
                    )
                    total_dirs += 1

                    # ── Folder-level addon detection ────────────────────
                    # If the path lives in a Blender context AND the dir
                    # contains __init__.py with bl_info, treat the entire
                    # folder as a single addon unit — skip per-file scan.
                    if is_blender_context(current_norm):
                        addon_ok, addon_sigs = probe_addon_folder(current)
                        if addon_ok:
                            sub_files, sub_size = quick_dir_stats(
                                current,
                                self.follow_symlinks,
                            )
                            dir_row = dir_rows[current_norm]
                            dir_row["category"] = "blender_addon_folders"
                            dir_row["signals"] = ",".join(addon_sigs)
                            dir_row["recursive_file_count"] = sub_files
                            dir_row["recursive_size"] = sub_size

                            # Virtual file record for reports / plans
                            addon_record = FileRecord(
                                root=root_norm,
                                path=current_norm,
                                parent_path=normalize_path(current.parent),
                                name=current.name,
                                stem=current.name,
                                normalized_stem=normalize_stem(current.name),
                                extension="[addon_folder]",
                                size=sub_size,
                                size_mb=size_mb(sub_size),
                                modified_time=modified,
                                created_time=None,
                                depth=depth,
                                category="blender_addon_folders",
                                signals=",".join(addon_sigs),
                                scan_id=scan_id,
                            )
                            file_batch.append(addon_record)
                            total_files += 1
                            total_size += sub_size

                            # Propagate size to ancestors
                            ancestor = current.parent
                            while True:
                                anc_norm = normalize_path(ancestor)
                                anc_row = dir_rows.get(anc_norm)
                                if anc_row is not None:
                                    anc_row["recursive_file_count"] += sub_files
                                    anc_row["recursive_size"] += sub_size
                                if anc_norm == root_norm or ancestor.parent == ancestor:
                                    break
                                ancestor = ancestor.parent

                            logging.info(
                                "[scan] addon folder: %s (%d files, %s)",
                                current_norm,
                                sub_files,
                                format_size(sub_size),
                            )
                            continue  # ← skip per-file scanning
                    # ── end addon detection ──────────────────────────────

                    try:
                        with os.scandir(current) as entries:
                            for entry in entries:
                                try:
                                    entry_path = Path(entry.path)
                                    entry_name_lower = entry.name.lower()
                                    is_symlink = entry.is_symlink()

                                    if entry.is_dir(
                                        follow_symlinks=self.follow_symlinks
                                    ):
                                        if entry_name_lower in self.exclude_dirs:
                                            continue
                                        if is_symlink and not self.follow_symlinks:
                                            continue
                                        stack.append((entry_path, depth + 1))
                                        continue

                                    if not entry.is_file(
                                        follow_symlinks=self.follow_symlinks
                                    ):
                                        continue
                                    if entry_name_lower in self.exclude_files:
                                        continue
                                    if is_symlink and not self.follow_symlinks:
                                        continue

                                    st = entry.stat(
                                        follow_symlinks=self.follow_symlinks
                                    )
                                    size = int(st.st_size)
                                    ext = entry_path.suffix.lower()
                                    category, signals = classify_file(
                                        str(entry_path), ext
                                    )
                                    parent_norm = normalize_path(entry_path.parent)
                                    modified_time = datetime.fromtimestamp(
                                        st.st_mtime
                                    ).isoformat(timespec="seconds")
                                    created_time = datetime.fromtimestamp(
                                        st.st_ctime
                                    ).isoformat(timespec="seconds")
                                    hidden = entry.name.startswith(".") or bool(
                                        getattr(st, "st_file_attributes", 0) & 2
                                    )

                                    record = FileRecord(
                                        root=root_norm,
                                        path=normalize_path(entry_path),
                                        parent_path=parent_norm,
                                        name=entry.name,
                                        stem=entry_path.stem,
                                        normalized_stem=normalize_stem(entry_path.stem),
                                        extension=ext,
                                        size=size,
                                        size_mb=size_mb(size),
                                        modified_time=modified_time,
                                        created_time=created_time,
                                        depth=depth + 1,
                                        category=category,
                                        signals=",".join(signals),
                                        is_hidden=hidden,
                                        is_symlink=is_symlink,
                                        scan_id=scan_id,
                                    )
                                    file_batch.append(record)
                                    total_files += 1
                                    total_size += size

                                    parent_row = dir_rows[current_norm]
                                    parent_row["direct_file_count"] += 1
                                    parent_row["direct_size"] += size

                                    ancestor = entry_path.parent
                                    while True:
                                        ancestor_norm = normalize_path(ancestor)
                                        row = dir_rows.get(ancestor_norm)
                                        if row is not None:
                                            row["recursive_file_count"] += 1
                                            row["recursive_size"] += size
                                        if (
                                            normalize_path(ancestor) == root_norm
                                            or ancestor.parent == ancestor
                                        ):
                                            break
                                        ancestor = ancestor.parent

                                    if len(file_batch) >= self.batch_size:
                                        self.db.insert_files(file_batch)
                                        self.db.conn.commit()
                                        file_batch.clear()

                                    if total_files and total_files % 5000 == 0:
                                        logging.info(
                                            "[scan] files: %s dirs: %s size: %s current: %s",
                                            total_files,
                                            total_dirs,
                                            format_size(total_size),
                                            current_norm,
                                        )
                                except (OSError, PermissionError) as exc:
                                    error_count += 1
                                    self.db.log_error(
                                        scan_id,
                                        getattr(entry, "path", str(current)),
                                        type(exc).__name__,
                                        str(exc),
                                    )
                    except (OSError, PermissionError) as exc:
                        error_count += 1
                        self.db.log_error(
                            scan_id, current_norm, type(exc).__name__, str(exc)
                        )

            if file_batch:
                self.db.insert_files(file_batch)
                self.db.conn.commit()

            dir_values = list(dir_rows.values())
            for i in range(0, len(dir_values), self.batch_size):
                self.db.insert_directories(dir_values[i : i + self.batch_size])
                self.db.conn.commit()

            self.db.finish_scan(
                scan_id, "finished", total_files, total_dirs, total_size, error_count
            )
            logging.info(
                "[scan] finished files=%s dirs=%s size=%s errors=%s",
                total_files,
                total_dirs,
                format_size(total_size),
                error_count,
            )
            return scan_id
        except Exception:
            self.db.finish_scan(
                scan_id, "failed", total_files, total_dirs, total_size, error_count
            )
            logging.exception("[scan] failed")
            raise


# ============================================================
# Reports
# ============================================================


class ReportBuilder:
    def __init__(
        self, db: Database, config: dict[str, Any], scan_id: str | None = None
    ):
        self.db = db
        self.config = config
        self.scan_id = scan_id or db.latest_scan_id()
        self.dirs = output_dirs(config)

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(x) for x in self.db.conn.execute(sql, params).fetchall()]

    def largest_files(self) -> list[dict[str, Any]]:
        limit = int(self.config["analysis"]["top_largest_files"])
        return self.rows(
            """
            SELECT path, size, size_mb, extension, category, modified_time
            FROM files WHERE scan_id = ?
            ORDER BY size DESC LIMIT ?
            """,
            (self.scan_id, limit),
        )

    def largest_dirs(self) -> list[dict[str, Any]]:
        limit = int(self.config["analysis"]["top_largest_dirs"])
        return self.rows(
            """
            SELECT path, recursive_size AS size, ROUND(recursive_size / 1024.0 / 1024.0, 2) AS size_mb,
                   recursive_file_count AS file_count, depth
            FROM directories WHERE scan_id = ?
            ORDER BY recursive_size DESC LIMIT ?
            """,
            (self.scan_id, limit),
        )

    def extension_stats(self) -> list[dict[str, Any]]:
        return self.rows(
            """
            SELECT COALESCE(NULLIF(extension, ''), '[no_ext]') AS extension,
                   COUNT(*) AS count,
                   SUM(size) AS total_size,
                   ROUND(SUM(size) / 1024.0 / 1024.0, 2) AS total_size_mb
            FROM files WHERE scan_id = ?
            GROUP BY extension
            ORDER BY total_size DESC
            """,
            (self.scan_id,),
        )

    def category_stats(self) -> list[dict[str, Any]]:
        return self.rows(
            """
            SELECT category, COUNT(*) AS count, SUM(size) AS total_size,
                   ROUND(SUM(size) / 1024.0 / 1024.0, 2) AS total_size_mb
            FROM files WHERE scan_id = ?
            GROUP BY category
            ORDER BY total_size DESC
            """,
            (self.scan_id,),
        )

    def root_stats(self) -> list[dict[str, Any]]:
        return self.rows(
            """
            SELECT root, COUNT(*) AS file_count, SUM(size) AS total_size,
                   ROUND(SUM(size) / 1024.0 / 1024.0, 2) AS total_size_mb
            FROM files WHERE scan_id = ?
            GROUP BY root
            ORDER BY total_size DESC
            """,
            (self.scan_id,),
        )

    def duplicate_candidates(self) -> list[dict[str, Any]]:
        min_size = int(
            float(self.config["analysis"].get("min_duplicate_size_mb", 1)) * 1024 * 1024
        )
        groups = self.rows(
            """
            SELECT normalized_stem, extension, size, COUNT(*) AS count,
                   ROUND(((COUNT(*) - 1) * size) / 1024.0 / 1024.0, 2) AS potential_wasted_mb
            FROM files
            WHERE scan_id = ? AND size >= ? AND normalized_stem <> ''
            GROUP BY normalized_stem, extension, size
            HAVING count > 1
            ORDER BY (size * count) DESC
            LIMIT 500
            """,
            (self.scan_id, min_size),
        )
        result: list[dict[str, Any]] = []
        for idx, group in enumerate(groups, start=1):
            files = self.rows(
                """
                SELECT path, name, size, size_mb, extension, category, modified_time
                FROM files
                WHERE scan_id = ? AND normalized_stem = ? AND extension = ? AND size = ?
                ORDER BY modified_time DESC
                """,
                (
                    self.scan_id,
                    group["normalized_stem"],
                    group["extension"],
                    group["size"],
                ),
            )
            result.append(
                {
                    "group_id": f"dup_meta_{idx:04d}",
                    "confidence": "medium",
                    "reason": "same normalized name + same size + same extension; metadata-only candidate",
                    "normalized_stem": group["normalized_stem"],
                    "extension": group["extension"],
                    "size": group["size"],
                    "count": group["count"],
                    "potential_wasted_mb": group["potential_wasted_mb"],
                    "files": files,
                }
            )
        return result

    def version_candidates(self) -> list[dict[str, Any]]:
        min_total = int(
            float(self.config["analysis"].get("min_version_group_size_mb", 5))
            * 1024
            * 1024
        )
        groups = self.rows(
            """
            SELECT normalized_stem, extension, COUNT(*) AS count, SUM(size) AS total_size,
                   ROUND(SUM(size) / 1024.0 / 1024.0, 2) AS total_size_mb
            FROM files
            WHERE scan_id = ? AND normalized_stem <> ''
            GROUP BY normalized_stem, extension
            HAVING count > 1 AND total_size >= ?
            ORDER BY total_size DESC
            LIMIT 500
            """,
            (self.scan_id, min_total),
        )
        result: list[dict[str, Any]] = []
        for idx, group in enumerate(groups, start=1):
            files = self.rows(
                """
                SELECT path, name, size, size_mb, extension, category, modified_time
                FROM files
                WHERE scan_id = ? AND normalized_stem = ? AND extension = ?
                ORDER BY modified_time DESC, size DESC
                """,
                (self.scan_id, group["normalized_stem"], group["extension"]),
            )
            has_signal = any(
                VERSION_RE.search(f["name"])
                or YEAR_RE.search(f["name"])
                or COPY_RE.search(f["name"])
                or QUANT_RE.search(f["name"])
                for f in files
            )
            if not has_signal:
                continue
            reason = "possible versions/copies by normalized name"
            if group["extension"] in AI_MODEL_EXTENSIONS:
                reason = "possible AI model versions/quantizations; not duplicates; manual review required"
            result.append(
                {
                    "group_id": f"ver_{idx:04d}",
                    "base_name": group["normalized_stem"],
                    "extension": group["extension"],
                    "reason": reason,
                    "confidence": "medium",
                    "risk": "high"
                    if group["extension"] in PROTECTED_REVIEW_EXTENSIONS
                    else "medium",
                    "total_size": group["total_size"],
                    "total_size_mb": group["total_size_mb"],
                    "files": files,
                }
            )
        return result

    def category_files(self, category: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.rows(
            """
            SELECT path, name, size, size_mb, extension, category, modified_time, signals
            FROM files WHERE scan_id = ? AND category = ?
            ORDER BY size DESC LIMIT ?
            """,
            (self.scan_id, category, limit),
        )

    def addon_folders(self) -> list[dict[str, Any]]:
        """Directories detected as Blender addons (folder-level entries)."""
        return self.rows(
            """
            SELECT path, recursive_size AS size,
                   ROUND(recursive_size / 1024.0 / 1024.0, 2) AS size_mb,
                   recursive_file_count AS file_count,
                   modified_time, category, signals
            FROM directories
            WHERE scan_id = ? AND category = 'blender_addon_folders'
            ORDER BY recursive_size DESC
            LIMIT 200
            """,
            (self.scan_id,),
        )

    def archive_exact_duplicates(self) -> list[dict[str, Any]]:
        """Archive duplicates found by exact *name + extension + size*
        (no normalisation that strips version/quant signals)."""
        min_size = int(
            float(self.config["analysis"].get("min_duplicate_size_mb", 1)) * 1024 * 1024
        )
        archive_exts = sorted(ARCHIVE_EXTENSIONS)
        placeholders = ",".join("?" * len(archive_exts))
        groups = self.rows(
            f"""
            SELECT name, extension, size,
                   COUNT(*) AS count,
                   ROUND(((COUNT(*) - 1) * size) / 1024.0 / 1024.0, 2) AS potential_wasted_mb
            FROM files
            WHERE scan_id = ? AND size >= ? AND extension IN ({placeholders})
              AND extension NOT IN ('.blend', '.blend1', '.blend2')
            GROUP BY name, extension, size
            HAVING count > 1
            ORDER BY (size * count) DESC
            LIMIT 300
            """,
            (self.scan_id, min_size, *archive_exts),
        )
        result: list[dict[str, Any]] = []
        for idx, group in enumerate(groups, start=1):
            files = self.rows(
                """
                SELECT path, name, size, size_mb, extension, category, modified_time
                FROM files
                WHERE scan_id = ? AND name = ? AND extension = ? AND size = ?
                ORDER BY modified_time DESC
                """,
                (self.scan_id, group["name"], group["extension"], group["size"]),
            )
            result.append(
                {
                    "group_id": f"arc_dup_{idx:04d}",
                    "confidence": "high",
                    "reason": "exact archive name + same size (no extraction needed)",
                    "name": group["name"],
                    "extension": group["extension"],
                    "size": group["size"],
                    "count": group["count"],
                    "potential_wasted_mb": group["potential_wasted_mb"],
                    "files": files,
                }
            )
        return result

    def build(self) -> dict[str, Any]:
        scan = dict(
            self.db.conn.execute(
                "SELECT * FROM scan_runs WHERE scan_id = ?", (self.scan_id,)
            ).fetchone()
        )
        duplicates = self.duplicate_candidates()
        versions = self.version_candidates()
        ai_models = self.category_files("ai_models", 200)
        for item in ai_models:
            item.update(parse_ai_model_name(item["name"]))
        report = {
            "scan": scan,
            "root_stats": self.root_stats(),
            "largest_files": self.largest_files(),
            "largest_dirs": self.largest_dirs(),
            "extensions": self.extension_stats(),
            "categories": self.category_stats(),
            "duplicate_candidates": duplicates,
            "version_candidates": versions,
            "archive_exact_duplicates": self.archive_exact_duplicates(),
            "ai_models": ai_models,
            "blender_addons": self.category_files("blender_addons", 200),
            "blender_addon_folders": self.addon_folders(),
            "blender_projects": self.category_files("blender_projects", 100),
            "cache": self.category_files("cache", 200),
            "temporary": self.category_files("temporary", 200),
        }
        return report

    def write(self) -> dict[str, Any]:
        logging.info("[report] building report for scan_id=%s", self.scan_id)
        report = self.build()
        reports_dir = self.dirs["reports"]
        json_dump(reports_dir / "report.json", report)
        self.write_markdown(reports_dir / "report.md", report)
        write_csv(reports_dir / "largest_files.csv", report["largest_files"])
        write_csv(reports_dir / "largest_dirs.csv", report["largest_dirs"])
        write_csv(reports_dir / "extensions.csv", report["extensions"])
        write_csv(reports_dir / "categories.csv", report["categories"])
        write_csv(
            reports_dir / "duplicate_candidates.csv",
            flatten_groups(report["duplicate_candidates"], "duplicate_group"),
        )
        write_csv(
            reports_dir / "version_candidates.csv",
            flatten_groups(report["version_candidates"], "version_group"),
        )
        write_csv(
            reports_dir / "archive_exact_duplicates.csv",
            flatten_groups(
                report.get("archive_exact_duplicates", []), "archive_dup_group"
            ),
        )
        addon_folders = report.get("blender_addon_folders", [])
        if addon_folders:
            write_csv(reports_dir / "blender_addon_folders.csv", addon_folders)
        json_dump(self.dirs["scans"] / "scan_summary.json", report["scan"])
        logging.info("[done] report: %s", reports_dir / "report.md")
        return report

    def write_markdown(self, path: Path, report: dict[str, Any]) -> None:
        lines: list[str] = []
        scan = report["scan"]
        lines.append("# File Cleanup Report")
        lines.append("")
        lines.append("## Summary")
        lines.append(f"- Scan id: `{scan['scan_id']}`")
        lines.append(f"- Files: {scan['total_files']}")
        lines.append(f"- Directories: {scan['total_dirs']}")
        lines.append(f"- Total size: {format_size(scan['total_size'])}")
        lines.append(f"- Access/errors: {scan['error_count']}")
        lines.append("")
        lines.extend(
            markdown_table(
                "## Total size by root",
                report["root_stats"],
                ["root", "file_count", "total_size_mb"],
            )
        )
        lines.extend(
            markdown_table(
                "## Largest directories",
                report["largest_dirs"],
                ["size_mb", "file_count", "path"],
            )
        )
        lines.extend(
            markdown_table(
                "## Largest files",
                report["largest_files"],
                ["size_mb", "extension", "category", "path"],
            )
        )
        lines.extend(
            markdown_table(
                "## File types",
                report["extensions"],
                ["extension", "count", "total_size_mb"],
            )
        )
        lines.extend(
            markdown_table(
                "## Categories",
                report["categories"],
                ["category", "count", "total_size_mb"],
            )
        )
        lines.extend(
            markdown_group_summary(
                "## Duplicate candidates",
                report["duplicate_candidates"],
                "potential_wasted_mb",
            )
        )
        lines.extend(
            markdown_group_summary(
                "## Version candidates", report["version_candidates"], "total_size_mb"
            )
        )
        lines.extend(
            markdown_table(
                "## AI models",
                report["ai_models"],
                [
                    "size_mb",
                    "extension",
                    "quantization",
                    "model_size",
                    "model_type",
                    "path",
                ],
            )
        )
        lines.extend(
            markdown_table(
                "## Blender addons",
                report["blender_addons"],
                ["size_mb", "extension", "modified_time", "path"],
            )
        )
        lines.extend(
            markdown_table(
                "## Blender addon folders (detected)",
                report.get("blender_addon_folders", []),
                ["size_mb", "file_count", "signals", "path"],
            )
        )
        lines.extend(
            markdown_group_summary(
                "## Archive exact duplicates (name + size)",
                report.get("archive_exact_duplicates", []),
                "potential_wasted_mb",
            )
        )
        lines.append("## Move suggestions")
        lines.append("See `outputs/plans/move_plan.json` after `plan-move`.")
        lines.append("")
        lines.append("## Delete review suggestions")
        lines.append(
            "See `outputs/plans/delete_plan.json` after `plan-delete`. Metadata-only candidates require review."
        )
        lines.append("")
        lines.append("## Manual review required")
        lines.append(
            "Projects, documents, .blend files, media and AI models are never marked for automatic deletion by metadata only."
        )
        lines.append("")
        lines.append("## Next actions")
        lines.append("1. Review CSV files in Excel.")
        lines.append("2. Run `plan-move` and `plan-delete`.")
        lines.append("3. Run `analyze` if local LLM is available.")
        lines.append("4. Apply only with `--dry-run` first.")
        path.write_text("\n".join(lines), encoding="utf-8")


def markdown_table(
    title: str, rows: list[dict[str, Any]], fields: list[str], limit: int = 50
) -> list[str]:
    lines = [title, ""]
    if not rows:
        return lines + ["No data.", ""]
    lines.append("| " + " | ".join(fields) + " |")
    lines.append("|" + "|".join(["---" for _ in fields]) + "|")
    for row in rows[:limit]:
        values = []
        for field in fields:
            value = row.get(field, "")
            if field == "path" or field.endswith("path") or field == "root":
                value = f"`{value}`"
            values.append(str(value))
        lines.append("| " + " | ".join(values) + " |")
    lines.append("")
    return lines


def markdown_group_summary(
    title: str, groups: list[dict[str, Any]], size_field: str, limit: int = 30
) -> list[str]:
    lines = [title, ""]
    if not groups:
        return lines + ["No candidates.", ""]
    lines.append("| Group | Confidence | Size MB | Count | Reason |")
    lines.append("|---|---|---:|---:|---|")
    for group in groups[:limit]:
        lines.append(
            f"| `{group.get('group_id')}` | {group.get('confidence', '')} | {group.get(size_field, '')} | "
            f"{group.get('count', len(group.get('files', [])))} | {group.get('reason', '')} |"
        )
    lines.append("")
    return lines


def flatten_groups(
    groups: list[dict[str, Any]], group_field: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in groups:
        for file in group.get("files", []):
            row = dict(file)
            row[group_field] = group.get("group_id")
            row["reason"] = group.get("reason")
            row["confidence"] = group.get("confidence")
            row["risk"] = group.get("risk")
            row["group_size_mb"] = group.get("total_size_mb") or group.get(
                "potential_wasted_mb"
            )
            rows.append(row)
    return rows


# ============================================================
# Plans / PowerShell / actions
# ============================================================


class Planner:
    def __init__(self, config: dict[str, Any], report: dict[str, Any]):
        self.config = config
        self.report = report
        self.dirs = output_dirs(config)
        self.central = config["output"].get("central_folders", {})

    def build_move_plan(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for file in self.report.get("blender_addons", []):
            target_root = self.central.get(
                "blender_addons_archives", "G:/Blender/Addons/Archives"
            )
            if not is_under(file["path"], target_root):
                items.append(
                    {
                        "source": file["path"],
                        "target": normalize_path(
                            Path(target_root)
                            / file.get("name", Path(file["path"]).name)
                        ),
                        "reason": "Blender addon archive/script found outside centralized Addons structure",
                        "confidence": "medium",
                        "risk": "low",
                        "action": "move",
                    }
                )
        # Folder-level addon directories detected by __init__.py probe
        for folder in self.report.get("blender_addon_folders", []):
            target_root = self.central.get(
                "blender_addons_installed", "G:/Blender/Addons/Installed"
            )
            if not is_under(folder["path"], target_root):
                items.append(
                    {
                        "source": folder["path"],
                        "target": normalize_path(
                            Path(target_root) / Path(folder["path"]).name
                        ),
                        "reason": "Blender addon folder (detected by __init__.py + bl_info)",
                        "confidence": "high",
                        "risk": "medium",
                        "action": "move",
                    }
                )
        for category, target_key, reason in [
            ("ai_models", "ai_models", "AI model found outside central models folder"),
            (
                "installers",
                "installers",
                "Installer can be centralized for later review",
            ),
            ("archives", "archives", "Archive can be centralized for later review"),
        ]:
            target_root = self.central.get(target_key)
            if not target_root:
                continue
            for file in self._category_rows(category, 200):
                if not is_under(file["path"], target_root):
                    items.append(
                        {
                            "source": file["path"],
                            "target": normalize_path(
                                Path(target_root)
                                / file.get("name", Path(file["path"]).name)
                            ),
                            "reason": reason,
                            "confidence": "low"
                            if category == "ai_models"
                            else "medium",
                            "risk": "medium" if category == "ai_models" else "low",
                            "action": "move",
                        }
                    )
        plan = {"created_at": now_iso(), "items": items}
        json_dump(self.dirs["plans"] / "move_plan.json", plan)
        write_apply_move_ps1(self.dirs["plans"] / "apply_move.ps1")
        return plan

    def build_delete_plan(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for group in self.report.get("duplicate_candidates", []):
            files = group.get("files", [])
            if len(files) < 2:
                continue
            keep = files[0]
            for file in files[1:]:
                risk = (
                    "high"
                    if file.get("extension") in PROTECTED_REVIEW_EXTENSIONS
                    else "medium"
                )
                items.append(
                    {
                        "path": file["path"],
                        "size_mb": file["size_mb"],
                        "reason": "Possible duplicate with same normalized name, extension and size; keep candidate: "
                        + keep["path"],
                        "confidence": "medium",
                        "risk": risk,
                        "action": "review_delete",
                    }
                )
        for category in ["cache", "temporary"]:
            for file in self._category_rows(category, 300):
                items.append(
                    {
                        "path": file["path"],
                        "size_mb": file["size_mb"],
                        "reason": "Cache/temp/build-like artifact by path or extension",
                        "confidence": "medium",
                        "risk": "medium",
                        "action": "review_delete",
                    }
                )
        total = round(sum(float(x.get("size_mb") or 0) for x in items), 2)
        plan = {
            "created_at": now_iso(),
            "mode": "review_only",
            "total_potential_size_mb": total,
            "items": items,
        }
        json_dump(self.dirs["plans"] / "delete_plan.json", plan)
        write_apply_delete_ps1(
            self.dirs["plans"] / "apply_delete.ps1",
            self.config["output"].get("quarantine_root", "G:/__cleanup_quarantine"),
        )
        self.write_cleanup_plan(plan)
        return plan

    def _category_rows(self, category: str, limit: int) -> list[dict[str, Any]]:
        if category in self.report:
            return self.report[category][:limit]
        # report.json does not keep every category list; use largest_files as a compact fallback.
        return [
            x
            for x in self.report.get("largest_files", [])
            if x.get("category") == category
        ][:limit]

    def write_cleanup_plan(self, delete_plan: dict[str, Any]) -> None:
        lines = ["# Cleanup plan", "", "## Delete review", ""]
        lines.append(
            f"Potential size after review: {delete_plan['total_potential_size_mb']} MB"
        )
        lines.append("")
        for item in delete_plan["items"][:100]:
            lines.append(
                f"- `{item['path']}` — {item['size_mb']} MB — {item['reason']} ({item['risk']})"
            )
        (self.dirs["plans"] / "cleanup_plan.md").write_text(
            "\n".join(lines), encoding="utf-8"
        )


def is_under(path: str, folder: str) -> bool:
    p = normalize_path(path).lower().rstrip("/")
    f = normalize_path(folder).lower().rstrip("/")
    return bool(f) and (p == f or p.startswith(f + "/"))


def write_apply_move_ps1(path: Path) -> None:
    path.write_text(
        r"""param(
  [string]$PlanPath = "move_plan.json",
  [switch]$Apply
)
$ErrorActionPreference = "Continue"
$plan = Get-Content $PlanPath -Raw | ConvertFrom-Json
foreach ($item in $plan.items) {
  $source = $item.source
  $target = $item.target
  if (!(Test-Path -LiteralPath $source)) { Write-Host "MISS $source"; continue }
  $dir = Split-Path -Parent $target
  if ($Apply) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  $final = $target
  $i = 1
  while (Test-Path -LiteralPath $final) {
    $base = [System.IO.Path]::GetFileNameWithoutExtension($target)
    $ext = [System.IO.Path]::GetExtension($target)
    $final = Join-Path $dir ("$base.$i$ext")
    $i++
  }
  if ($Apply) { Move-Item -LiteralPath $source -Destination $final; Write-Host "MOVED $source -> $final" }
  else { Write-Host "DRY-RUN MOVE $source -> $final" }
}
""",
        encoding="utf-8",
    )


def write_apply_delete_ps1(path: Path, quarantine_root: str) -> None:
    escaped = quarantine_root.replace("'", "''")
    path.write_text(
        rf"""param(
  [string]$PlanPath = "delete_plan.json",
  [switch]$ApplyDelete
)
$ErrorActionPreference = "Continue"
$plan = Get-Content $PlanPath -Raw | ConvertFrom-Json
$quarantine = Join-Path '{escaped}' (Get-Date -Format 'yyyy-MM-dd')
foreach ($item in $plan.items) {{
  $source = $item.path
  if (!(Test-Path -LiteralPath $source)) {{ Write-Host "MISS $source"; continue }}
  $relative = ($source -replace ':', '').TrimStart([char[]]@('\\','/'))
  $target = Join-Path $quarantine $relative
  $dir = Split-Path -Parent $target
  if ($ApplyDelete) {{ New-Item -ItemType Directory -Force -Path $dir | Out-Null; Move-Item -LiteralPath $source -Destination $target; Write-Host "QUARANTINED $source -> $target" }}
  else {{ Write-Host "DRY-RUN QUARANTINE $source -> $target" }}
}}
""",
        encoding="utf-8",
    )


def apply_move(plan_path: str, apply: bool) -> None:
    plan = read_json(plan_path)
    for item in plan.get("items", []):
        src = Path(item["source"])
        dst = Path(item["target"])
        if not src.exists():
            print(f"MISS {src}")
            continue
        final = unique_path(dst)
        if apply:
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(final))
            print(f"MOVED {src} -> {final}")
        else:
            print(f"DRY-RUN MOVE {src} -> {final}")


def apply_delete(plan_path: str, quarantine_root: str, apply_delete: bool) -> None:
    plan = read_json(plan_path)
    quarantine = Path(quarantine_root) / datetime.now().strftime("%Y-%m-%d")
    for item in plan.get("items", []):
        src = Path(item["path"])
        if not src.exists():
            print(f"MISS {src}")
            continue
        safe_relative = str(src).replace(":", "").lstrip("\\/")
        dst = unique_path(quarantine / safe_relative)
        if apply_delete:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            print(f"QUARANTINED {src} -> {dst}")
        else:
            print(f"DRY-RUN QUARANTINE {src} -> {dst}")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(1, 10000):
        candidate = path.with_name(f"{path.stem}.{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to build unique path for {path}")


# ============================================================
# Hash duplicates
# ============================================================


def partial_hash(path: str, size: int, block_size: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    h.update(str(size).encode("ascii"))
    with open(path, "rb") as f:
        h.update(f.read(block_size))
        if size > block_size:
            f.seek(max(0, size - block_size))
            h.update(f.read(block_size))
    return h.hexdigest()


def full_sha256(path: str, block_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block_size), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_duplicates(
    db: Database, config: dict[str, Any], scan_id: str | None = None
) -> list[dict[str, Any]]:
    scan_id = scan_id or db.latest_scan_id()
    min_size = int(float(config["analysis"].get("hash_min_size_mb", 1)) * 1024 * 1024)
    max_size = int(
        float(config["analysis"].get("hash_max_size_gb", 20)) * 1024 * 1024 * 1024
    )
    groups = db.conn.execute(
        """
        SELECT size FROM files WHERE scan_id = ? AND size BETWEEN ? AND ?
        GROUP BY size HAVING COUNT(*) > 1 ORDER BY size DESC
        """,
        (scan_id, min_size, max_size),
    ).fetchall()
    exact_groups: list[dict[str, Any]] = []
    for group in groups:
        rows = [
            dict(x)
            for x in db.conn.execute(
                "SELECT id, path, name, size, size_mb, extension FROM files WHERE scan_id = ? AND size = ?",
                (scan_id, group["size"]),
            ).fetchall()
        ]
        partials: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            try:
                ph = partial_hash(row["path"], row["size"])
                partials.setdefault(ph, []).append(row)
                db.conn.execute(
                    "UPDATE files SET partial_hash = ? WHERE id = ?", (ph, row["id"])
                )
            except OSError as exc:
                db.log_error(scan_id, row["path"], type(exc).__name__, str(exc))
        db.conn.commit()
        for ph, candidates in partials.items():
            if len(candidates) < 2:
                continue
            fulls: dict[str, list[dict[str, Any]]] = {}
            for row in candidates:
                try:
                    sha = full_sha256(row["path"])
                    fulls.setdefault(sha, []).append(row)
                    db.conn.execute(
                        "UPDATE files SET sha256 = ? WHERE id = ?", (sha, row["id"])
                    )
                except OSError as exc:
                    db.log_error(scan_id, row["path"], type(exc).__name__, str(exc))
            db.conn.commit()
            for sha, files in fulls.items():
                if len(files) > 1:
                    exact_groups.append(
                        {
                            "group_id": f"hash_{len(exact_groups) + 1:04d}",
                            "confidence": "high",
                            "reason": "same full SHA256",
                            "sha256": sha,
                            "partial_hash": ph,
                            "count": len(files),
                            "potential_wasted_mb": round(
                                (len(files) - 1) * files[0]["size"] / 1024 / 1024, 2
                            ),
                            "files": files,
                        }
                    )
    dirs = output_dirs(config)
    json_dump(dirs["reports"] / "exact_duplicate_candidates.json", exact_groups)
    write_csv(
        dirs["reports"] / "exact_duplicate_candidates.csv",
        flatten_groups(exact_groups, "hash_group"),
    )
    return exact_groups


# ============================================================
# LLM
# ============================================================

SYSTEM_PROMPT = """
Ты анализатор файловой системы.

Твоя задача — помочь пользователю освободить место и привести файлы в порядок.

Тебе запрещено:
- предлагать безусловно удалить файл, если уверенность низкая;
- считать разные квантизации AI-модели дубликатами;
- считать разные версии проекта дубликатами;
- предлагать удалять исходники, проекты, документы и рабочие файлы без ручной проверки;
- делать выводы по содержимому файла, потому что содержимое не предоставлено.

Ты можешь использовать только путь, имя, расширение, размер, дату изменения, группу кандидатов и категорию.
Всегда разделяй рекомендации на safe_to_review_delete, move_candidates, keep_candidates, manual_review, organization_suggestions.
Ответ возвращай строго в JSON.
""".strip()


class LLMClient:
    def __init__(self, config: dict[str, Any], raw_dir: Path):
        self.cfg = config["llm"]
        self.raw_dir = raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def chat(
        self,
        messages: list[dict[str, str]],
        response_format: str = "json",
        raw_name: str = "response.json",
    ) -> str:
        base_url = str(self.cfg["base_url"]).rstrip("/")
        url = base_url + "/chat/completions"
        payload = {
            "model": self.cfg["model"],
            "messages": messages,
            "temperature": self.cfg.get("temperature", 0.2),
            "max_tokens": self.cfg.get("max_tokens", 4096),
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        raw_prompt = json.dumps(payload, ensure_ascii=False)
        max_prompt = int(self.cfg.get("max_prompt_chars", 120_000))
        if len(raw_prompt) > max_prompt:
            raise RuntimeError(
                f"LLM prompt is too large: {len(raw_prompt)} > {max_prompt}"
            )
        headers = {"Content-Type": "application/json"}
        api_key = self.cfg.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        data = raw_prompt.encode("utf-8")
        retries = int(self.cfg.get("retries", 2))
        timeout = int(self.cfg.get("timeout_sec", 300))
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                request = urllib.request.Request(
                    url, data=data, headers=headers, method="POST"
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read().decode("utf-8")
                (self.raw_dir / raw_name).write_text(raw, encoding="utf-8")
                parsed = json.loads(raw)
                content = parsed["choices"][0]["message"]["content"]
                if response_format == "json":
                    json.loads(extract_json(content))
                return content
            except (
                urllib.error.URLError,
                TimeoutError,
                KeyError,
                json.JSONDecodeError,
                RuntimeError,
            ) as exc:
                last_error = exc
                logging.warning("[llm] attempt %s failed: %s", attempt + 1, exc)
                time.sleep(1 + attempt)
        raise RuntimeError(f"LLM request failed: {last_error}")


def extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if match:
        return match.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    raise json.JSONDecodeError("No JSON object found", text, 0)


def build_llm_chunks(report: dict[str, Any], max_items: int) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for chunk_type in [
        "largest_files",
        "largest_dirs",
        "duplicate_candidates",
        "version_candidates",
        "archive_exact_duplicates",
        "ai_models",
        "blender_addons",
        "blender_addon_folders",
        "cache",
    ]:
        items = report.get(chunk_type, [])
        for i in range(0, len(items), max_items):
            chunks.append({"chunk_type": chunk_type, "items": items[i : i + max_items]})
    return chunks


def analyze_with_llm(config: dict[str, Any]) -> None:
    dirs = output_dirs(config)
    report_path = dirs["reports"] / "report.json"
    if not report_path.exists():
        raise RuntimeError("report.json not found. Run report first.")
    report = read_json(report_path)
    if not config["llm"].get("enabled", True):
        logging.info("[llm] disabled")
        return
    client = LLMClient(config, dirs["chunks"])
    chunks = build_llm_chunks(
        report, int(config["analysis"].get("max_files_for_llm_chunk", 200))
    )
    partials: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, start=1):
        logging.info(
            "[llm] analyzing chunk %s/%s type=%s", idx, len(chunks), chunk["chunk_type"]
        )
        chunk_path = dirs["chunks"] / f"chunk_{idx:04d}.json"
        json_dump(chunk_path, chunk)
        prompt = (
            "Проанализируй список файлов/папок. Это только метаданные. "
            "Верни JSON с ключами chunk_summary, safe_to_review_delete, move_candidates, "
            "keep_candidates, manual_review, organization_suggestions, warnings.\n\nДанные:\n"
            + json.dumps(chunk, ensure_ascii=False)
        )
        try:
            content = client.chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format="json",
                raw_name=f"chunk_{idx:04d}.response.raw.json",
            )
            parsed = json.loads(extract_json(content))
            partials.append(
                {"chunk": idx, "chunk_type": chunk["chunk_type"], "analysis": parsed}
            )
            json_dump(dirs["chunks"] / f"chunk_{idx:04d}.response.json", parsed)
        except RuntimeError as exc:
            partials.append(
                {"chunk": idx, "chunk_type": chunk["chunk_type"], "error": str(exc)}
            )
    json_dump(dirs["llm"] / "partial_summaries.json", partials)

    final_prompt = (
        "Ты получил результаты нескольких частичных анализов файловой системы. "
        "Сделай итоговый план очистки. Верни JSON с ключами markdown_report и plan.\n\n"
        + json.dumps(
            {
                "scan": report.get("scan"),
                "categories": report.get("categories"),
                "partials": partials[:80],
            },
            ensure_ascii=False,
        )
    )
    content = client.chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": final_prompt},
        ],
        response_format="json",
        raw_name="final.response.raw.json",
    )
    final = json.loads(extract_json(content))
    markdown = (
        final.get("markdown_report")
        or "# Итоговый отчет очистки\n\nLLM did not return markdown_report."
    )
    (dirs["llm"] / "final_llm_report.md").write_text(markdown, encoding="utf-8")
    json_dump(dirs["llm"] / "final_llm_plan.json", final.get("plan", final))


# ============================================================
# CLI
# ============================================================


def db_from_config(config: dict[str, Any]) -> Database:
    dirs = output_dirs(config)
    return Database(dirs["scans"] / "file_index.sqlite")


def load_report_or_build(db: Database, config: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dirs(config)["reports"] / "report.json"
    if report_path.exists():
        return read_json(report_path)
    return ReportBuilder(db, config).write()


def cmd_scan(args: argparse.Namespace) -> None:
    config = load_config(args.config, args.env)
    setup_logging(config)
    with closing(db_from_config(config)) as db:
        scan_id = Scanner(db, config).run()
        print(f"[done] scan_id: {scan_id}")
        print(f"[done] db: {output_dirs(config)['scans'] / 'file_index.sqlite'}")


def cmd_report(args: argparse.Namespace) -> None:
    config = load_config(args.config, args.env)
    setup_logging(config)
    with closing(db_from_config(config)) as db:
        ReportBuilder(db, config, getattr(args, "scan_id", None)).write()


def cmd_analyze(args: argparse.Namespace) -> None:
    config = load_config(args.config, args.env)
    setup_logging(config)
    analyze_with_llm(config)


def cmd_plan_move(args: argparse.Namespace) -> None:
    config = load_config(args.config, args.env)
    setup_logging(config)
    with closing(db_from_config(config)) as db:
        plan = Planner(config, load_report_or_build(db, config)).build_move_plan()
        print(f"[done] move items: {len(plan['items'])}")
        print(f"[done] plan: {output_dirs(config)['plans'] / 'move_plan.json'}")


def cmd_plan_delete(args: argparse.Namespace) -> None:
    config = load_config(args.config, args.env)
    setup_logging(config)
    with closing(db_from_config(config)) as db:
        plan = Planner(config, load_report_or_build(db, config)).build_delete_plan()
        print(f"[done] delete review items: {len(plan['items'])}")
        print(f"[done] plan: {output_dirs(config)['plans'] / 'delete_plan.json'}")


def cmd_full(args: argparse.Namespace) -> None:
    config = load_config(args.config, args.env)
    setup_logging(config)
    with closing(db_from_config(config)) as db:
        Scanner(db, config).run()
        report = ReportBuilder(db, config).write()
        planner = Planner(config, report)
        planner.build_move_plan()
        planner.build_delete_plan()
    if config["llm"].get("enabled", True) and not getattr(args, "no_llm", False):
        analyze_with_llm(config)
    dirs = output_dirs(config)
    print("Готово.")
    print(f"Отчет: {dirs['reports'] / 'report.md'}")
    print(f"Планы: {dirs['plans']}")


def cmd_apply_move(args: argparse.Namespace) -> None:
    if args.apply and args.dry_run:
        raise SystemExit("Use either --dry-run or --apply, not both")
    apply_move(args.plan, apply=bool(args.apply))


def cmd_apply_delete(args: argparse.Namespace) -> None:
    if args.apply_delete and args.dry_run:
        raise SystemExit("Use either --dry-run or --apply-delete, not both")
    quarantine = args.quarantine or DEFAULT_CONFIG["output"]["quarantine_root"]
    apply_delete(args.plan, quarantine, apply_delete=bool(args.apply_delete))


def cmd_hash_duplicates(args: argparse.Namespace) -> None:
    config = load_config(args.config, args.env)
    setup_logging(config)
    with closing(db_from_config(config)) as db:
        groups = hash_duplicates(db, config, getattr(args, "scan_id", None))
        print(f"[done] exact duplicate groups: {len(groups)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Disk metadata cleaner with SQLite reports and local LLM analysis."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default="config.yaml", help="Path to config.yaml")
        p.add_argument("--env", default=".env", help="Path to legacy .env file")

    p = sub.add_parser("scan", help="Scan roots and write SQLite index")
    add_config(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("report", help="Build reports from SQLite index")
    add_config(p)
    p.add_argument("--scan-id", default=None)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("analyze", help="Run local LLM chunk analysis")
    add_config(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("full", help="scan -> report -> analyze -> plan")
    add_config(p)
    p.add_argument("--no-llm", action="store_true")
    p.set_defaults(func=cmd_full)

    p = sub.add_parser("plan-move", help="Build move_plan.json and apply_move.ps1")
    add_config(p)
    p.set_defaults(func=cmd_plan_move)

    p = sub.add_parser(
        "plan-delete", help="Build delete_plan.json and apply_delete.ps1"
    )
    add_config(p)
    p.set_defaults(func=cmd_plan_delete)

    p = sub.add_parser("apply-move", help="Apply/dry-run move plan")
    p.add_argument("--plan", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_apply_move)

    p = sub.add_parser("apply-delete", help="Move delete-plan items to quarantine")
    p.add_argument("--plan", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply-delete", action="store_true")
    p.add_argument("--quarantine", default=None)
    p.set_defaults(func=cmd_apply_delete)

    p = sub.add_parser(
        "hash-duplicates", help="Optional exact duplicate detection with SHA256"
    )
    add_config(p)
    p.add_argument("--scan-id", default=None)
    p.set_defaults(func=cmd_hash_duplicates)

    return parser


def main() -> None:
    commands = {
        "scan",
        "report",
        "analyze",
        "full",
        "plan-move",
        "plan-delete",
        "apply-move",
        "apply-delete",
        "hash-duplicates",
    }
    if (
        "-h" not in sys.argv[1:]
        and "--help" not in sys.argv[1:]
        and not any(arg in commands for arg in sys.argv[1:])
    ):
        # Backward-compatible default: running the script without a command
        # performs the full safe cycle and still reads paths from .env.
        sys.argv.insert(1, "full")

    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
