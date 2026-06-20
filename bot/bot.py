import discord
from discord import app_commands
from discord.ext import tasks
import json
import os
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# Match reminder configuration
WORLDCUP_FILE = os.getenv('WORLDCUP_FILE', 'worldcup.json')
REMINDER_CHANNEL = os.getenv('REMINDER_CHANNEL', 'general')   # channel name (fallback)
# Optional: target a specific channel by ID; takes precedence over the name above.
REMINDER_CHANNEL_ID = int(os.getenv('REMINDER_CHANNEL_ID')) if os.getenv('REMINDER_CHANNEL_ID', '').strip().isdigit() else None
# Optional: role pinged in the 15-minute reminder, and a voice channel whose
# status is set to the match title at kickoff.
REMINDER_ROLE_ID = int(os.getenv('REMINDER_ROLE_ID')) if os.getenv('REMINDER_ROLE_ID', '').strip().isdigit() else None
VOICE_CHANNEL_ID = int(os.getenv('VOICE_CHANNEL_ID')) if os.getenv('VOICE_CHANNEL_ID', '').strip().isdigit() else None
REMINDER_LEAD_MINUTES = [60, 15]                 # remind 1 hour and 15 minutes before
REMINDER_GRACE = timedelta(minutes=10)           # max lateness before a reminder is skipped
RESULT_REMINDER_DELAY = timedelta(hours=2)       # nudge admins to record a result this long after kickoff
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reminder_state.json')

# Results / standings configuration
RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results.json')
# Discord user IDs allowed to enter match results (comma-separated). For now: user "x".
RESULT_ADMIN_IDS = {x.strip() for x in os.getenv('RESULT_ADMIN_IDS', '').split(',') if x.strip()}
# Optional: anyone with this role may also record results.
RESULT_ADMIN_ROLE_ID = int(os.getenv('RESULT_ADMIN_ROLE_ID')) if os.getenv('RESULT_ADMIN_ROLE_ID', '').strip().isdigit() else None

# Prediction game configuration
PREDICTIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'predictions.json')
POINTS_EXACT = 5     # exact scoreline
POINTS_GD = 3        # correct result and goal difference (but not exact)
POINTS_RESULT = 2    # correct result only (right winner / draw)

# Timezone used to decide which calendar day "today"/"tomorrow" falls on
# (hours offset from UTC, e.g. -6 for the host cities). Default: UTC.
DAY_UTC_OFFSET = int(os.getenv('DAY_UTC_OFFSET')) if os.getenv('DAY_UTC_OFFSET', '').lstrip('-').isdigit() else 0
DAY_TZ = timezone(timedelta(hours=DAY_UTC_OFFSET))

# Loaded on_ready
MATCHES = []           # list of (kickoff_utc, match_dict)
MATCHES_BY_MID = {}    # mid -> match_dict (mid is a stable 1-based id we assign)
MATCHES_BY_NUM = {}    # official knockout number (e.g. 74) -> match_dict
GROUPS = {}            # group letter -> list of group-stage match_dicts

# Discord client setup
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True          # required for the `^` text commands
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)  # `/` slash commands

# Optional: sync slash commands to a single guild for instant availability
# (a global sync can take up to an hour to appear). Set GUILD_ID to enable.
GUILD_ID = int(os.getenv('GUILD_ID')) if os.getenv('GUILD_ID', '').strip().isdigit() else None


class Responder:
    """Lets the same handlers reply to either a slash interaction or a channel.
    Exposes .send() like a channel; for an interaction it uses the initial
    response first, then follow-up messages."""

    def __init__(self, interaction, ephemeral=False):
        self.interaction = interaction
        self.ephemeral = ephemeral
        self._responded = False

    async def send(self, content=None, **kwargs):
        # kwargs (files=, file=, view=, …) are forwarded so the same handlers
        # can attach backups or buttons whether replying to a slash or a channel.
        if not self._responded:
            await self.interaction.response.send_message(content, ephemeral=self.ephemeral, **kwargs)
            self._responded = True
        else:
            await self.interaction.followup.send(content, ephemeral=self.ephemeral, **kwargs)


@client.event
async def on_ready():
    global MATCHES
    print(f'Bot connected as {client.user}')
    MATCHES = load_matches()
    print(f'Loaded {len(MATCHES)} matches for reminders')
    # Re-attach the reminder buttons so they keep working across restarts
    # (each custom_id carries the match id, decoded on click).
    client.add_dynamic_items(PredictButton, RecordButton)
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
        else:
            synced = await tree.sync()
        print(f'Synced {len(synced)} slash commands')
    except Exception as e:
        print(f'Slash command sync failed: {e}')
    if not check_reminders.is_running():
        check_reminders.start()

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if not message.content.startswith('^'):
        return
    if message.content.startswith('^next'):
        await send_upcoming(message.channel, limit=1)
        return
    elif message.content.startswith('^matches'):
        parts = message.content.split()
        limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
        await send_upcoming(message.channel, limit=limit)
        return
    elif message.content.startswith('^today'):
        await send_day_matches(message.channel, 0, 'today')
        return
    elif message.content.startswith('^tomorrow') or message.content.startswith('^tmw'):
        await send_day_matches(message.channel, 1, 'tomorrow')
        return
    elif message.content.startswith('^results'):   # must precede ^result
        await send_results(message.channel)
        return
    elif message.content.startswith('^result'):
        await result_text_command(message)
        return
    elif message.content.startswith('^standings'):
        await send_standings(message.channel, message.content.split()[1:])
        return
    elif message.content.startswith('^status'):
        await send_group_status(message.channel)
        return
    elif message.content.startswith('^group'):
        await send_group_detail(message.channel, message.content.split()[1:])
        return
    elif message.content.startswith('^team'):
        await send_team_matches(message.channel, message.content.split()[1:])
        return
    elif message.content.startswith('^qualified'):
        await send_qualified(message.channel)
        return
    elif message.content.startswith('^thirds'):
        await send_thirds(message.channel)
        return
    elif message.content.startswith('^bracket'):
        await send_bracket(message.channel)
        return
    elif message.content.startswith('^predictions'):   # must precede ^predict
        await predictions_text_command(message)
        return
    elif message.content.startswith('^predict'):
        await predict_text_command(message)
        return
    elif message.content.startswith('^leaderboard') or message.content.startswith('^lb'):
        await send_leaderboard(message.channel)
        return
    elif message.content.startswith('^backup'):
        await do_backup(message.author, message.channel)
        return
    elif message.content.startswith('^upload'):
        await upload_text_command(message)
        return
    elif message.content.startswith('^help'):
        await send_help(message.channel)
        return


# ---------------------------------------------------------------------------
# `^` text-command wrappers: parse arguments, then call the shared do_* logic.
# ---------------------------------------------------------------------------

async def predict_text_command(message):
    parts = message.content.split()
    if len(parts) < 4:
        await message.channel.send("Usage: `^predict <id> <home> <away>` — e.g. `^predict 1 2 1`")
        return
    try:
        mid, s1, s2 = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        await message.channel.send("⚠️ Id and scores must be whole numbers.")
        return
    await do_predict(message.author, message.channel, mid, s1, s2)


async def result_text_command(message):
    parts = message.content.split()
    if len(parts) < 4:
        await message.channel.send(
            "Usage: `^result <id> <home> <away> [pen:1|2]` — e.g. `^result 1 2 1`")
        return
    try:
        mid, s1, s2 = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        await message.channel.send("⚠️ Id and scores must be whole numbers.")
        return
    pen = None
    for p in parts[4:]:
        if p.startswith('pen:') and p[4:] in ('1', '2'):
            pen = int(p[4:])
    await do_result(message.author, message.channel, mid, s1, s2, pen)


