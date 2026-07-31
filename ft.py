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
import time
from pathlib import Path

FORMAT_PREFIX = "FTPKG1"
TOKEN_RE = re.compile(r"FTPKG1\.[A-Za-z0-9_-]+\.[0-9a-f]{64}")
ALL_TOKENS_RE = re.compile(r"FTPKG1\.[A-Za-z0-9_-]+\.[0-9a-f]{64}")
TEXT_PREVIEW_MAX_BYTES = 512 * 1024
SPLIT_DEFAULT_THRESHOLD = 500 * 1024  # 500 KB
CLIPBOARD_MAX_CHARS = 500_000  # 500KB — skip clipboard for larger text
CLIPBOARD_TIMEOUT = 5  # seconds — subprocess timeout for clip operations
GUI_PREVIEW_MAX_CHARS = 200_000  # truncate preview in GUI Text widgets
GUI_TOKEN_PREVIEW_MAX_CHARS = 20_000  # keep token preview responsive in tkinter


def parse_size_spec(spec):
    """Parse human-readable size spec like '500k', '1m', '512000' to bytes."""
    spec = spec.strip().lower()
    multipliers = {"k": 1024, "kb": 1024, "m": 1024 * 1024, "mb": 1024 * 1024}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if spec.endswith(suffix):
            return int(float(spec[: -len(suffix)]) * mult)
    return int(spec)


def split_archive_bytes(archive_bytes, n_parts):
    """Split raw archive bytes into n_parts roughly equal chunks."""
    if n_parts <= 0:
        raise ValueError("n_parts must be positive")
    if n_parts == 1:
        return [archive_bytes]
    chunk_size = len(archive_bytes) // n_parts
    chunks = []
    for i in range(n_parts):
        start = i * chunk_size
        end = start + chunk_size if i < n_parts - 1 else len(archive_bytes)
        chunks.append(archive_bytes[start:end])
    return chunks


def _build_one_token(
    source_name, kind, chunk_bytes, split_total=None, split_index=None
):
    """Build a single FTPKG1 token from raw archive chunk bytes."""
    meta = {
        "name": source_name,
        "kind": kind,
        "archive": "tar.xz",
        "version": 1,
    }
    if split_total is not None and split_index is not None:
        meta["split_total"] = split_total
        meta["split_index"] = split_index
    meta_bytes = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    payload = len(meta_bytes).to_bytes(4, "big") + meta_bytes + chunk_bytes
    payload_hash = sha256_bytes(payload)
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    token = "{}.{}.{}".format(FORMAT_PREFIX, encoded, payload_hash)
    timestamp = time.strftime("%H%M%S")
    if split_total is not None and split_index is not None:
        return "{}_{}_{timestamp}-{token}".format(
            split_index + 1, split_total, timestamp=timestamp, token=token
        )
    return "{}-{}".format(timestamp, token)


def build_split_tokens(source, n_parts):
    """Pack source into n_parts tokens. Returns list of token strings."""
    archive_bytes = make_tar_xz(source)
    kind = "dir" if source.is_dir() else "file"
    chunks = split_archive_bytes(archive_bytes, n_parts)
    return [
        _build_one_token(source.name, kind, chunk, split_total=n_parts, split_index=i)
        for i, chunk in enumerate(chunks)
    ]


def extract_all_tokens_from_text(text):
    """Find ALL FTPKG1 tokens in text (not just the first).

    For large texts, normalizes whitespace to handle tokens split across lines.
    The C-regex engine is fast even on multi-MB strings.
    """
    compact = normalize_text(text)
    return [m.group(0) for m in ALL_TOKENS_RE.finditer(compact)]


def reassemble_from_parts(tokens):
    """Reassemble split tokens into (meta, archive_bytes).

    Works with both single tokens and multi-part sets.
    For single tokens, behaves like decode_token().
    For multi-part sets, joins chunks in order.
    """
    if not tokens:
        raise ValueError("Нет токенов для сборки")

    metas = []
    archives = []
    for tok in tokens:
        meta, archive_bytes = decode_token(tok)
        metas.append(meta)
        archives.append(archive_bytes)

    # Single token — no split metadata
    first_meta = metas[0]
    if first_meta.get("split_total") is None:
        if len(tokens) > 1:
            raise ValueError("Найдено несколько токенов, но это не части одного файла")
        return first_meta, archives[0]

    # Multi-part: verify all parts belong to the same set.
    # Order does not matter: parts are sorted by split_index below.
    split_total = first_meta["split_total"]
    identity_keys = ("name", "kind", "archive", "version", "split_total")
    for m in metas:
        if m.get("split_total") is None:
            raise ValueError("Смешаны обычный токен и части split-токена")
        for key in identity_keys:
            if m.get(key) != first_meta.get(key):
                raise ValueError("Части относятся к разным split-токенам")

    by_index = {}
    for m, archive in zip(metas, archives):
        index = m.get("split_index")
        if not isinstance(index, int):
            raise ValueError("Некорректный номер части split-токена")
        if index in by_index:
            prev_meta, prev_archive = by_index[index]
            if prev_archive != archive:
                raise ValueError(
                    "Есть разные части split-токена с одинаковым номером {}".format(
                        index + 1
                    )
                )
            # Same part pasted more than once — ignore duplicate.
            continue
        by_index[index] = (m, archive)

    if len(by_index) != split_total:
        raise ValueError(
            "Ожидалось {} частей, найдено {}".format(split_total, len(by_index))
        )

    # Sort by split_index and join
    indexed = sorted(by_index.values(), key=lambda p: p[0]["split_index"])
    found_indices = [m["split_index"] for m, _ in indexed]
    for i, (m, _) in enumerate(indexed):
        if m["split_index"] != i:
            raise ValueError(
                "Отсутствует часть {} (найдены индексы: {})".format(
                    i, found_indices
                )
            )
    combined_archive = b"".join(a for _, a in indexed)

    # Build a clean meta without split fields for downstream consumers
    clean_meta = {
        k: v for k, v in first_meta.items() if k not in ("split_total", "split_index")
    }
    return clean_meta, combined_archive


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


def _is_path_inside(base_dir, target_path):
    try:
        target_path.resolve().relative_to(base_dir.resolve())
        return True
    except ValueError:
        return False


def _safe_tar_members(tar, output_dir):
    output_root = output_dir.resolve()
    for member in tar.getmembers():
        member_target = output_root / member.name
        if not _is_path_inside(output_root, member_target):
            raise ValueError("Архив содержит небезопасный путь: {}".format(member.name))
        if member.issym() or member.islnk():
            raise ValueError(
                "Архив содержит ссылку, распаковка ссылок отключена: {}".format(
                    member.name
                )
            )
        yield member


def extract_tar_xz(archive_bytes, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ft_unpack_") as tmp_dir:
        archive_path = Path(tmp_dir) / "payload.tar.xz"
        archive_path.write_bytes(archive_bytes)
        with tarfile.open(archive_path, mode="r:xz") as tar:
            tar.extractall(output_dir, members=list(_safe_tar_members(tar, output_dir)))


def build_token(source):
    archive_bytes = make_tar_xz(source)

    meta = {
        "name": source.name,
        "kind": "dir" if source.is_dir() else "file",
        "archive": "tar.xz",
        "version": 1,
    }
    meta_bytes = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )

    payload = len(meta_bytes).to_bytes(4, "big") + meta_bytes + archive_bytes
    payload_hash = sha256_bytes(payload)

    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    token = "{}.{}.{}".format(FORMAT_PREFIX, encoded, payload_hash)
    timestamp = time.strftime("%H%M%S")
    return "{}-{}".format(timestamp, token)


def extract_token_from_text(text):
    compact = normalize_text(text)
    match = TOKEN_RE.search(compact)
    if not match:
        raise ValueError("Пакет не найден в тексте")
    return match.group(0)


def decode_token(token):
    compact = normalize_text(token)
    # Optional human-readable prefix:
    #   HHMMSS-FTPKG1...              (normal token)
    #   PART_TOTAL_HHMMSS-FTPKG1...   (split token, e.g. 1_4_114749-FTPKG1...)
    compact = re.sub(r"^(?:\d+_\d+_)?\d{6}-", "", compact, count=1)
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

    meta_bytes = payload[4 : 4 + meta_len]
    archive_bytes = payload[4 + meta_len :]

    meta = json.loads(meta_bytes.decode("utf-8"))
    return meta, archive_bytes


