# Drops

1. @BotFather → /newbot → получи BOT_TOKEN.
2. Залей папку на GitHub → Render → New Web Service (Python).
   Build: `pip install -r requirements.txt`   Start: `python bot.py`
3. Env-переменные: BOT_TOKEN, WEBAPP_URL (адрес сервиса на Render), TON_ADDRESS (твой кошелёк).
4. Render → Disks → добавь диск (например /var/data) и поставь DATA_DIR=/var/data,
   иначе база (предметы, фото, балансы) обнулится при каждом перезапуске.
5. Напиши боту /start — в мини-аппе у тебя (ID 618124780) появится шестерёнка с админ-панелью.
