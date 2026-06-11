# Discord World Cup Bot

A Discord bot and full World Cup 2026 companion: kickoff reminders, live
schedules, group standings, result tracking, an auto-resolving knockout bracket,
and a prediction sweepstake.

Every command is available **two ways** — as a Discord slash command (`/next`,
`/predict …`, with typed, named parameters) **or** as a text command with the
`^` prefix (`^next`, `^predict …`). Type `/help` or `^help` to see them all.

---

## Quick start

1. **Configure secrets.** Create a `.env` file in the repo root (loaded by
   `docker-compose.yml`):

   ```env
   DISCORD_TOKEN=your-bot-token
   RESULT_ADMIN_IDS=123456789012345678   # Discord user IDs allowed to enter results
   # Optional overrides:
   # GUILD_ID=123456789012345678         # sync slash commands to one server instantly
   # REMINDER_CHANNEL=general            # reminders channel by name (default)
   # REMINDER_CHANNEL_ID=123456789012345678  # OR target one channel by ID (takes precedence)
   # REMINDER_ROLE_ID=123456789012345678     # role pinged in the 15-minute reminder
   # VOICE_CHANNEL_ID=123456789012345678     # voice channel whose status shows the live match
   ```

2. **Run it.**

   ```bash
   docker compose up --build
   ```

3. **Invite the bot** with both the `bot` and `applications.commands` scopes
   (the latter is required for slash commands), and make sure it can **View
   Channel** and **Send Messages** in your reminders channel (default `#general`).

> - Enable the **Message Content Intent** in the Discord Developer Portal (needed
>   for the `^` text commands).
> - Slash commands appear after the bot syncs them on startup. A **global** sync
>   can take up to an hour the first time; set `GUILD_ID` to sync to one server
>   instantly while testing.

### Finding your Discord user ID

Enable **Settings → Advanced → Developer Mode**, then right-click your name and
choose **Copy User ID**. Put that number in `RESULT_ADMIN_IDS` (comma-separate
several IDs). Only these users can record match results.

---

## Match reminders

For **every** match in `worldcup.json`, the bot automatically posts to the
reminders channel:

- **1 hour before** kickoff
- **15 minutes before** kickoff

Each reminder shows the teams (with flags), the kickoff time, and a live
countdown. Times use Discord's native timestamp format, so **every user sees
the time in their own local timezone** — no manual conversion needed.

- The **15-minute reminder** also pings `REMINDER_ROLE_ID`, if set.
- At **kickoff**, if `VOICE_CHANNEL_ID` is set, the bot sets that voice channel's
  status to the match title (e.g. `🇲🇽 Mexico vs 🇿🇦 South Africa`). Requires
  discord.py ≥ 2.4 and the **Set Voice Channel Status** permission.

Reminders are de-duplicated and survive restarts (tracked in
`bot/reminder_state.json`). If the bot is offline through a reminder window,
that reminder is skipped rather than fired late.

---

## Commands

| Command                             | What it does                                                         |
| ----------------------------------- | -------------------------------------------------------------------- |
| `^help`                             | List all commands                                                    |
| `^next`                             | Show the next upcoming match                                         |
| `^matches [n]`                      | Show the next _n_ matches (default 5), e.g. `^matches 10`            |
| `^team <name\|flag>`                | All matches for a team, e.g. `^team Brazil` or `^team 🇧🇷`            |
| `^status`                           | Group progress overview (one line per group)                         |
| `^group [letters]`                  | Fixtures **and** standings per group — all, or e.g. `^group A`       |
| `^standings [group]`                | Group tables only — all groups, or one, e.g. `^standings A`          |
| `^qualified`                        | Group winners, runners-up, and best third-placed teams               |
| `^bracket`                          | Knockout bracket with resolved teams and scores                      |
| `^predict <id> <home> <away>`       | Predict a scoreline before kickoff, e.g. `^predict 1 2 1`            |
| `^predictions [id]`                 | Your predictions & points, or everyone's for a match (after kickoff) |
| `^leaderboard`                      | Prediction standings (alias `^lb`)                                   |
| `^result <id> <s1> <s2> [pen:1\|2]` | Record a match result (**admins only**)                              |

Every match in the listings is shown with an **id** (`#12`) — that's what you
pass to `^result`.

### `^team` example

Look up a team by name (full or partial, case-insensitive) or by its flag
emoji. It lists every match for that team — group fixtures with scores, plus any
knockout ties once they advance:

```
^team Brazil
^team brazil
^team 🇧🇷
```

```
🇧🇷 Brazil — Matches

# 13  Sat 13 Jun 2026 ...  🇧🇷 Brazil 5–0 🇲🇦 Morocco · Group C
# 17  ...                  🏴 Scotland 0–5 🇧🇷 Brazil · Group C
# 76  ...                  🇧🇷 Brazil vs 🇹🇳 Tunisia · Round of 32
```

If a partial name is ambiguous (e.g. `^team South`), the bot lists the matching
options so you can pick one.

There are three levels of group detail:

- `^status` — all groups at a glance (one line each)
- `^group [letters]` — fixtures + standings for a group (or all groups)
- `^standings [group]` — just the standings tables

### `^status` example

```
📋 Group status

✅ Group A — complete · 🥇 🇨🇿 Czech Republic, 🥈 🇿🇦 South Africa
🟡 Group B — 2/6 played · leaders: 🇨🇦 Canada, 🇶🇦 Qatar
⚪ Group C — not started
...
```