async def predictions_text_command(message):
    parts = message.content.split()
    match_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    await do_predictions(message.author, message.channel, match_id)


async def upload_text_command(message):
    if not message.attachments:
        await message.channel.send(
            "⚠️ Attach a predictions backup (`.json`) to the `^upload` message.")
        return
    await do_upload(message.author, message.channel, message.attachments[0])


# ---------------------------------------------------------------------------
# `/` slash commands — same handlers, replying via a Responder.
# ---------------------------------------------------------------------------

@tree.command(name="next", description="Show the next upcoming match")
async def slash_next(interaction: discord.Interaction):
    await send_upcoming(Responder(interaction), limit=1)


@tree.command(name="matches", description="Show upcoming matches")
@app_commands.describe(count="How many matches to show (default 5)")
async def slash_matches(interaction: discord.Interaction, count: int = 5):
    await send_upcoming(Responder(interaction), limit=max(1, count))


@tree.command(name="today", description="Matches kicking off today")
async def slash_today(interaction: discord.Interaction):
    await send_day_matches(Responder(interaction), 0, 'today')


@tree.command(name="tomorrow", description="Matches kicking off tomorrow")
async def slash_tomorrow(interaction: discord.Interaction):
    await send_day_matches(Responder(interaction), 1, 'tomorrow')


@tree.command(name="team", description="All matches for a team (name or flag)")
@app_commands.describe(team="Team name or flag emoji, e.g. Brazil or 🇧🇷")
async def slash_team(interaction: discord.Interaction, team: str):
    await send_team_matches(Responder(interaction), [team])


@tree.command(name="status", description="Group progress overview")
async def slash_status(interaction: discord.Interaction):
    await send_group_status(Responder(interaction))


@tree.command(name="group", description="Fixtures and standings for a group")
@app_commands.describe(group="Group letter A–L (leave blank for all)")
async def slash_group(interaction: discord.Interaction, group: str = ""):
    await send_group_detail(Responder(interaction), [group] if group else [])


@tree.command(name="standings", description="Group tables")
@app_commands.describe(group="Group letter A–L (leave blank for all)")
async def slash_standings(interaction: discord.Interaction, group: str = ""):
    await send_standings(Responder(interaction), [group] if group else [])


@tree.command(name="qualified", description="Qualified teams and best third-placed")
async def slash_qualified(interaction: discord.Interaction):
    await send_qualified(Responder(interaction))


@tree.command(name="thirds", description="Best third-placed race — the top 8 of 12 advance")
async def slash_thirds(interaction: discord.Interaction):
    await send_thirds(Responder(interaction))


@tree.command(name="results", description="All recorded match results")
async def slash_results(interaction: discord.Interaction):
    await send_results(Responder(interaction))


@tree.command(name="bracket", description="Knockout bracket with resolved teams")
async def slash_bracket(interaction: discord.Interaction):
    await send_bracket(Responder(interaction))


@tree.command(name="predict", description="Predict a match scoreline before kickoff")
@app_commands.describe(match_id="Match id (from /matches)", home="Home goals", away="Away goals")
async def slash_predict(interaction: discord.Interaction, match_id: int, home: int, away: int):
    await do_predict(interaction.user, Responder(interaction, ephemeral=True), match_id, home, away)


@tree.command(name="predictions", description="Your predictions, or everyone's for a match")
@app_commands.describe(match_id="Match id to show everyone's picks (after kickoff)")
async def slash_predictions(interaction: discord.Interaction, match_id: int = 0):
    await do_predictions(interaction.user, Responder(interaction, ephemeral=(match_id == 0)),
                         match_id or None)


@tree.command(name="leaderboard", description="Prediction standings")
async def slash_leaderboard(interaction: discord.Interaction):
    await send_leaderboard(Responder(interaction))


@tree.command(name="backup", description="Download predictions + results backup (admins only)")
async def slash_backup(interaction: discord.Interaction):
    await do_backup(interaction.user, Responder(interaction, ephemeral=True))


@tree.command(name="upload", description="Restore predictions from a backup file (admins only)")
@app_commands.describe(file="A predictions backup (.json) to restore")
async def slash_upload(interaction: discord.Interaction, file: discord.Attachment):
    await do_upload(interaction.user, Responder(interaction, ephemeral=True), file)


@tree.command(name="result", description="Record a match result (admins only)")
@app_commands.describe(match_id="Match id", home="Home goals", away="Away goals",
                       penalties="Penalty shootout winner (for a knockout draw)")
@app_commands.choices(penalties=[
    app_commands.Choice(name="Home", value=1),
    app_commands.Choice(name="Away", value=2),
])
async def slash_result(interaction: discord.Interaction, match_id: int, home: int, away: int,
                       penalties: app_commands.Choice[int] = None):
    pen = penalties.value if penalties else None
    await do_result(interaction.user, Responder(interaction), match_id, home, away, pen)


@tree.command(name="help", description="List all commands")
async def slash_help(interaction: discord.Interaction):
    await send_help(Responder(interaction))


# ---------------------------------------------------------------------------
# World Cup match reminders
# ---------------------------------------------------------------------------

def _find_worldcup_file():
    """Locate worldcup.json whether the bot runs from /app or the repo root."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        WORLDCUP_FILE,
        os.path.join(here, 'worldcup.json'),
        os.path.join(here, '..', 'worldcup.json'),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return WORLDCUP_FILE


def parse_match_datetime(match):
    """Parse a match's date/time (with its embedded UTC offset) into a UTC datetime."""
    try:
        clock, tz_part = match['time'].split()          # "13:00 UTC-6"
        hour, minute = (int(x) for x in clock.split(':'))
        offset_hours = int(tz_part.upper().replace('UTC', '').replace('GMT', '') or 0)
        tz = timezone(timedelta(hours=offset_hours))
        year, month, day = (int(x) for x in match['date'].split('-'))
        local = datetime(year, month, day, hour, minute, tzinfo=tz)
        return local.astimezone(timezone.utc)
    except (KeyError, ValueError) as e:
        print(f'Could not parse match time: {match} ({e})')
        return None


def load_matches():
    """Read worldcup.json, assign stable ids, build lookup indices, and return
    [(kickoff_utc, match_dict), ...]."""
    global MATCHES_BY_MID, GROUPS, MATCHES_BY_NUM
    MATCHES_BY_MID, GROUPS, MATCHES_BY_NUM = {}, {}, {}
    try:
        with open(_find_worldcup_file(), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f'Could not load worldcup file: {e}')
        return []
    matches = []
    for index, match in enumerate(data.get('matches', []), start=1):
        match['mid'] = index                      # stable id used to report results
        MATCHES_BY_MID[index] = match
        if 'num' in match:                        # official knockout number
            MATCHES_BY_NUM[match['num']] = match
        if match.get('group'):                    # group-stage match
            GROUPS.setdefault(match['group'][-1], []).append(match)
        kickoff = parse_match_datetime(match)
        if kickoff is not None:
            matches.append((kickoff, match))
    return matches


def load_sent_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_sent_state(sent):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(sorted(sent), f)
    except OSError as e:
        print(f'Could not save reminder state: {e}')


# Reminders already sent, persisted so restarts (e.g. watchmedo) don't double-post
sent_reminders = load_sent_state()


def match_key(match, lead):
    return f"{match.get('date')}|{match.get('time')}|{match.get('team1')}|{match.get('team2')}|{lead}"


