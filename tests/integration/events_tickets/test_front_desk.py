from datetime import date, time
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event, TicketType


async def _make_event(db_session: AsyncSession, admin_user, *, plan: str = "premium") -> Event:
    event = Event(
        title="Front Desk Test Gala",
        event_date=date(2026, 12, 1),
        event_time=time(18, 0),
        location="Accra",
        created_by=admin_user["user"].user_id,
        plan=plan,
    )
    db_session.add(event)
    await db_session.flush()
    return event


async def _make_ticket_type(db_session: AsyncSession, event: Event, *, price="50.00", quantity=5) -> TicketType:
    ticket_type = TicketType(
        event_id=event.event_id,
        type_name="Regular",
        price=Decimal(price),
        quantity_available=quantity,
    )
    db_session.add(ticket_type)
    await db_session.flush()
    return ticket_type


class TestFrontDeskOpenToAllPlans:
    async def test_standard_plan_event_accepted(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user, plan="standard")
        res = await client.post(
            f"/api/v1/events/{event.event_id}/front-desk/register",
            json={"name": "Kojo Mensah", "phone": "0244000000"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text


class TestFrontDeskSell:
    async def test_door_sale_creates_checked_in_tickets_and_decrements_stock(
        self, client: AsyncClient, db_session, admin_user
    ):
        event = await _make_event(db_session, admin_user)
        ticket_type = await _make_ticket_type(db_session, event, quantity=5)

        res = await client.post(
            f"/api/v1/events/{event.event_id}/front-desk/sell",
            json={
                "ticket_type_id": str(ticket_type.ticket_type_id),
                "quantity": 2,
                "name": "Abena Owusu",
                "phone": "0201234567",
                "origin": "Kumasi",
                "payment_method": "cash",
            },
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        tickets = res.json()
        assert len(tickets) == 2
        for t in tickets:
            assert t["ticket_status"] == "used"
            assert t["check_in_channel"] == "door-sale"
            assert t["origin"] == "Kumasi"

        await db_session.refresh(ticket_type)
        assert ticket_type.quantity_available == 3

        sales_res = await client.get(
            "/api/v1/tickets/sales",
            params={"event_id": str(event.event_id)},
            headers=admin_user["headers"],
        )
        assert sales_res.status_code == 200
        assert len(sales_res.json()) == 2

    async def test_insufficient_stock_returns_409(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        ticket_type = await _make_ticket_type(db_session, event, quantity=1)

        res = await client.post(
            f"/api/v1/events/{event.event_id}/front-desk/sell",
            json={
                "ticket_type_id": str(ticket_type.ticket_type_id),
                "quantity": 2,
                "name": "Yaw Darko",
                "phone": "0201111111",
                "payment_method": "mobile_money",
            },
            headers=admin_user["headers"],
        )
        assert res.status_code == 409

    async def test_wrong_event_ticket_type_rejected(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        other_event = await _make_event(db_session, admin_user)
        other_ticket_type = await _make_ticket_type(db_session, other_event)

        res = await client.post(
            f"/api/v1/events/{event.event_id}/front-desk/sell",
            json={
                "ticket_type_id": str(other_ticket_type.ticket_type_id),
                "quantity": 1,
                "name": "Efua Asante",
                "phone": "0202222222",
                "payment_method": "cash",
            },
            headers=admin_user["headers"],
        )
        assert res.status_code == 400


class TestFrontDeskRegister:
    async def test_registration_creates_uncheckedin_ticket_with_free_type(
        self, client: AsyncClient, db_session, admin_user
    ):
        event = await _make_event(db_session, admin_user)

        res = await client.post(
            f"/api/v1/events/{event.event_id}/front-desk/register",
            json={"name": "Kofi Adjei", "phone": "0209999999", "origin": "Tema"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["ticket_status"] == "valid"
        assert body["check_in_channel"] == "registration"
        assert body["origin"] == "Tema"

        sales_res = await client.get(
            "/api/v1/tickets/sales",
            params={"event_id": str(event.event_id)},
            headers=admin_user["headers"],
        )
        assert any(s["ticket_type_name"] == "Free Registration" for s in sales_res.json())

    async def test_second_registration_reuses_free_ticket_type(
        self, client: AsyncClient, db_session, admin_user
    ):
        event = await _make_event(db_session, admin_user)

        await client.post(
            f"/api/v1/events/{event.event_id}/front-desk/register",
            json={"name": "Ama Boateng", "phone": "0203333333"},
            headers=admin_user["headers"],
        )
        await client.post(
            f"/api/v1/events/{event.event_id}/front-desk/register",
            json={"name": "Kwame Owusu", "phone": "0204444444"},
            headers=admin_user["headers"],
        )

        sales_res = await client.get(
            "/api/v1/tickets/sales",
            params={"event_id": str(event.event_id)},
            headers=admin_user["headers"],
        )
        free_type_names = [s["ticket_type_name"] for s in sales_res.json() if s["ticket_type_name"] == "Free Registration"]
        assert len(free_type_names) == 2
