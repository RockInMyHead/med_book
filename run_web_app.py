#!/usr/bin/env python3
"""
Скрипт для запуска веб-приложения Text Re-phraser
"""

import subprocess
import sys
import os

def main():
    """Запуск веб-приложения Streamlit"""

    print("🚀 Запуск веб-приложения Text Re-phraser...")
    print("=" * 50)

    # Проверка наличия файла app.py
    if not os.path.exists("app.py"):
        print("❌ Ошибка: файл app.py не найден!")
        print("💡 Убедитесь, что вы находитесь в корневой директории проекта")
        return

    # Проверка наличия зависимостей
    try:
        import streamlit
        print("✅ Streamlit найден")
    except ImportError:
        print("⚠️  Streamlit не установлен. Установка...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "streamlit>=1.28.0"])
            print("✅ Streamlit успешно установлен")
        except subprocess.CalledProcessError:
            print("❌ Ошибка установки Streamlit")
            return

    # Проверка других зависимостей
    required_packages = ["openai", "PyMuPDF", "python-docx", "markdown"]
    missing_packages = []

    for package in required_packages:
        try:
            __import__(package)
            print(f"✅ {package} найден")
        except ImportError:
            missing_packages.append(package)

    if missing_packages:
        print(f"⚠️  Отсутствующие пакеты: {', '.join(missing_packages)}")
        print("💡 Установка всех зависимостей...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
            print("✅ Все зависимости установлены")
        except subprocess.CalledProcessError:
            print("❌ Ошибка установки зависимостей")
            return

    print("\n" + "=" * 50)
    print("🌐 Запуск веб-сервера...")
    print("📱 Приложение будет доступно по адресу: http://localhost:8501")
    print("🛑 Для остановки нажмите Ctrl+C")
    print("=" * 50)

    # Запуск Streamlit
    try:
        subprocess.run([sys.executable, "-m", "streamlit", "run", "app.py"], check=True)
    except KeyboardInterrupt:
        print("\n\n👋 Веб-приложение остановлено пользователем")
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Ошибка запуска: {e}")
        print("💡 Попробуйте запустить вручную: streamlit run app.py")

if __name__ == "__main__":
    main()