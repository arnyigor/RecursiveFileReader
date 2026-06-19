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
        <title>FT Transfer — Web Interface</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f5f7; margin: 0; padding: 40px; color: #333; }
            .container { max-width: 700px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }
            h1 { font-size: 22px; border-bottom: 2px solid #0052cc; padding-bottom: 10px; margin-top: 0; }
            .section { margin-top: 30px; padding: 20px; background: #fafbfc; border: 1px solid #dfe1e6; border-radius: 6px; }
            h2 { font-size: 16px; margin-top: 0; color: #172b4d; }
            .form-group { margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px dashed #dfe1e6; }
            .form-group:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
            label { display: block; font-weight: bold; margin-bottom: 8px; font-size: 13px; color: #5e6c84; text-transform: uppercase;}
            input[type="file"], input[type="text"], textarea { width: 100%; padding: 8px; box-sizing: border-box; border: 2px solid #dfe1e6; border-radius: 4px; outline: none; transition: border 0.2s; background: #fff;}
            input[type="file"] { padding: 5px; }
            input[type="text"]:focus, textarea:focus { border-color: #0052cc; }
            button { background-color: #0052cc; color: white; border: none; padding: 10px 20px; font-size: 14px; border-radius: 4px; cursor: pointer; font-weight: bold; transition: background 0.2s; margin-top: 10px; width: 100%;}
            button:hover { background-color: #0047b3; }
            button:disabled { background-color: #a5b2c0; cursor: not-allowed; }
            .btn-secondary { background-color: #6b778c; width: auto; margin-top: 0;}
            .btn-secondary:hover { background-color: #505f79; }
            .text-muted { color: #6b778c; font-size: 12px; margin-top: 8px; line-height: 1.4; }
            .result-box { margin-top: 15px; padding: 15px; border-radius: 4px; display: none; }
            .result-box.success { background-color: #e3fcef; border: 1px solid #36b37e; color: #006644; }
            .result-box.error { background-color: #ffebe6; border: 1px solid #ff5630; color: #bf2600; }
            .flex-actions { display: flex; gap: 8px; margin-top: 8px;}
            .loader { display: none; text-align: center; font-size: 13px; color: #0052cc; font-weight: bold; margin-top: 10px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>FT Transfer</h1>
            
            <div style="margin: 15px 0; padding: 12px; background: #f0f4ff; border-radius: 6px; border: 1px solid #d0d9f0;">
                <label style="display: flex; align-items: center; gap: 8px; margin: 0; font-size: 13px; text-transform: none; color: #172b4d;">
                    <input type="checkbox" id="autoMode" checked> Автоматический режим: распознавание при вставке
                </label>
            </div>
            
            <div class="section">
                <h2>Упаковка (Pack)</h2>
                
                <div class="form-group">
                    <label>Вариант 1: Выбрать один файл</label>
                    <input type="file" id="packFile" onchange="clearOthers('packFile')">
                </div>
                
                <div class="form-group">
                    <label>Вариант 2: Выбрать папку целиком</label>
                    <input type="file" id="packFolder" webkitdirectory directory onchange="clearOthers('packFolder')">
                    <div class="text-muted">Для больших папок (сотни мегабайт) используйте консольную версию.</div>
                </div>
                
                <div class="form-group">
                    <label>Вариант 3: Вставить текст как .txt файл</label>
                    <input type="text" id="packTextName" placeholder="Имя файла (например: config.json)" style="margin-bottom: 8px;" oninput="clearOthers('packTextContent')">
                    <textarea id="packTextContent" rows="4" placeholder="Вставь исходный текст..." oninput="clearOthers('packTextContent')"></textarea>
                </div>

                <button id="btnPack" onclick="handlePack()">Сгенерировать токен</button>
                <div id="packLoader" class="loader">Сборка и упаковка данных, подождите...</div>
                <div id="packResult" class="result-box"></div>
            </div>

            <div class="section">
                <h2>Распаковка (Unpack)</h2>
                <div class="form-group">
                    <label>Вариант 1: Загрузить файл токена (.ft.txt)</label>
                    <input type="file" id="unpackFile" onchange="document.getElementById('unpackText').value = '';">
                </div>
                <div class="form-group">
                    <label>Вариант 2: Вставить сырой токен FTPKG1...</label>
                    <textarea id="unpackText" rows="5" placeholder="FTPKG1..." oninput="document.getElementById('unpackFile').value = '';"></textarea>
                </div>
                <button id="btnUnpack" onclick="handleUnpack()">Распаковать на сервере</button>
                <p class="text-muted">Распаковка происходит локально на стороне сервера в директорию <b>./restored</b>.</p>
                <div id="unpackLoader" class="loader">Распаковка данных, подождите...</div>
                <div id="unpackResult" class="result-box"></div>
            </div>
        </div>

        <script>
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
                el.className = `result-box ${type}`;
                el.innerHTML = message;
                el.style.display = 'block'; // Жесткий оверрайд inline-стиля
            }

            window.currentExportToken = '';
            window.currentExportFilename = '';

            window.copyTextElement = async function(btn, elementId) {
                const ta = document.getElementById(elementId);
                try {
                    await navigator.clipboard.writeText(ta.value);
                } catch (err) {
                    ta.select();
                    document.execCommand('copy');
                }
                const originalText = btn.innerText;
                btn.innerText = 'Скопировано!';
                btn.style.backgroundColor = '#36b37e';
                setTimeout(() => {
                    btn.innerText = originalText;
                    btn.style.backgroundColor = '#6b778c';
                }, 2000);
            };

            window.downloadOutToken = function() {
                const blob = new Blob([window.currentExportToken], { type: 'text/plain' });
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = window.currentExportFilename + '.ft.txt';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
            };

            async function handlePack() {
                const btn = document.getElementById('btnPack');
                const loader = document.getElementById('packLoader');
                const fileInput = document.getElementById('packFile');
                const folderInput = document.getElementById('packFolder');
                const textContent = document.getElementById('packTextContent').value.trim();
                const textName = document.getElementById('packTextName').value.trim();
                
                btn.disabled = true;
                loader.style.display = 'block';
                document.getElementById('packResult').style.display = 'none';

                try {
                    let payload = {};
                    
                    if (fileInput.files.length > 0) {
                        const file = fileInput.files[0];
                        const base64 = await new Promise((resolve, reject) => {
                            const reader = new FileReader();
                            reader.onload = e => {
                                const b64Part = e.target.result.split(',')[1];
                                resolve(b64Part ? b64Part : '');
                            };
                            reader.onerror = () => reject(new Error("Ошибка чтения файла"));
                            reader.readAsDataURL(file);
                        });
                        payload = { is_folder: false, is_text: false, filename: file.name, content_b64: base64 };
                    } 
                    else if (folderInput.files.length > 0) {
                        const files = folderInput.files;
                        const folderName = files[0].webkitRelativePath ? files[0].webkitRelativePath.split('/')[0] : 'packed_folder';
                        
                        const filePromises = Array.from(files).map(file => {
                            return new Promise((resolve, reject) => {
                                const reader = new FileReader();
                                reader.onload = e => {
                                    const b64Part = e.target.result.split(',')[1];
                                    resolve({
                                        path: file.webkitRelativePath || file.name,
                                        b64: b64Part ? b64Part : ''
                                    });
                                };
                                reader.onerror = () => reject(new Error(`Ошибка чтения ${file.name}`));
                                reader.readAsDataURL(file);
                            });
                        });
                        
                        const fileData = await Promise.all(filePromises);
                        payload = { is_folder: true, folder_name: folderName, files: fileData };
                    }
                    else if (textContent) {
                        payload = { is_folder: false, is_text: true, filename: textName || 'pasted_text.txt', text_content: textContent };
                    } 
                    else {
                        throw new Error('Выберите файл, папку или вставьте текст.');
                    }

                    const res = await fetch('/api/pack', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);

                    window.currentExportToken = data.token;
                    window.currentExportFilename = data.filename;

                    const successHtml = `
                        <div style="margin-bottom: 10px; font-weight: bold; color: #006644;">Токен успешно сгенерирован (${data.filename}):</div>
                        <textarea id="outTokenArea" rows="6" readonly style="width: 100%; margin-bottom: 10px; font-family: monospace; cursor: text;"></textarea>
                        <div class="flex-actions">
                            <button class="btn-secondary" onclick="copyTextElement(this, 'outTokenArea')">Скопировать токен</button>
                            <button class="btn-secondary" onclick="downloadOutToken()">Скачать .ft.txt</button>
                        </div>
                    `;
                    showResult('packResult', 'success', successHtml);
                    document.getElementById('outTokenArea').value = data.token;
                } catch (err) {
                    showResult('packResult', 'error', err.message);
                } finally {
                    btn.disabled = false;
                    loader.style.display = 'none';
                }
            }

            async function handleUnpack() {
                const btn = document.getElementById('btnUnpack');
                const loader = document.getElementById('unpackLoader');
                const fileInput = document.getElementById('unpackFile');
                const tokenText = document.getElementById('unpackText').value.trim();
                
                btn.disabled = true;
                loader.style.display = 'block';
                document.getElementById('unpackResult').style.display = 'none';

                try {
                    let tokenPayload = "";

                    if (fileInput.files.length > 0) {
                        const file = fileInput.files[0];
                        tokenPayload = await new Promise((resolve, reject) => {
                            const reader = new FileReader();
                            reader.onload = e => resolve(e.target.result);
                            reader.onerror = () => reject(new Error("Ошибка чтения токена"));
                            reader.readAsText(file);
                        });
                    } else if (tokenText) {
                        tokenPayload = tokenText;
                    } else {
                        throw new Error('Выберите файл токена или вставьте текст.');
                    }

                    const res = await fetch('/api/unpack', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ token: tokenPayload })
                    });
                    
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);
                    
                    let resultHtml = `Данные успешно восстановлены по пути:<br><b style="color:#172b4d;">${data.path}</b>`;
                    
                    if (data.is_text && data.text_content !== null) {
                        const truncationNotice = data.text_truncated ? '<div class="text-muted">Предпросмотр обрезан. Полный файл сохранён на диск.</div>' : '';
                        resultHtml += `
                            <div style="margin-top: 15px; font-weight: bold; color: #006644;">Содержимое файла:</div>
                            ${truncationNotice}
                            <textarea id="unpackedContentArea" rows="10" readonly style="width: 100%; margin-top: 8px; margin-bottom: 10px; font-family: monospace; cursor: text;"></textarea>
                            <div>
                                <button class="btn-secondary" onclick="copyTextElement(this, 'unpackedContentArea')">Скопировать текст</button>
                            </div>
                        `;
                        showResult('unpackResult', 'success', resultHtml);
                        document.getElementById('unpackedContentArea').value = data.text_content;
                    } else {
                        showResult('unpackResult', 'success', resultHtml);
                    }

                } catch (err) {
                    showResult('unpackResult', 'error', err.message);
                } finally {
                    btn.disabled = false;
                    loader.style.display = 'none';
                }
            }

            // ===== AUTO-MODE: debounce + auto-pack + auto-unpack =====
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
                    const res = await fetch('/api/pack', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    const data = await res.json();
                    if (data.error) throw new Error(data.error);
                    window.currentExportToken = data.token;
                    window.currentExportFilename = data.filename;
                    const html = `
                        <div style="margin-bottom: 10px; font-weight: bold; color: #006644;">Токен (${data.filename}):</div>
                        <textarea id="outTokenArea" rows="6" readonly style="width: 100%; margin-bottom: 10px; font-family: monospace; cursor: text;"></textarea>
                        <div class="flex-actions">
                            <button class="btn-secondary" onclick="copyTextElement(this, 'outTokenArea')">Скопировать токен</button>
                            <button class="btn-secondary" onclick="downloadOutToken()">Скачать .ft.txt</button>
                        </div>
                    `;
                    showResult('packResult', 'success', html);
                    document.getElementById('outTokenArea').value = data.token;
                } catch (err) {
                    // Auto: silently ignore errors
                } finally {
                    loader.style.display = 'none';
                }
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
                    const res = await fetch('/api/unpack', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ token: tokenText })
                    });
                    const data = await res.json();
                    if (data.error) return;
                    let resultHtml = `Данные восстановлены:<br><b style="color:#172b4d;">${data.path}</b>`;
                    if (data.is_text && data.text_content !== null) {
                        const trunc = data.text_truncated ? '<div class="text-muted">Предпросмотр обрезан.</div>' : '';
                        resultHtml += `
                            <div style="margin-top: 15px; font-weight: bold; color: #006644;">Содержимое:</div>
                            ${trunc}
                            <textarea id="unpackedContentArea" rows="10" readonly style="width: 100%; margin-top: 8px; margin-bottom: 10px; font-family: monospace; cursor: text;"></textarea>
                            <div>
                                <button class="btn-secondary" onclick="copyTextElement(this, 'unpackedContentArea')">Скопировать текст</button>
                            </div>
                        `;
                        showResult('unpackResult', 'success', resultHtml);
                        document.getElementById('unpackedContentArea').value = data.text_content;
                    } else {
                        showResult('unpackResult', 'success', resultHtml);
                    }
                } catch (err) {
                    // Auto: silently ignore errors
                } finally {
                    loader.style.display = 'none';
                }
            }

            document.getElementById('packTextContent').addEventListener('input', function() {
                _debounce('_packAuto', 800, autoPack);
            });
            document.getElementById('unpackText').addEventListener('input', function() {
                _debounce('_unpackAuto', 800, autoUnpack);
            });
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
