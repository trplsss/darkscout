import asyncio, json, os, re, urllib.parse
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession

load_dotenv()
HERE = os.path.dirname(os.path.abspath(__file__))
client = None  # создаётся при старте
KEY = os.environ.get("SITE_KEY", "")  # пароль на вход в сайт


def auth(k: str = ""):
    if KEY and k != KEY:
        raise HTTPException(401)

# Только контент для взрослых: запросы про несовершеннолетних блокируются.
BAD = re.compile(r"child|kid|minor|underage|preteen|school|дет[иск]|ребён|ребен|школьн|малолет|несовершеннолет|подрост", re.I)
LINK = re.compile(r"(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z][A-Za-z0-9_]{4,31})")
SKIP = {"joinchat", "addstickers", "share", "proxy", "socks", "login", "iv", "setlanguage", "addemoji"}


@asynccontextmanager
async def lifespan(app):
    global client
    client = TelegramClient(StringSession(os.environ.get("SESSION", "")), int(os.environ["API_ID"]), os.environ["API_HASH"])
    await client.connect()
    await sensitive_on()
    yield
    await client.disconnect()


app = FastAPI(lifespan=lifespan)


async def rpc(req):
    for _ in range(3):
        try:
            return await client(req)
        except FloodWaitError as e:
            if e.seconds > 90:
                raise
            await asyncio.sleep(e.seconds + 1)


def variants(q):
    return [q, f"{q} 18+", f"{q} порно", f"{q} xxx", f"{q} video"]


async def web_search(v):
    names = set()
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as h:
        for query in (f"site:t.me {v} 18+", f"site:t.me/s {v} porn"):
            try:
                r = await h.post("https://html.duckduckgo.com/html/", data={"q": query})
                names |= {m.lower() for m in LINK.findall(urllib.parse.unquote(r.text)) if m.lower() not in SKIP}
            except Exception:
                pass
    return names


async def discover(q, web):
    found = set()

    def add(chats):
        for c in chats:
            if isinstance(c, types.Channel) and c.broadcast and c.username:
                found.add(c.username.lower())

    for v in variants(q):
        try:
            add((await rpc(functions.contacts.SearchRequest(q=v, limit=100))).chats)
            r = await rpc(functions.messages.SearchGlobalRequest(
                q=v, filter=types.InputMessagesFilterVideo(), min_date=None, max_date=None,
                offset_rate=0, offset_peer=types.InputPeerEmpty(), offset_id=0, limit=100, broadcasts_only=True))
            add(r.chats)
        except Exception as e:
            print("tg search:", e)
        if web:
            found |= await web_search(v)
    return found


async def related(username):
    """Каналы, на которые ссылается/из которых репостит канал — так находятся самые «тихие» каналы."""
    names = set()
    try:
        async for m in client.iter_messages(username, limit=100):
            ch = m.forward.chat if m.forward else None
            if ch is not None and getattr(ch, "username", None):
                names.add(ch.username.lower())
            names |= {x.lower() for x in LINK.findall(m.message or "") if x.lower() not in SKIP}
    except Exception:
        pass
    return names


async def analyze(name):
    try:
        e = await client.get_entity(name)
        if not (isinstance(e, types.Channel) and e.broadcast) or e.scam or e.fake:
            return None
        full = await rpc(functions.channels.GetFullChannelRequest(e))
        vid = (await client.get_messages(e, limit=1, filter=types.InputMessagesFilterVideo)).total
        pho = (await client.get_messages(e, limit=1, filter=types.InputMessagesFilterPhotos)).total
        last = await client.get_messages(e, limit=1)
        total = last.total or 1
        date = last[0].date if last else None
        age = (datetime.now(timezone.utc) - date).days if date else 9999
        ratio = round(vid / total, 3)
        subs = full.full_chat.participants_count or 0
        return dict(username=e.username or name, title=e.title, about=(full.full_chat.about or "")[:220],
                    subs=subs, videos=vid, photos=pho, total=total, ratio=ratio,
                    last=date.isoformat() if date else None, age=age,
                    score=round(vid * (0.5 + ratio) * (1.25 if age < 30 else 1), 1))
    except FloodWaitError:
        await asyncio.sleep(5)
    except Exception:
        pass
    return None


async def sensitive_on():  # показ «чувствительного» контента, если аккаунту это разрешено
    try:
        if await client.is_user_authorized():
            c = await client(functions.account.GetContentSettingsRequest())
            if c.sensitive_can_change and not c.sensitive_enabled:
                await client(functions.account.SetContentSettingsRequest(sensitive_enabled=True))
    except Exception as e:
        print("content settings:", e)


@app.get("/api/state", dependencies=[Depends(auth)])
async def state():
    return {"ok": await client.is_user_authorized()}


@app.post("/api/login", dependencies=[Depends(auth)])
async def login(req: Request):
    d = await req.json()
    try:
        if d.get("phone"):
            r = await client.send_code_request(d["phone"])
            app.state.ph = (d["phone"], r.phone_code_hash)
            return {"step": "code"}
        phone, h = app.state.ph
        try:
            await client.sign_in(phone, d["code"], phone_code_hash=h)
        except SessionPasswordNeededError:
            if not d.get("password"):
                return {"step": "password"}
            await client.sign_in(password=d["password"])
        await sensitive_on()
        return {"step": "done", "session": client.session.save()}
    except Exception as e:
        return {"error": str(e)[:200]}


@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/api/search", dependencies=[Depends(auth)])
async def search(q: str, min_videos: int = 30, web: bool = True, deep: bool = False):
    def ev(**k):
        return f"data: {json.dumps(k, ensure_ascii=False)}\n\n"

    async def gen():
        q_ = q.strip()
        if not q_ or BAD.search(q_):
            yield ev(type="error", text="Запрос пустой или недопустим.")
            return
        if not await client.is_user_authorized():
            yield ev(type="error", text="Сначала войдите в Telegram (обновите страницу).")
            return
        yield ev(type="status", text="Ищу по Telegram и в интернете…", p=5)
        task = asyncio.create_task(discover(q_, web))
        while not task.done():  # «пинги», чтобы хостинг не рвал соединение
            await asyncio.wait({task}, timeout=10)
            yield ": ping\n\n"
        seen = task.result()
        batch, good, checked = list(seen), [], 0
        yield ev(type="status", text=f"Кандидатов: {len(seen)}. Считаю видео…", p=20)
        for depth in range(2 if deep else 1):
            sem = asyncio.Semaphore(4)

            async def job(n):
                async with sem:
                    return await analyze(n)

            for fut in asyncio.as_completed([job(n) for n in batch]):
                r = await fut
                checked += 1
                if r and r["videos"] >= min_videos:
                    good.append(r)
                    yield ev(type="channel", channel=r)
                yield ev(type="status", text=f"Проверено {checked} из {len(seen)} · подходящих {len(good)}",
                         p=min(95, 20 + int(75 * checked / max(len(seen), 1))))
            if depth == 0 and deep and good:
                yield ev(type="status", text="Глубокий поиск: изучаю связанные каналы…", p=92)
                nxt = set()
                for g in sorted(good, key=lambda x: -x["score"])[:12]:
                    nxt |= await related(g["username"])
                batch = list(nxt - seen)
                seen |= nxt
        yield ev(type="done", total=len(good), checked=checked, p=100)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
