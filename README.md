# IMDb Titles

A plugin for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).

Renames VOD movies and series to their official titles, looked up by each title's TMDB ID. Removes provider prefixes and tags like `EN - ... (US)`. It can also merge many provider categories into fewer shared ones, such as Netflix, Disney+ or Action & Adventure.

## Install

Copy this repository's files into `/data/plugins/imdb_titles` in your Dispatcharr data folder, then enable **IMDb Titles** on the Plugins page.

## Use

1. Add a free [TMDB](https://www.themoviedb.org/settings/api) API key in the plugin settings.
2. **Rename Run** starts as a dry run, so check the results before turning dry run off.
3. Original names are kept, so **Revert All Renames** puts everything back.

## Notes

- Titles without a TMDB ID are left alone, because renaming them would create duplicates on the next VOD refresh.

## Licence

MIT
