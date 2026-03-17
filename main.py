import os
import re
import json
import time
import random
import uuid
import threading
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Query, HTTPException, Request, Response, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import json5
from datetime import datetime
from PIL import Image

app = FastAPI(title="Media Gallery")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],    
)

# ── Configure ────────────────────────────────────────────────────────────────
SECRET_KEY = "Your_Secret_Key"
DOWNLOADS_DIR = Path(r"Downloads") #Path where images are stored. Multiple paths can be added with some changes.
CACHE_FILE    = Path(__file__).parent / "data" / "cache.json" # Cache file with image path,fodler details and tags. Used while serving on front-end. Created at first star and can be regenerated anytime with generate api endpoint.
TAGS_FILE     = Path(__file__).parent / "data" / "tags.json" # Tags - Used for filters. Can be also used with cache by doing some changes. by default tags are added to cache and behaviour can be changed.
FOLDERS_FILE  = Path(__file__).parent / "data" / "folders.json"
CACHE_TTL     = 0  # 0 = never auto-expire
STATS_FILE    = Path(__file__).parent / "data" / "stats.json"
HOTLOG_FILE = Path(__file__).parent / "data" / "hotlog.json"
VISITLOG_FILE = Path(__file__).parent / "data" / "visitlog.json"
THUMBNAILS_DIR = Path(__file__).parent / "thumbnails"
THUMB_SIZE     = (400, 400)  # max width/height, keeps aspect ratio -  400x400 is more than enough for 1080p screens. 800x800 can be used for 1440p and above. seperate code can be added to use depending on resolution but not recommended as it increased backend load and caching.
_thumb_progress = {"total": 0, "created": 0, "skipped": 0, "running": False, "done": False}
_thumb_lock = threading.Lock()
# ────────────────────────────────────────────────────────────────────────────

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}

# Create directory if it doesn't exist
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/thumbs", StaticFiles(directory=str(THUMBNAILS_DIR)), name="thumbs")
app.mount("/media", StaticFiles(directory=str(DOWNLOADS_DIR)), name="media")
PAGES_DIR = Path(__file__).parent / "pages"

@app.get("/", response_class=HTMLResponse)
async def page_gallery(request: Request, response: Response, sid: str = Cookie(default=None)):
    return serve_page("gallery.html", request, response, sid)

@app.get("/folders", response_class=HTMLResponse)
async def page_folders(request: Request, response: Response, sid: str = Cookie(default=None)):
    return serve_page("folders.html", request, response, sid)

# Path to the folder containing images
current_folder = Path(__file__).parent
images_folder = current_folder / "site_images"

# Mount the images folder at /images URL
app.mount("/images", StaticFiles(directory=images_folder), name="images")


# ── Load helpers ──────────────────────────────────────────────────────────────

def load_tags() -> dict:
    """
    tags.json: { "ActualFolderName": ["tag1", "tag2"], ... }
    """
    if not TAGS_FILE.exists():
        return {}
    try:
        return json.loads(TAGS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[tags] failed to load: {e}")
        return {}


def load_display_names() -> dict:
    """
    folders.json: { "ActualFolderName": "Display Name To Show", ... }
    Falls back to actual folder name if key missing.
    """
    if not FOLDERS_FILE.exists():
        return {}
    try:
        raw = json.loads(FOLDERS_FILE.read_text(encoding="utf-8"))
        # support both string value and list value (takes first element)
        result = {}
        for k, v in raw.items():
            result[k] = v[0] if isinstance(v, list) else str(v)
        return result
    except Exception as e:
        print(f"[folders] failed to load: {e}")
        return {}


# ── Visit stats ───────────────────────────────────────────────────────────────
#
# stats.json structure:
# {
#   "cumulative": 42,          <- total page loads ever
#   "unique": 15,              <- unique sessions ever seen
#   "sessions": ["sid1", ...]  <- list of seen session ids
# }
 
_stats_lock = threading.Lock()
 
def _load_stats() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"cumulative": 0, "unique": 0, "sessions": []}
 
def _save_stats(data: dict):
    try:
        STATS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[stats] write failed: {e}")

_hotlog_lock = threading.Lock()

