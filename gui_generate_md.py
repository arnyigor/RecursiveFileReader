#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Markdown-генератор с поддержкой ограничений ввода.
Фикс: нижние кнопки всегда видны независимо от размера списка.
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
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Set, Optional, Tuple, Dict

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


@dataclass(frozen=True)
class OutputLimits:
    max_chars: int = 0
    max_lines: int = 0
    max_tokens: int = 0
    chars_per_token: float = 3.5

    @property
    def has_any_limit(self) -> bool:
        return self.max_chars > 0 or self.max_lines > 0 or self.max_tokens > 0

    def effective_max_chars(self) -> int:
        limits = []
        if self.max_chars > 0:
            limits.append(self.max_chars)
        if self.max_tokens > 0:
            limits.append(int(self.max_tokens * self.chars_per_token))
        return min(limits) if limits else 0

    def estimate_tokens(self, text: str) -> int:
        return int(math.ceil(len(text) / self.chars_per_token))


def human_readable_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def count_lines(content: str) -> int:
    return len([line for line in content.splitlines() if line.strip()])


def get_language_tag(extension: str) -> str:
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


def _split_entries(raw: str) -> Set[str]:
    return {
        part.strip()
        for part in re.split(r'[\s,]+', raw.strip())
        if part.strip()
    }


def parse_extensions(raw: str) -> Set[str]:
    return {f".{ext.lstrip('.').lower()}" for ext in _split_entries(raw)}


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
            continue
        files.append(p)
    return sorted(files)


def read_file_with_stats(file_path: pathlib.Path) -> Tuple[str, int, int]:
    try:
        size = file_path.stat().st_size
        content = file_path.read_text(encoding="utf-8", errors="replace")
        lines = count_lines(content)
        return content, size, lines
    except Exception as exc:
        LOGGER.warning("Cannot read %s: %s", file_path, exc)
        return "", 0, 0


@dataclass(frozen=True)
class Settings:
    source: pathlib.Path = field(default_factory=lambda: pathlib.Path.cwd())
    extensions: Set[str] = field(default_factory=lambda: {".kt", ".java", ".py"})
    exclude: Set[str] = field(
        default_factory=lambda: {"node_modules", "__pycache__", ".git"}
    )
    output: str = "source_dump.md"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            source=pathlib.Path(os.getenv("SOURCE", "")).expanduser(),
            extensions=parse_extensions(os.getenv("EXTENSIONS", ".kt,.java")),
            exclude=parse_exclusions(os.getenv("EXCLUDE", "")),
            output=os.getenv("OUTPUT", "source_dump.md"),
        )


# ═══════════════════════════════════════════════════════════
#  Чанки
# ═══════════════════════════════════════════════════════════

@dataclass
class ChunkInfo:
    part_number: int
    total_parts: int
    content: str
    char_count: int
    line_count: int
    token_estimate: int
    files_included: List[str]


def _build_file_block(rel_path, content, size, lines, lang_tag):
    size_str = human_readable_size(size)
    suffix = pathlib.Path(rel_path).suffix
    type_str = suffix[1:].upper() if suffix else "TXT"
    return (
        f"### `{rel_path}`\n\n"
        f"**Size:** {size_str} | **Lines:** {lines} | **Type:** {type_str}\n\n"
        f"```{lang_tag}\n{content.rstrip()}\n```\n\n"
    )


def _build_header(root, part=0, total=0):
    h = f"# Source Code Documentation\n\n"
    h += f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  \n"
    h += f"**Root:** `{root}`\n\n"
    if total > 1:
        h += f"**Part {part} of {total}**\n\n"
    return h


def _build_summary(stats):
    s = "## Summary Statistics\n\n"
    s += f"- **Total Files:** {stats['total_files']}\n"
    s += f"- **Total Lines:** {stats['total_lines']:,}\n"
    s += f"- **Total Size:** {human_readable_size(stats['total_size'])}\n"
    s += f"- **Languages:** {', '.join(sorted(stats['languages']))}\n"
    avg = stats['total_size'] // max(stats['total_files'], 1)
    s += f"- **Avg Size:** {human_readable_size(avg)}\n\n---\n\n"
    return s


