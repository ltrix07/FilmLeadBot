from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.bot.gate import SubscriptionGateMiddleware, gate_router
from app.bot.routers import admin_router, menu_router, partner_router, start_router
from app.services.subscription import SubscriptionAccessService


ping_router = Router(name="ping")


@ping_router.message(Command("ping"))
async def ping(message: Message) -> None:
    await message.answer("pong")


def create_bot(token: str) -> Bot:
    return Bot(token=token)


def create_dispatcher(subscription_service: SubscriptionAccessService) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(ping_router)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(start_router)
    dispatcher.include_router(gate_router)
    dispatcher.include_router(partner_router)
    menu_router.message.middleware(SubscriptionGateMiddleware(subscription_service))
    menu_router.callback_query.middleware(SubscriptionGateMiddleware(subscription_service))
    dispatcher.include_router(menu_router)
    return dispatcher
