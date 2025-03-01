import discord
from discord.ext import commands
import asyncpg
import logging
import json
import os
import asyncio
import time

# Für die Charts
import matplotlib.pyplot as plt
from io import BytesIO

# Monitoring-Modul importieren
from monitoring import monitor

# ------------------------
# Logging-Konfiguration
# ------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s:%(levelname)s:%(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ------------------------
# Konfiguration laden
# ------------------------
CONFIG_FILE = "config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"{CONFIG_FILE} existiert nicht!")
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)
    logger.info("Konfiguration gespeichert.")

config = load_config()

TOKEN = config.get("token")
PREFIX = config.get("prefix", "!")
FORUM_CHANNEL_ID = config.get("forum_channel_id")
STARBOARD_CHANNEL_ID = config.get("starboard_channel_id")
STAR_THRESHOLD = config.get("star_threshold", 3)
DB_CONFIG = config.get("db", {})

STAR_EMOJI = "⭐"

# ------------------------
# Datenbank-Pool (asyncpg) global
# ------------------------
db_pool = None

# ------------------------
# Bot-Initialisierung
# ------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.messages = True
intents.guilds = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)
bot.remove_command("help")  # Entferne den Standard-Help-Command

# ------------------------
# Periodischer Task für Systemressourcen
# ------------------------
async def system_usage_loop():
    """Misst in regelmäßigen Abständen CPU- und RAM-Verbrauch."""
    while True:
        monitor.record_system_usage()
        await asyncio.sleep(30)  # Alle 30 Sekunden messen

# ------------------------
# PostgreSQL-Funktionen für das Starboard-Mapping (mit Monitoring)
# ------------------------
async def get_mapping(message_id: int):
    start = time.perf_counter()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT starboard_message_id FROM starboard_mapping WHERE message_id = $1", message_id
        )
    duration = time.perf_counter() - start
    monitor.record_db_query(duration)
    return row["starboard_message_id"] if row else None

async def upsert_mapping(message_id: int, starboard_message_id: int, stars: int):
    start = time.perf_counter()
    async with db_pool.acquire() as connection:
        await connection.execute("""
            INSERT INTO starboard_mapping(message_id, starboard_message_id, stars)
            VALUES($1, $2, $3)
            ON CONFLICT (message_id) DO UPDATE
            SET starboard_message_id = EXCLUDED.starboard_message_id,
                stars = EXCLUDED.stars,
                updated_at = CURRENT_TIMESTAMP
        """, message_id, starboard_message_id, stars)
    duration = time.perf_counter() - start
    monitor.record_db_query(duration)

async def remove_mapping(message_id: int):
    start = time.perf_counter()
    async with db_pool.acquire() as connection:
        await connection.execute("DELETE FROM starboard_mapping WHERE message_id = $1", message_id)
    duration = time.perf_counter() - start
    monitor.record_db_query(duration)

# ------------------------
# Hilfsfunktion: Prüfen, ob eine Nachricht im Ziel-Forum ist
# ------------------------
def is_in_target_forum(message: discord.Message):
    if isinstance(message.channel, discord.Thread):
        if message.channel.parent is not None:
            logger.info(f"Thread-Parent-ID: {message.channel.parent.id}, Typ: {message.channel.parent.type}")
            return (message.channel.parent.id == FORUM_CHANNEL_ID and 
                    message.channel.parent.type == discord.ChannelType.forum)
        else:
            logger.info("Nachricht in einem Thread ohne Parent.")
            return False
    return False

