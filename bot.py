# bot.py — Final Version + Offense Tracking + Mod-Log (NO /moderate)
# Includes:
# - /ping
# - /strike (auto-escalation)
# - /offenses
# - /reset_offenses
# - JSON offense tracking
# - Mod-log channel logging
# - Delete & Timeout (10m) message context menu
# - Universal timeout compatibility
# - Message deletion support
# - Guild-scoped command sync

import os
import re
import json
from datetime import timedelta, datetime, timezone
from typing import Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# -------------------- env & intents --------------------

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("GUILD_ID")
MODLOG_CHANNEL_ID = os.getenv("MODLOG_CHANNEL_ID")

intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

CMD_KW = {}
if TEST_GUILD_ID:
    CMD_KW["guild"] = discord.Object(id=int(TEST_GUILD_ID))

# -------------------- offense JSON helpers --------------------

OFFENSE_FILE = "offenses.json"

def load_offenses() -> dict:
    if not os.path.exists(OFFENSE_FILE):
        with open(OFFENSE_FILE, "w") as f:
            json.dump({}, f)
    with open(OFFENSE_FILE, "r") as f:
        return json.load(f)

def save_offenses(data: dict):
    with open(OFFENSE_FILE, "w") as f:
        json.dump(data, f, indent=4)

def add_offense(user_id: int) -> int:
    data = load_offenses()
    current = data.get(str(user_id), 0) + 1
    data[str(user_id)] = current
    save_offenses(data)
    return current

def get_offenses(user_id: int) -> int:
    data = load_offenses()
    return data.get(str(user_id), 0)

# -------------------- helpers --------------------

DURATION_RX = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.I)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

async def dm_user(user: discord.User | discord.Member, text: str) -> bool:
    try:
        dm = await user.create_dm()
        await dm.send(text)
        return True
    except Exception:
        return False

def format_rule_notice(rule: str, notes: Optional[str], td: Optional[timedelta]) -> str:
    base: list[str] = []
    if td is not None:
        minutes = td.total_seconds() // 60
        pretty = (
            f"{int(minutes)} minutes"
            if minutes < 120
            else f"{round(td.total_seconds()/3600, 1)} hours"
        )
        base.append(f"You were punished for **{pretty}** due to a rule violation.")
    else:
        base.append("You received a **warning** for a rule violation.")
    base.append(f"**Violated rule:** {rule}")
    if notes:
        base.append(f"**Moderator note:** {notes}")
    base.append("If you believe this was a mistake, you may reply here.")
    return "\n".join(base)

async def delete_message_from_link(client: discord.Client, message_link: str):
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

        me = channel.guild.me  # type: ignore
        perms = channel.permissions_for(me)
        if not (perms.view_channel and perms.read_message_history):
            return None, "missing permission: View Channel / Read History"
        if not perms.manage_messages:
            return None, "missing permission: Manage Messages"

        msg = await channel.fetch_message(message_id)

        try:
            await msg.delete(reason="Deleted via /strike")
        except TypeError:
            await msg.delete()

        return msg, None

    except discord.Forbidden:
        return None, "forbidden"
    except discord.NotFound:
        return None, "not found"
    except ValueError:
        return None, "invalid numbers"
    except Exception as e:
        return None, f"unexpected error: {e}"

async def send_modlog(
    guild: discord.Guild,
    title: str,
    user: discord.Member | discord.User,
    moderator: discord.Member | discord.User,
    details: dict
):
    if not MODLOG_CHANNEL_ID:
        return

    try:
        channel_id = int(MODLOG_CHANNEL_ID)
    except ValueError:
        return

    channel = guild.get_channel(channel_id)
    if not channel:
        return

    embed = discord.Embed(
        title=title,
        color=0xFF4444,
        timestamp=now_utc()
    )

    embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=False)
    embed.add_field(name="Moderator", value=f"{moderator} (`{moderator.id}`)", inline=False)

    for k, v in details.items():
        embed.add_field(name=k, value=str(v), inline=False)

    await channel.send(embed=embed)

# -------------------- /ping --------------------

@tree.command(name="ping", description="Test command", **CMD_KW)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)

# -------------------- /strike (auto escalation + message deletion) --------------------