def _load_hotlog() -> dict:
    if HOTLOG_FILE.exists():
        try:
            return json.loads(HOTLOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"images": {}, "folders": {}}

def _save_hotlog(data: dict):
    try:
        HOTLOG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[hotlog] write failed: {e}")

_visitlog_lock = threading.Lock()

def _load_visitlog() -> dict:
    if VISITLOG_FILE.exists():
        try:
            return json.loads(VISITLOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"countries": {}, "paths": {}, "recent": []}

def _save_visitlog(data: dict):
    try:
        VISITLOG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[visitlog] write failed: {e}")

def log_visit(request: Request, path: str):
    """Log country and path from Cloudflare headers."""
    country = request.headers.get("CF-IPCountry", "XX")  # XX = unknown
    with _visitlog_lock:
        data = _load_visitlog()
        # country count
        data["countries"][country] = data["countries"].get(country, 0) + 1
        # path count
        data["paths"][path] = data["paths"].get(path, 0) + 1
        # recent visits — keep last 200
        data["recent"].append({
            "country": country,
            "path":    path,
            "time":    time.time(),
        })
        data["recent"] = data["recent"][-200:]
        _save_visitlog(data)
 
def record_visit(sid: str | None) -> tuple[str, bool]:
    """
    Increment counters. Returns (session_id, is_new_session).
    Creates a new session id if sid is None or unrecognised.
    """
    with _stats_lock:
        data = _load_stats()
        is_new = False
        if not sid or sid not in data["sessions"]:
            sid = str(uuid.uuid4())
            data["sessions"].append(sid)
            data["unique"] += 1
            is_new = True
        data["cumulative"] += 1
        _save_stats(data)
    return sid, is_new
 
def serve_page(filename: str, request: Request, response: Response, sid: str | None) -> HTMLResponse:
    new_sid, is_new = record_visit(sid)
    log_visit(request, request.url.path)
    html = (PAGES_DIR / filename).read_text(encoding="utf-8")
    resp = HTMLResponse(content=html)
    if is_new:
        # set session cookie — expires when browser closes (session cookie)
        resp.set_cookie(key="sid", value=new_sid, httponly=True, samesite="lax")
    return resp

#-- Scan folder names for dates for sorting
def extract_date_from_name(name: str) -> float | None:
    """
    Auto-detect any date in folder name.
    Tries 8-digit then 6-digit runs, then year-only fallback.
    """
    def try_date(y, m, d):
        try:
            if 1990 <= y <= 2030 and 1 <= m <= 12 and 1 <= d <= 31:
                return datetime(y, m, d).timestamp()
        except Exception:
            pass
        return None

    # strip separators but keep original for year fallback
    clean = re.sub(r'[_\-\.]', '', name)

    # 8-digit: try YYYYMMDD first, then DDMMYYYY
    for digits in re.findall(r'(?<!\d)\d{8}(?!\d)', clean):
        t = try_date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        if t: return t
        t = try_date(int(digits[4:]), int(digits[2:4]), int(digits[:2]))
        if t: return t

    # 6-digit: only match if NOT part of an 8-digit run
    # remove all 8-digit sequences first to avoid partial matches
    clean6 = re.sub(r'\d{8}', '', clean)
    for digits in re.findall(r'(?<!\d)\d{6}(?!\d)', clean6):
        # YYMMDD
        y = int(digits[:2])
        y = 2000 + y if y <= 30 else 1900 + y
        t = try_date(y, int(digits[2:4]), int(digits[4:]))
        if t: return t
        # DDMMYY
        y2 = int(digits[4:])
        y2 = 2000 + y2 if y2 <= 30 else 1900 + y2
        t = try_date(y2, int(digits[2:4]), int(digits[:2]))
        if t: return t

    # year only fallback
    m = re.search(r'(?<!\d)(19|20)(\d{2})(?!\d)', name)
    if m:
        try:
            return datetime(int(m.group(1) + m.group(2)), 1, 1).timestamp()
        except Exception:
            pass

    return None


# ── Cache ─────────────────────────────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.Lock()


