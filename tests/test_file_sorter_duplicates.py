import os
import zipfile
from pathlib import Path

from files_manager import file_sorter_assistant as sorter


def touch_file(path: Path, size: int, mtime: int = 1_700_000_000) -> Path:
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def write_zip(path: Path, entries: list[str], mtime: int = 1_700_000_000) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for entry in entries:
            archive.writestr(entry, b"x")

    os.utime(path, (mtime, mtime))
    return path


def test_version_parser_normalizes_trailing_zero_build_parts() -> None:
    file_path = Path("Quadspinner.Gaea2-2.2.6.0-ARGIE.rar")

    assert sorter.duplicate_version_tuple(file_path) == (2, 2, 6, 0)
    assert sorter.normalize_version_tuple(sorter.duplicate_version_tuple(file_path)) == (2, 2, 6)
    assert sorter.format_duplicate_version(file_path) == "2.2.6.0 (= 2.2.6)"


def test_compact_product_version_is_detected() -> None:
    assert sorter.duplicate_version_tuple(Path("EmbrGen126.rar")) == (1, 2, 6)
    assert sorter.format_duplicate_version(Path("EmbrGen126.rar")) == "1.2.6"


def test_explicit_product_version_beats_engine_compatibility_version() -> None:
    old_engine_named = Path("Easy Multi Save v1.62 5.4.rar")
    latest = Path("EasyMultiSaveUE57_v1.77.7z")

    assert sorter.duplicate_version_tuple(old_engine_named) == (1, 62)
    assert sorter.format_duplicate_version(old_engine_named) == "1.62"
    assert sorter.duplicate_file_score(latest)[0] > sorter.duplicate_file_score(old_engine_named)[0]


def test_llm_bool_parser_does_not_treat_false_string_as_true() -> None:
    assert sorter.parse_llm_bool(True)
    assert sorter.parse_llm_bool("true")
    assert not sorter.parse_llm_bool(False)
    assert not sorter.parse_llm_bool("false")
    assert not sorter.parse_llm_bool("")


def test_delete_duplicate_files_sends_to_trash(monkeypatch, tmp_path: Path) -> None:
    target = touch_file(tmp_path / "duplicate.zip", 10)
    trashed: list[str] = []

    def fake_send2trash(path: str) -> None:
        trashed.append(path)
        Path(path).unlink()

    monkeypatch.setattr(sorter, "send2trash", fake_send2trash)

    assert sorter.delete_duplicate_files([target], tmp_path) == 1
    assert trashed == [str(target)]
    assert not target.exists()


def test_delete_duplicate_files_falls_back_to_review_folder(monkeypatch, tmp_path: Path) -> None:
    target = touch_file(tmp_path / "duplicate.zip", 10)

    def fake_send2trash(_path: str) -> None:
        raise RuntimeError("trash unavailable")

    monkeypatch.setattr(sorter, "send2trash", fake_send2trash)

    assert sorter.delete_duplicate_files([target], tmp_path) == 1
    assert not target.exists()
    assert (tmp_path / sorter.DUPLICATE_REVIEW_FOLDER / "duplicate.zip").exists()


def test_duplicate_relation_does_not_promote_generic_outlier() -> None:
    assert sorter.duplicate_files_look_related(
        Path("TreeDesigner + 400 Procedural Trees.rar"),
        Path("TreeDesigner + 400 trees.rar"),
    )
    assert not sorter.duplicate_files_look_related(
        Path("Procedural.rar"),
        Path("TreeDesigner + 400 trees.rar"),
    )


def test_duplicate_relation_does_not_promote_single_word_into_different_projects() -> None:
    assert sorter.duplicate_files_look_related(Path("Widget1_2.rar"), Path("Widget1_3.rar"))
    assert sorter.duplicate_files_look_related(Path("Widget1_3.rar"), Path("Widget_1_1.rar"))
    assert not sorter.duplicate_files_look_related(
        Path("Advanced Widget Constructor.zip"),
        Path("Widget.rar"),
    )
    assert not sorter.duplicate_files_look_related(
        Path("Ultimate Widget Toolkit UE5.3+.rar"),
        Path("Widget_1_1.rar"),
    )


