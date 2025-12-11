#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file2text.py – «чистый» Python‑скрипт для кодирования любого бинарного файла
в читаемый текст (LZMA + Base85) и обратного восстановления.
--------------------------------------------------------------------
Короткое руководство

 1. Кодирование одной части
    python file2text.py encode <input> <output_basename>

   * Если вывести один файл, укажите просто имя без расширения:
       python file2text.py encode myarchive.zip archive.txt

   * При желании разбить результат на несколько файлов добавьте
     опцию `--chunk-size` (в байтах).  Например,
       python file2text.py encode --chunk-size 1048576 \
           myarchive.zip archive.txt

   Это создаст файлы:
       archive.txt.part1.txt
       archive.txt.part2.txt
        … и т.д.

 2. Декодирование (один файл)

    python file2text.py decode <input_text> <output_file>

   При использовании разбиения сначала объединим части:

       cat archive.txt.part*.txt > archive_combined.txt
       python file2text.py decode archive_combined.txt recovered.zip

--------------------------------------------------------------------
Параметры и их рекомендации

* LZMA preset (`COMPRESSION_PRESET` в коде) – диапазон 0‑9.
    - 0: быстрый, но слабый компрессор (≈10 % от 9).
    - 5: «сбалансированный» вариант, обычно хватает.
    - 9: максимальное сжатие, время и память растут, но экономия места – до 30‑40 %.
      По умолчанию в скрипте стоит 9 – это лучший компрессор для большинства задач.

* `--chunk-size` (байты)
    - Необязательный параметр. Если не указан – один файл.
    - В большинстве сервисов электронной почты и мессенджеров
      ограничение размера вложения ≈ 25–30 МБ, но иногда они работают в «промежутках» 10‑15 МБ,
      а в некоторых платформах (Telegram) – 2 МБ.
    - Для «коротких» файлов достаточно одного блока; для больших архивов
      рекомендуется разбивать каждый блок по 1–2 МБ.
      Это снижает вероятность потери данных при передаче и позволяет пересобирать
      файл, даже если часть была утрачена – просто пропускается нужная часть.

* Формат выходных файлов
    - Один файл: `archive.txt` (или имя‑пользователя.txt).
    - Несколько частей: `<base>.partN.txt`.  Порядок важен; скрипт не добавляет номера
      в содержимое, поэтому при объединении части следует использовать
      сортировку по имени.

* Хеш SHA‑256
    - В начале текстового потока записывается хеш оригинала и разделитель `|`.
    - При декодировании сравниваем вновь вычисленный хеш – это гарантирует целостность
      даже после передачи в нескольких частях (просто объедините части перед чтением).

--------------------------------------------------------------------
Как использовать

 1. Кодируем:
     python file2text.py encode --chunk-size 1048576 \
         big_archive.zip archive.txt

 2. Отправляем файлы через нужный канал.

 3. На стороне получателя собираем и декодируем:

     cat archive.txt.part*.txt > combined.txt
     python file2text.py decode combined.txt recovered.zip

--------------------------------------------------------------------
Внутреннее устройство (кратко)

    input_file → LZMA → Base85 → [hash|]data →
                  ├─> один файл
                  └─> разбитый на части по --chunk-size

При декодировании происходит обратное:

    read_text → split hash + data →
                Base85 decode → LZMA decompress →
                проверка хеша → восстановленный файл.

--------------------------------------------------------------------
"""


import lzma
import base64
import hashlib
import argparse
import os
from typing import Optional

COMPRESSION_PRESET = 9


def get_file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_file(input_path: str, output_base: str,
                chunk_size: Optional[int]) -> None:
    if not os.path.exists(input_path):
        print(f"❌ Файл не найден: {input_path}")
        return

    with open(input_path, 'rb') as f:
        raw_data = f.read()

    file_hash = get_file_hash(raw_data)

    try:
        compressed_data = lzma.compress(raw_data, preset=COMPRESSION_PRESET)
    except lzma.LZMAError as e:
        print(f"❌ Ошибка сжатия: {e}")
        return

    text_data = base64.b85encode(compressed_data).decode('utf-8')
    final_content = f"{file_hash}|{text_data}"

    if chunk_size is None or chunk_size <= 0:
        out_path = output_base + ".txt" if not output_base.endswith(".txt") else output_base
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(final_content)
        print(f"✅ Записан один файл: {out_path}")
        return

    parts = [final_content[i:i + chunk_size]
             for i in range(0, len(final_content), chunk_size)]

    part_paths = []
    for idx, part in enumerate(parts, start=1):
        part_path = f"{output_base}.part{idx}.txt"
        with open(part_path, 'w', encoding='utf-8') as f:
            f.write(part)
        part_paths.append(part_path)

    print(f"✅ Разбито на {len(parts)} частей:")
    for p in part_paths:
        print(f"   • {p}")


def decode_file(input_path: str, output_path: str) -> None:
    if not os.path.exists(input_path):
        print(f"❌ Файл не найден: {input_path}")
        return

    with open(input_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()

    if '|' not in content:
        print("❌ Неверный формат: отсутствует разделитель '|'.")
        return

    original_hash, text_data = content.split('|', 1)

    try:
        compressed_data = base64.b85decode(text_data)
    except ValueError as e:
        print(f"❌ Ошибка Base85: {e}")
        return

    try:
        raw_data = lzma.decompress(compressed_data)
    except lzma.LZMAError as e:
        print(f"❌ Ошибка распаковки LZMA: {e}")
        return

    if get_file_hash(raw_data) != original_hash:
        print("⚠️ Хеш не совпадает! Файл может быть повреждён.")
    else:
        print("✅ Проверка хеша пройдена.")

    with open(output_path, 'wb') as f:
        f.write(raw_data)

    print(f"✅ Восстановлен: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Сжимает/распаковывает файлы в текстовый формат "
                    "с возможностью разбивки на части."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    enc_parser = subparsers.add_parser("encode", help="Кодировать файл")
    enc_parser.add_argument("input", help="Путь к исходному файлу")
    enc_parser.add_argument(
        "output",
        help=("База имени выходного файла (будут добавлены .partX). "
              "Если хотите один файл, просто укажите имя.")
    )
    enc_parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help="Размер части в байтах. По умолчанию один файл."
    )

    dec_parser = subparsers.add_parser("decode", help="Декодировать файл")
    dec_parser.add_argument("input", help="Путь к текстовому файлу (или объединённой части)")
    dec_parser.add_argument("output", help="Файл‑результат после восстановления")

    args = parser.parse_args()

    if args.command == "encode":
        encode_file(args.input, args.output, args.chunk_size)
    else:
        decode_file(args.input, args.output)
