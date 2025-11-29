"""
Компонент загрузки файлов для приложения Text Re-phraser
"""

import streamlit as st
from typing import Optional, List
from config import SUPPORTED_FILE_TYPES, MAX_FILE_SIZE


class FileUploader:
    """Компонент для загрузки файлов с валидацией"""

    @staticmethod
    def render(
        label: str,
        allowed_types: List[str] = None,
        help_text: str = None,
        key: str = None
    ) -> Optional[any]:
        """
        Отображает компонент загрузки файла

        Args:
            label: Текст ярлыка
            allowed_types: Список разрешенных типов файлов
            help_text: Текст подсказки
            key: Уникальный ключ компонента

        Returns:
            Загруженный файл или None
        """
        if allowed_types is None:
            allowed_types = SUPPORTED_FILE_TYPES

        if help_text is None:
            help_text = f"Поддерживаемые форматы: {', '.join(allowed_types).upper()}"

        uploaded_file = st.file_uploader(
            label=label,
            type=allowed_types,
            help=help_text,
            key=key
        )

        return uploaded_file

    @staticmethod
    def validate_file(file) -> tuple[bool, str]:
        """
        Валидирует загруженный файл

        Args:
            file: Загруженный файл

        Returns:
            Кортеж (is_valid, error_message)
        """
        if file is None:
            return False, "Файл не выбран"

        if file.size > MAX_FILE_SIZE:
            return False, f"Размер файла превышает {MAX_FILE_SIZE // (1024*1024)} MB"

        return True, ""

    @staticmethod
    def get_file_info(file) -> dict:
        """
        Получает информацию о файле

        Args:
            file: Загруженный файл

        Returns:
            Словарь с информацией о файле
        """
        if file is None:
            return {}

        return {
            'name': file.name,
            'size': file.size,
            'size_mb': round(file.size / (1024 * 1024), 2),
            'type': file.type
        }
