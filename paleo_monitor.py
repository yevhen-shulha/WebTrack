#!/usr/bin/env python3
"""
Paleo Festival Ticket Monitor
Перевіряє наявність квитків на bourse.paleo.ch
та надсилає сповіщення на email або Telegram.

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
from datetime import datetime, time as dtime
import re

import httpx
from playwright.async_api import async_playwright

# ─── Конфігурація ────────────────────────────────────────────────────────────

URL = "https://bourse.paleo.ch/content?lang=en"

# False = тестовий режим: сповіщати при появі квитків на БУДЬ-ЯКИЙ день.
# True  = бойовий режим:  сповіщати лише якщо день збігається з DAY_KEYWORDS.
DAY_FILTER_ENABLED = False

# Ключові слова для фільтру дня (якщо DAY_FILTER_ENABLED = True).
# Сторінка завантажується з ?lang=en, тому дні завжди англійською.
DAY_KEYWORDS = ["thursday"]

# Регулярний вираз для заголовків дат на сторінці, напр. "Thursday 23 July 2026"
DATE_TITLE_RE = re.compile(
    r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b\s+\d{1,2}\s+\S+\s+\d{4}'
)

# Ключові слова, що вказують на наявність квитків
AVAILABLE_KEYWORDS = ["add to cart", "buy", "ticket", "ajouter", "acheter", "kaufen"]

# Ключові слова, що вказують на відсутність квитків
SOLD_OUT_KEYWORDS = ["sold out", "épuisé", "ausverkauft", "not available"]

# Надсилати статусне повідомлення в Telegram після КОЖНОЇ перевірки (для тестування).
# Встановіть False коли переконаєтесь що все працює.
NOTIFY_ALWAYS = os.getenv("NOTIFY_ALWAYS", "false").lower() == "true"

# Інтервал перевірки (секунди). За замовчуванням — 3600 (1 година).
# Встановіть 0 щоб запуститись один раз і вийти (для GitHub Actions / cron).
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 3600))

# ─── Активні години ──────────────────────────────────────────────────────────
# Скрипт перевіряє сторінку лише в цьому діапазоні часу.
# None = без обмежень (перевіряти цілодобово).
# Часовий пояс береться з системного налаштування сервера (TZ=Europe/Zurich).
ACTIVE_HOURS_ENABLED = os.getenv("ACTIVE_HOURS_ENABLED", "true").lower() == "true"
ACTIVE_HOUR_FROM     = int(os.getenv("ACTIVE_HOUR_FROM", 9))   # включно
ACTIVE_HOUR_TO       = int(os.getenv("ACTIVE_HOUR_TO",  20))   # виключно (20 = до 19:59)

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


# ─── Активні години ──────────────────────────────────────────────────────────

def is_active_hour() -> bool:
    """Повертає True якщо поточний час потрапляє у активний діапазон."""
    if not ACTIVE_HOURS_ENABLED:
        return True
    current_hour = datetime.now().hour
    return ACTIVE_HOUR_FROM <= current_hour < ACTIVE_HOUR_TO


def seconds_until_active() -> int:
    """
    Повертає кількість секунд до початку активного вікна.
    Викликати лише якщо is_active_hour() == False.
    """
    now = datetime.now()
    current_hour = now.hour
    current_minute = now.minute
    current_second = now.second

    if current_hour < ACTIVE_HOUR_FROM:
        # Ще не настав активний час сьогодні
        delta_hours = ACTIVE_HOUR_FROM - current_hour - 1
        delta_minutes = 59 - current_minute
        delta_seconds = 60 - current_second
    else:
        # Активний час вже минув — чекаємо до завтра
        delta_hours = (24 - current_hour + ACTIVE_HOUR_FROM) - 1
        delta_minutes = 59 - current_minute
        delta_seconds = 60 - current_second

    return delta_hours * 3600 + delta_minutes * 60 + delta_seconds


# ─── Функції ─────────────────────────────────────────────────────────────────

async def fetch_page_text() -> str:
    """Завантажує сторінку через Playwright (JS-рендеринг) і повертає текст."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(URL, wait_until="networkidle", timeout=60_000)
            await page.wait_for_timeout(3000)
            text = await page.inner_text("body")
        finally:
            await browser.close()
    return text


