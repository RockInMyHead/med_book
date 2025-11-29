"""
Менеджер настроек для приложения Text Re-phraser
"""

import json
import os
from typing import Dict, Any, Optional
from pathlib import Path

class SettingsManager:
    """Класс для управления настройками приложения"""

    def __init__(self, settings_file: str = "settings.json"):
        """
        Инициализация менеджера настроек

        Args:
            settings_file: Путь к файлу настроек
        """
        self.settings_file = Path(settings_file)
        self._settings = {}
        self._load_settings()

    def _load_settings(self) -> None:
        """Загрузка настроек из файла"""
        if self.settings_file.exists():
            try:
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    self._settings = json.load(f)
                print(f"✅ Настройки загружены из {self.settings_file}")
            except (json.JSONDecodeError, IOError) as e:
                print(f"⚠️  Ошибка загрузки настроек: {e}")
                print("📝 Будут использованы настройки по умолчанию")
                self._settings = self._get_default_settings()
        else:
            print(f"📝 Файл настроек не найден. Создание с настройками по умолчанию")
            self._settings = self._get_default_settings()
            self._save_settings()

    def _save_settings(self) -> None:
        """Сохранение настроек в файл"""
        try:
            # Создаем директорию, если она не существует
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)

            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)

            print(f"💾 Настройки сохранены в {self.settings_file}")
        except IOError as e:
            print(f"❌ Ошибка сохранения настроек: {e}")

    def _get_default_settings(self) -> Dict[str, Any]:
        """Получение настроек по умолчанию"""
        return {
            "openai_api_key": "",
            "default_theme": "РЕНТГЕНОДИАГНОСТИКА ЗАБОЛЕВАНИЙ КОСТЕЙ И СУСТАВОВ",
            "model": "gpt-4o",
            "temperature": 0.4,
            "max_tokens": 2000,
            "block_size": 500,
            "auto_save": True,
            "last_used_theme": "",
            "include_research": False,  # Добавлено по умолчанию флаг добавления исследований
            # API ключи для иллюстраций
            "nanobanana_api_key": "",  # Для генерации изображений
            "dalle_api_key": "",  # Для генерации изображений через DALL-E 2
            "google_search_api_key": "",  # Для поиска клинических изображений
            "google_search_engine_id": "",  # ID поисковой системы Google
            # Настройки иллюстраций
            "auto_illustration": False,  # Автоматическая иллюстрация
            "illustration_quality": "high",  # Качество изображений
            "brand_style": "medical",  # Стиль брендинга
            "tcia_enabled": False,  # TCIA API (бесплатный) - отключен по умолчанию из-за проблем с доступностью
            "tcia_timeout": 30  # Таймаут для TCIA API в секундах
        }

    def get(self, key: str, default: Any = None) -> Any:
        """
        Получение значения настройки

        Args:
            key: Ключ настройки
            default: Значение по умолчанию

        Returns:
            Значение настройки или default
        """
        return self._settings.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """
        Установка значения настройки

        Args:
            key: Ключ настройки
            value: Новое значение
        """
        self._settings[key] = value

        # Автоматическое сохранение, если включено
        if self.get("auto_save", True):
            self._save_settings()

    def save(self) -> None:
        """Принудительное сохранение настроек"""
        self._save_settings()

    def get_api_key(self) -> str:
        """
        Получение API ключа

        Returns:
            API ключ или пустая строка
        """
        return self.get("openai_api_key", "")

    def set_api_key(self, api_key: str) -> None:
        """
        Установка API ключа

        Args:
            api_key: Новый API ключ
        """
        if api_key and api_key.strip():
            self.set("openai_api_key", api_key.strip())
            print("🔑 API ключ сохранен")
        else:
            print("⚠️  API ключ не может быть пустым")

    def has_api_key(self) -> bool:
        """
        Проверка наличия API ключа

        Returns:
            True если API ключ установлен
        """
        api_key = self.get_api_key()
        return bool(api_key and api_key.strip())

    def clear_api_key(self) -> None:
        """Удаление API ключа"""
        self.set("openai_api_key", "")
        print("🗑️  API ключ удален")

    def get_all_settings(self) -> Dict[str, Any]:
        """
        Получение всех настроек

        Returns:
            Словарь со всеми настройками
        """
        return self._settings.copy()

    def reset_to_defaults(self) -> None:
        """Сброс настроек к значениям по умолчанию"""
        self._settings = self._get_default_settings()
        self._save_settings()
        print("🔄 Настройки сброшены к значениям по умолчанию")

    def __str__(self) -> str:
        """Строковое представление настроек (без чувствительных данных)"""
        settings_copy = self._settings.copy()

        # Маскируем API ключ
        if "openai_api_key" in settings_copy:
            api_key = settings_copy["openai_api_key"]
            if api_key:
                settings_copy["openai_api_key"] = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "***"
            else:
                settings_copy["openai_api_key"] = "(не установлен)"

        return f"SettingsManager(settings_file={self.settings_file}, settings={settings_copy})"

