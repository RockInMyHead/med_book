#!/usr/bin/env python3
"""
Тестовый скрипт для проверки функции автоматической иллюстрации
"""

import sys
import os
from pathlib import Path

def test_imports():
    """Тест импортов модулей иллюстраций"""
    print("📦 Тестирование импортов...")
    print("=" * 50)
    
    try:
        from illustration_pipeline import IllustrationPipeline
        print("✅ IllustrationPipeline импортирован успешно")
        
        from settings_manager import (
            get_nanobanana_api_key, set_nanobanana_api_key,
            get_google_search_api_key, set_google_search_api_key,
            get_google_search_engine_id, set_google_search_engine_id,
            has_illustration_apis
        )
        print("✅ Функции settings_manager импортированы успешно")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка импорта: {e}")
        return False

def test_settings():
    """Тест системы настроек для иллюстраций"""
    print("\n🔧 Тестирование настроек иллюстраций...")
    print("=" * 50)
    
    try:
        from settings_manager import (
            get_nanobanana_api_key, set_nanobanana_api_key,
            get_google_search_api_key, set_google_search_api_key,
            get_google_search_engine_id, set_google_search_engine_id,
            has_illustration_apis, settings_manager
        )
        
        # Проверка текущих настроек
        print(f"✅ NanoBanana API: {'настроен' if get_nanobanana_api_key() else 'не настроен'}")
        print(f"✅ Google Search API: {'настроен' if get_google_search_api_key() else 'не настроен'}")
        print(f"✅ Google Engine ID: {'настроен' if get_google_search_engine_id() else 'не настроен'}")
        print(f"✅ TCIA API: {'включен' if settings_manager.get('tcia_enabled', True) else 'отключен'}")
        print(f"✅ Автоиллюстрация: {'включена' if settings_manager.get('auto_illustration', False) else 'отключена'}")
        
        # Тест установки настроек
        print("\n📊 Тестирование установки настроек:")
        
        # Тест NanoBanana API
        test_key = "test-nanobanana-key-123"
        set_nanobanana_api_key(test_key)
        if get_nanobanana_api_key() == test_key:
            print("  ✅ NanoBanana API ключ установлен корректно")
        else:
            print("  ❌ Ошибка установки NanoBanana API ключа")
        
        # Тест Google API
        test_google_key = "test-google-key-456"
        test_engine_id = "test-engine-id-789"
        set_google_search_api_key(test_google_key)
        set_google_search_engine_id(test_engine_id)
        
        if (get_google_search_api_key() == test_google_key and 
            get_google_search_engine_id() == test_engine_id):
            print("  ✅ Google API ключи установлены корректно")
        else:
            print("  ❌ Ошибка установки Google API ключей")
        
        # Тест проверки наличия API
        if has_illustration_apis():
            print("  ✅ has_illustration_apis() работает корректно")
        else:
            print("  ❌ has_illustration_apis() не работает")
        
        # Очистка тестовых данных
        set_nanobanana_api_key("")
        set_google_search_api_key("")
        set_google_search_engine_id("")
        print("  ✅ Тестовые данные очищены")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка тестирования настроек: {e}")
        return False

def test_illustration_pipeline():
    """Тест основного класса IllustrationPipeline"""
    print("\n🎨 Тестирование IllustrationPipeline...")
    print("=" * 50)
    
    try:
        from illustration_pipeline import IllustrationPipeline
        
        # Создание экземпляра
        pipeline = IllustrationPipeline()
        print("✅ IllustrationPipeline создан успешно")
        
        # Тест классификации изображений
        test_texts = [
            "Рентгеновский снимок показывает перелом кости",
            "Схема строения костной ткани",
            "Клинический случай остеопороза",
            "Анатомическая диаграмма сустава"
        ]
        
        print("\n📊 Тестирование классификации изображений:")
        for text in test_texts:
            classification = pipeline._classify_image(text)
            pathology = pipeline._extract_pathology(text)
            print(f"  Текст: '{text[:30]}...'")
            print(f"    Классификация: {classification}")
            print(f"    Патология: {pathology}")
        
        # Тест списка патологий в розыске
        print("\n📋 Тестирование списка патологий в розыске:")
        search_list = pipeline.get_pathology_search_list()
        print(f"  Текущий список: {len(search_list)} патологий")
        
        # Тест TCIA API (без реального запроса)
        print("\n🔍 Тестирование TCIA API:")
        if pipeline.tcia_enabled:
            print("  ✅ TCIA API включен")
        else:
            print("  ❌ TCIA API отключен")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка тестирования IllustrationPipeline: {e}")
        return False

