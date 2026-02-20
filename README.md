# Grandmaster-MCP: Chess Intelligence Layer

Grandmaster-MCP is a powerful chess coaching system that connects a local Stockfish engine to Claude Desktop (or any other MCP client). It allows for real-time game analysis, tactical motif detection, and engine-backed move suggestions.

## Features

- **Standardized Tools**:
  - `analyze_pgn`: Full move-by-move analysis of a PGN string.
  - `get_hint`: Context-aware tactical hints.
  - `get_best_move`: Direct best-move lookup for any FEN position.
  - `play_engine_move`: Play against Stockfish with state tracking.
- **Intelligence Layer**:
  - Blunder, Mistake, and Inaccuracy classification based on Centipawn loss.
  - Tactical motif detection (Check, Capture, Promotion, Back-rank threats).
- **LLM Optimization**:
  - Includes a custom "Grandmaster Coach" prompt template to ensure the LLM uses engine data before guessing.

## Prerequisites

- [uv](https://github.com/astral-sh/uv) (Python package manager)
- [Stockfish](https://stockfishchess.org/) (Installed via Homebrew: `brew install stockfish`)

## Installation

1. Clone this repository (or ensure you are in the project folder).
2. Sync dependencies:
   ```bash
   uv sync
   ```

## Setup for Claude Desktop

1. Open your Claude Desktop configuration file:
   `~/Library/Application Support/Claude/claude_desktop_config.json`
2. Add the following to the `mcpServers` object:
   ```json
   "Grandmaster-Coach": {
     "command": "uv",
     "args": [
       "--directory",
       "/Users/riteshsingh/Desktop/Files/VSCode/chess_analyzer",
       "run",
       "mcp_server.py"
     ]
   }
   ```
3. Restart Claude Desktop.

## How to Stop or Disable the Server

Since Claude Desktop launches the server automatically upon startup, you have two ways to stop it:

### 1. Temporarily Stop (Close Claude)
Simply quitting the Claude Desktop app will terminate the background `mcp_server.py` process.

### 2. Disable Permanently
To prevent Claude from starting the server again:
- Open `claude_desktop_config.json`.
- Remove the `Grandmaster-Coach` entry from the `mcpServers` list.
- Save and restart Claude.

## Testing with Inspector

To test the tools manually without opening Claude:
```bash
uv run fastmcp dev mcp_server.py
```
This will open the MCP Inspector in your browser at `http://localhost:6274`.
