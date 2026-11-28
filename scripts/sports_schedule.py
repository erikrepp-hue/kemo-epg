"""Match sports listings to free public ESPN scoreboards.

No credentials, secrets, or stream URLs. Scoreboards are the public
site.api.espn.com JSON feeds (broadcast names included).

Linear sports channels with a generic title such as "NBA Basketball" or
"Live: NHL Hockey" are retitled "NBA: Away Team at Home Team" when the
league, the network, and the start time (within 30 minutes) agree.
A programme that already has a specific title keeps it, and the matchup
is written to sub-title.

Numbered event / PPV channels:
  * a matchup and start time in the channel name are used when that
    start is still inside the guide window
  * a team name, or Monday / Thursday / Sunday night, selects that game
  * any other numbered slot lists the day's games in the description
    ("NHL: Live Game (see description)") and does not guess which one is on
The rest of an empty window is "No event scheduled".
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
    ("netflix", r"\bnetflix\b"),
    ("prime", r"\bprime\s+video\b|\bamazon\s+prime\b|\bprime\s+video\b"),
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
    "amazon prime": "prime",
    "netflix": "netflix",
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
    r"big interview|game\s*night|football night|night in america|countdown|kickoff|\bpresented\b|"
    r"football saturday|saturday night'?s main|\bpregame\b|\bpostgame\b",
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
    neutral: bool = False

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
    affiliate_generic_remaining: int = 0
    subtitle_duplicates: int = 0
    league_pack: dict = field(default_factory=dict)
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


_BARE_SPORT = {
    "football": ["nfl", "ncaaf"],
    "basketball": ["nba", "ncaam"],
    "hockey": ["nhl"],
    "baseball": ["mlb"],
}


def candidate_leagues(title: str) -> list[str]:
    """Leagues a still-generic title might be. Empty for studio shows and real matchups."""
    if not title or is_studio_show(title):
        return []
    if re.search(r"\b(?:vs\.?|@)\b", title) or re.search(r"\bat\b", _core_title(title), re.I):
        # "Team at Team" is already a matchup. "Live at 7" is not a title we generate.
        if re.search(r"\bvs\.?\b", title) or re.search(r".+\sat\s+\S+", title):
            return []
    core = _core_title(title).lower().replace("'", "'")
    if core in _GENERIC_CORE:
        return [_GENERIC_CORE[core]]
    if core in _BARE_SPORT:
        return list(_BARE_SPORT[core])
    for phrase, league in sorted(_GENERIC_CORE.items(), key=lambda kv: -len(kv[0])):
        if core == phrase or core.startswith(phrase + " ") or core.startswith(phrase + ":"):
            return [league]
    return []


def generic_league(title: str) -> str | None:
    """League id when the title is only a generic game name, else None."""
    leagues = candidate_leagues(title)
    return leagues[0] if len(leagues) == 1 else None


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


def _cached_today(dest: Path) -> bool:
    """Scoreboards are only reused when they were downloaded today."""
    if not dest.exists() or dest.stat().st_size < 50:
        return False
    modified = datetime.fromtimestamp(dest.stat().st_mtime, TZ).date()
    return modified == datetime.now(TZ).date()


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
                venue=venue, networks=nets, neutral=bool(comp.get("neutralSite")))


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
                data = _fetch_json(url, dest, refresh or not _cached_today(dest))
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


# Nicknames whose metro is not the ESPN city name. Used only to break a tie
# when several games share one network.
_NICK_METRO = {
    "titans": ["nashville"],
    "packers": ["green bay", "milwaukee", "wisconsin"],
    "patriots": ["boston", "new england"],
    "commanders": ["washington"],
    "49ers": ["san francisco", "bay area"],
    "raiders": ["las vegas", "vegas"],
    "cardinals": ["phoenix"],
    "chargers": ["los angeles"],
    "rams": ["los angeles"],
    "giants": ["new york"],
    "jets": ["new york"],
    "warriors": ["san francisco", "bay area"],
    "clippers": ["los angeles"],
    "lakers": ["los angeles"],
    "nets": ["brooklyn"],
    "knicks": ["new york"],
}


def _short_team(name: str) -> str:
    parts = (name or "").split()
    if not parts:
        return name
    nick = parts[-1]
    if nick.lower() in {"state", "tech", "a&m", "university", "college"} or len(nick) < 4:
        return name
    return nick


def _sub_ok(title: str, sub: str) -> str:
    """TiviMate joins title and sub-title, so the sub-title must not repeat the title."""
    sub = (sub or "").strip()
    title = (title or "").strip()
    if not sub or not title:
        return ""
    tl, sl = title.lower(), sub.lower()
    if sl == tl or sl in tl or tl in sl:
        return ""
    return sub


def _venues(games: list[Game]) -> str:
    seen: list[str] = []
    for game in games:
        if game.venue and game.venue not in seen:
            seen.append(game.venue)
    return "; ".join(seen)


def games_title(games: list[Game]) -> str:
    if len(games) == 1:
        return games[0].title
    parts = [f"{_short_team(g.away)} at {_short_team(g.home)}" for g in games[:5]]
    text = f"{games[0].label}: " + " / ".join(parts)
    if len(games) > 5:
        text += " / ..."
    return text


def _market_hit(game: Game, channel_name: str) -> bool:
    text = _norm(channel_name)
    if not text:
        return False
    for side in (game.away, game.home):
        side_n = _norm(side)
        if len(side_n) >= 4 and side_n in text:
            return True
        parts = side_n.split()
        if len(parts) >= 2:
            city = " ".join(parts[:-1])
            nick = parts[-1]
            if len(city) >= 4 and city in text:
                return True
            if len(nick) >= 5 and re.search(rf"\b{re.escape(nick)}\b", text):
                return True
            for metro in _NICK_METRO.get(nick, []):
                if metro in text:
                    return True
    return False


_STREAM_NFL = {"netflix", "prime", "peacock"}
_BROADCAST = {"abc", "cbs", "nbc", "fox"}


def _game_on_channel(game: Game, networks: set[str], channel_name: str) -> bool:
    if game.networks & networks:
        return True
    # Netflix / Prime / Peacock NFL still airs on the home and away stations' locals.
    if (game.league == "nfl" and game.networks & _STREAM_NFL and networks & _BROADCAST
            and _market_hit(game, channel_name)):
        return True
    return False


def match_games(networks: set[str], league: str | None, start: datetime,
                stop: datetime | None, games: list[Game], channel_name: str = "",
                *, allow_any_league: bool = False, minutes: int = 60) -> list[Game]:
    """Games on this network within `minutes` of the listing, or inside it."""
    if start is None or not networks:
        return []
    if league is None and not allow_any_league:
        return []
    window = timedelta(minutes=minutes)
    found: list[Game] = []
    seen: set[tuple] = set()
    for game in games:
        if league and game.league != league:
            continue
        if not _game_on_channel(game, networks, channel_name):
            continue
        near = abs(game.start - start) <= window
        inside = stop is not None and start <= game.start <= stop
        if not near and not inside:
            continue
        key = (game.league, game.away, game.home, game.start)
        if key in seen:
            continue
        seen.add(key)
        found.append(game)
    found.sort(key=lambda g: (abs((g.start - start).total_seconds()), g.start, g.away))
    return found


def choose_games(found: list[Game], channel_name: str) -> list[Game]:
    """Prefer the game whose team or city is in the channel name; otherwise keep the window."""
    if len(found) <= 1:
        return found
    marketed = [g for g in found if _market_hit(g, channel_name)]
    if marketed and len(marketed) < len(found):
        return marketed
    return found


def _describe(original: str, games: list[Game]) -> str:
    original = (original or "").strip()
    venues = _venues(games)
    if original and venues:
        return f"{original} — {venues}"
    return original or venues


def _retitle(prog: dict, games: list[Game]) -> dict:
    updated = dict(prog)
    title = games_title(games)
    updated["title"] = title
    updated["desc"] = _describe(prog.get("title") or "", games)
    venue = games[0].venue if len(games) == 1 else ""
    updated["sub"] = _sub_ok(title, venue)
    updated["event"] = True
    return updated


def _subtitle(prog: dict, games: list[Game]) -> dict:
    updated = dict(prog)
    title = (prog.get("title") or "").strip()
    matchup = games[0].matchup if len(games) == 1 else games_title(games)
    sub = _sub_ok(title, matchup)
    if not sub:
        updated["sub"] = _sub_ok(title, prog.get("sub") or "")
        return updated
    updated["sub"] = sub
    venues = _venues(games)
    if venues and venues.lower() not in (updated.get("desc") or "").lower():
        existing = (updated.get("desc") or "").strip()
        updated["desc"] = f"{existing} — {venues}" if existing else venues
    updated["event"] = True
    return updated


def _overlap_seconds(a: dict, b: dict) -> float:
    start = max(a["start"], b["start"])
    stop = min(a["stop"], b["stop"])
    return max(0.0, (stop - start).total_seconds())


def dedupe_programmes(items: list[dict]) -> list[dict]:
    """Drop a second listing of the same slot, and a generic title under a specific one."""
    ordered = sorted(items, key=lambda p: (p["start"], -(p["stop"] - p["start"]).total_seconds()))
    kept: list[dict] = []
    for prog in ordered:
        drop = False
        for index, prev in enumerate(kept):
            overlap = _overlap_seconds(prev, prog)
            gap = (prog["start"] - prev["stop"]).total_seconds()
            same_start = abs((prog["start"] - prev["start"]).total_seconds()) <= 5 * 60
            touches = overlap >= 15 * 60 or (same_start and overlap >= 60)
            abuts = 0 <= gap <= 60
            if not touches and not abuts:
                continue
            ta = (prev.get("title") or "").strip()
            tb = (prog.get("title") or "").strip()
            same = ta.lower() == tb.lower() or (ta and tb and (ta.lower() in tb.lower() or tb.lower() in ta.lower()))
            generic_under = (
                touches
                and generic_league(tb) is not None
                and generic_league(ta) is None
                and title_league(tb) is not None
                and title_league(tb) == title_league(ta)
            )
            if not same and not generic_under:
                continue
            if same and (prog["stop"] - prog["start"]) > (prev["stop"] - prev["start"]):
                merged = dict(prog)
                merged["start"] = min(prev["start"], prog["start"])
                merged["stop"] = max(prev["stop"], prog["stop"])
                merged["start_s"] = _xmltv(merged["start"])
                merged["stop_s"] = _xmltv(merged["stop"])
                merged["sub"] = _sub_ok(merged.get("title") or "", merged.get("sub") or "")
                kept[index] = merged
            elif same and (abuts or touches):
                prev["stop"] = max(prev["stop"], prog["stop"])
                prev["start"] = min(prev["start"], prog["start"])
                prev["start_s"] = _xmltv(prev["start"])
                prev["stop_s"] = _xmltv(prev["stop"])
            drop = True
            break
        if not drop:
            kept.append(prog)
    kept.sort(key=lambda p: p["start"])
    return kept


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
            leagues = candidate_leagues(title)
            generic = bool(leagues)
            if not generic:
                if is_studio_show(title):
                    continue
                league = title_league(title)
                if league is None and not re.search(r"\bvs\.?\b", title, re.I):
                    continue
                leagues = [league] if league else [None]
            found: list[Game] = []
            for league in leagues:
                found.extend(match_games(
                    nets,
                    league,
                    prog.get("start"),
                    prog.get("stop"),
                    games,
                    ch["name"],
                    allow_any_league=league is None,
                ))
            if len(leagues) > 1 and found:
                found.sort(key=lambda g: abs((g.start - prog["start"]).total_seconds()))
                best = found[0].league
                found = [g for g in found if g.league == best]
            chosen = choose_games(found, ch["name"])
            if not chosen:
                continue
            if generic:
                items[index] = _retitle(prog, chosen)
                stats.retitled += 1
                when = prog["start"].astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
                nashville = (
                    "nashville" in ch["name"].lower()
                    and re.search(r"\b(fox|cbs)\b", ch["name"], re.I)
                    and prog["start"].astimezone(TZ).date().isoformat() == "2026-11-26"
                )
                if nashville:
                    stats.samples.insert(0, (ch["name"], when, items[index]["title"]))
                elif chosen[0].league not in seen_leagues or len(stats.samples) < 8:
                    seen_leagues.add(chosen[0].league)
                    if len(stats.samples) < 12:
                        stats.samples.append((ch["name"], when, items[index]["title"]))
            else:
                updated = _subtitle(prog, chosen)
                if updated is not prog and updated.get("sub"):
                    items[index] = updated
                    stats.subtitled += 1
                else:
                    items[index] = dict(prog)
                    items[index]["sub"] = _sub_ok(prog.get("title") or "", prog.get("sub") or "")
        scrubbed = []
        for prog in items:
            prog = dict(prog)
            prog["sub"] = _sub_ok(prog.get("title") or "", prog.get("sub") or "")
            scrubbed.append(prog)
        progs[cid] = dedupe_programmes(scrubbed)
    return stats


def scrub_subtitles(progs: dict[str, list[dict]]) -> int:
    """Clear any sub-title that repeats its title. Returns how many were cleared."""
    cleared = 0
    for cid, items in progs.items():
        for prog in items:
            old = prog.get("sub") or ""
            prog["sub"] = _sub_ok(prog.get("title") or "", old)
            if old and not prog["sub"]:
                cleared += 1
    return cleared


def count_listing_issues(playlist: list[dict], progs: dict[str, list[dict]]) -> tuple[int, int]:
    """Remaining generic titles on ABC/NBC/CBS/FOX for Nov 25-29, and title/sub duplicates."""
    names = {}
    for ch in playlist:
        names.setdefault(ch["id"], ch["name"])
    generic = 0
    dupes = 0
    seen: set[str] = set()
    start = datetime(2026, 11, 25, tzinfo=TZ)
    end = datetime(2026, 11, 30, tzinfo=TZ)
    for cid, items in progs.items():
        if cid in seen:
            continue
        seen.add(cid)
        name = names.get(cid, "")
        nets = network_ids(name)
        affiliate = bool(nets & {"abc", "nbc", "cbs", "fox"})
        for prog in items:
            title = prog.get("title") or ""
            sub = prog.get("sub") or ""
            if sub and not _sub_ok(title, sub):
                dupes += 1
            if not affiliate or prog.get("placeholder"):
                continue
            when = prog.get("start")
            if when is None or not (start <= when.astimezone(TZ) < end):
                continue
            if candidate_leagues(title):
                generic += 1
    return generic, dupes


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


_NET_LABEL = {
    "espn": "ESPN", "espn2": "ESPN2", "espnu": "ESPNU", "espn-news": "ESPNews",
    "abc": "ABC", "cbs": "CBS", "nbc": "NBC", "fox": "Fox",
    "fs1": "FS1", "fs2": "FS2", "cbssn": "CBSSN",
    "tnt": "TNT", "tbs": "TBS", "nfl-net": "NFL Network",
    "nba-tv": "NBA TV", "nhl-net": "NHL Network", "mlb-net": "MLB Network",
    "sec": "SEC Network", "acc": "ACC Network", "bigten": "Big Ten Network",
    "prime": "Prime Video", "peacock": "Peacock", "netflix": "Netflix",
}


def _net_label(game: Game) -> str:
    labels = []
    for nid in sorted(game.networks):
        labels.append(_NET_LABEL.get(nid, nid.upper()))
    return ", ".join(labels)


def slate_lines(games: list[Game]) -> str:
    """One line per game: CT start, matchup, network, venue."""
    lines = []
    for game in _dedupe_games(games):
        when = game.start.astimezone(TZ).strftime("%a %b %-d, %-I:%M %p CT")
        line = f"{when}  {game.away} at {game.home}"
        nets = _net_label(game)
        if nets:
            line += f" ({nets})"
        if game.venue:
            line += f" — {game.venue}"
        lines.append(line)
    return "\n".join(lines)


def slate_day_programmes(cid: str, games: list[Game], label: str,
                         window_start: datetime, window_end: datetime) -> list[dict]:
    """One block per day listing every game. Does not pick a slot's game."""
    by_day: dict = {}
    for game in games:
        if game.start + timedelta(hours=3) <= window_start or game.start >= window_end:
            continue
        by_day.setdefault(game.start.astimezone(TZ).date(), []).append(game)
    blocks = []
    for day in sorted(by_day):
        slate = _dedupe_games(by_day[day])
        if not slate:
            continue
        start = min(game.start for game in slate)
        stop = max(game.start + timedelta(hours=3) for game in slate)
        title = f"{label}: Live Game (see description)"
        blocks.append(_programme(cid, start, stop, title, slate_lines(slate), event=True))
    return blocks


