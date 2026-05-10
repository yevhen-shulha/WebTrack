# 🎵 Paleo Festival — Монітор квитків на четвер

Скрипт перевіряє сторінку [bourse.paleo.ch](https://bourse.paleo.ch/content?lang=en)
на наявність квитків на **четвер** і надсилає сповіщення на **email** або **Telegram**.

---

## ⚙️ Встановлення

### 1. Клонуйте / скопіюйте файли

```
paleo_monitor/
├── paleo_monitor.py
├── requirements.txt
├── .env.example   → скопіюйте в .env
└── README.md
```

### 2. Встановіть залежності

```bash
pip install -r requirements.txt
playwright install chromium
```

> Потрібен Python 3.10+

---

## 🔧 Конфігурація

Скопіюйте `.env.example` у `.env` і заповніть:

```bash
cp .env.example .env
nano .env   # або будь-який редактор
```

### Email (Gmail)

1. Увімкніть 2FA на своєму Google-акаунті.
2. Перейдіть на [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Створіть App Password для «Mail» → скопіюйте у `SMTP_PASSWORD`.

```env
EMAIL_ENABLED=true
SMTP_USER=your_email@gmail.com
SMTP_PASSWORD=xxxx xxxx xxxx xxxx
EMAIL_TO=your_email@gmail.com
```

### Telegram

1. Напишіть **@BotFather** → `/newbot` → отримайте токен.
2. Напишіть **@userinfobot** → отримайте ваш `id`.

```env
TELEGRAM_ENABLED=true
TELEGRAM_TOKEN=123456789:AAF...
TELEGRAM_CHAT_ID=123456789
```

---

## ▶️ Запуск

### Варіант A — вручну (термінал)

```bash
source .env        # або: export $(cat .env | xargs)
python paleo_monitor.py
```

### Варіант B — фоновий процес (Linux/macOS)

```bash
source .env
nohup python paleo_monitor.py > paleo.log 2>&1 &
echo $! > paleo.pid   # зберігаємо PID
```

Зупинити:
```bash
kill $(cat paleo.pid)
```

### Варіант C — cron (якщо потрібен запуск раз на годину, а не постійно)

```bash
crontab -e
```

Додайте рядок:
```
0 * * * * cd /path/to/paleo_monitor && source .env && python paleo_monitor.py --once >> paleo.log 2>&1
```

> Для режиму `--once` додайте аргумент у скрипт або використовуйте `CHECK_INTERVAL=0`.

---

## 📝 Логи

Скрипт пише логи у `paleo_monitor.log` і в консоль.

```
2025-07-03 10:00:01 [INFO] 🔍 Перевірка сторінки: https://bourse.paleo.ch/...
2025-07-03 10:00:07 [INFO] 😴 Квитків на четвер поки немає.
2025-07-03 11:00:01 [INFO] 🔍 Перевірка сторінки: https://bourse.paleo.ch/...
2025-07-03 11:00:08 [INFO] 🎉 ЗНАЙДЕНО квитки на четвер!
```

---

## 🔄 Зміна інтервалу

За замовчуванням — перевірка **раз на годину**.
Щоб змінити — встановіть `CHECK_INTERVAL` у секундах:

```env
CHECK_INTERVAL=1800   # кожні 30 хвилин
CHECK_INTERVAL=300    # кожні 5 хвилин (не зловживайте!)
```

---

## ❓ Troubleshooting

| Проблема | Рішення |
|----------|---------|
| `playwright: command not found` | `pip install playwright && playwright install chromium` |
| Email не надсилається | Перевірте App Password, увімкніть «менш безпечні додатки» або 2FA |
| Telegram 401 Unauthorized | Перевірте токен бота |
| Сторінка порожня | Сайт міг змінити структуру — перевірте `THURSDAY_KEYWORDS` |
