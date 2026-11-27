"""Season and episode labels for single-show marathon channels.

Public FAST listings (Pluto, Samsung, Plex, Rakuten, Roku) already name the
episode that is on. Those numbers are kept and written as XMLTV xmltv_ns and
onscreen. TVmaze, which needs no key, supplies the episode title for a known
number, or the number for a known episode title.

A marathon with no listing that says which episode is on does not get an
invented number. It gets the series name and a TVmaze synopsis with the season
count and years.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import build_epg

# Marathon groups that are one series, not a movie shelf or a person.
_SKIP_LOOKUP = {
    "24/7 Adult",
    "Adults 24-7",
    "24/7 Movie Series",
    "24/7 Movie Categories",
    "24/7 Actors and Actresses",
    "24/7 Music",
    "24/7 Sports Replay",
}
_ONSCREEN = re.compile(r"S\s*0*(\d+)\s*E\s*0*(\d+)", re.I)
_CROSSED = re.compile(r"\b0*(\d+)\s*x\s*0*(\d+)\b", re.I)
_XMLTV_NS = re.compile(r"^(\d+)\.(\d+)(?:\.|$)")
_HTML = re.compile(r"<[^>]+>")
_LEAD = re.compile(r"^S\d+\s+E\d+\b", re.I)


def parse_season_episode(text: str) -> tuple[int, int] | None:
    """Return 1-based season and episode, or None when the text is not one."""
    raw = (text or "").strip()
    if not raw:
        return None
    match = _ONSCREEN.search(raw) or _CROSSED.search(raw)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
    else:
        ns = _XMLTV_NS.match(raw)
        if not ns:
            return None
        season, episode = int(ns.group(1)) + 1, int(ns.group(2)) + 1
    if season < 1 or episode < 1 or season > 80 or episode > 500:
        return None
    return season, episode


def best_episode_numbers(tagged: dict[str, str]) -> tuple[int, int] | None:
    """Prefer an on-screen SxxExx over xmltv_ns. Ignore Pluto ids and air dates."""
    for system in ("onscreen", "xmltv_ns", ""):
        found = parse_season_episode(tagged.get(system) or "")
        if found:
            return found
    for system, text in tagged.items():
        if system in ("pluto", "original-air-date", "dd_progid"):
            continue
        found = parse_season_episode(text)
        if found:
            return found
    return None


def is_marathon(ch: dict) -> bool:
    blob = f"{ch.get('category') or ''} {ch.get('name') or ''}".lower()
    return "24/7" in blob or "24-7" in blob


def series_query(name: str) -> str:
    key = build_epg.normalize_name(name, keep_feed=False)
    key = re.sub(r"\b(?:24\s*/\s*7|24-7|24 7)\b", " ", key)
    return build_epg.SPACE_RE.sub(" ", key).strip()


def _tokens(text: str) -> set[str]:
    folded = build_epg.normalize_name(text, keep_feed=False)
    skip = {"the", "a", "an", "and", "old", "classic", "original"}
    return {tok for tok in folded.split() if tok not in skip and not tok.isdigit()}


def _premiere_year(show: dict) -> int:
    year = (show.get("premiered") or "")[:4]
    return int(year) if year.isdigit() else 9999


def _choose_show(query: str, rows: list) -> dict | None:
    """Pick one series. A tied new reboot loses to the established show of that name."""
    exact = []
    for row in rows or []:
        show = row.get("show") or {}
        if names_agree(query, show.get("name") or ""):
            exact.append((row.get("score") or 0, show))
    if not exact:
        return None
    year_match = re.search(r"\b(?:19|20)\d{2}\b", query)
    if year_match:
        want = int(year_match.group(0))
        ranked = sorted(exact, key=lambda item: abs(_premiere_year(item[1]) - want))
        if abs(_premiere_year(ranked[0][1]) - want) <= 2:
            return ranked[0][1]
        return None
    if re.search(r"\b(?:old|classic|original)\b", query):
        dated = [show for _score, show in exact if _premiere_year(show) != 9999]
        return min(dated, key=_premiere_year) if dated else None
    exact.sort(key=lambda item: -item[0])
    top = exact[0][1]
    if _premiere_year(top) >= 2023 or _premiere_year(top) == 9999:
        older = [show for _score, show in exact if _premiere_year(show) <= 2018]
        if older:
            return older[0]
    return top


def names_agree(query: str, showname: str) -> bool:
    left, right = _tokens(query), _tokens(showname)
    if not left or not right:
        return False
    return left == right


def _plain(html: str) -> str:
    text = _HTML.sub("", html or "")
    text = (text.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
            .replace("&apos;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    return build_epg.SPACE_RE.sub(" ", text).strip()


def _sub_ok(title: str, sub: str) -> str:
    sub = (sub or "").strip()
    title = (title or "").strip()
    if not sub or not title:
        return ""
    left, right = title.lower(), sub.lower()
    if right == left or right in left or left in right:
        return ""
    if parse_season_episode(sub):
        return ""
    return sub


def _prefix(desc: str, season: int, episode: int, episode_title: str) -> str:
    lead = f"S{season} E{episode}"
    if episode_title:
        lead = f"{lead} · {episode_title}"
    body = (desc or "").strip()
    if _LEAD.match(body):
        return body
    return f"{lead}. {body}".strip() if body else lead


class TVmaze:
    """Polite client. Results are stored in data/tvmaze-cache.json."""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"shows": {}, "episodes": {}}
        if path.exists() and path.stat().st_size > 2:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self.data["shows"] = loaded.get("shows") or {}
                self.data["episodes"] = loaded.get("episodes") or {}
            except json.JSONDecodeError:
                pass
        self.network = 0
        self._last = 0.0

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.part")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        tmp.replace(self.path)

    def _get(self, url: str):
        wait = 0.55 - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        last_err = None
        for attempt in range(4):
            self._last = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": build_epg.UA,
                                                           "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=40) as resp:
                    self.network += 1
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                self.network += 1
                if exc.code == 404:
                    return None
                if exc.code == 429:
                    retry = exc.headers.get("Retry-After") if exc.headers else None
                    time.sleep(int(retry) if retry and str(retry).isdigit() else 8)
                    last_err = exc
                    continue
                if 400 <= exc.code < 500:
                    return None
                last_err = exc
                time.sleep(min(8, 2 ** attempt))
            except Exception as exc:
                last_err = exc
                time.sleep(min(8, 2 ** attempt))
        raise RuntimeError(last_err)

    def show(self, query: str) -> dict | None:
        key = series_query(query) or build_epg.normalize_name(query, keep_feed=False)
        if len(key) < 4:
            return None
        cached = self.data["shows"].get(key)
        if cached is not None:
            return cached if cached.get("matched") else None
        # A year or "old" in the channel name is for us, not the search box.
        # "doctor who 2005" has to search as "doctor who" or TVmaze returns nothing.
        lookup = re.sub(r"\b(?:19|20)\d{2}\b", " ", key)
        lookup = re.sub(r"\b(?:old|classic|original)\b", " ", lookup)
        lookup = build_epg.SPACE_RE.sub(" ", lookup).strip() or key
        url = "https://api.tvmaze.com/search/shows?q=" + urllib.request.quote(lookup)
        try:
            rows = self._get(url) or []
        except Exception:
            return None
        chosen = _choose_show(query, rows)
        if not chosen:
            # "stargate sg 1" is indexed as "sg-1".
            hyphen = re.sub(r"\b([a-z]{1,6}) (\d+)\b", r"\1-\2", lookup)
            if hyphen != lookup:
                try:
                    rows = self._get("https://api.tvmaze.com/search/shows?q=" + urllib.request.quote(hyphen)) or []
                except Exception:
                    rows = []
                chosen = _choose_show(query, rows)
        if not chosen:
            self.data["shows"][key] = {"matched": False}
            if self.network and self.network % 40 == 0:
                self.save()
            return None
        detail = None
        if chosen.get("id"):
            try:
                detail = self._get(f"https://api.tvmaze.com/shows/{chosen['id']}?embed=seasons")
            except Exception:
                detail = None
        payload = detail or chosen
        seasons = (payload.get("_embedded") or {}).get("seasons") or []
        record = {
            "matched": True,
            "id": payload.get("id"),
            "name": payload.get("name") or query,
            "premiered": (payload.get("premiered") or "")[:4],
            "ended": (payload.get("ended") or "")[:4],
            "seasons": len(seasons) or None,
            "summary": _plain(payload.get("summary") or "")[:700],
        }
        self.data["shows"][key] = record
        if self.network and self.network % 40 == 0:
            self.save()
        return record

    def episodes(self, show_id: int) -> list[dict]:
        key = str(show_id)
        cached = self.data["episodes"].get(key)
        if cached is not None:
            return cached
        payload = self._get(f"https://api.tvmaze.com/shows/{show_id}/episodes")
        rows = []
        for item in payload or []:
            season, number = item.get("season"), item.get("number")
            name = (item.get("name") or "").strip()
            if not season or not number or not name:
                continue
            rows.append({"s": int(season), "e": int(number), "name": name})
        self.data["episodes"][key] = rows
        if self.network and self.network % 25 == 0:
            self.save()
        return rows


def _blurb(record: dict) -> str:
    parts = []
    seasons = record.get("seasons")
    start, end = record.get("premiered") or "", record.get("ended") or ""
    if seasons and start and end:
        parts.append(f"{seasons} seasons, {start}–{end}.")
    elif seasons and start:
        parts.append(f"{seasons} seasons, {start}–present.")
    elif start and end:
        parts.append(f"Aired {start}–{end}.")
    elif start:
        parts.append(f"Premiered {start}.")
    if record.get("summary"):
        parts.append(record["summary"])
    return " ".join(parts)


def _apply_known(prog: dict, season: int, episode: int, episode_title: str) -> None:
    prog["season"] = season
    prog["epnum"] = episode
    title = prog.get("title") or ""
    sub = _sub_ok(title, episode_title or prog.get("sub") or "")
    prog["sub"] = sub
    shown = sub or (episode_title if episode_title and episode_title.lower() not in title.lower() else "")
    prog["desc"] = _prefix(prog.get("desc") or "", season, episode, shown or episode_title)


def _lookup_title(rows: list[dict], title: str) -> dict | None:
    want = build_epg.normalize_name(title, keep_feed=False)
    if len(want) < 4:
        return None
    hits = [row for row in rows if build_epg.normalize_name(row["name"], keep_feed=False) == want]
    if len(hits) == 1:
        return hits[0]
    return None


def apply_episode_numbers(playlist: list[dict], progs: dict, cache_path: Path, log) -> dict:
    """Label real episodes and describe marathons whose episode is unknown."""
    maze = TVmaze(cache_path)
    stats = {
        "marathon": 0,
        "with_numbers": 0,
        "synopsis": 0,
        "unmatched": 0,
        "programmes": 0,
        "samples": [],
        "unmatched_examples": [],
    }
    seen: set[str] = set()
    try:
        for ch in playlist:
            if not is_marathon(ch):
                continue
            cid = ch["id"]
            if cid in seen:
                continue
            seen.add(cid)
            stats["marathon"] += 1
            items = progs.get(cid) or []
            query = series_query(ch.get("name") or "")
            lookup = (ch.get("category") or "") not in _SKIP_LOOKUP and len(query) >= 4
            real = [p for p in items if not p.get("placeholder")]
            needs_catalog = False
            for prog in real:
                if not prog.get("season"):
                    found = parse_season_episode(prog.get("episode") or "")
                    if found:
                        prog["season"], prog["epnum"] = found
                has_number = bool(prog.get("season") and prog.get("epnum"))
                sub = (prog.get("sub") or "").strip()
                if has_number and not _sub_ok(prog.get("title") or "", sub):
                    needs_catalog = True
                if not has_number and _sub_ok(prog.get("title") or "", sub):
                    needs_catalog = True
            record = maze.show(query) if lookup and (needs_catalog or not real) else None
            catalog = maze.episodes(record["id"]) if record and needs_catalog and record.get("id") else []
            numbered = False
            for prog in real:
                season, episode = prog.get("season"), prog.get("epnum")
                episode_title = _sub_ok(prog.get("title") or "", prog.get("sub") or "")
                if not (season and episode) and episode_title and catalog:
                    hit = _lookup_title(catalog, episode_title)
                    if hit:
                        season, episode = hit["s"], hit["e"]
                        episode_title = hit["name"]
                if season and episode and not episode_title and catalog:
                    hit = next((row for row in catalog if row["s"] == season and row["e"] == episode), None)
                    if hit:
                        episode_title = hit["name"]
                if not (season and episode):
                    continue
                _apply_known(prog, int(season), int(episode), episode_title)
                numbered = True
                stats["programmes"] += 1
                own = sum(1 for row in stats["samples"] if row[0] == ch["name"])
                if own < 2 and len(stats["samples"]) < 8:
                    when = prog["start"].astimezone(build_epg.TZ).strftime("%Y-%m-%d %H:%M %Z")
                    stats["samples"].append((
                        ch["name"], when, prog.get("title") or "",
                        f"S{int(season):02d}E{int(episode):02d}",
                        episode_title or prog.get("sub") or "",
                    ))
            if numbered:
                stats["with_numbers"] += 1
                continue
            if record and items and all(
                    p.get("placeholder") or (p.get("desc") or "") in ("", "No guide data") for p in items):
                text = _blurb(record)
                if text:
                    for prog in items:
                        prog["title"] = record["name"]
                        prog["desc"] = text
                        prog["sub"] = ""
                        prog["season"] = None
                        prog["epnum"] = None
                    stats["synopsis"] += 1
                    continue
            if lookup:
                stats["unmatched"] += 1
                if len(stats["unmatched_examples"]) < 15:
                    if not record and not real:
                        why = "no public listing, and TVmaze has no series under this exact name"
                    elif record and not real:
                        why = "TVmaze has the series, but no listing says which episode is on"
                    else:
                        why = "the listing does not give a unique season and episode"
                    stats["unmatched_examples"].append((ch["name"], why))
    finally:
        maze.save()
    log(f"  marathon channels: {stats['marathon']:,}; "
        f"with a real episode number: {stats['with_numbers']:,}; "
        f"series synopsis only: {stats['synopsis']:,}; "
        f"no series match: {stats['unmatched']:,}; "
        f"TVmaze requests this run: {maze.network:,}")
    return stats
