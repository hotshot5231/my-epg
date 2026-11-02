from copy import deepcopy
from datetime import datetime, timedelta, timezone
import difflib
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

SCRAPE_SITES = ["tvpassport.com"]  # tvtv.us disabled: every request returns 404
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

# Cable channels matched by name in epg.pw
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
    "MLBStrikeZone.us
