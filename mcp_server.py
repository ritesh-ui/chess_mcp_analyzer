from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
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

# Game context populated by the GUI after every move
game_context = {
    "fen": chess.STARTING_FEN,
    "pgn": "",
    "last_move": None,
    "turn": "white",
    "updated_at": None,
    "prev_score": 0.3 # Average white advantage at start
}

# --- Connection Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> bool:
        try:
            await websocket.accept()
            self.active_connections.append(websocket)
            print(f"[Hub] New connection: {id(websocket)}. Total: {len(self.active_connections)}")
            # Send immediate greeting and state
            await self.send_personal_message({"type": "coach_tip", "message": "Connection Established! AI Coach is ready."}, websocket)
            await self.send_personal_message(self.get_current_state(), websocket)
            return True
        except Exception as e:
            print(f"[Hub] Connection error: {e}")
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
        print(f"[Hub Broadcast] Type: {message.get('type')} | Content: {str(message)[:100]}...")
        print(f"[Hub Broadcast] Active connections: {len(self.active_connections)}")
        
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
                print(f"[Hub Broadcast] Sent to connection: {id(connection)}")
            except Exception as e:
                print(f"[Hub Broadcast] Error sending to {id(connection)}: {e}")
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

class GameSyncRequest(BaseModel):
    fen: str
    pgn: str
    last_move: str | None = None
    turn: str
    player_color: str = "white"

class CoachQuery(BaseModel):
    fen: str
    pgn: str
    question: str
    player_color: str = "white"

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

