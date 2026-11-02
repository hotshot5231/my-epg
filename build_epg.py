from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET

M3U_URL = "https://iptv-org.github.io/iptv/countries/us.m3u"
OUTPUT_DIR = Path("public")
OUTPUT_FILE = OUTPUT_DIR / "test_guide.xml"
MIN_CHANNELS = 150  # refuse to publish if fewer channels than this match

SCRAPE_SITES = ["tvtv.us", "tvpassport.com"]  # tried in this order
SCRAPE_DAYS = 2

# Guide feeds, in priority order for name matching.
FEEDS = {
    "pluto": "https://i.mjh.nz/PlutoTV/us.xml",
    "plex": "https://i.mjh.nz/Plex/us.xml",
    "samsung": "https://i.mjh.nz/SamsungTVPlus/us.xml",
    "roku": "https://i.mjh.nz/Roku/all.xml",
    "epgpw": "https://epg.pw/xmltv/epg_US.xml",
}

GUIDES_URL = "https://iptv-org.github.io/api/guides.json"
# iptv-org guides.json lists which i.mjh.nz feed/id each tvg-id uses.
MJH_PREFIX = {
    "PlutoTV/us#": "pluto",
    "Plex/us#": "plex",
    "SamsungTVPlus/us#": "samsung",
    "Roku/all#": "roku",
}

# Manual overrides: playlist tvg-id -> (feed, source channel id[, shift_hours]).
# shift_hours moves the listings later (for West Coast copies of an East feed).
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
_FOX_E = ("epgpw", "549207")  # a real Fox affiliate (WCTI New Bern), Eastern time

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
    # Fox (East feed; West Coast copies shifted +3h)
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
}
# ABC News Live 1-10 all share one guide.
for _n in range(1, 11):
    OVERRIDES[f"ABCNewsLive{_n}.us@SD"] = ("epgpw", "465150")


def download(url, path):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 EPG builder"})
    with urllib.request.urlopen(request, timeout=120) as response, open(path, "wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def normalize(name):
    name = name.lower()
    name = re.sub(r"\(.*?\)", " ", name)
    name = re.sub(r"\bhd\b", " ", name)
    return re.sub(r"[^a-z0-9]", "", name)


def normalize_loose(name):
    """Looser match: fold accents, drop trailing noise words like HD/TV/Network."""
    name = "".join(
        c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)
    ).lower()
    name = re.sub(r"\(.*?\)", " ", name).replace("&", " and ")
    name = name.strip()
    noise = r"(hd|sd|us|usa|tv|east|west|network|channel)"
    while True:
        new = re.sub(r"\s+" + noise + r"$", "", name)
        if new == name:
            break
        name = new
    return re.sub(r"[^a-z0-9]", "", name)


NETWORKS = r"(?:ABC|NBC|CBS|FOX|Fox|PBS|CW|The CW|MyNetworkTV|Telemundo|Univision|UniMas|Ion|ION)"


def call_sign(name):
    """Local-station call sign (e.g. KGTV) from names like 'ABC KGTV (720p)'."""
    m = re.match(
        r"^" + NETWORKS + r"(?:\s+\(United States\))?\s+([KW][A-Z]{2,3})(?:-(?:TV|DT|LD|CD))?\b",
        name,
    )
    return m.group(1) if m else None


def read_guides_map():
    """tvg-id base (e.g. 'ESPN.us') -> list of (feed, source id) from iptv-org's guides.json."""
    try:
        request = urllib.request.Request(GUIDES_URL, headers={"User-Agent": "Mozilla/5.0 EPG builder"})
        data = json.load(urllib.request.urlopen(request, timeout=120))
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
    request = urllib.request.Request(M3U_URL, headers={"User-Agent": "Mozilla/5.0 EPG builder"})
    text = urllib.request.urlopen(request, timeout=60).read().decode("utf-8", "replace")
    pattern = re.compile(
        r'#EXTINF:[^\n]*tvg-id="([^"]+)"[^\n]*,([^\r\n]*)\r?\n(?:#[^\n]*\n)*(https?://[^\r\n]+)'
    )
    return pattern.findall(text)


def read_channels(path):
    channels = {}
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
            if element.tag == "channel":
                channels[element.get("id")] = element.findtext("display-name") or ""
            root.remove(element)
        stack.pop()
    return channels


def read_programmes(path, wanted_ids):
    """Return {source_id: [programme elements]} for the wanted source ids."""
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
            if element.tag == "programme" and element.get("channel") in wanted_ids:
                found.setdefault(element.get("channel"), []).append(deepcopy(element))
            root.remove(element)
        stack.pop()
    return found


def parse_time(value):
    try:
        return datetime.strptime(value.strip(), "%Y%m%d%H%M%S %z")
    except ValueError:
        return None


def run_scraper(unmatched_ids, tmp):
    """Scrape still-unmatched channels. Returns {site: (output_path, [tvg-ids])}."""
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
        for ch in ET.parse(chan_file).getroot().iter("channel"):
            xid = ch.get("xmltv_id")
            if xid in remaining and xid not in ids:
                root.append(deepcopy(ch))
                ids.append(xid)
        if not ids:
            continue
        cf = tmp / f"{site}.channels.xml"
        out = tmp / f"{site}.guide.xml"
        ET.ElementTree(root).write(cf, encoding="utf-8", xml_declaration=True)
        print(f"Scraping {len(ids)} channels from {site}...", flush=True)
        try:
            subprocess.run(
                ["npm", "run", "grab", "---", f"--channels={cf}", f"--output={out}",
                 f"--days={SCRAPE_DAYS}", "--maxConnections=10", "--timeout=30000"],
                cwd=base, timeout=75 * 60, check=False,
            )
        except Exception as error:
            print(f"  scrape failed for {site}: {error}", flush=True)
        if out.exists():
            results[site] = (out, ids)
            remaining -= set(ids)
    return results


