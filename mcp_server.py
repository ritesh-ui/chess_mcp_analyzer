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

# Load .env file automatically (so OPENAI_API_KEY persists across server restarts)
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("[Config] .env loaded successfully.")
except ImportError:
    pass  # python-dotenv not installed; env vars must be set manually

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
    "prev_score": 0.3, # Average white advantage at start
    "hot_squares": [], # List of {square: 'a1', type: 'gold'|'red'}
    "active_challenge": None, # {target_square: 'e4', message: '...'}
    "analysis_history": [], # List of {fen: str, move: str, cp_loss: float, turn: str}
    "last_critical_tip_time": 0, # Timestamp of last blunder/mistake alert
    "last_move_quality": "Good", # Track quality of the very last move
    "analyze_cpu": False # DEFAULT: DISABLED
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
    analyze_cpu: bool = False

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

@app.post("/game/review")
async def game_review():
    """
    Summarizes the game and identifies the biggest blunder for the 'Memory Session'.
    """
    history = game_context.get("analysis_history", [])
    if not history:
        return {"lessons": ["No moves recorded for review."], "blunder": None}

    # 1. Identify Biggest Blunder
    # Filter for player moves (assuming we're coaching the player_color)
    player_color = game_context.get("player_color", "white")
    player_history = [h for h in history if h["turn"] == player_color]
    
    biggest_blunder = None
    if player_history:
        # Sort by CP loss descending
        sorted_history = sorted(player_history, key=lambda x: x["cp_loss"], reverse=True)
        # Only count if loss > 1.0 (100cp)
        if sorted_history[0]["cp_loss"] > 1.0:
            biggest_blunder = sorted_history[0]

    # 2. Generate Lessons using LLM
    api_key = os.getenv("OPENAI_API_KEY")
    summary = "The game was complex. Focus on center control and piece activity."
    
    if api_key:
        client = OpenAI(api_key=api_key)
        game_log = "\n".join([f"Move: {h['move']} | Turn: {h['turn']} | CP Loss: {h['cp_loss']:.2f}" for h in history[-20:]])
        
        system_prompt = "You are 'The Grandmaster Coach'. Summarize the key strategic takeaway from this game session in exactly 3 short bullet points. Focus on general improvement advice."
        user_prompt = f"Game History (Last 20 moves):\n{game_log}\n\nSummarize the Top 3 Lessons:"

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7
            )
            summary = response.choices[0].message.content
        except: pass

    # 3. Get best move for the blunder drill
    drill_data = None
    if biggest_blunder and os.path.exists(STOCKFISH_PATH):
        try:
            temp_board = chess.Board(biggest_blunder["fen"])
            transport, engine = await chess.engine.popen_uci(STOCKFISH_PATH)
            analysis = await engine.analyse(temp_board, chess.engine.Limit(depth=18))
            await engine.quit()
            
            if analysis:
                best_move = temp_board.san(analysis[0]["pv"][0])
                drill_data = {
                    "fen": biggest_blunder["fen"],
                    "played_move": biggest_blunder["move"],
                    "best_move": best_move,
                    "target_square": chess.square_name(analysis[0]["pv"][0].from_square)
                }
        except: pass

    return {
        "lessons": summary.split("\n") if "\n" in summary else [summary],
        "blunder": drill_data
    }

