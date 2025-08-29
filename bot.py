"""
665 — Avrae-style TTRPG Discord Bot (discord.py + asyncpg Postgres)

ENV (Render -> your service -> Environment):
- DISCORD_TOKEN = <your Discord bot token>
- DATABASE_URL  = postgresql://USER:PASSWORD@HOST:PORT/DB?sslmode=require

Start command on Render: python bot.py
Build command:          pip install -r requirements.txt

This file contains:
- Keep-alive Flask web server (so Render's free Web Service sees a bound $PORT)
- Postgres schema + asyncpg pool
- Slash commands for your custom system:
  /create, /sheet, /roll, /oppose, /hp, /rv, /favor,
  /inv_add, /inv_remove, /inv_list,
  /bonus_add, /bonus_list, /bonus_remove,
  /weakness_add, /weakness_list, /weakness_remove,
  /career_set (Humans), lineage abilities: /orbing, /healing, /rest,
  GM: /gm_skill
"""

# ------------------------- keep-alive (in this file) -------------------------
from flask import Flask
from threading import Thread
import os, random, json, io
from typing import Dict, Optional, List, Tuple

app = Flask(__name__)

@app.get("/")
def home():
    return "665 is alive."

def _run_keepalive():
    port = int(os.environ.get("PORT", "10000"))
    # host 0.0.0.0 so Render can hit it
    app.run(host="0.0.0.0", port=port)

def start_keep_alive():
    Thread(target=_run_keepalive, daemon=True).start()

# ---------------------------- bot + database ---------------------------------
import discord
from discord.ext import commands
from discord import app_commands
import asyncpg

BOT_NAME = "665"
EMOJI_STAR = "✨️"  # show totals as **✨️<n>✨️**

# Skills (exactly the 10 you defined)
SKILLS = [
    "investigate", "persuade", "insight", "athletics", "stealth",
    "combat", "occult", "streetwise", "tech", "willpower",
]

# Lineages
LINEAGES = ["whitelighter", "human", "demon", "witch"]

# Human Career choices
HOMES = ["Cramped", "Modest", "Comfortable", "Well-Off", "Weird"]
TRANSPORTS = ["None", "Junk Vehicle", "Reliable Ride", "Specialty Vehicle", "Supernatural Travel"]
WEALTH = ["Broke", "Struggling", "Stable", "Comfortable", "Absolute"]

def slug(s: str) -> str:
    return s.strip().lower().replace(" ", "_")

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

# ------------------------------- SQL schema ----------------------------------
CREATE_SQL = [
    # players
    """
    CREATE TABLE IF NOT EXISTS players (
        guild_id BIGINT,
        user_id  BIGINT,
        name TEXT,
        quote TEXT,
        lineage TEXT,
        hp INTEGER DEFAULT 10,
        max_hp INTEGER DEFAULT 10,
        rv INTEGER DEFAULT 5,
        max_rv INTEGER DEFAULT 5,
        favors INTEGER DEFAULT 1,
        last_favor_reset DATE,
        home TEXT,
        transport TEXT,
        wealth TEXT,
        connections INTEGER DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );
    """,
    # skills (base points)
    """
    CREATE TABLE IF NOT EXISTS skills (
        guild_id BIGINT,
        user_id  BIGINT,
        skill TEXT,
        points INTEGER,
        PRIMARY KEY (guild_id, user_id, skill)
    );
    """,
    # extra bonuses that stack
    """
    CREATE TABLE IF NOT EXISTS bonuses (
        id BIGSERIAL PRIMARY KEY,
        guild_id BIGINT,
        user_id BIGINT,
        skill TEXT,
        bonus INTEGER,
        reason TEXT
    );
    """,
    # inventory
    """
    CREATE TABLE IF NOT EXISTS inventory (
        guild_id BIGINT,
        user_id BIGINT,
        item TEXT,
        qty INTEGER,
        PRIMARY KEY (guild_id, user_id, item)
    );
    """,
    # weaknesses
    """
    CREATE TABLE IF NOT EXISTS weaknesses (
        id BIGSERIAL PRIMARY KEY,
        guild_id BIGINT,
        user_id BIGINT,
        text TEXT
    );
    """,
    # lineage ability usage (per scene/rest)
    """
    CREATE TABLE IF NOT EXISTS ability_usage (
        guild_id BIGINT,
        user_id BIGINT,
        ability TEXT,
        scope TEXT,  -- 'scene' or 'rest'
        used BOOLEAN DEFAULT FALSE,
        PRIMARY KEY (guild_id, user_id, ability)
    );
    """,
]

