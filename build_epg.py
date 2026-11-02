from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

M3U_URL = "https://iptv-org.github.io/iptv/countries/us.m3u"
OUTPUT_DIR = Path("public")
OUTPUT_FILE = OUTPUT_DIR / "test_guide.xml"
MIN_CHANNELS = 150  # refuse to publish if fewer channels than this match

# Guide feeds, in priority order for name matching.
FEEDS = {
    "pluto": "https://i.mjh.nz/PlutoTV/us.xml",
    "plex": "https://i.mjh.nz/Plex/us.xml",
    "samsung": "https://i.mjh.nz/SamsungTVPlus/us.xml",
    "roku": "https://i.mjh.nz/Roku/all.xml",
    "epgpw": "https://epg.pw/xmltv/epg_US.xml",
}

# Manual overrides: playlist tvg-id -> (feed, source channel id).
OVERRIDES = {
    "ESPN.us@HD": ("epgpw", "465198"),
    "ESPN.us@SD": ("epgpw", "465198"),
    "HallmarkMoviesMore.us@SD": (
        "plex",
        "6a1610bebdf296985fd95603-6244cf3c742b0d58cd4f2514",
    ),
}


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

    # Decide a (feed, source id) for every playlist channel.
    mapping = {}  # tvg-id -> (feed, source id, display name)
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
        if match and match[0] in paths:
            mapping[tvg_id] = (match[0], match[1], name.strip())

    # Pull programmes feed by feed.
    programmes = {}
    for key in paths:
        wanted = {src for feed, src, _ in mapping.values() if feed == key}
        if wanted:
            programmes[key] = read_programmes(paths[key], wanted)

    now = datetime.now(timezone.utc)
    tv = ET.Element("tv", {"generator-info-name": "Custom mapped EPG"})
    all_programmes = []
    kept = 0
    for tvg_id, (feed, source_id, display) in mapping.items():
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
