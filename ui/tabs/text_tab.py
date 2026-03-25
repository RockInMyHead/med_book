"""
Модуль вкладки перефразирования текста
"""

import os
import re
import logging
import hashlib
from io import BytesIO
from datetime import datetime
import tempfile
import base64
import threading
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)
import streamlit as st
import streamlit.components.v1 as components
import markdown as md
from main import TextProcessor
from core.pubmed import fetch_pubmed_summaries, fetch_abstracts_for_pmids
from core.article_processor import (
    normalize_article_structure,
    check_spelling_ru,
    add_markdown_links,
)
from ui.components.file_uploader import FileUploader
from ui.components.progress_display import ProgressDisplay
from config import DEFAULT_TEMPERATURE, MIN_TEMPERATURE, MAX_TEMPERATURE, DEFAULT_THEME
from ui.utils import slider_value_for_step
from settings_manager import (
    settings_manager,
    get_api_key,
    has_api_key,
    get_deepseek_api_key,
    get_gemini_api_key,
    get_llm_provider,
    set_llm_provider,
    has_active_api_key,
    get_nanobanana_api_key,
    get_dalle_api_key,
)
from core.db import create_book, add_article_to_history, list_article_history, mark_article_saved
from core.pdf_export import text_to_pdf, text_to_pdf_with_images, _find_figure_refs_in_block
from core.docx_export import text_to_docx


class TextTab:
    """Класс для управления вкладкой перефразирования текста"""

    def __init__(self):
        self.file_uploader = FileUploader()

    def _format_markdown_preview(self, text: str) -> str:
        """Добавляет титульный блок и секцию исследований для Markdown-превью."""
        editor_block = "ГЛАВНЫЙ РЕДАКТОР\n\n**Покровский Вадим Валенти**"
        research_block = (
            "## Последние научные исследования\n\n"
            "_Этот раздел заполняется автоматически при включенной опции "
            "«Добавлять информацию о новых исследованиях»._"
        )
        return f"# {editor_block}\n\n---\n\n{research_block}\n\n---\n\n{text}"

    # Лимит размера одного WebSocket-сообщения Streamlit (~8K); при большем — RangeError
    _STREAM_DISPLAY_MAX_CHARS = 7200
    _STREAM_UPDATE_INTERVAL_CHARS = 1200
    # До этой длины рендерим статью одним блоком (одна область прокрутки)
    _SINGLE_BLOCK_MAX_CHARS = 28000

    def _render_article_display(self, article: str, max_chars: Optional[int] = None) -> None:
        """Красиво отображает сгенерированную статью. max_chars: при стриме не передавать в UI больше (избегает WebSocket RangeError)."""
        if not article or not article.strip():
            return
        if max_chars and len(article) > max_chars:
            article = article[-max_chars:]
        if not max_chars and ("data:image/" in article or "](file:" in article):
            self._render_article_with_inline_images(article)
            return
        self._render_long_text(article)

    _RE_FILE_PATH_CLEANUP = re.compile(r'\(file:[^)]+\)')

    def _clean_file_paths_from_text(self, text: str) -> str:
        """Remove any remaining raw (file:...) references from text intended for st.markdown."""
        return self._RE_FILE_PATH_CLEANUP.sub('', text).strip()

    def _render_article_with_inline_images(self, article: str, allow_regen: bool = True) -> None:
        """Рендерит текст с изображениями: data-URI или file:path.
        Использует st.markdown для текста (без iframe) и st.image для картинок,
        чтобы вся статья шла единым потоком без отдельных скроллбаров.
        allow_regen: показывать кнопку перегенерации изображения."""
        from config import ILLUSTRATION_STYLES
        style_options = list(ILLUSTRATION_STYLES.keys())
        current_style = settings_manager.get("illustration_style", "academic")

        pattern = re.compile(
            r'!\[(?P<alt>[^\]]*)\]\((?P<data>data:image\/[^)]+)\)|'
            r'!\[(?P<alt2>[^\]]*)\]\((?P<fpath>file:[^)]+)\)'
        )
        last = 0
        img_counter = 0
        for m in pattern.finditer(article):
            text_part = article[last:m.start()].strip()
            if text_part:
                st.markdown(self._clean_file_paths_from_text(text_part))
            alt = (m.group("alt") or m.group("alt2") or "").strip()
            img_counter += 1
            displayed = False
            file_path = None
            if m.group("data"):
                img_bytes, _ = self._decode_data_uri(m.group("data"))
                if img_bytes:
                    st.image(img_bytes, caption=alt or None, use_container_width=True)
                    displayed = True
            else:
                path_raw = (m.group("fpath") or "").strip()
                if path_raw.startswith("file:"):
                    path_raw = path_raw[5:]
                if path_raw:
                    path_check = os.path.abspath(path_raw) if not os.path.isabs(path_raw) else path_raw
                    if os.path.isfile(path_check):
                        try:
                            st.image(path_check, caption=alt or None, use_container_width=True)
                            displayed = True
                            file_path = path_check
                        except Exception:
                            st.caption(f"⚠ Не удалось отобразить: {alt or os.path.basename(path_check)}")
                    else:
                        st.caption(f"🖼 {alt or 'Изображение'} (файл недоступен)")

            if allow_regen and displayed:
                regen_key = f"img_regen_{img_counter}"
                with st.expander(f"🔄 Перегенерировать изображение", expanded=False):
                    regen_style = st.selectbox(
                        "Стиль",
                        options=style_options,
                        index=style_options.index(current_style) if current_style in style_options else 2,
                        key=f"{regen_key}_style",
                    )
                    regen_prompt = st.text_input(
                        "Промпт (описание изображения)",
                        value=alt or "",
                        key=f"{regen_key}_prompt",
                    )
                    if st.button("Перегенерировать", key=f"{regen_key}_btn", type="primary"):
                        self._regenerate_single_image(
                            img_counter, regen_prompt, regen_style, file_path, article
                        )
            last = m.end()
        remaining = article[last:].strip()
        if remaining:
            st.markdown(self._clean_file_paths_from_text(remaining))

    def _regenerate_single_image(
        self,
        img_index: int,
        prompt: str,
        style: str,
        old_path: str | None,
        article: str,
    ) -> None:
        """Regenerate a single image and update the article in session state."""
        from illustration_pipeline import IllustrationPipeline
        pipeline = IllustrationPipeline()
        if not pipeline.nanobanana_api_key and not pipeline.dalle_api_key:
            st.error("API ключи NanoBanana / DALL-E / Gemini не настроены.")
            return
        errors: list = []
        with st.spinner(f"Генерация изображения ({style})..."):
            new_path = pipeline.generate_image_nanobanana(prompt, style=style, errors=errors)
            if not new_path and pipeline.dalle_api_key:
                new_path = pipeline.generate_image_dalle(prompt, size="512x512", style=style, errors=errors)
        if not new_path:
            st.error("Не удалось сгенерировать изображение. " + ("; ".join(errors[:2]) if errors else ""))
            return
        if old_path:
            new_abs = os.path.abspath(new_path).replace("\\", "/")
            old_abs = os.path.abspath(old_path).replace("\\", "/")
            for key in ("last_article_topic", "last_article_docs"):
                art = st.session_state.get(key, "")
                if art and old_abs in art:
                    st.session_state[key] = art.replace(
                        f"file:{old_abs}", f"file:{new_abs}"
                    )
            for key in ("_illustration_paths_topic", "_illustration_paths_docs"):
                paths = st.session_state.get(key, [])
                if paths and old_abs in [os.path.abspath(p).replace("\\", "/") for p in paths]:
                    st.session_state[key] = [
                        new_path if os.path.abspath(p).replace("\\", "/") == old_abs else p
                        for p in paths
                    ]
        st.success("Изображение обновлено.")
        st.rerun()

    def _split_by_sections(self, text: str, max_chunk: int) -> List[str]:
        """Делит текст по заголовкам ##, собирает куски не больше max_chunk. Блок «## Источники» всегда в конце."""
        text = text.strip()
        if not text or len(text) <= max_chunk:
            return [text] if text else []
        parts = re.split(r'(\n## )', text)
        sections = []
        buf = ""
        for i, p in enumerate(parts):
            if p == "\n## ":
                if buf.strip():
                    sections.append(buf.strip())
                buf = "\n## "
                continue
            buf += p
        if buf.strip():
            sections.append(buf.strip())
        if not sections:
            return [text]
        # В конец переносим только секцию с заголовком именно «## Источники» (не «Использованные источники» и т.п.)
        def is_sources_heading(section: str) -> bool:
            first_line = section.split("\n")[0].strip().lower()
            return first_line == "## источники"
        main_sections = []
        sources_section = None
        for s in sections:
            if is_sources_heading(s):
                sources_section = s
            else:
                main_sections.append(s)
        if sources_section:
            main_sections.append(sources_section)
        # Собираем в чанки не больше max_chunk
        chunks = []
        current = ""
        for sec in main_sections:
            if len(current) + len(sec) + 2 <= max_chunk and current:
                current += "\n\n" + sec
            else:
                if current:
                    chunks.append(current)
                current = sec
        if current:
            chunks.append(current)
        return chunks

    def _render_long_text(self, text: str) -> None:
        """Рендерит длинный текст: до _SINGLE_BLOCK_MAX_CHARS — одним блоком (одна прокрутка), иначе по секциям в одном iframe (одна прокрутка)."""
        if not text or not text.strip():
            return
        text = text.strip()
        max_safe = self._STREAM_DISPLAY_MAX_CHARS
        single_max = self._SINGLE_BLOCK_MAX_CHARS
        if len(text) <= single_max:
            self._render_article_html(text)
            return
        chunks = self._split_by_sections(text, max_safe)
        if len(chunks) == 1:
            self._render_article_html(chunks[0])
            return
        # Все чанки в одном HTML — одна область прокрутки вместо 2–3
        self._render_article_html_merged(chunks)

    def _render_article_html(self, article: str) -> None:
        """Рендерит один текстовый фрагмент Markdown → HTML в одной области с общим скроллом (высота по содержимому)."""
        if not article or not article.strip():
            return
        cleaned = self._clean_file_paths_from_text(article.strip())
        article_html = md.markdown(
            cleaned,
            extensions=["tables", "fenced_code", "sane_lists"],
        )
        full_html = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><meta name="color-scheme" content="light"><style>
