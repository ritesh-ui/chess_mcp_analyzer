## Install UV
pip install uv

## Initialize UV
uv init .

## Install fastmcp
uv add fastmcp

## Add this decorator to make an python function an MCP tool
@mcp.tool

## Test the server
uv run fastmcp dev my_server.py

## Run the server
uv run fastmcp run my_server.py

## Install the MCP server to a client
uv run fastmcp install claude-desktop my_server.py

## Incase of any issue.
Open claud config . Put full path of uv. which uv command can help

## To run MCP server at cloud
uv run my_server.py

fastmcp run my_server.py --transport http --host 0.0.0.0 --port 8000