def _league_for_slot(name: str) -> str | None:
    slot = slate_slot(name)
    if slot:
        return slot[0]
    label = _slot_label(name)
    for league, pretty in _LABEL.items():
        if pretty == label:
            return league
    if re.search(r"sportsnet\s*\+", name or "", re.I):
        return "nhl"
    return None


def _linear_network(name: str) -> bool:
    """A real network, not a numbered game slot."""
    if is_numbered_event_channel(name):
        return False
    key = _norm(name)
    return bool(re.search(
        r"\b(?:nfl network|red\s*zone|nba tv|nhl network|mlb network|"
        r"sportsnet(?!\s*\+)|tsn\b|bein sports|nbc sports|fanduel sports)\b",
        key,
    ))


def event_programmes(ch: dict, games: list[Game], now: datetime,
                     window_end: datetime) -> tuple[list[dict] | None, str]:
    """Programmes for one numbered event channel, or (None, "") if it is not one.

    The source tag is "event-name" or "event-slate".
    """
    name = ch["name"]
    if provider_owns(ch) or not is_numbered_event_channel(name):
        return None, ""
    cid = ch["id"]
    # Same lookback as the rest of the guide, so a game already underway is kept.
    window_start = now - timedelta(hours=3)
    chosen: list[dict] = []
    source = ""
    named = parse_named_event(name, now, window_start, window_end)
    named_in_window = False
    if named is not None:
        stop = named.start + timedelta(hours=3)
        # Only a matchup whose start is still in the window is that channel's game.
        # A date that has already passed does not say what is on today.
        if (_sides(name) or " at " in (named.title or "").lower()) and stop > window_start and named.start < window_end:
            chosen.append(_programme(
                cid, named.start, stop, named.title,
                named.title, event=True,
            ))
            named_in_window = True
            source = "event-name"
    if not named_in_window:
        league = _league_for_slot(name)
        if league:
            chosen = slate_day_programmes(
                cid, [g for g in games if g.league == league],
                _LABEL.get(league, league.upper()), window_start, window_end,
            )
            source = "event-slate"
        else:
            source = "event-name"
    return _gaps(cid, chosen, window_start, window_end), source