def _build_cache() -> dict:
    tag_map      = load_tags()
    display_map  = load_display_names()

    folders: dict = {}
    tag_index: dict = {}
    all_images: list = []

    for folder in sorted(DOWNLOADS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        images = sorted(f for f in folder.iterdir() if f.suffix.lower() in IMAGE_EXTS)
        if not images:
            continue

        fname        = folder.name
        display_name = display_map.get(fname, fname)   # fallback to actual name
        tags         = tag_map.get(fname, ["misc"])
        urls         = [f"/media/{f.relative_to(DOWNLOADS_DIR).as_posix()}" for f in images]
        mtimes = [f.stat().st_mtime for f in images]
        name_date = extract_date_from_name(fname)
        first_thumb = "/thumbs/" + images[0].relative_to(DOWNLOADS_DIR).with_suffix(".jpg").as_posix()
        folders[fname] = {
            "display_name":   display_name,
            "tags":           tags,
            "images":         urls,
            "mtimes":         mtimes,
            "thumbnail":      first_thumb,
            "mtime_thumb":    mtimes[0],
            "mtime_latest":   name_date if name_date else max(mtimes),
            "mtime_earliest": name_date if name_date else min(mtimes),
            "count":          len(urls),
        }

        for tag in tags:
            tag_index.setdefault(tag, []).append(fname)

        for img_path, url in zip(images, urls):
            thumb_url = "/thumbs/" + img_path.relative_to(DOWNLOADS_DIR).with_suffix(".jpg").as_posix()
            all_images.append({
                "url":           url,
                "thumb_url":     thumb_url,
                "folder":        fname,
                "display_name":  display_name,
                "mtime":         name_date if name_date else img_path.stat().st_mtime,
                "has_name_date": name_date is not None,
            })

    return {
        "built_at":   time.time(),
        "folders":    folders,
        "tag_index":  tag_index,
        "all_images": all_images,
    }


def _save_cache(data: dict):
    try:
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[cache] write failed: {e}")


def get_cache() -> dict:
    global _cache
    with _cache_lock:
        if _cache:
            return _cache
        print("[cache] building ...")
        data = _build_cache()
        _cache = data
        _save_cache(data)
        print(f"[cache] done — {len(data['all_images'])} images, {len(data['folders'])} folders")
        return _cache


# ── Management endpoints ──────────────────────────────────────────────────────

@app.post("/api/cache/rebuild")
def rebuild_cache():
    """Rebuild cache after editing tags.json or folders.json."""
    global _cache
    data = _build_cache()
    with _cache_lock:
        _cache = data
    _save_cache(data)
    return {
        "status":  "rebuilt",
        "folders": len(data["folders"]),
        "images":  len(data["all_images"]),
    }


@app.get("/api/cache/status")
def cache_status():
    c = get_cache()
    return {
        "built_at":    c.get("built_at"),
        "age_seconds": int(time.time() - c.get("built_at", 0)),
        "folders":     len(c["folders"]),
        "images":      len(c["all_images"]),
        "cache_file":  str(CACHE_FILE),
        "tags_file":   str(TAGS_FILE),
        "folders_file": str(FOLDERS_FILE),
    }

@app.get("/api/tags/generate")
def generate_tags_file(key: str):
    if key != SECRET_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    # Load existing tags if file exists
    if TAGS_FILE.exists():
        with TAGS_FILE.open("r", encoding="utf-8") as f:
            result = json.load(f)
    else:
        result = {}

    new_folders = 0
    for folder in sorted(DOWNLOADS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        if any(f.suffix.lower() in IMAGE_EXTS for f in folder.iterdir()):
            if folder.name not in result:
                result[folder.name] = ["misc"]
                new_folders += 1

    TAGS_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    return {
        "status": "updated",
        "new_folders_added": new_folders,
        "total_folders": len(result),
        "file": str(TAGS_FILE),
        "warning": "Edit tags.json manually, then call POST /api/cache/rebuild."
    }


@app.get("/api/folders/generate")
def generate_folders_file(key: str):
    """
    Generate folders.json with every folder display name defaulting to the actual folder name.
    Only call once on first setup — overwrites manual edits!
    """
    if key != SECRET_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
    result = {}
    for folder in sorted(DOWNLOADS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        if any(f.suffix.lower() in IMAGE_EXTS for f in folder.iterdir()):
            result[folder.name] = folder.name   # default: show actual name
    FOLDERS_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status":  "generated",
        "folders": len(result),
        "file":    str(FOLDERS_FILE),
        "warning": "Edit folders.json manually, then call POST /api/cache/rebuild."
    }


@app.get("/api/tags")
def get_tags_file():
    return load_tags()


@app.get("/api/folders/names")
def get_folders_file():
    return load_display_names()


# ── API ────────────────────────────────────────────────────────────────────────

@app.get("/api/filters")
def get_filters():
    c = get_cache()
    result = []
    for tag, folder_names in c["tag_index"].items():
        count = sum(c["folders"][fn]["count"] for fn in folder_names if fn in c["folders"])
        result.append({"tag": tag, "count": count})
    result.sort(key=lambda x: -x["count"])
    return result


@app.get("/api/images")
def get_images(
    page: int = Query(1, ge=1),
    per_page: int = Query(40, ge=1, le=200),
    tags: Optional[str] = Query(None, description="Comma-separated tags — AND logic"),
    shuffle: bool = Query(False),
    sort: Optional[str] = Query(None, description="oldest, newest"),
):
    c = get_cache()

    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            valid = None
            for tag in tag_list:
                s = set(c["tag_index"].get(tag, []))
                valid = s if valid is None else valid & s
            valid = valid or set()
            items = [img for img in c["all_images"] if img["folder"] in valid]
        else:
            items = list(c["all_images"])
    else:
        items = list(c["all_images"])

    if sort == "oldest":
        items = [x for x in items if x.get("has_name_date")]
        items.sort(key=lambda x: x.get("mtime", 0))
    elif sort == "newest":
        items = [x for x in items if x.get("has_name_date")]
        items.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    elif sort == "hot":
        with _hotlog_lock:
            hotdata = _load_hotlog()
        scores = hotdata.get("images", {})
        items = [x for x in items if x["url"] in scores]
        items.sort(key=lambda x: scores.get(x["url"], 0), reverse=True)
    elif shuffle:
        random.shuffle(items)


    total = len(items)
    start = (page - 1) * per_page
    end   = start + per_page
    return {
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "has_more": end < total,
        "items":    items[start:end],
    }

@app.get("/api/folders")
def get_folders(sort: Optional[str] = Query(None)):
    c = get_cache()
    folders = [
        {
            "name":           name,
            "display_name":   info["display_name"],
            "count":          info["count"],
            "thumbnail":      info["thumbnail"],
            "tags":           info["tags"],
            "mtime_latest":   info.get("mtime_latest", 0),
            "mtime_earliest": info.get("mtime_earliest", 0),
        }
        for name, info in c["folders"].items()
    ]
    if sort == "hot":
        with _hotlog_lock:
            hotdata = _load_hotlog()
        scores = hotdata.get("folders", {})
        folders = [f for f in folders if f["name"] in scores]
        folders.sort(key=lambda x: scores.get(x["name"], 0), reverse=True)
    return folders

@app.get("/api/folder/{folder_name}")
def get_folder_images(
    folder_name: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(60, ge=1, le=300),
    sort: Optional[str] = Query(None),
):
    c = get_cache()
    if folder_name not in c["folders"]:
        raise HTTPException(404, "Folder not found")
    info   = c["folders"][folder_name]
    paired = list(zip(info["images"], info.get("mtimes", [0]*len(info["images"]))))
    if sort == "oldest":
        paired.sort(key=lambda x: x[1])
    elif sort == "newest":
        paired.sort(key=lambda x: x[1], reverse=True)
    total  = len(paired)
    start  = (page - 1) * per_page
    end    = start + per_page
    items  = [{"url": url, "thumb_url": url.replace("/media/", "/thumbs/").rsplit(".", 1)[0] + ".jpg", "folder": folder_name, "display_name": info["display_name"], "mtime": mt}
              for url, mt in paired[start:end]]
    return {"total": total, "page": page, "per_page": per_page, "has_more": end < total, "items": items}

@app.get("/api/stats")
def get_stats():
    with _stats_lock:
        data = _load_stats()
    return {
        "cumulative": data["cumulative"],
        "unique":     data["unique"],
    }

@app.get("/api/cache/generate")
def generate_cache():
    """Rescan all folders and rebuild cache.json. Call after adding new images."""
    global _cache
    data = _build_cache()
    with _cache_lock:
        _cache = data
    _save_cache(data)
    return {
        "status":   "generated",
        "folders":  len(data["folders"]),
        "images":   len(data["all_images"]),
        "built_at": data["built_at"],
    }
def get_thumb_url(media_url: str) -> str:
    """Convert /media/Folder/img.jpg -> /thumbs/Folder/img.jpg"""
    return media_url.replace("/media/", "/thumbs/", 1)


def generate_thumbnail(args) -> str:
    src_path, thumb_path, thumb_size = args
    try:
        if thumb_path.exists():
            return "skipped"
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src_path) as img:
            img = img.convert("RGB")
            img.thumbnail(thumb_size, Image.LANCZOS)
            img.save(thumb_path, "JPEG", quality=75, optimize=True)
        return "created"
    except Exception as e:
        print(f"[thumb] failed {src_path}: {e}")
        return f"error:{e}"

@app.get("/api/thumbnails/generate")
def generate_thumbnails(key: str):
    """
    Generate thumbnails for all images that don't have one yet.
    Safe to call multiple times — skips existing thumbnails.
    """
    if key != SECRET_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    global _thumb_progress
    if _thumb_progress["running"]:
        return {"status": "already_running", "progress": _thumb_progress}

    # count total first
    total = sum(
        1 for folder in DOWNLOADS_DIR.iterdir()
        if folder.is_dir()
        for f in folder.iterdir()
        if f.suffix.lower() in IMAGE_EXTS
    )

    with _thumb_lock:
        _thumb_progress = {"total": total, "created": 0, "skipped": 0, "running": True, "done": False, "current": ""}

    def run():
        global _thumb_progress
        THUMBNAILS_DIR.mkdir(exist_ok=True)

        tasks = []
        for folder in sorted(DOWNLOADS_DIR.iterdir()):
            if not folder.is_dir():
                continue
            for img_path in sorted(folder.iterdir()):
                if img_path.suffix.lower() not in IMAGE_EXTS:
                    continue
                rel        = img_path.relative_to(DOWNLOADS_DIR)
                thumb_path = THUMBNAILS_DIR / rel.with_suffix(".jpg")
                tasks.append((img_path, thumb_path, THUMB_SIZE))

        workers = max(1, os.cpu_count() - 1)
        print(f"[thumbs] starting {len(tasks)} tasks on {workers} workers")

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(generate_thumbnail, t): t for t in tasks}
            for future in as_completed(futures):
                result = future.result()
                with _thumb_lock:
                    _thumb_progress["current"] = futures[future][0].name
                    if result == "created":
                        _thumb_progress["created"] += 1
                    elif result == "skipped":
                        _thumb_progress["skipped"] += 1

        with _thumb_lock:
            _thumb_progress["running"] = False
            _thumb_progress["done"]    = True
        print("[thumbs] done")

    threading.Thread(target=run, daemon=True).start()
    return {"status": "started", "total": total}


