#!/usr/bin/env python3
"""Build a custom XMLTV guide for the Kemo playlist (TiviMate).

Headless GitHub Actions run (default): re-download the free public XMLTV
sources, keep channel ids / display-names / logos from the playlist files,
and write kemo-custom-epg.xml.gz at the repo root. The uncompressed XML is
removed after compression because it is over GitHub's 100 MB file limit.

The provider snapshot (data/kemo-epg-live.xml) cannot be refreshed here — that
needs a login — and is never fetched. Only programmes in that file whose start
is still in the future are reused. This script does not read or write
credentials, secrets, or stream URLs.

Channel ids follow TiviMate matching rules:
  * playlist tvg-id when the playlist has one (including typo ids)
  * otherwise the exact playlist channel name
  * first <display-name> is always the exact playlist name
  * second <display-name> is the playlist group

Programme data starts with future provider listings, then fills gaps from
public XMLTV (epgshare01 US/UK/CA/locals/sports, i.mjh.nz Samsung/Pluto,
Ireland). Matching prefers precision over recall. Logos come from
tv-logo/tv-logos, then iptv-org, then a safe playlist image URL.

Generic sports titles on network channels are matched to the public ESPN
scoreboard (league + network + start within 30 minutes) and retitled with the
teams. Numbered event / PPV channels use a matchup in the channel name when
there is one; otherwise a single-league slot is a best-guess lineup of that
day's games. Other 24/7 channels with no schedule still get 2-hour
placeholders titled with the channel name and desc "No guide data".
"""

from __future__ import annotations

import argparse
import csv
import sports_schedule
import gzip
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape, quoteattr
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
XMLTV_DT = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\s*([+-]\d{4})?")
UA = "KemoCustomEPG/1.0 (personal XMLTV builder; +https://github.com/erikrepp-hue/kemo-epg)"
TV_LOGO_RAW = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/"
EPGSHARE = "https://epgshare01.online/epgshare01"

# Public sources, in fill-priority order after the Kemo provider feed.
SOURCES = [
    # name, filename, url, kind
    ("epgshare-us-locals", "epg_ripper_US_LOCALS1.xml.gz",
     f"{EPGSHARE}/epg_ripper_US_LOCALS1.xml.gz", "us-locals"),
    ("epgshare-us", "epg_ripper_US2.xml.gz",
     f"{EPGSHARE}/epg_ripper_US2.xml.gz", "us"),
    ("epgshare-uk", "epg_ripper_UK1.xml.gz",
     f"{EPGSHARE}/epg_ripper_UK1.xml.gz", "uk"),
    ("epgshare-ca", "epg_ripper_CA2.xml.gz",
     f"{EPGSHARE}/epg_ripper_CA2.xml.gz", "ca"),
    ("epgshare-us-sports", "epg_ripper_US_SPORTS1.xml.gz",
     f"{EPGSHARE}/epg_ripper_US_SPORTS1.xml.gz", "us-sports"),
    ("epgshare-ie", "epg_ripper_IE1.xml.gz",
     f"{EPGSHARE}/epg_ripper_IE1.xml.gz", "ie"),
    ("epgshare-fanduel", "epg_ripper_FANDUEL1.xml.gz",
     f"{EPGSHARE}/epg_ripper_FANDUEL1.xml.gz", "us-sports"),
    ("samsung-us", "samsung_us.xml",
     "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/SamsungTVPlus/us.xml",
     "fast"),
    ("pluto-us", "pluto_us.xml",
     "https://raw.githubusercontent.com/matthuisman/i.mjh.nz/master/PlutoTV/us.xml",
     "fast"),
    # More keyless public guides. Used when a real channel is still empty.
    ("epgshare-plex", "epg_ripper_PLEX1.xml.gz",
     f"{EPGSHARE}/epg_ripper_PLEX1.xml.gz", "fast"),
    ("epgshare-distro", "epg_ripper_DISTROTV1.xml.gz",
     f"{EPGSHARE}/epg_ripper_DISTROTV1.xml.gz", "fast"),
    ("epgshare-rakuten", "epg_ripper_RAKUTEN1.xml.gz",
     f"{EPGSHARE}/epg_ripper_RAKUTEN1.xml.gz", "fast"),
    ("epgshare-peacock", "epg_ripper_PEACOCK1.xml.gz",
     f"{EPGSHARE}/epg_ripper_PEACOCK1.xml.gz", "fast"),
    ("epgshare-bein", "epg_ripper_BEIN1.xml.gz",
     f"{EPGSHARE}/epg_ripper_BEIN1.xml.gz", "us-sports"),
    ("epgshare-directv-sports", "epg_ripper_DIRECTVSPORTS1.xml.gz",
     f"{EPGSHARE}/epg_ripper_DIRECTVSPORTS1.xml.gz", "us-sports"),
]

LOGO_ASSETS = [
    ("iptv-org-channels.json", "https://iptv-org.github.io/api/channels.json"),
    ("iptv-org-logos.json", "https://iptv-org.github.io/api/logos.json"),
    ("tv-logos-tree.json",
     "https://api.github.com/repos/tv-logo/tv-logos/git/trees/main?recursive=1"),
]

# Words that look like US call signs but are not.
CALL_BLACKLIST = {
    "WEST", "EAST", "BEST", "NEWS", "PLUS", "GOLD", "LIVE", "SHOW", "STAR",
    "WAVE", "WILD", "WIRE", "WORK", "WEEK", "WELL", "WANT", "WHAT", "WHEN",
    "WITH", "WASH", "WIND", "WOOD", "WORD", "WARM", "WISH", "WISE", "WIDE",
    "KIDS", "KNOW", "KEEP", "KIND", "KICK", "KISS", "KILN", "KILO", "KITE",
    "KEY", "KEYS",
    "ONLY", "REAL", "TIME", "PLAY", "FOOD", "MAXX", "MAIN", "HOME", "FREE",
    "CINE", "FILM", "SPOT", "SPIKE", "HALL", "HOPE", "HERO", "HITS",
}

QUALITY_RE = re.compile(
    r"\b(?:uhd|fhd|hdr|hd|sd|4k|8k|lhd|hevc|h\.?265|h\.?264|1080p?|720p?|"
    r"2160p?|50fps|60fps|25fps)\b",
    re.I,
)
# Leading country / region pack prefixes (not California "CA |").
PREFIX_RE = re.compile(
    r"^(?:(?:usa|us|uk|gb|ie|ire|ireland|can|canada|ca|au|aus|nz|de|fr|es|"
    r"it|nl|se|no|dk|fi|pl|pt|br|mx|in|jp|kr|za|ae|sa|int|world)\s*"
    r"[:.\-]+\s*|(?:usa|uk|gb|ie|ireland)\s+)",
    re.I,
)
CA_PIPE_RE = re.compile(r"^CA\s*\|", re.I)  # California locals, keep after strip of CA|
PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
SPACE_RE = re.compile(r"\s+")
SLUG_RE = re.compile(r"[^A-Za-z0-9]+")
SAFE_LOGO_BAD = re.compile(
    r"(?:login|username|password|/live/|/movie/|/series/|/play/|\.m3u8|"
    r"player_api|get\.php|s:\d+:/|://[^/]*:[^/]*@)",
    re.I,
)
EVENT_CAT_RE = re.compile(
    r"(?:24\s*/\s*7|24-7|ppv|pay[\s-]*per[\s-]*view|\bevents?\b|sportsnet\s*\+|"
    r"espn\+|paramount\+|dazn|big\s*10\+|nfl sunday|ncaab|ncaaf|ufc)",
    re.I,
)
EVENT_NAME_RE = re.compile(
    r"(?:^24\s*/\s*7\b|^24-7\b|\bppv\b|pay[\s-]*per[\s-]*view|"
    r"\b\d{1,4}\s*:\s|"  # ESPN+ 058: / NBA 10: / Sportsnet+ 21:
    r"@\s*\d{1,2}:\d{2}|"
    r"\(\d{4}-\d{2}-\d{2}|"
    r"\bvs\.?\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b)",
    re.I,
)

# Default regional feeds when the playlist name has no region.
REGIONAL_DEFAULTS = {
    "bbc one": ["bbc one lon", "bbc one london", "bbc one hd"],
    "bbc two": ["bbc two england", "bbc two hd", "bbc two"],
    "itv1": ["itv1 hd", "itv1 london", "itv1"],
    "itv 1": ["itv1 hd", "itv1 london", "itv 1"],
    "cbc": ["cbc toronto", "cbc toronto hd"],
    "ctv": ["ctv toronto", "ctv toronto hd"],
    "global": ["global toronto", "global toronto hd"],
    "citytv": ["citytv toronto", "citytv toronto hd", "citytv"],
}


def log(msg: str) -> None:
    ts = datetime.now(TZ).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def slug(s: str) -> str:
    return SLUG_RE.sub("-", s).strip("-").lower()


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")


def normalize_name(name: str, *, keep_feed: bool = True) -> str:
    """Normalize a channel name for matching.

    Strips pack prefixes (USA, UK:, CA:), quality tags (HD/FHD/4K), and
    punctuation. East/West (and similar) are kept when keep_feed is True so
    coastal feeds are not collapsed into each other.
    """
    s = fold(name).strip()
    if not s:
        return ""
    # California "CA | City | Network" — strip only the CA pipe, keep the rest.
    if CA_PIPE_RE.match(s):
        s = CA_PIPE_RE.sub("", s)
    else:
        s = PREFIX_RE.sub("", s)
    s = s.replace("&", " and ")
    s = s.replace("+", " plus ")
    s = QUALITY_RE.sub(" ", s)
    if not keep_feed:
        s = re.sub(r"\b(?:east|west|pacific|atlantic|mountain|central)\b", " ", s, flags=re.I)
    s = PUNCT_RE.sub(" ", s)
    s = SPACE_RE.sub(" ", s).strip().lower()
    # Common pack leftovers (CA | California already stripped above, so
    # a leading "ca " here is the Canada pack prefix).
    s = re.sub(r"^(?:usa|uk|us|ca|can|canada|gb|ie|ireland)\s+", "", s)
    # espn2 / itv1 / tsn5 / 4seven → espn 2 / itv 1 / tsn 5 / 4 seven
    s = re.sub(r"([a-z])(\d)", r"\1 \2", s)
    s = re.sub(r"(\d)([a-z])", r"\1 \2", s)
    s = SPACE_RE.sub(" ", s).strip()
    return s


_WORD_NUM = (
    ("one", "1"), ("two", "2"), ("three", "3"), ("four", "4"), ("five", "5"),
    ("six", "6"), ("seven", "7"), ("eight", "8"), ("nine", "9"),
)
_BACKUP_RE = re.compile(
    r"\b(?:backup|back up|alternate|alt|feed 2)\b", re.I
)


