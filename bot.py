"""EcoCycle X — Telegram-бот: запуск Mini App, оплата Premium в Telegram Stars, Supabase."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
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


# ── Supabase ─────────────────────────────────────────────────────────────
def _upsert_user(tg_id: int, username: str | None, full_name: str) -> None:
    sb.table("users").upsert(
        {"tg_id": tg_id, "username": username, "full_name": full_name},
        on_conflict="tg_id",
    ).execute()


def _add_premium(tg_id: int, stars: int, charge_id: str, payload: str) -> datetime:
    res = sb.table("users").select("premium_until").eq("tg_id", tg_id).execute()
    now = datetime.now(timezone.utc)
    start = now
    if res.data and res.data[0].get("premium_until"):
        cur = datetime.fromisoformat(res.data[0]["premium_until"].replace("Z", "+00:00"))
        start = max(now, cur)
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
        sb.table("listings")
        .select("type,kg,price,place,phone")
        .order("created_at", desc=True)
        .limit(n)
        .execute()
        .data
    )


# ── Клавиатуры и тексты ──────────────────────────────────────────────────
def app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="♻️ EcoCycle X ni ochish", web_app=WebAppInfo(url=MINIAPP_URL))]
        ]
    )


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


WELCOME = (
    "👋 <b>EcoCycle X</b> platformasiga xush kelibsiz!\n\n"
    "Bu yerda siz uyingizdan chiqqan chiqindi va ikkilamchi xomashyolarni "
    "(plastik, karton, metall) to'g'ridan-to'g'ri yirik zavodlarga eng yuqori narxlarda sotishingiz mumkin.\n\n"
    "Ro'yxatdan o'tish uchun quyidagi tugma orqali telefon raqamingizni yuboring:"
)

REGISTERED_TEXT = (
    "Ajoyib! Siz <b>EcoCycle X</b> platformasida Sotuvchi sifatida muvaffaqiyatli ro'yxatdan o'tdingiz 🎉\n\n"
    "Ilovani ishga tushirish uchun pastdagi menyulardan birini bosing 👇"
)


# ── Команды ──────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if not user:
        return

    await asyncio.to_thread(_upsert_user, user.id, user.username, user.full_name)
    phone = await asyncio.to_thread(_get_phone, user.id)

    if not phone:
        await message.answer(WELCOME, reply_markup=phone_keyboard(), parse_mode="HTML")
    else:
        await message.answer(REGISTERED_TEXT, reply_markup=app_keyboard(), parse_mode="HTML")


@router.message(F.contact)
async def handle_contact(message: Message) -> None:
    user = message.from_user
    contact = message.contact
    if not user or not contact:
        return

    await asyncio.to_thread(_upsert_user, user.id, user.username, user.full_name)
    await asyncio.to_thread(_save_phone, user.id, contact.phone_number)

    await message.answer("✅ Raqamingiz saqlandi!", reply_markup=ReplyKeyboardRemove())
    await message.answer(REGISTERED_TEXT, reply_markup=app_keyboard(), parse_mode="HTML")


@router.message(Command("premium"))
async def cmd_premium(message: Message) -> None:
    await message.answer_invoice(
        title="EcoCycle X Premium",
        description=f"{PREMIUM_DAYS} kunlik Premium obuna (ustuvor e'lonlar va kengaytirilgan imkoniyatlar).",
        payload=f"premium_{PREMIUM_DAYS}d",
        currency="XTR",
        prices=[LabeledPrice(label="Premium", amount=PREMIUM_STARS)],
    )


@router.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    user = message.from_user
    payment = message.successful_payment
    if not user or not payment:
        return

    until = await asyncio.to_thread(
        _add_premium,
        user.id,
        payment.total_amount,
        payment.telegram_payment_charge_id,
        payment.invoice_payload,
    )
    await message.answer(
        f"🌟 Rahmat! <b>EcoCycle X Premium</b> {until.strftime('%d.%m.%Y')} gacha faollashtirildi.",
        parse_mode="HTML",
    )


# ── Запуск (Polling + Healthcheck для хостинга) ──────────────────────────
async def health(request: web.Request) -> web.Response:
    return web.Response(text="EcoCycle X bot is running")


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="EcoCycle X", web_app=WebAppInfo(url=MINIAPP_URL))
    )

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
    
