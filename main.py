import argparse
import logging
import os
import re
import markdown
import fitz  # PyMuPDF
from openai import OpenAI
from typing import List, Optional
import time
from settings_manager import settings_manager, get_api_key, set_api_key, has_api_key

# Конфигурация логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация OpenAI (теперь хранится в settings.json)

class TextProcessor:
    def __init__(self, api_key: str, temperature: float = 0.4, include_research: bool = False):
        """Инициализирует процессор текста с клиентом OpenAI."""
        self.blocks = []
        self.temperature = temperature
        # Флаг включения информации о новых исследованиях
        self.include_research = False  # default, will be set in constructor call
        try:
            self.client = OpenAI(api_key=api_key)
            logger.info(f"Клиент OpenAI успешно инициализирован с temperature={temperature}")
        except Exception as e:
            logger.error(f"Ошибка инициализации клиента OpenAI: {str(e)}")
            raise Exception("Не удалось инициализировать клиент OpenAI. Проверьте API-ключ и соединение.")

    def read_text_file(self, file_path: str) -> str:
        """Читает текст из файлов .txt или .md."""
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                content = file.read()
                if file_path.endswith('.md'):
                    content = markdown.markdown(content)
                self.blocks = [para.strip() for para in content.split('\n\n') if para.strip() and not para.strip().isdigit()]
                logger.info(f"Извлечено {len(self.blocks)} блоков из текстового файла {file_path}")
                return '\n\n'.join(self.blocks)
        except Exception as e:
            logger.error(f"Ошибка при чтении текстового файла {file_path}: {e}")
            return ""

    def read_docx_file(self, file_path: str) -> str:
        """Читает текст из файлов .docx."""
        try:
            import docx
            doc = docx.Document(file_path)
            self.blocks = [para.text.strip() for para in doc.paragraphs if para.text.strip() and not para.text.strip().isdigit()]
            logger.info(f"Извлечено {len(self.blocks)} блоков из файла DOCX {file_path}")
            return '\n\n'.join(self.blocks)
        except Exception as e:
            logger.error(f"Ошибка при чтении файла DOCX {file_path}: {e}")
            return ""

    def read_pdf_file(self, file_path: str, output_original: str = 'output/original.txt') -> str:
        """Извлекает текст из PDF, разбивает на абзацы и сохраняет в original.txt."""
        try:
            doc = fitz.open(file_path)
            blocks = []
            block_number = 1

            for page in doc:
                page_blocks = page.get_text("blocks", sort=False)
                left_column = []
                right_column = []
                page_width = page.rect.width
                column_threshold = page_width / 2

                for block in page_blocks:
                    x0, y0, x1, y1, block_text = block[:5]
                    block_text = block_text.strip()
                    if block_text and not block_text.isdigit():
                        if x0 < column_threshold:
                            left_column.append((y0, block_text))
                        else:
                            right_column.append((y0, block_text))

                left_column.sort(key=lambda x: x[0])
                right_column.sort(key=lambda x: x[0])

                for _, block_text in left_column:
                    blocks.append({'number': block_number, 'text': block_text})
                    block_number += 1
                for _, block_text in right_column:
                    blocks.append({'number': block_number, 'text': block_text})
                    block_number += 1

            os.makedirs(os.path.dirname(output_original), exist_ok=True)
            with open(output_original, 'w', encoding='utf-8') as f:
                for block in blocks:
                    if len(block['text']) > 2:
                        f.write(f"Блок {block['number']}: {block['text']}\n\n")

            doc.close()
            self.blocks = [block['text'] for block in blocks]
            logger.info(f"Извлечено {len(self.blocks)} блоков из файла PDF {file_path}, сохранено в {output_original}")
            return '\n\n'.join(self.blocks)
        except Exception as e:
            logger.error(f"Ошибка при чтении файла PDF {file_path}: {e}")
            return ""

    def read_input_file(self, file_path: str, output_original: str = 'output/original.txt') -> str:
        """Читает входной файл в зависимости от его расширения."""
        ext = os.path.splitext(file_path)[1].lower()
        if ext in [".txt", ".md"]:
            return self.read_text_file(file_path)
        elif ext == ".docx":
            return self.read_docx_file(file_path)
        elif ext == ".pdf":
            return self.read_pdf_file(file_path, output_original)
        else:
            logger.error(f"Неподдерживаемый формат файла: {ext}")
            return ""

    def clean_block(self, block: str) -> str:
        """Очищает блок текста, удаляя все после символов '===' или '<s>'."""
        cleaned = re.split(r'===|<s>', block)[0].strip()
        logger.debug(f"Очищенный блок: {cleaned[:50]}...")
        return cleaned

    def split_block(self, block: str, max_length: int = 500) -> List[str]:
        """Разделяет длинный блок текста на части, если он превышает max_length символов."""
        if len(block) <= max_length:
            return [block]
        words = block.split()
        chunks = []
        current_chunk = []
        current_length = 0
        for word in words:
            if current_length + len(word) + 1 > max_length:
                chunks.append(" ".join(current_chunk))
                current_chunk = [word]
                current_length = len(word) + 1
            else:
                current_chunk.append(word)
                current_length += len(word) + 1
        if current_chunk:
            chunks.append(" ".join(current_chunk))
        logger.debug(f"Блок разделён на {len(chunks)} частей")
        return chunks

    def paraphrase_block(self, block: str, theme: str, block_index: int) -> str:
        """Перефразирует отдельный блок текста с использованием API OpenAI."""
        chunks = self.split_block(block, max_length=500)
        paraphrased_chunks = []
        for chunk_idx, chunk in enumerate(chunks, 1):
            prompt = f"""
            Перефразируйте текст на русском языке, строго сохраняя академический стиль и точную научную терминологию для публикации в области {theme}. 
            Следуйте этим правилам:
            1. Сохраняйте исходный смысл, не добавляйте новых фактов и не удаляйте существующую информацию.
            2. Поддерживайте структуру текста: сохраняйте количество предложений и их порядок, избегая излишнего переструктурирования.
            3. Если текст начинается или заканчивается дефисом, оставьте неизменным. Если в начале блока первое слово не понятно и с маленькой буквы, оставь его неизменным.
            4. Если текст слишком короток, отсутствует или содержит бессмысленные знаки или цифры, верните его неизменным.
            5. Удалите префиксы типа "Блок n", если они присутствуют.
            6. Верните только перефразированный текст без дополнительных комментариев.
            7. Перефразированный текст должен быть близок по объёму к оригиналу (±10% слов).
            Текст: {chunk}
            """
            # Добавить информацию о новых исследованиях, если включено
            if self.include_research:
                prompt += ("\n\nТакже включите информацию о новых исследованиях из различных известных источников, "
                          "чтобы сделать текст более актуальным и информативным.")
            try:
                response = self.client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": "Вы — эксперт по перефразированию научных текстов на русском языке в академическом стиле."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=2000,
                    temperature=self.temperature
                )
                paraphrased_text = response.choices[0].message.content.strip()
                if not paraphrased_text:
                    logger.warning(f"Часть {chunk_idx} блока {block_index}: пустой ответ от API, возвращается оригинал")
                    paraphrased_chunks.append(chunk)
                else:
                    paraphrased_chunks.append(paraphrased_text)
                    logger.info(f"Часть {chunk_idx} блока {block_index} успешно перефразирована")
            except Exception as e:
                logger.error(f"Ошибка перефразирования части {chunk_idx} блока {block_index}: {str(e)}")
                if "rate_limit_exceeded" in str(e) or "insufficient_quota" in str(e):
                    logger.info("Превышен лимит запросов или квота. Ожидание 30 секунд...")
                    time.sleep(30)
                    try:
                        response = self.client.chat.completions.create(
                            model="gpt-4o",
                            messages=[
                                {"role": "system", "content": "Вы — эксперт по перефразированию научных текстов на русском языке в академическом стиле."},
                                {"role": "user", "content": prompt}
                            ],
                            max_tokens=2000,
                            temperature=self.temperature
                        )
                        paraphrased_text = response.choices[0].message.content.strip()
                        if not paraphrased_text:
                            logger.warning(f"Часть {chunk_idx} блока {block_index}: пустой ответ при повторной попытке, возвращается оригинал")
                            paraphrased_chunks.append(chunk)
                        else:
                            paraphrased_chunks.append(paraphrased_text)
                            logger.info(f"Часть {chunk_idx} блока {block_index} успешно перефразирована после повторной попытки")
                    except Exception as retry_e:
                        logger.error(f"Повторная попытка не удалась для части {chunk_idx} блока {block_index}: {retry_e}")
                        paraphrased_chunks.append(chunk)
                else:
                    paraphrased_chunks.append(chunk)
        return " ".join(paraphrased_chunks)

    def process_text(self, text: str, theme: str) -> str:
        """Обрабатывает текст: перефразирует блоки."""
        blocks = [block.strip() for block in text.split('\n\n') if block.strip()]
        processed_blocks = []
        for i, block in enumerate(blocks, 1):
            if not block or len(block) <= 2:
                logger.info(f"Блок {i} пустой или слишком короткий, пропущен")
                continue
            cleaned_block = self.clean_block(block)
            if not cleaned_block:
                logger.info(f"Блок {i} после очистки пуст, пропущен")
                continue
            paraphrased_block = self.paraphrase_block(cleaned_block, theme, i)
            if paraphrased_block == cleaned_block:
                logger.warning(f"Блок {i} не был перефразирован, сохранён оригинал")
            processed_blocks.append(f"Блок {i}: {paraphrased_block}")
        return '\n\n'.join(processed_blocks) if processed_blocks else ""

    def save_file(self, content: str, output_path: str):
        """Сохраняет перефразированный текст в .txt."""
        try:
            output_dir = os.path.dirname(output_path) or '.'
            os.makedirs(output_dir, exist_ok=True)
            if not os.access(output_dir, os.W_OK):
                raise PermissionError(f"Нет прав на запись в директорию: {output_dir}")
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except PermissionError:
                    raise Exception(f"Ошибка доступа: невозможно перезаписать {output_path}")
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"Текст сохранён в {output_path}")
        except Exception as e:
            logger.error(f"Ошибка при сохранении файла {output_path}: {e}")
            raise

    def process(self, input_path: str, output_report_path: str, theme: str = "РЕНТГЕНОДИАГНОСТИКА ЗАБОЛЕВАНИЙ КОСТЕЙ И СУСТАВОВ"):
        """Основная функция обработки: чтение, перефразирование и сохранение."""
        try:
            text = self.read_input_file(input_path)
            if not text:
                raise Exception("Не удалось извлечь текст из файла")
            processed_text = self.process_text(text, theme)
            if not processed_text:
                raise Exception("Не удалось обработать текст")
            self.save_file(processed_text, output_report_path)
            return True, "Обработка успешно завершена"
        except Exception as e:
            logger.error(f"Ошибка обработки: {str(e)}")
            return False, f"Ошибка: {str(e)}"