def name_keys(name: str, *, keep_feed: bool = True) -> set[str]:
    """Normalized keys plus number-word aliases (BBC Two ↔ bbc 2)."""
    n = normalize_name(name, keep_feed=keep_feed)
    if not n:
        return set()
    keys = {n}
    swapped = n
    for w, d in _WORD_NUM:
        swapped = re.sub(rf"\b{w}\b", d, swapped)
    keys.add(SPACE_RE.sub(" ", swapped).strip())
    swapped = n
    for w, d in _WORD_NUM:
        swapped = re.sub(rf"\b{d}\b", w, swapped)
    keys.add(SPACE_RE.sub(" ", swapped).strip())
    # ignore quality already stripped; also a backup-stripped variant
    trimmed = _BACKUP_RE.sub(" ", n)
    trimmed = SPACE_RE.sub(" ", trimmed).strip()
    if trimmed:
        keys.add(trimmed)
    # UK region abbreviations used in XMLTV ids/names
    abbr = (
        (r"\bsth\b", "south"),
        (r"\bwal\b", "wales"),
        (r"\bni\b", "northern ireland"),
        (r"\blon\b", "london"),
        (r"\bscot\b", "scotland"),
        (r"\bn west\b", "north west"),
        (r"\bs west\b", "south west"),
        (r"\bs east\b", "south east"),
        (r"\be mid\b", "east midlands"),
        (r"\bwm\b", "west midlands"),
        (r"\byorks\b", "yorkshire"),
        (r"\by and l\b", "yorkshire and lincolnshire"),
        (r"\bne and c\b", "north east and cumbria"),
    )
    extra = set()
    for key in list(keys):
        exp = key
        for pat, repl in abbr:
            exp = re.sub(pat, repl, exp)
        if exp != key:
            extra.add(SPACE_RE.sub(" ", exp).strip())
    keys |= extra
    return {k for k in keys if k}


def extract_feed_side(name: str) -> str | None:
    m = re.search(r"\b(east|west|pacific|atlantic|mountain)\b", fold(name), re.I)
    return m.group(1).lower() if m else None


def extract_call_signs(name: str, tvg_id: str = "") -> set[str]:
    """US (K/W) and a conservative set of Canadian (C) call signs."""
    found: set[str] = set()
    text = fold(name).upper()
    # Parenthetical: (WKRN), (WSMV), (KNDB2)
    for m in re.finditer(r"\(([KW][A-Z]{2,3})(?:-?[A-Z]{0,2}\d?)?\)", text):
        c = m.group(1)
        if c not in CALL_BLACKLIST:
            found.add(c)
    # Trailing / mid-name tokens: "NBC KTVF", "Nashville WKRN", "KODE Joplin"
    for m in re.finditer(r"\b([KW][A-Z]{2,3})(?:-?(?:TV|DT|CD|LD|HD))?\b", text):
        c = m.group(1)
        if c not in CALL_BLACKLIST:
            # Require it is not a mid-sentence English word already blacklisted.
            found.add(c)
    # tvg-id patterns: abcwkrn.us, cbs5wtvf.us, foxwztv.us, nbc12wsfa.us, nbcwsmw.us
    tid = (tvg_id or "").lower()
    tid = re.sub(r"~\d+$", "", tid)
    # Call sign must be its own token. "cbssportsnetworkusa.us" ends in the
    # letters kusa but is not KUSA; "nbcwkyc.us" and "cbs19woio.us" are.
    m = re.search(
        r"(?:^|[^a-z]|nbc|abc|cbs|fox|cw|pbs|my|ion)"
        r"([kw][a-z]{3})(?:-?dt\d?)?(?:\.[a-z0-9_]+)?$",
        tid,
    )
    if m:
        c = m.group(1).upper()
        if c not in CALL_BLACKLIST:
            found.add(c)
    # Also "WKRN-DT.us_locals1" style already covered.
    # Typo helper: WSMW in id but WSMV in the name — name already captured.
    return found


def looks_invented_slug(cid: str, name: str) -> bool:
    """True when kemo-epg-channels.xml used a generated kebab slug, not a tvg-id."""
    raw = re.sub(r"~\d+$", "", cid or "")
    if not raw:
        return True
    if "." in raw:
        return False  # country-style tvg-id, even if wrong (e.g. 360northanchorage.us)
    if re.fullmatch(r"[A-Za-z0-9]+", raw):
        # Compact tokens like KODEDT / KMIZ / ALTSPRT are real tvg-ids,
        # unless they are exactly the kebab-less slug of the name (rare).
        if raw.lower() == slug(name).replace("-", ""):
            return True
        return False
    s = slug(name)
    if raw == s or s.startswith(raw) or raw.startswith(s[:16]):
        return True
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", raw):
        return True
    return False


def tivi_id(stored_id: str, name: str) -> str:
    """Channel id TiviMate will actually match against the playlist."""
    raw = re.sub(r"~\d+$", "", stored_id or "")
    if looks_invented_slug(stored_id, name) or not raw:
        return name
    return raw


def _id_stem_keys(tvg_id: str) -> set[str]:
    raw = re.sub(r"~\d+$", "", tvg_id or "")
    stem = raw.split(".")[0] if raw else ""
    if not stem:
        return set()
    # fox34wdfx / abcwkrn already handled via call signs; also normalize stem
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    return name_keys(spaced.replace("_", " ").replace("-", " "), keep_feed=True)


def tvg_id_fits_channel(tvg_id: str, name: str, calls: set[str]) -> bool:
    """Whether a playlist tvg-id looks like it actually belongs to this name."""
    if not tvg_id:
        return False
    id_calls = extract_call_signs("", tvg_id)
    if id_calls and calls and id_calls & calls:
        return True
    if id_calls and calls and not (id_calls & calls):
        return False
    keys = name_keys(name, keep_feed=False)
    stems = _id_stem_keys(tvg_id)
    if stems and keys and (stems & keys):
        return True
    # substring stem in name for compact ids like "4seven" / "redzone"
    for st in stems:
        if any(st == k or st in k or k in st for k in keys if len(st) >= 4):
            return True
    return False


def same_station(a: dict, b: dict) -> bool:
    ca = a.get("name_calls") or a.get("calls") or set()
    cb = b.get("name_calls") or b.get("calls") or set()
    if ca and cb and ca & cb:
        return True
    if ca and cb and not (ca & cb):
        return False
    ka = name_keys(a["name"], keep_feed=False)
    kb = name_keys(b["name"], keep_feed=False)
    if ka & kb:
        return True
    return False


def resolve_tivi_ids(playlist: list[dict]) -> None:
    """Keep a tvg-id only when it is unique to one station (or clearly fits).

    Many playlist rows share junk provider ids (e.g. 360northanchorage.us on
    Nashville WSMV *and* Amarillo KAMR). A shared wrong id would give every
    row the same (wrong) guide. In that case TiviMate name-matching is used
    instead: the channel id becomes the exact playlist name.
    """
    groups: dict[str, list[dict]] = {}
    for ch in playlist:
        tvg = ch.get("tvg_id") or ""
        if tvg:
            groups.setdefault(tvg, []).append(ch)
        else:
            ch["id"] = ch["name"]
    for tvg, members in groups.items():
        if len(members) == 1:
            # Unique tvg-id: keep it, even if it is a typo (nbcwsmw.us for WSMV)
            # unless the id's own call sign conflicts with the name.
            ch = members[0]
            if tvg_id_fits_channel(tvg, ch["name"], ch["calls"]) or not extract_call_signs("", tvg):
                ch["id"] = tvg
            elif extract_call_signs("", tvg) and ch["calls"] and not (extract_call_signs("", tvg) & ch["calls"]):
                # typo like nbcwsmw.us still has WSMW vs WSMV — keep typo id
                # when it is unique, because that is what TiviMate will send.
                ch["id"] = tvg
            else:
                ch["id"] = tvg
            continue
        # Multiple rows share this id.
        all_same = all(same_station(members[0], m) for m in members[1:])
        if all_same:
            for ch in members:
                ch["id"] = tvg
            continue
        # Mixed stations: only rows the id actually fits keep it; others use name.
        for ch in members:
            if tvg_id_fits_channel(tvg, ch["name"], ch["calls"]):
                ch["id"] = tvg
            else:
                ch["id"] = ch["name"]


def is_safe_logo_url(url: str) -> bool:
    if not url:
        return False
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    if SAFE_LOGO_BAD.search(u):
        return False
    # Must look like an image path or a known image host, not a stream.
    if re.search(r"\.(m3u8|ts|mpd|mp4)(?:\?|$)", u, re.I):
        return False
    return True


def is_placeholder_channel(name: str, category: str) -> bool:
    if EVENT_CAT_RE.search(category or ""):
        return True
    if EVENT_NAME_RE.search(name or ""):
        return True
    return False


def region_of(name: str, category: str) -> str:
    cat = (category or "")
    n = name or ""
    if cat.startswith("UK") or n.startswith("UK:") or n.startswith("UK ") or n.startswith("UK|"):
        return "UK"
    if cat.startswith("CA:") or cat.startswith("Canada") or cat.startswith("CANADA") \
            or n.startswith("CA:") or n.startswith("CA "):
        # "CA |" is California (US local)
        if CA_PIPE_RE.match(n) or cat.startswith("CA |") or "California" in cat:
            return "US"
        if cat.startswith("CA:") or cat.startswith("Canada") or cat.startswith("CANADA") \
                or n.startswith("CA:"):
            return "CA"
    if cat.startswith("USA") or cat.startswith("US ") or n.startswith("USA ") \
            or n.startswith("US ") or CA_PIPE_RE.match(n) or "USA" in cat:
        return "US"
    if cat.startswith("IRE") or n.startswith("IE:") or n.startswith("IRE"):
        return "IE"
    return "OTHER"


def parse_xmltv_time(s: str) -> datetime | None:
    if not s:
        return None
    m = XMLTV_DT.match(s.strip())
    if not m:
        return None
    y, mo, d, h, mi, se, off = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se))
    if off:
        sign = 1 if off[0] == "+" else -1
        oh, om = int(off[1:3]), int(off[3:5])
        # store as aware UTC
        from datetime import timezone
        tz = timezone(timedelta(hours=sign * oh, minutes=sign * om))
        return dt.replace(tzinfo=tz)
    from datetime import timezone
    return dt.replace(tzinfo=timezone.utc)


def overlaps(a_start: datetime, a_stop: datetime, b_start: datetime, b_stop: datetime) -> bool:
    return a_start < b_stop and b_start < a_stop


def download(url: str, dest: Path, timeout: int = 180, attempts: int = 4) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            log(f"Downloading {url} -> {dest.name} (attempt {attempt}/{attempts})")
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
            if tmp.stat().st_size < 100:
                raise RuntimeError(f"download too small ({tmp.stat().st_size} bytes)")
            tmp.replace(dest)
            log(f"  saved {dest.stat().st_size:,} bytes")
            return
        except Exception as e:
            last = e
            log(f"  download failed: {e}")
            if tmp.exists():
                tmp.unlink()
            if attempt < attempts:
                time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"failed to download {url}: {last}")


def ensure_file(cache: Path, filename: str, url: str, refresh: bool) -> Path:
    path = cache / filename
    if path.exists() and path.stat().st_size > 100 and not refresh:
        # Resolve symlink and keep using it
        return path
    # US locals is the large grab (~50 MB compressed).
    timeout = 600 if "LOCALS" in filename else 240
    download(url, path if not path.is_symlink() else Path(os.path.realpath(path)),
             timeout=timeout)
    return path


def localname(tag: str) -> str:
    if tag and "}" in tag:
        return tag.split("}", 1)[1]
    return tag or ""


def text_of(elem: ET.Element | None) -> str:
    if elem is None or elem.text is None:
        return ""
    return elem.text


def iterparse_xmltv(path: Path, stop_after_channels: bool = False):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as f:
        root = None
        for event, elem in ET.iterparse(f, events=("start", "end")):
            if event == "start" and root is None:
                root = elem
                continue
            if event != "end":
                continue
            tag = localname(elem.tag)
            if tag == "channel":
                yield tag, elem
                elem.clear()
                if root is not None:
                    root.clear()
            elif tag == "programme":
                if stop_after_channels:
                    elem.clear()
                    return
                yield tag, elem
                elem.clear()
                if root is not None:
                    root.clear()