@app_commands.describe(
    user="User receiving the strike",
    rule="Which rule was violated?",
    notes="Optional moderator note",
    message_link="Optional: link to the offending message"
)
@tree.command(
    name="strike",
    description="Apply escalating punishments based on offense history.",
    **CMD_KW
)
async def strike(
    interaction: discord.Interaction,
    user: discord.Member,
    rule: str,
    notes: Optional[str] = None,
    message_link: Optional[str] = None
):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You need **Timeout Members**.", ephemeral=True)
        return

    me: discord.Member = interaction.guild.me  # type: ignore

    if user.top_role >= me.top_role or user == interaction.guild.owner:
        await interaction.response.send_message("Cannot moderate that user (role hierarchy).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    deleted_msg = None
    delete_err = None
    if message_link:
        deleted_msg, delete_err = await delete_message_from_link(
            interaction.client,
            message_link
        )

    offense = add_offense(user.id)

    if offense == 1:
        punishment = "Warning"
        td = None
    elif offense == 2:
        punishment = "Timeout (10m)"
        td = timedelta(minutes=10)
    elif offense == 3:
        punishment = "Timeout (1h)"
        td = timedelta(hours=1)
    else:
        punishment = "Ban"
        td = None

    timed_out = False
    banned = False
    error = None

    if td is None and offense == 1:
        pass

    elif td and offense in (2, 3):
        until = now_utc() + td
        try:
            await user.timeout(until, reason=f"{rule} — {notes or ''}")
            timed_out = True
        except TypeError:
            try:
                await user.edit(timed_out_until=until, reason=f"{rule} — {notes or ''}")
                timed_out = True
            except Exception as e:
                error = str(e)
        except Exception as e:
            error = str(e)

    else:
        try:
            await interaction.guild.ban(user, reason=f"{rule} — {notes or ''}")
            banned = True
        except Exception as e:
            error = str(e)

    dm_ok = await dm_user(
        user,
        format_rule_notice(rule, notes, td)
    )

    summary = [
        f"• Offense count: **{offense}**",
        f"• Penalty: **{punishment}**",
        f"• DM to user: {'sent' if dm_ok else 'not delivered'}"
    ]

    if message_link:
        if deleted_msg:
            summary.append("• Offending message: **deleted**")
        else:
            summary.append(f"• Offending message: failed ({delete_err})")

    if timed_out:
        summary.append("• Timeout applied")
    if banned:
        summary.append("• User has been **banned**")
    if error:
        summary.append(f"• Error: {error}")

    await interaction.followup.send("\n".join(summary), ephemeral=True)

    details = {
        "Offense Count": offense,
        "Penalty": punishment,
        "Rule": rule,
        "Notes": notes or "None",
    }

    if message_link:
        details["Message Link"] = message_link
        details["Message Deleted"] = (
            "Yes" if deleted_msg else f"No ({delete_err})"
        )

    await send_modlog(
        guild=interaction.guild,
        title="Strike Issued",
        user=user,
        moderator=interaction.user,
        details=details
    )

# -------------------- /offenses --------------------

@app_commands.describe(
    user="User whose offense count you want to check"
)
@tree.command(
    name="offenses",
    description="Check how many offenses a user has.",
    **CMD_KW
)
async def offenses(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You need **Timeout Members**.", ephemeral=True)
        return

    count = get_offenses(user.id)
    await interaction.response.send_message(
        f"⚖️ **{user}** currently has **{count} offense(s)**.",
        ephemeral=True
    )

# -------------------- /reset_offenses --------------------

@app_commands.describe(
    user="User whose offense count you want to reset"
)
@tree.command(
    name="reset_offenses",
    description="Reset a user's offense count to zero.",
    **CMD_KW
)
async def reset_offenses(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You need **Timeout Members**.", ephemeral=True)
        return

    data = load_offenses()
    data[str(user.id)] = 0
    save_offenses(data)

    await interaction.response.send_message(
        f"♻️ Offenses for **{user}** have been **reset to 0**.",
        ephemeral=True
    )

    await send_modlog(
        guild=interaction.guild,
        title="Offense Count Reset",
        user=user,
        moderator=interaction.user,
        details={"New Count": 0}
    )

# -------------------- Context Menu: Delete & Timeout (10m) --------------------

@tree.context_menu(name="Delete & Timeout (10m)", **CMD_KW)
async def quick_delete_timeout(interaction: discord.Interaction, message: discord.Message):
    me: discord.Member = interaction.guild.me  # type: ignore

    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("You need **Timeout Members**.", ephemeral=True)
        return

    if not (me.guild_permissions.manage_messages and me.guild_permissions.moderate_members):
        await interaction.response.send_message("Missing Manage Messages/Timeout Members.", ephemeral=True)
        return

    target = message.author if isinstance(message.author, discord.Member) else None
    if not target:
        await interaction.response.send_message("User not found.", ephemeral=True)
        return

    if target.top_role >= me.top_role or target == interaction.guild.owner:
        await interaction.response.send_message("Cannot moderate (role hierarchy).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    deleted = False
    try:
        try:
            await message.delete(reason=f"By {interaction.user} via context menu")
        except TypeError:
            await message.delete()
        deleted = True
    except Exception:
        deleted = False

    td = timedelta(minutes=10)
    until = now_utc() + td
    rule = "Rule violation"
    reason = f"{rule} — context menu"
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
        f"• DM to user: {'sent' if dm_ok else 'not delivered'}",
    ]

    await interaction.followup.send("\n".join(lines), ephemeral=True)

    await send_modlog(
        guild=interaction.guild,
        title="Context Menu: Delete & Timeout",
        user=target,
        moderator=interaction.user,
        details={
            "Message Deleted": "Yes" if deleted else "No",
            "Timeout Applied": "Yes" if timed_out else "No",
            "Rule": rule,
        }
    )

# -------------------- startup & sync --------------------

@bot.event
async def on_ready():
    try:
        if TEST_GUILD_ID:
            guild_obj = discord.Object(id=int(TEST_GUILD_ID))
            await tree.sync(guild=guild_obj)
            g = bot.get_guild(int(TEST_GUILD_ID))
            print(f"Commands synced to test guild {TEST_GUILD_ID} ({g.name if g else 'unknown'})")
        else:
            await tree.sync()
            print("Commands synced globally")
    except Exception as e:
        print("Sync failed:", e)

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set!")
    bot.run(TOKEN)