# ------------------------
# Starboard-Update-Funktion mit Performance-Messung
# ------------------------
async def update_starboard_message(message: discord.Message):
    start_update = time.perf_counter()
    # Suche nach der STAR-Emoji-Reaktion
    star_reaction = None
    for reaction in message.reactions:
        if str(reaction.emoji) == STAR_EMOJI:
            star_reaction = reaction
            break

    starboard_channel = bot.get_channel(STARBOARD_CHANNEL_ID)
    if not starboard_channel:
        logger.error("Starboard Channel nicht gefunden!")
        return

    # Falls keine STAR-Reaktion vorhanden ist -> ggf. Post löschen
    if star_reaction is None:
        logger.info(f"Keine STAR-Emoji-Reaktion in Nachricht {message.id} gefunden.")
        starboard_message_id = await get_mapping(message.id)
        if starboard_message_id:
            logger.info(f"Versuche, Starboard-Post {starboard_message_id} für Nachricht {message.id} zu löschen (keine Reaktionen).")
            try:
                star_msg = await starboard_channel.fetch_message(starboard_message_id)
                await star_msg.delete()
                logger.info(f"Starboard-Post für Nachricht {message.id} gelöscht.")
            except discord.Forbidden:
                logger.error("Bot hat keine Berechtigung, den Starboard-Post zu löschen!")
            except discord.NotFound:
                logger.warning(f"Starboard-Post für Nachricht {message.id} nicht gefunden.")
            await remove_mapping(message.id)
        monitor.record_update(time.perf_counter() - start_update)
        return

    count = star_reaction.count
    logger.info(f"Star-Emoji-Zähler für Nachricht {message.id}: {count}")

    # Falls Sterne < Threshold -> Post löschen
    if count < STAR_THRESHOLD:
        starboard_message_id = await get_mapping(message.id)
        if starboard_message_id:
            logger.info(f"Versuche, Starboard-Post {starboard_message_id} für Nachricht {message.id} zu löschen (Sterne: {count}).")
            try:
                star_msg = await starboard_channel.fetch_message(starboard_message_id)
                await star_msg.delete()
                logger.info(f"Starboard-Post für Nachricht {message.id} gelöscht (Sterne: {count}).")
            except discord.Forbidden:
                logger.error("Bot hat keine Berechtigung, den Starboard-Post zu löschen!")
            except discord.NotFound:
                logger.warning(f"Starboard-Post für Nachricht {message.id} nicht gefunden.")
            await remove_mapping(message.id)
        monitor.record_update(time.perf_counter() - start_update)
        return

    # Erstelle ein Embed
    embed = discord.Embed(title="Starred Message", color=0xFFD700)
    embed.add_field(name="Autor", value=message.author.mention, inline=True)
    embed.add_field(name="Sterne", value=str(count), inline=True)
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    embed.description = message.content
    embed.set_footer(text=f"Nachrichten-ID: {message.id}")

    # Aktualisiere oder erstelle neuen Starboard-Post
    starboard_message_id = await get_mapping(message.id)
    if starboard_message_id:
        try:
            star_msg = await starboard_channel.fetch_message(starboard_message_id)
            await star_msg.edit(content=f"{STAR_EMOJI} {count}", embed=embed)
            logger.info(f"Starboard-Post für Nachricht {message.id} aktualisiert (Sterne: {count}).")
            await upsert_mapping(message.id, starboard_message_id, count)
        except discord.NotFound:
            new_msg = await starboard_channel.send(content=f"{STAR_EMOJI} {count}", embed=embed)
            await upsert_mapping(message.id, new_msg.id, count)
            logger.info(f"Neuer Starboard-Post für Nachricht {message.id} erstellt (Sterne: {count}).")
    else:
        new_msg = await starboard_channel.send(content=f"{STAR_EMOJI} {count}", embed=embed)
        await upsert_mapping(message.id, new_msg.id, count)
        logger.info(f"Starboard-Post für Nachricht {message.id} erstellt (Sterne: {count}).")

    monitor.record_update(time.perf_counter() - start_update)

# ------------------------
# Bot-Events
# ------------------------
@bot.event
async def on_ready():
    global db_pool
    logger.info(f"Bot ist online als {bot.user}")
    try:
        db_pool = await asyncpg.create_pool(
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            database=DB_CONFIG["database"],
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"]
        )
        logger.info("Datenbankverbindung hergestellt.")
    except Exception as e:
        logger.error(f"Fehler beim Erstellen der DB-Pool: {e}")

    # Starte den Loop für Systemressourcen
    bot.loop.create_task(system_usage_loop())

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    logger.info(f"[on_raw_reaction_add] Reaktion {payload.emoji} von User {payload.user_id} in Channel {payload.channel_id} hinzugefügt.")
    if str(payload.emoji) != STAR_EMOJI:
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        logger.error("Channel nicht gefunden!")
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Nachricht: {e}")
        return
    if not is_in_target_forum(message):
        logger.info("Nachricht befindet sich nicht im Ziel-Forum.")
        return
    monitor.record_reaction_add()
    await update_starboard_message(message)

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    logger.info(f"[on_raw_reaction_remove] Reaktion {payload.emoji} von User {payload.user_id} in Channel {payload.channel_id} entfernt.")
    if str(payload.emoji) != STAR_EMOJI:
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        logger.error("Channel nicht gefunden!")
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Nachricht: {e}")
        return
    if not is_in_target_forum(message):
        logger.info("Nachricht befindet sich nicht im Ziel-Forum.")
        return
    monitor.record_reaction_remove()
    await update_starboard_message(message)

