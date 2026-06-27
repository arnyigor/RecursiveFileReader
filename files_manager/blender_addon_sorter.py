#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Blender Addons Sorter — llama.cpp HTTP API edition
Запусти llama-server заранее: llama-server -m model.gguf -c 65536
"""

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

import requests

# ========== КОНФИГУРАЦИЯ ==========
ROOT = Path(r"f:\Install\Soft\3D\Blender\Blender Addons")
SOURCE = ROOT / "2 No-sorted"
LLAMA_API = "http://localhost:8080/v1/chat/completions"

# Правила быстрой классификации (до LLM)
QUICK_RULES: List[Tuple[str, List[str]]] = [
    (
        "Trees&Plants",
        ["tree", "plant", "gscatter", "bagapie", "forest", "ivy", "botani", "foliage"],
    ),
    (
        "Humans&Rigs",
        [
            "rig",
            "human",
            "mocap",
            "mixamo",
            "auto-rig",
            "car-rig",
            "makehuman",
            "bone",
            "skin",
        ],
    ),
    (
        "Material-Shading",
        [
            "material",
            "shader",
            "pbr",
            "substance",
            "decal",
            "grungit",
            "eevee",
            "manga",
            "comic",
            "cavity",
            "hdri",
            "texture pack",
        ],
    ),
    (
        "Buildings",
        [
            "city",
            "building",
            "archipack",
            "road",
            "scene city",
            "postussr",
            "buildify",
            "architect",
            "house",
            "construction",
        ],
    ),
    (
        "Landscape",
        [
            "landscape",
            "terrain",
            "scatter",
            "gscatter",
            "real snow",
            "ground",
            "rock",
            "grass",
            "environment",
        ],
    ),
    (
        "Transport",
        [
            "car",
            "vehicle",
            "transport",
            "traffic",
            "truck",
            "aircraft",
            "boat",
            "train",
            "wheel",
        ],
    ),
    (
        "Render-Camera-Lighting",
        [
            "photographer",
            "camera",
            "light",
            "ssgi",
            "ssrt",
            "pure sky",
            "puresky",
            "luxcore",
            "denois",
            "hdri",
            "flare",
            "render",
            "weather",
            "thunder",
            "eve ",
            "cycles",
            "eevee",
            "atmospheric",
            "fog",
            "volumetric",
        ],
    ),
    (
        "Archviz",
        [
            "archviz",
            "archipack",
            "interior",
            "furniture",
            "decoration",
            "room",
            "floor",
            "wall",
        ],
    ),
    (
        "Animation",
        [
            "animation",
            "anim",
            "rigify",
            "auto-rig",
            "motion",
            "timeline",
            "keyframe",
            "mograph",
            "simulation",
        ],
    ),
    (
        "Generators",
        [
            "generator",
            "array",
            "bagapie",
            "buildify",
            "city builder",
            "procedural",
            "scatter",
            "create",
            "spawn",
        ],
    ),
    (
        "VFX",
        [
            "flip",
            "fluid",
            "rbd",
            "fracture",
            "blaze",
            "ravage",
            "cloud",
            "smoke",
            "fire",
            "explosion",
            "destruction",
            "physics",
        ],
    ),
    (
        "Assets",
        [
            "kitops",
            "true-asset",
            "polyhaven",
            "bis",
            "asset",
            "library",
            "kitbash",
            "megascan",
            "quixel",
        ],
    ),
    (
        "Modeling-Hardsurfase",
        [
            "hardops",
            "hops",
            "boxcutter",
            "meshmachine",
            "fluent",
            "welder",
            "cablerator",
            "quick shape",
            "quickcurve",
            "grid modeler",
            "qblocker",
            "machin3",
            "rebevel",
            "knife",
            "bool",
            "cutter",
            "bevel",
            "chamfer",
            "extrude",
            "inset",
        ],
    ),
    (
        "Modeling-Organic-Retopology",
        [
            "retopo",
            "speedretopo",
            "instant mesh",
            "quadremesher",
            "polydamage",
            "retopoflow",
            "sculpt",
            "remesh",
            "topology",
            "unwrap",
            "uv",
        ],
    ),
    ("Clouds", ["cloud", "vdb", "volume", "atmosphere", "sky", "nebula", "fog"]),
    (
        "Import-Export-Management",
        [
            "fbx",
            "send2ue",
            "unreal",
            "better fbx",
            "datasmith",
            "gltf",
            "collada",
            "vmf",
            "xnalar",
            "import",
            "export",
            "convert",
            "pipeline",
            "bridge",
        ],
    ),
    (
        "Geonodes",
        [
            "geonode",
            "geometry node",
            "node group",
            "procedural",
            "sverchok",
            "parametric",
        ],
    ),
    (
        "Texturing-UV-Drawing",
        [
            "textool",
            "uv ",
            "texture",
            "tex ",
            "layer painter",
            "lilysurface",
            "bake",
            "painter",
            "drawing",
            "stencil",
            "projection",
        ],
    ),
    ("Particles", ["particle", "hair", "fur", "grass", "instance", "emitter"]),
    ("Cloth", ["cloth", "fabric", "sewing", "sew", "drape", "simulation", "softbody"]),
    (
        "System-Modifiers-Nodes-Menu-Interface",
        [
            "modifier",
            "system",
            "menu",
            "panel",
            "cleanpanel",
            "collection grid",
            "keymap",
            "shortcut",
            "pie menu",
            "addon manager",
        ],
    ),
    (
        "UI",
        [
            "ui ",
            "interface",
            "theme",
            "icon",
            "viewport",
            "hud",
            "overlay",
            "workspace",
        ],
    ),
    (
        "Optimise",
        ["optim", "lod", "decimate", "simplify", "reduce", "clean", "merge", "cleanup"],
    ),
    (
        "Game",
        ["game", "godot", "unity", "engine", "collision", "navmesh", "level design"],
    ),
    (
        "Video-Editor",
        [
            "video",
            "vse",
            "sequence",
            "post fx",
            "postfx",
            "edit",
            "cut",
            "transition",
            "subtitle",
        ],
    ),
    (
        "Alignment-Closing",
        [
            "align",
            "snap",
            "distribute",
            "array",
            "mirror",
            "symmetry",
            "origin",
            "pivot",
        ],
    ),
    (
        "Scripts-Arrays",
        [
            "script",
            "addon template",
            "batch",
            "array tool",
            "automation",
            "macro",
            "template",
        ],
    ),
    (
        "Internet",
        [
            "blenderkit",
            "megascan",
            "bridge",
            "online",
            "download",
            "sync",
            "cloud",
            "repository",
        ],
    ),
    (
        "Bake",
        ["bake", "simplebake", "pbr bake", "texture bake", "lightmap", "normal bake"],
    ),
]

# Паттерны версий для поиска дубликатов
VERSION_PATTERNS = {
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


@dataclass
class FileInfo:
    name: str
    path: Path
    size_kb: float
    detected_addon: Optional[str] = None
    detected_version: Optional[str] = None
    quick_folder: Optional[str] = None
    is_duplicate: bool = False
    newer_exists: bool = False
    newer_path: Optional[str] = None

    def __str__(self):
        parts = [f"{self.name} ({self.size_kb:.1f} KB)"]
        if self.detected_addon:
            parts.append(f"addon={self.detected_addon}")
        if self.detected_version:
            parts.append(f"v={self.detected_version}")
        if self.quick_folder:
            parts.append(f"quick→{self.quick_folder}")
        if self.is_duplicate:
            parts.append("DUPLICATE")
        if self.newer_exists:
            parts.append(f"NEWER_EXISTS({self.newer_path})")
        return " | ".join(parts)


class AddonDatabase:
    """Сканирует все существующие аддоны во всех папках."""

    def __init__(self, root: Path, exclude: Set[str]):
        self.root = root
        self.exclude = exclude
        self.addons: Dict[str, List[Tuple[str, str, Path]]] = {}
        self._scan()

    def _extract_version(self, filename: str, pattern: str) -> Optional[str]:
        match = re.search(pattern, filename, re.IGNORECASE)
        return match.group(1) if match else None

    def _scan(self):
        for folder in self.root.iterdir():
            if not folder.is_dir() or folder.name in self.exclude:
                continue

            for file_path in folder.rglob("*"):
                if not file_path.is_file():
                    continue

                fname = file_path.name
                for addon_name, pattern in VERSION_PATTERNS.items():
                    ver = self._extract_version(fname, pattern)
                    if ver:
                        self.addons.setdefault(addon_name, []).append(
                            (ver, folder.name, file_path)
                        )
                        break

    def check_duplicate(self, filename: str) -> Tuple[bool, bool, Optional[str]]:
        for addon_name, pattern in VERSION_PATTERNS.items():
            ver = self._extract_version(filename, pattern)
            if not ver:
                continue

            existing = self.addons.get(addon_name, [])
            if not existing:
                return True, False, None

            for ex_ver, ex_folder, ex_path in existing:
                cmp = self._compare_versions(ver, ex_ver)
                if cmp < 0:
                    return True, True, f"{ex_folder}/{ex_path.name}"
                elif cmp == 0:
                    return True, True, f"{ex_folder}/{ex_path.name} (same version)"

            return True, False, None

        return False, False, None

    @staticmethod
    def _compare_versions(v1: str, v2: str) -> int:
        def norm(v):
            return [int(x) for x in re.findall(r"\d+", v)]

        n1, n2 = norm(v1), norm(v2)
        for a, b in zip(n1, n2):
            if a != b:
                return 1 if a > b else -1
        return 0 if len(n1) == len(n2) else (1 if len(n1) > len(n2) else -1)


class LlamaClient:
    """Клиент для llama.cpp HTTP API."""

    def __init__(self, api_url: str = "http://localhost:8080/v1/chat/completions"):
        self.api_url = api_url
        self._check_connection()

    def _check_connection(self):
        try:
            r = requests.get("http://localhost:8080/health", timeout=5)
            if r.status_code == 200:
                print("✓ llama.cpp сервер доступен")
                return
        except requests.ConnectionError:
            pass

        print("❌ llama.cpp сервер не отвечает на localhost:8080")
        print("   Запусти: llama-server -m model.gguf -c 65536 --port 8080")
        raise SystemExit(1)

    def generate(
            self,
            prompt: str,
            temperature: float = 0.1,
            max_tokens: int = 32768,
            retries: int = 3,
    ) -> str:
        """
        Отправка запроса с retry и валидацией ответа.

        Поддержка thinking/reasoning моделей (Gemma, DeepSeek-R1 и т.д.):
        - Если content пуст, ищем JSON в reasoning_content
        - Увеличен max_tokens до 16384 по умолчанию (thinking модели тратят токены на reasoning)
        """
        last_error: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                payload = {
                    "model": "local",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Ты — эксперт по аддонам Blender. "
                                "Отвечай ТОЛЬКО валидным JSON объектом. "
                                "Никакого текста, объяснений или markdown до или после JSON. "
                                "Просто начни с { и закончи на }."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }

                response = requests.post(
                    self.api_url,
                    json=payload,
                    timeout=600,
                )
                response.raise_for_status()
                result = response.json()

                # Извлечение контента с защитой от None/пустоты
                choices = result.get("choices", [])
                if not choices:
                    raise ValueError("Ответ не содержит choices")

                choice = choices[0]
                message = choice.get("message", {})
                finish_reason = choice.get("finish_reason", "unknown")

                # 1. Основной путь: content
                content = message.get("content") or ""
                content = content.strip()

                # 2. Thinking model fallback: reasoning_content
                if not content:
                    reasoning = message.get("reasoning_content") or ""
                    if reasoning:
                        content = self._extract_json_from_text(reasoning)
                        if content:
                            print(
                                f"  ⚠️ Попытка {attempt}: content пуст, "
                                f"JSON найден в reasoning_content"
                            )

                # 3. finish_reason=length → модель не успела закончить
                if not content and finish_reason == "length":
                    raise ValueError(
                        f"Модель не закончила генерацию (finish_reason=length, "
                        f"max_tokens={max_tokens}). Увеличь max_tokens."
                    )

                # 4. Прямой поиск JSON в content (если есть текст, но не чистый JSON)
                if content and not content.lstrip().startswith("{"):
                    extracted = self._extract_json_from_text(content)
                    if extracted:
                        content = extracted

                if not content:
                    raise ValueError(
                        f"Пустой ответ от LLM (попытка {attempt}/{retries})"
                    )

                return content

            except (requests.RequestException, ValueError, KeyError) as e:
                last_error = e
                print(f"  ⚠️ Попытка {attempt}/{retries} не удалась: {e}")
                if attempt < retries:
                    wait = 5 * attempt
                    print(f"  ⏳ Повтор через {wait} сек...")
                    time.sleep(wait)

        raise RuntimeError(
            f"LLM не ответил после {retries} попыток. Последняя ошибка: {last_error}"
        )

    @staticmethod
    def _extract_json_from_text(text: str) -> Optional[str]:
        """Ищет JSON-объект {key: value, ...} в тексте."""
        if not text:
            return None

        # Ищем первую { и последующую matching }
        start = text.find("{")
        if start == -1:
            return None

        depth = 0
        end = -1
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break

        if end == -1 or end <= start:
            return None

        candidate = text[start: end + 1]

        # Валидация: это должен быть dict со строковыми values
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                # Проверяем что хотя бы часть values — строки (файл→папка)
                str_values = sum(1 for v in parsed.values() if isinstance(v, str))
                if str_values >= len(parsed) * 0.5:
                    return candidate
        except (json.JSONDecodeError, RecursionError):
            pass

        return None


class Sorter:
    """Главный класс сортировщика."""

    def __init__(self, root: Path, source: Path):
        self.root = root
        self.source = source
        self.db = AddonDatabase(
            root, exclude={source.name, "_TO_DELETE", "_MOVED", "_UNSURE"}
        )
        self.llm: Optional[LlamaClient] = None

        self.trash = source / "_TO_DELETE"
        self.moved = source / "_MOVED"
        self.unsure = source / "_UNSURE"
        for d in [self.trash, self.moved, self.unsure]:
            d.mkdir(exist_ok=True)

        # Счётчик ошибок для итогового отчёта
        self.errors: List[str] = []

    def safe_move(self, src: Path, dst: Path, label: str = "") -> bool:
        """
        Безопасное перемещение файла с обработкой конфликтов.

        Стратегия при конфликте:
        - Если файл назначения существует и идентичен (размер совпадает) → пропуск
        - Если размер отличается → перемещение с суффиксом _v2, _v3, ...
        - Если os.rename не работает → fallback через copy+delete

        Возвращает True если файл перемещён/пропущен без ошибок.
        """
        # Защита: файл мог исчезнуть между сканом и перемещением
        # (antivirus, broken symlink, внешний процесс)
        if not src.exists():
            msg = f"{label}{src.name}: файл не найден в источнике (возможно, уже перемещён)"
            print(f"  ⚠️  {msg}")
            self.errors.append(msg)
            return False

        if dst.exists():
            try:
                src_size = src.stat().st_size
                dst_size = dst.stat().st_size
            except OSError as e:
                msg = f"{label}{src.name}: не удалось прочитать размер: {e}"
                print(f"  ❌  {msg}")
                self.errors.append(msg)
                return False

            if src_size == dst_size:
                print(
                    f"  ⏭️  {label}{src.name} — уже существует в назначении (идентичный), пропуск"
                )
                # Удаляем источник, т.к. дубликат
                try:
                    src.unlink()
                except OSError:
                    pass
                return True
            else:
                # Файл назначения другой — ищем свободное имя
                stem = dst.stem
                suffix = dst.suffix
                parent = dst.parent
                counter = 2
                while dst.exists():
                    dst = parent / f"{stem}_v{counter}{suffix}"
                    counter += 1
                print(
                    f"  ⚠️  {label}Конфликт: файл назначения другой ({dst_size} vs {src_size} байт)"
                )
                print(f"     → Сохраняю как: {dst.name}")

        try:
            shutil.move(str(src), str(dst))
            return True
        except OSError as e:
            # Fallback: copy + delete (если os.rename не работает через разделы)
            try:
                shutil.copy2(str(src), str(dst))
                src.unlink()
                return True
            except OSError as e2:
                msg = f"{label}{src.name}: {e2}"
                print(f"  ❌  {msg}")
                self.errors.append(msg)
                return False

    def scan_source(self) -> List[FileInfo]:
        files = []

        for file_path in self.source.iterdir():
            if not file_path.is_file():
                continue
            if file_path.name.startswith("_"):
                continue

            size = file_path.stat().st_size / 1024

            info = FileInfo(name=file_path.name, path=file_path, size_kb=size)

            is_addon, newer_exists, newer_path = self.db.check_duplicate(file_path.name)
            if is_addon:
                info.detected_addon = self._detect_addon_name(file_path.name)
                info.detected_version = self._extract_version(file_path.name)
                info.is_duplicate = True
                info.newer_exists = newer_exists
                info.newer_path = newer_path

            info.quick_folder = self._quick_classify(file_path.name)
            files.append(info)

        return files

    def _detect_addon_name(self, filename: str) -> Optional[str]:
        for name, pattern in VERSION_PATTERNS.items():
            if re.search(pattern, filename, re.IGNORECASE):
                return name
        return None

    def _extract_version(self, filename: str) -> Optional[str]:
        for name, pattern in VERSION_PATTERNS.items():
            match = re.search(pattern, filename, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _quick_classify(self, filename: str) -> Optional[str]:
        fn_lower = filename.lower()
        for folder, keywords in QUICK_RULES:
            for kw in keywords:
                if kw.lower() in fn_lower:
                    return folder
        return None

    def build_llm_prompt(self, files: List[FileInfo]) -> str:
        llm_files = [f for f in files if not f.newer_exists and not f.quick_folder]
        if not llm_files:
            return ""

        folders = sorted(
            [
                d.name
                for d in self.root.iterdir()
                if d.is_dir()
                   and d.name not in {self.source.name, "_TO_DELETE", "_MOVED", "_UNSURE"}
            ]
        )

        lines = [
            "Ты — эксперт по аддонам Blender. Распредели файлы по папкам.",
            "",
            "ПРАВИЛА:",
            "1. Ответь ТОЛЬКО валидным JSON, без markdown, без объяснений.",
            "2. Ключ — имя файла точно как в списке.",
            "3. Значение — название папки из списка ниже.",
            '4. Если не уверен — значение "UNSURE".',
            '5. Если это старая версия аддона, для которой есть новее — значение "TRASH".',
            "",
            "ДОСТУПНЫЕ ПАПКИ:",
        ]
        for folder in folders:
            lines.append(f"- {folder}")

        lines.extend(["", "ФАЙЛЫ ДЛЯ РАСПРЕДЕЛЕНИЯ:"])
        for f in llm_files:
            lines.append(f'"{f.name}"')

        lines.extend(
            [
                "",
                "ОТВЕТ (строго JSON):",
                "{",
            ]
        )
        for f in llm_files:
            lines.append(f'    "{f.name}": "НазваниеПапки",')
        lines.append("}")

        return "\n".join(lines)

    def parse_llm_response(self, response: str) -> Dict[str, str]:
        """Парсит JSON-ответ LLM с валидацией структуры {filename: folder}."""
        try:
            start = response.find("{")
            end = response.rfind("}")
            if start == -1 or end == -1 or end <= start:
                print("⚠️ JSON не найден в ответе LLM")
                return {}

            parsed = json.loads(response[start: end + 1])

            if not isinstance(parsed, dict):
                print(f"⚠️ LLM вернул {type(parsed).__name__}, ожидался dict")
                return {}

            # Валидация: все ключи и значения должны быть строками
            result: Dict[str, str] = {}
            skipped = 0
            for key, value in parsed.items():
                if isinstance(key, str) and isinstance(value, str):
                    result[key] = value
                else:
                    skipped += 1

            if skipped:
                print(f"⚠️ Пропущено {skipped} невалидных записей (ожидался str→str)")

            return result

        except json.JSONDecodeError as e:
            print(f"⚠️ Ошибка парсинга JSON: {e}")
            print(f"Ответ: {response[:500]}...")
            return {}

    def execute(self, dry_run: bool = True):
        print("=" * 70)
        print("  BLENDER ADDONS SORTER — llama.cpp HTTP API")
        print("=" * 70)

        print("\n🔍 Сканирование файлов...")
        files = self.scan_source()
        print(f"   Найдено: {len(files)} файлов")

        quick_count = sum(1 for f in files if f.quick_folder)
        dup_count = sum(1 for f in files if f.newer_exists)
        llm_count = len(files) - quick_count - dup_count

        print(f"\n📊 Статистика:")
        print(f"   • Быстрая классификация: {quick_count}")
        print(f"   • Дубли (новее есть): {dup_count} → корзина")
        print(f"   • Нужна LLM: {llm_count}")

        quick_moves: Dict[str, List[FileInfo]] = {}
        trash_files: List[FileInfo] = []
        llm_files: List[FileInfo] = []

        for f in files:
            if f.newer_exists:
                trash_files.append(f)
            elif f.quick_folder:
                quick_moves.setdefault(f.quick_folder, []).append(f)
            else:
                llm_files.append(f)

        print("\n" + "=" * 70)
        print("ПЛАН ДЕЙСТВИЙ:")
        print("=" * 70)

        if trash_files:
            print(f"\n🗑️ В КОРЗИНУ ({len(trash_files)} файлов):")
            for f in trash_files:
                print(f"   • {f.name}")
                print(f"     → новее: {f.newer_path}")

        if quick_moves:
            print(f"\n📁 БЫСТРАЯ КЛАССИФИКАЦИЯ:")
            for folder, file_list in sorted(quick_moves.items()):
                print(f"\n   → {folder}/ ({len(file_list)} файлов):")
                for f in file_list[:5]:
                    print(f"      • {f.name}")
                if len(file_list) > 5:
                    print(f"      ... и ещё {len(file_list) - 5}")

        llm_results = {}
        if llm_files:
            print(f"\n🤖 LLM КЛАССИФИКАЦИЯ ({len(llm_files)} файлов):")
            for f in llm_files:
                print(f"   • {f.name}")

            prompt = self.build_llm_prompt(files)

            if dry_run:
                print(f"\n📋 Промпт для LLM ({len(prompt)} символов):")
                print("-" * 50)
                print(prompt[:2000])
                print("..." if len(prompt) > 2000 else "")
                print("-" * 50)
                print("\n(В dry-run режиме LLM не вызывается)")
            else:
                if self.llm is None:
                    self.llm = LlamaClient(LLAMA_API)

                print(f"\n⏳ Отправка запроса в llama.cpp...")
                response = self.llm.generate(
                    prompt, temperature=0.1, max_tokens=32768, retries=3
                )
                print(f"✓ Ответ получен ({len(response)} символов)")

                llm_results = self.parse_llm_response(response)

                if not llm_results:
                    print(
                        "⚠️  LLM не вернул валидный JSON. Все файлы останутся на месте."
                    )
                    print(f"   Ответ LLM: {response[:300]}...")
                else:
                    print(f"\n📊 LLM распределил ({len(llm_results)} файлов):")
                    for fname, folder in llm_results.items():
                        print(f"   • {fname} → {folder}")

        print("\n" + "=" * 70)
        print("ИТОГОВЫЙ ПЛАН:")
        print("=" * 70)

        all_moves = dict(quick_moves)
        for fname, folder in llm_results.items():
            if folder not in ("UNSURE", "TRASH"):
                all_moves.setdefault(folder, []).append(
                    next(f for f in files if f.name == fname)
                )

        for folder, file_list in sorted(all_moves.items()):
            print(f"\n📁 {folder}/ — {len(file_list)} файлов")

        unsure_after_llm = [
            f
            for f in llm_files
            if f.name in llm_results and llm_results[f.name] == "UNSURE"
        ]
        if unsure_after_llm:
            print(f"\n❓ UNSURE — {len(unsure_after_llm)} файлов (ручная проверка)")

        if dry_run:
            print("\n" + "=" * 70)
            print("⚠️ DRY RUN — изменения не применяются!")
            print("Запусти с dry_run=False для выполнения.")
            print("=" * 70)
            return

        confirm = input("\nВыполнить перемещение? (yes/no): ").strip().lower()
        if confirm not in ("yes", "y", "да", "д"):
            print("❌ Отменено.")
            return

        print("\n" + "=" * 70)
        print("ВЫПОЛНЕНИЕ...")
        print("=" * 70)

        for f in trash_files:
            dst = self.trash / f.name
            self.safe_move(f.path, dst, "🗑️  ")

        for folder, file_list in quick_moves.items():
            dst_dir = self.root / folder
            dst_dir.mkdir(exist_ok=True)
            for f in file_list:
                dst = dst_dir / f.name
                self.safe_move(f.path, dst, f"📁 [{folder}] ")

        for fname, folder in llm_results.items():
            f = next((x for x in files if x.name == fname), None)
            if not f:
                continue

            if folder == "TRASH":
                dst = self.trash / fname
                self.safe_move(f.path, dst, "🗑️  [LLM] ")
            elif folder == "UNSURE":
                dst = self.unsure / fname
                self.safe_move(f.path, dst, "❓ [LLM] ")
            else:
                dst_dir = self.root / folder
                dst_dir.mkdir(exist_ok=True)
                dst = dst_dir / fname
                self.safe_move(f.path, dst, f"📁 [{folder}] [LLM] ")

        print("\n" + "=" * 70)
        print("✅ ГОТОВО!")
        print(f"   Корзина: {len(list(self.trash.iterdir()))} файлов")
        print(f"   Неопознанные: {len(list(self.unsure.iterdir()))} файлов")
        if self.errors:
            print(f"   ❌ Ошибки ({len(self.errors)}):")
            for err in self.errors:
                print(f"      • {err}")
        print("=" * 70)


# ========== ЗАПУСК ==========
if __name__ == "__main__":
    sorter = Sorter(ROOT, SOURCE)

    # Сначала dry run
    sorter.execute(dry_run=True)

    print("\n" + "=" * 70)
    go = input("Запустить с LLM и реальным перемещением? (yes/no): ").strip().lower()

    if go in ("yes", "y", "да", "д"):
        sorter.execute(dry_run=False)
