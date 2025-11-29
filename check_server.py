#!/usr/bin/env python3
"""
Скрипт для проверки статуса веб-сервера Text Re-phraser
"""

import subprocess
import requests
import time

def check_process():
    """Проверка наличия процесса streamlit"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "streamlit run app.py"],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except Exception:
        return False

def check_web_server():
    """Проверка доступности веб-сервера"""
    try:
        response = requests.get("http://localhost:8501", timeout=5)
        return response.status_code == 200
    except Exception:
        return False

def main():
    """Основная функция проверки"""

    print("🔍 Проверка статуса веб-сервера Text Re-phraser...")
    print("=" * 50)

    # Проверка процесса
    process_running = check_process()
    print(f"📊 Процесс streamlit: {'✅ Запущен' if process_running else '❌ Не запущен'}")

    # Проверка веб-сервера
    if process_running:
        time.sleep(1)  # Небольшая задержка
        web_running = check_web_server()
        print(f"🌐 Веб-сервер: {'✅ Доступен' if web_running else '❌ Недоступен'}")

        if web_running:
            print("\n🎉 Веб-приложение работает корректно!")
            print("📱 Доступно по адресу: http://localhost:8501")
        else:
            print("\n⚠️  Процесс запущен, но веб-сервер недоступен")
            print("💡 Попробуйте перезапустить приложение")
    else:
        print("\n❌ Веб-сервер не запущен")
        print("💡 Запустите командой: python run_web_app.py")

    print("=" * 50)

if __name__ == "__main__":
    main()