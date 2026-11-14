"""EcoCycle X — Telegram-бот: запуск Mini App, оплата Premium в Telegram Stars, Supabase."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, LabeledPrice,
    MenuButtonWebApp, Message, PreCheckoutQuery, ReplyKeyboardMarkup,
    ReplyKeyboardRemove, WebAppInfo,
)
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
MINIAPP_URL = os.environ["MINIAPP_URL"]
PREMIUM_STARS = int(os.getenv("PREMIUM_STARS", "100"))
PREMIUM_DAYS = int(os.getenv("PREMIUM_DAYS", "30"))

sb: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
router = Router()


# ---------- Supabase (синхронный клиент → в отдельном потоке) ----------
def _upsert_user(tg_id: int, username: str | None, full_name: str) -> None:
    sb.table("users").upsert(
        {"tg_id": tg_id, "username": username, "full_name": full_name}, on_conflict="tg_id"
    ).execute()


def _add_premium(tg_id: int, stars: int, charge_id: str, payload: str) -> datetime:
    res = sb.table("users").select("premium_until").eq("tg_id", tg_id).execute()
    now = datetime.now(timezone.utc)
    start = now
    if res.data and res.data[0].get("premium_until"):
        cur = datetime.fromisoformat(res.data[0]["premium_until"].replace("Z", "+00:00"))
        start = max(now, cur)  # продлеваем от конца текущей подписки
    until = start + timedelta(days=PREMIUM_DAYS)
    sb.table("payments").insert(
        {"tg_id": tg_id, "stars": stars, "charge_id": charge_id, "payload": payload}
    ).execute()
    sb.table("users").update({"premium_until": until.isoformat()}).eq("tg_id", tg_id).execute()
    return until


def _get_phone(tg_id: int) -> str | None:
    res = sb.table("users").select("phone").eq("tg_id", tg_id).execute()
    return res.data[0].get("phone") if res.data else None


def _save_phone(tg_id: int, phone: str) -> None:
    sb.table("users").update({"phone": phone}).eq("tg_id", tg_id).execute()


def _premium_until(tg_id: int) -> datetime | None:
    res = sb.table("users").select("premium_until").eq("tg_id", tg_id).execute()
    if res.data and res.data[0].get("premium_until"):
        return datetime.fromisoformat(res.data[0]["premium_until"].replace("Z", "+00:00"))
    return None


def _last_listings(n: int = 5) -> list[dict]:
    return (
        sb.table("listings").select("type,kg,price,place,phone")
        .order("created_at", desc=True).limit(n).execute().data
    )


# ---------- Команды ----------
def app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="♻️ EcoCycle X ni ochish", web_app=WebAppInfo(url=MINIAPP_URL))
    ]])


WELCOME = (
    "👋 EcoCycle X platformasiga xush kelibsiz!\n\n"
    "Uyingizdagi chiqindilarni soting yoki zavod sifatida xarid qiling.\n\n"
    "Ro'yxatdan o'tish uchun bir marta quyidagi tugma orqali "
    "telefon raqamingizni yuboring:"
)


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def send_premium_invoice(message: Message) -> None:
    await message.answer_invoice(
        title="EcoCycle X Premium",
        description=f"{PREMIUM_DAYS} kun: qabul qiluvchilar uchun premium imkoniyatlar.",
        payload=f"premium_{PREMIUM_DAYS}d",
        currency="XTR",                 # Telegram Stars
        prices=[LabeledPrice(label=f"Premium {PREMIUM_DAYS} kun", amount=PREMIUM_STARS)],
        provider_token="",              # для Stars токен провайдера пустой
    )


@router.message(CommandStart())
async def start(message: Message, command: CommandObject) -> None:
    u = message.from_user
    await asyncio.to_thread(_upsert_user, u.id, u.username, u.full_name)
    if command.args == "premium":
        await send_premium_invoice(message)
        return
    phone = await asyncio.to_thread(_get_phone, u.id)
    if phone:
        await message.answer(
            "👋 EcoCycle X platformasiga xush kelibsiz!\n\n"
            "Uyingizdagi chiqindilarni soting yoki zavod sifatida xarid qiling.",
            reply_markup=app_keyboard(),
        )
    else:
        await message.answer(WELCOME, reply_markup=phone_keyboard())


@router.message(F.contact)
async def got_contact(message: Message) -> None:
    c = message.contact
    # принимаем только СВОЙ номер, а не пересланный чужой контакт
    if c.user_id != message.from_user.id:
        await message.answer("Iltimos, oʻzingizning raqamingizni tugma orqali yuboring.",
                             reply_markup=phone_keyboard())
        return
    phone = c.phone_number if c.phone_number.startswith("+") else "+" + c.phone_number
    await asyncio.to_thread(_save_phone, message.from_user.id, phone)
    await message.answer("✅ Rahmat! Roʻyxatdan oʻtdingiz.", reply_markup=ReplyKeyboardRemove())
    await message.answer("Ilovani ochish uchun tugmani bosing:", reply_markup=app_keyboard())


@router.message(Command("premium"))
async def premium(message: Message) -> None:
    until = await asyncio.to_thread(_premium_until, message.from_user.id)
    if until and until > datetime.now(timezone.utc):
        await message.answer(f"⭐ Premium faol: {until:%d.%m.%Y} gacha. Uzaytirish uchun toʻlang:")
    await send_premium_invoice(message)


@router.message(Command("list"))
async def last(message: Message) -> None:
    rows = await asyncio.to_thread(_last_listings)
    if not rows:
        await message.answer("Hozircha eʼlonlar yoʻq.")
        return
    names = {"pet": "Plastik", "bag": "Paketlar", "glass": "Shisha",
             "paper": "Qogʻoz", "cap": "Qopqoqlar", "metal": "Metall"}
    lines = [
        f"• {names.get(r['type'], r['type'])}, {r['kg']:g} kg — "
        f"{'bepul' if not r['price'] else str(int(r['price'])) + ' soʻm/kg'} · {r['place']}"
        for r in rows
    ]
    await message.answer("Oxirgi eʼlonlar:\n" + "\n".join(lines), reply_markup=app_keyboard())


@router.message(Command("paysupport"))  # Telegram требует эту команду у ботов с платежами
async def paysupport(message: Message) -> None:
    await message.answer(
        "Toʻlov boʻyicha savollar uchun @your_support_username ga yozing "
        "va toʻlov sanasini koʻrsating."
    )


# ---------- Оплата Stars ----------
@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    await q.answer(ok=q.invoice_payload.startswith("premium_"))


@router.message(F.successful_payment)
async def paid(message: Message) -> None:
    p = message.successful_payment
    until = await asyncio.to_thread(
        _add_premium, message.from_user.id, p.total_amount,
        p.telegram_payment_charge_id, p.invoice_payload,
    )
    await message.answer(f"✅ Rahmat! Premium {until:%d.%m.%Y} gacha faol.")


async def run_health_server() -> None:
    """Мини-сервер для Render Web Service: ему нужен открытый порт и ответ на пинг."""
    async def ok(_request):
        return web.Response(text="EcoCycle X bot is running")

    app = web.Application()
    app.router.add_get("/", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()


async def main() -> None:
    await run_health_server()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_chat_menu_button(  # кнопка «EcoCycle X mini ilovasi» рядом с полем ввода
        menu_button=MenuButtonWebApp(text="EcoCycle X mini ilovasi 🌿",
                                     web_app=WebAppInfo(url=MINIAPP_URL))
    )
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