def test_enforce_priority_preserves_llm_keep_after_refinement(tmp_path: Path) -> None:
    procedural = touch_file(tmp_path / "Procedural.rar", 10)
    tree_full = touch_file(tmp_path / "TreeDesigner + 400 Procedural Trees.rar", 110)
    tree_small = touch_file(tmp_path / "TreeDesigner + 400 trees.rar", 64)
    id_to_path = {38: procedural, 52: tree_full, 53: tree_small}
    source_groups = [
        {
            "title": "TreeDesigner broad hint",
            "confidence": 0.8,
            "keep": 53,
            "delete": [38, 52],
            "reason": "broad local hint",
        }
    ]
    refined_groups = [
        {
            "title": "TreeDesigner",
            "confidence": 0.9,
            "keep": 53,
            "delete": [52],
            "reason": "deep analysis removed outlier",
        }
    ]

    fixed = sorter.enforce_llm_duplicate_priority(refined_groups, source_groups, id_to_path)

    assert len(fixed) == 1
    assert 38 not in {fixed[0]["keep"], *fixed[0]["delete"]}
    assert fixed[0]["keep"] == 53
    assert set(fixed[0]["delete"]) == {52}


def test_enforce_priority_does_not_override_llm_version_choice(tmp_path: Path) -> None:
    old_engine_named = touch_file(tmp_path / "Easy Multi Save v1.62 5.4.rar", 26)
    latest = touch_file(tmp_path / "EasyMultiSaveUE57_v1.77.7z", 28)
    group = {
        "title": "Easy Multi Save",
        "confidence": 0.98,
        "keep": 18,
        "delete": [13],
        "analysis": "LLM selected product version v1.77 over v1.62 despite UE 5.4 in the older filename",
        "reason": "newest product version selected by LLM",
    }

    fixed = sorter.enforce_llm_duplicate_priority([group], [group], {13: old_engine_named, 18: latest})

    assert fixed[0]["keep"] == 18
    assert fixed[0]["delete"] == [13]
    assert "правил" not in fixed[0]["reason"].lower()


def test_structure_override_keeps_llm_choice_over_size_date_rule(tmp_path: Path) -> None:
    cleaner = touch_file(tmp_path / "Addon v2.zip", 100, mtime=1_700_000_000)
    noisy = touch_file(tmp_path / "Addon v2 repack.zip", 200, mtime=1_700_100_000)
    group = {
        "title": "Addon",
        "confidence": 1.0,
        "keep": 1,
        "delete": [2],
        "structure_override": True,
        "analysis": "same useful content, second archive contains extra noise",
        "reason": "deep structure comparison",
    }

    fixed = sorter.enforce_llm_duplicate_priority([group], [group], {1: cleaner, 2: noisy})

    assert fixed[0]["keep"] == 1
    assert fixed[0]["delete"] == [2]
    assert "structure_override=true" in fixed[0]["reason"]


def test_expand_group_does_not_auto_add_structurally_different_archive(tmp_path: Path) -> None:
    puzzle_v2 = write_zip(
        tmp_path / "Puzzle1_2.zip",
        ["Puzzle/Content/Blueprints/PuzzleDoor.uasset", "Puzzle/Puzzle.uproject"],
    )
    puzzle_v3 = write_zip(
        tmp_path / "Puzzle1_3.zip",
        ["Puzzle/Content/Blueprints/PuzzleDoor.uasset", "Puzzle/Puzzle.uproject"],
    )
    constructor = write_zip(
        tmp_path / "Puzzle Constructor.zip",
        ["PuzzleConstructor/Content/Constructor.uasset", "PuzzleConstructor/PuzzleConstructor.uproject"],
    )
    id_to_path = {1: puzzle_v2, 2: puzzle_v3, 3: constructor}
    groups = [
        {
            "title": "Puzzle versions",
            "confidence": 0.9,
            "keep": 2,
            "delete": [1],
            "reason": "same version line",
        }
    ]

    expanded = sorter.expand_llm_duplicate_groups(groups, id_to_path)

    assert len(expanded) == 1
    assert 3 not in {expanded[0]["keep"], *expanded[0]["delete"]}


