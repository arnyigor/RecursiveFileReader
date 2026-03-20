#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
file2text.py – Encode/decode binary files to text with compression statistics.
--------------------------------------------------------------------
Улучшения:
- Показывает размер оригинала, сжатого и текстового представления
- Коэффициент сжатия
- Скорость обработки
- Проверка целостности с прогрессом
"""

import lzma
import base64
import hashlib
import argparse
import os
import time
from typing import Optional

COMPRESSION_PRESET = 9


def human_readable_size(size_bytes: int) -> str:
    """Convert bytes to human readable format."""
    if size_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def get_file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def format_ratio(original: int, compressed: int) -> str:
    """Calculate compression ratio."""
    if original == 0:
        return "N/A"
    ratio = (1 - compressed / original) * 100
    return f"{ratio:.1f}%"


def encode_file(input_path: str, output_base: str, chunk_size: Optional[int]) -> None:
    start_time = time.time()

    if not os.path.exists(input_path):
        print(f"❌ Файл не найден: {input_path}")
        return

    file_size = os.path.getsize(input_path)
    print(f"📄 Входной файл: {input_path}")
    print(f"   Размер оригинала: {human_readable_size(file_size)}")

    with open(input_path, 'rb') as f:
        raw_data = f.read()

    file_hash = get_file_hash(raw_data)
    print(f"   SHA-256: {file_hash[:16]}...")

    print(f"🗜️  Сжатие (preset={COMPRESSION_PRESET})...")
    try:
        compressed_data = lzma.compress(raw_data, preset=COMPRESSION_PRESET)
        compress_time = time.time() - start_time
        print(f"   Сжато до: {human_readable_size(len(compressed_data))} "
              f"({format_ratio(file_size, len(compressed_data))}) "
              f"за {compress_time:.2f}s")
    except lzma.LZMAError as e:
        print(f"❌ Ошибка сжатия: {e}")
        return

    print(f"🔤 Кодирование Base85...")
    encode_start = time.time()
    text_data = base64.b85encode(compressed_data).decode('utf-8')
    final_content = f"{file_hash}|{text_data}"
    encode_time = time.time() - encode_start

    text_size = len(final_content.encode('utf-8'))
    print(f"   Текстовый размер: {human_readable_size(text_size)} "
          f"({format_ratio(file_size, text_size)} от оригинала)")
    print(f"   Кодирование заняло: {encode_time:.2f}s")

    if chunk_size is None or chunk_size <= 0:
        out_path = output_base + ".txt" if not output_base.endswith(".txt") else output_base
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(final_content)
        print(f"✅ Создан файл: {out_path}")
    else:
        parts = [final_content[i:i + chunk_size]
                 for i in range(0, len(final_content), chunk_size)]

        print(f"📦 Разбиение на {len(parts)} частей...")
        part_paths = []
        for idx, part in enumerate(parts, start=1):
            part_path = f"{output_base}.part{idx}.txt"
            with open(part_path, 'w', encoding='utf-8') as f:
                f.write(part)
            part_paths.append(part_path)
            print(f"   Часть {idx}: {human_readable_size(len(part.encode('utf-8')))}")

    total_time = time.time() - start_time
    print(f"✨ Готово за {total_time:.2f} секунд")


def decode_file(input_path: str, output_path: str) -> None:
    start_time = time.time()

    if not os.path.exists(input_path):
        print(f"❌ Файл не найден: {input_path}")
        return

    text_size = os.path.getsize(input_path)
    print(f"📖 Чтение текстового файла: {human_readable_size(text_size)}")

    with open(input_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()

    if '|' not in content:
        print("❌ Неверный формат: отсутствует разделитель '|'.")
        return

    original_hash, text_data = content.split('|', 1)
    print(f"   Ожидаемый хеш: {original_hash[:16]}...")

    print(f"🔡 Декодирование Base85...")
    try:
        compressed_data = base64.b85decode(text_data)
        print(f"   Сжатые данные: {human_readable_size(len(compressed_data))}")
    except ValueError as e:
        print(f"❌ Ошибка Base85: {e}")
        return

    print(f"📦 Распаковка LZMA...")
    try:
        raw_data = lzma.decompress(compressed_data)
        print(f"   Распаковано: {human_readable_size(len(raw_data))}")
    except lzma.LZMAError as e:
        print(f"❌ Ошибка распаковки: {e}")
        return

    print(f"🔍 Проверка целостности...")
    actual_hash = get_file_hash(raw_data)
    if actual_hash != original_hash:
        print(f"⚠️  Хеш не совпадает!")
        print(f"   Ожидалось: {original_hash}")
        print(f"   Получено:  {actual_hash}")
    else:
        print(f"   ✅ Хеш SHA-256 совпадает")

    with open(output_path, 'wb') as f:
        f.write(raw_data)

    elapsed = time.time() - start_time
    print(f"✅ Восстановлен: {output_path} ({elapsed:.2f}s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Кодирует файлы в текст (LZMA+Base85) с детальной статистикой.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  %(prog)s encode bigfile.zip archive
  %(prog)s encode --chunk-size 1048576 video.mp4 video_parts
  %(prog)s decode archive.txt restored.zip
        """
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    enc_parser = subparsers.add_parser("encode", help="Кодировать файл")
    enc_parser.add_argument("input", help="Входной файл")
    enc_parser.add_argument("output", help="Базовое имя выходного файла")
    enc_parser.add_argument(
        "--chunk-size", type=int, default=None,
        help="Размер части в байтах (например: 1048576 для 1MB)"
    )

    dec_parser = subparsers.add_parser("decode", help="Декодировать файл")
    dec_parser.add_argument("input", help="Текстовый файл")
    dec_parser.add_argument("output", help="Восстановленный файл")

    args = parser.parse_args()

    if args.command == "encode":
        encode_file(args.input, args.output, args.chunk_size)
    else:
        decode_file(args.input, args.output)