<img src="logo.png" alt="" width="96">

# Clean Titles

A plugin for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).

Renames VOD movies and series to their official titles, looked up by each title's TMDB ID. Removes provider prefixes and tags like `EN - ... (US)`. It can also replace the providers' many categories with the streaming services you select, such as Netflix, Disney+ or Tubi, in your order of priority.

Tested on Dispatcharr 0.33.0.

## Install

Copy this repository's files into `/data/plugins/clean_titles` in your Dispatcharr data folder, then enable **Clean Titles** on the Plugins page.

Or download `clean_titles-2.2.zip` from Releases and import it on the Plugins page. Use that zip rather than GitHub's source zip, because Dispatcharr names the plugin after the zip's top folder.

## Use

1. Add a free [TMDB](https://www.themoviedb.org/settings/api) API key in the plugin settings.
2. **Rename Run** starts as a dry run, so check the results before turning dry run off.
3. Original names are kept, so **Revert All Renames** puts everything back.
4. Pick your services in **Select Streaming Services to Use + Priority**, then run **Apply Streaming Categories**. **Revert Streaming Categories** puts titles back in their provider categories.
5. Titles only for rent or sale go to their own category, **Rent/Buy** by default.
6. To remove titles in other languages, run **Preview Language Filter**, turn on **Remove titles in other languages**, then press **Remove**. It deletes titles, because Dispatcharr can't hide them, and runs again after each VOD scan. Turning it off and refreshing the account brings them back with new IDs.

## Notes

- Titles without a TMDB ID are left alone, because renaming them would create duplicates on the next VOD refresh.

## Drag and search

Dispatcharr's multi-select can't be sorted or searched, so this plugin adds both to **Select Streaming Services to Use + Priority**: drag a service to change its priority, and type to search the list.

It does this by adding a small script to Dispatcharr's web page as the page is sent, on every page while the plugin is enabled. No files are changed. Turn off **Drag and search in the service list** to remove it, then refresh the page. Without it, priority is the order you pick services.

Dispatcharr needs a restart after installing for this to reach every page load.

## More by Brxdy22

- [M3U Failover](https://github.com/Brxdy22/Dispatcharr-M3U-Failover): Proxy that gives an M3U account several provider URLs and fails over between them. Docker image: [brxdy22/m3u-failover](https://hub.docker.com/r/brxdy22/m3u-failover).
- [VOD Failover](https://github.com/Brxdy22/Dispatcharr-VOD-Failover-Plugin): Retries a failed VOD stream on the next provider account that carries the title.
- [Gluetun AutoSwitch](https://github.com/Brxdy22/Dispatcharr-Gluetun-AutoSwitch-Plugin): Moves Gluetun to another VPN exit when streams slow down, VOD is refused or speed tests fail.
- [Teamarr Assistant](https://github.com/Brxdy22/Dispatcharr-Teamarr-Assistant-Plugin): Tags regional sports networks with their teams and starts Teamarr generations when new streams appear.

## Release notes

See [Releases](https://github.com/Brxdy22/Dispatcharr-Clean-Titles-Plugin/releases/latest).

## Credits

<img src="tmdb-logo.svg" alt="TMDB" width="56">

This product uses the [TMDB](https://www.themoviedb.org) API but is not endorsed or certified by TMDB.

This plugin is not affiliated with or endorsed by IMDb.

The TMDB logo belongs to TMDB and is not covered by this repository's MIT licence.

## Licence

MIT