def split_into_chunks(root, file_data, limits, stats):
    if not limits.has_any_limit:
        all_content = _build_header(root) + _build_summary(stats)
        all_files = []
        for path, content, size, lines in file_data:
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = path.name
            all_content += _build_file_block(rel, content, size, lines, get_language_tag(path.suffix))
            all_files.append(rel)
        return [ChunkInfo(1, 1, all_content, len(all_content),
                          all_content.count('\n'),
                          limits.estimate_tokens(all_content), all_files)]

    effective_limit = limits.effective_max_chars()
    max_lines_limit = limits.max_lines
    header_len = len(_build_header(root, 1, 99))

    blocks = []
    for path, content, size, lines in file_data:
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = path.name
        blocks.append((rel, _build_file_block(rel, content, size, lines, get_language_tag(path.suffix))))

    summary_text = _build_summary(stats)
    chunks, current_files, current_text, current_lines = [], [], "", 0

    def _flush():
        nonlocal current_text, current_files, current_lines
        if current_text.strip():
            chunks.append(ChunkInfo(len(chunks) + 1, 0, current_text,
                                    len(current_text), current_lines,
                                    limits.estimate_tokens(current_text),
                                    list(current_files)))
        current_text, current_files, current_lines = "", [], 0

    current_text = _build_header(root, 1, 0) + summary_text
    current_lines = current_text.count('\n')

    for rel_path, block in blocks:
        bc, bl = len(block), block.count('\n')
        over_chars = effective_limit > 0 and (len(current_text) + bc) > effective_limit
        over_lines = max_lines_limit > 0 and (current_lines + bl) > max_lines_limit
        if (over_chars or over_lines) and current_files:
            _flush()
            current_text = _build_header(root, len(chunks) + 1, 0)
            current_lines = current_text.count('\n')

        if effective_limit > 0 and bc > effective_limit - header_len:
            avail = effective_limit - len(current_text) - 100
            if avail > 200:
                t = block[:avail] + f"\n\n<!-- TRUNCATED: {bc} chars -->\n\n"
                current_text += t
                current_lines += t.count('\n')
                current_files.append(f"{rel_path} [TRUNCATED]")
            else:
                _flush()
                current_text = _build_header(root, len(chunks) + 1, 0)
                t = block[:max(effective_limit - header_len - 100, 200)]
                t += "\n\n<!-- TRUNCATED -->\n\n"
                current_text += t
                current_lines = current_text.count('\n')
                current_files.append(f"{rel_path} [TRUNCATED]")
        else:
            current_text += block
            current_lines += bl
            current_files.append(rel_path)
    _flush()

    total = len(chunks)
    final = []
    for c in chunks:
        if total > 1:
            content = c.content.replace(
                _build_header(root, c.part_number, 0),
                _build_header(root, c.part_number, total), 1)
        else:
            content = c.content
        final.append(ChunkInfo(c.part_number, total, content,
                               len(content), content.count('\n'),
                               limits.estimate_tokens(content), c.files_included))
    return final


async def _generate_async(app, root, files, out_name, limits):
    if not files:
        raise FileNotFoundError("File list is empty")
    stats = {'total_files': 0, 'total_lines': 0, 'total_size': 0, 'languages': set()}
    app.set_progress(len(files))
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

    chunks = split_into_chunks(root, file_data, limits, stats)
    out_base = pathlib.Path(out_name).expanduser().resolve()
    out_base.parent.mkdir(parents=True, exist_ok=True)
    created = []
    if len(chunks) == 1:
        out_base.write_text(chunks[0].content, encoding="utf-8")
        created.append(out_base)
    else:
        for c in chunks:
            p = out_base.parent / f"{out_base.stem}_part{c.part_number}{out_base.suffix or '.md'}"
            p.write_text(c.content, encoding="utf-8")
            created.append(p)
    stats['chunks'] = len(chunks)
    stats['chunk_details'] = [
        {'part': c.part_number, 'chars': c.char_count, 'lines': c.line_count,
         'tokens': c.token_estimate, 'files': len(c.files_included)}
        for c in chunks
    ]
    app.set_progress(0)
    return created, stats


