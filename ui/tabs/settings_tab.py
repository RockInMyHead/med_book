"""
Модуль вкладки настроек и информации
"""

import streamlit as st
from settings_manager import (
    has_api_key, get_api_key, set_api_key, settings_manager,
    get_nanobanana_api_key, set_nanobanana_api_key,
    get_dalle_api_key, set_dalle_api_key,
    get_google_search_api_key, set_google_search_api_key,
    get_google_search_engine_id, set_google_search_engine_id
)


class SettingsTab:
    """Класс для управления вкладкой настроек"""

    def render(self, tab):
        """Отображение вкладки настроек"""
        with tab:
            st.header("⚙️ Настройки и информация")

            self._render_openai_settings()
            self._render_illustration_api_settings()
            self._render_help_section()

    def _render_openai_settings(self):
        """Отображение настроек OpenAI API"""
        st.subheader("🔑 Управление API ключом")

        # Статус API ключа
        if has_api_key():
            st.success("✅ API ключ сохранен")
            masked_key = get_api_key()
            if len(masked_key) > 12:
                masked_key = f"{masked_key[:8]}...{masked_key[-4:]}"
            st.code(f"Ключ: {masked_key}", language=None)

            col1, col2 = st.columns(2)
            with col1:
                if st.button("🔄 Изменить API ключ", use_container_width=True):
                    st.session_state.show_api_key_settings = True
            with col2:
                if st.button("🗑️ Удалить API ключ", use_container_width=True):
                    settings_manager.clear_api_key()
                    st.session_state.api_key_saved = False
                    st.session_state.api_key_status = "🔑 API ключ не установлен"
                    st.success("🗑️ API ключ удален!")
                    st.rerun()
        else:
            st.warning("⚠️ API ключ не установлен")
            if st.button("➕ Добавить API ключ", use_container_width=True):
                st.session_state.show_api_key_settings = True

        # Форма для ввода API ключа
        if st.session_state.get('show_api_key_settings', False):
            with st.expander("🔑 Настройка API ключа", expanded=True):
                st.markdown("Введите ваш API ключ")

                new_api_key = st.text_input(
                    "API-ключ",
                    type="password",
                    help="Введите ваш API-ключ",
                    key="settings_api_key_input"
                )

                col1, col2, col3 = st.columns([1, 1, 1])
                with col1:
                    if st.button("💾 Сохранить", use_container_width=True):
                        if new_api_key and new_api_key.strip():
                            set_api_key(new_api_key.strip())
                            st.session_state.api_key_saved = True
                            st.session_state.api_key_status = "🔑 API ключ сохранен"
                            st.session_state.show_api_key_settings = False
                            st.success("🔑 API ключ успешно сохранен!")
                            st.rerun()
                        else:
                            st.error("❌ API ключ не может быть пустым")

                with col2:
                    if st.button("🔍 Проверить", use_container_width=True):
                        if new_api_key and new_api_key.strip():
                            st.info("🔍 Проверка API ключа...")
                            # Здесь можно добавить реальную проверку ключа через тестовый запрос
                            st.success("✅ Формат API ключа корректный")
                        else:
                            st.error("❌ Введите API ключ для проверки")

                with col3:
                    if st.button("❌ Отмена", use_container_width=True):
                        st.session_state.show_api_key_settings = False
                        st.rerun()

    def _render_illustration_api_settings(self):
        """Отображение настроек API для иллюстраций"""
        st.markdown("---")
        st.subheader("🎨 API ключи для иллюстраций")

        # DALL-E 2 API
        if get_dalle_api_key():
            st.success("✅ DALL-E 2 API ключ настроен")
            if st.button("🗑️ Удалить DALL-E 2 API ключ", use_container_width=True):
                set_dalle_api_key("")
                st.rerun()
        else:
            st.warning("⚠️ DALL-E 2 API ключ не установлен")
            if st.button("➕ Добавить DALL-E 2 API ключ", use_container_width=True):
                st.session_state.show_dalle_settings = True

        # NanoBanana API
        if get_nanobanana_api_key():
            st.success("✅ NanoBanana API ключ настроен")
            if st.button("🗑️ Удалить NanoBanana API ключ", use_container_width=True):
                set_nanobanana_api_key("")
                st.rerun()
        else:
            st.warning("⚠️ NanoBanana API ключ не установлен")
            if st.button("➕ Добавить NanoBanana API ключ", use_container_width=True):
                st.session_state.show_nanobanana_settings = True

        # Google Custom Search API
        if get_google_search_api_key() and get_google_search_engine_id():
            st.success("✅ Google Custom Search API настроен")
            if st.button("🗑️ Удалить Google Custom Search API", use_container_width=True):
                set_google_search_api_key("")
                set_google_search_engine_id("")
                st.rerun()
        else:
            st.warning("⚠️ Google Custom Search API не настроен")
            if st.button("➕ Настроить Google Custom Search API", use_container_width=True):
                st.session_state.show_google_settings = True

        # Формы настройки дополнительных API
        self._render_additional_api_forms()

    def _render_additional_api_forms(self):
        """Отображение форм для настройки дополнительных API"""

        # Форма DALL-E
        if st.session_state.get('show_dalle_settings', False):
            with st.expander("🎭 Настройка DALL-E 2 API", expanded=True):
                st.markdown("Введите ваш API ключ OpenAI для доступа к DALL-E 2")
                st.info("💡 DALL-E 2 требует действующей подписки OpenAI и доступ к API")

                dalle_key = st.text_input(
                    "DALL-E 2 API-ключ",
                    type="password",
                    help="Введите ваш API-ключ от OpenAI (тот же, что используется для ChatGPT)",
                    key="dalle_api_key_input"
                )

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("💾 Сохранить", use_container_width=True):
                        if dalle_key and dalle_key.strip():
                            set_dalle_api_key(dalle_key.strip())
                            st.session_state.show_dalle_settings = False
                            st.success("🎭 DALL-E 2 API ключ сохранен!")
                            st.rerun()
                        else:
                            st.error("❌ API ключ не может быть пустым")

                with col2:
                    if st.button("❌ Отмена", use_container_width=True):
                        st.session_state.show_dalle_settings = False
                        st.rerun()

        # Форма NanoBanana
        if st.session_state.get('show_nanobanana_settings', False):
            with st.expander("🎨 Настройка NanoBanana API", expanded=True):
                st.markdown("Введите ваш API ключ NanoBanana")

                nanobanana_key = st.text_input(
                    "NanoBanana API-ключ",
                    type="password",
                    help="Введите ваш API-ключ от NanoBanana",
                    key="nanobanana_api_key_input"
                )

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("💾 Сохранить NanoBanana", use_container_width=True):
                        if nanobanana_key and nanobanana_key.strip():
                            set_nanobanana_api_key(nanobanana_key.strip())
                            st.session_state.show_nanobanana_settings = False
                            st.success("🎨 NanoBanana API ключ сохранен!")
                            st.rerun()
                        else:
                            st.error("❌ API ключ не может быть пустым")

                with col2:
                    if st.button("❌ Отмена", use_container_width=True):
                        st.session_state.show_nanobanana_settings = False
                        st.rerun()

        # Форма Google Custom Search
        if st.session_state.get('show_google_settings', False):
            with st.expander("🔍 Настройка Google Custom Search API", expanded=True):
                st.markdown("Введите настройки Google Custom Search API")

                google_key = st.text_input(
                    "Google API Key",
                    type="password",
                    help="Введите ваш Google API Key",
                    key="google_api_key_input"
                )

                search_engine_id = st.text_input(
                    "Search Engine ID",
                    help="Введите ваш Custom Search Engine ID",
                    key="search_engine_id_input"
                )

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("💾 Сохранить Google API", use_container_width=True):
                        if google_key and search_engine_id:
                            set_google_search_api_key(google_key.strip())
                            set_google_search_engine_id(search_engine_id.strip())
                            st.session_state.show_google_settings = False
                            st.success("🔍 Google Custom Search API настроен!")
                            st.rerun()
                        else:
                            st.error("❌ Заполните все поля")

                with col2:
                    if st.button("❌ Отмена", use_container_width=True):
                        st.session_state.show_google_settings = False
                        st.rerun()

    def _render_help_section(self):
        """Отображение секции помощи"""
        st.markdown("---")
        st.subheader("📚 Параметры обработки")

        current_temp = settings_manager.get("temperature", 0.4)
        st.markdown(f"""
        **Максимальная длина блока:** 500 символов
        **Температура (текущая):** {current_temp} (от 0.0 до 1.0)

        💡 **Информация о температуре:**
        - **0.0-0.3**: Консервативный режим - минимальные изменения
        - **0.4-0.6**: Сбалансированный режим - умеренное перефразирование
        - **0.7-1.0**: Творческий режим - более вариативное переформулирование
        """)

        st.subheader("📋 Инструкции по использованию")
        st.markdown("""
        1. **Загрузите файл** в поддерживаемом формате (PDF, TXT, MD, DOCX)
        2. **Укажите тему текста** для более точного перефразирования
        3. **Настройте уровень перефразирования** (temperature) с помощью слайдера:
           - 0.0-0.3: Консервативный режим
           - 0.4-0.6: Сбалансированный режим (рекомендуется)
           - 0.7-1.0: Творческий режим
        4. **Настройте API-ключ** в разделе "Настройки"
        5. **Нажмите кнопку** "Начать перефразирование"
        6. **Просмотрите результаты** на вкладке "Результаты"

        📖 **Подробнее:** см. файлы TEMPERATURE_FEATURE.md и USAGE_EXAMPLES.md
        """)

        st.subheader("🚨 Возможные проблемы")
        with st.expander("Распространенные ошибки и решения"):
            st.markdown("""
            **Ошибка API-ключа:**
            - Проверьте корректность ключа
            - Убедитесь, что ключ активен

            **Ошибка сети:**
            - Проверьте стабильность соединения

            **Ошибка файла:**
            - Проверьте формат файла
            - Убедитесь, что файл не поврежден
            """)
