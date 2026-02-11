#!/usr/bin/env python3
"""FastAPI web version of the Flickr Interesting Photos Set Creator."""

import asyncio
import json
import os
import secrets
import sys
import time
from datetime import datetime
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Token extraction CLI mode (exit early before heavy imports) ---
if __name__ == "__main__" and "--extract-token" in sys.argv:
    from dotenv import load_dotenv as _ld
    _ld(os.path.join(BASE_DIR, ".env"))
    import sqlite3
    api_key = os.environ.get("FLICKR_API_KEY")
    if not api_key:
        print("Error: FLICKR_API_KEY not set in environment or .env file.")
        sys.exit(1)
    db_path = os.path.expanduser("~/.flickr/oauth-tokens.sqlite")
    if not os.path.exists(db_path):
        print(f"Error: Token database not found at {db_path}")
        print("Run the desktop app first to authenticate with Flickr.")
        sys.exit(1)
    db = sqlite3.connect(db_path)
    curs = db.cursor()
    curs.execute(
        "SELECT oauth_token, oauth_token_secret, user_nsid FROM oauth_tokens WHERE api_key=?",
        (api_key,),
    )
    row = curs.fetchone()
    db.close()
    if row:
        print("Add these to your cloud host's environment variables:\n")
        print(f"FLICKR_OAUTH_TOKEN={row[0]}")
        print(f"FLICKR_OAUTH_TOKEN_SECRET={row[1]}")
        print(f"FLICKR_USER_NSID={row[2]}")
    else:
        print(f"No token found for API key {api_key}.")
        print("Run the desktop app first to authenticate with Flickr.")
    sys.exit(0)

import flickrapi
import pyotp
from flickrapi.auth import FlickrAccessToken
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# Load environment
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Import core logic
sys.path.insert(0, BASE_DIR)
import flickr_interestingness as core

SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

# --- FastAPI app ---
app = FastAPI(title="Flickr Interesting Photos Set Creator")

# Module-level state (single-user, no database needed)
log_queue: asyncio.Queue = asyncio.Queue()
log_buffer: list[str] = []
job_status = {"running": False, "last_run": None}
flickr_client: Optional[flickrapi.FlickrAPI] = None
flickr_nsid: Optional[str] = None
auth_flickr_temp: Optional[flickrapi.FlickrAPI] = None

# Auth cookie token (generated once per process)
AUTH_COOKIE_TOKEN = secrets.token_hex(32)

# TOTP config for authenticator-app 2FA
TOTP_SECRET = os.environ.get("TOTP_SECRET", "")


def get_flickr_client():
    """Initialize Flickr client from env vars or cached token."""
    api_key = os.environ.get("FLICKR_API_KEY")
    api_secret = os.environ.get("FLICKR_API_SECRET")
    if not api_key or not api_secret:
        return None, None

    # Strategy A: environment variable token (for cloud deployment)
    oauth_token = os.environ.get("FLICKR_OAUTH_TOKEN")
    oauth_token_secret = os.environ.get("FLICKR_OAUTH_TOKEN_SECRET")
    user_nsid = os.environ.get("FLICKR_USER_NSID")

    if oauth_token and oauth_token_secret and user_nsid:
        token = FlickrAccessToken(
            token=oauth_token,
            token_secret=oauth_token_secret,
            access_level="write",
            fullname="",
            username="",
            user_nsid=user_nsid,
        )
        f = flickrapi.FlickrAPI(api_key, api_secret, token=token, format="parsed-json")
        return f, user_nsid

    # Strategy B: on-disk token cache (local development)
    f = flickrapi.FlickrAPI(api_key, api_secret, format="parsed-json")
    if f.token_valid(perms="write"):
        nsid = f.token_cache.token.user_nsid
        return f, nsid

    return None, None


@app.on_event("startup")
async def startup():
    global flickr_client, flickr_nsid
    flickr_client, flickr_nsid = get_flickr_client()
    if flickr_client:
        print(f"Authenticated as: {flickr_nsid}")
    else:
        print("Not authenticated. Use /auth/start or set FLICKR_OAUTH_TOKEN env vars.")


# --- Password protection middleware ---

