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

## Credits

<img src="tmdb-logo.svg" alt="TMDB" width="56">

This product uses the [TMDB](https://www.themoviedb.org) API but is not endorsed or certified by TMDB.

This plugin is not affiliated with or endorsed by IMDb.

The TMDB logo belongs to TMDB and is not covered by this repository's MIT licence.

## Licence

MIT
