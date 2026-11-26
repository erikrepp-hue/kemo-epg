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

On sports networks, a generic title such as "NBA Basketball" or "Live: NHL Hockey" is replaced when it lines up with a public ESPN scoreboard game (same league, same network, start time within 30 minutes). The new title looks like `NBA: Los Angeles Lakers at Boston Celtics`, with the original title and the venue in the description. If the listing already has a specific title, the matchup is added as the sub-title instead.

Numbered event and PPV channels (NBA 01, NCAAF 03, ESPN+ 12, and similar) are a **best-guess** mapping. If the channel name already contains a matchup or a start time, that is what airs, for three hours. Otherwise a single-league number is lined up with that day's public games in start-time order (game 1 on channel 01, game 2 on channel 02, and so on). Outside those windows the guide says "No event scheduled". Other 24/7 channels with no real schedule still get two-hour placeholders titled with the channel name.

The guide is published as gzip only. The uncompressed XML is about 117 MB, over GitHub's 100 MB file limit. Daily updates amend a single `epg` commit on `main`, so the raw URL stays the same and the repository history does not grow with every build.
