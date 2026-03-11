FROM python:3.11-slim

# Install system dependencies including Stockfish and Curl (often useful)
RUN apt-get update && \
    apt-get install -y stockfish curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy application files
COPY . /app

# Install dependencies using pip (Render doesn't need uv specifically, pip is fine)
RUN pip install --no-cache-dir fastapi uvicorn "chess>=1.10.0" openai pydantic python-dotenv fastmcp

# Expose port 8000 (Render uses $PORT dynamically, but exposing 8000 is good practice for local testing)
EXPOSE 8000

# Run the FastAPI server natively via uvicorn so it handles HTTP and WebSockets on Render
CMD ["sh", "-c", "uvicorn mcp_server:app --host 0.0.0.0 --port ${PORT:-8000}"]
