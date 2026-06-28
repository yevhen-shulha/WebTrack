#!/usr/bin/env python3
"""
Paleo Festival Ticket Monitor — ЧЕТВЕР (пряма сторінка товару)
Перевіряє наявність квитків на ЧЕТВЕР через пряму сторінку перепродажу
(а не через сторінку каталогу з усіма днями), що значно простіше і надійніше.

Запуск:
    python paleo_monitor.py

Конфігурація — через змінні середовища або .env файл.
"""

import asyncio
import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import httpx
from playwright.async_api import async_playwright

# ─── Конфігурація ────────────────────────────────────────────────────────────

# ID товару для ЧЕТВЕРГА (23 July 2026). Якщо productId зміниться — оновіть тут.
PRODUCT_ID = os.getenv("PRODUCT_ID", "10229259587720")

URL = f"https://bourse.paleo.ch/selection/resale/passItem?productId={PRODUCT_ID}"

# Фраза, яка зявляється на сторінці, коли квитків НЕМАЄ
# (текст стає видимим лише в стані "sold out")
SOLD_OUT_PHRASES = [
    "there are currently no tickets being resold",
    "no tickets being resold",
]

# Ключові слова, що вказують на наявність квитків (кнопка купівлі)
AVAILABLE_KEYWORDS = ["add to cart", "buy"]

# Фраза, яка ЗАВЖДИ присутня на сторінці незалежно від наявності квитків —
# використовується для діагностики (чи сторінка завантажилась коректно)
ALWAYS_PRESENT_PHRASE = "this pass is valid on"

# Інтервал перевірки (секунди). За замовчуванням — 3600 (1 година).
# Встановіть 0 щоб запуститись один раз і вийти (для GitHub Actions / cron).
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 3600))

# ─── Активні години ──────────────────────────────────────────────────────────
ACTIVE_HOURS_ENABLED = os.getenv("ACTIVE_HOURS_ENABLED", "true").lower() == "true"
ACTIVE_HOUR_FROM     = int(os.getenv("ACTIVE_HOUR_FROM", 9))
ACTIVE_HOUR_TO       = int(os.getenv("ACTIVE_HOUR_TO",  20))

# Надсилати статусне повідомлення в Telegram після КОЖНОЇ перевірки (для тестування).
NOTIFY_ALWAYS = os.getenv("NOTIFY_ALWAYS", "false").lower() == "true"

# ─── Налаштування email ───────────────────────────────────────────────────────
EMAIL_ENABLED  = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", 587))
SMTP_USER      = os.getenv("SMTP_USER", "your_email@gmail.com")
SMTP_PASSWORD  = os.getenv("SMTP_PASSWORD", "")
EMAIL_TO       = os.getenv("EMAIL_TO", "your_email@gmail.com")

# ─── Налаштування Telegram ────────────────────────────────────────────────────
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Логування ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("paleo_monitor.log"),
    ],
)
log = logging.getLogger(__name__)


# ─── Функції ─────────────────────────────────────────────────────────────────