def test_structure_filter_prunes_broad_llm_group_to_same_internal_project(tmp_path: Path) -> None:
    constructor = write_zip(
        tmp_path / "Advanced Widget Constructor.zip",
        ["AdvancedWidgetConstructor/Content/Constructor.uasset", "AdvancedWidgetConstructor/AdvancedWidgetConstructor.uproject"],
    )
    system_kit = write_zip(
        tmp_path / "Widget System Kit 5.0.zip",
        ["WidgetSystemKit/Content/System.uasset", "WidgetSystemKit/WidgetSystemKit.uproject"],
    )
    widget_base = write_zip(
        tmp_path / "Widget.zip",
        ["Widget/Content/Core.uasset", "Widget/Widget.uproject"],
    )
    widget_v2 = write_zip(
        tmp_path / "Widget1_2.zip",
        ["Widget/Content/Core.uasset", "Widget/Widget.uproject"],
    )
    widget_v3 = write_zip(
        tmp_path / "Widget1_3.zip",
        ["Widget/Content/Core.uasset", "Widget/Widget.uproject"],
    )
    widget_v1 = write_zip(
        tmp_path / "Widget_1_1.zip",
        ["Widget/Content/Core.uasset", "Widget/Widget.uproject"],
    )
    fps_kit = write_zip(
        tmp_path / "Ultimate FPS Widget Kit UE5.3+.zip",
        ["UltimateFPSWidgetKit/Content/FPS.uasset", "UltimateFPSWidgetKit/UltimateFPSWidgetKit.uproject"],
    )
    id_to_path = {
        2: constructor,
        73: system_kit,
        74: widget_base,
        75: widget_v2,
        76: widget_v3,
        77: widget_v1,
        104: fps_kit,
    }
    groups = [
        {
            "title": "Widget variants",
            "confidence": 0.75,
            "keep": 73,
            "delete": [2, 74, 75, 76, 77, 104],
            "analysis": "broad filename match",
            "reason": "broad filename match",
        }
    ]

    filtered = sorter.split_llm_duplicate_groups_by_archive_structure(groups, id_to_path)

    assert len(filtered) == 1
    assert filtered[0]["keep"] == 76
    assert set(filtered[0]["delete"]) == {74, 75, 77}
    assert set(filtered[0]["structure_pruned_ids"]) == {2, 73, 104}


def test_multipart_archives_are_not_treated_as_duplicates() -> None:
    id_to_path = {
        1: Path("Photoshop 2026 (27.2.0.15).part1.rar"),
        2: Path("Photoshop 2026 (27.2.0.15).part2.rar"),
    }

    assert sorter.multipart_archive_part(id_to_path[1]) == ("photoshop 2026 27 2 0 15", "1")
    assert sorter.group_contains_multipart_set_parts([1, 2], id_to_path)
    assert sorter.all_same_multipart_set([1, 2], id_to_path)


def test_deep_archive_report_reads_one_level_nested_archive(tmp_path: Path) -> None:
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("UltraDynamicSky/UltraDynamicSky.uplugin", "{}")
        archive.writestr("UltraDynamicSky/Content/Asset.uasset", b"asset")

    outer = tmp_path / "Ultra Dynamic Sky v9.3 - UE 5.5-5.7.zip"
    with zipfile.ZipFile(outer, "w") as archive:
        archive.write(inner, "download/Ultra Dynamic Sky v9.3.zip")
        archive.writestr("download/CGDownload.html", "<html></html>")

    report, details = sorter.format_duplicate_archive_deep_report(17, outer)

    assert "nested_archives:" in report
    assert "classification: ue_plugin" in report
    assert any("::UltraDynamicSky/UltraDynamicSky.uplugin" in detail.path for detail in details)