def _merge_duplicate_titles(matches):
    """
    Сайт іноді рендерить заголовок дати ДВІЧІ для картки без 'Sold out'
    (прихований <h3> усередині <a> + видимий <p> заголовок).
    Об'єднуємо послідовні однакові заголовки в одну "картку".
    """
    merged = []
    i = 0
    while i < len(matches):
        j = i
        while j + 1 < len(matches) and matches[j + 1].group(0) == matches[i].group(0):
            j += 1
        merged.append({
            "day": matches[i].group(1),
            "label": matches[i].group(0),
            "start": matches[i].start(),
            "end": matches[j].end(),
        })
        i = j + 1
    return merged


def check_tickets(text: str) -> tuple:
    """
    Аналізує текст сторінки, розбиваючи її на "картки" за заголовками дат
    (напр. "Thursday 23 July 2026"), а не за довільним контекстним вікном рядків.

    Для кожної картки:
      - "backward" — текст МІЖ кінцем попередньої картки та початком заголовка
        цієї картки. Індикатор 'Sold out' на сторінці рендериться САМЕ тут
        (перед заголовком власної картки).
      - "forward" — текст МІЖ кінцем заголовка цієї картки та початком
        заголовка НАСТУПНОЇ картки. Кнопка 'Buy' рендериться саме тут.

    Це коректно прив'язує кожен індикатор до "своєї" картки і уникає
    помилки попередньої версії, де сусідня картка могла потрапити
    у те саме контекстне вікно.

    DAY_FILTER_ENABLED=False → шукає будь-які доступні квитки (тест).
    DAY_FILTER_ENABLED=True  → шукає лише квитки для DAY_KEYWORDS.

    Повертає (True, [snippets]) або (False, []).
    """
    lower = text.lower()
    raw_matches = list(DATE_TITLE_RE.finditer(lower))
    cards = _merge_duplicate_titles(raw_matches)
    found_snippets = []

    for idx, card in enumerate(cards):
        if DAY_FILTER_ENABLED and card["day"] not in DAY_KEYWORDS:
            continue

        prev_end = cards[idx - 1]["end"] if idx > 0 else 0
        next_start = cards[idx + 1]["start"] if idx + 1 < len(cards) else len(lower)

        backward = lower[prev_end:card["start"]]
        forward = lower[card["end"]:next_start]

        # 'Sold out' для ЦІЄЇ картки рендериться ПЕРЕД її заголовком
        if any(kw in backward for kw in SOLD_OUT_KEYWORDS):
            continue

        # Кнопка купівлі рендериться ПІСЛЯ заголовка, до наступної картки
        if any(kw in forward for kw in AVAILABLE_KEYWORDS):
            snippet = text[card["start"]:card["end"] + len(forward)].strip()
            if snippet and snippet not in found_snippets:
                found_snippets.append(snippet)

    return bool(found_snippets), found_snippets


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


async def notify(snippets: list) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    mode_label = "ЧЕТВЕР" if DAY_FILTER_ENABLED else "будь-який день (тест)"
    snippets_text = f"\n{'─'*40}\n".join(snippets)

    subject = f"🎵 Paleo Festival — квитки з'явились! ({mode_label})"
    body = (
        f"[{now}] Виявлено квитки на Paleo Festival!\n"
        f"Режим: {mode_label}\n\n"
        f"Сторінка: {URL}\n\n"
        f"Знайдені фрагменти:\n{snippets_text}\n\n"
        "Поспішайте — квитки розходяться швидко!"
    )
    tg_message = (
        f"🎵 <b>Paleo Festival — квитки!</b>\n"
        f"<i>Режим: {mode_label}</i>\n\n"
        f"👉 <a href='{URL}'>Перейти до квитків</a>\n\n"
        f"<pre>{snippets[0][:600]}</pre>"
        + (f"\n<i>...ще {len(snippets) - 1} фрагм.</i>" if len(snippets) > 1 else "")
    )

    send_email(subject, body)
    await send_telegram(tg_message)