@app.post("/coach/query")
async def coach_query(request: CoachQuery):
    """
    Handles interactive questions from the user via the LLM.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {"response": "I'd love to chat more deeply, but my AI brain (OpenAI API Key) isn't plugged in right now! Please set the OPENAI_API_KEY environment variable to enable full interactive coaching."}

    client = OpenAI(api_key=api_key)
    
    # 1. Analyze with Stockfish first to provide context to the LLM
    eval_str = "Unknown"
    best_lines = []
    if os.path.exists(STOCKFISH_PATH):
        try:
            # We use a temporary board for thread safety or just use the FEN
            temp_board = chess.Board(request.fen)
            transport, engine = await chess.engine.popen_uci(STOCKFISH_PATH)
            analysis = await engine.analyse(temp_board, chess.engine.Limit(time=0.3), multipv=2)
            await engine.quit()
            
            if analysis:
                top = analysis[0]
                score = top["score"].relative.score(mate_score=10000)
                eval_val = score / 100.0 if score is not None else 0
                eval_str = f"{'+' if eval_val > 0 else ''}{eval_val:.2f}"
                for i, entry in enumerate(analysis):
                    if "pv" in entry:
                        line = temp_board.san(entry["pv"][0])
                        best_lines.append(f"Rank {i+1}: {line}")
        except Exception as e:
            print(f"Error gathering Stockfish context for LLM: {e}")

    # 2. Build the Prompt
    system_prompt = (
        "You are 'The Grandmaster Coach', a world-class chess mentor. "
        "Your goal is to explain chess concepts in a human, encouraging, and clear way. "
        "Avoid raw engine jargon or deep variations unless specifically asked. "
        "Speak like a mentor who wants the student to improve their general understanding. "
        f"You are coaching the {request.player_color} player."
    )
    
    user_context = (
        f"Game State (FEN): {request.fen}\n"
        f"Move History (PGN): {request.pgn}\n"
        f"Current Engine Evaluation: {eval_str}\n"
        f"Top Engine Suggestions: {', '.join(best_lines)}\n\n"
        f"Student Question: {request.question}"
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_context}
            ],
            temperature=0.7
        )
        return {"response": response.choices[0].message.content}
    except Exception as e:
        return {"response": f"Sorry, I encountered an error while thinking: {str(e)}"}

@app.post("/reset")
async def reset_board():
    board.reset()
    # Reset internal coaching memory
    game_context["prev_score"] = 0.3
    game_context["pgn"] = ""
    game_context["last_move"] = None
    # BROADCAST CHANGE
    asyncio.run_coroutine_threadsafe(manager.broadcast(), loop)
    return {"status": "reset", "fen": board.fen()}

@app.post("/game/sync")
async def game_sync(request: GameSyncRequest):
    """Called by the GUI after every move. Keeps server in sync with GUI game state."""
    import datetime
    global board
    
    # 1. Update context for Claude
    game_context["fen"] = request.fen
    game_context["pgn"] = request.pgn
    game_context["last_move"] = request.last_move
    game_context["turn"] = request.turn
    game_context["player_color"] = request.player_color
    game_context["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    
    # 2. SYNC GLOBAL BOARD (Fix for Stockfish tools)
    try:
        board = chess.Board(request.fen)
    except Exception as e:
        print(f"[Error] Failed to sync board: {e}")

    # Use san move for better logging if available
    move_display = request.last_move
    if request.last_move and len(request.last_move) >= 4:
        try:
            # We need the previous board to get the SAN
            # But the request.fen is already the NEW board.
            # So we just log the raw move for now, or trust the frontend sent SAN?
            # Actually, request.last_move in index.js is 'from + to' (UCI).
            pass
        except: pass

    print(f"[Game Sync] Move: {request.last_move} | Turn: {request.turn} | Player: {request.player_color} | FEN: {request.fen[:40]}...")
    
    # 3. TRIGGER AUTO-ANALYSIS (Optional/Background)
    if loop:
        asyncio.run_coroutine_threadsafe(push_auto_analysis(request.fen), loop)
        
    return {"status": "synced"}

PIECE_NAMES = {
    "P": "Pawn",
    "N": "Knight",
    "B": "Bishop",
    "R": "Rook",
    "Q": "Queen",
    "K": "King"
}

def get_piece_name(symbol: str) -> str:
    return PIECE_NAMES.get(symbol.upper(), symbol)

def get_friendly_quality_message(quality: str, is_player: bool, eval_val: float) -> str:
    import random
    
    if is_player:
        if "Blunder" in quality:
            return random.choice([
                "Ouch! That's a major slip-up.",
                "Wait, you might have missed something big there.",
                "You've given the engine a huge opening.",
                "Tough move. You've dropped a lot of ground here."
            ])
        elif "Mistake" in quality:
            return random.choice([
                "That's a bit of an error, unfortunately.",
                "You had a better option than that one.",
                "Careful! This makes your position harder to defend.",
                "A small mistake, but it might hurt you later."
            ])
        elif "Inaccuracy" in quality:
            return random.choice([
                "Not the best move, but you're still in it.",
                "A slight inaccuracy. Let's see if you can recover.",
                "There were slightly better squares for that piece.",
                "You're drifting a bit, but nothing fatal yet."
            ])
        elif "Great" in quality:
            return random.choice([
                "Brilliant! That's exactly what the position called for.",
                "Excellent find! You're playing like a pro.",
                "Wow, what a move! A very strong continuation.",
                "Fantastic! That's a high-level master move."
            ])
        elif "Good" in quality:
            if eval_val > 5: return "You're dominating! Just stay clean and win this."
            return random.choice([
                "Solid move. You're maintaining the pressure.",
                "Good choice. Keeping the position stable.",
                "A perfectly fine developing move.",
                "Consistent play. Keep it up!"
            ])
    else:
        # Engine just moved
        if "Blunder" in quality or "Mistake" in quality:
            return "The computer made a mistake! This is your chance!"
        elif "Inaccuracy" in quality:
            return "The engine played a sub-optimal move. Can you capitalize?"
        else:
            return "The computer plays a solid move. You'll need to stay sharp."
            
    return "The game is evolving. Let's see what happens next."

def get_conceptual_hint(board: chess.Board, move: chess.Move) -> str:
    piece = board.piece_at(move.from_square)
    if not piece: return "Look for a strategic improvement."
    
    p_name = get_piece_name(piece.symbol())
    is_capture = board.is_capture(move)
    is_check = board.is_check()
    
    # Regional hints
    file_idx = chess.square_file(move.to_square)
    rank_idx = chess.square_rank(move.to_square)
    
    region = "center"
    if file_idx < 3: region = "Queenside"
    elif file_idx > 4: region = "Kingside"
    
    if is_capture:
        return f"There's a strong tactical opportunity to make a <strong>Capture</strong>."
    
    if piece.piece_type == chess.PAWN:
        if region == "center":
            return "Consider solidifying or challenging the <strong>center</strong> with your pawns."
        return f"A pawn move on the <strong>{region}</strong> could improve your structure."
        
    if piece.piece_type in [chess.KNIGHT, chess.BISHOP]:
        return f"One of your <strong>{p_name}s</strong> is looking for a more active square on the {region}."
        
    if piece.piece_type == chess.ROOK:
        return f"Look for an opportunity to activate your <strong>Rook</strong>, perhaps on an open file."
        
    if piece.piece_type == chess.QUEEN:
        return "Your <strong>Queen</strong> could take a more dominant position in the game."

    return f"Think about how to better position your <strong>{p_name}</strong>."

async def push_auto_analysis(fen: str):
    """Performs a deep Stockfish analysis and pushes tactical coaching to the GUI."""
    if not os.path.exists(STOCKFISH_PATH):
        return
        
    try:
        current_board = chess.Board(fen)
        player_color = game_context.get("player_color", "white")
        
        # Determine who just moved
        side_who_moved = "white" if current_board.turn == chess.BLACK else "black"
        is_player_move = (side_who_moved == player_color)
        
        transport, engine = await chess.engine.popen_uci(STOCKFISH_PATH)
        try:
            # 1. ANALYZE CURRENT POSITION
            analysis = await engine.analyse(current_board, chess.engine.Limit(time=0.5), multipv=3)
            
            top_pv = analysis[0]
            score = top_pv["score"].relative.score(mate_score=10000)
            
            # perspective of white
            white_score = score if current_board.turn == chess.WHITE else -score
            eval_val = white_score / 100.0 if white_score is not None else 0
            prefix = "+" if eval_val > 0 else ""
            
            # 2. CALCULATE MOVE QUALITY
            move_quality = "Good"
            cp_loss = 0
            feedback = ""
            
            prev_score = game_context.get("prev_score", 0.3)
            player_score_after = -score if score is not None else 0
            cp_loss = prev_score - player_score_after
            
            game_context["prev_score"] = score if score is not None else 0
            
            color = "#198754" # Default Green
            if cp_loss > 300: 
                move_quality = "🚨 Blunder"
                color = "#dc3545" # Red
            elif cp_loss > 150: 
                move_quality = "❓ Mistake"
                color = "#fd7e14" # Orange
            elif cp_loss > 50: 
                move_quality = "⚠️ Inaccuracy"
                color = "#ffc107" # Yellow
            elif cp_loss < -50: 
                move_quality = "✨ Great Move"
                color = "#0dcaf0" # Cyan
            else: 
                move_quality = "✅ Good Move"

            # 3. CONCEPTUAL HINTS
            prediction = ""
            if is_player_move:
                # User just moved, hint at what the engine might do without giving the square
                if len(analysis) > 0:
                    best_opp_move = analysis[0]["pv"][0]
                    opp_piece = current_board.piece_at(best_opp_move.from_square)
                    opp_p_name = get_piece_name(opp_piece.symbol()) if opp_piece else "piece"
                    prediction = f"The computer is looking to activate its <strong>{opp_p_name}</strong> soon."
            else:
                # Engine just moved, give a CONCEPTUAL hint for the user
                if len(analysis) > 0:
                    best_user_move = analysis[0]["pv"][0]
                    hint = get_conceptual_hint(current_board, best_user_move)
                    prediction = f"Coach Hint: <strong>{hint}</strong>"
            
            # 4. MATERIAL/HUNG PIECES
            if "score" in top_pv and top_pv["pv"]:
                best_move = top_pv["pv"][0]
                if current_board.is_capture(best_move):
                    captured_piece = current_board.piece_at(best_move.to_square)
                    if captured_piece:
                        p_name = get_piece_name(captured_piece.symbol())
                        if is_player_move:
                            feedback = f"Heads up! Your <strong>{p_name}</strong> is under pressure."
                        else:
                            feedback = f"Look closely! You have a chance to challenge their <strong>{p_name}</strong>."

            # 5. ASSEMBLE FRIENDLY MESSAGE
            friendly_intro = get_friendly_quality_message(move_quality, is_player_move, eval_val)
            header_text = move_quality if is_player_move else f"Engine plays: [Hidden]"
            
            # Note: We keep the header_text clear for the player's move quality, 
            # but for engine moves we can hide the exact SAN if you want, 
            # though the user already sees it on the board. 
            # Let's just focus on the suggestion part.
            if not is_player_move:
                # Get the last move from game_context or board
                lm = game_context.get("last_move", "??")
                header_text = f"Engine Move Analysis"

            html_msg = f"<div style='margin-bottom:8px'><strong style='color:{color}; font-size:1.1em'>{header_text}</strong> <span style='color:#6c757d'>(Eval: {prefix}{eval_val:0.2f})</span></div>"
            html_msg += f"<div style='margin-bottom:10px; font-style:italic; font-size:1.05em; color:#212529'>\"{friendly_intro}\"</div>"
            
            if feedback:
                html_msg += f"<div style='margin-bottom:8px; color:#d63384'>💡 {feedback}</div>"
            
            html_msg += f"<div style='color:#495057'>💭 {prediction}</div>"
            
            if is_player_move and cp_loss > 50 and "pv" in top_pv:
                best_move = top_pv["pv"][0]
                better_hint = get_conceptual_hint(current_board, best_move)
                html_msg += f"<div style='margin-top:8px; color:#0d6efd'>💡 A better approach would have involved: <strong>{better_hint}</strong></div>"

            # Broadcast to GUI
            await manager.broadcast({"type": "coach_tip", "message": html_msg})
        finally:
            await engine.quit()
    except Exception as e:
        print(f"[Auto-Analysis Error] {e}")

@app.get("/game/status")
async def get_game_status():
    """Returns the current game context."""
    return game_context

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
async def get_game_context() -> str:
    """Returns the current chess game state: FEN, PGN, last move, and whose turn it is."""
    if not game_context["pgn"] and game_context["fen"] == chess.STARTING_FEN:
        return "No game in progress. The board is at the starting position."
    return (
        f"Current FEN: {game_context['fen']}\n"
        f"PGN so far: {game_context['pgn']}\n"
        f"Last Move: {game_context['last_move']}\n"
        f"Turn: {game_context['turn']}\n"
        f"Updated at: {game_context['updated_at']}"
    )

@mcp.tool()
async def push_coaching_tip(message: str) -> str:
    """Pushes a coaching tip or analysis message to the Chess AI Coach GUI in real-time via WebSocket."""
    if loop is None:
        return "Error: WebSocket Hub event loop is not initialized yet."
    
    payload = {"type": "coach_tip", "message": message}
    try:
        asyncio.run_coroutine_threadsafe(manager.broadcast(payload), loop)
        return f"Coaching tip sent to GUI: {message[:80]}..."
    except Exception as e:
        return f"Error broadcasting tip: {e}"

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
hub_thread = None

def start_http_hub():
    global loop
    # Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="error")
    server = uvicorn.Server(config)
    
    # Run the server until the loop is closed
    loop.run_until_complete(server.serve())

def ensure_hub_started():
    global hub_thread
    if hub_thread is None or not hub_thread.is_alive():
        print("[System] Starting WebSocket Hub thread...")
        hub_thread = threading.Thread(target=start_http_hub, name="HubThread", daemon=True)
        hub_thread.start()
        # Give the thread a moment to initialize the loop
        import time
        max_wait = 5
        start_time = time.time()
        while loop is None and (time.time() - start_time) < max_wait:
            time.sleep(0.1)
        if loop is None:
            print("[Warning] Hub loop failed to initialize in time.")
        else:
            print("[System] WebSocket Hub is ready.")

# Initialize Hub on import so it works with 'fastmcp dev'
ensure_hub_started()

if __name__ == "__main__":
    # Start MCP Server (Stdio)
    mcp.run()
