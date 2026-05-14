#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

FORMAT_PREFIX = "FTPKG1"
TOKEN_RE = re.compile(r"FTPKG1\.[A-Za-z0-9_-]+\.[0-9a-f]{64}")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def normalize_text(text):
    return re.sub(r"\s+", "", text)


def make_tar_xz(source):
    with tempfile.TemporaryDirectory(prefix="ft_pack_") as tmp_dir:
        archive_path = Path(tmp_dir) / (source.name + ".tar.xz")
        with tarfile.open(archive_path, mode="w:xz") as tar:
            tar.add(source, arcname=source.name)
        return archive_path.read_bytes()


def extract_tar_xz(archive_bytes, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ft_unpack_") as tmp_dir:
        archive_path = Path(tmp_dir) / "payload.tar.xz"
        archive_path.write_bytes(archive_bytes)
        with tarfile.open(archive_path, mode="r:xz") as tar:
            tar.extractall(output_dir)


def build_token(source):
    archive_bytes = make_tar_xz(source)

    meta = {
        "name": source.name,
        "kind": "dir" if source.is_dir() else "file",
        "archive": "tar.xz",
        "version": 1,
    }
    meta_bytes = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    payload = len(meta_bytes).to_bytes(4, "big") + meta_bytes + archive_bytes
    payload_hash = sha256_bytes(payload)

    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return "{}.{}.{}".format(FORMAT_PREFIX, encoded, payload_hash)


def extract_token_from_text(text):
    compact = normalize_text(text)
    match = TOKEN_RE.search(compact)
    if not match:
        raise ValueError("Пакет не найден в тексте")
    return match.group(0)


def decode_token(token):
    compact = normalize_text(token)
    parts = compact.split(".")
    if len(parts) != 3:
        raise ValueError("Неверный формат пакета")

    prefix, encoded, expected_hash = parts
    if prefix != FORMAT_PREFIX:
        raise ValueError("Неизвестный формат пакета")

    padding = "=" * ((4 - len(encoded) % 4) % 4)
    payload = base64.urlsafe_b64decode(encoded + padding)

    actual_hash = sha256_bytes(payload)
    if actual_hash != expected_hash:
        raise ValueError("Контрольная сумма не совпадает")

    if len(payload) < 4:
        raise ValueError("Пакет поврежден")

    meta_len = int.from_bytes(payload[:4], "big")
    if len(payload) < 4 + meta_len:
        raise ValueError("Пакет поврежден: метаданные обрезаны")

    meta_bytes = payload[4:4 + meta_len]
    archive_bytes = payload[4 + meta_len:]

    meta = json.loads(meta_bytes.decode("utf-8"))
    return meta, archive_bytes


def try_set_clipboard(text):
    system = platform.system().lower()

    try:
        if "windows" in system:
            proc = subprocess.Popen(["clip"], stdin=subprocess.PIPE, text=True)
            proc.communicate(text)
            if proc.returncode == 0:
                return "windows:clip"

        if shutil.which("wl-copy"):
            proc = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE, text=True)
            proc.communicate(text)
            if proc.returncode == 0:
                return "linux:wl-copy"

        if shutil.which("xclip"):
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard"],
                stdin=subprocess.PIPE,
                text=True
            )
            proc.communicate(text)
            if proc.returncode == 0:
                return "linux:xclip"

        if shutil.which("xsel"):
            proc = subprocess.Popen(
                ["xsel", "--clipboard", "--input"],
                stdin=subprocess.PIPE,
                text=True
            )
            proc.communicate(text)
            if proc.returncode == 0:
                return "linux:xsel"

    except Exception:
        pass

    return None


