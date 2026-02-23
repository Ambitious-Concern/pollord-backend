import logging
from typing import Dict, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, channel: str):
        await websocket.accept()
        if channel not in self.active_connections:
            self.active_connections[channel] = []
        self.active_connections[channel].append(websocket)
        logger.info(f"WebSocket connected to channel: {channel}")

    def disconnect(self, websocket: WebSocket, channel: str):
        if channel in self.active_connections:
            self.active_connections[channel].remove(websocket)
            if not self.active_connections[channel]:
                del self.active_connections[channel]
        logger.info(f"WebSocket disconnected from channel: {channel}")

    async def broadcast(self, channel: str, message: dict):
        if channel in self.active_connections:
            disconnected = []
            for connection in self.active_connections[channel]:
                try:
                    await connection.send_json(message)
                except Exception:
                    disconnected.append(connection)
            for conn in disconnected:
                self.disconnect(conn, channel)


manager = ConnectionManager()


@router.websocket("/ws/elections/{election_id}/live")
async def election_live(websocket: WebSocket, election_id: str):
    channel = f"election_{election_id}"
    await manager.connect(websocket, channel)
    try:
        while True:
            data = await websocket.receive_text()
            # Keep-alive; real updates are broadcast from services
    except WebSocketDisconnect:
        manager.disconnect(websocket, channel)


@router.websocket("/ws/events/{event_id}/scanning")
async def event_scanning(websocket: WebSocket, event_id: str):
    channel = f"event_scanning_{event_id}"
    await manager.connect(websocket, channel)
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, channel)


@router.websocket("/ws/notifications")
async def notifications(websocket: WebSocket):
    # In production, authenticate via query param token
    channel = "notifications"
    await manager.connect(websocket, channel)
    try:
        while True:
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, channel)
