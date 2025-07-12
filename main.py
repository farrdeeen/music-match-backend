from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
from typing import Optional, Dict, List
from dotenv import load_dotenv
import httpx
import jwt
import os
from fastapi.middleware.cors import CORSMiddleware

# Load environment variables
load_dotenv()

# Initialize FastAPI app
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Open for development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Environment variables
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
FRONTEND_URI = os.getenv("FRONTEND_URI")
MONGODB_URI = os.getenv("MONGODB_URI")
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key")

# MongoDB setup
client = AsyncIOMotorClient(MONGODB_URI)
db = client["music_match_db"]
users_collection = db["users"]
sessions_collection = db["sessions"]
chats_collection = db["chats"]

# In-memory storage for WebSocket connections
active_connections: Dict[str, List[WebSocket]] = {}

@app.on_event("startup")
async def startup_event():
    try:
        await client.admin.command("ping")
        print("✅ MongoDB connection successful.")

        # Ensure chats collection exists
        collections = await db.list_collection_names()
        if "chats" not in collections:
            await db.create_collection("chats")
            print("✅ Created 'chats' collection.")
        else:
            print("✅ 'chats' collection already exists.")

    except Exception as e:
        print("❌ MongoDB connection error:", e)

@app.on_event("shutdown")
async def shutdown_event():
    client.close()

@app.get("/")
def root():
    return {
        "message": "🎵 Music Matcher API",
        "status": "running",
        "endpoints": ["/login", "/callback", "/me", "/current-track", "/chats", "/ws/chat", "/debug", "/test-token"]
    }

@app.get("/debug")
def debug_env():
    return {
        "CLIENT_ID": "SET" if CLIENT_ID else "NOT SET",
        "CLIENT_SECRET": "SET" if CLIENT_SECRET else "NOT SET",
        "REDIRECT_URI": REDIRECT_URI or "NOT SET",
        "FRONTEND_URI": FRONTEND_URI or "NOT SET",
        "MONGODB_URI": "SET" if MONGODB_URI else "NOT SET",
        "JWT_SECRET": "SET" if JWT_SECRET else "NOT SET"
    }

@app.websocket("/ws/chat/{sender_id}/{receiver_id}")
async def websocket_chat(websocket: WebSocket, sender_id: str, receiver_id: str):
    await websocket.accept()
    chat_key = get_chat_key(sender_id, receiver_id)

    if chat_key not in active_connections:
        active_connections[chat_key] = []
    active_connections[chat_key].append(websocket)
    print(f"🔗 New WebSocket connection: {sender_id} <-> {receiver_id}")

    try:
        while True:
            data = await websocket.receive_json()
            message = data.get("message")
            timestamp = datetime.utcnow()

            if not message:
                continue

            chat_doc = {
                "sender_id": sender_id,
                "receiver_id": receiver_id,
                "message": message,
                "timestamp": timestamp
            }

            # Store message in MongoDB
            await chats_collection.insert_one(chat_doc)

            # Broadcast message to all active connections for this chat
            for conn in active_connections[chat_key]:
                await conn.send_json({
                    "sender_id": sender_id,
                    "receiver_id": receiver_id,
                    "message": message,
                    "timestamp": timestamp.isoformat()
                })

    except WebSocketDisconnect:
        active_connections[chat_key].remove(websocket)
        print(f"❌ WebSocket disconnected: {sender_id} <-> {receiver_id}")
        if not active_connections[chat_key]:
            del active_connections[chat_key]

@app.get("/chats")
async def get_chats(sender_id: str, receiver_id: str):
    chats = await chats_collection.find({
        "$or": [
            {"sender_id": sender_id, "receiver_id": receiver_id},
            {"sender_id": receiver_id, "receiver_id": sender_id}
        ]
    }).sort("timestamp", 1).to_list(length=100)

    for chat in chats:
        chat["_id"] = str(chat["_id"])  # Convert ObjectId to string

    return {"chats": chats}

def get_chat_key(user1: str, user2: str) -> str:
    """Helper function to get a consistent chat key regardless of sender/receiver order"""
    return "_".join(sorted([user1, user2]))

# ⚡ Existing Spotify routes are unchanged
# (keep your /login, /callback, /me, /current-track, etc.)

# ✅ Helper function for user profile
async def get_spotify_user_profile(access_token: str):
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        response = await client.get("https://api.spotify.com/v1/me", headers=headers)
        if response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to fetch Spotify profile. Status: {response.status_code}, Response: {response.text}"
            )
        return response.json()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
