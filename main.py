import argparse
import json
import logging
import os
import re
import markdown
import fitz  # PyMuPDF
from openai import OpenAI
from typing import List, Optional, Tuple, Dict, Any
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    import tiktoken
except Exception:
    tiktoken = None
from settings_manager import (
    settings_manager,
    get_api_key,
    set_api_key,
    has_api_key,
    has_active_api_key,
    get_deepseek_api_key,
    get_gemini_api_key,
    get_llm_provider,
)
from config import MAX_CHUNK_LENGTH, MAX_CONCURRENT_REQUESTS
from core.pubmed import (
    fetch_pubmed_summaries,
    fetch_pubmed_entries,
    filter_entries_by_title_relevance,
    fetch_abstracts_for_pmids,
)

# Конфигурация логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class QuotaExhaustedError(Exception):
    """Исключение при исчерпании квоты API — останавливаем обработку и сохраняем частичный результат."""
    pass

# Фразы отказа модели (при срабатывании политики контента) — в таких случаях оставляем оригинал
REFUSAL_PHRASES = (
    "извините, но я не могу помочь",
    "я не могу помочь с этой просьбой",
    "извините, но я не могу перефразировать",
    "не могу перефразировать",
    "текст отсутствует",
    "слишком корот",
    "i'm sorry, i cannot help",
    "i cannot assist with",
    "i'm not able to help",
    "cannot fulfill this request",
)


def _is_model_refusal(text: str) -> bool:
    """Проверяет, является ли ответ модели отказом в выполнении запроса."""
    if not text or len(text.strip()) < 10:
        return False
    lower = text.strip().lower()
    if any(phrase in lower for phrase in REFUSAL_PHRASES):
        return True
    # Дополнительные шаблоны отказа
    return bool(
        re.search(r"не могу\s+.*перефразир", lower)
        or re.search(r"текст\s+.*(отсутствует|слишком\s+корот)", lower)
    )


# Конфигурация OpenAI (теперь хранится в settings.json)


# --- Gemini REST API (production): generativelanguage.googleapis.com/v1beta ---
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _gemini_rest_request(api_key: str, model: str, payload: dict, stream: bool = False):
    """POST generateContent или streamGenerateContent. Возвращает response (requests)."""
    import requests
    method = "streamGenerateContent" if stream else "generateContent"
    url = f"{GEMINI_BASE}/models/{model}:{method}?key={api_key}"
    if stream:
        url += "&alt=sse"  # SSE-формат (data: {...}), иначе стрим может быть пустым или в другом формате
    r = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=120, stream=stream)
    r.raise_for_status()
    return r


