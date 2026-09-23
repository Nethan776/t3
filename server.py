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
AUDIO_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)


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
LAST_ESP32_SEEN_VERSION = -1
# clear stale tmps from crashes (they cause WinError 32 on next replace)
for _stale in AUDIO_DIR.glob("reply-*.tmp*.mp3"):
    try:
        _stale.unlink()
    except Exception:
        pass

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
    """Chat via OpenRouter. Returns '' on failure so caller can fall back."""
    if not OPENROUTER_API_KEY:
        return ""
    try:
        import urllib.request
        from datetime import datetime
        now = datetime.now().strftime("%A, %d %B %Y, %I:%M %p")
        payload = json.dumps({
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": f"You are WALL-E, the little trash-compactor robot from the movies: warm, curious, speaks simply like he's still learning words. Minimal impressions only — once in a while (not every reply) add ONE tiny WALL-E-ism like Wall-E! or Eee-va? or Bee-boop. Never overdo it, never narrate actions. Keep replies under 35 words, spoken style, no markdown, no emojis. Current local time: {now}. For time questions answer from that. For weather you have no live data — say you can't check live weather and suggest checking phone."},
                {"role": "user", "content": prompt},
            ],
            # Reasoning models (gpt-oss) spend tokens thinking: keep budget high
            # and effort low so real content fits.
            "max_tokens": int(os.getenv("OPENROUTER_MAX_TOKENS", "512")),
            "temperature": 0.7,
            "reasoning": {"effort": os.getenv("OPENROUTER_REASONING_EFFORT", "low")},
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
    # AI mode: OpenRouter first (if key set), then Ollama, then echo
    if OPENROUTER_API_KEY:
        r = openrouter_reply(user_text)
        if r:
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


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status():
    return {"version": VERSION, "last_reply": LAST_REPLY, "last_provider": LAST_PROVIDER,
            "esp32_seen": LAST_ESP32_SEEN_VERSION,
            "ollama": USE_OLLAMA, "ollama_model": OLLAMA_MODEL, "voice": TTS_VOICE,
            "openrouter": bool(OPENROUTER_API_KEY), "openrouter_model": OPENROUTER_MODEL}


@app.get("/api/next")
async def next_audio(last: int = 0):
    """Polled by ESP32 every ~1.5s."""
    with lock:
        v = VERSION
        reply = LAST_REPLY
    if v > last:
        return {"version": v, "audio_url": f"/audio/reply-{v}.mp3", "reply": reply, "new": True}
    return {"version": v, "new": False}


@app.post("/api/chat")
async def chat(req: Request):
    global VERSION, NEXT_ID, LAST_REPLY, LAST_PROVIDER
    body = await req.json()
    user_text = body.get("text", "")
    mode = body.get("mode", "reply")  # "reply" or "repeat"
    reply_text = generate_reply_text(user_text, mode)

    # Unique ID per request (concurrent mic sends must NOT share a file)
    import time as _time
    import uuid as _uuid
    with lock:
        NEXT_ID += 1
        v = NEXT_ID

    tmp = AUDIO_DIR / f"reply-{v}.{_uuid.uuid4().hex[:8]}.tmp.mp3"
    out = AUDIO_DIR / f"reply-{v}.mp3"
    try:
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
    except Exception as e:
        print(f"[tts] failed: {e}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return JSONResponse({"error": f"TTS failed: {e}. Check internet (edge-tts needs it)."}, status_code=500)

    with lock:
        # Only move forward (late-finishing older request must not overwrite newer)
        if v > VERSION:
            VERSION = v
            LAST_REPLY = reply_text

    # keep only last 20 mp3s
    for f in sorted(AUDIO_DIR.glob("reply-*.mp3"))[:-20]:
        try:
            f.unlink()
        except Exception:
            pass

    print(f"[v{v}] ({LAST_PROVIDER}) USER: {user_text} -> ESP32 SAYS: {reply_text}")
    return {"reply": reply_text, "version": v, "audio_url": f"/audio/reply-{v}.mp3", "provider": LAST_PROVIDER}


@app.post("/api/played")
async def played(req: Request):
    """ESP32 reports what it finished playing so phone UI can show status."""
    global LAST_ESP32_SEEN_VERSION
    body = await req.json()
    LAST_ESP32_SEEN_VERSION = int(body.get("version", -1))
    return {"ok": True}


# serve mp3s
app.mount("/audio", StaticFiles(directory=str(AUDIO_DIR)), name="audio")

if __name__ == "__main__":
    print("Open from phone: http://<LAPTOP-IP>:8000/  (find IP with `ipconfig`)")
    print(f"[cfg] openrouter={'ON' if OPENROUTER_API_KEY else 'OFF (echo mode)'} model={OPENROUTER_MODEL} voice={TTS_VOICE} rate={TTS_RATE} pitch={TTS_PITCH}")
    uvicorn.run(app, host="0.0.0.0", port=8000)