_PACKAGE_LEAGUE = {
    "usa nba": "nba",
    "usa nhl": "nhl",
}


# Whole groups that are provider event / league-pass slots. TiviMate uses the
# provider's own guide for these, so this file must not publish them.
_PROVIDER_CATEGORIES = {
    "CA: Sportsnet +",
    "Sport NCAA Men Basketball",
    "Sport NCAA Women Basketball",
    "Sport NJCAA Men Basketball",
    "Sport NJCAA Women Basketball",
    "Sport Lacrosse",
    "Sport Match Center",
    "Sport Golf",
    "Sport Tennis",
    "Sport Cycling",
    "PPV FLOSPORTS",
    "Pay Per View Events",
    "PPV-MMA/BOXING/WWE/UFC",
    "Sports DAZN",
    "USA NCAAF",
    "USA WNBA",
}
_PACKAGE_CATEGORIES = {
    "USA NBA",
    "USA NHL",
    "USA NFL - Sunday Ticket",
    "USA MLB",
}
# Linear networks that stay in the guide even when they sit in a package group.
_LINEAR_KEEP = re.compile(
    r"(?i)\b(?:nfl network|red\s*zone|nba tv|nhl network|mlb network|mlb strike zone|"
    r"espn\s*2|(?<![a-z+])espn(?!\s*\+)|fox sports\s*[12]|\bfs\s*[12]\b|"
    r"\btnt\b|\btbs\b|cbs sports|espn\s*u|sec network|acc network|"
    r"big ten|\bbtn\b|\btsn\s*[1-5]\b|bein sports)\b"
)