def channel_from_elem(elem: ET.Element) -> dict:
    cid = elem.get("id") or ""
    names = []
    icon = ""
    for child in list(elem):
        t = localname(child.tag)
        if t == "display-name" and child.text:
            names.append(child.text.strip())
        elif t == "icon" and not icon:
            src = (child.get("src") or "").strip()
            if is_safe_logo_url(src) and "generic_sources" not in src:
                icon = src
    return {"id": cid, "names": names, "icon": icon}


def programme_from_elem(elem: ET.Element) -> dict | None:
    ch = elem.get("channel") or ""
    start_s = elem.get("start") or ""
    stop_s = elem.get("stop") or ""
    start = parse_xmltv_time(start_s)
    stop = parse_xmltv_time(stop_s)
    if not ch or start is None:
        return None
    title = desc = sub = date = ""
    cats: list[str] = []
    episode = ""
    ep_sys = ""
    for child in list(elem):
        t = localname(child.tag)
        if t == "title" and not title:
            title = (child.text or "").strip()
        elif t == "sub-title" and not sub:
            sub = (child.text or "").strip()
        elif t == "desc" and not desc:
            desc = (child.text or "").strip()
        elif t == "date" and not date:
            date = (child.text or "").strip()
        elif t == "category" and child.text:
            cats.append(child.text.strip())
        elif t == "episode-num" and not episode and child.text:
            episode = child.text.strip()
            ep_sys = child.get("system") or ""
    return {
        "channel": ch,
        "start_s": start_s,
        "stop_s": stop_s,
        "start": start,
        "stop": stop,
        "title": title,
        "sub": sub,
        "desc": desc,
        "date": date,
        "cats": cats[:4],
        "episode": episode,
        "ep_sys": ep_sys,
    }


class SourceIndex:
    def __init__(self, name: str, kind: str):
        self.name = name
        self.kind = kind
        self.by_id: dict[str, dict] = {}
        self.by_exact: dict[str, list[str]] = defaultdict(list)
        self.by_norm: dict[str, list[str]] = defaultdict(list)
        self.by_norm_nofeed: dict[str, list[str]] = defaultdict(list)
        self.by_call: dict[str, list[str]] = defaultdict(list)

    def add(self, ch: dict) -> None:
        cid = ch["id"]
        if not cid:
            return
        self.by_id[cid] = ch
        for n in ch["names"] or [cid]:
            key = fold(n).strip().lower()
            if key:
                self.by_exact[key].append(cid)
            for nn in name_keys(n, keep_feed=True):
                self.by_norm[nn].append(cid)
            for nf in name_keys(n, keep_feed=False):
                self.by_norm_nofeed[nf].append(cid)
            for call in extract_call_signs(n, cid):
                self.by_call[call].append(cid)
        # Call signs from the source id itself (WKRN-DT.us_locals1)
        for call in extract_call_signs(" ".join(ch["names"]), cid):
            if cid not in self.by_call[call]:
                self.by_call[call].append(cid)

    def unique(self, ids: Iterable[str]) -> list[str]:
        out = []
        seen = set()
        for i in ids:
            if i not in seen and i in self.by_id:
                seen.add(i)
                out.append(i)
        return out


def prefer_candidate(playlist_name: str, ids: list[str], index: SourceIndex) -> str | None:
    """Pick one source id or reject if the match would be ambiguous."""
    ids = index.unique(ids)
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    want = normalize_name(playlist_name, keep_feed=True)
    side = extract_feed_side(playlist_name)
    want_hd = bool(QUALITY_RE.search(playlist_name))

    scored = []
    for cid in ids:
        names = index.by_id[cid]["names"] or [cid]
        norms = [normalize_name(n, keep_feed=True) for n in names]
        bestn = min(norms, key=lambda n: (n != want, len(n)))
        exact = int(any(n == want for n in norms))
        same_side = 0
        if side:
            cs = extract_feed_side(" ".join(names))
            if cs == side:
                same_side = 2
            elif cs and cs != side:
                same_side = -2
            else:
                same_side = 0
        hd = int(any(QUALITY_RE.search(n) for n in names))
        if want_hd:
            hd_score = hd
        else:
            hd_score = 0
        # Prefer shorter / less-qualified names when the playlist is generic.
        scored.append((exact, same_side, hd_score, -len(bestn), cid))
    scored.sort(reverse=True)
    # If the playlist has no region and this is a known regional network,
    # try a default.
    base = normalize_name(playlist_name, keep_feed=False)
    if base in REGIONAL_DEFAULTS:
        prefs = REGIONAL_DEFAULTS[base]
        for pref in prefs:
            for cid in ids:
                names = index.by_id[cid]["names"] or [cid]
                if any(normalize_name(n, keep_feed=True) == pref or
                       normalize_name(n, keep_feed=False) == pref for n in names):
                    return cid

    top = scored[0]
    # If first two tie on exact + side, it's ambiguous — skip.
    if len(scored) > 1:
        a, b = scored[0], scored[1]
        if (a[0], a[1]) == (b[0], b[1]) and a[0] == 0:
            # neither is exact and sides tie — too risky unless one name equals want
            return None
        if a[0] == b[0] == 1 and a[1] == b[1]:
            # two exact matches; prefer HD/side already in sort, take top if unique key
            if (a[0], a[1], a[2]) == (b[0], b[1], b[2]):
                # identical — take first only if names are essentially the same feed
                return a[4]
    # Don't map to the opposite coast.
    if side and top[1] < 0:
        return None
    return top[4]


_NATIONAL_CABLE = re.compile(
    r"\b(?:cbs sports|espn|espnews|fox news|fox business|fox sports|"
    r"nbc sports|nbc news|cnbc|sec network|acc network|big ten|"
    r"golf channel|nfl network|nba tv|nhl network|mlb network|"
    r"longhorn|bein sports|newsmax|newsnation|news nation)\b",
    re.I,
)


def national_cable(name: str) -> bool:
    """A national network, not a local affiliate that shares a word with it."""
    if extract_call_signs(name or ""):
        return False
    return bool(_NATIONAL_CABLE.search(name or ""))


def is_affiliate_source(cid: str) -> bool:
    return bool(re.search(
        r"(?:^|[._-])[KW][A-Z]{3,4}(?:-?(?:DT|LD|CD|TV)\d?)?(?:\.|$)",
        cid or "",
    ))


def match_in_index(name: str, tvg_id: str, calls: set[str], index: SourceIndex,
                   allow_fast: bool = False) -> str | None:
    # A national name must never be matched by an embedded call sign
    # ("cbssportsnetworkusa.us" is not KUSA).
    if national_cable(name):
        calls = set()
    # 1) exact source id equals our tvg-id
    if tvg_id and tvg_id in index.by_id and not (
            national_cable(name) and is_affiliate_source(tvg_id)):
        return tvg_id
    # 2) exact display-name (case-insensitive)
    key = fold(name).strip().lower()
    if key in index.by_exact:
        picked = prefer_candidate(name, index.by_exact[key], index)
        if picked:
            return picked
    # also try without quality tags on the exact string
    # 3) call sign — only if every candidate shares that call (locals)
    if calls and index.kind in ("us-locals", "us", "ca"):
        for call in sorted(calls):
            cands = index.by_call.get(call) or []
            # Prefer the main DT feed, not -DT2 subchannels, unless the name
            # clearly asks for a subchannel / CW / MyNet / Charge etc.
            if cands:
                def _src_names(cid: str) -> str:
                    return " ".join(index.by_id[cid]["names"]).upper()

                name_u = name.upper()
                # The call sign on the source must be this station, not a
                # longer sign that merely contains the same letters.
                exact = []
                for cid in cands:
                    src_calls = extract_call_signs(" ".join(index.by_id[cid]["names"]), cid)
                    if not src_calls or call in src_calls:
                        exact.append(cid)
                if exact:
                    cands = exact
                if re.search(r"\bCW\b", name_u):
                    branded = [c for c in cands if re.search(r"\bCW\b", _src_names(c))]
                    if branded:
                        cands = branded
                elif re.search(r"\bMY(?:TV|NET|NETWORK)?\b", name_u):
                    branded = [c for c in cands if re.search(r"\bMY", _src_names(c))]
                    if branded:
                        cands = branded
                wants_sub = bool(re.search(r"\b(?:DT\s*[2-9]|DT[2-9]|\.2|TV\s*2)\b", name_u))
                if not wants_sub:
                    main = [c for c in cands if not re.search(r"-(?:DT|TV)[2-9]\b", c, re.I)
                            and not re.search(r"(?:DT|TV)\s*[2-9]", _src_names(c))]
                    if main:
                        cands = main
                # Full-power DT over a low-power LD/CD sibling (WSB-DT, not WSB-LD).
                if not re.search(r"\b(?:LD|CD|LPTV)\b", name_u):
                    full = [c for c in cands if re.search(r"-DT(?:\.|$)", c, re.I)
                            and not re.search(r"-DT[2-9]", c, re.I)]
                    if full:
                        cands = full
                picked = prefer_candidate(name, cands, index)
                if picked:
                    return picked
    # 4) normalized unique name
    nn = normalize_name(name, keep_feed=True)
    if nn and nn in index.by_norm:
        picked = prefer_candidate(name, index.by_norm[nn], index)
        if picked:
            return picked
    # 5) normalized without east/west, only if a single feed exists (or same side)
    nf = normalize_name(name, keep_feed=False)
    if nf and nf in index.by_norm_nofeed:
        picked = prefer_candidate(name, index.by_norm_nofeed[nf], index)
        if picked:
            return picked
    # "Fox News" → "Fox News Channel" when the short name itself is absent
    for suffix in (" channel", " television"):
        cand = (nn or "") + suffix
        if cand and cand in index.by_norm:
            picked = prefer_candidate(name, index.by_norm[cand], index)
            if picked:
                return picked
    # FAST sources: only unique normalized name, no loose call-sign use
    if allow_fast and index.kind == "fast":
        if nn and nn in index.by_norm and len(index.unique(index.by_norm[nn])) == 1:
            return index.unique(index.by_norm[nn])[0]
        # "24/7 The Office" → "the office" already in nn because 24/7 stripped?
        # 24/7 is not a quality tag; strip it for FAST only.
        fast = re.sub(r"^24\s*/\s*7\s+", "", nn or "")
        fast = re.sub(r"^24-7\s+", "", fast).strip()
        if fast and fast in index.by_norm and len(index.unique(index.by_norm[fast])) == 1:
            return index.unique(index.by_norm[fast])[0]
    return None


def check_playlist_csv(csv_path: Path, playlist: list[dict]) -> None:
    """Confirm names and groups in the CSV match the channel XML.

    Ids and logos live only in the XML. The CSV is the playlist name/group
    list and must stay aligned with it; the XML remains the mapping source.
    """
    if not csv_path.exists():
        log(f"WARNING: playlist CSV not found ({csv_path}); using the channel XML only")
        return
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    pairs = [(r.get("channel") or "", r.get("category") or "") for r in rows]
    xml_pairs = [(ch["name"], ch["category"]) for ch in playlist]
    if pairs == xml_pairs:
        log(f"  playlist CSV matches channel XML ({len(pairs):,} names/groups)")
        return
    log("WARNING: kemo-full-channels.csv differs from kemo-epg-channels.xml; "
        "ids, display-names, and logos still come from the channel XML")


def drop_finished_provider_programmes(programmes: dict[str, list[dict]],
                                      now: datetime) -> tuple[dict[str, list[dict]], int]:
    """Keep provider listings whose start is still in the future.

    The snapshot cannot be refreshed from Actions. A programme that has
    already started is not reused, even if it has not ended.
    """
    kept: dict[str, list[dict]] = {}
    dropped = 0
    for cid, items in programmes.items():
        future = []
        for prog in items:
            start = prog.get("start")
            if start is not None and start >= now:
                future.append(prog)
            else:
                dropped += 1
        if future:
            kept[cid] = future
    return kept, dropped


