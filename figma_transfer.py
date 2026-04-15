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


def safe_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def make_tar_xz(source: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="figma_transfer_") as tmp_dir:
        archive_path = Path(tmp_dir) / (source.name + ".tar.xz")
        with tarfile.open(archive_path, mode="w:xz") as tar:
            tar.add(source, arcname=source.name)
        return archive_path.read_bytes()


def extract_tar_xz(archive_bytes: bytes, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="figma_restore_") as tmp_dir:
        archive_path = Path(tmp_dir) / "payload.tar.xz"
        archive_path.write_bytes(archive_bytes)
        with tarfile.open(archive_path, mode="r:xz") as tar:
            tar.extractall(output_dir, filter="data")


def build_package(source: Path) -> str:
    archive_bytes = make_tar_xz(source)

    meta = {
        "name": source.name,
        "kind": "dir" if source.is_dir() else "file",
        "archive": "tar.xz",
        "version": 1,
    }
    meta_bytes = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

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
        self.geometry("980x760")
        self.minsize(860, 620)

        self.selected_path = None

        self._build_ui()

    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        top = ttk.LabelFrame(root, text="Упаковка", padding=10)
        top.pack(fill="x", pady=(0, 10))

        btn_row = ttk.Frame(top)
        btn_row.pack(fill="x", pady=(0, 8))

        ttk.Button(btn_row, text="Выбрать файл", command=self.choose_file).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Выбрать папку", command=self.choose_dir).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Упаковать и скопировать", command=self.pack_and_copy).pack(side="left", padx=(0, 8))

        self.source_var = tk.StringVar(value="Источник не выбран")
        ttk.Label(top, textvariable=self.source_var).pack(fill="x", pady=(0, 8))

        self.pack_info_var = tk.StringVar(value="Готов к упаковке")
        ttk.Label(top, textvariable=self.pack_info_var).pack(fill="x")

        mid = ttk.LabelFrame(root, text="Текст для Figma", padding=10)
        mid.pack(fill="both", expand=True, pady=(0, 10))

        text_btns = ttk.Frame(mid)
        text_btns.pack(fill="x", pady=(0, 8))

        ttk.Button(text_btns, text="Скопировать текст", command=self.copy_text).pack(side="left", padx=(0, 8))
        ttk.Button(text_btns, text="Вставить из буфера", command=self.paste_text).pack(side="left", padx=(0, 8))
        ttk.Button(text_btns, text="Очистить", command=self.clear_text).pack(side="left", padx=(0, 8))

        self.text = tk.Text(mid, wrap="word", height=20)
        self.text.pack(fill="both", expand=True)

        bottom = ttk.LabelFrame(root, text="Распаковка", padding=10)
        bottom.pack(fill="x")

        unpack_btns = ttk.Frame(bottom)
        unpack_btns.pack(fill="x", pady=(0, 8))

        ttk.Button(unpack_btns, text="Вставить и распаковать", command=self.paste_and_unpack).pack(side="left",
                                                                                                   padx=(0, 8))
        ttk.Button(unpack_btns, text="Распаковать из текста", command=self.unpack_from_text).pack(side="left",
                                                                                                  padx=(0, 8))

        self.unpack_info_var = tk.StringVar(value="Готов к распаковке")
        ttk.Label(bottom, textvariable=self.unpack_info_var).pack(fill="x")

        tips = ttk.LabelFrame(root, text="Как использовать", padding=10)
        tips.pack(fill="x", pady=(10, 0))

        tips_text = (
            "1. Выбери файл или папку.\n"
            "2. Нажми «Упаковать и скопировать».\n"
            "3. Вставь полученный текст одним блоком в Figma.\n"
            "4. На другой стороне скопируй текст из Figma.\n"
            "5. Нажми «Вставить и распаковать» или сначала вставь в поле, потом «Распаковать из текста».\n\n"
            "Формат устойчив к пробелам и переносам строк: при распаковке они игнорируются."
        )
        ttk.Label(tips, text=tips_text, justify="left").pack(fill="x")

    def choose_file(self):
        path = filedialog.askopenfilename()
        if path:
            self.selected_path = Path(path)
            self.source_var.set(f"Файл: {self.selected_path}")

    def choose_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.selected_path = Path(path)
            self.source_var.set(f"Папка: {self.selected_path}")

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
        self.pack_info_var.set("Поле очищено")
        self.unpack_info_var.set("Поле очищено")

    def pack_and_copy(self):
        if not self.selected_path:
            messagebox.showwarning("Нет источника", "Сначала выбери файл или папку")
            return

        try:
            token = build_package(self.selected_path)
            self.text.delete("1.0", "end")
            self.text.insert("1.0", token)

            self.clipboard_clear()
            self.clipboard_append(token)
            self.update()

            self.pack_info_var.set(
                f"Упаковано и скопировано. Длина токена: {len(token)} символов"
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
            restored_path = output_dir / meta["name"]
            self.unpack_info_var.set(
                f"Распаковано: {restored_path}"
            )
            messagebox.showinfo(
                "Готово",
                f"Успешно восстановлено:\n{restored_path}"
            )
        except Exception as exc:
            messagebox.showerror("Ошибка распаковки", str(exc))


def main():
    try:
        app = App()
        app.mainloop()
    except Exception as exc:
        messagebox.showerror("Фатальная ошибка", str(exc))
        raise


if __name__ == "__main__":
    main()
