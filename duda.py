#!/usr/bin/env python3
# ================================================================
# duda.py - CDN Live TV Extractor (Soccer only + Composite Thumbnails)
# ================================================================

import os
import io
import re
import base64
import time
import random
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3
from PIL import Image, ImageDraw

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# === CONFIGURAZIONE ===
API_CHANNELS = "https://api.cdnlivetv.is/api/v1/channels/?user=cdnlivetv&plan=free"
API_EVENTS = "https://api.cdnlivetv.is/api/v1/events/sports/?user=cdnlivetv&plan=free"

CHANNELS_OUTPUT = "cdnlivetv_channels.m3u"
EVENTS_OUTPUT = "cdnlivetv_events.m3u"
THUMBNAILS_DIR = "thumbnails"

# Deteksi username/repo otomatis dari GitHub Actions
GITHUB_REPO = os.getenv("GITHUB_REPOSITORY", "")

# --- SOLO CALCIO ---
ONLY_SPORT = "Soccer"  # None = semua cabang olahraga

# --- ANTI RATE-LIMIT ---
MAX_WORKERS = 3
TIMEOUT = 20
RETRIES = 4
JITTER_MIN = 0.5
JITTER_MAX = 1.2
PAUSE_BETWEEN_PHASES = 10
BACKOFF_BASE = 3

MAX_CHANNELS = 0
EVENTS_ONLY_LIVE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://cdnlivetv.tv/",
    "Origin": "https://cdnlivetv.tv",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_resolve_cache: dict = {}
os.makedirs(THUMBNAILS_DIR, exist_ok=True)


# ================================================================
# UTILITY & THUMBNAIL GENERATOR
# ================================================================
def b64d(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s)
    except Exception:
        return b""


def normalize_event_name(name: str) -> str:
    if not name:
        return ""
    name = re.sub(
        r"\s*-\s*[^-]*(?:HD|FHD|SD|UHD|4K|CA|US|UK|BR|PT|ES|IT|DE|FR|AR|MX|NL|PL)\s*$",
        "", name, flags=re.IGNORECASE,
    )
    name = re.sub(
        r"\s*-\s*(?:Premiere|TSN|Sportsnet|Sky Sport|DAZN|ESPN|Fox|beIN|Sport TV|"
        r"Polsat|Movistar|Canal|Nova|V Sport|Stan Sport|Fox Sports|Max Sport|"
        r"Cosmote|Cytavision|Digi Sport|Prima Sport|Sport 5|Sport)\s*\d*\s*"
        r"(?:HD|FHD|SD)?\s*$",
        "", name, flags=re.IGNORECASE,
    )
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", name).strip().lower()


def download_image(url: str) -> Image.Image | None:
    if not url or not url.startswith("http"):
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=8, verify=False)
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGBA")
    except Exception:
        pass
    return None


