import asyncio
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
import requests

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ================= КОНФИГУРАЦИЯ =================
# Впиши свои данные один раз, чтобы бот знал, кого слушать:
BOT_TOKEN = "9105296066:AAELT1nDFpRYNzh7xKh8hApQ05xMqcD0HXw"
ALLOWED_USER_ID = 1149995199  # Твой цифровой ID из @userinfobot

# Настройки по умолчанию (их ты будешь менять прямо в чате)
settings = {
    "query": "",                # Пусто при старте
    "max_price": 0,             # 0 = без ограничения по цене
    "stop_words": [],           # Список стоп-слов
    "check_interval": 60,       # Секунды между проверками
    "is_active": False,
}

seen_ids = set()
monitoring_task = None

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}


# ================= СОСТОЯНИЯ ДИАЛОГА =================
class Form(StatesGroup):
    waiting_query = State()
    waiting_max_price = State()
    waiting_add_stopword = State()
    waiting_remove_stopword = State()
    waiting_interval = State()


# ================= КЛАВИАТУРА И МЕНЮ =================
def get_main_keyboard():
    toggle_text = "⏹ Остановить поиск" if settings["is_active"] else "▶️ Запустить поиск"

    buttons = [
        [
            InlineKeyboardButton(text="🔍 Название товара", callback_data="set_query"),
            InlineKeyboardButton(text="💰 Макс. цена", callback_data="set_price"),
        ],
        [
            InlineKeyboardButton(text="➕ Добавить стоп-слова", callback_data="add_stop"),
            InlineKeyboardButton(text="➖ Удалить стоп-слово", callback_data="del_stop"),
        ],
        [
            InlineKeyboardButton(text="⏱ Интервал проверки", callback_data="set_interval"),
            InlineKeyboardButton(text=toggle_text, callback_data="toggle_run"),
        ],
        [InlineKeyboardButton(text="🔄 Обновить статус", callback_data="refresh_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_status_text() -> str:
    status_str = "🟢 <b>Идет поиск</b>" if settings["is_active"] else "🔴 <b>На паузе</b>"
    query_str = f"<code>{settings['query']}</code>" if settings["query"] else "<i>не задан</i>"
    price_str = f"<b>{settings['max_price']:,} ₽</b>" if settings["max_price"] > 0 else "<i>любая</i>"
    stops_str = ", ".join(f"<code>{w}</code>" for w in settings["stop_words"]) if settings["stop_words"] else "<i>нет</i>"

    return (
        f"⚙️ <b>Панель мониторинга Авито</b>\n\n"
        f"• <b>Статус:</b> {status_str}\n"
        f"• <b>Товар:</b> {query_str}\n"
        f"• <b>Потолок цены:</b> {price_str}\n"
        f"• <b>Интервал:</b> {settings['check_interval']} сек.\n"
        f"• <b>Стоп-слова:</b> {stops_str}\n"
        f"• <b>Запомнено объявлений:</b> {len(seen_ids)}\n\n"
        f"<i>Нажимай кнопки ниже для настройки:</i>"
    )


# ================= ЛОГИКА ПАРСИНГА =================
def parse_price(price_raw: str) -> int:
    digits = "".join(filter(str.isdigit, price_raw))
    return int(digits) if digits else 0


def fetch_avito_items(query: str):
    url = f"https://www.avito.ru/rossiya?q={quote_plus(query)}&s=104"
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            logging.warning(f"Авито вернул статус: {res.status_code}")
            return []
        soup = BeautifulSoup(res.text, "html.parser")
        return soup.find_all("div", attrs={"data-marker": "item"})
    except Exception as e:
        logging.error(f"Ошибка запроса к Авито: {e}")
        return []


async def monitor_loop():
    global seen_ids
    logging.info("Цикл мониторинга запущен")

    # Инициализация первого прогона в фоновом потоке
    init_items = await asyncio.to_thread(fetch_avito_items, settings["query"])
    for item in init_items:
        i_id = item.get("data-item-id")
        if i_id:
            seen_ids.add(i_id)

    while settings["is_active"]:
        try:
            items = await asyncio.to_thread(fetch_avito_items, settings["query"])
            for item in items:
                item_id = item.get("data-item-id")
                if not item_id or item_id in seen_ids:
                    continue

                link_tag = item.find("a", attrs={"data-marker": "item-title"})
                if not link_tag:
                    continue

                item_url = "https://www.avito.ru" + link_tag.get("href", "")
                title = link_tag.get("title", "") or link_tag.text.strip()

                price_tag = item.find("meta", itemprop="price")
                if price_tag and price_tag.get("content"):
                    price = int(price_tag.get("content"))
                else:
                    price_node = item.find("strong")
                    price = parse_price(price_node.text) if price_node else 0

                desc_node = item.find("div", attrs={"class": lambda x: x and "description" in x.lower()})
                snippet = desc_node.text.strip().lower() if desc_node else ""
                full_text = f"{title.lower()} {snippet}"

                # Проверка стоп-слов
                has_stop = any(w.lower() in full_text for w in settings["stop_words"])

                # Проверка цены
                price_matches = True
                if settings["max_price"] > 0:
                    price_matches = 0 < price <= settings["max_price"]

                if price_matches and not has_stop:
                    alert_text = (
                        f"🔥 <b>Найдено новое объявление!</b>\n\n"
                        f"🏷 <b>{title}</b>\n"
                        f"💰 <b>Цена:</b> {price:,} ₽\n\n"
                        f"🔗 <a href='{item_url}'>Открыть на Авито</a>"
                    )
                    await bot.send_message(
                        chat_id=ALLOWED_USER_ID,
                        text=alert_text,
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                    )

                seen_ids.add(item_id)

        except Exception as e:
            logging.error(f"Сбой в цикле мониторинга: {e}")

        await asyncio.sleep(settings["check_interval"])


# ================= ОБРАБОТЧИКИ КОМАНД =================
@dp.message(CommandStart())
async def start_handler(msg: Message, state: FSMContext):
    if msg.from_user.id != ALLOWED_USER_ID:
        return await msg.answer("Доступ закрыт.")
    await state.clear()
    await msg.answer(format_status_text(), parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "refresh_menu")
async def cb_refresh(call: CallbackQuery):
    await call.message.edit_text(format_status_text(), parse_mode="HTML", reply_markup=get_main_keyboard())
    await call.answer()


@dp.callback_query(F.data == "toggle_run")
async def cb_toggle(call: CallbackQuery):
    global monitoring_task
    if not settings["query"]:
        return await call.answer("Сначала укажи название товара!", show_alert=True)

    settings["is_active"] = not settings["is_active"]

    if settings["is_active"]:
        monitoring_task = asyncio.create_task(monitor_loop())
    else:
        if monitoring_task and not monitoring_task.done():
            monitoring_task.cancel()

    await call.message.edit_text(format_status_text(), parse_mode="HTML", reply_markup=get_main_keyboard())
    await call.answer()


@dp.callback_query(F.data == "set_query")
async def cb_set_query(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_query)
    await call.message.answer("✏️ Напиши название товара (например: <code>PlayStation 5</code>):", parse_mode="HTML")
    await call.answer()


@dp.message(Form.waiting_query)
async def process_query(msg: Message, state: FSMContext):
    settings["query"] = msg.text.strip()
    seen_ids.clear()  # Сбрасываем кэш, чтобы при смене товара искать заново
    await state.clear()
    await msg.answer(f"✅ Товар изменен на: <b>{settings['query']}</b>", parse_mode="HTML")
    await msg.answer(format_status_text(), parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "set_price")
async def cb_set_price(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_max_price)
    await call.message.answer("💰 Отправь максимальную цену числом (например: <code>35000</code>). Напиши <code>0</code>, если цена не важна:", parse_mode="HTML")
    await call.answer()


@dp.message(Form.waiting_max_price)
async def process_price(msg: Message, state: FSMContext):
    digits = "".join(filter(str.isdigit, msg.text))
    if not digits:
        return await msg.answer("Отправь просто число без лишних знаков.")
    settings["max_price"] = int(digits)
    await state.clear()
    await msg.answer(f"✅ Потолок цены: <b>{settings['max_price']:,} ₽</b>", parse_mode="HTML")
    await msg.answer(format_status_text(), parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "add_stop")
async def cb_add_stop(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_add_stopword)
    await call.message.answer("➕ Напиши стоп-слова через запятую (например: <code>реплика, копия, на запчасти, дефект</code>):", parse_mode="HTML")
    await call.answer()


@dp.message(Form.waiting_add_stopword)
async def process_add_stop(msg: Message, state: FSMContext):
    words = [w.strip().lower() for w in msg.text.split(",") if w.strip()]
    for w in words:
        if w not in settings["stop_words"]:
            settings["stop_words"].append(w)
    await state.clear()
    await msg.answer("✅ Стоп-слова добавлены.")
    await msg.answer(format_status_text(), parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "del_stop")
async def cb_del_stop(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_remove_stopword)
    await call.message.answer("➖ Напиши стоп-слово, которое хочешь удалить из списка:")
    await call.answer()


@dp.message(Form.waiting_remove_stopword)
async def process_remove_stop(msg: Message, state: FSMContext):
    target = msg.text.strip().lower()
    if target in settings["stop_words"]:
        settings["stop_words"].remove(target)
        await msg.answer(f"✅ Слово <code>{target}</code> удалено.", parse_mode="HTML")
    else:
        await msg.answer("Этого слова нет в списке.")
    await state.clear()
    await msg.answer(format_status_text(), parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "set_interval")
async def cb_set_interval(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_interval)
    await call.message.answer("⏱ Укажи частоту проверки в секундах (не ставь меньше 30, чтобы не получить бан):")
    await call.answer()


@dp.message(Form.waiting_interval)
async def process_interval(msg: Message, state: FSMContext):
    digits = "".join(filter(str.isdigit, msg.text))
    if not digits:
        return await msg.answer("Введи число.")
    sec = int(digits)
    if sec < 20:
        return await msg.answer("Минимум 20 секунд.")
    settings["check_interval"] = sec
    await state.clear()
    await msg.answer(f"✅ Интервал: {sec} сек.")
    await msg.answer(format_status_text(),
# ================= ЗАПУСК (ДЛЯ RENDER) =================
import os
from aiohttp import web

async def dummy_server(request):
    return web.Response(text="Bot is running!")

async def main():
    app = web.Application()
    app.router.add_get("/", dummy_server)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
