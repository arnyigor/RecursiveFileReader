#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import base64
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
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


def print_usage():
    print("Использование:")
    print("  python ft.py pack <файл_или_папка> [выходной_txt]")
    print("  python ft.py unpack [входной_txt] [папка_назначения]")
    print("")
    print("Примеры:")
    print("  python ft.py pack ./project")
    print("  python ft.py pack ./project ./payload.txt")
    print("  python ft.py unpack")
    print("  python ft.py unpack ./payload.txt")
    print("  python ft.py unpack ./payload.txt ./out")
    print("")
    print("Если буфер недоступен, для unpack можно вставить текст прямо в терминал.")


def main():
    if len(sys.argv) < 2:
        print_usage()
        return 1

    cmd = sys.argv[1].lower()

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
