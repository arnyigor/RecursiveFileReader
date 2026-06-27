from pathlib import Path
import csv
import re
from difflib import SequenceMatcher
from collections import defaultdict

# =========================
# НАСТРОЙКИ
# =========================

REPORT_FILE = "files_report.txt"

# Куда предлагать складывать UE-библиотеку
TARGET_ROOT = Path(r"f:\3D\UE")

# Ничего не перемещать. Только план.
APPLY_MOVE = True

# Если APPLY_MOVE = True, файлы реально будут перемещены.
# Сначала обязательно проверь CSV/TXT.
ALLOW_REAL_MOVE = True

ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z",
    ".001", ".002", ".003", ".004", ".005", ".006", ".007", ".008", ".009",
    ".z01", ".z02", ".z03", ".z04", ".z05",
}

IGNORE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".mp4", ".mp3",
    ".txt", ".md", ".pdf", ".doc", ".docx",
    ".xlsx", ".xml", ".vcf", ".torrent",
}

# Пути, где файлы уже частично являются UE-библиотекой
KNOWN_UE_LIBRARY_ROOTS = [
    Path(r"f:\3D\UE"),
    Path(r"e:\Unreal"),
]

# Папки, которые считать входящим мусорным буфером
INBOX_ROOT_HINTS = {
    "telegram desktop",
    "01_check",
    "02_tempfolder",
    "99_inbox",
}

# =========================
# PARSE REPORT
# =========================

FILE_LINE_RE = re.compile(r"^\[FILE\]\s+(.+?)\s+\|\s+([\d.]+)\s+MB\s*$", re.IGNORECASE)


def parse_report(path: Path):
    result = []

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = FILE_LINE_RE.match(line.strip())
        if not match:
            continue

        file_path = Path(match.group(1))
        size_mb = float(match.group(2))

        result.append({
            "path": file_path,
            "size_mb": size_mb,
            "name": file_path.name,
            "suffix": file_path.suffix.lower(),
        })

    return result


# =========================
# HELPERS
# =========================

def lower_text(value: str) -> str:
    return value.lower()


def path_text(path: Path) -> str:
    return str(path).lower()


def is_archive(path: Path) -> bool:
    name = path.name.lower()

    if path.suffix.lower() in ARCHIVE_EXTENSIONS:
        return True

    # multipart rar: .part1.rar
    if re.search(r"\.part\d+\.rar$", name):
        return True

    # split 7z: .7z.001
    if re.search(r"\.7z\.\d{3}$", name):
        return True

    return False


def is_multipart(path: Path) -> bool:
    name = path.name.lower()
    return bool(
        re.search(r"\.part\d+\.rar$", name)
        or re.search(r"\.7z\.\d{3}$", name)
        or re.search(r"\.z\d{2}$", name)
    )


def multipart_base_name(name: str) -> str:
    value = name.lower()

    value = re.sub(r"\.part\d+\.rar$", ".rar", value)
    value = re.sub(r"\.7z\.\d{3}$", ".7z", value)
    value = re.sub(r"\.z\d{2}$", ".zip", value)

    return value


def looks_like_ue_candidate(path: Path) -> bool:
    text = path_text(path)
    name = path.name.lower()

    # Уже лежит внутри f:\3D\UE — считаем кандидатом UE, кроме явно Blender/софта
    if str(path).lower().startswith(r"f:\3d\ue".lower()):
        return True

    ue_keywords = [
        "unreal", "ue4", "ue5", "ue 4", "ue 5",
        "niagara", "blueprint", "marketplace",
        "plugin", "template", "uasset",
        "metahuman", "nanite", "lumen",
    ]

    if any(k in name for k in ue_keywords):
        return True

    # Названия UE-ассетов без UE в имени
    likely_asset_keywords = [
        "horror", "abandoned", "zombie", "mocap", "locomotion",
        "fps", "bodycam", "sky creator", "ultra dynamic sky",
        "easy rain", "easy snow", "infinity weather",
        "screen space fog", "brushify", "environment pack",
        "asset pack", "vfx pack", "sound pack",
    ]

    return any(k in name for k in likely_asset_keywords)


