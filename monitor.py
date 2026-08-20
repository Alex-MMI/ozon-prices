"""
Озон Монитор цен — GitHub Actions Edition
Запускается автоматически каждый час через GitHub Actions.
Управление через Telegram-бота: просто отправь ссылку на товар.
"""

import json
import os
import re
import time
import requests
from datetime import datetime

# ── Настройки из GitHub Secrets ──────────────────────────────────────────────
TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
DATA_FILE  = "products.json"

# ── Заголовки для запросов к Озону ───────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── Загрузка / сохранение данных ─────────────────────────────────────────────
def load_data():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"_offset": 0, "products": {}}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Telegram ─────────────────────────────────────────────────────────────────
def tg_send(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}")


def tg_updates(offset: int):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 5},
            timeout=10,
        )
        return r.json().get("result", [])
    except Exception:
        return []


# ── Парсер цены Озон ─────────────────────────────────────────────────────────
def _find(obj, key, depth=0):
    if depth > 15:
        return None
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, "", 0):
            return obj[key]
        for v in obj.values():
            r = _find(v, key, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj[:15]:
            r = _find(item, key, depth + 1)
            if r is not None:
                return r
    return None


def fetch_price(url: str) -> dict:
    result = {"name": None, "price": None, "image": None}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        html = resp.text

        # 1. __NEXT_DATA__
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            data = json.loads(m.group(1))
            price = _find(data, "finalPrice") or _find(data, "salePrice") or _find(data, "price")
            name  = _find(data, "displayName") or _find(data, "name") or _find(data, "title")
            image = _find(data, "coverImage") or _find(data, "image")
            if price:
                result["price"] = float(re.sub(r"[^\d.]", "", str(price)))
            if name and isinstance(name, str) and len(name) > 4:
                result["name"] = name[:250]
            if image and isinstance(image, str) and image.startswith("http"):
                result["image"] = image

        # 2. JSON-LD
        if not result["price"]:
            m2 = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
            if m2:
                ld = json.loads(m2.group(1))
                if isinstance(ld, list): ld = ld[0]
                offers = ld.get("offers", {})
                if isinstance(offers, list): offers = offers[0]
                p = offers.get("price") or offers.get("lowPrice")
                if p:
                    result["price"] = float(str(p).replace(",", "."))
                result["name"] = result["name"] or ld.get("name", "")[:250]

        # 3. og:title
        if not result["name"]:
            m3 = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            if m3:
                result["name"] = m3.group(1)[:250]

    except Exception as e:
        print(f"  Ошибка при получении {url}: {e}")

    return result


# ── Форматирование цены ───────────────────────────────────────────────────────
def fmt(price):
    return f"{price:,.0f} ₽".replace(",", " ")


# ── Обработка команд из Telegram ─────────────────────────────────────────────
def process_commands(data: dict) -> bool:
    updates  = tg_updates(data["_offset"])
    changed  = False

    for upd in updates:
        data["_offset"] = upd["update_id"] + 1
        msg  = upd.get("message") or upd.get("channel_post", {})
        text = (msg.get("text") or "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))

        if chat != str(TG_CHAT_ID):
            continue

        # Просто ссылка или /add ссылка → добавить
        urls = re.findall(r'https?://(?:www\.)?ozon\.ru/product/[^\s]+', text)
        if urls:
            for url in urls:
                url = url.rstrip(".,)")
                if url not in data["products"]:
                    data["products"][url] = {
                        "name": None, "last_price": None,
                        "added": datetime.utcnow().isoformat()
                    }
                    tg_send(f"✅ Добавлен в мониторинг!\nЦену проверю в ближайший час.\n\n🔗 {url}")
                    changed = True
                else:
                    tg_send("Этот товар уже отслеживается.")
            continue

        if text in ("/list", "/список"):
            prods = data["products"]
            if not prods:
                tg_send("Список пуст. Просто отправь ссылку на товар Озон — добавлю.")
            else:
                lines = [f"📋 <b>Отслеживается {len(prods)} товара(ов):</b>\n"]
                for url, p in prods.items():
                    name  = p.get("name") or "без названия"
                    price = p.get("last_price")
                    pstr  = fmt(price) if price else "ещё не проверялось"
                    lines.append(f"• <b>{name[:60]}</b>\n  💰 {pstr}\n  <a href=\"{url}\">ссылка</a>")
                tg_send("\n\n".join(lines))
            continue

        if text.startswith("/del ") or text.startswith("/удали "):
            idx_str = text.split(None, 1)[1].strip()
            prods   = list(data["products"].keys())
            try:
                idx = int(idx_str) - 1
                url = prods[idx]
                name = data["products"][url].get("name") or url[:50]
                del data["products"][url]
                tg_send(f"🗑 Удалён: {name}")
                changed = True
            except Exception:
                tg_send("Укажи номер товара из /list, например: /del 2")
            continue

        if text in ("/help", "/старт", "/start"):
            tg_send(
                "🛒 <b>Озон Монитор цен</b>\n\n"
                "Просто отправь ссылку на товар Озон — начну следить за ценой "
                "и пришлю уведомление когда она изменится.\n\n"
                "Команды:\n"
                "/list — список отслеживаемых товаров\n"
                "/del 2 — удалить товар №2 из списка"
            )
            continue

    return changed


# ── Проверка цен ─────────────────────────────────────────────────────────────
def check_prices(data: dict) -> bool:
    products = data["products"]
    if not products:
        print("Нет товаров для проверки.")
        return False

    changed = False
    print(f"Проверяю {len(products)} товар(ов)...")

    for url, p in products.items():
        name = p.get("name") or url[:60]
        print(f"  → {name}...")

        info = fetch_price(url)
        new_price = info.get("price")

        # Обновляем название и картинку если ещё не было
        if not p.get("name") and info.get("name"):
            p["name"] = info["name"]
        if not p.get("image") and info.get("image"):
            p["image"] = info["image"]

        if new_price is None:
            print(f"    ✗ цена не найдена")
            time.sleep(2)
            continue

        old_price = p.get("last_price")
        print(f"    ✓ {fmt(new_price)}", end="")

        if old_price is not None and abs(new_price - old_price) > 0.5:
            diff    = new_price - old_price
            pct     = abs(diff) / old_price * 100
            emoji   = "📉" if diff < 0 else "📈"
            direction = "снизилась" if diff < 0 else "поднялась"
            print(f"  ({'+' if diff > 0 else ''}{diff:,.0f} ₽)".replace(",", " "))

            tg_send(
                f"{emoji} <b>Цена {direction}!</b>\n\n"
                f"📦 {p.get('name') or 'Товар'}\n"
                f"💰 Было: <b>{fmt(old_price)}</b>\n"
                f"💰 Стало: <b>{fmt(new_price)}</b>\n"
                f"{'🔻' if diff < 0 else '🔺'} {fmt(abs(diff))} ({pct:.1f}%)\n\n"
                f"🔗 <a href=\"{url}\">Открыть на Озоне</a>"
            )
        else:
            print()  # новая строка

        p["last_price"] = new_price
                p["checked_at"] = datetime.utcnow().isoformat()
        if "history" not in p:
            p["history"] = []
        p["history"].append({"price": new_price, "at": datetime.utcnow().isoformat()})
        p["history"] = p["history"][-30:]
        p["checked_at"] = datetime.utcnow().isoformat()
        changed = True

        # пауза между запросами
        time.sleep(3)

    return changed


# ── Главная точка входа ───────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print(f"Запуск: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    data = load_data()
    cmd_changed   = process_commands(data)
    price_changed = check_prices(data)

    if cmd_changed or price_changed:
        save_data(data)
        print("Данные сохранены.")

    print("Готово.")
