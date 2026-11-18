"""BookMyShow ticket checker for up to two movies.

Environment variables:
  BMS1_URL, BMS1_DATES, BMS1_THEATRES, BMS1_TIME
  BMS2_URL, BMS2_DATES, BMS2_THEATRES, BMS2_TIME
  RESEND_API_KEY, RESEND_TO_EMAIL, RESEND_FROM_EMAIL
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  BMS_TEST_MODE (true/false)

BMS_TEST_MODE=true sends exactly one Telegram test message after the movie
checks complete. It suppresses change-triggered Telegram messages, but real
change-triggered email notifications are still sent.
"""

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATE_FILE = "bms_state.json"
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)

BMS_TEST_MODE = os.getenv("BMS_TEST_MODE", "false").strip().lower() in {
    "true", "1", "yes", "on"
}

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT", "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST", "🟠"),
    "3": ("AVAILABLE", "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default": "AVAILABLE",
}

TIME_PERIODS = {
    "morning": (600, 1200),
    "afternoon": (1200, 1600),
    "evening": (1600, 1900),
    "night": (1900, 2400),
}

REGION_MAP = {
    "chennai": ("CHEN", "chennai", "13.056", "80.206", "tf3"),
    "mumbai": ("MUMBAI", "mumbai", "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "delhi": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "bangalore": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "hyderabad": ("HYD", "hyderabad", "17.385", "78.487", "tep"),
    "kolkata": ("KOLK", "kolkata", "22.573", "88.364", "tun"),
    "pune": ("PUNE", "pune", "18.520", "73.856", "te2"),
    "kochi": ("KOCH", "kochi", "9.932", "76.267", "t9z"),
}


def env_movie_config(number):
    prefix = f"BMS{number}_"
    # BMS1_* / BMS2_* are preferred. BMS_URL etc. remain supported for BMS1.
    legacy = number == 1
    return {
        "name": f"Movie {number}",
        "url": os.getenv(prefix + "URL", os.getenv("BMS_URL", "") if legacy else "").strip(),
        "dates": os.getenv(prefix + "DATES", os.getenv("BMS_DATES", "") if legacy else "").strip(),
        "theatres": os.getenv(prefix + "THEATRES", os.getenv("BMS_THEATRES", os.getenv("BMS_THEATRE", "")) if legacy else "").strip(),
        "time": os.getenv(prefix + "TIME", os.getenv("BMS_TIME", "") if legacy else "").strip(),
    }


MOVIES = [env_movie_config(1), env_movie_config(2)]

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CatInfo:
    name: str
    price: str
    status: str


@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list = field(default_factory=list)


@dataclass
class DateInfo:
    date_code: str
    status: str


# ---------------------------------------------------------------------------
# URL parsing and region resolution
# ---------------------------------------------------------------------------

def parse_bms_url(url):
    parts = urlparse(url).path.strip("/").split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for part in parts:
        if re.fullmatch(r"ET\d{8,}", part):
            result["event_code"] = part
        elif re.fullmatch(r"\d{8}", part):
            result["date_code"] = part
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result


def resolve_region(slug):
    key = (slug or "").lower().strip()
    return REGION_MAP.get(key, (key.upper()[:6], key, "0", "0", ""))


# ---------------------------------------------------------------------------
# BookMyShow API
# ---------------------------------------------------------------------------

def fetch_bms(event_code, date_code, region_code, region_slug, lat, lon, geohash):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://in.bookmyshow.com/movies/{region_slug}/buytickets/{event_code}/",
        "sec-ch-ua": '"Chromium";v="145", "Not:A-Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "",
        "lsId": "",
        "subCode": "",
        "lat": lat,
        "lon": lon,
    }
    try:
        response = requests.get(API_URL, headers=headers, params=params, timeout=20)
        if response.status_code == 200:
            return response.json()
        print(f"  ❌ BMS API returned HTTP {response.status_code}: {response.text[:300]}")
    except requests.RequestException as exc:
        print(f"  ❌ BMS request failed: {exc}")
    except ValueError as exc:
        print(f"  ❌ BMS returned invalid JSON: {exc}")
    return None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    root = data.get("data", {})
    for widget in root.get("topStickyWidgets", []):
        if widget.get("type") == "horizontal-text-list":
            for item in widget.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for component in row.get("components", []):
                        value = component.get("text", "")
                        if "•" in value:
                            info["language"] = value.strip()
    bottom = root.get("bottomSheetData", {})
    for widget in bottom.get("format-selector", {}).get("widgets", []):
        if widget.get("type") == "vertical-text-list":
            for item in widget.get("data", []):
                if item.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = item.get("text", info["name"])
    return info


