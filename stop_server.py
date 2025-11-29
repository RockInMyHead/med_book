#!/usr/bin/env python3
"""
Скрипт для остановки веб-сервера Text Re-phraser
"""

import subprocess
import sys

def main():
    """Остановка веб-сервера Streamlit"""

    print("🛑 Остановка веб-сервера Text Re-phraser...")

    try:
        # Находим и убиваем процесс streamlit
        result = subprocess.run(
            ["pgrep", "-f", "streamlit run app.py"],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                if pid:
                    subprocess.run(["kill", pid], check=True)
                    print(f"✅ Процесс {pid} остановлен")
        else:
            print("⚠️  Процесс streamlit не найден")

    except subprocess.CalledProcessError as e:
        print(f"❌ Ошибка при остановке сервера: {e}")
        return 1
    except Exception as e:
        print(f"❌ Непредвиденная ошибка: {e}")
        return 1

    print("✅ Веб-сервер успешно остановлен")
    return 0

if __name__ == "__main__":
    sys.exit(main())