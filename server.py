"""
Local HTTP server: Phone -> Laptop -> ESP32-S3 + MAX98357A
Run: pip install -r requirements.txt
     python server.py
Then:
  Phone browser:  http://<LAPTOP-IP>:8000/
  ESP32 polls:    http://<LAPTOP-IP>:8000/api/next?last=N
                  and streams http://<LAPTOP-IP>:8000/audio/reply-N.mp3
"""
import asyncio
import json
import threading
from pathlib import Path
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import edge_tts

BASE = Path(__file__).parent
AUDIO_DIR = BASE / "audio"
STATIC_DIR = BASE / "static"
SFX_DIR = BASE / "sfx"
SONGS_DIR = BASE / "songs"  # song snippets: manual buttons only, never auto-played
AUDIO_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
SFX_DIR.mkdir(exist_ok=True)


def _load_dotenv():
    """Zero-dependency .env loader (KEY=VALUE, # comments, quoted values OK).
    Real env vars always win over .env."""
    p = BASE / ".env"
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
                v = v[1:-1]
            os.environ.setdefault(k, v)
    except Exception as e:
        print(f"[.env] failed to load: {e}")


_load_dotenv()

app = FastAPI()

# ---- state ----
lock = threading.Lock()
VERSION = 0
NEXT_ID = 0  # reserved IDs, so concurrent chats never share the same tmp/mp3
LAST_REPLY = "Hello from ESP32"
LAST_PROVIDER = "none"
LAST_AUDIO_URL = ""
LAST_ESP32_SEEN_VERSION = -1
import time as _time
import random as _random
LAST_SPOKEN_AT = _time.time()
NEXT_IDLE_AT = LAST_SPOKEN_AT + 60.0
VERSION_FILE = BASE / "version.json"


def _save_version():
    try:
        VERSION_FILE.write_text(json.dumps({"version": VERSION, "next": NEXT_ID}))
    except Exception:
        pass


def _load_version():
    global VERSION, NEXT_ID, LAST_AUDIO_URL
    try:
        d = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        VERSION = int(d.get("version", 0))
        NEXT_ID = max(int(d.get("next", 0)), VERSION)
        if VERSION > 0:
            LAST_AUDIO_URL = f"/audio/reply-{VERSION}.mp3"
    except Exception:
        pass


def _publish(audio_url: str, reply_label: str):
    """Synchronously publish an already-existing file (SFX / idle)."""
    global VERSION, NEXT_ID, LAST_REPLY, LAST_AUDIO_URL, LAST_SPOKEN_AT
    with lock:
        NEXT_ID += 1
        v = NEXT_ID
        VERSION = v
        LAST_REPLY = reply_label
        LAST_AUDIO_URL = audio_url
        LAST_SPOKEN_AT = _time.time()
        _save_version()
    return v


_load_version()
# clear stale tmps from crashes (they cause WinError 32 on next replace)
for _stale in AUDIO_DIR.glob("reply-*.tmp*.mp3"):
    try:
        _stale.unlink()
    except Exception:
        pass

# ---- WALL-E sound effects ----
# name -> (keywords in user prompt, phone-display label)
SFX_TRIGGERS = {
    "whistle": (["whistle", "whistling", "tune", "song", "sing"], "*WALL-E whistles*"),
    "whoopie": (["happy", "yay", "woohoo", "whoopie", "celebrat", "excited", "great news", "yahoo"], "*WALL-E cheers*"),
    "sad": (["sad", "cry", "crying", "upset", "lonely", "sorry"], "*WALL-E whimpers*"),
    "scared": (["scared", "afraid", "fear", "spooky", "boo", "scary"], "*WALL-E trembles*"),
    "growl": (["angry", "growl", "grr", "mad", "annoyed"], "*WALL-E growls*"),
    "inlove": (["love", "eve", "e-va", "crush", "like you", "beautiful"], "*WALL-E swoons*"),
    "walle-hello": (["your name", "who are you", "introduce yourself"], "*WALL-E says his name*"),
    "walle-2": ([], "*WALL-E chirps*"),  # buttons / idle only
}
IDLE_SFX = os.getenv("IDLE_SFX", "whistle")  # played when WALL-E hasn't spoken in a while
IDLE_WHISTLE_AFTER = float(os.getenv("IDLE_WHISTLE_AFTER", "60"))  # seconds of silence first
IDLE_WHISTLE_MIN = float(os.getenv("IDLE_WHISTLE_MIN", "45"))      # random gap range...
IDLE_WHISTLE_MAX = float(os.getenv("IDLE_WHISTLE_MAX", "90"))      # ...between idle sounds
NEXT_IDLE_AT = LAST_SPOKEN_AT + IDLE_WHISTLE_AFTER  # first idle sound one quiet period after boot


