#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Markdown‑генератор исходников с асинхронной генерацией,
прогресс‑баром, сохранением конфигурации в .env (python-dotenv)
и возможностью вручную управлять списком файлов для MD.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import pathlib
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable, List, Set, Optional

# ───── Зависимости ────────────────────────────────────────────────
try:
    from dotenv import load_dotenv, set_key  # type: ignore
except ModuleNotFoundError as exc:
    sys.stderr.write(
        "Не найден пакет python‑dotenv. Установите его: pip install python-dotenv\n"
    )
    raise exc

# ───── GUI‑модули ────────────────────────────────────────────────
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk  # для прогресс‑бара

# ───── Настройки ────────────────────────────────────────────────
ENV_PATH = pathlib.Path(__file__).parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

LOGGER = logging.getLogger("md_generator")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)


def _ensure_env_dir() -> None:
    """Гарантирует существование каталога .env‑файла."""
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    source: pathlib.Path = field(default_factory=lambda: pathlib.Path.cwd())
    extensions: Set[str] = field(default_factory=lambda: {".kt", ".java"})
    exclude: Set[str] = field(default_factory=set)
    output: str = "source_dump.md"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            source=pathlib.Path(os.getenv("SOURCE", "")).expanduser(),
            extensions=parse_extensions(os.getenv("EXTENSIONS", "")),
            exclude=parse_exclusions(os.getenv("EXCLUDE", "")),
            output=os.getenv("OUTPUT", ""),
        )


# ───── Утилиты ────────────────────────────────────────────────
def parse_extensions(raw: str) -> Set[str]:
    return {
        f".{ext.lstrip('.').lower()}" for ext in raw.split(",") if ext.strip()
    }


def parse_exclusions(raw: str) -> Set[str]:
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


def _is_text_file(path: pathlib.Path) -> bool:
    mime, _ = mimetypes.guess_type(str(path))
    return mime is None or mime.startswith("text/")


def collect_source_files(
        root: pathlib.Path,
        extensions: Set[str],
        exclude_dirs: Set[str],
) -> List[pathlib.Path]:
    if not root.is_dir():
        return []

    files: List[pathlib.Path] = []

    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in extensions:
            continue
        # Исключаем любые каталоги из списка exclude_dirs, даже если они находятся в пути.
        if any(part.lower() in exclude_dirs for part in p.parts):
            continue
        if not _is_text_file(p):
            LOGGER.debug("Пропуск бинарного файла: %s", p)
            continue
        files.append(p)

    return sorted(files)


def read_file_contents(file_path: pathlib.Path) -> str:
    try:
        return file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        LOGGER.warning("Не удалось прочитать %s: %s", file_path, exc)
        return ""


