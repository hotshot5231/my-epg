from copy import deepcopy
from datetime import datetime, timedelta, timezone
import difflib
import gzip
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET


M3U_URL = "https://iptv-org.github.io/iptv/countries/us.m3u"
OUTPUT_DIR = Path("public")
OUTPUT_FILE = OUTPUT_DIR / "test_guide.xml"

# Refuse to replace the published guide unless at least this many
# playlist channels have valid current or upcoming listings.
MIN_CHANNELS = 150

SCRAPE_SITES = ["tvpassport.com"]
SCRAPE_DAYS = 2

# Existing sources retain their existing priority.
FEEDS = {
    "pluto": "https://i.mjh.nz/PlutoTV/us.xml",
    "plex": "https://i.mjh.nz/Plex/us.xml",
    "samsung": "https://i.mjh.nz/SamsungTVPlus/us.xml",
    "roku": "https://i.mjh.nz/Roku/all.xml",
    "epgpw": "https://epg.pw/xmltv/epg_US.xml",
}

# Only used AFTER existing feeds and scraper results have been checked.
# Earlier fallback feeds get first opportunity to recover a channel.
FALLBACK_FEEDS = {
    "epgtalk_us": (
        "https://raw.githubusercontent.com/acidjesuz/EPGTalk/"
        "master/US_guide.xml.gz"
    ),
    "epgtalk_local": (
        "https://raw.githubusercontent.com/acidjesuz/EPGTalk/"
        "master/US_local_guide.xml.gz"
    ),
    "epgtalk_free": (
        "https://raw.githubusercontent.com/acidjesuz/EPGTalk/"
        "master/FreeTV_guide.xml.gz"
    ),
}

# Trailing provider labels used in EPGTalk display names.
# Value = provider id (used to honor a provider hint), or None for
# labels that are just noise (e.g. "(FAST)").
PROVIDER_LABELS = {
    "plex": "plex",
    "samsung tv plus": "samsung",
    "pluto tv": "pluto",
    "pluto": "pluto",
    "roku channel": "roku",
    "the roku channel": "roku",
    "roku": "roku",
    "tubi": "tubi",
    "fast": None,
}

GUIDES_URL = "https://iptv-org.github.io/api/guides.json"

MJH_PREFIX = {
    "PlutoTV/us#": "pluto",
    "Plex/us#": "plex",
    "SamsungTVPlus/us#": "samsung",
    "Roku/all#": "roku",
}

# Manual overrides:
# playlist tvg-id -> (feed, source channel id[, shift_hours])
#
# shift_hours moves listings later, for example an East feed used
# for a West Coast copy.
_ESPN = ("epgpw", "465198")
_ABC_E = ("epgpw", "464902")
_NBC_E = ("epgpw", "465059")
_NBC_W = ("epgpw", "465252")
_PBS = ("epgpw", "465245")
_PBSK_E = ("epgpw", "464925")
_PBSK_W = ("epgpw", "565870")
_TEL_E = ("epgpw", "464962")
_TEL_W = ("epgpw", "465086")
_UNI_W = ("epgpw", "465341")
_UNIMAS_C = ("epgpw", "464928")
_UNIMAS_W = ("epgpw", "464918")
_FOX_E = ("epgpw", "549207")