- ✅ complete — both qualifiers shown
- 🟡 in progress — `played/6` and current leaders
- ⚪ not started

### `^group A` example

Shows each match (with id, kickoff time, and score if played) followed by the
standings table:

```
Group A — Fixtures
#  1  Wed 11 Jun 2026 22:00  🇲🇽 Mexico 0–2 🇿🇦 South Africa
#  2  Thu 12 Jun 2026 05:00  🇰🇷 South Korea 0–2 🇨🇿 Czech Republic
#  5  Sun 15 Jun 2026 ...     🇨🇿 Czech Republic vs 🇲🇽 Mexico   (upcoming)
...

Group A  (in progress)
# Team                   P W D L GF GA  GD Pts
1 Czech Republic         2 2 0 0  4  0   4   6
2 South Africa           2 1 0 1  2  2   0   3
...
```

`^group` with no letter posts every group (one message per group).

---

## Recording results

Only users listed in `RESULT_ADMIN_IDS` can record results.

```
^result <id> <score1> <score2> [pen:1|2]
```

- `<id>` — the match id shown by `^matches` / `^bracket` (e.g. `#1` → `1`)
- `<score1>` / `<score2>` — goals for the home / away team
- `pen:1` or `pen:2` — **required for a knockout match that ends level**,
  naming the penalty-shootout winner (`1` = home, `2` = away)

Examples:

```
^result 1 2 1          # group game: 2–1
^result 89 1 1 pen:2   # knockout draw, away side wins on penalties
```

Results are stored in `bot/results.json`. Re-entering an id overwrites it, so
you can fix a mistake by sending the corrected `^result` again.

### How the bracket fills in

`worldcup.json` defines knockout slots with placeholders that the bot resolves
automatically as results come in:

| Placeholder  | Resolves to                                              |
| ------------ | -------------------------------------------------------- |
| `1A` / `2A`  | Winner / runner-up of Group A                            |
| `3A/B/C/D/F` | One of the 8 best third-placed teams (from those groups) |
| `W74`        | Winner of match #74                                      |
| `L101`       | Loser of match #101                                      |

- Group positions are finalized once **all six** matches in that group have
  results.
- The eight best third-placed teams are determined once **all twelve** groups
  are complete.
- Knockout winners propagate up the bracket automatically, so `^bracket`
  always reflects the latest entered results.

---

## Prediction game (sweepstake)

Anyone in the server can predict scorelines and compete for points across the
whole tournament — the player with the most points by the final wins.

**Make a prediction** (any user, any match, until it kicks off):

```
^predict <id> <home> <away>
^predict 1 2 1          # predict Mexico 2–1 South Africa
```

- You can change your prediction freely until kickoff; re-sending overwrites it.
- Predictions **lock at kickoff** and are hidden from everyone until then (so no
  one can copy), then revealed once the match starts or its result is entered.

**Scoring** (applied automatically when a result is recorded):

| Outcome of your prediction                        | Points |
| ------------------------------------------------- | ------ |
| Exact scoreline                                   | **5**  |
| Correct result **and** winning margin (non-draws) | **3**  |
| Correct result only (right winner, or a draw)     | **2**  |
| Wrong result                                      | 0      |

**See predictions & standings:**

```
^predictions          # your own picks, points, and running total
^predictions 1        # everyone's picks for match #1 (after it kicks off)
^leaderboard          # overall standings (alias: ^lb)
```

When a result is entered, the bot announces how many predictions were scored and
shouts out anyone who nailed the exact score.

---

## Files

| File                      | Purpose                                                    |
| ------------------------- | ---------------------------------------------------------- |
| `worldcup.json`           | The match schedule (dates, times, teams, venues)           |
| `bot/results.json`        | Recorded match results (created on first `^result`)        |
| `bot/predictions.json`    | Everyone's match predictions (created on first `^predict`) |
| `bot/reminder_state.json` | Which reminders have already been sent                     |

---

## Notes & limitations

- **Standings tiebreakers** use points → goal difference → goals scored → name.
  FIFA's full criteria also include head-to-head and fair-play points, which are
  not modeled.
- **Best third-placed assignment** produces a _valid_ mapping (each third-placed
  team lands in a slot allowed for its group). In rare combinations this may
  differ from FIFA's specific published table while remaining a legal pairing.
- The `^standings` table omits flags on purpose so its columns stay aligned in
  Discord's monospace block.

---

## Environment variables

| Variable              | Default         | Purpose                                                               |
| --------------------- | --------------- | --------------------------------------------------------------------- |
| `DISCORD_TOKEN`       | —               | Bot token (required)                                                  |
| `RESULT_ADMIN_IDS`    | _(empty)_       | Comma-separated Discord user IDs allowed to record results            |
| `GUILD_ID`            | _(unset)_       | Sync slash commands to this one server instantly (else global sync)   |
| `REMINDER_CHANNEL`    | `general`       | Reminders channel **by name** (posts to every matching channel)       |
| `REMINDER_CHANNEL_ID` | _(unset)_       | Reminders channel **by ID** — takes precedence over the name when set |
| `REMINDER_ROLE_ID`    | _(unset)_       | Role mentioned in the 15-minute reminder                              |
| `VOICE_CHANNEL_ID`    | _(unset)_       | Voice channel whose status is set to the match title at kickoff       |
| `WORLDCUP_FILE`       | `worldcup.json` | Path to the schedule file                                             |
