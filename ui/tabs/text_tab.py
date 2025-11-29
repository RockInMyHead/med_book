"""
Модуль вкладки перефразирования текста
"""

import os
import tempfile
import time
import streamlit as st
from main import TextProcessor
from ui.components.file_uploader import FileUploader
from ui.components.progress_display import ProgressDisplay
from config import DEFAULT_TEMPERATURE, MIN_TEMPERATURE, MAX_TEMPERATURE, DEFAULT_THEME
from settings_manager import settings_manager, get_api_key, has_api_key


class TextTab:
    """Класс для управления вкладкой перефразирования текста"""

    def __init__(self):
        self.file_uploader = FileUploader()

    def render(self, tab):
        """Отображение вкладки перефразирования текста"""
        with tab:
            st.header("🔄 Перефразирование текста")

            # Загрузка файла
            uploaded_file = self.file_uploader.render(
                label="Выберите файл для обработки",
                allowed_types=["pdf", "txt", "md", "docx"],
                help_text="Поддерживаемые форматы: PDF, TXT, MD, DOCX"
            )

            # Ввод темы
            theme = st.text_input(
                "Тема текста",
                value=DEFAULT_THEME,
                help="Укажите тематику текста для более точного перефразирования"
            )

            # Настройка уровня перефразирования
            temperature = self._render_temperature_controls()

            # Переключатель добавления информации о новых исследованиях
            include_research = st.checkbox(
                "Добавлять информацию о новых исследованиях из разных известных источников",
                value=settings_manager.get("include_research", False),
                help="Включает добавление актуальной научной информации"
            )

            # Информация об API ключе
            self._render_api_key_status()

            # Кнопка запуска
            if st.button("🚀 Начать перефразирование", type="primary", use_container_width=True):
                self._process_file(uploaded_file, theme, temperature, include_research)

    def _render_temperature_controls(self):
        """Отображение элементов управления температурой"""
        st.markdown("#### 🎚️ Настройка стиля перефразирования")

        col1, col2 = st.columns([3, 1])
        with col1:
            temperature = st.slider(
                "Уровень творческого переформулирования",
                min_value=MIN_TEMPERATURE,
                max_value=MAX_TEMPERATURE,
                value=settings_manager.get("temperature", DEFAULT_TEMPERATURE),
                step=0.1,
                help="Низкие значения (0.0-0.3): точное сохранение смысла, минимальные изменения\n"
                 "Средние значения (0.4-0.6): баланс между точностью и вариативностью\n"
                 "Высокие значения (0.7-1.0): более творческое переформулирование"
            )
        with col2:
            st.metric("Значение", f"{temperature:.1f}")

        # Описание выбранного уровня
        if temperature <= 0.3:
            st.info("📌 **Консервативный режим**: Максимальное сохранение оригинальной структуры и терминологии")
        elif temperature <= 0.6:
            st.info("⚖️ **Сбалансированный режим**: Умеренное перефразирование с сохранением академического стиля")
        else:
            st.info("✨ **Творческий режим**: Более вариативное переформулирование с сохранением смысла")

        return temperature

    def _render_api_key_status(self):
        """Отображение статуса API ключа"""
        if has_api_key():
            st.info("ℹ️ API ключ настроен. Переходите к загрузке файла и перефразированию.")
        else:
            st.warning("⚠️ API ключ не установлен. Пожалуйста, настройте его в разделе 'Настройки' перед началом работы.")

    def _process_file(self, uploaded_file, theme, temperature, include_research):
        """Обработка загруженного файла"""
        # Валидация входных данных
        if not uploaded_file:
            st.error("❌ Пожалуйста, загрузите файл для обработки")
            return

        if not has_api_key():
            st.error("❌ API ключ не настроен. Пожалуйста, настройте его в разделе 'Настройки'")
            return

        if not theme:
            st.error("❌ Пожалуйста, укажите тему текста")
            return

        # Сохранение настроек
        settings_manager.set("temperature", temperature)
        settings_manager.set("include_research", include_research)

        # Получение API ключа
        api_key = get_api_key()

        # Создание компонента прогресса
        progress = ProgressDisplay()

        try:
            progress.start("📁 Сохранение файла...")

            # Сохранение файла во временную директорию
            with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{uploaded_file.name}") as tmp_file:
                tmp_file.write(uploaded_file.getvalue())
                temp_file_path = tmp_file.name

            progress.update_progress(0.1, "🔍 Извлечение текста...")

            # Создание процессора
            processor = TextProcessor(api_key, temperature=temperature, include_research=include_research)

            # Извлечение текста
            original_text = processor.read_input_file(temp_file_path)

            if not original_text:
                st.error("❌ Не удалось извлечь текст из файла")
                return

            progress.update_progress(0.6, f"🤖 Перефразирование текста (temperature: {temperature})...")

            # Перефразирование
            paraphrased_text = processor.process_text(original_text, theme)

            if not paraphrased_text:
                st.error("❌ Не удалось перефразировать текст")
                return

            progress.update_progress(0.9, "💾 Сохранение результатов...")

            # Сохранение результатов
            self._save_results(original_text, paraphrased_text)

            progress.complete("✅ Обработка завершена успешно!")
            st.success("🎉 Перефразирование завершено! Перейдите на вкладку 'Результаты' для просмотра.")

        except Exception as e:
            progress.update_message(f"❌ Произошла ошибка: {str(e)}")
            st.info("💡 Рекомендации по устранению ошибки:\n"
                   "1. Проверьте корректность API-ключа\n"
                   "2. Убедитесь в стабильности интернет-соединения\n"
                   "3. Проверьте, что файл не поврежден\n"
                   "4. Попробуйте использовать VPN")

        finally:
            # Очистка временного файла
            if 'temp_file_path' in locals():
                try:
                    os.unlink(temp_file_path)
                except:
                    pass

    def _save_results(self, original_text, paraphrased_text):
        """Сохранение результатов обработки"""
        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)

        # Сохранение оригинального текста
        with open(f"{output_dir}/original.txt", "w", encoding="utf-8") as f:
            f.write(original_text)

        # Сохранение перефразированного текста
        with open(f"{output_dir}/paraphrased.txt", "w", encoding="utf-8") as f:
            f.write(paraphrased_text)

        # Сохранение в session_state
        st.session_state.original_text = original_text
        st.session_state.paraphrased_text = paraphrased_text
        st.session_state.processing_complete = True
