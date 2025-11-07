# bot.py
# Moderation bot with /ping, /moderate, and a message context "Delete & Timeout (10m)"
# Includes: clearer delete-message helper, delete() patches for older discord.py,
# and robust timeout call compatible across versions.

import os
import re
from datetime import timedelta, datetime, timezone
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# -------------------- env & intents --------------------

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("GUILD_ID")  # instant command sync during development

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Guild-scoped decorator helper (so commands appear instantly in your test guild)
CMD_KW: dict = {}
if TEST_GUILD_ID:
    CMD_KW["guild"] = discord.Object(id=int(TEST_GUILD_ID))

# -------------------- helpers --------------------

DURATION_RX = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.I)

def parse_duration_to_timedelta(text: str) -> timedelta:
    """
    Accepts '30s', '10m', '1h', '7d', '2w' → timedelta.
    Caps at Discord's 28-day timeout limit.
    """
    m = DURATION_RX.match(text or "")
    if not m:
        raise ValueError("Duration must look like 30s, 10m, 1h, 7d, or 2w.")
    n = int(m.group(1))
    unit = m.group(2).lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    seconds = n * mult
    if seconds <= 0 or seconds > 28 * 24 * 3600:
        raise ValueError("Duration must be > 0 and ≤ 28 days.")
    return timedelta(seconds=seconds)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

async def dm_user(user: discord.User | discord.Member, text: str) -> bool:
    try:
        dm = await user.create_dm()
        await dm.send(text)
        return True
    except Exception:
        return False

def format_rule_notice(rule: Optional[str], notes: Optional[str], td: timedelta) -> str:
    minutes = int(td.total_seconds() // 60)
    pretty = f"{minutes} minutes" if minutes < 120 else f"{round(td.total_seconds()/3600, 1)} hours"
    base = [
        f"You were timed out for **{pretty}** due to a server rule violation.",
        f"**Violated rule:** {rule or 'Rule violation'}",
    ]
    if notes:
        base.append(f"**Moderator note:** {notes}")
    base.append("If you believe this was a mistake, you may reply here.")
    return "\n".join(base)

async def delete_message_from_link(
    client: discord.Client,
    message_link: str
) -> Tuple[Optional[discord.Message], Optional[str]]:
    """
    Deletes a message by its full link:
    https://discord.com/channels/<guild>/<channel>/<message>
    Returns (Message or None, error_reason or None).
    """
    try:
        link = message_link.strip().strip("<>")
        parts = link.split("/")
        if len(parts) < 3:
            return None, "invalid link format"

        channel_id = int(parts[-2])
        message_id = int(parts[-1])

        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None, "not a text channel or thread"

        # permission sanity
        me = channel.guild.me  # type: ignore
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.read_message_history):
            return None, "missing view/read permissions in that channel"
        if not perms.manage_messages:
            return None, "missing Manage Messages in that channel"

        msg = await channel.fetch_message(message_id)
        # Some discord.py versions' delete() don't accept reason for PartialMessage
        try:
            await msg.delete(reason="Deleted via /moderate")
        except TypeError:
            await msg.delete()
        return msg, None

    except discord.Forbidden:
        return None, "forbidden (role/override denies)"
    except discord.NotFound:
        return None, "not found (bad link or already deleted)"
    except ValueError:
        return None, "invalid numeric IDs in link"
    except Exception as e:
        return None, f"unexpected error: {e}"

async def safe_reply(interaction: discord.Interaction, content: str, ephemeral: bool = True):
    """Reply once: use response if free, else followup."""
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral)

# -------------------- /ping (test) --------------------

@tree.command(name="ping", description="Test command", **CMD_KW)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)

# -------------------- /moderate --------------------

