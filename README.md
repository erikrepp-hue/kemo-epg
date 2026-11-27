# kemo-epg

Custom XMLTV guide for the Kemo playlist in TiviMate.

## EPG URL

https://raw.githubusercontent.com/erikrepp-hue/kemo-epg/main/kemo-custom-epg.xml.gz

## Add it in TiviMate

1. Open **Settings**.
2. Open **Playlists**.
3. Select the playlist.
4. Open **EPG sources**.
5. Choose **Add EPG source**.
6. Paste the URL above.

TiviMate matches a channel on the playlist `tvg-id` when that id is unique to the station, and otherwise on the exact channel name. The first display name in the guide is that playlist name.

## Updates

GitHub Actions rebuilds the guide every day at 10:00 UTC. You can also run the **Build EPG** workflow by hand. Each run re-downloads the free public XMLTV sources. The provider snapshot in `data/kemo-epg-live.xml` is not refreshed from Actions (that needs a login); only programmes in it that have not started yet are reused. No credentials or stream URLs are stored in this repo.

Channel ids, display names, and logos come from `data/kemo-epg-channels.xml`. Names and groups are also listed in `data/kemo-full-channels.csv`.

A national cable channel (CBS Sports Network, ESPNU, FS1, Fox News, and the rest) is never matched to a local affiliate by a shared word or a call sign buried in the id. Empty or placeholder-only channels that are real stations are filled from the public listings (epgshare, Plex, DistroTV, Rakuten, Peacock, beIN, DirecTV sports). A local ABC, NBC, CBS, FOX, CW, PBS, or MyNetwork station with no station listing gets that network's national feed, and local newscasts on that copy are titled "Local News". Provider 24/7 and PPV slots stay placeholders unless a public channel has the same name.

On sports networks, including every ABC, NBC, CBS, and FOX station, a generic title such as "Live: NFL Football", "Football", "College Football", or "Live: NHL Hockey" is replaced when it lines up with a public ESPN scoreboard game (same league, same network, start within 60 minutes or inside the listing). If the scoreboard does not match, a wider window is tried, and otherwise two team names in the description ("the Sooners and the Boilermakers meet") are used. The description itself is kept. Golf listings get a "Location: course, city, country" line from the public tour schedule, and a generic golf title is renamed to the tournament. An NFL game that ESPN lists on Netflix, Prime Video, or Peacock is also applied to the local station for that team’s market. One game is titled like `NBA: Los Angeles Lakers at Boston Celtics`. When several games share that network and the channel name does not identify the market, the title lists them (`NFL: Chiefs at Bills / Eagles at Cowboys`). Pregame and postgame shows keep their names. The sub-title is the venue, not a copy of the title, because TiviMate shows both.

NBA, NHL, and NFL package channels (the `USA NBA`, `USA NHL`, and `USA NFL - Sunday Ticket` groups) are published under both the playlist id and the channel name. A channel named for a team gets that team’s game; otherwise channel 01 gets game 1. Each game is a three-hour block, with "No event scheduled" in between.

Single-show marathon channels (the `24/7` groups, and the same kind of Pluto, Samsung, Plex, Roku, and Rakuten channel) keep season and episode numbers when a public listing says which episode is on. The guide writes both `<episode-num system="xmltv_ns">` and `SxxExx`, puts the episode title in the sub-title, and starts the description with `S1 E5 · Episode Title`. A marathon with no listing for the episode stays the show name plus a short series description (seasons and years). It does not get an invented episode number.

Numbered event and PPV channels (NBA 01, NCAAF 03, ESPN+ 12, and similar) are a **best-guess** mapping. If the channel name already contains a matchup or a start time, that is what airs, for three hours. Otherwise a single-league number is lined up with that day's public games in start-time order (game 1 on channel 01, game 2 on channel 02, and so on). Outside those windows the guide says "No event scheduled". Other 24/7 channels with no real schedule still get two-hour placeholders titled with the channel name.

The guide is published as gzip only. The uncompressed XML is about 117 MB, over GitHub's 100 MB file limit. Daily updates amend a single `epg` commit on `main`, so the raw URL stays the same and the repository history does not grow with every build.