def try_get_clipboard():
    system = platform.system().lower()

    try:
        if "windows" in system:
            result = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                text=True,
                stderr=subprocess.DEVNULL
            )
            if result.strip():
                return result

        if shutil.which("wl-paste"):
            result = subprocess.check_output(
                ["wl-paste", "-n"],
                text=True,
                stderr=subprocess.DEVNULL
            )
            if result.strip():
                return result

        if shutil.which("xclip"):
            result = subprocess.check_output(
                ["xclip", "-selection", "clipboard", "-o"],
                text=True,
                stderr=subprocess.DEVNULL
            )
            if result.strip():
                return result

        if shutil.which("xsel"):
            result = subprocess.check_output(
                ["xsel", "--clipboard", "--output"],
                text=True,
                stderr=subprocess.DEVNULL
            )
            if result.strip():
                return result

    except Exception:
        pass

    return None


def save_text_file(path, text):
    path.write_text(text, encoding="utf-8")


def read_text_file(path):
    return path.read_text(encoding="utf-8")


def pack_command(source_path, out_file=None):
    source = Path(source_path).resolve()
    if not source.exists():
        print("Ошибка: источник не найден: {}".format(source), file=sys.stderr)
        return 1

    print("[*] Упаковка: {}".format(source))
    token = build_token(source)

    if out_file:
        output_path = Path(out_file).resolve()
    else:
        output_path = Path.cwd() / "{}.ft.txt".format(source.name)

    save_text_file(output_path, token)
    clip_status = try_set_clipboard(token)

    print("[+] Готово")
    print("    Файл: {}".format(output_path))
    print("    Длина токена: {} символов".format(len(token)))
    if clip_status:
        print("    Буфер: {}".format(clip_status))
    else:
        print("    Буфер: недоступен")
    print("    Дальше вставь содержимое файла или буфера в Figma.")
    return 0


def unpack_command(input_path=None, out_dir=None):
    raw_text = None
    source_desc = None

    # 1. Явно заданный файл
    if input_path:
        path = Path(input_path).resolve()
        if not path.exists():
            print("Ошибка: файл не найден: {}".format(path), file=sys.stderr)
            return 1
        raw_text = read_text_file(path)
        source_desc = "file:{}".format(path)

    # 2. Пробуем буфер
    if not raw_text:
        raw_text = try_get_clipboard()
        if raw_text:
            source_desc = "clipboard"

    # 3. Пробуем stdin
    if not raw_text:
        if sys.stdin.isatty():
            print("[*] Буфер недоступен или пуст.")
            print("[*] Вставь текст пакета и заверши ввод:")
            print("    Linux: Ctrl+D")
            print("    Windows: Ctrl+Z затем Enter")
        raw_text = sys.stdin.read()
        if raw_text:
            source_desc = "stdin"

    if not raw_text or not raw_text.strip():
        print("Ошибка: нет текста для распаковки", file=sys.stderr)
        return 1

    try:
        token = extract_token_from_text(raw_text)
        meta, archive_bytes = decode_token(token)
    except Exception as exc:
        print("Ошибка распаковки: {}".format(exc), file=sys.stderr)
        return 1

    if out_dir:
        target_dir = Path(out_dir).resolve()
    else:
        target_dir = Path.cwd() / "restored"

    try:
        extract_tar_xz(archive_bytes, target_dir)
    except Exception as exc:
        print("Ошибка извлечения архива: {}".format(exc), file=sys.stderr)
        return 1

    restore_root = meta.get("root")
    if restore_root:
        restored_path = target_dir / restore_root
    elif "name" in meta:
        restored_path = target_dir / meta["name"]
    else:
        restored_path = target_dir

    print("[+] Готово")
    print("    Источник: {}".format(source_desc))
    print("    Восстановлено: {}".format(restored_path))
    return 0