# ───── Асинхронный слой генерации ─────────────────────────────────
async def _generate_async(
        app: "App",
        root: pathlib.Path,
        files: List[pathlib.Path],
        out_name: str,
) -> pathlib.Path:
    """
    Генерация MD‑файла из переданного списка файлов.
    Прогресс обновляется через API App.
    """
    if not files:
        raise FileNotFoundError("Список исходных файлов пуст")

    out_path = pathlib.Path(out_name).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lang_map: dict[str, str] = {".kt": "kotlin", ".java": "java"}

    app.set_progress(len(files))
    try:
        with out_path.open("w", encoding="utf-8") as f:
            for idx, path in enumerate(files, 1):
                if not app.is_generating:   # пользователь нажал «Отмена» (не реализовано)
                    break
                content = await asyncio.to_thread(read_file_contents, path)
                lang_tag = lang_map.get(path.suffix.lower(), "")
                try:
                    rel_path = path.relative_to(root)
                except ValueError:
                    rel_path = path.name
                f.write(f"### `{rel_path}`\n\n")
                f.write(f"```{lang_tag}\n{content.rstrip()}\n```\n\n")

                if idx % max(1, len(files) // 20) == 0:   # каждые 5%
                    app.update_progress(idx)
    finally:
        app.set_progress(0)

    return out_path


# ───── GUI ──────────────────────
class App(tk.Tk):
    """Основное окно приложения."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Markdown‑генератор исходников")
        # Увеличиваем геометрию окна для удобства работы со списком файлов.
        self.geometry("800x700")
        self.resizable(False, False)

        # ───── Виджеты ────────────────────────────────
        tk.Label(self, text="Папка с исходниками:").grid(
            row=0,
            column=0,
            padx=10,
            pady=(15, 5),
            sticky=tk.W,
        )
        self.src_entry = tk.Entry(self, width=60)
        self.src_entry.grid(row=0, column=1, padx=5, pady=(15, 5))
        tk.Button(
            self, text="Обзор…", command=self.browse_src
        ).grid(row=0, column=2, padx=5)

        tk.Label(self, text="Расширения (запятая):").grid(
            row=1,
            column=0,
            sticky=tk.W,
            padx=10,
        )
        self.ext_entry = tk.Entry(self, width=60)
        self.ext_entry.grid(row=1, column=1, padx=5)

        tk.Label(self, text="Исключать папки (через запятую):").grid(
            row=2,
            column=0,
            sticky=tk.W,
            padx=10,
        )
        self.exclude_entry = tk.Entry(self, width=60)
        self.exclude_entry.grid(row=2, column=1, padx=5)

        tk.Button(
            self, text="Обзор…", command=self.browse_exclude
        ).grid(row=2, column=2, padx=5)

        tk.Label(self, text="Имя md‑файла:").grid(
            row=3,
            column=0,
            sticky=tk.W,
            padx=10,
        )
        self.out_entry = tk.Entry(self, width=60)
        self.out_entry.grid(row=3, column=1, padx=5)

        # ───── Список файлов ────────────────────────────────
        list_frame = ttk.LabelFrame(self, text="Список файлов для MD")
        list_frame.grid(row=4, column=0, columnspan=3, padx=10, pady=(15, 5), sticky="nsew")

        self.file_listbox = tk.Listbox(
            list_frame,
            width=80,
            height=12,
            selectmode=tk.MULTIPLE,
        )
        self.file_listbox.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=5)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=5)
        self.file_listbox.configure(yscrollcommand=scrollbar.set)

        btn_add_file = tk.Button(
            list_frame,
            text="Добавить файл…",
            command=self.add_file,
        )
        btn_add_file.grid(row=1, column=0, sticky="w", padx=(10, 5), pady=2)

        btn_remove_sel = tk.Button(
            list_frame,
            text="Удалить выбранные",
            command=self.remove_selected,
        )
        btn_remove_sel.grid(row=1, column=0, sticky="e", padx=(5, 10), pady=2)

        # ───── Генерация ────────────────────────────────
        self.generate_btn = tk.Button(
            self,
            text="Создать MD",
            command=self.on_generate_clicked,
        )
        self.generate_btn.grid(
            row=5, column=0, columnspan=3, pady=(20, 10)
        )

        # Статус и прогресс
        self.status = tk.Label(self, text="", fg="green")
        self.status.grid(row=6, column=0, columnspan=3)

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            self,
            variable=self.progress_var,
            maximum=100,  # будем использовать проценты
            length=650,
        )
        self.progress_bar.grid(row=7, column=0, columnspan=3, pady=(10, 5))

        # Открытие папки
        self.open_folder_btn = tk.Button(
            self,
            text="Открыть папку",
            command=self._open_output_folder,
            state=tk.DISABLED,
        )
        self.open_folder_btn.grid(row=8, column=0, columnspan=3, pady=(10, 5))

        # ───── Состояния ────────────────────────────────
        self.settings = Settings.from_env()
        self.load_settings_to_ui()
        self.last_out_path: Optional[pathlib.Path] = None
        self.is_generating: bool = False

        # Асинхронный цикл + executor
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()
        self.executor = ThreadPoolExecutor(max_workers=4)

        # Переменная для прогресса (число файлов)
        self._progress_total: int = 0

        # Список файлов, которые будут участвовать в генерации
        self.available_files: List[pathlib.Path] = []

    def _run_loop(self) -> None:
        """Запускает event‑loop в отдельном потоке."""
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        except Exception as exc:
            LOGGER.exception("Ошибка в цикле событий: %s", exc)

    # ───── Конфигурация UI ────────────────────────
    def load_settings_to_ui(self) -> None:
        self.src_entry.delete(0, tk.END)
        self.src_entry.insert(0, str(self.settings.source))

        self.ext_entry.delete(0, tk.END)
        self.ext_entry.insert(
            0,
            ",".join(ext.lstrip(".") for ext in sorted(self.settings.extensions)),
        )

        self.exclude_entry.delete(0, tk.END)
        self.exclude_entry.insert(
            0, ", ".join(sorted(self.settings.exclude))
        )

        self.out_entry.delete(0, tk.END)
        self.out_entry.insert(0, self.settings.output)

    def get_current_settings(self) -> Settings:
        return Settings(
            source=pathlib.Path(self.src_entry.get().strip()).expanduser(),
            extensions=parse_extensions(self.ext_entry.get()),
            exclude=parse_exclusions(self.exclude_entry.get()),
            output=self.out_entry.get().strip() or "source_dump.md",
        )

    # ───── Обработчики UI ────────────────────────
    def browse_src(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.src_entry.delete(0, tk.END)
            self.src_entry.insert(0, folder)
            # После выбора папки сразу загружаем список файлов.
            self.load_files_to_listbox()

    def browse_exclude(self) -> None:
        folder = filedialog.askdirectory()
        if not folder:
            return
        folder_name = pathlib.Path(folder).name.lower()
        parts = [
            p.strip() for p in self.exclude_entry.get().split(",") if p.strip()
        ]
        if folder_name not in (p.lower() for p in parts):
            parts.append(folder_name)
            new_text = ", ".join(parts)
            self.exclude_entry.delete(0, tk.END)
            self.exclude_entry.insert(0, new_text)

    def load_files_to_listbox(self) -> None:
        """
        Считываем файлы из выбранной папки согласно расширениям и исключениям
        и заполняем ListBox.
        """
        root_dir = pathlib.Path(self.src_entry.get().strip()).expanduser().resolve()
        if not root_dir.is_dir():
            return

        exts = parse_extensions(self.ext_entry.get())
        excludes = parse_exclusions(self.exclude_entry.get())

        files = collect_source_files(root_dir, exts, excludes)

        self.available_files = files
        self.file_listbox.delete(0, tk.END)
        for f in files:
            try:
                rel = f.relative_to(root_dir)
                display = str(rel)
            except ValueError:
                display = f.name
            self.file_listbox.insert(tk.END, display)

    def add_file(self) -> None:
        """
        Открывает диалог выбора файлов и добавляет их в список,
        если они удовлетворяют расширениям и не попадают под исключения.
        """
        root_dir = pathlib.Path(self.src_entry.get().strip()).expanduser().resolve()
        if not root_dir.is_dir():
            messagebox.showwarning("Предупреждение", "Сначала выберите папку с исходниками.")
            return

        selected_paths = filedialog.askopenfilenames(title="Выберите файлы для добавления")
        if not selected_paths:
            return

        exts = parse_extensions(self.ext_entry.get())
        excludes = parse_exclusions(self.exclude_entry.get())

        added_any = False
        for path_str in selected_paths:
            p = pathlib.Path(path_str).resolve()
            if not p.is_file():
                continue
            if p.suffix.lower() not in exts:
                continue
            # Проверяем, не попадает ли файл в исключённый каталог относительно root_dir.
            try:
                rel_parts = p.relative_to(root_dir).parts
            except ValueError:
                # Файл вне root_dir – игнорируем
                continue
            if any(part.lower() in excludes for part in rel_parts):
                continue

            if p in self.available_files:
                continue  # уже есть

            self.available_files.append(p)
            try:
                display = str(p.relative_to(root_dir))
            except ValueError:
                display = p.name
            self.file_listbox.insert(tk.END, display)
            added_any = True

        if not added_any:
            messagebox.showinfo("Информация", "Ни один файл не удовлетворил условиям.")

    def remove_selected(self) -> None:
        """
        Удаляет выбранные элементы из списка и внутреннего массива.
        """
        indices = list(map(int, self.file_listbox.curselection()))
        if not indices:
            return
        # удаляем с конца, чтобы индексы не смещались
        for idx in reversed(indices):
            del self.available_files[idx]
            self.file_listbox.delete(idx)

    def on_generate_clicked(self) -> None:
        if self.is_generating:
            return

        src_path_str = self.src_entry.get().strip()
        extensions_raw = self.ext_entry.get().strip()
        exclude_raw = self.exclude_entry.get().strip()
        out_name = self.out_entry.get().strip()

        if not src_path_str:
            messagebox.showerror("Ошибка", "Путь к папке не указан.")
            return
        if not extensions_raw:
            messagebox.showerror("Ошибка", "Расширения файлов не заданы.")
            return

        root_dir = pathlib.Path(src_path_str).expanduser().resolve()
        if not root_dir.is_dir():
            messagebox.showerror(
                "Ошибка",
                f"Папка {root_dir} не существует.",
            )
            return

        extensions_set = parse_extensions(extensions_raw)
        exclude_set = parse_exclusions(exclude_raw)

        # UI‑блокировка
        self.is_generating = True
        self.generate_btn.config(state=tk.DISABLED, text="Генерация…")
        self.open_folder_btn.config(state=tk.DISABLED)
        self.status.config(text="", fg="black")

        # Если пользователь не изменил список вручную – обновляем его.
        if not self.available_files:
            self.load_files_to_listbox()

        asyncio.run_coroutine_threadsafe(
            self._run_generation(root_dir, extensions_set, exclude_set, out_name),
            self.loop,
        )

    async def _run_generation(
            self,
            root: pathlib.Path,
            exts: Set[str],
            excludes: Set[str],
            out_name: str,
    ) -> None:
        """Обёртка над асинхронной генерацией с UI‑обновлениями."""
        try:
            # Передаём список файлов, сформированный в UI
            out_path = await _generate_async(
                self, root, self.available_files, out_name
            )
        except Exception as exc:
            LOGGER.exception("Ошибка генерации Markdown")
            self.after(0, lambda: [
                messagebox.showerror("Ошибка", f"Не удалось создать MD:\n{exc}"),
                self._reset_ui(),
            ])
            return

        # Успешно – сохраняем настройки
        self.settings = self.get_current_settings()
        self._save_env()

        self.last_out_path = out_path
        self.after(0, lambda: [
            self.open_folder_btn.config(state=tk.NORMAL),
            self.status.config(
                text=f"✅ {out_path.name} создан в {out_path.parent}",
                fg="green",
            ),
            self.generate_btn.config(state=tk.NORMAL, text="Создать MD"),
        ])
        self.is_generating = False

    def _reset_ui(self) -> None:
        self.generate_btn.config(state=tk.NORMAL, text="Создать MD")
        self.open_folder_btn.config(state=tk.DISABLED)
        self.status.config(text="", fg="black")
        self.is_generating = False

    # ───── Прогресс‑бар ─────────────────────────
    def set_progress(self, total: int) -> None:
        """Инициирует прогресс‑бар для указанного количества файлов."""
        if total <= 0:
            self.progress_var.set(0)
            return
        self._progress_total = total
        # прогресс от 0 до 100 %
        self.after(0, lambda: [
            self.progress_bar.config(maximum=100),
            self.progress_var.set(0),
            self.status.config(text="Начинаем сбор…"),
        ])

    def update_progress(self, current: int) -> None:
        """Обновление прогресса (вызывается из async‑задания)."""
        if not self._progress_total:
            return
        percent = (current / self._progress_total) * 100
        # UI‑обновления в главном потоке
        self.after(0, lambda: [
            self.progress_var.set(percent),
            self.status.config(
                text=f"Обработано {current} из {self._progress_total}"
            ),
        ])

    # ───── Открытие папки ─────────────────────────
    def _open_output_folder(self) -> None:
        if not self.last_out_path:
            messagebox.showinfo("Информация", "Файл ещё не создан.")
            return
        path_str = str(self.last_out_path.parent)
        try:
            if sys.platform == "win32":
                os.startfile(path_str)
            elif sys.platform == "darwin":
                subprocess.run(["open", path_str], check=True)
            else:
                subprocess.run(["xdg-open", path_str], check=True)
        except Exception as exc:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку:\n{exc}")

    # ───── Сохранение в .env ------------------------------
    def _save_env(self) -> None:
        """Записывает актуальные настройки в файл <script>.env."""
        _ensure_env_dir()
        set_key(ENV_PATH, "SOURCE", str(self.settings.source))
        set_key(
            ENV_PATH,
            "EXTENSIONS",
            ",".join(sorted(ext.lstrip(".") for ext in self.settings.extensions)),
        )
        set_key(
            ENV_PATH,
            "EXCLUDE",
            ",".join(sorted(self.settings.exclude)),
        )
        set_key(ENV_PATH, "OUTPUT", self.settings.output)


# ───── Запуск ----------------------------------------------
def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    sys.argv[:] = [sys.argv[0]]
    main()
