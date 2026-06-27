#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Blender Addon Manager
=====================

Combined fast scanner/sorter for Blender addons:
- scans multiple source/library folders;
- detects addon folders, .py addon files and addon archives;
- finds duplicates/older versions before moving;
- uses cheap filename/rule classification first;
- asks an OpenAI-compatible local LLM only for unresolved items;
- if the LLM is still unsure, performs a separate *fast archive probe* that
  lists archive entries and reads only tiny metadata snippets, without full
  extraction, then asks the LLM again.

Safe by default: creates a JSON plan and only moves files with --apply.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except OSError:
        pass
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Defaults compatible with the older sorter script
# ---------------------------------------------------------------------------

DEFAULT_ROOT = Path(r"f:\Install\Soft\3D\Blender\Blender Addons")
DEFAULT_SOURCE = DEFAULT_ROOT / "2 No-sorted"
DEFAULT_LLM_URL = "http://localhost:8080/v1/chat/completions"

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

TEXT_HINT_NAMES = {
    "__init__.py",
    "blender_manifest.toml",
    "manifest.toml",
    "readme.md",
    "readme.txt",
    "license.txt",
    "setup.py",
    "pyproject.toml",
}

QUICK_RULES: list[tuple[str, list[str]]] = [
    ("Trees&Plants", ["tree", "plant", "gscatter", "bagapie", "forest", "ivy", "botani", "foliage"]),
    ("Humans&Rigs", ["rig", "human", "mocap", "mixamo", "auto-rig", "car-rig", "makehuman", "bone", "skin"]),
    ("Material-Shading", ["material", "shader", "pbr", "substance", "decal", "grungit", "eevee", "manga", "comic", "cavity", "hdri", "texture pack"]),
    ("Buildings", ["city", "building", "archipack", "road", "scene city", "postussr", "buildify", "architect", "house", "construction"]),
    ("Landscape", ["landscape", "terrain", "scatter", "gscatter", "real snow", "ground", "rock", "grass", "environment"]),
    ("Transport", ["car", "vehicle", "transport", "traffic", "truck", "aircraft", "boat", "train", "wheel"]),
    ("Render-Camera-Lighting", ["photographer", "camera", "light", "ssgi", "ssrt", "pure sky", "puresky", "luxcore", "denois", "hdri", "flare", "render", "weather", "thunder", "eve ", "cycles", "eevee", "atmospheric", "fog", "volumetric"]),
    ("Archviz", ["archviz", "archipack", "interior", "furniture", "decoration", "room", "floor", "wall"]),
    ("Animation", ["animation", "anim", "rigify", "auto-rig", "motion", "timeline", "keyframe", "mograph", "simulation"]),
    ("Generators", ["generator", "array", "bagapie", "buildify", "city builder", "procedural", "scatter", "create", "spawn"]),
    ("VFX", ["flip", "fluid", "rbd", "fracture", "blaze", "ravage", "cloud", "smoke", "fire", "explosion", "destruction", "physics"]),
    ("Assets", ["kitops", "true-asset", "polyhaven", "bis", "asset", "library", "kitbash", "megascan", "quixel"]),
    ("Modeling-Hardsurfase", ["hardops", "hops", "boxcutter", "meshmachine", "fluent", "welder", "cablerator", "quick shape", "quickcurve", "grid modeler", "qblocker", "machin3", "rebevel", "knife", "bool", "cutter", "bevel", "chamfer", "extrude", "inset"]),
    ("Modeling-Organic-Retopology", ["retopo", "speedretopo", "instant mesh", "quadremesher", "polydamage", "retopoflow", "sculpt", "remesh", "topology", "unwrap", "uv"]),
    ("Clouds", ["cloud", "vdb", "volume", "atmosphere", "sky", "nebula", "fog"]),
    ("Import-Export-Management", ["fbx", "send2ue", "unreal", "better fbx", "datasmith", "gltf", "collada", "vmf", "xnalar", "import", "export", "convert", "pipeline", "bridge"]),
    ("Geonodes", ["geonode", "geometry node", "node group", "procedural", "sverchok", "parametric"]),
    ("Texturing-UV-Drawing", ["textool", "uv ", "texture", "tex ", "layer painter", "lilysurface", "bake", "painter", "drawing", "stencil", "projection"]),
    ("Particles", ["particle", "hair", "fur", "grass", "instance", "emitter"]),
    ("Cloth", ["cloth", "fabric", "sewing", "sew", "drape", "simulation", "softbody"]),
    ("System-Modifiers-Nodes-Menu-Interface", ["modifier", "system", "menu", "panel", "cleanpanel", "collection grid", "keymap", "shortcut", "pie menu", "addon manager"]),
    ("UI", ["ui ", "interface", "theme", "icon", "viewport", "hud", "overlay", "workspace"]),
    ("Optimise", ["optim", "lod", "decimate", "simplify", "reduce", "clean", "merge", "cleanup"]),
    ("Game", ["game", "godot", "unity", "engine", "collision", "navmesh", "level design"]),
    ("Video-Editor", ["video", "vse", "sequence", "post fx", "postfx", "edit", "cut", "transition", "subtitle"]),
    ("Alignment-Closing", ["align", "snap", "distribute", "array", "mirror", "symmetry", "origin", "pivot"]),
    ("Scripts-Arrays", ["script", "addon template", "batch", "array tool", "automation", "macro", "template"]),
    ("Internet", ["blenderkit", "megascan", "bridge", "online", "download", "sync", "cloud", "repository"]),
    ("Bake", ["bake", "simplebake", "pbr bake", "texture bake", "lightmap", "normal bake"]),
]

