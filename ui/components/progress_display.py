"""
Компонент отображения прогресса для приложения Text Re-phraser
"""

import streamlit as st
from typing import Optional
from config import PROGRESS_UPDATE_INTERVAL


class ProgressDisplay:
    """Компонент для отображения прогресса операций"""

    def __init__(self):
        self.progress_bar = None
        self.status_text = None

    def start(self, initial_message: str = "Начинаем обработку..."):
        """Запуск отображения прогресса"""
        self.progress_bar = st.progress(0)
        self.status_text = st.empty()
        self.update_message(initial_message)

    def update_progress(self, progress: float, message: Optional[str] = None):
        """
        Обновление прогресса

        Args:
            progress: Значение прогресса (0.0 - 1.0)
            message: Сообщение статуса (опционально)
        """
        if self.progress_bar:
            self.progress_bar.progress(progress)

        if message:
            self.update_message(message)

    def update_message(self, message: str):
        """Обновление сообщения статуса"""
        if self.status_text:
            self.status_text.text(message)

    def complete(self, final_message: str = "✅ Обработка завершена!"):
        """Завершение отображения прогресса"""
        if self.progress_bar:
            self.progress_bar.progress(1.0)
        self.update_message(final_message)

    def cleanup(self):
        """Очистка компонентов прогресса"""
        if self.progress_bar:
            self.progress_bar.empty()
        if self.status_text:
            self.status_text.empty()

    def __enter__(self):
        """Контекстный менеджер - вход"""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Контекстный менеджер - выход"""
        if exc_type:
            self.update_message(f"❌ Ошибка: {str(exc_val)}")
        else:
            self.complete()
        self.cleanup()


class ProgressCallback:
    """Класс для обратных вызовов прогресса"""

    def __init__(self, progress_display: ProgressDisplay):
        self.progress_display = progress_display
        self.steps = []

    def add_step(self, progress: float, message: str):
        """Добавление шага прогресса"""
        self.steps.append((progress, message))

    def execute_step(self, step_index: int):
        """Выполнение шага прогресса"""
        if 0 <= step_index < len(self.steps):
            progress, message = self.steps[step_index]
            self.progress_display.update_progress(progress, message)
