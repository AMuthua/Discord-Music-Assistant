# Discord Music Assistant

A robust, self-hosted Discord music bot designed to function as a private "Radio Station." Built with Python, Wavelink, and SQLite, WheyBot brings automated DJ features and smart playlist management to your Discord servers.

## Key Features
*   **Smart Playlist Management:** Save and retrieve custom music playlists directly from an SQLite database.
*   **Auto-DJ Mode:** Built-in Wavelink Autoplay ensures the music never stops, even when your queue is empty.
*   **Smart Status:** Automatically updates the bot's "Now Playing" status to keep listeners informed.
*   **Weather Integration:** Need to know if it's a good day to go out? Built-in commands provide real-time weather and clothing advice.
*   **Lightweight & Efficient:** Optimized to run as a systemd service on low-power hardware (like AMD E-Series).

## Tech Stack
*   **Language:** Python 3.12
*   **Audio Engine:** Lavalink
*   **Database:** aiosqlite
*   **API Integration:** OpenWeatherMap API

## Setup Instructions

### Prerequisites
1. Install [Java](https://adoptium.net/) (for Lavalink).
2. Create a `.env` file in the root directory:
   ```env
   TOKEN=your_discord_bot_token
   WEATHER_API_KEY=your_openweathermap_api_key
   LAVALINK_PASSWORD=your_secure_password