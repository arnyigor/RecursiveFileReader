
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart LLM Edit Collector GUI v2

Features:
- Git mode: collect changes vs base ref for a target file/folder
- Session mode: snapshot current state, then later collect only changes from this session
- Prompt text box with clipboard paste (no prompt file required)
- Handles tracked/untracked/added/modified/deleted/renamed
- Saves before/after, diff, meta, summary, prompt, tests, optional zip
- Persists defaults and active session metadata
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import traceback
import threading
import queue
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


APP_NAME = "Smart LLM Edit Collector GUI"
APP_STATE_DIR = Path.home() / ".smart_llm_edit_collector"
CONFIG_FILE = APP_STATE_DIR / "config.json"
ACTIVE_SESSION_FILE = APP_STATE_DIR / "active_session.json"
SESSIONS_DIR = APP_STATE_DIR / "sessions"

DEFAULTS = {
    "base_ref": "HEAD",
    "model_name": "qwen3.6-local",
    "test_cmd": "",
    "output_root": "",
    "auto_open_output_dir": True,
    "include_untracked": True,
    "detect_renames": True,
    "unified_context_lines": 20,
    "prompt_text": "",
    "last_result_dir": "",
    "last_bundle_md": "",
}


# ---------- Generic helpers ----------

def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def ensure_app_dirs() -> None:
    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def safe_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", errors="replace")