def provider_owns(ch: dict) -> bool:
    """True when this channel is left out so the provider guide can fill it.

    No channel element and no programmes are written. Linear networks stay.
    """
    name = ch.get("name") or ""
    category = ch.get("category") or ""
    if category in _PROVIDER_CATEGORIES:
        return True
    if re.search(r"espn\s*\+|espn\s+plus", name, re.I):
        return True
    if re.search(r"(?i)(?:^|[^a-z])ppv(?:[^a-z]|$)|\bpay\s*per\s*view\b", name):
        return True
    if re.search(r"(?i)\bpeacock\s+\d{1,3}\b", name):
        return True
    if re.search(r"(?i)\bdazn\b", name) and re.search(r"\d", name):
        return True
    if re.search(r"(?i)cent(?:er|re)\s+ice", name):
        return True
    if re.search(r"(?i)\b(?:soccer|cricket|golf|tennis|cycling|lacrosse)\s*0?\d{1,3}\b", name):
        return True
    if re.search(r"(?i)\b(?:ncaa|njcaa)\b.{0,40}\b\d{1,3}\b", name) and re.search(
            r"(?i)baseball|softball|basketball", name):
        return True
    if re.search(r"(?i)premier league replay\s+\d", name):
        return True
    if category in _PACKAGE_CATEGORIES and not _LINEAR_KEEP.search(name):
        return True
    return False


def package_league(category: str) -> str | None:
    key = _norm(category)
    if key in _PACKAGE_LEAGUE:
        return _PACKAGE_LEAGUE[key]
    if "sunday ticket" in key or "game pass" in key or "league pass" in key and "nba" in key:
        return "nfl" if "nfl" in key or "sunday" in key or "game pass" in key else "nba"
    if "center ice" in key:
        return "nhl"
    if "league pass" in key:
        return "nba"
    return None


def _dedupe_games(games: list[Game]) -> list[Game]:
    out: list[Game] = []
    seen: set[tuple] = set()
    for game in sorted(games, key=lambda g: (g.start, g.away, g.home)):
        key = (game.league, game.away, game.home, game.start)
        if key in seen:
            continue
        seen.add(key)
        out.append(game)
    return out


def _has_real_matchup(items: list[dict]) -> bool:
    for prog in items or []:
        if prog.get("placeholder"):
            continue
        title = prog.get("title") or ""
        if title and title != "No event scheduled" and " at " in title:
            return True
    return False


