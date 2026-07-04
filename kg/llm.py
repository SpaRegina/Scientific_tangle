# -*- coding: utf-8 -*-
"""
Опциональный LLM-модуль (routerai.ru — разрешён правилами хакатона; OpenAI-совместимый API).
Настройка через .env в корне проекта или переменные окружения:
  ROUTERAI_API_KEY  — ключ (если не задан, система работает в rule-based режиме)
  ROUTERAI_BASE_URL — по умолчанию https://routerai.ru/api/v1
  ROUTERAI_MODEL    — по умолчанию minimax/minimax-m3
Используется для: (1) разбора запроса на естественном языке, (2) синтеза ответа с цитатами,
(3) дообогащения графа связями. Всё имеет rule-based fallback — LLM не обязателен.
"""
import os
import json
import urllib.request
import urllib.error


def _load_dotenv():
    """Подхватываем .env из корня проекта (без сторонних зависимостей)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_dotenv()

BASE_URL = os.environ.get("ROUTERAI_BASE_URL", "https://routerai.ru/api/v1")
API_KEY = os.environ.get("ROUTERAI_API_KEY", "")
MODEL = os.environ.get("ROUTERAI_MODEL", "minimax/minimax-m3")
ENABLED = os.environ.get("ROUTERAI_ENABLE", "").strip().lower() in {"1", "true", "yes", "on"}

LAST_ERROR = None  # диагностика последнего сбоя LLM (отдаётся в /api/ask)


def available():
    return bool(API_KEY and ENABLED)


def _strip_think(text):
    """Reasoning-модели (minimax-m3 и др.) могут возвращать <think>...</think> — убираем."""
    import re
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def chat(messages, max_tokens=4000, temperature=0.2, timeout=120):
    """max_tokens с запасом: у reasoning-моделей рассуждения тоже расходуют токены.
    Сетевые сбои (таймаут TLS-рукопожатия и т.п.) ретраятся один раз."""
    global LAST_ERROR
    payload = json.dumps({"model": MODEL, "messages": messages,
                          "max_tokens": max_tokens, "temperature": temperature}).encode()
    data = None
    for attempt in (1, 2):
        req = urllib.request.Request(
            BASE_URL.rstrip("/") + "/chat/completions", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:
            LAST_ERROR = f"HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}"
            raise
        except Exception as e:
            LAST_ERROR = f"{type(e).__name__}: {e}"
            if attempt == 2:
                raise
    if "error" in data:
        LAST_ERROR = str(data["error"])[:300]
        raise RuntimeError(LAST_ERROR)
    msg = data["choices"][0]["message"]
    content = _strip_think(msg.get("content") or "")
    if not content:
        LAST_ERROR = "пустой ответ модели (возможно, весь бюджет токенов ушёл на reasoning)"
        raise RuntimeError(LAST_ERROR)
    LAST_ERROR = None
    return content


def parse_query(question, known_terms):
    """NL-запрос -> структурный фильтр {terms, types, geography, year_from, numeric}."""
    prompt = (
        "Ты — парсер запросов к графу знаний по горно-металлургическим исследованиям.\n"
        "Верни ТОЛЬКО JSON: {\"terms\": [канонические термины из списка], "
        "\"keywords\": [прочие ключевые слова для полнотекстового поиска], "
        "\"geography\": \"RU\"|\"Мир\"|null, \"year_from\": int|null, "
        "\"numeric\": [{\"property\": str, \"op\": \"<=\"|\">=\"|\"<\"|\">\"|\"~\", \"value\": num, \"unit\": str}]}\n"
        f"Список канонических терминов: {', '.join(sorted(known_terms))}\n\nЗапрос: {question}")
    out = chat([{"role": "user", "content": prompt}], max_tokens=3000)
    out = out[out.find("{"): out.rfind("}") + 1]
    return json.loads(out)


def synthesize_answer(question, contexts):
    """Синтез структурированного ответа по найденным фрагментам с указанием источников."""
    ctx = "\n\n".join(f"[{i+1}] {c['title']} ({c.get('year') or 'год н/д'}, {c.get('geography','')}), стр. {c['page']}:\n{c['text'][:1200]}"
                      for i, c in enumerate(contexts[:10]))
    prompt = (
        "Ты — аналитик R&D горно-металлургической компании. На основе ТОЛЬКО приведённых фрагментов "
        "ответь на вопрос структурированно: краткий вывод, найденные методы/решения с параметрами, "
        "консенсус и противоречия, пробелы. После каждого утверждения ставь ссылку [N] на источник. "
        "Если данных мало — прямо скажи об этом.\n\n"
        f"Вопрос: {question}\n\nФрагменты:\n{ctx}")
    return chat([{"role": "user", "content": prompt}], max_tokens=6000)


def enrich_document(db, doc_id, pages, max_chars=6000):
    """Извлечение дополнительных связей из документа (вызывается при --llm в build)."""
    text = "\n".join(t for _, t in pages)[:max_chars]
    prompt = (
        "Извлеки из текста тройки (субъект, отношение, объект) о процессах, материалах, оборудовании, "
        "условиях и эффектах. Верни ТОЛЬКО JSON-массив: "
        "[{\"src\": str, \"src_type\": \"Material|Process|Equipment|Property|Parameter\", "
        "\"rel\": str, \"dst\": str, \"dst_type\": str, \"quote\": str}]. Не более 15 троек.\n\n" + text)
    out = chat([{"role": "user", "content": prompt}], max_tokens=3000)
    out = out[out.find("["): out.rfind("]") + 1]
    for t in json.loads(out):
        try:
            src = db.node_id(t["src"].strip().lower()[:80], t["src_type"])
            dst = db.node_id(t["dst"].strip().lower()[:80], t["dst_type"])
            db.add_edge(src, dst, t["rel"].strip()[:40], doc_id, 1, [t.get("quote", "")[:300] + " [LLM]"])
        except Exception:
            continue