def safe_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_bytes_safe(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.read()


def is_probably_binary_bytes(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def try_decode_text(data: bytes) -> Optional[str]:
    for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def copy_file_raw(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def open_in_file_manager(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass



def run_command(cmd: str, cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = "\n".join([
        f"$ {cmd}",
        "",
        "STDOUT:",
        proc.stdout,
        "",
        "STDERR:",
        proc.stderr,
        "",
        f"EXIT_CODE={proc.returncode}",
    ])
    return proc.returncode, text


def find_git_root_optional(start_path: Path) -> Optional[Path]:
    p = start_path.resolve()
    if p.is_file():
        p = p.parent
    current = p
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def find_git_root(start_path: Path) -> Path:
    repo = find_git_root_optional(start_path)
    if repo is None:
        raise RuntimeError(f"Git repo not found for path: {start_path}")
    return repo


def rel_to_repo(repo: Path, target: Path) -> str:
    return str(target.resolve().relative_to(repo.resolve())).replace("\\", "/")


def repo_to_abs(repo: Path, rel_path: str) -> Path:
    return (repo / rel_path.replace("/", os.sep)).resolve()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_rel(path_str: str) -> str:
    return path_str.replace("\\", "/").lstrip("./")


# ---------- Smart test command detection ----------

def detect_smart_test_command(target_path: Path) -> str:
    root = target_path.resolve()
    if root.is_file():
        root = root.parent

    search_roots = []
    repo = find_git_root_optional(root)
    if repo:
        search_roots.append(repo)
    search_roots.append(root)

    seen = set()
    ordered = []
    for r in search_roots:
        if r not in seen:
            seen.add(r)
            ordered.append(r)

    is_win = sys.platform.startswith("win")

    for base in ordered:
        if (base / "gradlew.bat").exists():
            return "gradlew.bat test" if is_win else "./gradlew test"
        if (base / "gradlew").exists():
            return "./gradlew test"
        if (base / "mvnw.cmd").exists():
            return "mvnw.cmd test" if is_win else "./mvnw test"
        if (base / "mvnw").exists():
            return "./mvnw test"
        if (base / "pytest.ini").exists():
            return "pytest -q"
        if (base / "pyproject.toml").exists():
            text = (base / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
            if "pytest" in text.lower():
                return "pytest -q"
        if (base / "package.json").exists():
            return "npm test"
        if (base / "Cargo.toml").exists():
            return "cargo test"

    return ""


# ---------- Diff generation ----------

def build_unified_diff_text(
        old_bytes: Optional[bytes],
        new_bytes: Optional[bytes],
        old_name: str,
        new_name: str,
        context_lines: int,
) -> str:
    old_b = old_bytes if old_bytes is not None else b""
    new_b = new_bytes if new_bytes is not None else b""

    old_is_bin = is_probably_binary_bytes(old_b)
    new_is_bin = is_probably_binary_bytes(new_b)

    if old_is_bin or new_is_bin:
        header = [
            f"--- {old_name}",
            f"+++ {new_name}",
            "@@ binary @@",
            "Binary files differ",
            "",
        ]
        return "\n".join(header)

    old_text = try_decode_text(old_b) or ""
    new_text = try_decode_text(new_b) or ""

    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=old_name,
            tofile=new_name,
            n=context_lines,
        )
    )
    if not diff_lines:
        return ""
    return "".join(diff_lines) + ("\n" if not diff_lines[-1].endswith("\n") else "")


# ---------- Target file enumeration for session mode ----------

def iter_target_files(target_path: Path) -> list[tuple[str, Path]]:
    target_path = target_path.resolve()
    items: list[tuple[str, Path]] = []

    if target_path.is_file():
        return [(target_path.name, target_path)]

    for p in sorted(target_path.rglob("*")):
        if not p.is_file():
            continue
        if ".git" in p.parts:
            continue
        rel = str(p.relative_to(target_path)).replace("\\", "/")
        items.append((rel, p))
    return items


# ---------- Session snapshot logic ----------

def create_session_snapshot(target_path: Path, model_name: str, prompt_text: str, log: Callable[[str], None]) -> dict:
    ensure_app_dirs()

    target_path = target_path.resolve()
    if not target_path.exists():
        raise RuntimeError(f"Path does not exist: {target_path}")

    session_id = f"session_{now_ts()}"
    session_dir = SESSIONS_DIR / session_id
    snapshot_dir = session_dir / "snapshot"
    snapshot_content_dir = snapshot_dir / "content"
    session_dir.mkdir(parents=True, exist_ok=True)

    repo = find_git_root_optional(target_path)
    target_files = iter_target_files(target_path)

    manifest = {}
    for idx, (rel, abs_path) in enumerate(target_files, start=1):
        data = read_bytes_safe(abs_path)
        manifest[rel] = {
            "sha256": sha256_bytes(data),
            "size": len(data),
            "is_binary": is_probably_binary_bytes(data),
        }
        safe_write_bytes(snapshot_content_dir / rel, data)
        if idx % 25 == 0 or idx == len(target_files):
            log(f"Snapshot files: {idx}/{len(target_files)}")

    session_meta = {
        "session_id": session_id,
        "created_at": now_ts(),
        "target_path": str(target_path),
        "is_file_target": target_path.is_file(),
        "repo_root": str(repo) if repo else None,
        "target_relative_to_repo": rel_to_repo(repo, target_path) if repo else None,
        "model_name": model_name,
        "prompt_text": prompt_text,
        "file_count": len(target_files),
    }
    save_json(snapshot_dir / "manifest.json", manifest)
    save_json(session_dir / "session_meta.json", session_meta)
    save_json(ACTIVE_SESSION_FILE, {
        "session_id": session_id,
        "session_dir": str(session_dir),
        "target_path": str(target_path),
        "created_at": session_meta["created_at"],
        "model_name": model_name,
    })
    return {
        "session_id": session_id,
        "session_dir": session_dir,
        "snapshot_dir": snapshot_dir,
        "manifest": manifest,
        "meta": session_meta,
    }


def load_active_session() -> Optional[dict]:
    data = load_json(ACTIVE_SESSION_FILE, None)
    if not data:
        return None
    session_dir = Path(data["session_dir"])
    if not session_dir.exists():
        return None
    meta = load_json(session_dir / "session_meta.json", {})
    manifest = load_json(session_dir / "snapshot" / "manifest.json", {})
    return {
        "session_dir": session_dir,
        "meta": meta,
        "manifest": manifest,
    }


def clear_active_session() -> None:
    if ACTIVE_SESSION_FILE.exists():
        ACTIVE_SESSION_FILE.unlink()


# ---------- Git mode collection ----------

def run_git(repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def git_show_bytes(repo: Path, ref_path: str) -> Optional[bytes]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", ref_path],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def parse_git_name_status(repo: Path, base_ref: str, rel_target: str, detect_renames: bool) -> list[dict]:
    args = ["diff", "--name-status"]
    if detect_renames:
        args.append("-M")
    args.extend([base_ref, "--", rel_target])
    res = run_git(repo, args)
    items = []
    for raw in res.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        code = status[0]
        if code == "R" and len(parts) >= 3:
            items.append({
                "status": "renamed",
                "score": status[1:],
                "old_path": normalize_rel(parts[1]),
                "new_path": normalize_rel(parts[2]),
                "display_path": normalize_rel(parts[2]),
            })
        elif code == "A" and len(parts) >= 2:
            p = normalize_rel(parts[1])
            items.append({"status": "added", "old_path": None, "new_path": p, "display_path": p})
        elif code == "M" and len(parts) >= 2:
            p = normalize_rel(parts[1])
            items.append({"status": "modified", "old_path": p, "new_path": p, "display_path": p})
        elif code == "D" and len(parts) >= 2:
            p = normalize_rel(parts[1])
            items.append({"status": "deleted", "old_path": p, "new_path": None, "display_path": p})
        else:
            if len(parts) >= 2:
                p = normalize_rel(parts[-1])
                items.append({"status": f"other:{status}", "old_path": p, "new_path": p, "display_path": p})
    return items


def git_untracked_paths(repo: Path, rel_target: str) -> list[str]:
    res = run_git(repo, ["ls-files", "--others", "--exclude-standard", "--", rel_target])
    return [normalize_rel(line) for line in res.stdout.splitlines() if line.strip()]


def git_status_porcelain(repo: Path, rel_target: str) -> list[str]:
    res = run_git(repo, ["status", "--porcelain", "--", rel_target])
    return [line.rstrip("\n") for line in res.stdout.splitlines() if line.strip()]


def collect_git_mode(
        target_path: Path,
        base_ref: str,
        out_dir: Path,
        prompt_text: str,
        model_name: str,
        test_cmd: str,
        include_untracked: bool,
        detect_renames: bool,
        context_lines: int,
        log: Callable[[str], None],
) -> dict:
    repo = find_git_root(target_path)
    rel_target = rel_to_repo(repo, target_path)

    log(f"Git repo: {repo}")
    log(f"Target relative path: {rel_target}")

    changes = parse_git_name_status(repo, base_ref, rel_target, detect_renames)
    known_paths = {c["display_path"] for c in changes}

    if include_untracked:
        for p in git_untracked_paths(repo, rel_target):
            if p not in known_paths:
                changes.append({
                    "status": "untracked",
                    "old_path": None,
                    "new_path": p,
                    "display_path": p,
                })

    changes.sort(key=lambda x: x["display_path"])

    before_dir = out_dir / "before"
    after_dir = out_dir / "after"

    diff_parts = []
    collected_files = []

    for idx, item in enumerate(changes, start=1):
        status = item["status"]
        old_path = item.get("old_path")
        new_path = item.get("new_path")
        display_path = item["display_path"]

        log(f"[{idx}/{len(changes)}] {status}: {display_path}")

        old_bytes = git_show_bytes(repo, f"{base_ref}:{old_path}") if old_path else None
        new_abs = repo_to_abs(repo, new_path) if new_path else None
        new_bytes = read_bytes_safe(new_abs) if (new_abs and new_abs.exists() and new_abs.is_file()) else None

        if old_path and old_bytes is not None:
            safe_write_bytes(before_dir / old_path, old_bytes)

        if new_path and new_abs and new_abs.exists() and new_abs.is_file():
            copy_file_raw(new_abs, after_dir / new_path)

        old_label = f"a/{old_path}" if old_path else "/dev/null"
        new_label = f"b/{new_path}" if new_path else "/dev/null"

        # git diff already includes tracked items; manual diff is essential for untracked and safe for a complete package.
        diff_text = build_unified_diff_text(old_bytes, new_bytes, old_label, new_label, context_lines)
        if diff_text:
            diff_parts.append(diff_text)

        collected_files.append({
            "status": status,
            "old_path": old_path,
            "new_path": new_path,
            "display_path": display_path,
            "exists_in_base": old_bytes is not None,
            "exists_now": new_bytes is not None,
        })

    status_lines = git_status_porcelain(repo, rel_target)
    diff_stat = run_git(repo, ["diff", "--stat", base_ref, "--", rel_target]).stdout

    safe_write_text(out_dir / "prompt.txt", prompt_text or "")
    safe_write_text(out_dir / "git_status.txt", "\n".join(status_lines) + ("\n" if status_lines else ""))
    safe_write_text(out_dir / "changed_files.txt", "\n".join(c["display_path"] for c in collected_files) + ("\n" if collected_files else ""))
    safe_write_text(out_dir / "diff.patch", "".join(diff_parts))
    safe_write_text(out_dir / "diff_stat.txt", diff_stat)

    test_output = None
    test_exit_code = None
    if test_cmd.strip():
        log("Running test/build command...")
        test_exit_code, test_output = run_command(test_cmd, repo)
        safe_write_text(out_dir / "tests.txt", test_output)
        log(f"Test/build exit code: {test_exit_code}")

    summary = {
        "mode": "git",
        "timestamp": now_ts(),
        "repo_root": str(repo),
        "target_path": str(target_path.resolve()),
        "target_relative_to_repo": rel_target,
        "base_ref": base_ref,
        "model_name": model_name,
        "changed_files_count": len(collected_files),
        "counts": summarize_status_counts(collected_files),
        "include_untracked": include_untracked,
        "detect_renames": detect_renames,
        "context_lines": context_lines,
        "test_cmd": test_cmd or None,
        "test_exit_code": test_exit_code,
        "changed_files": collected_files,
    }
    save_json(out_dir / "meta.json", summary)

    safe_write_text(out_dir / "summary.txt", render_human_summary(summary, status_lines, diff_stat))
    return summary


# ---------- Session mode collection ----------

def summarize_status_counts(items: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for item in items:
        status = item["status"]
        counts[status] = counts.get(status, 0) + 1
    return counts


def compare_snapshot_to_current(target_path: Path, manifest: dict, detect_renames: bool) -> tuple[list[dict], dict[str, bytes]]:
    current_map: dict[str, Path] = {}
    current_bytes_map: dict[str, bytes] = {}
    for rel, abs_path in iter_target_files(target_path):
        current_map[rel] = abs_path
        current_bytes_map[rel] = read_bytes_safe(abs_path)

    original_paths = set(manifest.keys())
    current_paths = set(current_map.keys())

    added = sorted(current_paths - original_paths)
    deleted = sorted(original_paths - current_paths)
    common = sorted(current_paths & original_paths)

    items: list[dict] = []

    deleted_by_hash: dict[str, list[str]] = {}
    added_by_hash: dict[str, list[str]] = {}

    if detect_renames:
        for rel in deleted:
            h = manifest[rel]["sha256"]
            deleted_by_hash.setdefault(h, []).append(rel)
        for rel in added:
            h = sha256_bytes(current_bytes_map[rel])
            added_by_hash.setdefault(h, []).append(rel)

        paired_deleted = set()
        paired_added = set()
        for h, dels in deleted_by_hash.items():
            adds = added_by_hash.get(h, [])
            while dels and adds:
                old_rel = dels.pop(0)
                new_rel = adds.pop(0)
                paired_deleted.add(old_rel)
                paired_added.add(new_rel)
                items.append({
                    "status": "renamed",
                    "old_path": old_rel,
                    "new_path": new_rel,
                    "display_path": new_rel,
                    "exists_in_base": True,
                    "exists_now": True,
                })

        added = [p for p in added if p not in paired_added]
        deleted = [p for p in deleted if p not in paired_deleted]

    for rel in common:
        old_hash = manifest[rel]["sha256"]
        new_hash = sha256_bytes(current_bytes_map[rel])
        if old_hash != new_hash:
            items.append({
                "status": "modified",
                "old_path": rel,
                "new_path": rel,
                "display_path": rel,
                "exists_in_base": True,
                "exists_now": True,
            })

    for rel in added:
        items.append({
            "status": "added",
            "old_path": None,
            "new_path": rel,
            "display_path": rel,
            "exists_in_base": False,
            "exists_now": True,
        })

    for rel in deleted:
        items.append({
            "status": "deleted",
            "old_path": rel,
            "new_path": None,
            "display_path": rel,
            "exists_in_base": True,
            "exists_now": False,
        })

    items.sort(key=lambda x: (x["status"], x["display_path"]))
    return items, current_bytes_map


def collect_session_mode(
        active_session: dict,
        out_dir: Path,
        prompt_text: str,
        model_name: str,
        test_cmd: str,
        detect_renames: bool,
        context_lines: int,
        log: Callable[[str], None],
) -> dict:
    session_dir = Path(active_session["session_dir"])
    session_meta = active_session["meta"]
    manifest = active_session["manifest"]

    target_path = Path(session_meta["target_path"]).resolve()
    if not target_path.exists():
        raise RuntimeError(f"Session target path no longer exists: {target_path}")

    snapshot_content = session_dir / "snapshot" / "content"
    repo = find_git_root_optional(target_path)

    changes, current_bytes_map = compare_snapshot_to_current(target_path, manifest, detect_renames)

    before_dir = out_dir / "before"
    after_dir = out_dir / "after"
    diff_parts = []

    for idx, item in enumerate(changes, start=1):
        status = item["status"]
        old_path = item.get("old_path")
        new_path = item.get("new_path")
        display_path = item["display_path"]

        log(f"[{idx}/{len(changes)}] {status}: {display_path}")

        old_bytes = read_bytes_safe(snapshot_content / old_path) if old_path and (snapshot_content / old_path).exists() else None
        new_abs = (target_path / new_path) if (new_path and target_path.is_dir()) else (target_path if new_path else None)
        if target_path.is_file() and new_path:
            new_abs = target_path
        new_bytes = current_bytes_map.get(new_path) if new_path else None

        if old_path and old_bytes is not None:
            safe_write_bytes(before_dir / old_path, old_bytes)

        if new_path and new_abs and Path(new_abs).exists() and Path(new_abs).is_file():
            copy_file_raw(Path(new_abs), after_dir / new_path)

        old_label = f"a/{old_path}" if old_path else "/dev/null"
        new_label = f"b/{new_path}" if new_path else "/dev/null"
        diff_text = build_unified_diff_text(old_bytes, new_bytes, old_label, new_label, context_lines)
        if diff_text:
            diff_parts.append(diff_text)

    safe_write_text(out_dir / "prompt.txt", prompt_text or session_meta.get("prompt_text", "") or "")
    safe_write_text(out_dir / "changed_files.txt", "\n".join(c["display_path"] for c in changes) + ("\n" if changes else ""))
    safe_write_text(out_dir / "diff.patch", "".join(diff_parts))

    git_status_lines = []
    diff_stat = ""
    if repo:
        rel_target = rel_to_repo(repo, target_path)
        git_status_lines = git_status_porcelain(repo, rel_target)
        safe_write_text(out_dir / "git_status.txt", "\n".join(git_status_lines) + ("\n" if git_status_lines else ""))
        try:
            diff_stat = run_git(repo, ["diff", "--stat", "--", rel_target]).stdout
        except Exception:
            diff_stat = ""
        safe_write_text(out_dir / "diff_stat.txt", diff_stat)

    test_output = None
    test_exit_code = None
    if test_cmd.strip():
        cmd_cwd = repo or (target_path.parent if target_path.is_file() else target_path)
        log("Running test/build command...")
        test_exit_code, test_output = run_command(test_cmd, cmd_cwd)
        safe_write_text(out_dir / "tests.txt", test_output)
        log(f"Test/build exit code: {test_exit_code}")

    summary = {
        "mode": "session",
        "timestamp": now_ts(),
        "session_id": session_meta["session_id"],
        "session_created_at": session_meta["created_at"],
        "repo_root": str(repo) if repo else None,
        "target_path": str(target_path),
        "base_ref": None,
        "model_name": model_name or session_meta.get("model_name"),
        "changed_files_count": len(changes),
        "counts": summarize_status_counts(changes),
        "detect_renames": detect_renames,
        "context_lines": context_lines,
        "test_cmd": test_cmd or None,
        "test_exit_code": test_exit_code,
        "changed_files": changes,
    }
    save_json(out_dir / "meta.json", summary)
    safe_write_text(out_dir / "summary.txt", render_human_summary(summary, git_status_lines, diff_stat))
    return summary


def render_human_summary(summary: dict, git_status_lines: list[str], diff_stat: str) -> str:
    lines = [
        f"Mode: {summary.get('mode')}",
        f"Timestamp: {summary.get('timestamp')}",
        f"Target path: {summary.get('target_path')}",
        f"Repo root: {summary.get('repo_root') or '-'}",
        f"Base ref: {summary.get('base_ref') or '-'}",
        f"Model name: {summary.get('model_name') or '-'}",
        f"Changed files: {summary.get('changed_files_count', 0)}",
        "Counts:",
    ]
    for k, v in sorted((summary.get("counts") or {}).items()):
        lines.append(f"  - {k}: {v}")

    if summary.get("session_id"):
        lines.extend([
            f"Session id: {summary.get('session_id')}",
            f"Session created at: {summary.get('session_created_at')}",
        ])

    lines.extend([
        "",
        "Git status:",
        *(git_status_lines or ["<empty>"]),
        "",
        "Diff stat:",
        diff_stat.strip() or "<empty>",
        "",
        ])

    if summary.get("test_cmd"):
        lines.append(f"Test command: {summary['test_cmd']}")
        lines.append(f"Test exit code: {summary.get('test_exit_code')}")

    return "\n".join(lines) + "\n"




# ---------- Markdown bundle ----------

def _read_text_from_artifact(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

def _read_binary_or_text_for_bundle(path: Path) -> tuple[str, bool]:
    if not path.exists() or not path.is_file():
        return "<missing>", False
    data = read_bytes_safe(path)
    if is_probably_binary_bytes(data):
        return f"<binary file, {len(data)} bytes>", True
    text = try_decode_text(data)
    return (text if text is not None else "<unable to decode text>"), False

def generate_markdown_bundle(out_dir: Path) -> Path:
    meta = load_json(out_dir / "meta.json", {})
    prompt_text = _read_text_from_artifact(out_dir / "prompt.txt")
    summary_text = _read_text_from_artifact(out_dir / "summary.txt")
    git_status_text = _read_text_from_artifact(out_dir / "git_status.txt")
    diff_stat_text = _read_text_from_artifact(out_dir / "diff_stat.txt")
    diff_patch_text = _read_text_from_artifact(out_dir / "diff.patch")
    tests_text = _read_text_from_artifact(out_dir / "tests.txt")

    lines: list[str] = []
    lines.append("# LLM Edit Session Bundle")
    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    for key in [
        "mode",
        "timestamp",
        "session_id",
        "session_created_at",
        "repo_root",
        "target_path",
        "target_relative_to_repo",
        "base_ref",
        "model_name",
        "changed_files_count",
    ]:
        value = meta.get(key)
        if value not in (None, ""):
            lines.append(f"- **{key}**: `{value}`")
    counts = meta.get("counts") or {}
    if counts:
        lines.append("- **counts**:")
        for k, v in sorted(counts.items()):
            lines.append(f"  - `{k}`: {v}")
    lines.append("")

    lines.append("## Prompt")
    lines.append("")
    lines.append("```text")
    lines.append(prompt_text.rstrip())
    lines.append("```")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("```text")
    lines.append(summary_text.rstrip())
    lines.append("```")
    lines.append("")

    lines.append("## Changed files")
    lines.append("")
    changed_files = meta.get("changed_files") or []
    if changed_files:
        for item in changed_files:
            lines.append(f"- `{item.get('display_path')}` — status: `{item.get('status')}`")
    else:
        lines.append("<empty>")
    lines.append("")

    lines.append("## Git status")
    lines.append("")
    lines.append("```text")
    lines.append(git_status_text.rstrip() or "<empty>")
    lines.append("```")
    lines.append("")

    lines.append("## Diff stat")
    lines.append("")
    lines.append("```text")
    lines.append(diff_stat_text.rstrip() or "<empty>")
    lines.append("```")
    lines.append("")

    lines.append("## Diff patch")
    lines.append("")
    lines.append("```diff")
    lines.append(diff_patch_text.rstrip() or "<empty>")
    lines.append("```")
    lines.append("")

    if tests_text.strip():
        lines.append("## Tests / build output")
        lines.append("")
        lines.append("```text")
        lines.append(tests_text.rstrip())
        lines.append("```")
        lines.append("")

    before_dir = out_dir / "before"
    after_dir = out_dir / "after"
    if changed_files:
        lines.append("## File snapshots")
        lines.append("")
        for item in changed_files:
            status = item.get("status")
            display = item.get("display_path") or "-"
            old_path = item.get("old_path")
            new_path = item.get("new_path")
            lines.append(f"### `{display}`")
            lines.append("")
            lines.append(f"- Status: `{status}`")
            if old_path:
                lines.append(f"- Before path: `{old_path}`")
            if new_path:
                lines.append(f"- After path: `{new_path}`")
            lines.append("")

            if old_path:
                old_text, old_is_bin = _read_binary_or_text_for_bundle(before_dir / old_path)
                lines.append("#### Before")
                lines.append("")
                if old_is_bin:
                    lines.append(f"`{old_text}`")
                else:
                    lines.append("```text")
                    lines.append(old_text.rstrip())
                    lines.append("```")
                lines.append("")
            if new_path:
                new_text, new_is_bin = _read_binary_or_text_for_bundle(after_dir / new_path)
                lines.append("#### After")
                lines.append("")
                if new_is_bin:
                    lines.append(f"`{new_text}`")
                else:
                    lines.append("```text")
                    lines.append(new_text.rstrip())
                    lines.append("```")
                lines.append("")

    bundle_path = out_dir / "llm_bundle.md"
    safe_write_text(bundle_path, "\n".join(lines).rstrip() + "\n")
    return bundle_path


# ---------- Config ----------

@dataclass
class AppConfig:
    target_path: str = ""
    base_ref: str = DEFAULTS["base_ref"]
    model_name: str = DEFAULTS["model_name"]
    output_root: str = DEFAULTS["output_root"]
    test_cmd: str = DEFAULTS["test_cmd"]
    auto_open_output_dir: bool = DEFAULTS["auto_open_output_dir"]
    include_untracked: bool = DEFAULTS["include_untracked"]
    detect_renames: bool = DEFAULTS["detect_renames"]
    unified_context_lines: int = DEFAULTS["unified_context_lines"]
    prompt_text: str = DEFAULTS["prompt_text"]
    last_result_dir: str = DEFAULTS["last_result_dir"]
    last_bundle_md: str = DEFAULTS["last_bundle_md"]


def load_config() -> AppConfig:
    ensure_app_dirs()
    data = load_json(CONFIG_FILE, {})
    cfg = AppConfig()
    for k, v in data.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg


def save_config(cfg: AppConfig) -> None:
    save_json(CONFIG_FILE, asdict(cfg))


# ---------- GUI ----------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        ensure_app_dirs()
        self.title(APP_NAME)
        self.geometry("1120x860")
        self.minsize(960, 700)
        self.cfg = load_config()
        self.active_session = load_active_session()

        self.var_target_path = tk.StringVar(value=self.cfg.target_path)
        self.var_base_ref = tk.StringVar(value=self.cfg.base_ref)
        self.var_model_name = tk.StringVar(value=self.cfg.model_name)
        self.var_output_root = tk.StringVar(value=self.cfg.output_root)
        self.var_test_cmd = tk.StringVar(value=self.cfg.test_cmd)
        self.var_auto_open = tk.BooleanVar(value=self.cfg.auto_open_output_dir)
        self.var_include_untracked = tk.BooleanVar(value=self.cfg.include_untracked)
        self.var_detect_renames = tk.BooleanVar(value=self.cfg.detect_renames)
        self.var_context_lines = tk.IntVar(value=self.cfg.unified_context_lines)
        self.var_last_result_dir = tk.StringVar(value=self.cfg.last_result_dir)
        self.var_last_bundle_md = tk.StringVar(value=self.cfg.last_bundle_md)
        self.var_session_info = tk.StringVar(value=self._build_session_info())
        self._ui_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._busy = False
        self._busy_widgets = []

        self._build_ui()
        self.after(100, self._process_ui_queue)
        self._log("Готово.")
        self._log(f"Config: {CONFIG_FILE}")

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        title = ttk.Label(root, text="Умный сборщик артефактов LLM-правок", font=("Segoe UI", 15, "bold"))
        title.pack(anchor="w", pady=(0, 10))

        top = ttk.Frame(root)
        top.pack(fill="x")

        self._labeled_entry(top, 0, "Путь к папке/файлу:", self.var_target_path, browse=self._browse_target)
        self._labeled_entry(top, 1, "Base ref (для Git режима):", self.var_base_ref)
        self._labeled_entry(top, 2, "Имя модели:", self.var_model_name)
        self._labeled_entry(top, 3, "Корень вывода:", self.var_output_root, browse=self._browse_output_root)
        self._labeled_entry(top, 4, "Команда тестов/сборки:", self.var_test_cmd, browse=self._suggest_test_cmd, browse_text="Подобрать")

        options = ttk.LabelFrame(root, text="Опции")
        options.pack(fill="x", pady=(12, 8))

        ttk.Checkbutton(options, text="Включать untracked файлы", variable=self.var_include_untracked).grid(row=0, column=0, sticky="w", padx=10, pady=8)
        ttk.Checkbutton(options, text="Пытаться находить rename", variable=self.var_detect_renames).grid(row=0, column=1, sticky="w", padx=10, pady=8)
        ttk.Checkbutton(options, text="Автооткрывать папку результата", variable=self.var_auto_open).grid(row=0, column=2, sticky="w", padx=10, pady=8)

        ttk.Label(options, text="Строк контекста diff:").grid(row=1, column=0, sticky="w", padx=10, pady=(0, 8))
        ttk.Spinbox(options, from_=0, to=200, textvariable=self.var_context_lines, width=8).grid(row=1, column=1, sticky="w", pady=(0, 8))
        ttk.Label(options, text="После каждого сбора автоматически создаётся llm_bundle.md").grid(row=1, column=2, columnspan=2, sticky="w", padx=10, pady=(0, 8))

        prompt_frame = ttk.LabelFrame(root, text="Prompt")
        prompt_frame.pack(fill="both", expand=False, pady=(8, 8))

        prompt_btns = ttk.Frame(prompt_frame)
        prompt_btns.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Button(prompt_btns, text="Вставить из буфера", command=self._paste_prompt_from_clipboard).pack(side="left")
        ttk.Button(prompt_btns, text="Очистить", command=self._clear_prompt).pack(side="left", padx=(8, 0))

        self.prompt_widget = scrolledtext.ScrolledText(prompt_frame, height=10, wrap="word", font=("Consolas", 10))
        self.prompt_widget.pack(fill="both", expand=True, padx=8, pady=8)
        if self.cfg.prompt_text:
            self.prompt_widget.insert("1.0", self.cfg.prompt_text)

        session_frame = ttk.LabelFrame(root, text="Режим сессии")
        session_frame.pack(fill="x", pady=(4, 8))
        ttk.Label(session_frame, textvariable=self.var_session_info, justify="left").pack(anchor="w", padx=10, pady=(8, 6))

        session_btns = ttk.Frame(session_frame)
        session_btns.pack(fill="x", padx=10, pady=(0, 8))
        self.btn_start_session = ttk.Button(session_btns, text="Старт сессии", command=self._start_session)
        self.btn_start_session.pack(side="left")
        self.btn_finish_session = ttk.Button(session_btns, text="Завершить сессию и собрать", command=self._finish_session)
        self.btn_finish_session.pack(side="left", padx=(8, 0))
        self.btn_reset_session = ttk.Button(session_btns, text="Сбросить активную сессию", command=self._reset_active_session)
        self.btn_reset_session.pack(side="left", padx=(8, 0))

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(0, 8))
        self.btn_check_repo = ttk.Button(actions, text="Проверить git/repo", command=self._check_repo)
        self.btn_check_repo.pack(side="left")
        self.btn_collect_now = ttk.Button(actions, text="Собрать сейчас (Git режим)", command=self._collect_now)
        self.btn_collect_now.pack(side="left", padx=(8, 0))
        self.btn_open_last_result = ttk.Button(actions, text="Открыть последний результат", command=self._open_last_result)
        self.btn_open_last_result.pack(side="left", padx=(8, 0))
        self.btn_open_last_bundle = ttk.Button(actions, text="Открыть итоговый MD", command=self._open_last_bundle_md)
        self.btn_open_last_bundle.pack(side="left", padx=(8, 0))
        self.btn_copy_last_bundle = ttk.Button(actions, text="Копировать MD в буфер", command=self._copy_last_bundle_to_clipboard)
        self.btn_copy_last_bundle.pack(side="left", padx=(8, 0))
        self.btn_save_defaults = ttk.Button(actions, text="Сохранить дефолты", command=self._save_defaults)
        self.btn_save_defaults.pack(side="left", padx=(8, 0))
        self.btn_reset_defaults = ttk.Button(actions, text="Сбросить дефолты", command=self._reset_defaults)
        self.btn_reset_defaults.pack(side="left", padx=(8, 0))
        self.btn_clear_log = ttk.Button(actions, text="Очистить лог", command=self._clear_log)
        self.btn_clear_log.pack(side="left", padx=(8, 0))

        log_frame = ttk.LabelFrame(root, text="Лог")
        log_frame.pack(fill="both", expand=True)

        self.log_widget = scrolledtext.ScrolledText(log_frame, wrap="word", font=("Consolas", 10))
        self.log_widget.pack(fill="both", expand=True, padx=8, pady=8)

        self._busy_widgets = [
            self.btn_start_session,
            self.btn_finish_session,
            self.btn_reset_session,
            self.btn_check_repo,
            self.btn_collect_now,
            self.btn_open_last_result,
            self.btn_open_last_bundle,
            self.btn_copy_last_bundle,
            self.btn_save_defaults,
            self.btn_reset_defaults,
        ]

    def _build_session_info(self) -> str:
        active = load_active_session()
        if not active:
            return "Активной сессии нет."
        meta = active["meta"]
        return (
            f"Активная сессия: {meta.get('session_id')}\n"
            f"Создана: {meta.get('created_at')}\n"
            f"Target: {meta.get('target_path')}\n"
            f"Model: {meta.get('model_name') or '-'}\n"
            f"Файлов в snapshot: {meta.get('file_count', 0)}"
        )

    def _refresh_session_info(self) -> None:
        self.active_session = load_active_session()
        self.var_session_info.set(self._build_session_info())

    def _labeled_entry(self, parent, row: int, label: str, variable, browse=None, browse_text="Обзор...") -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=6)
        if browse:
            ttk.Button(parent, text=browse_text, command=browse).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=6)
        parent.columnconfigure(1, weight=1)

    def _log(self, text: str) -> None:
        self.log_widget.insert("end", text + "\n")
        self.log_widget.see("end")
        self.update_idletasks()

    def _clear_log(self) -> None:
        self.log_widget.delete("1.0", "end")

    def _get_prompt_text(self) -> str:
        return self.prompt_widget.get("1.0", "end").rstrip()

    def _set_prompt_text(self, text: str) -> None:
        self.prompt_widget.delete("1.0", "end")
        self.prompt_widget.insert("1.0", text)

    def _paste_prompt_from_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except Exception:
            messagebox.showerror(APP_NAME, "Буфер обмена пуст или недоступен.")
            return
        self._set_prompt_text(text)
        self._log("Prompt вставлен из буфера.")

    def _clear_prompt(self) -> None:
        self._set_prompt_text("")

    def _browse_target(self) -> None:
        path = filedialog.askdirectory(title="Выберите папку")
        if not path:
            path = filedialog.askopenfilename(title="Или выберите файл")
        if path:
            self.var_target_path.set(path)

    def _browse_output_root(self) -> None:
        path = filedialog.askdirectory(title="Выберите корень для результатов")
        if path:
            self.var_output_root.set(path)

    def _check_repo(self) -> None:
        target = self.var_target_path.get().strip()
        if not target:
            messagebox.showerror(APP_NAME, "Укажи путь к папке или файлу.")
            return

        try:
            target_path = Path(target).resolve()
            repo = find_git_root_optional(target_path)
            if repo:
                rel = rel_to_repo(repo, target_path)
                status_lines = git_status_porcelain(repo, rel)
                self._log(f"Git repo найден: {repo}")
                self._log(f"Target relative path: {rel}")
                self._log(f"Текущих git-изменений в target: {len(status_lines)}")
                messagebox.showinfo(APP_NAME, f"Git repo найден:\n{repo}\n\nИзменений в target: {len(status_lines)}")
            else:
                self._log("Git repo не найден. Session mode всё равно может работать.")
                messagebox.showinfo(APP_NAME, "Git repo не найден.\nSession mode всё равно может работать.")
        except Exception as e:
            self._log(f"Ошибка: {e}")
            messagebox.showerror(APP_NAME, str(e))

    def _suggest_test_cmd(self) -> None:
        target = self.var_target_path.get().strip()
        if not target:
            messagebox.showerror(APP_NAME, "Укажи путь к папке или файлу.")
            return
        cmd = detect_smart_test_command(Path(target))
        if cmd:
            self.var_test_cmd.set(cmd)
            self._log(f"Подобрана команда: {cmd}")
        else:
            self._log("Команда тестов не определена автоматически.")
            messagebox.showinfo(APP_NAME, "Автоматически не удалось определить команду тестов.")

    def _current_config(self) -> AppConfig:
        return AppConfig(
            target_path=self.var_target_path.get().strip(),
            base_ref=self.var_base_ref.get().strip() or DEFAULTS["base_ref"],
            model_name=self.var_model_name.get().strip() or DEFAULTS["model_name"],
            output_root=self.var_output_root.get().strip(),
            test_cmd=self.var_test_cmd.get().strip(),
            auto_open_output_dir=self.var_auto_open.get(),
            include_untracked=self.var_include_untracked.get(),
            detect_renames=self.var_detect_renames.get(),
            unified_context_lines=int(self.var_context_lines.get()),
            prompt_text=self._get_prompt_text(),
            last_result_dir=self.var_last_result_dir.get().strip(),
            last_bundle_md=self.var_last_bundle_md.get().strip(),
        )

    def _save_defaults(self) -> None:
        cfg = self._current_config()
        save_config(cfg)
        self._log("Дефолты сохранены.")
        messagebox.showinfo(APP_NAME, f"Сохранено:\n{CONFIG_FILE}")

    def _reset_defaults(self) -> None:
        cfg = AppConfig()
        self.var_base_ref.set(cfg.base_ref)
        self.var_model_name.set(cfg.model_name)
        self.var_output_root.set(cfg.output_root)
        self.var_test_cmd.set(cfg.test_cmd)
        self.var_auto_open.set(cfg.auto_open_output_dir)
        self.var_include_untracked.set(cfg.include_untracked)
        self.var_detect_renames.set(cfg.detect_renames)
        self.var_context_lines.set(cfg.unified_context_lines)
        self._set_prompt_text(cfg.prompt_text)
        self.var_last_bundle_md.set(cfg.last_bundle_md)
        self._log("Дефолты сброшены.")

    def _resolve_output_dir(self) -> Path:
        root = self.var_output_root.get().strip()
        out_root = Path(root).resolve() if root else Path.cwd()
        out_root.mkdir(parents=True, exist_ok=True)
        return out_root / f"llm_edit_session_{now_ts()}"


    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for widget in self._busy_widgets:
            try:
                widget.configure(state=state)
            except Exception:
                pass

    def _process_ui_queue(self) -> None:
        try:
            while True:
                event, payload = self._ui_queue.get_nowait()
                if event == "log":
                    self._log(str(payload))
                elif event == "done":
                    self._on_background_done(payload)
                elif event == "error":
                    err_text, tb = payload
                    self._log("Ошибка.")
                    self._log(err_text)
                    self._log(tb)
                    self._set_busy(False)
                    messagebox.showerror(APP_NAME, f"Ошибка:\n{err_text}")
        except queue.Empty:
            pass
        self.after(100, self._process_ui_queue)

    def _run_background(self, title: str, worker) -> None:
        if self._busy:
            messagebox.showinfo(APP_NAME, "Сейчас уже выполняется другая операция.")
            return
        self._set_busy(True)
        self._log("=" * 80)
        self._log(title)

        def _runner():
            try:
                result = worker()
                self._ui_queue.put(("done", result))
            except Exception as e:
                self._ui_queue.put(("error", (str(e), traceback.format_exc())))

        threading.Thread(target=_runner, daemon=True).start()

    def _finalize_output(self, out_dir: Path, bundle_path: Path | None = None, clear_session_after: bool = False) -> None:
        self.var_last_result_dir.set(str(out_dir))
        if bundle_path:
            self.var_last_bundle_md.set(str(bundle_path))

        if clear_session_after:
            clear_active_session()
            self._refresh_session_info()

        cfg = self._current_config()
        cfg.last_result_dir = str(out_dir)
        cfg.last_bundle_md = str(bundle_path) if bundle_path else self.var_last_bundle_md.get().strip()
        save_config(cfg)

        if self.var_auto_open.get():
            open_in_file_manager(out_dir)

        message = f"Артефакты собраны:\n{out_dir}"
        if bundle_path:
            message += f"\n\nMarkdown bundle:\n{bundle_path}"
        self._set_busy(False)
        messagebox.showinfo(APP_NAME, message)

    def _on_background_done(self, payload) -> None:
        kind = payload.get("kind")
        if kind == "start_session":
            self._refresh_session_info()
            self._set_busy(False)
            self._log(f"Сессия создана: {payload['session_id']}")
            messagebox.showinfo(APP_NAME, f"Сессия создана:\n{payload['session_id']}")
            return

        if kind in {"collect_git", "finish_session"}:
            summary = payload["summary"]
            out_dir = Path(payload["out_dir"])
            bundle_path = Path(payload["bundle_path"])
            self._log(f"Готово. Изменённых файлов: {summary['changed_files_count']}")
            self._log(f"Markdown bundle: {bundle_path}")
            self._finalize_output(
                out_dir=out_dir,
                bundle_path=bundle_path,
                clear_session_after=(kind == "finish_session"),
            )
            return

        self._set_busy(False)

    def _collect_now(self) -> None:
        target = self.var_target_path.get().strip()
        if not target:
            messagebox.showerror(APP_NAME, "Укажи путь к папке или файлу.")
            return

        cfg = self._current_config()
        save_config(cfg)
        target_path = Path(target).resolve()
        out_dir = self._resolve_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)

        def worker():
            def bg_log(msg: str) -> None:
                self._ui_queue.put(("log", msg))

            summary = collect_git_mode(
                target_path=target_path,
                base_ref=cfg.base_ref,
                out_dir=out_dir,
                prompt_text=cfg.prompt_text,
                model_name=cfg.model_name,
                test_cmd=cfg.test_cmd,
                include_untracked=cfg.include_untracked,
                detect_renames=cfg.detect_renames,
                context_lines=cfg.unified_context_lines,
                log=bg_log,
            )
            bundle_path = generate_markdown_bundle(out_dir)
            return {
                "kind": "collect_git",
                "summary": summary,
                "out_dir": str(out_dir),
                "bundle_path": str(bundle_path),
            }

        self._run_background("Старт Git режима...", worker)

    def _start_session(self) -> None:
        target = self.var_target_path.get().strip()
        if not target:
            messagebox.showerror(APP_NAME, "Укажи путь к папке или файлу.")
            return

        existing = load_active_session()
        if existing:
            answer = messagebox.askyesno(
                APP_NAME,
                "Уже есть активная сессия.\n\nПерезаписать её новым snapshot?",
            )
            if not answer:
                return

        cfg = self._current_config()
        save_config(cfg)
        target_path = Path(target).resolve()

        def worker():
            def bg_log(msg: str) -> None:
                self._ui_queue.put(("log", msg))

            repo = find_git_root_optional(target_path)
            if repo:
                rel = rel_to_repo(repo, target_path)
                dirty_count = len(git_status_porcelain(repo, rel))
                bg_log(f"Текущих git-изменений в target на момент старта сессии: {dirty_count}")
                if dirty_count:
                    bg_log("Это нормально: session mode будет сравнивать относительно snapshot, а не HEAD.")

            info = create_session_snapshot(
                target_path=target_path,
                model_name=cfg.model_name,
                prompt_text=cfg.prompt_text,
                log=bg_log,
            )
            return {
                "kind": "start_session",
                "session_id": info["session_id"],
            }

        self._run_background("Создаю snapshot для session mode...", worker)

    def _finish_session(self) -> None:
        active = load_active_session()
        if not active:
            messagebox.showerror(APP_NAME, "Активной сессии нет.")
            return

        cfg = self._current_config()
        save_config(cfg)
        out_dir = self._resolve_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)

        def worker():
            def bg_log(msg: str) -> None:
                self._ui_queue.put(("log", msg))

            summary = collect_session_mode(
                active_session=active,
                out_dir=out_dir,
                prompt_text=cfg.prompt_text,
                model_name=cfg.model_name,
                test_cmd=cfg.test_cmd,
                detect_renames=cfg.detect_renames,
                context_lines=cfg.unified_context_lines,
                log=bg_log,
            )
            bundle_path = generate_markdown_bundle(out_dir)
            return {
                "kind": "finish_session",
                "summary": summary,
                "out_dir": str(out_dir),
                "bundle_path": str(bundle_path),
            }

        self._run_background("Завершаю сессию и собираю артефакты...", worker)

    def _reset_active_session(self) -> None:
        if self._busy:
            messagebox.showinfo(APP_NAME, "Нельзя сбросить сессию во время активной операции.")
            return
        if not load_active_session():
            self._refresh_session_info()
            return
        if not messagebox.askyesno(APP_NAME, "Сбросить активную сессию? Snapshot на диске останется, но активная ссылка будет удалена."):
            return
        clear_active_session()
        self._refresh_session_info()
        self._log("Активная сессия сброшена.")

    def _open_last_result(self) -> None:
        last_dir = self.var_last_result_dir.get().strip()
        if not last_dir:
            messagebox.showinfo(APP_NAME, "Пока нет сохранённого результата.")
            return
        p = Path(last_dir)
        if not p.exists():
            messagebox.showerror(APP_NAME, f"Путь не найден:\n{p}")
            return
        open_in_file_manager(p)

    def _open_last_bundle_md(self) -> None:
        bundle = self.var_last_bundle_md.get().strip()
        if not bundle:
            messagebox.showinfo(APP_NAME, "Пока нет сохранённого Markdown bundle.")
            return
        p = Path(bundle)
        if not p.exists():
            messagebox.showerror(APP_NAME, f"Файл не найден:\n{p}")
            return
        open_in_file_manager(p)

    def _copy_last_bundle_to_clipboard(self) -> None:
        bundle = self.var_last_bundle_md.get().strip()
        if not bundle:
            messagebox.showinfo(APP_NAME, "Пока нет сохранённого Markdown bundle.")
            return
        p = Path(bundle)
        if not p.exists():
            messagebox.showerror(APP_NAME, f"Файл не найден:\n{p}")
            return
        text = p.read_text(encoding="utf-8", errors="replace")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self._log("Markdown bundle скопирован в буфер.")
        messagebox.showinfo(APP_NAME, "Markdown bundle скопирован в буфер.")


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
