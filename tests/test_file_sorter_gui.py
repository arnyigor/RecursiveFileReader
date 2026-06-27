import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


def write_file(path: Path, size: int = 10) -> Path:
    path.write_bytes(b"x" * size)
    return path


def test_main_window_smoke_creates_two_tabs(qapp) -> None:
    from files_manager.file_sorter_gui import MainWindow

    window = MainWindow()

    assert window.tabs.count() == 2
    assert window.duplicates_tab is not None
    assert window.smart_move_tab is not None

    window.close()


def test_duplicate_review_tab_allows_manual_keep_change(qapp, tmp_path: Path) -> None:
    from files_manager.file_sorter_gui import DuplicateGroup, DuplicateReviewTab

    first = write_file(tmp_path / "Cinematic Compositor Addon v1.4.zip", 100)
    second = write_file(tmp_path / "Cinematic_Compositor_Addon_v2.zip", 80)
    tab = DuplicateReviewTab()
    group = DuplicateGroup(
        title="Cinematic Compositor Addon",
        confidence=0.95,
        keep=2,
        delete=[1],
        reason="newer version",
        analysis="v2 is newer",
        files={1: first, 2: second},
    )

    tab.set_groups([group])
    tab.keep_combo.setCurrentIndex(0)

    assert tab.current_group() is group
    assert group.keep == 1
    assert group.delete == [2]
    assert tab.file_table.rowCount() == 2

    tab.close()
