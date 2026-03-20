#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Улучшенный Markdown‑генератор с детальной статистикой файлов.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import pathlib
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Set, Optional, Tuple, Dict

try:
    from dotenv import load_dotenv, set_key
except ModuleNotFoundError as exc:
    sys.stderr.write("pip install python-dotenv\n")
    raise exc

import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

ENV_PATH = pathlib.Path(__file__).parent / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

LOGGER = logging.getLogger("md_generator")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)


def human_readable_size(size_bytes: int) -> str:
    """Convert bytes to human readable format."""
    if size_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def count_lines(content: str) -> int:
    """Count non-empty lines."""
    return len([line for line in content.splitlines() if line.strip()])


def get_language_tag(extension: str) -> str:
    """Map extension to markdown language."""
    mapping = {
        '.kt': 'kotlin', '.java': 'java', '.py': 'python',
        '.js': 'javascript', '.ts': 'typescript', '.go': 'go',
        '.rs': 'rust', '.cpp': 'cpp', '.c': 'c', '.cs': 'csharp',
        '.rb': 'ruby', '.php': 'php', '.swift': 'swift',
        '.json': 'json', '.xml': 'xml', '.yaml': 'yaml', '.yml': 'yaml',
        '.sql': 'sql', '.sh': 'bash', '.html': 'html', '.css': 'css',
    }
    return mapping.get(extension.lower(), '')


def _ensure_env_dir() -> None:
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    source: pathlib.Path = field(default_factory=lambda: pathlib.Path.cwd())
    extensions: Set[str] = field(default_factory=lambda: {".kt", ".java", ".py"})
    exclude: Set[str] = field(default_factory=lambda: {"node_modules", "__pycache__", ".git"})
    output: str = "source_dump.md"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            source=pathlib.Path(os.getenv("SOURCE", "")).expanduser(),
            extensions=parse_extensions(os.getenv("EXTENSIONS", ".kt,.java")),
            exclude=parse_exclusions(os.getenv("EXCLUDE", "")),
            output=os.getenv("OUTPUT", "source_dump.md"),
        )


def _split_entries(raw: str) -> Set[str]:
    return {
        part.strip()
        for part in re.split(r'[\s,]+', raw.strip())
        if part.strip()
    }


def parse_extensions(raw: str) -> Set[str]:
    return {
        f".{ext.lstrip('.').lower()}"
        for ext in _split_entries(raw)
    }


def parse_exclusions(raw: str) -> Set[str]:
    return {name.strip().lower() for name in _split_entries(raw)}


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
        if any(part.lower() in exclude_dirs for part in p.parts):
            continue
        if not _is_text_file(p):
            LOGGER.debug("Пропуск бинарного файла: %s", p)
            continue
        files.append(p)

    return sorted(files)


def read_file_with_stats(file_path: pathlib.Path) -> Tuple[str, int, int]:
    """Return content, size_bytes, line_count."""
    try:
        size = file_path.stat().st_size
        content = file_path.read_text(encoding="utf-8", errors="replace")
        lines = count_lines(content)
        return content, size, lines
    except Exception as exc:
        LOGGER.warning("Не удалось прочитать %s: %s", file_path, exc)
        return "", 0, 0