# Flag emoji for every participating nation. Tokens without a flag (e.g. an
# unresolved "W74" or "2A") fall back to the bare token.
TEAM_FLAGS = {
    'Mexico': '🇲🇽', 'South Africa': '🇿🇦', 'South Korea': '🇰🇷', 'Czech Republic': '🇨🇿',
    'Canada': '🇨🇦', 'Bosnia & Herzegovina': '🇧🇦', 'Qatar': '🇶🇦', 'Switzerland': '🇨🇭',
    'Brazil': '🇧🇷', 'Morocco': '🇲🇦', 'Haiti': '🇭🇹', 'Scotland': '🏴󠁧󠁢󠁳󠁣󠁴󠁿',
    'USA': '🇺🇸', 'Paraguay': '🇵🇾', 'Australia': '🇦🇺', 'Turkey': '🇹🇷',
    'Germany': '🇩🇪', 'Curaçao': '🇨🇼', 'Ivory Coast': '🇨🇮', 'Ecuador': '🇪🇨',
    'Netherlands': '🇳🇱', 'Japan': '🇯🇵', 'Sweden': '🇸🇪', 'Tunisia': '🇹🇳',
    'Belgium': '🇧🇪', 'Egypt': '🇪🇬', 'Iran': '🇮🇷', 'New Zealand': '🇳🇿',
    'Spain': '🇪🇸', 'Cape Verde': '🇨🇻', 'Saudi Arabia': '🇸🇦', 'Uruguay': '🇺🇾',
    'France': '🇫🇷', 'Senegal': '🇸🇳', 'Iraq': '🇮🇶', 'Norway': '🇳🇴',
    'Argentina': '🇦🇷', 'Algeria': '🇩🇿', 'Austria': '🇦🇹', 'Jordan': '🇯🇴',
    'Portugal': '🇵🇹', 'DR Congo': '🇨🇩', 'Uzbekistan': '🇺🇿', 'Colombia': '🇨🇴',
    'England': '🏴󠁧󠁢󠁥󠁮󠁧󠁿', 'Croatia': '🇭🇷', 'Ghana': '🇬🇭', 'Panama': '🇵🇦',
}


def team_label(name):
    """Team name prefixed with its flag, or the bare token if unknown/unresolved."""
    if not name:
        return name
    flag = TEAM_FLAGS.get(name)
    return f"{flag} {name}" if flag else name


def match_team_names(query):
    """Resolve a user query to team name(s): by flag emoji, then exact name
    (case-insensitive), then any names containing the query as a substring."""
    q = query.strip()
    by_flag = [n for n, flag in TEAM_FLAGS.items() if flag == q]
    if by_flag:
        return by_flag
    q = q.lower()
    names = sorted(TEAM_FLAGS)
    exact = [n for n in names if n.lower() == q]
    return exact or [n for n in names if q in n.lower()]


def discord_timestamp(kickoff_utc, style='F'):
    """Render a Discord <t:...> timestamp that each user sees in their own timezone.
    Styles: F=full date/time, f=short, t=time, R=relative ("in 2 hours")."""
    return f"<t:{int(kickoff_utc.timestamp())}:{style}>"


def build_reminder(match, kickoff_utc, lead):
    when = '1 hour' if lead == 60 else f'{lead} minutes'
    team1 = resolve_token(match.get('team1')) or match.get('team1')
    team2 = resolve_token(match.get('team2')) or match.get('team2')
    context = match.get('group') or match.get('round', '')
    ground = match.get('ground', '')
    lines = []
    # Ping the configured role only on the final (15-minute) reminder.
    if lead == 15 and REMINDER_ROLE_ID:
        lines.append(f"<@&{REMINDER_ROLE_ID}>")
    lines += [
        f"⏰ **Match in {when}!**",
        f"🏟️ **{team_label(team1)} vs {team_label(team2)}**" + (f" — {context}" if context else ""),
        f"🕒 Kickoff: **{discord_timestamp(kickoff_utc)}** ({discord_timestamp(kickoff_utc, 'R')})",
    ]
    if ground:
        lines.append(f"📍 {ground}")
    return "\n".join(lines)


def build_record_reminder(match, kickoff_utc):
    """Posted ~2 hours after kickoff to prompt admins to record the result."""
    team1 = resolve_token(match.get('team1')) or match.get('team1')
    team2 = resolve_token(match.get('team2')) or match.get('team2')
    context = match.get('group') or match.get('round', '')
    lines = []
    # Ping whoever may record results, if a role is configured.
    if RESULT_ADMIN_ROLE_ID:
        lines.append(f"<@&{RESULT_ADMIN_ROLE_ID}>")
    lines += [
        "📝 **Time to record a result!**",
        f"🏟️ **{team_label(team1)} vs {team_label(team2)}**" + (f" — {context}" if context else ""),
        f"🕒 Kicked off {discord_timestamp(kickoff_utc, 'R')} · match `#{match['mid']}`",
        "Tap **Record result** below, or use `/result`.",
    ]
    return "\n".join(lines)


def format_match_line(kickoff_utc, match):
    team1 = resolve_token(match.get('team1')) or match.get('team1')
    team2 = resolve_token(match.get('team2')) or match.get('team2')
    context = match.get('group') or match.get('round', '')
    suffix = f" ({context})" if context else ""
    # :F is the absolute kickoff (localized per user); :R is the live countdown.
    return (f"`#{match['mid']:>3}` **{discord_timestamp(kickoff_utc)}** "
            f"({discord_timestamp(kickoff_utc, 'R')}) — "
            f"{team_label(team1)} vs {team_label(team2)}{suffix}")


async def send_upcoming(channel, limit=5):
    now = datetime.now(timezone.utc)
    upcoming = sorted((m for m in MATCHES if m[0] >= now), key=lambda m: m[0])[:limit]
    if not upcoming:
        await channel.send("No upcoming matches. 🏁")
        return
    header = "⚽ **Next match:**" if limit == 1 else f"⚽ **Next {len(upcoming)} matches:**"
    body = "\n".join(format_match_line(kickoff, match) for kickoff, match in upcoming)
    await channel.send(f"{header}\n{body}")


def format_day_line(kickoff_utc, match, results):
    """One fixture line for a day listing: shows the score if a result is in,
    otherwise the kickoff time and live countdown. Day is known, so time only."""
    t1 = resolve_token(match.get('team1'), results) or match.get('team1')
    t2 = resolve_token(match.get('team2'), results) or match.get('team2')
    context = match.get('group') or match.get('round', '')
    suffix = f" · {context}" if context else ""
    res = results.get(str(match['mid']))
    ts = discord_timestamp(kickoff_utc, 't')
    if res:
        return (f"`#{match['mid']:>3}` {ts} — "
                f"{team_label(t1)} **{res['s1']}–{res['s2']}** {team_label(t2)}{suffix}")
    return (f"`#{match['mid']:>3}` {ts} ({discord_timestamp(kickoff_utc, 'R')}) — "
            f"{team_label(t1)} vs {team_label(t2)}{suffix}")


async def send_day_matches(channel, offset_days, label):
    """Matches whose kickoff falls on today (offset 0) or tomorrow (offset 1),
    using DAY_TZ to decide the calendar day."""
    target = (datetime.now(DAY_TZ) + timedelta(days=offset_days)).date()
    day = sorted((m for m in MATCHES if m[0].astimezone(DAY_TZ).date() == target),
                 key=lambda m: m[0])
    if not day:
        await channel.send(f"No matches {label}. 🏁")
        return
    results = load_results()
    lines = [f"⚽ **{label.capitalize()}'s matches** ({len(day)}):"]
    lines += [format_day_line(kickoff, match, results) for kickoff, match in day]
    await send_lines(channel, lines)


