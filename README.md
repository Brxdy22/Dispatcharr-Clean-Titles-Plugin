<img src="logo.png" alt="" width="96">

# Clean Titles

A plugin for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).

Renames VOD movies and series to their official titles, looked up by each title's TMDB ID. Removes provider prefixes and tags like `EN - ... (US)`. It can also merge many provider categories into fewer shared ones, such as Netflix, Disney+ or Action & Adventure.

Tested on Dispatcharr 0.33.0.

## Install

Copy this repository's files into `/data/plugins/clean_titles` in your Dispatcharr data folder, then enable **Clean Titles** on the Plugins page.

Or download `clean_titles-1.15.0.zip` from Releases and import it on the Plugins page. Use that zip rather than GitHub's source zip, because Dispatcharr names the plugin after the zip's top folder.

## Use

1. Add a free [TMDB](https://www.themoviedb.org/settings/api) API key in the plugin settings.
2. **Rename Run** starts as a dry run, so check the results before turning dry run off.
3. Original names are kept, so **Revert All Renames** puts everything back.

## Notes

- Titles without a TMDB ID are left alone, because renaming them would create duplicates on the next VOD refresh.

## Changelog

Versions before 1.15.0 were released under the name IMDb Titles.

### 1.15.0
- Renamed to Clean Titles and added the TMDB credit.
- When a title is on several services, bigger services now win, paid and free alike. Services named in the service order setting still go first.

### 1.14.0
- Other services TMDB lists can now get their own category, if they have enough titles. Added the **Minimum titles for other services** setting.

### 1.13.0
- Added settings for where movies in theaters go, and where titles on no streaming service go.

### 1.12.0
- Added last-resort services, which are only chosen when no other listed service streams the title.
- Variants of a service now count as that service, so `Netflix Kids` goes to Netflix.

### 1.10.0 to 1.11.0
- Added **Apply Category Groups** and **Revert Category Groups**, which merge many provider categories into fewer shared ones.
- Added settings for category groups, streaming service order, streaming region and filing genre titles under their service.
- Category groups are applied again after each VOD scan.
- No detailed record of 1.10.0 was kept.

### 1.9.0
- Titles are now handled automatically after an M3U refresh: the plugin waits for the VOD scan, then fixes posters and renames that account's titles. A refresh that arrives during a run is queued.

### 1.8.0
- Added **Fix Images**.

### 1.7.0
- Added **Unify Series** and **Revert Unify**.

### 1.6.0
- Removed **Merge Duplicates** and its settings.

### 1.2.0 to 1.5.0
- Added **Merge Duplicates** in 1.2.0, with a limit per run and a speed test for each copy.
- No detailed record of 1.3.0 to 1.5.0 was kept.

### 1.1.0
- Added **Tag Duplicates**.

### 1.0.1 and 1.0.2
- Renamed **Stop** to **Stop Run** in 1.0.1.
- No detailed record of 1.0.2 was kept.

### 1.0.0
- First version: renames movies and series to their official titles using each title's TMDB ID, and removes provider prefixes and tags.
- Original names are kept, so renames can be reverted. Starts as a dry run.

## Credits

<img src="tmdb-logo.svg" alt="TMDB" width="56">

This product uses the [TMDB](https://www.themoviedb.org) API but is not endorsed or certified by TMDB.

This plugin is not affiliated with or endorsed by IMDb.

The TMDB logo belongs to TMDB and is not covered by this repository's MIT licence.

## Licence

MIT
