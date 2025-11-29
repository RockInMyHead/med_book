"""
Модуль вкладки результатов перефразирования
"""

import streamlit as st


class ResultsTab:
    """Класс для управления вкладкой результатов перефразирования"""

    def render(self, tab):
        """Отображение вкладки результатов"""
        with tab:
            st.header("📊 Результаты перефразирования")

            if 'processing_complete' in st.session_state and st.session_state.processing_complete:
                self._render_results()
            else:
                st.info("ℹ️ Выполните перефразирование на вкладке 'Перефразирование', чтобы увидеть результаты здесь.")

    def _render_results(self):
        """Отображение результатов перефразирования"""
        col1, col2 = st.columns(2)

        with col1:
            self._render_original_text()

        with col2:
            self._render_paraphrased_text()
            self._render_statistics()

    def _render_original_text(self):
        """Отображение оригинального текста"""
        st.subheader("📄 Оригинальный текст")

        # Добавляем возможность скачать оригинальный текст
        st.download_button(
            label="📥 Скачать оригинал",
            data=st.session_state.original_text,
            file_name="original.txt",
            mime="text/plain",
            key="download_original"
        )

        st.text_area(
            "Оригинальный текст:",
            st.session_state.original_text,
            height=400,
            disabled=True,
            key="original_text_area"
        )

    def _render_paraphrased_text(self):
        """Отображение перефразированного текста"""
        st.subheader("✨ Перефразированный текст")

        # Добавляем возможность скачать перефразированный текст
        st.download_button(
            label="📥 Скачать результат",
            data=st.session_state.paraphrased_text,
            file_name="paraphrased.txt",
            mime="text/plain",
            key="download_paraphrased"
        )

        st.text_area(
            "Перефразированный текст:",
            st.session_state.paraphrased_text,
            height=400,
            disabled=True,
            key="paraphrased_text_area"
        )

    def _render_statistics(self):
        """Отображение статистики результатов"""
        st.markdown("---")
        st.subheader("📈 Статистика")

        col1, col2, col3 = st.columns(3)

        original_words = len(st.session_state.original_text.split())
        paraphrased_words = len(st.session_state.paraphrased_text.split())

        with col1:
            st.metric("Слов в оригинале", original_words)

        with col2:
            st.metric("Слов в результате", paraphrased_words)

        with col3:
            if original_words > 0:
                diff_percent = round((paraphrased_words - original_words) / original_words * 100, 1)
                st.metric("Изменение объема", f"{diff_percent}%")
            else:
                st.metric("Изменение объема", "0%")
