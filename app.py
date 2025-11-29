"""
Главный файл приложения Text Re-phraser
"""

import streamlit as st
from ui.base import BaseUI
from ui.tabs.text_tab import TextTab
from ui.tabs.results_tab import ResultsTab
from ui.tabs.images_tab import ImagesTab
from ui.tabs.settings_tab import SettingsTab


class TextRephraserApp:
    """Главный класс приложения Text Re-phraser"""

    def __init__(self):
        """Инициализация приложения"""
        self.ui = BaseUI()
        self.tabs = {
            'text': TextTab(),
            'results': ResultsTab(),
            'images': ImagesTab(),
            'settings': SettingsTab()
        }

    def run(self):
        """Запуск приложения"""
        # Настройка страницы
        self.ui.setup_page()

        # Инициализация состояния
        self.ui.init_session_state()

        # Создание табов
        tab1, tab2, tab3, tab5 = self.ui.create_tabs()

        # Отображение табов
        self.tabs['text'].render(tab1)
        self.tabs['results'].render(tab2)
        self.tabs['images'].render(tab3)
        self.tabs['settings'].render(tab5)


def main():
    """Точка входа в приложение"""
    app = TextRephraserApp()
    app.run()


if __name__ == "__main__":
    main()