def try_set_clipboard(text):
    if len(text) > CLIPBOARD_MAX_CHARS:
        return None  # Too large for clipboard, skip

    system = platform.system().lower()

    try:
        if "windows" in system:
            proc = subprocess.Popen(["clip"], stdin=subprocess.PIPE, text=True)
            try:
                proc.communicate(text, timeout=CLIPBOARD_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                return None
            if proc.returncode == 0:
                return "windows:clip"

        if shutil.which("wl-copy"):
            proc = subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE, text=True)
            try:
                proc.communicate(text, timeout=CLIPBOARD_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                return None
            if proc.returncode == 0:
                return "linux:wl-copy"

        if shutil.which("xclip"):
            proc = subprocess.Popen(
                ["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE, text=True
            )
            try:
                proc.communicate(text, timeout=CLIPBOARD_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                return None
            if proc.returncode == 0:
                return "linux:xclip"

        if shutil.which("xsel"):
            proc = subprocess.Popen(
                ["xsel", "--clipboard", "--input"], stdin=subprocess.PIPE, text=True
            )
            try:
                proc.communicate(text, timeout=CLIPBOARD_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                return None
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
                stderr=subprocess.DEVNULL,
                timeout=CLIPBOARD_TIMEOUT,
            )
            if result.strip():
                return result

        if shutil.which("wl-paste"):
            result = subprocess.check_output(
                ["wl-paste", "-n"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=CLIPBOARD_TIMEOUT,
            )
            if result.strip():
                return result

        if shutil.which("xclip"):
            result = subprocess.check_output(
                ["xclip", "-selection", "clipboard", "-o"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=CLIPBOARD_TIMEOUT,
            )
            if result.strip():
                return result

        if shutil.which("xsel"):
            result = subprocess.check_output(
                ["xsel", "--clipboard", "--output"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=CLIPBOARD_TIMEOUT,
            )
            if result.strip():
                return result

    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        pass

    return None


def open_folder_path(path):
    system = platform.system().lower()
    path_text = str(path)
    if system.startswith("windows"):
        os.startfile(path_text)
        return True
    if system == "darwin":
        subprocess.Popen(["open", path_text])
        return True
    for opener in ("xdg-open", "gio", "kde-open"):
        opener_path = shutil.which(opener)
        if opener_path:
            cmd = [opener_path, "open", path_text] if opener == "gio" else [opener_path, path_text]
            subprocess.Popen(cmd)
            return True
    raise RuntimeError("Не найден xdg-open/gio/kde-open для открытия папки")


def save_text_file(path, text):
    path.write_text(text, encoding="utf-8")


def read_text_file(path):
    return path.read_text(encoding="utf-8")


def read_text_preview(path, max_bytes=TEXT_PREVIEW_MAX_BYTES):
    size = path.stat().st_size
    with path.open("rb") as fh:
        raw = fh.read(max_bytes + 1)

    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]

    if any(byte < 32 and byte not in (9, 10, 12, 13) for byte in raw):
        return False, "", truncated, size

    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return True, raw.decode(encoding), truncated, size
        except UnicodeDecodeError:
            continue

    return False, "", truncated, size


def pack_command(source_path, out_file=None, split=None, split_threshold=None):
    source = Path(source_path).resolve()
    if not source.exists():
        print("Ошибка: источник не найден: {}".format(source), file=sys.stderr)
        return 1

    print("[*] Упаковка: {}".format(source))

    archive_bytes = make_tar_xz(source)
    effective_threshold = (
        parse_size_spec(str(split_threshold))
        if split_threshold is not None
        else SPLIT_DEFAULT_THRESHOLD
    )

    # Determine split count: explicit --split wins, then auto-threshold
    n_parts = None
    if split is not None:
        n_parts = max(1, int(split))
    elif len(archive_bytes) > effective_threshold:
        # Auto-split: 2 parts per 500KB above threshold
        n_parts = max(2, (len(archive_bytes) - 1) // effective_threshold + 1)
        print(
            "    [*] Файл {} KB > порог {} KB, авто-разделение на {} частей".format(
                len(archive_bytes) // 1024,
                effective_threshold // 1024,
                n_parts,
            )
        )

    kind = "dir" if source.is_dir() else "file"

    if n_parts and n_parts > 1:
        chunks = split_archive_bytes(archive_bytes, n_parts)
        tokens = [
            _build_one_token(
                source.name, kind, chunk, split_total=n_parts, split_index=i
            )
            for i, chunk in enumerate(chunks)
        ]
    else:
        tokens = [build_token(source)]

    # Save token(s) to file(s)
    if len(tokens) == 1:
        if out_file:
            output_path = Path(out_file).resolve()
        else:
            output_path = Path.cwd() / "{}.ft.txt".format(source.name)
        save_text_file(output_path, tokens[0])
        print("    Файл: {}".format(output_path))
        print("    Длина токена: {} символов".format(len(tokens[0])))
    else:
        if out_file:
            base_path = Path(out_file).resolve()
            stem = base_path.stem
            parent = base_path.parent
        else:
            parent = Path.cwd()
            stem = source.name
        saved_paths = []
        for i, tok in enumerate(tokens):
            part_path = parent / "{}_part{}_{}.ft.txt".format(stem, i + 1, len(tokens))
            save_text_file(part_path, tok)
            saved_paths.append(part_path)
        print("    Файлы ({} частей):".format(len(tokens)))
        for p in saved_paths:
            print("      {}".format(p))

    # Clipboard
    all_text = "\n".join(tokens)
    clip_status = try_set_clipboard(all_text)

    print("[+] Готово")
    if clip_status:
        print("    Буфер: {}".format(clip_status))
    else:
        print("    Буфер: недоступен")
    return 0


def unpack_command(input_path=None, out_dir=None):
    raw_text = None
    source_desc = None

    if input_path:
        path = Path(input_path).resolve()
        if not path.exists():
            print("Ошибка: файл не найден: {}".format(path), file=sys.stderr)
            return 1
        raw_text = read_text_file(path)
        source_desc = "file:{}".format(path)

    if not raw_text:
        raw_text = try_get_clipboard()
        if raw_text:
            source_desc = "clipboard"

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
        all_tokens = extract_all_tokens_from_text(raw_text)
        if not all_tokens:
            raise ValueError("Пакет не найден в тексте")
        if len(all_tokens) > 1:
            print("[*] Найдено {} частей токена, сборка...".format(len(all_tokens)))
        meta, archive_bytes = reassemble_from_parts(all_tokens)
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


def launch_web(port=5000):
    """Инициализация и запуск встроенного HTTP-сервера (Zero Dependencies)"""
    import http.server
    import socketserver

    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>FT Transfer</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif;
                background: linear-gradient(135deg, #e8ecf1 0%, #d5dbe3 100%);
                min-height: 100vh; padding: 32px 16px; color: #1a1a2e;
            }
            .wrapper { max-width: 680px; margin: 0 auto; }

            .header { text-align: center; margin-bottom: 28px; }
            .header h1 { font-size: 26px; font-weight: 700; color: #0d1b3e; letter-spacing: -0.5px; }
            .header .subtitle { font-size: 13px; color: #6b7a90; margin-top: 6px; }

            .auto-toggle {
                display: flex; align-items: center; gap: 8px; justify-content: center;
                margin-bottom: 20px; padding: 10px 16px; background: #fff;
                border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.06);
                font-size: 13px; color: #3d4f6f;
            }
            .auto-toggle input[type="checkbox"] { accent-color: #2563eb; width: 16px; height: 16px; }

            .help-box {
                margin-bottom: 18px; padding: 14px 16px; background: #fff;
                border: 1px solid #dbe4f0; border-radius: 10px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.05);
                font-size: 13px; color: #3d4f6f; line-height: 1.55;
            }
            .help-box summary { cursor: pointer; font-weight: 700; color: #0d1b3e; }
            .help-box ul { margin: 10px 0 0 18px; padding: 0; }
            .help-box li { margin: 5px 0; }
            .help-box b { color: #0d1b3e; }

            .tabs {
                display: flex; gap: 0; margin-bottom: -1px; position: relative; z-index: 1;
            }
            .tab-btn {
                flex: 1; padding: 14px 0; font-size: 15px; font-weight: 600;
                text-align: center; border: none; border-radius: 12px 12px 0 0;
                cursor: pointer; transition: all 0.2s;
                background: #dce1e8; color: #6b7a90;
            }
            .tab-btn.active { background: #fff; color: #0d1b3e; box-shadow: 0 -2px 8px rgba(0,0,0,0.04); }
            .tab-btn:not(.active):hover { background: #d0d7e0; }

            .card {
                background: #fff; border-radius: 0 0 12px 12px;
                box-shadow: 0 4px 20px rgba(0,0,0,0.06); padding: 28px;
            }
            .tab-content { display: none; }
            .tab-content.active { display: block; }

            .field { margin-bottom: 20px; }
            .field:last-child { margin-bottom: 0; }
            .field-label {
                display: block; font-size: 12px; font-weight: 600;
                color: #6b7a90; text-transform: uppercase; letter-spacing: 0.5px;
                margin-bottom: 8px;
            }
            .field-hint { font-size: 11px; color: #8e9bb5; margin-top: 6px; }

            input[type="file"], input[type="text"], textarea {
                width: 100%; padding: 10px 12px; border: 2px solid #e2e7ee;
                border-radius: 8px; font-size: 14px; outline: none;
                transition: border-color 0.2s, box-shadow 0.2s; background: #f9fafb;
                font-family: inherit;
            }
            input[type="file"] { padding: 8px; background: #fff; }
            input[type="text"]:focus, textarea:focus {
                border-color: #2563eb; background: #fff;
                box-shadow: 0 0 0 3px rgba(37,99,235,0.08);
            }
            textarea { resize: vertical; font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; font-size: 13px; line-height: 1.5; }

            .btn-row { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
            .btn-primary {
                flex: 1; padding: 12px 20px; font-size: 14px; font-weight: 600;
                border: none; border-radius: 8px; cursor: pointer; transition: all 0.2s;
                background: #2563eb; color: #fff;
            }
            .btn-primary:hover { background: #1d4ed8; transform: translateY(-1px); box-shadow: 0 4px 12px rgba(37,99,235,0.25); }
            .btn-primary:active { transform: translateY(0); }
            .btn-primary:disabled { background: #a3b8d0; cursor: not-allowed; transform: none; box-shadow: none; }

            .btn-ghost {
                padding: 10px 16px; font-size: 13px; font-weight: 500;
                border: 2px solid #e2e7ee; border-radius: 8px; cursor: pointer;
                background: #fff; color: #3d4f6f; transition: all 0.2s;
            }
            .btn-ghost:hover { border-color: #2563eb; color: #2563eb; background: #f0f5ff; }

            .btn-paste {
                display: inline-flex; align-items: center; gap: 5px;
                padding: 7px 14px; font-size: 12px; font-weight: 500;
                border: 1px solid #d0d7e0; border-radius: 6px; cursor: pointer;
                background: #f0f4f8; color: #4a5568; transition: all 0.15s;
            }
            .btn-paste:hover { background: #e2e8f0; border-color: #a0aec0; }
            .btn-paste.pasted { background: #dcfce7; border-color: #86efac; color: #166534; }

            .paste-row { display: flex; gap: 6px; align-items: center; margin-bottom: 8px; }

            .result-box {
                margin-top: 16px; padding: 16px; border-radius: 10px;
                display: none; font-size: 13px; line-height: 1.6;
            }
            .result-box.success { background: #ecfdf5; border: 1px solid #a7f3d0; color: #065f46; }
            .result-box.error { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }

            .flex-actions { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }

            .loader {
                display: none; text-align: center; padding: 16px 0;
                font-size: 13px; color: #2563eb; font-weight: 500;
            }
            .loader::before {
                content: ''; display: inline-block; width: 16px; height: 16px;
                border: 2px solid #bfdbfe; border-top-color: #2563eb;
                border-radius: 50%; animation: spin 0.7s linear infinite;
                vertical-align: middle; margin-right: 8px;
            }
            @keyframes spin { to { transform: rotate(360deg); } }

            .divider { border: none; border-top: 1px dashed #e2e7ee; margin: 16px 0; }

            .token-display {
                width: 100%; padding: 12px; font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
                font-size: 12px; line-height: 1.5; border: 2px solid #e2e7ee; border-radius: 8px;
                background: #f8fafc; resize: vertical; cursor: text;
            }
        </style>
    </head>
    <body>
        <div class="wrapper">
            <div class="header">
                <h1>FT Transfer</h1>
                <div class="subtitle">Упаковка файлов и текста в токены и обратно</div>
            </div>

            <div class="auto-toggle">
                <input type="checkbox" id="autoMode" checked>
                <label for="autoMode">Авто-режим: распознавание при вставке</label>
            </div>

            <details class="help-box">
                <summary>Краткая справка: для чего это и как пользоваться</summary>
                <ul>
                    <li><b>Упаковать:</b> выберите файл/папку или вставьте текст — приложение создаст текстовый FT-токен.</li>
                    <li><b>Передача:</b> скопируйте токен и отправьте его как обычный текст. Для больших данных используйте split-части.</li>
                    <li><b>Split:</b> если данных много, токен делится на части вида <code>1_4_HHMMSS-FTPKG1...</code>. Для восстановления нужны все части, порядок вставки не важен.</li>
                    <li><b>Распаковать:</b> вставьте токен или все split-части в поле «Распаковать» — результат появится в папке <b>./restored</b>.</li>
                    <li><b>Авто-режим:</b> при вставке токена восстановление запускается автоматически; его можно выключить сверху.</li>
                    <li><b>Открыть папку:</b> кнопка после распаковки открывает папку результата на компьютере, где запущен сервер/EXE.</li>
                </ul>
            </details>

            <div class="tabs">
                <button class="tab-btn active" onclick="switchTab('pack')">Упаковать</button>
                <button class="tab-btn" onclick="switchTab('unpack')">Распаковать</button>
            </div>
            <div class="card">

                <!-- ==================== PACK TAB ==================== -->
                <div id="tab-pack" class="tab-content active">
                    <div class="field">
                        <span class="field-label">Файл</span>
                        <input type="file" id="packFile" onchange="onPackFileSelectionChange('packFile');">
                    </div>

                    <div class="field">
                        <span class="field-label">Папка</span>
                        <input type="file" id="packFolder" webkitdirectory directory onchange="onPackFileSelectionChange('packFolder');">
                        <div class="field-hint">Для больших папок (100+ MB) используйте CLI.</div>
                    </div>

                    <hr class="divider">

                    <div class="field">
                        <span class="field-label">Или текст</span>
                        <div class="paste-row">
                            <button class="btn-paste" onclick="pasteClipboard('packTextContent', this)" title="Вставить из буфера">&#128203; Вставить</button>
                        </div>
                        <textarea id="packTextContent" rows="5" placeholder="Вставьте исходный текст..." oninput="clearOthers('packTextContent')"></textarea>
                    </div>

                    <div class="field" style="display:flex; gap:16px; align-items:center; flex-wrap:wrap;">
                        <label style="display:flex; align-items:center; gap:6px; font-size:13px; color:#3d4f6f; cursor:pointer;">
                            <input type="checkbox" id="splitEnabled" onchange="updateSplitModeInputs()" style="accent-color:#2563eb; width:16px; height:16px;"> Разделять большие файлы
                        </label>
                        <label style="display:flex; align-items:center; gap:4px; font-size:13px; color:#6b7a90; cursor:pointer;">
                            <input type="radio" name="splitMode" value="parts" checked onchange="updateSplitModeInputs()"> по количеству
                        </label>
                        <label id="splitPartsLabel" style="font-size:13px; color:#6b7a90;">Частей: <input type="number" id="splitParts" value="2" min="2" max="64" style="width:50px; padding:4px 6px; border:1px solid #e2e7ee; border-radius:6px; font-size:13px;"></label>
                        <label style="display:flex; align-items:center; gap:4px; font-size:13px; color:#6b7a90; cursor:pointer;">
                            <input type="radio" name="splitMode" value="threshold" onchange="updateSplitModeInputs()"> по порогу
                        </label>
                        <label id="splitThresholdLabel" style="font-size:13px; color:#6b7a90;">Порог (KB): <input type="number" id="splitThreshold" value="500" min="10" style="width:70px; padding:4px 6px; border:1px solid #e2e7ee; border-radius:6px; font-size:13px;"></label>
                    </div>
                    <div id="splitAutoHint" class="field-hint" style="display:none; margin-top:-12px; margin-bottom:12px; color:#b45309;"></div>

                    <div class="btn-row">
                        <button class="btn-primary" id="btnPack" onclick="handlePack()">Сгенерировать токен</button>
                    </div>
                    <div id="packLoader" class="loader">Сборка и упаковка данных...</div>
                    <div id="packResult" class="result-box"></div>
                </div>

                <!-- ==================== UNPACK TAB ==================== -->
                <div id="tab-unpack" class="tab-content">
                    <div class="field">
                        <span class="field-label">Файл токена (.ft.txt)</span>
                        <input type="file" id="unpackFile" onchange="document.getElementById('unpackText').value = '';">
                    </div>

                    <hr class="divider">

                    <div class="field">
                        <span class="field-label">Или вставьте токен</span>
                        <div class="paste-row">
                            <button class="btn-paste" onclick="pasteClipboard('unpackText', this)" title="Вставить токен из буфера">&#128203; Вставить токен</button>
                            <button class="btn-paste" onclick="clearUnpackToken()" title="Очистить поле токена">Очистить токен</button>
                        </div>
                        <textarea id="unpackText" rows="6" placeholder="HHMMSS-FTPKG1..." oninput="document.getElementById('unpackFile').value = '';"></textarea>
                    </div>

                    <div class="btn-row">
                        <button class="btn-primary" id="btnUnpack" onclick="handleUnpack()">Распаковать</button>
                    </div>
                    <div class="field-hint" style="margin-top: 8px;">Распаковка на сервере в папку <b>./restored</b>.</div>
                    <div id="unpackLoader" class="loader">Распаковка данных...</div>
                    <div id="unpackResult" class="result-box"></div>
                </div>
            </div>
        </div>

        <script>
            /* ===== Tab switching ===== */
            function switchTab(name) {
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                document.getElementById('tab-' + name).classList.add('active');
                const btns = document.querySelectorAll('.tab-btn');
                btns[name === 'pack' ? 0 : 1].classList.add('active');
            }

            /* ===== Clipboard paste ===== */
            async function pasteClipboard(targetId, btn) {
                try {
                    const text = await navigator.clipboard.readText();
                    if (!text) return;
                    const target = document.getElementById(targetId);
                    const current = target.value || '';
                    if (targetId === 'unpackText' && current.trim() && text.includes('FTPKG1.')) {
                        const decision = decideUnpackPaste(current, text);
                        if (decision.action === 'append') {
                            target.value = current.replace(/\\s*$/, '') + '\\n' + text.trim();
                        } else if (decision.action === 'replace') {
                            target.value = text.trim();
                        } else {
                            showResult('unpackResult', 'error', decision.message);
                            return;
                        }
                    } else {
                        target.value = text;
                    }
                    if (btn) {
                        btn.classList.add('pasted');
                        btn.innerHTML = '&#10003; Вставлено';
                        setTimeout(() => { btn.classList.remove('pasted'); btn.innerHTML = '&#128203; ' + (targetId === 'unpackText' ? 'Вставить токен' : 'Вставить'); }, 1500);
                    }
                    document.getElementById(targetId).dispatchEvent(new Event('input'));
                } catch (err) {
                    alert('Не удалось прочитать буфер обмена. Разрешите доступ к Clipboard API или вставьте вручную (Ctrl+V).');
                }
            }

            /* ===== Clear helpers ===== */
            function clearOthers(activeId) {
                if (activeId !== 'packFile') document.getElementById('packFile').value = '';
                if (activeId !== 'packFolder') document.getElementById('packFolder').value = '';
                if (activeId !== 'packTextContent') {
                    document.getElementById('packTextContent').value = '';
                }
            }

            function showResult(elementId, type, message) {
                const el = document.getElementById(elementId);
                el.className = 'result-box ' + type;
                el.innerHTML = message;
                el.style.display = 'block';
            }

            function getSplitMode() {
                const checked = document.querySelector('input[name="splitMode"]:checked');
                return checked ? checked.value : 'parts';
            }

            function updateSplitModeInputs() {
                const enabled = document.getElementById('splitEnabled').checked;
                const mode = getSplitMode();
                document.getElementById('splitParts').disabled = !enabled || mode !== 'parts';
                document.getElementById('splitThreshold').disabled = !enabled || mode !== 'threshold';
                document.getElementById('splitPartsLabel').style.opacity = enabled && mode === 'parts' ? '1' : '0.45';
                document.getElementById('splitThresholdLabel').style.opacity = enabled && mode === 'threshold' ? '1' : '0.45';
            }

            function applySplitParams(payload) {
                const splitEnabled = document.getElementById('splitEnabled');
                if (!splitEnabled || !splitEnabled.checked) return;
                if (getSplitMode() === 'parts') {
                    payload.split = parseInt(document.getElementById('splitParts').value) || 2;
                } else {
                    payload.split_threshold = (parseInt(document.getElementById('splitThreshold').value) || 500) * 1024;
                }
            }

            function formatBytes(bytes) {
                if (bytes < 1024) return bytes + ' B';
                if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
                return (bytes / 1024 / 1024).toFixed(1) + ' MB';
            }

            function autoEnableSplitForLargeInput(sizeBytes, description) {
                const thresholdKb = parseInt(document.getElementById('splitThreshold').value) || 500;
                const thresholdBytes = thresholdKb * 1024;
                const hint = document.getElementById('splitAutoHint');
                if (!sizeBytes || sizeBytes <= thresholdBytes) {
                    hint.style.display = 'none';
                    hint.innerHTML = '';
                    return;
                }
                const splitEnabled = document.getElementById('splitEnabled');
                if (!splitEnabled.checked) {
                    splitEnabled.checked = true;
                    const partsMode = document.querySelector('input[name="splitMode"][value="parts"]');
                    if (partsMode) partsMode.checked = true;
                    updateSplitModeInputs();
                }
                hint.innerHTML = 'Источник большой: ' + description + ' (' + formatBytes(sizeBytes) + ') > порога ' + thresholdKb + ' KB. Разделение автоматически включено по количеству частей; можно выключить вручную.';
                hint.style.display = 'block';
            }

            function detectPackTextSizeForSplit() {
                const text = document.getElementById('packTextContent').value || '';
                if (!text) {
                    document.getElementById('splitAutoHint').style.display = 'none';
                    document.getElementById('splitAutoHint').innerHTML = '';
                    return;
                }
                autoEnableSplitForLargeInput(new Blob([text]).size, 'текст');
            }

            function onPackFileSelectionChange(activeId) {
                clearOthers(activeId);
                const input = document.getElementById(activeId);
                const files = Array.from(input.files || []);
                if (files.length > 0) {
                    const totalSize = files.reduce((sum, file) => sum + file.size, 0);
                    autoEnableSplitForLargeInput(totalSize, activeId === 'packFolder' ? 'папка' : 'файл');
                }
                autoPackFile();
            }

            /* ===== Copy / Download ===== */
            window.currentExportToken = '';
            window.currentExportTokens = [];
            window.currentExportFilename = '';
            window.currentUnpackedPath = '';

            function animateActionButton(btn, text) {
                const orig = btn.innerText;
                btn.innerText = text || 'Скопировано!';
                btn.style.background = '#dcfce7'; btn.style.color = '#166534'; btn.style.borderColor = '#86efac';
                setTimeout(() => { btn.innerText = orig; btn.style.background = ''; btn.style.color = ''; btn.style.borderColor = ''; }, 1500);
            }

            window.copyTextElement = async function(btn, elementId) {
                const ta = document.getElementById(elementId);
                try { await navigator.clipboard.writeText(ta.value); }
                catch (err) { ta.select(); document.execCommand('copy'); }
                animateActionButton(btn);
            };

            async function copyTextValue(btn, text) {
                if (!text) return;
                try { await navigator.clipboard.writeText(text); }
                catch (err) {
                    const ta = document.createElement('textarea');
                    ta.value = text;
                    ta.style.position = 'fixed';
                    ta.style.left = '-9999px';
                    document.body.appendChild(ta);
                    ta.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta);
                }
                animateActionButton(btn);
            }

            window.copyCurrentExportToken = async function(btn) {
                await copyTextValue(btn, window.currentExportToken || '');
            };

            window.copyExportTokenPart = async function(btn, index) {
                await copyTextValue(btn, window.currentExportTokens[index] || '');
            };

            function downloadTextFile(filename, text) {
                const blob = new Blob([text], { type: 'text/plain' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url; a.download = filename;
                document.body.appendChild(a); a.click(); document.body.removeChild(a);
                URL.revokeObjectURL(url);
            }

            window.downloadOutToken = function() {
                downloadTextFile(window.currentExportFilename + '.ft.txt', window.currentExportToken);
            };

            window.downloadExportTokenPart = function(index) {
                const total = window.currentExportTokens.length;
                const token = window.currentExportTokens[index] || '';
                if (!token) return;
                downloadTextFile(window.currentExportFilename + '_part' + (index + 1) + '_' + total + '.ft.txt', token);
            };

            window.downloadAllExportTokenParts = function() {
                window.currentExportTokens.forEach((_, index) => window.downloadExportTokenPart(index));
            };

            window.openServerFolder = async function(btn, path) {
                try {
                    const res = await fetch('/api/open-folder', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: path || '' })
                    });
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);
                    animateActionButton(btn, 'Открыто');
                } catch (err) {
                    alert('Не удалось открыть папку на сервере: ' + err.message);
                }
            };

            function unpackSuccessHtml(data, previewTruncatedText) {
                window.currentUnpackedPath = data.path || '';
                let html = 'Восстановлено: <b>' + data.path + '</b>';
                html += '<div class="flex-actions"><button class="btn-ghost" onclick="openServerFolder(this, window.currentUnpackedPath)">Открыть папку</button></div>';
                if (data.is_text && data.text_content !== null) {
                    if (data.text_truncated) html += '<div class="field-hint">' + (previewTruncatedText || 'Предпросмотр обрезан. Полный файл на диске.') + '</div>';
                    html += '<textarea id="unpackedContentArea" class="token-display" rows="10" readonly style="margin-top:10px;"></textarea>';
                    html += '<div class="flex-actions"><button class="btn-ghost" onclick="copyTextElement(this, &apos;unpackedContentArea&apos;)">Копировать текст</button></div>';
                }
                return html;
            }

            const WEB_TOKEN_PREVIEW_MAX_CHARS = 200000;

            function tokenPreviewText(text) {
                if (text.length <= WEB_TOKEN_PREVIEW_MAX_CHARS) return text;
                return text.slice(0, WEB_TOKEN_PREVIEW_MAX_CHARS) +
                    String.fromCharCode(10) + '... (обрезано для предпросмотра)';
            }

            function showPackTokenResult(data) {
                const tokens = data.tokens || [data.token];
                const tokenDisplay = tokens.join(String.fromCharCode(10));
                window.currentExportToken = tokenDisplay;
                window.currentExportTokens = tokens;
                window.currentExportFilename = data.filename;
                const isSplit = tokens.length > 1;
                const previewSource = isSplit ? tokens[0] : tokenDisplay;
                const previewText = tokenPreviewText(previewSource);
                const partsInfo = isSplit ? ' (' + tokens.length + ' частей)' : '';
                let info = '<div class="field-hint" style="margin-bottom:8px;">Файл: <b>' + data.filename + '</b>; длина: ' + tokenDisplay.length + ' символов.</div>';
                if (isSplit) {
                    info += '<div class="field-hint" style="margin-bottom:8px;">Ниже показан предпросмотр части 1. Передавайте/копируйте части отдельно и распаковывайте все части вместе.</div>';
                } else if (previewText.length !== tokenDisplay.length) {
                    info += '<div class="field-hint" style="margin-bottom:8px;">Предпросмотр обрезан, но копирование и скачивание используют полный токен.</div>';
                }

                let actions = '<div class="flex-actions">';
                if (isSplit) {
                    tokens.forEach((token, index) => {
                        actions += '<button class="btn-ghost" onclick="copyExportTokenPart(this, ' + index + ')">Копировать часть ' + (index + 1) + '/' + tokens.length + ' (' + token.length + ')</button>';
                    });
                    actions += '<button class="btn-ghost" onclick="downloadAllExportTokenParts()">Скачать все части</button>';
                } else {
                    actions += '<button class="btn-ghost" onclick="copyCurrentExportToken(this)">Копировать токен</button>';
                    actions += '<button class="btn-ghost" onclick="downloadOutToken()">Скачать .ft.txt</button>';
                }
                actions += '</div>';

                showResult('packResult', 'success',
                    '<div style="margin-bottom:8px;font-weight:700;">✅ Токен сгенерирован' + partsInfo + '</div>' +
                    info +
                    '<textarea id="outTokenArea" class="token-display" rows="6" readonly></textarea>' +
                    actions
                );
                document.getElementById('outTokenArea').value = previewText;
            }

            function extractTokensForClient(rawText) {
                const compact = (rawText || '').replace(/\\s+/g, '');
                return compact.match(/FTPKG1\\.[A-Za-z0-9_-]+\\.[0-9a-f]{64}/g) || [];
            }

            function decodeTokenMetaForClient(token) {
                try {
                    const encoded = token.split('.')[1];
                    let b64 = encoded.replace(/-/g, '+').replace(/_/g, '/');
                    b64 += '='.repeat((4 - b64.length % 4) % 4);
                    const bin = atob(b64);
                    if (bin.length < 4) return null;
                    const metaLen = (
                        (bin.charCodeAt(0) << 24) |
                        (bin.charCodeAt(1) << 16) |
                        (bin.charCodeAt(2) << 8) |
                        bin.charCodeAt(3)
                    ) >>> 0;
                    if (bin.length < 4 + metaLen) return null;
                    const metaBytes = new Uint8Array(metaLen);
                    for (let i = 0; i < metaLen; i++) metaBytes[i] = bin.charCodeAt(4 + i);
                    return JSON.parse(new TextDecoder('utf-8').decode(metaBytes));
                } catch (err) { return null; }
            }

            function splitTokenIdentity(meta) {
                return [
                    meta.name || '',
                    meta.kind || '',
                    meta.archive || '',
                    String(meta.version || ''),
                    String(meta.split_total || '')
                ].join('\u001f');
            }

            function getSplitSessionInfo(rawText) {
                const tokens = extractTokensForClient(rawText);
                const metas = tokens.map(decodeTokenMetaForClient).filter(m => m);
                const splitMetas = metas.filter(m => m.split_total);
                const plainCount = metas.length - splitMetas.length;
                if (splitMetas.length === 0) {
                    return { hasSplit: false, hasTokens: tokens.length > 0, plainCount: plainCount };
                }

                const groups = {};
                splitMetas.forEach(meta => {
                    const key = splitTokenIdentity(meta);
                    if (!groups[key]) {
                        groups[key] = {
                            key: key,
                            name: meta.name || 'без имени',
                            total: parseInt(meta.split_total) || 0,
                            indices: new Set(),
                            count: 0
                        };
                    }
                    const index = parseInt(meta.split_index);
                    if (!isNaN(index)) groups[key].indices.add(index);
                    groups[key].count += 1;
                });

                const groupList = Object.values(groups).sort((a, b) => b.indices.size - a.indices.size);
                const active = groupList[0] || { key: '', name: 'без имени', total: 0, indices: new Set(), count: 0 };
                const found = active.indices.size;
                const total = active.total;
                return {
                    hasSplit: true,
                    hasTokens: tokens.length > 0,
                    plainCount: plainCount,
                    groups: groupList,
                    groupCount: groupList.length,
                    active: active,
                    key: active.key,
                    name: active.name,
                    found: found,
                    total: total,
                    incomplete: total > 0 && found < total,
                    duplicate: active.count !== found,
                    mixed: groupList.length > 1 || plainCount > 0
                };
            }

            function getSplitTokenStatus(rawText) {
                const session = getSplitSessionInfo(rawText);
                if (!session.hasSplit) return { incomplete: false, found: 0, total: 0 };

                if (session.mixed) {
                    return {
                        error: true,
                        mixed: true,
                        found: session.found,
                        total: session.total,
                        message: 'Вставлен токен от другого файла/другого split-набора. Текущий split-набор «' + session.name + '» не завершён: найдено ' + session.found + ' из ' + session.total + '. Очистите поле или вставьте остальные части именно этого набора.'
                    };
                }

                return {
                    incomplete: session.incomplete,
                    duplicate: session.duplicate,
                    found: session.found,
                    total: session.total
                };
            }

            function decideUnpackPaste(currentText, incomingText) {
                const current = getSplitSessionInfo(currentText);
                const incoming = getSplitSessionInfo(incomingText);

                if (!current.hasSplit) {
                    return { action: 'replace' };
                }
                if (!incoming.hasSplit) {
                    return {
                        action: 'block',
                        message: 'Текущая split-сессия «' + current.name + '» не завершена: найдено ' + current.found + ' из ' + current.total + '. Вставьте остальные части этого split-набора или очистите поле перед вставкой другого токена.'
                    };
                }
                if (incoming.groupCount > 1 || incoming.plainCount > 0) {
                    return {
                        action: 'block',
                        message: 'В буфере несколько разных токенов. Очистите поле и вставьте один split-набор целиком либо добавляйте части текущего набора.'
                    };
                }
                if (current.incomplete && current.key === incoming.key) {
                    return { action: 'append' };
                }
                if (current.key !== incoming.key) {
                    const replace = confirm(
                        'В буфере токен от другого файла/другого split-набора.\\n\\n' +
                        'Текущая split-сессия «' + current.name + '»: ' + current.found + ' из ' + current.total + '.\\n' +
                        'Новая split-сессия «' + incoming.name + '»: ' + incoming.found + ' из ' + incoming.total + '.\\n\\n' +
                        'Начать новую вставку и заменить текущие части?'
                    );
                    return replace ? { action: 'replace' } : {
                        action: 'block',
                        message: 'Вставка отменена: текущая split-сессия не завершена. Вставьте остальные части или очистите поле.'
                    };
                }
                return { action: 'append' };
            }

            function clearUnpackToken() {
                const target = document.getElementById('unpackText');
                const fileInput = document.getElementById('unpackFile');
                const text = target.value || '';
                const session = getSplitSessionInfo(text);
                if (session.hasSplit) {
                    const ok = confirm(
                        'В поле есть части split-токена.\\n\\n' +
                        'Сессия «' + session.name + '»: найдено ' + session.found + ' из ' + session.total + '.\\n' +
                        'Очистка прервёт текущую split-сессию. Продолжить?'
                    );
                    if (!ok) return;
                }
                target.value = '';
                fileInput.value = '';
                document.getElementById('unpackResult').style.display = 'none';
                target.dispatchEvent(new Event('input'));
            }

            /* ===== Pack ===== */
            async function handlePack() {
                const btn = document.getElementById('btnPack');
                const loader = document.getElementById('packLoader');
                const fileInput = document.getElementById('packFile');
                const folderInput = document.getElementById('packFolder');
                const textContent = document.getElementById('packTextContent').value.trim();

                btn.disabled = true; loader.style.display = 'block';
                document.getElementById('packResult').style.display = 'none';

                try {
                    let payload = {};

                    if (fileInput.files.length > 0) {
                        const file = fileInput.files[0];
                        const base64 = await new Promise((resolve, reject) => {
                            const r = new FileReader();
                            r.onload = e => { const b = e.target.result.split(',')[1]; resolve(b ? b : ''); };
                            r.onerror = () => reject(new Error("Ошибка чтения файла"));
                            r.readAsDataURL(file);
                        });
                        payload = { is_folder: false, is_text: false, filename: file.name, content_b64: base64 };
                    }
                    else if (folderInput.files.length > 0) {
                        const files = folderInput.files;
                        const folderName = files[0].webkitRelativePath ? files[0].webkitRelativePath.split('/')[0] : 'packed_folder';
                        const fileData = await Promise.all(Array.from(files).map(file => new Promise((resolve, reject) => {
                            const r = new FileReader();
                            r.onload = e => { const b = e.target.result.split(',')[1]; resolve({ path: file.webkitRelativePath || file.name, b64: b ? b : '' }); };
                            r.onerror = () => reject(new Error("Ошибка чтения " + file.name));
                            r.readAsDataURL(file);
                        })));
                        payload = { is_folder: true, folder_name: folderName, files: fileData };
                    }
                    else if (textContent) {
                        payload = { is_folder: false, is_text: true, filename: 'pasted_text.txt', text_content: textContent };
                    }
                    else { throw new Error('Выберите файл, папку или вставьте текст.'); }

                    applySplitParams(payload);

                    const res = await fetch('/api/pack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);

                    showPackTokenResult(data);
                } catch (err) {
                    showResult('packResult', 'error', err.message);
                } finally {
                    btn.disabled = false; loader.style.display = 'none';
                }
            }

            /* ===== Unpack ===== */
            async function handleUnpack() {
                const btn = document.getElementById('btnUnpack');
                const loader = document.getElementById('unpackLoader');
                const fileInput = document.getElementById('unpackFile');
                const tokenText = document.getElementById('unpackText').value.trim();

                btn.disabled = true; loader.style.display = 'block';
                document.getElementById('unpackResult').style.display = 'none';

                try {
                    let tokenPayload = "";
                    if (fileInput.files.length > 0) {
                        tokenPayload = await new Promise((resolve, reject) => {
                            const r = new FileReader();
                            r.onload = e => resolve(e.target.result);
                            r.onerror = () => reject(new Error("Ошибка чтения токена"));
                            r.readAsText(fileInput.files[0]);
                        });
                    } else if (tokenText) { tokenPayload = tokenText; }
                    else { throw new Error('Выберите файл токена или вставьте текст.'); }

                    const splitStatus = getSplitTokenStatus(tokenPayload);
                    if (splitStatus.error) {
                        throw new Error(splitStatus.message);
                    }
                    if (splitStatus.incomplete) {
                        throw new Error('Вставлена только часть split-токена: найдено ' + splitStatus.found + ' из ' + splitStatus.total + '. Вставьте все части в это поле или выберите файл со всеми частями.');
                    }

                    const res = await fetch('/api/unpack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: tokenPayload }) });
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);

                    let html = unpackSuccessHtml(data, 'Предпросмотр обрезан. Полный файл на диске.');
                    showResult('unpackResult', 'success', html);
                    if (data.is_text && data.text_content !== null) {
                        document.getElementById('unpackedContentArea').value = data.text_content;
                    }
                } catch (err) {
                    showResult('unpackResult', 'error', err.message);
                } finally {
                    btn.disabled = false; loader.style.display = 'none';
                }
            }

            /* ===== AUTO-MODE: debounce + auto-pack + auto-unpack ===== */
            function _debounce(key, ms, fn) {
                clearTimeout(window[key]);
                window[key] = setTimeout(fn, ms);
            }

            async function autoPack() {
                if (!document.getElementById('autoMode').checked) return;
                const textContent = document.getElementById('packTextContent').value.trim();
                if (!textContent) return;
                if (document.getElementById('packFile').files.length > 0) return;
                if (document.getElementById('packFolder').files.length > 0) return;

                const loader = document.getElementById('packLoader');
                loader.style.display = 'block';
                try {
                    const payload = { is_folder: false, is_text: true, filename: 'pasted_text.txt', text_content: textContent };
                    applySplitParams(payload);
                    const res = await fetch('/api/pack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);
                    showPackTokenResult(data);
                } catch (err) { /* silent */ } finally { loader.style.display = 'none'; }
            }

            async function autoPackFile() {
                if (!document.getElementById('autoMode').checked) return;
                const btn = document.getElementById('btnPack');
                const loader = document.getElementById('packLoader');
                const fileInput = document.getElementById('packFile');
                const folderInput = document.getElementById('packFolder');

                if (fileInput.files.length === 0 && folderInput.files.length === 0) return;

                btn.disabled = true; loader.style.display = 'block';
                document.getElementById('packResult').style.display = 'none';

                try {
                    let payload = {};

                    if (fileInput.files.length > 0) {
                        const file = fileInput.files[0];
                        const base64 = await new Promise((resolve, reject) => {
                            const r = new FileReader();
                            r.onload = e => { const b = e.target.result.split(',')[1]; resolve(b ? b : ''); };
                            r.onerror = () => reject(new Error('Ошибка чтения файла'));
                            r.readAsDataURL(file);
                        });
                        payload = { is_folder: false, is_text: false, filename: file.name, content_b64: base64 };
                    }
                    else if (folderInput.files.length > 0) {
                        const files = folderInput.files;
                        const folderName = files[0].webkitRelativePath ? files[0].webkitRelativePath.split('/')[0] : 'packed_folder';
                        const fileData = await Promise.all(Array.from(files).map(file => new Promise((resolve, reject) => {
                            const r = new FileReader();
                            r.onload = e => { const b = e.target.result.split(',')[1]; resolve({ path: file.webkitRelativePath || file.name, b64: b ? b : '' }); };
                            r.onerror = () => reject(new Error('Ошибка чтения ' + file.name));
                            r.readAsDataURL(file);
                        })));
                        payload = { is_folder: true, folder_name: folderName, files: fileData };
                    }

                    applySplitParams(payload);
                    const res = await fetch('/api/pack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);

                    showPackTokenResult(data);
                } catch (err) { showResult('packResult', 'error', err.message); }
                finally { btn.disabled = false; loader.style.display = 'none'; }
            }

            async function autoUnpack() {
                if (!document.getElementById('autoMode').checked) return;
                const tokenText = document.getElementById('unpackText').value.trim();
                if (!tokenText) return;
                if (!tokenText.includes('FTPKG1.')) return;
                if (document.getElementById('unpackFile').files.length > 0) return;

                const splitStatus = getSplitTokenStatus(tokenText);
                if (splitStatus.error) {
                    showResult('unpackResult', 'error', splitStatus.message);
                    return;
                }
                if (splitStatus.incomplete) {
                    showResult(
                        'unpackResult',
                        'success',
                        'Вставлена часть split-токена: найдено <b>' + splitStatus.found + '</b> из <b>' + splitStatus.total + '</b>. Вставьте остальные части, авто-распаковка запустится после полного набора.'
                    );
                    return;
                }

                const loader = document.getElementById('unpackLoader');
                loader.style.display = 'block';
                try {
                    const res = await fetch('/api/unpack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: tokenText }) });
                    const data = await res.json();
                    if (data.error) return;
                    let html = unpackSuccessHtml(data, 'Предпросмотр обрезан.');
                    showResult('unpackResult', 'success', html);
                    if (data.is_text && data.text_content !== null) {
                        document.getElementById('unpackedContentArea').value = data.text_content;
                    }
                } catch (err) { /* silent */ } finally { loader.style.display = 'none'; }
            }

            updateSplitModeInputs();
            document.getElementById('packTextContent').addEventListener('input', function() { detectPackTextSizeForSplit(); _debounce('_packAuto', 800, autoPack); });
            document.getElementById('unpackText').addEventListener('input', function() { _debounce('_unpackAuto', 800, autoUnpack); });
        </script>
    </body>
    </html>
    """

    class FTRequestHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send_json(self, data, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path not in ["/api/pack", "/api/unpack", "/api/open-folder"]:
                return self._send_json({"error": "Not Found"}, 404)

            try:
                content_length = int(self.headers.get("Content-Length", 0))
                if content_length == 0:
                    return self._send_json({"error": "Empty body"}, 400)

                body = self.rfile.read(content_length).decode("utf-8")
                data = json.loads(body)
            except Exception as e:
                return self._send_json(
                    {
                        "error": f"Ошибка обработки запроса (возможно превышен лимит): {e}"
                    },
                    400,
                )

            if self.path == "/api/open-folder":
                try:
                    restored_root = (Path.cwd() / "restored").resolve()
                    requested = data.get("path") or str(restored_root)
                    target = Path(requested).resolve()
                    if not _is_path_inside(restored_root, target) and target != restored_root:
                        return self._send_json(
                            {"error": "Можно открыть только папку restored"}, 400
                        )
                    folder = target.parent if target.is_file() else target
                    if not folder.exists():
                        return self._send_json({"error": "Папка не найдена"}, 404)
                    open_folder_path(folder)
                    return self._send_json({"status": "ok", "path": str(folder)})
                except Exception as exc:
                    return self._send_json({"error": str(exc)}, 500)

            if self.path == "/api/pack":
                try:
                    with tempfile.TemporaryDirectory(prefix="ft_web_pack_") as tmp_dir:
                        if data.get("is_folder"):
                            folder_name = (
                                Path(data.get("folder_name", "packed_folder")).name
                                or "packed_folder"
                            )
                            source_path = Path(tmp_dir) / folder_name
                            source_path.mkdir(parents=True, exist_ok=True)

                            for f in data.get("files", []):
                                file_rel_path = f.get("path", "")
                                if not file_rel_path:
                                    continue

                                safe_parts = [
                                    p
                                    for p in Path(file_rel_path).parts
                                    if p not in ("", ".", "..")
                                    and not p.startswith("\\")
                                    and not p.startswith("/")
                                ]
                                if not safe_parts:
                                    continue

                                full_path = Path(tmp_dir).joinpath(*safe_parts)
                                full_path.parent.mkdir(parents=True, exist_ok=True)

                                try:
                                    full_path.write_bytes(
                                        base64.b64decode(f.get("b64", ""))
                                    )
                                except Exception as write_err:
                                    print(
                                        f"Skipping file {full_path}: {write_err}",
                                        file=sys.stderr,
                                    )
                                    continue

                        else:
                            filename = Path(data.get("filename", "file")).name or "file"
                            source_path = Path(tmp_dir) / filename

                            if data.get("is_text"):
                                source_path.write_text(
                                    data.get("text_content", ""), encoding="utf-8"
                                )
                            else:
                                b64 = data.get("content_b64", "")
                                source_path.write_bytes(base64.b64decode(b64))

                        split_n = data.get("split")
                        split_threshold = data.get("split_threshold")
                        archive_bytes = make_tar_xz(source_path)
                        actual_split = int(split_n) if split_n and int(split_n) > 1 else None
                        if actual_split is None and split_threshold:
                            threshold_bytes = max(1, int(split_threshold))
                            if len(archive_bytes) > threshold_bytes:
                                actual_split = max(
                                    2, (len(archive_bytes) - 1) // threshold_bytes + 1
                                )

                        if actual_split and actual_split > 1:
                            kind = "dir" if source_path.is_dir() else "file"
                            chunks = split_archive_bytes(archive_bytes, actual_split)
                            tokens = [
                                _build_one_token(
                                    source_path.name,
                                    kind,
                                    chunk,
                                    split_total=actual_split,
                                    split_index=i,
                                )
                                for i, chunk in enumerate(chunks)
                            ]
                            self._send_json(
                                {"tokens": tokens, "filename": source_path.name}
                            )
                        else:
                            token = _build_one_token(
                                source_path.name,
                                "dir" if source_path.is_dir() else "file",
                                archive_bytes,
                            )
                            self._send_json(
                                {"token": token, "filename": source_path.name}
                            )
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 500)

            elif self.path == "/api/unpack":
                raw_text = data.get("token", "")
                if not raw_text.strip():
                    return self._send_json({"error": "Токен пуст"}, 400)

                try:
                    all_tokens = extract_all_tokens_from_text(raw_text)
                    if not all_tokens:
                        raise ValueError("Пакет не найден в тексте")
                    meta, archive_bytes = reassemble_from_parts(all_tokens)
                    target_dir = Path.cwd() / "restored"
                    extract_tar_xz(archive_bytes, target_dir)

                    restore_root = meta.get("root")
                    if restore_root:
                        restored_path = target_dir / restore_root
                    elif "name" in meta:
                        restored_path = target_dir / meta["name"]
                    else:
                        restored_path = target_dir

                    is_text = False
                    text_content = None
                    text_truncated = False
                    if restored_path.is_file():
                        try:
                            is_text, text_content, text_truncated, _ = (
                                read_text_preview(restored_path)
                            )
                            if not is_text:
                                text_content = None
                        except Exception:
                            text_content = None

                    self._send_json(
                        {
                            "status": "ok",
                            "path": str(restored_path.resolve()),
                            "is_text": is_text,
                            "text_content": text_content,
                            "text_truncated": text_truncated,
                        }
                    )
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 400)

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), FTRequestHandler)
        print(
            f"[*] Запуск Zero-Dependency Web-сервера FT Transfer на http://0.0.0.0:{port}"
        )
        print("[*] Нажмите Ctrl+C для остановки")
        server.serve_forever()
    except OSError as e:
        print(
            f"Ошибка: Не удалось запустить сервер на порту {port}. Порт занят? ({e})",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("\n[*] Сервер остановлен.")
        server.server_close()
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
            self.geometry("980x820")
            self.minsize(920, 720)

            self.pack_source_var = tk.StringVar()
            self.pack_output_var = tk.StringVar()
            self.pack_text_name_var = tk.StringVar(value="pasted_text.txt")
            self.unpack_input_var = tk.StringVar()
            self.unpack_output_var = tk.StringVar(
                value=str((Path.cwd() / "restored").resolve())
            )
            self.status_var = tk.StringVar(value="Готово")
            self.busy = False
            self.last_unpacked_path = None
            self.unpack_preview_text_cache = ""
            # Автоматический режим — включён по умолчанию
            self.auto_mode_var = tk.BooleanVar(value=True)
            # Разделение больших файлов
            self.split_enabled_var = tk.BooleanVar(value=False)
            self.split_mode_var = tk.StringVar(value="parts")
            self.split_parts_var = tk.StringVar(value="2")
            self.split_threshold_var = tk.StringVar(value="500")
            # Cached full token for save/copy (not truncated preview)
            self.last_pack_token = ""
            self.last_pack_tokens = []
            self.pack_result_var = tk.StringVar(value="")
            self.pack_part_var = tk.StringVar(value="")
            self.split_hint_var = tk.StringVar(value="")

            self._configure_style()
            self._build_ui()

        def _configure_style(self):
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure(
                "Title.TLabel", font=("Segoe UI", 18, "bold"), padding=(0, 0, 0, 4)
            )
            style.configure(
                "Subtitle.TLabel", font=("Segoe UI", 10), foreground="#5c6470"
            )
            style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
            style.configure(
                "Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 8)
            )
            style.configure("TButton", padding=(8, 5))
            style.configure("TEntry", padding=5)
            style.configure(
                "Copied.TButton",
                font=("Segoe UI", 9, "bold"),
                foreground="white",
                background="#36b37e",
                padding=(8, 5),
            )
            style.map(
                "Copied.TButton",
                background=[("active", "#2e9e6a"), ("disabled", "#a5d6b8")],
            )

        def _build_ui(self):
            root = ttk.Frame(self, padding=18)
            root.pack(fill="both", expand=True)

            ttk.Label(root, text="FT Transfer", style="Title.TLabel").pack(anchor="w")
            ttk.Label(
                root,
                text="Удобная упаковка файлов, папок и вставленного текста в FTPKG-токен и восстановление обратно.",
                style="Subtitle.TLabel",
            ).pack(anchor="w", pady=(0, 6))

            # Галочка автоматического режима (общая для обеих вкладок)
            auto_frame = ttk.Frame(root)
            auto_frame.pack(fill="x", pady=(0, 10))
            ttk.Checkbutton(
                auto_frame,
                text="Автоматический режим: пересчёт токена и распаковка при вставке текста",
                variable=self.auto_mode_var,
            ).pack(anchor="w")

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
            source_box = ttk.Labelframe(
                self.pack_tab, text="Что упаковать", style="Section.TLabelframe"
            )
            source_box.pack(fill="x")
            self._path_row(
                source_box,
                "Файл или папка:",
                self.pack_source_var,
                [("Файл", self._choose_pack_file), ("Папка", self._choose_pack_dir)],
            )
            self._path_row(
                source_box,
                "Куда сохранить .ft.txt:",
                self.pack_output_var,
                [("Выбрать", self._choose_pack_output)],
            )

            # --- Разделение больших файлов ---
            split_box = ttk.Labelframe(
                self.pack_tab, text="Разделение (Split)", style="Section.TLabelframe"
            )
            split_box.pack(fill="x", pady=(12, 0))
            split_row = ttk.Frame(split_box, padding=(10, 8))
            split_row.pack(fill="x")
            ttk.Checkbutton(
                split_row,
                text="Разделять большие файлы",
                variable=self.split_enabled_var,
                command=self._update_split_controls,
            ).pack(side="left")
            ttk.Radiobutton(
                split_row,
                text="по количеству",
                variable=self.split_mode_var,
                value="parts",
                command=self._update_split_controls,
            ).pack(side="left", padx=(16, 2))
            ttk.Label(split_row, text="Частей:").pack(side="left", padx=(4, 2))
            self.split_parts_spin = ttk.Spinbox(
                split_row, from_=2, to=64, width=4, textvariable=self.split_parts_var
            )
            self.split_parts_spin.pack(side="left")
            ttk.Radiobutton(
                split_row,
                text="по порогу",
                variable=self.split_mode_var,
                value="threshold",
                command=self._update_split_controls,
            ).pack(side="left", padx=(16, 2))
            ttk.Label(split_row, text="Порог (KB):").pack(side="left", padx=(4, 2))
            self.split_threshold_entry = ttk.Entry(
                split_row, textvariable=self.split_threshold_var, width=8
            )
            self.split_threshold_entry.pack(side="left")
            ttk.Label(
                split_box,
                textvariable=self.split_hint_var,
                style="Subtitle.TLabel",
            ).pack(anchor="w", padx=10, pady=(0, 8))
            self._update_split_controls()

            text_box = ttk.Labelframe(
                self.pack_tab,
                text="Или вставьте текст без выбора файла",
                style="Section.TLabelframe",
            )
            text_box.pack(fill="both", expand=True, pady=(12, 0))
            name_row = ttk.Frame(text_box, padding=(10, 8, 10, 0))
            name_row.pack(fill="x")
            ttk.Label(name_row, text="Имя файла в пакете:", width=22).pack(side="left")
            ttk.Entry(name_row, textvariable=self.pack_text_name_var).pack(
                side="left", fill="x", expand=True
            )
            ttk.Label(
                text_box,
                text="Если поле ниже не пустое, будет упакован этот текст как обычный .txt-файл.",
                style="Subtitle.TLabel",
            ).pack(anchor="w", padx=10, pady=(6, 0))
            self.pack_text = scrolledtext.ScrolledText(
                text_box, wrap="word", height=12, undo=True
            )
            self.pack_text.pack(fill="both", expand=True, padx=10, pady=(8, 8))
            # Автоупаковка при вводе текста
            self.pack_text.bind("<KeyRelease>", self._on_pack_text_changed)

            text_actions = ttk.Frame(text_box)
            text_actions.pack(fill="x", padx=10, pady=(0, 10))
            ttk.Button(
                text_actions,
                text="Вставить из буфера",
                command=self._paste_pack_clipboard,
            ).pack(side="left")
            ttk.Button(
                text_actions, text="Очистить текст", command=self._clear_pack_text
            ).pack(side="left", padx=(8, 0))

            actions = ttk.Frame(self.pack_tab)
            actions.pack(fill="x", pady=(12, 8))
            ttk.Button(
                actions,
                text="Упаковать",
                style="Accent.TButton",
                command=self._pack_clicked,
            ).pack(side="left")
            ttk.Button(actions, text="Очистить", command=self._clear_pack).pack(
                side="left", padx=(8, 0)
            )
            ttk.Label(
                actions,
                textvariable=self.pack_result_var,
                style="Subtitle.TLabel",
            ).pack(side="left", fill="x", expand=True, padx=(12, 8))
            ttk.Button(
                actions,
                text="Открыть папку результата",
                command=self._open_pack_output_folder,
            ).pack(side="right")

            preview_box = ttk.Labelframe(
                self.pack_tab, text="Готовый токен", style="Section.TLabelframe"
            )
            preview_box.pack(fill="x", expand=False)
            self.pack_preview = scrolledtext.ScrolledText(
                preview_box, wrap="char", height=4, undo=False
            )
            self.pack_preview.pack(fill="both", expand=True, padx=10, pady=(8, 8))

            preview_actions = ttk.Frame(preview_box)
            preview_actions.pack(fill="x", padx=10, pady=(0, 10))
            self.copy_pack_preview_btn = ttk.Button(
                preview_actions,
                text="Копировать токен",
                command=self._copy_pack_preview,
            )
            self.copy_pack_preview_btn.pack(side="left")
            ttk.Label(preview_actions, text="Часть:").pack(side="left", padx=(12, 4))
            self.pack_part_combo = ttk.Combobox(
                preview_actions,
                textvariable=self.pack_part_var,
                width=8,
                state="disabled",
            )
            self.pack_part_combo.pack(side="left")
            self.copy_pack_part_btn = ttk.Button(
                preview_actions,
                text="Копировать часть",
                command=self._copy_selected_pack_part,
                state="disabled",
            )
            self.copy_pack_part_btn.pack(side="left", padx=(8, 0))
            ttk.Button(
                preview_actions,
                text="Сохранить как...",
                command=self._save_pack_preview_as,
            ).pack(side="left", padx=(8, 0))

        def _build_unpack_tab(self):
            input_box = ttk.Labelframe(
                self.unpack_tab, text="Откуда взять токен", style="Section.TLabelframe"
            )
            input_box.pack(fill="x")
            self._path_row(
                input_box,
                "Файл .ft.txt:",
                self.unpack_input_var,
                [
                    ("Выбрать", self._choose_unpack_input),
                    ("Загрузить", self._load_unpack_input_file),
                ],
            )
            self._path_row(
                input_box,
                "Папка назначения:",
                self.unpack_output_var,
                [("Выбрать", self._choose_unpack_output)],
            )

            text_box = ttk.Labelframe(
                self.unpack_tab,
                text="Или вставьте токен / текст из Figma",
                style="Section.TLabelframe",
            )
            text_box.pack(fill="x", expand=False, pady=(12, 0))
            self.unpack_text = scrolledtext.ScrolledText(
                text_box, wrap="word", height=6, undo=True
            )
            self.unpack_text.pack(fill="both", expand=True, padx=10, pady=(8, 8))
            # Автораспаковка при вводе токена
            self.unpack_text.bind("<KeyRelease>", self._on_unpack_text_changed)

            text_actions = ttk.Frame(text_box)
            text_actions.pack(fill="x", padx=10, pady=(0, 10))
            ttk.Button(
                text_actions,
                text="Вставить из буфера",
                command=self._paste_unpack_clipboard,
            ).pack(side="left")
            ttk.Button(
                text_actions, text="Очистить текст", command=self._clear_unpack_text
            ).pack(side="left", padx=(8, 0))
            ttk.Button(
                text_actions,
                text="Открыть папку назначения",
                command=self._open_unpack_output_folder,
            ).pack(side="right")

            actions = ttk.Frame(self.unpack_tab)
            actions.pack(fill="x", pady=(12, 0))
            ttk.Button(
                actions,
                text="Распаковать",
                style="Accent.TButton",
                command=self._unpack_clicked,
            ).pack(side="left")
            self.open_unpacked_btn = ttk.Button(
                actions,
                text="Открыть результат",
                command=self._open_unpacked_path,
                state="disabled",
            )
            self.open_unpacked_btn.pack(side="right")
            self.copy_unpacked_result_btn = ttk.Button(
                actions,
                text="Копировать результат",
                command=self._copy_unpack_preview,
                state="disabled",
            )
            self.copy_unpacked_result_btn.pack(side="right", padx=(0, 8))
            self.unpack_result_var = tk.StringVar(value="")
            ttk.Label(actions, textvariable=self.unpack_result_var, width=48).pack(
                side="left", fill="x", expand=True, padx=(12, 8)
            )

            preview_box = ttk.Labelframe(
                self.unpack_tab, text="Просмотр результата", style="Section.TLabelframe"
            )
            preview_box.pack(fill="both", expand=True, pady=(10, 0))
            self.unpack_preview_title_var = tk.StringVar(
                value="После распаковки одиночного файла здесь появится предпросмотр."
            )
            ttk.Label(
                preview_box,
                textvariable=self.unpack_preview_title_var,
                style="Subtitle.TLabel",
            ).pack(anchor="w", padx=10, pady=(8, 0))
            self.unpack_preview = scrolledtext.ScrolledText(
                preview_box, wrap="word", height=16, undo=False, state="disabled"
            )
            self.unpack_preview.pack(fill="both", expand=True, padx=10, pady=(8, 8))

            preview_actions = ttk.Frame(preview_box)
            preview_actions.pack(fill="x", padx=10, pady=(0, 10))
            self.copy_unpack_preview_btn = ttk.Button(
                preview_actions,
                text="Копировать текст просмотра",
                command=self._copy_unpack_preview,
                state="disabled",
            )
            self.copy_unpack_preview_btn.pack(side="left")
            self.save_unpacked_file_btn = ttk.Button(
                preview_actions,
                text="Сохранить копию файла...",
                command=self._save_unpacked_file_as,
                state="disabled",
            )
            self.save_unpacked_file_btn.pack(side="left", padx=(8, 0))

        # -----------------------------------------------------------------
        # Вспомогательные методы debounce / статус
        # -----------------------------------------------------------------
        def _debounce(self, ms, func, attr_name="_debounce_id"):
            if hasattr(self, attr_name):
                self.after_cancel(getattr(self, attr_name))
            setattr(self, attr_name, self.after(ms, func))

        def _show_temporary_status(self, text, ms=1000):
            self._set_status(text)
            if hasattr(self, "_status_after_id"):
                self.after_cancel(self._status_after_id)
            self._status_after_id = self.after(ms, lambda: self._set_status("Готово"))

        def _animate_button_copy(self, button):
            """Показать анимацию «Скопировано!» на кнопке."""
            original_text = button.cget("text")
            # Сохраняем оригинальный стиль для корректного восстановления
            original_style = button.cget("style") or "TButton"
            button.configure(
                text="✓ Скопировано!", style="Copied.TButton", state="disabled"
            )
            button.update_idletasks()

            def restore():
                button.configure(
                    text=original_text, style=original_style, state="normal"
                )

            self.after(1500, restore)

        def _update_split_controls(self):
            if not hasattr(self, "split_parts_spin"):
                return
            enabled = self.split_enabled_var.get()
            mode = self.split_mode_var.get()
            self.split_parts_spin.configure(
                state="normal" if enabled and mode == "parts" else "disabled"
            )
            self.split_threshold_entry.configure(
                state="normal" if enabled and mode == "threshold" else "disabled"
            )

        def _auto_enable_split_for_large_input(self, size_bytes, description):
            try:
                threshold_bytes = int(float(self.split_threshold_var.get()) * 1024)
            except ValueError:
                threshold_bytes = SPLIT_DEFAULT_THRESHOLD
            if not size_bytes or size_bytes <= threshold_bytes:
                self.split_hint_var.set("")
                return
            if not self.split_enabled_var.get():
                self.split_enabled_var.set(True)
                self.split_mode_var.set("parts")
                self._update_split_controls()
            self.split_hint_var.set(
                "Источник большой: {} ({}) > порога {}. Разделение автоматически включено по количеству частей; можно выключить вручную.".format(
                    description,
                    self._format_bytes(size_bytes),
                    self._format_bytes(threshold_bytes),
                )
            )

        def _make_pack_preview_text(self, tokens):
            if not tokens:
                return ""
            if len(tokens) > 1:
                first = tokens[0]
                preview = first[:GUI_TOKEN_PREVIEW_MAX_CHARS]
                if len(first) > GUI_TOKEN_PREVIEW_MAX_CHARS:
                    preview += "\n... (часть 1 обрезана для предпросмотра)"
                return (
                    "Токен разделён на {} частей.\n"
                    "Копируйте части отдельными кнопками ниже; для распаковки нужны все части.\n\n"
                    "Предпросмотр части 1/{}:\n{}"
                ).format(len(tokens), len(tokens), preview)
            token = tokens[0]
            preview = token[:GUI_TOKEN_PREVIEW_MAX_CHARS]
            if len(token) > GUI_TOKEN_PREVIEW_MAX_CHARS:
                preview += "\n... (обрезано для предпросмотра)"
            return preview

        def _update_pack_part_controls(self):
            if len(self.last_pack_tokens) > 1:
                values = [
                    "{} / {}".format(i + 1, len(self.last_pack_tokens))
                    for i in range(len(self.last_pack_tokens))
                ]
                self.pack_part_combo.configure(values=values, state="readonly")
                self.pack_part_var.set(values[0])
                self.copy_pack_part_btn.configure(state="normal")
                self.copy_pack_preview_btn.configure(state="disabled")
            else:
                self.pack_part_combo.configure(values=[], state="disabled")
                self.pack_part_var.set("")
                self.copy_pack_part_btn.configure(state="disabled")
                self.copy_pack_preview_btn.configure(state="normal")

        # -----------------------------------------------------------------
        # Автоупаковка
        # -----------------------------------------------------------------
        def _on_pack_text_changed(self, event=None):
            text = self.pack_text.get("1.0", "end-1c")
            if text:
                self._auto_enable_split_for_large_input(
                    len(text.encode("utf-8")), "текст"
                )
            if not self.auto_mode_var.get():
                return
            # Если начали печатать текст — сбрасываем выбор файла/папки
            if self.pack_source_var.get().strip():
                self.pack_source_var.set("")
            self._debounce(800, self._auto_pack_text, "_pack_debounce_id")

        def _auto_pack_text(self):
            if not self.auto_mode_var.get() or self.busy:
                return
            pasted_text = self.pack_text.get("1.0", "end-1c").strip()
            if not pasted_text:
                return
            if self.pack_source_var.get().strip():
                return

            text_file_name = self._get_pack_text_filename()

            def work():
                with tempfile.TemporaryDirectory(prefix="ft_gui_text_") as tmp_dir:
                    source = Path(tmp_dir) / text_file_name
                    save_text_file(source, pasted_text)
                    token = build_token(source)
                return token, text_file_name

            def done(result):
                token, name = result
                self.last_pack_token = token
                self.last_pack_tokens = [token]
                self.pack_preview.delete("1.0", "end")
                self.pack_preview.insert("1.0", self._make_pack_preview_text([token]))
                self._update_pack_part_controls()
                preview_note = (
                    " (предпросмотр обрезан)"
                    if len(token) > GUI_TOKEN_PREVIEW_MAX_CHARS
                    else ""
                )
                self.pack_result_var.set(
                    "✅ Токен сгенерирован: {} символов{}".format(
                        len(token), preview_note
                    )
                )
                self._show_temporary_status("Авто: токен обновлён")

            self._run_background("Автоупаковка...", work, done, silent=True)

        # -----------------------------------------------------------------
        # Автораспаковка
        # -----------------------------------------------------------------
        def _on_unpack_text_changed(self, event=None):
            if not self.auto_mode_var.get():
                return
            # Если начали печатать токен — сбрасываем выбор файла
            if self.unpack_input_var.get().strip():
                self.unpack_input_var.set("")
            self._debounce(800, self._auto_unpack_text, "_unpack_debounce_id")

        def _auto_unpack_text(self):
            if not self.auto_mode_var.get() or self.busy:
                return
            raw_text = self.unpack_text.get("1.0", "end-1c").strip()
            if not raw_text:
                return
            if self.unpack_input_var.get().strip():
                return

            output_text = self.unpack_output_var.get().strip()
            if not output_text:
                return
            target_dir = Path(output_text).resolve()

            def work():
                all_tokens = extract_all_tokens_from_text(raw_text)
                if not all_tokens:
                    raise ValueError("Пакет не найден в тексте")
                meta, archive_bytes = reassemble_from_parts(all_tokens)
                extract_tar_xz(archive_bytes, target_dir)
                restore_root = meta.get("root")
                if restore_root:
                    restored_path = target_dir / restore_root
                elif "name" in meta:
                    restored_path = target_dir / meta["name"]
                else:
                    restored_path = target_dir
                return restored_path, meta

            def done(result):
                restored_path, meta = result
                self._show_unpack_result(restored_path, "авто-вставка", meta)
                if restored_path.is_file():
                    is_text, text_content, truncated, size = read_text_preview(
                        restored_path
                    )
                    if is_text and text_content:
                        self._show_temporary_status("Авто: распаковано")
                    else:
                        self._show_temporary_status("Авто: файл распакован (бинарный)")
                else:
                    self._show_temporary_status("Авто: папка распакована")

            self._clear_unpack_result_preview()
            self._run_background("Автораспаковка...", work, done, silent=True)

        # -----------------------------------------------------------------
        # Остальные методы GUI
        # -----------------------------------------------------------------
        def _path_row(self, parent, label, variable, buttons):
            row = ttk.Frame(parent, padding=(12, 10, 12, 4))
            row.pack(fill="x")
            ttk.Label(row, text=label, width=22).pack(side="left")
            ttk.Entry(row, textvariable=variable).pack(
                side="left", fill="x", expand=True, padx=(0, 8)
            )
            for text, command in buttons:
                ttk.Button(row, text=text, command=command).pack(
                    side="left", padx=(0, 4)
                )

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
            if source.is_file():
                try:
                    self._auto_enable_split_for_large_input(source.stat().st_size, "файл")
                except OSError:
                    pass
            self.pack_output_var.set(
                str((source.parent / "{}.ft.txt".format(source.name)).resolve())
            )

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
                filetypes=[
                    ("FT token", "*.ft.txt"),
                    ("Text files", "*.txt"),
                    ("All files", "*.*"),
                ],
            )
            if path:
                self.pack_output_var.set(path)

        def _choose_unpack_input(self):
            path = filedialog.askopenfilename(
                title="Выберите файл с токеном",
                filetypes=[
                    ("FT token", "*.ft.txt"),
                    ("Text files", "*.txt"),
                    ("All files", "*.*"),
                ],
            )
            if path:
                self.unpack_input_var.set(path)
                # Очищаем текстовое поле, чтобы авто-режим не конфликтовал
                self.unpack_text.delete("1.0", "end")
                self._clear_unpack_result_preview()

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
            self._clear_unpack_result_preview()
            self._set_status("Файл загружен")

        def _choose_unpack_input_or_clipboard(self):
            text = self.unpack_text.get("1.0", "end").strip()
            if text:
                return text, "поле ввода"

            path_text = self.unpack_input_var.get().strip()
            if path_text:
                return read_text_file(Path(path_text)), "файл:{}".format(
                    Path(path_text).resolve()
                )

            try:
                clipboard_text = self.clipboard_get()
            except tk.TclError:
                clipboard_text = ""
            if clipboard_text.strip():
                return clipboard_text, "буфер обмена"

            raise ValueError(
                "Нет текста для распаковки: вставьте токен, выберите файл или скопируйте токен в буфер"
            )

        def _pack_clicked(self):
            if self.busy:
                return

            # Отменяем pending debounce автоупаковки, чтобы не перезаписала результат
            if hasattr(self, "_pack_debounce_id"):
                self.after_cancel(self._pack_debounce_id)
                del self._pack_debounce_id

            pasted_text = self.pack_text.get("1.0", "end-1c")
            has_pasted_text = bool(pasted_text.strip())
            source_text = self.pack_source_var.get().strip()
            output_text = self.pack_output_var.get().strip()
            output_path = Path(output_text).resolve() if output_text else None

            # Split params: either fixed parts OR threshold, not both.
            do_split = self.split_enabled_var.get()
            split_mode = self.split_mode_var.get()
            split_n = None
            if do_split and split_mode == "parts":
                try:
                    split_n = int(self.split_parts_var.get())
                except ValueError:
                    split_n = 2
            split_thresh = (
                self.split_threshold_var.get().strip()
                if do_split and split_mode == "threshold"
                else None
            )
            threshold_bytes = None
            if split_thresh:
                try:
                    threshold_bytes = int(float(split_thresh) * 1024)
                except ValueError:
                    threshold_bytes = SPLIT_DEFAULT_THRESHOLD

            if has_pasted_text:
                text_file_name = self._get_pack_text_filename()
                if output_path is None:
                    output_path = (
                        Path.cwd() / "{}.ft.txt".format(text_file_name)
                    ).resolve()
                    self.pack_output_var.set(str(output_path))

                def work():
                    with tempfile.TemporaryDirectory(prefix="ft_gui_text_") as tmp_dir:
                        source = Path(tmp_dir) / text_file_name
                        save_text_file(source, pasted_text)
                        archive_bytes = make_tar_xz(source)
                        actual_split = split_n
                        if (
                            actual_split is None
                            and threshold_bytes is not None
                            and len(archive_bytes) > threshold_bytes
                        ):
                            actual_split = max(
                                2, (len(archive_bytes) - 1) // threshold_bytes + 1
                            )
                        if actual_split and actual_split > 1:
                            kind = "dir" if source.is_dir() else "file"
                            chunks = split_archive_bytes(archive_bytes, actual_split)
                            tokens = [
                                _build_one_token(
                                    source.name,
                                    kind,
                                    chunk,
                                    split_total=actual_split,
                                    split_index=i,
                                )
                                for i, chunk in enumerate(chunks)
                            ]
                        else:
                            tokens = [build_token(source)]
                    all_text = "\n".join(tokens)
                    if output_path:
                        if len(tokens) == 1:
                            save_text_file(output_path, all_text)
                        else:
                            stem = output_path.stem
                            parent = output_path.parent
                            for i, tok in enumerate(tokens):
                                part_path = parent / "{}_part{}_{}.ft.txt".format(
                                    stem, i + 1, len(tokens)
                                )
                                save_text_file(part_path, tok)
                    return (
                        all_text,
                        output_path,
                        "вставленный текст ({})".format(text_file_name),
                        tokens,
                    )
            else:
                if not source_text:
                    messagebox.showwarning(
                        "Не выбран источник",
                        "Выберите файл/папку или вставьте текст для упаковки.",
                    )
                    return
                source = Path(source_text).resolve()
                if not source.exists():
                    messagebox.showerror("Источник не найден", str(source))
                    return

                def work():
                    archive_bytes = make_tar_xz(source)
                    actual_split = split_n
                    if (
                        actual_split is None
                        and threshold_bytes is not None
                        and len(archive_bytes) > threshold_bytes
                    ):
                        actual_split = max(
                            2, (len(archive_bytes) - 1) // threshold_bytes + 1
                        )
                    if actual_split and actual_split > 1:
                        kind = "dir" if source.is_dir() else "file"
                        chunks = split_archive_bytes(archive_bytes, actual_split)
                        tokens = [
                            _build_one_token(
                                source.name,
                                kind,
                                chunk,
                                split_total=actual_split,
                                split_index=i,
                            )
                            for i, chunk in enumerate(chunks)
                        ]
                    else:
                        tokens = [build_token(source)]
                    all_text = "\n".join(tokens)
                    if output_path:
                        if len(tokens) == 1:
                            save_text_file(output_path, all_text)
                        else:
                            stem = output_path.stem
                            parent = output_path.parent
                            for i, tok in enumerate(tokens):
                                part_path = parent / "{}_part{}_{}.ft.txt".format(
                                    stem, i + 1, len(tokens)
                                )
                                save_text_file(part_path, tok)
                    return all_text, output_path, str(source), tokens

            def done(result):
                all_text, saved_path, source_desc, tokens = result
                n_tokens = len(tokens)
                self.last_pack_token = all_text
                self.last_pack_tokens = tokens
                self.pack_preview.delete("1.0", "end")
                self.pack_preview.insert("1.0", self._make_pack_preview_text(tokens))
                self._update_pack_part_controls()
                preview_note = (
                    " (предпросмотр обрезан)"
                    if any(len(token) > GUI_TOKEN_PREVIEW_MAX_CHARS for token in tokens)
                    else ""
                )
                self.pack_result_var.set(
                    "✅ Токен сгенерирован{}: {} символов{}".format(
                        " ({} частей)".format(n_tokens) if n_tokens > 1 else "",
                        len(all_text),
                        preview_note,
                    )
                )
                # Автокопирование токена в буфер обмена + анимация кнопки
                clip_ok = self._copy_text_to_clipboard(all_text)
                if clip_ok:
                    self._animate_button_copy(self.copy_pack_preview_btn)
                parts = [
                    "Упаковка завершена",
                    "{} символов".format(len(all_text)),
                    "источник: {}".format(source_desc),
                ]
                if n_tokens > 1:
                    parts.append("частей: {}".format(n_tokens))
                if saved_path:
                    parts.append("файл: {}".format(saved_path))
                if not clip_ok and len(all_text) > CLIPBOARD_MAX_CHARS:
                    parts.append("буфер: слишком большой")
                self._set_status("; ".join(parts))

            self.pack_result_var.set("Генерация токена...")
            self._run_background("Упаковка...", work, done)

        def _unpack_clicked(self):
            if self.busy:
                return

            # Отменяем pending debounce автораспаковки
            if hasattr(self, "_unpack_debounce_id"):
                self.after_cancel(self._unpack_debounce_id)
                del self._unpack_debounce_id

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
                all_tokens = extract_all_tokens_from_text(raw_text)
                if not all_tokens:
                    raise ValueError("Пакет не найден в тексте")
                meta, archive_bytes = reassemble_from_parts(all_tokens)
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
                self._show_unpack_result(restored_path, source_desc, meta)

            self._clear_unpack_result_preview()
            self._run_background("Распаковка...", work, done)

        def _set_unpack_preview_text(self, text):
            self.unpack_preview.configure(state="normal")
            self.unpack_preview.delete("1.0", "end")
            if text:
                display = text
                if len(display) > GUI_PREVIEW_MAX_CHARS:
                    display = (
                        display[:GUI_PREVIEW_MAX_CHARS]
                        + "\n... (обрезано для предпросмотра)"
                    )
                self.unpack_preview.insert("1.0", display)
            self.unpack_preview.configure(state="disabled")
            self.unpack_preview_text_cache = text or ""

        def _clear_unpack_result_preview(self):
            self.last_unpacked_path = None
            self.unpack_result_var.set("")
            self.unpack_preview_title_var.set(
                "После распаковки одиночного файла здесь появится предпросмотр."
            )
            self._set_unpack_preview_text("")
            self.open_unpacked_btn.configure(state="disabled")
            self.copy_unpacked_result_btn.configure(state="disabled")
            self.copy_unpack_preview_btn.configure(state="disabled")
            self.save_unpacked_file_btn.configure(state="disabled")

        def _show_unpack_result(self, restored_path, source_desc, meta):
            restored_path = Path(restored_path)
            self.last_unpacked_path = restored_path
            self.unpack_result_var.set("Восстановлено: {}".format(restored_path))
            self.open_unpacked_btn.configure(state="normal")
            self.copy_unpacked_result_btn.configure(state="normal")
            self._set_status(
                "Распаковка завершена; источник: {}; тип: {}".format(
                    source_desc, meta.get("kind", "?")
                )
            )

            if restored_path.is_file():
                is_text, preview_text, truncated, size = read_text_preview(
                    restored_path
                )
                size_label = self._format_bytes(size)
                self.unpack_preview_title_var.set(
                    "Файл: {} | Размер: {}".format(restored_path.name, size_label)
                )
                self.save_unpacked_file_btn.configure(state="normal")

                if is_text:
                    if truncated:
                        preview_text += "\n\n---\nПредпросмотр обрезан до {}. Полный файл сохранён на диск.".format(
                            self._format_bytes(TEXT_PREVIEW_MAX_BYTES)
                        )
                    self._set_unpack_preview_text(preview_text)
                    self.copy_unpack_preview_btn.configure(state="normal")
                else:
                    self._set_unpack_preview_text(
                        "Одиночный файл восстановлен, но он выглядит как бинарный или не является текстом.\n\n"
                        "Путь: {}\nРазмер: {}\n\n"
                        "Используйте кнопку «Открыть результат» или «Сохранить копию файла...».".format(
                            restored_path, size_label
                        )
                    )
                    self.copy_unpack_preview_btn.configure(state="disabled")
                return

            if restored_path.is_dir():
                files_count, dirs_count = self._count_folder_entries(restored_path)
                self.unpack_preview_title_var.set(
                    "Папка: {}".format(restored_path.name)
                )
                self._set_unpack_preview_text(
                    "Восстановлена папка. Предпросмотр содержимого включается только для одиночного файла.\n\n"
                    "Путь: {}\nФайлов: {}\nПапок: {}".format(
                        restored_path, files_count, dirs_count
                    )
                )
                return

            self.unpack_preview_title_var.set("Результат распаковки")
            self._set_unpack_preview_text(
                "Путь результата не найден: {}".format(restored_path)
            )

        def _count_folder_entries(self, folder):
            files_count = 0
            dirs_count = 0
            try:
                for item in folder.rglob("*"):
                    if item.is_file():
                        files_count += 1
                    elif item.is_dir():
                        dirs_count += 1
            except Exception:
                pass
            return files_count, dirs_count

        def _copy_unpack_preview(self):
            text = self.unpack_preview_text_cache.strip()
            if not text:
                return
            if self._copy_text_to_clipboard(text):
                self._animate_button_copy(self.copy_unpacked_result_btn)
                self._animate_button_copy(self.copy_unpack_preview_btn)
            else:
                messagebox.showerror(
                    "Буфер недоступен", "Не удалось скопировать текст."
                )

        def _save_unpacked_file_as(self):
            if not self.last_unpacked_path or not self.last_unpacked_path.is_file():
                messagebox.showwarning(
                    "Нет файла", "Сначала распакуйте одиночный файл."
                )
                return
            path = filedialog.asksaveasfilename(
                title="Сохранить копию восстановленного файла",
                initialfile=self.last_unpacked_path.name,
                initialdir=str(self.last_unpacked_path.parent),
                filetypes=[("All files", "*.*")],
            )
            if path:
                shutil.copy2(self.last_unpacked_path, path)
                self._set_status("Копия сохранена: {}".format(path))

        def _open_unpacked_path(self):
            if not self.last_unpacked_path:
                messagebox.showwarning(
                    "Нет результата", "Сначала выполните распаковку."
                )
                return
            self._open_path(self.last_unpacked_path)

        @staticmethod
        def _format_bytes(size_bytes):
            value = float(size_bytes)
            for unit in ("B", "KB", "MB", "GB"):
                if abs(value) < 1024.0:
                    return "{:.1f} {}".format(value, unit)
                value /= 1024.0
            return "{:.1f} TB".format(value)

        def _run_background(self, status, work, done, silent=False):
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
                self.after(
                    0, lambda: self._finish_background(result, error, done, silent)
                )

            threading.Thread(target=runner, daemon=True).start()

        def _finish_background(self, result, error, done, silent=False):
            self.progress.stop()
            self.busy = False
            if error:
                self._set_status("Ошибка: {}".format(error))
                if not silent:
                    messagebox.showerror("Ошибка", str(error))
                return
            done(result)

        def _copy_text_to_clipboard(self, text):
            if len(text) > CLIPBOARD_MAX_CHARS:
                return False  # Too large for clipboard
            try:
                self.clipboard_clear()
                self.clipboard_append(text)
                self.update_idletasks()
                return True
            except tk.TclError:
                return bool(try_set_clipboard(text))

        def _copy_pack_preview(self):
            text = self.last_pack_token or self.pack_preview.get("1.0", "end").strip()
            if not text:
                return
            if len(self.last_pack_tokens) > 1:
                messagebox.showinfo(
                    "Токен разделён",
                    "Токен состоит из частей. Используйте кнопку «Копировать часть», "
                    "чтобы копировать части отдельно.",
                )
                return
            if self._copy_text_to_clipboard(text):
                self._animate_button_copy(self.copy_pack_preview_btn)
            else:
                messagebox.showerror(
                    "Буфер недоступен",
                    "Не удалось скопировать токен.{}".format(
                        " (токен слишком большой для буфера)"
                        if len(text) > CLIPBOARD_MAX_CHARS
                        else ""
                    ),
                )

        def _copy_selected_pack_part(self):
            if not self.last_pack_tokens:
                return
            selected = self.pack_part_var.get().split("/", 1)[0].strip()
            try:
                index = max(0, int(selected) - 1)
            except ValueError:
                index = 0
            if index >= len(self.last_pack_tokens):
                index = 0
            text = self.last_pack_tokens[index]
            if self._copy_text_to_clipboard(text):
                self._animate_button_copy(self.copy_pack_part_btn)
                self._set_status(
                    "Скопирована часть {} из {}".format(
                        index + 1, len(self.last_pack_tokens)
                    )
                )
            else:
                messagebox.showerror(
                    "Буфер недоступен",
                    "Не удалось скопировать часть.{}".format(
                        " (часть слишком большая для буфера)"
                        if len(text) > CLIPBOARD_MAX_CHARS
                        else ""
                    ),
                )

        def _save_pack_preview_as(self):
            text = self.last_pack_token or self.pack_preview.get("1.0", "end").strip()
            if not text:
                messagebox.showwarning(
                    "Нет токена", "Сначала упакуйте файл, папку или текст."
                )
                return
            path = filedialog.asksaveasfilename(
                title="Сохранить токен",
                defaultextension=".txt",
                filetypes=[
                    ("FT token", "*.ft.txt"),
                    ("Text files", "*.txt"),
                    ("All files", "*.*"),
                ],
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
                messagebox.showwarning(
                    "Буфер пуст", "В буфере обмена нет текста для упаковки."
                )
                return
            self.pack_source_var.set("")
            self.pack_text.delete("1.0", "end")
            self.pack_text.insert("1.0", text)
            if not self.pack_output_var.get().strip():
                text_file_name = self._get_pack_text_filename()
                self.pack_output_var.set(
                    str((Path.cwd() / "{}.ft.txt".format(text_file_name)).resolve())
                )
            self._set_status("Текст для упаковки вставлен из буфера")
            # Триггерим авто-режим после вставки
            self._on_pack_text_changed()

        def _clear_pack_text(self):
            self.pack_text.delete("1.0", "end")
            self.split_hint_var.set("")
            self._set_status("Текст для упаковки очищен")

        def _paste_unpack_clipboard(self):
            try:
                text = self.clipboard_get()
            except tk.TclError:
                text = try_get_clipboard() or ""
            if not text.strip():
                messagebox.showwarning(
                    "Буфер пуст", "В буфере обмена нет текста токена."
                )
                return
            self.unpack_input_var.set("")
            self.unpack_text.delete("1.0", "end")
            self.unpack_text.insert("1.0", text)
            self._clear_unpack_result_preview()
            self._set_status("Текст вставлен из буфера")
            # Триггерим авто-режим после вставки
            self._on_unpack_text_changed()

        def _clear_pack(self):
            self.pack_source_var.set("")
            self.pack_output_var.set("")
            self.pack_text_name_var.set("pasted_text.txt")
            self.pack_text.delete("1.0", "end")
            self.pack_preview.delete("1.0", "end")
            self.last_pack_token = ""
            self.last_pack_tokens = []
            self.pack_result_var.set("")
            self.split_hint_var.set("")
            self._update_pack_part_controls()
            self._set_status("Готово")

        def _clear_unpack_text(self):
            current_text = self.unpack_text.get("1.0", "end-1c")
            has_split_parts = False
            try:
                for token in extract_all_tokens_from_text(current_text):
                    meta, _ = decode_token(token)
                    if meta.get("split_total"):
                        has_split_parts = True
                        break
            except Exception:
                has_split_parts = False
            if has_split_parts:
                if not messagebox.askyesno(
                    "Прервать split-сессию?",
                    "В поле есть части split-токена. Очистка прервёт текущую split-сессию. Продолжить?",
                ):
                    return
            self.unpack_text.delete("1.0", "end")
            self._clear_unpack_result_preview()
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
    print(
        "  python ft.py web [порт]              # запустить HTML сервер (zero dependencies)"
    )
    print("  python ft.py pack <файл_или_папка> [выходной_txt] [опции]")
    print("  python ft.py unpack [входной_txt] [папка_назначения]")
    print("")
    print("Опции pack:")
    print("  --split N                разделить на N частей (принудительно)")
    print("  --split-threshold SIZE   порог авто-разделения (по умолч. 500k)")
    print("                           формат: 500k, 1m, 512000")
    print("")
    print("Примеры:")
    print("  python ft.py web 8080")
    print("  python ft.py pack ./project")
    print("  python ft.py pack ./big_file.zip --split 3")
    print("  python ft.py pack ./data --split-threshold 200k")
    print("  python ft.py unpack ./payload.txt ./out")


def main():
    if len(sys.argv) < 2:
        return launch_gui()

    cmd = sys.argv[1].lower()

    if cmd in ("gui", "--gui"):
        return launch_gui()

    if cmd in ("web", "server", "--web"):
        port = int(sys.argv[2]) if len(sys.argv) >= 3 else 5000
        return launch_web(port)

    if cmd in ("help", "-h", "--help"):
        print_usage()
        return 0

    if cmd == "pack":
        if len(sys.argv) < 3:
            print_usage()
            return 1
        source_path = sys.argv[2]
        # Parse optional positional out_file and named --split / --split-threshold
        out_file = None
        split_val = None
        threshold_val = None
        positional_args = []
        i = 3
        while i < len(sys.argv):
            arg = sys.argv[i]
            if arg == "--split" and i + 1 < len(sys.argv):
                split_val = int(sys.argv[i + 1])
                i += 2
            elif arg == "--split-threshold" and i + 1 < len(sys.argv):
                threshold_val = sys.argv[i + 1]
                i += 2
            elif arg.startswith("--split="):
                split_val = int(arg.split("=", 1)[1])
                i += 1
            elif arg.startswith("--split-threshold="):
                threshold_val = arg.split("=", 1)[1]
                i += 1
            else:
                positional_args.append(arg)
                i += 1
        if positional_args:
            out_file = positional_args[0]
        return pack_command(
            source_path, out_file, split=split_val, split_threshold=threshold_val
        )

    if cmd == "unpack":
        input_path = sys.argv[2] if len(sys.argv) >= 3 else None
        out_dir = sys.argv[3] if len(sys.argv) >= 4 else None
        return unpack_command(input_path, out_dir)

    print_usage()
    return 1


if __name__ == "__main__":
    sys.exit(main())