@app_commands.describe(
    user="Member to moderate",
    duration="Timeout duration (e.g., 10m, 1h, 7d)",
    rule="Which rule was violated (e.g., Rule 3: Spam)",
    notes="Optional mod note (included in DM & audit log)",
    message_link="Optional: link to the offending message (will delete it)"
)
@tree.command(
    name="moderate",
    description="Delete a message (optional), timeout the user, and DM them the rule.",
    **CMD_KW
)
async def moderate(
    interaction: discord.Interaction,
    user: discord.Member,
    duration: str,
    rule: str,
    notes: Optional[str] = None,
    message_link: Optional[str] = None
):
    # --- quick permission checks ---
    me: discord.Member = interaction.guild.me  # type: ignore
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You need **Timeout Members** permission to use this.", ephemeral=True)
        return

    missing_perms = []
    if not me.guild_permissions.moderate_members:
        missing_perms.append("Timeout Members")
    if message_link and not me.guild_permissions.manage_messages:
        missing_perms.append("Manage Messages")
    if missing_perms:
        await interaction.response.send_message(
            f"I’m missing required permissions: {', '.join(missing_perms)}.",
            ephemeral=True
        )
        return

    # --- role hierarchy / ownership guard ---
    if user.top_role >= me.top_role or (interaction.guild.owner and user == interaction.guild.owner):
        await interaction.response.send_message(
            "I can’t moderate that member due to role hierarchy / ownership.",
            ephemeral=True
        )
        return

    # --- parse duration ---
    try:
        td = parse_duration_to_timedelta(duration)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    # --- IMPORTANT: defer early to avoid 'unknown interaction' ---
    await interaction.response.defer(ephemeral=True, thinking=True)

    # --- optional delete by link (with detailed reason) ---
    deleted_msg = None
    delete_err = None
    if message_link:
        deleted_msg, delete_err = await delete_message_from_link(interaction.client, message_link)

    # --- apply timeout (positional 'until' for compatibility; fallback to edit) ---
    until = now_utc() + td
    reason = f"{rule}" + (f" — {notes}" if notes else "")
    timed_out = False
    timeout_err = None
    try:
        # Some discord.py versions want positional 'until'
        await user.timeout(until, reason=reason)
        timed_out = True
    except TypeError:
        # Fallback for environments requiring edit()
        try:
            await user.edit(timed_out_until=until, reason=reason)
            timed_out = True
        except Exception as e:
            timeout_err = str(e)
    except Exception as e:
        timeout_err = str(e)

    # --- DM the user ---
    dm_ok = await dm_user(user, format_rule_notice(rule, notes, td))

    # --- build result ---
    lines = []
    if message_link:
        if deleted_msg:
            lines.append("• Message: **deleted**")
        else:
            lines.append(f"• Message: could not be deleted ({delete_err})")
    if timed_out:
        lines.append(f"• Timeout: **applied** until {until.astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    else:
        lines.append(f"• Timeout: **failed** ({timeout_err})")
    lines.append(f"• DM to user: {'sent' if dm_ok else 'not delivered (DMs closed?)'}")
    lines.append(f"• Reason/Audit: {reason}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)

# -------------------- Message context: Delete & Timeout (10m) --------------------

@tree.context_menu(name="Delete & Timeout (10m)", **CMD_KW)
async def quick_delete_timeout(interaction: discord.Interaction, message: discord.Message):
    me: discord.Member = interaction.guild.me  # type: ignore

    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You need **Timeout Members** to use this.", ephemeral=True)
        return
    if not (me.guild_permissions.manage_messages and me.guild_permissions.moderate_members):
        await interaction.response.send_message("I’m missing **Manage Messages** and/or **Timeout Members**.", ephemeral=True)
        return

    target = message.author if isinstance(message.author, discord.Member) else None
    if not target:
        await interaction.response.send_message("Target is not a guild member.", ephemeral=True)
        return
    if target.top_role >= me.top_role or (interaction.guild.owner and target == interaction.guild.owner):
        await interaction.response.send_message("I can’t moderate that member (role hierarchy).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    # delete message (patch: ignore reason on older versions)
    deleted = False
    try:
        try:
            await message.delete(reason=f"By {interaction.user} via context menu")
        except TypeError:
            await message.delete()
        deleted = True
    except Exception:
        deleted = False

    # timeout 10m
    td = timedelta(minutes=10)
    until = now_utc() + td
    rule = "Rule violation"
    reason = f"{rule} — quick action via context menu"
    timed_out = False
    timeout_err = None
    try:
        await target.timeout(until, reason=reason)
        timed_out = True
    except TypeError:
        try:
            await target.edit(timed_out_until=until, reason=reason)
            timed_out = True
        except Exception as e:
            timeout_err = str(e)
    except Exception as e:
        timeout_err = str(e)

    dm_ok = await dm_user(target, format_rule_notice(rule, None, td))

    lines = [
        f"• Message: {'deleted' if deleted else 'not deleted'}",
        f"• Timeout: {'applied' if timed_out else f'failed ({timeout_err})'}",
        f"• DM to user: {'sent' if dm_ok else 'not delivered (DMs closed?)'}",
    ]
    await interaction.followup.send("\n".join(lines), ephemeral=True)

# -------------------- startup & sync --------------------

@bot.event
async def on_ready():
    try:
        if TEST_GUILD_ID:
            guild_obj = discord.Object(id=int(TEST_GUILD_ID))
            await tree.sync(guild=guild_obj)
            g = bot.get_guild(int(TEST_GUILD_ID))
            print(f"Commands synced to test guild {TEST_GUILD_ID} ({g.name if g else 'unknown guild'})")
        else:
            await tree.sync()
            print("Commands synced globally")
    except Exception as e:
        print("Command sync failed:", e)

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set in environment or .env")
    bot.run(TOKEN)