# ------------------------
# Bot-Commands
# ------------------------
@bot.command(name="setthreshold")
@commands.has_permissions(administrator=True)
async def set_threshold(ctx, threshold: int):
    global STAR_THRESHOLD
    STAR_THRESHOLD = threshold
    config["star_threshold"] = threshold
    save_config(config)
    logger.info(f"Administrator {ctx.author} hat den Stern-Schwellenwert auf {threshold} gesetzt.")
    await ctx.send(f"Der Stern-Schwellenwert wurde auf {threshold} eingestellt.")

@bot.command(name="setforum")
@commands.has_permissions(administrator=True)
async def set_forum_channel(ctx, *, channel_input: str):
    logger.info(f"setforum wurde aufgerufen mit channel_input: {channel_input}")
    try:
        channel = await commands.GuildChannelConverter().convert(ctx, channel_input)
    except commands.BadArgument as e:
        logger.error(f"Fehler bei der Konvertierung: {e}")
        await ctx.send("Fehler: Ungültiger Parameter. Bitte erwähne den Channel mit #.")
        return

    if channel.type != discord.ChannelType.forum:
        await ctx.send("Fehler: Der angegebene Channel ist kein Forum. Bitte erwähne einen Forum-Channel (z.B. #test-forum).")
        return

    global FORUM_CHANNEL_ID
    FORUM_CHANNEL_ID = channel.id
    config["forum_channel_id"] = channel.id
    save_config(config)
    logger.info(f"Administrator {ctx.author} hat den Forum-Channel auf {channel.id} gesetzt.")
    await ctx.send(f"Forum-Channel-ID wurde auf {channel.id} gesetzt.")

@set_forum_channel.error
async def set_forum_channel_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Fehler: Bitte erwähne den Forum-Channel. Beispiel: `!setforum #mein-forum`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Fehler: Ungültiger Parameter. Bitte erwähne den Channel mit #.")
    else:
        logger.error(f"Unbekannter Fehler im setforum-Command: {error}")
        await ctx.send("Ein unbekannter Fehler ist aufgetreten. Bitte versuche es erneut.")

@bot.command(name="setstarboard")
@commands.has_permissions(administrator=True)
async def set_starboard_channel(ctx, channel: discord.TextChannel):
    global STARBOARD_CHANNEL_ID
    STARBOARD_CHANNEL_ID = channel.id
    config["starboard_channel_id"] = channel.id
    save_config(config)
    
    perms = channel.permissions_for(ctx.guild.me)
    required_perms = {
        "send_messages": perms.send_messages,
        "embed_links": perms.embed_links,
        "manage_messages": perms.manage_messages
    }
    
    missing = [perm for perm, has in required_perms.items() if not has]
    if missing:
        logger.error(f"Bot hat nicht die folgenden notwendigen Berechtigungen im Starboard-Channel {channel.name} (ID: {channel.id}): {', '.join(missing)}")
        await ctx.send(f"Warnung: Der Bot hat nicht alle notwendigen Berechtigungen im Starboard-Channel. Fehlend: {', '.join(missing)}")
    else:
        logger.info(f"Bot hat ausreichende Berechtigungen im Starboard-Channel {channel.name} (ID: {channel.id}).")
    
    await ctx.send(f"Starboard-Channel-ID wurde auf {channel.id} gesetzt.")

@set_starboard_channel.error
async def set_starboard_channel_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Fehler: Bitte erwähne den Starboard-Channel. Beispiel: `!setstarboard #mein-starboard`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Fehler: Ungültiger Parameter. Bitte erwähne den Channel mit #.")
    else:
        logger.error(f"Unbekannter Fehler im setstarboard-Command: {error}")
        await ctx.send("Ein unbekannter Fehler ist aufgetreten. Bitte versuche es erneut.")