def load_playlist(channels_xml: Path) -> list[dict]:
    rows = []
    # Preserve document order (playlist order)
    for event, elem in ET.iterparse(channels_xml, events=("end",)):
        if localname(elem.tag) != "channel":
            continue
        stored = elem.get("id") or ""
        names = [c.text.strip() for c in elem if localname(c.tag) == "display-name" and c.text]
        icon = ""
        for c in elem:
            if localname(c.tag) == "icon":
                src = (c.get("src") or "").strip()
                if is_safe_logo_url(src):
                    icon = src
                    break
        name = names[0] if names else stored
        category = names[1] if len(names) > 1 else ""
        tid = tivi_id(stored, name)
        # raw tvg-id if it wasn't invented
        raw_tvg = "" if looks_invented_slug(stored, name) else re.sub(r"~\d+$", "", stored)
        name_calls = extract_call_signs(name, "")
        id_calls = extract_call_signs("", raw_tvg) if raw_tvg else set()
        # A junk/shared tvg-id often embeds someone else's call sign
        # (nbcwpxi.us on WPTZ, abcwjla.us on WCTI). Never let that bleed
        # into this row's station identity.
        if name_calls and id_calls and not (name_calls & id_calls):
            calls = name_calls
        else:
            calls = name_calls | id_calls
        rows.append({
            "name": name,
            "category": category,
            "stored_id": stored,
            "tvg_id": raw_tvg,
            "id": tid,
            "playlist_logo": icon,
            "region": region_of(name, category),
            "calls": calls,
            "name_calls": name_calls,
            "placeholder_eligible": is_placeholder_channel(name, category),
        })
        elem.clear()
    return rows


def load_source_channels(path: Path, name: str, kind: str) -> SourceIndex:
    idx = SourceIndex(name, kind)
    n = 0
    # Only US_LOCALS-scale files are reliably channels-then-programmes and
    # large enough to need an early stop. Samsung/Plex interleave them.
    stop_early = path.stat().st_size > 20_000_000
    for tag, elem in iterparse_xmltv(path, stop_after_channels=stop_early):
        if tag != "channel":
            continue
        idx.add(channel_from_elem(elem))
        n += 1
    log(f"  indexed {n:,} channels from {name}")
    return idx


def collect_programmes(path: Path, wanted: set[str], window_start: datetime,
                       window_end: datetime) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    n_keep = n_seen = 0
    for tag, elem in iterparse_xmltv(path):
        if tag != "programme":
            continue
        n_seen += 1
        ch = elem.get("channel") or ""
        if ch not in wanted:
            continue
        prog = programme_from_elem(elem)
        if not prog or not prog["title"]:
            continue
        start, stop = prog["start"], prog["stop"]
        if stop is None:
            stop = start + timedelta(hours=1)
            prog["stop"] = stop
        # Keep if overlaps the window
        if stop <= window_start or start >= window_end:
            continue
        out[ch].append(prog)
        n_keep += 1
    log(f"  kept {n_keep:,} programmes ({n_seen:,} seen) from {path.name}")
    return out


def add_programme(bucket: list[dict], prog: dict) -> bool:
    """Append prog if it does not overlap an existing one. Prefer existing."""
    start, stop = prog["start"], prog["stop"]
    if stop is None:
        return False
    for ex in bucket:
        if overlaps(start, stop, ex["start"], ex["stop"]):
            return False
    bucket.append(prog)
    return True


def load_tv_logos(tree_path: Path) -> dict[str, list[tuple[str, str]]]:
    """norm stem -> list of (country, raw_url)."""
    data = json.loads(tree_path.read_text())
    tree = data.get("tree") or []
    by: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for item in tree:
        path = item.get("path") or ""
        if not path.endswith(".png") or not path.startswith("countries/"):
            continue
        parts = path.split("/")
        if len(parts) < 3:
            continue
        country = parts[1]
        stem = Path(parts[-1]).stem  # espn-us
        # strip trailing country suffix from filename
        core = re.sub(r"-(?:us|uk|gb|ca|ie|au|nz|de|fr|es|it|nl|se|no|dk|fi|pl|pt|br|mx|in|jp|kr|za|int)$",
                      "", stem, flags=re.I)
        norm = normalize_name(core.replace("-", " "), keep_feed=True)
        if not norm:
            continue
        url = TV_LOGO_RAW + path
        by[norm].append((country, url))
    return by


COUNTRY_FOLDER = {
    "US": "united-states",
    "UK": "united-kingdom",
    "CA": "canada",
    "IE": "ireland",
}


def load_iptv_org_logos(ch_path: Path, logo_path: Path) -> dict[str, list[tuple[str, str, int]]]:
    """norm name -> list of (country, url, area)."""
    channels = json.loads(ch_path.read_text())
    logos = json.loads(logo_path.read_text())
    by_id: dict[str, list[dict]] = defaultdict(list)
    for lg in logos:
        if not lg.get("in_use"):
            continue
        url = lg.get("url") or ""
        if not is_safe_logo_url(url):
            continue
        by_id[lg.get("channel") or ""].append(lg)
    out: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for ch in channels:
        cid = ch.get("id") or ""
        if cid not in by_id:
            continue
        country = (ch.get("country") or "").upper()
        if country == "GB":
            country = "UK"
        names = [ch.get("name") or ""] + list(ch.get("alt_names") or [])
        # pick best logo for this channel
        best = None
        best_score = -1
        for lg in by_id[cid]:
            w, h = int(lg.get("width") or 0), int(lg.get("height") or 0)
            fmt = (lg.get("format") or "").upper()
            score = w * h + (100000 if fmt == "PNG" else 0)
            if score > best_score:
                best_score = score
                best = lg
        if not best:
            continue
        url = best["url"]
        area = int(best.get("width") or 0) * int(best.get("height") or 0)
        for n in names:
            nn = normalize_name(n, keep_feed=True)
            if nn:
                out[nn].append((country, url, area))
    return out


def pick_logo(name: str, region: str, tv_logos, iptv_logos, playlist_logo: str) -> tuple[str, str]:
    """Return (url, source_label) or ("", "none")."""
    nn = normalize_name(name, keep_feed=True)
    keys = list(name_keys(name, keep_feed=True))
    folder = COUNTRY_FOLDER.get(region)

    def tv_pick(key: str) -> str | None:
        cands = tv_logos.get(key) or []
        if not cands:
            return None
        if folder:
            scoped = [u for c, u in cands if c == folder]
            if len(scoped) == 1:
                return scoped[0]
            if len(scoped) > 1:
                # prefer exact same stem length / first
                return scoped[0]
        # only accept unscoped if unique url
        urls = list(dict.fromkeys(u for c, u in cands))
        if len(urls) == 1:
            return urls[0]
        if folder:
            scoped = [u for c, u in cands if c == folder]
            if scoped:
                return scoped[0]
        return None

    for key in keys:
        url = tv_pick(key)
        if url:
            return url, "tv-logos"
    # try without feed side
    for key in name_keys(name, keep_feed=False):
        url = tv_pick(key)
        if url:
            return url, "tv-logos"

    def org_pick(key: str) -> str | None:
        cands = iptv_logos.get(key) or []
        if not cands:
            return None
        want = region if region in ("US", "UK", "CA", "IE") else None
        if want:
            scoped = [c for c in cands if c[0] == want]
            if scoped:
                scoped.sort(key=lambda x: x[2], reverse=True)
                return scoped[0][1]
        # unique url across countries?
        urls = list(dict.fromkeys(c[1] for c in cands))
        if len(urls) == 1:
            return urls[0]
        if not want:
            cands = sorted(cands, key=lambda x: x[2], reverse=True)
            return cands[0][1]
        return None

    for key in keys:
        url = org_pick(key)
        if url:
            return url, "iptv-org"
    for key in name_keys(name, keep_feed=False):
        url = org_pick(key)
        if url:
            return url, "iptv-org"

    if playlist_logo and is_safe_logo_url(playlist_logo):
        return playlist_logo, "playlist"
    return "", "none"


def write_xmltv(path: Path, channels: list[dict], progs_by_id: dict[str, list[dict]],
                generator: str) -> None:
    # Write unique programmes per id; one <channel> per playlist row.
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
        f.write(f'<tv generator-info-name="{escape(generator)}">\n')
        for ch in channels:
            f.write(f'  <channel id={quoteattr(ch["id"])}>\n')
            f.write(f'    <display-name>{escape(ch["name"])}</display-name>\n')
            if ch.get("category"):
                f.write(f'    <display-name>{escape(ch["category"])}</display-name>\n')
            if ch.get("logo"):
                f.write(f'    <icon src={quoteattr(ch["logo"])}/>\n')
            f.write("  </channel>\n")
        written = set()
        count = 0
        for ch in channels:
            cid = ch["id"]
            if cid in written:
                continue
            written.add(cid)
            items = progs_by_id.get(cid) or []
            items.sort(key=lambda p: p["start"])
            for p in items:
                f.write(
                    f'  <programme start={quoteattr(p["start_s"])} '
                    f'stop={quoteattr(p["stop_s"])} channel={quoteattr(cid)}>\n'
                )
                f.write(f'    <title>{escape(p["title"])}</title>\n')
                if p.get("sub"):
                    f.write(f'    <sub-title>{escape(p["sub"])}</sub-title>\n')
                if p.get("desc"):
                    f.write(f'    <desc>{escape(p["desc"])}</desc>\n')
                for cat in p.get("cats") or []:
                    f.write(f'    <category>{escape(cat)}</category>\n')
                if p.get("date"):
                    f.write(f'    <date>{escape(p["date"])}</date>\n')
                if p.get("episode"):
                    sys_attr = f' system={quoteattr(p["ep_sys"])}' if p.get("ep_sys") else ""
                    f.write(f'    <episode-num{sys_attr}>{escape(p["episode"])}</episode-num>\n')
                f.write("  </programme>\n")
                count += 1
        f.write("</tv>\n")
    log(f"Wrote {path} ({path.stat().st_size:,} bytes, {count:,} programme rows)")