@app.get("/api/thumbnails/progress")
def thumbnail_progress():
    with _thumb_lock:
        p = dict(_thumb_progress)
    p["percent"] = round((p["created"] + p["skipped"]) / max(p["total"], 1) * 100, 1)
    return p


@app.get("/api/thumbnails/status")
def thumbnail_status():
    """Check how many thumbnails exist vs total images."""
    if not THUMBNAILS_DIR.exists():
        return {"thumbs": 0, "total": 0, "ready": False}
    thumbs = sum(1 for f in THUMBNAILS_DIR.rglob("*") if f.suffix.lower() == ".jpg")
    c = get_cache()
    return {
        "thumbs":  thumbs,
        "total":   len(c["all_images"]),
        "ready":   thumbs > 0,
        "thumbs_dir": str(THUMBNAILS_DIR),
    }

@app.get("/api/thumbnails/progress/view", response_class=HTMLResponse)
def thumbnail_progress_view():
    return """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>Thumbnail Progress</title>
<style>
  body { font-family: monospace; background: #0a0a0f; color: #e2e2ef; padding: 2rem; }
  h2 { color: #7c6af7; margin-bottom: 1rem; }
  .bar-wrap { background: #1e1e2e; border-radius: 8px; height: 24px; margin: 1rem 0; overflow: hidden; }
  .bar { height: 100%; background: #7c6af7; transition: width .3s; border-radius: 8px; }
  .info { color: #6b6b80; font-size: .85rem; margin-top: .5rem; }
  .done { color: #4caf50; font-size: 1.2rem; margin-top: 1rem; }
  .stat { margin: .3rem 0; }
</style>
</head><body>
<h2>Thumbnail Generation</h2>
<div class="bar-wrap"><div class="bar" id="bar" style="width:0%"></div></div>
<div id="percent" style="font-size:1.5rem;color:#7c6af7">0%</div>
<div class="stat" id="counts">Starting...</div>
<div class="stat info" id="current"></div>
<div id="doneMsg"></div>
<script>
async function poll() {
  try {
    const r = await fetch('/api/thumbnails/progress');
    const d = await r.json();
    const done = d.created + d.skipped;
    document.getElementById('bar').style.width = d.percent + '%';
    document.getElementById('percent').textContent = d.percent + '%';
    document.getElementById('counts').textContent =
      `${done.toLocaleString()} / ${d.total.toLocaleString()} — created: ${d.created.toLocaleString()}, skipped: ${d.skipped.toLocaleString()}`;
    document.getElementById('current').textContent = d.current ? `Processing: ${d.current}` : '';
    if (d.done) {
      document.getElementById('doneMsg').innerHTML = '<div class="done">✓ All done!</div>';
      return;
    }
    if (d.running || (!d.done && done < d.total)) setTimeout(poll, 800);
  } catch(e) { setTimeout(poll, 2000); }
}
poll();
</script>
</body></html>"""

