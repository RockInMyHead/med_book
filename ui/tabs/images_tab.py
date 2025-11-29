"""
Модуль вкладки автоматической иллюстрации книги
"""

import os
import tempfile
import streamlit as st
from ui.components.file_uploader import FileUploader
from ui.components.image_gallery import ImageGallery
from illustration_pipeline import IllustrationPipeline
from settings_manager import get_nanobanana_api_key


class ImagesTab:
    """Класс для управления вкладкой иллюстраций"""

    def __init__(self):
        self.file_uploader = FileUploader()
        self.image_gallery = ImageGallery()

    def render(self, tab):
        """Отображение вкладки иллюстраций"""
        with tab:
            st.header("🎨 Автоматическая иллюстрация книги")

            # Раздел загрузки книги
            self._render_pdf_upload_section()

            st.markdown("---")

            # Раздел просмотра изображений
            self._render_image_viewer_section()

    def _render_pdf_upload_section(self):
        """Отображение секции загрузки PDF"""
        st.subheader("📚 Загрузка книги (PDF)")

        uploaded_pdf_book = self.file_uploader.render(
            label="Выберите PDF файл книги для извлечения изображений",
            allowed_types=["pdf"],
            help_text="Загрузите PDF книгу, чтобы извлечь из нее изображения и подписи."
        )

        if uploaded_pdf_book:
            file_info = self.file_uploader.get_file_info(uploaded_pdf_book)
            st.info(f"📄 Загружен файл: {file_info['name']} ({file_info['size_mb']} MB)")

            extract_images = st.checkbox(
                "Извлечь изображения из загруженной книги",
                value=True,
                help="Если отмечено, изображения будут извлечены из PDF и сохранены в папку 'extracted_images/'."
            )

            if st.button("🚀 Извлечь изображения", type="primary", use_container_width=True):
                if not extract_images:
                    st.error("❌ Включите опцию извлечения изображений")
                else:
                    self._process_pdf_extraction(uploaded_pdf_book)

    def _process_pdf_extraction(self, uploaded_pdf_book):
        """Обработка извлечения изображений из PDF"""
        progress_bar = st.progress(0)
        status_text = st.empty()

        try:
            status_text.text("📁 Сохранение PDF файла...")
            progress_bar.progress(10)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                tmp_file.write(uploaded_pdf_book.getvalue())
                temp_pdf_path = tmp_file.name

            status_text.text("🎨 Инициализация системы обработки...")
            progress_bar.progress(30)

            pipeline = IllustrationPipeline()

            status_text.text("🔍 Извлечение изображений из PDF...")
            progress_bar.progress(60)

            results = pipeline.extract_images_from_pdf(temp_pdf_path)

            status_text.text("🧠 Анализ и классификация изображений...")
            progress_bar.progress(80)

            pipeline._save_image_metadata()

            progress_bar.progress(100)
            status_text.text("✅ Извлечение завершено!")

            st.success(f"🎉 Извлечено **{len(results)}** изображений из книги!")

            if results:
                self._display_extraction_results(results)

            progress_bar.empty()
            status_text.empty()
            st.rerun()

        except Exception as e:
            st.error(f"❌ Ошибка при извлечении изображений: {str(e)}")
        finally:
            if 'temp_pdf_path' in locals():
                try:
                    os.unlink(temp_pdf_path)
                except:
                    pass

    def _display_extraction_results(self, results):
        """Отображение результатов извлечения"""
        col_res1, col_res2, col_res3 = st.columns(3)

        with col_res1:
            st.metric("Всего изображений", len(results))

        clinical = sum(1 for r in results if r.get('classification') == 'clinical')
        encyclopedia = sum(1 for r in results if r.get('classification') == 'encyclopedia')

        with col_res2:
            st.metric("Клинические", clinical)

        with col_res3:
            st.metric("Энциклопедические", encyclopedia)

        st.subheader("🖼️ Извлеченные изображения")
        preview_images = results[:6]
        cols = st.columns(3)
        for i, img_info in enumerate(preview_images):
            with cols[i % 3]:
                img_path = img_info.get('file_path', '')
                if os.path.exists(img_path):
                    try:
                        st.image(img_path, caption=f"Изображение {i+1}", width=150)
                    except:
                        st.error(f"Ошибка загрузки изображения {i+1}")

    def _render_image_viewer_section(self):
        """Отображение секции просмотра изображений"""
        # Автоматический поиск всех изображений
        if not os.path.exists("extracted_images"):
            st.warning("📁 Директория extracted_images не найдена")
            st.info("ℹ️ Изображения должны находиться в папке extracted_images/")
            return

        image_files = [f for f in os.listdir("extracted_images") if f.endswith(('.png', '.jpg', '.jpeg'))]

        if not image_files:
            st.info("📭 Изображения не найдены в директории extracted_images")
            st.info("ℹ️ Добавьте изображения в папку extracted_images/ для работы с ними")
            return

        st.subheader(f"🖼️ Найдено изображений: {len(image_files)}")

        # Сортировка изображений
        image_files.sort()

        # Инициализация индекса
        if 'current_image_index' not in st.session_state:
            st.session_state.current_image_index = 0

        # Навигация между изображениями
        self._render_image_navigation(image_files)

        # Отображение текущего изображения
        self._render_current_image(image_files)

    def _render_image_navigation(self, image_files):
        """Отображение элементов навигации"""
        col_nav1, col_nav2, col_nav3 = st.columns([1, 2, 1])

        with col_nav1:
            if st.button("⬅️ Предыдущее", disabled=st.session_state.current_image_index == 0):
                st.session_state.current_image_index = max(0, st.session_state.current_image_index - 1)
                st.rerun()

        with col_nav2:
            selected_image = st.selectbox(
                "Выберите изображение:",
                image_files,
                index=st.session_state.current_image_index,
                format_func=lambda x: f"{st.session_state.current_image_index + 1}/{len(image_files)}: {x}",
                label_visibility="collapsed",
                key="image_selector"
            )
            st.session_state.current_image_index = image_files.index(selected_image)

        with col_nav3:
            if st.button("Следующее ➡️", disabled=st.session_state.current_image_index == len(image_files) - 1):
                st.session_state.current_image_index = min(len(image_files) - 1, st.session_state.current_image_index + 1)
                st.rerun()

    def _render_current_image(self, image_files):
        """Отображение текущего выбранного изображения"""
        selected_image = image_files[st.session_state.current_image_index]
        image_path = os.path.join("extracted_images", selected_image)

        # Основная информация
        st.markdown(f"**📊 Изображение {st.session_state.current_image_index + 1} из {len(image_files)}**")
        st.markdown(f"**📄 Файл:** {selected_image}")

        # Получение метаданных изображения
        pipeline = IllustrationPipeline()
        metadata = pipeline.get_image_metadata(image_path)

        # Отображение изображения и метаданных
        self.image_gallery.display_single_image_with_metadata(image_path, metadata)

    def _extract_medical_terms(self, text: str) -> str:
        """Извлекает ключевые медицинские термины из текста и переводит их на английский"""
        # Словарь распространенных медицинских терминов
        medical_translations = {
            # Анатомия
            'легкое': 'lung',
            'сердце': 'heart',
            'печень': 'liver',
            'почки': 'kidneys',
            'желудок': 'stomach',
            'кишечник': 'intestine',
            'мозг': 'brain',
            'кости': 'bones',
            'суставы': 'joints',
            'кровь': 'blood',
            'мышцы': 'muscles',

            # Патологии
            'эмфизема': 'emphysema',
            'рак': 'cancer',
            'опухоль': 'tumor',
            'инфаркт': 'infarction',
            'инсульт': 'stroke',
            'перелом': 'fracture',
            'воспаление': 'inflammation',
            'инфекция': 'infection',
            'тромб': 'thrombus',
            'спазм': 'spasm',

            # Методы диагностики
            'рентген': 'x-ray',
            'томограмма': 'tomogram',
            'ультразвук': 'ultrasound',
            'компьютерная томография': 'CT scan',
            'магнитно-резонансная томография': 'MRI',

            # Клинические признаки
            'боль': 'pain',
            'отек': 'edema',
            'кровотечение': 'bleeding',
            'температура': 'fever',
            'давление': 'pressure',

            # Органы и системы
            'дыхательная система': 'respiratory system',
            'сердечно-сосудистая система': 'cardiovascular system',
            'пищеварительная система': 'digestive system',
            'нервная система': 'nervous system',
            'мочевыделительная система': 'urinary system',
        }

        found_terms = []

        # Приводим текст к нижнему регистру для поиска
        text_lower = text.lower()

        # Ищем совпадения с медицинскими терминами
        for ru_term, en_term in medical_translations.items():
            if ru_term in text_lower:
                found_terms.append(en_term)

        # Ищем специфические паттерны
        if 'рентген' in text_lower or 'радиография' in text_lower:
            found_terms.append('chest x-ray')

        if 'томограмма' in text_lower:
            found_terms.append('CT scan')

        if 'эмфизема легкого' in text_lower:
            found_terms.append('lung emphysema')

        # Удаляем дубликаты и ограничиваем количество терминов
        unique_terms = list(set(found_terms))[:5]  # Максимум 5 терминов

        if unique_terms:
            return ', '.join(unique_terms)
        else:
            # Если не найдено специфических терминов, возвращаем общий термин
            return 'medical anatomy'

    # Кнопка перерисовки
        if get_nanobanana_api_key():
            if st.button("🎭 Перерисовать через Nano Banana", type="primary", key=f"redraw_simple_{selected_image}"):
                self._redraw_image(selected_image, image_path, metadata, pipeline)
        else:
            st.info("ℹ️ Для перерисовки изображений настройте API ключ Nano Banana в разделе '⚙️ Настройки'")

    def _redraw_image(self, selected_image, image_path, metadata, pipeline):
        """Перерисовка изображения через Nano Banana"""
        st.markdown("---")
        st.subheader("🎨 Перерисовка изображения")

        # Используем описание рисунка из подписи к изображению
        if metadata and metadata.get("text_around"):
            text_around = metadata["text_around"].strip()
            classification = metadata.get("classification", "unknown")

            # Извлекаем ключевые медицинские термины и создаем понятный промпт
            medical_terms = self._extract_medical_terms(text_around)

            if medical_terms:
                # Создаем понятный промпт на основе извлеченных терминов
                if classification == "clinical":
                    prompt = f"""Generate a detailed clinical medical illustration showing {medical_terms}.

Create a professional anatomical diagram with clear medical details, showing the pathology and anatomical structures. High-quality medical illustration style, anatomically accurate, educational purpose.

IMPORTANT: Generate a CLEAN image with NO text, NO labels, NO captions, NO writing, NO annotations, NO legends. Only pure visual medical illustration elements."""
                elif classification == "encyclopedia":
                    prompt = f"""Create a detailed anatomical diagram illustrating {medical_terms}.

Professional medical illustration style, clear anatomical structures, educational scientific diagram, precise medical details.

IMPORTANT: Generate a CLEAN image with NO text, NO labels, NO captions, NO writing, NO annotations, NO legends. Only pure visual medical illustration elements."""
                else:
                    prompt = f"""Generate a detailed medical illustration showing {medical_terms}.

Professional anatomical diagram, clear medical structures, high-quality educational illustration.

IMPORTANT: Generate a CLEAN image with NO text, NO labels, NO captions, NO writing, NO annotations, NO legends. Only pure visual medical illustration elements."""
            else:
                # Fallback для случаев, когда не удалось извлечь термины
                prompt = f"""Generate a detailed medical illustration of anatomical structures.

Professional medical illustration style, clear anatomical details, educational purpose.

IMPORTANT: Generate a CLEAN image with NO text, NO labels, NO captions, NO writing, NO annotations, NO legends. Only pure visual medical illustration elements."""

        else:
            # Fallback промпт если подпись отсутствует
            prompt = f"""Generate a detailed anatomical medical illustration.

Professional medical illustration style, clear anatomical structures, educational purpose.

IMPORTANT: Generate a CLEAN image with NO text, NO labels, NO captions, NO writing, NO annotations, NO legends. Only pure visual medical illustration elements."""

        # Отображение промпта
        st.markdown("**🎨 Сгенерированный промпт для Nano Banana:**")
        st.info(prompt)
        st.markdown("*Промпт создан на основе анализа подписи к изображению*")

        # Показываем прогресс
        with st.spinner("🎨 Генерация изображения через Google Gemini..."):
            try:
                # Вызываем перерисовку
                result_path = pipeline.redraw_image_with_nanobanana(
                    image_info={
                        "file_path": image_path,
                        "text_around": prompt,
                        "classification": metadata.get("classification", "unknown") if metadata else "unknown",
                        "pathology": metadata.get("pathology") if metadata else None
                    },
                    custom_prompt=prompt,
                    size="1024x1024"
                )

                if result_path:
                    st.success("✅ Изображение успешно перерисовано!")

                    # Отображаем результат
                    col_res1, col_res2 = st.columns(2)

                    with col_res1:
                        st.markdown("**📷 Оригинал:**")
                        st.image(image_path, use_container_width=True)

                    with col_res2:
                        st.markdown("**🎨 Перерисовано:**")
                        st.image(result_path, caption=f"Перерисовано: {os.path.basename(result_path)}", use_container_width=True)

                    # Кнопка скачивания
                    with open(result_path, "rb") as file:
                        st.download_button(
                            label="📥 Скачать изображение",
                            data=file,
                            file_name=os.path.basename(result_path),
                            mime="image/png",
                            key=f"download_redraw_{selected_image}"
                        )
                else:
                    st.error("❌ Не удалось перерисовать изображение. Проверьте API ключ и попробуйте снова.")

            except Exception as e:
                st.error(f"❌ Ошибка при перерисовке: {str(e)}")
                st.info("💡 Убедитесь, что API ключ NanoBanana корректен и у вас есть доступ к Google Gemini API")