def main():
    playlist = read_playlist()
    print(f"Playlist channels: {len(playlist)}", flush=True)

    tmp = Path(tempfile.mkdtemp())
    paths, names = {}, {}
    for key, url in FEEDS.items():
        print(f"Downloading {key}: {url}", flush=True)
        try:
            paths[key] = tmp / f"{key}.xml"
            download(url, paths[key])
            names[key] = read_channels(paths[key])
            print(f"  {len(names[key])} guide channels", flush=True)
        except Exception as error:
            print(f"  SKIPPED {key}: {error}", flush=True)
            paths.pop(key, None)

    name_index = {}
    for key in FEEDS:  # priority order
        for source_id, display in names.get(key, {}).items():
            name_index.setdefault(normalize(display), (key, source_id))

    loose_index = {}
    for key in FEEDS:
        for source_id, display in names.get(key, {}).items():
            loose_index.setdefault(normalize_loose(display), (key, source_id))

    # Local stations in epg.pw are listed by call sign, e.g. "KGTV-DT".
    call_index = {}
    for source_id, display in names.get("epgpw", {}).items():
        m = re.match(r"^([KW][A-Z]{2,3})-(?:DT|CD|LD)$", display.strip())
        if m:
            call_index.setdefault(m.group(1), []).append(source_id)

    guides_map = read_guides_map()

    # Decide a (feed, source id) for every playlist channel.
    mapping = {}  # tvg-id -> (feed, source id, display name, shift)
    for tvg_id, name, url in playlist:
        if tvg_id in mapping:
            continue
        match = OVERRIDES.get(tvg_id)
        if not match:
            pluto = re.search(r"plu-([0-9a-f]{24})", url)
            if pluto and pluto.group(1) in names.get("pluto", {}):
                match = ("pluto", pluto.group(1))
        if not match:
            match = name_index.get(normalize(name))
        if not match:
            match = loose_index.get(normalize_loose(name))
        if not match and "epgpw" in paths:
            sign = call_sign(name)
            if sign and len(call_index.get(sign, [])) == 1:
                match = ("epgpw", call_index[sign][0])
        if not match:
            for feed, source_id in guides_map.get(tvg_id.split("@")[0], []):
                if source_id in names.get(feed, {}):
                    match = (feed, source_id)
                    break
        if match and match[0] in paths:
            shift = match[2] if len(match) > 2 else 0
            mapping[tvg_id] = (match[0], match[1], name.strip(), shift)

    # Scrape per-station listings for whatever is still unmatched.
    pl_names = {t: n for t, n, _ in playlist}
    try:
        scraped = run_scraper(set(pl_names) - set(mapping), tmp)
    except Exception as error:
        print(f"Scrape step failed, continuing without it: {error}", flush=True)
        scraped = {}
    for site, (out, ids) in scraped.items():
        key = f"scrape_{site}"
        paths[key] = out
        for tvg_id in ids:
            mapping[tvg_id] = (key, tvg_id, pl_names[tvg_id].strip(), 0)

    # Pull programmes feed by feed.
    programmes = {}
    for key in paths:
        wanted = {m[1] for m in mapping.values() if m[0] == key}
        if wanted:
            programmes[key] = read_programmes(paths[key], wanted)

    now = datetime.now(timezone.utc)
    tv = ET.Element("tv", {"generator-info-name": "Custom mapped EPG"})
    all_programmes = []
    kept = 0
    for tvg_id, (feed, source_id, display, shift) in mapping.items():
        items = programmes.get(feed, {}).get(source_id, [])
        live = [
            p for p in items
            if (parse_time(p.get("stop", "")) or now) > now
        ]
        if not live:
            continue
        channel = ET.SubElement(tv, "channel", {"id": tvg_id})
        ET.SubElement(channel, "display-name").text = display
        for p in live:
            copy = deepcopy(p)
            copy.set("channel", tvg_id)
            if shift:
                for attr in ("start", "stop"):
                    moved = parse_time(copy.get(attr, ""))
                    if moved:
                        copy.set(attr, (moved + timedelta(hours=shift)).strftime("%Y%m%d%H%M%S %z"))
            all_programmes.append(copy)
        kept += 1

    if kept < MIN_CHANNELS:
        raise RuntimeError(
            f"Only {kept} channels have current listings (minimum {MIN_CHANNELS}). Not publishing."
        )

    all_programmes.sort(key=lambda p: (p.get("start", ""), p.get("channel", "")))
    tv.extend(all_programmes)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(tv).write(OUTPUT_FILE, encoding="utf-8", xml_declaration=True)
    size_mb = OUTPUT_FILE.stat().st_size / 1e6
    print(
        f"Matched {len(mapping)} of {len(playlist)} playlist channels; "
        f"{kept} have current listings; {len(all_programmes)} programmes; {size_mb:.1f} MB.",
        flush=True,
    )


if __name__ == "__main__":
    main()