async def fetch_page_text() -> str:
    """Завантажує сторінку через Playwright (JS-рендеринг) і повертає видимий текст."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        try:
            response = await page.goto(URL, wait_until="networkidle", timeout=60_000)
            await page.wait_for_timeout(3000)

            # ─── Діагностика ───────────────────────────────────────────
            status = response.status if response else None
            title = await page.title()
            raw_html = await page.content()
            log.info("🌐 HTTP статус відповіді: %s", status)
            log.info("📑 Заголовок сторінки (title): %r", title)
            log.info("📦 Довжина сирого HTML: %d символів", len(raw_html))
            # ─────────────────────────────────────────────────────────

            text = await page.inner_text("body")
        finally:
            await browser.close()
    return text


def check_availability(text: str) -> tuple:
    """
    Перевіряє чи є квитки на сторінці конкретного товару (четвер).

    Текст 'There are currently no tickets being resold' стає ВИДИМИМ
    (а отже потрапляє у inner_text) лише коли квитків немає.
    Кнопка 'Add to cart' / 'Buy' видима лише коли квитки Є.

    Повертає (available: bool, snippet: str).
    """
    lower = text.lower()

    sold_out = any(p in lower for p in SOLD_OUT_PHRASES)
    available = any(kw in lower for kw in AVAILABLE_KEYWORDS)

    if available and not sold_out:
        return True, text.strip()
    if sold_out and not available:
        return False, ""

    # Неоднозначний випадок — обидва або жодного індикатора не знайдено.
    # Поводимось консервативно: вважаємо що квитків немає, але логуємо попередження.
    log.warning(
        "⚠️  Неоднозначний результат розбору сторінки (available=%s, sold_out=%s). "
        "Можливо структура сторінки змінилась.",
        available, sold_out
    )
    return False, ""


def send_email(subject: str, body: str) -> None:
    if not EMAIL_ENABLED:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = EMAIL_TO
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
        log.info("✅ Email надіслано на %s", EMAIL_TO)
    except Exception as e:
        log.error("❌ Помилка надсилання email: %s", e)


async def send_telegram(message: str) -> None:
    if not TELEGRAM_ENABLED:
        return
    try:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        async with httpx.AsyncClient() as client:
            resp = await client.post(api_url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            }, timeout=15)
            resp.raise_for_status()
        log.info("✅ Telegram-повідомлення надіслано")
    except Exception as e:
        log.error("❌ Помилка Telegram: %s", e)


async def notify(snippet: str) -> None:
    """Формує та надсилає сповіщення про знайдені квитки на четвер."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    subject = "🎵 Paleo Festival — квитки на ЧЕТВЕР з'явились!"
    body = (
        f"[{now}] Виявлено квитки на ЧЕТВЕР на Paleo Festival!\n\n"
        f"Сторінка: {URL}\n\n"
        f"Фрагмент сторінки:\n{'-'*40}\n{snippet[:1000]}\n{'-'*40}\n\n"
        "Поспішайте — квитки розходяться швидко!"
    )
    tg_message = (
        f"🎵 <b>Paleo Festival — ЧЕТВЕР!</b>\n"
        f"Квитки на четвер з'явились!\n\n"
        f"👉 <a href='{URL}'>Купити квитки</a>\n\n"
        f"<pre>{snippet[:500]}</pre>"
    )

    send_email(subject, body)
    await send_telegram(tg_message)


async def run_once() -> bool:
    """Одна ітерація перевірки. Повертає True якщо знайдено квитки."""
    log.info("🔍 Перевірка сторінки ЧЕТВЕРГА (productId=%s): %s", PRODUCT_ID, URL)
    try:
        text = await fetch_page_text()

        # ─── Діагностика наявності очікуваного контенту ───────────────
        log.info("📄 Довжина отриманого тексту: %d символів", len(text))
        if ALWAYS_PRESENT_PHRASE in text.lower():
            log.info("✅ Діагностика: сторінка завантажилась коректно (знайдено '%s').", ALWAYS_PRESENT_PHRASE)
        else:
            log.warning(
                "⚠️  Діагностика: фраза '%s' НЕ знайдена! Сторінка могла не завантажитись "
                "повністю, бути заблокованою, або змінити структуру. Перші 500 символів:",
                ALWAYS_PRESENT_PHRASE
            )
            log.warning(text[:500])
        # ───────────────────────────────────────────────────────────────

        available, snippet = check_availability(text)
        if available:
            log.info("🎉 ЗНАЙДЕНО квитки на ЧЕТВЕР!")
            log.info("Фрагмент:\n%s", snippet[:300])
            await notify(snippet)
        else:
            log.info("😴 Квитків на четвер поки немає.")
            if NOTIFY_ALWAYS:
                await send_telegram(
                    f"😴 Квитків на четвер поки немає.\n"
                    f"👉 <a href='{URL}'>Перейти до сторінки</a>"
                )
        return available
    except Exception as e:
        log.error("⚠️  Помилка під час перевірки: %s", e)
        return False


async def main():
    log.info("=" * 55)
    log.info("Paleo Monitor (ЧЕТВЕР) запущено")
    log.info("Сторінка: %s", URL)
    log.info("Інтервал: %d сек (%d хв)", CHECK_INTERVAL, CHECK_INTERVAL // 60)
    if ACTIVE_HOURS_ENABLED:
        log.info("Активні години: %02d:00 – %02d:00", ACTIVE_HOUR_FROM, ACTIVE_HOUR_TO)
    else:
        log.info("Активні години: цілодобово (ACTIVE_HOURS_ENABLED=false)")
    log.info("Email: %s | Telegram: %s | NOTIFY_ALWAYS: %s", EMAIL_ENABLED, TELEGRAM_ENABLED, NOTIFY_ALWAYS)
    log.info("=" * 55)

    # Режим «запустився → перевірив → вийшов» (для GitHub Actions / cron)
    if CHECK_INTERVAL == 0:
        await run_once()
        return

    # Режим безперервного моніторингу
    while True:
        await run_once()
        log.info("⏳ Наступна перевірка через %d хв...", CHECK_INTERVAL // 60)
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
