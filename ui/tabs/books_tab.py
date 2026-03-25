"""
Books tab: shows rewritten books from DB and manages access grants.
"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from config import DEFAULT_THEME
from core.db import (
    add_comment,
    get_book,
    get_version,
    grant_access,
    list_book_access,
    list_books,
    list_books_for_moderator,
    list_comments,
    list_versions,
    rename_book,
    restore_version,
    revoke_access,
    update_book_paraphrased,
)
from core.users import list_moderators
from main import TextProcessor
from settings_manager import (
    get_api_key,
    get_deepseek_api_key,
    get_gemini_api_key,
    get_llm_provider,
    has_active_api_key,
    settings_manager,
)


def _fmt_dt(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value or ""


def _paragraph_blocks(text: str) -> list[str]:
    return [b.strip() for b in (text or "").split("\n\n") if b.strip()]


class BooksTab:
    def render(self, tab):
        with tab:
            st.header("Книги")

            role = st.session_state.get("user_role", "guest")
            if role == "admin":
                self._render_admin()
            elif role == "moderator":
                self._render_moderator()
            else:
                st.info("Войдите в систему, чтобы увидеть книги.")

    def _render_admin(self):
        st.subheader("Все переписанные книги")

        books = list_books()
        if not books:
            st.info("Пока нет сохранённых книг. Сначала перепишите книгу во вкладке «Перефразирование».")
            return

        options = {f"#{b['id']} — {b['title']}": b["id"] for b in books}

        default_id = st.session_state.get("last_saved_book_id")
        labels = list(options.keys())
        if default_id is not None:
            try:
                idx = labels.index(next(k for k, v in options.items() if v == default_id))
            except Exception:
                idx = 0
        else:
            idx = 0

        selected_label = st.selectbox("Выберите книгу", labels, index=idx)
        book_id = int(options[selected_label])

        book = get_book(book_id)
        if not book:
            st.error("Книга не найдена.")
            return

        st.caption(
            f"Создано: {_fmt_dt(book.get('created_at', ''))} • "
            f"Автор: {book.get('created_by', '') or '—'} • "
            f"Файл: {book.get('source_filename', '') or '—'}"
        )

        st.markdown("**Название**")
        new_title = st.text_input("Название книги", value=book.get("title", ""), key=f"title_{book_id}")
        if st.button("Сохранить название", width="stretch"):
            try:
                rename_book(book_id, new_title)
                st.success("Название обновлено.")
                st.rerun()
            except Exception as e:
                st.error(f"Не удалось сохранить: {e}")

        st.markdown("---")
        st.subheader("Доступ модераторов")

        moderators = list_moderators()
        book_body = book.get("paraphrased_text", "") or ""
        dl_kwargs = dict(
            label="Скачать переписанный текст",
            data=book_body,
            file_name=f"book_{book_id}_paraphrased.txt",
            mime="text/plain",
            width="stretch",
        )
        if not moderators:
            st.info(
                "Нет модераторов. Создайте их во вкладке «Модераторы». "
                "Редактирование книги и комментарии ниже доступны всегда."
            )
            st.download_button(**dl_kwargs)
        else:
            all_usernames = [m.username for m in moderators]
            current = set(list_book_access(book_id))

            selected = st.multiselect(
                "Выдать доступ модераторам",
                options=all_usernames,
                default=[u for u in all_usernames if u in current],
            )

            col1, col2 = st.columns(2)
            with col1:
                if st.button("Сохранить доступ", type="primary", width="stretch"):
                    desired = set(selected)
                    to_add = sorted(desired - current)
                    to_remove = sorted(current - desired)
                    try:
                        for u in to_add:
                            grant_access(
                                book_id=book_id,
                                moderator_username=u,
                                granted_by=st.session_state.get("username", "admin"),
                            )
                        for u in to_remove:
                            revoke_access(book_id=book_id, moderator_username=u)
                        st.success("Доступ обновлён.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Не удалось обновить доступ: {e}")

            with col2:
                st.download_button(**dl_kwargs)

        st.markdown("---")
        self._render_versions(book_id, book, st.session_state.get("username", "admin"))

        st.markdown("---")
        st.subheader("Редактирование и перегенерация (администратор)")
        adm_user = st.session_state.get("username", "admin")
        edit_key_adm = f"adm_edit_{book_id}"
        edited_adm = st.text_area(
            "Текст книги (можно редактировать)",
            value=book.get("paraphrased_text", "") or "",
            height=320,
            key=edit_key_adm,
        )
        if st.button("Сохранить правки", key=f"adm_save_{book_id}", width="stretch"):
            try:
                update_book_paraphrased(
                    book_id,
                    edited_adm,
                    change_note="Редактирование администратором",
                    created_by=adm_user,
                )
                st.success("Правки сохранены.")
                st.rerun()
            except Exception as e:
                st.error(f"Не удалось сохранить: {e}")

        self._render_paragraph_regen_ui(book_id, book, edited_adm, adm_user, key_prefix="adm")
        self._render_fragment_regen_ui(book_id, book, edited_adm, adm_user, key_prefix="adm")

        st.markdown("---")
        self._render_admin_comments(book_id, edited_adm)

    def _render_moderator(self):
        username = st.session_state.get("username", "")
        st.subheader("Доступные вам книги")

        books = list_books_for_moderator(username)
        if not books:
            st.info("Пока нет книг, к которым вам выдан доступ.")
            return

        options = {f"#{b['id']} — {b['title']}": b["id"] for b in books}
        selected_label = st.selectbox("Выберите книгу", list(options.keys()))
        book_id = int(options[selected_label])

        book = get_book(book_id)
        if not book:
            st.error("Книга не найдена.")
            return

        st.caption(f"Создано: {_fmt_dt(book.get('created_at', ''))} • Файл: {book.get('source_filename', '') or '—'}")

        st.download_button(
            "Скачать переписанный текст",
            data=book.get("paraphrased_text", "") or "",
            file_name=f"book_{book_id}_paraphrased.txt",
            mime="text/plain",
            width="stretch",
        )

        # История версий
        self._render_versions(book_id, book, username)

        # Редактирование книги
        st.markdown("---")
        st.subheader("Редактирование")
        edit_key = f"mod_edit_{book_id}"
        edited_text = st.text_area(
            "Текст книги (можно редактировать)",
            value=book.get("paraphrased_text", "") or "",
            height=300,
            key=edit_key,
        )
        if st.button("Сохранить правки", key=f"mod_save_{book_id}", width="stretch"):
            try:
                update_book_paraphrased(
                    book_id,
                    edited_text,
                    change_note="Редактирование модератором",
                    created_by=username,
                )
                st.success("Правки сохранены.")
                st.rerun()
            except Exception as e:
                st.error(f"Не удалось сохранить: {e}")

        st.markdown("---")
        self._render_paragraph_regen_ui(book_id, book, edited_text, username, key_prefix="mod")
        self._render_fragment_regen_ui(book_id, book, edited_text, username, key_prefix="mod")

        st.markdown("---")
        st.subheader("Комментарии")
        comments = list_comments(book_id)
        for c in comments:
            para_info = f" (к абзацу {c['paragraph_index'] + 1})" if c.get("paragraph_index") is not None else ""
            st.caption(f"{c['author']} • {_fmt_dt(c.get('created_at', ''))}{para_info}")
            st.markdown(c.get("comment_text", ""))
            st.markdown("---")

        self._render_paragraph_index_legend(edited_text)
        with st.form(f"add_comment_form_{book_id}", clear_on_submit=True):
            new_comment = st.text_area("Новый комментарий", placeholder="Введите комментарий...", height=100)
            para_for_comment = st.number_input(
                "К абзацу (0 = общий комментарий)",
                min_value=0,
                value=0,
                step=1,
                help="Номер см. в списке абзацев выше.",
            )
            if st.form_submit_button("Добавить комментарий"):
                if (new_comment or "").strip():
                    try:
                        add_comment(
                            book_id=book_id,
                            author=username,
                            comment_text=new_comment.strip(),
                            paragraph_index=para_for_comment - 1 if para_for_comment > 0 else None,
                        )
                        st.success("Комментарий добавлен.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Не удалось добавить: {e}")
                else:
                    st.error("Введите текст комментария.")

    def _style_defaults_for_book(self, book: dict) -> dict:
        """Стили при сохранении книги (колонки БД) или текущие настройки проекта."""

        def one(col: str, sk: str, lo: int, hi: int, d: int) -> int:
            v = book.get(col)
            if v is not None:
                try:
                    return max(lo, min(hi, int(v)))
                except (TypeError, ValueError):
                    pass
            return max(lo, min(hi, int(settings_manager.get(sk, d))))

        return {
            "science": one("style_science", "style_science", 1, 5, 3),
            "depth": one("style_depth", "style_depth", 1, 5, 3),
            "accuracy": one("style_accuracy", "style_accuracy", 1, 5, 3),
            "readability": one("style_readability", "style_readability", 1, 7, 3),
            "source_quality": one("style_source_quality", "style_source_quality", 1, 5, 3),
        }

    def _render_paragraph_regen_ui(
        self,
        book_id: int,
        book: dict,
        edited_text: str,
        username: str,
        key_prefix: str,
    ) -> None:
        st.subheader("Перегенерация абзацев")
        blocks = _paragraph_blocks(edited_text)
        if not blocks:
            st.caption("Нет абзацев (пустой текст).")
            return
        defaults = self._style_defaults_for_book(book)
        st.caption(
            "Отметьте абзацы. Слайдеры стиля по умолчанию — как при сохранении книги в базу "
            "(если записано) или из раздела «Настройки» / генерации сейчас."
        )
        if not has_active_api_key():
            st.warning("Настройте API-ключ в «Настройках» для перегенерации.")
            return

        selected_indices: list[int] = []
        for i, block in enumerate(blocks[:50]):
            short = (block[:80] + "…") if len(block) > 80 else block
            if st.checkbox(f"Абзац {i + 1}: {short}", key=f"{key_prefix}_sel_{book_id}_{i}"):
                selected_indices.append(i)
        if len(blocks) > 50:
            st.caption(f"Показаны первые 50 из {len(blocks)} абзацев.")

        regen_prompt = ""
        regen_science = defaults["science"]
        regen_depth = defaults["depth"]
        regen_readability = defaults["readability"]
        regen_accuracy = defaults["accuracy"]
        regen_sq = defaults["source_quality"]

        if selected_indices:
            regen_prompt = st.text_area(
                "Инструкция для перегенерации (необязательно)",
                placeholder="Например: упростить язык, добавить примеры, убрать повторы…",
                height=80,
                key=f"{key_prefix}_regen_prompt_{book_id}",
            )
            st.markdown("##### Параметры стиля для этой перегенерации")
            rc1, rc2 = st.columns(2)
            with rc1:
                regen_science = st.select_slider(
                    "Научность",
                    options=[1, 2, 3, 4, 5],
                    value=defaults["science"],
                    key=f"{key_prefix}_regen_science_{book_id}",
                )
                regen_depth = st.select_slider(
                    "Глубина",
                    options=[1, 2, 3, 4, 5],
                    value=defaults["depth"],
                    key=f"{key_prefix}_regen_depth_{book_id}",
                )
                regen_sq = st.select_slider(
                    "Качество источников",
                    options=[1, 2, 3, 4, 5],
                    value=defaults["source_quality"],
                    key=f"{key_prefix}_regen_srcq_{book_id}",
                )
            with rc2:
                regen_readability = st.select_slider(
                    "Читаемость",
                    options=[1, 2, 3, 4, 5, 6, 7],
                    value=min(7, max(1, int(defaults["readability"]))),
                    key=f"{key_prefix}_regen_readability_{book_id}",
                )
                regen_accuracy = st.select_slider(
                    "Точность",
                    options=[1, 2, 3, 4, 5],
                    value=defaults["accuracy"],
                    key=f"{key_prefix}_regen_accuracy_{book_id}",
                )

        if selected_indices and st.button(
            "Перегенерировать выбранные", key=f"{key_prefix}_regen_btn_{book_id}", type="primary"
        ):
            theme = book.get("theme") or DEFAULT_THEME
            temperature = float(book.get("temperature") or settings_manager.get("temperature", 0.4))
            provider = get_llm_provider()
            api_key = (
                get_deepseek_api_key()
                if provider == "deepseek"
                else get_gemini_api_key()
                if provider == "gemini"
                else get_api_key()
            )
            regen_style = {
                "science": regen_science,
                "depth": regen_depth,
                "accuracy": regen_accuracy,
                "readability": regen_readability,
                "source_quality": regen_sq,
            }
            try:
                processor = TextProcessor(api_key, temperature=temperature, include_research=False)
                new_blocks = list(blocks)
                with st.spinner("Перегенерация..."):
                    for idx in selected_indices:
                        new_blocks[idx] = processor.paraphrase_block(
                            blocks[idx],
                            theme,
                            idx + 1,
                            custom_prompt=regen_prompt or "",
                            style_controls=regen_style,
                        )
                new_text = "\n\n".join(new_blocks)
                note = f"Перегенерация {len(selected_indices)} абзацев"
                if regen_prompt and regen_prompt.strip():
                    note += f" (инструкция: {regen_prompt.strip()[:60]})"
                update_book_paraphrased(
                    book_id,
                    new_text,
                    change_note=note,
                    created_by=username,
                )
                st.success(f"Перегенерировано абзацев: {len(selected_indices)}")
                st.rerun()
            except Exception as e:
                st.error(f"Ошибка перегенерации: {e}")

    def _render_fragment_regen_ui(
        self,
        book_id: int,
        book: dict,
        edited_text: str,
        username: str,
        key_prefix: str,
    ) -> None:
        defaults = self._style_defaults_for_book(book)
        with st.expander("Пересоздать выделенный фрагмент (скопируйте из текста выше)", expanded=False):
            st.caption(
                "Вставьте **точную** подстроку из поля «Текст книги». Она должна встречаться **ровно один раз**."
            )
            frag = st.text_area("Фрагмент", height=100, key=f"{key_prefix}_frag_txt_{book_id}")
            frag_instr = st.text_area(
                "Инструкция (что исправить)",
                height=72,
                key=f"{key_prefix}_frag_ins_{book_id}",
            )
            fc1, fc2 = st.columns(2)
            with fc1:
                fs = st.select_slider(
                    "Научность", options=[1, 2, 3, 4, 5], value=defaults["science"], key=f"{key_prefix}_frag_sci_{book_id}"
                )
                fd = st.select_slider(
                    "Глубина", options=[1, 2, 3, 4, 5], value=defaults["depth"], key=f"{key_prefix}_frag_dep_{book_id}"
                )
                fsrc = st.select_slider(
                    "Качество источников",
                    options=[1, 2, 3, 4, 5],
                    value=defaults["source_quality"],
                    key=f"{key_prefix}_frag_src_{book_id}",
                )
            with fc2:
                fr = st.select_slider(
                    "Читаемость",
                    options=[1, 2, 3, 4, 5, 6, 7],
                    value=min(7, max(1, int(defaults["readability"]))),
                    key=f"{key_prefix}_frag_read_{book_id}",
                )
                fa = st.select_slider(
                    "Точность", options=[1, 2, 3, 4, 5], value=defaults["accuracy"], key=f"{key_prefix}_frag_acc_{book_id}"
                )

            if st.button("Пересоздать фрагмент через LLM", key=f"{key_prefix}_frag_go_{book_id}", type="primary"):
                raw = (edited_text or "")
                needle = (frag or "").strip()
                if not needle:
                    st.error("Вставьте непустой фрагмент.")
                    return
                if not has_active_api_key():
                    st.warning("Настройте API-ключ в «Настройках».")
                    return
                n = raw.count(needle)
                if n != 1:
                    st.error(f"Фрагмент должен встречаться ровно один раз (найдено: {n}).")
                    return
                theme = book.get("theme") or DEFAULT_THEME
                temperature = float(book.get("temperature") or settings_manager.get("temperature", 0.4))
                provider = get_llm_provider()
                api_key = (
                    get_deepseek_api_key()
                    if provider == "deepseek"
                    else get_gemini_api_key()
                    if provider == "gemini"
                    else get_api_key()
                )
                style = {
                    "science": fs,
                    "depth": fd,
                    "accuracy": fa,
                    "readability": fr,
                    "source_quality": fsrc,
                }
                try:
                    processor = TextProcessor(api_key, temperature=temperature, include_research=False)
                    with st.spinner("Перефразирование фрагмента..."):
                        new_part = processor.paraphrase_block(
                            needle,
                            theme,
                            0,
                            custom_prompt=frag_instr or "",
                            style_controls=style,
                        )
                    new_text = raw.replace(needle, new_part, 1)
                    update_book_paraphrased(
                        book_id,
                        new_text,
                        change_note="Пересоздан фрагмент по запросу пользователя",
                        created_by=username,
                    )
                    st.success("Фрагмент обновлён.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Ошибка: {e}")

    def _render_paragraph_index_legend(self, edited_text: str) -> None:
        blocks = _paragraph_blocks(edited_text)
        if not blocks:
            return
        with st.expander("Список абзацев с номерами (для комментариев)", expanded=False):
            st.caption("Выделение мышью в Streamlit недоступно — используйте номер абзаца.")
            for i, b in enumerate(blocks[:80]):
                preview = (b.replace("\n", " ")[:140] + "…") if len(b) > 140 else b.replace("\n", " ")
                st.markdown(f"**{i + 1}.** {preview}")
            if len(blocks) > 80:
                st.caption(f"… и ещё {len(blocks) - 80} абзацев.")

    def _render_admin_comments(self, book_id: int, edited_text: str = ""):
        """Блок комментариев модераторов (видимый администратору)."""
        st.subheader("Комментарии модераторов")
        comments = list_comments(book_id)
        if not comments:
            st.caption("Нет комментариев к этой книге.")
        else:
            for c in comments:
                para_info = (
                    f" (к абзацу {c['paragraph_index'] + 1})"
                    if c.get("paragraph_index") is not None
                    else ""
                )
                st.caption(
                    f"**{c['author']}** • {_fmt_dt(c.get('created_at', ''))}{para_info}"
                )
                st.markdown(c.get("comment_text", ""))
                st.markdown("---")
        self._render_paragraph_index_legend(edited_text)
        with st.form(f"admin_comment_form_{book_id}", clear_on_submit=True):
            new_comment = st.text_area(
                "Добавить комментарий (от администратора)",
                placeholder="Введите комментарий...",
                height=80,
            )
            para_for = st.number_input(
                "К абзацу (0 = общий комментарий)",
                min_value=0,
                value=0,
                step=1,
                help="Номер абзаца — см. список выше.",
            )
            if st.form_submit_button("Добавить комментарий"):
                if (new_comment or "").strip():
                    try:
                        add_comment(
                            book_id=book_id,
                            author=st.session_state.get("username", "admin"),
                            comment_text=new_comment.strip(),
                            paragraph_index=para_for - 1 if para_for > 0 else None,
                        )
                        st.success("Комментарий добавлен.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Не удалось добавить: {e}")
                else:
                    st.error("Введите текст комментария.")

    def _render_versions(self, book_id: int, book: dict, current_user: str):
        """Блок истории версий книги."""
        versions = list_versions(book_id)
        if not versions:
            st.caption("История версий пока пуста.")
            return

        st.subheader("История версий")
        for v in versions:
            note = v.get("change_note") or "—"
            with st.expander(
                f"Версия {v['version_number']} • {_fmt_dt(v.get('created_at', ''))} • {v.get('created_by', '')} • {note}",
                expanded=False,
            ):
                st.caption(f"Создано: {_fmt_dt(v.get('created_at', ''))} • Автор: {v.get('created_by', '')}")
                st.text_area(
                    "Текст версии",
                    value=v.get("paraphrased_text", "") or "",
                    height=200,
                    disabled=True,
                    key=f"ver_{v['id']}",
                )
                if st.button("Восстановить эту версию", key=f"restore_{v['id']}", width="stretch"):
                    try:
                        restore_version(book_id, v["id"], created_by=current_user)
                        st.success("Версия восстановлена.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Не удалось восстановить: {e}")