def looks_like_blender(path: Path) -> bool:
    name = path.name.lower()

    blender_keywords = [
        "blender", "blend", "hard ops", "boxcutter", "decalmachine",
        "auto-rig", "autorig", "baga", "geometry nodes", "geo node",
        "quickshape", "retopoflow", "uvpackmaster", "fluent",
        "physical starlight", "photographer", "node pie",
        "simplebake", "kushiro", "ucupaint", "cablerator",
        "rigicar", "mesh materializer", "quad remesher",
        "better fbx importer", "send2ue",
    ]

    if path.suffix.lower() == ".blend":
        return True

    return any(k in name for k in blender_keywords)


def looks_like_external_software(path: Path) -> bool:
    name = path.name.lower()

    software_keywords = [
        "photoshop", "topaz", "boris", "houdini", "gaea",
        "cascadeur", "rizomuv", "vray", "arnold",
        "embergen", "illugen", "liquigen",
        "windows", "setup", "installer", "crack",
        "repack", "portable",
    ]

    if path.suffix.lower() == ".exe":
        return True

    return any(k in name for k in software_keywords)


def clean_folder_name(value: str) -> str:
    value = value.strip()
    value = re.sub(r'[<>:"/\\|?*]', "_", value)
    value = re.sub(r"\s+", " ", value)
    return value[:160].strip()


# =========================
# CLASSIFICATION
# =========================