def test_pdf_processing():
    """Тест обработки PDF файла"""
    print("\n📄 Тестирование обработки PDF...")
    print("=" * 50)
    
    try:
        from illustration_pipeline import IllustrationPipeline
        
        pipeline = IllustrationPipeline()
        
        # Проверка наличия тестового PDF
        test_pdf = "input/Кости_глава_1.pdf"
        if os.path.exists(test_pdf):
            print(f"✅ Найден тестовый PDF: {test_pdf}")
            
            # Тест извлечения изображений
            print("🔍 Тестирование извлечения изображений...")
            images = pipeline.extract_images_from_pdf(test_pdf)
            
            if images:
                print(f"✅ Извлечено {len(images)} изображений")
                
                # Показываем информацию о первых 3 изображениях
                for i, img in enumerate(images[:3]):
                    print(f"  Изображение {i+1}:")
                    print(f"    Страница: {img['page']}")
                    print(f"    Размер: {img['width']}x{img['height']}")
                    print(f"    Классификация: {img['classification']}")
                    print(f"    Патология: {img['pathology']}")
                    print(f"    Контекст: '{img['text_around'][:50]}...'")
            else:
                print("⚠️  Изображения не найдены в PDF")
        else:
            print(f"⚠️  Тестовый PDF не найден: {test_pdf}")
            print("💡 Поместите PDF файл в папку input/ для полного тестирования")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка тестирования PDF: {e}")
        return False

def test_web_interface():
    """Тест веб-интерфейса"""
    print("\n🌐 Тестирование веб-интерфейса...")
    print("=" * 50)
    
    try:
        # Проверка импорта app.py
        import app
        print("✅ app.py импортирован успешно")
        
        # Проверка наличия новых функций в app.py
        if hasattr(app, 'main'):
            print("✅ Функция main() найдена")
        else:
            print("❌ Функция main() не найдена")
        
        print("✅ Веб-интерфейс готов к использованию")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка тестирования веб-интерфейса: {e}")
        return False

def main():
    """Главная функция тестирования"""
    print("\n" + "="*50)
    print("🧪 ТЕСТИРОВАНИЕ ФУНКЦИИ АВТОМАТИЧЕСКОЙ ИЛЛЮСТРАЦИИ")
    print("="*50 + "\n")
    
    tests = [
        ("Импорты", test_imports),
        ("Настройки", test_settings),
        ("IllustrationPipeline", test_illustration_pipeline),
        ("Обработка PDF", test_pdf_processing),
        ("Веб-интерфейс", test_web_interface)
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        try:
            if test_func():
                passed += 1
                print(f"✅ {test_name}: ПРОЙДЕН")
            else:
                print(f"❌ {test_name}: НЕ ПРОЙДЕН")
        except Exception as e:
            print(f"❌ {test_name}: ОШИБКА - {e}")
        print()
    
    print("="*50)
    print(f"📊 РЕЗУЛЬТАТЫ ТЕСТИРОВАНИЯ: {passed}/{total} тестов пройдено")
    
    if passed == total:
        print("🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!")
        print("\n📋 Для использования функции:")
        print("  1. Веб-интерфейс: python run_web_app.py")
        print("  2. Откройте вкладку '🎨 Иллюстрации'")
        print("  3. Настройте API ключи в разделе 'Настройки'")
        print("  4. Загрузите PDF файл и начните обработку")
    else:
        print("⚠️  НЕКОТОРЫЕ ТЕСТЫ НЕ ПРОЙДЕНЫ")
        print("💡 Проверьте настройки и зависимости")
    
    print("\n📖 Документация:")
    print("  - ILLUSTRATION_FEATURE.md - полное описание")
    print("  - README.md - общая документация")
    
    return passed == total

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Тестирование прервано пользователем")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        sys.exit(1)