def parse_dates(data):
    dates = []
    for widget in data.get("data", {}).get("topStickyWidgets", []):
        if widget.get("type") != "horizontal-block-list":
            continue
        for item in widget.get("data", []):
            if len(item.get("data", [])) >= 3:
                style = item.get("styleId", "")
                dates.append(DateInfo(
                    date_code=str(item.get("id", "")),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                ))
    return dates


def parse_shows(data):
    shows = []
    for widget in data.get("data", {}).get("showtimeWidgets", []):
        if widget.get("type") != "groupList":
            continue
        for group in widget.get("data", []):
            if group.get("type") != "venueGroup":
                continue
            for card in group.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                additional = card.get("additionalData", {})
                venue_name = additional.get("venueName", "Unknown Theatre")
                venue_code = str(additional.get("venueCode", ""))
                for item in card.get("showtimes", []):
                    extra = item.get("additionalData", {})
                    date_code = str(
                        extra.get("showDateCode", "")
                        or extra.get("dateCode", "")
                    ).strip()
                    cutoff = str(extra.get("cutOffDateTime", ""))
                    if not date_code and re.match(r"^\d{8}", cutoff):
                        date_code = cutoff[:8]
                    show = ShowInfo(
                        venue_code=venue_code,
                        venue_name=venue_name,
                        session_id=str(extra.get("sessionId", "") or extra.get("showTimeCode", "") or item.get("title", "")),
                        date_code=date_code,
                        time=str(item.get("title", "")),
                        time_code=str(extra.get("showTimeCode", "")),
                        screen_attr=str(item.get("screenAttr", "") or extra.get("attributes", "")),
                    )
                    for category in extra.get("categories", []):
                        show.categories.append(CatInfo(
                            name=str(category.get("priceDesc", "Unknown")),
                            price=str(category.get("curPrice", "0")),
                            status=str(category.get("availStatus", "")),
                        ))
                    shows.append(show)
    return shows


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_shows(shows, theatre_filter, time_periods, date_codes):
    result = []
    theatre_terms = [x.strip().lower() for x in theatre_filter.split(",") if x.strip()]
    periods = [x.strip().lower() for x in time_periods.split(",") if x.strip()]
    dates = {x.strip() for x in date_codes.split(",") if x.strip()}

    for show in shows:
        if theatre_terms and not any(term in show.venue_name.lower() for term in theatre_terms):
            continue
        if dates and show.date_code and show.date_code not in dates:
            continue
        if periods:
            try:
                time_code = int(show.time_code)
            except (TypeError, ValueError):
                time_code = -1
            if not any(
                period in TIME_PERIODS and TIME_PERIODS[period][0] <= time_code < TIME_PERIODS[period][1]
                for period in periods
            ):
                continue
        result.append(show)
    return result


# ---------------------------------------------------------------------------
# State persistence and change detection
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def migrate_legacy_state(state, configured_movies):
    """Convert the previous single-movie {shows, dates} state format."""
    if not isinstance(state, dict) or "shows" not in state:
        return state
    if len(configured_movies) != 1:
        print("  ⚠️ Legacy single-movie state found with multiple movies configured; starting fresh state.")
        return {}

    parsed = parse_bms_url(configured_movies[0]["url"])
    event_code = parsed.get("event_code")
    if not event_code:
        return {}

    venues = {}
    showtimes = {}
    for legacy_key, item in state.get("shows", {}).items():
        parts = legacy_key.split("|")
        venue_code = parts[0] if parts else ""
        session_id = parts[1] if len(parts) > 1 else item.get("time", "")
        date_code = parts[2] if len(parts) > 2 else item.get("date", "")
        key = f"{venue_code}|{session_id}|{date_code}"
        venue_name = item.get("venue", "Unknown Theatre")
        venues[venue_code] = venue_name
        show = showtimes.setdefault(key, {
            "venue_code": venue_code,
            "venue": venue_name,
            "time": item.get("time", ""),
            "date": date_code,
            "screen": "",
            "categories": {},
        })
        category_name = item.get("cat", "Unknown")
        show["categories"][category_name] = {
            "price": str(item.get("price", "0")),
            "status": str(item.get("status", "")),
        }
    print("  ℹ️ Migrated previous single-movie state to the new format.")
    return {event_code: {"venues": venues, "showtimes": showtimes}}