@app.post("/reset")
async def reset_board():
    global board
    board.reset()
    # Reset internal coaching memory
    game_context["prev_score"] = 0.3
    game_context["pgn"] = ""
    game_context["last_move"] = None
    game_context["analysis_history"] = []
    game_context["active_challenge"] = None
    game_context["hot_squares"] = []
    game_context["last_move_quality"] = "Good"
    game_context["last_critical_tip_time"] = 0
    
    # BROADCAST CHANGE to clear highlights on frontend
    if loop:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({
                "type": "coach_tip",
                "message": "<div class='text-center py-2 opacity-50 small'>Board re-initialized. Ready for new game.</div>",
                "hot_squares": [],
                "challenge": None
            }), 
            loop
        )
    
    print("[System] Full backend reset completed.")
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
    game_context["analyze_cpu"] = request.analyze_cpu
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
                "Critical error. You've dropped significant ground.",
                "Major oversight. The position is now heavily compromised.",
                "Blunder detected. You've given away a huge advantage."
            ])
        elif "Mistake" in quality:
            return random.choice([
                "Sub-optimal move. You had a stronger continuation.",
                "A clear error that makes defense much harder.",
                "Mistake. This gives the opponent a clear opening."
            ])
        elif "Inaccuracy" in quality:
            return random.choice([
                "Slight inaccuracy. There were better squares available.",
                "Not the best. You're drifting from the optimal line.",
                "Subtle slip-up. Keep an eye on the initiative."
            ])
        elif "Great" in quality:
            return random.choice([
                "Excellent! A precise and powerful continuation.",
                "Brilliant find. You've secured a strong advantage.",
                "Master-level precision. Exactly what the board needed."
            ])
        elif "Good" in quality:
            if eval_val > 5: return "Dominating position. Stay clinical."
            return random.choice([
                "Solid choice. Maintaining the pressure.",
                "Consistent play. Keeping the position stable.",
                "Fine development. Proceed with your plan."
            ])
    else:
        # Engine just moved
        if "Blunder" in quality or "Mistake" in quality:
            return "Engine error! Seize the opportunity immediately."
        elif "Inaccuracy" in quality:
            return "Sub-optimal engine move. Can you capitalize?"
        else:
            return "Solid engine response. Stay sharp."
            
    return "Game evolving. Watch the center."

def get_conceptual_hint(board: chess.Board, move: chess.Move) -> str:
    piece = board.piece_at(move.from_square)
    if not piece: return "Look for tactical improvements."
    
    p_name = get_piece_name(piece.symbol())
    is_capture = board.is_capture(move)
    is_check = board.is_check()
    
    file_idx = chess.square_file(move.to_square)
    region = "center"
    if file_idx < 3: region = "Queenside"
    elif file_idx > 4: region = "Kingside"
    
    if is_capture: return "Tactical Opportunity: Capture available."
    
    if piece.piece_type == chess.PAWN:
        if region == "center": return "Goal: Contest the center with pawns."
        return f"Structure: Improve {region} pawn chain."
        
    if piece.piece_type in [chess.KNIGHT, chess.BISHOP]:
        return f"Activation: Move {p_name} to {region}."
        
    if piece.piece_type == chess.ROOK:
        return "Activation: Place Rook on open file."
        
    if piece.piece_type == chess.QUEEN:
        return "Dominance: Centralize the Queen."

    return f"Positioning: Improve {p_name} placement."