def launch_gui():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except Exception as exc:
        print("Ошибка запуска GUI: {}".format(exc), file=sys.stderr)
        print_usage()
        return 1

    class FtGui(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("FT Transfer — упаковка и восстановление")
            self.minsize(920, 680)

            self.pack_source_var = tk.StringVar()
            self.pack_output_var = tk.StringVar()
            self.pack_text_name_var = tk.StringVar(value="pasted_text.txt")
            self.pack_copy_var = tk.BooleanVar(value=True)
            self.unpack_input_var = tk.StringVar()
            self.unpack_output_var = tk.StringVar(value=str((Path.cwd() / "restored").resolve()))
            self.status_var = tk.StringVar(value="Готово")
            self.busy = False

            self._configure_style()
            self._build_ui()

        def _configure_style(self):
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"), padding=(0, 0, 0, 4))
            style.configure("Subtitle.TLabel", font=("Segoe UI", 10), foreground="#5c6470")
            style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
            style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8))
            style.configure("TButton", padding=(8, 5))
            style.configure("TEntry", padding=5)

        def _build_ui(self):
            root = ttk.Frame(self, padding=18)
            root.pack(fill="both", expand=True)

            ttk.Label(root, text="FT Transfer", style="Title.TLabel").pack(anchor="w")
            ttk.Label(
                root,
                text="Удобная упаковка файлов, папок и вставленного текста в FTPKG-токен и восстановление обратно.",
                style="Subtitle.TLabel"
            ).pack(anchor="w", pady=(0, 14))

            notebook = ttk.Notebook(root)
            notebook.pack(fill="both", expand=True)

            self.pack_tab = ttk.Frame(notebook, padding=12)
            self.unpack_tab = ttk.Frame(notebook, padding=12)
            notebook.add(self.pack_tab, text="Упаковать")
            notebook.add(self.unpack_tab, text="Распаковать")

            self._build_pack_tab()
            self._build_unpack_tab()

            bottom = ttk.Frame(root)
            bottom.pack(fill="x", pady=(12, 0))
            self.progress = ttk.Progressbar(bottom, mode="indeterminate")
            self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
            ttk.Label(bottom, textvariable=self.status_var).pack(side="right")

        def _build_pack_tab(self):
            source_box = ttk.Labelframe(self.pack_tab, text="Что упаковать", style="Section.TLabelframe")
            source_box.pack(fill="x")
            self._path_row(
                source_box,
                "Файл или папка:",
                self.pack_source_var,
                [("Файл", self._choose_pack_file), ("Папка", self._choose_pack_dir)]
            )
            self._path_row(
                source_box,
                "Куда сохранить .ft.txt:",
                self.pack_output_var,
                [("Выбрать", self._choose_pack_output)]
            )
            ttk.Checkbutton(
                source_box,
                text="Скопировать токен в буфер обмена после упаковки",
                variable=self.pack_copy_var
            ).pack(anchor="w", padx=12, pady=(0, 10))

            text_box = ttk.Labelframe(self.pack_tab, text="Или вставьте текст без выбора файла", style="Section.TLabelframe")
            text_box.pack(fill="both", expand=True, pady=(12, 0))
            name_row = ttk.Frame(text_box, padding=(10, 8, 10, 0))
            name_row.pack(fill="x")
            ttk.Label(name_row, text="Имя файла в пакете:", width=22).pack(side="left")
            ttk.Entry(name_row, textvariable=self.pack_text_name_var).pack(side="left", fill="x", expand=True)
            ttk.Label(
                text_box,
                text="Если поле ниже не пустое, будет упакован этот текст как обычный .txt-файл.",
                style="Subtitle.TLabel"
            ).pack(anchor="w", padx=10, pady=(6, 0))
            self.pack_text = scrolledtext.ScrolledText(text_box, wrap="word", height=7, undo=True)
            self.pack_text.pack(fill="both", expand=True, padx=10, pady=(8, 8))

            text_actions = ttk.Frame(text_box)
            text_actions.pack(fill="x", padx=10, pady=(0, 10))
            ttk.Button(text_actions, text="Вставить из буфера", command=self._paste_pack_clipboard).pack(side="left")
            ttk.Button(text_actions, text="Очистить текст", command=self._clear_pack_text).pack(side="left", padx=(8, 0))

            actions = ttk.Frame(self.pack_tab)
            actions.pack(fill="x", pady=(12, 8))
            ttk.Button(actions, text="Упаковать", style="Accent.TButton", command=self._pack_clicked).pack(side="left")
            ttk.Button(actions, text="Очистить", command=self._clear_pack).pack(side="left", padx=(8, 0))
            ttk.Button(actions, text="Открыть папку результата", command=self._open_pack_output_folder).pack(side="right")

            preview_box = ttk.Labelframe(self.pack_tab, text="Готовый токен", style="Section.TLabelframe")
            preview_box.pack(fill="both", expand=True)
            self.pack_preview = scrolledtext.ScrolledText(preview_box, wrap="word", height=10, undo=True)
            self.pack_preview.pack(fill="both", expand=True, padx=10, pady=(8, 8))

            preview_actions = ttk.Frame(preview_box)
            preview_actions.pack(fill="x", padx=10, pady=(0, 10))
            ttk.Button(preview_actions, text="Копировать токен", command=self._copy_pack_preview).pack(side="left")
            ttk.Button(preview_actions, text="Сохранить как...", command=self._save_pack_preview_as).pack(side="left", padx=(8, 0))

        def _build_unpack_tab(self):
            input_box = ttk.Labelframe(self.unpack_tab, text="Откуда взять токен", style="Section.TLabelframe")
            input_box.pack(fill="x")
            self._path_row(
                input_box,
                "Файл .ft.txt:",
                self.unpack_input_var,
                [("Выбрать", self._choose_unpack_input), ("Загрузить", self._load_unpack_input_file)]
            )
            self._path_row(
                input_box,
                "Папка назначения:",
                self.unpack_output_var,
                [("Выбрать", self._choose_unpack_output)]
            )

            text_box = ttk.Labelframe(self.unpack_tab, text="Или вставьте токен / текст из Figma", style="Section.TLabelframe")
            text_box.pack(fill="both", expand=True, pady=(12, 0))
            self.unpack_text = scrolledtext.ScrolledText(text_box, wrap="word", height=14, undo=True)
            self.unpack_text.pack(fill="both", expand=True, padx=10, pady=(8, 8))

            text_actions = ttk.Frame(text_box)
            text_actions.pack(fill="x", padx=10, pady=(0, 10))
            ttk.Button(text_actions, text="Вставить из буфера", command=self._paste_unpack_clipboard).pack(side="left")
            ttk.Button(text_actions, text="Очистить текст", command=self._clear_unpack_text).pack(side="left", padx=(8, 0))
            ttk.Button(text_actions, text="Открыть папку назначения", command=self._open_unpack_output_folder).pack(side="right")

            actions = ttk.Frame(self.unpack_tab)
            actions.pack(fill="x", pady=(12, 0))
            ttk.Button(actions, text="Распаковать", style="Accent.TButton", command=self._unpack_clicked).pack(side="left")
            self.unpack_result_var = tk.StringVar(value="")
            ttk.Label(actions, textvariable=self.unpack_result_var).pack(side="left", padx=(12, 0))

        def _path_row(self, parent, label, variable, buttons):
            row = ttk.Frame(parent, padding=(12, 10, 12, 4))
            row.pack(fill="x")
            ttk.Label(row, text=label, width=22).pack(side="left")
            ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True, padx=(0, 8))
            for text, command in buttons:
                ttk.Button(row, text=text, command=command).pack(side="left", padx=(0, 4))

        def _choose_pack_file(self):
            path = filedialog.askopenfilename(title="Выберите файл для упаковки")
            if path:
                self._set_pack_source(path)

        def _choose_pack_dir(self):
            path = filedialog.askdirectory(title="Выберите папку для упаковки")
            if path:
                self._set_pack_source(path)

        def _set_pack_source(self, path):
            self.pack_source_var.set(path)
            if hasattr(self, "pack_text"):
                self.pack_text.delete("1.0", "end")
            source = Path(path)
            self.pack_output_var.set(str((source.parent / "{}.ft.txt".format(source.name)).resolve()))

        def _get_pack_text_filename(self):
            name = self.pack_text_name_var.get().strip() or "pasted_text.txt"
            name = name.replace("\\", "/").split("/")[-1].strip()
            name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", name)
            if not name or name in (".", ".."):
                name = "pasted_text.txt"
            return name

        def _choose_pack_output(self):
            initial = self.pack_output_var.get().strip() or None
            path = filedialog.asksaveasfilename(
                title="Куда сохранить токен",
                initialfile=Path(initial).name if initial else "payload.ft.txt",
                initialdir=str(Path(initial).parent) if initial else str(Path.cwd()),
                defaultextension=".txt",
                filetypes=[("FT token", "*.ft.txt"), ("Text files", "*.txt"), ("All files", "*.*")]
            )
            if path:
                self.pack_output_var.set(path)

        def _choose_unpack_input(self):
            path = filedialog.askopenfilename(
                title="Выберите файл с токеном",
                filetypes=[("FT token", "*.ft.txt"), ("Text files", "*.txt"), ("All files", "*.*")]
            )
            if path:
                self.unpack_input_var.set(path)

        def _choose_unpack_output(self):
            path = filedialog.askdirectory(title="Выберите папку назначения")
            if path:
                self.unpack_output_var.set(path)

        def _load_unpack_input_file(self):
            path_text = self.unpack_input_var.get().strip()
            if not path_text:
                self._choose_unpack_input()
                path_text = self.unpack_input_var.get().strip()
            if not path_text:
                return
            try:
                text = read_text_file(Path(path_text))
            except Exception as exc:
                messagebox.showerror("Не удалось прочитать файл", str(exc))
                return
            self.unpack_text.delete("1.0", "end")
            self.unpack_text.insert("1.0", text)
            self._set_status("Файл загружен")

        def _choose_unpack_input_or_clipboard(self):
            text = self.unpack_text.get("1.0", "end").strip()
            if text:
                return text, "поле ввода"

            path_text = self.unpack_input_var.get().strip()
            if path_text:
                return read_text_file(Path(path_text)), "файл:{}".format(Path(path_text).resolve())

            try:
                clipboard_text = self.clipboard_get()
            except tk.TclError:
                clipboard_text = ""
            if clipboard_text.strip():
                return clipboard_text, "буфер обмена"

            raise ValueError("Нет текста для распаковки: вставьте токен, выберите файл или скопируйте токен в буфер")

        def _pack_clicked(self):
            if self.busy:
                return

            pasted_text = self.pack_text.get("1.0", "end-1c")
            has_pasted_text = bool(pasted_text.strip())
            source_text = self.pack_source_var.get().strip()
            output_text = self.pack_output_var.get().strip()
            output_path = Path(output_text).resolve() if output_text else None

            if has_pasted_text:
                text_file_name = self._get_pack_text_filename()
                if output_path is None:
                    output_path = (Path.cwd() / "{}.ft.txt".format(text_file_name)).resolve()
                    self.pack_output_var.set(str(output_path))

                def work():
                    with tempfile.TemporaryDirectory(prefix="ft_gui_text_") as tmp_dir:
                        source = Path(tmp_dir) / text_file_name
                        save_text_file(source, pasted_text)
                        token = build_token(source)
                    if output_path:
                        save_text_file(output_path, token)
                    return token, output_path, "вставленный текст ({})".format(text_file_name)
            else:
                if not source_text:
                    messagebox.showwarning("Не выбран источник", "Выберите файл/папку или вставьте текст для упаковки.")
                    return
                source = Path(source_text).resolve()
                if not source.exists():
                    messagebox.showerror("Источник не найден", str(source))
                    return

                def work():
                    token = build_token(source)
                    if output_path:
                        save_text_file(output_path, token)
                    return token, output_path, str(source)

            def done(result):
                token, saved_path, source_desc = result
                self.pack_preview.delete("1.0", "end")
                self.pack_preview.insert("1.0", token)
                copied = False
                if self.pack_copy_var.get():
                    copied = self._copy_text_to_clipboard(token)
                parts = ["Упаковка завершена", "{} символов".format(len(token)), "источник: {}".format(source_desc)]
                if saved_path:
                    parts.append("файл: {}".format(saved_path))
                if copied:
                    parts.append("скопировано в буфер")
                self._set_status("; ".join(parts))
                messagebox.showinfo("Готово", "Токен создан{}.".format(" и скопирован в буфер" if copied else ""))

            self._run_background("Упаковка...", work, done)

        def _unpack_clicked(self):
            if self.busy:
                return
            output_text = self.unpack_output_var.get().strip()
            if not output_text:
                messagebox.showwarning("Не выбрана папка", "Выберите папку назначения.")
                return
            target_dir = Path(output_text).resolve()
            try:
                raw_text, source_desc = self._choose_unpack_input_or_clipboard()
            except Exception as exc:
                messagebox.showerror("Нет данных", str(exc))
                return

            def work():
                token = extract_token_from_text(raw_text)
                meta, archive_bytes = decode_token(token)
                extract_tar_xz(archive_bytes, target_dir)
                restore_root = meta.get("root")
                if restore_root:
                    restored_path = target_dir / restore_root
                elif "name" in meta:
                    restored_path = target_dir / meta["name"]
                else:
                    restored_path = target_dir
                return restored_path, source_desc, meta

            def done(result):
                restored_path, source_desc, meta = result
                self.unpack_result_var.set("Восстановлено: {}".format(restored_path))
                self._set_status("Распаковка завершена; источник: {}; тип: {}".format(source_desc, meta.get("kind", "?")))
                messagebox.showinfo("Готово", "Восстановлено:\n{}".format(restored_path))

            self._run_background("Распаковка...", work, done)

        def _run_background(self, status, work, done):
            self.busy = True
            self._set_status(status)
            self.progress.start(12)

            def runner():
                try:
                    result = work()
                    error = None
                except Exception as exc:
                    result = None
                    error = exc
                self.after(0, lambda: self._finish_background(result, error, done))

            threading.Thread(target=runner, daemon=True).start()

        def _finish_background(self, result, error, done):
            self.progress.stop()
            self.busy = False
            if error:
                self._set_status("Ошибка: {}".format(error))
                messagebox.showerror("Ошибка", str(error))
                return
            done(result)

        def _copy_text_to_clipboard(self, text):
            try:
                self.clipboard_clear()
                self.clipboard_append(text)
                self.update_idletasks()
                return True
            except tk.TclError:
                return bool(try_set_clipboard(text))

        def _copy_pack_preview(self):
            text = self.pack_preview.get("1.0", "end").strip()
            if not text:
                messagebox.showwarning("Нет токена", "Сначала упакуйте файл, папку или текст.")
                return
            if self._copy_text_to_clipboard(text):
                self._set_status("Токен скопирован в буфер")
            else:
                messagebox.showerror("Буфер недоступен", "Не удалось скопировать токен.")

        def _save_pack_preview_as(self):
            text = self.pack_preview.get("1.0", "end").strip()
            if not text:
                messagebox.showwarning("Нет токена", "Сначала упакуйте файл, папку или текст.")
                return
            path = filedialog.asksaveasfilename(
                title="Сохранить токен",
                defaultextension=".txt",
                filetypes=[("FT token", "*.ft.txt"), ("Text files", "*.txt"), ("All files", "*.*")]
            )
            if path:
                save_text_file(Path(path), text)
                self.pack_output_var.set(path)
                self._set_status("Токен сохранен: {}".format(path))

        def _paste_pack_clipboard(self):
            try:
                text = self.clipboard_get()
            except tk.TclError:
                text = try_get_clipboard() or ""
            if not text.strip():
                messagebox.showwarning("Буфер пуст", "В буфере обмена нет текста для упаковки.")
                return
            self.pack_source_var.set("")
            self.pack_text.delete("1.0", "end")
            self.pack_text.insert("1.0", text)
            if not self.pack_output_var.get().strip():
                text_file_name = self._get_pack_text_filename()
                self.pack_output_var.set(str((Path.cwd() / "{}.ft.txt".format(text_file_name)).resolve()))
            self._set_status("Текст для упаковки вставлен из буфера")

        def _clear_pack_text(self):
            self.pack_text.delete("1.0", "end")
            self._set_status("Текст для упаковки очищен")

        def _paste_unpack_clipboard(self):
            try:
                text = self.clipboard_get()
            except tk.TclError:
                text = try_get_clipboard() or ""
            if not text.strip():
                messagebox.showwarning("Буфер пуст", "В буфере обмена нет текста токена.")
                return
            self.unpack_text.delete("1.0", "end")
            self.unpack_text.insert("1.0", text)
            self._set_status("Текст вставлен из буфера")

        def _clear_pack(self):
            self.pack_source_var.set("")
            self.pack_output_var.set("")
            self.pack_text_name_var.set("pasted_text.txt")
            self.pack_text.delete("1.0", "end")
            self.pack_preview.delete("1.0", "end")
            self._set_status("Готово")

        def _clear_unpack_text(self):
            self.unpack_text.delete("1.0", "end")
            self.unpack_result_var.set("")
            self._set_status("Готово")

        def _open_pack_output_folder(self):
            path_text = self.pack_output_var.get().strip()
            path = Path(path_text).resolve().parent if path_text else Path.cwd()
            self._open_path(path)

        def _open_unpack_output_folder(self):
            path_text = self.unpack_output_var.get().strip()
            self._open_path(Path(path_text).resolve() if path_text else Path.cwd())

        def _open_path(self, path):
            try:
                if platform.system().lower().startswith("windows"):
                    os.startfile(str(path))
                elif platform.system().lower() == "darwin":
                    subprocess.Popen(["open", str(path)])
                else:
                    subprocess.Popen(["xdg-open", str(path)])
            except Exception as exc:
                messagebox.showerror("Не удалось открыть", str(exc))

        def _set_status(self, text):
            self.status_var.set(text)

    app = FtGui()
    app.mainloop()
    return 0


