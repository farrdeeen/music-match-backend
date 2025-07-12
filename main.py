from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
import httpx
import jwt
import os
from fastapi.middleware.cors import CORSMiddleware

# Load environment variables
load_dotenv()

# Initialize FastAPI app
app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for development
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
chats_collection = db["chats"]  # ✅ Ensure chats_collection is set

@app.on_event("startup")
async def startup_event():
    try:
        await client.admin.command("ping")
        print("✅ MongoDB connection successful.")

        # ✅ Ensure 'chats' collection exists
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
        "endpoints": [
            "/login", "/callback", "/me", "/current-track", 
            "/match-users", "/chats", "/test-token"
        ]
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

@app.get("/login")
def login():
    if not CLIENT_ID or not REDIRECT_URI:
        return JSONResponse(
            content={"error": "Missing CLIENT_ID or REDIRECT_URI"},
            status_code=500
        )

    scope = "user-read-playback-state"
    auth_url = (
        "https://accounts.spotify.com/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={scope}"
    )
    return RedirectResponse(auth_url)

@app.get("/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Authorization code not provided")

    token_url = "https://accounts.spotify.com/api/token"
    print("Exchanging code for token with redirect_uri:", REDIRECT_URI)

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }

    async with httpx.AsyncClient() as http_client:
        try:
            token_res = await http_client.post(token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
            print("Raw token response:", token_res.text)
            token_res.raise_for_status()
            token_data = token_res.json()
            print("Parsed token data:", token_data)
        except httpx.HTTPError as e:
            print("❌ Failed to exchange code for token:", e)
            raise HTTPException(status_code=500, detail="Spotify token exchange failed")

    if "access_token" not in token_data:
        raise HTTPException(status_code=400, detail="Failed to retrieve access token")

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")

    try:
        user_profile = await get_spotify_user_profile(access_token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch Spotify profile: {e}")

    print("✅ Fetched Spotify user profile:", user_profile)

    user_data = {
        "spotify_id": user_profile["id"],
        "display_name": user_profile.get("display_name", ""),
        "profile_image": user_profile["images"][0]["url"] if user_profile.get("images") else "",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "last_login": datetime.utcnow()
    }

    await users_collection.update_one(
        {"spotify_id": user_profile["id"]},
        {
            "$set": user_data,
            "$setOnInsert": {"created_at": datetime.utcnow()}
        },
        upsert=True
    )

    app_token = jwt.encode(
        {"spotify_id": user_profile["id"], "exp": datetime.utcnow().timestamp() + 86400},
        JWT_SECRET,
        algorithm="HS256"
    )

    redirect_url = f"{FRONTEND_URI}/dashboard?token={app_token}"
    print(f"✅ Redirecting to frontend: {redirect_url}")
    return RedirectResponse(redirect_url)

@app.get("/me")
async def get_current_user(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")

    token = auth_header.split(" ")[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        spotify_id = payload["spotify_id"]
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = await users_collection.find_one({"spotify_id": spotify_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user["_id"] = str(user["_id"])
    user.pop("access_token", None)
    user.pop("refresh_token", None)

    return user

# ✅ Chat endpoints
@app.post("/chats")
async def save_chat(request: Request):
    try:
        data = await request.json()
        print(f"📥 Incoming chat data: {data}")

        sender_id = data.get("sender_id")
        receiver_id = data.get("receiver_id")
        message = data.get("message")

        if not sender_id or not receiver_id or not message:
            raise HTTPException(status_code=400, detail="Missing sender_id, receiver_id, or message.")

        chat_doc = {
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "message": message,
            "timestamp": datetime.utcnow()
        }

        result = await chats_collection.insert_one(chat_doc)
        print(f"✅ Chat saved with ID: {result.inserted_id}")

        return {
            "success": True,
            "chat_id": str(result.inserted_id),
            "message": "Chat saved successfully"
        }

    except Exception as e:
        print(f"❌ Error saving chat: {e}")
        raise HTTPException(status_code=500, detail="Failed to save chat")

@app.get("/chats")
async def get_chats(sender_id: str, receiver_id: str):
    try:
        print(f"📡 Fetching chats between {sender_id} and {receiver_id}")

        chats_cursor = chats_collection.find({
            "$or": [
                {"sender_id": sender_id, "receiver_id": receiver_id},
                {"sender_id": receiver_id, "receiver_id": sender_id}
            ]
        }).sort("timestamp", 1)

        chats = await chats_cursor.to_list(length=100)

        for chat in chats:
            chat["_id"] = str(chat["_id"])
            chat["timestamp"] = chat["timestamp"].isoformat()

        print(f"✅ Retrieved {len(chats)} chats")
        return {
            "success": True,
            "total": len(chats),
            "chats": chats
        }

    except Exception as e:
        print(f"❌ Error fetching chats: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch chats")


# ✅ Helper: Get Spotify profile
async def get_spotify_user_profile(access_token: str):
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        response = await client.get("https://api.spotify.com/v1/me", headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Failed to fetch Spotify profile")
        return response.json()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