async def run_once() -> bool:
    """Одна ітерація перевірки. Повертає True якщо знайдено квитки."""
    mode = "лише четвер" if DAY_FILTER_ENABLED else "всі дні"
    log.info("🔍 Перевірка [%s]: %s", mode, URL)
    try:
        text = await fetch_page_text()
        found, snippets = check_tickets(text)
        if found:
            log.info("🎉 ЗНАЙДЕНО квитки! (%d фрагментів)", len(snippets))
            for idx, s in enumerate(snippets, 1):
                log.info("  Фрагмент %d:\n%s", idx, s[:300])
            await notify(snippets)
        else:
            log.info("😴 Квитків поки немає (%s).", mode)
            if NOTIFY_ALWAYS:
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                await send_telegram(
                    f"😴 <b>Paleo Monitor — перевірка о {now}</b>\n"
                    f"Квитків поки немає. Наступна перевірка через {CHECK_INTERVAL // 60} хв."
                    if CHECK_INTERVAL > 0 else
                    f"😴 Квитків поки немає."
                )
        return found
    except Exception as e:
        log.error("⚠️  Помилка під час перевірки: %s", e)
        return False


async def main():
    log.info("=" * 55)
    log.info("Paleo Monitor запущено")
    log.info(
        "Режим: %s",
        "лише четвер (DAY_FILTER_ENABLED=True)" if DAY_FILTER_ENABLED
        else "ВСІ ДНІ — тестовий (DAY_FILTER_ENABLED=False)"
    )
    log.info("Інтервал: %d сек (%d хв)", CHECK_INTERVAL, CHECK_INTERVAL // 60)
    if ACTIVE_HOURS_ENABLED:
        log.info("Активні години: %02d:00 – %02d:00", ACTIVE_HOUR_FROM, ACTIVE_HOUR_TO)
    else:
        log.info("Активні години: цілодобово (ACTIVE_HOURS_ENABLED=false)")
    log.info("Email: %s | Telegram: %s", EMAIL_ENABLED, TELEGRAM_ENABLED)
    log.info("=" * 55)

    if not DAY_FILTER_ENABLED:
        log.info("ℹ️  Тест: сповіщення надходять при квитках на БУДЬ-ЯКИЙ день.")
        log.info("ℹ️  Коли переконаєтесь — встановіть DAY_FILTER_ENABLED = True.")

    # Режим «один запуск» (для GitHub Actions / cron)
    if CHECK_INTERVAL == 0:
        if is_active_hour():
            await run_once()
        else:
            log.info(
                "🌙 Поза активними годинами (%02d:00–%02d:00), пропускаємо.",
                ACTIVE_HOUR_FROM, ACTIVE_HOUR_TO
            )
        return

    # Режим безперервного моніторингу
    while True:
        if is_active_hour():
            await run_once()
            log.info("⏳ Наступна перевірка через %d хв...", CHECK_INTERVAL // 60)
            await asyncio.sleep(CHECK_INTERVAL)
        else:
            wait_sec = seconds_until_active()
            wake_at = datetime.now().replace(
                hour=ACTIVE_HOUR_FROM if datetime.now().hour >= ACTIVE_HOUR_TO
                else ACTIVE_HOUR_FROM,
                minute=0, second=0, microsecond=0
            )
            log.info(
                "🌙 Поза активними годинами (%02d:00–%02d:00). "
                "Сплю %d хв до %02d:00...",
                ACTIVE_HOUR_FROM, ACTIVE_HOUR_TO,
                wait_sec // 60, ACTIVE_HOUR_FROM
            )
            await asyncio.sleep(wait_sec)
if __name__ == "__main__":
    asyncio.run(main())