async def init_db(pool: asyncpg.Pool):
    async with pool.acquire() as con:
        for sql in CREATE_SQL:
            await con.execute(sql)

async def get_pool_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    print("[665] DB URL present:", bool(url))
    print("[665] DB URL scheme:", (url.split("://", 1)[0] if "://" in url else "(none)"))
    if not url:
        raise RuntimeError("Set DATABASE_URL (Neon Postgres URL).")
    if "sslmode" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url

# ------------------------------- Bot class -----------------------------------
class TTRPGBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.pool: Optional[asyncpg.Pool] = None

    async def setup_hook(self):
        db_url = await get_pool_url()
        self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
        await init_db(self.pool)
        await self.tree.sync()

bot = TTRPGBot()

@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.playing, name="/sheet • /roll")
    )
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ----------------------------- DB helpers ------------------------------------
def pool() -> asyncpg.Pool:
    assert bot.pool is not None, "DB pool not ready"
    return bot.pool

async def ensure_player(gid: int, uid: int, name: str = "Player", lineage: Optional[str] = None):
    p = pool()
    async with p.acquire() as con:
        row = await con.fetchrow("SELECT 1 FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid)
        if row is None:
            await con.execute(
                "INSERT INTO players (guild_id,user_id,name,lineage) VALUES ($1,$2,$3,$4)",
                gid, uid, name, lineage or "human"
            )
        for sk in SKILLS:
            await con.execute(
                "INSERT INTO skills (guild_id,user_id,skill,points) VALUES ($1,$2,$3,0) ON CONFLICT DO NOTHING",
                gid, uid, sk
            )

async def get_skill_total(gid: int, uid: int, skill: str) -> Tuple[int, int, int]:
    s = slug(skill)
    p = pool()
    async with p.acquire() as con:
        base = await con.fetchval(
            "SELECT points FROM skills WHERE guild_id=$1 AND user_id=$2 AND skill=$3", gid, uid, s
        )
        base = int(base or 0)
        bonus = await con.fetchval(
            "SELECT COALESCE(SUM(bonus),0) FROM bonuses WHERE guild_id=$1 AND user_id=$2 AND skill=$3",
            gid, uid, s
        )
        bonus = int(bonus or 0)
    return base, bonus, base + bonus

def is_gm(member: discord.Member) -> bool:
    p = member.guild_permissions
    return p.administrator or p.manage_guild

# --------------------------- Character Creation ------------------------------
@bot.tree.command(description="Create your character with lineage and skill distribution")
@app_commands.describe(
    name="Character name",
    lineage="Lineage",
    quote="Optional quote for your sheet",
    plus3="Skill at +3",
    plus2_a="First +2 skill",
    plus2_b="Second +2 skill",
    plus1_a="First +1 skill",
    plus1_b="Second +1 skill",
)
@app_commands.choices(
    lineage=[app_commands.Choice(name=x.capitalize(), value=x) for x in LINEAGES],
    plus3=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus2_a=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus2_b=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus1_a=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus1_b=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
)
async def create(
    interaction: discord.Interaction,
    name: str,
    lineage: app_commands.Choice[str],
    quote: Optional[str] = None,
    plus3: Optional[app_commands.Choice[str]] = None,
    plus2_a: Optional[app_commands.Choice[str]] = None,
    plus2_b: Optional[app_commands.Choice[str]] = None,
    plus1_a: Optional[app_commands.Choice[str]] = None,
    plus1_b: Optional[app_commands.Choice[str]] = None,
):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id

    picks = [x.value for x in [plus3, plus2_a, plus2_b, plus1_a, plus1_b] if x]
    if len(picks) != 5 or len(set(picks)) != 5:
        return await interaction.response.send_message(
            "Pick **five different** skills: one +3, two +2, and two +1.", ephemeral=True
        )

    await ensure_player(gid, uid, name=name, lineage=lineage.value)
    p = pool()
    async with p.acquire() as con:
        # reset skills then apply distribution
        await con.execute("UPDATE skills SET points=0 WHERE guild_id=$1 AND user_id=$2", gid, uid)
        await con.execute(
            "UPDATE skills SET points=3 WHERE guild_id=$1 AND user_id=$2 AND skill=$3", gid, uid, plus3.value
        )
        for s in [plus2_a.value, plus2_b.value]:
            await con.execute("UPDATE skills SET points=2 WHERE guild_id=$1 AND user_id=$2 AND skill=$3", gid, uid, s)
        for s in [plus1_a.value, plus1_b.value]:
            await con.execute("UPDATE skills SET points=1 WHERE guild_id=$1 AND user_id=$2 AND skill=$3", gid, uid, s)

        # lineage effects
        if lineage.value == "whitelighter":
            await con.execute("UPDATE players SET rv=rv+2, max_rv=max_rv+2 WHERE guild_id=$1 AND user_id=$2", gid, uid)
            await con.execute(
                "INSERT INTO ability_usage (guild_id,user_id,ability,scope,used) VALUES ($1,$2,'orbing','scene',FALSE)"
                " ON CONFLICT DO NOTHING",
                gid, uid
            )
            await con.execute(
                "INSERT INTO ability_usage (guild_id,user_id,ability,scope,used) VALUES ($1,$2,'healing','rest',FALSE)"
                " ON CONFLICT DO NOTHING",
                gid, uid
            )
        elif lineage.value == "human":
            await con.execute("UPDATE players SET connections=connections+3 WHERE guild_id=$1 AND user_id=$2", gid, uid)

        await con.execute(
            "UPDATE players SET name=$1, quote=$2, lineage=$3 WHERE guild_id=$4 AND user_id=$5",
            name, quote, lineage.value, gid, uid
        )

    embed = discord.Embed(title=f"{name} created!", color=discord.Color.magenta())
    embed.add_field(name="Lineage", value=lineage.name)
    if quote:
        embed.add_field(name="Quote", value=f"“{quote}”", inline=False)
    embed.add_field(name="Starts with", value="HP 10, RV 5 (Whitelighter +2), Favors 1", inline=False)
    embed.set_footer(text="Use /sheet to view, /roll to play.")
    await interaction.response.send_message(embed=embed)

# ------------------------------- Career (Human) -------------------------------
@bot.tree.command(description="(Human) Set your Career: Home, Transportation, Wealth (+ optional gear note)")
@app_commands.describe(
    home="Home type", transport="Transportation", wealth="Wealth level", gear="Freeform gear to stash in inventory"
)
@app_commands.choices(
    home=[app_commands.Choice(name=h, value=h) for h in HOMES],
    transport=[app_commands.Choice(name=t, value=t) for t in TRANSPORTS],
    wealth=[app_commands.Choice(name=w, value=w) for w in WEALTH],
)
async def career_set(
    interaction: discord.Interaction,
    home: app_commands.Choice[str],
    transport: app_commands.Choice[str],
    wealth: app_commands.Choice[str],
    gear: Optional[str] = None,
):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        lin = await con.fetchval("SELECT lineage FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid)
        if lin != "human":
            return await interaction.response.send_message("Only Humans set Careers.", ephemeral=True)
        await con.execute(
            "UPDATE players SET home=$1, transport=$2, wealth=$3 WHERE guild_id=$4 AND user_id=$5",
            home.value, transport.value, wealth.value, gid, uid
        )
        if gear:
            await con.execute(
                "INSERT INTO inventory (guild_id,user_id,item,qty) VALUES ($1,$2,$3,1) ON CONFLICT DO NOTHING",
                gid, uid, f"Career Gear: {gear}"
            )
    await interaction.response.send_message("Career saved.")

# -------------------------------- Character Sheet ----------------------------
@bot.tree.command(description="Show your character sheet")
async def sheet(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    target = member or interaction.user
    gid = interaction.guild_id
    uid = target.id
    p = pool()
    async with p.acquire() as con:
        pl = await con.fetchrow("SELECT * FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid)
        if not pl:
            return await interaction.response.send_message("No character. Use /create.", ephemeral=True)
        sk = await con.fetch(
            "SELECT skill, points FROM skills WHERE guild_id=$1 AND user_id=$2 ORDER BY skill", gid, uid
        )
        inv = await con.fetch(
            "SELECT item, qty FROM inventory WHERE guild_id=$1 AND user_id=$2 ORDER BY item", gid, uid
        )
        weaks = await con.fetch("SELECT text FROM weaknesses WHERE guild_id=$1 AND user_id=$2", gid, uid)

    skills_text = ", ".join([f"{r['skill']} +{r['points']}" for r in sk if r["points"]]) or "(choose with /create)"
    inv_text = ", ".join([f"{r['item']}×{r['qty']}" for r in inv]) or "(none)"
    weak_text = "; ".join([w["text"] for w in weaks]) if weaks else None

    embed = discord.Embed(title=f"{pl['name']} — Lineage: {pl['lineage'].upper()}", color=discord.Color.dark_magenta())
    if pl["quote"]:
        embed.description = f"“{pl['quote']}”"
    embed.add_field(name="❤️ HP", value=f"{pl['hp']} / {pl['max_hp']}")
    embed.add_field(name="🔘 RV", value=f"{pl['rv']} / {pl['max_rv']}")
    embed.add_field(name="◽ FAVORS", value=str(pl["favors"]))
    if pl["home"] or pl["transport"] or pl["wealth"]:
        embed.add_field(
            name="Career",
            value=f"Home: {pl['home'] or '-'} | Transport: {pl['transport'] or '-'} | Wealth: {pl['wealth'] or '-'}",
            inline=False,
        )
    embed.add_field(name="Skills", value=skills_text, inline=False)
    if weak_text:
        embed.add_field(name="Weaknesses", value=weak_text, inline=False)
    embed.add_field(name="Inventory", value=inv_text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------------------- Rolls -----------------------------------
@bot.tree.command(description="Roll 1d20 + skill (+ bonuses). Supports advantage/disadvantage and DC.")
@app_commands.describe(
    skill="Which skill",
    advantage="Roll 2d20 take higher",
    disadvantage="Roll 2d20 take lower",
    dc="Optional difficulty to compare",
    bonus="Extra situational modifier",
    note="Footer note"
)
@app_commands.choices(skill=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS])
async def roll(
    interaction: discord.Interaction,
    skill: app_commands.Choice[str],
    advantage: Optional[bool] = False,
    disadvantage: Optional[bool] = False,
    dc: Optional[int] = None,
    bonus: Optional[int] = 0,
    note: Optional[str] = None,
):
    if advantage and disadvantage:
        return await interaction.response.send_message("Can't have both advantage and disadvantage.", ephemeral=True)
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)

    gid = interaction.guild_id
    uid = interaction.user.id
    await ensure_player(gid, uid, interaction.user.display_name)

    base, extra, total_skill = await get_skill_total(gid, uid, skill.value)

    d1, d2 = random.randint(1, 20), random.randint(1, 20)
    d20 = max(d1, d2) if advantage else (min(d1, d2) if disadvantage else d1)
    adv_note = f"Advantage ({d1}/{d2})" if advantage else (f"Disadvantage ({d1}/{d2})" if disadvantage else None)

    total = d20 + total_skill + int(bonus or 0)

    title = f"{interaction.user.display_name} rolls {skill.name}"
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    nat = " (CRIT!)" if d20 == 20 else (" (BOTCH)" if d20 == 1 else "")
    embed.add_field(name="d20", value=f"{d20}{nat}")
    embed.add_field(name="Skill base", value=f"+{base}")
    if extra:
        embed.add_field(name="Bonuses", value=f"+{extra}")
    if bonus:
        embed.add_field(name="Situational", value=f"+{bonus}")
    embed.add_field(name="Total", value=f"**{EMOJI_STAR}{total}{EMOJI_STAR}**", inline=False)

    if dc is not None:
        outcome = "✅ Success" if (total >= dc or d20 == 20) else "❌ Failure"
        if d20 == 1:
            outcome = "❌ **Nat 1** (Complication)"
        elif d20 == 20:
            outcome = "✅ **Nat 20** (Automatic)"
        embed.add_field(name=f"vs DC {dc}", value=outcome, inline=False)

    footer = " • ".join([x for x in [adv_note, note] if x])
    if footer:
        embed.set_footer(text=footer)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(description="Opposed roll (you vs enemy bonus)")
@app_commands.describe(skill="Your skill", enemy_bonus="Enemy flat bonus")
@app_commands.choices(skill=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS])
async def oppose(interaction: discord.Interaction, skill: app_commands.Choice[str], enemy_bonus: int):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    await ensure_player(gid, uid, interaction.user.display_name)

    _, _, my_bonus = await get_skill_total(gid, uid, skill.value)
    my_d = random.randint(1, 20)
    enemy_d = random.randint(1, 20)
    my_total = my_d + my_bonus
    enemy_total = enemy_d + enemy_bonus

    embed = discord.Embed(title="Opposed Roll", color=discord.Color.orange())
    embed.add_field(name="You", value=f"d20={my_d} • bonus=+{my_bonus} → **{EMOJI_STAR}{my_total}{EMOJI_STAR}**")
    embed.add_field(name="Enemy", value=f"d20={enemy_d} • bonus=+{enemy_bonus} → **{EMOJI_STAR}{enemy_total}{EMOJI_STAR}**")
    if my_total > enemy_total:
        embed.add_field(name="Result", value="You win.", inline=False)
    elif my_total < enemy_total:
        embed.add_field(name="Result", value="Enemy wins.", inline=False)
    else:
        embed.add_field(name="Result", value="Tie (GM/defender decides).", inline=False)
    await interaction.response.send_message(embed=embed)

# ---------------------------- HP / RV / Favors -------------------------------
@bot.tree.command(description="Damage or heal HP (positive damages, negative heals)")
async def hp(interaction: discord.Interaction, amount: int):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        row = await con.fetchrow("SELECT hp,max_hp FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid)
        if not row:
            return await interaction.response.send_message("No character.", ephemeral=True)
        cur, mx = int(row["hp"]), int(row["max_hp"])
        newv = clamp(cur - amount if amount > 0 else cur + (-amount), 0, mx)
        await con.execute("UPDATE players SET hp=$1 WHERE guild_id=$2 AND user_id=$3", newv, gid, uid)
    await interaction.response.send_message(f"HP now **{newv}**/{mx}.")

@bot.tree.command(description="Spend or restore RV (positive spends, negative restores)")
async def rv(interaction: discord.Interaction, amount: int):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        row = await con.fetchrow("SELECT rv,max_rv FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid)
        if not row:
            return await interaction.response.send_message("No character.", ephemeral=True)
        cur, mx = int(row["rv"]), int(row["max_rv"])
        newv = clamp(cur - amount if amount > 0 else cur + (-amount), 0, mx)
        await con.execute("UPDATE players SET rv=$1 WHERE guild_id=$2 AND user_id=$3", newv, gid, uid)
    await interaction.response.send_message(f"RV now **{newv}**/{mx}.")

@bot.tree.command(description="Spend a Favor or GM-reset all to 1")
@app_commands.describe(spend="If true, spend 1 Favor", reset="GM only: reset everyone on this server to 1")
async def favor(interaction: discord.Interaction, spend: Optional[bool] = None, reset: Optional[bool] = None):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    if reset:
        if not isinstance(interaction.user, discord.Member) or not is_gm(interaction.user):
            return await interaction.response.send_message("GM only.", ephemeral=True)
        async with p.acquire() as con:
            await con.execute("UPDATE players SET favors=1 WHERE guild_id=$1", gid)
        return await interaction.response.send_message("Favors reset to 1 for this server.")
    if spend:
        async with p.acquire() as con:
            fav = int(await con.fetchval("SELECT favors FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid) or 0)
            if fav <= 0:
                return await interaction.response.send_message("You have no Favors left.", ephemeral=True)
            await con.execute("UPDATE players SET favors=favors-1 WHERE guild_id=$1 AND user_id=$2", gid, uid)
        return await interaction.response.send_message("Spent **1 Favor**.")
    else:
        async with p.acquire() as con:
            fav = int(await con.fetchval("SELECT favors FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid) or 0)
        return await interaction.response.send_message(f"You have **{fav}** Favor(s).", ephemeral=True)

# -------------------------------- Inventory ----------------------------------
@bot.tree.command(description="Add item to your inventory")
async def inv_add(interaction: discord.Interaction, item: str, qty: Optional[int] = 1):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    qty = max(1, qty or 1)
    p = pool()
    async with p.acquire() as con:
        await con.execute(
            "INSERT INTO inventory (guild_id,user_id,item,qty) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (guild_id,user_id,item) DO UPDATE SET qty = inventory.qty + EXCLUDED.qty",
            gid, uid, item, qty
        )
    await interaction.response.send_message(f"Added {qty}× {item}.")

@bot.tree.command(description="Remove item(s) from inventory")
async def inv_remove(interaction: discord.Interaction, item: str, qty: Optional[int] = 1):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    qty = max(1, qty or 1)
    p = pool()
    async with p.acquire() as con:
        await con.execute("UPDATE inventory SET qty = GREATEST(qty - $1, 0) WHERE guild_id=$2 AND user_id=$3 AND item=$4",
                          qty, gid, uid, item)
        await con.execute("DELETE FROM inventory WHERE guild_id=$1 AND user_id=$2 AND item=$3 AND qty<=0",
                          gid, uid, item)
    await interaction.response.send_message(f"Removed {qty}× {item}.")

@bot.tree.command(description="List your inventory")
async def inv_list(interaction: discord.Interaction):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        rows = await con.fetch("SELECT item, qty FROM inventory WHERE guild_id=$1 AND user_id=$2 ORDER BY item", gid, uid)
    text = "\n".join([f"• {r['item']}×{r['qty']}" for r in rows]) or "(empty)"
    await interaction.response.send_message("**Inventory**\n" + text, ephemeral=True)

# --------------------------------- Bonuses -----------------------------------
@bot.tree.command(description="Add a skill bonus (can be negative). These stack until removed.")
@app_commands.describe(skill="Skill to affect", amount="Bonus (±)", reason="Why (item, effect, etc.)")
@app_commands.choices(skill=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS])
async def bonus_add(interaction: discord.Interaction, skill: app_commands.Choice[str], amount: int, reason: Optional[str] = None):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        await con.execute("INSERT INTO bonuses (guild_id,user_id,skill,bonus,reason) VALUES ($1,$2,$3,$4,$5)",
                          gid, uid, skill.value, amount, reason)
    await interaction.response.send_message(f"Added bonus **{amount:+}** to *{skill.name}*.")

@bot.tree.command(description="List your active skill bonuses")
async def bonus_list(interaction: discord.Interaction):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        rows = await con.fetch(
            "SELECT id, skill, bonus, COALESCE(reason,'') AS reason FROM bonuses WHERE guild_id=$1 AND user_id=$2 ORDER BY id",
            gid, uid
        )
    if not rows:
        return await interaction.response.send_message("No bonuses.", ephemeral=True)
    text = "\n".join([f"#{r['id']}: {r['skill']} {r['bonus']:+} — {r['reason']}" for r in rows])
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(description="Remove a bonus by its #id (see /bonus_list)")
async def bonus_remove(interaction: discord.Interaction, bonus_id: int):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        await con.execute("DELETE FROM bonuses WHERE id=$1 AND guild_id=$2 AND user_id=$3", bonus_id, gid, uid)
    await interaction.response.send_message(f"Removed bonus #{bonus_id}.")

# -------------------------------- Weaknesses ---------------------------------
@bot.tree.command(description="Add a weakness (max two stored)")
async def weakness_add(interaction: discord.Interaction, text: str):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        count = int(await con.fetchval("SELECT COUNT(*) FROM weaknesses WHERE guild_id=$1 AND user_id=$2", gid, uid) or 0)
        if count >= 2:
            return await interaction.response.send_message(
                "You already have two weaknesses. Use /weakness_remove first.", ephemeral=True
            )
        await con.execute("INSERT INTO weaknesses (guild_id,user_id,text) VALUES ($1,$2,$3)", gid, uid, text)
    await interaction.response.send_message("Weakness saved.")

@bot.tree.command(description="List your weaknesses")
async def weakness_list(interaction: discord.Interaction):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        rows = await con.fetch("SELECT id, text FROM weaknesses WHERE guild_id=$1 AND user_id=$2", gid, uid)
    if not rows:
        return await interaction.response.send_message("(none)", ephemeral=True)
    await interaction.response.send_message("\n".join([f"#{r['id']}: {r['text']}" for r in rows]), ephemeral=True)

@bot.tree.command(description="Remove a weakness by id")
async def weakness_remove(interaction: discord.Interaction, weakness_id: int):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        await con.execute("DELETE FROM weaknesses WHERE id=$1 AND guild_id=$2 AND user_id=$3", weakness_id, gid, uid)
    await interaction.response.send_message("Weakness removed.")

# ---------------------------- Lineage Abilities ------------------------------
@bot.tree.command(description="(Whitelighter) Orbing once per scene (RP helper)")
async def orbing(interaction: discord.Interaction, note: Optional[str] = None):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        lin = await con.fetchval("SELECT lineage FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid)
        if lin != "whitelighter":
            return await interaction.response.send_message("Only Whitelighters can Orb.", ephemeral=True)
        used = await con.fetchval(
            "SELECT used FROM ability_usage WHERE guild_id=$1 AND user_id=$2 AND ability='orbing'", gid, uid
        )
        if used:
            return await interaction.response.send_message("Already Orbed this scene. Use /rest scope:scene.", ephemeral=True)
        await con.execute(
            "INSERT INTO ability_usage (guild_id,user_id,ability,scope,used) VALUES ($1,$2,'orbing','scene',TRUE) "
            "ON CONFLICT (guild_id,user_id,ability) DO UPDATE SET used=TRUE",
            gid, uid
        )
    await interaction.response.send_message(f"✨ Orbing activated. {note or ''}")

@bot.tree.command(description="(Whitelighter) Healing once per rest: +3 HP or +2 RV to a target")
@app_commands.describe(target="Who to heal", resource="HP +3 or RV +2")
@app_commands.choices(resource=[
    app_commands.Choice(name="HP +3", value="hp"),
    app_commands.Choice(name="RV +2", value="rv"),
])
async def healing(interaction: discord.Interaction, target: discord.Member, resource: app_commands.Choice[str]):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        lin = await con.fetchval("SELECT lineage FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid)
        if lin != "whitelighter":
            return await interaction.response.send_message("Only Whitelighters can Heal.", ephemeral=True)
        used = await con.fetchval(
            "SELECT used FROM ability_usage WHERE guild_id=$1 AND user_id=$2 AND ability='healing'", gid, uid
        )
        if used:
            return await interaction.response.send_message("Healing already used this rest. Use /rest scope:rest.", ephemeral=True)
        await ensure_player(gid, target.id, target.display_name)
        if resource.value == "hp":
            await con.execute("UPDATE players SET hp=LEAST(max_hp, hp+3) WHERE guild_id=$1 AND user_id=$2", gid, target.id)
        else:
            await con.execute("UPDATE players SET rv=LEAST(max_rv, rv+2) WHERE guild_id=$1 AND user_id=$2", gid, target.id)
        await con.execute(
            "INSERT INTO ability_usage (guild_id,user_id,ability,scope,used) VALUES ($1,$2,'healing','rest',TRUE) "
            "ON CONFLICT (guild_id,user_id,ability) DO UPDATE SET used=TRUE",
            gid, uid
        )
    await interaction.response.send_message(f"Healing applied to {target.display_name}.")

@bot.tree.command(description="Reset scene/rest counters; long_rest fully restores RV")
@app_commands.describe(scope="scene or rest", long_rest="If true and scope=rest, refill RV to max")
@app_commands.choices(scope=[
    app_commands.Choice(name="scene", value="scene"),
    app_commands.Choice(name="rest", value="rest"),
])
async def rest(interaction: discord.Interaction, scope: app_commands.Choice[str], long_rest: Optional[bool] = False):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    gid = interaction.guild_id
    uid = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        if scope.value == "scene":
            await con.execute("UPDATE ability_usage SET used=FALSE WHERE guild_id=$1 AND user_id=$2 AND scope='scene'", gid, uid)
            msg = "Scene counters reset."
        else:
            await con.execute("UPDATE ability_usage SET used=FALSE WHERE guild_id=$1 AND user_id=$2 AND scope='rest'", gid, uid)
            msg = "Rest counters reset."
            if long_rest:
                await con.execute("UPDATE players SET rv=max_rv WHERE guild_id=$1 AND user_id=$2", gid, uid)
                msg += " RV fully restored."
    await interaction.response.send_message(msg)

# ------------------------------ GM advancement -------------------------------
@bot.tree.command(description="[GM] Adjust a player's skill (caps at +5)")
@app_commands.describe(member="Who", skill="Which skill", amount="How many points (±)")
@app_commands.choices(skill=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS])
async def gm_skill(interaction: discord.Interaction, member: discord.Member, skill: app_commands.Choice[str], amount: int):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not is_gm(interaction.user):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    gid = interaction.guild_id
    await ensure_player(gid, member.id, member.display_name)
    p = pool()
    async with p.acquire() as con:
        cur = int(await con.fetchval(
            "SELECT points FROM skills WHERE guild_id=$1 AND user_id=$2 AND skill=$3", gid, member.id, skill.value
        ) or 0)
        newv = clamp(cur + amount, 0, 5)
        await con.execute(
            "UPDATE skills SET points=$1 WHERE guild_id=$2 AND user_id=$3 AND skill=$4",
            newv, gid, member.id, skill.value
        )
    await interaction.response.send_message(f"{member.display_name}'s {skill.name} is now +{newv}.")

# ---------------------------------- RUN --------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in the environment.")

if __name__ == "__main__":
    # Keep-alive web server for Render free plan
    start_keep_alive()
    bot.run(TOKEN)
