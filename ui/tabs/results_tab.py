"""
Модуль вкладки результатов перефразирования
"""

import os
from datetime import datetime

import streamlit as st

from core.db import create_book, get_book, list_books
from core.pdf_export import text_to_pdf
from settings_manager import settings_manager

OUTPUT_DIR = "output"
PARAPHRASED_FILE = os.path.join(OUTPUT_DIR, "paraphrased.txt")
ORIGINAL_FILE = os.path.join(OUTPUT_DIR, "original.txt")


def _fmt_dt(value) -> str:
    try:
        if hasattr(value, "replace"):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        return str(value or "")
    except Exception:
        return str(value or "")


def _is_partial_result(original: str, paraphrased: str, ratio_threshold: float = 0.35) -> bool:
    """Считаем результат частичным, если перефраз заметно короче оригинала."""
    o, p = (original or "").strip(), (paraphrased or "").strip()
    if not o or not p:
        return False
    return len(p) < ratio_threshold * len(o)


class ResultsTab:
    """Класс для управления вкладкой результатов перефразирования"""

    def _load_from_files(self) -> tuple[str, str]:
        """Загружает из output/. Возвращает (original_text, paraphrased_text) или ("", "")."""
        if not os.path.isfile(PARAPHRASED_FILE):
            return "", ""
        try:
            with open(PARAPHRASED_FILE, "r", encoding="utf-8") as f:
                paraphrased = f.read()
            if not paraphrased.strip():
                return "", ""
            original = ""
            if os.path.isfile(ORIGINAL_FILE):
                with open(ORIGINAL_FILE, "r", encoding="utf-8") as f:
                    original = f.read()
            return original, paraphrased
        except Exception:
            return "", ""

    def render(self, tab):
        """Отображение вкладки результатов"""
        with tab:
            st.header("Результаты перефразирования")

            books = list_books()
            file_original, file_paraphrased = self._load_from_files()

            # Несохранённый результат из файла (частичный или последний) — показываем первым с кнопкой «Добавить в книги»
            if file_paraphrased.strip():
                in_db = False
                if books:
                    last = get_book(books[0]["id"])
                    if last and (last.get("paraphrased_text") or "").strip() == file_paraphrased.strip():
                        in_db = True
                if not in_db:
                    is_partial = _is_partial_result(file_original, file_paraphrased)
                    st.subheader(
                        "Результат из файла (частичный или последний)" if is_partial else "Результат из файла"
                    )
                    if is_partial:
                        st.caption(
                            "⚠️ Частичный результат перефразирования (обработка была прервана). "
                            "Сохранён только в файлы; в БД не добавлен."
                        )
                    st.caption(
                        "Ещё не сохранён в список книг. Нажмите «Добавить в книги», "
                        "чтобы он появился во вкладке «Книги»."
                    )
                    col1, col2 = st.columns(2)
                    with col1:
                        with st.expander("Оригинальный текст", expanded=False):
                            st.text_area(
                                "Оригинальный текст",
                                value=file_original,
                                height=200,
                                disabled=True,
                                key="res_file_orig",
                                label_visibility="collapsed",
                            )
                    with col2:
                        with st.expander(
                            "Перефразированный текст" + (" (частично)" if is_partial else ""),
                            expanded=False,
                        ):
                            st.text_area(
                                "Перефразированный текст",
                                value=file_paraphrased,
                                height=200,
                                disabled=True,
                                key="res_file_par",
                                label_visibility="collapsed",
                            )
                    if st.button("Добавить в книги", key="add_file_to_books", type="primary"):
                        try:
                            title = "Переписанная книга (из результата)"
                            created_by = st.session_state.get("username", "admin")
                            book_id = create_book(
                                title=title,
                                source_filename=None,
                                theme=None,
                                temperature=None,
                                include_research=False,
                                original_text=file_original or None,
                                paraphrased_text=file_paraphrased,
                                created_by=created_by,
                                style_science=int(settings_manager.get("style_science", 3)),
                                style_depth=int(settings_manager.get("style_depth", 3)),
                                style_accuracy=int(settings_manager.get("style_accuracy", 3)),
                                style_readability=int(settings_manager.get("style_readability", 3)),
                                style_source_quality=int(
                                    settings_manager.get("style_source_quality", 3)
                                ),
                            )
                            st.session_state.last_saved_book_id = book_id
                            st.success(
                                f"Книга добавлена (#{book_id}). Перейдите во вкладку «Книги»."
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(f"Не удалось добавить: {e}")
                    st.markdown("---")

            # Список всех сохранённых книг
            st.subheader("Все переписанные книги")
            if not books:
                if not file_paraphrased.strip():
                    st.info(
                        "ℹ️ Выполните перефразирование на вкладке «Перефразирование», "
                        "чтобы увидеть результаты здесь."
                    )
                return

            for b in books:
                book = get_book(b["id"])
                if not book or not book.get("paraphrased_text"):
                    continue
                title = book.get("title") or f"Книга #{book['id']}"
                created_at = _fmt_dt(book.get("created_at"))
                created_by = book.get("created_by") or "—"
                orig = (book.get("original_text") or "").strip()
                par = (book.get("paraphrased_text") or "").strip()
                is_partial = _is_partial_result(orig, par)

                expander_label = f"**{title}** — {created_at} ({created_by})"
                if is_partial:
                    expander_label += " — частичный результат"
                with st.expander(expander_label, expanded=False):
                    if is_partial:
                        st.caption(
                            "⚠️ В книге сохранён частичный результат перефразирования "
                            "(обработка была прервана или добавлена из файла)."
                        )
                    col1, col2 = st.columns(2)
                    with col1:
                        st.caption("Оригинальный текст")
                        st.text_area(
                            "Оригинал",
                            value=orig,
                            height=180,
                            disabled=True,
                            key=f"res_orig_{book['id']}",
                        )
                    with col2:
                        st.caption(
                            "Перефразированный текст"
                            + (" (частично)" if is_partial else "")
                        )
                        st.text_area(
                            "Перефраз",
                            value=par,
                            height=180,
                            disabled=True,
                            key=f"res_par_{book['id']}",
                        )
                    st.download_button(
                        label="Скачать перефразированный текст",
                        data=par,
                        file_name=f"book_{book['id']}_paraphrased.txt",
                        mime="text/plain",
                        key=f"res_dl_{book['id']}",
                    )
                    if st.button("Выбрать книгу", key=f"res_add_{book['id']}"):
                        st.session_state.last_saved_book_id = book["id"]
                        st.success(
                            "Книга выбрана. Перейдите во вкладку «Книги»."
                        )
                        st.rerun()