def _slot_rank(name: str, peers: list[str]) -> int | None:
    nums = []
    for peer in peers:
        found = [int(n) for n in re.findall(r"\b(\d{1,4})\b", peer)]
        if found:
            nums.append(found[-1])
    mine = [int(n) for n in re.findall(r"\b(\d{1,4})\b", name or "")]
    if not mine or not nums:
        return None
    num = mine[-1]
    ordered = sorted(set(nums))
    if num not in ordered:
        return None
    # Small indexes are already the game number. Large Sunday Ticket numbers
    # are ranked among themselves so 705 is game 1.
    if num <= 40:
        return num
    return ordered.index(num) + 1


def _night_games(name: str, games: list[Game], league: str,
                 start: datetime, end: datetime) -> list[Game]:
    text = _norm(name)
    weekday = None
    prefer: set[str] = set()
    if "monday night" in text:
        weekday, prefer = 0, {"prime", "espn", "abc"}
    elif "thursday night" in text:
        weekday, prefer = 3, {"prime", "fox", "nfl-net", "amazon"}
    elif "sunday night" in text:
        weekday, prefer = 6, {"nbc", "prime", "espn"}
    if weekday is None:
        return []
    night = []
    for game in games:
        if game.league != league:
            continue
        if game.start + timedelta(hours=3) <= start or game.start >= end:
            continue
        if game.start.astimezone(TZ).weekday() != weekday:
            continue
        night.append(game)
    preferred = [g for g in night if g.networks & prefer]
    return _dedupe_games(preferred or night[:1])


def _indexed_games(rank: int, games: list[Game], league: str,
                   start: datetime, end: datetime) -> list[Game]:
    by_day: dict = {}
    for game in games:
        if game.league != league:
            continue
        if game.start + timedelta(hours=3) <= start or game.start >= end:
            continue
        by_day.setdefault(game.start.astimezone(TZ).date(), []).append(game)
    chosen = []
    for day in sorted(by_day):
        slate = _dedupe_games(by_day[day])
        if rank <= len(slate):
            chosen.append(slate[rank - 1])
    return chosen


def fill_league_packages(playlist: list[dict], progs: dict[str, list[dict]],
                         games: list[Game], now: datetime, window_end: datetime,
                         stats: SportsStats) -> None:
    """Give every NBA, NHL, and NFL package channel that league's games.

    A team name, or a Monday / Thursday / Sunday night name, gets that game.
    A numbered slot that does not name a game lists the day's slate instead
    of being assigned game N. Linear networks keep their public guide.
    """
    window_start = now - timedelta(hours=3)
    groups: dict[str, list[dict]] = {}
    for ch in playlist:
        if provider_owns(ch):
            continue
        league = package_league(ch.get("category") or "")
        if league:
            groups.setdefault(ch["category"], []).append(ch)
    report = {}
    for category, members in groups.items():
        league = package_league(category)
        names = [ch["name"] for ch in members]
        with_matchup = 0
        with_programmes = 0
        for ch in members:
            # A shared junk tvg-id (USA NHL 18 stored as a Myrtle Beach NBC id)
            # must not overwrite that local station. Publish the package under
            # its display name instead.
            if any(other is not ch and other.get("id") == ch["id"]
                   and not package_league(other.get("category") or "")
                   for other in playlist):
                ch["id"] = ch["name"]
            items = progs.get(ch["id"]) or []
            if any(not p.get("placeholder") and (p.get("title") or "") not in ("", "No event scheduled") for p in items):
                with_programmes += 1
            if _linear_network(ch["name"]) and any(
                    not p.get("placeholder") and (p.get("title") or "") not in ("", "No event scheduled")
                    for p in items):
                if _has_real_matchup(items):
                    with_matchup += 1
                continue
            if _has_real_matchup(items) and not is_numbered_event_channel(ch["name"]):
                with_matchup += 1
                continue
            selected = _dedupe_games(
                g for g in games
                if g.league == league
                and g.start < window_end
                and g.start + timedelta(hours=3) > window_start
                and _market_hit(g, ch["name"])
            )
            if not selected:
                selected = _night_games(ch["name"], games, league, window_start, window_end)
            # A bare number is not a reliable map of game N onto channel N.
            if not selected:
                blocks = slate_day_programmes(
                    ch["id"], [g for g in games if g.league == league],
                    _LABEL.get(league, league.upper()), window_start, window_end,
                )
                if blocks:
                    progs[ch["id"]] = _gaps(ch["id"], blocks, window_start, window_end)
                    ch["epg_source"] = "league-slate"
                    with_programmes += 1
                continue
            blocks = []
            for game in selected:
                title = game.title
                blocks.append(_programme(
                    ch["id"], game.start, game.start + timedelta(hours=3),
                    title,
                    f"{title} — {game.venue}" if game.venue else title,
                    sub=_sub_ok(title, game.venue), event=True,
                ))
            progs[ch["id"]] = _gaps(ch["id"], blocks, window_start, window_end)
            ch["epg_source"] = "league-pack"
            if _has_real_matchup(progs[ch["id"]]):
                with_matchup += 1
            if any(not p.get("placeholder") for p in progs[ch["id"]]):
                with_programmes += 1
            if len(stats.samples) < 20 and blocks:
                when = blocks[0]["start"].astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
                stats.samples.append((ch["name"], when, blocks[0]["title"]))
        report[category] = {
            "channels": len(members),
            "with_programmes": with_programmes,
            "with_matchup": with_matchup,
        }
    stats.league_pack = report
    # Centre Ice is an NHL package under a Canada group, not "USA NHL".
    for ch in playlist:
        if provider_owns(ch) or not re.search(r"cent(?:er|re)\s+ice", ch.get("name") or "", re.I):
            continue
        items = progs.get(ch["id"]) or []
        if any(not p.get("placeholder") and (p.get("title") or "") not in ("", "No event scheduled") for p in items):
            continue
        blocks = slate_day_programmes(
            ch["id"], [g for g in games if g.league == "nhl"], "NHL",
            now - timedelta(hours=3), window_end,
        )
        if blocks:
            progs[ch["id"]] = _gaps(ch["id"], blocks, now - timedelta(hours=3), window_end)
            ch["epg_source"] = "league-slate"