def make_placeholders(name: str, cid: str, start: datetime, end: datetime) -> list[dict]:
    """2-hour blocks titled with the channel name."""
    # Align to a 2-hour boundary in Chicago time
    local = start.astimezone(TZ)
    hour = (local.hour // 2) * 2
    cur = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    end_l = end.astimezone(TZ)
    out = []
    while cur < end_l:
        nxt = cur + timedelta(hours=2)
        out.append({
            "channel": cid,
            "start_s": cur.strftime("%Y%m%d%H%M%S %z"),
            "stop_s": nxt.strftime("%Y%m%d%H%M%S %z"),
            "start": cur,
            "stop": nxt,
            "title": name,
            "sub": "",
            "desc": "No guide data",
            "date": "",
            "cats": [],
            "episode": "",
            "ep_sys": "",
            "placeholder": True,
        })
        cur = nxt
    return out


def validate_xml(path: Path) -> tuple[int, int]:
    n_ch = n_pr = 0
    for tag, elem in iterparse_xmltv(path):
        if tag == "channel":
            n_ch += 1
        elif tag == "programme":
            n_pr += 1
    return n_ch, n_pr


# epgshare01 US ids. "FS1 HD" and "FS1 Fox Sports 1 HD" carry the same
# schedule; the Fox Sports 1 HD id is the one we publish.
FS_PUBLIC = {
    1: ("epgshare-us", "FS1.Fox.Sports.1.HD.us2"),
    2: ("epgshare-us", "FS2.Fox.Sports.2.HD.us2"),
}
MOVIE_CATEGORIES = {
    "USA Movies Channels",
    "UK: Movies",
    "CA: Canada Cinema",
    "24/7 Movie Series",
    "24/7 Movie Categories",
}
_PREMIUM_MOVIE = re.compile(
    r"\b(?:hbo|cinemax|showtime|starz|encore|mgm\+|mgm plus|\bmgm\b|"
    r"movie channel|\btmc\b|\btcm\b|\bamc\b|\bifc\b|sundance|\bfxm\b|"
    r"hallmark|lifetime movie|sony movie|\bflix\b|sky cinema|sky movies|film4|"
    r"epix)\b",
    re.I,
)
_MOVIE_SWAPS = (
    ("fxm", "fx movie channel"),
    ("tcm", "turner classic movies"),
    ("epix hits", "mgm plus hits"),
    ("epix", "mgm plus"),
    ("movie channel xtra", "movie channel extra"),
    ("hallmark movies and mysteries", "hallmark mystery"),
    ("hbo drama", "hbo signature"),
    ("hbo hits", "hbo 2"),
    ("hbo movies", "hbo zone"),
    ("paramount plus with showtime", "showtime"),
    ("star z", "starz"),
    ("action max", "cinemax action"),
    ("5 star max", "cinemax classics"),
    ("more max", "cinemax hits"),
    ("outer max", "cinemax"),
    ("thriller max", "cinemax"),
)


def linear_fox_sports(name: str) -> int | None:
    """1 or 2 for the FS1/FS2 network itself, not an event slot that mentions it."""
    folded = normalize_name(name, keep_feed=True)
    if folded in ("fox sports 1", "fs 1"):
        return 1
    if folded in ("fox sports 2", "fs 2"):
        return 2
    return None


# Exact normalized names of linear networks. Event slots that merely mention
# the network ("FS1: NCAAB ...") are longer and are not in this map.
FORCED_LINEAR = {
    "fox sports 1": ("epgshare-us", "FS1.Fox.Sports.1.HD.us2"),
    "fs 1": ("epgshare-us", "FS1.Fox.Sports.1.HD.us2"),
    "fox sports 2": ("epgshare-us", "FS2.Fox.Sports.2.HD.us2"),
    "fs 2": ("epgshare-us", "FS2.Fox.Sports.2.HD.us2"),
    "cbs sports network": ("epgshare-us", "CBS.Sports.Network.HD.us2"),
    "cbs sports hq": ("epgshare-us", "CBS.Sports.HQ.us2"),
    "espn u": ("epgshare-us", "ESPNU.HD.us2"),
    "espnu": ("epgshare-us", "ESPNU.HD.us2"),
    "espn 2": ("epgshare-us", "ESPN2.HD.us2"),
    "espn2": ("epgshare-us", "ESPN2.HD.us2"),
    "espn news": ("epgshare-us", "ESPNEWS.HD.us2"),
    "espnews": ("epgshare-us", "ESPNEWS.HD.us2"),
    "sec network": ("epgshare-us", "SEC.Network.HD.us2"),
    "acc network": ("epgshare-us", "ACC.Network.us2"),
    "big ten network": ("epgshare-us", "Big.Ten.Network.HD.us2"),
    "btn": ("epgshare-us", "Big.Ten.Network.HD.us2"),
    "golf channel": ("epgshare-us", "Golf.Channel.HD.us2"),
    "fox news": ("epgshare-us", "Fox.News.Channel.HD.us2"),
    "fox news channel": ("epgshare-us", "Fox.News.Channel.HD.us2"),
    "fox business": ("epgshare-us", "Fox.Business.HD.us2"),
    "fox business network": ("epgshare-us", "Fox.Business.HD.us2"),
    "cnbc": ("epgshare-us", "CNBC.HD.us2"),
    "espn": ("epgshare-us", "ESPN.HD.us2"),
}


def forced_linear(name: str) -> tuple[str, str] | None:
    key = normalize_name(name, keep_feed=False)
    key = _BACKUP_RE.sub(" ", key)
    key = SPACE_RE.sub(" ", key).strip()
    return FORCED_LINEAR.get(key)


def provider_slot(ch: dict) -> bool:
    """24/7 or PPV with no linear channel behind the name. Those may stay placeholders."""
    name = ch.get("name") or ""
    cat = ch.get("category") or ""
    blob = f"{cat} {name}"
    if re.search(r"24\s*/\s*7|24-7", blob, re.I):
        return True
    if re.search(r"\bppv\b|pay[\s-]*per[\s-]*view", blob, re.I):
        return True
    if EVENT_NAME_RE.search(name) and not (ch.get("calls") or extract_call_signs(name)):
        return True
    return False


def broadcast_network(ch: dict) -> str | None:
    """ABC/NBC/CBS/FOX/CW/PBS/MyNetwork for a US local station, else None."""
    name = ch.get("name") or ""
    cat = ch.get("category") or ""
    if national_cable(name):
        return None
    if re.search(r"\b(?:FOX NEWS|FOX SPORTS|FOX BUSINESS|FS1|FS2)\b", name, re.I):
        return None
    m = re.search(r"USA Local\s*-\s*(NBC|ABC|CBS|FOX|CW|PBS|MY)", cat, re.I)
    if m:
        token = m.group(1).lower()
        return "mynet" if token == "my" else token
    calls = ch.get("calls") or extract_call_signs(name)
    looks_local = cat.startswith("USA Local") or calls or bool(
        re.search(r"\b(?:CW\d*|FOX\s*NET|MY\s*\d|MYNET)\b", name, re.I))
    if not looks_local:
        return None
    blob = name.upper()
    if re.search(r"\bPBS\b", blob):
        return "pbs"
    if re.search(r"\bCW\d*\b", blob):
        return "cw"
    if re.search(r"\bMY\b|\bMYNET\b|\bMY(?:TV|NET|NETWORK)\b", blob):
        return "mynet"
    if re.search(r"\bABC\b", blob):
        return "abc"
    if re.search(r"\bNBC\b", blob) and "NEWS NOW" not in blob:
        return "nbc"
    if re.search(r"\bCBS\b", blob) and "SPORTS" not in blob:
        return "cbs"
    if re.search(r"\bFOX\b", blob) or re.search(r"\bFOX\s*NET\b", blob):
        return "fox"
    if cat.startswith("USA Local") and calls:
        return ""
    return None


def canada_network(ch: dict) -> str | None:
    if ch.get("region") != "CA" or provider_slot(ch):
        return None
    name = ch.get("name") or ""
    if re.search(r"news network|\bnews\b|\bsportsnet\b|\btsn\b|comedy|documentary", name, re.I):
        return None
    if "news" in (ch.get("tvg_id") or "").lower():
        return None
    if re.search(r"\bCBC\b", name):
        return "cbc"
    if re.search(r"\bCTV\b", name) and "NEWS" not in name.upper():
        return "ctv"
    if re.search(r"\bGLOBAL\b", name, re.I):
        return "global"
    if re.search(r"\bCITY(?:TV| TV)\b", name, re.I):
        return "city"
    return None


_US_CANON_CALL = {
    "abc": "WABC", "nbc": "WNBC", "cbs": "WCBS", "fox": "WNYW",
    "cw": "WPIX", "pbs": "WNET", "mynet": "WWOR",
}
_CA_CANON_KEYS = {
    "cbc": ("cbc toronto hd", "cbc toronto"),
    "ctv": ("ctv toronto hd", "ctv toronto"),
    "global": ("global toronto hd", "global toronto"),
    "city": ("citytv toronto hd", "citytv toronto", "city tv toronto"),
}
_KEEP_SHOW = re.compile(
    r"nightly news|evening news|world news tonight|\btoday\b|good morning|"
    r"meet the press|face the nation|this week|dateline|60 minutes|20/20|"
    r"nightline|sunday today|football night|\bnba\b|\bnfl\b|\bnhl\b|\bmlb\b|"
    r"college|olympics",
    re.I,
)
_LOCAL_NEWS = re.compile(
    r"\bnews\b|\bnewscast\b|eyewitness|action news|newscenter|first alert|"
    r"\d+\s*news|news\s*\d+",
    re.I,
)


def _copy_feed(ch: dict, incoming: list[dict], *, local_news: bool) -> list[dict]:
    copied = []
    for prog in incoming:
        item = dict(prog)
        item["channel"] = ch["id"]
        title = (item.get("title") or "").strip()
        if local_news and _LOCAL_NEWS.search(title) and not _KEEP_SHOW.search(title):
            item["title"] = "Local News"
            item["desc"] = "Local news for this market."
            item["sub"] = ""
        elif not (item.get("desc") or "").strip():
            item["desc"] = title or "Local programming"
        copied.append(item)
    return copied


def _dt_id(cands: list[str]) -> str | None:
    full = [c for c in cands if re.search(r"-DT(?:\.|$)", c, re.I)
            and not re.search(r"-DT[2-9]", c, re.I)]
    return full[0] if full else (cands[0] if len(cands) == 1 else None)


def is_movie_channel(ch: dict) -> bool:
    if (ch.get("category") or "") in MOVIE_CATEGORIES:
        return True
    return bool(_PREMIUM_MOVIE.search(ch.get("name") or ""))


def _real_programmes(ch: dict, items: list[dict]) -> int:
    name = (ch.get("name") or "").strip()
    count = 0
    for prog in items or []:
        if prog.get("placeholder"):
            continue
        title = (prog.get("title") or "").strip()
        if not title or title in ("No guide data", "No event scheduled") or title == name:
            continue
        count += 1
    return count


def _movie_queries(name: str) -> list[str]:
    seeds = [normalize_name(name, keep_feed=False), normalize_name(name, keep_feed=True)]
    out: list[str] = []

    def add(text: str) -> None:
        text = SPACE_RE.sub(" ", text or "").strip()
        if text and text not in out:
            out.append(text)

    for seed in seeds:
        add(seed)
        for src, dst in _MOVIE_SWAPS:
            if src in seed:
                add(seed.replace(src, dst))
        add(seed.replace("family zone", "familyzone"))
        if seed in ("sundance", "sundance tv"):
            add("sundancetv")
            add("sundance tv")
        if "action max" in seed:
            add("cinemax action")
        if "encore edge" in seed:
            add("starz edge")
        if "hallmark drama" in seed or "hallmark family" in seed:
            add("hallmark family")
        if seed.startswith("epix 2") or seed.startswith("epix2"):
            add("mgm plus hits")
        if seed.startswith("tcm"):
            add("tcm movies")
    return out


def _feed_score(playlist_name: str, source_names: list[str]) -> int:
    blob = " ".join(source_names).lower()
    want_west = bool(re.search(r"west|pacific", playlist_name, re.I))
    is_west = bool(re.search(r"west|pacific", blob))
    score = 5 if want_west == is_west else -4
    if re.search(r"\bhd\b", blob):
        score += 1
    # Prefer the shorter, less-qualified feed when coasts agree.
    score -= min(len(blob), 80) // 40
    return score


def find_movie_source(ch: dict, indexes: list) -> tuple[str, str] | None:
    """Pick one public channel for a movie network, respecting east/west."""
    if ch["region"] == "UK":
        order = sorted(indexes, key=lambda ix: ix.kind not in ("uk", "ie"))
    elif ch["region"] == "CA":
        order = sorted(indexes, key=lambda ix: ix.kind != "ca")
    else:
        order = sorted(indexes, key=lambda ix: ix.kind not in ("us", "us-sports"))
    best: tuple[int, str, str] | None = None
    for ix in order:
        for key in _movie_queries(ch["name"]):
            ids = ix.unique(list(ix.by_norm.get(key) or []) + list(ix.by_norm_nofeed.get(key) or []))
            for sid in ids:
                score = _feed_score(ch["name"], ix.by_id[sid]["names"] or [sid])
                if best is None or score > best[0]:
                    best = (score, ix.name, sid)
    if best is None or best[0] < 0:
        return None
    return best[1], best[2]


def fill_remaining_gaps(playlist, progs, indexes, public_progs, path_by_name,
                       window_start, window_end) -> tuple[int, int, list[tuple[str, str, str]]]:
    """Pull a public schedule for any real channel that is still empty.

    24/7 and PPV slots are filled only when a public channel has that exact
    name. Locals with no station listing get the network feed, with local
    news labeled "Local News".
    """
    locals_ix = next((ix for ix in indexes if ix.kind == "us-locals"), None)
    ca_ix = next((ix for ix in indexes if ix.kind == "ca"), None)
    us_canon: dict[str, tuple[str, str]] = {}
    if locals_ix is not None:
        for net, call in _US_CANON_CALL.items():
            sid = _dt_id(list(locals_ix.by_call.get(call) or []))
            if sid:
                us_canon[net] = (locals_ix.name, sid)
    ca_canon: dict[str, tuple[str, str]] = {}
    if ca_ix is not None:
        for net, keys in _CA_CANON_KEYS.items():
            for key in keys:
                ids = ca_ix.unique(list(ca_ix.by_norm.get(key) or []))
                if not ids:
                    continue
                hd = [sid for sid in ids if ".HD." in sid or sid.endswith(".HD.ca2")]
                ca_canon[net] = (ca_ix.name, (hd or ids)[0])
                break

    gained = 0
    fed = 0
    plans: list[tuple[dict, str, str, str]] = []
    for ch in playlist:
        if _real_programmes(ch, progs.get(ch["id"]) or []):
            continue
        if forced_linear(ch["name"]):
            continue
        found = None
        failed = ch.get("public_match") or ""
        if provider_slot(ch):
            key = normalize_name(ch["name"], keep_feed=False)
            key = re.sub(r"^(?:24\s*/\s*7|24-7|24 7)\s+", "", key).strip()
            hits = []
            if len(key) >= 4:
                for ix in indexes:
                    for sid in ix.unique(list(ix.by_norm.get(key) or [])
                                         + list(ix.by_norm_nofeed.get(key) or [])):
                        if not is_affiliate_source(sid):
                            hits.append((ix.name, sid))
            if len({sid for _n, sid in hits}) == 1 and hits:
                found = hits[0]
        else:
            ordered = indexes
            if ch["region"] == "UK":
                ordered = [ix for ix in indexes if ix.kind in ("uk", "ie")] + \
                          [ix for ix in indexes if ix.kind not in ("uk", "ie")]
            elif ch["region"] == "CA":
                ordered = [ix for ix in indexes if ix.kind == "ca"] + \
                          [ix for ix in indexes if ix.kind != "ca"]
            for ix in ordered:
                mid = match_in_index(ch["name"], ch.get("tvg_id") or "", ch.get("calls") or set(),
                                     ix, allow_fast=ix.kind == "fast")
                if mid and national_cable(ch["name"]) and is_affiliate_source(mid):
                    continue
                if mid and f"{ix.name}:{mid}" == failed:
                    continue
                if mid:
                    found = (ix.name, mid)
                    break
        if found:
            plans.append((ch, found[0], found[1], "public"))
            continue
        net = broadcast_network(ch)
        canon = us_canon.get(net or "") if net else None
        kind = "us-feed"
        if not canon:
            cnet = canada_network(ch)
            canon = ca_canon.get(cnet or "") if cnet else None
            kind = "ca-feed"
            net = cnet
        if canon and (net or (ch.get("category") or "").startswith("USA Local")):
            plans.append((ch, canon[0], canon[1], kind))
            continue
        if (ch.get("category") or "").startswith("USA Local") or net:
            calls = ", ".join(sorted(ch.get("calls") or [])) or "no call sign"
            if net:
                ch["gap_reason"] = (
                    f"no public listing for {calls}; national {net.upper()} feed not in the sources"
                )
            else:
                ch["gap_reason"] = f"no public listing for {calls} and the network is not ABC/NBC/CBS/FOX/CW/PBS/MyNetwork"
        elif canada_network(ch):
            ch["gap_reason"] = "no public Canadian listing and no national CBC/CTV/Global/City feed"
    needed: dict[str, set[str]] = defaultdict(set)
    for _ch, sname, sid, _kind in plans:
        if sid not in public_progs.get(sname, {}):
            needed[sname].add(sid)
    for sname, ids in needed.items():
        path = path_by_name.get(sname)
        bucket = public_progs.setdefault(sname, {})
        if not path:
            for sid in ids:
                bucket.setdefault(sid, [])
            continue
        fetched = collect_programmes(path, ids, window_start, window_end)
        bucket.update(fetched)
        for sid in ids:
            bucket.setdefault(sid, [])
    for ch, sname, sid, kind in plans:
        incoming = (public_progs.get(sname) or {}).get(sid) or []
        if not incoming:
            calls = ", ".join(sorted(ch.get("calls") or [])) or "no call sign"
            ch["gap_reason"] = f"public source {sname}:{sid} had no programmes in the window ({calls})"
            continue
        if kind == "public":
            progs[ch["id"]] = [dict(p) for p in incoming]
            ch["epg_source"] = sname
            ch["public_match"] = f"{sname}:{sid}"
            gained += 1
        else:
            progs[ch["id"]] = _copy_feed(ch, incoming, local_news=(kind == "us-feed"))
            ch["epg_source"] = "network-feed"
            ch["gap_reason"] = ""
            fed += 1
    empty: list[tuple[str, str, str]] = []
    for ch in playlist:
        cat = ch.get("category") or ""
        is_local = cat.startswith("USA Local") or bool(broadcast_network(ch)) or bool(canada_network(ch))
        if not is_local:
            continue
        if _real_programmes(ch, progs.get(ch["id"]) or []):
            continue
        empty.append((ch["name"], ch["id"], ch.get("gap_reason") or "no programmes after every public source"))
    return gained, fed, empty


def ensure_local_descriptions(playlist, progs) -> int:
    """Every local station gets a description on each programme."""
    stations = 0
    seen: set[str] = set()
    for ch in playlist:
        cid = ch["id"]
        if cid in seen:
            continue
        cat = ch.get("category") or ""
        if not (cat.startswith("USA Local") or broadcast_network(ch) or canada_network(ch)):
            continue
        seen.add(cid)
        changed = False
        for prog in progs.get(cid) or []:
            if not (prog.get("desc") or "").strip():
                prog["desc"] = (prog.get("title") or "").strip() or "Local programming"
                changed = True
        if changed:
            stations += 1
    return stations


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="Build a custom Kemo XMLTV guide")
    ap.add_argument("--cache", type=Path, default=root / "cache")
    ap.add_argument("--outdir", type=Path, default=root)
    ap.add_argument("--playlist", type=Path,
                    default=root / "data" / "kemo-epg-channels.xml")
    ap.add_argument("--kemo-feed", type=Path,
                    default=root / "data" / "kemo-epg-live.xml")
    ap.add_argument("--channels-csv", type=Path,
                    default=root / "data" / "kemo-full-channels.csv")
    ap.add_argument("--refresh", action=argparse.BooleanOptionalAction, default=True,
                    help="Re-download public XMLTV and logo sources (default: on)")
    ap.add_argument("--keep-xml", action="store_true",
                    help="Keep the uncompressed XML (over GitHub's 100 MB limit)")
    ap.add_argument("--days", type=float, default=7, help="Days of guide to keep ahead of now (default 7)")
    args = ap.parse_args(argv)

    cache: Path = args.cache
    outdir: Path = args.outdir
    cache.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(TZ)
    window_start = now - timedelta(hours=3)
    window_end = now + timedelta(hours=int(args.days * 24))
    log(f"Window {window_start.isoformat()} → {window_end.isoformat()} (America/Chicago)")

    log("Loading playlist channels")
    playlist = load_playlist(args.playlist)
    resolve_tivi_ids(playlist)
    n_name_ids = sum(1 for ch in playlist if ch["id"] == ch["name"])
    n_tvg_ids = len(playlist) - n_name_ids
    log(f"  {len(playlist):,} playlist channels "
        f"({n_tvg_ids:,} using tvg-id, {n_name_ids:,} using exact name as id)")
    if not playlist:
        log("ERROR: empty playlist")
        return 1
    check_playlist_csv(args.channels_csv, playlist)

    # --- logos ---
    for fname, url in LOGO_ASSETS:
        try:
            ensure_file(cache, fname, url, args.refresh)
        except Exception as e:
            log(f"WARNING: could not fetch {fname}: {e}")

    tv_logos = {}
    iptv_logos = {}
    tv_path = cache / "tv-logos-tree.json"
    org_ch = cache / "iptv-org-channels.json"
    org_lg = cache / "iptv-org-logos.json"
    if tv_path.exists():
        log("Indexing tv-logos")
        tv_logos = load_tv_logos(tv_path)
        log(f"  {len(tv_logos):,} normalized logo stems")
    if org_ch.exists() and org_lg.exists():
        log("Indexing iptv-org logos")
        iptv_logos = load_iptv_org_logos(org_ch, org_lg)
        log(f"  {len(iptv_logos):,} normalized names with logos")

    for ch in playlist:
        url, src = pick_logo(ch["name"], ch["region"], tv_logos, iptv_logos, ch["playlist_logo"])
        ch["logo"] = url
        ch["logo_source"] = src

    # --- Kemo provider feed first ---
    kemo_names: dict[str, str] = {}  # lower name -> kemo channel id
    kemo_all: dict[str, list[dict]] = {}
    if args.kemo_feed.exists():
        log(f"Parsing Kemo feed {args.kemo_feed}")
        for tag, elem in iterparse_xmltv(args.kemo_feed, stop_after_channels=True):
            if tag == "channel":
                rec = channel_from_elem(elem)
                for n in rec["names"]:
                    kemo_names[fold(n).strip().lower()] = rec["id"]
        log(f"  {len(kemo_names):,} Kemo display-name keys")
        kemo_all = collect_programmes_all(args.kemo_feed, window_start, window_end)
        before = sum(len(v) for v in kemo_all.values())
        kemo_all, dropped_past = drop_finished_provider_programmes(kemo_all, now)
        kept_n = sum(len(v) for v in kemo_all.values())
        log(f"  provider snapshot in window: {before:,}; "
            f"kept {kept_n:,} with start still in the future "
            f"(dropped {dropped_past:,} already started) across {len(kemo_all):,} ids")
    else:
        log("WARNING: provider snapshot not found; building from public sources only")

    # Match playlist → kemo
    progs: dict[str, list[dict]] = defaultdict(list)
    epg_source: dict[str, str] = {}  # playlist unique key (idx) later; use by row
    for i, ch in enumerate(playlist):
        ch["epg_source"] = "none"
        ch["kemo_match"] = None
        mid = None
        # Only use an id match when we actually kept that id for this row
        # (junk shared tvg-ids are rewritten to the exact name).
        if ch["id"] != ch["name"] and ch["id"] in kemo_all:
            id_calls = extract_call_signs("", ch["id"])
            name_calls = ch.get("name_calls") or set()
            # Unique typo ids are kept for TiviMate (nbcwsmw.us, cwwztv.us)
            # but must not pull Kemo's slot when the id's call sign is a
            # different station than the playlist name.
            if name_calls and id_calls and not (name_calls & id_calls):
                mid = None
            else:
                mid = ch["id"]
        if mid is None:
            key = fold(ch["name"]).strip().lower()
            if key in kemo_names and kemo_names[key] in kemo_all:
                mid = kemo_names[key]
        if mid:
            id_calls = extract_call_signs("", mid)
            name_calls = ch.get("name_calls") or set()
            if name_calls and id_calls and not (name_calls & id_calls):
                mid = None
        if mid:
            ch["kemo_match"] = mid
            for p in kemo_all[mid]:
                add_programme(progs[ch["id"]], p)
            if progs[ch["id"]]:
                ch["epg_source"] = "kemo"

    # --- public sources ---
    # Download / locate
    source_files: list[tuple[str, Path, str]] = []
    for sname, fname, url, kind in SOURCES:
        try:
            p = ensure_file(cache, fname, url, args.refresh)
            source_files.append((sname, p, kind))
        except Exception as e:
            log(f"WARNING: skipping {sname}: {e}")

    # Channel indexes (skip programmes)
    indexes: list[SourceIndex] = []
    for sname, path, kind in source_files:
        log(f"Indexing channels in {sname}")
        try:
            indexes.append(load_source_channels(path, sname, kind))
            indexes[-1].path = path  # type: ignore
        except Exception as e:
            log(f"WARNING: failed to index {sname}: {e}")

    # Match remaining / also find a public match to extend Kemo horizon
    # Map: source_name -> source_channel_id -> list of playlist ids that want its programmes
    need: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for ch in playlist:
        name, tvg, calls = ch["name"], ch["tvg_id"], ch["calls"]
        # Prefer locals for US call-sign channels first, then country source, etc.
        ordered = indexes
        if ch["region"] == "UK":
            ordered = [ix for ix in indexes if ix.kind in ("uk", "ie")] + \
                      [ix for ix in indexes if ix.kind not in ("uk", "ie")]
        elif ch["region"] == "CA":
            ordered = [ix for ix in indexes if ix.kind == "ca"] + \
                      [ix for ix in indexes if ix.kind != "ca"]
        elif ch["region"] == "US":
            if calls:
                ordered = [ix for ix in indexes if ix.kind == "us-locals"] + \
                          [ix for ix in indexes if ix.kind != "us-locals"]
            else:
                ordered = [ix for ix in indexes if ix.kind in ("us", "us-sports")] + \
                          [ix for ix in indexes if ix.kind not in ("us", "us-sports")]
        elif ch["region"] == "IE":
            ordered = [ix for ix in indexes if ix.kind in ("ie", "uk")] + \
                      [ix for ix in indexes if ix.kind not in ("ie", "uk")]

        picked = None
        picked_ix = None
        forced = forced_linear(name)
        if forced:
            source_name, source_id = forced
            for ix in indexes:
                if ix.name == source_name and source_id in ix.by_id:
                    picked, picked_ix = source_id, ix
                    break
        for ix in ordered:
            if picked:
                break
            allow_fast = ix.kind == "fast"
            mid = match_in_index(name, tvg, calls, ix, allow_fast=allow_fast)
            if mid and national_cable(name) and is_affiliate_source(mid):
                continue
            if mid:
                picked, picked_ix = mid, ix
                break
        if picked and picked_ix:
            need[picked_ix.name][picked].append(ch["id"])
            ch["public_match"] = f"{picked_ix.name}:{picked}"
        else:
            ch["public_match"] = ""

    # Collect programmes only for needed source channels
    public_progs: dict[str, dict[str, list[dict]]] = {}
    path_by_name = {sname: path for sname, path, kind in source_files}
    for sname, mapping in need.items():
        path = path_by_name.get(sname)
        if not path:
            continue
        log(f"Collecting programmes from {sname} for {len(mapping):,} matched source channels")
        public_progs[sname] = collect_programmes(path, set(mapping.keys()), window_start, window_end)

    # Fill gaps (and whole guide if no Kemo)
    for ch in playlist:
        pm = ch.get("public_match") or ""
        if not pm or ":" not in pm:
            continue
        sname, sid = pm.split(":", 1)
        incoming = (public_progs.get(sname) or {}).get(sid) or []
        # Forced linear networks (FS1, CBSSN, ESPNU, ...) replace a bad
        # affiliate match or an empty guide with the public channel.
        if forced_linear(ch["name"]) and incoming:
            progs[ch["id"]] = [dict(p) for p in incoming]
            ch["epg_source"] = sname
            continue
        added = 0
        for p in incoming:
            if add_programme(progs[ch["id"]], p):
                added += 1
        if added and ch["epg_source"] == "none":
            ch["epg_source"] = sname
        elif added and ch["epg_source"] == "kemo":
            ch["epg_source"] = f"kemo+{sname}"
        elif added and "+" not in ch["epg_source"] and ch["epg_source"] != sname:
            ch["epg_source"] = f"{ch['epg_source']}+{sname}"

    movie_channels = [ch for ch in playlist if is_movie_channel(ch)]
    movie_before = sum(1 for ch in movie_channels if _real_programmes(ch, progs.get(ch["id"]) or []))
    movie_filled = 0
    movie_missing: list[str] = []
    extra_need: dict[str, set[str]] = defaultdict(set)
    movie_plan: list[tuple[dict, str, str]] = []
    for ch in movie_channels:
        if _real_programmes(ch, progs.get(ch["id"]) or []):
            continue
        found = find_movie_source(ch, indexes)
        if not found:
            movie_missing.append(ch["name"])
            continue
        sname, sid = found
        extra_need[sname].add(sid)
        movie_plan.append((ch, sname, sid))
    for sname, ids in extra_need.items():
        path = path_by_name.get(sname)
        if not path:
            continue
        already = public_progs.get(sname)
        if already is None:
            public_progs[sname] = {}
            already = public_progs[sname]
        missing_ids = {sid for sid in ids if sid not in already}
        if not missing_ids:
            continue
        fetched = collect_programmes(path, missing_ids, window_start, window_end)
        already.update(fetched)
    for ch, sname, sid in movie_plan:
        incoming = (public_progs.get(sname) or {}).get(sid) or []
        if not incoming:
            movie_missing.append(ch["name"])
            continue
        # Placeholder-only channels are replaced. Empty ones are filled.
        progs[ch["id"]] = [dict(p) for p in incoming]
        if ch["epg_source"] in ("none", "placeholder", ""):
            ch["epg_source"] = sname
        elif sname not in ch["epg_source"]:
            ch["epg_source"] = f"{ch['epg_source']}+{sname}"
        movie_filled += 1
    movie_after = sum(1 for ch in movie_channels if _real_programmes(ch, progs.get(ch["id"]) or []))
    log(f"  movie channels: {len(movie_channels):,}  real before {movie_before:,}  "
        f"real after {movie_after:,}  newly filled {movie_filled:,}  "
        f"still no source {len(movie_missing):,}")
    if movie_missing:
        shown = movie_missing[:40]
        log("  movie channels still without a public source: " + "; ".join(shown)
            + (f" (+{len(movie_missing) - len(shown)} more)" if len(movie_missing) > len(shown) else ""))

    gap_gained, gap_fed, empty_locals = fill_remaining_gaps(
        playlist, progs, indexes, public_progs, path_by_name, window_start, window_end)
    log(f"  gap fill: {gap_gained:,} real channels gained a public schedule; "
        f"{gap_fed:,} locals used a network feed; {len(empty_locals):,} locals still empty")

    log("Loading public scoreboards")
    games = sports_schedule.load_games(cache, window_start, window_end, args.refresh, log)
    sports_stats = sports_schedule.apply_sports_titles(playlist, progs, games)
    log(f"  retitled {sports_stats.retitled:,} generic sports programmes; "
        f"matchup sub-title on {sports_stats.subtitled:,} more")
    desc_samples = sports_schedule.apply_description_matchups(
        playlist, progs, games, cache, args.refresh, log)
    golf_samples = sports_schedule.apply_golf(
        playlist, progs, cache, args.refresh, log)
    local_desc_stations = ensure_local_descriptions(playlist, progs)
    log(f"  local stations that gained descriptions: {local_desc_stations:,}")

    # Event/PPV slots, then placeholders for other 24/7 channels with no data.
    n_placeholder_ch = 0
    ph_end = window_end
    for ch in playlist:
        if progs[ch["id"]]:
            continue
        blocks, source = sports_schedule.event_programmes(ch, games, now, ph_end)
        if blocks is not None:
            progs[ch["id"]] = blocks
            ch["epg_source"] = source
            if source == "event-slate":
                sports_stats.event_from_slate += 1
            else:
                sports_stats.event_from_name += 1
            game = next((p for p in blocks if not p.get("placeholder")), None)
            if game is not None and len(sports_stats.samples) < 16:
                when = game["start"].astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
                sports_stats.samples.append((ch["name"], when, game["title"]))
            continue
        if not ch["placeholder_eligible"]:
            continue
        blocks = make_placeholders(ch["name"], ch["id"], now, ph_end)
        progs[ch["id"]] = blocks
        ch["epg_source"] = "placeholder"
        n_placeholder_ch += 1
    sports_schedule.fill_league_packages(playlist, progs, games, now, ph_end, sports_stats)
    for cat, info in sports_stats.league_pack.items():
        log(f"  {cat}: {info['with_matchup']:,} of {info['channels']:,} channels have a matchup "
            f"({info['with_programmes']:,} have programmes)")
    sports_schedule.scrub_subtitles(progs)
    generic_left, sub_dupes = sports_schedule.count_listing_issues(playlist, progs)
    sports_stats.affiliate_generic_remaining = generic_left
    sports_stats.subtitle_duplicates = sub_dupes
    log(f"  ABC/NBC/CBS/FOX generic titles left Nov 25-29: {generic_left:,}; "
        f"title/sub-title duplicates: {sub_dupes:,}")

    # Inherit epg_source onto rows that share a legitimate id
    best_src: dict[str, str] = {}
    def src_rank(s: str) -> int:
        if s in ("none", "", None):
            return 0
        if s == "placeholder":
            return 1
        if "+" in s:
            return 3
        return 2
    for ch in playlist:
        src = ch["epg_source"]
        prev = best_src.get(ch["id"])
        if prev is None or src_rank(src) > src_rank(prev):
            best_src[ch["id"]] = src
    for ch in playlist:
        if ch["id"] in best_src and src_rank(best_src[ch["id"]]) > src_rank(ch["epg_source"]):
            ch["epg_source"] = best_src[ch["id"]]

    # Counts
    for ch in playlist:
        # programme_count is unique-id based; duplicate playlist ids share programmes
        ch["programme_count"] = len(progs.get(ch["id"]) or [])
        if ch["epg_source"] == "placeholder":
            continue
        # If this row shares an id with a row that already got a source, inherit
        if ch["epg_source"] == "none" and (progs.get(ch["id"])):
            # find a sibling later in reporting; leave as none only if empty
            pass

    # Write outputs
    xml_path = outdir / "kemo-custom-epg.xml"
    gz_path = outdir / "kemo-custom-epg.xml.gz"
    csv_path = outdir / "match-report.csv"
    sum_path = outdir / "SUMMARY.txt"

    coverage_table: list[tuple[str, int, int, int, int]] = []
    cov: dict[str, list[int]] = {}
    for ch in playlist:
        bucket = cov.setdefault(ch.get("category") or "(none)", [0, 0, 0, 0])
        bucket[0] += 1
        items = progs.get(ch["id"]) or []
        if _real_programmes(ch, items):
            bucket[1] += 1
        elif items:
            bucket[2] += 1
        else:
            bucket[3] += 1
    coverage_table = [(cat, n, real, ph, empty) for cat, (n, real, ph, empty) in cov.items()]
    n_alias = sports_schedule.add_playlist_id_aliases(playlist, progs)
    log(f"  also publishing {n_alias:,} playlist tvg-id/name aliases so TiviMate can match either")
    log("Writing XMLTV")
    write_xmltv(xml_path, playlist, progs, "Kemo custom EPG (snapshot)")

    log("Compressing")
    # mtime=0 so an unchanged guide produces the same gzip bytes.
    with open(xml_path, "rb") as inp, open(gz_path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as out:
            while True:
                chunk = inp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
    gz_size = gz_path.stat().st_size
    xml_size = xml_path.stat().st_size
    log(f"Wrote {gz_path} ({gz_size:,} bytes)")
    if gz_size >= 100 * 1024 * 1024:
        log("ERROR: compressed guide is at or above GitHub's 100 MB file limit")
        return 1

    log("Writing match-report.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["channel", "id", "epg_source", "logo_source", "programme_count"])
        for ch in playlist:
            w.writerow([ch["name"], ch["id"], ch["epg_source"], ch["logo_source"],
                        ch["programme_count"]])

    log("Validating gzip XMLTV well-formedness")
    try:
        n_ch, n_pr = validate_xml(gz_path)
    except Exception as e:
        log(f"ERROR: compressed guide is not valid XMLTV: {e}")
        return 1
    log(f"  well-formed: {n_ch:,} channels, {n_pr:,} programmes")
    if n_ch != len(playlist) or n_pr < 1:
        log(f"ERROR: expected {len(playlist):,} channels and at least one programme")
        return 1
    if not args.keep_xml:
        xml_path.unlink()
        log(f"Removed uncompressed XML ({xml_size:,} bytes; over GitHub's 100 MB limit)")

    # Stats
    def count_where(pred) -> int:
        return sum(1 for ch in playlist if pred(ch))

    real = [ch for ch in playlist if ch["epg_source"] not in ("none", "placeholder") and ch["programme_count"] > 0]
    # unique by id for programme totals already written once
    by_source: dict[str, int] = defaultdict(int)
    for ch in playlist:
        src = ch["epg_source"]
        # collapse kemo+x as both? report primary token + combo as its own
        by_source[src] += 1
    by_region_real: dict[str, int] = defaultdict(int)
    for ch in real:
        by_region_real[ch["region"]] += 1

    # Date range of real (non-placeholder) programmes
    real_starts = []
    real_stops = []
    for ch in playlist:
        if ch["epg_source"] in ("none", "placeholder"):
            continue
        for p in progs.get(ch["id"]) or []:
            if p.get("placeholder"):
                continue
            real_starts.append(p["start"])
            real_stops.append(p["stop"])
    min_start = min(real_starts).astimezone(TZ) if real_starts else None
    max_stop = max(real_stops).astimezone(TZ) if real_stops else None

    n_logo = count_where(lambda c: bool(c["logo"]))
    n_logo_tv = count_where(lambda c: c["logo_source"] == "tv-logos")
    n_logo_org = count_where(lambda c: c["logo_source"] == "iptv-org")
    n_logo_pl = count_where(lambda c: c["logo_source"] == "playlist")

    # Nashville locals
    nash_keys = ("WKRN", "WSMV", "WTVF", "WZTV", "WNAB", "WUXP", "WTNX")
    nash_lines = []
    for ch in playlist:
        u = ch["name"].upper()
        if any(k in u for k in nash_keys) and "PREDATORS" not in u and "NASHVILLE @" not in u \
                and not ch["name"].startswith("24/7") and "ESPN+" not in u and "NHL " not in u \
                and "Sportsnet+" not in ch["name"]:
            nash_lines.append(
                f"  {ch['name']}  id={ch['id']}  source={ch['epg_source']}  "
                f"progs={ch['programme_count']}  logo={ch['logo_source']}"
            )

    # Notable national gaps
    highlights = [
        "USA ESPN HD", "USA ESPN 2", "USA CNN", "USA FOX News", "USA NBC", "USA CBS",
        "USA ABC", "USA HBO", "UK: BBC One HD", "UK BBC One HD", "UK BBC Two HD",
        "UK BBC News", "UK Sky Sports Football", "UK Sky News", "CA CBC", "CA CTV",
        "CA TSN 1", "CA Sportsnet Ontario",
    ]
    # fuzzy highlight report
    highlight_lines = []
    for want in ["ESPN HD", "ESPN 2", "CNN", "FOX News Channel", "FOX News",
                 "BBC One", "BBC Two", "BBC News", "Sky Sports Football", "Sky News",
                 "ITV 1", "ITV1", "CBC Toronto", "CTV Toronto", "TSN 1", "TSN1",
                 "Sportsnet Ontario", "HBO HD", "HBO"]:
        hits = [ch for ch in playlist if normalize_name(ch["name"], keep_feed=True) ==
                normalize_name(want, keep_feed=True)
                or normalize_name(ch["name"], keep_feed=True).endswith(" " + normalize_name(want))]
        # keep it tighter: name contains the target as its core
        if not hits:
            hits = [ch for ch in playlist
                    if normalize_name(want, keep_feed=True) == normalize_name(ch["name"], keep_feed=True)]
        if hits:
            for ch in hits[:3]:
                highlight_lines.append(
                    f"  {ch['name']}  source={ch['epg_source']}  progs={ch['programme_count']}"
                )

    lines = []
    lines.append("Kemo custom EPG")
    lines.append(f"Built: {now.strftime('%A, %B %-d, %Y, %-I:%M %p')} America/Chicago")
    lines.append("Public XMLTV sources were re-downloaded for this run." if args.refresh
                 else "Public XMLTV sources were reused from cache.")
    lines.append("Provider snapshot programmes were kept only when their start is still in the future.")
    lines.append(f"Sports programmes retitled with a public matchup: {sports_stats.retitled:,}")
    lines.append(f"Sports programmes given a matchup sub-title: {sports_stats.subtitled:,}")
    lines.append("ABC/NBC/CBS/FOX generic titles still left "
                 f"(Nov 25-29): {sports_stats.affiliate_generic_remaining:,}")
    lines.append(f"Title/sub-title duplicates: {sports_stats.subtitle_duplicates:,}")
    lines.append(f"Movie channels: {len(movie_channels):,}  real before {movie_before:,}  "
                 f"real after {movie_after:,}  still no public source {len(movie_missing):,}")
    lines.append(f"Gap fill from public listings: {gap_gained:,} channels; "
                 f"network-feed locals: {gap_fed:,}")
    lines.append(f"Description/wider matchups: {len(desc_samples):,} samples "
                 f"(see log for the full added count)")
    lines.append(f"Local stations that gained a description: {local_desc_stations:,}")
    lines.append(f"Local stations still empty: {len(empty_locals):,}")
    if desc_samples:
        lines.append("Description fallback samples (before → after):")
        for sample_name, when, before, after in desc_samples[:8]:
            lines.append(f"  {when}  {before}  →  {after}")
            lines.append(f"    {sample_name}")
    if golf_samples:
        lines.append("Golf listings (before → after):")
        for sample_name, when, before_t, after_t, before_d, after_d in golf_samples[:6]:
            lines.append(f"  {when}  {before_t}  →  {after_t}")
            lines.append(f"    {sample_name}")
            lines.append(f"    desc: {after_d}")
    if empty_locals:
        lines.append("Local stations still empty:")
        for sample_name, sample_id, reason in empty_locals:
            lines.append(f"  {sample_name}  id={sample_id}  {reason}")
    lines.append("Coverage by playlist category (real = a programme that is not a placeholder):")
    lines.append(f"  {'category':<42} {'ch':>5} {'real':>5} {'ph':>5} {'empty':>5}")
    for cat, n_cat, n_real, n_ph, n_empty in sorted(coverage_table, key=lambda row: -row[1]):
        lines.append(f"  {cat[:42]:<42} {n_cat:5} {n_real:5} {n_ph:5} {n_empty:5}")
    if movie_missing:
        lines.append("Movie channels still without a public source:")
        for missing_name in movie_missing[:80]:
            lines.append(f"  {missing_name}")
        if len(movie_missing) > 80:
            lines.append(f"  (+{len(movie_missing) - 80} more)")
    lines.append(f"Event channels scheduled from the channel name: {sports_stats.event_from_name:,}")
    lines.append("Event channels filled from that day's public slate "
                 f"(best guess, game 1 on channel 01): {sports_stats.event_from_slate:,}")
    if sports_stats.samples:
        lines.append("Sample sports listings:")
        for name, when, title in sports_stats.samples:
            lines.append(f"  {when}  {title}")
            lines.append(f"    {name}")
    lines.append("")
    lines.append(f"Playlist channels: {len(playlist):,}")
    lines.append(f"XML channel elements (validated): {n_ch:,}")
    lines.append(f"Programme elements (validated): {n_pr:,}")
    lines.append(f"Channels with real programme data: {len(real):,}")
    lines.append("  By source:")
    for k, v in sorted(by_source.items(), key=lambda kv: (-kv[1], kv[0])):
        if k in ("none", "placeholder"):
            continue
        lines.append(f"    {k}: {v:,}")
    lines.append(f"  No programme data: {by_source.get('none', 0):,}")
    lines.append(f"  Placeholder (event/PPV/24/7, no real schedule): {by_source.get('placeholder', 0):,}")
    lines.append("  Real programme data by region (US / UK / CA / IE / OTHER):")
    for k in ("US", "UK", "CA", "IE", "OTHER"):
        lines.append(f"    {k}: {by_region_real.get(k, 0):,}")
    lines.append(f"Channels with a logo: {n_logo:,}  (tv-logos {n_logo_tv:,}, "
                 f"iptv-org {n_logo_org:,}, playlist {n_logo_pl:,})")
    if min_start and max_stop:
        lines.append(
            f"Real programme date range: {min_start.strftime('%Y-%m-%d %H:%M %Z')} "
            f"→ {max_stop.strftime('%Y-%m-%d %H:%M %Z')}"
        )
    lines.append("Files:")
    lines.append(f"  {gz_path}  ({gz_size:,} bytes)")
    if args.keep_xml:
        lines.append(f"  {xml_path}  ({xml_size:,} bytes)")
    else:
        lines.append(f"  uncompressed XML was {xml_size:,} bytes and was not kept")
    lines.append(f"  {csv_path}")
    lines.append("")
    lines.append("Nashville locals:")
    lines.extend(nash_lines or ["  (none found)"])
    lines.append("")
    lines.append("Selected networks:")
    lines.extend(highlight_lines or ["  (see match-report.csv)"])
    lines.append("")
    lines.append("Refresh: python3 scripts/build_epg.py")
    lines.append("  Public sources are re-downloaded unless --no-refresh is passed.")
    lines.append("EPG URL: https://raw.githubusercontent.com/erikrepp-hue/kemo-epg/main/kemo-custom-epg.xml.gz")
    text = "\n".join(lines) + "\n"
    sum_path.write_text(text, encoding="utf-8")
    print()
    print(text)
    return 0


def collect_programmes_all(path: Path, window_start: datetime, window_end: datetime) -> dict[str, list[dict]]:
    """Like collect_programmes but keeps every channel in the window."""
    out: dict[str, list[dict]] = defaultdict(list)
    n_keep = 0
    for tag, elem in iterparse_xmltv(path):
        if tag != "programme":
            continue
        prog = programme_from_elem(elem)
        if not prog or not prog["title"]:
            continue
        start, stop = prog["start"], prog["stop"]
        if stop is None:
            stop = start + timedelta(hours=1)
            prog["stop"] = stop
        if stop <= window_start or start >= window_end:
            continue
        out[prog["channel"]].append(prog)
        n_keep += 1
    log(f"  kept {n_keep:,} programmes from {path.name}")
    return out


# Fix circular: main references collect_programmes_all which is defined below; OK in Python.


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Interrupted")
        sys.exit(130)