def classify_file(path: Path):
    name = path.name.lower()
    current_folder = path.parent.name.lower()
    full = path_text(path)

    if not is_archive(path):
        if path.suffix.lower() in {".uasset", ".umap", ".fbx", ".blend"}:
            return "91_Extracted_Review", "raw asset file, not archive"
        return "99_NonArchive_Review", "not archive"

    if looks_like_external_software(path):
        return "80_NonUE_Related/ExternalTools", "external software/tool, not UE asset archive"

    if looks_like_blender(path):
        return "80_NonUE_Related/Blender_Addons_Assets", "Blender-related archive"

    if not looks_like_ue_candidate(path):
        return "00_Inbox/Need_Inspect", "not enough UE keywords"

    # Existing folder hints
    if "plugins" in full:
        return "03_Plugins/Unsorted", "path contains Plugins"

    if "blueprints" in full or "mechanicks" in full:
        return "04_Blueprints_Systems/Unsorted", "path contains Blueprints/Mechanicks"

    if "animations" in full:
        return "09_Animations_Mocap/Unsorted", "path contains Animations"

    if "sounds" in full:
        return "11_Audio/Unsorted", "path contains Sounds"

    if "vfx" in full:
        return "10_VFX/Unsorted", "path contains VFX"

    if "materials" in full:
        return "07_Materials_Textures_Decals/Unsorted", "path contains Materials"

    if "cinematics" in full or "postprocess" in full:
        return "12_Cinematics_Rendering/Unsorted", "path contains Cinematics/PostProcess"

    if "treesandlandscapes" in full or "brushify" in full:
        return "06_Nature_Landscape/Unsorted", "path contains Trees/Landscapes/Brushify"

    if "characters" in full or "animals" in full:
        return "08_Characters_Creatures/Unsorted", "path contains Characters/Animals"

    if "templates" in full:
        return "02_Templates/Unsorted", "path contains Templates"

    if "cars" in full:
        return "13_Vehicles/Cars", "path contains Cars"

    # Keyword rules
    plugin_keywords = [
        "plugin", "dlss", "easy multi save", "easymultisave",
        "oceanology", "victory", "uassetbrowser", "asset downgrader",
        "gaea2unreal", "compiler booster", "electronic nodes",
        "runtime vertex", "native tts", "blend importer",
    ]

    blueprint_keywords = [
        "blueprint", "quest", "inventory", "puzzle", "interaction",
        "lock system", "door", "generator", "procedural", "pcg",
        "locomotion system", "fps kit", "bodycam fps", "horror engine",
        "survival game kit", "objective", "mission", "save system",
        "car rig", "vehicle traffic", "road tool", "minimap",
    ]

    animation_keywords = [
        "animation", "animations", "animset", "anim pack", "mocap",
        "locomotion", "mixamo", "combat", "kickboxing", "muay thai",
        "kung fu", "parkour", "traversal", "emotes", "sleep",
        "walk", "idle", "creature mocap",
    ]

    vfx_keywords = [
        "vfx", "fx", "niagara", "explosion", "fire", "smoke",
        "blood", "fluid", "rain", "snow", "lightning", "sparks",
        "portal", "waterfall", "storm", "tornado", "fog",
        "muzzle", "impact", "magic", "spline niagara",
    ]

    audio_keywords = [
        "sound", "sounds", "sfx", "music", "ambient", "footstep",
        "foley", "horror pack", "soundtrack",
    ]

    cinematic_keywords = [
        "cinematic", "camera", "lens", "dof", "anamorphic",
        "moviepipeline", "mrq", "ocio", "opencolorio",
        "bodycam", "post process", "vhs",
    ]

    material_keywords = [
        "material", "materials", "texture", "textures", "decal",
        "decals", "lut", "luts", "hdri", "glass", "parallax",
        "cubemap", "shader", "water material",
    ]

    landscape_keywords = [
        "landscape", "forest", "tree", "trees", "foliage",
        "grass", "biome", "mountain", "brushify", "meadow",
        "pine", "birch", "redwood", "water", "ocean", "river",
        "sky", "weather", "cloud", "ultra dynamic sky",
    ]

    character_keywords = [
        "character", "characters", "zombie", "ghost", "creature",
        "monster", "soldier", "npc", "doll", "mummy", "animal",
        "rabbit", "bear", "bat", "dog", "cat", "wolf", "vulture",
        "pigeon", "rat", "squirrel", "mouse", "chimpanzee",
    ]

    vehicle_keywords = [
        "car", "cars", "vehicle", "truck", "spaceship", "space ship",
        "ship", "aircraft", "helicopter", "lander", "vtol",
        "tank", "motorcycle",
    ]

    horror_env_keywords = [
        "horror", "abandoned", "haunted", "asylum", "hospital",
        "backroom", "backrooms", "mansion", "cemetery", "bunker",
        "soviet", "post soviet", "post-apocalyptic", "apocalyptic",
        "prison", "corridor", "school", "classroom", "catacombs",
    ]

    interior_env_keywords = [
        "interior", "office", "apartment", "kitchen", "bathroom",
        "bedroom", "house", "room", "cafe", "restaurant", "laboratory",
        "lab", "laundry", "suburbs", "residential",
    ]

    industrial_env_keywords = [
        "industrial", "factory", "utility", "electrical", "substation",
        "warehouse", "pipes", "garage", "construction",
    ]

    urban_env_keywords = [
        "city", "street", "urban", "subway", "metro", "slums",
        "village", "market", "district",
    ]

    if any(k in name for k in plugin_keywords):
        return "03_Plugins/Unsorted", "plugin keyword"

    if any(k in name for k in blueprint_keywords):
        return "04_Blueprints_Systems/Unsorted", "blueprint/system keyword"

    if any(k in name for k in animation_keywords):
        return "09_Animations_Mocap/Unsorted", "animation/mocap keyword"

    if any(k in name for k in vfx_keywords):
        return "10_VFX/Unsorted", "vfx keyword"

    if any(k in name for k in audio_keywords):
        return "11_Audio/Unsorted", "audio keyword"

    if any(k in name for k in cinematic_keywords):
        return "12_Cinematics_Rendering/Unsorted", "cinematic/render keyword"

    if any(k in name for k in material_keywords):
        return "07_Materials_Textures_Decals/Unsorted", "material/texture/decal keyword"

    if any(k in name for k in landscape_keywords):
        return "06_Nature_Landscape/Unsorted", "landscape/nature keyword"

    if any(k in name for k in character_keywords):
        return "08_Characters_Creatures/Unsorted", "character/creature keyword"

    if any(k in name for k in vehicle_keywords):
        return "13_Vehicles/Unsorted", "vehicle keyword"

    if any(k in name for k in horror_env_keywords):
        return "05_Environments/Horror_Abandoned", "horror/abandoned environment keyword"

    if any(k in name for k in interior_env_keywords):
        return "05_Environments/Interior", "interior environment keyword"

    if any(k in name for k in industrial_env_keywords):
        return "05_Environments/Industrial", "industrial environment keyword"

    if any(k in name for k in urban_env_keywords):
        return "05_Environments/Urban", "urban environment keyword"

    return "00_Inbox/Need_Inspect", "UE candidate but category unclear"