def get_reminder_channels():
    # If a specific channel ID is configured, use only that channel.
    if REMINDER_CHANNEL_ID is not None:
        channel = client.get_channel(REMINDER_CHANNEL_ID)
        if channel is None:
            print(f'REMINDER_CHANNEL_ID {REMINDER_CHANNEL_ID} not found')
            return []
        if not channel.permissions_for(channel.guild.me).send_messages:
            print(f'Cannot send messages in channel ID {REMINDER_CHANNEL_ID}')
            return []
        return [channel]
    # Otherwise fall back to matching every text channel by name.
    channels = []
    for guild in client.guilds:
        for channel in guild.text_channels:
            if channel.name == REMINDER_CHANNEL and channel.permissions_for(guild.me).send_messages:
                channels.append(channel)
    return channels


def match_title(match):
    """Plain-text 'TeamA vs TeamB' (with flags) for a voice channel status."""
    results = load_results()
    t1 = resolve_token(match.get('team1'), results) or match.get('team1')
    t2 = resolve_token(match.get('team2'), results) or match.get('team2')
    return f"{team_label(t1)} vs {team_label(t2)}"


async def set_voice_status(title):
    """Set the configured voice channel's status (Discord voice-channel status)."""
    channel = client.get_channel(VOICE_CHANNEL_ID)
    if channel is None:
        print(f'VOICE_CHANNEL_ID {VOICE_CHANNEL_ID} not found')
        return
    try:
        await channel.set_status(title)
        print(f'Set voice status: {title}')
    except AttributeError:
        print('Voice status needs discord.py >= 2.4 (set_status unavailable)')
    except discord.DiscordException as e:
        print(f'Failed to set voice status: {e}')


@tasks.loop(seconds=30)
async def check_reminders():
    now = datetime.now(timezone.utc)
    channels = None
    for kickoff_utc, match in MATCHES:
        # At kickoff, set the voice channel status to the match title.
        if VOICE_CHANNEL_ID and kickoff_utc <= now < kickoff_utc + REMINDER_GRACE:
            start_key = match_key(match, 'start')
            if start_key not in sent_reminders:
                await set_voice_status(match_title(match))
                sent_reminders.add(start_key)
                save_sent_state(sent_reminders)
        for lead in REMINDER_LEAD_MINUTES:
            remind_at = kickoff_utc - timedelta(minutes=lead)
            # Fire only inside [remind_at, remind_at + grace); skips long-past matches on startup
            if not (remind_at <= now < remind_at + REMINDER_GRACE):
                continue
            key = match_key(match, lead)
            if key in sent_reminders:
                continue
            if channels is None:
                channels = get_reminder_channels()
            if not channels:
                print(f'No #{REMINDER_CHANNEL} channel available to send reminders')
                return
            message = build_reminder(match, kickoff_utc, lead)
            for channel in channels:
                try:
                    # A fresh view per send; the button lets users predict inline.
                    await channel.send(message, view=predict_view(match['mid']))
                    print(f'Sent reminder to #{channel.name}: {key}')
                except discord.DiscordException as e:
                    print(f'Failed to send reminder: {e}')
            sent_reminders.add(key)
            save_sent_state(sent_reminders)

        # Two hours after kickoff, nudge admins (once) to record the result,
        # unless it's already in. A button opens a modal to enter the score.
        record_at = kickoff_utc + RESULT_REMINDER_DELAY
        if (record_at <= now < record_at + REMINDER_GRACE
                and match_key(match, 'record') not in sent_reminders
                and str(match['mid']) not in load_results()):
            if channels is None:
                channels = get_reminder_channels()
            if channels:
                message = build_record_reminder(match, kickoff_utc)
                for channel in channels:
                    try:
                        await channel.send(message, view=record_view(match['mid']))
                        print(f"Sent record reminder to #{channel.name}: {match_key(match, 'record')}")
                    except discord.DiscordException as e:
                        print(f'Failed to send record reminder: {e}')
                sent_reminders.add(match_key(match, 'record'))
                save_sent_state(sent_reminders)


@check_reminders.before_loop
async def before_check_reminders():
    await client.wait_until_ready()


# ---------------------------------------------------------------------------
# Results, standings and knockout-bracket resolution
#
# Knockout slots in worldcup.json use placeholder tokens:
#   1A / 2A           -> winner / runner-up of group A
#   3A/B/C/D/F        -> one of the 8 best third-placed teams, from those groups
#   W74 / L101        -> winner / loser of match number 74 / 101
# Real team names (e.g. "Mexico") are returned as-is. Standings are computed
# from results entered with ^result; positions are only treated as final once
# every match in a group has a result.
# ---------------------------------------------------------------------------

RE_POS = re.compile(r'^([12])([A-L])$')
RE_THIRD = re.compile(r'^3[A-L](?:/[A-L])+$')
RE_WIN = re.compile(r'^W(\d+)$')
RE_LOSE = re.compile(r'^L(\d+)$')


def load_results():
    try:
        with open(RESULTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_results(results):
    try:
        with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2)
    except OSError as e:
        print(f'Could not save results: {e}')


def group_standings(group_letter, results):
    """Return ranked rows for a group (best first)."""
    table = {}

    def row(team):
        return table.setdefault(team, {
            'team': team, 'p': 0, 'w': 0, 'd': 0, 'l': 0,
            'gf': 0, 'ga': 0, 'pts': 0,
        })

    for match in GROUPS.get(group_letter, []):
        a, b = row(match['team1']), row(match['team2'])
        res = results.get(str(match['mid']))
        if not res:
            continue
        s1, s2 = res['s1'], res['s2']
        a['p'] += 1; b['p'] += 1
        a['gf'] += s1; a['ga'] += s2
        b['gf'] += s2; b['ga'] += s1
        if s1 > s2:
            a['w'] += 1; a['pts'] += 3; b['l'] += 1
        elif s2 > s1:
            b['w'] += 1; b['pts'] += 3; a['l'] += 1
        else:
            a['d'] += 1; b['d'] += 1; a['pts'] += 1; b['pts'] += 1

    rows = list(table.values())
    for r in rows:
        r['gd'] = r['gf'] - r['ga']
    # Tiebreakers: points, goal difference, goals for, then name (approximation
    # of FIFA's full criteria, which also include head-to-head and fair play).
    rows.sort(key=lambda r: (-r['pts'], -r['gd'], -r['gf'], r['team']))
    return rows


def group_complete(group_letter, results):
    matches = GROUPS.get(group_letter, [])
    return bool(matches) and all(str(m['mid']) in results for m in matches)


def third_place_slots():
    """All distinct '3X/Y/...' placeholder tokens and their allowed group sets."""
    slots = {}
    for match in MATCHES_BY_MID.values():
        for token in (match.get('team1'), match.get('team2')):
            if token and RE_THIRD.match(token):
                slots[token] = set(token[1:].split('/'))
    return slots


def assign_third_places(results):
    """Map each '3X/Y/...' slot to a best-third team via a valid matching.

    Only computed once all 12 groups are complete. The 8 best third-placed teams
    are matched to the 8 slots so each team lands in a slot whose allowed groups
    include its own (a valid assignment; FIFA's official table is one such)."""
    if not all(group_complete(g, results) for g in GROUPS) or len(GROUPS) < 12:
        return {}

    thirds = []
    for g in GROUPS:
        rows = group_standings(g, results)
        if len(rows) >= 3:
            thirds.append((rows[2], g))
    thirds.sort(key=lambda t: (-t[0]['pts'], -t[0]['gd'], -t[0]['gf'], t[0]['team']))
    best = thirds[:8]

    slots = list(third_place_slots().items())   # [(token, allowed_groups), ...]
    if len(best) < len(slots):
        return {}

    assignment, used = {}, [False] * len(best)

    def backtrack(i):
        if i == len(slots):
            return True
        token, allowed = slots[i]
        for j, (rowstat, group) in enumerate(best):
            if not used[j] and group in allowed:
                used[j] = True
                assignment[token] = rowstat['team']
                if backtrack(i + 1):
                    return True
                used[j] = False
                del assignment[token]
        return False

    return assignment if backtrack(0) else {}