def _gemini_messages_to_payload(messages: list, temperature: float, max_tokens: int) -> dict:
    """Собирает systemInstruction + contents + generationConfig из messages и параметров."""
    system = ""
    user_parts = []
    for m in messages:
        role = (m.get("role") or "").lower()
        content = (m.get("content") or "").strip()
        if role == "system":
            system = content
        elif role == "user":
            user_parts.append(content)
    user_text = "\n\n".join(user_parts) if user_parts else ""
    payload = {
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    return payload


class _GeminiChatCompletions:
    """OpenAI-совместимый фасад поверх Gemini REST API (generateContent / streamGenerateContent)."""

    def __init__(self, api_key: str, model: str):
        self._api_key = api_key
        self._model = model

    def create(self, *, model: str = None, messages: list, max_tokens: int = 8192, temperature: float = 0.4, stream: bool = False):
        model = model or self._model
        payload = _gemini_messages_to_payload(messages, temperature, max_tokens)
        if not stream:
            r = _gemini_rest_request(self._api_key, model, payload, stream=False)
            r.encoding = "utf-8"
            data = r.json()
            text = ""
            if data.get("candidates"):
                parts = data["candidates"][0].get("content", {}).get("parts", [])
                if parts and "text" in parts[0]:
                    text = (parts[0]["text"] or "").strip()
            choice = type("Choice", (), {"message": type("Message", (), {"content": text})()})()
            return type("Response", (), {"choices": [choice]})()
        # streaming (SSE: строки "data: {...}" или NDJSON без префикса)
        def _stream():
            r = _gemini_rest_request(self._api_key, model, payload, stream=True)
            r.encoding = "utf-8"  # ответ API в UTF-8; без этого requests может взять ISO-8859-1 и получится mojibake
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.strip():
                    continue
                # SSE: "data: {...}" или "data: [DONE]"
                raw = line.strip()
                if raw.startswith("data: "):
                    raw = raw[6:].strip()
                if raw == "[DONE]" or not raw:
                    continue
                try:
                    chunk_data = json.loads(raw)
                    if not chunk_data.get("candidates"):
                        continue
                    cand = chunk_data["candidates"][0]
                    content = cand.get("content") or {}
                    parts = content.get("parts") or []
                    for p in parts:
                        if isinstance(p, dict) and "text" in p:
                            part = (p["text"] or "").strip()
                            if part:
                                delta = type("Delta", (), {"content": part})()
                                choice = type("Choice", (), {"delta": delta})()
                                yield type("Chunk", (), {"choices": [choice]})()
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
        return _stream()


class _GeminiChat:
    def __init__(self, api_key: str, model: str):
        self.completions = _GeminiChatCompletions(api_key, model)


class _GeminiAdapter:
    """Адаптер к Gemini через REST API (интерфейс как у OpenAI client.chat.completions.create)."""

    def __init__(self, api_key: str, model: str):
        self._api_key = api_key
        self._model = model
        self.chat = _GeminiChat(api_key, model)


class TextProcessor:
    def __init__(
        self,
        api_key: str = None,
        temperature: float = 0.4,
        include_research: bool = False,
        style_controls: Optional[Dict[str, Any]] = None,
    ):
        """Инициализирует процессор текста с клиентом OpenAI или DeepSeek."""
        self.blocks = []
        self.temperature = temperature
        self.include_research = include_research
        self._pubmed_context = ""
        self.style_controls = style_controls or self._get_style_controls_from_settings()
        
        provider = get_llm_provider()
        self.provider = provider
        # Тарификация: последние оценки стоимости генерации статьи (LLM-токены).
        # Заполняется в generate_article_plan / generate_article_final_stream.
        self._last_article_cost_stats: Dict[str, Any] = {}
        
        try:
            if provider == "deepseek":
                ds_key = api_key if api_key else get_deepseek_api_key()
                self.client = OpenAI(api_key=ds_key, base_url="https://api.deepseek.com")
                self.model = settings_manager.get("deepseek_model", "deepseek-chat")
                logger.info(f"Клиент DeepSeek успешно инициализирован (модель: {self.model})")
            elif provider == "gemini":
                gemini_key = api_key if api_key else get_gemini_api_key()
                # Проверенные ID: production (2.5) и preview (3.x). Endpoint: .../models/{id}:generateContent
                self.model = settings_manager.get("gemini_model", "gemini-2.5-flash")
                allowed = {
                    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
                    "gemini-3-pro-preview", "gemini-3-flash-preview",
                    "gemini-3.1-pro-preview", "gemini-3.1-flash-lite-preview",
                }
                if self.model not in allowed:
                    logger.warning(
                        f"Модель Gemini '{self.model}' не в списке поддерживаемых, "
                        "использую gemini-2.5-flash."
                    )
                    self.model = "gemini-2.5-flash"
                self.client = _GeminiAdapter(api_key=gemini_key, model=self.model)
                logger.info(f"Клиент Gemini успешно инициализирован (модель: {self.model})")
            else:
                oa_key = api_key if api_key else get_api_key()
                self.client = OpenAI(api_key=oa_key)
                self.model = settings_manager.get("model", "gpt-4o")
                logger.info(f"Клиент OpenAI успешно инициализирован (модель: {self.model})")
        except Exception as e:
            logger.error(f"Ошибка инициализации клиента {provider}: {str(e)}")
            raise Exception(f"Не удалось инициализировать клиент {provider}. Проверьте API-ключ и соединение.")

    def _check_token_budget(self) -> None:
        """Raises QuotaExhaustedError if monthly budget exceeded."""
        budget = float(settings_manager.get("token_budget_usd", 0) or 0)
        if budget <= 0:
            return
        try:
            from core.db import get_token_usage_totals
            totals = get_token_usage_totals(period_days=30)
            spent = float(totals.get("total_cost") or 0)
            if spent >= budget:
                raise QuotaExhaustedError(
                    f"Превышен лимит расхода токенов: ${spent:.2f} / ${budget:.2f} за 30 дней"
                )
        except QuotaExhaustedError:
            raise
        except Exception:
            pass

    def _log_usage(self, operation: str, input_tokens: int, output_tokens: int) -> None:
        """Log token usage to DB."""
        try:
            from core.db import log_token_usage
            cost = self._estimate_llm_cost_usd(input_tokens, output_tokens)
            log_token_usage(
                created_by="system",
                operation=operation,
                provider=self.provider or "unknown",
                model=self.model or "unknown",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
            )
        except Exception:
            pass

    def _get_style_controls_from_settings(self) -> Dict[str, int]:
        """Берёт параметры управления стилем из settings.json (1-5)."""
        def _clamp(val: Any) -> int:
            try:
                v = int(val)
            except Exception:
                v = 3
            return max(1, min(5, v))

        return {
            "science": _clamp(settings_manager.get("style_science", 3)),
            "depth": _clamp(settings_manager.get("style_depth", 3)),
            "accuracy": _clamp(settings_manager.get("style_accuracy", 3)),
            "readability": max(1, min(7, int(settings_manager.get("style_readability", 3) or 3))),
            "source_quality": _clamp(settings_manager.get("style_source_quality", 3)),
        }

    def _science_level(self) -> int:
        """Текущий уровень научности (1–5) из style_controls."""
        s = self.style_controls or {}
        return max(1, min(5, int(s.get("science", 3))))

    def _style_guidance_text(self) -> str:
        """Формирует текстовые инструкции по стилю на основе 5 параметров (1-5). Явные уровни для всех параметров."""
        s = self.style_controls or {}
        science = max(1, min(5, int(s.get("science", 3))))
        depth = max(1, min(5, int(s.get("depth", 3))))
        accuracy = max(1, min(5, int(s.get("accuracy", 3))))
        readability = max(1, min(7, int(s.get("readability", 3))))
        source_quality = max(1, min(5, int(s.get("source_quality", 3))))

        def science_desc(val: int) -> str:
            if val == 1:
                return "ОБЯЗАТЕЛЬНО: статья для ДЕТЕЙ (младший и средний школьный возраст). Очень простые слова, короткие предложения. Никакой специальной терминологии — только бытовой язык. Объясняй «как ребёнку»: что это, зачем, почему важно. Сравнения с привычными вещами. Никаких латинских названий, формул, механизмов. Стиль — добрый рассказ или детская энциклопедия."
            if val == 2:
                return "Статья для подростков или широкой аудитории без подготовки. Простой язык, минимум терминов; каждый термин при первом упоминании обязательно поясняй простыми словами. Без сложных конструкций. Стиль — научпоп, живой и доступный."
            if val == 3:
                return "Умеренно научный стиль: баланс между доступностью и профессиональной терминологией. Термины допустимы, сложные — с краткими пояснениями. Для заинтересованной взрослой аудитории."
            if val == 4:
                return "Научный стиль с профессиональной терминологией. Допускаются сложные формулировки, механизмы, классификации. Изложение остаётся понятным для подготовленного читателя."
            return "Строгий научный/академический стиль: полная профессиональная терминология, как в учебнике для вуза или клинической статье. Без упрощений, с механизмами, патогенезом, ссылками на классификации."

        def depth_desc(val: int) -> str:
            if val == 1:
                return "Очень кратко: 1–2 абзаца на раздел. Только главное, без деталей. Как краткая справка или ликбез для ребёнка."
            if val == 2:
                return "Кратко: по 2–3 абзаца на раздел, без углубления в механизмы и детали."
            if val == 3:
                return "Средняя детализация: достаточно для понимания сути, без избыточного углубления."
            if val == 4:
                return "Подробно: разбор ключевых механизмов, критериев, с достаточной детализацией."
            return "Максимальная глубина: полный разбор механизмов, патогенеза, классификаций, доказательной базы. Как развёрнутая глава учебника."

        def accuracy_desc(val: int) -> str:
            if val == 1:
                return "Допускаются упрощения и образные формулировки ради понятности. «Примерно», «часто», «обычно» — нормально. Не требуется точных формулировок и цифр."
            if val == 2:
                return "Общие формулировки допустимы, но факты должны быть верными. Без жёстких требований к точным критериям и цифрам."
            if val == 3:
                return "Точность важна: корректные формулировки, при необходимости — ключевые критерии и цифры."
            if val == 4:
                return "Высокая точность: корректная терминология, критерии, диапазоны, без расплывчатых формулировок."
            return "Максимальная точность: только проверяемые утверждения, критерии по гайдам, без предположений и «возможно». Как в научной публикации."

        def readability_desc(val: int) -> str:
            if val == 1:
                return "Максимально просто: короткие предложения, простые слова, каждое новое понятие объясняй сразу. Как в тексте для детей."
            if val == 2:
                return "Очень понятно: простые конструкции, пояснение терминов при первом упоминании."
            if val == 3:
                return "Сбалансированно: понятно, но без излишнего упрощения."
            if val == 4:
                return "Научно, но с пояснениями сложных мест. Читатель с подготовкой."
            if val == 5:
                return "Плотный научный язык: минимум пояснений, допускаются сложные конструкции. Для специалистов."
            if val == 6:
                return "Максимальная читаемость: короткие предложения, разбивка на абзацы, подзаголовки, списки. Текст легко сканировать и усваивать."
            return "Высшая читаемость: предельно ясная структура, короткие абзацы, ключевые мысли выделены, минимум сложных оборотов. Текст должен читаться без усилий."

        def source_quality_desc(val: int) -> str:
            if val == 1:
                return "Достаточно общих знаний и упрощённых объяснений. Можно опираться на «известно, что», без обязательных ссылок на типы исследований."
            if val == 2:
                return "Можно упоминать наблюдения, предварительные данные, обзоры без строгой иерархии доказательств."
            if val == 3:
                return "Предпочтение обзорам и когортным данным; упоминать уровень доказательности где уместно."
            if val == 4:
                return "Опора на систематические обзоры, мета-анализы, клинические рекомендации; указывать тип источника."
            return "Строго: только систематические обзоры, мета-анализы, клинические гайды. Формулировки в духе «по данным мета-анализа», «согласно рекомендациям»."

        return (
            "Параметры управления статьёй (соблюдай строго):\n"
            f"- Научность: {science}/5 — {science_desc(science)}\n"
            f"- Глубина: {depth}/5 — {depth_desc(depth)}\n"
            f"- Точность: {accuracy}/5 — {accuracy_desc(accuracy)}\n"
            f"- Читаемость: {readability}/7 — {readability_desc(readability)}\n"
            f"- Качество источников: {source_quality}/5 — {source_quality_desc(source_quality)}\n"
        )

    def _system_message_for_article(self) -> str:
        """Системное сообщение для генерации статьи с учётом уровня научности (1–5)."""
        science = self._science_level()
        if science == 1:
            return (
                "Ты автор познавательных текстов для ДЕТЕЙ (младший и средний школьный возраст). Пиши очень простыми словами, короткими предложениями. Никаких специальных терминов — только бытовой язык. Объясняй всё «как ребёнку», с сравнениями с привычными вещами. Никаких латинских названий, формул, механизмов. Стиль — добрый рассказ или детская энциклопедия. Цель — чтобы ребёнок понял и заинтересовался."
            )
        if science == 2:
            return (
                "Ты автор научно-популярных текстов для широкой аудитории. Пиши простым, доступным языком: минимум сложных терминов, при первом упоминании термина — кратко поясняй. Стиль — научпоп, не академическая статья. Цель — понятно и интересно донести суть без потери точности фактов."
            )
        if science == 3:
            return (
                "Ты автор научных и научно-популярных статей. Пишешь с балансом глубины и доступности: точная терминология там, где нужно, сложные термины — с краткими пояснениями. Стиль — научный, но понятный."
            )
        return (
            "Ты автор научных статей. Пишешь с научной глубиной: точная терминология, механизмы (где уместно), классификации, опора на доказательства. Стиль — научный, развёрнуто; сохраняй содержательность и строгость."
        )

    def _system_message_for_plan(self) -> str:
        """Системное сообщение для генерации плана статьи — в том же стиле, что и сама статья (звёзды)."""
        science = self._science_level()
        if science == 1:
            return (
                "Ты эксперт по познавательным материалам для ДЕТЕЙ. Строишь планы статей с простыми, понятными названиями разделов: «Что это такое», «Почему бывает», «Как лечат», «Что запомнить» — без научных терминов в заголовках. Описания разделов — короткие, доступным языком. Отвечай только валидным JSON."
            )
        if science == 2:
            return (
                "Ты эксперт по научно-популярным статьям. Строишь планы с доступными названиями разделов и описаниями: понятные формулировки, минимум сложной терминологии в названиях шагов. Отвечай только валидным JSON."
            )
        if science == 3:
            return (
                "Ты эксперт по научным и научно-популярным статьям. Строишь планы с балансом: названия разделов могут содержать и понятные, и профессиональные формулировки. Описания — средняя детализация. Отвечай только валидным JSON."
            )
        return (
            "Ты эксперт по научным статьям. Строишь планы с научной структурой: названия разделов — профессиональная терминология, описания — с указанием глубины и аспектов. Отвечай только валидным JSON."
        )

    def _estimate_tokens(self, text: str, model: Optional[str] = None) -> int:
        """Оценка числа токенов для тарификации (приблизительно)."""
        if not text:
            return 0
        if not tiktoken:
            # Если tiktoken недоступен — грубая оценка: 1 токен ~= 4 символа.
            return max(1, len(text) // 4)
        model_name = (model or "").strip() or self.model
        try:
            enc = tiktoken.encoding_for_model(model_name)
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")
        try:
            return len(enc.encode(text))
        except Exception:
            return max(1, len(text) // 4)

    def _get_llm_rates_usd_per_1m_tokens(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Возвращает (input_rate, output_rate) в USD за 1M токенов.
        Если тарифы для текущей модели не известны — вернёт (None, None).
        """
        model = (self.model or "").strip()
        provider = self.provider or ""
        # Примерные тарифы для оценки.
        if provider == "openai":
            openai_rates = {
                "gpt-4o": (5.0, 15.0),
                "gpt-4o-mini": (0.15, 0.6),
                "gpt-4-turbo": (10.0, 30.0),
                "gpt-4": (30.0, 60.0),
            }
            return openai_rates.get(model, (None, None))
        if provider == "deepseek":
            deepseek_rates = {
                "deepseek-chat": (0.14, 0.28),
                "deepseek-reasoner": (None, None),
            }
            return deepseek_rates.get(model, (None, None))
        # Для Gemini тарифы не задаём, чтобы не подменять точной оценки.
        return (None, None)

    def _estimate_llm_cost_usd(self, input_tokens: int, output_tokens: int) -> Optional[float]:
        in_rate, out_rate = self._get_llm_rates_usd_per_1m_tokens()
        if in_rate is None or out_rate is None:
            return None
        return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate

    def _volume_guidance_for_tokens(self, max_tokens: int) -> str:
        """Инструкция по объёму: модель должна уложиться в лимит и закончить статью целиком, а не обрываться."""
        approx_words = max(500, min(32000, max_tokens // 2))
        critical = (
            f"**КРИТИЧНО:** Лимит ответа — ровно {max_tokens} токенов. "
            "Статья должна ПОЛНОСТЬЮ уместиться в этот лимит и быть ЗАКОНЧЕННОЙ: обязательно раздел ## Источники (если есть) и заключение. "
            "Не пиши длинную статью — она обрежется. Рассчитай объём: меньше абзацев на раздел, короче разделы, чтобы гарантированно уложиться и завершить текст."
        )
        if max_tokens <= 4096:
            return (
                critical
                + f" **Целевой объём:** примерно 1500–2000 слов. По 1–2 абзаца на раздел плана, затем заключение и источники."
            )
        if max_tokens <= 8192:
            return (
                critical
                + f" **Целевой объём:** примерно 2000–3500 слов. По 2–3 абзаца на раздел. Обязательно закончи статью полностью."
            )
        if max_tokens <= 16384:
            return (
                critical
                + f" **Целевой объём:** примерно 3500–6000 слов. Раскрой разделы, но уложись в лимит и заверши статью."
            )
        if max_tokens <= 32768:
            return (
                critical
                + f" **Целевой объём:** до примерно {approx_words} слов. Можно подробнее, но обязательно закончи раздел ## Источники и заключение до исчерпания лимита."
            )
        return (
            "**Объём статьи:** минимум 3000–4000 слов, можно длиннее в пределах лимита ответа. Обязательно заверши статью разделом ## Источники и заключением."
        )

    def _main_instruction_for_article(self, with_sources: bool = False) -> str:
        """Основной блок инструкций для статьи в зависимости от научности (чтобы не перебивать слайдер)."""
        science = self._science_level()
        toc = " В самом начале статьи, после введения или перед ним, обязательно добавь раздел ## Содержание со списком всех разделов статьи."
        cite = (
            " Используй данные из PubMed и указывай ссылки на источники в квадратных скобках: [1], [2] и т.д. В конце статьи ОБЯЗАТЕЛЬНО добавь раздел ## Источники и перечисли использованные источники в формате из списка выше."
            if with_sources else ""
        )
        if science == 1:
            return (
                "**Инструкции:** Статья для ДЕТЕЙ. Очень простые слова, короткие предложения. Никакой специальной терминологии — только то, что понятно ребёнку. Объясняй с сравнениями и примерами из жизни. По глубине и остальным параметрам строго следуй блоку «Параметры управления стилем» ниже. Сохраняй заголовки (##) как в плане. Markdown: ## для разделов, короткие абзацы, списки (-), **жирный** для важных слов. Напиши статью ПОЛНОСТЬЮ до конца."
                + toc + cite
            )
        if science == 2:
            return (
                "**Инструкции:** Статья должна быть ПОНЯТНОЙ и ДОСТУПНОЙ для широкой аудитории: простой язык, минимум сложных терминов, при необходимости кратко поясняй термины. Не пиши в академическом стиле — цель научпоп. По глубине и объёму разделов ориентируйся на параметры стиля ниже. Сохраняй заголовки (##) как в плане. Markdown: ## для разделов, абзацы, списки (-), **жирный** для ключевых терминов. Напиши статью ПОЛНОСТЬЮ до конца, не обрывай на середине раздела."
                + toc + cite
            )
        if science == 3:
            return (
                "**Инструкции:** Статья — умеренно научная: баланс между доступностью и профессиональной терминологией. Раскрывай разделы подробно, но без излишней академичности. Соблюдай параметры стиля ниже. Сохраняй заголовки (##) как в плане. Markdown: ## для разделов, абзацы, списки (-), **жирный** для ключевых терминов. Объём: полноценные разделы. Напиши статью ПОЛНОСТЬЮ до конца."
                + toc + cite
            )
        return (
            "**Инструкции:** Статья должна быть научной по глубине и стилю: точная терминология, механизмы и патогенез где уместно, опора на данные и классификации. Одновременно — подробной: по 4–6 абзацев на раздел. Сохраняй научную строгость при понятном изложении. Соблюдай параметры стиля ниже. Сохраняй заголовки (##) как в плане. Markdown: ## для разделов, абзацы, списки (-), **жирный** для терминов. Объём: минимум 3000–4000 слов. Напиши статью ПОЛНОСТЬЮ до конца."
            + toc + cite
        )

    def _source_quality_pubmed_filter(self) -> str:
        """
        Возвращает фильтр PubMed по типу публикации [pt] для повышения качества источников.
        Используется как AND-дополнение к запросу в esearch (не простой текст, а фильтр по типу статьи).
        """
        s = self.style_controls or {}
        score = int(s.get("source_quality", 3))
        if score >= 5:
            # Систематические обзоры, мета-анализы, клинические рекомендации
            return " AND (systematic review[pt] OR meta-analysis[pt] OR practice guideline[pt] OR guideline[pt])"
        if score == 4:
            # Обзоры литературы
            return " AND review[pt]"
        if score == 2:
            # Можно сузить до наблюдательных исследований (опционально)
            return " AND (observational study[pt] OR cohort studies[pt] OR case-control studies[pt])"
        # 1 и 3 — без фильтра по типу, релевантность и дата решают
        return ""

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
                        f.write(f"{block['text']}\n\n")

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
        """
        Старый вариант разбиения блока по количеству символов.
        Оставлен как запасной вариант, если токенизатор недоступен.
        """
        if len(block) <= max_length:
            return [block]
        words = block.split()
        chunks: List[str] = []
        current_chunk: List[str] = []
        current_length = 0
        for word in words:
            add_len = len(word) + (1 if current_length > 0 else 0)
            if current_length + add_len > max_length:
                if current_chunk:
                    chunks.append(" ".join(current_chunk))
                current_chunk = [word]
                current_length = len(word)
            else:
                current_chunk.append(word)
                current_length += add_len
        if current_chunk:
            chunks.append(" ".join(current_chunk))
        logger.debug(f"Блок (символьное разбиение) разделён на {len(chunks)} частей")
        return chunks

    def split_block_tokens(
        self,
        block: str,
        max_tokens: int = 400,
        overlap_tokens: int = 50,
    ) -> List[str]:
        """
        Разбивает блок текста по ТОКЕНАМ модели, а не по символам.

        - max_tokens: целевой размер чанка в токенах.
        - overlap_tokens: перекрытие между чанками, чтобы не терять контекст на границах.

        Если tiktoken недоступен — падает обратно на split_block (по символам) с приблизительной длиной.
        """
        text = block.strip()
        if not text:
            return []
        if max_tokens <= 0:
            return [text]

        try:
            import tiktoken  # type: ignore
        except ImportError:
            # Fallback: грубо считаем, что 1 токен ≈ 4 символа
            approx_chars = max_tokens * 4
            logger.warning("tiktoken не установлен, используем разбиение по символам.")
            return self.split_block(text, max_length=approx_chars)

        try:
            try:
                encoding = tiktoken.encoding_for_model(self.model)
            except Exception:
                # Универсальный энкодер для современных моделей
                encoding = tiktoken.get_encoding("cl100k_base")
        except Exception as e:
            logger.warning(f"Не удалось получить токенизатор tiktoken: {e}, используем разбиение по символам.")
            approx_chars = max_tokens * 4
            return self.split_block(text, max_length=approx_chars)

        tokens = encoding.encode(text)
        if not tokens:
            return []

        chunks: List[str] = []
        n = len(tokens)
        # Шаг с перекрытием
        step = max_tokens - overlap_tokens if overlap_tokens < max_tokens else max_tokens
        i = 0
        while i < n:
            window = tokens[i : i + max_tokens]
            chunk_text = encoding.decode(window).strip()
            if chunk_text:
                chunks.append(chunk_text)
            if i + max_tokens >= n:
                break
            i += step

        logger.debug(f"Блок (токенное разбиение) разделён на {len(chunks)} частей")
        return chunks

    def _paraphrase_one_chunk(
        self, chunk: str, theme: str, block_index: int, chunk_idx: int
    ) -> Tuple[int, str]:
        """Перефразирует один чанк текста (один вызов API). Возвращает (chunk_idx, результат)."""
        style_hint = ""
        if self.style_controls:
            style_hint = "\n\n" + self._style_guidance_text()

        custom_hint = ""
        if getattr(self, "_custom_regen_prompt", ""):
            custom_hint = f"\n\nДОПОЛНИТЕЛЬНОЕ УКАЗАНИЕ ПОЛЬЗОВАТЕЛЯ (учти при перефразировании):\n{self._custom_regen_prompt}"

        prompt = f"""
Перефразируйте текст на русском языке, строго сохраняя академический стиль и точную научную терминологию для публикации в области {theme}.
Следуйте этим правилам:
1. Сохраняйте исходный смысл, не добавляйте новых фактов и не удаляйте существующую информацию.
2. Поддерживайте структуру текста: сохраняйте количество предложений и их порядок, избегая излишнего переструктурирования.
3. Если текст начинается или заканчивается дефисом, оставьте неизменным. Если в начале блока первое слово не понятно и с маленькой буквы, оставь его неизменным.
4. Если текст слишком короток, отсутствует или содержит бессмысленные знаки или цифры, верните его неизменным.
5. Удалите префиксы типа "Блок n", если они присутствуют.
6. Верните только перефразированный текст без дополнительных комментариев.
7. Перефразированный текст должен быть близок по объёму к оригиналу (±10% слов).{style_hint}{custom_hint}

Текст: {chunk}
""".strip()

        if self.include_research:
            prompt += (
                "\n\nТакже включите информацию о новых исследованиях из различных известных источников, "
                "чтобы сделать текст более актуальным и информативным."
            )

        if getattr(self, "_pubmed_context", "") and self._pubmed_context.strip():
            prompt += (
                "\n\nСвежие статьи с PubMed по теме (используй только для ремарок):\n"
                + self._pubmed_context.strip()
                + "\n\nПри перефразировании при необходимости добавляй короткие ремарки, "
                "ссылающиеся на современные исследования (например: «по данным последних работ…», "
                "«согласно публикациям…»), опираясь только на этот список. Не выдумывай названия статей."
            )

        max_tokens = min(8192, settings_manager.get("max_tokens", 8192))

        def _call_api() -> str:
            self._check_token_budget()
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "Вы — эксперт по перефразированию научных текстов на русском языке в академическом стиле.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=self.temperature,
            )
            result_text = (response.choices[0].message.content or "").strip()
            in_tok = self._estimate_tokens(prompt)
            out_tok = self._estimate_tokens(result_text)
            self._log_usage("paraphrase", in_tok, out_tok)
            return result_text

        try:
            paraphrased_text = _call_api()
            if not paraphrased_text:
                logger.warning(
                    f"Часть {chunk_idx} блока {block_index}: пустой ответ от API, возвращается оригинал"
                )
                return (chunk_idx, chunk)
            if _is_model_refusal(paraphrased_text):
                logger.warning(
                    f"Часть {chunk_idx} блока {block_index}: модель отказала в перефразировании, оставляем оригинал"
                )
                return (chunk_idx, chunk)
            logger.info(f"Часть {chunk_idx} блока {block_index} успешно перефразирована")
            return (chunk_idx, paraphrased_text)
        except Exception as e:
            err_str = str(e)
            logger.error(f"Ошибка перефразирования части {chunk_idx} блока {block_index}: {e}")
            if "insufficient_quota" in err_str:
                logger.warning(
                    "Исчерпана квота API. Обработка остановлена, частичный результат будет сохранён."
                )
                raise QuotaExhaustedError("insufficient_quota")
            if "rate_limit_exceeded" in err_str:
                logger.info("Превышен лимит запросов в минуту. Ожидание 30 секунд...")
                time.sleep(30)
                try:
                    paraphrased_text = _call_api()
                    if not paraphrased_text or _is_model_refusal(paraphrased_text):
                        return (chunk_idx, chunk)
                    return (chunk_idx, paraphrased_text)
                except Exception as retry_e:
                    logger.error(
                        f"Повторная попытка не удалась для части {chunk_idx} блока {block_index}: {retry_e}"
                    )
            return (chunk_idx, chunk)

    def paraphrase_block(
        self,
        block: str,
        theme: str,
        block_index: int,
        custom_prompt: str = "",
        style_controls: Optional[Dict[str, int]] = None,
    ) -> str:
        """Перефразирует отдельный блок текста с использованием API (параллельно по чанкам).
        custom_prompt: дополнительная инструкция пользователя (что исправить).
        style_controls: dict с ключами science, depth, accuracy, readability, source_quality.
        """
        if style_controls:
            old = self.style_controls
            self.style_controls = style_controls
        self._custom_regen_prompt = (custom_prompt or "").strip()
        chunks = self.split_block_tokens(block, max_tokens=MAX_CHUNK_LENGTH, overlap_tokens=50)
        if not chunks:
            return block
        workers = min(MAX_CONCURRENT_REQUESTS, len(chunks))
        if workers <= 1:
            # Последовательно
            results = [self._paraphrase_one_chunk(c, theme, block_index, i) for i, c in enumerate(chunks, 1)]
        else:
            results = []
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(self._paraphrase_one_chunk, chunk, theme, block_index, idx): idx
                    for idx, chunk in enumerate(chunks, 1)
                }
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except QuotaExhaustedError:
                        raise  # останавливаем обработку, частичный результат сохраним в process_text
                    except Exception as e:
                        logger.error(f"Ошибка в потоке перефразирования: {e}")
                        idx = futures[future]
                        results.append((idx, chunks[idx - 1]))
            results.sort(key=lambda x: x[0])
        result = " ".join(r[1] for r in results)
        if style_controls:
            self.style_controls = old
        self._custom_regen_prompt = ""
        return result

    def process_text(self, text: str, theme: str, callback=None) -> str:
        """Обрабатывает текст: перефразирует блоки."""
        if self.include_research and theme and theme.strip():
            try:
                self._pubmed_context = fetch_pubmed_summaries(theme.strip(), max_results=5)
                if self._pubmed_context:
                    logger.info("PubMed: загружены резюме для ремарок в перефразировании")
            except Exception as e:
                logger.warning(f"PubMed: не удалось подтянуть резюме для перефразирования: {e}")
                self._pubmed_context = ""
        else:
            self._pubmed_context = ""

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
            try:
                paraphrased_block = self.paraphrase_block(cleaned_block, theme, i)
            except QuotaExhaustedError:
                # Квота исчерпана — сохраняем уже обработанное и выходим
                logger.warning("Остановка из-за исчерпания квоты API. Сохранены частичные результаты.")
                processed_blocks.append(cleaned_block)
                if callback:
                    callback(cleaned_block, cleaned_block)
                break
            except Exception as e:
                logger.error(f"Ошибка перефразирования блока {i}: {e}")
                paraphrased_block = cleaned_block

            if paraphrased_block == cleaned_block:
                logger.warning(f"Блок {i} не был перефразирован, сохранён оригинал")
            
            processed_blocks.append(paraphrased_block)
            if callback:
                callback(cleaned_block, paraphrased_block)

        result = '\n\n'.join(processed_blocks) if processed_blocks else ""

        # Вставляем явный блок с PubMed-резюме в конец текста, если он был успешно получен.
        # Это дополнение к коротким ремаркам внутри перефразированных блоков.
        if result and self._pubmed_context:
            result += "\n\n## Современные исследования (PubMed)\n\n" + self._pubmed_context

        return result

    def generate_article_plan(
        self,
        theme: str,
        audience: str = "подготовленная аудитория",
        num_plan_steps: int = 10,
    ) -> List[dict]:
        """
        Шаг 1: Генерация плана статьи. Возвращает список шагов с searchQueries для PubMed.
        Формат: [{"step": "...", "description": "...", "searchQueries": ["q1", "q2"]}, ...]
        num_plan_steps: желаемое количество пунктов плана (5–20).
        """
        theme_clean = theme.strip()
        num_plan_steps = max(5, min(20, int(num_plan_steps)))
        style_guidance = self._style_guidance_text()
        with_pubmed = bool(self.include_research)
        theme_phrase = self._get_pubmed_theme_phrase(theme_clean) if with_pubmed else ""
        theme_instruction = ""
        if with_pubmed:
            if theme_phrase:
                theme_instruction = (
                    f'\n\n**КРИТИЧНО — searchQueries для PubMed:** Используй в КАЖДОМ запросе именно эту английскую фразу темы: «{theme_phrase}». '
                    f'Каждый searchQuery = только эта фраза + один аспект раздела (3–5 слов). Примеры: «{theme_phrase} mechanisms», «{theme_phrase} treatment guidelines», «{theme_phrase} diagnosis». '
                    'Запросы только на английском.\n\n'
                )
            else:
                theme_instruction = (
                    "\n\n**searchQueries для PubMed:** на английском, короткие (3–6 слов). Каждый запрос должен начинаться с ключевого термина темы на английском + аспект раздела.\n\n"
                )
        plan_prompt = (
            f"Создай УНИКАЛЬНЫЙ план статьи на русском по теме «{theme_clean}» для аудитории: {audience}.\n\n"
            "ВАЖНО: план должен быть разным именно для ЭТОЙ темы, а не шаблонным. Подстрой структуру под содержание:\n"
            "- для явления/проблемы: определения, причины, механизмы, последствия, примеры, ограничения;\n"
            "- для метода/технологии/подхода: принцип, применение, эффективность, риски/ограничения, сравнение альтернатив;\n"
            "- для понятия/модели: определение, ключевые компоненты, как работает, где применяется, современные взгляды.\n"
            "Названия разделов формулируй КОНКРЕТНО под тему (не общие фразы вроде «Что это такое»). Включай глубину: механизмы/принципы, классификации (если уместно), доказательная база/источники, точная терминология; плюс по желанию — история, мифы и факты, кейс, практика.\n\n"
        )
        if with_pubmed:
            plan_prompt += (
                "**searchQueries для PubMed (ОБЯЗАТЕЛЬНО):**\n"
                "- На английском языке, короткие (3–6 слов).\n"
                "- КАЖДЫЙ запрос ОБЯЗАТЕЛЬНО должен начинаться с ключевого термина ТЕМЫ на английском. Без этого PubMed вернёт случайные статьи не по теме.\n"
                "- Формат: [английский термин темы] [уточнение по разделу].\n"
                + theme_instruction
            )
        plan_prompt += (
            "Верни ТОЛЬКО валидный JSON-массив (без markdown, без ```). Каждый элемент — объект с полями:\n"
            '- "step": название раздела (на русском, конкретное под тему)\n'
            '- "description": 1–2 предложения, что будет в разделе\n'
            + ('- "searchQueries": массив из 1–2 запросов для PubMed (каждый запрос = английский термин темы + аспект раздела)\n\n' if with_pubmed else "\n")
            + f"Ограничения: ровно {num_plan_steps} шагов, до 2 запросов на шаг. Не повторяй один и тот же набор разделов для любой темы — меняй состав и порядок в зависимости от «{theme_clean}»."
        )
        if style_guidance:
            plan_prompt += (
                "\n\n**Параметры стиля (применяются и к плану):**\n"
                "Названия разделов плана (поле «step») и их описания («description») должны строго соответствовать уровню научности и читаемости ниже: "
                "при 1–2 звёздах — простые, доступные формулировки разделов (например «Что это такое», «Как лечат», «Что важно знать»); "
                "при 4–5 звёздах — научные формулировки («Механизмы», «Классификации», «Ограничения и риски»). "
                "Тот же стиль будет использован для текста статьи.\n\n"
                f"{style_guidance}"
            )
        try:
            system_msg = self._system_message_for_plan()
            plan_input_tokens = self._estimate_tokens(system_msg + "\n\n" + plan_prompt)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": plan_prompt},
                ],
                max_tokens=8192,
                temperature=0.55,
            )
            raw = (response.choices[0].message.content or "").strip()
            plan_output_tokens = self._estimate_tokens(raw)
            plan_cost = self._estimate_llm_cost_usd(plan_input_tokens, plan_output_tokens)
            self._last_article_cost_stats["plan"] = {
                "provider": self.provider,
                "model": self.model,
                "input_tokens_est": plan_input_tokens,
                "output_tokens_est": plan_output_tokens,
                "cost_usd_est": plan_cost,
            }
            if "```" in raw:
                m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
                if m:
                    raw = m.group(1).strip()
            data = json.loads(raw)
            if not isinstance(data, list):
                data = [data] if isinstance(data, dict) else []
            result = []
            for i, item in enumerate(data[:num_plan_steps]):
                if not isinstance(item, dict):
                    continue
                step = item.get("step") or f"Шаг {i+1}"
                desc = item.get("description") or ""
                queries = item.get("searchQueries") or []
                if isinstance(queries, str):
                    queries = [queries[:80] for _ in [1]]
                else:
                    queries = [str(q)[:80] for q in queries[:2] if q]
                result.append({"step": step, "description": desc, "searchQueries": queries})
            return result
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Не удалось распарсить план статьи: {e}")
            return []

    def _fallback_illustration_markers(
        self, article: str, num_images: int
    ) -> Tuple[str, List[Dict[str, str]]]:
        """
        Запасной вариант: вставляет маркеры [ILLUSTRATION_N] после разделов (## или нумерованных "1. ", "2. ")
        и формирует промпты из заголовков для генерации иллюстраций.
        """
        if not article or not article.strip():
            logger.info("Fallback иллюстраций: пустая статья, маркеры не добавлены")
            return article, []
        num_images = max(1, min(5, int(num_images)))
        science = self._science_level()
        simple_labels_rule = (
            "Use very simple Russian labels for children or broad audience: short 1-3 word labels, avoid medical jargon and complex terminology."
            if science <= 2
            else "Use precise Russian labels suitable for educational medical content."
        )
        # Сначала ищем заголовки ## (кроме Источники/Содержание)
        section_re = re.compile(r"\n(## )(?!Содержание|Источники)([^\n]+)\n", re.IGNORECASE)
        matches = list(section_re.finditer(article))
        # Если нет ## — ищем нумерованные разделы "1. Заголовок", "2. Заголовок" (до раздела "Источники")
        if not matches:
            numbered_re = re.compile(r"(?:^|\n)(\d+)\.\s+([^\n]+(?:\n|$))")
            for m in numbered_re.finditer(article):
                title = (m.group(2) or "").strip()
                if re.match(r"^(Источники|Содержание|Вывод)\s*", title, re.IGNORECASE):
                    break
                matches.append(m)
                if len(matches) >= num_images:
                    break
        if matches:
            logger.info(f"Fallback иллюстраций: найдено {len(matches)} секций для маркеров (заголовки)")
        # Если после обоих проходов ничего не нашли — используем первые абзацы как опорные места
        if not matches:
            # Разбиваем по двойным переводам строк
            blocks = [b for b in article.split("\n\n") if b.strip()]
            if not blocks:
                logger.info("Fallback иллюстраций: нет блоков текста, маркеры не добавлены")
                return article, []
            result_prompts: List[Dict[str, str]] = []
            new_blocks = []
            used = 0
            for idx, block in enumerate(blocks):
                new_blocks.append(block)
                title_line = block.split("\n", 1)[0].strip()
                if used < num_images and not re.match(r"^(Источники|Содержание)\b", title_line, re.IGNORECASE):
                    marker = f"[ILLUSTRATION_{used + 1}]"
                    new_blocks.append(f"{marker}")
                    caption_ru = title_line or f"Рисунок {used + 1}"
                    prompt_english = (
                        f"Medical illustration, educational diagram: {title_line}. "
                        "Professional healthcare visual, clear and informative. "
                        f"{simple_labels_rule}"
                    )
                    result_prompts.append(
                        {
                            "marker": marker,
                            "prompt_english": prompt_english[:500],
                            "caption_ru": caption_ru,
                        }
                    )
                    used += 1
                if used >= num_images:
                    # Остальные блоки просто добавляем без маркеров
                    new_blocks.extend(blocks[idx + 1 :])
                    break
            new_article = "\n\n".join(new_blocks)
            logger.info(f"Fallback иллюстраций: маркеры по абзацам, создано промптов: {len(result_prompts)}")
            return new_article, result_prompts
        result_prompts = []
        parts = []
        last_end = 0
        for i, m in enumerate(matches[:num_images]):
            parts.append(article[last_end : m.end()])
            title = (m.group(2) or "").strip() if m.lastindex and m.lastindex >= 2 else ""
            marker = f"[ILLUSTRATION_{i + 1}]"
            parts.append(f"\n\n{marker}\n\n")
            last_end = m.end()
            caption_ru = title or f"Рисунок {i + 1}"
            # Просим модель делать подписи и текст на изображении на русском
            prompt_english = (
                f"Medical illustration, educational diagram: {title}. "
                f"Professional healthcare visual, clear and informative. "
                f"All labels and any text on the image must be in Russian language. "
                f"{simple_labels_rule}"
            )
            result_prompts.append({"marker": marker, "prompt_english": prompt_english[:500], "caption_ru": caption_ru})
        parts.append(article[last_end:])
        logger.info(f"Fallback иллюстраций: маркеры по секциям, создано промптов: {len(result_prompts)}")
        return "".join(parts), result_prompts

    def generate_article_image_prompts(
        self, article: str, num_images: int = 5
    ) -> Tuple[str, List[Dict[str, str]]]:
        """
        Генерирует 3–5 промптов для иллюстраций и вставляет маркеры в текст статьи.
        Возвращает (статья_с_маркерами, список {"marker", "prompt_english", "caption_ru"}).
        """
        if not article or not article.strip():
            logger.info("Генерация промптов иллюстраций: пустая статья, выходим")
            return article, []
        num_images = max(1, min(5, int(num_images)))
        science = self._science_level()
        audience_hint = (
            "The article is for children / broad audience. Keep wording and labels very simple."
            if science <= 2
            else "The article is for trained audience. Medical precision is allowed."
        )
        labels_hint = (
            "All labels and any text on the image must be in Russian language and very simple: short 1-3 word labels, no complex jargon."
            if science <= 2
            else "All labels and any text on the image must be in Russian language."
        )
        user_content = (
            f"Дана научная медицинская статья на русском языке. Нужно добавить в неё от {num_images - 1} до {num_images} иллюстраций.\n\n"
            f"Audience hint: {audience_hint}\n\n"
            "**Задача:**\n"
            "1. Вставь в текст статьи маркеры [ILLUSTRATION_1], [ILLUSTRATION_2], … (по одному на каждую иллюстрацию) в тех местах, где логично разместить рисунок (после абзаца или подзаголовка). Не более одного маркера на раздел ##.\n"
            "2. Для каждого маркера придумай короткий промпт на английском для генерации медицинской иллюстрации (text-to-image): описание сцены, схемы, диаграммы или клинического изображения в образовательном стиле.\n\n"
            "**Ограничения:** маркеры строго в формате [ILLUSTRATION_N], где N — номер от 1. Промпты на английском, 1–2 предложения, стиль professional medical illustration, educational.\n\n"
            "Верни ответ в формате (строго придерживайся):\n"
            "---ARTICLE---\n"
            "<текст статьи с вставленными маркерами [ILLUSTRATION_1] …>\n"
            "---PROMPTS---\n"
            "JSON-массив, каждый элемент: {\"marker\": \"[ILLUSTRATION_1]\", \"prompt_english\": \"...\", \"caption_ru\": \"Подпись на русском\"}\n\n"
            "Статья:\n\n"
            f"{article.strip()[:12000]}"
        )
        try:
            logger.info(
                f"Генерация промптов иллюстраций: длина статьи {len(article)}, запрошено изображений: {num_images}"
            )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are a medical editor. Output the article with markers and a JSON array of illustration prompts. Use only the format ---ARTICLE--- and ---PROMPTS---."},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=8192,
                temperature=0.3,
            )
            raw = (response.choices[0].message.content or "").strip()
            logger.info(f"Генерация промптов иллюстраций: сырой ответ модели длиной {len(raw)} символов")
            article_part = raw
            prompts_part = ""
            if "---ARTICLE---" in raw and "---PROMPTS---" in raw:
                _, _, rest = raw.partition("---ARTICLE---")
                article_part, _, prompts_part = rest.partition("---PROMPTS---")
                article_part = article_part.strip()
                prompts_part = prompts_part.strip()
            if not article_part:
                logger.warning("Генерация промптов иллюстраций: article_part пустой, используем оригинальный текст")
                article_part = article.strip()[:12000]
            # Парсим JSON из prompts_part (может быть обёрнут в ```json)
            if "```" in prompts_part:
                m = re.search(r"```(?:json)?\s*([\s\S]*?)```", prompts_part)
                if m:
                    prompts_part = m.group(1).strip()
            prompts_data = []
            try:
                prompts_data = json.loads(prompts_part) if prompts_part else []
            except json.JSONDecodeError:
                arr_m = re.search(r"\[\s*\{[\s\S]*\}\s*\]", prompts_part)
                if arr_m:
                    try:
                        prompts_data = json.loads(arr_m.group(0))
                    except json.JSONDecodeError:
                        pass
            if not isinstance(prompts_data, list):
                prompts_data = []
            result_prompts = []
            for i, item in enumerate(prompts_data[:num_images]):
                if not isinstance(item, dict):
                    continue
                marker = (item.get("marker") or f"[ILLUSTRATION_{i+1}]").strip()
                prompt_english = (item.get("prompt_english") or item.get("prompt") or "").strip()
                caption_ru = (item.get("caption_ru") or item.get("caption") or f"Рисунок {i+1}").strip()
                if prompt_english:
                    # Гарантируем, что модель подпишет всё на русском
                    if "Russian language" not in prompt_english:
                        prompt_english = (
                            prompt_english.rstrip(". ")
                            + f". {labels_hint}"
                        )
                    elif science <= 2 and "simple" not in prompt_english.lower():
                        prompt_english = (
                            prompt_english.rstrip(". ")
                            + " Use very simple Russian labels (1-3 words), avoid complex terms."
                        )
                    result_prompts.append(
                        {"marker": marker, "prompt_english": prompt_english[:500], "caption_ru": caption_ru}
                    )
            logger.info(f"Генерация промптов иллюстраций: получено валидных промптов: {len(result_prompts)}")
            # Если модель не вернула промпты — вставляем маркеры по разделам и строим промпты из заголовков
            if not result_prompts and article_part and "[ILLUSTRATION_1]" not in article_part:
                logger.info("Генерация промптов иллюстраций: промптов нет, используем fallback по структуре статьи")
                article_part, result_prompts = self._fallback_illustration_markers(article_part, num_images)
            return article_part, result_prompts
        except Exception as e:
            logger.warning(f"Ошибка генерации промптов для иллюстраций: {e}")
            article_fallback, prompts_fallback = self._fallback_illustration_markers(article.strip()[:12000], 5)
            logger.info(
                f"Генерация промптов иллюстраций: используем fallback после ошибки, промптов: {len(prompts_fallback)}"
            )
            return article_fallback, prompts_fallback

    def _get_pubmed_theme_phrase(self, theme: str) -> str:
        """
        Возвращает короткую английскую фразу темы для поиска в PubMed,
        чтобы все запросы были привязаны к теме статьи (избегаем случайных результатов).
        """
        theme_clean = (theme or "").strip()
        if not theme_clean:
            return ""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You return only a short English phrase for PubMed search (2-4 words), no explanation. Medical terminology."},
                    {"role": "user", "content": f"PubMed search phrase for this medical topic (reply with phrase only): {theme_clean}"},
                ],
                max_tokens=30,
                temperature=0,
            )
            phrase = (response.choices[0].message.content or "").strip()
            # Убираем кавычки и лишнее
            phrase = phrase.strip('"\'').split("\n")[0].strip()
            if phrase and len(phrase) < 80:
                return phrase
        except Exception as e:
            logger.warning(f"Не удалось получить английскую фразу темы для PubMed: {e}")
        return ""

    def _verify_article_relevance(self, theme: str, title: str, abstract: str) -> bool:
        """
        Проверяет через LLM, что статья (заголовок + аннотация) действительно относится к теме статьи.
        Возвращает True, если статья релевантна или проверка недоступна (оставляем статью).
        """
        theme_clean = (theme or "").strip()
        if not theme_clean:
            return True
        title = (title or "").strip()
        abstract = (abstract or "").strip()[:2000]
        if not title and not abstract:
            return True
        if not has_active_api_key():
            return True
        try:
            content = (
                f"Тема статьи, которую пишем: «{theme_clean}».\n\n"
                f"Найденная статья для цитирования:\nЗаголовок: {title}\n"
            )
            if abstract:
                content += f"Аннотация: {abstract}\n"
            content += (
                "\nОтветь строго одним словом: ДА — если эта статья уместна как источник по данной теме "
                "(то же заболевание/явление/вопрос). НЕТ — если статья явно не по теме (другая нозология, другой объект)."
            )
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "Ты проверяешь релевантность научной статьи теме. Отвечай только ДА или НЕТ."},
                    {"role": "user", "content": content},
                ],
                max_tokens=10,
                temperature=0,
            )
            text = (response.choices[0].message.content or "").strip().upper()
            return "ДА" in text or "YES" in text
        except Exception as e:
            logger.warning(f"Проверка релевантности статьи PubMed: {e}")
            return True

    def execute_article_searches(
        self,
        plan: List[dict],
        theme: str = "",
        max_chars: int = 12000,
    ) -> Tuple[str, str]:
        """
        Шаг 2: Параллельный поиск в PubMed по searchQueries из плана.
        Если передан theme, все запросы привязываются к теме (английская фраза + запрос),
        чтобы результаты соответствовали теме статьи.
        Возвращает (контекст для промпта, список источников для раздела «Источники»).
        """
        all_queries: List[Tuple[str, str]] = []
        for item in plan:
            step_name = item.get("step", "")
            for q in item.get("searchQueries") or []:
                if q and str(q).strip():
                    all_queries.append((step_name, str(q).strip()))
        if not all_queries:
            return "", ""

        # Привязка к теме: один общий английский термин для PubMed, подставляем ко всем запросам
        theme_phrase = ""
        if (theme or "").strip():
            theme_phrase = self._get_pubmed_theme_phrase(theme.strip())
        if theme_phrase:
            # Каждый запрос = тема + аспект; так PubMed не вернёт случайные статьи
            scoped: List[Tuple[str, str]] = []
            for step_name, q in all_queries:
                q_clean = q.strip()
                # Если запрос уже содержит тему (начинается с похожих слов), не дублируем
                q_lower = q_clean.lower()
                theme_words = set(theme_phrase.lower().split())
                if theme_words and not any(w in q_lower for w in theme_words if len(w) > 2):
                    q_clean = f"{theme_phrase} {q_clean}"
                quality_filter = self._source_quality_pubmed_filter()
                if quality_filter:
                    q_clean = (q_clean.rstrip() + quality_filter).strip()
                scoped.append((step_name, q_clean))
            all_queries = scoped
        else:
            quality_filter = self._source_quality_pubmed_filter()
            if quality_filter:
                all_queries = [(s, (q.rstrip() + quality_filter).strip()) for s, q in all_queries]

        # Собираем все статьи с дедупликацией по PMID
        # theme_in_title_abstract — только статьи, где тема в заголовке/аннотации; fallback при 0 результатах
        seen_pmid: set = set()
        ordered_entries: List[dict] = []

        def fetch_entries(args):
            step_name, query = args
            try:
                entries = fetch_pubmed_entries(
                    query,
                    max_results=12,
                    sort="relevance",
                    theme_in_title_abstract=theme_phrase if theme_phrase else None,
                )
                if theme_phrase and entries:
                    entries = filter_entries_by_title_relevance(entries, theme_phrase, min_words_in_title=2)
                return (step_name, query, entries[:6])
            except Exception as e:
                logger.warning(f"PubMed для «{query}»: {e}")
                return (step_name, query, [])

        step_blocks: List[Tuple[str, str, List[dict]]] = []
        with ThreadPoolExecutor(max_workers=min(5, len(all_queries))) as ex:
            futures = {ex.submit(fetch_entries, a): a for a in all_queries}
            for future in as_completed(futures):
                try:
                    step_name, query, entries = future.result()
                    for e in entries:
                        if e.get("pmid") and e["pmid"] not in seen_pmid:
                            seen_pmid.add(e["pmid"])
                            ordered_entries.append(e)
                    if entries:
                        step_blocks.append((step_name, query, entries))
                except Exception:
                    pass

        if not ordered_entries:
            return "", ""

        # Сортируем по релевантности: больше слов темы в заголовке — выше
        if theme_phrase:
            from core.pubmed import score_entry_by_theme
            ordered_entries.sort(key=lambda e: score_entry_by_theme(e, theme_phrase), reverse=True)

        # Фильтр релевантности: для каждой статьи загружаем аннотацию и проверяем через LLM, что она по теме
        theme_for_filter = (theme or "").strip()
        if theme_for_filter and ordered_entries:
            pmids = [e["pmid"] for e in ordered_entries if e.get("pmid")]
            abstracts = fetch_abstracts_for_pmids(pmids)
            for e in ordered_entries:
                e["abstract"] = abstracts.get(e.get("pmid", ""), "")
            verified_pmids = set()
            for e in ordered_entries:
                if self._verify_article_relevance(
                    theme_for_filter,
                    e.get("title") or "",
                    e.get("abstract") or "",
                ):
                    verified_pmids.add(e["pmid"])
            if verified_pmids:
                ordered_entries = [e for e in ordered_entries if e.get("pmid") in verified_pmids]
                step_blocks = [
                    (step_name, query, [e for e in entries if e.get("pmid") in verified_pmids])
                    for step_name, query, entries in step_blocks
                ]
            if not ordered_entries:
                return "", ""

        pmid_to_num = {e["pmid"]: i + 1 for i, e in enumerate(ordered_entries)}

        def fmt_ref(e: dict, num: int) -> str:
            j = e.get("journal") or ""
            y = e.get("year") or ""
            meta = ", ".join(p for p in [j, y] if p)
            return f"[{num}] {e.get('title', '')}" + (f" ({meta})." if meta else ".") + f" PMID: {e.get('pmid', '')}"

        sources_list = "\n".join(fmt_ref(e, i + 1) for i, e in enumerate(ordered_entries))

        parts = []
        total = 0
        for step_name, query, entries in step_blocks:
            ref_lines = []
            for e in entries:
                num = pmid_to_num.get(e.get("pmid"))
                if num:
                    ref_lines.append(fmt_ref(e, num))
            if not ref_lines:
                continue
            block = f"\n### {step_name}\n**Запрос:** {query}\n" + "\n".join(ref_lines)
            if total + len(block) > max_chars:
                break
            parts.append(block)
            total += len(block)

        context = "\n".join(parts) if parts else ""
        return context, sources_list

    def _is_article_incomplete(self, article: str, plan: List[dict], sources_list: str) -> bool:
        """Проверяет, обрезана ли статья (нужно продолжение)."""
        if not article or len(article.strip()) < 500:
            return False
        text = article.strip()
        # Обрыв на середине: заканчивается на "..." или союз/запятую без точки
        trailing = text[-80:].rstrip()
        if trailing.endswith("...") or trailing.endswith(",") or trailing.endswith(" и") or trailing.endswith(" —"):
            return True
        # Не хватает раздела Источники при наличии источников
        if sources_list and "## Источники" not in text and "## Источники" not in text.replace(" ", ""):
            return True
        # Слишком мало разделов ## (меньше половины плана)
        sections = len(re.findall(r"^##\s+", text, re.MULTILINE))
        if plan and sections < max(3, len(plan) // 2):
            return True
        return False

    def generate_article_final_stream(
        self,
        theme: str,
        plan: List[dict],
        search_context: str,
        audience: str = "подготовленная аудитория",
        sources_list: str = "",
    ):
        """
        Шаг 3: Генерация финальной статьи с учётом плана и результатов поиска.
        Генератор, yield'ит чанки текста (стриминг).
        Если передан sources_list — в тексте нужно цитировать источники [1], [2], … и в конце добавить раздел «Источники».
        """
        theme_clean = theme.strip()
        plan_text = "\n".join(
            f"{i+1}. **{s.get('step', '')}**: {s.get('description', '')}"
            for i, s in enumerate(plan)
        )
        style_guidance = self._style_guidance_text()
        user_content = (
            f"Напиши большую научную статью на русском по теме «{theme_clean}» для аудитории: {audience}.\n\n"
            "**План статьи (следуй ему строго):**\n{}\n\n"
        ).format(plan_text)
        if search_context:
            remaining = max(0, 12000 - len(user_content))
            user_content += (
                "**Источники из PubMed (цитируй в тексте по номерам [1], [2], …):**\n{}\n\n"
            ).format(search_context[:remaining])
        if sources_list:
            user_content += (
                "**Список источников для раздела «Источники» (обязательно добавь в конец статьи):**\n{}\n\n"
            ).format(sources_list[:4000])
        if style_guidance:
            user_content += f"**Параметры управления стилем (соблюдай строго):**\n{style_guidance}\n\n"
        user_content += self._main_instruction_for_article(with_sources=bool(sources_list))
        max_tokens_article = min(65536, settings_manager.get("max_tokens_article", 32768))
        user_content += "\n\n" + self._volume_guidance_for_tokens(max_tokens_article)
        system_msg = self._system_message_for_article()
        article_input_tokens = self._estimate_tokens(system_msg + "\n\n" + user_content)
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_content},
        ]
        try:
            self._check_token_budget()
            collected = []
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens_article,
                temperature=self.temperature,
                stream=True,
            )
            chunk_count = 0
            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and getattr(delta, "content", None):
                    chunk_count += 1
                    collected.append(delta.content)
                    yield delta.content
            # Если стрим не вернул ни одного чанка (часто с Gemini при некорректном разборе SSE) — fallback на один запрос без стрима
            if chunk_count == 0:
                logger.warning("Стриминг статьи не вернул данных, повтор без стрима (generate_article_final_stream).")
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens_article,
                    temperature=self.temperature,
                    stream=False,
                )
                content = (response.choices[0].message.content or "").strip() if response.choices else ""
                if content:
                    collected.append(content)
                    yield content
            # Продолжение при обрыве статьи
            article_so_far = "".join(collected)
            if chunk_count > 0 and self._is_article_incomplete(article_so_far, plan, sources_list):
                logger.info("Статья обрезана, запрашиваю продолжение...")
                continue_messages = [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": article_so_far[-30000:]},  # последние 30k символов
                    {"role": "user", "content": (
                        "Статья обрезана. Продолжи с того места, где остановился. Не повторяй уже написанное. "
                        "Добавь оставшиеся разделы по плану. "
                        + ("Обязательно заверши разделом ## Источники со списком использованных источников." if sources_list else "Заверши заключением.")
                    )},
                ]
                try:
                    stream2 = self.client.chat.completions.create(
                        model=self.model,
                        messages=[messages[0]] + continue_messages,
                        max_tokens=max_tokens_article,
                        temperature=self.temperature,
                        stream=True,
                    )
                    for chunk in stream2:
                        delta = chunk.choices[0].delta if chunk.choices else None
                        if delta and getattr(delta, "content", None):
                            collected.append(delta.content)
                            yield delta.content
                except Exception as e2:
                    logger.warning(f"Не удалось продолжить статью: {e2}")
            # Финальная оценка стоимости LLM для статьи (план уже оценён в generate_article_plan).
            full_output = "".join(collected)

            # Fallback: если раздел Источники так и не появился, добавляем его принудительно
            if sources_list and "## Источники" not in full_output and "## Источники" not in full_output.replace(" ", ""):
                logger.info("Раздел Источники отсутствует в ответе LLM, добавляю принудительно.")
                sources_block = f"\n\n## Источники\n\n{sources_list}"
                full_output += sources_block
                yield sources_block

            article_output_tokens = self._estimate_tokens(full_output)
            article_cost = self._estimate_llm_cost_usd(article_input_tokens, article_output_tokens)
            self._last_article_cost_stats["article"] = {
                "provider": self.provider,
                "model": self.model,
                "input_tokens_est": article_input_tokens,
                "output_tokens_est": article_output_tokens,
                "cost_usd_est": article_cost,
            }
            plan_cost = self._last_article_cost_stats.get("plan", {}).get("cost_usd_est")
            if plan_cost is not None or article_cost is not None:
                self._last_article_cost_stats["total_cost_usd_est"] = (
                    (float(plan_cost) if plan_cost is not None else 0.0)
                    + (float(article_cost) if article_cost is not None else 0.0)
                )
            self._log_usage("article_generation", article_input_tokens, article_output_tokens)
        except Exception as e:
            logger.error(f"Ошибка стриминга статьи: {e}")
            raise

    def generate_article(
        self,
        theme: str,
        source_texts: Optional[List[str]] = None,
        audience: str = "подготовленная аудитория",
    ) -> str:
        """
        Генерирует структурированную обзорную статью.

        Если source_texts переданы — опирается на них и минимизирует «галлюцинации».
        Если нет — пишет обзорную статью по теме на основе общих знаний модели.
        """
        if not theme or not theme.strip():
            raise ValueError("Тема статьи не указана.")

        theme_clean = theme.strip()
        style_guidance = self._style_guidance_text()

        if source_texts:
            joined = "\n\n---\n\n".join(t.strip() for t in source_texts if t and t.strip())
            # Ограничиваем размер исходного корпуса, чтобы не переполнить контекст
            max_chars = 40_000
            if len(joined) > max_chars:
                joined = joined[:max_chars] + "\n\n[Текст обрезан по длине для генерации статьи]"
            user_content = (
                f"Ты помощник редактора. По теме «{theme_clean}» нужно написать "
                f"структурированную учебную статью для аудитории: {audience}.\n\n"
                "Используй ТОЛЬКО следующий корпус исходных текстов (не придумывай факты вне них, "
                "если явно не указано и не включена опция исследований):\n\n"
                f"{joined}\n\n"
                "Составь единую цельную статью на русском языке, строго следуя структуре ниже. Стиль и уровень научности — строго по блоку «Параметры управления стилем» в конце запроса.\n"
                "ОБЯЗАТЕЛЬНО: В самом начале статьи добавь раздел ## Содержание со списком всех разделов.\n"
                "1. Введение\n"
                "2. Контекст и актуальность\n"
                "3. Ключевые определения и базовые понятия\n"
                "4. Механизмы / принципы / устройство (если уместно)\n"
                "5. Практические аспекты / применение / примеры\n"
                "6. Ограничения, риски, типичные ошибки (если уместно)\n"
                "7. Современные подходы и исследования (если уместно и не противоречит корпусу)\n"
                "8. Заключение\n\n"
                "Не дублируй заголовок темы, сразу переходи к разделу «Введение».\n"
                "Раскрывай каждый раздел по 3–5 абзацев. Соблюдай уровень научности и глубины из параметров стиля в конце запроса.\n"
                "КРИТИЧНО: напиши статью ПОЛНОСТЬЮ до конца, не обрывай на середине раздела.\n"
                "Верни результат в виде Markdown-документа с заголовками второго уровня (##) для разделов.\n"
                "Форматирование: используй абзацы, маркированные списки (-), нумерованные списки (1. 2. 3.), "
                "**жирный** для ключевых терминов — для разнообразного и удобного чтения."
            )
            if style_guidance:
                user_content += f"\n\n**Параметры управления стилем:**\n{style_guidance}"
            max_tokens_article = min(65536, settings_manager.get("max_tokens_article", 32768))
            user_content += "\n\n" + self._volume_guidance_for_tokens(max_tokens_article)
        else:
            user_content = (
                f"Напиши большую статью на русском языке по теме «{theme_clean}» для аудитории: {audience}. Стиль и уровень научности — строго по блоку «Параметры управления стилем» в конце запроса.\n\n"
                "**ШАГ 1 — ПЛАН:**\n"
                "Сформируй подробный план и выведи его в разделе «## План статьи» (10–20 пунктов). План должен включать научные аспекты: механизмы/принципы, классификации (если уместно), доказательная база/источники, терминология, плюс разнообразие (история, мифы и факты, практические примеры).\n\n"
                "**ШАГ 2 — РАСШИФРОВКА ПЛАНА:**\n"
                "Ниже напиши саму статью — подробно, по 4–6 абзацев на раздел. Строго соблюдай уровень научности из параметров стиля: при 1–2 звёздах — простой язык, научпоп; при 4–5 — научная глубина и терминология. Плавные переходы, примеры где уместно.\n"
                "ОБЯЗАТЕЛЬНО: В самом начале статьи добавь раздел ## Содержание со списком всех разделов.\n\n"
                "**Стиль и глубина:** определяются параметрами стиля ниже. Рассмотри тему в соответствии с выбранной глубиной: базовые понятия, ключевые механизмы/принципы, примеры применения, ограничения/риски; при необходимости — история, мифы и факты.\n\n"
                "**Рекомендуемая универсальная структура (можно расширять и дробить на подпункты плана):**\n"
                "1. Введение (интригующее, цепляющее)\n"
                "2. Контекст и актуальность\n"
                "3. Термины и базовые понятия\n"
                "4. Как это работает / ключевые принципы\n"
                "5. Практика и примеры\n"
                "6. Ошибки, ограничения, риски\n"
                "7. Современные исследования и тренды (если уместно)\n"
                "8. Заключение\n\n"
                "Дополнительно: внутри уместных разделов добавь 1–2 мини‑блока с «мифами и фактами» или "
                "«частыми вопросами», чтобы удержать интерес.\n\n"
                "Форматирование: используй Markdown с заголовками уровня ## для крупных разделов, абзацы через "
                "пустую строку, маркированные (-) и нумерованные (1. 2. 3.) списки, **жирный** для ключевых терминов. "
                "Сначала выведи план, затем полную статью по этому плану. КРИТИЧНО: напиши статью ПОЛНОСТЬЮ до конца, не обрывай на середине раздела."
            )
            if style_guidance:
                user_content += f"\n\n**Параметры управления стилем:**\n{style_guidance}"
            max_tokens_article = min(65536, settings_manager.get("max_tokens_article", 32768))
            user_content += "\n\n" + self._volume_guidance_for_tokens(max_tokens_article)

        if self.include_research:
            user_content += (
                "\n\nЕсли это не противоречит корпусу текста, аккуратно добавь 1–3 абзаца про актуальные "
                "научные исследования и современные подходы, с формулировками вида «по данным современных "
                "исследований» без точных ссылок."
            )

        system_content = self._system_message_for_article()
        max_tokens_article = min(65536, settings_manager.get("max_tokens_article", 32768))
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=max_tokens_article,
                temperature=self.temperature,
            )
            content = (response.choices[0].message.content or "").strip()
            if not content:
                raise RuntimeError("Модель вернула пустой ответ при генерации статьи.")
            if _is_model_refusal(content):
                logger.warning("Модель отказалась генерировать статью, возвращаю краткое сообщение об отказе.")
                return "Модель отказалась сгенерировать статью по запрошенной теме."
            return content
        except Exception as e:
            logger.error(f"Ошибка генерации статьи: {e}")
            raise

    def generate_article_stream(
        self,
        theme: str,
        source_texts: Optional[List[str]] = None,
        audience: str = "подготовленная аудитория",
    ):
        """
        То же, что generate_article, но с потоковой отдачей чанков (стриминг).
        Удобно для Gemini и других провайдеров — текст появляется по мере генерации.
        """
        if not theme or not theme.strip():
            raise ValueError("Тема статьи не указана.")

        theme_clean = theme.strip()
        style_guidance = self._style_guidance_text()

        if source_texts:
            joined = "\n\n---\n\n".join(t.strip() for t in source_texts if t and t.strip())
            max_chars = 40_000
            if len(joined) > max_chars:
                joined = joined[:max_chars] + "\n\n[Текст обрезан по длине для генерации статьи]"
            user_content = (
                f"Ты помощник медицинского редактора. По теме «{theme_clean}» нужно написать "
                f"структурированную учебную статью для аудитории: {audience}.\n\n"
                "Используй ТОЛЬКО следующий корпус исходных текстов (не придумывай факты вне них, "
                "если явно не указано и не включена опция исследований):\n\n"
                f"{joined}\n\n"
                "Составь единую цельную статью на русском языке, строго следуя структуре ниже. Стиль и уровень научности — строго по блоку «Параметры управления стилем» в конце запроса.\n"
                "ОБЯЗАТЕЛЬНО: В самом начале статьи добавь раздел ## Содержание со списком всех разделов.\n"
                "1. Введение\n"
                "2. Актуальность проблемы\n"
                "3. Анатомия и физиология (если уместно)\n"
                "4. Этиология и патогенез (механизмы, ключевые звенья)\n"
                "5. Клиническая картина\n"
                "6. Диагностика (критерии, методы визуализации, лабораторные показатели)\n"
                "7. Лечение (принципы, доказательная база, без торговых названий)\n"
                "8. Профилактика и реабилитация\n"
                "9. Заключение\n\n"
                "Не дублируй заголовок темы, сразу переходи к разделу «Введение».\n"
                "Раскрывай каждый раздел по 3–5 абзацев. Соблюдай уровень научности и глубины из параметров стиля в конце запроса.\n"
                "КРИТИЧНО: напиши статью ПОЛНОСТЬЮ до конца, не обрывай на середине раздела.\n"
                "Верни результат в виде Markdown-документа с заголовками второго уровня (##) для разделов.\n"
                "Форматирование: используй абзацы, маркированные списки (-), нумерованные списки (1. 2. 3.), "
                "**жирный** для ключевых терминов — для разнообразного и удобного чтения."
            )
            if style_guidance:
                user_content += f"\n\n**Параметры управления стилем:**\n{style_guidance}"
            max_tokens_article = min(65536, settings_manager.get("max_tokens_article", 32768))
            user_content += "\n\n" + self._volume_guidance_for_tokens(max_tokens_article)
        else:
            user_content = (
                f"Напиши большую статью на русском языке по теме «{theme_clean}» для аудитории: {audience}. Стиль и уровень научности — строго по блоку «Параметры управления стилем» в конце запроса.\n\n"
                "**ШАГ 1 — ПЛАН:**\n"
                "Сформируй подробный план и выведи его в разделе «## План статьи» (10–20 пунктов). План должен включать научные аспекты: механизмы, патогенез, классификации, доказательная база, терминология, плюс разнообразие (история, мифы и факты, клиника).\n\n"
                "**ШАГ 2 — РАСШИФРОВКА ПЛАНА:**\n"
                "Ниже напиши саму статью — подробно, по 4–6 абзацев на раздел. Строго соблюдай уровень научности из параметров стиля: при 1–2 звёздах — простой язык, научпоп; при 4–5 — научная глубина и терминология. Плавные переходы, примеры где уместно.\n"
                "ОБЯЗАТЕЛЬНО: В самом начале статьи добавь раздел ## Содержание со списком всех разделов.\n\n"
                "**Стиль и глубина:** определяются параметрами стиля ниже. Рассмотри тему в соответствии с выбранной глубиной: этиология, патогенез, клиника, диагностика, лечение, профилактика; при необходимости — история, мифы и факты.\n\n"
                "**Рекомендуемая медицинская структура (можно расширять и дробить на подпункты плана):**\n"
                "1. Введение (интригующее, цепляющее)\n"
                "2. Актуальность проблемы\n"
                "3. Анатомия и физиология (если уместно)\n"
                "4. Этиология и патогенез\n"
                "5. Клиническая картина\n"
                "6. Диагностика (визуализация, лабораторные методы)\n"
                "7. Лечение (принципы, без торговых названий)\n"
                "8. Профилактика и реабилитация\n"
                "9. Заключение\n\n"
                "Дополнительно: внутри уместных разделов добавь 1–2 мини‑блока с «мифами и фактами» или "
                "«частыми вопросами», чтобы удержать интерес.\n\n"
                "Форматирование: используй Markdown с заголовками уровня ## для крупных разделов, абзацы через "
                "пустую строку, маркированные (-) и нумерованные (1. 2. 3.) списки, **жирный** для ключевых терминов. "
                "Сначала выведи план, затем полную статью по этому плану. КРИТИЧНО: напиши статью ПОЛНОСТЬЮ до конца, не обрывай на середине раздела."
            )
            if style_guidance:
                user_content += f"\n\n**Параметры управления стилем:**\n{style_guidance}"
            max_tokens_article = min(65536, settings_manager.get("max_tokens_article", 32768))
            user_content += "\n\n" + self._volume_guidance_for_tokens(max_tokens_article)

        if self.include_research:
            user_content += (
                "\n\nЕсли это не противоречит корпусу текста, аккуратно добавь 1–3 абзаца про актуальные "
                "научные исследования и современные подходы, с формулировками вида «по данным современных "
                "исследований» без точных ссылок."
            )

        system_content = self._system_message_for_article()
        max_tokens_article = min(65536, settings_manager.get("max_tokens_article", 32768))
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=max_tokens_article,
                temperature=self.temperature,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and getattr(delta, "content", None):
                    yield delta.content
        except Exception as e:
            logger.error(f"Ошибка стриминга статьи: {e}")
            raise

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
    parser.add_argument("--provider", type=str, choices=["openai", "deepseek", "gemini"],
                        help="Выбрать провайдера LLM (openai, deepseek или gemini)")
    args = parser.parse_args()

    try:
        if args.provider:
            from settings_manager import set_llm_provider
            set_llm_provider(args.provider)
            print(f"🤖 Провайдер LLM изменен на {args.provider}")
            return

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
                if key in ["openai_api_key", "deepseek_api_key", "gemini_api_key"] and value:
                    if len(value) > 12:
                        print(f"  {key}: {value[:8]}...{value[-4:]}")
                    else:
                        print(f"  {key}: {value}")
                elif key in ["openai_api_key", "deepseek_api_key", "gemini_api_key"] and not value:
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
        provider = get_llm_provider()
        api_key = args.api_key
        if not api_key:
            if has_active_api_key():
                api_key = (
                    get_gemini_api_key()
                    if provider == "gemini"
                    else get_deepseek_api_key()
                    if provider == "deepseek"
                    else get_api_key()
                )
                logger.info("Используется сохраненный API-ключ из настроек")
            else:
                prompt_name = {"gemini": "Gemini", "deepseek": "DeepSeek"}.get(provider, "OpenAI")
                api_key = input(f"Введите API-ключ {prompt_name}: ").strip()
                if api_key:
                    save_key = input("Сохранить API-ключ для будущих запусков? (y/n): ").strip().lower()
                    if save_key in ["y", "yes", "да"]:
                        if provider == "gemini":
                            from settings_manager import set_gemini_api_key
                            set_gemini_api_key(api_key)
                        elif provider == "deepseek":
                            from settings_manager import set_deepseek_api_key
                            set_deepseek_api_key(api_key)
                        else:
                            set_api_key(api_key)
                        logger.info("API-ключ сохранен в настройках")
                else:
                    logger.error("API-ключ не указан")
                    print("❌ API-ключ обязателен для работы. Настройте провайдера и ключ в Настройках или укажите --api-key.")
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
