#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import base64
import hashlib
import json
import re
import tarfile
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "Figma Transfer GUI"
FORMAT_PREFIX = "FTPKG1"
TOKEN_RE = re.compile(r"FTPKG1\.[A-Za-z0-9_-]+\.[0-9a-f]{64}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_tar_xz(sources: list[Path]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="figma_transfer_") as tmp_dir:
        archive_path = Path(tmp_dir) / "bundle.tar.xz"

        with tarfile.open(archive_path, mode="w:xz") as tar:
            used_names: set[str] = set()

            for source in sources:
                arcname = Path("bundle") / source.name

                if str(arcname) in used_names:
                    if source.is_file():
                        stem = source.stem
                        suffix = source.suffix
                    else:
                        stem = source.name
                        suffix = ""

                    idx = 2
                    while True:
                        candidate_name = f"{stem}_{idx}{suffix}"
                        candidate = Path("bundle") / candidate_name
                        if str(candidate) not in used_names:
                            arcname = candidate
                            break
                        idx += 1

                used_names.add(str(arcname))
                tar.add(source, arcname=str(arcname))

        return archive_path.read_bytes()


def extract_tar_xz(archive_bytes: bytes, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="figma_restore_") as tmp_dir:
        archive_path = Path(tmp_dir) / "payload.tar.xz"
        archive_path.write_bytes(archive_bytes)

        with tarfile.open(archive_path, mode="r:xz") as tar:
            try:
                tar.extractall(output_dir, filter="data")
            except TypeError:
                tar.extractall(output_dir)


def build_package(sources: list[Path]) -> str:
    if not sources:
        raise ValueError("Нет источников для упаковки")

    archive_bytes = make_tar_xz(sources)

    if len(sources) == 1:
        source_kind = "dir" if sources[0].is_dir() else "file"
    else:
        source_kind = "multi"

    meta = {
        "names": [p.name for p in sources],
        "count": len(sources),
        "kind": source_kind,
        "archive": "tar.xz",
        "root": "bundle",
        "version": 2,
    }

    meta_bytes = json.dumps(
        meta,
        ensure_ascii=False,
        separators=(",", ":")
    ).encode("utf-8")

    payload = (
            len(meta_bytes).to_bytes(4, "big") +
            meta_bytes +
            archive_bytes
    )

    payload_hash = sha256_bytes(payload)
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    token = f"{FORMAT_PREFIX}.{encoded}.{payload_hash}"
    return token


def normalize_text_for_search(text: str) -> str:
    return re.sub(r"\s+", "", text)


def extract_token(text: str) -> str:
    compact = normalize_text_for_search(text)
    match = TOKEN_RE.search(compact)
    if not match:
        raise ValueError("Не найден корректный пакет в тексте")
    return match.group(0)


def decode_package(token: str):
    compact = normalize_text_for_search(token)
    parts = compact.split(".")

    if len(parts) != 3:
        raise ValueError("Неверный формат пакета")

    prefix, encoded, expected_hash = parts
    if prefix != FORMAT_PREFIX:
        raise ValueError("Неизвестный формат пакета")

    padding = "=" * ((4 - len(encoded) % 4) % 4)

    try:
        payload = base64.urlsafe_b64decode(encoded + padding)
    except Exception as exc:
        raise ValueError(f"Ошибка декодирования base64url: {exc}") from exc

    actual_hash = sha256_bytes(payload)
    if actual_hash != expected_hash:
        raise ValueError("Контрольная сумма не совпадает. Данные повреждены.")

    if len(payload) < 4:
        raise ValueError("Пакет слишком короткий")

    meta_len = int.from_bytes(payload[:4], "big")
    if len(payload) < 4 + meta_len:
        raise ValueError("Пакет поврежден: обрезаны метаданные")

    meta_bytes = payload[4:4 + meta_len]
    archive_bytes = payload[4 + meta_len:]

    try:
        meta = json.loads(meta_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Ошибка чтения метаданных: {exc}") from exc

    return meta, archive_bytes


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1040x820")
        self.minsize(900, 680)

        self.selected_paths: list[Path] = []

        self._build_ui()

    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        top = ttk.LabelFrame(root, text="Упаковка", padding=10)
        top.pack(fill="x", pady=(0, 10))

        btn_row = ttk.Frame(top)
        btn_row.pack(fill="x", pady=(0, 8))

        ttk.Button(
            btn_row,
            text="Добавить файлы",
            command=self.choose_files
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_row,
            text="Добавить папку",
            command=self.choose_dir
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            btn_row,
            text="Упаковать и скопировать",
            command=self.pack_and_copy
        ).pack(side="left", padx=(0, 8))

        self.source_var = tk.StringVar(value="Источники не выбраны")
        ttk.Label(top, textvariable=self.source_var).pack(fill="x", pady=(0, 8))

        list_frame = ttk.Frame(top)
        list_frame.pack(fill="both", expand=True, pady=(0, 8))

        self.sources_list = tk.Listbox(
            list_frame,
            height=8,
            selectmode=tk.EXTENDED,
            exportselection=False
        )
        self.sources_list.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.sources_list.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.sources_list.config(yscrollcommand=scrollbar.set)

        actions_row = ttk.Frame(top)
        actions_row.pack(fill="x", pady=(0, 8))

        ttk.Button(
            actions_row,
            text="Удалить выбранное",
            command=self.remove_selected
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            actions_row,
            text="Очистить список",
            command=self.clear_sources
        ).pack(side="left", padx=(0, 8))

        self.pack_info_var = tk.StringVar(value="Готов к упаковке")
        ttk.Label(top, textvariable=self.pack_info_var).pack(fill="x")

        mid = ttk.LabelFrame(root, text="Текст для Figma", padding=10)
        mid.pack(fill="both", expand=True, pady=(0, 10))

        text_btns = ttk.Frame(mid)
        text_btns.pack(fill="x", pady=(0, 8))

        ttk.Button(
            text_btns,
            text="Скопировать текст",
            command=self.copy_text
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            text_btns,
            text="Вставить из буфера",
            command=self.paste_text
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            text_btns,
            text="Очистить текст",
            command=self.clear_text
        ).pack(side="left", padx=(0, 8))

        self.text = tk.Text(mid, wrap="word", height=20)
        self.text.pack(fill="both", expand=True)

        bottom = ttk.LabelFrame(root, text="Распаковка", padding=10)
        bottom.pack(fill="x")

        unpack_btns = ttk.Frame(bottom)
        unpack_btns.pack(fill="x", pady=(0, 8))

        ttk.Button(
            unpack_btns,
            text="Вставить и распаковать",
            command=self.paste_and_unpack
        ).pack(side="left", padx=(0, 8))

        ttk.Button(
            unpack_btns,
            text="Распаковать из текста",
            command=self.unpack_from_text
        ).pack(side="left", padx=(0, 8))

        self.unpack_info_var = tk.StringVar(value="Готов к распаковке")
        ttk.Label(bottom, textvariable=self.unpack_info_var).pack(fill="x")

        tips = ttk.LabelFrame(root, text="Как использовать", padding=10)
        tips.pack(fill="x", pady=(10, 0))

        tips_text = (
            "1. Добавь один или несколько файлов/папок.\n"
            "2. Нажми «Упаковать и скопировать».\n"
            "3. Вставь полученный текст одним блоком в Figma.\n"
            "4. На другой стороне скопируй текст из Figma.\n"
            "5. Нажми «Вставить и распаковать» или сначала вставь в поле, потом «Распаковать из текста».\n\n"
            "Формат устойчив к пробелам и переносам строк: при распаковке они игнорируются."
        )
        ttk.Label(tips, text=tips_text, justify="left").pack(fill="x")

    def clear_list_selection(self):
        self.sources_list.selection_clear(0, "end")

    def choose_files(self):
        paths = filedialog.askopenfilenames()
        if not paths:
            return

        added = 0
        for path in paths:
            p = Path(path)
            if p not in self.selected_paths:
                self.selected_paths.append(p)
                added += 1

        self.refresh_sources_view()
        self.pack_info_var.set(
            f"Добавлено файлов: {added}. Всего объектов: {len(self.selected_paths)}"
        )

    def choose_dir(self):
        path = filedialog.askdirectory()
        if not path:
            return

        p = Path(path)
        if p not in self.selected_paths:
            self.selected_paths.append(p)

        self.refresh_sources_view()
        self.pack_info_var.set(f"Всего объектов: {len(self.selected_paths)}")

    def refresh_sources_view(self):
        self.sources_list.delete(0, "end")

        for p in self.selected_paths:
            prefix = "[DIR]" if p.is_dir() else "[FILE]"
            self.sources_list.insert("end", f"{prefix} {p}")

        self.clear_list_selection()

        if not self.selected_paths:
            self.source_var.set("Источники не выбраны")
        elif len(self.selected_paths) == 1:
            self.source_var.set(f"Выбран 1 объект: {self.selected_paths[0]}")
        else:
            self.source_var.set(f"Выбрано объектов: {len(self.selected_paths)}")

    def remove_selected(self):
        selection = list(self.sources_list.curselection())
        if not selection:
            messagebox.showwarning("Нет выбора", "Сначала выдели один или несколько элементов в списке")
            return

        for index in reversed(selection):
            del self.selected_paths[index]

        self.refresh_sources_view()
        self.clear_list_selection()
        self.pack_info_var.set(f"После удаления объектов: {len(self.selected_paths)}")

    def clear_sources(self):
        self.selected_paths.clear()
        self.refresh_sources_view()
        self.clear_list_selection()
        self.pack_info_var.set("Список источников очищен")

    def copy_text(self):
        content = self.text.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showwarning("Пусто", "Нет текста для копирования")
            return

        self.clipboard_clear()
        self.clipboard_append(content)
        self.update()

        self.pack_info_var.set(f"Текст скопирован в буфер. Длина: {len(content)} символов")

    def paste_text(self):
        try:
            content = self.clipboard_get()
        except Exception:
            messagebox.showerror("Ошибка", "Не удалось прочитать буфер обмена")
            return

        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.unpack_info_var.set(f"Текст вставлен. Длина: {len(content)} символов")

    def clear_text(self):
        self.text.delete("1.0", "end")
        self.pack_info_var.set("Текст очищен")
        self.unpack_info_var.set("Текст очищен")

    def pack_and_copy(self):
        if not self.selected_paths:
            messagebox.showwarning("Нет источника", "Сначала добавь хотя бы один файл или папку")
            return

        missing = [str(p) for p in self.selected_paths if not p.exists()]
        if missing:
            messagebox.showerror(
                "Ошибка",
                "Некоторые пути больше не существуют:\n\n" + "\n".join(missing[:20])
            )
            return

        try:
            token = build_package(self.selected_paths)

            self.text.delete("1.0", "end")
            self.text.insert("1.0", token)

            self.clipboard_clear()
            self.clipboard_append(token)
            self.update()

            self.pack_info_var.set(
                f"Упаковано объектов: {len(self.selected_paths)}. Длина токена: {len(token)} символов"
            )
        except Exception as exc:
            messagebox.showerror("Ошибка упаковки", str(exc))

    def paste_and_unpack(self):
        try:
            content = self.clipboard_get()
        except Exception:
            messagebox.showerror("Ошибка", "Не удалось прочитать буфер обмена")
            return

        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        self.unpack_from_text()

    def unpack_from_text(self):
        content = self.text.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showwarning("Пусто", "Нет текста для распаковки")
            return

        try:
            token = extract_token(content)
            meta, archive_bytes = decode_package(token)
        except Exception as exc:
            messagebox.showerror("Ошибка чтения пакета", str(exc))
            return

        out_dir = filedialog.askdirectory(title="Выбери папку для распаковки")
        if not out_dir:
            return

        try:
            output_dir = Path(out_dir)
            extract_tar_xz(archive_bytes, output_dir)

            restore_root = meta.get("root", "bundle")
            restored_path = output_dir / restore_root

            self.unpack_info_var.set(f"Распаковано: {restored_path}")

            if meta.get("count", 1) == 1:
                names_text = meta.get("names", ["объект"])[0]
            else:
                names_text = ", ".join(meta.get("names", []))

            messagebox.showinfo(
                "Готово",
                f"Успешно восстановлено:\n{restored_path}\n\nОбъектов: {meta.get('count', 1)}\n{names_text}"
            )
        except Exception as exc:
            messagebox.showerror("Ошибка распаковки", str(exc))

def main():
    try:
        app = App()
        app.mainloop()
    except Exception as exc:
        try:
            messagebox.showerror("Фатальная ошибка", str(exc))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()