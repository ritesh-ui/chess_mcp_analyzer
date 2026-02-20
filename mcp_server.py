from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastmcp import FastMCP
import chess
import chess.engine
import shutil
import os
import asyncio
import uvicorn
import logging
import sys
import threading
import json
from pydantic import BaseModel
from typing import List

# SILENCE LOGGING: Essential for MCP Stdio transport
logging.getLogger("uvicorn.error").setLevel(logging.ERROR)
logging.getLogger("uvicorn.access").setLevel(logging.ERROR)

# --- Configuration ---
STOCKFISH_PATH = shutil.which("stockfish") or "/opt/homebrew/bin/stockfish"

# --- Global State Hub ---
board = chess.Board()

# --- Connection Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> bool:
        try:
            await websocket.accept()
            self.active_connections.append(websocket)
            # Immediate sync on connect
            await self.send_personal_message(self.get_current_state(), websocket)
            return True
        except Exception:
            return False

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    def get_current_state(self):
        return {
            "type": "state_update",
            "fen": board.fen(),
            "turn": "white" if board.turn == chess.WHITE else "black",
            "is_game_over": board.is_game_over()
        }

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        await websocket.send_text(json.dumps(message))

    async def broadcast(self, message: dict = None):
        if message is None:
            message = self.get_current_state()
        
        # Log for debugging
        print(f"[Hub Broadcast] FEN: {message.get('fen')}")
        
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                # Connection might be stale, but we let disconnect handler handle it
                pass

manager = ConnectionManager()
app = FastAPI(title="Chess WebSocket Hub")
mcp = FastMCP("Grandmaster-Coach")

# CORS for React
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if await manager.connect(websocket):
        try:
            while True:
                # We mostly use WS for server -> client push
                # But we can listen for pings/heartbeats if needed
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)

# --- HTTP Models ---
class MoveRequest(BaseModel):
    move: str

# --- HTTP Endpoints for React UI ---
@app.get("/status")
async def get_status():
    return manager.get_current_state()

@app.post("/move")
async def make_move(request: MoveRequest):
    try:
        move = None
        try:
            move = board.parse_uci(request.move)
        except ValueError:
            move = board.parse_san(request.move)
            
        if move in board.legal_moves:
            board.push(move)
            # BRROADCAST CHANGE
            asyncio.run_coroutine_threadsafe(manager.broadcast(), loop)
            return {"status": "success", "fen": board.fen()}
        else:
            raise HTTPException(status_code=400, detail="Illegal move")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/reset")
async def reset_board():
    board.reset()
    # BROADCAST CHANGE
    asyncio.run_coroutine_threadsafe(manager.broadcast(), loop)
    return {"status": "reset", "fen": board.fen()}

# --- MCP Tools for Claude ---
@mcp.tool()
async def get_board_analysis() -> str:
    """Evaluates the current board state and explains why the last move was good or bad."""
    if not os.path.exists(STOCKFISH_PATH):
        return "Error: Stockfish not found."
    
    transport, engine = await chess.engine.popen_uci(STOCKFISH_PATH)
    try:
        analysis = await engine.analyse(board, chess.engine.Limit(time=0.5))
        score = analysis["score"].relative.score(mate_score=10000)
        feedback = "Position is balanced."
        if score > 150: feedback = "White has a significant advantage."
        elif score > 50: feedback = "White is slightly better."
        elif score < -150: feedback = "Black has a significant advantage."
        elif score < -50: feedback = "Black is slightly better."
        return f"FEN: {board.fen()}\nEvaluation: {score/100.0}\nAnalysis: {feedback}"
    finally:
        await engine.quit()

@mcp.tool()
async def play_engine_move() -> str:
    """Finds the best move for the current turn, updates the board, and returns the move."""
    if board.is_game_over():
        return "Game is already over."
        
    transport, engine = await chess.engine.popen_uci(STOCKFISH_PATH)
    try:
        result = await engine.play(board, chess.engine.Limit(time=1.0))
        move_san = board.san(result.move)
        board.push(result.move)
        
        # BROADCAST TO UI INSTANTLY
        asyncio.run_coroutine_threadsafe(manager.broadcast(), loop)
        
        return f"Engine plays: {move_san}. New FEN: {board.fen()}"
    finally:
        await engine.quit()

# --- Hybrid Orchestration ---
loop = None

def start_http_hub():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="error")
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())

if __name__ == "__main__":
    # 1. Start HTTP/WS Hub in background thread with its own loop
    threading.Thread(target=start_http_hub, daemon=True).start()
    
    # Give the thread a moment to initialize the loop
    import time
    time.sleep(1)
    
    # 2. Start MCP Server (Stdio)
    mcp.run()