def resolve_token(token, results=None, _seen=None):
    """Resolve a placeholder token to a concrete team name, or None if undecided."""
    if not token:
        return None
    if results is None:
        results = load_results()

    pos = RE_POS.match(token)
    if pos:
        rank, group = int(pos.group(1)), pos.group(2)
        if not group_complete(group, results):
            return None
        rows = group_standings(group, results)
        return rows[rank - 1]['team'] if len(rows) >= rank else None

    if RE_THIRD.match(token):
        return assign_third_places(results).get(token)

    win = RE_WIN.match(token)
    if win:
        return _match_outcome(MATCHES_BY_NUM.get(int(win.group(1))), results, _seen, loser=False)

    lose = RE_LOSE.match(token)
    if lose:
        return _match_outcome(MATCHES_BY_NUM.get(int(lose.group(1))), results, _seen, loser=True)

    return token   # already a real team name


def _match_outcome(match, results, _seen, loser):
    """Winner (or loser) of a knockout match, resolving its teams recursively."""
    if match is None:
        return None
    _seen = _seen or set()
    if match['mid'] in _seen:      # guard against malformed cyclic references
        return None
    _seen = _seen | {match['mid']}

    t1 = resolve_token(match.get('team1'), results, _seen)
    t2 = resolve_token(match.get('team2'), results, _seen)
    res = results.get(str(match['mid']))
    if not res or not t1 or not t2:
        return None

    s1, s2 = res['s1'], res['s2']
    if s1 != s2:
        winner = t1 if s1 > s2 else t2
    else:                          # knockout draw -> decided on penalties
        pen = res.get('pen')
        winner = t1 if pen == 1 else t2 if pen == 2 else None
    if winner is None:
        return None
    if loser:
        return t2 if winner == t1 else t1
    return winner


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def is_result_admin(user):
    if str(user.id) in RESULT_ADMIN_IDS:
        return True
    if RESULT_ADMIN_ROLE_ID is not None:
        return any(role.id == RESULT_ADMIN_ROLE_ID for role in getattr(user, 'roles', []))
    return False


async def do_result(user, responder, mid, s1, s2, pen):
    """Record a match result (admins only). Shared by `/result` and `^result`."""
    if not is_result_admin(user):
        await responder.send("🚫 You are not allowed to enter results.")
        return
    if s1 < 0 or s2 < 0:
        await responder.send("⚠️ Scores cannot be negative.")
        return

    match = MATCHES_BY_MID.get(mid)
    if match is None:
        await responder.send(f"⚠️ No match with id `#{mid}`.")
        return

    is_knockout = not match.get('group')
    if is_knockout and s1 == s2 and pen is None:
        await responder.send(
            "⚠️ Knockout match can't end level — set the penalty winner "
            "(`/result` penalties option, or `pen:1` / `pen:2` with `^result`).")
        return

    results = load_results()
    entry = {'s1': s1, 's2': s2}
    if pen is not None:
        entry['pen'] = pen
    results[str(mid)] = entry
    save_results(results)

    t1 = resolve_token(match.get('team1'), results) or match.get('team1')
    t2 = resolve_token(match.get('team2'), results) or match.get('team2')
    line = f"✅ Recorded: **{team_label(t1)} {s1}–{s2} {team_label(t2)}**"
    if pen is not None:
        line += f" (pens: {'home' if pen == 1 else 'away'} win)"

    entries = load_predictions().get(str(mid), {})
    if entries:
        res = results[str(mid)]
        # Ping (not just name) whoever nailed the exact scoreline.
        winners = [uid for uid, p in entries.items()
                   if score_prediction(p, res) == POINTS_EXACT]
        line += f"\n🎯 {len(entries)} prediction(s) scored — see the leaderboard."
        if winners:
            mentions = ', '.join(f"<@{uid}>" for uid in winners)
            line += f" Exact score by: {mentions} 🎉"
    await responder.send(line)


def format_standings_block(group_letter, results):
    rows = group_standings(group_letter, results)
    if not rows:
        return None
    status = "" if group_complete(group_letter, results) else "  (in progress)"
    lines = [f"**Group {group_letter}**{status}", "```",
             f"{'#':<2}{'Team':<22}{'P':>2}{'W':>2}{'D':>2}{'L':>2}{'GF':>3}{'GA':>3}{'GD':>4}{'Pts':>4}"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i:<2}{r['team']:<22}{r['p']:>2}{r['w']:>2}{r['d']:>2}"
                     f"{r['l']:>2}{r['gf']:>3}{r['ga']:>3}{r['gd']:>4}{r['pts']:>4}")
    lines.append("```")
    return "\n".join(lines)


async def send_standings(channel, args):
    results = load_results()
    groups = [a.upper() for a in args if a.upper() in GROUPS] or sorted(GROUPS)
    blocks = [b for g in groups if (b := format_standings_block(g, results))]
    if not blocks:
        await channel.send("No group data yet.")
        return
    # Send a few groups per message to stay under Discord's 2000-char limit.
    chunk = ""
    for block in blocks:
        if len(chunk) + len(block) > 1800:
            await channel.send(chunk)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + block
    if chunk:
        await channel.send(chunk)


def format_group_fixtures(group_letter, results):
    lines = [f"**Group {group_letter} — Fixtures**"]
    for m in GROUPS[group_letter]:
        kickoff = parse_match_datetime(m)
        ts = discord_timestamp(kickoff) if kickoff else ''
        t1, t2 = team_label(m['team1']), team_label(m['team2'])
        res = results.get(str(m['mid']))
        if res:
            lines.append(f"`#{m['mid']:>3}` {ts} — {t1} **{res['s1']}–{res['s2']}** {t2}")
        else:
            lines.append(f"`#{m['mid']:>3}` {ts} — {t1} vs {t2}")
    return "\n".join(lines)


async def send_group_detail(channel, args):
    """Fixtures (with results) and standings table for one or all groups."""
    results = load_results()
    groups = [a.upper() for a in args if a.upper() in GROUPS] or sorted(GROUPS)
    for g in groups:
        fixtures = format_group_fixtures(g, results)
        table = format_standings_block(g, results)
        await channel.send(f"{fixtures}\n\n{table}")


async def send_team_matches(channel, args):
    """All matches for a team — group fixtures plus any knockout ties reached."""
    query = " ".join(args).strip()
    if not query:
        await channel.send("Usage: `^team <name|flag>` — e.g. `^team Brazil` or `^team 🇧🇷`")
        return

    candidates = match_team_names(query)
    if not candidates:
        await channel.send(f"⚠️ No team matching “{query}”. Try the full name, "
                           "e.g. `^team South Korea`.")
        return
    if len(candidates) > 1:
        await channel.send("Did you mean: " + ", ".join(candidates) + " ?")
        return

    team = candidates[0]
    results = load_results()
    lines = [f"{team_label(team)} — Matches\n"]
    for kickoff_utc, match in sorted(MATCHES, key=lambda m: m[0]):
        t1 = resolve_token(match.get('team1'), results) or match.get('team1')
        t2 = resolve_token(match.get('team2'), results) or match.get('team2')
        if team not in (t1, t2):
            continue
        ts = discord_timestamp(kickoff_utc)
        context = match.get('group') or match.get('round', '')
        res = results.get(str(match['mid']))
        if res:
            body = f"{team_label(t1)} **{res['s1']}–{res['s2']}** {team_label(t2)}"
            when = ts
        else:
            body = f"{team_label(t1)} vs {team_label(t2)}"
            when = f"{ts} ({discord_timestamp(kickoff_utc, 'R')})"
        suffix = f" · {context}" if context else ""
        lines.append(f"`#{match['mid']:>3}` {when} — {body}{suffix}")

    if len(lines) == 1:
        lines.append("_No matches scheduled yet._")
    await channel.send("\n".join(lines))


