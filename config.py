"""
Конфигурационный файл для приложения Text Re-phraser
"""

# Основные настройки приложения
APP_TITLE = "Text Re-phraser"
APP_ICON = "📝"
LAYOUT = "wide"

# Настройки перефразирования текста
DEFAULT_TEMPERATURE = 0.4
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 1.0
MAX_TOKENS = 2000

# Настройки файлов
SUPPORTED_FILE_TYPES = ["pdf", "txt", "md", "docx"]
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Настройки изображений
EXTRACTED_IMAGES_DIR = "extracted_images"
MAX_IMAGES_IN_CAROUSEL = 10
DEFAULT_IMAGE_SIZE = "512x512"

# Настройки табов
TAB_NAMES = ["🔄 Перефразирование", "📊 Результаты", "🎨 Иллюстрации", "⚙️ Настройки"]

# Настройки темы по умолчанию
DEFAULT_THEME = "РЕНТГЕНОДИАГНОСТИКА ЗАБОЛЕВАНИЙ КОСТЕЙ И СУСТАВОВ"

# Настройки брендинга для изображений
BRAND_STYLES = {
    "medical": "Modern medical illustration style, professional healthcare design",
    "modern": "Contemporary medical illustration with clean lines and modern aesthetic",
    "classic": "Classic medical textbook illustration style, detailed and traditional"
}

# Настройки модели OpenAI
DEFAULT_MODEL = "gpt-4o"

# Настройки API ключей (маски для отображения)
API_KEY_MASK_PREFIX = 8
API_KEY_MASK_SUFFIX = 4

# Настройки прогресса
PROGRESS_UPDATE_INTERVAL = 0.1

# Настройки кеширования
CACHE_TTL_SECONDS = 3600  # 1 час

# Настройки логирования
LOG_LEVEL = "INFO"
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
