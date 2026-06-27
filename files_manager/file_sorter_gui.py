#!/usr/bin/env python3
"""
PySide6 GUI for File Sorter Assistant.

MVP scope:
- duplicate review workflow with background scanning;
- manual keep selection, move-to-review, delete-to-recycle-bin;
- settings shell for future smart file move workflow.
"""

from __future__ import annotations

import configparser
import os
import shutil
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

try:
    from send2trash import send2trash
except ImportError:  # pragma: no cover - optional dependency
    send2trash = None

try:
    from files_manager import file_sorter_assistant as sorter
except ImportError:  # Allow running this file directly from files_manager/.
    import file_sorter_assistant as sorter  # type: ignore


GUI_CONFIG_SECTION = "gui"


@dataclass
class DuplicateGroup:
    title: str
    confidence: float
    keep: int
    delete: list[int]
    reason: str
    analysis: str
    files: dict[int, Path]

    @property
    def all_ids(self) -> list[int]:
        return sorted({self.keep, *self.delete})


class WorkerSignals(QObject):
    log = Signal(str)
    progress = Signal(int, int)
    groups_ready = Signal(list)
    error = Signal(str)
    finished = Signal(bool)


class DuplicateScanWorker(QRunnable):
    def __init__(self, root: Path, recursive_depth: int) -> None:
        super().__init__()
        self.root = root
        self.recursive_depth = recursive_depth
        self.signals = WorkerSignals()
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def iter_files(self) -> list[Path]:
        if self.recursive_depth <= 0:
            return sorter.get_files_in_root(self.root)

        files: list[Path] = []

        for current_root, dirs, names in os.walk(self.root):
            if self.cancelled:
                break

            current = Path(current_root)
            rel_depth = len(current.relative_to(self.root).parts)

            if rel_depth >= self.recursive_depth:
                dirs[:] = []

            for name in names:
                path = current / name

                if path.is_file() and not path.is_symlink():
                    files.append(path)

        return sorted(files, key=lambda path: str(path).lower())

    @Slot()
    def run(self) -> None:
        try:
            self.signals.log.emit(f"[scan] Root: {self.root}")
            self.signals.log.emit(
                "[scan] Режим: только root"
                if self.recursive_depth <= 0
                else f"[scan] Рекурсивно до уровня: {self.recursive_depth}"
            )
            files = self.iter_files()
            self.signals.progress.emit(1, 5)

            if self.cancelled:
                self.signals.finished.emit(True)
                return

            if not files:
                self.signals.log.emit("[scan] Файлы не найдены.")
                self.signals.groups_ready.emit([])
                self.signals.finished.emit(False)
                return

            if len(files) > sorter.LLM_DUPLICATE_MAX_FILES:
                self.signals.log.emit(
                    f"[scan] Файлов {len(files)}; в LLM будет отправлено первых "
                    f"{sorter.LLM_DUPLICATE_MAX_FILES}."
                )
                files = files[: sorter.LLM_DUPLICATE_MAX_FILES]

            self.signals.log.emit(f"[LLM] Отправляю список файлов: {len(files)}")
            groups, id_to_path, rejected = sorter.ask_llm_duplicate_groups(files)
            self.signals.progress.emit(2, 5)

            if self.cancelled:
                self.signals.finished.emit(True)
                return

            if rejected:
                self.signals.log.emit(f"[LLM] Отклоненные широкие кандидаты: {len(rejected)}")

                for item in rejected[:10]:
                    ids = ", ".join(str(file_id) for file_id in item.get("ids", []))
                    self.signals.log.emit(f"  - ids [{ids}]: {item.get('reason', '')}")

            if not groups:
                self.signals.groups_ready.emit([])
                self.signals.finished.emit(False)
                return

            self.signals.log.emit("[dup] Расширяю группы похожими именами.")
            groups = sorter.expand_llm_duplicate_groups(groups, id_to_path)
            self.signals.progress.emit(3, 5)

            if self.cancelled:
                self.signals.finished.emit(True)
                return

            if not groups:
                self.signals.groups_ready.emit([])
                self.signals.finished.emit(False)
                return

            self.signals.log.emit("[archive] Выполняю deep-анализ архивов кандидатов.")
            groups = sorter.refine_llm_duplicate_groups_with_archives(groups, id_to_path)
            self.signals.progress.emit(4, 5)

            if self.cancelled:
                self.signals.finished.emit(True)
                return

            result: list[DuplicateGroup] = []

            for group in groups:
                ids = sorted({group["keep"], *group["delete"]})
                live_files = {
                    file_id: id_to_path[file_id]
                    for file_id in ids
                    if file_id in id_to_path and id_to_path[file_id].exists()
                }
                delete_ids = [
                    file_id
                    for file_id in group["delete"]
                    if file_id in live_files and file_id != group["keep"]
                ]

                if group["keep"] not in live_files or not delete_ids:
                    continue

                result.append(
                    DuplicateGroup(
                        title=str(group.get("title", "")),
                        confidence=float(group.get("confidence", 0.0)),
                        keep=int(group["keep"]),
                        delete=delete_ids,
                        reason=str(group.get("reason", "")),
                        analysis=str(group.get("analysis", "")),
                        files=live_files,
                    )
                )

            self.signals.progress.emit(5, 5)
            self.signals.groups_ready.emit(result)
            self.signals.finished.emit(False)
        except Exception:
            self.signals.error.emit(traceback.format_exc())
            self.signals.finished.emit(False)


class DuplicateReviewTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.thread_pool = QThreadPool.globalInstance()
        self.worker: DuplicateScanWorker | None = None
        self.groups: list[DuplicateGroup] = []
        self.current_index = -1
        self.root_path: Path | None = None
        self.setup_ui()

    def setup_ui(self) -> None:
        root_layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText("Root folder")
        browse_btn = QPushButton("Выбрать...")
        browse_btn.clicked.connect(self.choose_root)
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(0, 10)
        self.depth_spin.setValue(0)
        self.depth_spin.setToolTip("0 = только файлы root. Рекурсия заметно дольше.")
        self.scan_btn = QPushButton("Сканировать")
        self.scan_btn.clicked.connect(self.start_scan)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_scan)

        controls.addWidget(QLabel("Root:"))
        controls.addWidget(self.root_edit, 1)
        controls.addWidget(browse_btn)
        controls.addWidget(QLabel("Глубина:"))
        controls.addWidget(self.depth_spin)
        controls.addWidget(self.scan_btn)
        controls.addWidget(self.cancel_btn)
        root_layout.addLayout(controls)

        self.recursive_warning = QLabel(
            "Глубина > 0 включает рекурсивный обход: анализ будет дольше и зависит от количества файлов."
        )
        self.recursive_warning.setStyleSheet("color: #9a6a00;")
        root_layout.addWidget(self.recursive_warning)

        self.progress = QProgressBar()
        self.progress.setRange(0, 5)
        root_layout.addWidget(self.progress)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.group_table = QTableWidget(0, 4)
        self.group_table.setHorizontalHeaderLabels(["#", "Название", "Conf", "Файлов"])
        self.group_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.group_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.group_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.group_table.currentCellChanged.connect(self.on_group_selected)
        left_layout.addWidget(self.group_table)

        action_box = QGroupBox("Действия")
        action_layout = QVBoxLayout(action_box)
        self.keep_combo = QComboBox()
        self.keep_combo.currentIndexChanged.connect(self.on_keep_changed)
        self.move_btn = QPushButton("Переместить остальные в _duplicates_review")
        self.move_btn.clicked.connect(self.move_selected_group)
        self.delete_btn = QPushButton("Удалить остальные в корзину")
        self.delete_btn.clicked.connect(self.delete_selected_group)
        self.skip_btn = QPushButton("Пропустить группу")
        self.skip_btn.clicked.connect(self.skip_group)
        action_layout.addWidget(QLabel("Оставить:"))
        action_layout.addWidget(self.keep_combo)
        action_layout.addWidget(self.move_btn)
        action_layout.addWidget(self.delete_btn)
        action_layout.addWidget(self.skip_btn)
        left_layout.addWidget(action_box)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.file_table = QTableWidget(0, 6)
        self.file_table.setHorizontalHeaderLabels(["Keep", "ID", "Имя", "Размер", "Дата", "Версия"])
        self.file_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.file_table.setEditTriggers(QTableWidget.NoEditTriggers)
        right_layout.addWidget(self.file_table)

        self.analysis = QTextEdit()
        self.analysis.setReadOnly(True)
        self.analysis.setPlaceholderText("LLM-анализ группы")
        right_layout.addWidget(self.analysis, 1)

        self.tech_report = QPlainTextEdit()
        self.tech_report.setReadOnly(True)
        self.tech_report.setPlaceholderText("Технический отчет будет добавлен в следующих итерациях.")
        self.tech_report.setMaximumHeight(160)
        right_layout.addWidget(self.tech_report)
        splitter.addWidget(right)
        splitter.setSizes([360, 860])

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setMaximumHeight(170)
        root_layout.addWidget(self.log)

        self.set_actions_enabled(False)

    def set_actions_enabled(self, enabled: bool) -> None:
        self.keep_combo.setEnabled(enabled)
        self.move_btn.setEnabled(enabled)
        self.delete_btn.setEnabled(enabled)
        self.skip_btn.setEnabled(enabled)

    def choose_root(self) -> None:
        initial = self.root_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Выбрать root", initial)

        if folder:
            self.root_edit.setText(folder)

    def log_line(self, text: str) -> None:
        self.log.appendPlainText(text)

    def start_scan(self) -> None:
        root_text = self.root_edit.text().strip()

        if not root_text:
            QMessageBox.warning(self, "Root не выбран", "Выберите папку root.")
            return

        root = Path(root_text)

        if not root.is_dir():
            QMessageBox.warning(self, "Root недоступен", f"Папка не существует:\n{root}")
            return

        if self.depth_spin.value() > 0:
            ok = QMessageBox.question(
                self,
                "Рекурсивное сканирование",
                "Рекурсивное сканирование может быть долгим и дорогим по LLM-запросам. Продолжить?",
            )

            if ok != QMessageBox.Yes:
                return

        self.root_path = root
        self.groups = []
        self.current_index = -1
        self.group_table.setRowCount(0)
        self.file_table.setRowCount(0)
        self.analysis.clear()
        self.tech_report.clear()
        self.progress.setValue(0)
        self.log_line("[ui] Запуск сканирования.")
        self.scan_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.set_actions_enabled(False)

        self.worker = DuplicateScanWorker(root, self.depth_spin.value())
        self.worker.signals.log.connect(self.log_line)
        self.worker.signals.progress.connect(lambda value, total: self.progress.setValue(value))
        self.worker.signals.groups_ready.connect(self.set_groups)
        self.worker.signals.error.connect(self.on_worker_error)
        self.worker.signals.finished.connect(self.on_worker_finished)
        self.thread_pool.start(self.worker)

    def cancel_scan(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.log_line("[ui] Cancel requested.")
        self.cancel_btn.setEnabled(False)

    def on_worker_error(self, text: str) -> None:
        self.log_line(text)
        QMessageBox.critical(self, "Ошибка worker", text[:4000])

    def on_worker_finished(self, cancelled: bool) -> None:
        self.scan_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.worker = None
        self.log_line("[ui] Сканирование отменено." if cancelled else "[ui] Сканирование завершено.")

    def set_groups(self, groups: list[DuplicateGroup]) -> None:
        self.groups = groups
        self.group_table.setRowCount(len(groups))

        for row, group in enumerate(groups):
            values = [
                str(row + 1),
                group.title,
                f"{group.confidence:.0%}",
                str(len(group.all_ids)),
            ]

            for col, value in enumerate(values):
                self.group_table.setItem(row, col, QTableWidgetItem(value))

        if groups:
            self.group_table.selectRow(0)
            self.render_group(0)
            self.log_line(f"[dup] Найдено групп: {len(groups)}")
        else:
            self.set_actions_enabled(False)
            self.log_line("[dup] Группы не найдены.")

    def on_group_selected(self, current_row: int, _current_col: int, _prev_row: int, _prev_col: int) -> None:
        if 0 <= current_row < len(self.groups):
            self.render_group(current_row)

    def render_group(self, index: int) -> None:
        self.current_index = index
        group = self.groups[index]
        self.keep_combo.blockSignals(True)
        self.keep_combo.clear()

        for file_id in group.all_ids:
            path = group.files[file_id]
            self.keep_combo.addItem(f"[{file_id}] {path.name}", file_id)

            if file_id == group.keep:
                self.keep_combo.setCurrentIndex(self.keep_combo.count() - 1)

        self.keep_combo.blockSignals(False)
        self.render_files(group)
        self.analysis.setPlainText(
            "\n".join(
                part
                for part in [
                    f"Название: {group.title}",
                    f"Уверенность: {group.confidence:.0%}",
                    "",
                    group.analysis.strip(),
                    "",
                    f"Причина: {group.reason}",
                ]
                if part.strip()
            )
        )
        self.tech_report.setPlainText("Deep technical report is used by LLM worker; UI display will be expanded next.")
        self.set_actions_enabled(True)

    def render_files(self, group: DuplicateGroup) -> None:
        self.file_table.setRowCount(len(group.all_ids))

        for row, file_id in enumerate(group.all_ids):
            path = group.files[file_id]
            is_keep = file_id == group.keep
            values = [
                "✓" if is_keep else "",
                str(file_id),
                path.name,
                sorter.format_size(path.stat().st_size) if path.exists() else "?",
                sorter.format_mtime(path),
                sorter.format_duplicate_version(path),
            ]

            for col, value in enumerate(values):
                item = QTableWidgetItem(value)

                if is_keep:
                    item.setBackground(QColor("#dff5df"))

                self.file_table.setItem(row, col, item)

    def on_keep_changed(self) -> None:
        if not (0 <= self.current_index < len(self.groups)):
            return

        file_id = self.keep_combo.currentData()

        if file_id is None:
            return

        group = self.groups[self.current_index]
        all_ids = group.all_ids
        group.keep = int(file_id)
        group.delete = [candidate_id for candidate_id in all_ids if candidate_id != group.keep]
        group.reason = f"Выбор пользователя: оставить {group.files[group.keep].name}"
        self.render_files(group)
        self.log_line(f"[dup] Группа {self.current_index + 1}: выбран keep [{group.keep}]")

    def current_group(self) -> DuplicateGroup | None:
        if not (0 <= self.current_index < len(self.groups)):
            return None

        return self.groups[self.current_index]

    def duplicate_review_dir(self) -> Path:
        root = self.root_path or Path(self.root_edit.text().strip())
        return root / sorter.DUPLICATE_REVIEW_FOLDER

    def move_selected_group(self) -> None:
        group = self.current_group()

        if not group:
            return

        files = [group.files[file_id] for file_id in group.delete if group.files[file_id].exists()]

        if not files:
            QMessageBox.information(self, "Нечего перемещать", "Файлы уже недоступны.")
            return

        review_dir = self.duplicate_review_dir()
        review_dir.mkdir(parents=True, exist_ok=True)

        moved = 0

        for path in files:
            try:
                dest = sorter.resolve_destination(review_dir, path.name)
                shutil.move(str(path), str(dest))
                self.log_line(f"[move] {path.name} -> {dest}")
                moved += 1
            except OSError as e:
                self.log_line(f"[move:error] {path.name}: {e}")

        QMessageBox.information(self, "Готово", f"Перемещено файлов: {moved}")
        self.remove_current_group()

    def delete_selected_group(self) -> None:
        group = self.current_group()

        if not group:
            return

        files = [group.files[file_id] for file_id in group.delete if group.files[file_id].exists()]

        if not files:
            QMessageBox.information(self, "Нечего удалять", "Файлы уже недоступны.")
            return

        names = "\n".join(f"- {path.name}" for path in files[:20])
        suffix = "\n..." if len(files) > 20 else ""
        confirm = QMessageBox.question(
            self,
            "Удалить в корзину?",
            f"Файлы будут отправлены в корзину:\n{names}{suffix}",
        )

        if confirm != QMessageBox.Yes:
            return

        if send2trash is None:
            QMessageBox.warning(
                self,
                "send2trash не установлен",
                "Для удаления в корзину установите зависимость send2trash.",
            )
            return

        deleted = 0

        for path in files:
            try:
                send2trash(str(path))
                self.log_line(f"[trash] {path}")
                deleted += 1
            except Exception as e:
                self.log_line(f"[trash:error] {path.name}: {e}")

        QMessageBox.information(self, "Готово", f"Отправлено в корзину: {deleted}")
        self.remove_current_group()

    def skip_group(self) -> None:
        self.log_line(f"[dup] Группа {self.current_index + 1} пропущена.")
        self.remove_current_group()

    def remove_current_group(self) -> None:
        if not (0 <= self.current_index < len(self.groups)):
            return

        row = self.current_index
        self.groups.pop(row)
        self.group_table.removeRow(row)

        if not self.groups:
            self.current_index = -1
            self.file_table.setRowCount(0)
            self.analysis.clear()
            self.set_actions_enabled(False)
            return

        next_row = min(row, len(self.groups) - 1)
        self.group_table.selectRow(next_row)
        self.render_group(next_row)


class SmartMoveSignals(QObject):
    log = Signal(str)
    status = Signal(str)
    progress = Signal(int, int)
    file_started = Signal(object)
    recommendations_ready = Signal(object, object)
    file_done = Signal(str)
    error = Signal(str)
    finished = Signal(bool)


class SmartMoveWorker(QRunnable):
    def __init__(
        self,
        root: Path,
        dest: Path,
        include_extensions: set[str],
        sort_by: str,
    ) -> None:
        super().__init__()
        self.root = root
        self.dest = dest
        self.include_extensions = include_extensions
        self.sort_by = sort_by
        self.signals = SmartMoveSignals()
        self.cancelled = False
        self.skip_requested = False
        self._action_event = threading.Event()
        self._action: dict | None = None
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self.cancelled = True
        self._action_event.set()

    def skip_current(self) -> None:
        self.skip_requested = True
        self._action = {"type": "skip"}
        self._action_event.set()

    def choose_folder(self, folder: str) -> None:
        with self._lock:
            self._action = {"type": "move", "folder": folder}
            self._action_event.set()

    def files_to_process(self) -> list[Path]:
        try:
            files = [path for path in self.root.iterdir() if path.is_file() and not path.is_symlink()]
        except OSError as e:
            raise RuntimeError(f"Не удалось прочитать root: {e}") from e

        if self.include_extensions:
            files = [path for path in files if path.suffix.lower() in self.include_extensions]

        if self.sort_by == "name":
            files.sort(key=lambda path: path.name.lower())
        elif self.sort_by == "size":
            files.sort(key=lambda path: path.stat().st_size if path.exists() else 0, reverse=True)
        elif self.sort_by == "date":
            files.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)

        return files

    def wait_for_action(self) -> dict | None:
        while not self.cancelled:
            self._action_event.wait(0.2)
            if self.cancelled:
                return None
            with self._lock:
                if self._action is not None:
                    action = self._action
                    self._action = None
                    self._action_event.clear()
                    return action
            self._action_event.clear()
        return None

    def move_to_folder(self, file_path: Path, folder: str) -> Path:
        normalized = sorter.normalize_rel_folder(folder)
        if not normalized:
            raise RuntimeError("Некорректный путь папки назначения.")
        dest_dir = sorter.safe_join_base(self.dest, normalized)
        if not dest_dir:
            raise RuntimeError("Небезопасный путь папки назначения.")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = sorter.resolve_destination(dest_dir, file_path.name)
        sorter.verified_move_file(file_path, dest_path)
        return dest_path

    @Slot()
    def run(self) -> None:
        cancelled = False
        try:
            self.signals.status.emit("Сканирование папок назначения...")
            tree_str, flat_paths = sorter.build_tree(self.dest, sorter.MAX_DEPTH)
            files = self.files_to_process()
            total = len(files)
            self.signals.progress.emit(0, max(total, 1))
            self.signals.log.emit(f"[start] Root: {self.root}")
            self.signals.log.emit(f"[start] Dest: {self.dest}")
            self.signals.log.emit(
                "[start] Фильтр: "
                + (" ".join(sorted(self.include_extensions)) if self.include_extensions else "все файлы")
            )
            self.signals.log.emit(f"[start] Сортировка: {self.sort_by}")
            self.signals.log.emit(f"[scan] Найдено файлов: {total}; папок назначения: {len(flat_paths)}")

            if not files:
                self.signals.status.emit("Нет файлов для обработки.")
                self.signals.finished.emit(False)
                return

            for index, file_path in enumerate(files, 1):
                if self.cancelled:
                    cancelled = True
                    break
                if not file_path.exists():
                    self.signals.progress.emit(index, total)
                    continue

                self.skip_requested = False
                size = file_path.stat().st_size
                self.signals.file_started.emit(
                    {
                        "path": file_path,
                        "name": file_path.name,
                        "size": sorter.format_size(size),
                        "index": index,
                        "total": total,
                    }
                )
                self.signals.status.emit(f"LLM анализ: {file_path.name}")
                self.signals.log.emit(f"[file] {index}/{total}: {file_path.name}")

                archive_info = sorter.inspect_archive(file_path)
                if archive_info.inspected and archive_info.supported:
                    self.signals.log.emit(
                        f"[archive] {archive_info.archive_type}: просмотрено {archive_info.entries_scanned}"
                    )

                recommendations = sorter.ask_llm(
                    file_path,
                    tree_str,
                    flat_paths,
                    archive_info=archive_info,
                ) or []

                if self.cancelled:
                    cancelled = True
                    break
                if self.skip_requested:
                    self.signals.log.emit(f"[skip] {file_path.name}")
                    self.signals.file_done.emit("skipped")
                    self.signals.progress.emit(index, total)
                    continue

                self.signals.recommendations_ready.emit(file_path, recommendations)
                self.signals.status.emit("Выберите папку или пропустите файл.")
                action = self.wait_for_action()

                if self.cancelled or action is None:
                    cancelled = True
                    break
                if action.get("type") == "skip":
                    self.signals.log.emit(f"[skip] {file_path.name}")
                    self.signals.file_done.emit("skipped")
                    self.signals.progress.emit(index, total)
                    continue
                if action.get("type") == "move":
                    try:
                        destination = self.move_to_folder(file_path, str(action.get("folder", "")))
                        self.signals.log.emit(f"[move] {file_path.name} -> {destination}")
                        self.signals.file_done.emit("moved")
                    except Exception as e:
                        self.signals.log.emit(f"[move:error] {file_path.name}: {e}")
                        self.signals.error.emit(str(e))
                        self.signals.file_done.emit("error")

                self.signals.progress.emit(index, total)

            self.signals.status.emit("Отменено." if cancelled else "Готово.")
            self.signals.finished.emit(cancelled)
        except Exception:
            self.signals.error.emit(traceback.format_exc())
            self.signals.finished.emit(False)


