#!/usr/bin/env python3
"""
Monitor voor nieuw huuraanbod, voor meerdere makelaarssites tegelijk.

Per site wordt bijgehouden welke woningen al eens langs zijn gekomen. Verschijnt
er iets nieuws dat ook daadwerkelijk beschikbaar is, dan gaat er een mail uit.

Nieuwe site toevoegen? Zet er een blok bij in SITES hieronder. Sites die hun
aanbod pas met JavaScript inladen krijgen "browser": True; de rest niet, want
zonder browser is een controle een paar seconden in plaats van een minuut.

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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

HIER = Path(__file__).parent
INSTELLINGEN = HIER / "instellingen.json"


def laad_instellingen() -> None:
    """
    Lees instellingen.json in, als die er is.

    Op GitHub bestaat dit bestand niet en komen de gegevens uit de secrets.
    Op je laptop staat het naast het script. Bestaande omgevingsvariabelen
    winnen altijd, zodat GitHub nooit door een meegekomen bestand overruled
    kan worden.
    """
    if not INSTELLINGEN.exists():
        return
    try:
        gegevens = json.loads(INSTELLINGEN.read_text(encoding="utf-8"))
    except json.JSONDecodeError as fout:
        print(f"! instellingen.json is geen geldige JSON: {fout}", file=sys.stderr)
        print("! Let op ontbrekende komma's of aanhalingstekens.", file=sys.stderr)
        return
    for sleutel, waarde in gegevens.items():
        if sleutel.startswith("_"):
            continue  # regels die met _ beginnen zijn bedoeld als toelichting
        os.environ.setdefault(sleutel, str(waarde))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SITES = [
    {
        "id": "athomevastgoed",
        "naam": "At Home Vastgoed",
        "url": "https://www.athomevastgoed.nl/woningaanbod",
        "basis": "https://www.athomevastgoed.nl",
        "opslag": "seen.json",
        "browser": True,
        "soort": "athome",
        "wacht_op": 'a[href*="/woningaanbod/"]',
    },
    {
        "id": "rentalrotterdam",
        "naam": "Rental Rotterdam",
        "url": (
            "https://www.rentalrotterdam.nl/woningaanbod/huur/rotterdam/"
            "type-appartement,woonhuis?locationofinterest=Rotterdam"
            "&minlivablearea=25&minrooms=2&moveunavailablelistingstothebottom=true"
            "&pricerange.maxprice=1500&pricerange.minprice=100"
        ),
        "basis": "https://www.rentalrotterdam.nl",
        "opslag": "seen-rentalrotterdam.json",
        "browser": False,
        "soort": "rental",
        "wacht_op": 'a[href*="/woningaanbod/"]',
    },
    {
        "id": "indestad",
        "naam": "In de Stad",
        # per_page staat bewust op 50 in plaats van 10: dan past al het
        # gefilterde aanbod op een pagina en hoeft er niet gebladerd te worden.
        "url": (
            "https://www.indestad.nl/huurwoningen/?wpp_search%5Bpagination%5D=on"
            "&wpp_search%5Bper_page%5D=50&wpp_search%5Bstrict_search%5D=false"
            "&wpp_search%5Bproperty_type%5D=direct_aanbod"
            "&wpp_search%5Bprice%5D%5Bmin%5D=100&wpp_search%5Bprice%5D%5Bmax%5D=1400"
            "&wpp_search%5Barea%5D%5Bmin%5D=44&wpp_search%5Barea%5D%5Bmax%5D=200"
            "&wpp_search%5Bplaats%5D%5B0%5D=Rotterdam"
        ),
        "basis": "https://www.indestad.nl",
        "opslag": "seen-indestad.json",
        "browser": False,
        "soort": "indestad",
        "wacht_op": 'a[href*="/huurwoningen/"]',
    },
]

MAX_PAGES = 6
MAX_LOAD_MORE = 8
PAGE_TIMEOUT_MS = 45_000


# --------------------------------------------------------------------------
# Gereedschap voor het uitlezen van HTML
# --------------------------------------------------------------------------

ANCHOR_RE = re.compile(
    r"<a\b(?P<attrs>[^>]*?)href=[\"'](?P<href>[^\"']+)[\"'](?P<rest>[^>]*)>(?P<inner>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
ATTR_TEXT_RE = re.compile(r"(?:title|alt|aria-label)=[\"']([^\"']+)[\"']", re.IGNORECASE)
PAGINATION_RE = re.compile(r"href=[\"']([^\"']*[?&]page=\d+[^\"']*)[\"']", re.IGNORECASE)
AANTAL_RE = re.compile(r"\b(\d[\d.]*)\s+(?:object(?:en)?\s+)?gevonden\b", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

# Labels waarmee een site aangeeft dat reageren geen zin meer heeft. Alles wat
# hier NIET tussen staat geldt als beschikbaar, ook een label dat ik nog niet
# ken - liever een mail te veel dan een gemiste woning.
NIET_MEER_RE = re.compile(
    r"(bezichtiging\s+vol|zo\s+goed\s+als\s+verhuurd|\bverhuurd\b|\bverkocht\b"
    r"|onder\s+optie|\bvol\b\s*$)",
    re.IGNORECASE,
)
# Zinnen waarmee een site zelf zegt dat er niets aan je filter voldoet. Dat is
# een geldig antwoord en dus geen storing - het verschil met "ik kon de pagina
# niet lezen" is precies waar het om draait.
LEEG_RE = re.compile(
    r"(geen\s+(?:objecten|woningen|huurwoningen|resultaten|panden)\s+gevonden"
    r"|\b0\s+(?:objecten\s+)?gevonden"
    r"|geen\s+resultaten"
    r"|sorry,\s*geen)",
    re.IGNORECASE,
)
VERHUURD_RE = re.compile(r"\b(verhuurd|verkocht|onder\s+optie)\b", re.IGNORECASE)
BESCHIKBAAR_RE = re.compile(r"\b(te\s+huur|te\s+koop|nieuw\s+in\s+verhuur|beschikbaar)\b", re.IGNORECASE)
STATUS_PREFIX_RE = re.compile(r"^(?:te\s+huur|te\s+koop|verhuurd|verkocht)\s*:\s*", re.IGNORECASE)


def _clean_text(raw_html: str) -> str:
    return WS_RE.sub(" ", unescape(TAG_RE.sub(" ", raw_html))).strip()


def _status_uit(raw_anchor: str) -> str:
    """Lees uit een kaartje of de woning nog te huur is."""
    if VERHUURD_RE.search(raw_anchor):
        return "verhuurd"
    if BESCHIKBAAR_RE.search(raw_anchor):
        return "beschikbaar"
    return "beschikbaar"  # bij twijfel liever een mail te veel dan te weinig


def _beste_titel(match: re.Match) -> str:
    """Kies de mooiste omschrijving uit linktekst, title, alt of aria-label."""
    kandidaten = [_clean_text(match.group("inner"))]
    for stuk in (match.group("attrs"), match.group("rest"), match.group("inner")):
        kandidaten += [unescape(t).strip() for t in ATTR_TEXT_RE.findall(stuk)]

    beste = ""
    for kandidaat in kandidaten:
        kandidaat = STATUS_PREFIX_RE.sub("", kandidaat).strip()
        if len(kandidaat) < 6 or kandidaat.lower() in {"bekijk woning", "lees meer", "meer info", "bekijk details"}:
            continue
        if len(kandidaat) > len(beste):
            beste = kandidaat
    return beste[:200]


# --------------------------------------------------------------------------
# At Home Vastgoed: uniek nummer aan het eind van de URL
# --------------------------------------------------------------------------

ATHOME_PATH_RE = re.compile(
    r"^(?:https?://(?:www\.)?athomevastgoed\.nl)?(/woningaanbod/[^?#]+)$", re.IGNORECASE
)
ATHOME_ID_RE = re.compile(r"-(\d+)/?$")
POSTCODE_RE = re.compile(r"\b(\d{4}\s?[A-Z]{2})\b")


def titel_uit_slug(path: str) -> str:
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    slug = ATHOME_ID_RE.sub("", slug)
    for rommel in ("huren-", "huur-", "te-huur", "te-koop", "kopen-"):
        slug = slug.replace(rommel, " ")
    woorden = [w for w in slug.replace("-", " ").split() if w]
    return " ".join(w.capitalize() for w in woorden) or "Woning"


def extract_athome(html: str, basis: str) -> dict[str, dict]:
    gevonden: dict[str, dict] = {}
    for match in ANCHOR_RE.finditer(html):
        pad_match = ATHOME_PATH_RE.match(match.group("href").strip())
        if not pad_match:
            continue
        pad = pad_match.group(1)
        id_match = ATHOME_ID_RE.search(pad)
        if not id_match:
            continue

        # Bewust de straatnaam uit het webadres, niet de langste tekst uit het
        # kaartje: dat laatste levert prijzen en huurtermijnen op.
        titel = titel_uit_slug(pad)
        postcode = POSTCODE_RE.search(_beste_titel(match))
        if postcode:
            titel = f"{titel} ({postcode.group(1)})"
        woning_id = id_match.group(1)
        gevonden.setdefault(woning_id, {
            "title": titel,
            "url": urljoin(basis, pad),
            "status": "beschikbaar",  # deze site toont geen status in de lijst
        })
    return gevonden


# --------------------------------------------------------------------------
# Rental Rotterdam: /woningaanbod/huur/<plaats>/<straat>/<nummer>
# --------------------------------------------------------------------------

RENTAL_PATH_RE = re.compile(
    r"^(?:https?://(?:www\.)?rentalrotterdam\.nl)?"
    r"(/woningaanbod/(?:huur|koop)/[^/?#]+/[^/?#]+/[^/?#]+?)(?:[?#].*)?$",
    re.IGNORECASE,
)


def titel_uit_adres(pad: str) -> str:
    delen = [d for d in pad.split("/") if d]
    straat, nummer = delen[-2], delen[-1]
    straat = " ".join(w.capitalize() for w in straat.replace("-", " ").split())
    return f"{straat} {nummer.upper()}".strip()


def extract_rental(html: str, basis: str) -> dict[str, dict]:
    gevonden: dict[str, dict] = {}
    for match in ANCHOR_RE.finditer(html):
        pad_match = RENTAL_PATH_RE.match(match.group("href").strip())
        if not pad_match:
            continue
        pad = pad_match.group(1).rstrip("/")
        woning_id = pad.lower()

        titel = _beste_titel(match) or titel_uit_adres(pad)
        status = _status_uit(match.group(0))

        bestaand = gevonden.get(woning_id)
        if bestaand is None or len(titel) > len(bestaand["title"]):
            gevonden[woning_id] = {
                "title": titel,
                "url": urljoin(basis, pad),
                "status": status,
            }
        elif bestaand["status"] == "beschikbaar" and status == "verhuurd":
            bestaand["status"] = "verhuurd"  # verhuurd wint van te huur
    return gevonden


# --------------------------------------------------------------------------
# In de Stad: /huurwoningen/<straat-nummer>/
#
# Deze site zet labels als "Bezichtiging vol" NAAST de link in plaats van erin.
# Daarom knippen we de pagina op in kaartjes: alle vindplaatsen van dezelfde
# woning-URL vormen samen een blok, en het stukje HTML tussen het vorige blok
# en dit blok is waar het label staat.
# --------------------------------------------------------------------------

INDESTAD_PATH_RE = re.compile(
    r"^(?:https?://(?:www\.)?indestad\.nl)?(/huurwoningen/[^/?#]+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)
HREF_RE = re.compile(r"href=[\"'\']([^\"'\']+)[\"'\']", re.IGNORECASE)
HUISNUMMER_RE = re.compile(r"^(.*?)-((?:\d+[a-z]?)(?:-\d+[a-z]?)*)$", re.IGNORECASE)
PRIJS_RE = re.compile(r"\u20ac\s*([\d.,]+)\s*p/m", re.IGNORECASE)

TUSSENVOEGSELS = {"de", "den", "der", "het", "van", "aan", "op", "ter", "te", "in", "bij", "'t"}
LABEL_TERUGBLIK = 800  # tekens vóór een kaartje waarin we naar het label kijken


def titel_indestad(pad: str) -> str:
    """'/huurwoningen/pieter-de-hoochweg-3-2' -> 'Pieter de Hoochweg 3-2'."""
    slug = pad.rstrip("/").rsplit("/", 1)[-1]
    treffer = HUISNUMMER_RE.match(slug)
    woorddeel, nummerdeel = (treffer.group(1), treffer.group(2)) if treffer else (slug, "")

    woorden = []
    for stand, woord in enumerate(w for w in woorddeel.split("-") if w):
        woorden.append(woord if stand > 0 and woord in TUSSENVOEGSELS else woord.capitalize())
    return " ".join(woorden + ([nummerdeel.upper()] if nummerdeel else [])).strip() or "Woning"


def extract_indestad(html: str, basis: str) -> dict[str, dict]:
    # Alle vindplaatsen van woning-links, op volgorde van voorkomen.
    vindplaatsen: list[tuple[int, str]] = []
    for treffer in HREF_RE.finditer(html):
        pad_match = INDESTAD_PATH_RE.match(unescape(treffer.group(1)).strip())
        if pad_match:
            vindplaatsen.append((treffer.start(), pad_match.group(1).rstrip("/")))

    # Opeenvolgende links naar dezelfde woning horen bij één kaartje.
    blokken: list[list] = []
    for positie, pad in vindplaatsen:
        if blokken and blokken[-1][0] == pad:
            blokken[-1][2] = positie
        else:
            blokken.append([pad, positie, positie])

    gevonden: dict[str, dict] = {}
    vorig_eind = 0
    for pad, start, eind in blokken:
        zone_start = max(vorig_eind, start - LABEL_TERUGBLIK)
        kaartje = _clean_text(html[zone_start:eind])
        vorig_eind = eind

        woning_id = pad.lower()
        if woning_id in gevonden:
            continue  # eerste kaartje telt; latere herhaling negeren we

        titel = titel_indestad(pad)
        prijs = PRIJS_RE.search(kaartje)
        if prijs:
            titel = f"{titel} - \u20ac{prijs.group(1)} p/m"

        gevonden[woning_id] = {
            "title": titel[:200],
            "url": urljoin(basis, pad + "/"),
            "status": "verhuurd" if NIET_MEER_RE.search(kaartje) else "beschikbaar",
        }
    return gevonden


EXTRACTORS = {
    "athome": extract_athome,
    "rental": extract_rental,
    "indestad": extract_indestad,
}


def extract_pagination(html: str, basis: str, prefix: str) -> list[str]:
    urls = []
    for href in PAGINATION_RE.findall(html):
        volledig = urljoin(basis, unescape(href))
        if volledig.startswith(prefix) and volledig not in urls:
            urls.append(volledig)
    return urls


# --------------------------------------------------------------------------
# Pagina's ophalen
# --------------------------------------------------------------------------

def haal_statisch(url: str) -> str:
    verzoek = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.9"}
    )
    with urllib.request.urlopen(verzoek, timeout=30) as antwoord:
        ruw = antwoord.read()
        codering = antwoord.headers.get_content_charset() or "utf-8"
    return ruw.decode(codering, errors="replace")


def haal_met_browser(site: dict) -> list[str]:
    """Render de pagina (en eventuele vervolgpagina's) en geef de HTML terug."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    start_url, basis = site["url"], site["basis"]
    paginas: list[str] = []
    weigeren = re.compile(r"(alleen noodzakelijk|noodzakelijke|weigeren|reject|necessary only)", re.I)
    accepteren = re.compile(r"(accepteer|akkoord|accept|toestaan)", re.I)
    meer = re.compile(r"(laad meer|toon meer|meer laden|meer resultaten|load more|show more)", re.I)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            locale="nl-NL",
            viewport={"width": 1400, "height": 1000},
            user_agent=USER_AGENT,
        )
        page = context.new_page()

        wachtrij, bezocht = [start_url], set()
        while wachtrij and len(bezocht) < MAX_PAGES:
            url = wachtrij.pop(0)
            if url in bezocht:
                continue
            bezocht.add(url)

            print(f"     ophalen: {url}", flush=True)
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

            if len(bezocht) == 1:
                for patroon in (weigeren, accepteren):
                    try:
                        knop = page.get_by_role("button", name=patroon).first
                        if knop.count() and knop.is_visible():
                            knop.click(timeout=3000)
                            page.wait_for_timeout(500)
                            break
                    except (PlaywrightError, AssertionError):
                        continue

            try:
                page.wait_for_selector(site["wacht_op"], timeout=20_000)
            except Exception:
                print("     (geen woninglinks gezien binnen 20s)", flush=True)
            page.wait_for_timeout(2500)

            for _ in range(MAX_LOAD_MORE):
                try:
                    knop = page.get_by_role("button", name=meer).first
                    if not knop.count() or not knop.is_visible():
                        break
                    knop.click(timeout=5000)
                    page.wait_for_timeout(1500)
                except (PlaywrightError, AssertionError):
                    break

            html = page.content()
            paginas.append(html)
            for volgende in extract_pagination(html, basis, start_url.split("?")[0]):
                if volgende not in bezocht and volgende not in wachtrij:
                    wachtrij.append(volgende)

        browser.close()

    return paginas