def add_playlist_id_aliases(playlist: list[dict], progs: dict[str, list[dict]]) -> int:
    """Publish league-package rows under both the playlist id and the display name.

    TiviMate uses the tvg-id when the playlist has one, otherwise the channel name.
    The snapshot stores generated slugs for these channels, so either one may be
    what the playlist actually sends.
    """
    seen = {ch["id"] for ch in playlist}
    extras = []
    for ch in playlist:
        if provider_owns(ch) or not package_league(ch.get("category") or ""):
            continue
        aliases = []
        raw = re.sub(r"~\d+$", "", ch.get("stored_id") or "")
        if raw and raw != ch["id"]:
            aliases.append(raw)
        if ch.get("name") and ch["name"] != ch["id"]:
            aliases.append(ch["name"])
        for alias in aliases:
            # Never copy a package slate onto an id another station already uses.
            foreign = [other for other in playlist
                       if other is not ch and other.get("id") == alias
                       and not package_league(other.get("category") or "")]
            if foreign:
                continue
            if alias in seen:
                if not progs.get(alias) and progs.get(ch["id"]):
                    progs[alias] = list(progs[ch["id"]])
                continue
            seen.add(alias)
            clone = dict(ch)
            clone["id"] = alias
            extras.append(clone)
            progs[alias] = list(progs.get(ch["id"]) or [])
    playlist.extend(extras)
    return len(extras)


_TEAM_FEEDS = (
    ("basketball", "mens-college-basketball", "ncaam"),
    ("basketball", "womens-college-basketball", "ncaaw"),
    ("football", "college-football", "ncaaf"),
    ("basketball", "nba", "nba"),
    ("football", "nfl", "nfl"),
    ("hockey", "nhl", "nhl"),
    ("baseball", "mlb", "mlb"),
)
_LEAGUE_LABEL = {
    "ncaam": "NCAAB", "ncaaw": "NCAAW", "ncaaf": "NCAAF",
    "nba": "NBA", "nfl": "NFL", "nhl": "NHL", "mlb": "MLB",
    "wnba": "WNBA", "mls": "MLS", "epl": "EPL",
}
_MEET_TEAMS = re.compile(
    r"\b(?:[Tt]he\s+)?([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,3})\s*"
    r"\(\s*\d+\s*[-–]\s*\d+\s*\)\s+and\s+(?:[Tt]he\s+)?"
    r"([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,3})\s*"
    r"\(\s*\d+\s*[-–]\s*\d+\s*\)"
)
_HOST_TEAMS = re.compile(
    r"\b([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,3})\s+hosts?\s+"
    r"([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,3})"
)
_FACE_TEAMS = re.compile(
    r"\b([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,3})\s+faces?\s+"
    r"([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,3})"
)
_VS_TEAMS = re.compile(
    r"\b([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,3})\s+vs\.?\s+"
    r"([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,3})"
)
_AT_TEAMS = re.compile(
    r"\b([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,3})\s+at\s+"
    r"([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,3})"
)
_VISIT_TEAMS = re.compile(
    r"\b(?:[Tt]he\s+)?([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,4})\s+visits?\s+"
    r"(?:[Tt]he\s+)?(?:No\.\s*\d+\s+)?([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,4})"
)
_ARENA = re.compile(
    r"\bat\s+([A-Z][\w'&.-]+(?:\s+[A-Z][\w'&.-]+){0,6}"
    r"(?:Arena|Stadium|Center|Centre|Field|Coliseum|Garden|Dome|Park|Pavilion))"
)


def load_team_index(cache: Path, refresh: bool, log) -> dict[tuple[str, str], list[str]]:
    """Mascot/nickname → display names, per league. Public ESPN team lists."""
    index: dict[tuple[str, str], list[str]] = {}
    for sport, league, lid in _TEAM_FEEDS:
        url = (f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams"
               "?limit=1000")
        dest = cache / "espn" / f"teams-{lid}.json"
        try:
            data = _fetch_json(url, dest, refresh)
        except Exception as exc:
            log(f"WARNING: team list {lid}: {exc}")
            continue
        if not data:
            continue
        teams = []
        for sport_row in data.get("sports") or []:
            for league_row in sport_row.get("leagues") or []:
                teams.extend(league_row.get("teams") or [])
        for row in teams:
            team = row.get("team") or {}
            display = (team.get("displayName") or "").strip()
            if not display:
                continue
            tokens = {
                (team.get("name") or "").strip(),
                (team.get("nickname") or "").strip(),
                display.split()[-1] if display.split() else "",
            }
            for token in tokens:
                key = token.lower()
                if len(key) < 4:
                    continue
                bucket = index.setdefault((lid, key), [])
                if display not in bucket:
                    bucket.append(display)
    log(f"  team nicknames indexed: {len(index):,}")
    return index


def _resolve_side(token: str, league: str, index: dict[tuple[str, str], list[str]]) -> str | None:
    parts = [p for p in token.split() if p.lower() not in {"no.", "the"}]
    tries = [token.strip()]
    if len(parts) >= 2:
        tries.append(" ".join(parts[-2:]))
    if parts:
        tries.append(parts[-1])
    ambiguous = ""
    for cand in tries:
        hits = index.get((league, cand.lower())) or []
        if len(hits) == 1:
            return hits[0]
        if hits and not ambiguous:
            ambiguous = cand.strip()
    # Ambiguous mascot (Eagles, Tigers): keep the nickname, still confident.
    return ambiguous or None


