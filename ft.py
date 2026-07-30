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
TEXT_PREVIEW_MAX_BYTES = 512 * 1024


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
    if re.match(r"^\d{6}-", compact):
        compact = compact[7:]
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
                ["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE, text=True
            )
            proc.communicate(text)
            if proc.returncode == 0:
                return "linux:xclip"

        if shutil.which("xsel"):
            proc = subprocess.Popen(
                ["xsel", "--clipboard", "--input"], stdin=subprocess.PIPE, text=True
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
                stderr=subprocess.DEVNULL,
            )
            if result.strip():
                return result

        if shutil.which("wl-paste"):
            result = subprocess.check_output(
                ["wl-paste", "-n"], text=True, stderr=subprocess.DEVNULL
            )
            if result.strip():
                return result

        if shutil.which("xclip"):
            result = subprocess.check_output(
                ["xclip", "-selection", "clipboard", "-o"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            if result.strip():
                return result

        if shutil.which("xsel"):
            result = subprocess.check_output(
                ["xsel", "--clipboard", "--output"],
                text=True,
                stderr=subprocess.DEVNULL,
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

            <div class="tabs">
                <button class="tab-btn active" onclick="switchTab('pack')">Упаковать</button>
                <button class="tab-btn" onclick="switchTab('unpack')">Распаковать</button>
            </div>
            <div class="card">

                <!-- ==================== PACK TAB ==================== -->
                <div id="tab-pack" class="tab-content active">
                    <div class="field">
                        <span class="field-label">Файл</span>
                        <input type="file" id="packFile" onchange="clearOthers('packFile')">
                    </div>

                    <div class="field">
                        <span class="field-label">Папка</span>
                        <input type="file" id="packFolder" webkitdirectory directory onchange="clearOthers('packFolder')">
                        <div class="field-hint">Для больших папок (100+ MB) используйте CLI.</div>
                    </div>

                    <hr class="divider">

                    <div class="field">
                        <span class="field-label">Или текст</span>
                        <div class="paste-row">
                            <input type="text" id="packTextName" placeholder="Имя файла (config.json)" style="flex:1;" oninput="clearOthers('packTextContent')">
                            <button class="btn-paste" onclick="pasteClipboard('packTextContent', this)" title="Вставить из буфера">&#128203; Вставить</button>
                        </div>
                        <textarea id="packTextContent" rows="5" placeholder="Вставьте исходный текст..." oninput="clearOthers('packTextContent')"></textarea>
                    </div>

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
                    document.getElementById(targetId).value = text;
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
                    document.getElementById('packTextName').value = '';
                }
            }

            function showResult(elementId, type, message) {
                const el = document.getElementById(elementId);
                el.className = 'result-box ' + type;
                el.innerHTML = message;
                el.style.display = 'block';
            }

            /* ===== Copy / Download ===== */
            window.currentExportToken = '';
            window.currentExportFilename = '';

            window.copyTextElement = async function(btn, elementId) {
                const ta = document.getElementById(elementId);
                try { await navigator.clipboard.writeText(ta.value); }
                catch (err) { ta.select(); document.execCommand('copy'); }
                const orig = btn.innerText;
                btn.innerText = 'Скопировано!';
                btn.style.background = '#dcfce7'; btn.style.color = '#166534'; btn.style.borderColor = '#86efac';
                setTimeout(() => { btn.innerText = orig; btn.style.background = ''; btn.style.color = ''; btn.style.borderColor = ''; }, 1500);
            };

            window.downloadOutToken = function() {
                const blob = new Blob([window.currentExportToken], { type: 'text/plain' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url; a.download = window.currentExportFilename + '.ft.txt';
                document.body.appendChild(a); a.click(); document.body.removeChild(a);
                URL.revokeObjectURL(url);
            };

            /* ===== Pack ===== */
            async function handlePack() {
                const btn = document.getElementById('btnPack');
                const loader = document.getElementById('packLoader');
                const fileInput = document.getElementById('packFile');
                const folderInput = document.getElementById('packFolder');
                const textContent = document.getElementById('packTextContent').value.trim();
                const textName = document.getElementById('packTextName').value.trim();

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
                        payload = { is_folder: false, is_text: true, filename: textName || 'pasted_text.txt', text_content: textContent };
                    }
                    else { throw new Error('Выберите файл, папку или вставьте текст.'); }

                    const res = await fetch('/api/pack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);

                    window.currentExportToken = data.token;
                    window.currentExportFilename = data.filename;

                    showResult('packResult', 'success',
                        '<div style="margin-bottom:10px;font-weight:600;">Токен (' + data.filename + '):</div>' +
                        '<textarea id="outTokenArea" class="token-display" rows="6" readonly></textarea>' +
                        '<div class="flex-actions">' +
                            '<button class="btn-ghost" onclick="copyTextElement(this, \'outTokenArea\')">Копировать</button>' +
                            '<button class="btn-ghost" onclick="downloadOutToken()">Скачать .ft.txt</button>' +
                        '</div>'
                    );
                    document.getElementById('outTokenArea').value = data.token;
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

                    const res = await fetch('/api/unpack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: tokenPayload }) });
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);

                    let html = 'Восстановлено: <b>' + data.path + '</b>';
                    if (data.is_text && data.text_content !== null) {
                        if (data.text_truncated) html += '<div class="field-hint">Предпросмотр обрезан. Полный файл на диске.</div>';
                        html += '<textarea id="unpackedContentArea" class="token-display" rows="10" readonly style="margin-top:10px;"></textarea>';
                        html += '<div class="flex-actions"><button class="btn-ghost" onclick="copyTextElement(this, \'unpackedContentArea\')">Копировать текст</button></div>';
                        showResult('unpackResult', 'success', html);
                        document.getElementById('unpackedContentArea').value = data.text_content;
                    } else {
                        showResult('unpackResult', 'success', html);
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

                const textName = document.getElementById('packTextName').value.trim() || 'pasted_text.txt';
                const loader = document.getElementById('packLoader');
                loader.style.display = 'block';
                try {
                    const payload = { is_folder: false, is_text: true, filename: textName, text_content: textContent };
                    const res = await fetch('/api/pack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);
                    window.currentExportToken = data.token;
                    window.currentExportFilename = data.filename;
                    showResult('packResult', 'success',
                        '<div style="margin-bottom:10px;font-weight:600;">Токен (' + data.filename + '):</div>' +
                        '<textarea id="outTokenArea" class="token-display" rows="6" readonly></textarea>' +
                        '<div class="flex-actions">' +
                            '<button class="btn-ghost" onclick="copyTextElement(this, \'outTokenArea\')">Копировать</button>' +
                            '<button class="btn-ghost" onclick="downloadOutToken()">Скачать .ft.txt</button>' +
                        '</div>'
                    );
                    document.getElementById('outTokenArea').value = data.token;
                } catch (err) { /* silent */ } finally { loader.style.display = 'none'; }
            }

            async function autoUnpack() {
                if (!document.getElementById('autoMode').checked) return;
                const tokenText = document.getElementById('unpackText').value.trim();
                if (!tokenText) return;
                if (!tokenText.includes('FTPKG1.')) return;
                if (document.getElementById('unpackFile').files.length > 0) return;

                const loader = document.getElementById('unpackLoader');
                loader.style.display = 'block';
                try {
                    const res = await fetch('/api/unpack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: tokenText }) });
                    const data = await res.json();
                    if (data.error) return;
                    let html = 'Восстановлено: <b>' + data.path + '</b>';
                    if (data.is_text && data.text_content !== null) {
                        if (data.text_truncated) html += '<div class="field-hint">Предпросмотр обрезан.</div>';
                        html += '<textarea id="unpackedContentArea" class="token-display" rows="10" readonly style="margin-top:10px;"></textarea>';
                        html += '<div class="flex-actions"><button class="btn-ghost" onclick="copyTextElement(this, \'unpackedContentArea\')">Копировать текст</button></div>';
                        showResult('unpackResult', 'success', html);
                        document.getElementById('unpackedContentArea').value = data.text_content;
                    } else {
                        showResult('unpackResult', 'success', html);
                    }
                } catch (err) { /* silent */ } finally { loader.style.display = 'none'; }
            }

            document.getElementById('packTextContent').addEventListener('input', function() { _debounce('_packAuto', 800, autoPack); });
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
            if self.path not in ["/api/pack", "/api/unpack"]:
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

                        token = build_token(source_path)

                    self._send_json({"token": token, "filename": source_path.name})
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 500)

            elif self.path == "/api/unpack":
                raw_text = data.get("token", "")
                if not raw_text.strip():
                    return self._send_json({"error": "Токен пуст"}, 400)

                try:
                    token = extract_token_from_text(raw_text)
                    meta, archive_bytes = decode_token(token)
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
                preview_box, wrap="word", height=4, undo=True
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

        # -----------------------------------------------------------------
        # Автоупаковка
        # -----------------------------------------------------------------
        def _on_pack_text_changed(self, event=None):
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
                self.pack_preview.delete("1.0", "end")
                self.pack_preview.insert("1.0", token)
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
                        token = build_token(source)
                    if output_path:
                        save_text_file(output_path, token)
                    return (
                        token,
                        output_path,
                        "вставленный текст ({})".format(text_file_name),
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
                    token = build_token(source)
                    if output_path:
                        save_text_file(output_path, token)
                    return token, output_path, str(source)

            def done(result):
                token, saved_path, source_desc = result
                self.pack_preview.delete("1.0", "end")
                self.pack_preview.insert("1.0", token)
                # Автокопирование токена в буфер обмена + анимация кнопки
                if self._copy_text_to_clipboard(token):
                    self._animate_button_copy(self.copy_pack_preview_btn)
                parts = [
                    "Упаковка завершена",
                    "{} символов".format(len(token)),
                    "источник: {}".format(source_desc),
                ]
                if saved_path:
                    parts.append("файл: {}".format(saved_path))
                self._set_status("; ".join(parts))

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
                self._show_unpack_result(restored_path, source_desc, meta)

            self._clear_unpack_result_preview()
            self._run_background("Распаковка...", work, done)

        def _set_unpack_preview_text(self, text):
            self.unpack_preview.configure(state="normal")
            self.unpack_preview.delete("1.0", "end")
            if text:
                self.unpack_preview.insert("1.0", text)
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
                return
            if self._copy_text_to_clipboard(text):
                self._animate_button_copy(self.copy_pack_preview_btn)
            else:
                messagebox.showerror(
                    "Буфер недоступен", "Не удалось скопировать токен."
                )

        def _save_pack_preview_as(self):
            text = self.pack_preview.get("1.0", "end").strip()
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
            self._set_status("Готово")

        def _clear_unpack_text(self):
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
    print("  python ft.py pack <файл_или_папка> [выходной_txt]")
    print("  python ft.py unpack [входной_txt] [папка_назначения]")
    print("")
    print("Примеры:")
    print("  python ft.py web 8080")
    print("  python ft.py pack ./project")
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
