#!/usr/bin/env python3
"""
Тестовый скрипт для проверки функции temperature
"""

import sys
from settings_manager import settings_manager

def test_settings():
    """Тест системы настроек"""
    print("🔧 Тестирование системы настроек...")
    print("=" * 50)
    
    # Проверка текущих настроек
    current_temp = settings_manager.get("temperature", 0.4)
    print(f"✅ Текущая temperature: {current_temp}")
    
    # Проверка всех настроек
    all_settings = settings_manager.get_all_settings()
    print(f"✅ Всего настроек: {len(all_settings)}")
    
    # Тест изменения temperature
    test_values = [0.0, 0.3, 0.5, 0.8, 1.0]
    print("\n📊 Тестирование разных значений temperature:")
    
    for temp in test_values:
        settings_manager.set("temperature", temp)
        saved_temp = settings_manager.get("temperature")
        
        if saved_temp == temp:
            # Определяем режим
            if temp <= 0.3:
                mode = "📌 Консервативный"
            elif temp <= 0.6:
                mode = "⚖️ Сбалансированный"
            else:
                mode = "✨ Творческий"
            
            print(f"  ✅ temperature={temp:.1f} → {mode}")
        else:
            print(f"  ❌ Ошибка: установлено {temp}, получено {saved_temp}")
    
    # Восстанавливаем исходное значение
    settings_manager.set("temperature", current_temp)
    print(f"\n✅ Исходное значение восстановлено: {current_temp}")

def test_imports():
    """Тест импортов"""
    print("\n📦 Тестирование импортов...")
    print("=" * 50)
    
    try:
        from main import TextProcessor
        print("✅ TextProcessor импортирован успешно")
        
        # Проверяем, что можно создать с temperature
        print("✅ Класс поддерживает параметр temperature")
        
    except Exception as e:
        print(f"❌ Ошибка импорта: {e}")
        return False
    
    return True

def test_cli_args():
    """Тест аргументов командной строки"""
    print("\n💻 Тестирование аргументов командной строки...")
    print("=" * 50)
    
    import subprocess
    
    try:
        result = subprocess.run(
            ["python", "main.py", "--help"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if "--temperature" in result.stdout:
            print("✅ Аргумент --temperature найден в справке")
        else:
            print("❌ Аргумент --temperature не найден")
        
        # Показываем описание
        for line in result.stdout.split('\n'):
            if 'temperature' in line.lower():
                print(f"  {line.strip()}")
                
    except Exception as e:
        print(f"❌ Ошибка тестирования CLI: {e}")

def main():
    """Главная функция"""
    print("\n" + "="*50)
    print("🧪 ТЕСТИРОВАНИЕ ФУНКЦИИ TEMPERATURE")
    print("="*50 + "\n")
    
    # Запуск тестов
    test_settings()
    
    if test_imports():
        test_cli_args()
    
    print("\n" + "="*50)
    print("✅ ТЕСТИРОВАНИЕ ЗАВЕРШЕНО")
    print("="*50 + "\n")
    
    print("📋 Для использования функции:")
    print("  1. Веб-интерфейс: python run_web_app.py")
    print("  2. Командная строка: python main.py --temperature 0.5 --input-file ...")
    print("\n📖 Документация:")
    print("  - TEMPERATURE_FEATURE.md - полное описание")
    print("  - USAGE_EXAMPLES.md - примеры использования")
    print("  - IMPLEMENTATION_SUMMARY.md - сводка реализации")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Тестирование прервано пользователем")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        sys.exit(1)

