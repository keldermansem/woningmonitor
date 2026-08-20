#!/usr/bin/env python3
"""
Monitor voor nieuw woningaanbod van At Home Vastgoed (athomevastgoed.nl).

De woningen op de site worden via JavaScript ingeladen, dus we renderen de
pagina met een echte (headless) browser. Elke woning heeft een eigen URL met
een uniek nummer aan het eind, bijvoorbeeld:

    /woningaanbod/huren-appartement-rotterdam-crooswijkseweg-te-huur-5251

Dat nummer gebruiken we als vingerafdruk. Zien we een nummer dat nog niet in
seen.json staat, dan is het nieuw aanbod en gaat er een mail uit.

Instellen via omgevingsvariabelen (zie README.md):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO
    MAIL_FROM             (optioneel, standaard SMTP_USER)
    NOTIFY_ON_FIRST_RUN   (optioneel, "1" = ook mailen bij allereerste run)
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

BASE_URL = "https://www.athomevastgoed.nl"
LIST_URL = f"{BASE_URL}/woningaanbod"
STATE_FILE = Path(__file__).with_name("seen.json")

MAX_PAGES = 6          # hoeveel paginanummers we maximaal bezoeken
MAX_LOAD_MORE = 8      # hoe vaak we op een 'toon meer'-knop klikken
PAGE_TIMEOUT_MS = 45_000


# --------------------------------------------------------------------------
# HTML uitlezen (los van de browser, zodat dit apart te testen is)
# --------------------------------------------------------------------------

ANCHOR_RE = re.compile(
    r"<a\b[^>]*?href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<inner>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
LISTING_PATH_RE = re.compile(r"^(?:https?://(?:www\.)?athomevastgoed\.nl)?(/woningaanbod/[^?#]+)$", re.I)
LISTING_ID_RE = re.compile(r"-(\d+)/?$")
PAGINATION_RE = re.compile(r"href=[\"']([^\"']*[?&]page=\d+[^\"']*)[\"']", re.I)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def _clean_text(raw_html: str) -> str:
    """Haal tags weg en normaliseer witruimte."""
    return WS_RE.sub(" ", unescape(TAG_RE.sub(" ", raw_html))).strip()


def title_from_slug(path: str) -> str:
    """
    'huren-appartement-rotterdam-crooswijkseweg-te-huur-5251'
      -> 'Appartement Rotterdam Crooswijkseweg'
    Betrouwbare terugvaloptie als de linktekst leeg is.
    """
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    slug = LISTING_ID_RE.sub("", slug)
    for junk in ("huren-", "huur-", "te-huur", "te-koop", "kopen-"):
        slug = slug.replace(junk, " ")
    words = [w for w in slug.replace("-", " ").split() if w]
    return " ".join(w.capitalize() for w in words) or "Woning"


def extract_listings(html: str) -> dict[str, dict]:
    """Geef alle woningen terug die in deze HTML staan, als {id: {...}}."""
    found: dict[str, dict] = {}

    for match in ANCHOR_RE.finditer(html):
        href = match.group("href").strip()
        path_match = LISTING_PATH_RE.match(href)
        if not path_match:
            continue

        path = path_match.group(1)
        id_match = LISTING_ID_RE.search(path)
        if not id_match:
            continue  # bijv. /woningaanbod/filter -- geen echte woning

        listing_id = id_match.group(1)
        text = _clean_text(match.group("inner"))
        # Linktekst is vaak alleen "Bekijk woning" of leeg; dan de slug gebruiken.
        if len(text) < 8 or text.lower() in {"bekijk woning", "lees meer", "meer info"}:
            text = title_from_slug(path)

        existing = found.get(listing_id)
        if existing is None or len(text) > len(existing["title"]):
            found[listing_id] = {"title": text[:200], "url": urljoin(BASE_URL, path)}

    return found


def extract_pagination(html: str) -> list[str]:
    """Vind links naar volgende pagina's (?page=2 enzovoort)."""
    urls = []
    for href in PAGINATION_RE.findall(html):
        full = urljoin(BASE_URL, unescape(href))
        if full.startswith(f"{BASE_URL}/woningaanbod") and full not in urls:
            urls.append(full)
    return urls


# --------------------------------------------------------------------------
# De site ophalen met een echte browser
# --------------------------------------------------------------------------

def _dismiss_cookiebanner(page) -> None:
    """Klik een cookiemelding weg. Voorkeur voor 'alleen noodzakelijk'."""
    from playwright.sync_api import Error as PlaywrightError

    patterns = [
        re.compile(r"(alleen noodzakelijk|noodzakelijke|weigeren|reject|necessary only)", re.I),
        re.compile(r"(accepteer|akkoord|accept|toestaan)", re.I),
    ]
    for pattern in patterns:
        try:
            button = page.get_by_role("button", name=pattern).first
            if button.count() and button.is_visible():
                button.click(timeout=3000)
                page.wait_for_timeout(500)
                return
        except (PlaywrightError, AssertionError):
            continue


