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

Channel ids, display names, and logos come from `data/kemo-epg-channels.xml`. Names and groups are also listed in `data/kemo-full-channels.csv`. Event, PPV, and 24/7 channels with no real schedule get two-hour placeholders titled with the channel name.

The guide is published as gzip only. The uncompressed XML is about 117 MB, over GitHub's 100 MB file limit. Daily updates amend a single `epg` commit on `main`, so the raw URL stays the same and the repository history does not grow with every build.