KNOWN_ADDON_PATTERNS: dict[str, str] = {
    "gscatter": r"gscatter[_\s-]*(\d+(?:\.\d+)*)",
    "meshmachine": r"meshmachine[_\s-]*(\d+(?:\.\d+)*)",
    "hardops": r"(?:hardops|hops)[_\s\.]*(\d+(?:\.\d+)*)",
    "boxcutter": r"boxcutter[_\s\.]*(\d+(?:\.\d+)*)",
    "photographer": r"photographer[_\s-]*(\d+(?:\.\d+)*)",
    "bagapie": r"bagapie[_\s-]*(\d+(?:\.\d+)*)",
    "simplebake": r"simplebake[_\s-]*(\d+(?:\.\d+)*)",
    "nodepreview": r"nodepreview[_\s-]*(\d+(?:\.\d+)*)",
    "archipack": r"archipack[_\s-]*(\d+(?:\.\d+)*)",
    "blenderkit": r"blenderkit[_\s-]*(\d+(?:\.\d+)*)",
    "decalmachine": r"decal\s*machine[_\s-]*(\d+(?:\.\d+)*)",
    "cablerator": r"cablerator[_\s-]*(\d+(?:\.\d+)*)",
    "retopoflow": r"retopoflow[_\s-]*(\d+(?:\.\d+)*)",
    "send2ue": r"send2ue[_\s-]*(\d+(?:\.\d+)*)",
    "carrig": r"car[-\s]*rig[_\s]*pro[_\s-]*(\d+(?:\.\d+)*)",
    "fluent": r"fluent[_\s-]*(\d+(?:\.\d+)*)",
    "kitops": r"kitops[_\s-]*(\d+(?:\.\d+)*)",
    "auto-rig": r"auto[-\s]*rig[_\s]*pro[_\s-]*(\d+(?:\.\d+)*)",
}

GENERIC_VERSION_RE = re.compile(
    r"(?i)(?:^|[\s._\-\[\]()])(?:v|ver|version)?\s*([0-9]+(?:[._\-][0-9]+){1,4})(?:$|[\s._\-\[\]()])"
)
COPY_RE = re.compile(r"(?i)\b(copy|копия|backup|bak|old|старый|duplicate|дубликат|fixed|crack|nulled|final|latest|new)\b|\(\d+\)")


@dataclass(slots=True)
class AddonItem:
    path: str
    name: str
    source_role: str  # source/library
    kind: str  # archive/folder/script/other
    size: int
    modified_time: str | None
    addon_id: str | None = None
    addon_name: str | None = None
    version: str | None = None
    category: str | None = None
    confidence: str = "low"
    action: str = "unsure"  # move/duplicate/keep/unsure
    target: str | None = None
    reason: str = ""
    duplicate_group: str | None = None
    keep_path: str | None = None
    signals: list[str] = field(default_factory=list)
    archive_probe: dict[str, Any] | None = None

    @property
    def path_obj(self) -> Path:
        return Path(self.path)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def normalize_path(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def format_size(size: int) -> str:
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.2f} {units[idx]}"


def safe_stat(path: Path) -> tuple[int, str | None]:
    try:
        st = path.stat()
        return int(st.st_size), datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    except OSError:
        return 0, None


