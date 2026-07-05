# -*- coding: utf-8 -*-

import discord
from discord.ext import commands
import wavelink
import aiosqlite
import os
import aiohttp

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

    async def setup_hook(self):
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
                    await ctx.send(f"Here are all the playlists on the server:\n{playlists}\n\nType !show <name> to see the tracks.")
            return

        # If a name IS provided, show all tracks for that playlist (from
        # anyone)
        async with db.execute("SELECT song_url FROM playlists WHERE playlist_name = ?", (playlist_name,)) as cursor:
            rows = await cursor.fetchall()
            if not rows:
                await ctx.send(f"Playlist '{playlist_name}' not found.")
                return

            songs = "\n".join([row[0] for row in rows])
            await ctx.send(f"Songs in **{playlist_name}**:\n{songs}")


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
                # If it's a playlist, add all tracks; otherwise, add the single track
                if isinstance(result, wavelink.Playlist):
                    for track in result.tracks:
                        await player.queue.put_wait(track)
                else:
                    await player.queue.put_wait(result[0])
    else:
        results = await wavelink.Playable.search(search)
        if not results:
            return await ctx.send("Not found.")
        
        # Check if the search returned a full playlist
        if isinstance(results, wavelink.Playlist):
            for track in results.tracks:
                await player.queue.put_wait(track)
            await ctx.send(f"Added playlist: {results.name}")
        else:
            track = results[0]
            duration_ms = track.length
            duration = f"{duration_ms // 60000}:{(duration_ms // 1000) % 60:02d}"
            
            embed = discord.Embed(title="Added to Queue", color=discord.Color.blue())
            embed.add_field(name="Track", value=f"[{track.title}]({track.uri}) by {track.author}", inline=False)
            embed.add_field(name="Requested by", value=ctx.author.display_name, inline=True)
            embed.add_field(name="Duration", value=duration, inline=True)
            embed.add_field(name="Position", value=str(len(player.queue) + 1), inline=True)
            
            await player.queue.put_wait(track)
            await ctx.send(embed=embed)

    if not player.playing:
        await player.play(player.queue.get())


@bot.command()
async def nowplaying(ctx):
    player = ctx.voice_client
    if player and player.current:
        track = player.current
        # Calculate duration
        duration = f"{track.length // 60000}:{(track.length // 1000) % 60:02d}"
        
        # Calculate current position in the queue
        # We add 1 because the current track is effectively the "1st" track
        current_pos = 1 
        
        embed = discord.Embed(title="Now Playing", color=discord.Color.red())
        
        # Consistent link format: [Title](URL) by Artist
        track_display = f"[{track.title}]({track.uri})"
        embed.add_field(name="Track", value=f"{track_display} by {track.author}", inline=False)
        embed.add_field(name="Duration", value=duration, inline=True)
        
        # Add the progress if you want, or just leave it clean
        embed.set_footer(text="Use !play to add more tunes")
        
        await ctx.send(embed=embed)
    else:
        await ctx.send("Nothing is playing right now.")

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


# --- This is the weather API Calls (60 calls per minute) ---

@bot.command()
async def weather(ctx, *, location: str):
    # 1. Geocode the location (Convert city name to lat/lon)
    geo_url = f"http://api.openweathermap.org/geo/1.0/direct?q={location}&limit=1&appid={WEATHER_API_KEY}"

    async with aiohttp.ClientSession() as session:
        async with session.get(geo_url) as resp:
            geo_data = await resp.json()

        if not geo_data:
            return await ctx.send("I couldn't find that city.")

        lat = geo_data[0]['lat']
        lon = geo_data[0]['lon']
        city_name = geo_data[0]['name']

        # 2. Get the actual weather
        weather_url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={WEATHER_API_KEY}&units=metric"

        async with session.get(weather_url) as resp:
            data = await resp.json()

        temp = data["main"]["temp"]
        desc = data["weather"][0]["description"]

        # 3. Simple clothing advice logic
        advice = "It's a nice day out!"
        if temp < 15:
            advice = "It's chilly, you should have a pullover!"
        elif temp > 28:
            advice = "It's pretty hot, stay hydrated!"

        if "rain" in desc.lower():
            advice = "It's raining, don't forget your gumboots!"

        await ctx.send(
            f"The weather in {location} is {temp} degrees Celsius\n"
            f"{desc.capitalize()}\n"
            f"{advice}"
        )


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