def create_match_thumbnail(home_logo_url: str, away_logo_url: str, flag_logo_url: str, match_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(match_id))[:40]
    filename = f"{safe_id}.png"
    local_path = os.path.join(THUMBNAILS_DIR, filename)

    if os.path.exists(local_path):
        return local_path

    # Canvas dasar HD 16:9
    width, height = 1280, 720
    canvas = Image.new("RGBA", (width, height), (15, 23, 42, 255))
    draw = ImageDraw.Draw(canvas)

    # 1. Elemen tengah VS
    center_x, center_y = width // 2, height // 2 + 25
    radius = 50
    draw.ellipse(
        [center_x - radius, center_y - radius, center_x + radius, center_y + radius],
        fill=(30, 41, 59, 255),
        outline=(59, 130, 246, 255),
        width=3,
    )
    draw.text((center_x - 14, center_y - 10), "VS", fill=(255, 255, 255, 255))

    # 2. Logo Bendera / Turnamen (Atas Tengah, max 120x80)
    flag_img = download_image(flag_logo_url)
    if flag_img:
        flag_img.thumbnail((120, 80), Image.Resampling.LANCZOS)
        lx = (width - flag_img.width) // 2
        canvas.paste(flag_img, (lx, 40), flag_img)

    # 3. Logo Tim Kandang (Kiri, max 300x300)
    home_img = download_image(home_logo_url)
    if home_img:
        home_img.thumbnail((300, 300), Image.Resampling.LANCZOS)
        hx = 200 + (300 - home_img.width) // 2
        hy = center_y - (home_img.height // 2)
        canvas.paste(home_img, (hx, hy), home_img)

    # 4. Logo Tim Tandang (Kanan, max 300x300)
    away_img = download_image(away_logo_url)
    if away_img:
        away_img.thumbnail((300, 300), Image.Resampling.LANCZOS)
        ax = (width - 200 - 300) + (300 - away_img.width) // 2
        ay = center_y - (away_img.height // 2)
        canvas.paste(away_img, (ax, ay), away_img)

    canvas.save(local_path, format="PNG")
    print(f"  🎨 Thumbnail dibuat: {filename}")
    return local_path


# ================================================================
# RISOLUZIONE TOKEN
# ================================================================
def resolve_m3u8(player_url: str):
    if not player_url:
        return None, "no player url"
    if player_url in _resolve_cache:
        return _resolve_cache[player_url]

    time.sleep(random.uniform(JITTER_MIN, JITTER_MAX))
    last_err = ""

    for attempt in range(RETRIES):
        try:
            r = requests.get(player_url, headers=HEADERS, timeout=TIMEOUT, verify=False)
            if r.status_code in (429, 502, 503):
                time.sleep((2 ** attempt) * BACKOFF_BASE + random.uniform(0, 2))
                last_err = f"HTTP {r.status_code}"
                continue

            if r.status_code != 200:
                result = (None, f"HTTP {r.status_code}")
                _resolve_cache[player_url] = result
                return result

            html = r.text
            join_match = re.search(
                r"[A-Za-z_$][\w$]*\s*=\s*("
                r"(?:[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\)\s*\+\s*)+"
                r"[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\))",
                html,
            )
            if not join_match:
                result = (None, "no concat")
                _resolve_cache[player_url] = result
                return result

            args = re.findall(r"[A-Za-z_$][\w$]*\(([A-Za-z_$][\w$]*)\)", join_match.group(1))
            vars_dict = dict(re.findall(r"var\s+([A-Za-z_$][\w$]*)\s*=\s*'([^']*)'", html))

            parts = [b64d(vars_dict[a]).decode("utf-8", errors="replace") for a in args if a in vars_dict]
            url = "".join(parts)

            result = (url, None) if url.startswith("http") else (None, "url malformato")
            _resolve_cache[player_url] = result
            return result

        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:60]}"
            time.sleep(1 + attempt)

    result = (None, last_err or "unknown")
    _resolve_cache[player_url] = result
    return result


# ================================================================
# PROCESSORI
# ================================================================
def process_channel(ch: dict) -> dict:
    url, err = resolve_m3u8(ch.get("url", ""))
    return {
        "name": ch.get("name", "Unknown"),
        "code": ch.get("code", ""),
        "img": ch.get("image", ""),
        "url": url,
        "err": err,
    }


def process_event_item(event: dict, sport: str) -> list:
    out = []
    title = event.get("event") or f"{event.get('homeTeam', '')} vs {event.get('awayTeam', '')}".strip() or "Evento"
    league = event.get("tournament") or ""
    country = event.get("country") or ""
    status = (event.get("status") or "").lower()

    # Ekstraksi Jam dan Waktu langsung dari key API
    time_str = event.get("time") or ""
    if not time_str and event.get("start"):
        # Format start: "2026-09-26 15:00" -> ambil "15:00"
        parts = event.get("start", "").split(" ")
        if len(parts) > 1:
            time_str = parts[1]

    # Ekstraksi Logo Sesuai Format API
    home_logo = event.get("homeTeamIMG") or ""
    away_logo = event.get("awayTeamIMG") or ""
    flag_logo = event.get("countryIMG") or ""
    game_id = str(event.get("gameID") or re.sub(r"[^a-zA-Z0-9]", "_", title)[:30])

    composite_thumb_url = ""
    if home_logo or away_logo:
        local_img = create_match_thumbnail(home_logo, away_logo, flag_logo, game_id)
        if local_img and GITHUB_REPO:
            composite_thumb_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{local_img}"

    if EVENTS_ONLY_LIVE and status not in ("in", "live", "playing"):
        return out

    prefix_time = f"[{time_str}] " if time_str else ""

    for ch in event.get("channels", []) or []:
        ch_name = ch.get("channel_name", "")
        ch_url = ch.get("url", "")
        if not ch_url:
            continue

        full_name = f"{prefix_time}{title} - {ch_name}" if ch_name else f"{prefix_time}{title}"
        url, err = resolve_m3u8(ch_url)

        # Prioritas logo: Thumbnail komposit VS > logo channel bawaan
        final_logo = composite_thumb_url or ch.get("image", "")

        out.append({
            "name": full_name,
            "title": title,
            "league": league,
            "country": country,
            "time_str": time_str,
            "code": ch.get("channel_code", ""),
            "img": final_logo,
            "url": url,
            "err": err,
            "sport": sport,
        })
    return out


