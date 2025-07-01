from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
import os
from dotenv import load_dotenv
import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
import jwt
from typing import Optional

load_dotenv()

app = FastAPI()

# Environment variables
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
FRONTEND_URI = os.getenv("FRONTEND_URI")
MONGODB_URI = os.getenv("MONGODB_URI")
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key")  # Add this to your .env

# MongoDB connection
client = AsyncIOMotorClient(MONGODB_URI)
db = client["music_matcher"]  # Your database name
users_collection = db["users"]
sessions_collection = db["sessions"]

@app.on_event("startup")
async def startup_event():
    # Test the MongoDB connection
    try:
        await client.admin.command('ping')
        print("Successfully connected to MongoDB!")
    except Exception as e:
        print(f"Error connecting to MongoDB: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    client.close()

@app.get("/")
def root():
    return {"message": "Music Matcher API"}

@app.get("/login")
def login():
    scope = "user-read-playback-state user-read-currently-playing user-library-read user-top-read user-read-email"
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
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=data)
        token_data = response.json()

    if "access_token" not in token_data:
        raise HTTPException(status_code=400, detail="Failed to get access token")

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    # Get user profile from Spotify
    user_profile = await get_spotify_user_profile(access_token)
    
    # Store/update user in MongoDB
    user_data = {
        "spotify_id": user_profile["id"],
        "display_name": user_profile.get("display_name", ""),
        "email": user_profile.get("email", ""),
        "profile_image": user_profile["images"][0]["url"] if user_profile.get("images") else "",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "last_login": datetime.utcnow(),
        "created_at": datetime.utcnow()
    }
    
    # Upsert user (update if exists, create if not)
    await users_collection.update_one(
        {"spotify_id": user_profile["id"]},
        {"$set": user_data, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True
    )

    # Create a JWT token for your app
    app_token = jwt.encode(
        {"spotify_id": user_profile["id"], "exp": datetime.utcnow().timestamp() + 86400},  # 24 hours
        JWT_SECRET,
        algorithm="HS256"
    )

    return RedirectResponse(f"{FRONTEND_URI}/dashboard?token={app_token}")

@app.get("/refresh_token")
async def refresh_token(request: Request):
    refresh_token = request.query_params.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=400, detail="Refresh token not provided")
    
    token_url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=data)
        new_tokens = response.json()

    if "access_token" in new_tokens:
        # Update the access token in the database
        await users_collection.update_one(
            {"refresh_token": refresh_token},
            {"$set": {"access_token": new_tokens["access_token"]}}
        )

    return JSONResponse(content=new_tokens)

@app.get("/me")
async def get_current_user(request: Request):
    """Get current user info"""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    
    token = auth_header.split(" ")[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        spotify_id = payload["spotify_id"]
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user = await users_collection.find_one({"spotify_id": spotify_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Remove sensitive data
    user.pop("access_token", None)
    user.pop("refresh_token", None)
    user["_id"] = str(user["_id"])  # Convert ObjectId to string
    
    return user

@app.get("/current-track")
async def get_current_track(request: Request):
    """Get user's currently playing track"""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    
    token = auth_header.split(" ")[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        spotify_id = payload["spotify_id"]
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user = await users_collection.find_one({"spotify_id": spotify_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Use stored access token to get current track
    current_track = await get_spotify_current_track(user["access_token"])
    
    # Store listening session in MongoDB
    if current_track and current_track.get("is_playing"):
        session_data = {
            "user_id": spotify_id,
            "track_id": current_track["item"]["id"],
            "track_name": current_track["item"]["name"],
            "artist_name": current_track["item"]["artists"][0]["name"],
            "timestamp": datetime.utcnow(),
            "is_playing": current_track["is_playing"]
        }
        await sessions_collection.insert_one(session_data)
    
    return current_track

# Helper functions
async def get_spotify_user_profile(access_token: str):
    """Get user profile from Spotify API"""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        response = await client.get("https://api.spotify.com/v1/me", headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to get user profile")
        return response.json()

async def get_spotify_current_track(access_token: str):
    """Get currently playing track from Spotify API"""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient() as client:
        response = await client.get("https://api.spotify.com/v1/me/player/currently-playing", headers=headers)
        if response.status_code == 204:  # No content - nothing playing
            return None
        elif response.status_code != 200:
            return None
        return response.json()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)