OVERRIDES = {
    "ESPN.us@HD": _ESPN,
    "ESPN.us@SD": _ESPN,
    "HallmarkMoviesMore.us@SD": (
        "plex",
        "6a1610bebdf296985fd95603-6244cf3c742b0d58cd4f2514",
    ),

    # ABC / CBS / NBC
    "ABC.us@WFTV": _ABC_E,
    "ABC.us@WSOCTV": _ABC_E,
    "CBS.us@KIROTV": ("epgpw", "464830"),
    "NBC.us@East": _NBC_E,
    "NBC.us@WGAL": _NBC_E,
    "NBC.us@WJACTV": _NBC_E,
    "NBC.us@WNYT": _NBC_E,
    "NBC.us@West": _NBC_W,

    # Fox
    "Fox.us@WTXFTV": _FOX_E,
    "Fox.us@KCPQ": _FOX_E + (3,),
    "Fox.us@KMPHTV": _FOX_E + (3,),
    "Fox.us@West": _FOX_E + (3,),
    "FoxSports1.us@HD": ("epgpw", "465291"),
    "FoxSports2.us@HD": ("epgpw", "465355"),

    # PBS
    "PBS.us@East": _PBS,
    "PBS.us@WEDQ": _PBS,
    "PBSKidsEasternCentral.us@SD": _PBSK_E,
    "PBSKidsAlaska.us@SD": _PBSK_W,
    "PBSKidsHawaii.us@SD": _PBSK_W,
    "PBSKidsMountain.us@SD": _PBSK_W,
    "PBSKidsPacific.us@SD": _PBSK_W,

    # Spanish-language networks
    "Telemundo.us@East": _TEL_E,
    "Telemundo.us@EastHD": _TEL_E,
    "TelemundoCorpusChristi.us@SD": _TEL_E,
    "Telemundo.us@West": _TEL_W,
    "Univision.us@West": _UNI_W,
    "UniMas.us@Central": _UNIMAS_C,
    "UniMas.us@KUVNDT": _UNIMAS_C,
    "UniMas.us@Mountain": _UNIMAS_W,

    # Starz correction
    "StarzEncoreSpanish.us@East": ("epgpw", "465119"),
}

# ABC News Live 1-10 all share one guide.
for _n in range(1, 11):
    OVERRIDES[f"ABCNewsLive{_n}.us@SD"] = ("epgpw", "465150")

OVERRIDES.update({
    "MTV.us@East": ("epgpw", "465080"),
    "MTV2.us@East": ("epgpw", "465113"),
    "CNBC.us@SD": ("epgpw", "464791"),
    "EWTN.us@English": ("epgpw", "465352"),
    "E.us@East": ("epgpw", "464832"),
    "TCM.us@East": ("epgpw", "465196"),
    "HGTV.us@East": ("epgpw", "465028"),
    "GameShowNetwork.us@East": ("epgpw", "464810"),
    "InvestigationDiscovery.us@East": ("epgpw", "465294"),
    "DiscoveryChannel.us@East": ("epgpw", "465364"),
    "LifetimeMovies.us@East": ("epgpw", "464929"),
    "CartoonNetwork.us@East": ("epgpw", "465267"),
    "OWN.us@East": ("epgpw", "465259"),
    "StarzKidsFamily.us@East": ("epgpw", "465109"),
    "TNT.us@East": ("epgpw", "465114"),
    "TNT.us@West": ("epgpw", "465307"),
    "TBS.us@East": ("epgpw", "465285"),
    "TBS.us@West": ("epgpw", "465131"),
    "truTV.us@West": ("epgpw", "464988"),
    "Galavision.us@West": ("epgpw", "465312"),
    "News12NewJersey.us@SD": ("epgpw", "524797"),
    "News12Bronx.us@SD": ("epgpw", "578055"),
    "News12HudsonValley.us@SD": ("epgpw", "578056"),
    "News12Westchester.us@SD": ("epgpw", "578058"),
    "News12Conneticut.us@SD": ("epgpw", "578059"),
    "News12Brooklyn.us@SD": ("epgpw", "578063"),
    "News12LongIsland.us@SD": ("epgpw", "578068"),
    "WPLG101.us@HD": ("epgpw", "466285"),
    "KNXVTV611.us@HD": ("epgpw", "468352"),
    "KIIOLD101.us@HD": ("epgpw", "466398"),
    "DaystarTV.us@SD": ("epgpw", "464906"),
    "TVEInternacionalAmerica.es@SD": ("epgpw", "464986"),
    "AWE.us@SD": ("epgpw", "465071"),
    "Reelz.us@SD": ("epgpw", "465018"),
    "HopeChannelNorthAmerica.us@SD": ("epgpw", "464958"),
    "WorldHarvestTV.us@SD": ("epgpw", "465179"),
    "JewishLifeTelevision.us@SD": ("epgpw", "464860"),
    "CatholicFaithNetwork.us@SD": ("epgpw", "464982"),
    "SonyMovies.us@SD": ("epgpw", "464943"),
    "CineSony.us@SD": ("epgpw", "464793"),
    "MLBStrikeZone.us@SD": ("epgpw", "464844"),
    "VSiN.us@SD": ("epgpw", "465295"),
    "3ABNEnglish.us@SD": ("epgpw", "465145"),
    "Willow.us@SD": ("epgpw", "465010"),
})

