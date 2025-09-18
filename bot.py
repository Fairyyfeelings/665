"""
665 — Avrae-style TTRPG Discord Bot (discord.py + asyncpg Postgres)
Adds multi-NPC sheets and 'roll by name'.

ENV (Render -> your service -> Environment):
- DISCORD_TOKEN = <your Discord bot token>
- DATABASE_URL  = postgresql://USER:PASSWORD@HOST:PORT/DB?sslmode=require
- GUILD_ID      = <optional server id for instant slash sync>

Start command on Render: python bot.py
Build command:          pip install -r requirements.txt
"""

# ------------------------- keep-alive (in this file) -------------------------
from flask import Flask
from threading import Thread
import os, random
from typing import Optional, List, Tuple, Literal

app = Flask(__name__)

@app.get("/")
def home():
    return "665 is alive."

def _run_keepalive():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

def start_keep_alive():
    Thread(target=_run_keepalive, daemon=True).start()

# ---------------------------- bot + database ---------------------------------
import discord
from discord.ext import commands
from discord import app_commands
import asyncpg

BOT_NAME = "665"
EMOJI_STAR = "✨️"  # totals show as **✨️<n>✨️**
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)  # optional: for fast slash sync

# Skills (your 10)
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
    # ---- PCs (existing tables; unchanged) ----
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
    """
    CREATE TABLE IF NOT EXISTS skills (
        guild_id BIGINT,
        user_id  BIGINT,
        skill TEXT,
        points INTEGER,
        PRIMARY KEY (guild_id, user_id, skill)
    );
    """,
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
    """
    CREATE TABLE IF NOT EXISTS inventory (
        guild_id BIGINT,
        user_id BIGINT,
        item TEXT,
        qty INTEGER,
        PRIMARY KEY (guild_id, user_id, item)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS weaknesses (
        id BIGSERIAL PRIMARY KEY,
        guild_id BIGINT,
        user_id BIGINT,
        text TEXT
    );
    """,
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

    # ---- NPCs (new) ----
    """
    CREATE TABLE IF NOT EXISTS npc_chars (
        id BIGSERIAL PRIMARY KEY,
        guild_id BIGINT,
        owner_id BIGINT,      -- GM who created/controls this NPC
        name TEXT,
        quote TEXT,
        lineage TEXT,
        hp INTEGER DEFAULT 10,
        max_hp INTEGER DEFAULT 10,
        rv INTEGER DEFAULT 5,
        max_rv INTEGER DEFAULT 5,
        favors INTEGER DEFAULT 0,
        connections INTEGER DEFAULT 0,
        home TEXT,
        transport TEXT,
        wealth TEXT,
        UNIQUE (guild_id, owner_id, name)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS npc_skills (
        npc_id BIGINT,
        skill TEXT,
        points INTEGER,
        PRIMARY KEY (npc_id, skill),
        FOREIGN KEY (npc_id) REFERENCES npc_chars(id) ON DELETE CASCADE
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS npc_bonuses (
        id BIGSERIAL PRIMARY KEY,
        npc_id BIGINT,
        skill TEXT,
        bonus INTEGER,
        reason TEXT,
        FOREIGN KEY (npc_id) REFERENCES npc_chars(id) ON DELETE CASCADE
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
        intents = discord.Intents.default()
        intents.message_content = True  # needed for !r prefix command
        super().__init__(command_prefix="!", intents=intents)
        self.pool: Optional[asyncpg.Pool] = None

    async def setup_hook(self):
        db_url = await get_pool_url()
        self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
        await init_db(self.pool)

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[665] Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            print(f"[665] Synced {len(synced)} global commands")

bot = TTRPGBot()

@bot.event
async def on_ready():
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.playing, name="/sheet • /roll • !r • /npc_create")
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

async def get_skill_total_pc(gid: int, uid: int, skill: str) -> Tuple[int, int, int]:
    s = slug(skill)
    p = pool()
    async with p.acquire() as con:
        base = int(await con.fetchval(
            "SELECT points FROM skills WHERE guild_id=$1 AND user_id=$2 AND skill=$3", gid, uid, s
        ) or 0)
        bonus = int(await con.fetchval(
            "SELECT COALESCE(SUM(bonus),0) FROM bonuses WHERE guild_id=$1 AND user_id=$2 AND skill=$3",
            gid, uid, s
        ) or 0)
    return base, bonus, base + bonus

# ---- NPC helpers ----
async def npc_get(gid: int, owner_id: int, name: str):
    p = pool()
    async with p.acquire() as con:
        return await con.fetchrow("SELECT * FROM npc_chars WHERE guild_id=$1 AND owner_id=$2 AND name=$3",
                                  gid, owner_id, name)

async def npc_ensure_skills(npc_id: int):
    p = pool()
    async with p.acquire() as con:
        for sk in SKILLS:
            await con.execute(
                "INSERT INTO npc_skills (npc_id,skill,points) VALUES ($1,$2,0) ON CONFLICT DO NOTHING",
                npc_id, sk
            )

async def npc_skill_total(npc_id: int, skill: str) -> Tuple[int,int,int]:
    s = slug(skill)
    p = pool()
    async with p.acquire() as con:
        base = int(await con.fetchval("SELECT points FROM npc_skills WHERE npc_id=$1 AND skill=$2", npc_id, s) or 0)
        bonus = int(await con.fetchval("SELECT COALESCE(SUM(bonus),0) FROM npc_bonuses WHERE npc_id=$1 AND skill=$2",
                                       npc_id, s) or 0)
    return base, bonus, base + bonus

# A generic "actor" fetch used by rolling/sheet APIs
ActorKind = Literal["pc","npc"]
class Actor:
    def __init__(self, kind: ActorKind, gid: int, uid: int, name: Optional[str] = None, npc_id: Optional[int] = None):
        self.kind = kind
        self.gid = gid
        self.uid = uid
        self.name = name
        self.npc_id = npc_id

async def resolve_actor_for_roll(gid: int, uid: int, as_name: Optional[str]) -> Actor:
    """
    If as_name is provided, try NPC (owned by uid). Otherwise, PC.
    """
    if as_name:
        npc = await npc_get(gid, uid, as_name)
        if npc:
            return Actor("npc", gid, uid, name=npc["name"], npc_id=int(npc["id"]))
    # default to PC
    return Actor("pc", gid, uid)

# --------------------------- Character Creation (PC) -------------------------
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
    human_bonus_skill="(HUMAN) +1 to any one of your chosen skills",
    witch_bonus_skill="(WITCH) +2 to any one of your chosen skills",
)
@app_commands.choices(
    lineage=[app_commands.Choice(name=x.capitalize(), value=x) for x in LINEAGES],
    plus3=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus2_a=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus2_b=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus1_a=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus1_b=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    human_bonus_skill=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    witch_bonus_skill=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
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
    human_bonus_skill: Optional[app_commands.Choice[str]] = None,
    witch_bonus_skill: Optional[app_commands.Choice[str]] = None,
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
    picked_set = set(picks)

    await ensure_player(gid, uid, name=name, lineage=lineage.value)
    p = pool()
    async with p.acquire() as con:
        # Reset to baseline
        await con.execute("""
            UPDATE players
            SET hp=10, max_hp=10, rv=5, max_rv=5, favors=1, connections=0
            WHERE guild_id=$1 AND user_id=$2
        """, gid, uid)
        await con.execute("DELETE FROM ability_usage WHERE guild_id=$1 AND user_id=$2", gid, uid)

        # Reset skills then apply distribution
        await con.execute("UPDATE skills SET points=0 WHERE guild_id=$1 AND user_id=$2", gid, uid)
        await con.execute("UPDATE skills SET points=3 WHERE guild_id=$1 AND user_id=$2 AND skill=$3", gid, uid, plus3.value)
        for s in [plus2_a.value, plus2_b.value]:
            await con.execute("UPDATE skills SET points=2 WHERE guild_id=$1 AND user_id=$2 AND skill=$3", gid, uid, s)
        for s in [plus1_a.value, plus1_b.value]:
            await con.execute("UPDATE skills SET points=1 WHERE guild_id=$1 AND user_id=$2 AND skill=$3", gid, uid, s)

        # lineage effects
        start_text = "HP 10, RV 5, Favors 1"
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
            start_text += " (+2 RV from Whitelighter)"
        elif lineage.value == "human":
            await con.execute("UPDATE players SET connections=connections+3 WHERE guild_id=$1 AND user_id=$2", gid, uid)
            if human_bonus_skill and human_bonus_skill.value in picked_set:
                await con.execute(
                    "UPDATE skills SET points=LEAST(points+1,5) WHERE guild_id=$1 AND user_id=$2 AND skill=$3",
                    gid, uid, human_bonus_skill.value
                )
                start_text += f" (+3 connections; +1 to {human_bonus_skill.value.title()})"
            else:
                start_text += " (+3 connections)"
        elif lineage.value == "witch":
            if witch_bonus_skill and witch_bonus_skill.value in picked_set:
                await con.execute(
                    "UPDATE skills SET points=LEAST(points+2,5) WHERE guild_id=$1 AND user_id=$2 AND skill=$3",
                    gid, uid, witch_bonus_skill.value
                )
                start_text += f" (+2 to {witch_bonus_skill.value.title()})"
        # demon: glamour is narrative

        await con.execute(
            "UPDATE players SET name=$1, quote=$2, lineage=$3 WHERE guild_id=$4 AND user_id=$5",
            name, quote, lineage.value, gid, uid
        )

    embed = discord.Embed(title=f"{name} created!", color=discord.Color.magenta())
    embed.add_field(name="Lineage", value=lineage.name)
    if quote:
        embed.add_field(name="Quote", value=f"“{quote}”", inline=False)
    embed.add_field(name="Starts with", value=start_text, inline=False)
    embed.set_footer(text="Use /sheet to view, /roll or !r to play.")
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
@bot.tree.command(description="Show your character sheet (PC or NPC by name)")
async def sheet(interaction: discord.Interaction, member: Optional[discord.Member] = None, name: Optional[str] = None):
    """
    /sheet                 -> your PC sheet (ephemeral)
    /sheet member:@user    -> that user's PC sheet
    /sheet name:"Bob"      -> your NPC named Bob (owned by you)
    """
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)

    gid = interaction.guild_id
    p = pool()
    if name:
        # NPC owned by the caller
        npc = await npc_get(gid, interaction.user.id, name)
        if not npc:
            return await interaction.response.send_message("No such NPC (owned by you).", ephemeral=True)
        async with p.acquire() as con:
            sk = await con.fetch("SELECT skill, points FROM npc_skills WHERE npc_id=$1 ORDER BY skill", npc["id"])
        skills_text = ", ".join([f"{r['skill']} +{r['points']}" for r in sk if r["points"]]) or "(no skills set)"
        embed = discord.Embed(title=f"{npc['name']} — NPC ({npc['lineage'] or 'unknown'})", color=discord.Color.gold())
        if npc["quote"]:
            embed.description = f"“{npc['quote']}”"
        embed.add_field(name="❤️ HP", value=f"{npc['hp']} / {npc['max_hp']}")
        embed.add_field(name="🔘 RV", value=f"{npc['rv']} / {npc['max_rv']}")
        embed.add_field(name="Skills", value=skills_text, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # otherwise PC flow
    target = member or interaction.user
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

# --------------------------------- NPC CRUD ----------------------------------
@bot.tree.command(description="[GM] Create an NPC with lineage and skills (same flow as /create)")
@app_commands.describe(
    name="NPC name",
    lineage="Lineage",
    quote="Optional quote",
    plus3="Skill at +3",
    plus2_a="First +2 skill",
    plus2_b="Second +2 skill",
    plus1_a="First +1 skill",
    plus1_b="Second +1 skill",
    human_bonus_skill="(HUMAN) +1 to any one of the chosen skills",
    witch_bonus_skill="(WITCH) +2 to any one of the chosen skills",
)
@app_commands.choices(
    lineage=[app_commands.Choice(name=x.capitalize(), value=x) for x in LINEAGES],
    plus3=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus2_a=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus2_b=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus1_a=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    plus1_b=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    human_bonus_skill=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
    witch_bonus_skill=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS],
)
async def npc_create(
    interaction: discord.Interaction,
    name: str,
    lineage: app_commands.Choice[str],
    quote: Optional[str] = None,
    plus3: Optional[app_commands.Choice[str]] = None,
    plus2_a: Optional[app_commands.Choice[str]] = None,
    plus2_b: Optional[app_commands.Choice[str]] = None,
    plus1_a: Optional[app_commands.Choice[str]] = None,
    plus1_b: Optional[app_commands.Choice[str]] = None,
    human_bonus_skill: Optional[app_commands.Choice[str]] = None,
    witch_bonus_skill: Optional[app_commands.Choice[str]] = None,
):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("GM only.", ephemeral=True)

    gid = interaction.guild_id
    owner = interaction.user.id
    picks = [x.value for x in [plus3, plus2_a, plus2_b, plus1_a, plus1_b] if x]
    if len(picks) != 5 or len(set(picks)) != 5:
        return await interaction.response.send_message("Pick five different skills: +3, +2, +2, +1, +1.", ephemeral=True)
    picked_set = set(picks)

    p = pool()
    async with p.acquire() as con:
        # Upsert (replace if exists)
        existing = await con.fetchrow("SELECT id FROM npc_chars WHERE guild_id=$1 AND owner_id=$2 AND name=$3",
                                      gid, owner, name)
        if existing:
            npc_id = int(existing["id"])
            await con.execute("DELETE FROM npc_bonuses WHERE npc_id=$1", npc_id)
            await con.execute("DELETE FROM npc_skills WHERE npc_id=$1", npc_id)
            await con.execute("""
                UPDATE npc_chars SET quote=$1, lineage=$2, hp=10, max_hp=10, rv=5, max_rv=5, favors=0, connections=0
                WHERE id=$3
            """, quote, lineage.value, npc_id)
        else:
            row = await con.fetchrow(
                "INSERT INTO npc_chars (guild_id,owner_id,name,quote,lineage) VALUES ($1,$2,$3,$4,$5) RETURNING id",
                gid, owner, name, quote, lineage.value
            )
            npc_id = int(row["id"])

        await npc_ensure_skills(npc_id)
        # Base distribution
        await con.execute("UPDATE npc_skills SET points=0 WHERE npc_id=$1", npc_id)
        await con.execute("UPDATE npc_skills SET points=3 WHERE npc_id=$1 AND skill=$2", npc_id, plus3.value)
        for s in [plus2_a.value, plus2_b.value]:
            await con.execute("UPDATE npc_skills SET points=2 WHERE npc_id=$1 AND skill=$2", npc_id, s)
        for s in [plus1_a.value, plus1_b.value]:
            await con.execute("UPDATE npc_skills SET points=1 WHERE npc_id=$1 AND skill=$2", npc_id, s)

        start_text = "HP 10, RV 5"
        # Lineage effects
        if lineage.value == "whitelighter":
            await con.execute("UPDATE npc_chars SET rv=rv+2, max_rv=max_rv+2 WHERE id=$1", npc_id)
            start_text += " (+2 RV from Whitelighter)"
        elif lineage.value == "human":
            await con.execute("UPDATE npc_chars SET connections=connections+3 WHERE id=$1", npc_id)
            if human_bonus_skill and human_bonus_skill.value in picked_set:
                await con.execute("UPDATE npc_skills SET points=LEAST(points+1,5) WHERE npc_id=$1 AND skill=$2",
                                  npc_id, human_bonus_skill.value)
                start_text += f" (+3 connections; +1 to {human_bonus_skill.value.title()})"
            else:
                start_text += " (+3 connections)"
        elif lineage.value == "witch":
            if witch_bonus_skill and witch_bonus_skill.value in picked_set:
                await con.execute("UPDATE npc_skills SET points=LEAST(points+2,5) WHERE npc_id=$1 AND skill=$2",
                                  npc_id, witch_bonus_skill.value)
                start_text += f" (+2 to {witch_bonus_skill.value.title()})"
        # demon: glamour narrative only

    embed = discord.Embed(title=f"NPC '{name}' saved", color=discord.Color.gold())
    if quote:
        embed.add_field(name="Quote", value=f"“{quote}”", inline=False)
    embed.add_field(name="Lineage", value=lineage.name)
    embed.add_field(name="Starts with", value=start_text, inline=False)
    embed.set_footer(text="Use /npc_sheet name:<name> • roll with /roll as_name:<name> or !r ... as <name>")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(description="[GM] List your NPCs")
async def npc_list(interaction: discord.Interaction):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    gid = interaction.guild_id
    owner = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        rows = await con.fetch("SELECT name, lineage FROM npc_chars WHERE guild_id=$1 AND owner_id=$2 ORDER BY name", gid, owner)
    if not rows:
        return await interaction.response.send_message("(no NPCs yet)", ephemeral=True)
    text = "\n".join([f"• {r['name']} — {r['lineage']}" for r in rows])
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(description="[GM] Show an NPC sheet you own")
async def npc_sheet(interaction: discord.Interaction, name: str):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    gid = interaction.guild_id
    owner = interaction.user.id
    npc = await npc_get(gid, owner, name)
    if not npc:
        return await interaction.response.send_message("No such NPC.", ephemeral=True)
    p = pool()
    async with p.acquire() as con:
        sk = await con.fetch("SELECT skill, points FROM npc_skills WHERE npc_id=$1 ORDER BY skill", npc["id"])
    skills_text = ", ".join([f"{r['skill']} +{r['points']}" for r in sk if r["points"]]) or "(no skills set)"
    embed = discord.Embed(title=f"{npc['name']} — NPC ({npc['lineage'] or 'unknown'})", color=discord.Color.gold())
    if npc["quote"]:
        embed.description = f"“{npc['quote']}”"
    embed.add_field(name="❤️ HP", value=f"{npc['hp']} / {npc['max_hp']}")
    embed.add_field(name="🔘 RV", value=f"{npc['rv']} / {npc['max_rv']}")
    embed.add_field(name="Skills", value=skills_text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(description="[GM] Delete an NPC you own")
async def npc_delete(interaction: discord.Interaction, name: str):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    gid = interaction.guild_id
    owner = interaction.user.id
    p = pool()
    async with p.acquire() as con:
        row = await con.fetchrow("DELETE FROM npc_chars WHERE guild_id=$1 AND owner_id=$2 AND name=$3 RETURNING id",
                                 gid, owner, name)
    if row:
        return await interaction.response.send_message(f"Deleted NPC '{name}'.", ephemeral=True)
    return await interaction.response.send_message("No such NPC.", ephemeral=True)

@bot.tree.command(description="[GM] Damage/heal an NPC's HP (positive damages; negative heals)")
async def npc_hp(interaction: discord.Interaction, name: str, amount: int):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    gid = interaction.guild_id
    owner = interaction.user.id
    npc = await npc_get(gid, owner, name)
    if not npc:
        return await interaction.response.send_message("No such NPC.", ephemeral=True)
    cur, mx = int(npc["hp"]), int(npc["max_hp"])
    newv = clamp(cur - amount if amount > 0 else cur + (-amount), 0, mx)
    p = pool()
    async with p.acquire() as con:
        await con.execute("UPDATE npc_chars SET hp=$1 WHERE id=$2", newv, npc["id"])
    await interaction.response.send_message(f"{name} HP now **{newv}**/{mx}.", ephemeral=True)

@bot.tree.command(description="[GM] Spend/restore an NPC's RV (positive spends; negative restores)")
async def npc_rv(interaction: discord.Interaction, name: str, amount: int):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
        return await interaction.response.send_message("GM only.", ephemeral=True)
    gid = interaction.guild_id
    owner = interaction.user.id
    npc = await npc_get(gid, owner, name)
    if not npc:
        return await interaction.response.send_message("No such NPC.", ephemeral=True)
    cur, mx = int(npc["rv"]), int(npc["max_rv"])
    newv = clamp(cur - amount if amount > 0 else cur + (-amount), 0, mx)
    p = pool()
    async with p.acquire() as con:
        await con.execute("UPDATE npc_chars SET rv=$1 WHERE id=$2", newv, npc["id"])
    await interaction.response.send_message(f"{name} RV now **{newv}**/{mx}.", ephemeral=True)

# ----------------------------------- Rolls -----------------------------------
@bot.tree.command(description="Roll 1d20 + skill (+ bonuses). Supports advantage/disadvantage, DC, and 'as_name' for NPC.")
@app_commands.describe(
    skill="Which skill",
    advantage="Roll 2d20 take higher",
    disadvantage="Roll 2d20 take lower",
    dc="Optional difficulty to compare",
    bonus="Extra situational modifier",
    note="Footer note",
    as_name="Roll as your NPC with this name (owned by you)"
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
    as_name: Optional[str] = None,
):
    if advantage and disadvantage:
        return await interaction.response.send_message("Can't have both advantage and disadvantage.", ephemeral=True)
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)

    gid = interaction.guild_id
    uid = interaction.user.id
    actor = await resolve_actor_for_roll(gid, uid, as_name)

    if actor.kind == "pc":
        await ensure_player(gid, uid, interaction.user.display_name)
        base, extra, total_skill = await get_skill_total_pc(gid, uid, skill.value)
        who = interaction.user.display_name
    else:
        base, extra, total_skill = await npc_skill_total(actor.npc_id, skill.value)
        who = f"{as_name} (NPC)"

    d1, d2 = random.randint(1, 20), random.randint(1, 20)
    d20 = max(d1, d2) if advantage else (min(d1, d2) if disadvantage else d1)
    adv_note = f"Advantage ({d1}/{d2})" if advantage else (f"Disadvantage ({d1}/{d2})" if disadvantage else None)

    total = d20 + total_skill + int(bonus or 0)

    title = f"{who} rolls {skill.name}"
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

# ---------------------------- Avrae-style !r command -------------------------
def _pick_skill_token(tokens: List[str]) -> Optional[str]:
    low = [t.lower() for t in tokens]
    for s in SKILLS:
        if s in low:
            return s
    for s in SKILLS:
        for t in low:
            if len(t) >= 3 and s.startswith(t):
                return s
    return None

@bot.command(name="r", aliases=["roll"])
async def r_prefix(ctx: commands.Context, *, text: str = ""):
    """
    Avrae-style:
      !r 1d20 streetwise
      !r 1d20 adv
      !r 1d20 stealth dis vs 16 slipping past guards
      !r 1d20 vs 13
      !r 1d20 streetwise as Bob       (roll as your NPC named Bob)
    """
    if ctx.guild is None:
        return await ctx.reply("Use this in a server.")

    tokens = text.split()
    if not tokens:
        return await ctx.reply("Try: `!r 1d20 stealth` or `!r 1d20 dis vs 15`")

    # dice
    d_expr = tokens[0].lower()
    if "d" not in d_expr:
        return await ctx.reply("First token should be a dice expression like `1d20`.")
    try:
        dice_n, dice_sides = d_expr.split("d", 1)
        dice_n = int(dice_n or "1")
        dice_sides = int(dice_sides)
    except Exception:
        return await ctx.reply("Couldn’t parse dice. Use `1d20`.")
    tokens = tokens[1:]

    # adv/dis
    advantage = any(t.lower() in ("adv", "advantage") for t in tokens)
    disadvantage = any(t.lower() in ("dis", "disadvantage") for t in tokens)
    if advantage and disadvantage:
        return await ctx.reply("Can't have both advantage and disadvantage.")

    # DC: "vs 15" or "dc 15"
    dc = None
    i = 0
    while i < len(tokens)-1:
        t = tokens[i].lower()
        if t in ("vs", "dc"):
            try:
                dc = int(tokens[i+1])
                del tokens[i:i+2]
                continue
            except Exception:
                pass
        i += 1

    # 'as NAME' (NPC name) — single token name; use underscores for multiword
    as_name = None
    if "as" in [t.lower() for t in tokens]:
        for i, t in enumerate(list(tokens)):
            if t.lower() == "as" and i+1 < len(tokens):
                as_name = tokens[i+1]
                del tokens[i:i+2]
                break

    # skill: find one (supports partials)
    skill = _pick_skill_token(tokens)
    if skill:
        for i, tok in enumerate(list(tokens)):
            if tok.lower() == skill or (len(tok) >= 3 and skill.startswith(tok.lower())):
                tokens.pop(i)
                break

    note = " ".join(tokens).strip() or None

    gid = ctx.guild.id
    uid = ctx.author.id
    actor = await resolve_actor_for_roll(gid, uid, as_name)

    if actor.kind == "pc":
        await ensure_player(gid, uid, ctx.author.display_name)
        base = extra = total_skill = 0
        who = ctx.author.display_name
        if skill:
            base, extra, total_skill = await get_skill_total_pc(gid, uid, skill)
    else:
        base = extra = total_skill = 0
        who = f"{actor.name} (NPC)"
        if skill:
            base, extra, total_skill = await npc_skill_total(actor.npc_id, skill)

    # roll
    if dice_n < 1 or dice_sides < 1:
        return await ctx.reply("Dice must be positive.")
    if dice_sides == 20 and dice_n == 1 and (advantage or disadvantage):
        d1, d2 = random.randint(1, 20), random.randint(1, 20)
        d20 = max(d1, d2) if advantage else min(d1, d2)
        advtxt = f"Advantage ({d1}/{d2})" if advantage else f"Disadvantage ({d1}/{d2})"
        dice_total = d20
    else:
        rolls = [random.randint(1, dice_sides) for _ in range(min(dice_n, 50))]
        dice_total = sum(rolls)
        advtxt = None

    total = dice_total + total_skill

    title = f"{who} rolls {d_expr}"
    if skill:
        title += f" • {skill.title()}"

    embed = discord.Embed(title=title, color=discord.Color.blurple())
    if dice_sides == 20 and dice_n == 1:
        nat = " (CRIT!)" if dice_total == 20 else (" (BOTCH)" if dice_total == 1 else "")
        embed.add_field(name="d20", value=f"{dice_total}{nat}")
    else:
        embed.add_field(name="Dice", value=str(dice_total))
    if skill:
        embed.add_field(name="Skill base", value=f"+{base}")
        if extra:
            embed.add_field(name="Bonuses", value=f"+{extra}")
    embed.add_field(name="Total", value=f"**{EMOJI_STAR}{total}{EMOJI_STAR}**", inline=False)

    if dc is not None:
        outcome = "✅ Success" if (dice_total == 20 or total >= dc) else "❌ Failure"
        if dice_sides == 20 and dice_n == 1 and dice_total == 1:
            outcome = "❌ **Nat 1** (Complication)"
        elif dice_sides == 20 and dice_n == 1 and dice_total == 20:
            outcome = "✅ **Nat 20** (Automatic)"
        embed.add_field(name=f"vs DC {dc}", value=outcome, inline=False)

    footer_parts = []
    if advtxt:
        footer_parts.append(advtxt)
    if note:
        footer_parts.append(note)
    if footer_parts:
        embed.set_footer(text=" • ".join(footer_parts))

    await ctx.reply(embed=embed)

# ---------------------------- HP / RV / Favors (PC) --------------------------
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
        if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
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

# --------------------------------- Bonuses/Inv/Weakness (PC) -----------------
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
    text = "\n".join([f"• {r['item']}×{r['qty']}" for r in rows]) if rows else "(empty)"
    await interaction.response.send_message("**Inventory**\n" + text, ephemeral=True)

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

# ---------------------------- Lineage Abilities (PC) -------------------------
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
        # ensure target PC exists (if they haven't made a sheet yet, this will create a stub)
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

# ------------------------------ GM advancement (PC) --------------------------
@bot.tree.command(description="[GM] Adjust a player's skill (caps at +5)")
@app_commands.describe(member="Who", skill="Which skill", amount="How many points (±)")
@app_commands.choices(skill=[app_commands.Choice(name=s.title(), value=s) for s in SKILLS])
async def gm_skill(interaction: discord.Interaction, member: discord.Member, skill: app_commands.Choice[str], amount: int):
    if interaction.guild_id is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    if not isinstance(interaction.user, discord.Member) or not (interaction.user.guild_permissions.manage_guild or interaction.user.guild_permissions.administrator):
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

# --- Pregnancy Check Command -----------------------------------------------
import random
from discord import app_commands
from discord import Interaction

def d20():
    return random.randint(1, 20)

def d100():
    return random.randint(1, 100)

# If you already have a `tree = app_commands.CommandTree(bot)` or similar, reuse it.
# Replace `tree` below with your actual CommandTree variable if different.

@tree.command(name="pregnancy", description="Resolve a pregnancy check with optional pull-out/protection and twins.")
@app_commands.describe(
    dc="DC for each partner's d20 check (default 10).",
    impregnator_bonus="Bonus to the impregnator's d20 (default 0).",
    partner_bonus="Bonus to the potential pregnant character's d20 (default 0).",
    base_chance="Base conception chance % if unprotected & inside (default 25).",
    pull_out="If true, attempt to pull out (default False).",
    protection="Type of protection used (none/condom/spell).",
    condom_fail_pct="Condom failure % (default 2).",
    protection_residual_pct="Residual % chance if protection works (default 1).",
    pullout_fail_pct="Pull-out failure % (default 20).",
    pullout_residual_pct="Residual % if pull-out succeeds (default 5).",
    twin_chance_pct="Chance of twins % if pregnant (default 3).",
    twin_mod="Modifier to twin roll threshold (can be negative/positive).",
)
@app_commands.choices(
    protection=[
        app_commands.Choice(name="none", value="none"),
        app_commands.Choice(name="condom", value="condom"),
        app_commands.Choice(name="spell", value="spell"),  # magical ward, etc.
    ]
)
async def pregnancy(
    itx: Interaction,
    dc: int = 10,
    impregnator_bonus: int = 0,
    partner_bonus: int = 0,
    base_chance: int = 25,
    pull_out: bool = False,
    protection: app_commands.Choice[str] = None,
    condom_fail_pct: int = 2,
    protection_residual_pct: int = 1,
    pullout_fail_pct: int = 20,
    pullout_residual_pct: int = 5,
    twin_chance_pct: int = 3,
    twin_mod: int = 0,
):
    """
    Flow:
    1) Opposed-style: both roll d20 vs DC. If either fails -> not pregnant.
    2) If both succeed: compute final conception % with pull-out/protection gates.
    3) Roll d100 vs final % for pregnancy. If pregnant, roll twins.
    """

    # 1) d20 checks
    him = d20()
    her = d20()
    him_total = him + impregnator_bonus
    her_total = her + partner_bonus
    both_pass = (him_total >= dc) and (her_total >= dc)

    # Early message scaffolding
    lines = []
    lines.append("**Pregnancy Check**")
    lines.append(f"• DC: **{dc}**")
    lines.append(f"• Impregnator roll: d20 (**{him}**) + {impregnator_bonus} = **{him_total}** → {'✅ success' if him_total >= dc else '❌ fail'}")
    lines.append(f"• Partner roll: d20 (**{her}**) + {partner_bonus} = **{her_total}** → {'✅ success' if her_total >= dc else '❌ fail'}")

    if not both_pass:
        await itx.response.send_message("\n".join(lines) + "\n\n**Result:** Not pregnant (one or both checks failed).")
        return

    # 2) Compute modified conception chance
    final_pct = float(base_chance)
    notes = [f"Base: {base_chance}%"]

    # Pull-out logic
    if pull_out:
        po_roll = d100()
        lines.append(f"• Pull-out attempt: d100 (**{po_roll}**) ≤ {pullout_fail_pct}% means **failed pull-out**")
        if po_roll <= pullout_fail_pct:
            notes.append("Pull-out failed: no reduction")
        else:
            notes.append(f"Pull-out succeeded: applying residual {pullout_residual_pct}% of current")
            final_pct *= (pullout_residual_pct / 100.0)

    # Protection logic
    protection_val = protection.value if protection else "none"
    if protection_val == "condom":
        prot_roll = d100()
        lines.append(f"• Condom check: d100 (**{prot_roll}**) ≤ {condom_fail_pct}% → **break**")
        if prot_roll <= condom_fail_pct:
            notes.append("Condom broke: no reduction")
        else:
            notes.append(f"Condom intact: applying residual {protection_residual_pct}% of current")
            final_pct *= (protection_residual_pct / 100.0)

    elif protection_val == "spell":
        # You can tune a ward however you like; here we use a tiny 1% fail and 1% residual by default
        ward_fail_pct = min(5, condom_fail_pct)  # small fail chance
        prot_roll = d100()
        lines.append(f"• Ward check: d100 (**{prot_roll}**) ≤ {ward_fail_pct}% → **ward fails**")
        if prot_roll <= ward_fail_pct:
            notes.append("Ward failed: no reduction")
        else:
            notes.append(f"Ward holds: applying residual {protection_residual_pct}% of current")
            final_pct *= (protection_residual_pct / 100.0)

    # Clamp and roll
    if final_pct < 0: final_pct = 0.0
    if final_pct > 100: final_pct = 100.0

    # 3) Conception roll
    conceive_roll = d100()
    conceived = conceive_roll <= final_pct

    lines.append(f"• Final conception chance: **{final_pct:.2f}%** ({', '.join(notes)})")
    lines.append(f"• Conception roll: d100 (**{conceive_roll}**) → {'**PREGNANT** ✅' if conceived else '**Not pregnant** ❌'}")

    # Twins if pregnant
    if conceived:
        twin_threshold = max(0, min(100, twin_chance_pct + twin_mod))
        t_roll = d100()
        is_twins = t_roll <= twin_threshold
        lines.append(f"• Twins check: d100 (**{t_roll}**) ≤ {twin_threshold}% → {'**TWINS** 👶👶' if is_twins else 'single pregnancy'}")

    await itx.response.send_message("\n".join(lines))
# --- End Pregnancy Command ---------------------------------------------------


# ---------------------------------- RUN --------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in the environment.")

if __name__ == "__main__":
    start_keep_alive()  # keep Render free plan happy
    bot.run(TOKEN)