html,body{{background:#ffffff!important;color:#1e293b!important;margin:0;padding:0}}
.article-display{{font-family:"Palatino Linotype","Book Antiqua",Georgia,serif;line-height:1.75;max-width:820px;color:#1e293b;padding:1.5rem 0;background:#ffffff}}
.article-display p{{margin-bottom:1em;text-align:justify;color:#1e293b}}
.article-display ul,.article-display ol{{margin:0.6em 0 1em 1.8em;padding-left:0.5em}}
.article-display li{{margin-bottom:0.35em;color:#1e293b}}
.article-display h1{{margin-top:0;margin-bottom:1em;font-size:1.5em;font-weight:700;color:#0f172a}}
.article-display h2{{margin-top:1.8em;margin-bottom:0.6em;font-size:1.25em;font-weight:700;color:#0f172a;border-bottom:1px solid #e2e8f0;padding-bottom:0.3em}}
.article-display h2:first-of-type{{margin-top:0}}
.article-display h3{{margin-top:1.2em;margin-bottom:0.4em;font-size:1.1em;font-weight:600;color:#334155}}
.article-display strong{{color:#0f172a;font-weight:600}}
.article-display em{{color:#334155;font-style:italic}}
.article-display a{{color:#1e40af}}
.article-display sup,.article-display sub{{color:#334155;font-size:0.85em}}
.article-display blockquote{{border-left:4px solid #94a3b8;margin:1em 0;padding-left:1em;color:#475569;font-style:italic;background:#f8fafc}}
</style></head><body><div class="article-display">{article_html}</div></body></html>"""
        height = min(1200, 400 + article.count("\n") * 20)
        components.html(full_html, height=height, scrolling=True)

    def _render_article_html_merged(self, chunks: List[str]) -> None:
        """Рендерит несколько чанков в одном iframe — одна область прокрутки."""
        if not chunks:
            return
        body_parts = []
        total_lines = 0
        for chunk in chunks:
            if not chunk or not chunk.strip():
                continue
            part_html = md.markdown(
                chunk.strip(),
                extensions=["tables", "fenced_code", "sane_lists"],
            )
            body_parts.append(f'<div class="article-display">{part_html}</div>')
            total_lines += chunk.count("\n")
        if not body_parts:
            return
        full_html = (
            '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><meta name="color-scheme" content="light"><style>'
            "html,body{background:#fff!important;color:#1e293b!important;margin:0;padding:0}"
            ".article-display{font-family:\"Palatino Linotype\",\"Book Antiqua\",Georgia,serif;line-height:1.75;max-width:820px;color:#1e293b;padding:1rem 0}"
            ".article-display p{margin-bottom:1em;text-align:justify}"
            ".article-display ul,.article-display ol{margin:0.6em 0 1em 1.8em}"
            ".article-display h2{margin-top:1.8em;margin-bottom:0.6em;font-size:1.25em;font-weight:700;border-bottom:1px solid #e2e8f0}"
            ".article-display h2:first-of-type{margin-top:0}"
            ".article-display h3{margin-top:1.2em;margin-bottom:0.4em;font-size:1.1em}"
            ".article-display strong{font-weight:600}"
            ".article-display a{color:#1e40af}"
            "</style></head><body>"
            + "".join(body_parts) +
            "</body></html>"
        )
        # Если объём слишком большой — fallback на отдельные блоки (избегаем RangeError)
        if len(full_html) > 32000:
            for chunk in chunks:
                self._render_article_html(chunk)
            return
        height = min(1600, 400 + total_lines * 18)
        try:
            components.html(full_html, height=height, scrolling=True)
        except Exception:
            for chunk in chunks:
                self._render_article_html(chunk)

    def _decode_data_uri(self, data_uri: str) -> Tuple[bytes, str]:
        """Декодирует data:image/...;base64,... в байты изображения."""
        try:
            if not data_uri.startswith("data:") or "," not in data_uri:
                return b"", ""
            header, b64 = data_uri.split(",", 1)
            mime = header.split(";")[0].replace("data:", "").strip()
            if "base64" not in header:
                return b"", mime
            return base64.b64decode(b64), mime
        except Exception:
            return b"", ""

    def _image_to_base64(self, path: str) -> Tuple[str, str]:
        """Читает изображение и возвращает (base64, mime_type) для вставки в HTML."""
        if not path or not os.path.exists(path):
            return "", "image/png"
        ext = os.path.splitext(path)[1].lower()
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        try:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii"), mime
        except Exception:
            return "", "image/png"

    def _find_marker_in_text(self, text: str, marker: str, fallback: str) -> Optional[Tuple[int, int]]:
        """Ищет маркер в тексте. Возвращает (start, end) или None."""
        for variant in (marker, fallback):
            if variant in text:
                start = text.index(variant)
                return (start, start + len(variant))
        marker_lo = marker.lower().replace(" ", "")
        pattern = re.compile(r"\[\s*ILLUSTRATION\s*_\s*(\d+)\s*\]", re.IGNORECASE)
        m = pattern.search(text)
        if m:
            return (m.start(), m.end())
        return None

    def _add_illustrations_to_article(
        self,
        article_with_markers: str,
        prompts_list: List[Dict[str, str]],
        progress_callback=None,
        illustration_style: str = "academic",
    ) -> Tuple[str, List[str], List[str]]:
        """
        Генерирует изображения через NanaBanana (или DALL-E как fallback) и вставляет в статью.
        Возвращает (article, image_paths, errors).
        """
        from illustration_pipeline import IllustrationPipeline
        pipeline = IllustrationPipeline()
        errors: List[str] = []
        if not pipeline.nanobanana_api_key and not pipeline.dalle_api_key:
            logger.info("Добавление иллюстраций: нет API ключей NanoBanana/DALL-E, пропускаем генерацию")
            return article_with_markers, [], errors
        result = article_with_markers
        image_paths: List[str] = []
        total = len(prompts_list)
        insertions: List[Tuple[int, str]] = []
        logger.info(f"Добавление иллюстраций: получено промптов: {total}")
        for i, item in enumerate(prompts_list):
            marker = (item.get("marker") or f"[ILLUSTRATION_{i+1}]").strip()
            prompt_english = item.get("prompt_english", "")
            caption_ru = item.get("caption_ru", f"Рисунок {i + 1}")
            if progress_callback:
                progress_callback(i + 1, total, caption_ru)
            logger.info(f"Иллюстрация {i+1}/{total}: маркер={marker}, подпись='{caption_ru[:60]}'")
            figure_label = f"Рис. {i + 1}. {caption_ru}"
            path = pipeline.generate_image_nanobanana(prompt_english, style=illustration_style, errors=errors)
            if not path and pipeline.dalle_api_key:
                path = pipeline.generate_image_dalle(prompt_english, size="512x512", style=illustration_style, errors=errors)
            fallback_marker = f"[ILLUSTRATION_{i+1}]"
            span = self._find_marker_in_text(result, marker, fallback_marker)
            if path:
                logger.info(f"Иллюстрация {i+1}: файл сгенерирован: {path}")
                image_paths.append(path)
                b64, mime = self._image_to_base64(path)
                if b64:
                    # Одна подпись в тексте + картинка; без дублирования курсивом
                    img_md = f'\n\n{figure_label}\n\n![{caption_ru}](data:{mime};base64,{b64})\n\n'
                else:
                    img_md = f'\n\n{figure_label} — изображение: {os.path.basename(path)}\n\n'
            else:
                logger.warning(f"Иллюстрация {i+1}: не удалось сгенерировать файл, вставляем только подпись")
                img_md = f"\n\n{figure_label}\n\n"
            if span is not None:
                start, end = span
                result = result[:start] + img_md + result[end:]
            else:
                insertions.append((i, img_md))
        if insertions:
            logger.info(f"Добавление иллюстраций: {len(insertions)} изображений вставлены по разделам (fallback)")
            result = self._insert_images_at_sections(result, insertions)
        logger.info(f"Добавление иллюстраций: всего сгенерировано файлов: {len(image_paths)}")
        return result, image_paths, errors

    def _insert_images_at_sections(self, text: str, insertions: List[Tuple[int, str]]) -> str:
        """Вставляет картинки/подписи после разделов ## (если маркеры не были найдены в тексте)."""
        section_re = re.compile(r"\n(## )(?!Содержание|Источники)([^\n]+)\n", re.IGNORECASE)
        matches = list(section_re.finditer(text))
        if not matches:
            for _, img_md in insertions:
                text += img_md
            return text
        out = []
        last_end = 0
        used = 0
        for m in matches:
            out.append(text[last_end : m.end()])
            if used < len(insertions):
                out.append(insertions[used][1])
                used += 1
            last_end = m.end()
        out.append(text[last_end:])
        for idx in range(used, len(insertions)):
            out.append(insertions[idx][1])
        return "".join(out)

    def _article_data_uri_to_file_refs(self, article: str, image_paths: List[str]) -> str:
        """Заменяет data:image/... на file:path в тексте статьи для хранения в session state (без тяжёлого base64)."""
        if not image_paths or "data:image/" not in article:
            return article
        pattern = re.compile(r'!\[(?P<alt>[^\]]*)\]\((data:image\/[^)]+)\)')
        result = []
        path_index = [0]
        def repl(m):
            alt = m.group("alt") or ""
            if path_index[0] < len(image_paths):
                path = image_paths[path_index[0]]
                path_index[0] += 1
                path_norm = os.path.abspath(path).replace("\\", "/")
                return f'![{alt}](file:{path_norm})'
            return m.group(0)
        return pattern.sub(repl, article)

    def _article_file_refs_to_data_uri(self, article: str) -> str:
        """Заменяет file:/abs/path на data:image/...;base64,... для экспорта в .md."""
        if not article or "](file:" not in article:
            return article
        pattern = re.compile(r'!\[(?P<alt>[^\]]*)\]\((file:[^)]+)\)')

        def repl(m):
            alt = (m.group("alt") or "").strip()
            raw = (m.group(2) or "").strip()
            path_raw = raw[5:] if raw.startswith("file:") else raw
            path_abs = path_raw if os.path.isabs(path_raw) else os.path.abspath(path_raw)
            if not os.path.isfile(path_abs):
                return m.group(0)
            b64, mime = self._image_to_base64(path_abs)
            if not b64:
                return m.group(0)
            return f'![{alt}](data:{mime};base64,{b64})'

        return pattern.sub(repl, article)

    def _render_markdown_preview(self, markdown_text: str, fullscreen: bool, blocks: Optional[List[Dict]] = None) -> None:
        """Рендерит красивое Markdown-превью прямо во время генерации."""
        header_html = md.markdown(
            self._format_markdown_preview(""),
            extensions=["tables", "fenced_code", "sane_lists"],
        )
        if blocks:
            block_html_parts = []
            image_paths = st.session_state.get("redrawn_image_paths", [])
            inserted_figures = set()
            for item in blocks:
                paraphrased = item.get("paraphrased", "")
                original = item.get("original", "")
                idx = item.get("index", 0)
                rendered = md.markdown(
                    paraphrased,
                    extensions=["tables", "fenced_code", "sane_lists"],
                )
                original_b64 = base64.b64encode(original.encode("utf-8")).decode("ascii")
                block_html_parts.append(
                    f'<section class="para-block" data-original="{original_b64}" data-index="{idx}">{rendered}</section>'
                )
                # Вставляем изображения после блоков со ссылками на рисунки
                for fig_num in _find_figure_refs_in_block(paraphrased):
                    if fig_num in inserted_figures:
                        continue
                    if fig_num - 1 < len(image_paths):
                        img_path = image_paths[fig_num - 1]
                        img_b64, mime = self._image_to_base64(img_path)
                        if img_b64:
                            block_html_parts.append(
                                f'<p class="fig-caption">Рисунок {fig_num}</p>'
                                f'<img src="data:{mime};base64,{img_b64}" '
                                f'style="max-width:100%; height:auto; border-radius:8px; margin:12px 0;" '
                                f'alt="Рисунок {fig_num}" />'
                            )
                            inserted_figures.add(fig_num)
            html_body = header_html + "".join(block_html_parts)
        else:
            html_body = md.markdown(
                self._format_markdown_preview(markdown_text),
                extensions=["tables", "fenced_code", "sane_lists"],
            )
        height = 880 if fullscreen else 560
        html = f"""
        <!DOCTYPE html>
        <html lang="ru">
        <head>
          <meta charset="utf-8" />
          <style>
            :root {{
              --page-width: 920px;
              --page-padding: 56px 72px;
              --bg: #f5f7fb;
              --ink: #0f172a;
              --muted: #475569;
              --accent: #2563eb;
              --rule: #e2e8f0;
            }}
            * {{ box-sizing: border-box; }}
            html, body {{
              height: 100%;
              margin: 0;
              padding: 0;
              background: var(--bg);
              color: var(--ink);
              font-family: "Palatino Linotype", "Book Antiqua", Palatino, "Times New Roman", serif;
            }}
            .wrap {{
              height: 100%;
              overflow: auto;
              padding: 24px 18px;
            }}
            .page {{
              background: #ffffff;
              border-radius: 18px;
              box-shadow: 0 18px 45px rgba(15, 23, 42, 0.12);
              max-width: var(--page-width);
              margin: 0 auto;
              padding: var(--page-padding);
            }}
            .para-block {{
              padding: 12px 10px;
              border-radius: 12px;
              cursor: pointer;
              transition: background 120ms ease, box-shadow 120ms ease;
            }}
            .para-block:hover {{
              background: rgba(37, 99, 235, 0.06);
              box-shadow: inset 0 0 0 1px rgba(37, 99, 235, 0.12);
            }}
            .fig-caption {{
              font-size: 14px;
              color: var(--muted);
              margin: 16px 0 4px 0;
            }}
            h1, h2, h3 {{
              font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Palatino, "Times New Roman", serif;
              letter-spacing: -0.01em;
            }}
            h1 {{
              font-size: 28px;
              margin: 0 0 18px 0;
            }}
            h2 {{
              font-size: 22px;
              margin: 28px 0 10px 0;
            }}
            h3 {{
              font-size: 18px;
              margin: 20px 0 8px 0;
            }}
            p {{
              font-size: 16px;
              line-height: 1.65;
              margin: 0 0 12px 0;
              color: var(--ink);
            }}
            em {{ color: var(--muted); }}
            hr {{
              border: none;
              border-top: 1px dashed var(--rule);
              margin: 34px 0;
            }}
            blockquote {{
              border-left: 3px solid var(--rule);
              padding-left: 14px;
              color: var(--muted);
              margin: 16px 0;
            }}
            ul, ol {{
              margin: 0 0 12px 20px;
            }}
            a {{
              color: var(--accent);
              text-decoration: none;
            }}
            code {{
              font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
              font-size: 0.9em;
              background: #f1f5f9;
              padding: 2px 6px;
              border-radius: 6px;
            }}
            pre code {{
              display: block;
              padding: 14px;
              overflow-x: auto;
            }}
            .modal {{
              position: fixed;
              inset: 0;
              background: rgba(15, 23, 42, 0.55);
              display: flex;
              align-items: center;
              justify-content: center;
              padding: 24px;
              z-index: 9999;
            }}
            .modal.hidden {{
              display: none;
            }}
            .modal-card {{
              background: #ffffff;
              border-radius: 16px;
              max-width: 820px;
              width: 100%;
              box-shadow: 0 20px 60px rgba(15, 23, 42, 0.2);
              padding: 24px;
              display: grid;
              gap: 12px;
            }}
            .modal-title {{
              font-size: 18px;
              font-weight: 600;
            }}
            .modal-pre {{
              background: #f8fafc;
              border: 1px solid #e2e8f0;
              border-radius: 12px;
              padding: 12px;
              max-height: 50vh;
              overflow: auto;
              white-space: pre-wrap;
              font-size: 14px;
              line-height: 1.5;
            }}
            .modal-actions {{
              display: flex;
              justify-content: flex-end;
            }}
            .modal-btn {{
              border: none;
              border-radius: 10px;
              padding: 8px 14px;
              background: #2563eb;
              color: #ffffff;
              font-weight: 600;
              cursor: pointer;
            }}
          </style>
        </head>
        <body>
          <div class="wrap">
            <article class="page">
              {html_body}
            </article>
          </div>
          <div id="orig-modal" class="modal hidden" role="dialog" aria-modal="true">
            <div class="modal-card">
              <div class="modal-title" id="modal-title">Исходный текст</div>
              <pre class="modal-pre" id="modal-body"></pre>
              <div class="modal-actions">
                <button class="modal-btn" id="modal-close">Закрыть</button>
              </div>
            </div>
          </div>
          <script>
            const modal = document.getElementById("orig-modal");
            const modalBody = document.getElementById("modal-body");
            const modalTitle = document.getElementById("modal-title");
            const closeBtn = document.getElementById("modal-close");
            const blocks = document.querySelectorAll(".para-block");

            function openModal(text, idx) {{
              modalTitle.textContent = idx ? `Исходный текст (блок ${{idx}})` : "Исходный текст";
              modalBody.textContent = text || "";
              modal.classList.remove("hidden");
            }}
            function closeModal() {{
              modal.classList.add("hidden");
            }}
            blocks.forEach((block) => {{
              block.addEventListener("click", () => {{
                const encoded = block.getAttribute("data-original") || "";
                const idx = block.getAttribute("data-index") || "";
                let decoded = "";
                try {{
                  decoded = decodeURIComponent(escape(atob(encoded)));
                }} catch (e) {{
                  decoded = "";
                }}
                openModal(decoded, idx);
              }});
            }});
            closeBtn.addEventListener("click", closeModal);
            modal.addEventListener("click", (e) => {{
              if (e.target === modal) closeModal();
            }});
          </script>
        </body>
        </html>
        """
        components.html(html, height=height, scrolling=True)

    def _extract_images_for_redraw(self, pdf_path: str) -> Tuple[List[Dict], List[str]]:
        """Извлекает изображения из PDF для перерисовки. Возвращает (extracted, paths_list)."""
        try:
            from illustration_pipeline import IllustrationPipeline
            pipeline = IllustrationPipeline()
        except Exception:
            return [], []
        if not pdf_path or not os.path.exists(pdf_path):
            return [], []
        extracted = pipeline.extract_images_from_pdf(pdf_path)
        if not extracted:
            return [], []
        paths_list = [img["file_path"] for img in extracted]
        return extracted, paths_list

    @staticmethod
    def _redraw_images_worker(extracted: List[Dict], paths_list: List[str]) -> None:
        """Воркер для фоновой перерисовки изображений (обновляет paths_list по месту)."""
        try:
            from illustration_pipeline import IllustrationPipeline
            pipeline = IllustrationPipeline()
        except Exception:
            return
        for i, img_info in enumerate(extracted):
            if not pipeline.nanobanana_api_key:
                break
            path, _ = pipeline.redraw_image_with_nanobanana(img_info)
            if path and os.path.exists(path):
                paths_list[i] = path

    def _render_redrawn_images_result(self, extracted: List[Dict], paths_list: List[str]) -> None:
        """Показывает секцию «Оригинал / Перерисовано» по уже готовым путям (без вызова API)."""
        st.markdown("---")
        st.subheader("Перерисованные изображения (этап перефразирования)")
        for idx, (img_info, final_path) in enumerate(zip(extracted, paths_list), 1):
            cols = st.columns(2)
            with cols[0]:
                st.image(img_info["file_path"], caption=f"Оригинал #{idx}", width="stretch")
            with cols[1]:
                if final_path and os.path.exists(final_path) and final_path != img_info["file_path"]:
                    st.image(final_path, caption=f"Перерисовано #{idx}", width="stretch")
                else:
                    st.info("Перерисовка недоступна или не удалась.")
                    st.image(img_info["file_path"], caption=f"Использован оригинал #{idx}", width="stretch")

    def _render_redrawn_images(self, pdf_path: str) -> List[str]:
        """
        Извлекает и перерисовывает изображения из PDF (синхронно, по одному).
        Используется при отключённой параллельной перерисовке.
        """
        extracted, paths_list = self._extract_images_for_redraw(pdf_path)
        if not extracted:
            st.markdown("---")
            st.subheader("Перерисованные изображения (этап перефразирования)")
            st.info("Изображения в PDF не найдены.")
            return []
        try:
            from illustration_pipeline import IllustrationPipeline
            pipeline = IllustrationPipeline()
        except Exception as e:
            st.error(f"Не удалось инициализировать модуль иллюстраций: {e}")
            return paths_list
        st.markdown("---")
        st.subheader("Перерисованные изображения (этап перефразирования)")
        if not pipeline.nanobanana_api_key:
            st.warning("NanoBanana API ключ не настроен. Показываю только оригинальные изображения.")
        for idx, img_info in enumerate(extracted, 1):
            cols = st.columns(2)
            with cols[0]:
                st.image(img_info["file_path"], caption=f"Оригинал #{idx}", width="stretch")
            with cols[1]:
                redrawn_path, redraw_error = None, None
                if pipeline.nanobanana_api_key:
                    redrawn_path, redraw_error = pipeline.redraw_image_with_nanobanana(img_info)
                else:
                    redraw_error = "NanoBanana (Gemini) API ключ не настроен. Укажите ключ в Настройках."
                if redrawn_path and os.path.exists(redrawn_path):
                    st.image(redrawn_path, caption=f"Перерисовано #{idx}", width="stretch")
                    paths_list[idx - 1] = redrawn_path
                else:
                    st.info(redraw_error or "Перерисовка недоступна или не удалась.")
        st.session_state.redrawn_image_paths = paths_list
        return paths_list

    def _extract_images_only(self, pdf_path: str) -> None:
        """Извлекает изображения из PDF без перерисовки (для вставки в markdown/PDF)."""
        try:
            from illustration_pipeline import IllustrationPipeline
            pipeline = IllustrationPipeline()
        except Exception:
            st.session_state.redrawn_image_paths = []
            return

        if not pdf_path or not os.path.exists(pdf_path):
            st.session_state.redrawn_image_paths = []
            return

        extracted = pipeline.extract_images_from_pdf(pdf_path)
        paths = [img["file_path"] for img in extracted if img.get("file_path") and os.path.exists(img["file_path"])]
        st.session_state.redrawn_image_paths = paths
        if paths:
            st.caption(f"Извлечено {len(paths)} изображений из PDF (будут вставлены в текст по ссылкам «рис. N»).")

    def render(self, tab):
        """Отображение вкладки перефразирования текста"""
        with tab:
            st.header("Перефразирование и генерация статей")

            mode = st.radio(
                "Режим работы",
                [
                    "Перефразирование файла",
                    "Генерация статьи по нескольким документам",
                    "Генерация статьи по теме",
                ],
            )

            # История генерации (для режимов генерации статей)
            if mode in ("Генерация статьи по нескольким документам", "Генерация статьи по теме"):
                self._render_article_history()

            # Пресеты настроек модели: выбор сохранённого и сохранение текущих
            st.markdown("##### Пресеты настроек модели")
            presets = settings_manager.get_model_presets()
            preset_names = sorted(presets.keys())
            _preset_options = ["— Не менять —"] + preset_names if preset_names else ["— Нет сохранённых пресетов —"]
            col_sel, col_btn = st.columns([2, 1])
            with col_sel:
                _sel = st.selectbox(
                    "Сохранённые пресеты",
                    options=_preset_options,
                    index=0,
                    help="Применить сохранённый набор настроек (провайдер, модель, температура, стиль статьи).",
                    key="text_tab_preset_select",
                )
            with col_btn:
                apply_clicked = st.button("Применить пресет", key="text_tab_apply_preset", disabled=not preset_names)
            if apply_clicked and _sel and _sel != "— Не менять —" and _sel != "— Нет сохранённых пресетов —":
                if settings_manager.load_model_preset(_sel):
                    st.success(f"Пресет «{_sel}» применён.")
                    st.rerun()
            with st.expander("Сохранить текущие настройки как пресет", expanded=False):
                preset_name = st.text_input(
                    "Название пресета",
                    placeholder="Например: Статьи на Gemini, Быстрый перефраз…",
                    key="text_tab_new_preset_name",
                )
                if st.button("Сохранить пресет", key="text_tab_save_preset"):
                    if (preset_name or "").strip():
                        settings_manager.save_model_preset(preset_name.strip())
                        st.success(f"Пресет «{preset_name.strip()}» сохранён.")
                        st.rerun()
                    else:
                        st.error("Введите название пресета.")

            # Выбор провайдера LLM (для генерации статей и перефразирования)
            st.markdown("##### Провайдер LLM")
            provider_options = [
                ("openai", "OpenAI (ChatGPT)"),
                ("deepseek", "DeepSeek"),
                ("gemini", "Google Gemini"),
            ]
            options_keys = [p[0] for p in provider_options]
            options_labels = {p[0]: p[1] for p in provider_options}
            current_provider = get_llm_provider()
            try:
                provider_index = options_keys.index(current_provider)
            except ValueError:
                provider_index = 0
            selected_provider = st.radio(
                "Модель для генерации статей и перефразирования",
                options=options_keys,
                format_func=lambda x: options_labels[x],
                index=provider_index,
                horizontal=True,
                key="text_tab_llm_provider",
            )
            if selected_provider != current_provider:
                set_llm_provider(selected_provider)
                st.success(f"Провайдер изменён на {options_labels[selected_provider]}")
                st.rerun()

            # Настройка уровня перефразирования / генерации
            temperature = self._render_temperature_controls()
            style_controls = self._render_style_controls()

            # Переключатель добавления информации о новых исследованиях
            include_research = st.checkbox(
                "Добавлять информацию о новых исследованиях из разных известных источников",
                value=settings_manager.get("include_research", False),
                help="Включает добавление актуальной научной информации (для перефразирования и статей).",
            )
            settings_manager.set("include_research", include_research)

            add_illustrations = st.checkbox(
                "Добавить иллюстрации в статью (NanaBanana 2)",
                value=settings_manager.get("add_article_illustrations", False),
                help="После генерации статьи будет создано 3–5 иллюстраций по тексту и вставлено в markdown. Требуется API ключ NanoBanana в настройках.",
            )
            settings_manager.set("add_article_illustrations", add_illustrations)

            # Выбор стиля иллюстраций (под чекбоксом NanaBanana)
            _style_labels = {
                "minimalist": "Минималистичный — чистые формы, минимум деталей, ограниченная палитра",
                "cartoon": "Мультяшный (cartoon) — упрощённые формы, яркие цвета",
                "academic": "Академический (scientific) — строгая научная подача, нейтральные цвета",
                "realistic": "Реалистичный — максимально близко к реальности",
                "semi_realistic": "Полуреалистичный — баланс реализма и графики",
                "diagrammatic": "Схематический — схемы, линии, стрелки, условные обозначения",
                "infographic": "Инфографический — диаграммы, подписи, цифры, иконки",
                "isometric": "Изометрический — псевдо-3D изометрия",
                "3d": "3D-визуализация — объёмные модели, свет и тени",
                "editorial": "Редакционный — художественная, метафорическая подача",
            }
            _style_options = list(_style_labels.keys())
            _current_style = settings_manager.get("illustration_style", "academic")
            _style_idx = _style_options.index(_current_style) if _current_style in _style_options else 2
            illustration_style = st.selectbox(
                "Стиль иллюстраций",
                options=_style_options,
                index=_style_idx,
                format_func=lambda k: _style_labels.get(k, k),
                help="Стиль генерации изображений для статьи.",
                key="text_tab_illustration_style",
            )
            if illustration_style != settings_manager.get("illustration_style", "academic"):
                settings_manager.set("illustration_style", illustration_style)

            # Информация об API ключе
            self._render_api_key_status()

            # Настройки Markdown-превью (показываем всегда, даже до старта)
            st.markdown("##### Markdown-превью")
            fullscreen_preview = st.toggle("Просмотр на всю страницу", value=False, key="md_fullscreen_toggle")

            if mode == "Перефразирование файла":
                # Загрузка файла
                uploaded_file = self.file_uploader.render(
                    label="Выберите файл для обработки",
                    allowed_types=["pdf", "txt", "md", "docx"],
                    help_text="Поддерживаемые форматы: PDF, TXT, MD, DOCX",
                )

                # Ввод темы
                theme = st.text_input(
                    "Тема текста",
                    value=DEFAULT_THEME,
                    help="Укажите тематику текста для более точного перефразирования",
                    key="paraphrase_theme",
                )

                redraw_images = st.checkbox(
                    "Перерисовывать изображения во время перефразирования (только PDF)",
                    value=False,
                    help="Извлекает изображения из PDF и перерисовывает их сразу на этапе перефразирования.",
                )

                start_clicked = st.button(
                    "Начать перефразирование",
                    type="primary",
                    width="stretch",
                    key="start_paraphrase_btn",
                )
                if start_clicked:
                    self._process_file(
                        uploaded_file,
                        theme,
                        temperature,
                        include_research,
                        fullscreen_preview,
                        redraw_images,
                        style_controls,
                    )
                else:
                    # Предпросмотр до запуска (красивый шаблон)
                    preview_text = st.session_state.get("paraphrased_text") or (
                        "### Предпросмотр\n\n"
                        "Здесь будет отображаться красиво оформленный Markdown в реальном времени "
                        "после запуска перефразирования."
                    )
                    preview_blocks = st.session_state.get("paraphrased_blocks") or []
                    self._render_markdown_preview(preview_text, fullscreen_preview, preview_blocks)

            elif mode == "Генерация статьи по нескольким документам":
                st.markdown("#### Генерация статьи по нескольким документам")
                article_theme_docs = st.text_input(
                    "Тема статьи",
                    value="",
                    help="Например: Современные подходы к диагностике и лечению инсульта",
                    key="article_theme_docs",
                )
                article_tz_docs = st.text_area(
                    "ТЗ (что именно нужно в статье)",
                    value="",
                    height=120,
                    help="Например: сделать упор на клинические рекомендации, сравнить методы, добавить критерии диагностики, включить алгоритмы, исключить фарм-бренды и т.д.",
                    key="article_tz_docs",
                )
                sources_docx = st.file_uploader(
                    "Загрузить DOCX со списком статей/источников (необязательно)",
                    type=["docx"],
                    accept_multiple_files=False,
                    help="Если у вас есть документ Word со списком источников (PMID/DOI/ссылки/названия), загрузите его — текст будет извлечён и добавлен в поле ниже.",
                    key="required_articles_docx",
                )
                if sources_docx is not None:
                    try:
                        docx_bytes = sources_docx.getvalue()
                        docx_hash = hashlib.sha256(docx_bytes).hexdigest()
                        if docx_hash != st.session_state.get("_required_articles_docx_hash"):
                            import docx  # python-docx

                            d = docx.Document(BytesIO(docx_bytes))
                            extracted = "\n".join(
                                p.text.strip() for p in d.paragraphs if p.text and p.text.strip()
                            ).strip()
                            extracted = re.sub(r"\n{3,}", "\n\n", extracted).strip()
                            if extracted:
                                prev = (st.session_state.get("required_articles_docs") or "").strip()
                                combined = (prev + "\n\n" + extracted).strip() if prev else extracted
                                st.session_state["required_articles_docs"] = combined
                                st.session_state["_required_articles_docx_hash"] = docx_hash
                                st.session_state["_required_articles_docx_status"] = (
                                    f"Из DOCX извлечено {len(extracted)} символов и добавлено в поле источников."
                                )
                                st.rerun()
                            else:
                                st.session_state["_required_articles_docx_hash"] = docx_hash
                                st.session_state["_required_articles_docx_status"] = (
                                    "DOCX загружен, но текст источников извлечь не удалось (пустые абзацы)."
                                )
                    except Exception as e:
                        st.session_state["_required_articles_docx_status"] = f"Не удалось прочитать DOCX: {e}"

                if st.session_state.get("_required_articles_docx_status"):
                    st.caption(st.session_state["_required_articles_docx_status"])
                required_articles_docs = st.text_area(
                    "Статьи/источники, которые нужно использовать (необязательно)",
                    value="",
                    height=140,
                    help="Вставьте список PMID/DOI/ссылок или просто названия статей (по одной строке). Модель будет стараться опираться на них в тексте и в разделе «Источники».",
                    key="required_articles_docs",
                )
                st.markdown("##### Изображения из загруженных PDF")
                use_existing_images_docs = st.checkbox(
                    "Вставлять изображения из PDF в статью",
                    value=False,
                    help="Извлекает изображения из загруженных PDF и расставляет их по разделам статьи.",
                    key="use_existing_images_docs",
                )
                improve_existing_images_docs = st.checkbox(
                    "Улучшать изображения (AI) перед вставкой",
                    value=False,
                    help="Перерисовывает/улучшает извлечённые изображения (img2img через Gemini) и затем вставляет в статью.",
                    key="improve_existing_images_docs",
                    disabled=not use_existing_images_docs,
                )
                num_plan_steps_docs = st.slider(
                    "Количество пунктов плана",
                    min_value=5,
                    max_value=20,
                    value=settings_manager.get("plan_steps", 10),
                    step=1,
                    help="Число разделов в плане статьи (влияет на структуру и поиск в PubMed).",
                    key="article_plan_steps_docs",
                )
                if num_plan_steps_docs != settings_manager.get("plan_steps", 10):
                    settings_manager.set("plan_steps", int(num_plan_steps_docs))
                # Кнопки скачивания под темой (показываются после генерации)
                if st.session_state.get("last_article_docs"):
                    if st.session_state.get("_ill_diag_docs"):
                        st.info(st.session_state["_ill_diag_docs"])
                    self._render_article_download_buttons(st.session_state["last_article_docs"], "docs")
                    st.markdown("---")
                    st.markdown("### Сгенерированная статья по документам")
                    self._render_article_display(st.session_state["last_article_docs"])
                docs = st.file_uploader(
                    "Выберите один или несколько файлов (PDF / TXT / MD / DOCX)",
                    type=["pdf", "txt", "md", "docx"],
                    accept_multiple_files=True,
                    key="article_files",
                )
                if st.button("Сгенерировать статью по документам", type="primary", width="stretch"):
                    self._generate_article_from_files(
                        docs,
                        article_theme_docs,
                        article_tz_docs,
                        required_articles_docs,
                        temperature,
                        include_research,
                        style_controls,
                        add_illustrations,
                        num_plan_steps_docs,
                        use_existing_images_docs,
                        improve_existing_images_docs,
                    )

            else:
                st.markdown("#### Генерация статьи только по теме")
                article_theme = st.text_input(
                    "Тема статьи",
                    value="",
                    help="Например: Патофизиология хронической сердечной недостаточности",
                    key="article_theme_topic",
                )
                num_plan_steps = st.slider(
                    "Количество пунктов плана",
                    min_value=5,
                    max_value=20,
                    value=settings_manager.get("plan_steps", 10),
                    step=1,
                    help="Число разделов в плане статьи (влияет на структуру и поиск в PubMed).",
                    key="article_plan_steps",
                )
                if num_plan_steps != settings_manager.get("plan_steps", 10):
                    settings_manager.set("plan_steps", int(num_plan_steps))
                # Кнопки скачивания под темой (показываются после генерации)
                if st.session_state.get("last_article_topic"):
                    # Диагностика иллюстраций (сохраняется в session_state, переживает st.rerun)
                    if st.session_state.get("_ill_diag"):
                        st.info(st.session_state["_ill_diag"])
                    self._render_article_download_buttons(st.session_state["last_article_topic"], "topic")
                    st.markdown("---")
                    st.markdown("### Сгенерированная статья по теме")
                    self._render_article_display(st.session_state["last_article_topic"])
                if st.button("Сгенерировать статью по теме", type="primary", width="stretch"):
                    self._generate_article_from_topic(article_theme, temperature, include_research, style_controls, add_illustrations, num_plan_steps)

    def _render_temperature_controls(self):
        """Отображение элементов управления температурой. Состояние слайдера сохраняется в session_state и в настройках."""
        st.markdown("#### Настройка стиля перефразирования")

        # Инициализация из настроек, чтобы при первом заходе показывать сохранённое значение
        _key = "text_tab_temperature"
        if _key not in st.session_state:
            st.session_state[_key] = float(settings_manager.get("temperature", DEFAULT_TEMPERATURE))

        def _on_temperature_change():
            if _key in st.session_state:
                val = st.session_state[_key]
                settings_manager.set("temperature", val)

        col1, col2 = st.columns([3, 1])
        with col1:
            temperature = st.slider(
                "Уровень творческого переформулирования (теплота)",
                min_value=MIN_TEMPERATURE,
                max_value=MAX_TEMPERATURE,
                value=st.session_state[_key],
                step=0.1,
                key=_key,
                on_change=_on_temperature_change,
                help="Низкие значения (0.0–0.3): точное сохранение смысла. Средние (0.4–0.6): баланс. Высокие (0.7–1.0): более творческое переформулирование. Значение применяется к перефразированию и генерации статей."
            )
        with col2:
            st.metric("Температура", f"{temperature:.1f}")

        # Описание выбранного уровня
        if temperature <= 0.3:
            st.info("Консервативный режим: максимальное сохранение оригинальной структуры и терминологии")
        elif temperature <= 0.6:
            st.info("Сбалансированный режим: умеренное перефразирование с сохранением академического стиля")
        else:
            st.info("Творческий режим: более вариативное переформулирование с сохранением смысла")

        return temperature

    def _get_style_controls_from_settings(self) -> dict:
        """Читает актуальные параметры стиля из настроек (для передачи в TextProcessor)."""
        def _clamp(val) -> int:
            try:
                v = int(val)
            except (TypeError, ValueError):
                v = 3
            return max(1, min(5, v))
        def _clamp_readability(val) -> int:
            try:
                v = int(val)
            except (TypeError, ValueError):
                v = 3
            return max(1, min(7, v))
        return {
            "science": _clamp(settings_manager.get("style_science", 3)),
            "depth": _clamp(settings_manager.get("style_depth", 3)),
            "accuracy": _clamp(settings_manager.get("style_accuracy", 3)),
            "readability": _clamp_readability(settings_manager.get("style_readability", 3)),
            "source_quality": _clamp(settings_manager.get("style_source_quality", 3)),
        }

    def _render_style_controls(self) -> dict:
        """Пять параметров управления статьей с оценкой 1-5 звезд."""
        st.markdown("#### Управление качеством статьи")
        st.caption("Поставьте 1–5 звёзд (читаемость — до 7). Значения применяются к генерации статей.")

        def star_label(value: int) -> str:
            return "★" * value + "☆" * (5 - value)

        def star_label_readability(value: int) -> str:
            return "★" * value + "☆" * (7 - value)

        def init_key(key: str, setting_key: str):
            if key not in st.session_state:
                st.session_state[key] = int(settings_manager.get(setting_key, 3))

        init_key("style_science", "style_science")
        init_key("style_depth", "style_depth")
        init_key("style_accuracy", "style_accuracy")
        init_key("style_readability", "style_readability")
        init_key("style_source_quality", "style_source_quality")

        def _save_style_on_change():
            for k, sk in [
                ("style_science", "style_science"),
                ("style_depth", "style_depth"),
                ("style_accuracy", "style_accuracy"),
                ("style_readability", "style_readability"),
                ("style_source_quality", "style_source_quality"),
            ]:
                if k in st.session_state:
                    settings_manager.set(sk, int(st.session_state[k]))

        col1, col2 = st.columns(2)
        with col1:
            science = st.select_slider(
                "Научность",
                options=[1, 2, 3, 4, 5],
                value=st.session_state["style_science"],
                format_func=star_label,
                key="style_science",
                on_change=_save_style_on_change,
            )
            depth = st.select_slider(
                "Глубина",
                options=[1, 2, 3, 4, 5],
                value=st.session_state["style_depth"],
                format_func=star_label,
                key="style_depth",
                on_change=_save_style_on_change,
            )
            accuracy = st.select_slider(
                "Точность",
                options=[1, 2, 3, 4, 5],
                value=st.session_state["style_accuracy"],
                format_func=star_label,
                key="style_accuracy",
                on_change=_save_style_on_change,
            )
        with col2:
            readability = st.select_slider(
                "Читаемость",
                options=[1, 2, 3, 4, 5, 6, 7],
                value=min(7, max(1, int(st.session_state.get("style_readability", 3)))),
                format_func=star_label_readability,
                key="style_readability",
                on_change=_save_style_on_change,
            )
            source_quality = st.select_slider(
                "Качество источников",
                options=[1, 2, 3, 4, 5],
                value=st.session_state["style_source_quality"],
                format_func=star_label,
                key="style_source_quality",
                on_change=_save_style_on_change,
            )
            # Количество токенов и выбор модели — в том же блоке
            _mta = settings_manager.get("max_tokens_article", 32768)
            max_tokens_article = st.slider(
                "Макс. токенов статьи",
                min_value=100,
                max_value=65536,
                value=slider_value_for_step(_mta, 100, 65536, 1024),
                step=1024,
                help="Ограничивает длину статьи. 32768+ — полные статьи без обрыва.",
                key="text_tab_max_tokens_article",
            )
            if max_tokens_article != settings_manager.get("max_tokens_article", 32768):
                settings_manager.set("max_tokens_article", int(max_tokens_article))
            provider = get_llm_provider()
            if provider == "gemini":
                gemini_models = [
                    "gemini-2.5-flash",
                    "gemini-2.5-flash-lite",
                    "gemini-2.5-pro",
                    "gemini-3-flash-preview",
                    "gemini-3-pro-preview",
                    "gemini-3.1-pro-preview",
                    "gemini-3.1-flash-lite-preview",
                ]
                current_model = settings_manager.get("gemini_model", "gemini-2.5-flash")
                idx = gemini_models.index(current_model) if current_model in gemini_models else 0
                selected_model = st.selectbox(
                    "Модель Gemini",
                    options=gemini_models,
                    index=idx,
                    help="2.5-flash — быстро, 2.5-pro — сложные статьи.",
                    key="text_tab_gemini_model",
                )
                if selected_model != current_model:
                    settings_manager.set("gemini_model", selected_model)
            elif provider == "deepseek":
                deepseek_models = ["deepseek-chat", "deepseek-reasoner"]
                current_model = settings_manager.get("deepseek_model", "deepseek-chat")
                idx = deepseek_models.index(current_model) if current_model in deepseek_models else 0
                selected_model = st.selectbox(
                    "Модель DeepSeek",
                    options=deepseek_models,
                    index=idx,
                    key="text_tab_deepseek_model",
                )
                if selected_model != current_model:
                    settings_manager.set("deepseek_model", selected_model)
            else:
                current_model = settings_manager.get("model", "gpt-4o")
                openai_models = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4"]
                idx = openai_models.index(current_model) if current_model in openai_models else 0
                selected_model = st.selectbox(
                    "Модель OpenAI",
                    options=openai_models,
                    index=idx,
                    key="text_tab_openai_model",
                )
                if selected_model != current_model:
                    settings_manager.set("model", selected_model)

        # Сохраняем в настройки (не трогаем session_state — виджеты с key= уже обновляют его)
        settings_manager.set("style_science", science)
        settings_manager.set("style_depth", depth)
        settings_manager.set("style_accuracy", accuracy)
        settings_manager.set("style_readability", readability)
        settings_manager.set("style_source_quality", source_quality)

        return {
            "science": science,
            "depth": depth,
            "accuracy": accuracy,
            "readability": readability,
            "source_quality": source_quality,
        }

    def _ensure_api_key(self) -> Optional[str]:
        """Проверяет наличие активного API ключа и возвращает его или None."""
        if not has_active_api_key():
            provider = get_llm_provider()
            provider_name = {"deepseek": "DeepSeek", "gemini": "Gemini"}.get(provider, "OpenAI")
            st.error(f"API ключ {provider_name} не настроен. Пожалуйста, настройте его в разделе 'Настройки'")
            return None
        provider = get_llm_provider()
        if provider == "deepseek":
            return get_deepseek_api_key()
        if provider == "gemini":
            return get_gemini_api_key()
        return get_api_key()

    def _generate_article_from_files(
        self,
        files,
        theme: str,
        tz: str,
        required_articles: str,
        temperature: float,
        include_research: bool,
        style_controls: dict,
        add_illustrations: bool = False,
        num_plan_steps: int = 10,
        use_existing_images: bool = False,
        improve_existing_images: bool = False,
    ) -> None:
        """Генерация статьи по теме на основе нескольких загруженных документов."""
        if not files:
            st.error("Пожалуйста, загрузите хотя бы один файл-источник.")
            return
        if not theme or not theme.strip():
            st.error("Пожалуйста, укажите тему статьи.")
            return

        api_key = self._ensure_api_key()
        if not api_key:
            return

        settings_manager.set("temperature", temperature)
        settings_manager.set("include_research", include_research)

        # Берём актуальные параметры стиля из настроек (слайдеры уже сохранили их в settings_manager)
        style_controls = self._get_style_controls_from_settings()

        progress = ProgressDisplay()
        audience = "подготовленная аудитория"
        tz_clean = (tz or "").strip()
        required_clean = (required_articles or "").strip()
        required_pmids: List[str] = []
        required_pmids_abstracts: Dict[str, str] = {}
        if required_clean:
            try:
                required_pmids = sorted(set(re.findall(r"\b\d{6,9}\b", required_clean)))
                # Подтягиваем аннотации заранее: это дешёвый запрос и сильно повышает шанс,
                # что модель реально опрётся на указанные пользователем статьи.
                if required_pmids:
                    required_pmids_abstracts = fetch_abstracts_for_pmids(required_pmids[:25])
            except Exception:
                required_pmids = []
                required_pmids_abstracts = {}
        try:
            progress.start("Подготовка исходных текстов...")
            processor = TextProcessor(
                api_key,
                temperature=temperature,
                include_research=include_research,
                style_controls=style_controls,
            )

            source_texts: List[str] = []
            extracted_images: List[Dict] = []
            extracted_image_paths: List[str] = []
            total = len(files)
            for idx, f in enumerate(files, 1):
                progress.update_progress(
                    min(0.4, 0.1 + 0.25 * idx / max(total, 1)),
                    f"Извлечение текста из файла {idx}/{total}: {getattr(f, 'name', '')}",
                )
                with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{f.name}") as tmp:
                    tmp.write(f.getvalue())
                    tmp_path = tmp.name
                try:
                    text = processor.read_input_file(tmp_path)
                    if text:
                        source_texts.append(text)
                    # Опционально: извлекаем изображения из PDF
                    if use_existing_images and str(f.name).lower().endswith(".pdf"):
                        try:
                            from illustration_pipeline import IllustrationPipeline
                            pipeline = IllustrationPipeline()
                            imgs = pipeline.extract_images_from_pdf(tmp_path)
                            if imgs:
                                extracted_images.extend(imgs)
                                for img in imgs:
                                    p = img.get("file_path")
                                    if p and os.path.exists(p):
                                        extracted_image_paths.append(p)
                        except Exception:
                            pass
                finally:
                    try:
                        os.unlink(tmp_path)
                    except:
                        pass

            if not source_texts:
                st.error("Не удалось извлечь текст ни из одного из загруженных файлов.")
                return
            # Шаг 1: Генерация плана
            progress.start("Шаг 1/3: Генерация плана статьи...")
            plan_theme = theme.strip()
            if tz_clean:
                plan_theme = f"{plan_theme}. ТЗ: {tz_clean}"
            if required_clean:
                plan_theme = f"{plan_theme}. Используй источники: {required_clean[:4000]}"
            plan = processor.generate_article_plan(
                theme=plan_theme,
                audience=audience,
                num_plan_steps=num_plan_steps,
            )

            if not plan:
                # Fallback: старый режим по документам
                progress.update_message("Не удалось сгенерировать план. Используем fallback (стриминг).")
                st.markdown("---")
                st.markdown("### Сгенерированная статья по документам")
                article_placeholder = st.empty()
                article = ""
                last_rendered_len = 0
                for chunk in processor.generate_article_stream(theme=theme, source_texts=source_texts):
                    article += chunk
                    if len(article) - last_rendered_len >= self._STREAM_UPDATE_INTERVAL_CHARS:
                        with article_placeholder.container():
                            self._render_article_display(article, max_chars=self._STREAM_DISPLAY_MAX_CHARS)
                        last_rendered_len = len(article)
                with article_placeholder.container():
                    self._render_article_display(article)
                if include_research:
                    try:
                        pubmed_block = fetch_pubmed_summaries(theme.strip(), max_results=5)
                        if pubmed_block:
                            article += "\n\n## Современные исследования (PubMed)\n\n" + pubmed_block
                    except Exception:
                        pass
                _diag_parts = []
                if add_illustrations and (get_nanobanana_api_key() or get_dalle_api_key()):
                    try:
                        _diag_parts.append("Чекбокс иллюстраций: ВКЛ")
                        nb_key = get_nanobanana_api_key()
                        da_key = get_dalle_api_key()
                        _diag_parts.append(f"NanoBanana ключ: {'задан (' + nb_key[:8] + '…)' if nb_key else 'НЕТ'}")
                        _diag_parts.append(f"DALL-E ключ: {'задан (' + da_key[:8] + '…)' if da_key else 'НЕТ'}")
                        progress.update_progress(0.7, "Генерация промптов для иллюстраций...")
                        article_with_markers, prompts_list = processor.generate_article_image_prompts(article, num_images=5)
                        _diag_parts.append(f"Промптов от LLM: {len(prompts_list)}")
                        if not prompts_list:
                            article_with_markers, prompts_list = processor._fallback_illustration_markers(article, num_images=5)
                            _diag_parts.append(f"Промптов после fallback: {len(prompts_list)}")
                        if not prompts_list:
                            _diag_parts.append("РЕЗУЛЬТАТ: промпты пустые — изображения не генерировались")
                        else:
                            def _ill_progress(i: int, t: int, cap: str):
                                progress.update_progress(0.7 + 0.25 * i / max(t, 1), f"Иллюстрация {i}/{t}: {cap[:40]}…")
                            _ill_style = settings_manager.get("illustration_style", "academic")
                            article_final, image_paths, ill_errors = self._add_illustrations_to_article(
                                article_with_markers, prompts_list, progress_callback=_ill_progress, illustration_style=_ill_style
                            )
                            _diag_parts.append(f"Сгенерировано файлов изображений: {len(image_paths)}")
                            if ill_errors:
                                _diag_parts.append("Ошибки API: " + "; ".join(ill_errors[:3]))
                            st.session_state["_illustration_paths_docs"] = image_paths
                            if not image_paths:
                                _diag_parts.append("РЕЗУЛЬТАТ: API не вернул ни одного изображения. Проверьте ключи и лимиты.")
                            else:
                                _diag_parts.append(f"РЕЗУЛЬТАТ: вставлено {len(image_paths)} изображений")
                            article = article_final
                    except Exception as e:
                        _diag_parts.append(f"ОШИБКА иллюстраций: {e}")
                elif add_illustrations and not get_nanobanana_api_key() and not get_dalle_api_key():
                    _diag_parts.append("Чекбокс иллюстраций: ВКЛ, но API ключи NanoBanana и DALL-E не заданы в Настройках!")
                elif not add_illustrations:
                    _diag_parts.append("Чекбокс иллюстраций: ВЫКЛ — генерация картинок не запускалась.")
                st.session_state["_ill_diag_docs"] = " | ".join(_diag_parts) if _diag_parts else ""
                st.session_state["last_article_docs"] = article
                try:
                    add_article_to_history(
                        theme=theme or "Без темы",
                        article_text=article,
                        source="docs",
                        created_by=st.session_state.get("username", "admin"),
                    )
                except Exception:
                    pass
                progress.complete("Статья успешно сгенерирована (fallback).")
                st.rerun()
                return

            # План
            with st.expander("📋 План выполнения", expanded=True):
                for i, step in enumerate(plan, 1):
                    queries = step.get("searchQueries") or []
                    q_parts = [str(q)[:50] + ("…" if len(str(q)) > 50 else "") for q in queries[:2]]
                    q_preview = ", ".join(q_parts) if q_parts else ""
                    st.markdown(f"**{i}. {step.get('step', '')}** — {step.get('description', '')}")
                    if q_preview:
                        st.caption(f"Поиск: {q_preview}")

            # Шаг 2: Поиск источников (только при включённых исследованиях)
            search_context, sources_list = "", ""
            if include_research:
                progress.update_progress(0.3, "Шаг 2/3: Поиск в PubMed...")
                search_context, sources_list = processor.execute_article_searches(
                    plan, theme=theme.strip(), max_chars=12000
                )
            st.session_state["_sources_list_docs"] = sources_list or ""

            # Добавляем контекст из документов
            docs_joined = "\n\n---\n\n".join(t.strip() for t in source_texts if t and t.strip())
            if len(docs_joined) > 8000:
                docs_joined = docs_joined[:8000] + "\n\n[Текст документов обрезан по длине]"
            if docs_joined:
                search_context = f"**Исходные документы:**\n{docs_joined}\n\n" + (search_context or "")
            if tz_clean:
                search_context = f"**ТЗ пользователя:**\n{tz_clean}\n\n" + (search_context or "")
            if required_clean:
                extra = ""
                if required_pmids:
                    blocks = []
                    for pmid in required_pmids[:25]:
                        ab = (required_pmids_abstracts.get(pmid) or "").strip()
                        if ab:
                            ab = ab[:1200] + ("…" if len(ab) > 1200 else "")
                            blocks.append(f"- PMID: {pmid}\n  Аннотация: {ab}")
                        else:
                            blocks.append(f"- PMID: {pmid}")
                    extra = "\n\n**Аннотации по указанным PMID (PubMed):**\n" + "\n".join(blocks) + "\n"
                search_context = (
                    f"**Обязательные источники (используй и процитируй):**\n{required_clean}\n{extra}\n"
                    + (search_context or "")
                )

            # Шаг 3: Генерация статьи
            progress.update_progress(0.5, "Шаг 3/3: Генерация статьи (стриминг)...")
            st.markdown("---")
            st.markdown("### Сгенерированная статья по документам")
            article_placeholder = st.empty()
            article = ""
            last_rendered_len = 0
            for chunk in processor.generate_article_final_stream(
                theme=theme.strip(),
                plan=plan,
                search_context=search_context,
                audience=audience,
                sources_list=sources_list,
            ):
                article += chunk
                if len(article) - last_rendered_len >= self._STREAM_UPDATE_INTERVAL_CHARS:
                    with article_placeholder.container():
                        self._render_article_display(article, max_chars=self._STREAM_DISPLAY_MAX_CHARS)
                    last_rendered_len = len(article)
            # Пост-процессинг для гарантии: содержание в начале + раздел Источники
            if include_research and sources_list and sources_list.strip():
                sources_txt = sources_list.strip()
                # Приводим заголовок к единому виду
                article = re.sub(
                    r"^##\s*источники.*$",
                    "## Источники",
                    article,
                    flags=re.IGNORECASE | re.MULTILINE,
                )

                # Заменяем содержимое раздела Источники на sources_list (даже если модель оставила пустой блок)
                def _replace_sources_section(m: re.Match) -> str:
                    return f"{m.group(1)}\n\n{sources_txt}\n"

                article = re.sub(
                    r"(^##\s*Источники\s*$)(.*?)(?=^##\s+|\Z)",
                    _replace_sources_section,
                    article,
                    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
                )

                # Если раздел не найден (редкий случай) — добавляем в конец
                if not re.search(r"^##\s*Источники\s*$", article, flags=re.IGNORECASE | re.MULTILINE):
                    article += "\n\n## Источники\n\n" + sources_txt
                # Для on-screen отображения достаточно нормализованной структуры;
                # кликабельные ссылки [1] → Источники добавляем только в экспортируемый .md.
                article = normalize_article_structure(article)
            with article_placeholder.container():
                self._render_article_display(article)

            # Вставка изображений из PDF (оригиналы или улучшенные)
            existing_paths: List[str] = []
            if use_existing_images and extracted_image_paths:
                existing_paths = extracted_image_paths[:10]  # ограничение, чтобы не перегружать UI/PDF
                if improve_existing_images and extracted_images:
                    try:
                        from illustration_pipeline import IllustrationPipeline
                        pipeline = IllustrationPipeline()
                        improved: List[str] = []
                        for i, img_info in enumerate(extracted_images[: len(existing_paths)], 1):
                            progress.update_progress(
                                0.72 + 0.1 * i / max(1, len(existing_paths)),
                                f"Улучшение изображения {i}/{len(existing_paths)}...",
                            )
                            new_path, _ = pipeline.redraw_image_with_nanobanana(img_info)
                            if new_path and os.path.exists(new_path):
                                improved.append(new_path)
                            else:
                                p = img_info.get("file_path")
                                if p and os.path.exists(p):
                                    improved.append(p)
                        if improved:
                            existing_paths = improved
                    except Exception:
                        pass

                # Расставляем изображения по разделам статьи
                insertions: List[Tuple[int, str]] = []
                for i, p in enumerate(existing_paths, 1):
                    p_abs = os.path.abspath(p).replace("\\", "/")
                    cap = f"Рис. {i}. Изображение из исходных документов"
                    img_md = f"\n\n{cap}\n\n![{cap}](file:{p_abs})\n\n"
                    insertions.append((i - 1, img_md))
                article = self._insert_images_at_sections(article, insertions) if insertions else article
                st.session_state["_illustration_paths_docs"] = existing_paths

            article_to_store = article
            _diag_parts = []
            try:
                llm_stats = getattr(processor, "_last_article_cost_stats", {}) or {}
                plan_stats = llm_stats.get("plan") or {}
                article_stats = llm_stats.get("article") or {}
                total_in = (plan_stats.get("input_tokens_est") or 0) + (article_stats.get("input_tokens_est") or 0)
                total_out = (plan_stats.get("output_tokens_est") or 0) + (article_stats.get("output_tokens_est") or 0)
                cost_total = llm_stats.get("total_cost_usd_est")
                if cost_total is not None:
                    _diag_parts.append(
                        f"Тарификация LLM: ${float(cost_total):.4f} (токены: вх {int(total_in):,}, вых {int(total_out):,})"
                    )
                else:
                    _diag_parts.append(f"Тарификация LLM: (нет тарифов) (токены: вх {int(total_in):,}, вых {int(total_out):,})")
            except Exception:
                pass
            if add_illustrations and (get_nanobanana_api_key() or get_dalle_api_key()):
                try:
                    _diag_parts.append("Чекбокс иллюстраций: ВКЛ")
                    nb_key = get_nanobanana_api_key()
                    da_key = get_dalle_api_key()
                    _diag_parts.append(f"NanoBanana ключ: {'задан (' + nb_key[:8] + '…)' if nb_key else 'НЕТ'}")
                    _diag_parts.append(f"DALL-E ключ: {'задан (' + da_key[:8] + '…)' if da_key else 'НЕТ'}")
                    progress.update_progress(0.85, "Генерация промптов для иллюстраций...")
                    article_with_markers, prompts_list = processor.generate_article_image_prompts(article, num_images=5)
                    _diag_parts.append(f"Промптов от LLM: {len(prompts_list)}")
                    if not prompts_list:
                        article_with_markers, prompts_list = processor._fallback_illustration_markers(article, num_images=5)
                        _diag_parts.append(f"Промптов после fallback: {len(prompts_list)}")
                    if not prompts_list:
                        _diag_parts.append("РЕЗУЛЬТАТ: промпты пустые — изображения не генерировались")
                    else:
                        def _ill_progress(i: int, t: int, cap: str):
                            progress.update_progress(0.85 + 0.12 * i / max(t, 1), f"Иллюстрация {i}/{t}: {cap[:40]}…")
                        _ill_style = settings_manager.get("illustration_style", "academic")
                        article, image_paths, ill_errors = self._add_illustrations_to_article(
                            article_with_markers, prompts_list, progress_callback=_ill_progress, illustration_style=_ill_style
                        )
                        _diag_parts.append(f"Сгенерировано файлов изображений: {len(image_paths)}")
                        if ill_errors:
                            model_used = None
                            fallback_used = None
                            for msg in ill_errors:
                                m = re.search(r"NanoBanana модель для генерации:\s*(.*?)\s*\(", msg, flags=re.IGNORECASE)
                                if m and not model_used:
                                    model_used = m.group(1).strip()
                                m2 = re.search(r"fallback\s+на\s+модель:\s*(.+)", msg, flags=re.IGNORECASE)
                                if m2 and not fallback_used:
                                    fallback_used = m2.group(1).strip()
                            if model_used:
                                _diag_parts.append(f"NanaBanana модель: {model_used}")
                            if fallback_used:
                                _diag_parts.append(f"Fallback модель: {fallback_used}")
                            _diag_parts.append("Ошибки API: " + "; ".join(ill_errors[:3]))
                        st.session_state["_illustration_paths_docs"] = image_paths
                        if not image_paths:
                            _diag_parts.append("РЕЗУЛЬТАТ: API не вернул ни одного изображения. Проверьте ключи и лимиты.")
                        else:
                            _diag_parts.append(f"РЕЗУЛЬТАТ: вставлено {len(image_paths)} изображений")
                        article = self._article_data_uri_to_file_refs(article, image_paths) if image_paths else article
                except Exception as e:
                    _diag_parts.append(f"ОШИБКА иллюстраций: {e}")
            elif add_illustrations and not get_nanobanana_api_key() and not get_dalle_api_key():
                _diag_parts.append("Чекбокс иллюстраций: ВКЛ, но API ключи NanoBanana и DALL-E не заданы в Настройках!")
            elif not add_illustrations:
                _diag_parts.append("Чекбокс иллюстраций: ВЫКЛ — генерация картинок не запускалась.")

            st.session_state["_ill_diag_docs"] = " | ".join(_diag_parts) if _diag_parts else ""
            st.session_state["last_article_docs"] = article
            try:
                add_article_to_history(
                    theme=theme or "Без темы",
                    article_text=article,
                    source="docs",
                    created_by=st.session_state.get("username", "admin"),
                )
            except Exception:
                pass
            progress.complete("Статья успешно сгенерирована.")
            st.rerun()
        except Exception as e:
            progress.update_message(f"Ошибка генерации статьи: {str(e)}")
            st.error(f"Ошибка генерации статьи по документам: {e}")

    def _generate_article_from_topic(
        self,
        theme: str,
        temperature: float,
        include_research: bool,
        style_controls: dict,
        add_illustrations: bool = False,
        num_plan_steps: int = 10,
    ) -> None:
        """Генерация статьи только по теме: план → поиск PubMed → стриминг финальной статьи."""
        if not theme or not theme.strip():
            st.error("Пожалуйста, укажите тему статьи.")
            return

        api_key = self._ensure_api_key()
        if not api_key:
            return

        settings_manager.set("temperature", temperature)
        settings_manager.set("include_research", include_research)

        # Берём актуальные параметры стиля из настроек (слайдеры уже сохранили их в settings_manager)
        style_controls = self._get_style_controls_from_settings()

        progress = ProgressDisplay()
        audience = "подготовленная аудитория"
        try:
            processor = TextProcessor(
                api_key,
                temperature=temperature,
                include_research=include_research,
                style_controls=style_controls,
            )

            # Шаг 1: Генерация плана
            progress.start("Шаг 1/3: Генерация плана статьи...")
            plan = processor.generate_article_plan(theme=theme.strip(), audience=audience, num_plan_steps=num_plan_steps)

            if not plan:
                progress.update_message("Не удалось сгенерировать план. Используем fallback (стриминг).")
                st.markdown("---")
                st.markdown("### Сгенерированная статья по теме")
                article_placeholder = st.empty()
                article = ""
                last_rendered_len = 0
                for chunk in processor.generate_article_stream(theme=theme, source_texts=None):
                    article += chunk
                    if len(article) - last_rendered_len >= self._STREAM_UPDATE_INTERVAL_CHARS:
                        with article_placeholder.container():
                            self._render_article_display(article, max_chars=self._STREAM_DISPLAY_MAX_CHARS)
                        last_rendered_len = len(article)
                with article_placeholder.container():
                    self._render_article_display(article)
                if include_research:
                    try:
                        pubmed_block = fetch_pubmed_summaries(theme.strip(), max_results=5)
                        if pubmed_block:
                            article += "\n\n## Современные исследования (PubMed)\n\n" + pubmed_block
                    except Exception:
                        pass
                _diag_parts = []
                if add_illustrations and (get_nanobanana_api_key() or get_dalle_api_key()):
                    try:
                        _diag_parts.append("Чекбокс иллюстраций: ВКЛ")
                        nb_key = get_nanobanana_api_key()
                        da_key = get_dalle_api_key()
                        _diag_parts.append(f"NanoBanana ключ: {'задан (' + nb_key[:8] + '…)' if nb_key else 'НЕТ'}")
                        _diag_parts.append(f"DALL-E ключ: {'задан (' + da_key[:8] + '…)' if da_key else 'НЕТ'}")
                        progress.update_progress(0.8, "Генерация промптов для иллюстраций...")
                        article_with_markers, prompts_list = processor.generate_article_image_prompts(article, num_images=5)
                        _diag_parts.append(f"Промптов от LLM: {len(prompts_list)}")
                        if not prompts_list:
                            article_with_markers, prompts_list = processor._fallback_illustration_markers(article, num_images=5)
                            _diag_parts.append(f"Промптов после fallback: {len(prompts_list)}")
                        if not prompts_list:
                            _diag_parts.append("РЕЗУЛЬТАТ: промпты пустые — изображения не генерировались")
                        else:
                            def _ill_progress(i: int, t: int, cap: str):
                                progress.update_progress(0.8 + 0.15 * i / max(t, 1), f"Иллюстрация {i}/{t}: {cap[:40]}…")
                            _ill_style = settings_manager.get("illustration_style", "academic")
                            article, image_paths, ill_errors = self._add_illustrations_to_article(
                                article_with_markers, prompts_list, progress_callback=_ill_progress, illustration_style=_ill_style
                            )
                            _diag_parts.append(f"Сгенерировано файлов изображений: {len(image_paths)}")
                            if ill_errors:
                                _diag_parts.append("Ошибки API: " + "; ".join(ill_errors[:3]))
                            if not image_paths:
                                _diag_parts.append("РЕЗУЛЬТАТ: API не вернул ни одного изображения. Проверьте ключи и лимиты.")
                            else:
                                _diag_parts.append(f"РЕЗУЛЬТАТ: вставлено {len(image_paths)} изображений")
                    except Exception as e:
                        _diag_parts.append(f"ОШИБКА иллюстраций: {e}")
                elif add_illustrations and not get_nanobanana_api_key() and not get_dalle_api_key():
                    _diag_parts.append("Чекбокс иллюстраций: ВКЛ, но API ключи NanoBanana и DALL-E не заданы в Настройках!")
                elif not add_illustrations:
                    _diag_parts.append("Чекбокс иллюстраций: ВЫКЛ — генерация картинок не запускалась.")
                st.session_state["_ill_diag"] = " | ".join(_diag_parts) if _diag_parts else ""
                st.session_state["last_article_topic"] = article
                try:
                    add_article_to_history(
                        theme=theme.strip(),
                        article_text=article,
                        source="topic",
                        created_by=st.session_state.get("username", "admin"),
                    )
                except Exception:
                    pass
                progress.complete("Статья успешно сгенерирована (fallback).")
                st.rerun()
                return

            # Показываем план в expander
            with st.expander("📋 План выполнения", expanded=True):
                for i, step in enumerate(plan, 1):
                    queries = step.get("searchQueries") or []
                    q_parts = [str(q)[:50] + ("…" if len(str(q)) > 50 else "") for q in queries[:2]]
                    q_preview = ", ".join(q_parts) if q_parts else ""
                    st.markdown(f"**{i}. {step.get('step', '')}** — {step.get('description', '')}")
                    if q_preview:
                        st.caption(f"Поиск: {q_preview}")

            # Шаг 2: Поиск источников (только при включённых исследованиях)
            search_context, sources_list = "", ""
            if include_research:
                progress.update_progress(0.3, "Шаг 2/3: Поиск в PubMed...")
                search_context, sources_list = processor.execute_article_searches(
                    plan, theme=theme.strip(), max_chars=12000
                )
            st.session_state["_sources_list_topic"] = sources_list or ""

            # Шаг 3: Стриминг финальной статьи
            progress.update_progress(0.5, "Шаг 3/3: Генерация статьи (стриминг)...")
            st.markdown("---")
            st.markdown("### Сгенерированная статья по теме")
            article_placeholder = st.empty()
            article = ""
            last_rendered_len = 0
            for chunk in processor.generate_article_final_stream(
                theme=theme.strip(),
                plan=plan,
                search_context=search_context,
                audience=audience,
                sources_list=sources_list,
            ):
                article += chunk
                if len(article) - last_rendered_len >= self._STREAM_UPDATE_INTERVAL_CHARS:
                    with article_placeholder.container():
                        self._render_article_display(article, max_chars=self._STREAM_DISPLAY_MAX_CHARS)
                    last_rendered_len = len(article)
            # Пост-процессинг для гарантии: содержание в начале + раздел Источники
            if include_research and sources_list and sources_list.strip():
                sources_txt = sources_list.strip()
                article = re.sub(
                    r"^##\s*источники.*$",
                    "## Источники",
                    article,
                    flags=re.IGNORECASE | re.MULTILINE,
                )

                def _replace_sources_section(m: re.Match) -> str:
                    return f"{m.group(1)}\n\n{sources_txt}\n"

                article = re.sub(
                    r"(^##\s*Источники\s*$)(.*?)(?=^##\s+|\Z)",
                    _replace_sources_section,
                    article,
                    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
                )

                if not re.search(r"^##\s*Источники\s*$", article, flags=re.IGNORECASE | re.MULTILINE):
                    article += "\n\n## Источники\n\n" + sources_txt
                article = normalize_article_structure(article)
            with article_placeholder.container():
                self._render_article_display(article)

            if include_research and not search_context:
                try:
                    pubmed_block = fetch_pubmed_summaries(theme.strip(), max_results=5)
                    if pubmed_block:
                        article += "\n\n## Современные исследования (PubMed)\n\n" + pubmed_block
                except Exception:
                    pass

            article_to_store = article
            image_paths: List[str] = []
            _diag_parts = []
            try:
                llm_stats = getattr(processor, "_last_article_cost_stats", {}) or {}
                plan_stats = llm_stats.get("plan") or {}
                article_stats = llm_stats.get("article") or {}
                total_in = (plan_stats.get("input_tokens_est") or 0) + (article_stats.get("input_tokens_est") or 0)
                total_out = (plan_stats.get("output_tokens_est") or 0) + (article_stats.get("output_tokens_est") or 0)
                cost_total = llm_stats.get("total_cost_usd_est")
                if cost_total is not None:
                    _diag_parts.append(
                        f"Тарификация LLM: ${float(cost_total):.4f} (токены: вх {int(total_in):,}, вых {int(total_out):,})"
                    )
                else:
                    _diag_parts.append(f"Тарификация LLM: (нет тарифов) (токены: вх {int(total_in):,}, вых {int(total_out):,})")
            except Exception:
                pass
            if add_illustrations and (get_nanobanana_api_key() or get_dalle_api_key()):
                try:
                    _diag_parts.append("Чекбокс иллюстраций: ВКЛ")
                    nb_key = get_nanobanana_api_key()
                    da_key = get_dalle_api_key()
                    _diag_parts.append(f"NanoBanana ключ: {'задан (' + nb_key[:8] + '…)' if nb_key else 'НЕТ'}")
                    _diag_parts.append(f"DALL-E ключ: {'задан (' + da_key[:8] + '…)' if da_key else 'НЕТ'}")
                    progress.update_progress(0.85, "Генерация промптов для иллюстраций...")
                    article_with_markers, prompts_list = processor.generate_article_image_prompts(article, num_images=5)
                    _diag_parts.append(f"Промптов от LLM: {len(prompts_list)}")
                    if not prompts_list:
                        article_with_markers, prompts_list = processor._fallback_illustration_markers(article, num_images=5)
                        _diag_parts.append(f"Промптов после fallback: {len(prompts_list)}")
                    if not prompts_list:
                        _diag_parts.append("РЕЗУЛЬТАТ: промпты пустые — изображения не генерировались")
                    else:
                        def _ill_progress(i: int, t: int, cap: str):
                            progress.update_progress(0.85 + 0.12 * i / max(t, 1), f"Иллюстрация {i}/{t}: {cap[:40]}…")
                        _ill_style = settings_manager.get("illustration_style", "academic")
                        article, image_paths, ill_errors = self._add_illustrations_to_article(
                            article_with_markers, prompts_list, progress_callback=_ill_progress, illustration_style=_ill_style
                        )
                        _diag_parts.append(f"Сгенерировано файлов изображений: {len(image_paths)}")
                        if ill_errors:
                            model_used = None
                            fallback_used = None
                            for msg in ill_errors:
                                m = re.search(r"NanoBanana модель для генерации:\s*(.*?)\s*\(", msg, flags=re.IGNORECASE)
                                if m and not model_used:
                                    model_used = m.group(1).strip()
                                m2 = re.search(r"fallback\s+на\s+модель:\s*(.+)", msg, flags=re.IGNORECASE)
                                if m2 and not fallback_used:
                                    fallback_used = m2.group(1).strip()
                            if model_used:
                                _diag_parts.append(f"NanaBanana модель: {model_used}")
                            if fallback_used:
                                _diag_parts.append(f"Fallback модель: {fallback_used}")
                            _diag_parts.append("Ошибки API: " + "; ".join(ill_errors[:3]))
                        if not image_paths:
                            _diag_parts.append("РЕЗУЛЬТАТ: API не вернул ни одного изображения. Проверьте ключи и лимиты.")
                        else:
                            _diag_parts.append(f"РЕЗУЛЬТАТ: вставлено {len(image_paths)} изображений")
                        # Сохраняем пути для PDF и храним статью в session_state через file: ссылки,
                        # чтобы не гонять мегабайты base64 по WebSocket (иначе UI «висит»).
                        st.session_state["_illustration_paths_topic"] = image_paths
                        article = self._article_data_uri_to_file_refs(article, image_paths) if image_paths else article
                except Exception as e:
                    _diag_parts.append(f"ОШИБКА иллюстраций: {e}")
            elif add_illustrations and not get_nanobanana_api_key() and not get_dalle_api_key():
                _diag_parts.append("Чекбокс иллюстраций: ВКЛ, но API ключи NanoBanana и DALL-E не заданы в Настройках!")
            elif not add_illustrations:
                _diag_parts.append("Чекбокс иллюстраций: ВЫКЛ — генерация картинок не запускалась.")
            st.session_state["_ill_diag"] = " | ".join(_diag_parts) if _diag_parts else ""
            st.session_state["last_article_topic"] = article
            try:
                add_article_to_history(
                    theme=theme.strip(),
                    article_text=article,
                    source="topic",
                    created_by=st.session_state.get("username", "admin"),
                )
            except Exception:
                pass
            progress.complete("Статья успешно сгенерирована.")
            st.rerun()
        except Exception as e:
            progress.update_message(f"Ошибка генерации статьи: {str(e)}")
            st.error(f"Ошибка генерации статьи по теме: {e}")

    def _inject_sources_if_needed(self, article: str, suffix: str) -> str:
        """Гарантированно подставляет полный список источников в секцию '## Источники'."""
        if not article:
            return ""
        sources_txt = (st.session_state.get(f"_sources_list_{suffix}", "") or "").strip()
        if not sources_txt:
            return article
        text = article
        text = re.sub(
            r"^##\s*источники.*$",
            "## Источники",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )

        def _replace_sources_section(m: re.Match) -> str:
            return f"{m.group(1)}\n\n{sources_txt}\n"

        text = re.sub(
            r"(^##\s*Источники\s*$)(.*?)(?=^##\s+|\Z)",
            _replace_sources_section,
            text,
            flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        if not re.search(r"^##\s*Источники\s*$", text, flags=re.IGNORECASE | re.MULTILINE):
            text += "\n\n## Источники\n\n" + sources_txt
        return text

    def _prepare_article_for_md(
        self,
        article: str,
        normalize_structure: bool = True,
        check_spelling: bool = False,
        suffix: str = "",
    ) -> str:
        """Подготовка статьи для скачивания .md: структура, источники, орфография."""
        if not article:
            return ""
        text = re.sub(r"\[ILLUSTRATION_\d+\]", "", article)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if suffix in ("topic", "docs"):
            text = self._inject_sources_if_needed(text, suffix)
        if normalize_structure:
            text = normalize_article_structure(text)
        if check_spelling:
            text, messages = check_spelling_ru(text)
            if messages:
                st.caption(f"Орфография: исправлено {len(messages)} предупреждений (Яндекс.Спеллер)")
        text = add_markdown_links(text)
        return text

    def _render_article_download_buttons(self, article: str, suffix: str) -> None:
        """Рендерит кнопки скачивания .md и .pdf для статьи."""
        st.caption("Перед скачиванием .md можно привести текст к структуре и проверить орфографию.")
        col_opts1, col_opts2, _ = st.columns([1, 1, 2])
        with col_opts1:
            opt_structure = st.checkbox(
                "Привести к структуре (заголовок, содержание, блоки, источники)",
                value=True,
                key=f"opt_structure_{suffix}",
            )
        with col_opts2:
            opt_spelling = st.checkbox(
                "Проверить орфографию (Яндекс.Спеллер)",
                value=False,
                key=f"opt_spelling_{suffix}",
            )
        md_ready = self._prepare_article_for_md(
            article,
            normalize_structure=opt_structure,
            check_spelling=opt_spelling,
            suffix=suffix,
        )
        col_md, col_pdf, col_docx = st.columns(3)
        with col_md:
            md_ready_with_images = self._article_file_refs_to_data_uri(md_ready)
            st.download_button(
                label="Скачать статью (.md)",
                data=self._format_markdown_preview(md_ready_with_images),
                file_name=f"article_from_{suffix}.md",
                mime="text/plain",
                key=f"download_article_{suffix}_md",
            )
        with col_pdf:
            article_for_export = self._inject_sources_if_needed(article, suffix)
            pdf_article = normalize_article_structure(article_for_export) if opt_structure else article_for_export
            # Если есть сгенерированные иллюстрации для этой статьи — вставляем их в PDF после ссылок «Рис. N»
            img_paths: List[str] = []
            if suffix == "topic":
                img_paths = st.session_state.get("_illustration_paths_topic", []) or []
            elif suffix == "docs":
                img_paths = st.session_state.get("_illustration_paths_docs", []) or []

            if img_paths:
                pdf_bytes, pdf_err = text_to_pdf_with_images(pdf_article, img_paths)
            else:
                pdf_bytes, pdf_err = text_to_pdf(pdf_article)
            if pdf_bytes:
                st.download_button(
                    label="Скачать статью (.pdf)",
                    data=pdf_bytes,
                    file_name=f"article_from_{suffix}.pdf",
                    mime="application/pdf",
                    key=f"download_article_{suffix}_pdf",
                )
            elif pdf_err:
                st.caption(f"PDF: {pdf_err[:80]}...")
        with col_docx:
            article_for_export = self._inject_sources_if_needed(article, suffix)
            docx_article = normalize_article_structure(article_for_export) if opt_structure else article_for_export
            docx_bytes, docx_err = text_to_docx(docx_article)
            if docx_bytes:
                st.download_button(
                    label="Скачать статью (.docx)",
                    data=docx_bytes,
                    file_name=f"article_from_{suffix}.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    key=f"download_article_{suffix}_docx",
                )
            elif docx_err:
                st.caption(f"DOCX: {docx_err[:80]}...")

    def _fmt_dt(self, value: str) -> str:
        """Форматирует дату для отображения."""
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(value) or ""

    def _render_article_history(self) -> None:
        """Отображает историю генерации статей с кнопкой «Сохранить в книги»."""
        history = list_article_history(limit=30)
        if not history:
            return
        with st.expander("📜 История генерации", expanded=False):
            for item in history:
                hist_id = item.get("id")
                theme = item.get("theme", "Без темы")
                source = item.get("source", "topic")
                source_label = "по теме" if source == "topic" else "по документам"
                created_at = self._fmt_dt(item.get("created_at", ""))
                created_by = item.get("created_by", "")
                saved_book_id = item.get("saved_book_id")
                article_text = item.get("article_text", "") or ""
                preview = (article_text[:200] + "…") if len(article_text) > 200 else article_text

                with st.container():
                    st.markdown(f"**{theme}**")
                    st.caption(f"{created_at} • {source_label} • {created_by}")
                    if preview:
                        st.text(preview)
                    col1, col2, _ = st.columns([1, 1, 2])
                    with col1:
                        if saved_book_id:
                            st.success(f"✓ Сохранено в книгу #{saved_book_id}")
                        else:
                            if st.button(f"Сохранить в книги", key=f"save_hist_{hist_id}"):
                                try:
                                    book_id = create_book(
                                        title=theme,
                                        source_filename=None,
                                        theme=theme,
                                        temperature=None,
                                        include_research=False,
                                        original_text=None,
                                        paraphrased_text=article_text,
                                        created_by=st.session_state.get("username", "admin"),
                                        style_science=int(settings_manager.get("style_science", 3)),
                                        style_depth=int(settings_manager.get("style_depth", 3)),
                                        style_accuracy=int(settings_manager.get("style_accuracy", 3)),
                                        style_readability=int(settings_manager.get("style_readability", 3)),
                                        style_source_quality=int(
                                            settings_manager.get("style_source_quality", 3)
                                        ),
                                    )
                                    mark_article_saved(hist_id, book_id)
                                    st.session_state.last_saved_book_id = book_id
                                    st.success(f"Сохранено в книгу #{book_id}. Перейдите в раздел «Книги».")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Ошибка: {e}")
                    st.markdown("---")

    def _render_api_key_status(self):
        """Отображение статуса API ключа"""
        provider = get_llm_provider()
        provider_name = {"deepseek": "DeepSeek", "gemini": "Gemini"}.get(provider, "OpenAI")
        
        if has_active_api_key():
            st.info(f"API ключ {provider_name} настроен. Переходите к загрузке файла и перефразированию.")
        else:
            st.warning(f"API ключ {provider_name} не установлен. Пожалуйста, настройте его в разделе 'Настройки' перед началом работы.")

    def _process_file(
        self,
        uploaded_file,
        theme,
        temperature,
        include_research,
        fullscreen_preview,
        redraw_images,
        style_controls: dict,
    ):
        """Обработка загруженного файла"""
        # Валидация входных данных
        if not uploaded_file:
            st.error("Пожалуйста, загрузите файл для обработки")
            return

        if not has_active_api_key():
            provider = get_llm_provider()
            provider_name = {"deepseek": "DeepSeek", "gemini": "Gemini"}.get(provider, "OpenAI")
            st.error(f"API ключ {provider_name} не настроен. Пожалуйста, настройте его в разделе 'Настройки'")
            return

        if not theme:
            st.error("Пожалуйста, укажите тему текста")
            return

        # Сохранение настроек
        settings_manager.set("temperature", temperature)
        settings_manager.set("include_research", include_research)

        # Получение API ключа
        provider = get_llm_provider()
        if provider == "deepseek":
            api_key = get_deepseek_api_key()
        elif provider == "gemini":
            api_key = get_gemini_api_key()
        else:
            api_key = get_api_key()

        # Создание компонента прогресса
        progress = ProgressDisplay()

        try:
            progress.start("Сохранение файла...")

            # Сохранение файла во временную директорию
            with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{uploaded_file.name}") as tmp_file:
                tmp_file.write(uploaded_file.getvalue())
                temp_file_path = tmp_file.name

            progress.update_progress(0.1, "Извлечение текста...")

            redraw_thread = None
            extracted_for_redraw = []
            paths_list_for_redraw = []

            if uploaded_file.name.lower().endswith(".pdf"):
                if redraw_images:
                    extracted_for_redraw, paths_list_for_redraw = self._extract_images_for_redraw(temp_file_path)
                    if extracted_for_redraw and paths_list_for_redraw:
                        st.session_state.redrawn_image_paths = paths_list_for_redraw
                        redraw_thread = threading.Thread(
                            target=self._redraw_images_worker,
                            args=(extracted_for_redraw, paths_list_for_redraw),
                            daemon=True,
                        )
                        redraw_thread.start()
                        st.caption("Перерисовка изображений запущена параллельно с перефразированием.")
                    else:
                        st.session_state.redrawn_image_paths = []
                else:
                    self._extract_images_only(temp_file_path)
            else:
                st.session_state.redrawn_image_paths = []

            # Создание процессора
            processor = TextProcessor(
                api_key,
                temperature=temperature,
                include_research=include_research,
                style_controls=style_controls,
            )

            # Извлечение текста
            original_text = processor.read_input_file(temp_file_path)

            if not original_text:
                st.error("Не удалось извлечь текст из файла")
                return

            st.session_state.original_text = original_text

            # Инициализация переменных для real-time вывода
            st.session_state.paraphrased_text = ""
            st.session_state.paraphrased_blocks = []
            st.session_state.processing_complete = False
            
            st.markdown("---")
            col_in_work, col_accumulated = st.columns(2)
            
            with col_in_work:
                st.markdown("#### Сейчас в работе (Оригинал)")
                current_original_area = st.empty()
            
            with col_accumulated:
                st.markdown("#### Накапливаемый результат")
                accumulated_result_area = st.empty()
                markdown_preview_area = st.empty()
                markdown_preview_area.caption("Markdown-превью обновляется в реальном времени.")
            full_markdown_preview_area = st.empty()
            
            def update_result_area(original_block, paraphrased_block):
                # Обновляем текущий блок в работе
                current_original_area.info(original_block)
                
                # Обновляем накопленный результат (текст + изображения по ссылкам «рис. N»)
                block_with_images = paraphrased_block
                image_paths = st.session_state.get("redrawn_image_paths", [])
                for fig_num in _find_figure_refs_in_block(paraphrased_block):
                    if fig_num - 1 < len(image_paths):
                        img_path = image_paths[fig_num - 1]
                        p_norm = os.path.abspath(img_path).replace("\\", "/")
                        block_with_images += f"\n\n![Рисунок {fig_num}](file:{p_norm})\n\n"
                    else:
                        block_with_images += f"\n\n![Рисунок {fig_num}]\n\n"

                if st.session_state.paraphrased_text:
                    st.session_state.paraphrased_text += "\n\n" + block_with_images
                else:
                    st.session_state.paraphrased_text = block_with_images

                # Сохраняем пары блоков для интерактивного превью
                blocks = st.session_state.get("paraphrased_blocks") or []
                blocks.append(
                    {
                        "index": len(blocks) + 1,
                        "original": original_block,
                        "paraphrased": paraphrased_block,
                    }
                )
                st.session_state.paraphrased_blocks = blocks
                
                accumulated_result_area.text_area(
                    "Все перефразированные блоки:",
                    st.session_state.paraphrased_text,
                    height=350
                )
                # Markdown-превью сразу, в реальном времени
                if st.session_state.paraphrased_text:
                    if fullscreen_preview:
                        markdown_preview_area.empty()
                        with full_markdown_preview_area.container():
                            self._render_markdown_preview(
                                st.session_state.paraphrased_text,
                                fullscreen_preview,
                                st.session_state.get("paraphrased_blocks") or [],
                            )
                    else:
                        full_markdown_preview_area.empty()
                        with markdown_preview_area.container():
                            self._render_markdown_preview(
                                st.session_state.paraphrased_text,
                                fullscreen_preview,
                                st.session_state.get("paraphrased_blocks") or [],
                            )

            # Перефразирование (идёт параллельно с перерисовкой изображений, если запущена)
            progress.update_progress(0.2, "Перефразирование блоков...")
            processor.process_text(original_text, theme, callback=update_result_area)

            # Канонический результат — накопленный текст с вставленными рисунками (и таблицами)
            paraphrased_text = st.session_state.get("paraphrased_text") or ""

            # Дожидаемся завершения перерисовки изображений
            if redraw_thread is not None and redraw_thread.is_alive():
                progress.update_progress(0.85, "Ожидание перерисовки изображений...")
                redraw_thread.join()
            if redraw_thread is not None:
                st.session_state.redrawn_image_paths = paths_list_for_redraw
                # Подставляем финальные пути к перерисованным изображениям в текст
                for i, img_info in enumerate(extracted_for_redraw):
                    if i >= len(paths_list_for_redraw):
                        break
                    orig_path = img_info["file_path"]
                    final_path = paths_list_for_redraw[i]
                    if orig_path != final_path and final_path:
                        paraphrased_text = paraphrased_text.replace(orig_path, final_path)
                st.session_state.paraphrased_text = paraphrased_text
                if extracted_for_redraw and paths_list_for_redraw:
                    self._render_redrawn_images_result(extracted_for_redraw, paths_list_for_redraw)

            # Используем частичные результаты, если полное перефразирование не удалось
            if not paraphrased_text.strip():
                paraphrased_text = st.session_state.get("paraphrased_text") or ""
                if paraphrased_text.strip():
                    st.warning("Перефразирование прервано. Сохранены частичные результаты.")

            if not paraphrased_text.strip():
                st.error("Не удалось перефразировать текст")
                return

            # Markdown-превью и скачивание (сразу после перефразирования)
            preview_target = full_markdown_preview_area if fullscreen_preview else markdown_preview_area
            with preview_target.container():
                blocks = st.session_state.get("paraphrased_blocks") or []
                self._render_markdown_preview(paraphrased_text, fullscreen_preview, blocks)
            col_md, col_pdf = st.columns(2)
            with col_md:
                        st.download_button(
                    label="Скачать Markdown (.md)",
                    data=self._format_markdown_preview(paraphrased_text),
                    file_name="paraphrased.md",
                            mime="text/plain",
                    key="download_markdown_tab",
                )
            with col_pdf:
                img_paths = st.session_state.get("redrawn_image_paths", [])
                if img_paths:
                    pdf_bytes, pdf_err = text_to_pdf_with_images(paraphrased_text, img_paths)
                else:
                    pdf_bytes, pdf_err = text_to_pdf(paraphrased_text)
                if pdf_bytes:
                        st.download_button(
                        label="Скачать PDF",
                        data=pdf_bytes,
                        file_name="paraphrased.pdf",
                        mime="application/pdf",
                        key="download_pdf_tab",
                    )
                elif pdf_err:
                    st.caption(f"PDF: {pdf_err[:50]}...")

            # Сохранение результатов
            self._save_results(
                original_text,
                paraphrased_text,
                source_filename=getattr(uploaded_file, "name", None),
                theme=theme,
                temperature=temperature,
                include_research=include_research,
                style_controls=style_controls,
            )
            
            # Устанавливаем флаг завершения
            st.session_state.processing_complete = True

            # Очищаем "в работе" после завершения
            current_original_area.success("Все блоки обработаны.")
            
            progress.complete("Обработка завершена успешно.")
            st.success("Перефразирование завершено. Перейдите на вкладку 'Результаты' для просмотра и скачивания.")
            if st.button("Перейти к результатам", width="stretch"):
                # В Streamlit нет прямого способа переключить вкладку программно без хаков,
                # но мы можем вывести подсказку.
                st.info("Пожалуйста, выберите вкладку 'Результаты' в верхнем меню.")

        except Exception as e:
            progress.update_message(f"Произошла ошибка: {str(e)}")
            # Сохраняем частичные результаты, если есть
            partial = st.session_state.get("paraphrased_text", "").strip()
            orig = st.session_state.get("original_text")
            if partial and orig is not None:
                try:
                    self._save_results(
                        orig,
                        partial,
                        source_filename=getattr(uploaded_file, "name", None),
                        theme=theme,
                        temperature=temperature,
                        include_research=include_research,
                        save_to_db=True,
                        style_controls=style_controls,
                    )
                    st.warning("Обработка прервана. Частичные результаты сохранены в файлы и добавлены в список книг — проверьте вкладку «Результаты».")
                except Exception:
                    pass
            st.info("Рекомендации по устранению ошибки:\n"
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

    def _save_results(
        self,
        original_text,
        paraphrased_text,
        *,
        source_filename,
        theme,
        temperature,
        include_research,
        save_to_db=True,
        style_controls=None,
    ):
        """Сохранение результатов обработки.
        save_to_db: при False только файлы (для частичных результатов при прерывании).
        style_controls: как при генерации (science, depth, …) — пишутся в книгу для дефолтов перегенерации.
        """
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

        # Сохранение в БД только при полном результате (не при частичном после прерывания)
        if save_to_db and st.session_state.get("user_role") == "admin":
            try:
                title = (source_filename or "").strip() or "Переписанная книга"
                sc = style_controls or {}
                book_id = create_book(
                    title=title,
                    source_filename=source_filename,
                    theme=theme,
                    temperature=float(temperature) if temperature is not None else None,
                    include_research=bool(include_research),
                    original_text=original_text,
                    paraphrased_text=paraphrased_text,
                    created_by=st.session_state.get("username", "admin"),
                    style_science=int(sc.get("science", settings_manager.get("style_science", 3))),
                    style_depth=int(sc.get("depth", settings_manager.get("style_depth", 3))),
                    style_accuracy=int(sc.get("accuracy", settings_manager.get("style_accuracy", 3))),
                    style_readability=int(sc.get("readability", settings_manager.get("style_readability", 3))),
                    style_source_quality=int(
                        sc.get("source_quality", settings_manager.get("style_source_quality", 3))
                    ),
                )
                st.session_state.last_saved_book_id = book_id
                st.success(f"Сохранено в БД (книга #{book_id}).")
            except Exception as e:
                st.warning(f"Не удалось сохранить книгу в БД: {e}")