# --------------------------------------------------------------------------
# Opslag
# --------------------------------------------------------------------------

def lees_opslag(bestandsnaam: str) -> dict:
    pad = HIER / bestandsnaam
    if not pad.exists():
        return {}
    try:
        return json.loads(pad.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"! {bestandsnaam} is beschadigd; ik begin voor deze site opnieuw.", file=sys.stderr)
        return {}


def schrijf_opslag(bestandsnaam: str, gegevens: dict) -> None:
    (HIER / bestandsnaam).write_text(
        json.dumps(gegevens, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Eén site controleren
# --------------------------------------------------------------------------

def controleer_site(site: dict) -> tuple[list[dict], dict | None]:
    """
    Geef (nieuwe woningen, bij te werken opslag) terug.

    Het wegschrijven gebeurt bewust niet hier maar in main(), want dat mag pas
    zodra de mail ook echt de deur uit is. Bij None ging er iets mis en laten
    we de opslag met rust, zodat de volgende run het opnieuw probeert.
    """
    print(f"\n=== {site['naam']} ===", flush=True)
    extractor = EXTRACTORS[site["soort"]]

    paginas: list[str] = []
    try:
        if site["browser"]:
            paginas = haal_met_browser(site)
        else:
            print(f"     ophalen: {site['url'][:90]}...", flush=True)
            paginas = [haal_statisch(site["url"])]
    except (urllib.error.URLError, OSError) as fout:
        print(f"! Ophalen mislukt: {fout}", file=sys.stderr)
        return [], None

    huidig: dict[str, dict] = {}
    for html in paginas:
        huidig.update(extractor(html, site["basis"]))

    def site_meldt_leeg() -> bool:
        return any(LEEG_RE.search(_clean_text(html)) for html in paginas)

    # Zegt de site zelf dat er niets aan het filter voldoet, dan zijn we klaar.
    # Dat is een geldig antwoord en geen storing, dus geen browserpoging en
    # geen foutmelding.
    if not huidig and site_meldt_leeg():
        print("     site meldt zelf: geen resultaten voor dit filter", flush=True)
        return [], lees_opslag(site["opslag"])

    # Terugval: sommige sites laden hun aanbod pas met JavaScript.
    if not huidig and not site["browser"]:
        print("     niets gevonden zonder browser; nog een poging mét browser", flush=True)
        try:
            paginas = haal_met_browser(site)
            for html in paginas:
                huidig.update(extractor(html, site["basis"]))
        except Exception as fout:
            print(f"! Ook met browser mislukt: {fout}", file=sys.stderr)
        if not huidig and site_meldt_leeg():
            print("     site meldt zelf: geen resultaten voor dit filter", flush=True)
            return [], lees_opslag(site["opslag"])

    if not huidig:
        print("! Geen enkele woning gevonden en de site zegt niet dat het filter "
              "leeg is. Opslag blijft ongewijzigd.", file=sys.stderr)
        return [], None

    # Controle tegen het aantal dat de site zelf noemt.
    for html in paginas:
        treffer = AANTAL_RE.search(html)
        if treffer:
            verwacht = int(treffer.group(1).replace(".", ""))
            if len(huidig) < verwacht:
                print(
                    f"! Let op: site meldt {verwacht} objecten, ik zie er {len(huidig)}. "
                    "Waarschijnlijk staat er een tweede pagina.",
                    file=sys.stderr,
                )
            break

    beschikbaar = sum(1 for w in huidig.values() if w["status"] == "beschikbaar")
    print(f"     {len(huidig)} woning(en), waarvan {beschikbaar} beschikbaar", flush=True)

    opslag = lees_opslag(site["opslag"])
    eerste_keer = not opslag
    nu = datetime.now(timezone.utc).isoformat(timespec="seconds")

    nieuw: list[dict] = []
    for woning_id, gegevens in huidig.items():
        vorige = opslag.get(woning_id)
        vorige_status = vorige.get("status", "beschikbaar") if vorige else None
        if gegevens["status"] == "beschikbaar" and vorige_status != "beschikbaar":
            nieuw.append({**gegevens, "site": site["naam"]})

    for woning_id, gegevens in huidig.items():
        regel = opslag.setdefault(woning_id, {"eerst_gezien": nu})
        regel.update(gegevens)

    if eerste_keer and os.environ.get("NOTIFY_ON_FIRST_RUN") != "1":
        print("     eerste run: vastgelegd als uitgangspunt, geen mail", flush=True)
        return [], opslag

    if not nieuw:
        print("     geen nieuw aanbod", flush=True)
        return [], opslag

    print(f"     {len(nieuw)} nieuw(e) woning(en):", flush=True)
    for woning in nieuw:
        print(f"       - {woning['title']} | {woning['url']}", flush=True)
    return nieuw, opslag


# --------------------------------------------------------------------------
# E-mail
# --------------------------------------------------------------------------

def verstuur_mail(nieuw: list[dict]) -> None:
    host = os.environ["SMTP_HOST"]
    poort = int(os.environ.get("SMTP_PORT", "587"))
    gebruiker = os.environ["SMTP_USER"]
    wachtwoord = os.environ["SMTP_PASS"]
    ontvangers = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]
    afzender = os.environ.get("MAIL_FROM", gebruiker)

    per_site: dict[str, list[dict]] = {}
    for woning in nieuw:
        per_site.setdefault(woning["site"], []).append(woning)

    aantal = len(nieuw)
    if aantal == 1:
        onderwerp = f"Nieuwe huurwoning: {nieuw[0]['title']} ({nieuw[0]['site']})"
    else:
        onderwerp = f"{aantal} nieuwe huurwoningen"

    # Zodat je ziet of deze mail van je laptop of van GitHub komt. Op GitHub
    # wordt GITHUB_ACTIONS altijd gezet, dus dat hoef je nergens in te vullen.
    label = os.environ.get("MONITOR_LABEL")
    if label is None:
        label = "github" if os.environ.get("GITHUB_ACTIONS") == "true" else ""
    if label.strip():
        onderwerp = f"[{label.strip()}] {onderwerp}"

    regels: list[str] = []
    blokken: list[str] = []
    for sitenaam, woningen in per_site.items():
        regels += [f"{sitenaam}:", ""]
        items = []
        for woning in woningen:
            regels += [f"  * {woning['title']}", f"    {woning['url']}", ""]
            items.append(
                f'<li style="margin-bottom:12px">'
                f'<a href="{woning["url"]}" style="font-size:16px;font-weight:600">{woning["title"]}</a>'
                f"</li>"
            )
        blokken.append(
            f'<h3 style="margin:22px 0 8px;font-size:14px;text-transform:uppercase;'
            f'letter-spacing:.05em;color:#666">{sitenaam}</h3>'
            f'<ul style="padding-left:18px;margin:0">{"".join(items)}</ul>'
        )

    bericht = EmailMessage()
    bericht["Subject"] = onderwerp
    bericht["From"] = afzender
    bericht["To"] = ", ".join(ontvangers)
    bericht.set_content("\n".join(regels))
    bericht.add_alternative(
        '<html><body style="font-family:Helvetica,Arial,sans-serif;color:#222">'
        f"<p>Nieuw aanbod gevonden ({aantal}):</p>{''.join(blokken)}"
        "</body></html>",
        subtype="html",
    )

    if poort == 465:
        with smtplib.SMTP_SSL(host, poort, context=ssl.create_default_context()) as server:
            server.login(gebruiker, wachtwoord)
            server.send_message(bericht)
    else:
        with smtplib.SMTP(host, poort, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(gebruiker, wachtwoord)
            server.send_message(bericht)

    print(f"\nE-mail verstuurd naar {', '.join(ontvangers)}", flush=True)


# --------------------------------------------------------------------------

def main() -> int:
    laad_instellingen()
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}] Controle gestart", flush=True)

    resultaten: list[tuple[dict, list[dict], dict]] = []
    mislukt: list[str] = []

    for site in SITES:
        try:
            nieuw, opslag = controleer_site(site)
        except Exception as fout:  # een kapotte site mag de rest niet blokkeren
            print(f"! Onverwachte fout bij {site['naam']}: {fout}", file=sys.stderr)
            mislukt.append(site["naam"])
            continue
        if opslag is None:
            mislukt.append(site["naam"])
            continue
        resultaten.append((site, nieuw, opslag))

    alles_nieuw = [woning for _, nieuw, _ in resultaten for woning in nieuw]

    if not alles_nieuw:
        for site, _, opslag in resultaten:
            schrijf_opslag(site["opslag"], opslag)
        print("\nNiets nieuws om te melden.", flush=True)
        return 1 if mislukt else 0

    try:
        verstuur_mail(alles_nieuw)
    except KeyError as ontbreekt:
        print(f"! Ontbrekende instelling: {ontbreekt}. Opslag niet bijgewerkt.", file=sys.stderr)
        return 1
    except Exception as fout:
        print(f"! Verzenden mislukt: {fout}. Opslag niet bijgewerkt.", file=sys.stderr)
        # Sites zonder nieuws mogen wel opslaan; die hebben geen mail nodig.
        for site, nieuw, opslag in resultaten:
            if not nieuw:
                schrijf_opslag(site["opslag"], opslag)
        return 1

    for site, _, opslag in resultaten:
        schrijf_opslag(site["opslag"], opslag)
    return 1 if mislukt else 0


if __name__ == "__main__":
    sys.exit(main())