# Глобальный экземпляр менеджера настроек
settings_manager = SettingsManager()

# Функции для удобного доступа
def get_setting(key: str, default: Any = None) -> Any:
    """Получение настройки"""
    return settings_manager.get(key, default)

def set_setting(key: str, value: Any) -> None:
    """Установка настройки"""
    settings_manager.set(key, value)

def get_api_key() -> str:
    """Получение API ключа"""
    return settings_manager.get_api_key()

def set_api_key(api_key: str) -> None:
    """Установка API ключа"""
    settings_manager.set_api_key(api_key)

def has_api_key() -> bool:
    """Проверка наличия API ключа"""
    return settings_manager.has_api_key()

def save_settings() -> None:
    """Сохранение настроек"""
    settings_manager.save()

def get_include_research() -> bool:
    """Получение флага добавления информации о новых исследованиях"""
    return settings_manager.get("include_research", False)

def set_include_research(value: bool) -> None:
    """Установка флага добавления информации о новых исследованиях"""
    settings_manager.set("include_research", bool(value))

def get_nanobanana_api_key() -> str:
    """Получение API ключа NanoBanana"""
    return settings_manager.get("nanobanana_api_key", "")

def set_nanobanana_api_key(api_key: str) -> None:
    """Установка API ключа NanoBanana"""
    if api_key and api_key.strip():
        settings_manager.set("nanobanana_api_key", api_key.strip())
        print("🎨 API ключ NanoBanana сохранен")
    else:
        print("⚠️  API ключ NanoBanana не может быть пустым")

def get_google_search_api_key() -> str:
    """Получение API ключа Google Custom Search"""
    return settings_manager.get("google_search_api_key", "")

def set_google_search_api_key(api_key: str) -> None:
    """Установка API ключа Google Custom Search"""
    if api_key and api_key.strip():
        settings_manager.set("google_search_api_key", api_key.strip())
        print("🔍 API ключ Google Custom Search сохранен")
    else:
        print("⚠️  API ключ Google Custom Search не может быть пустым")

def get_google_search_engine_id() -> str:
    """Получение ID поисковой системы Google"""
    return settings_manager.get("google_search_engine_id", "")

def set_google_search_engine_id(engine_id: str) -> None:
    """Установка ID поисковой системы Google"""
    if engine_id and engine_id.strip():
        settings_manager.set("google_search_engine_id", engine_id.strip())
        print("🔍 ID поисковой системы Google сохранен")
    else:
        print("⚠️  ID поисковой системы Google не может быть пустым")

def get_dalle_api_key() -> str:
    """Получение API ключа DALL-E 2"""
    return settings_manager.get("dalle_api_key", "")

def set_dalle_api_key(api_key: str) -> None:
    """Установка API ключа DALL-E 2"""
    if api_key and api_key.strip():
        settings_manager.set("dalle_api_key", api_key.strip())
        print("🎨 API ключ DALL-E 2 сохранен")
    else:
        print("⚠️  API ключ DALL-E 2 не может быть пустым")

def has_illustration_apis() -> bool:
    """Проверка наличия API ключей для иллюстраций"""
    nanobanana = bool(get_nanobanana_api_key())
    dalle = bool(get_dalle_api_key())
    google = bool(get_google_search_api_key() and get_google_search_engine_id())
    return nanobanana or dalle or google