_PLEX = "6a1610bebdf296985fd95603-"


def _name_key(text):
    text = re.sub(r"[\(\[].*?[\)\]]", "", text)
    return re.sub(r"[^a-z0-9]", "", text.lower())


NAME_OVERRIDES = {
    _name_key(name): target
    for name, target in {
        "Crime + Investigation": ("epgpw", "465040"),
        "Curiosity NOW EN": ("plex", _PLEX + "64831c8be3541c909e96d240"),
        "Dare To Dream Network": ("epgpw", "465061"),
        "Daystar TV Espanol": ("epgpw", "464748"),
        "Galavision East HD": ("epgpw", "465335"),
        "Galavision West HD": ("epgpw", "465312"),
        "Justice Central.TV": ("epgpw", "465216"),
        "Starz Encore Spanish East": ("epgpw", "465119"),
        "The Young Turks": ("epgpw", "465298"),
        "Wired2FishTV": ("plex", _PLEX + "608a2c722957c9002c6a617b"),
        "WLMB": ("epgpw", "469204"),
    }.items()
}


def download(url, path):
    """Download XML or gzip-compressed XML into a plain XML file."""
    path = Path(path)
    raw_path = path.with_suffix(path.suffix + ".download")
    expanded_path = path.with_suffix(path.suffix + ".expanded")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 EPG builder"},
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            with open(raw_path, "wb") as out:
                shutil.copyfileobj(response, out, length=1024 * 1024)

        with open(raw_path, "rb") as source:
            compressed = source.read(2) == b"\x1f\x8b"

        if compressed:
            with gzip.open(raw_path, "rb") as source:
                with open(expanded_path, "wb") as out:
                    shutil.copyfileobj(source, out, length=1024 * 1024)
            expanded_path.replace(path)
        else:
            raw_path.replace(path)
    finally:
        raw_path.unlink(missing_ok=True)
        expanded_path.unlink(missing_ok=True)


def normalize(name):
    name = name.lower()
    name = re.sub(r"\(.*?\)|\[.*?\]", " ", name)
    name = re.sub(r"\bhd\b", " ", name)
    return re.sub(r"[^a-z0-9]", "", name)


def normalize_loose(name):
    """Existing loose matching for the original sources."""
    name = "".join(
        c for c in unicodedata.normalize("NFKD", name)
        if not unicodedata.combining(c)
    ).lower()
    name = re.sub(r"\(.*?\)|\[.*?\]", " ", name).replace("&", " and ")
    name = name.strip()

    noise = r"(hd|sd|us|usa|tv|east|west|network|channel)"
    while True:
        new = re.sub(r"\s+" + noise + r"$", "", name)
        if new == name:
            break
        name = new

    return re.sub(r"[^a-z0-9]", "", name)


def split_provider(display):
    """Strip trailing provider labels like '(Plex)' or '(Samsung TV Plus)'.

    Returns (name_without_provider_labels, provider_or_None).
    Regional/language labels are left alone.
    """
    text = (display or "").strip()
    provider = None

    while True:
        match = re.search(r"\s*\(([^()]*)\)\s*$", text)
        if not match:
            break
        label = match.group(1).strip().lower()
        if label not in PROVIDER_LABELS:
            break
        if provider is None:
            provider = PROVIDER_LABELS[label]
        text = text[:match.start()]

    return text.strip(), provider


def fallback_name_key(name):
    """Conservative fallback key: provider labels, resolution markers and
    bracketed tags are removed; regions and language words are retained."""
    name, _ = split_provider(name)

    name = "".join(
        c for c in unicodedata.normalize("NFKD", name)
        if not unicodedata.combining(c)
    ).lower()

    # Resolution markers like (720p) / (HD)
    name = re.sub(
        r"[\(\[]\s*(?:\d{3,4}[pi]|hd|sd|fhd|uhd|4k|8k)\s*[\)\]]",
        " ",
        name,
    )
    # Bracketed status tags like [Not 24/7] / [Geo-blocked]
    name = re.sub(r"\[[^\]]*\]", " ", name)
    name = re.sub(r"\b(?:hd|sd|fhd|uhd|4k|8k)\b", " ", name)
    name = name.replace("&", " and ").replace("+", " plus ")
    return re.sub(r"[^a-z0-9]", "", name)