# =========================
# DUPLICATE NORMALIZATION
# =========================

def normalize_for_duplicate_key(path: Path) -> str:
    name = multipart_base_name(path.name.lower())

    # remove extension chain
    name = re.sub(r"\.(zip|rar|7z|001|002|003|004|005|z01|z02)$", "", name)

    # remove common noise
    noise_patterns = [
        r"pass\d+",
        r"unreal engine",
        r"ue\s*4[\.\d]*",
        r"ue\s*5[\.\d]*",
        r"ue4[\.\d]*",
        r"ue5[\.\d]*",
        r"v\d+(\.\d+)*",
        r"\b4\.\d+\b",
        r"\b5\.\d+\b",
        r"\b5\.\d+\s*-\s*5\.\d+\b",
        r"\bsource\b",
        r"\bprojectfiles\b",
        r"\bmarketplace\b",
        r"\bpack\b",
        r"\bbundle\b",
        r"\basset\b",
        r"\bassets\b",
        r"\bfor\b",
        r"\bplus\b",
    ]

    for pattern in noise_patterns:
        name = re.sub(pattern, " ", name)

    name = re.sub(r"[^a-zа-я0-9]+", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()

    return name


def build_duplicate_groups(items):
    groups = defaultdict(list)

    for item in items:
        path = item["path"]

        if not is_archive(path):
            continue

        key = normalize_for_duplicate_key(path)

        if len(key) < 4:
            continue

        groups[key].append(item)

    exactish = {
        key: values
        for key, values in groups.items()
        if len(values) >= 2
    }

    return exactish


def build_fuzzy_duplicate_groups(items, max_items=2500):
    candidates = [
        item for item in items
        if is_archive(item["path"])
    ]

    result = []
    keys = [
        (item, normalize_for_duplicate_key(item["path"]))
        for item in candidates
    ]

    for i in range(len(keys)):
        item_a, key_a = keys[i]

        if len(key_a) < 8:
            continue

        for j in range(i + 1, len(keys)):
            item_b, key_b = keys[j]

            if len(key_b) < 8:
                continue

            ratio = SequenceMatcher(None, key_a, key_b).ratio()

            if ratio >= 0.88:
                size_a = item_a["size_mb"]
                size_b = item_b["size_mb"]

                larger = max(size_a, size_b)
                smaller = min(size_a, size_b)

                if larger == 0:
                    continue

                size_ratio = smaller / larger

                # либо имена очень похожи, либо размер тоже близкий
                if ratio >= 0.93 or size_ratio >= 0.85:
                    result.append((ratio, item_a, item_b))

    return result


# =========================
# OUTPUT
# =========================

def write_move_plan(items):
    rows = []

    for item in items:
        src = item["path"]
        target_rel, reason = classify_file(src)
        target_dir = TARGET_ROOT / target_rel
        target_path = target_dir / src.name

        action = "PLAN_ONLY"

        if is_multipart(src):
            action = "MULTIPART_KEEP_TOGETHER"

        if "Need_Inspect" in target_rel:
            action = "REVIEW"

        if "NonUE" in target_rel:
            action = "NON_UE_REVIEW"

        rows.append({
            "action": action,
            "source": str(src),
            "size_mb": f"{item['size_mb']:.2f}",
            "target_dir": str(target_dir),
            "target_path": str(target_path),
            "reason": reason,
            "duplicate_key": normalize_for_duplicate_key(src),
        })

    rows.sort(key=lambda r: (r["target_dir"].lower(), r["source"].lower()))

    csv_path = Path("ue_move_plan.csv")
    txt_path = Path("ue_move_plan.txt")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "action",
                "source",
                "size_mb",
                "target_dir",
                "target_path",
                "reason",
                "duplicate_key",
            ],
            delimiter=";"
        )
        writer.writeheader()
        writer.writerows(rows)

    with txt_path.open("w", encoding="utf-8") as f:
        current_dir = None

        for row in rows:
            if row["target_dir"] != current_dir:
                current_dir = row["target_dir"]
                f.write("\n")
                f.write("=" * 120 + "\n")
                f.write(f"TARGET: {current_dir}\n")
                f.write("=" * 120 + "\n")

            f.write(
                f"[{row['action']}] {row['source']} | {row['size_mb']} MB | {row['reason']}\n"
            )

    return rows


