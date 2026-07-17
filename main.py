# -*- coding: utf-8 -*-

import discord
from discord.ext import commands
import wavelink
import aiosqlite
import os
import aiohttp
import re
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv


DB_FILE = "music_data.db"

# Load credentials from .env
load_dotenv()
TOKEN = os.getenv("TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD")



class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.web_session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        # One shared HTTP session for the bot's lifetime (cheaper than
        # opening a new one on every !weather call)
        self.web_session = aiohttp.ClientSession()

        # Initialize Database
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS playlists (
                    user_id INTEGER,
                    playlist_name TEXT,
                    song_url TEXT
                )
            ''')
            await db.commit()

        # Connect to your local Lavalink node
        node = wavelink.Node(
            uri="http://127.0.0.1:2333",
            password=LAVALINK_PASSWORD
        )
        await wavelink.Pool.connect(client=self, nodes=[node])

    async def close(self):
        if self.web_session:
            await self.web_session.close()
        await super().close()


bot = MyBot()


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')



@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player

    # 1. Safety Check: If the player was disconnected, stop logic.
    # This keeps the bot stable and avoids the 'NoneType' crashes.
    if not player:
        return

    # 2. Manual Queue Logic: If you have tracks in your queue, play the next
    # one.
    if not player.queue.is_empty:
        next_track = player.queue.get()
        await player.play(next_track)

    # 3. DJ/Autoplay Logic: If the queue is empty, let Wavelink's AutoPlay find a track.
    # This ensures the bot stays in the channel and keeps playing for your
    # mates.
    elif player.autoplay == wavelink.AutoPlayMode.enabled:
        # Wavelink's internal system will now fetch a similar track
        # automatically.
        pass

    else:
        # Fallback: Queue is empty and DJ mode is off.
        await player.channel.send("Queue finished! Add more tracks with !play or enable DJ mode.")

# --- Commands ---


@bot.command()
async def ping(ctx):
    await ctx.send('Pong! At your service :)')


@bot.command()
async def save(ctx, playlist_name: str, url: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO playlists (user_id, playlist_name, song_url) VALUES (?, ?, ?)",
            (ctx.author.id, playlist_name, url)
        )
        await db.commit()
    await ctx.send(f"Added to **{playlist_name}**! (Thanks, {ctx.author.name})")


@bot.command()
async def show(ctx, playlist_name: str = None):
    async with aiosqlite.connect(DB_FILE) as db:
        # If no playlist name, show ALL playlists from everyone
        if playlist_name is None:
            async with db.execute("SELECT DISTINCT playlist_name FROM playlists") as cursor:
                rows = await cursor.fetchall()
                if not rows:
                    await ctx.send("No playlists found in the database.")
                else:
                    playlists = "\n".join([row[0] for row in rows])
                    await ctx.send(f"Here are all the playlists on the server:\n{playlists}\n\nType `!show <playlist name>` to see the tracks.")
            return

        # If a name IS provided, show all tracks for that playlist (from
        # anyone)
        async with db.execute("SELECT song_url FROM playlists WHERE playlist_name = ?", (playlist_name,)) as cursor:
            rows = await cursor.fetchall()
            if not rows:
                await ctx.send(f"Playlist '{playlist_name}' not found.")
                return

            view = PlaylistView(playlist_name, rows, ctx.author)
            await ctx.send(content=view.header(), embeds=view.build_embeds(), view=view)


def tag_requester(track: wavelink.Playable, ctx) -> wavelink.Playable:
    """Stamp a track with who queued it, so it survives until nowplaying/queue reads it."""
    track.extras = {"requester_id": ctx.author.id, "requester_name": ctx.author.display_name}
    return track


@bot.command()
async def play(ctx, *, search: str):
    if not ctx.author.voice:
        return await ctx.send("You need to be in a voice channel.")

    channel = ctx.author.voice.channel
    player = ctx.voice_client
    if not player:
        player = await channel.connect(cls=wavelink.Player, timeout=20.0)
        player.autoplay = wavelink.AutoPlayMode.enabled

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT song_url FROM playlists WHERE playlist_name = ?", (search,)) as cursor:
            rows = await cursor.fetchall()

    if rows:
        embed = discord.Embed(title=f"Playlist: {search}", color=discord.Color.green())
        embed.description = f"Added {len(rows)} items to the queue."
        await ctx.send(embed=embed)
        
        for row in rows:
            result = await wavelink.Playable.search(row[0])
            if result:
                if isinstance(result, wavelink.Playlist):
                    for track in result.tracks:
                        await player.queue.put_wait(tag_requester(track, ctx))
                else:
                    await player.queue.put_wait(tag_requester(result[0], ctx))
    else:
        results = await wavelink.Playable.search(search)
        if not results:
            return await ctx.send("Not found.")
        
        # --- PLAYLIST EMBED FIX ---
        if isinstance(results, wavelink.Playlist):
            for track in results.tracks:
                await player.queue.put_wait(tag_requester(track, ctx))
            
            embed = discord.Embed(title="Playlist Added to Queue", color=discord.Color.green())
            embed.add_field(name="Playlist", value=results.name, inline=False)
            embed.add_field(name="Tracks Added", value=str(len(results.tracks)), inline=True)
            embed.add_field(name="Requested by", value=ctx.author.display_name, inline=True)
            await ctx.send(embed=embed)
            
        # --- SINGLE TRACK EMBED & TIME FIX ---
        else:
            track = results[0]
            
            # New Time Math (Handles Hours)
            seconds = track.length // 1000
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            secs = seconds % 60
            
            if hours > 0:
                duration = f"{hours}:{minutes:02d}:{secs:02d}"
            else:
                duration = f"{minutes}:{secs:02d}"
            
            embed = discord.Embed(title="Added to Queue", color=discord.Color.blue())
            embed.add_field(name="Track", value=f"[{track.title}]({track.uri}) by {track.author}", inline=False)
            embed.add_field(name="Requested by", value=ctx.author.display_name, inline=True)
            embed.add_field(name="Duration", value=duration, inline=True)
            embed.add_field(name="Position", value=str(len(player.queue) + 1), inline=True)
            
            await player.queue.put_wait(tag_requester(track, ctx))
            await ctx.send(embed=embed)

    if not player.playing:
        await player.play(player.queue.get())


# Source badge: same "data table instead of if/elif" pattern as the weather
# vibes. Add a new streaming source here and nowplaying picks it up for free.
SOURCE_STYLES = {
    "youtube":   {"emoji": "▶️", "color": discord.Color.from_rgb(255, 0, 0)},
    "spotify":   {"emoji": "🟢", "color": discord.Color.from_rgb(30, 215, 96)},
    "soundcloud": {"emoji": "🟠", "color": discord.Color.from_rgb(255, 119, 0)},
}
DEFAULT_SOURCE_STYLE = {"emoji": "🎵", "color": discord.Color.red()}

# Matches the video ID out of any common YouTube URL shape:
# watch?v=, youtu.be/, shorts/, embed/
YOUTUBE_ID_PATTERN = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([\w-]{11})"
)


def extract_youtube_id(url: str) -> str | None:
    match = YOUTUBE_ID_PATTERN.search(url)
    return match.group(1) if match else None


def get_platform_style(url: str) -> dict:
    """Same idea as SOURCE_STYLES but keyed off a raw saved URL instead of a
    resolved wavelink track, for use in !show before anything's been searched."""
    lowered = url.lower()
    if "spotify.com" in lowered:
        return SOURCE_STYLES["spotify"]
    if "soundcloud.com" in lowered:
        return SOURCE_STYLES["soundcloud"]
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return SOURCE_STYLES["youtube"]
    return DEFAULT_SOURCE_STYLE


class PlaylistView(discord.ui.View):
    """Paginated browser for a saved playlist: Previous/Next flip through
    10 tracks at a time, and the dropdown queues whichever track you pick -
    so spotting song #15 on page 2 is one click away, not a separate command."""

    PER_PAGE = 10

    def __init__(self, playlist_name: str, rows: list, author: discord.abc.User):
        super().__init__(timeout=180)
        self.playlist_name = playlist_name
        self.rows = rows
        self.author = author
        self.page = 0
        self.max_page = max(0, (len(rows) - 1) // self.PER_PAGE)
        self._sync_controls()

    def _page_rows(self):
        start = self.page * self.PER_PAGE
        return self.rows[start:start + self.PER_PAGE], start

    def _sync_controls(self):
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page == self.max_page

        page_rows, start = self._page_rows()
        self.track_select.options = [
            discord.SelectOption(label=f"Track {start + i + 1}", description=row[0][:100], value=str(start + i + 1))
            for i, row in enumerate(page_rows)
        ]

    def header(self) -> str:
        return f"Songs in **{self.playlist_name}** (page {self.page + 1}/{self.max_page + 1}):"

    def build_embeds(self) -> list:
        page_rows, start = self._page_rows()
        embeds = []
        for i, row in enumerate(page_rows, start=start + 1):
            url = row[0]
            style = get_platform_style(url)
            embed = discord.Embed(title=f"{style['emoji']} Track {i}", url=url, description=url, color=style["color"])
            video_id = extract_youtube_id(url)
            if video_id:
                embed.set_thumbnail(url=f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg")
            embeds.append(embed)
        return embeds

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Only the person who ran `!show` can use these controls.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.select(placeholder="Pick a track to queue it...")
    async def track_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        idx = int(select.values[0]) - 1
        url = self.rows[idx][0]
        await queue_track_for_interaction(interaction, url)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync_controls()
        await interaction.response.edit_message(content=self.header(), embeds=self.build_embeds(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync_controls()
        await interaction.response.edit_message(content=self.header(), embeds=self.build_embeds(), view=self)


async def queue_track_for_interaction(interaction: discord.Interaction, url: str):
    """Queue a single saved-playlist URL from a button/select interaction
    (mirrors the connect -> search -> tag -> queue flow in !play)."""
    if not interaction.user.voice:
        return await interaction.response.send_message("You need to be in a voice channel to queue a track.", ephemeral=True)

    channel = interaction.user.voice.channel
    player = interaction.guild.voice_client
    if not player:
        player = await channel.connect(cls=wavelink.Player, timeout=20.0)
        player.autoplay = wavelink.AutoPlayMode.enabled

    result = await wavelink.Playable.search(url)
    if not result:
        return await interaction.response.send_message("Couldn't find that track.", ephemeral=True)

    track = result.tracks[0] if isinstance(result, wavelink.Playlist) else result[0]
    track.extras = {"requester_id": interaction.user.id, "requester_name": interaction.user.display_name}
    await player.queue.put_wait(track)

    if not player.playing:
        await player.play(player.queue.get())

    await interaction.response.send_message(f"🎶 Queued **{track.title}**!", ephemeral=True)


@play.error
async def play_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("⚠️ You forgot to tell me what to play! Try something like: `!play lofi beats` or `!play paste a URL`.")


def format_duration(ms: int) -> str:
    seconds = ms // 1000
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_progress_bar(position_ms: int, length_ms: int, bar_length: int = 18) -> str:
    if not length_ms:
        return "▬" * bar_length
    ratio = max(0.0, min(1.0, position_ms / length_ms))
    filled = int(bar_length * ratio)
    bar = "▬" * filled + "🔘" + "▬" * (bar_length - filled)
    return bar


class NowPlayingView(discord.ui.View):
    """Playback controls attached to the nowplaying message: pause/resume
    toggles in place, skip moves to the next track, stop disconnects."""

    def __init__(self, player: wavelink.Player):
        super().__init__(timeout=300)
        self.player = player
        self._sync_pause_button()

    def _sync_pause_button(self):
        if self.player.paused:
            self.pause_button.label = "▶ Resume"
            self.pause_button.style = discord.ButtonStyle.success
        else:
            self.pause_button.label = "⏸ Pause"
            self.pause_button.style = discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only let people actually in the voice channel control playback
        if not interaction.user.voice or interaction.user.voice.channel != self.player.channel:
            await interaction.response.send_message("Join the voice channel to control playback.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.secondary)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.pause(not self.player.paused)
        self._sync_pause_button()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.primary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.skip()
        await interaction.response.send_message("Skipped!", ephemeral=True)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.disconnect()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Disconnected!", view=self)
        self.stop()


@bot.command()
async def nowplaying(ctx):
    player = ctx.voice_client
    if not (player and player.current):
        return await ctx.send("Nothing is playing right now.")

    track = player.current
    style = SOURCE_STYLES.get(getattr(track, "source", ""), DEFAULT_SOURCE_STYLE)

    elapsed = format_duration(player.position)
    total = format_duration(track.length)
    progress_bar = build_progress_bar(player.position, track.length)

    # Requester was stamped on the track back in !play, if it's still there
    requester_name = None
    if track.extras:
        requester_name = getattr(track.extras, "requester_name", None)

    embed = discord.Embed(
        title=f"{style['emoji']} Now Playing",
        description=f"[{track.title}]({track.uri})\nby **{track.author}**",
        color=style["color"],
    )

    artwork = getattr(track, "artwork", None)
    if artwork:
        embed.set_thumbnail(url=artwork)

    embed.add_field(name="Progress", value=f"`{elapsed}` {progress_bar} `{total}`", inline=False)
    if requester_name:
        embed.add_field(name="Requested by", value=requester_name, inline=True)
    embed.add_field(name="Status", value="⏸️ Paused" if player.paused else "▶️ Playing", inline=True)
    embed.set_footer(text="Use !play to add more tunes")

    await ctx.send(embed=embed, view=NowPlayingView(player))

@bot.command()
async def rename_playlist(ctx, old_name: str, new_name: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE playlists SET playlist_name = ? WHERE playlist_name = ?",
            (new_name, old_name)
        )
        await db.commit()
    await ctx.send(f"Renamed '{old_name}' to '{new_name}'.")


@bot.command()
async def stop(ctx):
    player = ctx.voice_client
    if player:
        await player.disconnect()
        await ctx.send("Disconnected!")


@bot.command()
async def pause(ctx):
    player = ctx.voice_client
    if player:
        await player.pause(True)  # True means "Yes, pause it"
        await ctx.send("Paused!")


@bot.command()
async def resume(ctx):
    player = ctx.voice_client
    if player:
        await player.pause(False)  # False means "Un-pause it"
        await ctx.send("Resumed!")


@bot.command()
async def queue(ctx):
    player = ctx.voice_client
    if not player or not player.queue:
        return await ctx.send("The queue is empty.")

    # Show the first 10 items in the queue
    upcoming = "\n".join(
        [f"{i + 1}. {track.title}" for i, track in enumerate(list(player.queue)[:10])])
    await ctx.send(f"Upcoming tracks:\n{upcoming}")


@bot.command()
async def skip(ctx):
    player = ctx.voice_client
    if player and player.playing:
        await player.skip()
        await ctx.send("Skipped to the next track!")


@bot.command()
async def jump(ctx, index: int):
    player = ctx.voice_client
    if not player or not player.queue:
        return await ctx.send("The queue is empty.")

    # Check if the index is valid (1-based index)
    if index < 1 or index > len(player.queue):
        return await ctx.send(f"Invalid index! Please pick a number between 1 and {len(player.queue)}.")

    # 1. Remove all tracks before the target index
    # We want the target track to be at the front, so we remove (index - 1)
    # items
    for _ in range(index - 1):
        if not player.queue.is_empty:
            player.queue.get()

    # 2. Skip to the new "front" of the queue
    await player.skip()
    await ctx.send(f"Jumped to track {index}!")


@bot.command()
async def remove(ctx, index: str):  # Change index to str to handle errors better
    try:
        idx = int(index)
        player = ctx.voice_client
        if player and player.queue:
            del player.queue[idx - 1]
            await ctx.send(f"Removed track {idx} from the queue.")
    except ValueError:
        await ctx.send("Please provide the number of the song you want to remove.")


@bot.command()
async def delete_playlist(ctx, *, playlist_name: str):
    async with aiosqlite.connect(DB_FILE) as db:
        # Check if it exists first
        cursor = await db.execute("SELECT * FROM playlists WHERE playlist_name = ?", (playlist_name,))
        row = await cursor.fetchone()

        if not row:
            await ctx.send(f"I couldn't find a playlist named '{playlist_name}'.")
            return

        # Delete it
        await db.execute("DELETE FROM playlists WHERE playlist_name = ?", (playlist_name,))
        await db.commit()
        await ctx.send(f"The playlist '{playlist_name}' has been deleted from the server.")


@bot.command()
async def volume(ctx, vol: int):
    player = ctx.voice_client
    if player:
        # Vol should be between 0 and 100
        await player.set_volume(vol)
        await ctx.send(f"Volume set to {vol}%")


# --- Weather API (60 calls per minute on the free tier) ---
#
# Instead of a chain of if/elif statements, the "mood" of a weather report
# is decided by a small rules table (WEATHER_VIBES). Each rule is
# (condition, emoji, line) and rules are checked top to bottom - first
# match wins. Want to teach the bot about "windy" or "heatwave" days later?
# Add one line to the table. No nested logic to untangle.

WEATHER_VIBES = [
    # High-priority conditions (storms, snow, rain) override plain temperature
    {"match": lambda t, c: "thunderstorm" in c,
     "emoji": "⛈️", "line": "Thunder's rolling in : maybe a stay-in, sip some tea."},
    {"match": lambda t, c: "snow" in c,
     "emoji": "❄️", "line": "Snowing! Bundle up, it's a hot-chocolate kind of day."},
    {"match": lambda t, c: "rain" in c or "drizzle" in c,
     "emoji": "🌧️", "line": "Grab an umbrella, it's coming down out there."},
    {"match": lambda t, c: "mist" in c or "fog" in c or "haze" in c,
     "emoji": "🌫️", "line": "Visibility's low : take it easy if you're driving."},

    # Fallback to temperature bands
    {"match": lambda t, c: t <= 0,
     "emoji": "🥶", "line": "Freezing! Layer up, this isn't a day to skip the coat."},
    {"match": lambda t, c: t <= 10,
     "emoji": "🧣", "line": "Chilly out : a jacket's a good call."},
    {"match": lambda t, c: t <= 18,
     "emoji": "🍂", "line": "Cool and comfortable, light layers should do it."},
    {"match": lambda t, c: t <= 25,
     "emoji": "🌤️", "line": "Pretty pleasant : a great day to be outside."},
    {"match": lambda t, c: t <= 32,
     "emoji": "☀️", "line": "Warm and sunny, stay hydrated!"},
    {"match": lambda t, c: True,
     "emoji": "🔥", "line": "Scorching! Seek shade and drink plenty of water."},
]

# Embed color drifts from icy blue to red as temperature climbs.
TEMP_COLORS = [
    (0,   discord.Color.from_rgb(150, 200, 255)),   # freezing
    (10,  discord.Color.blue()),
    (18,  discord.Color.teal()),
    (25,  discord.Color.green()),
    (32,  discord.Color.orange()),
    (999, discord.Color.red()),
]


def get_weather_vibe(temp: float, condition_main: str, icon: str):
    """Return (emoji, flavor_line) for the current temp + condition, accounting for day/night."""
    condition = condition_main.lower()
    for vibe in WEATHER_VIBES:
        if vibe["match"](temp, condition):
            emoji = vibe["emoji"]
            line = vibe["line"]
            
            # --- NIGHT MODE TWEAK ---
            # If the icon string contains 'n' (night) and the matched emoji is a sun, swap it!
            if 'n' in icon:
                if emoji == "☀️":
                    emoji = "🌙"
                    line = "Clear skies tonight, enjoy the stars."
                elif emoji == "🌤️":
                    emoji = "🌑"
                    line = "A pleasant night out."
            
            return emoji, line
            
    return "🌡️", "Weather's doing its thing out there."


def get_temp_color(temp: float) -> discord.Color:
    for threshold, color in TEMP_COLORS:
        if temp <= threshold:
            return color
    return discord.Color.red()


@bot.command()
async def weather(ctx, *, location: str):
    session = bot.web_session

    # 1. Geocode the location (city name -> lat/lon)
    geo_url = "http://api.openweathermap.org/geo/1.0/direct"
    geo_params = {"q": location, "limit": 1, "appid": WEATHER_API_KEY}

    async with session.get(geo_url, params=geo_params) as resp:
        if resp.status != 200:
            return await ctx.send("Weather service is having a moment, try again shortly.")
        geo_data = await resp.json()

    if not geo_data:
        return await ctx.send(f"I couldn't find a place called **{location}**.")

    lat = geo_data[0]["lat"]
    lon = geo_data[0]["lon"]
    city_name = geo_data[0]["name"]
    country = geo_data[0].get("country", "")

    # 2. Get current weather for those coordinates
    weather_url = "https://api.openweathermap.org/data/2.5/weather"
    weather_params = {"lat": lat, "lon": lon, "appid": WEATHER_API_KEY, "units": "metric"}

    async with session.get(weather_url, params=weather_params) as resp:
        if resp.status != 200:
            return await ctx.send("Weather service is having a moment, try again shortly.")
        data = await resp.json()

    temp = data["main"]["temp"]
    feels_like = data["main"]["feels_like"]
    humidity = data["main"]["humidity"]
    wind_speed = data["wind"]["speed"]
    condition_main = data["weather"][0]["main"]
    description = data["weather"][0]["description"]
    icon = data["weather"][0]["icon"]

    # --- LOCAL TIME CALCULATION ---
    tz_offset_seconds = data.get("timezone", 0)
    local_dt = datetime.now(timezone.utc) + timedelta(seconds=tz_offset_seconds)
    time_str = local_dt.strftime("%A, %B %d %Y at %I:%M %p") # Addition in time format, to showcase Day, Month, Date and the year.

    emoji, flavor_line = get_weather_vibe(temp, condition_main, icon)
    color = get_temp_color(temp)

    embed = discord.Embed(
        title=f"{emoji} Weather in {city_name}, {country}",
        description=f"**{description.capitalize()}**\n{flavor_line}",
        color=color,
    )
    embed.set_thumbnail(url=f"https://openweathermap.org/img/wn/{icon}@2x.png")

    embed.add_field(name="Local Time", value=f"🕒 {time_str}", inline=False)

    embed.add_field(name="Temperature", value=f"{temp:.1f}°C", inline=True)
    embed.add_field(name="Feels Like", value=f"{feels_like:.1f}°C", inline=True)
    embed.add_field(name="Humidity", value=f"{humidity}%", inline=True)
    embed.add_field(name="Wind", value=f"{wind_speed} m/s", inline=True)
    embed.set_footer(text=f"Requested by {ctx.author.display_name}")

    await ctx.send(embed=embed)


@bot.command()
async def shuffle(ctx):
    player = ctx.voice_client
    if player and player.queue:
        player.queue.shuffle()
        # Clean, non-intrusive feedback
        await ctx.message.add_reaction("Shuffle Activated")
    else:
        await ctx.send("Queue is empty, nothing to shuffle.")


@bot.command()
async def toggle_dj(ctx):
    player = ctx.voice_client
    if player:
        if player.autoplay == wavelink.AutoPlayMode.enabled:
            player.autoplay = wavelink.AutoPlayMode.disabled
            await ctx.send("DJ Mode: OFF")
        else:
            player.autoplay = wavelink.AutoPlayMode.enabled
            await ctx.send("DJ Mode: ON")

# Replace the token here, with the one got from Discord Developer Portal. 
bot.run(TOKEN)