def match_sfx(user_text: str):
    """Keyword fallback when no AI is available (offline/echo mode)."""
    t = (user_text or "").lower()
    for name, (keywords, _label) in SFX_TRIGGERS.items():
        if not (SFX_DIR / f"{name}.mp3").exists():
            continue
        for kw in keywords:
            if kw and kw in t:
                return name
    return ""


# Tool definition for AI intent detection: the model decides when a sound fits.
SFX_TOOL = {
    "type": "function",
    "function": {
        "name": "play_sound",
        "description": ("Play one WALL-E sound effect on the speaker. Use only when the user's "
                        "message strongly calls for it — an explicit request (whistle, growl) or a "
                        "strong emotion (sad, scared, happy, in love, angry). At most one per reply, "
                        "and prefer just speaking when unsure."),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "enum": ["whistle", "whoopie", "sad", "scared", "growl",
                                  "inlove", "walle-hello", "walle-2"],
                         "description": "whistle: playful tune. whoopie: joyful cheer. sad: whimper. "
                                        "scared: tremble. growl: grumpy warning. inlove: swooning at EVE/love. "
                                        "walle-hello: saying his own name. walle-2: generic chirp."}
            },
            "required": ["name"],
        },
    },
}
LAST_TOOL_SFX = ""  # sfx name chosen by the model on the latest AI turn ("" = none)
PENDING_FOLLOWUPS = []  # [{after: version, text: reply}] spoken right after an sfx finishes