async def _generate_async(
        app: "App",
        root: pathlib.Path,
        files: List[pathlib.Path],
        out_name: str,
) -> Tuple[pathlib.Path, Dict]:
    """Generate MD with statistics."""
    if not files:
        raise FileNotFoundError("Список файлов пуст")

    out_path = pathlib.Path(out_name).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    stats = {
        'total_files': 0,
        'total_lines': 0,
        'total_size': 0,
        'languages': set()
    }

    app.set_progress(len(files))

    try:
        with out_path.open("w", encoding="utf-8") as f:
            # Header
            f.write(f"# Source Code Documentation\n\n")
            f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  \n")
            f.write(f"**Root:** `{root}`\n\n")

            # Process files first to collect stats
            file_data = []
            for idx, path in enumerate(files, 1):
                if not app.is_generating:
                    break

                content, size, lines = await asyncio.to_thread(read_file_with_stats, path)
                if size > 0 or content:
                    file_data.append((path, content, size, lines))
                    stats['total_files'] += 1
                    stats['total_lines'] += lines
                    stats['total_size'] += size
                    stats['languages'].add(path.suffix.lower())

                if idx % max(1, len(files) // 20) == 0:
                    app.update_progress(idx)

            # Write summary
            f.write("## Summary Statistics\n\n")
            f.write(f"- **Total Files:** {stats['total_files']}\n")
            f.write(f"- **Total Lines:** {stats['total_lines']:,}\n")
            f.write(f"- **Total Size:** {human_readable_size(stats['total_size'])}\n")
            f.write(f"- **Languages:** {', '.join(sorted(stats['languages']))}\n")
            f.write(f"- **Avg Size:** {human_readable_size(stats['total_size'] // max(stats['total_files'], 1))}\n\n")
            f.write("---\n\n")

            # Write files with metadata
            for path, content, size, lines in file_data:
                try:
                    rel_path = path.relative_to(root)
                except ValueError:
                    rel_path = path.name

                lang_tag = get_language_tag(path.suffix)
                size_str = human_readable_size(size)

                f.write(f"### `{rel_path}`\n\n")
                f.write(f"**Size:** {size_str} | **Lines:** {lines} | **Type:** {path.suffix[1:].upper()}\n\n")
                f.write(f"```{lang_tag}\n{content.rstrip()}\n```\n\n")

    finally:
        app.set_progress(0)

    return out_path, stats


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Markdown Generator with Statistics")
        self.geometry("900x800")
        self.resizable(True, True)

        # Configure grid weights
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # Labels and Entries
        tk.Label(self, text="Source folder:", anchor="w").grid(
            row=0, column=0, padx=10, pady=(15, 5), sticky="ew"
        )
        self.src_entry = tk.Entry(self)
        self.src_entry.grid(row=0, column=1, padx=5, pady=(15, 5), sticky="ew")
        tk.Button(self, text="Browse…", command=self.browse_src).grid(row=0, column=2, padx=5)

        tk.Label(self, text="Extensions (comma):", anchor="w").grid(row=1, column=0, sticky="w", padx=10)
        self.ext_entry = tk.Entry(self)
        self.ext_entry.grid(row=1, column=1, padx=5, sticky="ew")

        tk.Label(self, text="Exclude folders:", anchor="w").grid(row=2, column=0, sticky="w", padx=10)
        self.exclude_entry = tk.Entry(self)
        self.exclude_entry.grid(row=2, column=1, padx=5, sticky="ew")
        tk.Button(self, text="Browse…", command=self.browse_exclude).grid(row=2, column=2, padx=5)

        tk.Label(self, text="Output filename:", anchor="w").grid(row=3, column=0, sticky="w", padx=10)
        self.out_entry = tk.Entry(self)
        self.out_entry.grid(row=3, column=1, padx=5, sticky="ew")

        # File list with scrollbar
        list_frame = ttk.LabelFrame(self, text="Files to Process")
        list_frame.grid(row=4, column=0, columnspan=3, padx=10, pady=10, sticky="nsew")
        list_frame.grid_columnconfigure(0, weight=1)
        list_frame.grid_rowconfigure(0, weight=1)

        self.file_listbox = tk.Listbox(list_frame, selectmode=tk.MULTIPLE, font=("Consolas", 9))
        self.file_listbox.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=5)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=5)
        self.file_listbox.configure(yscrollcommand=scrollbar.set)

        # Buttons frame
        btn_frame = tk.Frame(list_frame)
        btn_frame.grid(row=1, column=0, columnspan=2, pady=5)

        tk.Button(btn_frame, text="➕ Add Files", command=self.add_file).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="🗑️ Remove Selected", command=self.remove_selected).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="🔄 Refresh List", command=self.load_files_to_listbox).pack(side=tk.LEFT, padx=5)

        # Action buttons
        action_frame = tk.Frame(self)
        action_frame.grid(row=5, column=0, columnspan=3, pady=10)

        self.generate_btn = tk.Button(
            action_frame, text="📄 Generate MD", command=self.on_generate_clicked,
            bg="#4CAF50", fg="white", font=("Arial", 10, "bold"), padx=20
        )
        self.generate_btn.pack(side=tk.LEFT, padx=5)

        self.extract_btn = tk.Button(
            action_frame, text="📂 Extract MD", command=self.on_extract_clicked,
            bg="#2196F3", fg="white", font=("Arial", 10, "bold"), padx=20
        )
        self.extract_btn.pack(side=tk.LEFT, padx=5)

        # Statistics label
        self.stats_label = tk.Label(self, text="", fg="blue", font=("Arial", 9, "italic"))
        self.stats_label.grid(row=6, column=0, columnspan=3)

        # Status and Progress
        self.status = tk.Label(self, text="", fg="green")
        self.status.grid(row=7, column=0, columnspan=3)

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            self, variable=self.progress_var, maximum=100, length=750
        )
        self.progress_bar.grid(row=8, column=0, columnspan=3, pady=(10, 5), padx=10)

        # Open folder button
        self.open_folder_btn = tk.Button(
            self, text="📁 Open Output Folder", command=self._open_output_folder,
            state=tk.DISABLED
        )
        self.open_folder_btn.grid(row=9, column=0, columnspan=3, pady=(5, 15))

        # State
        self.settings = Settings.from_env()
        self.load_settings_to_ui()
        self.last_out_path: Optional[pathlib.Path] = None
        self.last_stats: Optional[Dict] = None
        self.is_generating: bool = False

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()
        self.executor = ThreadPoolExecutor(max_workers=4)
        self._progress_total: int = 0
        self.available_files: List[pathlib.Path] = []

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        except Exception as exc:
            LOGGER.exception("Event loop error: %s", exc)

    def load_settings_to_ui(self) -> None:
        self.src_entry.delete(0, tk.END)
        self.src_entry.insert(0, str(self.settings.source))

        self.ext_entry.delete(0, tk.END)
        self.ext_entry.insert(0, ",".join(ext.lstrip(".") for ext in sorted(self.settings.extensions)))

        self.exclude_entry.delete(0, tk.END)
        self.exclude_entry.insert(0, ", ".join(sorted(self.settings.exclude)))

        self.out_entry.delete(0, tk.END)
        self.out_entry.insert(0, self.settings.output)

    def get_current_settings(self) -> Settings:
        return Settings(
            source=pathlib.Path(self.src_entry.get().strip()).expanduser(),
            extensions=parse_extensions(self.ext_entry.get()),
            exclude=parse_exclusions(self.exclude_entry.get()),
            output=self.out_entry.get().strip() or "source_dump.md",
        )

    def browse_src(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.src_entry.delete(0, tk.END)
            self.src_entry.insert(0, folder)
            self.load_files_to_listbox()

    def browse_exclude(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            folder_name = pathlib.Path(folder).name.lower()
            parts = [p.strip() for p in self.exclude_entry.get().split(",") if p.strip()]
            if folder_name not in (p.lower() for p in parts):
                parts.append(folder_name)
                self.exclude_entry.delete(0, tk.END)
                self.exclude_entry.insert(0, ", ".join(parts))

    def load_files_to_listbox(self) -> None:
        root_dir = pathlib.Path(self.src_entry.get().strip()).expanduser().resolve()
        if not root_dir.is_dir():
            return

        exts = parse_extensions(self.ext_entry.get())
        excludes = parse_exclusions(self.exclude_entry.get())
        files = collect_source_files(root_dir, exts, excludes)

        self.available_files = files
        self.file_listbox.delete(0, tk.END)

        total_size = 0
        for f in files:
            try:
                size = f.stat().st_size
                total_size += size
                rel = f.relative_to(root_dir)
                display = f"{rel} ({human_readable_size(size)})"
            except ValueError:
                display = f"{f.name} ({human_readable_size(f.stat().st_size)})"
            self.file_listbox.insert(tk.END, display)

        self.stats_label.config(text=f"Files: {len(files)} | Total size: {human_readable_size(total_size)}")

    def add_file(self) -> None:
        root_dir = pathlib.Path(self.src_entry.get().strip()).expanduser().resolve()
        if not root_dir.is_dir():
            messagebox.showwarning("Warning", "Select source folder first.")
            return

        selected = filedialog.askopenfilenames(title="Select files")
        if not selected:
            return

        exts = parse_extensions(self.ext_entry.get())
        excludes = parse_exclusions(self.exclude_entry.get())

        for path_str in selected:
            p = pathlib.Path(path_str).resolve()
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            try:
                rel_parts = p.relative_to(root_dir).parts
            except ValueError:
                continue
            if any(part.lower() in excludes for part in rel_parts):
                continue
            if p not in self.available_files:
                self.available_files.append(p)
                try:
                    display = f"{p.relative_to(root_dir)} ({human_readable_size(p.stat().st_size)})"
                except ValueError:
                    display = f"{p.name} ({human_readable_size(p.stat().st_size)})"
                self.file_listbox.insert(tk.END, display)

    def remove_selected(self) -> None:
        indices = list(map(int, self.file_listbox.curselection()))
        if not indices:
            return
        for idx in reversed(indices):
            del self.available_files[idx]
            self.file_listbox.delete(idx)

    def on_generate_clicked(self) -> None:
        if self.is_generating:
            return

        settings = self.get_current_settings()
        if not settings.source.is_dir():
            messagebox.showerror("Error", f"Folder not found: {settings.source}")
            return

        self.is_generating = True
        self.generate_btn.config(state=tk.DISABLED, text="Generating…")
        self.open_folder_btn.config(state=tk.DISABLED)
        self.status.config(text="", fg="black")

        if not self.available_files:
            self.load_files_to_listbox()

        asyncio.run_coroutine_threadsafe(
            self._run_generation(settings.source, settings.extensions, settings.exclude, settings.output),
            self.loop,
        )

    async def _run_generation(self, root: pathlib.Path, exts: Set[str], excludes: Set[str], out_name: str) -> None:
        try:
            out_path, stats = await _generate_async(self, root, self.available_files, out_name)
        except Exception as exc:
            LOGGER.exception("Generation error")
            self.after(0, lambda: [
                messagebox.showerror("Error", f"Failed to generate:\n{exc}"),
                self._reset_ui(),
            ])
            return

        self.settings = self.get_current_settings()
        self._save_env()
        self.last_out_path = out_path
        self.last_stats = stats

        stats_text = f"Files: {stats['total_files']} | Lines: {stats['total_lines']:,} | Size: {human_readable_size(stats['total_size'])}"

        self.after(0, lambda: [
            self.open_folder_btn.config(state=tk.NORMAL),
            self.status.config(text=f"✅ {out_path.name} | {stats_text}", fg="green"),
            self.generate_btn.config(state=tk.NORMAL, text="📄 Generate MD"),
            self.stats_label.config(text=stats_text)
        ])
        self.is_generating = False

    def _reset_ui(self) -> None:
        self.generate_btn.config(state=tk.NORMAL, text="📄 Generate MD")
        self.extract_btn.config(state=tk.NORMAL, text="📂 Extract MD")
        self.open_folder_btn.config(state=tk.DISABLED)
        self.status.config(text="", fg="black")
        self.is_generating = False

    def set_progress(self, total: int) -> None:
        self._progress_total = total
        self.after(0, lambda: [
            self.progress_bar.config(maximum=100),
            self.progress_var.set(0),
            self.status.config(text=f"Processing {total} files…"),
        ])

    def update_progress(self, current: int) -> None:
        if not self._progress_total:
            return
        percent = (current / self._progress_total) * 100
        self.after(0, lambda: [
            self.progress_var.set(percent),
            self.status.config(text=f"Processed {current}/{self._progress_total}")
        ])

    def _open_output_folder(self) -> None:
        if not self.last_out_path:
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
            messagebox.showerror("Error", f"Cannot open folder:\n{exc}")

    def _save_env(self) -> None:
        _ensure_env_dir()
        set_key(ENV_PATH, "SOURCE", str(self.settings.source))
        set_key(ENV_PATH, "EXTENSIONS", ",".join(sorted(ext.lstrip(".") for ext in self.settings.extensions)))
        set_key(ENV_PATH, "EXCLUDE", ",".join(sorted(self.settings.exclude)))
        set_key(ENV_PATH, "OUTPUT", self.settings.output)

    def _select_md_file(self) -> Optional[pathlib.Path]:
        md_path = filedialog.askopenfilename(
            title="Select Markdown file",
            filetypes=[("Markdown", "*.md")],
        )
        return pathlib.Path(md_path).resolve() if md_path else None

    async def _extract_async(self, root: pathlib.Path, md_file: pathlib.Path) -> List[pathlib.Path]:
        md_text = md_file.read_text(encoding="utf-8", errors="replace")

        # Improved pattern to handle optional metadata line
        pattern = re.compile(
            r"###\s+`([^`]+)`\s*\n"
            r"(?:\*\*Size:\*\*[^|]+\|\s*\*\*Lines:\*\*[^|]+\|\s*\*\*Type:\*\*[^\n]*\n\n)?"
            r"```([^\s]*)\n(.*?)\n```",
            re.DOTALL,
        )
        matches = list(pattern.finditer(md_text))
        if not matches:
            raise RuntimeError("No code blocks found in Markdown")

        lang_to_ext = {v: k for k, v in {
            '.kt': 'kotlin', '.java': 'java', '.py': 'python',
            '.js': 'javascript', '.ts': 'typescript'
        }.items()}

        created_paths: List[pathlib.Path] = []
        for i, m in enumerate(matches, 1):
            rel_path_raw, lang_tag, code = m.group(1), m.group(2), m.group(3)
            rel_path = pathlib.Path(rel_path_raw)
            if rel_path.is_absolute():
                rel_path = pathlib.Path(rel_path.name)

            ext = lang_to_ext.get(lang_tag.lower()) or rel_path.suffix or ".txt"
            final_path = (root / rel_path).with_suffix(ext)
            final_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                final_path.write_text(code.strip() + "\n", encoding="utf-8")
                created_paths.append(final_path)
            except Exception as exc:
                raise RuntimeError(f"Cannot write {final_path}: {exc}")

            self.update_progress(i)
        return created_paths

    def on_extract_clicked(self) -> None:
        if self.is_generating:
            return
        md_file = self._select_md_file()
        if not md_file:
            return

        root_dir = pathlib.Path(self.src_entry.get().strip()).expanduser().resolve()
        if not root_dir.is_dir():
            messagebox.showerror("Error", f"Folder not found: {root_dir}")
            return

        self.is_generating = True
        self.extract_btn.config(state=tk.DISABLED, text="Extracting…")

        asyncio.run_coroutine_threadsafe(self._run_extraction(root_dir, md_file), self.loop)

    async def _run_extraction(self, root: pathlib.Path, md_file: pathlib.Path) -> None:
        try:
            created = await self._extract_async(root, md_file)
        except Exception as exc:
            LOGGER.exception("Extraction error")
            self.after(0, lambda: [
                messagebox.showerror("Error", f"Extraction failed:\n{exc}"),
                self._reset_ui(),
            ])
            return

        created_str = "\n".join(str(p.relative_to(root)) for p in created)
        self.after(0, lambda: [
            messagebox.showinfo("Done", f"Created {len(created)} files:\n{created_str}"),
            self._reset_ui(),
        ])


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()