async def send_group_status(channel):
    results = load_results()
    lines = ["📋 **Group status**\n"]
    for g in sorted(GROUPS):
        matches = GROUPS[g]
        total = len(matches)
        played = sum(1 for m in matches if str(m['mid']) in results)
        rows = group_standings(g, results)
        if played == 0:
            lines.append(f"⚪ **Group {g}** — not started")
        elif played == total:
            lines.append(f"✅ **Group {g}** — complete · "
                         f"🥇 {team_label(rows[0]['team'])}, 🥈 {team_label(rows[1]['team'])}")
        else:
            leaders = ", ".join(team_label(r['team']) for r in rows[:2] if r['p'] > 0)
            lines.append(f"🟡 **Group {g}** — {played}/{total} played"
                         + (f" · leaders: {leaders}" if leaders else ""))
    await channel.send("\n".join(lines))


async def send_qualified(channel):
    results = load_results()
    lines = ["🏆 **Qualified teams**\n"]
    for g in sorted(GROUPS):
        if group_complete(g, results):
            rows = group_standings(g, results)
            lines.append(f"**Group {g}:** 🥇 {team_label(rows[0]['team'])}  "
                         f"🥈 {team_label(rows[1]['team'])}  (3rd: {team_label(rows[2]['team'])})")
        else:
            lines.append(f"**Group {g}:** _group not finished_")

    thirds = assign_third_places(results)
    if thirds:
        qualified_thirds = ", ".join(team_label(t) for t in sorted(set(thirds.values())))
        lines.append(f"\n**Best third-placed (qualified):** {qualified_thirds}")
    else:
        lines.append("\n**Best third-placed:** _pending all groups finishing_")
    await channel.send("\n".join(lines))


async def send_thirds(channel):
    """The race for the 8 best third-placed spots, ranked live across groups.

    Shows each group's current 3rd-placed team; the top 8 (by points, goal
    difference, goals for) are in qualifying position. Marked provisional until
    every group has finished."""
    results = load_results()
    ranked, pending = [], []
    for g in sorted(GROUPS):
        if not any(str(m['mid']) in results for m in GROUPS[g]):
            pending.append(g)                 # not started — 3rd place undefined
            continue
        rows = group_standings(g, results)
        if len(rows) >= 3:
            ranked.append({**rows[2], 'group': g, 'final': group_complete(g, results)})
    ranked.sort(key=lambda r: (-r['pts'], -r['gd'], -r['gf'], r['team']))

    if not ranked:
        await channel.send("🥉 No group results yet — the third-placed race hasn't started.")
        return

    all_final = not pending and all(group_complete(g, results) for g in GROUPS)
    header = "🥉 **Best third-placed race** — the top 8 of 12 advance"
    if not all_final:
        header += "  _(provisional)_"
    lines = [header]
    for i, r in enumerate(ranked, start=1):
        mark = "✅" if i <= 8 else "❌"
        flag = "" if r['final'] else " ⏳"
        lines.append(f"{mark} `{i:>2}` {team_label(r['team'])} — Grp {r['group']} · "
                     f"{r['pts']} pts · GD {r['gd']:+d} · GF {r['gf']}{flag}")
    if pending:
        lines.append("\n⚪ Not started: " + ", ".join(f"Grp {g}" for g in pending))
    if not all_final:
        lines.append("_⏳ = group still in progress; standings can still change._")
    await send_lines(channel, lines)


async def send_results(channel):
    """List every recorded match result, in kickoff order."""
    results = load_results()
    recorded = [(k, m) for k, m in sorted(MATCHES, key=lambda x: x[0])
                if str(m['mid']) in results]
    if not recorded:
        await channel.send("No results recorded yet.")
        return
    lines = [f"📋 **Recorded results** ({len(recorded)})"]
    for _, match in recorded:
        res = results[str(match['mid'])]
        t1 = resolve_token(match.get('team1'), results) or match.get('team1')
        t2 = resolve_token(match.get('team2'), results) or match.get('team2')
        pen = ""
        if res.get('pen'):
            pen = f" _(pens {'home' if res['pen'] == 1 else 'away'})_"
        context = match.get('group') or match.get('round', '')
        suffix = f" · {context}" if context else ""
        lines.append(f"`#{match['mid']:>3}` {team_label(t1)} **{res['s1']}–{res['s2']}** "
                     f"{team_label(t2)}{pen}{suffix}")
    await send_lines(channel, lines)


async def send_bracket(channel):
    results = load_results()
    knockout = sorted(
        (m for m in MATCHES_BY_MID.values() if not m.get('group')),
        key=lambda m: (m.get('num', 0), m['mid']))
    if not knockout:
        await channel.send("No knockout matches found.")
        return

    by_round, order = {}, []
    for m in knockout:
        rnd = m.get('round', 'Knockout')
        if rnd not in by_round:
            by_round[rnd] = []
            order.append(rnd)
        t1 = resolve_token(m.get('team1'), results) or m['team1']
        t2 = resolve_token(m.get('team2'), results) or m['team2']
        res = results.get(str(m['mid']))
        score = ""
        if res:
            score = f"  **{res['s1']}–{res['s2']}**"
            if res.get('pen'):
                score += f" (pens {'home' if res['pen'] == 1 else 'away'})"
        by_round[rnd].append(f"`#{m['mid']:>3}` {team_label(t1)} vs {team_label(t2)}{score}")

    chunk = ""
    for rnd in order:
        block = f"**{rnd}**\n" + "\n".join(by_round[rnd])
        if len(chunk) + len(block) > 1800:
            await channel.send(chunk)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + block
    if chunk:
        await channel.send(chunk)


# ---------------------------------------------------------------------------
# Prediction game (sweepstake)
#
# Anyone may predict the scoreline of a match before it kicks off. When the
# result is entered, predictions are scored:
#   exact scoreline            -> POINTS_EXACT
#   right result + right GD    -> POINTS_GD
#   right result only          -> POINTS_RESULT
# The leaderboard totals everyone's points; most points by the final wins.
# ---------------------------------------------------------------------------