@app.post("/api/hot/image")
def log_image_click(url: str = Query(...)):
    """Log when a full-res image is opened."""
    with _hotlog_lock:
        data = _load_hotlog()
        data["images"][url] = data["images"].get(url, 0) + 1
        _save_hotlog(data)
    return {"ok": True}

@app.post("/api/hot/folder")
def log_folder_click(name: str = Query(...)):
    """Log when a folder is opened."""
    with _hotlog_lock:
        data = _load_hotlog()
        data["folders"][name] = data["folders"].get(name, 0) + 1
        _save_hotlog(data)
    return {"ok": True}

@app.get("/api/hot/stats")
def hot_stats():
    with _hotlog_lock:
        data = _load_hotlog()
    top_images  = sorted(data["images"].items(),  key=lambda x: -x[1])[:20]
    top_folders = sorted(data["folders"].items(), key=lambda x: -x[1])[:20]
    return {"top_images": top_images, "top_folders": top_folders}

@app.get("/api/visitlog")
def get_visitlog(key: str = ""):
    if key != SECRET_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
    with _visitlog_lock:
        data = _load_visitlog()
    # sort countries by count
    countries = sorted(data["countries"].items(), key=lambda x: -x[1])
    paths     = sorted(data["paths"].items(),     key=lambda x: -x[1])
    # format recent with human time
    recent = []
    for v in reversed(data["recent"][-50:]):
        recent.append({
            "country": v["country"],
            "path":    v["path"],
            "time":    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v["time"])),
        })
    return {
        "countries": countries,
        "paths":     paths,
        "recent":    recent,
    }
@app.get("/api/debug/headers")
def debug_headers(request: Request):
    return {
        "CF-IPCountry":    request.headers.get("CF-IPCountry", "NOT PRESENT"),
        "CF-Connecting-IP": request.headers.get("CF-Connecting-IP", "NOT PRESENT"),
        "X-Forwarded-For": request.headers.get("X-Forwarded-For", "NOT PRESENT"),
        "host":            request.headers.get("host", "NOT PRESENT"),
    }

@app.on_event("startup")
async def startup():
    get_cache()