def save_state(state):
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    fd, temp_path = tempfile.mkstemp(prefix="bms-state-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
        os.replace(temp_path, STATE_FILE)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def build_state(shows):
    venues = {}
    showtimes = {}
    for show in shows:
        venues[show.venue_code] = show.venue_name
        session_key = show.session_id or show.time
        key = f"{show.venue_code}|{session_key}|{show.date_code}"
        showtimes[key] = {
            "venue_code": show.venue_code,
            "venue": show.venue_name,
            "time": show.time,
            "date": show.date_code,
            "screen": show.screen_attr,
            "categories": {
                cat.name: {"price": cat.price, "status": cat.status}
                for cat in show.categories
            },
        }
    return {"venues": venues, "showtimes": showtimes}


def detect_changes(old_state, new_state):
    changes = []
    old_venues = old_state.get("venues", {})
    new_venues = new_state.get("venues", {})
    old_shows = old_state.get("showtimes", {})
    new_shows = new_state.get("showtimes", {})

    new_venue_codes = set(new_venues) - set(old_venues)
    for code in sorted(new_venue_codes):
        venue_name = new_venues[code]
        count = sum(1 for s in new_shows.values() if s["venue_code"] == code)
        changes.append(f"🏢 NEW THEATRE: {venue_name} ({count} showtime(s))")

    for key in sorted(set(new_shows) - set(old_shows)):
        show = new_shows[key]
        if show["venue_code"] in new_venue_codes:
            continue
        categories = show.get("categories", {})
        if categories:
            details = ", ".join(f"{name} ₹{v['price']}" for name, v in categories.items())
        else:
            details = "No category details"
        changes.append(
            f"🆕 NEW SHOWTIME: {show['venue']} — {show['time']} [{show['date']}] — {details}"
        )

    for key in sorted(set(old_shows) & set(new_shows)):
        old_show, new_show = old_shows[key], new_shows[key]
        venue = new_show["venue"]
        time = new_show["time"]
        date = new_show["date"]
        prefix = f"{venue} — {time} [{date}]"

        if old_show.get("screen", "") != new_show.get("screen", ""):
            changes.append(f"🖥️ SCREEN CHANGED: {prefix}: {old_show.get('screen') or 'N/A'} → {new_show.get('screen') or 'N/A'}")

        old_cats = old_show.get("categories", {})
        new_cats = new_show.get("categories", {})
        for category in sorted(set(new_cats) - set(old_cats)):
            item = new_cats[category]
            changes.append(f"➕ CATEGORY ADDED: {prefix} — {category} ₹{item['price']} ({status_label(item['status'])})")
        for category in sorted(set(old_cats) - set(new_cats)):
            changes.append(f"➖ CATEGORY REMOVED: {prefix} — {category}")
        for category in sorted(set(old_cats) & set(new_cats)):
            old_item, new_item = old_cats[category], new_cats[category]
            if old_item.get("price") != new_item.get("price"):
                changes.append(f"💰 PRICE CHANGED: {prefix} — {category}: ₹{old_item.get('price')} → ₹{new_item.get('price')}")
            if old_item.get("status") != new_item.get("status"):
                old_status = old_item.get("status", "")
                new_status = new_item.get("status", "")
                old_label = status_label(old_status)
                new_label = status_label(new_status)
                if old_status == "0" and new_status != "0":
                    changes.append(f"🟢 BACK AVAILABLE: {prefix} — {category}: {old_label} → {new_label}")
                else:
                    changes.append(f"🔄 AVAILABILITY CHANGED: {prefix} — {category}: {old_label} → {new_label}")

    for key in sorted(set(old_shows) - set(new_shows)):
        show = old_shows[key]
        changes.append(f"❌ SHOWTIME REMOVED: {show['venue']} — {show['time']} [{show['date']}]")

    return changes


def status_label(status):
    return AVAIL_STATUS_MAP.get(str(status), ("UNKNOWN", "⚪"))[0]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  ⚠️ Telegram skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        if response.ok:
            print("  ✅ Telegram message sent.")
            return True
        print(f"  ❌ Telegram error {response.status_code}: {response.text[:300]}")
    except requests.RequestException as exc:
        print(f"  ❌ Telegram request failed: {exc}")
    return False


def build_telegram_message(movie_name, movie_url, changes):
    lines = [f"🎬 BMS Alert: {movie_name}", "", *[f"• {change}" for change in changes]]
    if movie_url:
        lines.extend(["", movie_url])
    return "\n".join(lines)


def send_email(subject, changes, shows, movie_info, movie_url):
    if not RESEND_API_KEY.strip() or not RESEND_TO_EMAIL.strip():
        print("  ⚠️ Email skipped: RESEND_API_KEY or RESEND_TO_EMAIL is missing.")
        return False

    now = datetime.now().strftime("%d %b %Y, %I:%M %p")
    movie_name = movie_info.get("name", "Movie")
    change_items = "".join(f"<li>{escape(change)}</li>" for change in changes)

    venue_groups = {}
    for show in shows:
        venue_groups.setdefault(show.venue_name, []).append(show)

    show_sections = []
    plain_lines = [subject, f"Checked at: {now}", "", "Changes detected:"]
    plain_lines.extend(f"- {change}" for change in changes)
    plain_lines.extend(["", "Current selected-theatre showtimes:"])

    for venue, venue_shows in venue_groups.items():
        rows = []
        plain_lines.append(f"\n{venue}")
        for show in venue_shows:
            categories = " | ".join(
                f"{cat.name} ₹{cat.price} ({status_label(cat.status)})"
                for cat in show.categories
            ) or "No category details"
            screen = f" [{show.screen_attr}]" if show.screen_attr else ""
            rows.append(f"<li>{escape(show.time + screen)} — {escape(categories)}</li>")
            plain_lines.append(f"  {show.time}{screen} — {categories}")
        show_sections.append(
            f"<h3>{escape(venue)}</h3><ul>{''.join(rows)}</ul>"
        )

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#222">
      <h2>{escape(movie_name)} — BMS ticket update</h2>
      <p>Checked at: {escape(now)}</p>
      <h3>Changes detected</h3><ul>{change_items}</ul>
      <h3>Current showtimes</h3>{''.join(show_sections)}
      <p><a href="{escape(movie_url, quote=True)}">Open BookMyShow</a></p>
    </body></html>
    """
    plain_lines.extend(["", f"BookMyShow: {movie_url}", "", "Automated BMS Ticket Checker alert."])

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY.strip()}",
                "Content-Type": "application/json",
            },
            json={
                "from": RESEND_FROM_EMAIL.strip() or "onboarding@resend.dev",
                "to": [RESEND_TO_EMAIL.strip()],
                "subject": subject,
                "text": "\n".join(plain_lines),
                "html": html,
            },
            timeout=20,
        )
        if response.status_code in (200, 201):
            print(f"  ✅ Email sent to {RESEND_TO_EMAIL}")
            return True
        print(f"  ❌ Resend error {response.status_code}: {response.text[:300]}")
    except requests.RequestException as exc:
        print(f"  ❌ Email request failed: {exc}")
    return False


def send_test_telegram_message():
    now = datetime.now().strftime("%d %b %Y, %I:%M:%S %p")
    message = (
        "🧪 BMS Ticket Checker — TEST MODE\n\n"
        "✅ Telegram notification test successful.\n"
        f"🕒 {now}\n\n"
        "This is a one-time test notification."
    )
    print("\n🧪 Test mode enabled: sending one Telegram test message.")
    send_telegram_message(message)


# ---------------------------------------------------------------------------
# Per-movie processing
# ---------------------------------------------------------------------------

def process_movie(movie, all_state):
    if not movie["url"]:
        print(f"\n⏭️ {movie['name']}: URL not configured; skipping.")
        return

    parsed = parse_bms_url(movie["url"])
    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed.get("date_code", "")
    if not event_code or not region_slug:
        print(f"\n❌ {movie['name']}: invalid BMS URL; could not find event code/region.")
        return

    region_code, resolved_slug, lat, lon, geohash = resolve_region(region_slug)
    raw_dates = movie["dates"]
    if raw_dates:
        date_list = [d.strip() for d in raw_dates.split(",") if d.strip()]
    elif url_date:
        date_list = [url_date]
    else:
        date_list = [""]

    print(f"\n{'=' * 68}\n🎬 Checking {movie['name']} ({event_code})")
    print(f"Region: {region_code} | Dates: {date_list}")

    all_shows = []
    all_dates = []
    movie_info = {"name": movie["name"], "language": ""}
    fetch_failed = False

    for date_code in date_list:
        data = fetch_bms(event_code, date_code, region_code, resolved_slug, lat, lon, geohash)
        if data is None:
            print(f"  ⚠️ Fetch failed for date {date_code or '(default)'}. State will not be updated.")
            fetch_failed = True
            continue
        if movie_info["name"] == movie["name"]:
            parsed_info = parse_movie_info(data)
            if parsed_info.get("name") != "Unknown Movie":
                movie_info = parsed_info
        all_dates.extend(parse_dates(data))
        all_shows.extend(parse_shows(data))

    if fetch_failed:
        print(f"  ⚠️ At least one date failed for {movie['name']}; preserving previous state to avoid false removal alerts.")
        return

    filtered = filter_shows(all_shows, movie["theatres"], movie["time"], movie["dates"])
    print(f"  📊 {len(filtered)} selected showtime(s) after theatre/date/time filters.")
    if not filtered:
        print("  ⚠️ No matching showtimes returned. State will not be updated.")
        return

    new_state = build_state(filtered)
    old_state = all_state.get(event_code, {})
    changes = detect_changes(old_state, new_state)

    # Save the per-movie state only after all requested date fetches succeeded.
    all_state[event_code] = new_state
    save_state(all_state)

    if changes:
        print(f"\n  ⚡ {len(changes)} selected-theatre change(s):")
        for change in changes:
            print(f"    {change}")
        subject = f"BMS Alert: {movie_info.get('name', movie['name'])} — {len(changes)} change(s)"
        send_email(subject, changes, filtered, movie_info, movie["url"])
        if not BMS_TEST_MODE:
            send_telegram_message(build_telegram_message(movie_info.get("name", movie["name"]), movie["url"], changes))
    else:
        print("  ✅ No selected-theatre changes since the previous successful check.")

    print(f"\n  Current selected-theatre status ({len(filtered)} showtime(s)):")
    for show in filtered:
        cats = ", ".join(
            f"{cat.name}=₹{cat.price} ({status_label(cat.status)})"
            for cat in show.categories
        ) or "No category details"
        screen = f" | {show.screen_attr}" if show.screen_attr else ""
        print(f"    {show.venue_name} — {show.time}{screen} [{show.date_code}] — {cats}")


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] BMS Ticket Checker")
    print(f"Test mode: {'ON' if BMS_TEST_MODE else 'OFF'}")

    state = load_state()
    configured = [movie for movie in MOVIES if movie["url"]]
    state = migrate_legacy_state(state, configured)
    if not configured:
        print("❌ No movie URLs configured. Set BMS1_URL and/or BMS2_URL.")
        sys.exit(1)

    for movie in MOVIES:
        try:
            process_movie(movie, state)
        except Exception as exc:
            # A failure in one movie must not prevent the other movie being checked.
            print(f"❌ Unexpected error while processing {movie['name']}: {exc}")

    if BMS_TEST_MODE:
        send_test_telegram_message()

    print("\nDone.")


if __name__ == "__main__":
    main()