def stream_provider(url):
    """Provider hint taken from the playlist stream URL, if any."""
    text = (url or "").lower()
    if "plu-" in text or "pluto" in text:
        return "pluto"
    if "plex" in text:
        return "plex"
    if "samsung" in text:
        return "samsung"
    if "roku" in text:
        return "roku"
    return None


NETWORKS = (
    r"(?:ABC|NBC|CBS|FOX|Fox|PBS|CW|The CW|"
    r"MyNetworkTV|Telemundo|Univision|UniMas|Ion|ION)"
)


def call_sign(name):
    match = re.match(
        r"^" + NETWORKS
        + r"(?:\s+\(United States\))?\s+"
        + r"([KW][A-Z]{2,3})(?:-(?:TV|DT|LD|CD))?\b",
        name,
    )
    return match.group(1) if match else None


def read_guides_map():
    try:
        request = urllib.request.Request(
            GUIDES_URL,
            headers={"User-Agent": "Mozilla/5.0 EPG builder"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.load(response)
    except Exception as error:
        print(f"guides.json unavailable: {error}", flush=True)
        return {}

    result = {}
    for item in data:
        if item.get("site") != "i.mjh.nz":
            continue
        for prefix, feed in MJH_PREFIX.items():
            if item.get("site_id", "").startswith(prefix):
                result.setdefault(item["channel"], []).append(
                    (feed, item["site_id"][len(prefix):])
                )
    return result


def read_playlist():
    request = urllib.request.Request(
        M3U_URL,
        headers={"User-Agent": "Mozilla/5.0 EPG builder"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8", "replace")

    pattern = re.compile(
        r'#EXTINF:[^\n]*tvg-id="([^"]+)"[^\n]*,([^\r\n]*)'
        r'\r?\n(?:#[^\n]*\n)*(https?://[^\r\n]+)'
    )
    return pattern.findall(text)


def read_channel_records(path):
    """Read channel IDs and every display-name, validating the whole XML."""
    records = {}
    root = None
    stack = []

    for event, element in ET.iterparse(path, events=("start", "end")):
        if event == "start":
            if root is None:
                root = element
                if root.tag != "tv":
                    raise ValueError("Downloaded document is not XMLTV")
            stack.append(element)
            continue

        parent = stack[-2] if len(stack) > 1 else None
        if parent is root:
            if element.tag == "channel":
                source_id = element.get("id")
                if source_id:
                    displays = [
                        node.text.strip()
                        for node in element.findall("display-name")
                        if node.text and node.text.strip()
                    ]
                    records[source_id] = displays
            root.remove(element)
        stack.pop()

    return records


def read_channels(path):
    return {
        source_id: displays[0] if displays else ""
        for source_id, displays in read_channel_records(path).items()
    }


def read_programmes(path, wanted_ids):
    """Return programme elements for the requested source IDs."""
    found = {}
    root = None
    stack = []

    for event, element in ET.iterparse(path, events=("start", "end")):
        if event == "start":
            if root is None:
                root = element
            stack.append(element)
            continue

        parent = stack[-2] if len(stack) > 1 else None
        if parent is root:
            if (
                element.tag == "programme"
                and element.get("channel") in wanted_ids
            ):
                found.setdefault(
                    element.get("channel"), []
                ).append(deepcopy(element))
            root.remove(element)
        stack.pop()

    return found


def parse_time(value):
    try:
        return datetime.strptime(value.strip(), "%Y%m%d%H%M%S %z")
    except (ValueError, TypeError, AttributeError):
        return None


def usable_programmes(items, now, shift=0):
    """Require valid times, a title, and an unexpired shifted stop time."""
    offset = timedelta(hours=shift)
    result = []

    for item in items:
        start = parse_time(item.get("start", ""))
        stop = parse_time(item.get("stop", ""))
        title = item.findtext("title") or ""

        if (
            start is not None
            and stop is not None
            and stop > start
            and stop + offset > now
            and title.strip()
        ):
            result.append(item)

    return result


def covered_ids(mapping, programmes, now):
    return {
        tvg_id
        for tvg_id, (feed, source_id, display, shift) in mapping.items()
        if usable_programmes(
            programmes.get(feed, {}).get(source_id, []),
            now,
            shift,
        )
    }


def schedule_signature(items, limit=10):
    """Compact fingerprint of a schedule, for comparing candidates."""
    ordered = sorted(items, key=lambda p: p.get("start", ""))[:limit]
    return tuple(
        (p.get("start", ""), (p.findtext("title") or "").strip().lower())
        for p in ordered
    )


def run_scraper(unmatched_ids, tmp):
    base = Path("epg-scraper").resolve()
    results = {}
    remaining = set(unmatched_ids)

    if not base.exists():
        print("epg-scraper not found, skipping scrape", flush=True)
        return results

    for site in SCRAPE_SITES:
        chan_file = base / "sites" / site / f"{site}.channels.xml"
        if not chan_file.exists() or not remaining:
            continue

        root = ET.Element("channels")
        ids = []

        for channel in ET.parse(chan_file).getroot().iter("channel"):
            xid = channel.get("xmltv_id")
            if xid in remaining and xid not in ids:
                root.append(deepcopy(channel))
                ids.append(xid)

        if not ids:
            continue

        cf = tmp / f"{site}.channels.xml"
        out = tmp / f"{site}.guide.xml"
        ET.ElementTree(root).write(
            cf, encoding="utf-8", xml_declaration=True
        )
        print(f"Scraping {len(ids)} channels from {site}...", flush=True)

        try:
            subprocess.run(
                [
                    "npm", "run", "grab", "--",
                    f"--channels={cf}",
                    f"--output={out}",
                    f"--days={SCRAPE_DAYS}",
                    "--maxConnections=10",
                    "--timeout=30000",
                ],
                cwd=base,
                timeout=75 * 60,
                check=False,
            )
        except Exception as error:
            print(f"  scrape failed for {site}: {error}", flush=True)

        if out.exists():
            try:
                got = {
                    p.get("channel")
                    for p in ET.parse(out).getroot().iter("programme")
                }
            except Exception as error:
                print(
                    f"  could not read {site} output: {error}",
                    flush=True,
                )
                got = set()

            print(
                f"  {site}: {len(got)} channels returned listings",
                flush=True,
            )
            if got:
                results[site] = (out, [i for i in ids if i in got])
                remaining -= got

    return results


def recover_from_fallbacks(
    pl_names, pl_urls, mapping, programmes, names, tmp, now
):
    """Fill only coverage gaps; never replace a working existing match."""
    baseline = covered_ids(mapping, programmes, now)
    remaining = set(pl_names) - baseline
    reports = {}

    for feed, url in FALLBACK_FEEDS.items():
        report = {
            "status": "not needed",
            "name_candidates": 0,
            "ambiguous": [],
            "no_listings": [],
            "recovered": [],
        }
        reports[feed] = report

        if not remaining:
            continue

        print(f"Downloading fallback {feed}: {url}", flush=True)
        path = tmp / f"{feed}.xml"

        try:
            download(url, path)
            records = read_channel_records(path)

            display_names = {
                source_id: displays[0] if displays else ""
                for source_id, displays in records.items()
            }

            # normalized name -> ALL matching source ids, plus the
            # provider each source id belongs to (from its label).
            name_index = {}
            providers = {}
            for source_id, displays in records.items():
                for display in displays:
                    key = fallback_name_key(display)
                    if key:
                        name_index.setdefault(key, set()).add(source_id)
                    _, provider = split_provider(display)
                    if provider and source_id not in providers:
                        providers[source_id] = provider

            candidates = {}
            for tvg_id in sorted(remaining):
                if tvg_id in records:
                    candidates[tvg_id] = [tvg_id]
                    continue

                key = fallback_name_key(pl_names[tvg_id])
                source_ids = name_index.get(key, set())
                if source_ids:
                    candidates[tvg_id] = sorted(source_ids)

            report["name_candidates"] = len(candidates)

            wanted = {
                source_id
                for source_ids in candidates.values()
                for source_id in source_ids
            }
            source_programmes = (
                read_programmes(path, wanted) if wanted else {}
            )
            valid_programmes = {
                source_id: usable_programmes(items, now)
                for source_id, items in source_programmes.items()
            }

            recovered = {}
            for tvg_id, source_ids in candidates.items():
                valid = [s for s in source_ids if valid_programmes.get(s)]

                if not valid:
                    report["no_listings"].append(tvg_id)
                    continue

                chosen = None
                if len(valid) == 1:
                    chosen = valid[0]
                else:
                    # 1) honor a provider hint from the stream URL
                    hint = stream_provider(pl_urls.get(tvg_id, ""))
                    if hint:
                        hinted = [
                            s for s in valid if providers.get(s) == hint
                        ]
                        if len(hinted) == 1:
                            chosen = hinted[0]

                    # 2) otherwise accept only if all schedules agree
                    if chosen is None:
                        signatures = {
                            schedule_signature(valid_programmes[s])
                            for s in valid
                        }
                        if len(signatures) == 1:
                            chosen = valid[0]

                if chosen is None:
                    report["ambiguous"].append((tvg_id, valid))
                    continue

                recovered[tvg_id] = (
                    feed,
                    chosen,
                    pl_names[tvg_id].strip(),
                    0,
                )

            # Commit results only after everything succeeded.
            names[feed] = display_names
            programmes[feed] = valid_programmes
            mapping.update(recovered)
            remaining.difference_update(recovered)

            report["status"] = "ok"
            report["recovered"] = sorted(recovered)

            print(
                f"  {len(records)} guide channels; "
                f"{len(candidates)} name candidates; "
                f"{len(report['ambiguous'])} ambiguous; "
                f"{len(report['no_listings'])} without valid listings; "
                f"{len(recovered)} newly covered",
                flush=True,
            )

        except Exception as error:
            report["status"] = f"SKIPPED: {error}"
            print(f"  SKIPPED {feed}: {error}", flush=True)

    print("\n=== FALLBACK RECOVERY SUMMARY START ===", flush=True)
    print(
        f"Coverage before EPGTalk: {len(baseline)}; "
        f"gaps before EPGTalk: {len(pl_names) - len(baseline)}",
        flush=True,
    )

    total = 0
    for feed, report in reports.items():
        count = len(report["recovered"])
        total += count
        print(
            f"{feed}: status={report['status']}; "
            f"name_candidates={report['name_candidates']}; "
            f"ambiguous={len(report['ambiguous'])}; "
            f"no_valid_listings={len(report['no_listings'])}; "
            f"recovered={count}",
            flush=True,
        )
        for tvg_id in report["recovered"]:
            source_id = mapping[tvg_id][1]
            print(
                f"  + {tvg_id} | {pl_names[tvg_id].strip()!r} | "
                f"source={feed}:{source_id}",
                flush=True,
            )
        for tvg_id, source_ids in report["ambiguous"]:
            print(
                f"  ? AMBIGUOUS {tvg_id} | "
                f"{pl_names[tvg_id].strip()!r} | "
                f"candidates={', '.join(source_ids)}",
                flush=True,
            )
        for tvg_id in report["no_listings"]:
            print(
                f"  - NO LISTINGS {tvg_id} | "
                f"{pl_names[tvg_id].strip()!r}",
                flush=True,
            )

    print(f"Total additional coverage from EPGTalk: {total}", flush=True)
    print(f"Remaining gaps: {len(remaining)}", flush=True)
    print("=== FALLBACK RECOVERY SUMMARY END ===\n", flush=True)


def print_closest_names(unmapped_ids, pl_names, names):
    """Diagnostics only; fuzzy suggestions are never auto-applied."""
    print("\n=== CLOSEST GUIDE NAMES START ===", flush=True)
    keyed = {}

    for feed, channels in names.items():
        for source_id, display in channels.items():
            key = normalize_loose(display)
            if len(key) >= 3:
                keyed.setdefault(key, []).append(
                    (feed, source_id, display)
                )

    keys = list(keyed)
    shown = 0

    for tvg_id in sorted(unmapped_ids):
        name = pl_names[tvg_id].strip()
        target = normalize_loose(name)
        if len(target) < 3:
            continue

        close = difflib.get_close_matches(
            target, keys, n=3, cutoff=0.75
        )
        if not close:
            continue

        shown += 1
        print(f"  {tvg_id} | {name!r}", flush=True)
        for key in close:
            for feed, source_id, display in keyed[key][:2]:
                print(
                    f"    -> {feed}:{source_id} | {display!r}",
                    flush=True,
                )

    print(
        f"({shown} unmatched channels have close names)",
        flush=True,
    )
    print("=== CLOSEST GUIDE NAMES END ===\n", flush=True)


def build(tmp):
    playlist = read_playlist()
    print(f"Playlist channels: {len(playlist)}", flush=True)
    pl_names = {}
    pl_urls = {}
    for tvg_id, name, url in playlist:
        pl_names.setdefault(tvg_id, name)
        pl_urls.setdefault(tvg_id, url)

    paths = {}
    names = {}

    for key, url in FEEDS.items():
        print(f"Downloading {key}: {url}", flush=True)
        try:
            path = tmp / f"{key}.xml"
            download(url, path)
            channels = read_channels(path)
            paths[key] = path
            names[key] = channels
            print(f"  {len(channels)} guide channels", flush=True)
        except Exception as error:
            print(f"  SKIPPED {key}: {error}", flush=True)

    # Original-source matching retains its original priority.
    name_index = {}
    loose_index = {}

    for key in FEEDS:
        for source_id, display in names.get(key, {}).items():
            exact = normalize(display)
            loose = normalize_loose(display)
            if exact:
                name_index.setdefault(exact, (key, source_id))
            if loose:
                loose_index.setdefault(loose, (key, source_id))

    call_index = {}
    for source_id, display in names.get("epgpw", {}).items():
        match = re.match(
            r"^([KW][A-Z]{2,3})-(?:DT|CD|LD)$",
            display.strip(),
        )
        if match:
            call_index.setdefault(
                match.group(1), []
            ).append(source_id)

    guides_map = read_guides_map()
    mapping = {}

    for tvg_id, name, url in playlist:
        if tvg_id in mapping:
            continue

        match = OVERRIDES.get(tvg_id)
        if not match:
            match = NAME_OVERRIDES.get(_name_key(name))

        if not match:
            pluto = re.search(r"plu-([0-9a-f]{24})", url)
            if pluto and pluto.group(1) in names.get("pluto", {}):
                match = ("pluto", pluto.group(1))

        if not match:
            match = name_index.get(normalize(name))
        if not match:
            match = loose_index.get(normalize_loose(name))

        if not match and name.lower().startswith("pluto tv "):
            rest = name[9:]
            match = (
                name_index.get(normalize(rest))
                or loose_index.get(normalize_loose(rest))
            )

        if not match and "epgpw" in paths:
            sign = call_sign(name)
            if sign and len(call_index.get(sign, [])) == 1:
                match = ("epgpw", call_index[sign][0])

        if not match:
            for feed, source_id in guides_map.get(
                tvg_id.split("@")[0], []
            ):
                if source_id in names.get(feed, {}):
                    match = (feed, source_id)
                    break

        if match and match[0] in paths:
            shift = match[2] if len(match) > 2 else 0
            mapping[tvg_id] = (
                match[0], match[1], name.strip(), shift
            )

    # Preserve the existing scraper priority.
    try:
        scraped = run_scraper(set(pl_names) - set(mapping), tmp)
    except Exception as error:
        print(
            f"Scrape step failed, continuing without it: {error}",
            flush=True,
        )
        scraped = {}

    for site, (out, ids) in scraped.items():
        key = f"scrape_{site}"
        paths[key] = out
        for tvg_id in ids:
            mapping[tvg_id] = (
                key, tvg_id, pl_names[tvg_id].strip(), 0
            )

    programmes = {}
    for key, path in paths.items():
        wanted = {
            match[1]
            for match in mapping.values()
            if match[0] == key
        }
        if not wanted:
            continue
        try:
            programmes[key] = read_programmes(path, wanted)
        except Exception as error:
            print(
                f"  Could not read programmes from {key}: {error}",
                flush=True,
            )
            programmes[key] = {}

    now = datetime.now(timezone.utc)

    recover_from_fallbacks(
        pl_names, pl_urls, mapping, programmes, names, tmp, now
    )

    tv = ET.Element(
        "tv", {"generator-info-name": "Custom mapped EPG"}
    )
    all_programmes = []
    kept = 0

    for tvg_id, (feed, source_id, display, shift) in mapping.items():
        items = programmes.get(feed, {}).get(source_id, [])
        live = usable_programmes(items, now, shift)
        if not live:
            continue

        channel = ET.SubElement(tv, "channel", {"id": tvg_id})
        ET.SubElement(channel, "display-name").text = display

        for programme in live:
            copy = deepcopy(programme)
            copy.set("channel", tvg_id)

            if shift:
                for attr in ("start", "stop"):
                    moved = parse_time(copy.get(attr, ""))
                    copy.set(
                        attr,
                        (moved + timedelta(hours=shift)).strftime(
                            "%Y%m%d%H%M%S %z"
                        ),
                    )

            all_programmes.append(copy)

        kept += 1

    published_ids = {
        channel.get("id") for channel in tv.findall("channel")
    }
    missing_ids = set(pl_names) - published_ids
    unmapped_ids = missing_ids - set(mapping)
    empty_ids = missing_ids & set(mapping)

    print("\n=== MISSING GUIDE COVERAGE START ===", flush=True)
    print(
        f"Unique playlist IDs: {len(pl_names)}; "
        f"with listings: {len(published_ids)}; "
        f"missing listings: {len(missing_ids)}",
        flush=True,
    )

    print(f"\nNO SOURCE MATCH ({len(unmapped_ids)}):", flush=True)
    for tvg_id in sorted(unmapped_ids):
        print(
            f"  {tvg_id} | {pl_names[tvg_id].strip()!r}",
            flush=True,
        )

    print(
        f"\nMATCHED BUT NO CURRENT LISTINGS ({len(empty_ids)}):",
        flush=True,
    )
    for tvg_id in sorted(empty_ids):
        feed, source_id, display, shift = mapping[tvg_id]
        source_name = names.get(feed, {}).get(source_id, "")
        print(
            f"  {tvg_id} | {display!r} | "
            f"source={feed}:{source_id} | "
            f"source_name={source_name!r} | "
            f"shift_hours={shift}",
            flush=True,
        )

    print("=== MISSING GUIDE COVERAGE END ===\n", flush=True)

    try:
        print_closest_names(unmapped_ids, pl_names, names)
    except Exception as error:
        print(f"Closest-name debug skipped: {error}", flush=True)

    if kept < MIN_CHANNELS:
        raise RuntimeError(
            f"Only {kept} channels have current listings "
            f"(minimum {MIN_CHANNELS}). Not publishing."
        )

    all_programmes.sort(
        key=lambda programme: (
            programme.get("start", ""),
            programme.get("channel", ""),
        )
    )
    tv.extend(all_programmes)

    # Write and validate a staging file before replacing the guide.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    staging = OUTPUT_FILE.with_suffix(".xml.tmp")

    try:
        ET.ElementTree(tv).write(
            staging,
            encoding="utf-8",
            xml_declaration=True,
        )
        staged_channels = read_channels(staging)
        if len(staged_channels) != kept:
            raise RuntimeError(
                "Output validation failed: channel count mismatch. "
                "Not publishing."
            )
        staging.replace(OUTPUT_FILE)
    finally:
        staging.unlink(missing_ok=True)

    size_mb = OUTPUT_FILE.stat().st_size / 1e6
    print(
        f"Matched {len(mapping)} of {len(pl_names)} unique playlist IDs; "
        f"{kept} have current listings; "
        f"{len(all_programmes)} programmes; {size_mb:.1f} MB.",
        flush=True,
    )


def main():
    with tempfile.TemporaryDirectory(prefix="epg-build-") as directory:
        build(Path(directory))


if __name__ == "__main__":
    main()