def main():
    """Основная функция для обработки текста из файла или аргументов командной строки."""
    parser = argparse.ArgumentParser(description="Обработка текста: перефразирование с использованием API OpenAI.")
    parser.add_argument("--input-file", type=str, default=None,
                        help="Путь к входному файлу (.pdf, .txt, .md, .docx) (по умолчанию: запрашивается у пользователя)")
    parser.add_argument("--output-file", type=str, default="output/paraphrased.txt",
                        help="Путь к выходному файлу .txt (по умолчанию: output/paraphrased.txt)")
    parser.add_argument("--theme", type=str, default=None,
                        help="Тематика текста (по умолчанию: запрашивается у пользователя)")
    parser.add_argument("--api-key", type=str, default=None,
                        help="API-ключ OpenAI (по умолчанию: используется сохраненный или запрашивается)")
    parser.add_argument("--temperature", type=float, default=None,
                        help="Уровень творческого переформулирования от 0.0 до 1.0 (по умолчанию: используется сохраненный или 0.4)")
    parser.add_argument("--set-api-key", type=str, default=None,
                        help="Сохранить новый API-ключ в настройках")
    parser.add_argument("--clear-api-key", action="store_true",
                        help="Удалить сохраненный API-ключ из настроек")
    parser.add_argument("--include-research", action="store_true",
                        help="Включить добавление информации о новых исследованиях из известных источников")
    parser.add_argument("--show-settings", action="store_true",
                        help="Показать текущие настройки")
    args = parser.parse_args()

    try:
        # Обработка команд управления настройками
        if args.set_api_key:
            set_api_key(args.set_api_key)
            print("✅ API-ключ успешно сохранен в настройках")
            return
        elif args.clear_api_key:
            settings_manager.clear_api_key()
            print("🗑️ API-ключ удален из настроек")
            return
        elif args.show_settings:
            print("⚙️ Текущие настройки:")
            settings = settings_manager.get_all_settings()
            for key, value in settings.items():
                if key == "openai_api_key" and value:
                    if len(value) > 12:
                        print(f"  {key}: {value[:8]}...{value[-4:]}")
                    else:
                        print(f"  {key}: {value}")
                elif key == "openai_api_key" and not value:
                    print(f"  {key}: (не установлен)")
                else:
                    print(f"  {key}: {value}")
            return

        # Запрашиваем путь к входному файлу, если не указан в аргументах
        input_file = args.input_file if args.input_file else input("Введите путь к входному файлу (.pdf, .txt, .md, .docx): ").strip()
        if not input_file:
            input_file = "input/Кости_глава_1.pdf"
            logger.info(f"Путь к входному файлу не указан, используется значение по умолчанию: {input_file}")

        # Запрашиваем тему, если не указана в аргументах
        theme = args.theme if args.theme else input("Введите тематику текста: ").strip()
        if not theme:
            theme = "РЕНТГЕНОДИАГНОСТИКА ЗАБОЛЕВАНИЙ КОСТЕЙ И СУСТАВОВ"
            logger.info(f"Тема не указана, используется значение по умолчанию: {theme}")

        # Запрашиваем API-ключ, если не указан в аргументах
        api_key = args.api_key
        if not api_key:
            # Сначала проверяем сохраненный ключ в настройках
            if has_api_key():
                api_key = get_api_key()
                logger.info("Используется сохраненный API-ключ из настроек")
            else:
                # Запрашиваем у пользователя
                api_key = input("Введите API-ключ OpenAI: ").strip()
                if api_key:
                    # Предлагаем сохранить ключ
                    save_key = input("Сохранить API-ключ для будущих запусков? (y/n): ").strip().lower()
                    if save_key in ['y', 'yes', 'да']:
                        set_api_key(api_key)
                        logger.info("API-ключ сохранен в настройках")
                else:
                    logger.error("API-ключ не указан")
                    print("❌ API-ключ обязателен для работы. Получите его на https://platform.openai.com/")
                    return

        # Получаем значение temperature
        temperature = args.temperature
        if temperature is None:
            # Используем сохраненное значение или значение по умолчанию
            temperature = settings_manager.get("temperature", 0.4)
            logger.info(f"Используется temperature из настроек: {temperature}")
        else:
            # Валидация значения
            if not 0.0 <= temperature <= 1.0:
                logger.error("Temperature должна быть в диапазоне от 0.0 до 1.0")
                print("❌ Temperature должна быть в диапазоне от 0.0 до 1.0")
                return
            logger.info(f"Используется temperature из аргументов: {temperature}")

        # Получаем флаг include_research
        include_research = args.include_research
        if include_research is None or include_research is False:
            include_research = settings_manager.get("include_research", False)
            logger.info(f"Используется include_research из настроек: {include_research}")
        else:
            logger.info(f"Используется include_research из аргументов: {include_research}")
        # Сохраняем флаг include_research
        settings_manager.set("include_research", include_research)

        processor = TextProcessor(api_key=api_key, temperature=temperature, include_research=include_research)
        success, message = processor.process(input_file, args.output_file, theme)
        logger.info(message)

        # Выводим информацию о сохранённых файлах
        if success:
            print(f"Итоговые файлы сохранены:\n- Извлечённый текст: output/original.txt\n- Перефразированный текст: {args.output_file}")
        else:
            print("Обработка завершилась с ошибкой. Проверьте логи для подробностей.")

    except Exception as e:
        logger.error(f"Ошибка в main: {str(e)}")
        logger.info("Рекомендации по устранению ошибки:")
        logger.info("1. Убедитесь, что API-ключ OpenAI действителен: https://platform.openai.com/")
        logger.info("2. Проверьте наличие интернет-соединения.")
        logger.info("3. Убедитесь, что у вас достаточно квоты для API OpenAI.")
        logger.info("4. Проверьте, что установлены все библиотеки: `pip install -r requirements.txt`")
        print("Обработка завершилась с ошибкой. Проверьте логи для подробностей.")

if __name__ == "__main__":
    main()