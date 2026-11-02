"""
Drops — Telegram-бот + мини-апп с дропсами, инвентарём и магазином.

Запуск:  python bot.py
Переменные окружения (см. .env.example):
  BOT_TOKEN    токен бота от @BotFather
  WEBAPP_URL   публичный https-адрес сервиса (например https://drops.onrender.com)
  TON_ADDRESS  TON-кошелёк, на который приходят пополнения
  TONCENTER_KEY (необязательно) ключ toncenter.com — убирает лимиты запросов
  DATA_DIR     папка для db.json (на Render — путь к Persistent Disk)
  PORT         порт веб-сервера (Render задаёт сам)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote

import aiohttp
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
TON_ADDRESS = os.getenv("TON_ADDRESS", "")
TONCENTER_KEY = os.getenv("TONCENTER_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "618124780"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
PORT = int(os.getenv("PORT", "8080"))
DEV = os.getenv("DEV") == "1"  # без Telegram: ?uid=123 в адресе (только для локальных тестов)

COINS_PER_TON = 1000
ROOT = Path(__file__).parent
DB_FILE = DATA_DIR / "db.json"
log = logging.getLogger("drops")

# ───────────────────────── хранилище ─────────────────────────

def default_db():
    return {
        "users": {},
        "items": [],
        "drops": [],
        "seen_tx": [],
        "next_item": 1,
        "next_drop": 1,
    }


def load_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text("utf-8"))
        except Exception:
            log.exception("db.json повреждён, создаю новую (старая — db.broken.json)")
            DB_FILE.replace(DATA_DIR / "db.broken.json")
    return default_db()


DB = load_db()


def save_db():
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(DB, ensure_ascii=False), "utf-8")
    os.replace(tmp, DB_FILE)


def infer_coins(name):
    """«20 maksenet» → 20: такие предметы сразу зачисляются на баланс."""
    m = re.match(r"^\s*(\d+)\s*(maksenet|максенет)", str(name), re.I)
    return int(m.group(1)) if m else None


def migrate():
    """Проставляет coins у старых предметов и переносит уже выпавшие максенеты из инвентаря в баланс."""
    changed = False
    for it in DB["items"]:
        if "coins" not in it:
            it["coins"] = infer_coins(it["name"])
            changed = True
    coin_items = {str(i["id"]): i["coins"] for i in DB["items"] if i.get("coins")}
    for u in DB["users"].values():
        for iid in list(u["inv"]):
            if iid in coin_items:
                u["coins"] += u["inv"].pop(iid) * coin_items[iid]
                changed = True
    if changed:
        save_db()


def get_user(uid, name=None, username=None):
    u = DB["users"].get(str(uid))
    if not u:
        u = {"id": uid, "name": name or str(uid), "username": username, "coins": 0, "inv": {}, "joined": int(time.time())}
        DB["users"][str(uid)] = u
    if name:
        u["name"], u["username"] = name, username
    return u


migrate()


# ───────────────────────── набор призов ─────────────────────────
# Картинки кладём в папку static/img/ — имя файла любое из списка ниже, расширение любое (png/jpg/webp/gif).
IMG_DIR = ROOT / "static" / "img"
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PRIZE_IMG = {  # ключ приза -> имя файла без расширения
    "mute3h": "мут",
    "maksenet5k": "5кмонет",
    "maksenet3k": "3кмонет",
    "question": "вопрос",
    "ton1": "тон",
    "floor_nft": "нфт",
    "rostishka": "ростишка",
}
ROSTISHKA_ANIM_NAMES = [f"ростишка{i}" for i in range(1, 6)]


def find_img(stem):
    """Ищет static/img/<stem>.<любое расширение> (без учёта регистра), возвращает URL или None."""
    import unicodedata
    want = unicodedata.normalize("NFC", stem).lower()
    try:
        for f in IMG_DIR.iterdir():
            if f.suffix.lower() in IMG_EXT and unicodedata.normalize("NFC", f.stem).lower() == want:
                return "/static/img/" + quote(f.name)
    except OSError:
        pass
    return None


PRIZES = [
    {"key": "mute3h",     "name": "Мут в стенке на 3 часа (кто угодно)", "emoji": "🔇", "chance": 5.0,  "price": 20000},
    {"key": "maksenet5k", "name": "5к maksenet",                          "emoji": "🪙", "chance": 10.0, "price": 10000},
    {"key": "maksenet3k", "name": "3к maksenet",                          "emoji": "🪙", "chance": 20.0, "price": 6000},
    {"key": "question",   "name": "Любой вопрос",                         "emoji": "❓", "chance": 30.0, "price": 5000},
    {"key": "ton1",       "name": "1 TON",                                "emoji": "💎", "chance": 0.5,  "price": None},
    {"key": "floor_nft",  "name": "Флор NFT",                             "emoji": "🖼", "chance": 0.1,  "price": None},
    {"key": "rostishka",  "name": "Ростишка",                             "emoji": None, "chance": 34.4, "price": None},
]


def ensure_prizes():
    """Добавляет призы из PRIZES (один раз, по ключу). Старые предметы без key остаются, но с шансом 0."""
    changed = False
    if not DB.get("prizes_v2"):
        for it in DB["items"]:
            if not it.get("key"):
                it["chance"] = 0.0
        DB["prizes_v2"] = True
        changed = True
    have = {i.get("key") for i in DB["items"]}
    for p in PRIZES:
        if p["key"] in have:
            continue
        DB["items"].append({
            "id": DB["next_item"], "key": p["key"], "name": p["name"], "photo": p.get("photo"),
            "emoji": p["emoji"], "chance": p["chance"], "price": p["price"], "coins": None,
        })
        DB["next_item"] += 1
        changed = True
    if changed:
        save_db()


ensure_prizes()


# ───────────────────────── шансы ─────────────────────────

def normalize(items):
    """Сумма шансов всегда ровно 100."""
    if not items:
        return
    s = sum(i["chance"] for i in items)
    if s <= 0:
        for i in items:
            i["chance"] = 100 / len(items)
        return
    for i in items:
        i["chance"] = round(i["chance"] / s * 100, 4)
    diff = round(100 - sum(i["chance"] for i in items), 4)
    max(items, key=lambda i: i["chance"])["chance"] = round(max(items, key=lambda i: i["chance"])["chance"] + diff, 4)


def set_chance(item, p):
    """Ставит шанс предмету, остальные пропорционально пересчитываются под остаток."""
    others = [i for i in DB["items"] if i is not item]
    if not others:
        item["chance"] = 100.0
        return
    p = max(0.01, min(99.99, float(p)))
    rest = 100 - p
    s = sum(o["chance"] for o in others)
    for o in others:
        o["chance"] = (o["chance"] / s * rest) if s > 0 else rest / len(others)
    item["chance"] = p
    normalize(DB["items"])


def roll_item():
    items = DB["items"]
    return random.choices(items, weights=[i["chance"] for i in items], k=1)[0]


# ───────────────────────── авторизация ─────────────────────────

def check_init_data(init_data: str):
    if not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 7 * 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


@web.middleware
async def auth_mw(request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    tg = check_init_data(request.headers.get("X-Init-Data", ""))
    if not tg and DEV:
        uid = int(request.query.get("uid", request.headers.get("X-Dev-Uid", ADMIN_ID)))
        tg = {"id": uid, "first_name": f"dev{uid}"}
    if not tg:
        return web.json_response({"error": "Откройте приложение из Telegram"}, status=401)
    name = (tg.get("first_name", "") + " " + tg.get("last_name", "")).strip() or str(tg["id"])
    request["uid"] = tg["id"]
    request["user"] = get_user(tg["id"], name, tg.get("username"))
    request["is_admin"] = tg["id"] == ADMIN_ID
    resp = await handler(request)
    return resp


def admin_only(fn):
    async def wrap(request):
        if not request["is_admin"]:
            return web.json_response({"error": "Нет доступа"}, status=403)
        return await fn(request)
    return wrap


def err(msg, code=400):
    return web.json_response({"error": msg}, status=code)


# ───────────────────────── публичное API ─────────────────────────

def drop_active(d, now=None):
    now = now or time.time()
    return not d.get("cancelled") and now < d["expires"] and len(d["claimed"]) < d["total"]


def item_photo(i):
    stem = PRIZE_IMG.get(i.get("key"))
    return (find_img(stem) if stem else None) or i["photo"]


def item_public(i):
    return {"id": i["id"], "name": i["name"], "photo": item_photo(i), "emoji": i.get("emoji"), "price": i["price"], "coins": i.get("coins")}


def claim_item_public(i):
    """Предмет для ответа /api/claim: у «Ростишки» добавляется анимация открытия (5 картинок в случайном порядке)."""
    d = item_public(i)
    if i.get("key") == "rostishka":
        frames = [u for u in (find_img(n) for n in ROSTISHKA_ANIM_NAMES) if u]
        random.shuffle(frames)
        d["anim"] = frames
    return d


async def api_state(request):
    u = request["user"]
    now = time.time()
    items = {i["id"]: i for i in DB["items"]}
    inv = []
    for iid, cnt in u["inv"].items():
        it = items.get(int(iid))
        if it and cnt > 0 and not it.get("coins"):
            inv.append({**item_public(it), "count": cnt})
    drops = [
        {"id": d["id"], "icon": d["icon"], "left": d["total"] - len(d["claimed"]), "seconds": int(d["expires"] - now)}
        for d in DB["drops"]
        if drop_active(d, now) and str(u["id"]) not in d["claimed"]
    ]
    return web.json_response({
        "user": {"id": u["id"], "name": u["name"], "coins": u["coins"]},
        "is_admin": request["is_admin"],
        "inventory": inv,
        "shop": [item_public(i) for i in DB["items"] if not i.get("coins")],
        "drops": drops,
        "ton": {"address": TON_ADDRESS, "comment": f"drops{u['id']}", "rate": COINS_PER_TON},
    })


async def api_claim(request):
    body = await request.json()
    u = request["user"]
    d = next((x for x in DB["drops"] if x["id"] == body.get("id")), None)
    if not d:
        return err("Дроп не найден", 404)
    if str(u["id"]) in d["claimed"]:
        return err("Вы уже забрали этот дроп")
    if not drop_active(d):
        left = d["total"] - len(d["claimed"])
        return err("Дроп уже разобрали" if left <= 0 else "Время вышло")
    it = roll_item()
    d["claimed"][str(u["id"])] = it["id"]
    if it.get("coins"):
        u["coins"] += it["coins"]  # максенеты идут сразу на баланс
    else:
        u["inv"][str(it["id"])] = u["inv"].get(str(it["id"]), 0) + 1
    save_db()
    return web.json_response({"item": claim_item_public(it), "coins": u["coins"]})


async def api_buy(request):
    body = await request.json()
    u = request["user"]
    it = next((x for x in DB["items"] if x["id"] == body.get("item_id")), None)
    if not it:
        return err("Товар не найден", 404)
    if it.get("coins"):
        return err("Этот предмет нельзя купить")
    if not it["price"]:
        return err("Цена пока не задана")
    if u["coins"] < it["price"]:
        return err("Не хватает монет")
    u["coins"] -= it["price"]
    u["inv"][str(it["id"])] = u["inv"].get(str(it["id"]), 0) + 1
    save_db()
    return web.json_response({"coins": u["coins"]})


# ───────────────────────── пополнение TON ─────────────────────────

async def scan_deposits():
    """Читает входящие переводы на TON_ADDRESS и начисляет монеты по комментарию drops<uid>."""
    if not TON_ADDRESS:
        return 0
    params = {"address": TON_ADDRESS, "limit": 40, "archival": "true"}
    if TONCENTER_KEY:
        params["api_key"] = TONCENTER_KEY
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get("https://toncenter.com/api/v2/getTransactions", params=params) as r:
                data = await r.json()
    except Exception as e:
        log.warning("toncenter: %s", e)
        return 0
    credited = 0
    for tx in data.get("result", []):
        h = tx.get("transaction_id", {}).get("hash")
        msg = tx.get("in_msg") or {}
        text = (msg.get("message") or "").strip()
        value = int(msg.get("value") or 0)
        if not h or h in DB["seen_tx"] or value <= 0 or not text.startswith("drops"):
            continue
        DB["seen_tx"].append(h)
        try:
            uid = text[5:]
            u = DB["users"].get(uid)
            coins = value * COINS_PER_TON // 1_000_000_000
            if u and coins > 0:
                u["coins"] += coins
                credited += 1
                asyncio.create_task(notify(int(uid), f"✅ Пополнение: +{coins} монет"))
        except Exception:
            log.exception("deposit parse")
    DB["seen_tx"] = DB["seen_tx"][-500:]
    if credited or data.get("result"):
        save_db()
    return credited


async def api_deposit_check(request):
    await scan_deposits()
    return web.json_response({"coins": request["user"]["coins"]})


async def deposit_loop(app):
    while True:
        await asyncio.sleep(25)
        try:
            await scan_deposits()
        except Exception:
            log.exception("deposit_loop")


# ───────────────────────── админ-API ─────────────────────────

def clean_photo(p):
    if not isinstance(p, str) or not p.startswith("data:image/") or len(p) > 600_000:
        return None
    return p


async def api_admin_data(request):
    now = time.time()
    drops = []
    for d in reversed(DB["drops"][-30:]):
        drops.append({
            "id": d["id"], "icon": d["icon"], "total": d["total"], "claimed": len(d["claimed"]),
            "seconds": max(0, int(d["expires"] - now)),
            "active": drop_active(d, now), "cancelled": bool(d.get("cancelled")),
        })
    return web.json_response({
        "items": [{**item_public(i), "chance": i["chance"]} for i in DB["items"]],
        "drops": drops,
        "users": len(DB["users"]),
    })


async def api_admin_item_add(request):
    b = await request.json()
    name = str(b.get("name", "")).strip()[:40]
    photo = clean_photo(b.get("photo"))
    if not name:
        return err("Введите название")
    if not photo:
        return err("Нужна фотка предмета")
    try:
        chance = float(b.get("chance"))
    except (TypeError, ValueError):
        return err("Введите шанс в процентах")
    if not 0 < chance < 100:
        return err("Шанс должен быть от 0 до 100")
    price = b.get("price")
    price = int(price) if price not in (None, "") and int(price) > 0 else None
    coins = b.get("coins")
    coins = int(coins) if coins not in (None, "") and int(coins) > 0 else infer_coins(name)
    it = {"id": DB["next_item"], "name": name, "photo": photo, "emoji": None, "chance": chance, "price": price, "coins": coins}
    DB["next_item"] += 1
    DB["items"].append(it)
    set_chance(it, chance)
    save_db()
    return web.json_response({"ok": True})


async def api_admin_item_update(request):
    b = await request.json()
    it = next((x for x in DB["items"] if x["id"] == b.get("id")), None)
    if not it:
        return err("Предмет не найден", 404)
    if "name" in b and str(b["name"]).strip():
        it["name"] = str(b["name"]).strip()[:40]
    if "photo" in b and clean_photo(b["photo"]):
        it["photo"] = b["photo"]
    if "price" in b:
        v = b["price"]
        it["price"] = int(v) if v not in (None, "") and int(v) > 0 else None
    if "coins" in b:
        v = b["coins"]
        it["coins"] = int(v) if v not in (None, "") and int(v) > 0 else None
    if "chance" in b and b["chance"] not in (None, ""):
        c = float(b["chance"])
        if not 0 < c < 100:
            return err("Шанс должен быть от 0 до 100")
        set_chance(it, c)
    save_db()
    return web.json_response({"ok": True})


async def api_admin_item_delete(request):
    b = await request.json()
    if len(DB["items"]) <= 1:
        return err("Нельзя удалить последний предмет")
    DB["items"] = [i for i in DB["items"] if i["id"] != b.get("id")]
    normalize(DB["items"])
    save_db()
    return web.json_response({"ok": True})


async def api_admin_drop(request):
    b = await request.json()
    try:
        total = int(b["total"])
        seconds = int(b["seconds"])
    except (KeyError, TypeError, ValueError):
        return err("Заполните активации и время")
    if not 1 <= total <= 100000:
        return err("Активаций от 1 до 100000")
    if not 5 <= seconds <= 86400:
        return err("Время от 5 секунд до 24 часов")
    d = {
        "id": DB["next_drop"], "icon": random.randint(1, 3), "total": total,
        "created": time.time(), "expires": time.time() + seconds, "claimed": {},
    }
    DB["next_drop"] += 1
    DB["drops"].append(d)
    DB["drops"] = DB["drops"][-200:]
    save_db()
    if b.get("notify"):
        asyncio.create_task(broadcast(f"🎁 Новый дроп! {total} активаций, забрать можно {fmt_time(seconds)}."))
    return web.json_response({"ok": True, "id": d["id"]})


async def api_admin_drop_cancel(request):
    b = await request.json()
    for d in DB["drops"]:
        if d["id"] == b.get("id"):
            d["cancelled"] = True
    save_db()
    return web.json_response({"ok": True})


async def api_admin_give(request):
    b = await request.json()
    try:
        uid, amount = str(int(b["user_id"])), int(b["amount"])
    except (KeyError, TypeError, ValueError):
        return err("Введите ID и сумму")
    u = DB["users"].get(uid)
    if not u:
        return err("Пользователь не запускал бота")
    u["coins"] = max(0, u["coins"] + amount)
    save_db()
    return web.json_response({"ok": True, "coins": u["coins"]})


def fmt_time(s):
    if s >= 3600:
        return f"{s // 3600} ч {s % 3600 // 60} мин".replace(" 0 мин", "")
    if s >= 60:
        return f"{s // 60} мин"
    return f"{s} сек"


# ───────────────────────── бот ─────────────────────────

bot = None
dp = None


async def notify(uid, text):
    if bot:
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass


async def broadcast(text):
    if not bot:
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Забрать", web_app=WebAppInfo(url=WEBAPP_URL))]])
    for uid in list(DB["users"]):
        try:
            await bot.send_message(int(uid), text, reply_markup=kb)
        except Exception:
            pass
        await asyncio.sleep(0.05)


def setup_bot():
    global bot, dp
    from aiogram import Bot, Dispatcher
    from aiogram.filters import CommandStart
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(m: Message):
        name = (m.from_user.first_name or "") + " " + (m.from_user.last_name or "")
        get_user(m.from_user.id, name.strip(), m.from_user.username)
        save_db()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL))]])
        await m.answer("Лови дропы, копи призы и обменивай монеты в магазине.", reply_markup=kb)


async def bot_ctx(app):
    task = None
    if BOT_TOKEN:
        setup_bot()
        from aiogram.types import MenuButtonWebApp, WebAppInfo
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL)))
        except Exception:
            log.warning("не удалось поставить menu button")
        task = asyncio.create_task(dp.start_polling(bot))
    dep = asyncio.create_task(deposit_loop(app))
    yield
    dep.cancel()
    if task:
        task.cancel()
        await bot.session.close()


# ───────────────────────── сервер ─────────────────────────

async def index(request):
    return web.FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store"})


def make_app():
    app = web.Application(middlewares=[auth_mw], client_max_size=2 * 1024 * 1024)
    app.add_routes([
        web.get("/", index),
        web.get("/api/state", api_state),
        web.post("/api/claim", api_claim),
        web.post("/api/buy", api_buy),
        web.post("/api/deposit/check", api_deposit_check),
        web.get("/api/admin/data", admin_only(api_admin_data)),
        web.post("/api/admin/item", admin_only(api_admin_item_add)),
        web.post("/api/admin/item/update", admin_only(api_admin_item_update)),
        web.post("/api/admin/item/delete", admin_only(api_admin_item_delete)),
        web.post("/api/admin/drop", admin_only(api_admin_drop)),
        web.post("/api/admin/drop/cancel", admin_only(api_admin_drop_cancel)),
        web.post("/api/admin/give", admin_only(api_admin_give)),
        web.static("/static", ROOT / "static"),
    ])
    app.cleanup_ctx.append(bot_ctx)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