def load_predictions():
    try:
        with open(PREDICTIONS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_predictions(predictions):
    try:
        with open(PREDICTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(predictions, f, indent=2)
    except OSError as e:
        print(f'Could not save predictions: {e}')


def score_prediction(pred, res):
    """Points a prediction earns against a final result."""
    ps1, ps2, rs1, rs2 = pred['s1'], pred['s2'], res['s1'], res['s2']
    if ps1 == rs1 and ps2 == rs2:
        return POINTS_EXACT
    sign = lambda a, b: (a > b) - (a < b)        # 1 home win, 0 draw, -1 away win
    if sign(ps1, ps2) != sign(rs1, rs2):
        return 0                                  # wrong result
    # Goal-difference bonus rewards predicting the winning margin (non-draws only;
    # every draw trivially has GD 0, so it would otherwise always earn the bonus).
    if ps1 != ps2 and (ps1 - ps2) == (rs1 - rs2):
        return POINTS_GD                          # right result and goal difference
    return POINTS_RESULT                          # right result only


def compute_leaderboard(predictions, results):
    totals = {}
    for mid, entries in predictions.items():
        res = results.get(mid)
        if not res:
            continue
        for uid, pred in entries.items():
            row = totals.setdefault(uid, {'name': uid, 'pts': 0, 'exact': 0, 'scored': 0})
            row['name'] = pred.get('name', row['name'])
            pts = score_prediction(pred, res)
            row['pts'] += pts
            row['scored'] += 1
            if pts == POINTS_EXACT:
                row['exact'] += 1
    return sorted(totals.values(), key=lambda r: (-r['pts'], -r['exact'], r['name'].lower()))


async def send_lines(channel, lines, limit=1900):
    """Send a list of lines as one or more messages within Discord's size limit."""
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > limit:
            await channel.send(chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk:
        await channel.send(chunk)


def predictor_name(author):
    # Use the Discord username (stable handle), not the server display name.
    return getattr(author, 'name', None) or str(author.id)


async def do_predict(user, responder, mid, s1, s2):
    """Submit/replace a prediction before kickoff. Shared by `/predict` and `^predict`."""
    if s1 < 0 or s2 < 0:
        await responder.send("⚠️ Scores cannot be negative.")
        return

    match = MATCHES_BY_MID.get(mid)
    if match is None:
        await responder.send(f"⚠️ No match with id `#{mid}`.")
        return
    kickoff = parse_match_datetime(match)
    if kickoff is None:
        await responder.send("⚠️ This match has no scheduled time.")
        return
    if datetime.now(timezone.utc) >= kickoff:
        await responder.send(f"🔒 Predictions for `#{mid}` are closed — the match has started.")
        return

    predictions = load_predictions()
    predictions.setdefault(str(mid), {})[str(user.id)] = {
        's1': s1, 's2': s2, 'name': predictor_name(user),
    }
    save_predictions(predictions)

    t1 = resolve_token(match.get('team1')) or match.get('team1')
    t2 = resolve_token(match.get('team2')) or match.get('team2')
    await responder.send(
        f"✅ Prediction saved: **{team_label(t1)} {s1}–{s2} {team_label(t2)}** "
        f"(locks {discord_timestamp(kickoff, 'R')})")


async def do_predictions(user, responder, match_id=None):
    """match_id given  -> everyone's predictions for that match (after kickoff).
       match_id None   -> the caller's own predictions and points."""
    predictions = load_predictions()
    results = load_results()

    if match_id is not None:
        mid = match_id
        match = MATCHES_BY_MID.get(mid)
        if match is None:
            await responder.send(f"⚠️ No match with id `#{mid}`.")
            return
        entries = predictions.get(str(mid), {})
        kickoff = parse_match_datetime(match)
        not_started = kickoff and datetime.now(timezone.utc) < kickoff
        # Reveal once the match has kicked off, or once a result exists.
        if not_started and str(mid) not in results:
            await responder.send(
                f"🔒 Predictions for `#{mid}` are hidden until kickoff "
                f"({len(entries)} so far, locks {discord_timestamp(kickoff, 'R')}).")
            return
        if not entries:
            await responder.send(f"No predictions were made for `#{mid}`.")
            return
        t1 = resolve_token(match.get('team1'), results) or match.get('team1')
        t2 = resolve_token(match.get('team2'), results) or match.get('team2')
        res = results.get(str(mid))
        head = f"**Predictions — {team_label(t1)} vs {team_label(t2)}**"
        if res:
            head += f"  ·  actual **{res['s1']}–{res['s2']}**"
        rows = sorted(entries.values(),
                      key=lambda p: -(score_prediction(p, res) if res else 0))
        lines = [head]
        for p in rows:
            tag = f" · **{score_prediction(p, res)} pts**" if res else ""
            lines.append(f"• {p.get('name', '?')}: {p['s1']}–{p['s2']}{tag}")
        await send_lines(responder, lines)
        return

    # No id: caller's own predictions
    uid = str(user.id)
    mine = sorted(((int(mid), entries[uid]) for mid, entries in predictions.items()
                   if uid in entries), key=lambda x: x[0])
    if not mine:
        await responder.send(
            "You have no predictions yet. Use `/predict` or `^predict <id> <home> <away>`.")
        return
    lines = [f"**{predictor_name(user)}'s predictions**"]
    total = 0
    for mid, p in mine:
        match = MATCHES_BY_MID.get(mid)
        t1 = resolve_token(match.get('team1'), results) or match.get('team1')
        t2 = resolve_token(match.get('team2'), results) or match.get('team2')
        res = results.get(str(mid))
        if res:
            pts = score_prediction(p, res)
            total += pts
            tag = f" · **{pts} pts** (actual {res['s1']}–{res['s2']})"
        else:
            tag = " · _pending_"
        lines.append(f"`#{mid:>3}` {team_label(t1)} {p['s1']}–{p['s2']} {team_label(t2)}{tag}")
    lines.append(f"\n**Total: {total} pts**")
    await send_lines(responder, lines)


async def send_leaderboard(channel):
    board = compute_leaderboard(load_predictions(), load_results())
    if not board:
        await channel.send("🏅 No predictions have been scored yet. "
                           "Make picks with `^predict <id> <home> <away>`.")
        return
    medals = ['🥇', '🥈', '🥉']
    lines = ["🏅 **Prediction leaderboard**\n"]
    for i, row in enumerate(board):
        rank = medals[i] if i < 3 else f"`{i + 1}.`"
        lines.append(f"{rank} **{row['name']}** — {row['pts']} pts "
                     f"({row['exact']} exact · {row['scored']} scored)")
    await send_lines(channel, lines)


# ---------------------------------------------------------------------------
# Reminder "Predict" button
#
# Reminder messages carry a button whose custom_id encodes the match id
# (`predict:<mid>`). Clicking it opens a modal to enter the scoreline, which
# feeds the same do_predict() used by the slash/text commands. The DynamicItem
# is registered in on_ready so the buttons survive bot restarts.
# ---------------------------------------------------------------------------

class PredictModal(discord.ui.Modal):
    def __init__(self, mid, match):
        super().__init__(title=f"Predict — match #{mid}")
        self.mid = mid
        t1 = resolve_token(match.get('team1')) or match.get('team1') or 'Home'
        t2 = resolve_token(match.get('team2')) or match.get('team2') or 'Away'
        # Modal labels can't render flag emoji reliably, so use plain names (max 45 chars).
        self.home = discord.ui.TextInput(label=f"{t1} goals"[:45], placeholder="e.g. 2", max_length=2)
        self.away = discord.ui.TextInput(label=f"{t2} goals"[:45], placeholder="e.g. 1", max_length=2)
        self.add_item(self.home)
        self.add_item(self.away)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            s1, s2 = int(self.home.value), int(self.away.value)
        except ValueError:
            await interaction.response.send_message("⚠️ Scores must be whole numbers.", ephemeral=True)
            return
        await do_predict(interaction.user, Responder(interaction, ephemeral=True), self.mid, s1, s2)


class PredictButton(discord.ui.DynamicItem[discord.ui.Button], template=r'predict:(?P<mid>\d+)'):
    def __init__(self, mid):
        self.mid = mid
        super().__init__(discord.ui.Button(
            label='Predict', emoji='🎯',
            style=discord.ButtonStyle.primary,
            custom_id=f'predict:{mid}',
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match['mid']))

    async def callback(self, interaction: discord.Interaction):
        match = MATCHES_BY_MID.get(self.mid)
        if match is None:
            await interaction.response.send_message(f"⚠️ No match with id `#{self.mid}`.", ephemeral=True)
            return
        kickoff = parse_match_datetime(match)
        if kickoff and datetime.now(timezone.utc) >= kickoff:
            await interaction.response.send_message(
                f"🔒 Predictions for `#{self.mid}` are closed — the match has started.", ephemeral=True)
            return
        await interaction.response.send_modal(PredictModal(self.mid, match))


def predict_view(mid):
    """A view holding the reminder's Predict button for the given match."""
    view = discord.ui.View(timeout=None)
    view.add_item(PredictButton(mid))
    return view


# Penalty-winner words accepted in the record modal's optional field.
_PEN_HOME = {'1', 'h', 'home'}
_PEN_AWAY = {'2', 'a', 'away'}


class RecordModal(discord.ui.Modal):
    def __init__(self, mid, match):
        super().__init__(title=f"Record result — match #{mid}")
        self.mid = mid
        t1 = resolve_token(match.get('team1')) or match.get('team1') or 'Home'
        t2 = resolve_token(match.get('team2')) or match.get('team2') or 'Away'
        self.home = discord.ui.TextInput(label=f"{t1} goals"[:45], placeholder="e.g. 2", max_length=2)
        self.away = discord.ui.TextInput(label=f"{t2} goals"[:45], placeholder="e.g. 1", max_length=2)
        # Only needed when a knockout tie ends level; left blank otherwise.
        self.pen = discord.ui.TextInput(
            label="Penalty winner (knockout draw only)"[:45],
            placeholder="home / away — leave blank if not needed",
            required=False, max_length=4)
        self.add_item(self.home)
        self.add_item(self.away)
        self.add_item(self.pen)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            s1, s2 = int(self.home.value), int(self.away.value)
        except ValueError:
            await interaction.response.send_message("⚠️ Scores must be whole numbers.", ephemeral=True)
            return
        pen_raw = (self.pen.value or '').strip().lower()
        if pen_raw and pen_raw not in _PEN_HOME and pen_raw not in _PEN_AWAY:
            await interaction.response.send_message(
                "⚠️ Penalty winner must be `home` or `away` (or left blank).", ephemeral=True)
            return
        pen = 1 if pen_raw in _PEN_HOME else 2 if pen_raw in _PEN_AWAY else None
        await do_result(interaction.user, Responder(interaction), self.mid, s1, s2, pen)


class RecordButton(discord.ui.DynamicItem[discord.ui.Button], template=r'record:(?P<mid>\d+)'):
    def __init__(self, mid):
        self.mid = mid
        super().__init__(discord.ui.Button(
            label='Record result', emoji='📝',
            style=discord.ButtonStyle.success,
            custom_id=f'record:{mid}',
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match['mid']))

    async def callback(self, interaction: discord.Interaction):
        if not is_result_admin(interaction.user):
            await interaction.response.send_message("🚫 You are not allowed to enter results.", ephemeral=True)
            return
        match = MATCHES_BY_MID.get(self.mid)
        if match is None:
            await interaction.response.send_message(f"⚠️ No match with id `#{self.mid}`.", ephemeral=True)
            return
        await interaction.response.send_modal(RecordModal(self.mid, match))


def record_view(mid):
    """A view holding the 'Record result' button for the given match."""
    view = discord.ui.View(timeout=None)
    view.add_item(RecordButton(mid))
    return view


# ---------------------------------------------------------------------------
# Backup / restore of the prediction game data (admins only)
# ---------------------------------------------------------------------------

async def do_backup(user, responder):
    """Send predictions.json and results.json as downloadable attachments."""
    if not is_result_admin(user):
        await responder.send("🚫 You are not allowed to download backups.")
        return
    files = []
    for path, name in ((PREDICTIONS_FILE, 'predictions.json'), (RESULTS_FILE, 'results.json')):
        if os.path.exists(path):
            files.append(discord.File(path, filename=name))
    if not files:
        await responder.send("No data to back up yet — no predictions or results recorded.")
        return
    await responder.send("🗄️ **Backup** — predictions and results:", files=files)


def validate_predictions(data):
    """Check an uploaded object matches the predictions shape: {mid: {uid: {s1,s2,name}}}."""
    if not isinstance(data, dict):
        return False, "Top level must be an object mapping match id → predictions."
    for mid, entries in data.items():
        if not str(mid).isdigit():
            return False, f"Match id `{mid}` is not a number."
        if not isinstance(entries, dict):
            return False, f"Predictions for match `{mid}` must be an object."
        for uid, pred in entries.items():
            if not isinstance(pred, dict) or 's1' not in pred or 's2' not in pred:
                return False, f"Prediction for match `{mid}` / user `{uid}` is missing s1/s2."
            if not isinstance(pred['s1'], int) or not isinstance(pred['s2'], int):
                return False, f"Scores for match `{mid}` / user `{uid}` must be whole numbers."
    return True, "ok"


async def do_upload(user, responder, attachment):
    """Restore predictions from an uploaded backup file (replaces the current file)."""
    if not is_result_admin(user):
        await responder.send("🚫 You are not allowed to upload predictions.")
        return
    if attachment is None:
        await responder.send("⚠️ Attach a predictions backup (`.json`) to upload.")
        return
    try:
        raw = await attachment.read()
        data = json.loads(raw.decode('utf-8'))
    except (discord.DiscordException, UnicodeDecodeError, json.JSONDecodeError) as e:
        await responder.send(f"⚠️ Couldn't read `{attachment.filename}` as JSON: {e}")
        return
    ok, msg = validate_predictions(data)
    if not ok:
        await responder.send(f"⚠️ That file isn't a valid predictions backup — {msg}")
        return
    save_predictions(data)
    n_preds = sum(len(v) for v in data.values())
    await responder.send(
        f"✅ Restored predictions: **{n_preds}** prediction(s) across **{len(data)}** match(es).")


async def send_help(channel):
    await channel.send(
        "**World Cup bot** — every command works as a slash `/cmd` or text `^cmd`\n"
        "`/next` · `^next` — next match\n"
        "`/matches [count]` · `^matches [n]` — upcoming matches (default 5)\n"
        "`/today` · `^today` — matches kicking off today\n"
        "`/tomorrow` · `^tomorrow` (`^tmw`) — matches kicking off tomorrow\n"
        "`/team <name|flag>` · `^team …` — a team's matches (e.g. Brazil or 🇧🇷)\n"
        "`/status` · `^status` — group progress at a glance\n"
        "`/group [letter]` · `^group …` — fixtures + standings per group\n"
        "`/standings [group]` · `^standings …` — group tables\n"
        "`/qualified` · `^qualified` — winners/runners-up and best third-placed\n"
        "`/thirds` · `^thirds` — best third-placed race (top 8 of 12 advance)\n"
        "`/results` · `^results` — all recorded results\n"
        "`/bracket` · `^bracket` — knockout bracket with resolved teams\n"
        "**Predictions**\n"
        "`/predict <id> <home> <away>` · `^predict …` — predict before kickoff\n"
        "_…or just tap the 🎯 **Predict** button on a match reminder._\n"
        "`/predictions [id]` · `^predictions …` — your picks, or everyone's for a match\n"
        "`/leaderboard` · `^leaderboard` (`^lb`) — prediction standings\n"
        f"_Points: {POINTS_EXACT} exact · {POINTS_GD} right result+GD · {POINTS_RESULT} right result_\n"
        "**Admin**\n"
        "`/result <id> <home> <away> [penalties]` · `^result <id> <s1> <s2> [pen:1|2]` — record a result\n"
        "_…or tap the 📝 **Record result** button on the post-match reminder._\n"
        "`/backup` · `^backup` — download a predictions + results backup\n"
        "`/upload <file>` · `^upload` (with attachment) — restore predictions from a backup")


client.run(DISCORD_TOKEN)