def _parse_desc_teams(desc: str) -> tuple[str, str, str] | None:
    """(left, right, 'vs'|'at') from a listing description, or None."""
    text = desc or ""
    m = _MEET_TEAMS.search(text)
    if m:
        return m.group(1), m.group(2), "vs"
    m = _HOST_TEAMS.search(text)
    if m:
        # "X host Y" → Y at X
        return m.group(2), m.group(1), "at"
    m = _FACE_TEAMS.search(text)
    if m:
        return m.group(1), m.group(2), "vs"
    m = _VS_TEAMS.search(text)
    if m:
        return m.group(1), m.group(2), "vs"
    m = _VISIT_TEAMS.search(text)
    if m:
        return m.group(1), m.group(2), "at"
    m = _AT_TEAMS.search(text)
    if m:
        return m.group(1), m.group(2), "at"
    return None


def _game_for_names(games: list[Game], league: str, a: str, b: str) -> Game | None:
    def hit(game: Game) -> bool:
        sides = (game.away.lower(), game.home.lower())
        a_l, b_l = a.lower(), b.lower()
        pair = (
            (a_l in sides[0] or sides[0] in a_l) and (b_l in sides[1] or sides[1] in b_l)
        ) or (
            (b_l in sides[0] or sides[0] in b_l) and (a_l in sides[1] or sides[1] in a_l)
        )
        return pair
    found = [g for g in games if g.league == league and hit(g)]
    keys = {(g.away, g.home) for g in found}
    if len(keys) == 1:
        return found[0]
    return None


def apply_description_matchups(playlist: list[dict], progs: dict[str, list[dict]],
                               games: list[Game], cache: Path, refresh: bool,
                               log) -> list[tuple[str, str, str, str]]:
    """Retitle a generic game from a wider scoreboard window or its description.

    The description text is left as it is. The sub-title is the venue or blank.
    Returns (channel, when CT, before, after) samples.
    """
    index = load_team_index(cache, refresh, log)
    samples: list[tuple[str, str, str, str]] = []
    count = 0
    seen: set[str] = set()
    for ch in playlist:
        cid = ch["id"]
        if cid in seen:
            continue
        seen.add(cid)
        nets = network_ids(ch["name"])
        for prog in progs.get(cid) or []:
            if prog.get("placeholder"):
                continue
            before = (prog.get("title") or "").strip()
            leagues = candidate_leagues(before)
            if not leagues:
                continue
            found: list[Game] = []
            if nets:
                for league in leagues:
                    found.extend(match_games(
                        nets, league, prog.get("start"), prog.get("stop"), games,
                        ch["name"], minutes=180,
                    ))
            if found:
                if len(leagues) > 1:
                    found.sort(key=lambda g: abs((g.start - prog["start"]).total_seconds()))
                    best = found[0].league
                    found = [g for g in found if g.league == best]
                chosen = choose_games(found, ch["name"])
                if not chosen:
                    continue
                updated = _retitle(prog, chosen)
                if updated["title"] == before:
                    continue
                prog.update(updated)
                count += 1
                if len(samples) < 8:
                    when = prog["start"].astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
                    samples.append((ch["name"], when, before, prog["title"]))
                continue
            parsed = _parse_desc_teams(prog.get("desc") or "")
            if not parsed:
                continue
            left_raw, right_raw, style = parsed
            titled = None
            venue = ""
            for league in leagues:
                left = _resolve_side(left_raw, league, index)
                right = _resolve_side(right_raw, league, index)
                if not left or not right:
                    continue
                game = _game_for_names(games, league, left, right)
                label = _LEAGUE_LABEL.get(league, league.upper())
                if game is not None:
                    if game.neutral or style == "vs":
                        titled = f"{label}: {game.away} vs {game.home}"
                    else:
                        titled = f"{label}: {game.away} at {game.home}"
                    venue = game.venue
                else:
                    titled = f"{label}: {left} {style} {right}"
                break
            if not titled or titled.lower() == before.lower():
                continue
            if not venue:
                arena = _ARENA.search(prog.get("desc") or "")
                venue = arena.group(1) if arena else ""
            prog["title"] = titled
            prog["sub"] = _sub_ok(titled, venue)
            # Description stays as the listing wrote it.
            count += 1
            when = prog["start"].astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
            if len(samples) < 8 or (before.lower() == "college basketball" and "fs1" in ch["name"].lower()):
                if len(samples) < 12:
                    samples.append((ch["name"], when, before, titled))
    log(f"  description/wider-window matchups added: {count:,}")
    return samples


_GOLF_TOURS = (
    ("pga", "PGA Tour", ("pga tour", "pga championship", "masters", "u.s. open", "us open", "the open")),
    ("eur", "DP World Tour", ("dp world", "european tour", "dp world tour")),
    ("lpga", "LPGA", ("lpga",)),
    ("liv", "LIV Golf", ("liv golf", "liv ")),
    ("ntw", "Korn Ferry Tour", ("korn ferry",)),
)
_GOLF_GENERIC = re.compile(
    r"^(?:live[:\s-]*)?(?:"
    r"golf|pga tour(?: golf)?|lpga(?: tour)?(?: golf)?|liv golf|"
    r"korn ferry(?: tour)?(?: golf)?|dp world tour(?: golf)?|"
    r"european tour(?: golf)?|champions tour(?: golf)?"
    r")$",
    re.I,
)