def _click_load_more(page) -> None:
    """Klik herhaaldelijk op een 'toon meer'-knop, als die bestaat."""
    from playwright.sync_api import Error as PlaywrightError

    label = re.compile(r"(laad meer|toon meer|meer laden|meer resultaten|load more|show more)", re.I)
    for _ in range(MAX_LOAD_MORE):
        try:
            button = page.get_by_role("button", name=label).first
            if not button.count() or not button.is_visible():
                return
            button.click(timeout=5000)
            page.wait_for_timeout(1500)
        except (PlaywrightError, AssertionError):
            return


def fetch_current_listings() -> dict[str, dict]:
    """Render het woningaanbod (incl. eventuele vervolgpagina's) en lees het uit."""
    from playwright.sync_api import sync_playwright

    listings: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            locale="nl-NL",
            viewport={"width": 1400, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        queue = [LIST_URL]
        visited: set[str] = set()

        while queue and len(visited) < MAX_PAGES:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            print(f"  -> ophalen: {url}", flush=True)
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

            if len(visited) == 1:
                _dismiss_cookiebanner(page)

            # Wachten tot de JavaScript het aanbod heeft ingeladen.
            try:
                page.wait_for_selector('a[href*="/woningaanbod/"]', timeout=20_000)
            except Exception:
                print("     (geen woninglinks gezien binnen 20s)", flush=True)
            page.wait_for_timeout(2500)

            _click_load_more(page)

            html = page.content()
            page_listings = extract_listings(html)
            print(f"     {len(page_listings)} woning(en) gevonden", flush=True)
            listings.update(page_listings)

            for next_url in extract_pagination(html):
                if next_url not in visited and next_url not in queue:
                    queue.append(next_url)

        browser.close()

    return listings


# --------------------------------------------------------------------------
# Opslag van wat we al gezien hebben
# --------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("! seen.json is beschadigd; ik begin opnieuw.", file=sys.stderr)
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# E-mail versturen
# --------------------------------------------------------------------------

def send_email(new_items: list[dict]) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    mail_to = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]
    mail_from = os.environ.get("MAIL_FROM", user)

    count = len(new_items)
    subject = (
        f"Nieuwe huurwoning: {new_items[0]['title']}"
        if count == 1
        else f"{count} nieuwe huurwoningen bij At Home Vastgoed"
    )

    plain_lines = ["Nieuw op athomevastgoed.nl:", ""]
    html_rows = []
    for item in new_items:
        plain_lines += [f"* {item['title']}", f"  {item['url']}", ""]
        html_rows.append(
            f'<li style="margin-bottom:14px">'
            f'<a href="{item["url"]}" style="font-size:16px;font-weight:600">{item["title"]}</a>'
            f"</li>"
        )
    plain_lines.append(LIST_URL)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = ", ".join(mail_to)
    message.set_content("\n".join(plain_lines))
    message.add_alternative(
        "<html><body style=\"font-family:Helvetica,Arial,sans-serif;color:#222\">"
        f"<p>Er staat nieuw aanbod online ({count}):</p>"
        f"<ul style=\"padding-left:18px\">{''.join(html_rows)}</ul>"
        f'<p style="font-size:13px;color:#666">'
        f'<a href="{LIST_URL}">Bekijk het volledige aanbod</a></p>'
        "</body></html>",
        subtype="html",
    )

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as server:
            server.login(user, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.send_message(message)

    print(f"E-mail verstuurd naar {', '.join(mail_to)}", flush=True)


# --------------------------------------------------------------------------

def main() -> int:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}] Controle gestart", flush=True)

    current = fetch_current_listings()

    # Veiligheidsklep: bij nul resultaten is er iets stuk (site plat, layout
    # gewijzigd, browser mislukt). Dan de opslag NIET overschrijven, anders
    # zou de volgende run het complete aanbod als 'nieuw' mailen.
    if not current:
        print("! Geen enkele woning gevonden. Opslag blijft ongewijzigd.", file=sys.stderr)
        return 1

    print(f"Totaal {len(current)} woning(en) online.", flush=True)

    state = load_state()
    first_run = not state
    new_ids = [i for i in current if i not in state]

    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for listing_id, data in current.items():
        entry = state.setdefault(listing_id, {"eerst_gezien": timestamp})
        entry.update(data)

    if first_run and os.environ.get("NOTIFY_ON_FIRST_RUN") != "1":
        save_state(state)
        print(f"Eerste run: {len(current)} woningen vastgelegd als uitgangspunt. Geen mail.", flush=True)
        return 0

    if not new_ids:
        save_state(state)
        print("Geen nieuw aanbod.", flush=True)
        return 0

    new_items = [current[i] for i in sorted(new_ids, key=int, reverse=True)]
    print(f"{len(new_items)} nieuw(e) woning(en):", flush=True)
    for item in new_items:
        print(f"  - {item['title']} | {item['url']}", flush=True)

    try:
        send_email(new_items)
    except KeyError as missing:
        print(f"! Ontbrekende instelling: {missing}. Opslag niet bijgewerkt.", file=sys.stderr)
        return 1
    except Exception as error:  # mail mislukt -> state niet opslaan, volgende run probeert opnieuw
        print(f"! Verzenden mislukt: {error}. Opslag niet bijgewerkt.", file=sys.stderr)
        return 1

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