def write_duplicates(items):
    exactish = build_duplicate_groups(items)
    fuzzy = build_fuzzy_duplicate_groups(items)

    out = Path("ue_duplicates_review.txt")

    with out.open("w", encoding="utf-8") as f:
        f.write("DUPLICATES REVIEW\n")
        f.write("=" * 120 + "\n\n")

        f.write("EXACTISH GROUPS BY NORMALIZED NAME\n")
        f.write("=" * 120 + "\n")

        for key, values in sorted(exactish.items(), key=lambda kv: kv[0]):
            f.write(f"\nKEY: {key}\n")

            for item in sorted(values, key=lambda x: x["size_mb"], reverse=True):
                f.write(f"  - {item['path']} | {item['size_mb']:.2f} MB\n")

        f.write("\n\nFUZZY PAIRS\n")
        f.write("=" * 120 + "\n")

        for ratio, a, b in sorted(fuzzy, key=lambda x: x[0], reverse=True):
            f.write(f"\nSIMILARITY: {ratio:.3f}\n")
            f.write(f"  A: {a['path']} | {a['size_mb']:.2f} MB\n")
            f.write(f"  B: {b['path']} | {b['size_mb']:.2f} MB\n")


def optionally_apply_moves(rows):
    if not APPLY_MOVE:
        return

    if not ALLOW_REAL_MOVE:
        raise RuntimeError("APPLY_MOVE=True, but ALLOW_REAL_MOVE=False. Enable both intentionally.")

    import shutil

    for row in rows:
        if row["action"] in {"REVIEW", "NON_UE_REVIEW"}:
            continue

        src = Path(row["source"])
        dst = Path(row["target_path"])

        if not src.exists():
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)

        if dst.exists():
            # Do not overwrite
            continue

        shutil.move(str(src), str(dst))


def main():
    report = Path(REPORT_FILE)

    if not report.exists():
        print(f"Report not found: {report.resolve()}")
        return

    items = parse_report(report)

    print(f"Parsed files: {len(items)}")

    archive_items = [
        item for item in items
        if is_archive(item["path"])
    ]

    print(f"Archive candidates: {len(archive_items)}")

    rows = write_move_plan(items)
    write_duplicates(items)
    optionally_apply_moves(rows)

    print("Done.")
    print("Created: ue_move_plan.csv")
    print("Created: ue_move_plan.txt")
    print("Created: ue_duplicates_review.txt")
    print("No files were moved unless APPLY_MOVE=True and ALLOW_REAL_MOVE=True.")


if __name__ == "__main__":
    main()