# ------------------------
# Custom Help Command
# ------------------------
@bot.command(name="help")
async def custom_help(ctx):
    help_text = (
        "**Verfügbare Commands:**\n\n"
        "**!setthreshold <Wert>**\n"
        "  - Setzt den Stern-Schwellenwert für das Starboard. *(Admin)*\n\n"
        "**!setforum <#Channel>**\n"
        "  - Setzt den Forum-Channel, aus dem Threads als Ziel dienen. *(Admin)*\n\n"
        "**!setstarboard <#Channel>**\n"
        "  - Setzt den Starboard-Channel, in den Starboard-Posts gepostet werden. *(Admin)*\n\n"
        "**!botstats**\n"
        "  - Zeigt aktuelle Performance- und Monitoring-Daten an. *(Admin)*\n\n"
        "**!botchart**\n"
        "  - Zeigt ein Chart der Zeitreihendaten der Starboard-Updates. *(Admin)*\n\n"
        "**!systemchart**\n"
        "  - Zeigt ein Diagramm zur CPU- und RAM-Nutzung des Bots. *(Admin)*\n\n"
        "**!help**\n"
        "  - Zeigt diese Hilfemeldung an.\n"
    )
    await ctx.send(help_text)

# ------------------------
# Botstats Command
# ------------------------
@bot.command(name="botstats")
@commands.has_permissions(administrator=True)
async def bot_stats(ctx):
    stats = monitor.get_stats()
    stats_text = (
        f"**Bot-Statistiken:**\n"
        f"Reaktionen hinzugefügt: {stats['reaction_add_count']}\n"
        f"Reaktionen entfernt: {stats['reaction_remove_count']}\n"
        f"Starboard-Updates: {stats['starboard_updates']}\n"
        f"Durchschnittliche Update-Zeit: {stats['avg_update_time']:.4f} Sekunden\n"
        f"Maximale Update-Zeit: {stats['max_update_time']:.4f} Sekunden\n"
        f"Datenbankabfragen: {stats['db_query_count']}\n"
        f"Gesamte DB-Zeit: {stats['db_total_time']:.4f} Sekunden"
    )
    await ctx.send(stats_text)

# ------------------------
# Botchart Command
# ------------------------
@bot.command(name="botchart")
@commands.has_permissions(administrator=True)
async def bot_chart(ctx):
    if not monitor.history:
        await ctx.send("Keine historischen Daten vorhanden.")
        return

    timestamps, updates = zip(*monitor.history)
    start_time = timestamps[0]
    rel_times = [t - start_time for t in timestamps]

    plt.figure(figsize=(10, 5))
    plt.plot(rel_times, updates, marker="o", label="Starboard-Updates")
    plt.xlabel("Zeit (s) seit Start")
    plt.ylabel("Anzahl Updates")
    plt.title("Verlauf der Starboard-Updates")
    plt.grid(True)
    buf = BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    file = discord.File(fp=buf, filename="botchart.png")
    await ctx.send("Hier ist das aktuelle Starboard-Update-Diagramm:", file=file)

# ------------------------
# Systemchart Command
# ------------------------
@bot.command(name="systemchart")
@commands.has_permissions(administrator=True)
async def system_chart(ctx):
    if not monitor.system_usage:
        await ctx.send("Keine System-Daten vorhanden.")
        return

    timestamps, cpu_vals, mem_vals = zip(*monitor.system_usage)
    start_time = timestamps[0]
    rel_times = [t - start_time for t in timestamps]

    plt.figure(figsize=(10, 6))

    # CPU
    plt.subplot(2, 1, 1)
    plt.plot(rel_times, cpu_vals, marker="o", label="CPU-Auslastung (%)")
    plt.title("Systemressourcen")
    plt.ylabel("CPU %")
    plt.grid(True)
    plt.legend()

    # RAM
    plt.subplot(2, 1, 2)
    plt.plot(rel_times, mem_vals, marker="o", color="orange", label="RAM (MB)")
    plt.xlabel("Zeit (s) seit Start")
    plt.ylabel("RAM (MB)")
    plt.grid(True)
    plt.legend()

    buf = BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    file = discord.File(fp=buf, filename="systemchart.png")
    await ctx.send("Hier ist das Diagramm zur CPU- und RAM-Nutzung:", file=file)

# ------------------------
# Bot starten
# ------------------------
bot.run(TOKEN)