# ═══════════════════════════════════════════════════════════
#  GUI — трёхзонная компоновка
# ═══════════════════════════════════════════════════════════
#
#  ┌─────────────────────────────────────┐
#  │  TOP_FRAME (фиксированная высота)  │  ← настройки, лимиты
#  ├─────────────────────────────────────┤
#  │                                     │
#  │  MID_FRAME (растягивается)         │  ← список файлов
#  │                                     │
#  ├─────────────────────────────────────┤
#  │  BOT_FRAME (фиксированная высота)  │  ← кнопки, прогресс
#  └─────────────────────────────────────┘
#
#  BOT_FRAME всегда виден, даже если файлов 10 000.

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Markdown Generator — with Input Limits")
        self.geometry("950x850")
        self.minsize(700, 500)
        self.resizable(True, True)

        # ═══ Три зоны ═══
        # TOP — не растягивается
        top_frame = tk.Frame(self)
        top_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(10, 0))

        # MID — растягивается (список файлов)
        mid_frame = tk.Frame(self)
        mid_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)

        # BOT — не растягивается, ВСЕГДА ВИДНА
        bot_frame = tk.Frame(self)
        bot_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(0, 10))

        # ═══════════════════════════════════════
        #  TOP: Настройки
        # ═══════════════════════════════════════
        settings_grid = tk.Frame(top_frame)
        settings_grid.pack(fill=tk.X)
        settings_grid.grid_columnconfigure(1, weight=1)

        r = 0
        # Source folder
        tk.Label(settings_grid, text="Source folder:", anchor="w").grid(
            row=r, column=0, padx=(0, 5), pady=3, sticky="w")
        self.src_entry = tk.Entry(settings_grid)
        self.src_entry.grid(row=r, column=1, padx=5, pady=3, sticky="ew")
        tk.Button(settings_grid, text="Browse…", command=self.browse_src).grid(
            row=r, column=2, padx=(5, 0), pady=3)
        r += 1

        # Extensions
        tk.Label(settings_grid, text="Extensions:", anchor="w").grid(
            row=r, column=0, padx=(0, 5), pady=3, sticky="w")
        self.ext_entry = tk.Entry(settings_grid)
        self.ext_entry.grid(row=r, column=1, padx=5, pady=3, sticky="ew")
        r += 1

        # Exclude
        tk.Label(settings_grid, text="Exclude folders:", anchor="w").grid(
            row=r, column=0, padx=(0, 5), pady=3, sticky="w")
        self.exclude_entry = tk.Entry(settings_grid)
        self.exclude_entry.grid(row=r, column=1, padx=5, pady=3, sticky="ew")
        tk.Button(settings_grid, text="Browse…", command=self.browse_exclude).grid(
            row=r, column=2, padx=(5, 0), pady=3)
        r += 1

        # Output
        tk.Label(settings_grid, text="Output filename:", anchor="w").grid(
            row=r, column=0, padx=(0, 5), pady=3, sticky="w")
        self.out_entry = tk.Entry(settings_grid)
        self.out_entry.grid(row=r, column=1, padx=5, pady=3, sticky="ew")
        r += 1

        # ── Limits ──
        limits_frame = ttk.LabelFrame(top_frame, text="⚙ Output Limits (0 = no limit)")
        limits_frame.pack(fill=tk.X, pady=(8, 0))

        lim_row = tk.Frame(limits_frame)
        lim_row.pack(fill=tk.X, padx=10, pady=5)

        tk.Label(lim_row, text="Max chars:").pack(side=tk.LEFT, padx=(0, 2))
        self.max_chars_var = tk.StringVar(value="0")
        tk.Entry(lim_row, textvariable=self.max_chars_var, width=9).pack(
            side=tk.LEFT, padx=(0, 15))

        tk.Label(lim_row, text="Max lines:").pack(side=tk.LEFT, padx=(0, 2))
        self.max_lines_var = tk.StringVar(value="0")
        tk.Entry(lim_row, textvariable=self.max_lines_var, width=9).pack(
            side=tk.LEFT, padx=(0, 15))

        tk.Label(lim_row, text="Max tokens:").pack(side=tk.LEFT, padx=(0, 2))
        self.max_tokens_var = tk.StringVar(value="0")
        tk.Entry(lim_row, textvariable=self.max_tokens_var, width=9).pack(
            side=tk.LEFT, padx=(0, 5))

        # Presets
        presets_row = tk.Frame(limits_frame)
        presets_row.pack(fill=tk.X, padx=10, pady=(0, 5))
        tk.Label(presets_row, text="Presets:", fg="gray").pack(side=tk.LEFT, padx=(0, 5))

        for name, chars, lines, tokens in [
            ("No limit", 0, 0, 0),
            ("3000 lines", 0, 3000, 0),
        ]:
            tk.Button(
                presets_row, text=name, font=("Arial", 8),
                command=lambda c=chars, l=lines, t=tokens: self._apply_preset(c, l, t),
            ).pack(side=tk.LEFT, padx=2)

        # Limit counter
        self.limit_counter_label = tk.Label(
            limits_frame, text="", fg="blue", font=("Consolas", 9))
        self.limit_counter_label.pack(padx=10, pady=(0, 5))

        # ═══════════════════════════════════════
        #  MID: Список файлов (РАСТЯГИВАЕТСЯ)
        # ═══════════════════════════════════════
        list_frame = ttk.LabelFrame(mid_frame, text="Files to Process")
        list_frame.pack(fill=tk.BOTH, expand=True)

        # Listbox + scrollbar
        list_inner = tk.Frame(list_frame)
        list_inner.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.file_listbox = tk.Listbox(
            list_inner, selectmode=tk.MULTIPLE, font=("Consolas", 9))
        self.file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(
            list_inner, orient="vertical", command=self.file_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.file_listbox.configure(yscrollcommand=scrollbar.set)

        # Drag-select
        self._drag_selecting = False
        self._drag_last_index: Optional[int] = None
        self._drag_mode: Optional[bool] = None
        self.file_listbox.bind("<ButtonPress-1>", self._on_list_press)
        self.file_listbox.bind("<B1-Motion>", self._on_list_drag)
        self.file_listbox.bind("<ButtonRelease-1>", self._on_list_release)

        # List buttons
        list_btn_row = tk.Frame(list_frame)
        list_btn_row.pack(fill=tk.X, padx=5, pady=(0, 5))
        for text, cmd in [
            ("☑ Select All", self.select_all),
            ("☐ Deselect All", self.deselect_all),
            ("➕ Add Files", self.add_file),
            ("🗑️ Remove", self.remove_selected),
            ("🔄 Refresh", self.load_files_to_listbox),
        ]:
            tk.Button(list_btn_row, text=text, command=cmd).pack(
                side=tk.LEFT, padx=3)

        # ═══════════════════════════════════════
        #  BOT: Кнопки, статус, прогресс
        #  ВСЕГДА ВИДНА — pack(side=BOTTOM)
        # ═══════════════════════════════════════

        # Разделитель
        ttk.Separator(bot_frame, orient="horizontal").pack(fill=tk.X, pady=(0, 8))

        # Action buttons
        action_row = tk.Frame(bot_frame)
        action_row.pack(fill=tk.X, pady=(0, 5))

        self.generate_btn = tk.Button(
            action_row, text="📄 Generate MD", command=self.on_generate_clicked,
            bg="#4CAF50", fg="white", font=("Arial", 10, "bold"), padx=20)
        self.generate_btn.pack(side=tk.LEFT, padx=5)

        self.extract_btn = tk.Button(
            action_row, text="📂 Extract MD", command=self.on_extract_clicked,
            bg="#2196F3", fg="white", font=("Arial", 10, "bold"), padx=20)
        self.extract_btn.pack(side=tk.LEFT, padx=5)

        self.open_folder_btn = tk.Button(
            action_row, text="📁 Open Output Folder",
            command=self._open_output_folder, state=tk.DISABLED)
        self.open_folder_btn.pack(side=tk.RIGHT, padx=5)

        # Stats
        self.stats_label = tk.Label(
            bot_frame, text="", fg="blue", font=("Arial", 9, "italic"))
        self.stats_label.pack(fill=tk.X)

        # Status
        self.status = tk.Label(bot_frame, text="", fg="green")
        self.status.pack(fill=tk.X)

        # Progress bar
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            bot_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(5, 0))

        # ═══ State ═══
        self.settings = Settings.from_env()
        self.load_settings_to_ui()
        self.last_out_path: Optional[pathlib.Path] = None
        self.last_stats: Optional[Dict] = None
        self.is_generating: bool = False
        self.available_files: List[pathlib.Path] = []
        self._progress_total: int = 0

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, daemon=True).start()
        self.executor = ThreadPoolExecutor(max_workers=4)

    # ── Helpers ──

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _apply_preset(self, chars, lines, tokens):
        self.max_chars_var.set(str(chars))
        self.max_lines_var.set(str(lines))
        self.max_tokens_var.set(str(tokens))
        self._update_limit_counter()

    def _get_limits(self):
        def _safe(var):
            try:
                return max(0, int(var.get().strip()))
            except ValueError:
                return 0
        return OutputLimits(
            max_chars=_safe(self.max_chars_var),
            max_lines=_safe(self.max_lines_var),
            max_tokens=_safe(self.max_tokens_var))

    def _update_limit_counter(self):
        limits = self._get_limits()
        if not limits.has_any_limit:
            self.limit_counter_label.config(text="No limits set", fg="gray")
            return
        total_chars = sum(
            f.stat().st_size for f in self.available_files if f.exists()) + 500
        parts, color = [], "green"
        eff = limits.effective_max_chars()
        if eff > 0:
            ratio = total_chars / eff
            n = math.ceil(ratio) if ratio > 1 else 1
            parts.append(f"Chars: ~{total_chars:,} / {eff:,}")
            if ratio > 1:
                parts.append(f"→ {n} parts")
                color = "orange"
        if limits.max_lines > 0:
            parts.append(f"Lines limit: {limits.max_lines:,}")
        if limits.max_tokens > 0:
            parts.append(f"Tokens limit: {limits.max_tokens:,}")
        self.limit_counter_label.config(text=" | ".join(parts), fg=color)

    # ── Settings ↔ UI ──

    def load_settings_to_ui(self):
        self.src_entry.delete(0, tk.END)
        self.src_entry.insert(0, str(self.settings.source))
        self.ext_entry.delete(0, tk.END)
        self.ext_entry.insert(0, ",".join(
            ext.lstrip(".") for ext in sorted(self.settings.extensions)))
        self.exclude_entry.delete(0, tk.END)
        self.exclude_entry.insert(0, ", ".join(sorted(self.settings.exclude)))
        self.out_entry.delete(0, tk.END)
        self.out_entry.insert(0, self.settings.output)

    def get_current_settings(self):
        return Settings(
            source=pathlib.Path(self.src_entry.get().strip()).expanduser(),
            extensions=parse_extensions(self.ext_entry.get()),
            exclude=parse_exclusions(self.exclude_entry.get()),
            output=self.out_entry.get().strip() or "source_dump.md")

    # ── Browse ──

    def browse_src(self):
        folder = filedialog.askdirectory()
        if folder:
            self.src_entry.delete(0, tk.END)
            self.src_entry.insert(0, folder)
            self.load_files_to_listbox()

    def browse_exclude(self):
        folder = filedialog.askdirectory()
        if folder:
            name = pathlib.Path(folder).name.lower()
            parts = [p.strip() for p in self.exclude_entry.get().split(",") if p.strip()]
            if name not in (p.lower() for p in parts):
                parts.append(name)
                self.exclude_entry.delete(0, tk.END)
                self.exclude_entry.insert(0, ", ".join(parts))

    # ── List ──

    def select_all(self):
        self.file_listbox.selection_set(0, tk.END)
        self._update_selection_stats()

    def deselect_all(self):
        self.file_listbox.selection_clear(0, tk.END)
        self._update_selection_stats()

    def _update_selection_stats(self):
        sel = len(self.file_listbox.curselection())
        tot = self.file_listbox.size()
        if sel > 0:
            self.stats_label.config(text=f"Selected: {sel}/{tot} files")
        else:
            s = sum(f.stat().st_size for f in self.available_files if f.exists())
            self.stats_label.config(
                text=f"Files: {tot} | Total size: {human_readable_size(s)}")
        self._update_limit_counter()

    def _on_list_press(self, event):
        idx = self.file_listbox.nearest(event.y)
        if idx < 0 or idx >= self.file_listbox.size():
            return
        self._drag_selecting = True
        self._drag_last_index = idx
        was = idx in self.file_listbox.curselection()
        if was:
            self._drag_mode = False
            self.file_listbox.selection_clear(idx)
        else:
            self._drag_mode = True
            self.file_listbox.selection_set(idx)
        self._update_selection_stats()
        return "break"

    def _on_list_drag(self, event):
        if not self._drag_selecting or self._drag_mode is None:
            return
        if event.y < 0:
            self.file_listbox.yview_scroll(-1, "units")
        elif event.y > self.file_listbox.winfo_height():
            self.file_listbox.yview_scroll(1, "units")
        cur = self.file_listbox.nearest(event.y)
        if cur < 0 or cur >= self.file_listbox.size():
            return
        last = self._drag_last_index if self._drag_last_index is not None else cur
        for i in range(min(last, cur), max(last, cur) + 1):
            if self._drag_mode:
                self.file_listbox.selection_set(i)
            else:
                self.file_listbox.selection_clear(i)
        self._drag_last_index = cur
        self._update_selection_stats()
        return "break"

    def _on_list_release(self, event):
        self._drag_selecting = False
        self._drag_mode = None
        self._drag_last_index = None
        self._update_selection_stats()

    def load_files_to_listbox(self):
        root_dir = pathlib.Path(self.src_entry.get().strip()).expanduser().resolve()
        if not root_dir.is_dir():
            return
        files = collect_source_files(
            root_dir, parse_extensions(self.ext_entry.get()),
            parse_exclusions(self.exclude_entry.get()))
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
        self.stats_label.config(
            text=f"Files: {len(files)} | Total size: {human_readable_size(total_size)}")
        self._update_limit_counter()

    def add_file(self):
        root_dir = pathlib.Path(self.src_entry.get().strip()).expanduser().resolve()
        if not root_dir.is_dir():
            messagebox.showwarning("Warning", "Select source folder first.")
            return
        selected = filedialog.askopenfilenames(title="Select files")
        if not selected:
            return
        exts = parse_extensions(self.ext_entry.get())
        excludes = parse_exclusions(self.exclude_entry.get())
        for ps in selected:
            p = pathlib.Path(ps).resolve()
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            try:
                rp = p.relative_to(root_dir).parts
            except ValueError:
                continue
            if any(part.lower() in excludes for part in rp):
                continue
            if p not in self.available_files:
                self.available_files.append(p)
                try:
                    d = f"{p.relative_to(root_dir)} ({human_readable_size(p.stat().st_size)})"
                except ValueError:
                    d = f"{p.name} ({human_readable_size(p.stat().st_size)})"
                self.file_listbox.insert(tk.END, d)

    def remove_selected(self):
        indices = list(map(int, self.file_listbox.curselection()))
        for idx in reversed(indices):
            del self.available_files[idx]
            self.file_listbox.delete(idx)

    # ── Generate ──

    def on_generate_clicked(self):
        if self.is_generating:
            return
        s = self.get_current_settings()
        if not s.source.is_dir():
            messagebox.showerror("Error", f"Folder not found: {s.source}")
            return
        self.is_generating = True
        self.generate_btn.config(state=tk.DISABLED, text="Generating…")
        self.open_folder_btn.config(state=tk.DISABLED)
        self.status.config(text="", fg="black")
        if not self.available_files:
            self.load_files_to_listbox()
        limits = self._get_limits()
        asyncio.run_coroutine_threadsafe(
            self._run_generation(s.source, s.extensions, s.exclude, s.output, limits),
            self.loop)

    async def _run_generation(self, root, exts, excludes, out_name, limits):
        try:
            created, stats = await _generate_async(
                self, root, self.available_files, out_name, limits)
        except Exception as exc:
            LOGGER.exception("Generation error")
            self.after(0, lambda: [
                messagebox.showerror("Error", f"Failed:\n{exc}"),
                self._reset_ui()])
            return

        self.settings = self.get_current_settings()
        self._save_env()
        self.last_out_path = created[0] if created else None
        self.last_stats = stats

        pi = ""
        if stats.get('chunks', 1) > 1:
            pi = f" | Parts: {stats['chunks']}"

        st = (f"Files: {stats['total_files']} | "
              f"Lines: {stats['total_lines']:,} | "
              f"Size: {human_readable_size(stats['total_size'])}{pi}")
        names = ", ".join(f.name for f in created)
        self.after(0, lambda: [
            self.open_folder_btn.config(state=tk.NORMAL),
            self.status.config(text=f"✅ {names}", fg="green"),
            self.generate_btn.config(state=tk.NORMAL, text="📄 Generate MD"),
            self.stats_label.config(text=st)])
        self.is_generating = False

    def _reset_ui(self):
        self.generate_btn.config(state=tk.NORMAL, text="📄 Generate MD")
        self.extract_btn.config(state=tk.NORMAL, text="📂 Extract MD")
        self.open_folder_btn.config(state=tk.DISABLED)
        self.status.config(text="", fg="black")
        self.is_generating = False

    def set_progress(self, total):
        self._progress_total = total
        self.after(0, lambda: [
            self.progress_bar.config(maximum=100),
            self.progress_var.set(0),
            self.status.config(text=f"Processing {total} files…")])

    def update_progress(self, current):
        if not self._progress_total:
            return
        pct = (current / self._progress_total) * 100
        self.after(0, lambda: [
            self.progress_var.set(pct),
            self.status.config(text=f"Processed {current}/{self._progress_total}")])

    def _open_output_folder(self):
        if not self.last_out_path:
            return
        p = str(self.last_out_path.parent)
        try:
            if sys.platform == "win32":
                os.startfile(p)
            elif sys.platform == "darwin":
                subprocess.run(["open", p], check=True)
            else:
                subprocess.run(["xdg-open", p], check=True)
        except Exception as exc:
            messagebox.showerror("Error", f"Cannot open folder:\n{exc}")

    def _save_env(self):
        _ensure_env_dir()
        set_key(ENV_PATH, "SOURCE", str(self.settings.source))
        set_key(ENV_PATH, "EXTENSIONS",
                ",".join(sorted(e.lstrip(".") for e in self.settings.extensions)))
        set_key(ENV_PATH, "EXCLUDE", ",".join(sorted(self.settings.exclude)))
        set_key(ENV_PATH, "OUTPUT", self.settings.output)

    # ── Extract ──

    def _select_md_file(self):
        p = filedialog.askopenfilename(
            title="Select Markdown file", filetypes=[("Markdown", "*.md")])
        return pathlib.Path(p).resolve() if p else None

    async def _extract_async(self, root, md_file):
        md_text = md_file.read_text(encoding="utf-8", errors="replace")
        pattern = re.compile(
            r"###\s+`([^`]+)`\s*\n"
            r"(?:\*\*Size:\*\*[^|]+\|\s*\*\*Lines:\*\*[^|]+\|\s*\*\*Type:\*\*[^\n]*\n\n)?"
            r"```([^\s]*)\n(.*?)\n```", re.DOTALL)
        matches = list(pattern.finditer(md_text))
        if not matches:
            raise RuntimeError("No code blocks found")
        lang_to_ext = {'kotlin': '.kt', 'java': '.java', 'python': '.py',
                       'javascript': '.js', 'typescript': '.ts'}
        created = []
        for i, m in enumerate(matches, 1):
            rp, lt, code = m.group(1), m.group(2), m.group(3)
            rel = pathlib.Path(rp)
            if rel.is_absolute():
                rel = pathlib.Path(rel.name)
            ext = lang_to_ext.get(lt.lower()) or rel.suffix or ".txt"
            fp = (root / rel).with_suffix(ext)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(code.strip() + "\n", encoding="utf-8")
            created.append(fp)
            self.update_progress(i)
        return created

    def on_extract_clicked(self):
        if self.is_generating:
            return
        md = self._select_md_file()
        if not md:
            return
        root = pathlib.Path(self.src_entry.get().strip()).expanduser().resolve()
        if not root.is_dir():
            messagebox.showerror("Error", f"Folder not found: {root}")
            return
        self.is_generating = True
        self.extract_btn.config(state=tk.DISABLED, text="Extracting…")
        asyncio.run_coroutine_threadsafe(self._run_extraction(root, md), self.loop)

    async def _run_extraction(self, root, md_file):
        try:
            created = await self._extract_async(root, md_file)
        except Exception as exc:
            LOGGER.exception("Extraction error")
            self.after(0, lambda: [
                messagebox.showerror("Error", f"Failed:\n{exc}"),
                self._reset_ui()])
            return
        cs = "\n".join(str(p.relative_to(root)) for p in created)
        self.after(0, lambda: [
            messagebox.showinfo("Done", f"Created {len(created)} files:\n{cs}"),
            self._reset_ui()])


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()