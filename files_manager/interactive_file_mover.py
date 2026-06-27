#!/usr/bin/env python3
"""
Скрипт для интерактивного перемещения файлов
"""

import os
import shutil
from pathlib import Path


class FileMover:
    def __init__(self):
        self.root_dir = None  # Папка-источник
        self.dest_dir = None  # Папка назначения

    def show_help(self):
        """Показать справку"""
        help_text = """
╔══════════════════════════════════════════════════════════════╗
║                    СПРАВКА ПО КОМАНДАМ                       ║
╠══════════════════════════════════════════════════════════════╣
║  root       - Установить папку-источник (откуда копировать)  ║
║  dest       - Установить папку назначения (куда копировать)  ║
║  status     - Показать текущие настройки                     ║
║  list       - Показать файлы в папке-источнике               ║
║  <путь>     - Переместить указанный файл                     ║
║  q / quit   - Выход из программы                             ║
║  (пусто)    - Показать эту справку                           ║
╚══════════════════════════════════════════════════════════════╝
        """
        print(help_text)

    def show_status(self):
        """Показать текущие настройки"""
        print("\n📁 Текущие настройки:")
        print(f"   Источник (root): {self.root_dir or 'Не установлен'}")
        print(f"   Назначение (dest): {self.dest_dir or 'Не установлено'}\n")

    def set_root(self):
        """Установить папку-источник"""
        path = input("📂 Введите путь к папке-источнику: ").strip()

        if not path:
            print("❌ Путь не может быть пустым!")
            return

        path = os.path.expanduser(path)  # Раскрыть ~ в путь

        if os.path.isdir(path):
            self.root_dir = os.path.abspath(path)
            print(f"✅ Папка-источник установлена: {self.root_dir}")
        else:
            print(f"❌ Папка не существует: {path}")

    def set_dest(self):
        """Установить папку назначения"""
        path = input("📂 Введите путь к папке назначения: ").strip()

        if not path:
            print("❌ Путь не может быть пустым!")
            return

        path = os.path.expanduser(path)

        if os.path.isdir(path):
            self.dest_dir = os.path.abspath(path)
            print(f"✅ Папка назначения установлена: {self.dest_dir}")
        else:
            # Предложить создать папку
            create = input(f"⚠️  Папка не существует. Создать? (y/n): ").strip().lower()
            if create == 'y':
                try:
                    os.makedirs(path)
                    self.dest_dir = os.path.abspath(path)
                    print(f"✅ Папка создана и установлена: {self.dest_dir}")
                except Exception as e:
                    print(f"❌ Ошибка создания папки: {e}")
            else:
                print("❌ Папка назначения не установлена")

    def list_files(self):
        """Показать файлы в папке-источнике"""
        if not self.root_dir:
            print("❌ Сначала установите папку-источник (команда: root)")
            return

        print(f"\n📁 Файлы в {self.root_dir}:\n")

        try:
            items = os.listdir(self.root_dir)
            if not items:
                print("   (папка пуста)")
                return

            for item in sorted(items):
                full_path = os.path.join(self.root_dir, item)
                if os.path.isdir(full_path):
                    print(f"   📁 {item}/")
                else:
                    size = os.path.getsize(full_path)
                    print(f"   📄 {item} ({self.format_size(size)})")
            print()
        except Exception as e:
            print(f"❌ Ошибка чтения папки: {e}")

    def format_size(self, size):
        """Форматировать размер файла"""
        for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} ТБ"

    def move_file(self, file_path):
        """Переместить файл"""
        # Проверка настроек
        if not self.dest_dir:
            print("❌ Сначала установите папку назначения (команда: dest)")
            return

        # Определить полный путь к файлу
        if os.path.isabs(file_path):
            source_path = file_path
        elif self.root_dir:
            source_path = os.path.join(self.root_dir, file_path)
        else:
            source_path = os.path.abspath(file_path)

        source_path = os.path.expanduser(source_path)

        # Проверка существования файла
        if not os.path.exists(source_path):
            print(f"❌ Файл не найден: {source_path}")
            return

        # Определить путь назначения
        file_name = os.path.basename(source_path)
        dest_path = os.path.join(self.dest_dir, file_name)

        # Показать информацию и запросить подтверждение
        print(f"\n📄 Файл: {file_name}")
        print(f"   Откуда: {source_path}")
        print(f"   Куда:   {dest_path}")

        # Проверка на существование файла в назначении
        if os.path.exists(dest_path):
            print("⚠️  Файл уже существует в папке назначения!")

        confirm = input("\n🔄 Переместить файл? (y/n): ").strip().lower()

        if confirm == 'y':
            try:
                shutil.move(source_path, dest_path)
                print(f"✅ Файл успешно перемещён!")
            except Exception as e:
                print(f"❌ Ошибка перемещения: {e}")
        else:
            print("❌ Перемещение отменено")

    def run(self):
        """Основной цикл программы"""
        print("\n" + "="*60)
        print("       🗂️  ИНТЕРАКТИВНЫЙ МЕНЕДЖЕР ПЕРЕМЕЩЕНИЯ ФАЙЛОВ")
        print("="*60)
        self.show_help()

        while True:
            try:
                command = input(">>> ").strip()

                # Пустой ввод - показать справку
                if not command:
                    self.show_help()
                    continue

                # Выход
                if command.lower() in ('q', 'quit', 'exit'):
                    print("👋 До свидания!")
                    break

                # Установить источник
                elif command.lower() == 'root':
                    self.set_root()

                # Установить назначение
                elif command.lower() == 'dest':
                    self.set_dest()

                # Показать статус
                elif command.lower() == 'status':
                    self.show_status()

                # Показать файлы
                elif command.lower() == 'list':
                    self.list_files()

                # Справка
                elif command.lower() in ('help', 'h', '?'):
                    self.show_help()

                # Попытка переместить файл
                else:
                    self.move_file(command)

            except KeyboardInterrupt:
                print("\n👋 До свидания!")
                break
            except Exception as e:
                print(f"❌ Непредвиденная ошибка: {e}")


if __name__ == "__main__":
    mover = FileMover()
    mover.run()