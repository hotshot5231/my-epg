from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import xml.etree.ElementTree as ET

OUTPUT_DIR = Path("public")
OUTPUT_FILE = OUTPUT_DIR / "test_guide.xml"

# Same source IDs and playlist mappings as the working local guide.
SOURCES = [
    (
        "https://epg.pw/xmltv/epg_US.xml",
        "465198",
        [
            ("ESPN.us@HD", "ESPN HD"),
            ("ESPN.us@SD", "ESPN SD"),
        ],
    ),
    (
        "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/Plex/us.xml",
        "6a1610bebdf296985fd95603-6244cf3c742b0d58cd4f2514",
        [
            ("HallmarkMoviesMore.us@SD", "Hallmark Movies & More"),
        ],
    ),
]


def fetch_programmes(url, source_channel_id, target_channels):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 EPG builder"},
    )

    found_channel = False
    programmes = []
    stack = []
    root = None

    print(f"Reading guide: {url}", flush=True)

    with urllib.request.urlopen(request, timeout=90) as response:
        for event, element in ET.iterparse(
            response, events=("start", "end")
        ):
            if event == "start":
                if root is None:
                    root = element
                stack.append(element)
                continue

            parent = stack[-2] if len(stack) > 1 else None

            # Remove completed top-level items to limit memory use.
            if parent is root:
                if (
                    element.tag == "channel"
                    and element.get("id") == source_channel_id
                ):
                    found_channel = True

                elif (
                    element.tag == "programme"
                    and element.get("channel") == source_channel_id
                ):
                    for target_id, _ in target_channels:
                        programme = deepcopy(element)
                        programme.set("channel", target_id)
                        programmes.append(programme)

                root.remove(element)

            stack.pop()

    if not found_channel:
        raise RuntimeError(
            f"Source channel {source_channel_id!r} not found in {url}"
        )

    if not programmes:
        raise RuntimeError(
            f"No listings found for source channel {source_channel_id!r}"
        )

    print(f"Found {len(programmes)} mapped listings.", flush=True)
    return programmes


def parse_xmltv_time(value):
    try:
        return datetime.strptime(value.strip(), "%Y%m%d%H%M%S %z")
    except ValueError:
        return None


def validate_guide(tv):
    now = datetime.now(timezone.utc)

    for channel in tv.findall("channel"):
        channel_id = channel.get("id")
        has_unexpired_listing = False

        for programme in tv.findall("programme"):
            if programme.get("channel") != channel_id:
                continue

            stop = parse_xmltv_time(programme.get("stop", ""))
            if stop is not None and stop > now:
                has_unexpired_listing = True
                break

        if not has_unexpired_listing:
            raise RuntimeError(
                f"No unexpired listings with readable stop times for "
                f"{channel_id}. Publication stopped."
            )


def main():
    tv = ET.Element(
        "tv",
        {"generator-info-name": "Custom mapped EPG"},
    )
    all_programmes = []

    for url, source_id, target_channels in SOURCES:
        all_programmes.extend(
            fetch_programmes(url, source_id, target_channels)
        )

        for target_id, display_name in target_channels:
            channel = ET.SubElement(tv, "channel", {"id": target_id})
            ET.SubElement(channel, "display-name").text = display_name

    all_programmes.sort(
        key=lambda item: (
            item.get("start", ""),
            item.get("channel", ""),
        )
    )

    tv.extend(all_programmes)
    validate_guide(tv)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(tv).write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
    )

    print(
        f"Created {OUTPUT_FILE} with "
        f"{len(tv.findall('channel'))} channel IDs and "
        f"{len(all_programmes)} listings.",
        flush=True,
    )


if __name__ == "__main__":
    main()
