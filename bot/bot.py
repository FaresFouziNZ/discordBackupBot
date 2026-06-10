import discord
from discord.ext import tasks
from kafka import KafkaProducer
import json
import os
import re
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
KAFKA_BROKER = os.getenv('KAFKA_BROKER', 'localhost:9092')
KAFKA_TOPIC = os.getenv('KAFKA_TOPIC', 'discord-topic')

# Match reminder configuration
WORLDCUP_FILE = os.getenv('WORLDCUP_FILE', 'worldcup.json')
REMINDER_CHANNEL = os.getenv('REMINDER_CHANNEL', 'general')   # channel name (fallback)
# Optional: target a specific channel by ID; takes precedence over the name above.
REMINDER_CHANNEL_ID = int(os.getenv('REMINDER_CHANNEL_ID')) if os.getenv('REMINDER_CHANNEL_ID', '').strip().isdigit() else None
REMINDER_LEAD_MINUTES = [60, 15]                 # remind 1 hour and 15 minutes before
REMINDER_GRACE = timedelta(minutes=10)           # max lateness before a reminder is skipped
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reminder_state.json')

# Results / standings configuration
RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results.json')
# Discord user IDs allowed to enter match results (comma-separated). For now: user "x".
RESULT_ADMIN_IDS = {x.strip() for x in os.getenv('RESULT_ADMIN_IDS', '').split(',') if x.strip()}

# Prediction game configuration
PREDICTIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'predictions.json')
POINTS_EXACT = 5     # exact scoreline
POINTS_GD = 3        # correct result and goal difference (but not exact)
POINTS_RESULT = 2    # correct result only (right winner / draw)

# Loaded on_ready
MATCHES = []           # list of (kickoff_utc, match_dict)
MATCHES_BY_MID = {}    # mid -> match_dict (mid is a stable 1-based id we assign)
MATCHES_BY_NUM = {}    # official knockout number (e.g. 74) -> match_dict
GROUPS = {}            # group letter -> list of group-stage match_dicts

# Discord client setup
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
client = discord.Client(intents=intents)

