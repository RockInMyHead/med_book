"""
Компоненты для отображения галереи изображений
"""

import os
import streamlit as st
from typing import List, Dict, Optional
from config import MAX_IMAGES_IN_CAROUSEL, EXTRACTED_IMAGES_DIR


class ImageGallery:
    """Класс для отображения галерей изображений"""

    @staticmethod
    def display_image_carousel(images_dir: str = EXTRACTED_IMAGES_DIR, max_images: int = MAX_IMAGES_IN_CAROUSEL):
        """
        Отображает карусель изображений

        Args:
            images_dir: Директория с изображениями
            max_images: Максимальное количество изображений для отображения
        """
        if not os.path.exists(images_dir):
            st.warning(f"📁 Директория {images_dir} не найдена")
            return

        image_files = [f for f in os.listdir(images_dir) if f.endswith(('.png', '.jpg', '.jpeg'))]
        if not image_files:
            st.info("📭 Изображения не найдены в директории extracted_images")
            return

        # Сортируем по имени файла (page_1_img_0.png, page_1_img_1.png, etc.)
        image_files.sort()

        # Ограничиваем количество изображений для отображения
        display_files = image_files[:max_images]

        st.subheader("🎠 Карусель извлеченных изображений")
        st.write(f"📊 Найдено изображений: {len(image_files)} | Показано: {len(display_files)}")

        # Создаем колонки для изображений (максимум 4 в ряд для лучшего отображения)
        cols_per_row = min(4, len(display_files))
        rows = (len(display_files) + cols_per_row - 1) // cols_per_row

        for row in range(rows):
            cols = st.columns(cols_per_row)
            for col_idx in range(cols_per_row):
                img_idx = row * cols_per_row + col_idx
                if img_idx < len(display_files):
                    image_file = display_files[img_idx]
                    with cols[col_idx]:
                        image_path = os.path.join(images_dir, image_file)
                        try:
                            # Используем st.image с правильными параметрами
                            st.image(image_path, caption=f"{image_file}", width=150, use_container_width=False)
                        except Exception as e:
                            st.error(f"❌ Ошибка загрузки {image_file}: {str(e)}")
                            # Показываем путь для диагностики
                            st.code(f"Путь: {image_path}")

        if len(image_files) > max_images:
            st.info(f"ℹ️ Показано {max_images} изображений из {len(image_files)}. Обработка продолжается...")

    @staticmethod
    def display_found_images(results: Dict):
        """
        Отображает найденные изображения из поиска

        Args:
            results: Результаты поиска с found_images
        """
        if not results.get("found_images"):
            return

        st.subheader("🖼️ Найденные изображения из поиска")

        # Проверяем и фильтруем найденные изображения
        valid_images = []
        for img in results["found_images"]:
            if isinstance(img, dict) and "pathology" in img and "source" in img:
                valid_images.append(img)
            else:
                st.warning(f"⚠️ Пропущено некорректное изображение: {type(img)} - {str(img)[:100]}...")

        if not valid_images:
            st.info("ℹ️ Не найдено корректных изображений для отображения")
            return

        # Группируем изображения по патологиям
        images_by_pathology = {}
        for img in valid_images:
            pathology = img["pathology"]
            if pathology not in images_by_pathology:
                images_by_pathology[pathology] = []
            images_by_pathology[pathology].append(img)

        # Отображаем по каждой патологии
        for pathology, images in images_by_pathology.items():
            with st.expander(f"🔍 {pathology} ({len(images)} изображений)", expanded=False):
                st.write(f"**Патология:** {pathology}")
                st.write(f"**Найдено изображений:** {len(images)}")

                # Отображаем изображения в колонках
                for i in range(0, len(images), 3):
                    cols = st.columns(min(3, len(images) - i))
                    for j in range(min(3, len(images) - i)):
                        img = images[i + j]
                        with cols[j]:
                            try:
                                if "url" in img:
                                    st.image(img["url"], caption=f"{pathology}", width=200)
                                    st.markdown(f"[🔗 Открыть изображение]({img['url']})")
                                else:
                                    st.write(f"Изображение: {img.get('title', 'Без названия')}")
                                    if "description" in img:
                                        st.write(f"Описание: {img['description']}")
                            except Exception as e:
                                st.error(f"❌ Ошибка загрузки изображения: {str(e)}")

    @staticmethod
    def display_single_image_with_metadata(image_path: str, metadata: Optional[Dict] = None):
        """
        Отображает одиночное изображение с метаданными

        Args:
            image_path: Путь к изображению
            metadata: Метаданные изображения
        """
        try:
            st.image(image_path, caption=os.path.basename(image_path), width=400)
        except Exception as e:
            st.error(f"❌ Ошибка загрузки изображения: {str(e)}")
            return

        # Отображение метаданных
        if metadata:
            text_around = metadata.get("text_around", "").strip()
            if text_around:
                st.subheader("📝 Подпись к изображению")
                st.info(text_around)

                # Информация о классификации
                classification = metadata.get("classification", "unknown")
                if classification == "clinical":
                    st.success("🏥 **Тип:** Клиническое изображение (рентген, диагностика)")
                elif classification == "encyclopedia":
                    st.info("📚 **Тип:** Энциклопедическое изображение (схема, диаграмма)")
                else:
                    st.write(f"**Тип:** {classification}")

                # Информация о патологии (если есть)
                pathology = metadata.get("pathology")
                if pathology:
                    st.write(f"**Патология:** {pathology}")
            else:
                st.warning("⚠️ Подпись к изображению не найдена")
        else:
            st.info("ℹ️ Метаданные изображения не найдены. Извлеките изображения заново для получения подписей.")
