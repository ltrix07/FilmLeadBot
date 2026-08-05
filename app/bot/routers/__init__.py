from app.bot.routers.admin import admin_router
from app.bot.routers.menu import menu_router
from app.bot.routers.partner import partner_recruitment_router, partner_router
from app.bot.routers.start import start_router

__all__ = [
    "admin_router",
    "menu_router",
    "partner_recruitment_router",
    "partner_router",
    "start_router",
]