def _golf_course(cache: Path, refresh: bool, tour: str, event_id: str) -> tuple[str, str]:
    url = f"https://sports.core.api.espn.com/v2/sports/golf/leagues/{tour}/events/{event_id}"
    dest = cache / "espn" / f"golf-{tour}-{event_id}.json"
    data = _fetch_json(url, dest, refresh)
    if not data:
        return "", ""
    courses = data.get("courses") or []
    if not courses:
        return "", ""
    course = courses[0]
    name = (course.get("name") or "").strip()
    addr = course.get("address") or {}
    city = (addr.get("city") or "").strip()
    state = (addr.get("state") or "").strip()
    country = (addr.get("country") or "").strip()
    if state and country and country not in ("USA", "United States"):
        where = ", ".join(p for p in (city, f"{state}, {country}") if p)
    elif state:
        where = ", ".join(p for p in (city, state) if p)
    else:
        where = ", ".join(p for p in (city, country) if p)
    return name, where


def load_golf_events(cache: Path, refresh: bool, log) -> list[dict]:
    events = []
    for tour, label, _keys in _GOLF_TOURS:
        url = f"https://site.api.espn.com/apis/site/v2/sports/golf/{tour}/scoreboard"
        dest = cache / "espn" / f"golf-{tour}.json"
        try:
            data = _fetch_json(url, dest, refresh)
        except Exception as exc:
            log(f"WARNING: golf {tour}: {exc}")
            continue
        if not data:
            continue
        for ev in data.get("events") or []:
            eid = str(ev.get("id") or "")
            name = (ev.get("name") or ev.get("shortName") or "").strip()
            if not eid or not name:
                continue
            try:
                start = datetime.fromisoformat((ev.get("date") or "").replace("Z", "+00:00"))
                end = datetime.fromisoformat((ev.get("endDate") or ev.get("date") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            try:
                course, where = _golf_course(cache, refresh, tour, eid)
            except Exception as exc:
                log(f"WARNING: golf course {tour} {eid}: {exc}")
                course, where = "", ""
            broadcasts = []
            for comp in ev.get("competitions") or []:
                for cast in comp.get("broadcasts") or []:
                    broadcasts.extend(cast.get("names") or [])
            events.append({
                "tour": tour, "label": label, "name": name,
                "start": start, "end": end, "course": course, "where": where,
                "broadcasts": " ".join(broadcasts).lower(),
            })
    log(f"  golf events: {len(events):,}")
    return events


def _golf_location(event: dict) -> str:
    course = event.get("course") or ""
    where = event.get("where") or ""
    text = ", ".join(p for p in (course, where) if p)
    return f"Location: {text}." if text else ""


def apply_golf(playlist: list[dict], progs: dict[str, list[dict]],
               cache: Path, refresh: bool, log) -> list[tuple[str, str, str, str, str, str]]:
    """Put the course and city on golf listings, and name a generic title."""
    events = load_golf_events(cache, refresh, log)
    samples: list[tuple[str, str, str, str, str, str]] = []
    titled = 0
    located = 0
    seen: set[str] = set()
    for ch in playlist:
        cid = ch["id"]
        if cid in seen:
            continue
        seen.add(cid)
        channel_blob = (ch.get("name") or "").lower()
        for prog in progs.get(cid) or []:
            if prog.get("placeholder"):
                continue
            title = (prog.get("title") or "").strip()
            desc = prog.get("desc") or ""
            # Match the title or the channel. A description that merely
            # contains "Liv" or the word "golf" is not a golf listing.
            title_l = title.lower()
            blob = f"{title_l} {desc.lower()}"
            if not (
                re.search(r"\bgolf\b", f"{title_l} {channel_blob}")
                or re.search(r"\b(?:pga|lpga|korn ferry|dp world|liv golf)\b", title_l)
            ):
                continue
            start = prog.get("start")
            if start is None:
                continue
            overlapping = [ev for ev in events if ev["start"] <= start <= ev["end"]]
            if not overlapping:
                continue
            chosen = None
            for _tour, label, keys in _GOLF_TOURS:
                if any(k in blob for k in keys):
                    tour_hits = [ev for ev in overlapping if ev["label"] == label]
                    if len(tour_hits) == 1:
                        chosen = tour_hits[0]
                        break
            if chosen is None and len(overlapping) == 1:
                chosen = overlapping[0]
            if chosen is None and "golf" in channel_blob:
                aired = [ev for ev in overlapping if "golf" in ev["broadcasts"]]
                if len(aired) == 1:
                    chosen = aired[0]
            if chosen is None and overlapping:
                chosen = max(overlapping, key=lambda ev: ev["start"])
            if chosen is not None:
                keys = next(k for _t, label, k in _GOLF_TOURS if label == chosen["label"])
                mentions = any(k in blob for k in keys) or chosen["name"].lower() in blob
                generic = bool(_GOLF_GENERIC.match(_core_title(title)))
                # A show that names a different tour, or isn't a tournament listing,
                # does not inherit another event's course.
                if not mentions and not generic:
                    chosen = None
                elif generic and chosen["name"].lower() not in (desc or "").lower():
                    if re.search(r"\b(?:championship|classic|open|invitational|cup)\b",
                                 desc or "", re.I):
                        chosen = None
            if chosen is None:
                continue
            before_title, before_desc = title, desc
            core = _core_title(title)
            if _GOLF_GENERIC.match(core):
                prog["title"] = f"{chosen['label']}: {chosen['name']}"
                prog["sub"] = _sub_ok(prog["title"], chosen.get("course") or "")
                titled += 1
            loc = _golf_location(chosen)
            if loc and not (prog.get("desc") or "").lower().lstrip().startswith("location:"):
                existing = (prog.get("desc") or "").strip()
                prog["desc"] = f"{loc} {existing}".strip() if existing else loc
                located += 1
            if len(samples) < 8 and (before_title != prog["title"] or before_desc != prog.get("desc")):
                when = start.astimezone(TZ).strftime("%Y-%m-%d %H:%M %Z")
                samples.append((ch["name"], when, before_title, prog["title"],
                                before_desc[:180], (prog.get("desc") or "")[:180]))
    log(f"  golf titles set: {titled:,}; golf locations added: {located:,}")
    return samples