# Kafka producer setup
producer = KafkaProducer(
    bootstrap_servers=KAFKA_BROKER,
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

@client.event
async def on_ready():
    global MATCHES
    print(f'Bot connected as {client.user}')
    MATCHES = load_matches()
    print(f'Loaded {len(MATCHES)} matches for reminders')
    if not check_reminders.is_running():
        check_reminders.start()

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if not message.content.startswith('^'):
        return
    if message.content.startswith('^backup'):
        await backup_channel_history(message.channel)
    elif message.content.startswith('^next'):
        await send_upcoming(message.channel, limit=1)
        return
    elif message.content.startswith('^matches'):
        parts = message.content.split()
        limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
        await send_upcoming(message.channel, limit=limit)
        return
    elif message.content.startswith('^result'):
        await handle_result_command(message)
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
    elif message.content.startswith('^bracket'):
        await send_bracket(message.channel)
        return
    elif message.content.startswith('^predictions'):   # must precede ^predict
        await send_predictions(message, message.content.split()[1:])
        return
    elif message.content.startswith('^predict'):
        await handle_predict(message)
        return
    elif message.content.startswith('^leaderboard') or message.content.startswith('^lb'):
        await send_leaderboard(message.channel)
        return
    elif message.content.startswith('^help'):
        await send_help(message.channel)
        return
    print(f'Received message: {message.content}')
    send_message_to_kafka(message)

def send_message_to_kafka(message):
    data = get_message_data(message)
    producer.send(KAFKA_TOPIC, value=data)
    print(f'Sent to Kafka: {data}')

async def backup_channel_history(channel):
    async for msg in channel.history(limit=10, oldest_first=True):
        send_message_to_kafka(msg)

def get_message_data(message):
    attributes = [
    'application', 
    'application_id', 
    'attachments', 
    'channel_mentions', 
    'clean_content', 
    'components', 
    'content', 
    'embeds', 
    'id', 
    'interaction', 
    'jump_url', 
    'mention_everyone', 
    'mentions', 
    'nonce', 
    'pinned', 
    'position', 
    'raw_channel_mentions', 
    'raw_mentions', 
    'raw_role_mentions', 
    'reactions', 
    'reference', 
    'role_mentions', 
    'role_subscription', 
    'stickers', 
    'system_content', 
    'tts', 
    'webhook_id']
    
    x = {attr: getattr(message, attr, None) for attr in attributes}
    x['author'] = get_author_data(message.author)
    x['channel'] = get_channel_data(message.channel)
    x['guild'] = get_guild_data(message.guild)
    x['created_at'] = message.created_at.isoformat()
    x['edited_at'] = message.edited_at.isoformat() if message.edited_at else None

    print(f'message data: {x}')
    return x

def get_author_data(author):
    return {
        'id': author.id if hasattr(author, 'id') else None,
        'name': author.name if hasattr(author, 'name') else None,
        'global_name': author.global_name if hasattr(author, 'global_name') else None,
        'bot': author.bot if hasattr(author, 'bot') else None,
        'nick': author.nick if hasattr(author, 'nick') else None
    }

def get_channel_data(channel):
    return {
        'id': channel.id if hasattr(channel, 'id') else None,
        'name': channel.name if hasattr(channel, 'name') else None,
        'position': channel.position if hasattr(channel, 'position') else None,
        'nsfw': channel.nsfw if hasattr(channel, 'nsfw') else None,
        'news': channel.news if hasattr(channel, 'news') else None,
        'category_id': channel.category_id if hasattr(channel, 'category_id') else None
    }

def get_guild_data(guild):
    return {
        'id': guild.id if hasattr(guild, 'id') else None,
        'name': guild.name if hasattr(guild, 'name') else None,
        'shard_id': guild.shard_id if hasattr(guild, 'shard_id') else None,
        'chunked': guild.chunked if hasattr(guild, 'chunked') else None,
        'member_count': guild.member_count if hasattr(guild, 'member_count') else None
    }

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
    lines = [
        f"⏰ **Match in {when}!**",
        f"🏟️ **{team_label(team1)} vs {team_label(team2)}**" + (f" — {context}" if context else ""),
        f"🕒 Kickoff: **{discord_timestamp(kickoff_utc)}** ({discord_timestamp(kickoff_utc, 'R')})",
    ]
    if ground:
        lines.append(f"📍 {ground}")
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


@tasks.loop(seconds=30)
async def check_reminders():
    now = datetime.now(timezone.utc)
    channels = None
    for kickoff_utc, match in MATCHES:
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
                    await channel.send(message)
                    print(f'Sent reminder to #{channel.name}: {key}')
                except discord.DiscordException as e:
                    print(f'Failed to send reminder: {e}')
            sent_reminders.add(key)
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
    return str(user.id) in RESULT_ADMIN_IDS


async def handle_result_command(message):
    """^result <mid> <score1> <score2> [pen:1|2]   — record a match result."""
    if not is_result_admin(message.author):
        await message.channel.send("🚫 You are not allowed to enter results.")
        return

    parts = message.content.split()
    if len(parts) < 4:
        await message.channel.send(
            "Usage: `^result <id> <score1> <score2> [pen:1|2]`\n"
            "Example: `^result 1 2 1`  (use `pen:1`/`pen:2` for a knockout decided on penalties)")
        return

    try:
        mid = int(parts[1])
        s1, s2 = int(parts[2]), int(parts[3])
    except ValueError:
        await message.channel.send("⚠️ Id and scores must be whole numbers.")
        return
    if s1 < 0 or s2 < 0:
        await message.channel.send("⚠️ Scores cannot be negative.")
        return

    match = MATCHES_BY_MID.get(mid)
    if match is None:
        await message.channel.send(f"⚠️ No match with id `#{mid}`.")
        return

    pen = None
    for p in parts[4:]:
        if p.startswith('pen:') and p[4:] in ('1', '2'):
            pen = int(p[4:])

    is_knockout = not match.get('group')
    if is_knockout and s1 == s2 and pen is None:
        await message.channel.send(
            "⚠️ Knockout match can't end level — add the penalty winner, "
            "e.g. `pen:1` (home) or `pen:2` (away).")
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
        winners = [p['name'] for p in entries.values()
                   if score_prediction(p, res) == POINTS_EXACT]
        line += f"\n🎯 {len(entries)} prediction(s) scored — see `^leaderboard`."
        if winners:
            line += f" Exact score by: {', '.join(winners)} 🎉"
    await message.channel.send(line)


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


def display_name(author):
    return getattr(author, 'display_name', None) or getattr(author, 'name', str(author.id))


async def handle_predict(message):
    """^predict <id> <home> <away> — submit/replace a prediction before kickoff."""
    parts = message.content.split()
    if len(parts) < 4:
        await message.channel.send(
            "Usage: `^predict <id> <home> <away>` — e.g. `^predict 1 2 1`")
        return
    try:
        mid, s1, s2 = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        await message.channel.send("⚠️ Id and scores must be whole numbers.")
        return
    if s1 < 0 or s2 < 0:
        await message.channel.send("⚠️ Scores cannot be negative.")
        return

    match = MATCHES_BY_MID.get(mid)
    if match is None:
        await message.channel.send(f"⚠️ No match with id `#{mid}`.")
        return
    kickoff = parse_match_datetime(match)
    if kickoff is None:
        await message.channel.send("⚠️ This match has no scheduled time.")
        return
    if datetime.now(timezone.utc) >= kickoff:
        await message.channel.send(f"🔒 Predictions for `#{mid}` are closed — the match has started.")
        return

    predictions = load_predictions()
    predictions.setdefault(str(mid), {})[str(message.author.id)] = {
        's1': s1, 's2': s2, 'name': display_name(message.author),
    }
    save_predictions(predictions)

    t1 = resolve_token(match.get('team1')) or match.get('team1')
    t2 = resolve_token(match.get('team2')) or match.get('team2')
    await message.channel.send(
        f"✅ Prediction saved: **{team_label(t1)} {s1}–{s2} {team_label(t2)}** "
        f"(locks {discord_timestamp(kickoff, 'R')})")


async def send_predictions(message, args):
    """^predictions          — your own predictions and points.
       ^predictions <id>      — everyone's predictions for a match (after kickoff)."""
    predictions = load_predictions()
    results = load_results()

    if args and args[0].isdigit():
        mid = int(args[0])
        match = MATCHES_BY_MID.get(mid)
        if match is None:
            await message.channel.send(f"⚠️ No match with id `#{mid}`.")
            return
        entries = predictions.get(str(mid), {})
        kickoff = parse_match_datetime(match)
        not_started = kickoff and datetime.now(timezone.utc) < kickoff
        # Reveal once the match has kicked off, or once a result exists.
        if not_started and str(mid) not in results:
            await message.channel.send(
                f"🔒 Predictions for `#{mid}` are hidden until kickoff "
                f"({len(entries)} so far, locks {discord_timestamp(kickoff, 'R')}).")
            return
        if not entries:
            await message.channel.send(f"No predictions were made for `#{mid}`.")
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
        await send_lines(message.channel, lines)
        return

    # No id: caller's own predictions
    uid = str(message.author.id)
    mine = sorted(((int(mid), entries[uid]) for mid, entries in predictions.items()
                   if uid in entries), key=lambda x: x[0])
    if not mine:
        await message.channel.send(
            "You have no predictions yet. Use `^predict <id> <home> <away>`.")
        return
    lines = [f"**{display_name(message.author)}'s predictions**"]
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
    await send_lines(message.channel, lines)


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


async def send_help(channel):
    await channel.send(
        "**World Cup bot commands**\n"
        "`^next` — next match\n"
        "`^matches [n]` — next *n* matches (default 5)\n"
        "`^team <name|flag>` — all matches for a team, e.g. `^team Brazil` or `^team 🇧🇷`\n"
        "`^status` — group progress at a glance (played / leaders / qualified)\n"
        "`^group [letters]` — fixtures + standings per group (all, or e.g. `^group A`)\n"
        "`^standings [group]` — group tables (all groups, or e.g. `^standings A`)\n"
        "`^qualified` — group winners/runners-up and best third-placed teams\n"
        "`^bracket` — knockout bracket with resolved teams\n"
        "**Predictions**\n"
        "`^predict <id> <home> <away>` — predict a score before kickoff (e.g. `^predict 1 2 1`)\n"
        "`^predictions [id]` — your predictions, or everyone's for a match (after kickoff)\n"
        "`^leaderboard` — prediction standings (alias `^lb`)\n"
        f"_Points: {POINTS_EXACT} exact · {POINTS_GD} right result+GD · {POINTS_RESULT} right result_\n"
        "**Admin**\n"
        "`^result <id> <s1> <s2> [pen:1|2]` — record a result (admins only)\n"
        "`^backup` — back up channel history")


client.run(DISCORD_TOKEN)