def print_usage():
    print("Использование:")
    print("  python ft.py                         # запустить GUI")
    print("  python ft.py gui                     # запустить GUI")
    print("  python ft.py pack <файл_или_папка> [выходной_txt]")
    print("  python ft.py unpack [входной_txt] [папка_назначения]")
    print("")
    print("Примеры:")
    print("  python ft.py")
    print("  python ft.py pack ./project")
    print("  python ft.py pack ./project ./payload.txt")
    print("  python ft.py unpack")
    print("  python ft.py unpack ./payload.txt")
    print("  python ft.py unpack ./payload.txt ./out")
    print("")
    print("Если буфер недоступен, для unpack можно вставить текст прямо в терминал.")


def main():
    if len(sys.argv) < 2:
        return launch_gui()

    cmd = sys.argv[1].lower()

    if cmd in ("gui", "--gui"):
        return launch_gui()

    if cmd in ("help", "-h", "--help"):
        print_usage()
        return 0

    if cmd == "pack":
        if len(sys.argv) < 3:
            print_usage()
            return 1
        source_path = sys.argv[2]
        out_file = sys.argv[3] if len(sys.argv) >= 4 else None
        return pack_command(source_path, out_file)

    if cmd == "unpack":
        input_path = sys.argv[2] if len(sys.argv) >= 3 else None
        out_dir = sys.argv[3] if len(sys.argv) >= 4 else None
        return unpack_command(input_path, out_dir)

    print_usage()
    return 1


if __name__ == "__main__":
    sys.exit(main())