async def push_auto_analysis(fen: str):
    """
    Cost-Optimized Analysis Pipeline:
    Stage 1: Engine classifies the move using eval delta and material loss.
    Stage 2: Cost Gate — only Mistake/Blunder triggers an LLM call.
    Stage 3: Focused LLM prompt (<90 words) for genuine coaching on errors.
    """
    if not os.path.exists(STOCKFISH_PATH):
        return

    try:
        current_board = chess.Board(fen)
        player_color = game_context.get("player_color", "white")
        side_who_moved = "white" if current_board.turn == chess.BLACK else "black"
        is_player_move = (side_who_moved == player_color)

        # --- CPU Analysis Control ---
        if not is_player_move and not game_context.get("analyze_cpu", False):
            print(f"[Pacing] Skipping CPU analysis for {side_who_moved} (Analyze CPU is OFF)")
            return

        transport, engine = await chess.engine.popen_uci(STOCKFISH_PATH)
        try:
            # ─────────────────────────────────────────────────────────────
            # STAGE 1: ENGINE CLASSIFICATION
            # ─────────────────────────────────────────────────────────────

            # Analyse current (post-move) position
            analysis_after = await engine.analyse(current_board, chess.engine.Limit(time=0.5), multipv=1)
            top_pv = analysis_after[0]

            score_after_raw = top_pv["score"].relative.score(mate_score=10000)
            # Convert to centipawns from the perspective of the player who just moved
            # (relative score is from the perspective of the side TO MOVE)
            # After player moved, it's opponent's turn → relative is opponent's advantage
            # So player_delta = -score_after_raw vs prev_score
            score_after_player_pov = -(score_after_raw if score_after_raw is not None else 0)

            prev_score = game_context.get("prev_score", 30)  # stored in centipawns
            delta = prev_score - score_after_player_pov
            game_context["prev_score"] = score_after_player_pov

            # Detect material lost (was the move a bad capture or hanging piece eaten?)
            material_lost = None
            last_move_uci = game_context.get("last_move", "")
            if last_move_uci and len(last_move_uci) >= 4:
                try:
                    # Reconstruct the board BEFORE the move to detect capture context
                    pre_board = current_board.copy()
                    pre_board.push(chess.Move.from_uci(last_move_uci))
                    # That would be board after again – check if opponent best move captures back
                    if top_pv.get("pv"):
                        resp = top_pv["pv"][0]
                        if current_board.is_capture(resp):
                            captured = current_board.piece_at(resp.to_square)
                            if captured:
                                material_lost = get_piece_name(captured.symbol())
                except Exception:
                    pass

            # Classify
            if delta > 250 or (material_lost and delta > 100):
                classification = "Blunder"
                color = "#dc3545"
                badge = "🚨"
            elif delta > 100:
                classification = "Mistake"
                color = "#fd7e14"
                badge = "❓"
            elif delta > 30:
                classification = "Inaccuracy"
                color = "#ffc107"
                badge = "⚠️"
            elif delta < -50:
                classification = "Great Move"
                color = "#0dcaf0"
                badge = "✨"
            else:
                classification = "Good"
                color = "#198754"
                badge = "✅"

            game_context["last_move_quality"] = classification

            # Record in history for post-game review
            game_context["analysis_history"].append({
                "fen": fen,
                "move": game_context.get("last_move", "??"),
                "cp_loss": delta,
                "turn": side_who_moved
            })

            # Hot squares: best engine reply target
            hot_squares = []
            active_challenge = None
            if top_pv.get("pv"):
                best_move = top_pv["pv"][0]
                hot_squares.append({"square": chess.square_name(best_move.to_square), "type": "gold"})
                if current_board.is_capture(best_move):
                    hot_squares.append({"square": chess.square_name(best_move.to_square), "type": "red"})

            game_context["hot_squares"] = hot_squares
            game_context["active_challenge"] = active_challenge

            # ─────────────────────────────────────────────────────────────
            # PACING: Suppress routine CPU tips if a critical player tip was recent
            # ─────────────────────────────────────────────────────────────
            import time as _time
            current_time = _time.time()
            is_critical = classification in ("Blunder", "Mistake")
            if is_critical:
                game_context["last_critical_tip_time"] = current_time
            if not is_player_move and not is_critical:
                time_since_tip = current_time - game_context.get("last_critical_tip_time", 0)
                if time_since_tip < 5.0:
                    print(f"[Pacing] Suppressing routine CPU tip ({time_since_tip:.1f}s ago)")
                    return

            # ─────────────────────────────────────────────────────────────
            # STAGE 2: COST GATE
            # ─────────────────────────────────────────────────────────────
            if not is_player_move:
                # CPU moves: always use fast engine message, never LLM
                if classification in ("Blunder", "Mistake"):
                    cpu_msg = "<strong style='color:#0dcaf0'>Engine Error!</strong> Seize the opportunity immediately."
                elif classification == "Inaccuracy":
                    cpu_msg = "<strong style='color:#ffc107'>Sub-optimal CPU move.</strong> Can you capitalize?"
                elif classification == "Great Move":
                    cpu_msg = "<strong style='color:#0dcaf0'>Strong engine move.</strong> Stay alert and look for counterplay."
                else:
                    cpu_msg = "<strong style='color:#6c757d'>Solid engine response.</strong> Stay sharp."

                html_msg = f"<div style='margin-bottom:6px'><strong style='color:{color}'>{badge} CPU: {classification}</strong></div>"
                html_msg += f"<div style='color:#cbd5e1; font-size:0.95em'>{cpu_msg}</div>"
                await manager.broadcast({"type": "coach_tip", "message": html_msg, "hot_squares": hot_squares, "challenge": None})
                return

            # Player move — gate on classification
            if classification not in ("Mistake", "Blunder"):
                # ── NO LLM CALL — Simple engine message ──
                if classification == "Great Move":
                    simple_msg = "Excellent! Strong move — you've improved your position significantly. 💪"
                elif classification == "Inaccuracy":
                    simple_msg = "Slight inaccuracy. There was a marginally stronger option, but this is playable."
                else:  # Good
                    simple_msg = "Good move. Keep building your position with purpose."

                # Best hint (no LLM)
                best_hint = ""
                if top_pv.get("pv"):
                    best_opp = top_pv["pv"][0]
                    opp_piece = current_board.piece_at(best_opp.from_square)
                    opp_name = get_piece_name(opp_piece.symbol()) if opp_piece else "piece"
                    best_hint = f"<div style='margin-top:6px; color:#94a3b8; font-size:0.9em'>👀 Engine may activate its <strong>{opp_name}</strong> next.</div>"

                html_msg = f"<div style='margin-bottom:6px'><strong style='color:{color}'>{badge} {classification}</strong></div>"
                html_msg += f"<div style='color:#f1f5f9; margin-bottom:4px'>{simple_msg}</div>"
                html_msg += best_hint
                await manager.broadcast({"type": "coach_tip", "message": html_msg, "hot_squares": hot_squares, "challenge": active_challenge})
                return

            # ─────────────────────────────────────────────────────────────
             # ─────────────────────────────────────────────────────────────
             # STAGE 3: LLM COACHING (Only for Mistake / Blunder)
             # ─────────────────────────────────────────────────────────────
            api_key = os.getenv("OPENAI_API_KEY")

            # While we await LLM, immediately show a holding message
            holding_html = f"<div style='margin-bottom:6px'><strong style='color:{color}'>{badge} {classification}</strong></div>"
            holding_html += f"<div style='color:#94a3b8; font-size:0.9em'>🤔 Analyzing your move...</div>"
            await manager.broadcast({"type": "coach_tip", "message": holding_html, "hot_squares": hot_squares, "challenge": None})

            llm_response = None
            if api_key:
                # ── Validate best move legality BEFORE sending to LLM ──
                best_move_obj = None
                best_move_san = None
                key_issue = "positional error"

                if top_pv.get("pv"):
                    candidate = top_pv["pv"][0]
                    # Verify the move is actually legal in the current position
                    if candidate in current_board.legal_moves:
                        best_move_obj = candidate
                        try:
                            best_move_san = current_board.san(candidate)
                        except Exception as e:
                            print(f"[LLM Coach] SAN conversion failed: {e}")
                            best_move_san = candidate.uci()  # fallback to UCI notation
                    else:
                        print(f"[LLM Coach] WARNING: Engine move {candidate} is not legal in position {fen}. Skipping LLM call.")

                if best_move_san is None:
                    # Cannot guarantee a legal move — fall through to fallback below
                    print("[LLM Coach] No legal best move available. Skipping LLM call.")
                else:
                    if material_lost:
                        key_issue = f"Hanging piece ({material_lost})"
                    elif is_critical:
                        key_issue = "Tactical oversight"

                    # Determine side-to-move AFTER the played move (opponent's turn)
                    side_to_move_after = "White" if current_board.turn == chess.WHITE else "Black"
                    human_player_label = "White" if player_color == "white" else "Black"
                    side_label = "White" if side_who_moved == "white" else "Black"
                    played_move = game_context.get("last_move", "??")

                    # Determine material consequence for the payload
                    material_consequence = material_lost if material_lost else "None"

                    system_prompt = (
                        "You are a chess improvement coach.\n\n"
                        "You will receive structured factual information from a chess engine.\n"
                        "These facts are correct and must not be questioned.\n\n"
                        "IMPORTANT:\n"
                        "- Always coach from the HUMAN PLAYER'S perspective.\n"
                        "- The human player side is explicitly provided.\n"
                        "- The side to move after the played move is explicitly provided.\n"
                        "- The engine best move is already legal and verified.\n"
                        "- You must use ONLY the provided best engine move.\n"
                        "- Do NOT invent any move.\n"
                        "- Do NOT calculate new moves.\n"
                        "- Do NOT analyze the position independently.\n"
                        "- Do NOT mention evaluation numbers.\n"
                        "- Do NOT switch perspective.\n\n"
                        "If the best engine move belongs to the opponent:\n"
                        "Explain what threat that move creates and why the player's move allowed it.\n\n"
                        "If the best engine move belongs to the human player:\n"
                        "Explain why that move would have been stronger.\n\n"
                        "Keep explanation under 60 words.\n"
                        "Focus on one key idea only.\n"
                        "Suggest at most one move (the provided engine move).\n\n"
                        "End with one practical tip starting with:\n"
                        "\"Tip: \"\n\n"
                        "Start the response with the move classification on its own line.\n"
                        "Output plain text only."
                    )

                    user_payload = (
                        f"Human player side: {human_player_label}\n"
                        f"Side to move after played move: {side_to_move_after}\n"
                        f"Move classification: {classification}\n"
                        f"Move played: {played_move}\n"
                        f"Best engine move (legal and verified): {best_move_san}\n"
                        f"Material consequence: {material_consequence}\n"
                        f"Key issue detected: {key_issue}"
                    )

                    try:
                        client = OpenAI(api_key=api_key)
                        response = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: client.chat.completions.create(
                                model="gpt-4o-mini",
                                messages=[
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_payload}
                                ],
                                max_tokens=180,
                                temperature=0.3  # Lower temp = more deterministic, less hallucination
                            )
                        )
                        llm_response = response.choices[0].message.content.strip()
                        print(f"[LLM Coach] {classification} — called gpt-4o-mini. Best move sent: {best_move_san}. Tokens: {response.usage.total_tokens}")
                    except Exception as e:
                        print(f"[LLM Coach] Error: {e}")


            # ── Assemble final message ──
            html_msg = f"<div style='margin-bottom:8px'><strong style='color:{color}; font-size:1.05em'>{badge} {classification}</strong></div>"

            if llm_response:
                # Convert newlines to HTML, highlight the Tip line
                lines = llm_response.split("\n")
                formatted_lines = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("Tip:"):
                        formatted_lines.append(
                            f"<div style='margin-top:10px; padding:8px 10px; background:rgba(129,140,248,0.1); "
                            f"border-left:3px solid #818cf8; border-radius:4px; color:#a5b4fc; font-size:0.9em'>"
                            f"💡 {line}</div>"
                        )
                    else:
                        formatted_lines.append(f"<div style='margin-bottom:4px; color:#f1f5f9; font-size:0.95em'>{line}</div>")
                html_msg += "\n".join(formatted_lines)
            else:
                # Fallback if no API key or LLM failed
                fallback = "This was a significant error. Review the position carefully and look for the most forcing continuation."
                html_msg += f"<div style='color:#f1f5f9'>{fallback}</div>"
                if top_pv.get("pv"):
                    try:
                        best_san = current_board.san(top_pv["pv"][0])
                        html_msg += f"<div style='margin-top:6px; color:#818cf8; font-size:0.9em'>Better: <strong>{best_san}</strong></div>"
                    except Exception:
                        pass

            if hot_squares:
                html_msg += f"<div style='margin-top:8px; color:#94a3b8; font-size:0.85em'>🎯 Highlighted square shows the key opportunity.</div>"

            await manager.broadcast({
                "type": "coach_tip",
                "message": html_msg,
                "hot_squares": hot_squares,
                "challenge": active_challenge
            })

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
        # PACING: Wait if the player just blundered so they can read the tip
        last_quality = game_context.get("last_move_quality", "Good")
        if "Blunder" in last_quality or "Mistake" in last_quality:
            print(f"[Pacing] Delaying engine response for user reflection (Quality: {last_quality})")
            await asyncio.sleep(2.0)

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
