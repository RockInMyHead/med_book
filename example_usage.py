#!/usr/bin/env python3
"""
Примеры использования системы управления API-ключом
"""

from settings_manager import settings_manager, get_api_key, set_api_key, has_api_key

def demo_settings_management():
    """Демонстрация работы с системой настроек"""

    print("🔧 Демонстрация системы управления настройками")
    print("=" * 50)

    # 1. Проверка начального состояния
    print("1️⃣ Начальное состояние:")
    print(f"   API ключ установлен: {has_api_key()}")
    print(f"   Текущие настройки: {settings_manager.get_all_settings()}")
    print()

    # 2. Установка API ключа
    print("2️⃣ Установка API ключа:")
    test_key = "sk-test-key-12345678901234567890"
    set_api_key(test_key)
    print(f"   API ключ установлен: {has_api_key()}")
    print(f"   Получение API ключа: {get_api_key()[:20]}...")
    print()

    # 3. Просмотр настроек
    print("3️⃣ Просмотр всех настроек:")
    for key, value in settings_manager.get_all_settings().items():
        if key == "openai_api_key" and value:
            print(f"   {key}: {value[:8]}...{value[-4:]}")
        else:
            print(f"   {key}: {value}")
    print()

    # 4. Изменение других настроек
    print("4️⃣ Изменение настроек:")
    settings_manager.set("default_theme", "МАТЕМАТИКА")
    settings_manager.set("temperature", 0.5)
    print(f"   Новая тема: {settings_manager.get('default_theme')}")
    print(f"   Новая температура: {settings_manager.get('temperature')}")
    print()

    # 5. Удаление API ключа
    print("5️⃣ Удаление API ключа:")
    settings_manager.clear_api_key()
    print(f"   API ключ установлен: {has_api_key()}")
    print()

    # 6. Сброс настроек
    print("6️⃣ Сброс настроек к значениям по умолчанию:")
    settings_manager.reset_to_defaults()
    print("   Настройки сброшены")
    print(f"   API ключ установлен: {has_api_key()}")
    print(f"   Тема по умолчанию: {settings_manager.get('default_theme')}")

    print("\n✅ Демонстрация завершена!")

if __name__ == "__main__":
    demo_settings_management()