def version_tuple(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", version))


def normalize_addon_key(text: str) -> str:
    text = text.lower()
    text = Path(text).stem if "." in Path(text).name else text
    for _, pattern in KNOWN_ADDON_PATTERNS.items():
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = GENERIC_VERSION_RE.sub(" ", text)
    text = COPY_RE.sub(" ", text)
    text = re.sub(r"(?i)\b(blender|addon|add-on|plugin|cracked|win|mac|linux|x64)\b", " ", text)
    text = re.sub(r"[\s._\-\[\](){}]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quick_classify(text: str) -> tuple[str | None, str | None]:
    lowered = text.lower()
    for folder, keywords in QUICK_RULES:
        for keyword in keywords:
            if keyword.lower() in lowered:
                return folder, f"keyword:{keyword}"
    return None, None


def detect_known_addon(name: str) -> tuple[str | None, str | None]:
    for addon_id, pattern in KNOWN_ADDON_PATTERNS.items():
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            return addon_id, match.group(1)
    return None, None


def detect_generic_version(name: str) -> str | None:
    known_id, known_ver = detect_known_addon(name)
    if known_id and known_ver:
        return known_ver
    matches = GENERIC_VERSION_RE.findall(name)
    return matches[-1].replace("_", ".").replace("-", ".") if matches else None


def file_count_and_size(folder: Path, max_depth: int = 12) -> tuple[int, int]:
    count = 0
    total = 0
    stack: list[tuple[Path, int]] = [(folder, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            count += 1
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False) and depth < max_depth:
                            stack.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return count, total


# ---------------------------------------------------------------------------
# Metadata parsing: folders/scripts/archive snippets
# ---------------------------------------------------------------------------


def extract_bl_info(text: str) -> dict[str, Any]:
    """Parse a small Python snippet and extract literal bl_info if possible."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "bl_info" in names:
                try:
                    value = ast.literal_eval(node.value)
                    return value if isinstance(value, dict) else {}
                except (ValueError, SyntaxError):
                    return {}
    return {}


def parse_manifest_toml(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("id", "name", "version", "tagline", "maintainer", "type", "category"):
        match = re.search(rf'(?im)^\s*{re.escape(key)}\s*=\s*["\']([^"\']+)["\']', text)
        if match:
            result[key] = match.group(1).strip()
    return result


def read_text_head(path: Path, max_bytes: int = 16384) -> str:
    try:
        with path.open("rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def apply_text_metadata(item: AddonItem, text: str, source: str) -> None:
    if not text:
        return
    if "bl_info" in text:
        info = extract_bl_info(text)
        if info:
            item.signals.append(f"{source}:bl_info")
            item.addon_name = item.addon_name or str(info.get("name") or "").strip() or None
            version = info.get("version")
            if isinstance(version, (tuple, list)):
                item.version = item.version or ".".join(str(x) for x in version)
            elif isinstance(version, str):
                item.version = item.version or version
            category = str(info.get("category") or "").strip()
            if category and not item.category:
                quick, _ = quick_classify(category)
                item.category = quick or category
    if "blender_version_min" in text or "schema_version" in text or "tagline" in text:
        manifest = parse_manifest_toml(text)
        if manifest:
            item.signals.append(f"{source}:manifest")
            item.addon_id = item.addon_id or manifest.get("id")
            item.addon_name = item.addon_name or manifest.get("name")
            item.version = item.version or manifest.get("version")
            if not item.category:
                quick, _ = quick_classify(" ".join(manifest.values()))
                item.category = quick


def probe_addon_folder(path: Path) -> tuple[bool, dict[str, Any]]:
    manifest_path = path / "blender_manifest.toml"
    init_path = path / "__init__.py"
    data: dict[str, Any] = {"texts": {}, "signals": []}
    if manifest_path.exists():
        text = read_text_head(manifest_path)
        data["texts"]["blender_manifest.toml"] = text[:4096]
        data["signals"].append("folder_has_manifest")
        return True, data
    if init_path.exists():
        text = read_text_head(init_path)
        data["texts"]["__init__.py"] = text[:4096]
        if "bl_info" in text:
            data["signals"].append("folder_has_bl_info")
            return True, data
        if "def register" in text and "def unregister" in text:
            data["signals"].append("folder_has_register_pair")
            return True, data
    return False, data


# ---------------------------------------------------------------------------
# Fast archive probing - no full extraction
# ---------------------------------------------------------------------------


def is_text_hint(entry_name: str) -> bool:
    lowered = Path(entry_name).name.lower()
    if lowered in TEXT_HINT_NAMES:
        return True
    return lowered.endswith(".py") and Path(entry_name).name == "__init__.py"


def probe_zip(path: Path, max_entries: int, max_text_files: int, max_text_bytes: int) -> dict[str, Any]:
    entries: list[str] = []
    texts: dict[str, str] = {}
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        for info in infos[:max_entries]:
            entries.append(info.filename)
        for info in infos:
            if len(texts) >= max_text_files:
                break
            if info.is_dir() or not is_text_hint(info.filename):
                continue
            try:
                with zf.open(info) as f:
                    texts[info.filename] = f.read(max_text_bytes).decode("utf-8", errors="replace")
            except (OSError, RuntimeError, zipfile.BadZipFile):
                continue
    return {"method": "zip", "entries": entries, "texts": texts, "truncated": len(entries) >= max_entries}


def probe_tar(path: Path, max_entries: int, max_text_files: int, max_text_bytes: int) -> dict[str, Any]:
    entries: list[str] = []
    texts: dict[str, str] = {}
    with tarfile.open(path) as tf:
        members = tf.getmembers()
        for member in members[:max_entries]:
            entries.append(member.name)
        for member in members:
            if len(texts) >= max_text_files:
                break
            if not member.isfile() or not is_text_hint(member.name):
                continue
            try:
                f = tf.extractfile(member)
                if f is None:
                    continue
                with f:
                    texts[member.name] = f.read(max_text_bytes).decode("utf-8", errors="replace")
            except (OSError, tarfile.TarError):
                continue
    return {"method": "tar", "entries": entries, "texts": texts, "truncated": len(entries) >= max_entries}


def seven_zip_exe() -> str | None:
    for exe in ("7z", "7za", "7zz"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def probe_7z_listing(path: Path, max_entries: int, timeout: int) -> dict[str, Any]:
    exe = seven_zip_exe()
    if not exe:
        return {"method": "none", "entries": [], "texts": {}, "error": "7z executable not found"}
    proc = subprocess.run(
        [exe, "l", "-slt", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )
    entries: list[str] = []
    for line in proc.stdout.splitlines():
        if line.startswith("Path = "):
            value = line[len("Path = ") :].strip()
            if value and value != str(path):
                entries.append(value)
                if len(entries) >= max_entries:
                    break
    return {
        "method": "7z-list",
        "entries": entries,
        "texts": {},
        "truncated": len(entries) >= max_entries,
        "error": proc.stderr.strip()[:500] if proc.returncode else None,
    }


def fast_probe_archive(
    path: Path,
    max_entries: int = 80,
    max_text_files: int = 4,
    max_text_bytes: int = 8192,
    timeout: int = 5,
) -> dict[str, Any]:
    """Return cheap archive metadata. Never extracts the whole archive."""
    start = time.monotonic()
    ext = path.suffix.lower()
    try:
        if ext == ".zip":
            result = probe_zip(path, max_entries, max_text_files, max_text_bytes)
        elif ext in {".tar", ".tgz", ".gz", ".xz", ".bz2"} or path.name.lower().endswith((".tar.gz", ".tar.xz", ".tar.bz2")):
            result = probe_tar(path, max_entries, max_text_files, max_text_bytes)
        elif ext in {".7z", ".rar", ".zst"}:
            result = probe_7z_listing(path, max_entries, timeout)
        else:
            result = {"method": "unsupported", "entries": [], "texts": {}, "error": f"unsupported extension {ext}"}
    except (OSError, zipfile.BadZipFile, tarfile.TarError, RuntimeError, subprocess.SubprocessError) as exc:
        result = {"method": "error", "entries": [], "texts": {}, "error": f"{type(exc).__name__}: {exc}"}
    result["elapsed_sec"] = round(time.monotonic() - start, 3)
    return result


def apply_archive_probe_metadata(item: AddonItem) -> None:
    if not item.archive_probe:
        return
    entries = item.archive_probe.get("entries") or []
    texts = item.archive_probe.get("texts") or {}
    probe_blob = " ".join(entries[:80])
    category, signal = quick_classify(" ".join([item.name, probe_blob]))
    if category and not item.category:
        item.category = category
        item.confidence = "medium"
        item.signals.append(f"archive:{signal}")
    for entry_name, text in list(texts.items())[:4]:
        apply_text_metadata(item, text, f"archive:{Path(entry_name).name}")
    if not item.addon_id:
        root_names = [x.split("/", 1)[0] for x in entries if x and "/" in x]
        if root_names:
            most_common = max(set(root_names), key=root_names.count)
            key = normalize_addon_key(most_common)
            item.addon_id = key or None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class AddonScanner:
    def __init__(
        self,
        roots: list[Path],
        role: str,
        max_depth: int | None = None,
        exclude: list[Path] | None = None,
    ):
        self.roots = roots
        self.role = role
        self.max_depth = max_depth
        self.exclude = [x.resolve(strict=False) for x in (exclude or [])]

    def scan(self) -> list[AddonItem]:
        items: list[AddonItem] = []
        for root in self.roots:
            if not root.exists():
                print(f"⚠️  root missing: {root}")
                continue
            if root.is_file():
                item = self._item_from_file(root)
                if item:
                    items.append(item)
                continue
            stack: list[tuple[Path, int]] = [(root, 0)]
            while stack:
                current, depth = stack.pop()
                if self._is_excluded(current):
                    continue
                if self.max_depth is not None and depth > self.max_depth:
                    continue
                addon_ok, folder_probe = probe_addon_folder(current)
                if addon_ok:
                    items.append(self._item_from_folder(current, folder_probe))
                    continue
                try:
                    with os.scandir(current) as entries:
                        for entry in entries:
                            entry_path = Path(entry.path)
                            try:
                                if self._is_excluded(entry_path):
                                    continue
                                if entry.is_dir(follow_symlinks=False):
                                    if entry.name.startswith(".") or entry.name in {"__pycache__", ".git"}:
                                        continue
                                    stack.append((entry_path, depth + 1))
                                elif entry.is_file(follow_symlinks=False):
                                    item = self._item_from_file(entry_path)
                                    if item:
                                        items.append(item)
                            except OSError:
                                continue
                except OSError as exc:
                    print(f"⚠️  scan error: {current}: {exc}")
        return items

    def _is_excluded(self, path: Path) -> bool:
        if not self.exclude:
            return False
        resolved = path.resolve(strict=False)
        for excluded in self.exclude:
            try:
                if resolved == excluded or resolved.is_relative_to(excluded):
                    return True
            except ValueError:
                continue
        return False

    def _base_item(self, path: Path, kind: str, size: int | None = None) -> AddonItem:
        if size is None:
            size, mtime = safe_stat(path)
        else:
            _, mtime = safe_stat(path)
        known_id, known_ver = detect_known_addon(path.name)
        item = AddonItem(
            path=normalize_path(path),
            name=path.name,
            source_role=self.role,
            kind=kind,
            size=size,
            modified_time=mtime,
            addon_id=known_id,
            addon_name=None,
            version=known_ver or detect_generic_version(path.name),
        )
        quick, signal = quick_classify(path.name)
        if quick:
            item.category = quick
            item.confidence = "medium"
            item.action = "move" if self.role == "source" else "keep"
            item.reason = f"quick classification by {signal}"
            item.signals.append(signal or "quick")
        if not item.addon_id:
            item.addon_id = normalize_addon_key(path.name) or None
        return item

    def _item_from_file(self, path: Path) -> AddonItem | None:
        ext = path.suffix.lower()
        if ext in ARCHIVE_EXTENSIONS or path.name.lower().endswith((".tar.gz", ".tar.xz", ".tar.bz2")):
            item = self._base_item(path, "archive")
            item.signals.append("archive_extension")
            return item
        if ext == ".py":
            text = read_text_head(path)
            if "bl_info" not in text and "def register" not in text:
                return None
            item = self._base_item(path, "script")
            apply_text_metadata(item, text, "script")
            if not item.category:
                quick, signal = quick_classify(" ".join([item.name, item.addon_name or ""]))
                if quick:
                    item.category = quick
                    item.confidence = "medium"
                    item.reason = f"quick classification by {signal}"
            item.action = "move" if self.role == "source" and item.category else "keep"
            item.signals.append("python_addon_signature")
            return item
        return None

    def _item_from_folder(self, path: Path, folder_probe: dict[str, Any]) -> AddonItem:
        count, size = file_count_and_size(path)
        item = self._base_item(path, "folder", size=size)
        item.signals.extend(folder_probe.get("signals") or [])
        item.signals.append(f"folder_files:{count}")
        for text in (folder_probe.get("texts") or {}).values():
            apply_text_metadata(item, text, "folder")
        if not item.category:
            quick, signal = quick_classify(" ".join([item.name, item.addon_name or ""]))
            if quick:
                item.category = quick
                item.confidence = "medium"
                item.reason = f"quick classification by {signal}"
        item.action = "move" if self.role == "source" and item.category else "keep"
        return item


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


class LLMClient:
    def __init__(self, api_url: str, model: str = "local", timeout: int = 300, retries: int = 2):
        self.api_url = api_url
        self.model = model
        self.timeout = timeout
        self.retries = retries

    def ask_json(self, system: str, prompt: str, raw_path: Path | None = None) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = urllib.request.Request(self.api_url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                if raw_path:
                    raw_path.parent.mkdir(parents=True, exist_ok=True)
                    raw_path.write_text(raw, encoding="utf-8")
                content = json.loads(raw)["choices"][0]["message"].get("content") or ""
                return json.loads(extract_json(content))
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, OSError) as exc:
                last_error = exc
                print(f"  ⚠️ LLM attempt {attempt + 1}/{self.retries + 1} failed: {exc}")
                time.sleep(1 + attempt)
        raise RuntimeError(f"LLM request failed: {last_error}")


def extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    raise json.JSONDecodeError("No JSON object found", text, 0)


def build_llm_payload(items: list[AddonItem], folders: list[str], include_probe: bool) -> str:
    slim: list[dict[str, Any]] = []
    for item in items:
        row: dict[str, Any] = {
            "path": item.path,
            "name": item.name,
            "kind": item.kind,
            "size": item.size,
            "addon_id_hint": item.addon_id,
            "addon_name_hint": item.addon_name,
            "version_hint": item.version,
            "signals": item.signals[:12],
        }
        if include_probe and item.archive_probe:
            row["archive_probe"] = {
                "method": item.archive_probe.get("method"),
                "entries": (item.archive_probe.get("entries") or [])[:80],
                "texts": {
                    k: v[:2500]
                    for k, v in list((item.archive_probe.get("texts") or {}).items())[:4]
                },
                "error": item.archive_probe.get("error"),
            }
        slim.append(row)
    return json.dumps({"available_folders": folders, "items": slim}, ensure_ascii=False, indent=2)


def classify_with_llm(
    client: LLMClient,
    items: list[AddonItem],
    folders: list[str],
    out_dir: Path,
    pass_name: str,
    include_probe: bool = False,
) -> None:
    if not items:
        return
    system = (
        "Ты эксперт по аддонам Blender. Верни строго JSON объект вида "
        "{\"items\":[{\"path\":...,\"category\":...,\"addon_id\":...,\"addon_name\":...,"
        "\"version\":...,\"confidence\":\"high|medium|low\",\"action\":\"move|unsure|keep\","
        "\"reason\":...}]}. Категория должна быть только из available_folders. "
        "Если невозможно понять — action=unsure и category=null. Не придумывай версии."
    )
    prompt = (
        "Распредели аддоны Blender по папкам. Это "
        + ("отдельный быстрый анализ содержимого архивов; данные ограничены списком файлов и маленькими текстовыми фрагментами." if include_probe else "первичный анализ только по метаданным.")
        + "\n\n"
        + build_llm_payload(items, folders, include_probe)
    )
    result = client.ask_json(system, prompt, out_dir / f"{pass_name}.raw.json")
    by_path = {x.path: x for x in items}
    for row in result.get("items", []):
        if not isinstance(row, dict):
            continue
        item = by_path.get(str(row.get("path") or ""))
        if not item:
            continue
        category = row.get("category")
        if isinstance(category, str) and category in folders:
            item.category = category
        if isinstance(row.get("addon_id"), str) and row["addon_id"].strip():
            item.addon_id = normalize_addon_key(row["addon_id"]) or row["addon_id"].strip()
        if isinstance(row.get("addon_name"), str) and row["addon_name"].strip():
            item.addon_name = row["addon_name"].strip()
        if isinstance(row.get("version"), str) and row["version"].strip():
            item.version = row["version"].strip()
        if row.get("confidence") in {"high", "medium", "low"}:
            item.confidence = row["confidence"]
        if isinstance(row.get("reason"), str):
            item.reason = row["reason"][:500]
        if row.get("action") == "unsure" or not item.category:
            item.action = "unsure"
        elif item.source_role == "source":
            item.action = "move"


# ---------------------------------------------------------------------------
# Planning / duplicates / apply
# ---------------------------------------------------------------------------


class AddonPlanner:
    def __init__(self, target_root: Path, folders: list[str], duplicate_root_name: str = "_DUPLICATES", unsure_root_name: str = "_UNSURE"):
        self.target_root = target_root
        self.folders = folders
        self.duplicate_root_name = duplicate_root_name
        self.unsure_root_name = unsure_root_name

    def build(self, items: list[AddonItem]) -> list[AddonItem]:
        self._mark_duplicates(items)
        for item in items:
            if item.source_role != "source":
                item.action = "keep"
                continue
            if item.action == "duplicate":
                item.target = normalize_path(self.target_root / self.duplicate_root_name / item.name)
                continue
            if item.category in self.folders:
                item.action = "move"
                item.target = normalize_path(self.target_root / item.category / item.name)
            else:
                item.action = "unsure"
                item.target = normalize_path(self.target_root / self.unsure_root_name / item.name)
        return items

    def _group_key(self, item: AddonItem) -> str | None:
        base = item.addon_id or normalize_addon_key(item.addon_name or item.name)
        return base or None

    def _mark_duplicates(self, items: list[AddonItem]) -> None:
        groups: dict[str, list[AddonItem]] = {}
        for item in items:
            key = self._group_key(item)
            if not key:
                continue
            groups.setdefault(key, []).append(item)
        for key, group in groups.items():
            if len(group) < 2:
                continue
            group_id = f"dup:{key}"
            keeper = max(group, key=self._keep_score)
            for item in group:
                item.duplicate_group = group_id
                if item is keeper:
                    continue
                # Same addon key with older/equal version. Same-size archives are especially safe duplicates.
                cmp = compare_versions(item.version, keeper.version)
                same_size = item.size == keeper.size and item.size > 0
                if cmp < 0 or cmp == 0 or same_size:
                    item.action = "duplicate"
                    item.keep_path = keeper.path
                    item.reason = f"duplicate/older addon; keep {keeper.name}"
                    item.confidence = "high" if same_size or cmp < 0 else "medium"

    @staticmethod
    def _keep_score(item: AddonItem) -> tuple[int, tuple[int, ...], int, str]:
        role_score = 1 if item.source_role == "library" else 0
        return (role_score, version_tuple(item.version), item.size, item.modified_time or "")


def compare_versions(left: str | None, right: str | None) -> int:
    a = version_tuple(left)
    b = version_tuple(right)
    if not a and not b:
        return 0
    if not a:
        return -1
    if not b:
        return 1
    max_len = max(len(a), len(b))
    aa = a + (0,) * (max_len - len(a))
    bb = b + (0,) * (max_len - len(b))
    return (aa > bb) - (aa < bb)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_v{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Cannot build unique path for {path}")


def same_size(path_a: Path, path_b: Path) -> bool:
    try:
        return path_a.stat().st_size == path_b.stat().st_size
    except OSError:
        return False


def apply_plan(items: list[AddonItem], apply: bool, delete_duplicates: bool = False) -> None:
    for item in items:
        if item.source_role != "source" or item.action not in {"move", "duplicate", "unsure"} or not item.target:
            continue
        src = Path(item.path)
        dst = Path(item.target)
        if not src.exists():
            print(f"MISS {src}")
            continue
        if item.action == "duplicate" and delete_duplicates:
            label = "DELETE-DUPLICATE"
            if apply:
                if src.is_dir():
                    shutil.rmtree(src)
                else:
                    src.unlink()
                print(f"{label} {src}")
            else:
                print(f"DRY-RUN {label} {src}")
            continue
        if dst.exists() and same_size(src, dst):
            print(f"DUP-SAME {src} -> {dst}")
            if apply:
                if src.is_dir():
                    shutil.rmtree(src)
                else:
                    src.unlink()
            continue
        final = unique_path(dst)
        label = "MOVE" if item.action == "move" else ("DUPLICATE" if item.action == "duplicate" else "UNSURE")
        if apply:
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(final))
            print(f"{label} {src} -> {final}")
        else:
            print(f"DRY-RUN {label} {src} -> {final}")


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------


def discover_target_folders(target_root: Path, extra: list[str] | None = None) -> list[str]:
    names = {folder for folder, _ in QUICK_RULES}
    if target_root.exists():
        for child in target_root.iterdir():
            if child.is_dir() and not child.name.startswith("_") and child.name != DEFAULT_SOURCE.name:
                names.add(child.name)
    if extra:
        names.update(x for x in extra if x)
    return sorted(names)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown(path: Path, items: list[AddonItem]) -> None:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.action] = counts.get(item.action, 0) + 1
    lines = ["# Blender Addon Manager report", ""]
    lines.append("## Summary")
    for action, count in sorted(counts.items()):
        lines.append(f"- {action}: {count}")
    lines.append("")
    lines.append("## Source actions")
    lines.append("| Action | Category | Confidence | Size | Name | Target/Keep | Reason |")
    lines.append("|---|---|---|---:|---|---|---|")
    for item in items:
        if item.source_role != "source":
            continue
        target = item.target or item.keep_path or ""
        lines.append(
            f"| {item.action} | {item.category or ''} | {item.confidence} | {format_size(item.size)} | `{item.name}` | `{target}` | {item.reason} |"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast Blender addon sorter/deduplicator with optional local LLM.")
    parser.add_argument("--source", action="append", default=[], help="Folder/file to sort. Can be repeated.")
    parser.add_argument("--library", action="append", default=[], help="Existing sorted addon folder(s) used as duplicate/reference DB.")
    parser.add_argument("--target-root", default=str(DEFAULT_ROOT), help="Root with category folders.")
    parser.add_argument("--out", default="outputs/blender_addon_manager", help="Output directory for plan/report.")
    parser.add_argument("--max-depth", type=int, default=None, help="Max scan depth.")
    parser.add_argument("--apply", action="store_true", help="Actually move files/folders. Default is dry-run.")
    parser.add_argument("--duplicates-only", action="store_true", help="Only mark/delete/quarantine duplicates and old versions; do not sort the kept addons.")
    parser.add_argument("--delete-duplicates", action="store_true", help="With --apply, permanently delete duplicate/old-version items instead of moving them to _DUPLICATES. Use carefully.")
    parser.add_argument("--use-llm", action="store_true", help="Use OpenAI-compatible local LLM for unresolved items.")
    parser.add_argument("--llm-url", default=DEFAULT_LLM_URL)
    parser.add_argument("--llm-model", default="local")
    parser.add_argument("--llm-timeout", type=int, default=300)
    parser.add_argument("--archive-probe", action="store_true", help="Probe unresolved archives quickly, then classify again.")
    parser.add_argument("--probe-all-archives", action="store_true", help="Probe every archive before duplicate planning (still limited, no extraction).")
    parser.add_argument("--probe-timeout", type=int, default=5)
    parser.add_argument("--probe-max-entries", type=int, default=80)
    parser.add_argument("--probe-max-text-files", type=int, default=4)
    parser.add_argument("--probe-max-text-bytes", type=int, default=8192)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    target_root = Path(args.target_root)
    sources = [Path(x) for x in args.source] or [DEFAULT_SOURCE]
    libraries = [Path(x) for x in args.library] or [target_root]
    out_dir = Path(args.out) / now_tag()
    folders = discover_target_folders(target_root)

    print("=" * 72)
    print("BLENDER ADDON MANAGER")
    print("=" * 72)
    print("Sources:", ", ".join(str(x) for x in sources))
    print("Libraries:", ", ".join(str(x) for x in libraries))
    print("Target:", target_root)
    print("Mode:", "APPLY" if args.apply else "DRY-RUN")
    if args.duplicates_only:
        print("Scope: duplicates/old versions only")
    if args.delete_duplicates:
        print("Duplicate action: DELETE" if args.apply else "Duplicate action: DRY-RUN DELETE")

    print("\n🔍 Scanning...")
    source_items = AddonScanner(sources, "source", args.max_depth).scan()
    library_items = AddonScanner(libraries, "library", args.max_depth, exclude=sources).scan()
    items = source_items + library_items
    print(f"   Source items: {len(source_items)}")
    print(f"   Library/reference items: {len(library_items)}")

    archives_to_probe = [
        item
        for item in items
        if item.kind == "archive" and (args.probe_all_archives or (args.archive_probe and item.source_role == "source" and not item.category))
    ]
    if archives_to_probe:
        print(f"\n📦 Fast archive probe: {len(archives_to_probe)}")
        for idx, item in enumerate(archives_to_probe, start=1):
            print(f"   [{idx}/{len(archives_to_probe)}] {item.name}")
            item.archive_probe = fast_probe_archive(
                item.path_obj,
                max_entries=args.probe_max_entries,
                max_text_files=args.probe_max_text_files,
                max_text_bytes=args.probe_max_text_bytes,
                timeout=args.probe_timeout,
            )
            apply_archive_probe_metadata(item)
            if item.category and item.source_role == "source" and item.action == "unsure":
                item.action = "move"
                item.reason = item.reason or "classified by fast archive probe"

    unresolved = [item for item in source_items if not item.category and item.action != "duplicate"]
    if args.use_llm and unresolved:
        print(f"\n🤖 LLM metadata pass: {len(unresolved)}")
        client = LLMClient(args.llm_url, args.llm_model, args.llm_timeout)
        classify_with_llm(client, unresolved, folders, out_dir / "llm", "metadata_pass", include_probe=False)

    second_pass = [
        item
        for item in source_items
        if args.use_llm and args.archive_probe and item.kind == "archive" and (not item.category or item.action == "unsure")
    ]
    if second_pass:
        print(f"\n📦 Fast archive probe after LLM UNSURE: {len(second_pass)}")
        for item in second_pass:
            if not item.archive_probe:
                item.archive_probe = fast_probe_archive(
                    item.path_obj,
                    max_entries=args.probe_max_entries,
                    max_text_files=args.probe_max_text_files,
                    max_text_bytes=args.probe_max_text_bytes,
                    timeout=args.probe_timeout,
                )
                apply_archive_probe_metadata(item)
        still_unresolved = [item for item in second_pass if not item.category or item.action == "unsure"]
        if still_unresolved:
            print(f"\n🤖 LLM archive-probe pass: {len(still_unresolved)}")
            client = LLMClient(args.llm_url, args.llm_model, args.llm_timeout)
            classify_with_llm(client, still_unresolved, folders, out_dir / "llm", "archive_probe_pass", include_probe=True)

    print("\n🧩 Building duplicate/sort plan...")
    planned = AddonPlanner(target_root, folders).build(items)
    if args.duplicates_only:
        for item in planned:
            if item.source_role == "source" and item.action != "duplicate":
                item.action = "keep"
                item.target = None
                if not item.reason:
                    item.reason = "duplicates-only mode: keep, no sorting"

    plan = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": not args.apply,
        "target_root": normalize_path(target_root),
        "sources": [normalize_path(x) for x in sources],
        "libraries": [normalize_path(x) for x in libraries],
        "folders": folders,
        "items": [asdict(item) for item in planned],
    }
    write_json(out_dir / "plan.json", plan)
    write_markdown(out_dir / "report.md", planned)

    source_actions = [item for item in planned if item.source_role == "source"]
    print("\n📊 Plan:")
    for action in ("move", "duplicate", "unsure", "keep"):
        count = sum(1 for item in source_actions if item.action == action)
        if count:
            print(f"   {action}: {count}")
    print(f"\n📝 Report: {out_dir / 'report.md'}")
    print(f"📝 Plan:   {out_dir / 'plan.json'}")

    print("\n🚚 Applying plan..." if args.apply else "\n🚚 Dry-run moves...")
    apply_plan(planned, apply=args.apply, delete_duplicates=args.delete_duplicates)
    print("\n✅ Done")


if __name__ == "__main__":
    main()
