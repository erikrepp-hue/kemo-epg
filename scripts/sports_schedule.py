"""Match sports listings to free public ESPN scoreboards.

No credentials, secrets, or stream URLs. Scoreboards are the public
site.api.espn.com JSON feeds (broadcast names included).

Linear sports channels with a generic title such as "NBA Basketball" or
"Live: NHL Hockey" are retitled "NBA: Away Team at Home Team" when the
league, the network, and the start time (within 30 minutes) agree.
A programme that already has a specific title keeps it, and the matchup
is written to sub-title.

Numbered event / PPV channels are a best guess:
  * if the channel name contains a matchup or a start time, that is used
  * otherwise a single-league slot (NBA 01, NCAAF 03, ...) is lined up
    with that day's games in start-time order (game 1 on channel 01)
Programmes run from the listed start for three hours. The rest of the
window is "No event scheduled".
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
UA = "KemoCustomEPG/1.0 (personal XMLTV builder; +https://github.com/erikrepp-hue/kemo-epg)"

# sport, league path, groups, internal id, title label
LEAGUES = [
    ("basketball", "nba", None, "nba", "NBA"),
    ("football", "nfl", None, "nfl", "NFL"),
    ("hockey", "nhl", None, "nhl", "NHL"),
    ("baseball", "mlb", None, "mlb", "MLB"),
    ("basketball", "wnba", None, "wnba", "WNBA"),
    ("football", "college-football", "80", "ncaaf", "NCAAF"),
    ("basketball", "mens-college-basketball", "50", "ncaam", "NCAAB"),
    ("basketball", "womens-college-basketball", "50", "ncaaw", "NCAAW"),
    ("soccer", "usa.1", None, "mls", "MLS"),
    ("soccer", "eng.1", None, "epl", "EPL"),
]

# More specific ids are listed first. A later generic id is dropped when a
# specific one in the same family also matched.
_NET_RULES: list[tuple[str, str]] = [
    ("espn2", r"\bespn\s*2\b"),
    ("espnu", r"\bespnu\b|\bespn\s+u\b"),
    ("espn+", r"\bespn\s*\+|espn\s+plus\b"),
    ("espn-news", r"\bespn\s+news\b"),
    ("sec", r"\bsec\s+network\b|\bsecn\b|\bespn\s+sec\b"),
    ("acc", r"\bacc\s+network\b|\baccn\b"),
    ("bigten", r"\bbig\s+ten\b|\bbtn\b|\bb1g\b"),
    ("nba-tv", r"\bnba\s+tv\b"),
    ("nfl-net", r"\bnfl\s+network\b"),
    ("nhl-net", r"\bnhl\s+network\b"),
    ("mlb-net", r"\bmlb\s+network\b"),
    ("fs1", r"\bfs\s*1\b|\bfox\s+sports\s*1\b"),
    ("fs2", r"\bfs\s*2\b|\bfox\s+sports\s*2\b"),
    ("cbssn", r"\bcbs\s+sports\s+network\b|\bcbssn\b|\bcbs\s+sports\s+hq\b"),
    ("nbcs-ba", r"\bnbc\s+sports\s+(?:ba\b|bay\s+area)"),
    ("nbcs-ca", r"\bnbc\s+sports\s+cal"),
    ("nbcs-philly", r"\bnbc\s+sports\s+phil"),
    ("nbcs-chi", r"\bnbc\s+sports\s+chicago|\bchsn\b"),
    ("nbc-sports", r"\bnbc\s+sports\b"),
    ("fds-sun", r"\b(?:fanduel|bally)\s+(?:sports\s+)?(?:network\s+)?sun\b"),
    ("fds-south", r"\b(?:fanduel|bally)\s+(?:sports\s+)?(?:network\s+)?south\b(?!\s*west)"),
    ("fds-sw", r"\b(?:fanduel|bally)\s+(?:sports\s+)?(?:network\s+)?southwest\b"),
    ("fds-det", r"\b(?:fanduel|bally)\s+(?:sports\s+)?(?:network\s+)?detroit\b"),
    ("fds-mw", r"\b(?:fanduel|bally)\s+(?:sports\s+)?(?:network\s+)?midwest\b"),
    ("fds", r"\b(?:fanduel|bally)\s+sports\b"),
    ("yes", r"\byes\s+network\b|\bdazn\s+yes\b|(?:^|\s)yes(?:\s|$)"),
    ("altitude", r"\baltitude\b"),
    ("nesn", r"\bnesn\b"),
    ("msg", r"\bmsg\b"),
    ("sch", r"\bspace\s+city\b"),
    ("tnt", r"\btnt\b"),
    ("tbs", r"\btbs\b"),
    ("trutv", r"\btru\s*tv\b"),
    ("peacock", r"\bpeacock\b"),
    ("prime", r"\bprime\s+video\b|\bamazon\b"),
    ("espn", r"\bespn\b"),
    ("abc", r"\babc\b"),
    ("cbs", r"\bcbs\b"),
    ("nbc", r"\bnbc\b"),
    ("fox", r"\bfox\b"),
    ("tsn", r"\btsn\b"),
    ("sky", r"\bsky\s+sports\b"),
]

_DROP_GENERIC = {
    "espn": {"espn2", "espnu", "espn+", "espn-news", "sec"},
    "fox": {"fs1", "fs2"},
    "nbc": {"nbcs-ba", "nbcs-ca", "nbcs-philly", "nbcs-chi", "nbc-sports"},
    "cbs": {"cbssn"},
    "fds": {"fds-sun", "fds-south", "fds-sw", "fds-det", "fds-mw"},
}

# Whole-string aliases for the short names ESPN puts on broadcasts.
_EXACT = {
    "espn": "espn",
    "espn2": "espn2",
    "espn+": "espn+",
    "espnu": "espnu",
    "abc": "abc",
    "cbs": "cbs",
    "nbc": "nbc",
    "fox": "fox",
    "fs1": "fs1",
    "fs2": "fs2",
    "tnt": "tnt",
    "tbs": "tbs",
    "trutv": "trutv",
    "nba tv": "nba-tv",
    "nfl network": "nfl-net",
    "nhl network": "nhl-net",
    "mlb network": "mlb-net",
    "sec network": "sec",
    "secn": "sec",
    "acc network": "acc",
    "accn": "acc",
    "btn": "bigten",
    "big ten network": "bigten",
    "cbs sports network": "cbssn",
    "cbssn": "cbssn",
    "peacock": "peacock",
    "prime video": "prime",
    "amazon": "prime",
    "nesn": "nesn",
    "yes": "yes",
    "msg": "msg",
    "msg2": "msg",
    "msgb": "msg",
    "altitude": "altitude",
    "altitude sports": "altitude",
    "nbc sports ba": "nbcs-ba",
    "nbc sports california": "nbcs-ca",
    "nbc sports phil +": "nbcs-philly",
    "nbc sports philadelphia": "nbcs-philly",
    "nbc sports chicago": "nbcs-chi",
    "chsn": "nbcs-chi",
    "dazn yes": "yes",
    "dazn (yes)": "yes",
    "espn sec": "sec",
}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_TZN = r"(?:et|est|edt|ct|cst|cdt|mt|mst|mdt|pt|pst|pdt|utc|gmt)"
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)"
_TZMAP = {
    "et": ET, "est": ET, "edt": ET,
    "ct": TZ, "cst": TZ, "cdt": TZ,
    "mt": ZoneInfo("America/Denver"), "mst": ZoneInfo("America/Denver"),
    "mdt": ZoneInfo("America/Denver"),
    "pt": ZoneInfo("America/Los_Angeles"), "pst": ZoneInfo("America/Los_Angeles"),
    "pdt": ZoneInfo("America/Los_Angeles"),
    "utc": timezone.utc, "gmt": timezone.utc,
}

_STUDIO_RE = re.compile(
    r"scoreboard|pre-?game|post-?game|\btoday\b|\bfinal\b|highlights|stories|"
    r"preview|legends|mornings|goal\s*zone|\bcentral\b|podcast|\bshow\b|"
    r"\bstudio\b|\bwrap\b|bitesize|icons|reload|netbusters|fan'?s vote|"
    r"big interview|game\s*night|countdown|kickoff|\bpresented\b|"
    r"football saturday|saturday night'?s main",
    re.I,
)
_LIVE_RE = re.compile(r"^(?:live\s*[:\-]\s*|live\s+)", re.I)
_GENERIC_CORE = {
    "nba basketball": "nba",
    "nfl football": "nfl",
    "nhl hockey": "nhl",
    "mlb baseball": "mlb",
    "wnba": "wnba",
    "wnba basketball": "wnba",
    "college football": "ncaaf",
    "ncaa football": "ncaaf",
    "ncaaf": "ncaaf",
    "college basketball": "ncaam",
    "ncaa basketball": "ncaam",
    "ncaa men's basketball": "ncaam",
    "ncaa mens basketball": "ncaam",
    "ncaab": "ncaam",
    "women's college basketball": "ncaaw",
    "womens college basketball": "ncaaw",
    "ncaa women's basketball": "ncaaw",
    "ncaa womens basketball": "ncaaw",
    "mls soccer": "mls",
    "mls": "mls",
    "premier league": "epl",
    "premier league soccer": "epl",
    "epl": "epl",
    "english premier league": "epl",
}
_LEAGUE_IN_TITLE = [
    ("wnba", r"\bwnba\b"),
    ("nba-g", r"\bnba\s+g[\s-]*league\b"),
    ("nba", r"\bnba\b"),
    ("nfl", r"\bnfl\b|thursday night football|sunday night football|monday night football"),
    ("nhl", r"\bnhl\b"),
    ("mlb", r"\bmlb\b"),
    ("ncaaf", r"college football|ncaa football|\bncaaf\b"),
    ("ncaaw", r"women.?s college basketball|ncaa women|\bncaaw\b"),
    ("ncaam", r"college basketball|ncaa basketball|\bncaab\b|ncaa men"),
    ("mls", r"\bmls\b"),
    ("epl", r"premier league|\bepl\b"),
]
_SLATE_LEAGUE = {
    "nba": "nba",
    "nba g league": "nba-g",
    "nfl": "nfl",
    "nhl": "nhl",
    "mlb": "mlb",
    "wnba": "wnba",
    "ncaaf": "ncaaf",
    "ncaab": "ncaam",
}
_LABEL = {
    "nba": "NBA", "nba-g": "NBA G League", "nfl": "NFL", "nhl": "NHL",
    "mlb": "MLB", "wnba": "WNBA", "ncaaf": "NCAAF", "ncaam": "NCAAB",
    "ncaaw": "NCAAW", "mls": "MLS", "epl": "EPL",
}
_NUMBERED_EVENT = re.compile(
    r"(?i)\b(?:nba\s+g\s*league|nfl\s+sunday|ncaaf|ncaab|wnba|nba|nhl|mlb|nfl|"
    r"espn\s*\+|sportsnet\s*\+|flo\s+sports|pay\s+per\s+view|\bppv\b|"
    r"peacock|soccer)\s*\d{1,3}\b"
)
_LEAGUE_SLOT = re.compile(
    r"(?ix)(?:^|\b)"
    r"(?P<label>nba\s+g\s*league|ncaaf|ncaab|wnba|nba|nhl|mlb|nfl)"
    r"\s+(?P<num>\d{1,3})\s*:?\s*$"
)
_VS = re.compile(
    r"(?i)(?P<away>[A-Za-z0-9#.'`&\-][^@•|\n]{0,55}?)\s+(?:vs\.?|v\.)\s+"
    r"(?P<home>[A-Za-z0-9#.'`&\-][^@•|\n]{0,55})"
)
_AT = re.compile(
    r"(?i)(?P<away>[A-Za-z0-9#.'`&\-][^@•|\n]{0,45}?)\s+@\s+"
    r"(?P<home>[A-Za-z0-9#.'`&\-][^@•|\n]{0,45})"
)
_WORD_AT = re.compile(
    r"(?P<away>[A-Z][A-Za-z0-9.'`&\-]{0,24}(?:\s+[A-Z][A-Za-z0-9.'`&\-]{0,24}){0,3})"
    r"\s+at\s+"
    r"(?P<home>[A-Z][A-Za-z0-9.'`&\-]{0,24}(?:\s+[A-Z][A-Za-z0-9.'`&\-]{0,24}){0,4})"
)


@dataclass
class Game:
    league: str
    label: str
    start: datetime
    away: str
    home: str
    venue: str
    networks: set[str] = field(default_factory=set)

    @property
    def matchup(self) -> str:
        return f"{self.away} at {self.home}"

    @property
    def title(self) -> str:
        return f"{self.label}: {self.matchup}"


@dataclass
class NamedEvent:
    title: str
    start: datetime
    source: str  # "name" or "slate"


@dataclass
class SportsStats:
    games_loaded: int = 0
    retitled: int = 0
    subtitled: int = 0
    event_from_name: int = 0
    event_from_slate: int = 0
    samples: list[tuple[str, str, str]] = field(default_factory=list)


def _norm(text: str) -> str:
    raw = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    raw = raw.lower().replace("&", " and ")
    raw = re.sub(r"[^a-z0-9+]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def network_ids(text: str) -> set[str]:
    """Canonical network ids shared by playlist names and ESPN broadcast names."""
    key = _norm(text)
    if not key:
        return set()
    if re.search(r"\bfox news\b|\bcnn\b|\bmsnbc\b", key):
        return set()
    if key in _EXACT:
        return {_EXACT[key]}
    stripped = re.sub(r"\b(?:hd|sd|uhd|fhd|lhd|backup|east|west)\b", " ", key)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped in _EXACT:
        return {_EXACT[stripped]}
    found: set[str] = set()
    for nid, rx in _NET_RULES:
        if re.search(rx, key):
            found.add(nid)
    for generic, specifics in _DROP_GENERIC.items():
        if generic in found and found & specifics:
            found.discard(generic)
    if found & {"nbcs-ba", "nbcs-ca", "nbcs-philly", "nbcs-chi"}:
        found.discard("nbc-sports")
        found.discard("nbc")
    return found


def _core_title(title: str) -> str:
    text = _LIVE_RE.sub("", title or "").strip()
    text = re.sub(r"\s*\((?:live|hd|sd|uhd)\)\s*$", "", text, flags=re.I)
    text = re.sub(r"\s+\b(?:hd|sd|uhd|fhd)\b\s*$", "", text, flags=re.I)
    text = text.replace("'", "'").replace("`", "'")
    return re.sub(r"\s+", " ", text).strip(" :-")


def is_studio_show(title: str) -> bool:
    return bool(_STUDIO_RE.search(title or ""))


def generic_league(title: str) -> str | None:
    """League id when the title is only a generic game name, else None."""
    if not title or is_studio_show(title):
        return None
    core = _core_title(title).lower().replace("'", "'")
    return _GENERIC_CORE.get(core)


def title_league(title: str) -> str | None:
    if not title or is_studio_show(title):
        return None
    text = _core_title(title).lower()
    for league, rx in _LEAGUE_IN_TITLE:
        if re.search(rx, text):
            return league
    return None


def _xmltv(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%Y%m%d%H%M%S %z")


def _programme(cid: str, start: datetime, stop: datetime, title: str, desc: str,
               sub: str = "", *, placeholder: bool = False, event: bool = False) -> dict:
    return {
        "channel": cid,
        "start_s": _xmltv(start),
        "stop_s": _xmltv(stop),
        "start": start,
        "stop": stop,
        "title": title,
        "sub": sub,
        "desc": desc,
        "date": "",
        "cats": ["Sports"] if event and not placeholder else [],
        "episode": "",
        "ep_sys": "",
        "placeholder": placeholder,
        "event": event,
    }


def _year_for(month: int, day: int, now: datetime) -> int | None:
    year = now.year
    try:
        guess = datetime(year, month, day).date()
    except ValueError:
        return None
    delta = (guess - now.date()).days
    if delta < -200:
        year += 1
    elif delta > 200:
        year -= 1
    try:
        datetime(year, month, day)
    except ValueError:
        return None
    return year


def _hour12(hour, minute, ampm: str) -> tuple[int, int]:
    hour = int(hour)
    minute = int(minute or 0)
    if ampm.lower() == "pm" and hour != 12:
        hour += 12
    elif ampm.lower() == "am" and hour == 12:
        hour = 0
    return hour, minute


def _clock_on(hour: int, minute: int, ampm: str, tz, day, now: datetime,
              window_start: datetime, window_end: datetime) -> datetime | None:
    hour = int(hour)
    minute = int(minute or 0)
    if ampm.lower() == "pm" and hour != 12:
        hour += 12
    elif ampm.lower() == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    stop = start + timedelta(hours=3)
    if stop <= window_start or start >= window_end:
        return None
    return start


def _pick_clock(hour, minute, ampm, tz, now, window_start, window_end) -> datetime | None:
    """Clock time with no date: the occurrence closest to now that still matters."""
    best = None
    best_dist = None
    base = now.astimezone(tz)
    hh, mm = _hour12(hour, minute, ampm)
    if hh > 23 or mm > 59:
        return None
    for delta in range(-1, 5):
        day = (base + timedelta(days=delta)).date()
        start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
        stop = start + timedelta(hours=3)
        if stop <= now - timedelta(hours=6) or start >= window_end:
            continue
        dist = abs((start - now).total_seconds())
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = start
    return best


def extract_start(text: str, now: datetime, window_start: datetime,
                  window_end: datetime) -> tuple[datetime | None, str]:
    """Pull a start time out of a channel name. The second value is the name with that phrase removed."""
    # 25 Nov 05:00 PM ET
    m = re.search(
        rf"\b(\d{{1,2}})\s+({_MON})\.?\s+{_CLOCK}\s*({_TZN})\b",
        text, re.I,
    )
    if m:
        mon_key = re.match(r"[a-z]+", m.group(2).lower()).group(0)
        month = _MONTHS.get(mon_key[:4], _MONTHS.get(mon_key[:3]))
        day_n = int(m.group(1))
        year = _year_for(month, day_n, now) if month else None
        if year and month:
            hour, minute = _hour12(m.group(3), m.group(4), m.group(5))
            try:
                start = datetime(year, month, day_n, hour, minute, tzinfo=_TZMAP[m.group(6).lower()])
            except ValueError:
                start = None
            if start is not None:
                return start, (text[:m.start()] + " " + text[m.end():]).strip()

    # Nov 25 9:00AM ET
    m = re.search(
        rf"\b({_MON})\.?\s+(\d{{1,2}})\s+{_CLOCK}\s*({_TZN})\b",
        text, re.I,
    )
    if m:
        mon_key = re.match(r"[a-z]+", m.group(1).lower()).group(0)
        month = _MONTHS.get(mon_key[:4], _MONTHS.get(mon_key[:3]))
        year = _year_for(month, int(m.group(2)), now) if month else None
        if year and month:
            hour = int(m.group(3))
            minute = int(m.group(4) or 0)
            ampm = m.group(5).lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            start = datetime(year, month, int(m.group(2)), hour, minute,
                             tzinfo=_TZMAP[m.group(6).lower()])
            return start, (text[:m.start()] + " " + text[m.end():]).strip()

    # 11.25 1:00 PM ET
    m = re.search(rf"\b(\d{{1,2}})\.(\d{{1,2}})\s+{_CLOCK}\s*({_TZN})\b", text, re.I)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = _year_for(month, day, now)
        if year:
            hour = int(m.group(3))
            minute = int(m.group(4) or 0)
            ampm = m.group(5).lower()
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            start = datetime(year, month, day, hour, minute, tzinfo=_TZMAP[m.group(6).lower()])
            return start, (text[:m.start()] + " " + text[m.end():]).strip()

    # (2026-11-25 10:00:05) — wall time in US Eastern, seconds are a tie-break
    m = re.search(r"\((\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::\d{2})?\)", text)
    if m:
        start = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         int(m.group(4)), int(m.group(5)), tzinfo=ET)
        return start, (text[:m.start()] + " " + text[m.end():]).strip()

    # @ 05:00 PM EST  or  @ 7:00 pm
    m = re.search(rf"@\s*{_CLOCK}\s*({_TZN})?\b", text, re.I)
    if m:
        tz = _TZMAP[m.group(4).lower()] if m.group(4) else ET
        start = _pick_clock(m.group(1), m.group(2), m.group(3), tz, now, window_start, window_end)
        if start is not None:
            return start, (text[:m.start()] + " " + text[m.end():]).strip()
    return None, text


def _clean_team(text: str) -> str:
    text = (text or "").replace("`", "'")
    text = re.sub(r"\s*\([^)]*\)\s*", " ", text)
    text = re.sub(r"^.*:\s*", "", text)
    text = re.sub(r"^#\d+\s+", "", text.strip())
    text = re.sub(r"\s+", " ", text).strip(" .:-•|)")
    text = re.sub(r"\s+\bfeed\b.*$", "", text, flags=re.I)
    return text.strip(" .:-•|)")


def _sides(text: str) -> tuple[str, str] | None:
    body = re.sub(r"^[^:]{0,48}\b\d{1,3}\s*:\s*", "", text, count=1)
    # Prefer the first parenthetical that itself contains a matchup.
    paren = re.search(r"\(([^)]*\bvs\.?[^)]*)\)", body, re.I)
    target = paren.group(1) if paren else body
    m = _VS.search(target) or _AT.search(target) or _WORD_AT.search(target)
    if not m and paren:
        m = _VS.search(body) or _AT.search(body) or _WORD_AT.search(body)
    if not m:
        return None
    away, home = _clean_team(m.group("away")), _clean_team(m.group("home"))
    if len(away) < 2 or len(home) < 2 or len(away) > 42 or len(home) > 42:
        return None
    if re.search(r"\b(?:feed|network|channel)\b", away, re.I):
        return None
    return away, home


def _fallback_title(text: str) -> str:
    body = re.sub(r"^[^:]{0,48}\b\d{1,3}\s*:\s*", "", text, count=1)
    body = re.sub(r"\s*\([^)]*\)\s*", " ", body)
    body = re.sub(r"\s+", " ", body).strip(" :-")
    return body[:140]


def _slot_label(name: str) -> str | None:
    key = _norm(name)
    if re.search(r"\bnba g league\b", key):
        return "NBA G League"
    for rx, label in (
        (r"\bncaaf\b", "NCAAF"),
        (r"\bncaab\b", "NCAAB"),
        (r"\bwnba\b", "WNBA"),
        (r"\bnba\b", "NBA"),
        (r"\bnhl\b", "NHL"),
        (r"\bmlb\b", "MLB"),
        (r"\bnfl\b", "NFL"),
    ):
        if re.search(rx, key):
            return label
    return None


def is_numbered_event_channel(name: str) -> bool:
    return bool(_NUMBERED_EVENT.search(name or ""))


def slate_slot(name: str) -> tuple[str, int] | None:
    """(league id, 1-based index) for an empty 'NBA 10' style slot."""
    m = _LEAGUE_SLOT.search(name or "")
    if not m:
        return None
    label = re.sub(r"\s+", " ", m.group("label").lower()).strip()
    league = _SLATE_LEAGUE.get(label)
    number = int(m.group("num"))
    if not league or number < 1 or number > 30:
        return None
    return league, number


def parse_named_event(name: str, now: datetime, window_start: datetime,
                      window_end: datetime) -> NamedEvent | None:
    if not is_numbered_event_channel(name):
        return None
    start, rest = extract_start(name, now, window_start, window_end)
    if start is None:
        return None
    sides = _sides(rest)
    label = _slot_label(name)
    if sides:
        away, home = sides
        title = f"{label}: {away} at {home}" if label else f"{away} at {home}"
    else:
        title = _fallback_title(rest)
        if not title:
            return None
    return NamedEvent(title=title, start=start, source="name")


def _fetch_json(url: str, dest: Path, refresh: bool, timeout: int = 40) -> dict | None:
    if dest.exists() and dest.stat().st_size > 50 and not refresh:
        try:
            return json.loads(dest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            dest.write_bytes(raw)
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise RuntimeError(exc)
            last = exc
            time.sleep(min(8, 2 ** attempt))
        except Exception as exc:
            last = exc
            time.sleep(min(8, 2 ** attempt))
    raise RuntimeError(last)


def _game_from_event(event: dict, league: str, label: str) -> Game | None:
    comps = event.get("competitions") or []
    if not comps:
        return None
    comp = comps[0]
    start_s = comp.get("date") or event.get("date") or ""
    try:
        start = datetime.fromisoformat(start_s.replace("Z", "+00:00"))
    except ValueError:
        return None
    away = home = ""
    for side in comp.get("competitors") or []:
        team = (side.get("team") or {}).get("displayName") or ""
        if side.get("homeAway") == "away":
            away = team
        elif side.get("homeAway") == "home":
            home = team
    if not away or not home:
        return None
    names: list[str] = []
    for broadcast in comp.get("broadcasts") or []:
        names.extend(broadcast.get("names") or [])
    for geo in comp.get("geoBroadcasts") or []:
        short = ((geo.get("media") or {}).get("shortName") or "").strip()
        if short:
            names.append(short)
    nets: set[str] = set()
    for name in names:
        nets |= network_ids(name)
    venue = ((comp.get("venue") or {}).get("fullName") or "").strip()
    return Game(league=league, label=label, start=start, away=away, home=home,
                venue=venue, networks=nets)


def load_games(cache: Path, window_start: datetime, window_end: datetime,
               refresh: bool, log) -> list[Game]:
    """Download scoreboards for every US date the guide window touches."""
    first = (window_start.astimezone(ET) - timedelta(days=1)).date()
    last = window_end.astimezone(ET).date()
    games: list[Game] = []
    day = first
    while day <= last:
        stamp = day.strftime("%Y%m%d")
        for sport, league, groups, lid, label in LEAGUES:
            url = SCOREBOARD.format(sport=sport, league=league) + f"?dates={stamp}&limit=500"
            if groups:
                url += f"&groups={groups}"
            dest = cache / "espn" / f"{lid}-{stamp}.json"
            try:
                data = _fetch_json(url, dest, refresh)
            except Exception as exc:
                log(f"WARNING: scoreboard {label} {stamp}: {exc}")
                continue
            if not data:
                continue
            for event in data.get("events") or []:
                game = _game_from_event(event, lid, label)
                if game:
                    games.append(game)
        day += timedelta(days=1)
    # Stable order: start, then name.
    games.sort(key=lambda g: (g.start, g.away, g.home, g.league))
    log(f"  public scoreboard: {len(games):,} games "
        f"({first.isoformat()} → {last.isoformat()})")
    return games


def match_game(networks: set[str], league: str | None, start: datetime,
               games: list[Game], *, allow_any_league: bool = False) -> Game | None:
    if start is None or not networks:
        return None
    if league is None and not allow_any_league:
        return None
    window = timedelta(minutes=30)
    found: list[Game] = []
    for game in games:
        if league and game.league != league:
            continue
        if abs(game.start - start) > window:
            continue
        if not game.networks or not (networks & game.networks):
            continue
        found.append(game)
    if not found:
        return None
    found.sort(key=lambda g: abs((g.start - start).total_seconds()))
    if len(found) > 1:
        gap = abs((found[1].start - start).total_seconds()) - abs((found[0].start - start).total_seconds())
        if gap < 60:
            return None
    return found[0]


def _retitle(prog: dict, game: Game) -> dict:
    updated = dict(prog)
    original = (prog.get("title") or "").strip()
    venue = game.venue
    desc = original
    if venue:
        desc = f"{original} — {venue}" if original else venue
    updated["title"] = game.title
    updated["desc"] = desc
    updated["sub"] = game.matchup
    updated["event"] = True
    return updated


def _subtitle(prog: dict, game: Game) -> dict:
    updated = dict(prog)
    updated["sub"] = game.matchup
    if game.venue and game.venue not in (updated.get("desc") or ""):
        existing = (updated.get("desc") or "").strip()
        updated["desc"] = f"{existing} — {game.venue}" if existing else game.venue
    updated["event"] = True
    return updated


def apply_sports_titles(playlist: list[dict], progs: dict[str, list[dict]],
                        games: list[Game]) -> SportsStats:
    """Retitle generic sports programmes. Mutates the per-id programme lists."""
    stats = SportsStats(games_loaded=len(games))
    seen: set[str] = set()
    seen_leagues: set[str] = set()
    for ch in playlist:
        cid = ch["id"]
        if cid in seen:
            continue
        seen.add(cid)
        items = progs.get(cid) or []
        if not items:
            continue
        nets = network_ids(ch["name"])
        if not nets:
            continue
        for index, prog in enumerate(items):
            if prog.get("placeholder"):
                continue
            title = prog.get("title") or ""
            league = generic_league(title)
            generic = league is not None
            if not generic:
                if is_studio_show(title):
                    continue
                league = title_league(title)
                if league is None and not re.search(r"\bvs\.?\b", title, re.I):
                    continue
            game = match_game(
                nets,
                league,
                prog.get("start"),
                games,
                allow_any_league=not generic and league is None,
            )
            if game is None:
                continue
            if generic:
                items[index] = _retitle(prog, game)
                stats.retitled += 1
                if game.league not in seen_leagues or len(stats.samples) < 8:
                    seen_leagues.add(game.league)
                    if len(stats.samples) < 12:
                        when = prog["start"].astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
                        stats.samples.append((ch["name"], when, game.title))
            else:
                items[index] = _subtitle(prog, game)
                stats.subtitled += 1
    return stats


def _gaps(cid: str, events: list[dict], start: datetime, end: datetime) -> list[dict]:
    events = sorted(events, key=lambda p: p["start"])
    out: list[dict] = []
    cursor = start
    for prog in events:
        if prog["start"] > cursor:
            out.append(_programme(
                cid, cursor, prog["start"], "No event scheduled", "No event scheduled",
                placeholder=True, event=True,
            ))
        out.append(prog)
        if prog["stop"] > cursor:
            cursor = prog["stop"]
    if cursor < end:
        out.append(_programme(
            cid, cursor, end, "No event scheduled", "No event scheduled",
            placeholder=True, event=True,
        ))
    return out


def _overlaps(start: datetime, end: datetime, prog: dict) -> bool:
    return start < prog["stop"] and prog["start"] < end


def event_programmes(ch: dict, games: list[Game], now: datetime,
                     window_end: datetime) -> tuple[list[dict] | None, str]:
    """Programmes for one numbered event channel, or (None, "") if it is not one.

    The source tag is "event-name" or "event-slate".
    """
    name = ch["name"]
    if not is_numbered_event_channel(name):
        return None, ""
    cid = ch["id"]
    # Same lookback as the rest of the guide, so a game already underway is kept.
    window_start = now - timedelta(hours=3)
    chosen: list[dict] = []
    source = ""
    named = parse_named_event(name, now, window_start, window_end)
    if named is not None:
        stop = named.start + timedelta(hours=3)
        if stop > window_start and named.start < window_end:
            chosen.append(_programme(
                cid, named.start, stop, named.title, named.title, event=True,
            ))
        source = "event-name"
    else:
        slot = slate_slot(name)
        if slot:
            league, number = slot
            by_day: dict = {}
            for game in games:
                if game.league != league:
                    continue
                stop = game.start + timedelta(hours=3)
                if stop <= window_start or game.start >= window_end:
                    continue
                day = game.start.astimezone(TZ).date()
                by_day.setdefault(day, []).append(game)
            for day in sorted(by_day):
                slate = by_day[day]
                if number > len(slate):
                    continue
                game = slate[number - 1]
                prog = _programme(
                    cid, game.start, game.start + timedelta(hours=3),
                    game.title,
                    f"{game.title} — {game.venue}" if game.venue else game.title,
                    sub=game.matchup, event=True,
                )
                if any(_overlaps(prog["start"], prog["stop"], prev) for prev in chosen):
                    continue
                chosen.append(prog)
            source = "event-slate"
        else:
            source = "event-name"
    return _gaps(cid, chosen, window_start, window_end), source
