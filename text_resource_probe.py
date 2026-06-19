#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
text_resource_probe.py

Проверяет доступность публичных ресурсов для передачи большого текста (~10К+ символов)
и, при необходимости, делает тестовую публикацию БЕЗ внешних зависимостей.

Важно: тестовая публикация создает публичные ссылки на сторонних сервисах.
По умолчанию отправляется сгенерированный безобидный текст. Не отправляй пароли,
токены, персональные данные и закрытый код в публичные paste/file-сервисы.
"""

from __future__ import annotations

import argparse
import dataclasses
import http.cookiejar
import json
import os
import random
import socket
import string
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable, Dict, Iterable, List, Optional, Tuple

USER_AGENT = "text-resource-probe/1.0 (+local availability checker)"


@dataclasses.dataclass
class ProbeResult:
    name: str
    kind: str
    dns: str = "-"
    tcp: str = "-"
    http: str = "-"
    upload: str = "-"
    status: str = "UNKNOWN"
    detail: str = ""
    url: str = ""
    ms: int = 0


def now_ms() -> int:
    return int(time.time() * 1000)


def short(s: str, limit: int = 180) -> str:
    s = (s or "").replace("\r", " ").replace("\n", " ").strip()
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def ok_or_fail(value: bool, ok: str = "OK", fail: str = "FAIL") -> str:
    return ok if value else fail


def make_test_text(chars: int) -> str:
    marker = f"TEXT_TRANSFER_PROBE_{uuid.uuid4().hex}_"
    chunk = "Проверка передачи большого текста одной строкой 0123456789 ABC abc | "
    text = marker + (chunk * ((chars // len(chunk)) + 2))
    return text[:chars]


def load_text(path: Optional[str], chars: int) -> str:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        return data
    return make_test_text(chars)


def parse_host_port(url: str) -> Tuple[str, int]:
    p = urllib.parse.urlparse(url)
    if not p.hostname:
        raise ValueError(f"URL without host: {url}")
    if p.port:
        port = p.port
    elif p.scheme == "https":
        port = 443
    elif p.scheme == "http":
        port = 80
    else:
        port = 443
    return p.hostname, port


def probe_dns(host: str, port: int, timeout: float) -> Tuple[bool, str]:
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addrs = sorted({i[4][0] for i in infos})
        return True, ",".join(addrs[:3]) + ("…" if len(addrs) > 3 else "")
    except Exception as e:
        return False, type(e).__name__ + ": " + str(e)
    finally:
        socket.setdefaulttimeout(old_timeout)


def probe_tcp(host: str, port: int, timeout: float) -> Tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port}"
    except Exception as e:
        return False, type(e).__name__ + ": " + str(e)


def http_request(
    url: str,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> Tuple[int, Dict[str, str], bytes]:
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    op = opener or urllib.request.build_opener()
    try:
        with op.open(req, timeout=timeout) as resp:
            body = resp.read(1024 * 1024)
            return resp.getcode(), dict(resp.headers.items()), body
    except urllib.error.HTTPError as e:
        body = e.read(1024 * 1024)
        return e.code, dict(e.headers.items()), body


def basic_http_probe(url: str, timeout: float) -> Tuple[str, str, str]:
    host, port = parse_host_port(url)
    dns_ok, dns_detail = probe_dns(host, port, timeout)
    tcp_ok, tcp_detail = probe_tcp(host, port, timeout) if dns_ok else (False, "DNS failed")
    http_detail = "-"
    if tcp_ok:
        try:
            code, headers, body = http_request(url, method="GET", timeout=timeout)
            http_detail = f"HTTP {code}"
        except Exception as e:
            http_detail = type(e).__name__ + ": " + str(e)
    return ok_or_fail(dns_ok), ok_or_fail(tcp_ok), http_detail


def encode_form(fields: Dict[str, object]) -> bytes:
    return urllib.parse.urlencode(fields, doseq=True).encode("utf-8")


def encode_multipart(
    fields: Dict[str, object],
    files: List[Tuple[str, str, str, bytes]],
) -> Tuple[bytes, str]:
    boundary = "----PythonTextProbe" + uuid.uuid4().hex
    parts: List[bytes] = []

    def add(line: str) -> None:
        parts.append(line.encode("utf-8"))

    for name, value in fields.items():
        add(f"--{boundary}\r\n")
        add(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        add(str(value))
        add("\r\n")

    for field_name, filename, content_type, content in files:
        add(f"--{boundary}\r\n")
        safe_name = filename.replace('"', "")
        add(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_name}"\r\n'
        )
        add(f"Content-Type: {content_type}\r\n\r\n")
        parts.append(content)
        add("\r\n")

    add(f"--{boundary}--\r\n")
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def looks_like_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def probe_paste_rs(text: str, timeout: float) -> Tuple[str, str]:
    data = text.encode("utf-8")
    code, headers, body = http_request(
        "https://paste.rs/",
        method="POST",
        data=data,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        timeout=timeout,
    )
    response = body.decode("utf-8", "replace").strip()
    if code == 201 and looks_like_url(response):
        return "OK", response
    if code == 206:
        return "PARTIAL", "сервер принял только часть текста: " + short(response)
    return "FAIL", f"HTTP {code}: {short(response)}"


def probe_dpaste(text: str, timeout: float) -> Tuple[str, str]:
    data = encode_form({"content": text, "format": "url"})
    code, headers, body = http_request(
        "https://dpaste.org/api/",
        method="POST",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        timeout=timeout,
    )
    response = body.decode("utf-8", "replace").strip().strip('"')
    if code in (200, 201) and looks_like_url(response):
        return "OK", response
    return "FAIL", f"HTTP {code}: {short(response)}"


def probe_rentry(text: str, timeout: float) -> Tuple[str, str]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    code, headers, body = http_request("https://rentry.co/", timeout=timeout, opener=opener)
    if code >= 400:
        return "FAIL", f"homepage HTTP {code}"

    csrf = None
    for c in jar:
        if c.name == "csrftoken":
            csrf = c.value
            break
    if not csrf:
        return "FAIL", "csrftoken cookie not found"

    edit_code = "probe-" + uuid.uuid4().hex[:12]
    payload = {
        "csrfmiddlewaretoken": csrf,
        "url": "",
        "edit_code": edit_code,
        "text": text,
    }
    code, headers, body = http_request(
        "https://rentry.co/api/new",
        method="POST",
        data=encode_form(payload),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Referer": "https://rentry.co/",
        },
        timeout=timeout,
        opener=opener,
    )
    response = body.decode("utf-8", "replace")
    try:
        obj = json.loads(response)
    except Exception:
        return "FAIL", f"HTTP {code}: {short(response)}"

    if code in (200, 201) and obj.get("status") == "200":
        url = obj.get("url") or obj.get("content") or ""
        return "OK", str(url)
    if code in (200, 201) and obj.get("url"):
        return "OK", str(obj.get("url"))
    return "FAIL", f"HTTP {code}: {short(response)}"


def probe_0x0(text: str, timeout: float) -> Tuple[str, str]:
    body, ctype = encode_multipart(
        fields={"expires": "1", "secret": ""},
        files=[("file", "text_probe.txt", "text/plain; charset=utf-8", text.encode("utf-8"))],
    )
    code, headers, resp = http_request(
        "https://0x0.st",
        method="POST",
        data=body,
        headers={"Content-Type": ctype},
        timeout=timeout,
    )
    response = resp.decode("utf-8", "replace").strip()
    if code in (200, 201) and looks_like_url(response):
        return "OK", response
    return "FAIL", f"HTTP {code}: {short(response)}"


def probe_file_io(text: str, timeout: float) -> Tuple[str, str]:
    body, ctype = encode_multipart(
        fields={"expires": "1d", "maxDownloads": "1", "autoDelete": "true"},
        files=[("file", "text_probe.txt", "text/plain; charset=utf-8", text.encode("utf-8"))],
    )
    code, headers, resp = http_request(
        "https://file.io/",
        method="POST",
        data=body,
        headers={"Content-Type": ctype, "Accept": "application/json"},
        timeout=timeout,
    )
    response = resp.decode("utf-8", "replace")
    try:
        obj = json.loads(response)
        link = obj.get("link") or obj.get("url") or ""
        success = bool(obj.get("success", code in (200, 201)))
        if code in (200, 201) and success and link:
            return "OK", str(link)
    except Exception:
        pass
    return "FAIL", f"HTTP {code}: {short(response)}"


def probe_transfer_sh(text: str, timeout: float) -> Tuple[str, str]:
    filename = "text_probe_" + uuid.uuid4().hex[:10] + ".txt"
    url = "https://transfer.sh/" + filename
    code, headers, resp = http_request(
        url,
        method="PUT",
        data=text.encode("utf-8"),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Max-Days": "1",
            "Max-Downloads": "1",
        },
        timeout=timeout,
    )
    response = resp.decode("utf-8", "replace").strip()
    if code in (200, 201) and looks_like_url(response):
        return "OK", response
    return "FAIL", f"HTTP {code}: {short(response)}"


def probe_termbin(text: str, timeout: float) -> Tuple[str, str]:
    host, port = "termbin.com", 9999
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(text.encode("utf-8") + b"\n")
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            chunks = []
            while True:
                b = s.recv(4096)
                if not b:
                    break
                chunks.append(b)
                if sum(len(x) for x in chunks) > 8192:
                    break
        response = b"".join(chunks).decode("utf-8", "replace").strip()
        if looks_like_url(response):
            return "OK", response
        return "FAIL", short(response)
    except Exception as e:
        return "FAIL", type(e).__name__ + ": " + str(e)


def probe_telegraph(text: str, timeout: float) -> Tuple[str, str]:
    # Telegra.ph API accepts JSON-encoded NodeElement array in the 'content' field.
    account_payload = encode_form({
        "short_name": "text_probe",
        "author_name": "text_resource_probe",
    })
    code, headers, resp = http_request(
        "https://api.telegra.ph/createAccount",
        method="POST",
        data=account_payload,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        timeout=timeout,
    )
    response = resp.decode("utf-8", "replace")
    try:
        obj = json.loads(response)
        token = obj.get("result", {}).get("access_token")
    except Exception:
        token = None
    if code not in (200, 201) or not token:
        return "FAIL", f"createAccount HTTP {code}: {short(response)}"

    content_nodes = [{"tag": "pre", "children": [text]}]
    page_payload = encode_form({
        "access_token": token,
        "title": "Text transfer probe",
        "content": json.dumps(content_nodes, ensure_ascii=False),
        "return_content": "false",
    })
    code, headers, resp = http_request(
        "https://api.telegra.ph/createPage",
        method="POST",
        data=page_payload,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        timeout=timeout,
    )
    response = resp.decode("utf-8", "replace")
    try:
        obj = json.loads(response)
        result = obj.get("result", {})
        url = result.get("url") or ("https://telegra.ph/" + result.get("path", "") if result.get("path") else "")
        if code in (200, 201) and obj.get("ok") and url:
            return "OK", url
    except Exception:
        pass
    return "FAIL", f"createPage HTTP {code}: {short(response)}"


def probe_github_gist(text: str, timeout: float) -> Tuple[str, str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return "SKIP_AUTH", "нужен GITHUB_TOKEN/GH_TOKEN с правом gist"
    payload = {
        "description": "text_resource_probe temporary gist",
        "public": False,
        "files": {"text_probe.txt": {"content": text}},
    }
    code, headers, resp = http_request(
        "https://api.github.com/gists",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=timeout,
    )
    response = resp.decode("utf-8", "replace")
    try:
        obj = json.loads(response)
        url = obj.get("html_url") or obj.get("url") or ""
        if code == 201 and url:
            return "OK", url
    except Exception:
        pass
    return "FAIL", f"HTTP {code}: {short(response)}"


@dataclasses.dataclass
class Resource:
    name: str
    kind: str
    base_url: str
    uploader: Optional[Callable[[str, float], Tuple[str, str]]] = None
    note: str = ""
    tcp_host: Optional[str] = None
    tcp_port: Optional[int] = None


RESOURCES: List[Resource] = [
    Resource("paste.rs", "paste/http-raw-post", "https://paste.rs/", probe_paste_rs, "сырой POST, возвращает URL"),
    Resource("dpaste.org", "paste/http-form", "https://dpaste.org/api/", probe_dpaste, "POST form content+format=url"),
    Resource("rentry.co", "markdown-paste/http-form+csrf", "https://rentry.co/", probe_rentry, "нужен CSRF-cookie"),
    Resource("telegra.ph", "publishing/api", "https://api.telegra.ph/", probe_telegraph, "создает страницу через Telegraph API"),
    Resource("0x0.st", "file-host/multipart", "https://0x0.st", probe_0x0, "временный публичный file host"),
    Resource("file.io", "file-host/multipart", "https://file.io/", probe_file_io, "одноразовая/временная ссылка"),
    Resource("transfer.sh", "file-host/http-put", "https://transfer.sh/", probe_transfer_sh, "PUT upload-file стиль"),
    Resource("termbin.com", "paste/tcp-9999", "https://termbin.com/", probe_termbin, "raw TCP, аналог netcat", "termbin.com", 9999),
    Resource("github-gist", "gist/api-token", "https://api.github.com/", probe_github_gist, "опционально, нужен GITHUB_TOKEN/GH_TOKEN"),
    Resource("figma.com", "manual-ui/reachability-only", "https://www.figma.com/", None, "только проверка сайта; не paste API"),
]


def run_resource(res: Resource, text: str, timeout: float, upload: bool, debug: bool) -> ProbeResult:
    started = now_ms()
    result = ProbeResult(name=res.name, kind=res.kind)
    try:
        if res.tcp_host and res.tcp_port:
            dns_ok, dns_detail = probe_dns(res.tcp_host, res.tcp_port, timeout)
            result.dns = ok_or_fail(dns_ok)
            tcp_ok, tcp_detail = probe_tcp(res.tcp_host, res.tcp_port, timeout) if dns_ok else (False, "DNS failed")
            result.tcp = ok_or_fail(tcp_ok)
            # Для termbin отдельно также проверяем HTTPS-страницу описания.
            try:
                code, headers, body = http_request(res.base_url, timeout=timeout)
                result.http = f"HTTP {code}"
            except Exception as e:
                result.http = type(e).__name__ + ": " + str(e)
            if not tcp_ok:
                result.status = "FAIL"
                result.detail = tcp_detail
                return result
        else:
            result.dns, result.tcp, result.http = basic_http_probe(res.base_url, timeout)
            if result.dns != "OK" or result.tcp != "OK":
                result.status = "FAIL"
                result.detail = result.http
                return result

        if not upload or not res.uploader:
            result.upload = "SKIP"
            result.status = "REACHABLE"
            result.detail = res.note
            return result

        upload_status, detail = res.uploader(text, timeout)
        result.upload = upload_status
        if upload_status == "OK":
            result.status = "OK"
            result.url = detail
            result.detail = res.note
        elif upload_status in ("SKIP_AUTH", "PARTIAL"):
            result.status = upload_status
            result.detail = detail
        else:
            result.status = "FAIL"
            result.detail = detail
        return result
    except Exception as e:
        result.status = "ERROR"
        result.detail = type(e).__name__ + ": " + str(e)
        if debug:
            result.detail += " | " + traceback.format_exc().replace("\n", " | ")
        return result
    finally:
        result.ms = now_ms() - started


def print_table(rows: List[ProbeResult]) -> None:
    headers = ["name", "status", "dns", "tcp", "http", "upload", "ms", "detail/url"]
    data = []
    for r in rows:
        detail = r.url if r.url else r.detail
        data.append([r.name, r.status, r.dns, r.tcp, r.http, r.upload, str(r.ms), short(detail, 110)])
    widths = [len(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: Iterable[str]) -> str:
        return "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))

    print(fmt(headers))
    print(fmt(["-" * w for w in widths]))
    for row in data:
        print(fmt(row))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Проверка доступности сервисов для передачи большого текста без внешних зависимостей."
    )
    parser.add_argument("--size", type=int, default=10_000, help="размер тестового текста в символах, если не указан --text-file")
    parser.add_argument("--text-file", help="файл с текстом для реальной тестовой отправки; осторожно, загрузки публичные")
    parser.add_argument("--timeout", type=float, default=12.0, help="таймаут на сетевую операцию, секунд")
    parser.add_argument("--no-upload", action="store_true", help="только DNS/TCP/HTTP, без создания публичных тестовых ссылок")
    parser.add_argument("--only", help="проверить только перечисленные ресурсы через запятую, например: paste.rs,rentry.co")
    parser.add_argument("--skip", help="пропустить перечисленные ресурсы через запятую")
    parser.add_argument("--json", dest="json_path", help="сохранить полный результат в JSON-файл")
    parser.add_argument("--debug", action="store_true", help="показывать traceback в detail при ошибках")
    args = parser.parse_args(argv)

    upload = not args.no_upload
    text = load_text(args.text_file, args.size)
    text_bytes = len(text.encode("utf-8"))

    only = {x.strip() for x in args.only.split(",")} if args.only else None
    skip = {x.strip() for x in args.skip.split(",")} if args.skip else set()

    selected = []
    for res in RESOURCES:
        if only is not None and res.name not in only:
            continue
        if res.name in skip:
            continue
        selected.append(res)

    print(f"Тестовый текст: {len(text)} символов, {text_bytes} байт UTF-8")
    if upload:
        print("Режим: DNS/TCP/HTTP + тестовая публичная загрузка. Для безопасной проверки используй --no-upload")
    else:
        print("Режим: только доступность DNS/TCP/HTTP, без публикации текста")
    print()

    results = []
    for res in selected:
        results.append(run_resource(res, text, args.timeout, upload, args.debug))

    print_table(results)

    if args.json_path:
        payload = [dataclasses.asdict(r) for r in results]
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nJSON сохранен: {args.json_path}")

    ok_count = sum(1 for r in results if r.status == "OK")
    reachable_count = sum(1 for r in results if r.status in ("OK", "REACHABLE", "SKIP_AUTH", "PARTIAL"))
    print(f"\nИтог: OK upload={ok_count}, reachable/usable-ish={reachable_count}, total={len(results)}")
    return 0 if reachable_count > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
