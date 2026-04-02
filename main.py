import asyncio
import uuid
import uvicorn
import json
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, Query, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from google import genai
from google.genai import types

# --- CONFIGURATION ---
# --- CONFIGURATION ---
# This tells Python to look for a secret variable on the Railway server
# --- CONFIGURATION ---
API_KEY = os.environ.get("GEMINI_API_KEY") 
MODEL_ID = "gemini-2.5-flash" 

# Check if we are running on Railway. If yes, save to the permanent /data drive.
# If running locally on your computer, just save them in the current folder (".").
DATA_DIR = "/data" if os.environ.get("RAILWAY_ENVIRONMENT") else "."

USERS_DB_FILE = os.path.join(DATA_DIR, "users_db.json")
CHATS_DB_FILE = os.path.join(DATA_DIR, "chats_db.json")

app = FastAPI()
client = genai.Client(api_key=API_KEY)
active_chat_objects = {}

# --- BULLETPROOF DATABASE HELPERS ---
def load_json_db(filepath):
    # If file is missing or corrupted, start fresh instead of crashing
    if not os.path.exists(filepath):
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump({}, f)
        return {}
    
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if data else {}
    except Exception:
        # If it crashes reading the file, overwrite it with a clean slate
        return {}

def save_json_db(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

# Load databases into memory
users_db = load_json_db(USERS_DB_FILE)
chats_db = load_json_db(CHATS_DB_FILE)

# --- DATA MODELS ---
class UserAuth(BaseModel):
    username: str
    password: str

class ConfigUpdate(BaseModel):
    instruction: str

class RenameChat(BaseModel):
    name: str

# --- AUTHENTICATION ENDPOINTS ---
@app.post("/api/register")
async def register(user_data: UserAuth):
    username = user_data.username
    password = user_data.password
    
    if username in users_db:
        raise HTTPException(status_code=400, detail="Username already exists. Please login.")
        
    # Save to Users DB
    users_db[username] = {
        "password": password,
        "global_config": {"instruction": "Respond as a relatable, empathetic human by mirroring the user's energy and slang, offering sincere and opinionated support."}
    }
    save_json_db(USERS_DB_FILE, users_db)
    
    # Initialize their space in Chats DB
    chats_db[username] = {}
    save_json_db(CHATS_DB_FILE, chats_db)
    
    return {"status": "success", "message": "Account created successfully!"}

@app.post("/api/login")
async def login(user_data: UserAuth):
    username = user_data.username
    password = user_data.password
    
    if username not in users_db:
        raise HTTPException(status_code=404, detail="Username not found. Please Sign Up.")
        
    if users_db[username]["password"] != password:
        raise HTTPException(status_code=401, detail="Incorrect password.")
        
    return {"status": "success", "message": "Logged in successfully!"}

@app.get("/")
async def get_frontend():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

# --- CONFIGURATION ENDPOINTS ---
@app.post("/api/config")
async def update_config(config: ConfigUpdate, x_user: str = Header(...)):
    if x_user in users_db:
        users_db[x_user]["global_config"]["instruction"] = config.instruction
        save_json_db(USERS_DB_FILE, users_db)
        return {"status": "success"}
    raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/api/config")
async def get_config(x_user: str = Header(...)):
    if x_user in users_db:
        return {"instruction": users_db[x_user]["global_config"]["instruction"]}
    return {"instruction": ""}

# --- CHAT MANAGEMENT ENDPOINTS ---
@app.post("/api/chats")
async def create_new_chat(x_user: str = Header(...)):
    chat_id = str(uuid.uuid4())[:8]
    instruction = users_db.get(x_user, {}).get("global_config", {}).get("instruction", "")
    
    if x_user not in chats_db:
        chats_db[x_user] = {}
        
    chats_db[x_user][chat_id] = {
        "name": f"Chat {chat_id}",
        "instruction": instruction,
        "history": []
    }
    save_json_db(CHATS_DB_FILE, chats_db)
    return {"chat_id": chat_id, "name": chats_db[x_user][chat_id]["name"]}

@app.get("/api/chats")
async def list_chats(x_user: str = Header(...)):
    user_chats = chats_db.get(x_user, {})
    chats_list = [{"id": cid, "name": cdata["name"]} for cid, cdata in user_chats.items()]
    return {"chats": chats_list}

@app.get("/api/chats/{chat_id}/history")
async def get_chat_history(chat_id: str, x_user: str = Header(...)):
    user_chats = chats_db.get(x_user, {})
    if chat_id in user_chats:
        return {"history": user_chats[chat_id]["history"]}
    return {"history": []}

@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str, x_user: str = Header(...)):
    if x_user in chats_db and chat_id in chats_db[x_user]:
        del chats_db[x_user][chat_id]
        save_json_db(CHATS_DB_FILE, chats_db)
        if chat_id in active_chat_objects:
            del active_chat_objects[chat_id]
        return {"status": "success"}
    return {"status": "not found"}

@app.put("/api/chats/{chat_id}")
async def rename_chat(chat_id: str, payload: RenameChat, x_user: str = Header(...)):
    if x_user in chats_db and chat_id in chats_db[x_user]:
        chats_db[x_user][chat_id]["name"] = payload.name
        save_json_db(CHATS_DB_FILE, chats_db)
        return {"status": "success"}
    return {"status": "not found"}

# --- WEBSOCKET ENDPOINT ---
@app.websocket("/ws/{chat_id}")
async def websocket_endpoint(websocket: WebSocket, chat_id: str, user: str = Query(...)):
    await websocket.accept()
    
    user_chats = chats_db.get(user, {})
    if chat_id not in user_chats:
        await websocket.send_text("[ERROR] Chat session not found or unauthorized.")
        await websocket.close()
        return
        
    chat_data = user_chats[chat_id]
    
    if chat_id not in active_chat_objects:
        history_contents = []
        for msg in chat_data["history"]:
            role = "user" if msg["role"] == "user" else "model"
            history_contents.append(
                types.Content(role=role, parts=[types.Part.from_text(text=msg["text"])])
            )
            
        config = types.GenerateContentConfig(
            system_instruction=chat_data["instruction"],
            temperature=0.7, top_p=0.95
        )
        active_chat_objects[chat_id] = client.aio.chats.create(
            model=MODEL_ID, config=config, history=history_contents
        )
        
    chat = active_chat_objects[chat_id]
    
    try:
        while True:
            user_input = await websocket.receive_text()
            
            try:
                response_stream = await chat.send_message_stream(user_input)
                full_bot_response = ""
                
                async for chunk in response_stream:
                    if chunk.text:
                        full_bot_response += chunk.text
                        await websocket.send_text(chunk.text)
                
                chat_data["history"].append({"role": "user", "text": user_input})
                chat_data["history"].append({"role": "bot", "text": full_bot_response})
                save_json_db(CHATS_DB_FILE, chats_db)
                
                await websocket.send_text("<END_OF_STREAM>")

            except Exception as e:
                await websocket.send_text(f"\n[ERROR] {e}")
                await websocket.send_text("<END_OF_STREAM>")

    except WebSocketDisconnect:
        pass

if __name__ == "__main__":
    # Get the port from the cloud environment, or use 8000 locally
    port = int(os.environ.get("PORT", 8000))
    # 0.0.0.0 tells the server to accept connections from the outside internet
    uvicorn.run(app, host="0.0.0.0", port=port)