class SmartMoveTab(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.thread_pool = QThreadPool.globalInstance()
        self.worker: SmartMoveWorker | None = None
        self.current_recommendations: list[dict] = []
        self.current_file: Path | None = None
        self.setup_ui()

    def setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        title = QLabel("Умная сортировка файлов")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title)

        help_text = QLabel(
            "1) Выберите источник и папку назначения. 2) Настройте фильтр расширений и сортировку. "
            "3) Запустите анализ: для каждого файла можно переместить, пропустить или отменить процесс."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        paths_box = QGroupBox("Папки")
        paths_form = QFormLayout(paths_box)
        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText("Например: C:/Users/Name/Downloads")
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("Например: F:/3D/UE")
        root_row = QHBoxLayout()
        root_row.addWidget(self.root_edit, 1)
        root_browse = QPushButton("Выбрать...")
        root_browse.clicked.connect(lambda: self.choose_folder(self.root_edit, "Выбрать источник"))
        root_row.addWidget(root_browse)
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest_edit, 1)
        dest_browse = QPushButton("Выбрать...")
        dest_browse.clicked.connect(lambda: self.choose_folder(self.dest_edit, "Выбрать назначение"))
        dest_row.addWidget(dest_browse)
        paths_form.addRow("Источник root", root_row)
        paths_form.addRow("Назначение dest", dest_row)
        layout.addWidget(paths_box)

        options_box = QGroupBox("Фильтр и порядок")
        options_layout = QHBoxLayout(options_box)
        self.ext_edit = QLineEdit()
        self.ext_edit.setPlaceholderText("Пусто = все файлы; пример: .zip .rar .7z")
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("Имя A→Z", "name")
        self.sort_combo.addItem("Размер: большие первые", "size")
        self.sort_combo.addItem("Дата: новые первые", "date")
        self.sort_combo.addItem("Без сортировки", "none")
        options_layout.addWidget(QLabel("Расширения:"))
        options_layout.addWidget(self.ext_edit, 1)
        options_layout.addWidget(QLabel("Сортировка:"))
        options_layout.addWidget(self.sort_combo)
        layout.addWidget(options_box)

        controls = QHBoxLayout()
        self.start_btn = QPushButton("Старт")
        self.start_btn.clicked.connect(self.start)
        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.clicked.connect(self.cancel)
        self.skip_btn = QPushButton("Пропустить файл")
        self.skip_btn.clicked.connect(self.skip_current)
        self.cancel_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.skip_btn)
        controls.addWidget(self.cancel_btn)
        controls.addStretch(1)
        layout.addLayout(controls)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m")
        self.status_label = QLabel("Готов к запуску.")
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.status_label, 2)
        layout.addLayout(progress_row)

        current_box = QGroupBox("Текущий файл")
        current_layout = QVBoxLayout(current_box)
        self.current_label = QLabel("Файл не выбран")
        self.current_label.setWordWrap(True)
        current_layout.addWidget(self.current_label)
        layout.addWidget(current_box)

        splitter = QSplitter(Qt.Vertical)
        recommendations_widget = QWidget()
        recommendations_layout = QVBoxLayout(recommendations_widget)
        self.recommendations_table = QTableWidget(0, 4)
        self.recommendations_table.setHorizontalHeaderLabels(["#", "Папка", "Уверенность", "Причина"])
        self.recommendations_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.recommendations_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.recommendations_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.recommendations_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.recommendations_table.itemSelectionChanged.connect(self.sync_custom_folder_from_selection)
        recommendations_layout.addWidget(self.recommendations_table)

        move_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Выбранная или новая папка относительно dest")
        self.move_btn = QPushButton("Переместить в эту папку")
        self.move_btn.clicked.connect(self.move_current)
        self.move_btn.setEnabled(False)
        move_row.addWidget(QLabel("Папка:"))
        move_row.addWidget(self.folder_edit, 1)
        move_row.addWidget(self.move_btn)
        recommendations_layout.addLayout(move_row)
        splitter.addWidget(recommendations_widget)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        splitter.addWidget(self.log)
        splitter.setSizes([500, 180])
        layout.addWidget(splitter, 1)

    def choose_folder(self, target: QLineEdit, title: str) -> None:
        initial = target.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, title, initial)
        if folder:
            target.setText(folder)

    def parse_extensions(self) -> set[str]:
        result: set[str] = set()
        for raw in self.ext_edit.text().replace(",", " ").split():
            ext = raw.strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            result.add(ext)
        return result

    def set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.skip_btn.setEnabled(running)
        self.move_btn.setEnabled(False)
        self.root_edit.setEnabled(not running)
        self.dest_edit.setEnabled(not running)
        self.ext_edit.setEnabled(not running)
        self.sort_combo.setEnabled(not running)

    def log_line(self, text: str) -> None:
        self.log.appendPlainText(text)

    def start(self) -> None:
        root = Path(self.root_edit.text().strip())
        dest = Path(self.dest_edit.text().strip())
        if not root.is_dir():
            QMessageBox.warning(self, "Источник недоступен", f"Папка не существует:\n{root}")
            return
        if not dest.is_dir():
            QMessageBox.warning(self, "Назначение недоступно", f"Папка не существует:\n{dest}")
            return

        self.current_file = None
        self.current_recommendations = []
        self.recommendations_table.setRowCount(0)
        self.folder_edit.clear()
        self.log.clear()
        self.progress.setValue(0)
        self.status_label.setText("Запуск...")
        self.current_label.setText("Файл не выбран")
        self.set_running(True)

        self.worker = SmartMoveWorker(
            root=root,
            dest=dest,
            include_extensions=self.parse_extensions(),
            sort_by=str(self.sort_combo.currentData()),
        )
        self.worker.signals.log.connect(self.log_line)
        self.worker.signals.status.connect(self.status_label.setText)
        self.worker.signals.progress.connect(self.on_progress)
        self.worker.signals.file_started.connect(self.on_file_started)
        self.worker.signals.recommendations_ready.connect(self.on_recommendations_ready)
        self.worker.signals.file_done.connect(self.on_file_done)
        self.worker.signals.error.connect(self.on_error)
        self.worker.signals.finished.connect(self.on_finished)
        self.thread_pool.start(self.worker)

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.status_label.setText("Отмена запрошена. Жду завершения текущего LLM-запроса...")
            self.log_line("[ui] Отмена запрошена.")
        self.cancel_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)
        self.move_btn.setEnabled(False)

    def skip_current(self) -> None:
        if self.worker:
            self.worker.skip_current()
            self.status_label.setText("Пропуск запрошен...")
            self.log_line("[ui] Пропуск текущего файла.")
        self.move_btn.setEnabled(False)

    def move_current(self) -> None:
        folder = self.folder_edit.text().strip()
        if not folder:
            QMessageBox.warning(self, "Папка не выбрана", "Выберите рекомендацию или введите папку вручную.")
            return
        if self.worker:
            self.move_btn.setEnabled(False)
            self.skip_btn.setEnabled(False)
            self.worker.choose_folder(folder)
            self.status_label.setText("Перемещение...")

    def on_progress(self, value: int, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(value)

    def on_file_started(self, info: dict) -> None:
        self.current_file = info["path"]
        self.recommendations_table.setRowCount(0)
        self.folder_edit.clear()
        self.move_btn.setEnabled(False)
        self.skip_btn.setEnabled(True)
        self.current_label.setText(
            f"{info['index']} / {info['total']}  •  {info['name']}  •  {info['size']}"
        )

    def on_recommendations_ready(self, file_path: Path, recommendations: list[dict]) -> None:
        self.current_file = file_path
        self.current_recommendations = recommendations
        self.recommendations_table.setRowCount(len(recommendations))
        for row, recommendation in enumerate(recommendations):
            values = [
                str(row + 1),
                str(recommendation.get("folder", "")),
                f"{float(recommendation.get('confidence', 0.0)):.0%}",
                str(recommendation.get("reason", "")),
            ]
            for col, value in enumerate(values):
                self.recommendations_table.setItem(row, col, QTableWidgetItem(value))
        if recommendations:
            self.recommendations_table.selectRow(0)
            self.folder_edit.setText(str(recommendations[0].get("folder", "")))
        self.move_btn.setEnabled(True)
        self.skip_btn.setEnabled(True)

    def sync_custom_folder_from_selection(self) -> None:
        row = self.recommendations_table.currentRow()
        if 0 <= row < len(self.current_recommendations):
            self.folder_edit.setText(str(self.current_recommendations[row].get("folder", "")))

    def on_file_done(self, status: str) -> None:
        if status in {"moved", "skipped"}:
            self.recommendations_table.setRowCount(0)
            self.folder_edit.clear()
            self.move_btn.setEnabled(False)
            self.skip_btn.setEnabled(True)

    def on_error(self, text: str) -> None:
        self.log_line(text)
        QMessageBox.warning(self, "Ошибка", text[:3000])
        self.move_btn.setEnabled(True)
        self.skip_btn.setEnabled(True)

    def on_finished(self, cancelled: bool) -> None:
        self.set_running(False)
        self.worker = None
        self.status_label.setText("Отменено." if cancelled else "Готово.")
        self.log_line("[ui] Отменено." if cancelled else "[ui] Готово.")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("File Sorter Assistant GUI")
        self.resize(1280, 820)
        self.tabs = QTabWidget()
        self.smart_move_tab = SmartMoveTab()
        self.duplicates_tab = DuplicateReviewTab()
        self.tabs.addTab(self.smart_move_tab, "Сортировка")
        self.tabs.addTab(self.duplicates_tab, "Дубликаты")
        self.setCentralWidget(self.tabs)
        self.setup_toolbar()
        self.load_ini()

    def setup_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        self.addToolBar(toolbar)
        reload_action = QAction("Загрузить INI", self)
        reload_action.triggered.connect(self.load_ini)
        save_action = QAction("Сохранить INI", self)
        save_action.triggered.connect(self.save_ini)
        toolbar.addAction(reload_action)
        toolbar.addAction(save_action)

    def load_ini(self) -> None:
        config_path = sorter.CONFIG_PATH

        if not config_path.exists():
            return

        config = configparser.ConfigParser()
        config.read(config_path, encoding="utf-8")
        root = config.get("paths", "root", fallback="")
        dest = config.get("paths", "dest", fallback="")
        include_extensions = config.get("settings", "include_extensions", fallback="")
        sort_by = config.get("settings", "sort_by", fallback="name")
        duplicate_depth = config.getint(GUI_CONFIG_SECTION, "duplicate_depth", fallback=0)

        if root:
            self.duplicates_tab.root_edit.setText(root)
            self.smart_move_tab.root_edit.setText(root)
        if dest:
            self.smart_move_tab.dest_edit.setText(dest)
        self.smart_move_tab.ext_edit.setText(include_extensions)
        index = self.smart_move_tab.sort_combo.findData(sort_by)
        if index >= 0:
            self.smart_move_tab.sort_combo.setCurrentIndex(index)
        self.duplicates_tab.depth_spin.setValue(duplicate_depth)

    def save_ini(self) -> None:
        config_path = sorter.CONFIG_PATH
        config = configparser.ConfigParser()

        if config_path.exists():
            config.read(config_path, encoding="utf-8")

        if not config.has_section("paths"):
            config.add_section("paths")

        root_value = self.smart_move_tab.root_edit.text().strip() or self.duplicates_tab.root_edit.text().strip()
        config.set("paths", "root", root_value)
        config.set("paths", "dest", self.smart_move_tab.dest_edit.text().strip())

        if not config.has_section("settings"):
            config.add_section("settings")

        config.set("settings", "include_extensions", " ".join(sorted(self.smart_move_tab.parse_extensions())))
        config.set("settings", "sort_by", str(self.smart_move_tab.sort_combo.currentData()))

        if not config.has_section(GUI_CONFIG_SECTION):
            config.add_section(GUI_CONFIG_SECTION)

        config.set(GUI_CONFIG_SECTION, "duplicate_depth", str(self.duplicates_tab.depth_spin.value()))

        with config_path.open("w", encoding="utf-8") as f:
            config.write(f)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
