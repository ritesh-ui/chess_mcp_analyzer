import subprocess
import sys
import os

def install_stockfish():
    print("Checking for Homebrew...")
    try:
        subprocess.run(["brew", "--version"], check=True, capture_output=True)
        print("Homebrew found. Installing Stockfish...")
        subprocess.run(["brew", "install", "stockfish"], check=True)
        print("Stockfish installed successfully.")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Homebrew not found or installation failed.")
        print("Please install Homebrew first or download Stockfish manually from: https://stockfishchess.org/download/")
        sys.exit(1)

if __name__ == "__main__":
    install_stockfish()