APP_USERNAME = os.environ.get("APP_USERNAME", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.middleware("http")
async def check_auth(request: Request, call_next):
    if not APP_PASSWORD:
        return await call_next(request)

    path = request.url.path
    # Allow auth callback through (Flickr redirect)
    if path.startswith("/auth/callback"):
        return await call_next(request)
    # Allow login and 2FA verification pages
    if path in ("/login", "/verify"):
        return await call_next(request)

    # Check auth cookie
    if request.cookies.get("app_auth") == AUTH_COOKIE_TOKEN:
        return await call_next(request)

    return RedirectResponse("/login", status_code=302)


# --- Request model ---

class RunRequest(BaseModel):
    title: str = "Top 1000 Most Interesting"
    description: str = "Auto-generated set of my most interesting photos."
    count: int = 1000
    photoset_name: str = ""
    dry_run: bool = False


# --- Routes ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = ""):
    error_html = f'<p class="error">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login - Flickr Photoset Creator</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #2b2b2b; color: #e0e0e0; display: flex; justify-content: center;
           align-items: center; min-height: 100vh; margin: 0; }}
    .login-box {{ background: #3c3c3c; border-radius: 8px; padding: 32px; width: 320px;
                  box-shadow: 0 4px 24px rgba(0,0,0,0.3); }}
    h2 {{ margin: 0 0 20px; text-align: center; }}
    label {{ display: block; margin-bottom: 4px; font-size: 14px; }}
    input {{ width: 100%; padding: 8px; margin-bottom: 16px; border: 1px solid #555;
             background: #2b2b2b; color: #e0e0e0; border-radius: 4px; box-sizing: border-box; }}
    input:focus {{ outline: none; border-color: #6a9eda; }}
    button {{ width: 100%; padding: 10px; background: #0063dc; color: white; border: none;
              border-radius: 4px; cursor: pointer; font-size: 15px; }}
    button:hover {{ background: #0052b5; }}
    .error {{ color: #ff6b6b; text-align: center; margin-bottom: 12px; }}
</style>
</head><body>
<div class="login-box">
    <h2>Flickr Photoset Creator</h2>
    {error_html}
    <form method="post" action="/login">
        <label for="username">Username</label>
        <input type="text" id="username" name="username" required autocomplete="username">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" required autocomplete="current-password">
        <button type="submit">Log In</button>
    </form>
</div>
</body></html>"""


@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    if username != APP_USERNAME or password != APP_PASSWORD:
        return RedirectResponse("/login?error=Invalid+username+or+password", status_code=302)

    # If TOTP is configured, require authenticator code; otherwise grant access directly
    if TOTP_SECRET:
        return RedirectResponse("/verify", status_code=302)

    # No TOTP configured — skip 2FA
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        "app_auth", AUTH_COOKIE_TOKEN, httponly=True, samesite="lax", max_age=86400 * 30
    )
    return response


@app.get("/verify", response_class=HTMLResponse)
async def verify_page(error: str = ""):
    error_html = f'<p class="error">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verify - Flickr Photoset Creator</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #2b2b2b; color: #e0e0e0; display: flex; justify-content: center;
           align-items: center; min-height: 100vh; margin: 0; }}
    .login-box {{ background: #3c3c3c; border-radius: 8px; padding: 32px; width: 320px;
                  box-shadow: 0 4px 24px rgba(0,0,0,0.3); }}
    h2 {{ margin: 0 0 12px; text-align: center; }}
    p.info {{ font-size: 13px; color: #aaa; text-align: center; margin-bottom: 16px; }}
    label {{ display: block; margin-bottom: 4px; font-size: 14px; }}
    input {{ width: 100%; padding: 8px; margin-bottom: 16px; border: 1px solid #555;
             background: #2b2b2b; color: #e0e0e0; border-radius: 4px; box-sizing: border-box;
             font-size: 20px; text-align: center; letter-spacing: 6px; }}
    input:focus {{ outline: none; border-color: #6a9eda; }}
    button {{ width: 100%; padding: 10px; background: #0063dc; color: white; border: none;
              border-radius: 4px; cursor: pointer; font-size: 15px; }}
    button:hover {{ background: #0052b5; }}
    .error {{ color: #ff6b6b; text-align: center; margin-bottom: 12px; }}
    .back {{ display: block; text-align: center; margin-top: 12px; color: #6a9eda;
             text-decoration: none; font-size: 13px; }}
    .back:hover {{ text-decoration: underline; }}
</style>
</head><body>
<div class="login-box">
    <h2>Verification Code</h2>
    <p class="info">Enter the 6-digit code from your authenticator app.</p>
    {error_html}
    <form method="post" action="/verify">
        <label for="code">Enter Code</label>
        <input type="text" id="code" name="code" required maxlength="6" pattern="[0-9]{{6}}"
               inputmode="numeric" autocomplete="one-time-code" placeholder="------">
        <button type="submit">Verify</button>
    </form>
    <a class="back" href="/login">Back to login</a>
</div>
</body></html>"""


@app.post("/verify")
async def verify_submit(code: str = Form(...)):
    if not TOTP_SECRET:
        return RedirectResponse("/login", status_code=302)
    totp = pyotp.TOTP(TOTP_SECRET)
    if not totp.verify(code, valid_window=1):
        return RedirectResponse("/verify?error=Invalid+code.+Please+try+again", status_code=302)

    # Code is valid — grant access
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        "app_auth", AUTH_COOKIE_TOKEN, httponly=True, samesite="lax", max_age=86400 * 30
    )
    return response


@app.get("/setup-2fa", response_class=HTMLResponse)
async def setup_2fa_page():
    if not TOTP_SECRET:
        return HTMLResponse("<p>TOTP_SECRET not configured.</p>", status_code=500)
    totp = pyotp.TOTP(TOTP_SECRET)
    provisioning_uri = totp.provisioning_uri(
        name=APP_USERNAME or "user",
        issuer_name="Flickr Photoset Creator",
    )
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Setup 2FA - Flickr Photoset Creator</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           background: #2b2b2b; color: #e0e0e0; display: flex; justify-content: center;
           align-items: center; min-height: 100vh; margin: 0; }}
    .setup-box {{ background: #3c3c3c; border-radius: 8px; padding: 32px; width: 380px;
                  box-shadow: 0 4px 24px rgba(0,0,0,0.3); text-align: center; }}
    h2 {{ margin: 0 0 16px; }}
    p {{ font-size: 14px; color: #aaa; margin-bottom: 16px; line-height: 1.5; }}
    #qrcode {{ display: flex; justify-content: center; margin-bottom: 16px; }}
    #qrcode canvas {{ border-radius: 8px; }}
    .secret {{ background: #2b2b2b; border: 1px solid #555; border-radius: 4px;
               padding: 10px; font-family: monospace; font-size: 16px; letter-spacing: 2px;
               word-break: break-all; margin-bottom: 16px; user-select: all; }}
    .back {{ color: #6a9eda; text-decoration: none; font-size: 14px; }}
    .back:hover {{ text-decoration: underline; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
</head><body>
<div class="setup-box">
    <h2>Setup Two-Factor Authentication</h2>
    <p>Scan this QR code with Google Authenticator, Authy, or Microsoft Authenticator:</p>
    <div id="qrcode"></div>
    <p>Or enter this key manually:</p>
    <div class="secret">{TOTP_SECRET}</div>
    <a class="back" href="/">Back to app</a>
</div>
<script>new QRCode(document.getElementById("qrcode"), {{text: "{provisioning_uri}", width: 200, height: 200}});</script>
</body></html>"""


@app.get("/status")
async def status():
    return {
        "authenticated": flickr_client is not None,
        "user_nsid": flickr_nsid,
        "job_running": job_status["running"],
        "last_run": job_status["last_run"],
    }


@app.get("/settings")
async def get_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@app.post("/run")
async def run(req: RunRequest):
    if not flickr_client:
        return JSONResponse({"error": "Not authenticated with Flickr"}, status_code=401)
    if job_status["running"]:
        return JSONResponse({"error": "A job is already running"}, status_code=409)

    job_status["running"] = True
    log_buffer.clear()

    # Drain any stale messages from the queue
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, worker_thread, req, loop)

    return {"status": "started"}


@app.get("/stream")
async def stream(request: Request):
    async def event_generator():
        # Replay any buffered messages first (handles reconnection)
        idx = 0
        while idx < len(log_buffer):
            yield {"event": "log", "data": log_buffer[idx]}
            idx += 1

        while True:
            if await request.is_disconnected():
                break
            try:
                message = await asyncio.wait_for(log_queue.get(), timeout=25.0)
                yield {"event": "log", "data": message}
                if message.startswith("__DONE__") or message.startswith("__ERROR__"):
                    break
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "keepalive"}

    return EventSourceResponse(event_generator())


@app.get("/auth/start")
async def auth_start(request: Request):
    global auth_flickr_temp
    api_key = os.environ.get("FLICKR_API_KEY")
    api_secret = os.environ.get("FLICKR_API_SECRET")
    if not api_key or not api_secret:
        return JSONResponse({"error": "FLICKR_API_KEY and FLICKR_API_SECRET must be set"}, status_code=500)

    auth_flickr_temp = flickrapi.FlickrAPI(api_key, api_secret, format="parsed-json", store_token=False)
    base_url = str(request.base_url).rstrip("/")
    callback_url = f"{base_url}/auth/callback"
    auth_flickr_temp.get_request_token(oauth_callback=callback_url)
    auth_url = auth_flickr_temp.auth_url(perms="write")
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
async def auth_callback(oauth_token: str = "", oauth_verifier: str = ""):
    global flickr_client, flickr_nsid, auth_flickr_temp
    if not auth_flickr_temp:
        return JSONResponse({"error": "No OAuth flow in progress"}, status_code=400)
    if not oauth_verifier:
        return JSONResponse({"error": "OAuth verification failed"}, status_code=400)

    auth_flickr_temp.get_access_token(verifier=oauth_verifier)
    token = auth_flickr_temp.token_cache.token
    flickr_client = auth_flickr_temp
    flickr_nsid = token.user_nsid
    auth_flickr_temp = None
    return RedirectResponse("/")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_TEMPLATE


# --- Background worker ---

def emit_log(loop: asyncio.AbstractEventLoop, message: str):
    """Thread-safe way to send a log message to the SSE stream."""
    log_buffer.append(message)
    loop.call_soon_threadsafe(log_queue.put_nowait, message)


def worker_thread(req: RunRequest, loop: asyncio.AbstractEventLoop):
    """Runs the Flickr operation in a background thread."""
    try:
        emit_log(loop, f"Starting {'dry run' if req.dry_run else 'operation'}...")
        emit_log(loop, f"Authenticated as user: {flickr_nsid}")

        # Fetch interesting photos
        photo_ids = []
        per_page = 500
        total_pages = (req.count + per_page - 1) // per_page

        for page in range(1, total_pages + 1):
            emit_log(loop, f"Fetching page {page}/{total_pages}...")
            resp = core.api_call_with_retry(
                flickr_client.photos.search,
                user_id=flickr_nsid,
                sort="interestingness-desc",
                per_page=per_page,
                page=page,
            )
            photos = resp["photos"]["photo"]
            if not photos:
                break
            photo_ids.extend(p["id"] for p in photos)
            if int(resp["photos"]["pages"]) <= page:
                break

        photo_ids = photo_ids[:req.count]
        emit_log(loop, f"Found {len(photo_ids)} interesting photos.")

        if not photo_ids:
            emit_log(loop, "No photos found. Nothing to do.")
            emit_log(loop, "__DONE__")
            return

        # Resolve photoset name to ID if provided
        photoset_id = None
        if req.photoset_name:
            emit_log(loop, f"Looking up photoset '{req.photoset_name}'...")
            photoset_id = resolve_photoset_name(flickr_client, flickr_nsid, req.photoset_name, loop)
            if not photoset_id:
                emit_log(loop, f"Error: No photoset found with name '{req.photoset_name}'.")
                emit_log(loop, "__ERROR__")
                return
            emit_log(loop, f"Found photoset ID: {photoset_id}")

        if req.dry_run:
            action = "update" if photoset_id else "create"
            emit_log(loop, f"\n[DRY RUN] Would {action} photoset '{req.title}' with {len(photo_ids)} photos.")
            emit_log(loop, "First 20 photo IDs:")
            for pid in photo_ids[:20]:
                emit_log(loop, f"  {pid}")
            if len(photo_ids) > 20:
                emit_log(loop, f"  ... and {len(photo_ids) - 20} more")
            emit_log(loop, "__DONE__")
            return

        if photoset_id:
            # Update existing photoset
            emit_log(loop, f"Updating photoset '{req.photoset_name}' with {len(photo_ids)} photos...")
            timestamp = datetime.now().astimezone().strftime("%B %d, %Y at %I:%M %p %Z")
            update_desc = f"{req.description}\n\nLast updated: {timestamp}"
            emit_log(loop, "Updating photoset title and description...")
            core.api_call_with_retry(
                flickr_client.photosets.editMeta,
                photoset_id=photoset_id,
                title=req.title,
                description=update_desc,
            )
            try:
                emit_log(loop, "Replacing photos via editPhotos...")
                core.api_call_with_retry(
                    flickr_client.photosets.editPhotos,
                    photoset_id=photoset_id,
                    primary_photo_id=photo_ids[0],
                    photo_ids=",".join(photo_ids),
                )
                emit_log(loop, "All photos replaced successfully via editPhotos.")
            except Exception as e:
                emit_log(loop, f"editPhotos failed ({e}), falling back to addPhoto loop...")
                add_photos_individually(flickr_client, photoset_id, photo_ids, loop)
        else:
            # Create new photoset
            emit_log(loop, f"Creating photoset '{req.title}' with {len(photo_ids)} photos...")
            resp = core.api_call_with_retry(
                flickr_client.photosets.create,
                title=req.title,
                description=req.description,
                primary_photo_id=photo_ids[0],
            )
            photoset_id = resp["photoset"]["id"]
            emit_log(loop, f"Photoset created with ID: {photoset_id}")

            try:
                emit_log(loop, "Attempting bulk add via editPhotos...")
                core.api_call_with_retry(
                    flickr_client.photosets.editPhotos,
                    photoset_id=photoset_id,
                    primary_photo_id=photo_ids[0],
                    photo_ids=",".join(photo_ids),
                )
                emit_log(loop, "All photos added successfully via editPhotos.")
            except Exception as e:
                emit_log(loop, f"editPhotos failed ({e}), falling back to addPhoto loop...")
                add_photos_individually(flickr_client, photoset_id, photo_ids, loop)

        owner = flickr_nsid.replace("@", "%40")
        url = f"https://www.flickr.com/photos/{owner}/sets/{photoset_id}"
        emit_log(loop, f"\nDone! View your photoset at:\n  {url}")
        emit_log(loop, "__DONE__")

    except Exception as e:
        emit_log(loop, f"\nError: {e}")
        emit_log(loop, "__ERROR__")
    finally:
        job_status["running"] = False
        job_status["last_run"] = datetime.now().isoformat()


def resolve_photoset_name(flickr, nsid, name, loop):
    """Look up a photoset ID by name."""
    page = 1
    while True:
        resp = core.api_call_with_retry(
            flickr.photosets.getList,
            user_id=nsid,
            per_page=500,
            page=page,
        )
        photosets = resp["photosets"]["photoset"]
        for ps in photosets:
            if ps["title"]["_content"] == name:
                return ps["id"]
        if page >= int(resp["photosets"]["pages"]):
            break
        page += 1
    return None


def add_photos_individually(flickr, photoset_id, photo_ids, loop):
    """Fallback: add photos one by one with progress."""
    remaining = photo_ids[1:]
    added, failed = 0, 0
    for i, pid in enumerate(remaining, start=1):
        try:
            core.api_call_with_retry(
                flickr.photosets.addPhoto,
                photoset_id=photoset_id,
                photo_id=pid,
            )
            added += 1
        except Exception as ex:
            failed += 1
            emit_log(loop, f"  Failed to add {pid}: {ex}")
        if i % 50 == 0 or i == len(remaining):
            emit_log(loop, f"  Progress: {i}/{len(remaining)} (added: {added}, failed: {failed})")
        time.sleep(0.1)


# --- HTML Template ---

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flickr Interesting Photos Set Creator</title>
<style>
    :root {
        --bg: #ffffff; --surface: #f5f5f5; --text: #333333; --text-secondary: #666666;
        --border: #cccccc; --accent: #0063dc; --accent-hover: #0052b5;
        --input-bg: #ffffff; --log-bg: #1e1e1e; --log-text: #d4d4d4;
        --error: #dc3545; --success: #28a745;
    }
    body.dark {
        --bg: #2b2b2b; --surface: #3c3c3c; --text: #e0e0e0; --text-secondary: #aaaaaa;
        --border: #555555; --accent: #6a9eda; --accent-hover: #5a8aca;
        --input-bg: #3c3c3c; --log-bg: #1a1a1a; --log-text: #d4d4d4;
        --error: #ff6b6b; --success: #51cf66;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background: var(--bg); color: var(--text);
        min-height: 100vh; padding: 20px;
        transition: background 0.2s, color 0.2s;
    }
    .container { max-width: 700px; margin: 0 auto; }

    header {
        display: flex; justify-content: space-between; align-items: center;
        margin-bottom: 20px; padding-bottom: 12px; border-bottom: 1px solid var(--border);
    }
    header h1 { font-size: 20px; }
    .theme-btn {
        background: var(--surface); color: var(--text); border: 1px solid var(--border);
        border-radius: 4px; padding: 6px 12px; cursor: pointer; font-size: 13px;
    }
    .theme-btn:hover { background: var(--border); }

    .auth-status {
        background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
        padding: 10px 14px; margin-bottom: 16px; font-size: 14px;
        display: flex; justify-content: space-between; align-items: center;
    }
    .auth-status.ok { border-left: 4px solid var(--success); }
    .auth-status.not-ok { border-left: 4px solid var(--error); }
    .auth-status a { color: var(--accent); text-decoration: none; }
    .auth-status a:hover { text-decoration: underline; }

    .section {
        background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
        padding: 16px; margin-bottom: 16px;
    }
    .section h2 { font-size: 15px; margin-bottom: 12px; }

    .form-row { display: flex; align-items: center; margin-bottom: 10px; gap: 8px; }
    .form-row label { min-width: 130px; font-size: 14px; flex-shrink: 0; }
    .form-row input, .form-row select {
        flex: 1; padding: 6px 10px; border: 1px solid var(--border); border-radius: 4px;
        background: var(--input-bg); color: var(--text); font-size: 14px;
    }
    .form-row input:focus, .form-row select:focus {
        outline: none; border-color: var(--accent);
    }
    .form-row input[type="number"] { max-width: 120px; }
    .hint { font-size: 12px; color: var(--text-secondary); margin-left: 138px; margin-top: -6px; margin-bottom: 8px; }

    .btn-row { display: flex; gap: 8px; margin-bottom: 16px; }
    .btn {
        padding: 8px 20px; border: 1px solid var(--border); border-radius: 4px;
        cursor: pointer; font-size: 14px; background: var(--surface); color: var(--text);
    }
    .btn:hover { background: var(--border); }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn.primary { background: var(--accent); color: white; border-color: var(--accent); }
    .btn.primary:hover { background: var(--accent-hover); }
    .btn.primary:disabled { background: var(--accent); }

    .log-section h2 { margin-bottom: 8px; }
    #log {
        background: var(--log-bg); color: var(--log-text); border: 1px solid var(--border);
        border-radius: 4px; padding: 12px; font-family: "Cascadia Code", "Fira Code", Consolas, monospace;
        font-size: 13px; line-height: 1.5; min-height: 250px; max-height: 400px;
        overflow-y: auto; white-space: pre-wrap; word-break: break-word;
    }

    @media (max-width: 600px) {
        .form-row { flex-direction: column; align-items: stretch; }
        .form-row label { min-width: unset; margin-bottom: 2px; }
        .hint { margin-left: 0; }
    }
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>Flickr Interesting Photos Set Creator</h1>
        <button class="theme-btn" onclick="toggleTheme()" id="themeBtn">Dark Mode</button>
    </header>

    <div class="auth-status not-ok" id="authStatus">Checking authentication...</div>

    <div class="section">
        <h2>Photoset Settings</h2>
        <div class="form-row">
            <label for="title">Title</label>
            <input type="text" id="title" value="Top 1000 Most Interesting">
        </div>
        <div class="form-row">
            <label for="description">Description</label>
            <input type="text" id="description" value="Auto-generated set of my most interesting photos.">
        </div>
        <div class="form-row">
            <label for="count">Photo Count</label>
            <input type="number" id="count" min="1" max="5000" value="1000">
        </div>
        <div class="form-row">
            <label for="photoset_name">Existing Photoset</label>
            <input type="text" id="photoset_name" value="">
        </div>
        <div class="hint">Optional &mdash; name of an existing set to update</div>
    </div>

    <div class="btn-row">
        <button class="btn" id="dryRunBtn" onclick="startRun(true)">Dry Run</button>
        <button class="btn primary" id="createBtn" onclick="startRun(false)">Create Photoset</button>
    </div>

    <div class="section log-section">
        <h2>Output</h2>
        <div id="log"></div>
    </div>
</div>

<script>
    // --- Theme ---
    function toggleTheme() {
        const dark = document.body.classList.toggle('dark');
        document.getElementById('themeBtn').textContent = dark ? 'Light Mode' : 'Dark Mode';
        localStorage.setItem('theme', dark ? 'dark' : 'light');
    }
    (function() {
        if (localStorage.getItem('theme') === 'dark') {
            document.body.classList.add('dark');
            document.getElementById('themeBtn').textContent = 'Light Mode';
        }
    })();

    // --- Init ---
    async function init() {
        // Load auth status
        try {
            const status = await fetch('/status').then(r => r.json());
            const el = document.getElementById('authStatus');
            if (status.authenticated) {
                el.className = 'auth-status ok';
                el.textContent = 'Authenticated as: ' + status.user_nsid;
            } else {
                el.className = 'auth-status not-ok';
                el.innerHTML = 'Not authenticated. <a href="/auth/start">Authenticate with Flickr</a>';
            }
            if (status.job_running) {
                setButtonsEnabled(false);
                connectStream();
            }
        } catch (e) {
            document.getElementById('authStatus').textContent = 'Error checking status';
        }

        // Load saved settings
        try {
            const settings = await fetch('/settings').then(r => r.json());
            if (settings.title) document.getElementById('title').value = settings.title;
            if (settings.description) document.getElementById('description').value = settings.description;
            if (settings.count) document.getElementById('count').value = settings.count;
            if (settings.photoset_name) document.getElementById('photoset_name').value = settings.photoset_name;
        } catch (e) {}
    }
    init();

    // --- Run ---
    function setButtonsEnabled(enabled) {
        document.getElementById('dryRunBtn').disabled = !enabled;
        document.getElementById('createBtn').disabled = !enabled;
    }

    function appendLog(text) {
        const log = document.getElementById('log');
        log.textContent += text + '\\n';
        log.scrollTop = log.scrollHeight;
    }

    async function startRun(dryRun) {
        const body = {
            title: document.getElementById('title').value,
            description: document.getElementById('description').value,
            count: parseInt(document.getElementById('count').value) || 1000,
            photoset_name: document.getElementById('photoset_name').value,
            dry_run: dryRun,
        };

        document.getElementById('log').textContent = '';
        setButtonsEnabled(false);

        try {
            const resp = await fetch('/run', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                const err = await resp.json();
                appendLog('Error: ' + (err.error || 'Unknown error'));
                setButtonsEnabled(true);
                return;
            }
            connectStream();
        } catch (e) {
            appendLog('Error: Failed to connect to server');
            setButtonsEnabled(true);
        }
    }

    function connectStream() {
        const evtSource = new EventSource('/stream');
        evtSource.addEventListener('log', function(e) {
            if (e.data.startsWith('__DONE__') || e.data.startsWith('__ERROR__')) {
                evtSource.close();
                setButtonsEnabled(true);
            } else {
                appendLog(e.data);
            }
        });
        evtSource.addEventListener('ping', function() {});
        evtSource.onerror = function() {
            evtSource.close();
            setButtonsEnabled(true);
            appendLog('\\n[Connection lost]');
        };
    }
</script>
</body>
</html>"""