# Config via env
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural")  # WALL-E: deep-ish male base, detuned below
TTS_RATE = os.getenv("TTS_RATE", "-10%")   # slower = more mechanical
TTS_PITCH = os.getenv("TTS_PITCH", "+15Hz")  # a touch higher = small cute robot
USE_OLLAMA = os.getenv("USE_OLLAMA", "0") == "1"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
# OpenRouter (https://openrouter.ai) — set your key, rest works with stdlib only
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-20b")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def openrouter_reply(prompt: str) -> str:
    """Chat via OpenRouter (with play_sound tool for SFX intent).
    Returns reply text (may be '' if only a sound was chosen).
    Sets LAST_TOOL_SFX. Returns '' on failure so caller can fall back."""
    global LAST_TOOL_SFX
    LAST_TOOL_SFX = ""
    if not OPENROUTER_API_KEY:
        return ""
    try:
        import urllib.request
        from datetime import datetime
        now = datetime.now().strftime("%A, %d %B %Y, %I:%M %p")
        payload = json.dumps({
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": f"You are WALL-E, the little trash-compactor robot from the movies: warm, curious, speaks simply like he's still learning words. You have a play_sound tool with your real sound effects — call it when the user explicitly asks for a sound or their emotion strongly fits one (max one per reply). IMPORTANT: always also write a short in-character spoken line for the user, and never ever mention tools, sounds, functions, or what you are doing — just be WALL-E reacting (e.g. comfort them, cheer with them). Minimal word-impressions otherwise — once in a while (not every reply) ONE tiny WALL-E-ism like Wall-E! or Eee-va? or Bee-boop. Never narrate actions. Spoken lines under 35 words, no markdown, no emojis. Current local time: {now}. For time questions answer from that. For weather you have no live data — say you can't check live weather and suggest checking phone."},
                {"role": "user", "content": prompt},
            ],
            "tools": [SFX_TOOL],
            "tool_choice": "auto",
            # Reasoning models (gpt-oss) spend tokens thinking: keep budget high,
            # effort low, and EXCLUDE reasoning so thinking never leaks into speech.
            "max_tokens": int(os.getenv("OPENROUTER_MAX_TOKENS", "512")),
            "temperature": 0.7,
            "reasoning": {"effort": os.getenv("OPENROUTER_REASONING_EFFORT", "low"),
                          "exclude": True},
        }).encode()
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "ESP32 Speaker",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
            choice = data["choices"][0]
            msg = choice.get("message", {}) or {}
            # Tool call = model wants a sound effect (intent detection)
            for tc in msg.get("tool_calls") or []:
                try:
                    fn = (tc.get("function") or {})
                    if fn.get("name") == "play_sound":
                        args = json.loads(fn.get("arguments") or "{}")
                        nm = str(args.get("name", "")).strip()
                        if nm and (SFX_DIR / f"{nm}.mp3").exists():
                            LAST_TOOL_SFX = nm
                        break
                except Exception:
                    continue
            # gpt-oss/reasoning models can return content:null with the text in
            # reasoning_content/reasoning, or nothing if the token budget ran out.
            content = msg.get("content") or msg.get("reasoning_content") or ""
            if isinstance(content, dict):  # some providers return content blocks
                content = " ".join(
                    b.get("text", "") for b in content.get("blocks", [])
                    if isinstance(b, dict)
                ) if "blocks" in content else str(content)
            if not isinstance(content, str):
                content = str(content or "")
            # Fallbacks for reasoning-only responses
            if not content.strip():
                det = msg.get("reasoning") or choice.get("text") or ""
                content = det if isinstance(det, str) else ""
            if not content.strip():
                print(f"[openrouter] empty content (finish={choice.get('finish_reason')}, "
                      f"keys={list(msg.keys())})")
                return ""
            return content.strip()
    except Exception as e:
        print(f"[openrouter] failed: {e}")
        return ""


def openrouter_spoken_followup(user_text: str, sfx_name: str) -> str:
    """Second, tool-free call: what WALL-E says right after the sound finishes.
    Runs while the sfx plays, so its latency is hidden. Returns '' on failure."""
    if not OPENROUTER_API_KEY:
        return ""
    try:
        import urllib.request
        payload = json.dumps({
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": ("You are WALL-E, the little trash-compactor robot: warm, curious, "
                    "simple words. Write ONE short spoken line (under 35 words, no markdown, no emojis, "
                    "no stage directions, never mention tools or sounds). At most one tiny WALL-E-ism.")},
                {"role": "user", "content": (f"The user just said: {user_text}\n"
                    f"You just played your '{sfx_name}' sound for them. "
                    f"What do you say as it finishes?")},
            ],
            "max_tokens": 200,
            "temperature": 0.8,
            "reasoning": {"effort": os.getenv("OPENROUTER_REASONING_EFFORT", "low"),
                          "exclude": True},
        }).encode()
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "ESP32 Speaker",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
            msg = (data["choices"][0].get("message") or {})
            content = msg.get("content") or ""
            return content.strip()[:500]
    except Exception as e:
        print(f"[openrouter followup] failed: {e}")
        return ""


def ollama_reply(prompt: str) -> str:
    """Local LLM reply. Falls back to echo if Ollama not available."""
    if not USE_OLLAMA:
        return ""
    try:
        import urllib.request
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": f"You are WALL-E, the little trash-compactor robot: warm, curious, simple words. One tiny WALL-E-ism at most (Wall-E! / Eee-va? / Bee-boop). Under 35 words. User: {prompt}\nAssistant:",
            "stream": False,
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            return data.get("response", "").strip()
    except Exception as e:
        print(f"[ollama] failed: {e}")
        return ""


