from fastapi import APIRouter

from app.api.v1.endpoints import admin, analytics, auth, elections, events, files, organizations, payments, payouts, settings, tickets, users, voting, waitlist, whatsapp

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(elections.router)
api_router.include_router(voting.router)
api_router.include_router(events.router)
api_router.include_router(tickets.router)
api_router.include_router(analytics.router)
api_router.include_router(admin.router)
api_router.include_router(organizations.router)
api_router.include_router(whatsapp.router)
api_router.include_router(payments.router)
api_router.include_router(payouts.router)
api_router.include_router(files.router)
api_router.include_router(waitlist.router)
api_router.include_router(settings.router)