# ================================================================
# FETCH API
# ================================================================
def fetch_api(url: str, label: str):
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
            if r.status_code in (429, 502, 503):
                wait = (2 ** attempt) * BACKOFF_BASE + random.uniform(0, 2)
                print(f"  ⏸️ {label} HTTP {r.status_code}, wait {wait:.1f}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  ⚠️ {label} attempt {attempt+1}: {type(e).__name__}: {e}")
            time.sleep(2 + attempt)
    return None


# ================================================================
# MAIN
# ================================================================
def main():
    print("=" * 60)
    print(" CDN Live TV - Duda Extractor with Composite Thumbnails")
    print(f" Workers: {MAX_WORKERS} | Target Sport: {ONLY_SPORT}")
    print("=" * 60)

    # ============ FASE 1: EVENTI ============
    print("\n[1/2] Fetching sports events API...")
    data_ev = fetch_api(API_EVENTS, "API eventi")

    results_ev_raw = []
    if data_ev:
        root = data_ev.get("cdn-live-tv", {})
        all_events = []
        for sport, evs in root.items():
            if not isinstance(evs, list):
                continue
            if ONLY_SPORT and sport != ONLY_SPORT:
                continue
            for ev in evs:
                all_events.append((sport, ev))

        print(f"✓ {len(all_events)} events found.")

        if all_events:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = [ex.submit(process_event_item, ev, sp) for sp, ev in all_events]
                done = 0
                for fut in as_completed(futures):
                    try:
                        chunk = fut.result()
                        results_ev_raw.extend(chunk)
                    except Exception:
                        pass
                    done += 1
                    if done % 10 == 0 or done == len(all_events):
                        print(f"  Processed {done}/{len(all_events)} events...")

    # ============ PAUSA ============
    print(f"\n⏸️ Pause {PAUSE_BETWEEN_PHASES}s...")
    time.sleep(PAUSE_BETWEEN_PHASES)

    # ============ FASE 2: CANALI ============
    print("\n[2/2] Fetching TV channels API...")
    data_ch = fetch_api(API_CHANNELS, "API canali")

    channels = []
    if data_ch:
        channels = data_ch.get("channels", [])
        print(f"✓ {len(channels)} channels found.")

    if MAX_CHANNELS > 0:
        channels = channels[:MAX_CHANNELS]

    results_tv = []
    if channels:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [ex.submit(process_channel, ch) for ch in channels]
            done = 0
            for fut in as_completed(futures):
                results_tv.append(fut.result())
                done += 1
                if done % 50 == 0 or done == len(channels):
                    print(f"  Processed {done}/{len(channels)} channels...")

    # ============ DEDUPLIKASI ============
    seen_titles = set()
    results_ev = []
    for r in results_ev_raw:
        if not r.get("url"):
            continue
        key = normalize_event_name(r["title"]) or r["title"].lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        results_ev.append(r)

    # ============ OUTPUT ============
    tv_ok = [r for r in results_tv if r["url"]]
    with open(CHANNELS_OUTPUT, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for r in tv_ok:
            tvg_id = f' tvg-id="{r["code"]}"' if r["code"] else ""
            logo = f' tvg-logo="{r["img"]}"' if r["img"] else ""
            f.write(f'#EXTINF:-1{tvg_id}{logo} group-title="CDN Live TV Channels",{r["name"]}\n')
            f.write(f'{r["url"]}\n')

    with open(EVENTS_OUTPUT, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for r in results_ev:
            tvg_id = f' tvg-id="{r["code"]}"' if r.get("code") else ""
            logo = f' tvg-logo="{r["img"]}"' if r.get("img") else ""
            event_time = f' tvg-time="{r["time_str"]}"' if r.get("time_str") else ""
            sport = r.get("sport", "Sport")
            f.write(f'#EXTINF:-1{tvg_id}{logo}{event_time} group-title="CDN Live TV Events - {sport}",{r["name"]}\n')
            f.write(f'{r["url"]}\n')

    print(f"\n✅ TV Channels OK: {len(tv_ok)}/{len(results_tv)}")
    print(f"✅ Unique Events OK: {len(results_ev)}/{len(results_ev_raw)}")
    print(f"📄 Generated: {CHANNELS_OUTPUT} & {EVENTS_OUTPUT}")


if __name__ == "__main__":
    main()