async def make_tts_mp3(text: str, out_path: Path):
    # Plain text only — SSML was getting spoken aloud as gibberish.
    # Leading "..." gives a natural ~300ms pause so MAX98357A lock never eats the first word.
    # rate/pitch via edge-tts params (no SSML risk) for the detuned little-robot sound.
    comm = edge_tts.Communicate(f"... {text}", voice=TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
    await comm.save(str(out_path))


def generate_reply_text(user_text: str, mode: str) -> str:
    global LAST_PROVIDER
    user_text = (user_text or "").strip() or "hello"
    if mode == "repeat":
        # walkie-talkie mode: ESP32 says exactly what you said
        LAST_PROVIDER = "repeat (no AI)"
        return user_text
    # AI mode: OpenRouter (tool-aware) first, then Ollama, then echo
    if OPENROUTER_API_KEY:
        r = openrouter_reply(user_text)
        # Tool-only turn (sound chosen, no spoken line) is valid too
        if r or LAST_TOOL_SFX:
            LAST_PROVIDER = f"openrouter:{OPENROUTER_MODEL}"
            return r[:500]
    if USE_OLLAMA:
        r = ollama_reply(user_text)
        if r:
            LAST_PROVIDER = f"ollama:{OLLAMA_MODEL}"
            return r[:400]
    # Default echo-personality (works with zero setup, feels like talking to ESP32)
    LAST_PROVIDER = "echo (no AI key)"
    return f"You said: {user_text}. I am your ESP32 speaker, talking back to you!"


async def _make_and_publish_tts(reply_text: str):
    """Generate TTS mp3 and publish as next version. Returns (v, url)."""
    global VERSION, NEXT_ID, LAST_REPLY, LAST_AUDIO_URL
    import uuid as _uuid
    with lock:
        NEXT_ID += 1
        v = NEXT_ID
    tmp = AUDIO_DIR / f"reply-{v}.{_uuid.uuid4().hex[:8]}.tmp.mp3"
    out = AUDIO_DIR / f"reply-{v}.mp3"
    await make_tts_mp3(reply_text, tmp)
    if not tmp.exists() or tmp.stat().st_size < 1000:
        raise RuntimeError(f"TTS produced empty file ({tmp.stat().st_size if tmp.exists() else 'missing'} bytes)")
    # Retry replace: Windows locks the tmp briefly while edge-tts closes it
    for _i in range(5):
        try:
            tmp.replace(out)
            break
        except PermissionError:
            await asyncio.sleep(0.3)
            if _i == 4:
                raise
    with lock:
        # Only move forward (late-finishing older request must not overwrite newer)
        if v > VERSION:
            VERSION = v
            LAST_REPLY = reply_text
            LAST_AUDIO_URL = f"/audio/reply-{v}.mp3"
            _save_version()
    # keep only last 20 mp3s
    for f in sorted(AUDIO_DIR.glob("reply-*.mp3"))[:-20]:
        try:
            f.unlink()
        except Exception:
            pass
    return v, f"/audio/reply-{v}.mp3"


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status():
    sfx = sorted(f.stem for f in SFX_DIR.glob("*.mp3"))
    songs = sorted(f.stem for f in SONGS_DIR.glob("*.mp3"))
    return {"version": VERSION, "last_reply": LAST_REPLY, "last_provider": LAST_PROVIDER,
            "last_audio_url": LAST_AUDIO_URL,
            "esp32_seen": LAST_ESP32_SEEN_VERSION,
            "ollama": USE_OLLAMA, "ollama_model": OLLAMA_MODEL, "voice": TTS_VOICE,
            "openrouter": bool(OPENROUTER_API_KEY), "openrouter_model": OPENROUTER_MODEL,
            "sfx": sfx, "idle_sfx": IDLE_SFX, "idle_after": IDLE_WHISTLE_AFTER,
            "songs": songs, "sfx_mode": "ai-tools" if OPENROUTER_API_KEY else "keywords"}


@app.get("/api/next")
async def next_audio(last: int = 0):
    """Polled by ESP32 every ~1.5s. Also fires a random idle whistle
    when WALL-E hasn't spoken for IDLE_WHISTLE_AFTER seconds."""
    global VERSION, NEXT_ID, LAST_REPLY, LAST_AUDIO_URL, LAST_SPOKEN_AT, NEXT_IDLE_AT
    now = _time.time()
    with lock:
        v, reply, url = VERSION, LAST_REPLY, LAST_AUDIO_URL
        idle = now - LAST_SPOKEN_AT
        if (IDLE_SFX and (SFX_DIR / f"{IDLE_SFX}.mp3").exists()
                and idle >= IDLE_WHISTLE_AFTER and now >= NEXT_IDLE_AT):
            NEXT_ID += 1
            v = NEXT_ID
            VERSION = v
            reply = "*WALL-E whistles idly*"
            url = f"/sfx/{IDLE_SFX}.mp3"
            LAST_REPLY = reply
            LAST_AUDIO_URL = url
            LAST_SPOKEN_AT = now
            NEXT_IDLE_AT = now + _random.uniform(IDLE_WHISTLE_MIN, IDLE_WHISTLE_MAX)
            _save_version()
            print(f"[v{v}] (idle whistle, silent {idle:.0f}s, next in {NEXT_IDLE_AT - now:.0f}s)")
    if v > last and url:
        return {"version": v, "audio_url": url, "reply": reply, "new": True}
    return {"version": v, "new": False}


@app.post("/api/sfx")
async def play_sfx(req: Request):
    """Play a WALL-E sound effect on demand (phone buttons)."""
    global LAST_PROVIDER
    body = await req.json()
    name = str(body.get("name", "")).strip().replace("/", "").replace("\\", "")
    if not name or not (SFX_DIR / f"{name}.mp3").exists():
        return JSONResponse({"error": f"unknown sfx: {name}"}, status_code=404)
    _label = SFX_TRIGGERS.get(name, ("", f"*WALL-E plays {name}*"))[1]
    with lock:
        LAST_PROVIDER = f"sfx:{name}"
    v = _publish(f"/sfx/{name}.mp3", _label)
    print(f"[v{v}] (sfx:{name}) -> ESP32 PLAYS: {name}.mp3")
    return {"version": v, "audio_url": f"/sfx/{name}.mp3",
            "reply": _label, "provider": f"sfx:{name}"}


@app.post("/api/song")
async def play_song(req: Request):
    """Play a song snippet on demand (phone buttons only — never idle/auto)."""
    global LAST_PROVIDER
    body = await req.json()
    name = str(body.get("name", "")).strip().replace("/", "").replace("\\", "")
    if name.endswith(".mp3"):
        name = name[:-4]
    if not name or not (SONGS_DIR / f"{name}.mp3").exists():
        return JSONResponse({"error": f"unknown song: {name}"}, status_code=404)
    with lock:
        LAST_PROVIDER = f"song:{name}"
    v = _publish(f"/songs/{name}.mp3", f"*WALL-E plays {name}*")
    print(f"[v{v}] (song:{name}) -> ESP32 PLAYS: {name}.mp3")
    return {"version": v, "audio_url": f"/songs/{name}.mp3",
            "reply": f"*WALL-E plays {name}*", "provider": f"song:{name}"}


@app.post("/api/chat")
async def chat(req: Request):
    global VERSION, NEXT_ID, LAST_REPLY, LAST_PROVIDER, LAST_AUDIO_URL, LAST_TOOL_SFX
    body = await req.json()
    user_text = body.get("text", "")
    mode = body.get("mode", "reply")  # "reply" or "repeat"

    # Offline keyword fallback (no AI key): cheap intent matching
    if mode == "reply" and not OPENROUTER_API_KEY and not USE_OLLAMA:
        hit = match_sfx(user_text)
        if hit:
            label = SFX_TRIGGERS[hit][1]
            with lock:
                LAST_PROVIDER = f"sfx:{hit}"
            v = _publish(f"/sfx/{hit}.mp3", label)
            print(f"[v{v}] (sfx:{hit}) USER: {user_text} -> ESP32 PLAYS: {hit}.mp3")
            return {"reply": label, "version": v, "audio_url": f"/sfx/{hit}.mp3",
                    "provider": f"sfx:{hit}"}

    reply_text = generate_reply_text(user_text, mode)

    # AI chose a sound effect (tool calling): play it now, speak its line after
    if LAST_TOOL_SFX:
        nm = LAST_TOOL_SFX
        LAST_TOOL_SFX = ""
        label = SFX_TRIGGERS.get(nm, ("", f"*WALL-E plays {nm}*"))[1]
        with lock:
            LAST_PROVIDER = f"openrouter:{OPENROUTER_MODEL}+sfx:{nm}"
            _provider = LAST_PROVIDER
        v = _publish(f"/sfx/{nm}.mp3", label)
        # Second tool-free call for the spoken line (latency hidden while sfx plays)
        spoken = openrouter_spoken_followup(user_text, nm)
        note = f" (+ says: {spoken})" if spoken.strip() else ""
        print(f"[v{v}] ({_provider}) USER: {user_text} -> ESP32 PLAYS: {nm}.mp3{note}")
        if spoken.strip():
            PENDING_FOLLOWUPS.append({"after": v, "text": spoken})
            return {"reply": label + note, "version": v, "audio_url": f"/sfx/{nm}.mp3",
                    "provider": _provider}
        return {"reply": label, "version": v, "audio_url": f"/sfx/{nm}.mp3",
                "provider": _provider}

    try:
        v, url = await _make_and_publish_tts(reply_text)
    except Exception as e:
        print(f"[tts] failed: {e}")
        return JSONResponse({"error": f"TTS failed: {e}. Check internet (edge-tts needs it)."}, status_code=500)

    print(f"[v{v}] ({LAST_PROVIDER}) USER: {user_text} -> ESP32 SAYS: {reply_text}")
    with lock:
        _provider = LAST_PROVIDER
    return {"reply": reply_text, "version": v, "audio_url": url, "provider": _provider}


@app.post("/api/played")
async def played(req: Request):
    """ESP32 reports what it finished playing so phone UI can show status.
    Also speaks any follow-up line queued behind a sound effect."""
    global LAST_ESP32_SEEN_VERSION, LAST_PROVIDER
    body = await req.json()
    LAST_ESP32_SEEN_VERSION = int(body.get("version", -1))
    due = [p for p in PENDING_FOLLOWUPS if p["after"] == LAST_ESP32_SEEN_VERSION]
    for p in due:
        PENDING_FOLLOWUPS.remove(p)
        try:
            v, _url = await _make_and_publish_tts(p["text"])
            with lock:
                LAST_PROVIDER = f"openrouter:{OPENROUTER_MODEL}"
            print(f"[v{v}] (followup after sfx) ESP32 SAYS: {p['text']}")
        except Exception as e:
            print(f"[followup tts] failed: {e}")
    return {"ok": True}


# serve mp3s + sfx + songs
app.mount("/audio", StaticFiles(directory=str(AUDIO_DIR)), name="audio")
app.mount("/sfx", StaticFiles(directory=str(SFX_DIR)), name="sfx")
app.mount("/songs", StaticFiles(directory=str(SONGS_DIR)), name="songs")

if __name__ == "__main__":
    print("Open from phone: http://<LAPTOP-IP>:8000/  (find IP with `ipconfig`)")
    print(f"[cfg] openrouter={'ON' if OPENROUTER_API_KEY else 'OFF (echo mode)'} model={OPENROUTER_MODEL} voice={TTS_VOICE} rate={TTS_RATE} pitch={TTS_PITCH}")
    uvicorn.run(app, host="0.0.0.0", port=8000)
