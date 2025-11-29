#!/usr/bin/env python3
"""
Модуль автоматической иллюстрации книги
"""

import os
import json
import logging
import requests
import time
import base64
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import fitz  # PyMuPDF

try:
    from google import genai
    from google.genai import types
    GOOGLE_GENAI_AVAILABLE = True
except ImportError:
    GOOGLE_GENAI_AVAILABLE = False
    logging.warning("Google GenAI library not available. Install with: pip install google-genai")
from settings_manager import (
    get_nanobanana_api_key,
    get_dalle_api_key,
    get_google_search_api_key,
    get_google_search_engine_id,
    settings_manager
)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class IllustrationPipeline:
    """Класс для автоматической иллюстрации книги"""
    
    def __init__(self):
        """Инициализация pipeline иллюстраций"""
        self.nanobanana_api_key = get_nanobanana_api_key()
        self.dalle_api_key = get_dalle_api_key()
        self.google_api_key = get_google_search_api_key()
        self.google_engine_id = get_google_search_engine_id()
        self.tcia_enabled = settings_manager.get("tcia_enabled", True)
        self.auto_illustration = settings_manager.get("auto_illustration", False)
        self.illustration_quality = settings_manager.get("illustration_quality", "high")
        self.brand_style = settings_manager.get("brand_style", "medical")

        # Список патологий в розыске
        self.pathology_search_list = self._load_pathology_search_list()

        # Метаданные изображений
        self.image_metadata = self._load_image_metadata()

        logger.info("IllustrationPipeline инициализирован")
        logger.info(f"NanoBanana API: {'✅' if self.nanobanana_api_key else '❌'}")
        logger.info(f"DALL-E 2 API: {'✅' if self.dalle_api_key else '❌'}")
        logger.info(f"Google Search API: {'✅' if self.google_api_key and self.google_engine_id else '❌'}")
        logger.info(f"TCIA API: {'✅' if self.tcia_enabled else '❌'}")

    def _load_pathology_search_list(self) -> List[Dict]:
        """Загрузка списка патологий в розыске"""
        search_list_file = Path("pathology_search_list.json")
        if search_list_file.exists():
            try:
                with open(search_list_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки списка патологий: {e}")
        return []

    def _save_pathology_search_list(self):
        """Сохранение списка патологий в розыске"""
        search_list_file = Path("pathology_search_list.json")
        try:
            with open(search_list_file, 'w', encoding='utf-8') as f:
                json.dump(self.pathology_search_list, f, ensure_ascii=False, indent=2)
            logger.info(f"Список патологий сохранен в {search_list_file}")
        except Exception as e:
            logger.error(f"Ошибка сохранения списка патологий: {e}")

    def _load_image_metadata(self) -> Dict[str, Dict]:
        """Загрузка метаданных изображений"""
        metadata_file = Path("image_metadata.json")
        if metadata_file.exists():
            try:
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки метаданных изображений: {e}")
        return {}

    def _save_image_metadata(self):
        """Сохранение метаданных изображений"""
        metadata_file = Path("image_metadata.json")
        try:
            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(self.image_metadata, f, ensure_ascii=False, indent=2)
            logger.info(f"Метаданные изображений сохранены в {metadata_file}")
        except Exception as e:
            logger.error(f"Ошибка сохранения метаданных изображений: {e}")

    def extract_images_from_pdf(self, pdf_path: str, progress_callback=None) -> List[Dict]:
        """Извлечение изображений из PDF с их описанием"""
        images = []
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                page = doc[page_num]
                image_list = page.get_images()
                
                for img_index, img in enumerate(image_list):
                    xref = img[0]
                    pix = fitz.Pixmap(doc, xref)
                    
                    if pix.n - pix.alpha < 4:  # GRAY или RGB
                        # Получаем текст вокруг изображения для контекста
                        text_around = self._get_text_around_image(page, img)
                        
                        image_info = {
                            "page": page_num + 1,
                            "index": img_index,
                            "xref": xref,
                            "width": pix.width,
                            "height": pix.height,
                            "text_around": text_around,
                            "classification": self._classify_image(text_around),
                            "pathology": self._extract_pathology(text_around),
                            "file_path": f"extracted_images/page_{page_num+1}_img_{img_index}.png"
                        }
                        
                        # Сохраняем изображение
                        os.makedirs("extracted_images", exist_ok=True)
                        pix.save(image_info["file_path"])

                        # Сохраняем метаданные изображения
                        metadata_key = image_info["file_path"]
                        self.image_metadata[metadata_key] = {
                            "page": image_info["page"],
                            "index": image_info["index"],
                            "width": image_info["width"],
                            "height": image_info["height"],
                            "text_around": image_info["text_around"],
                            "classification": image_info["classification"],
                            "pathology": image_info["pathology"],
                            "file_path": image_info["file_path"]
                        }

                        images.append(image_info)

                        # Логируем извлечение изображения
                        log_message = f"Извлечено изображение: {image_info['file_path']}"
                        logger.info(log_message)

                        # Вызываем callback для обновления UI
                        if progress_callback:
                            progress_callback(log_message)
                    
                    pix = None
            
            doc.close()

            # Сохраняем метаданные изображений
            self._save_image_metadata()

            logger.info(f"Извлечено {len(images)} изображений из PDF")
            return images
            
        except Exception as e:
            logger.error(f"Ошибка извлечения изображений: {e}")
            return []

    def _get_text_around_image(self, page, img) -> str:
        """Получение текста вокруг изображения для контекста"""
        try:
            # Получаем прямоугольник изображения
            img_rect = page.get_image_rects(img[0])[0]

            # Расширяем область поиска текста (больше область для лучшего контекста)
            expanded_rect = fitz.Rect(
                img_rect.x0 - 100, img_rect.y0 - 100,
                img_rect.x1 + 100, img_rect.y1 + 100
            )

            # Пробуем разные методы извлечения текста
            text = ""

            # Метод 1: get_textbox с расширенной областью
            try:
                text = page.get_textbox(expanded_rect)
            except:
                pass

            # Метод 2: get_text с параметрами для лучшего извлечения
            if not text.strip():
                try:
                    text = page.get_text("text", clip=expanded_rect)
                except:
                    pass

            # Очищаем и нормализуем текст
            if text:
                text = self._clean_extracted_text(text)

            return text.strip()

        except Exception as e:
            logger.warning(f"Не удалось получить текст вокруг изображения: {e}")
            return ""

    def _clean_extracted_text(self, text: str) -> str:
        """Очистка и нормализация извлеченного текста"""
        if not text:
            return ""

        import re

        # Удаляем лишние пробелы и переносы строк
        text = re.sub(r'\n+', ' ', text)  # Заменяем множественные переносы на пробелы
        text = re.sub(r'\s+', ' ', text)  # Заменяем множественные пробелы на один

        # Удаляем странные символы и артефакты OCR
        text = re.sub(r'[^\w\s\.,;:!?\-\(\)\[\]{}«»""''"\'а-яёa-z0-9]', '', text, flags=re.IGNORECASE | re.UNICODE)

        # Исправляем распространенные проблемы с переносами слов
        # Ищем паттерны вида "слово-\nслово" и объединяем их
        text = re.sub(r'(\w+)-\s+(\w+)', r'\1\2', text)

        # Удаляем слишком короткие фрагменты (менее 3 символов), которые могут быть шумом
        words = text.split()
        filtered_words = []
        for word in words:
            if len(word) >= 3 or word in ['рис', 'рис.', 'и', 'в', 'на', 'с', 'к', 'у', 'за', 'из', 'от', 'до', 'по', 'при', 'для', 'как', 'что', 'или', 'это', 'так']:
                filtered_words.append(word)

        text = ' '.join(filtered_words)

        # Удаляем повторяющиеся слова (часто бывает в PDF)
        words = text.split()
        cleaned_words = []
        prev_word = None
        for word in words:
            if word != prev_word:
                cleaned_words.append(word)
                prev_word = word

        text = ' '.join(cleaned_words)

        # Ограничиваем длину текста (берем первые 500 символов наиболее релевантного контента)
        if len(text) > 500:
            # Ищем начало описания рисунка
            patterns = [r'рис\.\s*\d+', r'рисунок\s*\d+', r'табл\.\s*\d+', r'схема\s*\d+']
            start_pos = len(text)
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    start_pos = min(start_pos, match.start())

            if start_pos < len(text) - 100:  # Если нашли паттерн не в конце
                text = text[start_pos:start_pos + 500]
            else:
                text = text[:500]

        return text.strip()

    def _classify_image(self, text_around: str) -> str:
        """Классификация изображения на энциклопедическое или клиническое"""
        text_lower = text_around.lower()
        
        # Ключевые слова для клинических изображений
        clinical_keywords = [
            "рентген", "снимок", "патология", "заболевание", "диагноз",
            "симптом", "лечение", "пациент", "клинический", "медицинский",
            "анализ", "исследование", "томография", "ультразвук", "мрт"
        ]
        
        # Ключевые слова для энциклопедических изображений
        encyclopedia_keywords = [
            "строение", "анатомия", "схема", "диаграмма", "иллюстрация",
            "структура", "орган", "система", "функция", "процесс"
        ]
        
        clinical_score = sum(1 for keyword in clinical_keywords if keyword in text_lower)
        encyclopedia_score = sum(1 for keyword in encyclopedia_keywords if keyword in text_lower)
        
        if clinical_score > encyclopedia_score:
            return "clinical"
        elif encyclopedia_score > 0:
            return "encyclopedia"
        else:
            return "unknown"

    def _extract_pathology(self, text_around: str) -> Optional[str]:
        """Извлечение названия патологии из текста"""
        # Простая эвристика для извлечения патологии
        # В реальной реализации можно использовать NLP
        text_lower = text_around.lower()
        
        # Список известных патологий
        pathologies = [
            "перелом", "остеопороз", "артрит", "артроз", "опухоль",
            "киста", "воспаление", "инфекция", "некроз", "склероз"
        ]
        
        for pathology in pathologies:
            if pathology in text_lower:
                return pathology
        
        return None

    def check_tcia_availability(self) -> bool:
        """Проверка доступности TCIA API"""
        try:
            response = requests.get(
                "https://services.cancerimagingarchive.net/services/v4/TCIA/query/getCollectionValues",
                timeout=5  # Быстрая проверка, 5 секунд
            )
            return response.status_code == 200
        except:
            return False

    def search_dicom_in_tcia(self, pathology: str, error_callback=None) -> List[Dict]:
        """Поиск DICOM файлов в TCIA по патологии"""
        if not self.tcia_enabled or not pathology:
            return []

        # Быстрая проверка доступности TCIA
        if not self.check_tcia_availability():
            logger.warning("TCIA API недоступен, пропуск поиска")
            if error_callback:
                error_callback("TCIA API недоступен")
            return []

        # Получаем таймаут из настроек, по умолчанию 30 секунд
        tcia_timeout = settings_manager.get('tcia_timeout', 30)
        max_retries = 2

        for attempt in range(max_retries):
            try:
                logger.info(f"Поиск в TCIA для патологии '{pathology}' (попытка {attempt + 1}/{max_retries})")

                # TCIA API endpoint
                base_url = "https://services.cancerimagingarchive.net/services/v4/TCIA/query"

                # Поиск коллекций по ключевым словам
                search_url = f"{base_url}/getCollectionValues"
                response = requests.get(search_url, timeout=tcia_timeout)

                if response.status_code == 200:
                    collections = response.json()
                    matching_collections = []

                    for collection in collections:
                        if pathology.lower() in collection.get("Collection", "").lower():
                            matching_collections.append(collection)

                    logger.info(f"Найдено {len(matching_collections)} коллекций в TCIA для патологии '{pathology}'")
                    return matching_collections
                else:
                    logger.warning(f"Ошибка запроса к TCIA API: {response.status_code}")
                    if attempt == max_retries - 1:  # Последняя попытка
                        return []

            except requests.exceptions.Timeout:
                error_msg = f"Таймаут подключения к TCIA (попытка {attempt + 1}/{max_retries}): {tcia_timeout} сек"
                logger.warning(error_msg)
                if attempt == max_retries - 1:  # Последняя попытка
                    if error_callback:
                        error_callback(error_msg)
                    return []
                # Ждем перед следующей попыткой
                time.sleep(2)

            except requests.exceptions.ConnectionError:
                error_msg = f"Ошибка подключения к TCIA (попытка {attempt + 1}/{max_retries})"
                logger.warning(error_msg)
                if attempt == max_retries - 1:  # Последняя попытка
                    if error_callback:
                        error_callback(error_msg)
                    return []
                # Ждем перед следующей попыткой
                time.sleep(2)

            except Exception as e:
                error_msg = f"Ошибка поиска в TCIA: {e}"
                logger.error(error_msg)
                if error_callback:
                    error_callback(error_msg)
                return []

        return []

    def search_images_google(self, query: str, num_results: int = 5, error_callback=None) -> List[Dict]:
        """Поиск изображений через Google Custom Search API"""
        if not self.google_api_key or not self.google_engine_id:
            logger.warning("Google Custom Search API не настроен")
            return []
        
        try:
            url = "https://www.googleapis.com/customsearch/v1"
            params = {
                "key": self.google_api_key,
                "cx": self.google_engine_id,
                "q": query,
                "searchType": "image",
                "num": num_results,
                "safe": "medium",
                "imgType": "photo",
                "imgSize": "large"
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                images = []
                
                for item in data.get("items", []):
                    image_info = {
                        "title": item.get("title", ""),
                        "url": item.get("link", ""),
                        "thumbnail": item.get("image", {}).get("thumbnailLink", ""),
                        "context": item.get("image", {}).get("contextLink", ""),
                        "size": item.get("image", {}).get("byteSize", 0)
                    }
                    images.append(image_info)
                
                logger.info(f"Найдено {len(images)} изображений в Google для запроса '{query}'")
                return images
            else:
                error_msg = f"Ошибка Google Custom Search API: {response.status_code}"
                logger.error(error_msg)
                if error_callback:
                    error_callback(error_msg)
                return []

        except Exception as e:
            error_msg = f"Ошибка поиска в Google: {e}"
            logger.error(error_msg)
            if error_callback:
                error_callback(error_msg)
            return []

    def generate_image_nanobanana(self, prompt: str, style: str = "medical") -> Optional[str]:
        """Генерация изображения через NanoBanana API"""
        if not self.nanobanana_api_key:
            logger.warning("NanoBanana API ключ не настроен")
            return None

        try:
            # Здесь будет интеграция с NanoBanana API
            # Пока заглушка
            logger.info(f"Генерация изображения через NanoBanana: {prompt}")
            logger.info(f"Стиль: {style}")

            # В реальной реализации:
            # 1. Отправка запроса к NanoBanana API
            # 2. Получение URL сгенерированного изображения
            # 3. Скачивание и сохранение изображения

            return None  # Заглушка

        except Exception as e:
            logger.error(f"Ошибка генерации изображения: {e}")
            return None

    def generate_image_dalle(self, prompt: str, size: str = "512x512", style: str = "natural") -> Optional[str]:
        """Генерация изображения через DALL-E 2 API"""
        if not self.dalle_api_key:
            logger.warning("DALL-E 2 API ключ не настроен")
            return None

        try:
            import openai

            # Настраиваем OpenAI клиент для DALL-E
            client = openai.OpenAI(api_key=self.dalle_api_key)

            # Создаем промпт для медицинского изображения
            medical_prompt = f"Medical illustration in {style} style: {prompt}"
            if "medical" in style.lower():
                medical_prompt += ". Professional medical diagram, clear and educational."

            logger.info(f"Генерация изображения через DALL-E 2: {medical_prompt}")
            logger.info(f"Размер: {size}, Стиль: {style}")

            # Создаем изображение через DALL-E 2
            response = client.images.generate(
                model="dall-e-2",
                prompt=medical_prompt,
                size=size,
                quality="standard",
                n=1,
            )

            # Получаем URL сгенерированного изображения
            image_url = response.data[0].url

            if image_url:
                # Скачиваем и сохраняем изображение
                import uuid
                image_response = requests.get(image_url)
                if image_response.status_code == 200:
                    # Создаем уникальное имя файла
                    filename = f"dalle_redraw_{uuid.uuid4().hex[:8]}.png"
                    filepath = os.path.join("extracted_images", filename)

                    os.makedirs("extracted_images", exist_ok=True)
                    with open(filepath, 'wb') as f:
                        f.write(image_response.content)

                    logger.info(f"Изображение сохранено: {filepath}")
                    return filepath
                else:
                    logger.error(f"Не удалось скачать изображение: {image_response.status_code}")
                    return None
            else:
                logger.error("Не получен URL изображения от DALL-E")
                return None

        except ImportError:
            logger.error("OpenAI библиотека не установлена. Установите ее командой: pip install openai")
            return None
        except Exception as e:
            logger.error(f"Ошибка генерации изображения через DALL-E 2: {e}")
            return None

    def process_illustrations(self, pdf_path: str, progress_callback=None, error_callback=None) -> Dict:
        """Основной процесс обработки иллюстраций"""
        logger.info(f"Начало обработки иллюстраций для {pdf_path}")

        # Извлекаем изображения из PDF
        images = self.extract_images_from_pdf(pdf_path, progress_callback)
        
        results = {
            "total_images": len(images),
            "processed_images": 0,
            "generated_images": 0,
            "search_results": 0,
            "pathologies_found": [],
            "pathologies_missing": [],
            "found_images": []  # Список найденных изображений с патологиями
        }
        
        for image in images:
            logger.info(f"Обработка изображения: {image['file_path']}")
            
            if image["classification"] == "clinical" and image["pathology"]:
                # Поиск DICOM в TCIA
                dicom_results = self.search_dicom_in_tcia(image["pathology"], error_callback)
                
                if dicom_results:
                    results["search_results"] += len(dicom_results)
                    results["pathologies_found"].append(image["pathology"])
                    # Сохраняем найденные DICOM изображения
                    for dicom in dicom_results:
                        if isinstance(dicom, dict):
                            results["found_images"].append({
                                "pathology": image["pathology"],
                                "source": "TCIA",
                                "title": dicom.get("Collection", "DICOM Collection"),
                                "url": None,  # TCIA не предоставляет прямые URL изображений
                                "thumbnail": None
                            })
                        else:
                            logger.warning(f"Пропущен некорректный DICOM результат: {type(dicom)} - {dicom}")
                    logger.info(f"Найдены DICOM файлы для патологии: {image['pathology']}")
                else:
                    # Поиск в Google Images
                    google_results = self.search_images_google(
                        f"{image['pathology']} medical imaging x-ray",
                        error_callback=error_callback
                    )

                    if google_results:
                        results["search_results"] += len(google_results)
                        # Сохраняем найденные Google изображения
                        for google_img in google_results:
                            if isinstance(google_img, dict):
                                results["found_images"].append({
                                    "pathology": image["pathology"],
                                    "source": "Google Images",
                                    "title": google_img.get("title", "Medical Image"),
                                    "url": google_img.get("url"),  # Исправлено: было "link"
                                    "thumbnail": google_img.get("thumbnail")  # Исправлено: убираем лишний .get("src")
                                })
                            else:
                                logger.warning(f"Пропущен некорректный Google результат: {type(google_img)} - {google_img}")
                        logger.info(f"Найдены изображения в Google для патологии: {image['pathology']}")
                    else:
                        # Добавляем в список патологий в розыске
                        if image["pathology"] not in [p["pathology"] for p in self.pathology_search_list]:
                            self.pathology_search_list.append({
                                "pathology": image["pathology"],
                                "context": image["text_around"],
                                "date_added": str(Path().cwd()),
                                "status": "searching"
                            })
                            results["pathologies_missing"].append(image["pathology"])
                            logger.warning(f"Патология добавлена в розыск: {image['pathology']}")
            
            elif image["classification"] == "encyclopedia":
                # Генерация современной энциклопедической иллюстрации
                prompt = f"Modern medical illustration: {image['text_around'][:100]}"
                generated_image = self.generate_image_nanobanana(prompt, self.brand_style)
                
                if generated_image:
                    results["generated_images"] += 1
                    logger.info(f"Сгенерирована энциклопедическая иллюстрация")
            
            results["processed_images"] += 1
        
        # Сохраняем обновленный список патологий в розыске
        self._save_pathology_search_list()
        
        logger.info(f"Обработка завершена: {results}")
        return results

    def get_pathology_search_list(self) -> List[Dict]:
        """Получение списка патологий в розыске"""
        return self.pathology_search_list

    def redraw_image_with_nanobanana(self, image_info: Dict, custom_prompt: Optional[str] = None, size: str = "512x512") -> Optional[str]:
        """Перерисовка изображения через Google Gemini (Nano Banana)"""
        if not GOOGLE_GENAI_AVAILABLE:
            logger.error("Google GenAI library not installed. Install with: pip install google-genai")
            return None

        if not self.nanobanana_api_key:
            logger.warning("Nano Banana API ключ не настроен")
            return None

        try:
            # Создаем промпт на основе описания изображения
            if custom_prompt:
                prompt = custom_prompt
            else:
                text_around = image_info.get("text_around", "")
                pathology = image_info.get("pathology", "")
                classification = image_info.get("classification", "unknown")

                # Создаем промпт в зависимости от типа изображения
                if classification == "clinical" and pathology:
                    prompt = f"Medical X-ray or clinical imaging showing {pathology}. Professional medical diagnostic image, clear anatomical details, educational for medical students."
                elif classification == "encyclopedia":
                    prompt = f"Medical illustration diagram: {text_around[:200]}. Clean, professional medical illustration, anatomical accuracy, educational style."
                else:
                    prompt = f"Medical illustration: {text_around[:200]}. Professional healthcare visual, clear and informative."

                # Добавляем стиль брендинга
                if self.brand_style == "medical":
                    prompt += " Modern medical illustration style, professional healthcare design."
                elif self.brand_style == "modern":
                    prompt += " Contemporary medical illustration with clean lines and modern aesthetic."
                elif self.brand_style == "classic":
                    prompt += " Classic medical textbook illustration style, detailed and traditional."

            # Парсим размер изображения
            try:
                if "x" in size:
                    width, height = map(int, size.split("x"))
                else:
                    # Если размер передан как строка типа "512x512"
                    width = height = int(size.split("x")[0]) if "x" in size else int(size)
            except (ValueError, IndexError):
                width = height = 512  # fallback

            # Ограничиваем размер (Gemini имеет ограничения)
            if width > 2048:
                width = 2048
            if height > 2048:
                height = 2048

            logger.info(f"Перерисовка изображения: {image_info.get('file_path', 'unknown')}")
            logger.info(f"Промпт: {prompt}")
            logger.info(f"Размер: {width}x{height}")

            # Инициализация клиента через API key
            client = genai.Client(api_key=self.nanobanana_api_key)

            # Выбор модели для генерации изображений
            model = "gemini-3-pro-image-preview"  # Модель с меньшей цензурой для генерации изображений

            logger.info(f"Использование модели: {model}")

            # Генерация изображения через Google Gemini API
            resp = client.models.generate_content(
                model=model,
                contents=[prompt]
            )

            # Обработка ответа
            if hasattr(resp, 'candidates') and resp.candidates:
                candidate = resp.candidates[0]

                if hasattr(candidate, 'content') and candidate.content:
                    content = candidate.content

                    if hasattr(content, 'parts') and content.parts:
                        # Ищем часть с изображением (берем только ПЕРВОЕ изображение)
                        for part in content.parts:
                            # Проверяем inline_data (основная структура для изображений)
                            if hasattr(part, 'inline_data') and part.inline_data is not None:
                                inline_data = part.inline_data

                                # Проверяем наличие данных
                                if hasattr(inline_data, 'data') and inline_data.data is not None:
                                    # Данные уже в байтах, не нужно декодировать из base64
                                    img_bytes = inline_data.data

                                    # Проверяем, что это действительно изображение (простая проверка)
                                    if len(img_bytes) > 100:  # Минимальный размер для изображения
                                        # Сохраняем изображение
                                        output_filename = f"redrawn_{Path(image_info['file_path']).stem}.png"
                                        output_path = Path("extracted_images") / output_filename

                                        output_path.write_bytes(img_bytes)

                                        logger.info(f"Изображение сохранено: {output_path} (размер: {len(img_bytes)} байт)")
                                        logger.info("Обработка остановлена после генерации первого изображения")
                                        return str(output_path)
                                    else:
                                        logger.warning(f"Получены некорректные данные изображения, размер: {len(img_bytes)} байт")
                                else:
                                    logger.warning("inline_data.data is None - изображение не сгенерировано")
                            else:
                                # Проверяем, есть ли текстовая часть с объяснением отказа
                                if hasattr(part, 'text') and part.text:
                                    logger.warning(f"API вернул текстовый ответ вместо изображения: {part.text[:200]}...")
                                    # Если это сообщение об отказе в генерации, попробуем другой подход
                                    if "policy" in part.text.lower() or "medical" in part.text.lower():
                                        logger.warning("API отказал в генерации из-за медицинского контента")

            # Если не нашли изображение в стандартной структуре
            logger.error("Изображение не найдено в ответе API")
            logger.debug(f"Структура ответа: candidates={len(resp.candidates) if hasattr(resp, 'candidates') else 'N/A'}")

            return None

        except Exception as e:
            logger.error(f"Ошибка перерисовки изображения через Google Gemini: {e}")
            return None

    def get_image_metadata(self, image_path: str) -> Optional[Dict]:
        """Получение метаданных изображения по пути к файлу"""
        return self.image_metadata.get(image_path)

    def _generate_fallback_prompt(self, image_filename: str) -> str:
        """Генерация базового промпта при отсутствии метаданных"""
        try:
            # Извлекаем информацию из имени файла
            # Формат: page_X_img_Y.png
            parts = image_filename.replace('.png', '').split('_')
            if len(parts) >= 3 and parts[0] == 'page' and parts[2] == 'img':
                page_num = parts[1]
                img_num = parts[3] if len(parts) > 3 else parts[2]

                # Создаем базовый промпт для медицинского изображения
                base_prompt = f"Medical illustration from page {page_num}, image {img_num}. Professional healthcare visual showing anatomical or pathological medical content, clear and educational for medical students."

                # Добавляем специфические элементы в зависимости от номера страницы
                # (это грубая эвристика, но лучше чем ничего)
                try:
                    page_int = int(page_num)
                    if page_int <= 50:
                        base_prompt += " Focus on bone structure, skeletal system anatomy."
                    elif page_int <= 100:
                        base_prompt += " Focus on pathological conditions, diseases of bones and joints."
                    elif page_int <= 150:
                        base_prompt += " Focus on diagnostic imaging, X-rays, clinical cases."
                    elif page_int <= 200:
                        base_prompt += " Focus on treatment methods, surgical procedures."
                    else:
                        base_prompt += " Focus on advanced medical imaging and diagnostics."
                except ValueError:
                    pass

                return base_prompt
            else:
                # Неизвестный формат имени файла
                return f"Medical illustration: {image_filename}. Professional healthcare visual, clear and educational for medical students."

        except Exception as e:
            logger.warning(f"Ошибка генерации fallback промпта для {image_filename}: {e}")
            return f"Medical illustration from radiology textbook. Professional healthcare visual, clear and educational."

    def create_basic_metadata_for_all_images(self):
        """Создание базовых метаданных для всех изображений в extracted_images"""
        import os

        if not os.path.exists("extracted_images"):
            logger.warning("Папка extracted_images не существует")
            return

        # Получаем все PNG файлы
        image_files = [f for f in os.listdir("extracted_images") if f.endswith('.png')]
        logger.info(f"Найдено {len(image_files)} изображений")

        # Создаем базовые метаданные для изображений без них
        new_metadata_count = 0
        for image_file in image_files:
            image_path = f"extracted_images/{image_file}"
            if image_path not in self.image_metadata:
                # Создаем базовые метаданные
                try:
                    # Извлекаем информацию из имени файла
                    parts = image_file.replace('.png', '').split('_')
                    page_num = 1
                    img_index = 0

                    if len(parts) >= 2 and parts[0] == 'page':
                        try:
                            page_num = int(parts[1])
                        except ValueError:
                            pass

                    if len(parts) >= 4 and parts[2] == 'img':
                        try:
                            img_index = int(parts[3])
                        except ValueError:
                            pass

                    # Получаем размеры изображения
                    try:
                        from PIL import Image
                        with Image.open(f"extracted_images/{image_file}") as img:
                            width, height = img.size
                    except:
                        width, height = 800, 600  # значения по умолчанию

                    # Создаем базовые метаданные
                    self.image_metadata[image_path] = {
                        "page": page_num,
                        "index": img_index,
                        "width": width,
                        "height": height,
                        "text_around": "",  # Пустой текст - будет использоваться fallback промпт
                        "classification": "unknown",
                        "pathology": None,
                        "file_path": image_path
                    }
                    new_metadata_count += 1

                except Exception as e:
                    logger.warning(f"Ошибка создания метаданных для {image_file}: {e}")

        if new_metadata_count > 0:
            self._save_image_metadata()
            logger.info(f"Создано базовых метаданных для {new_metadata_count} изображений")
        else:
            logger.info("Все изображения уже имеют метаданные")

    def clear_pathology_search_list(self):
        """Очистка списка патологий в розыске"""
        self.pathology_search_list = []
        self._save_pathology_search_list()
        logger.info("Список патологий в розыске очищен")

def main():
    """Тестовая функция"""
    pipeline = IllustrationPipeline()
    
    # Тест с существующим PDF
    pdf_path = "input/Кости_глава_1.pdf"
    if os.path.exists(pdf_path):
        results = pipeline.process_illustrations(pdf_path)
        print(f"Результаты обработки: {results}")
    else:
        print(f"Файл {pdf_path} не найден")

if __name__ == "__main__":
    main()
