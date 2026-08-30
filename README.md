# Jellyfin Library Auditor

Jellyfin Library Auditor is a Python CLI that connects to one or more Jellyfin servers, audits movie and TV libraries, and generates CSV and static HTML reports.

It is designed to highlight library gaps that are easy to miss in day-to-day use, such as missing English subtitles, missing artwork, unknown codecs, and missing numbered TV seasons or episodes. It can also compare two Jellyfin servers and generate side-by-side comparison pages.

## Current audit coverage

The current implementation checks:

- Missing configured English subtitles
- Missing local backdrop files
- Missing Jellyfin primary images
- Missing or unknown primary video codecs
- Missing primary audio codecs
- Missing numbered TV seasons within a series (when a TheTVDB `api_key` is configured in `servers.toml`, checked against TheTVDB's full season list, catching seasons missing entirely rather than only gaps between seasons already on disk; season 0/specials is never reported missing, since TheTVDB's specials coverage is too inconsistent across series to treat a missing one as a real gap)
- Missing numbered TV episodes within a season (same TheTVDB cross-check, so an episode missing after the last one on disk - e.g. episodes 1-9 present but not the season's 10th - is caught too, not just internal gaps)
- TV episode metadata titles that don't match the title implied by the filename's `SxxExx` naming (including `SxxEyy-Ezz` multi-episode ranges)
- TV episode metadata titles that don't match the title implied by an embedded video/audio stream title (some rips, e.g. from mkvmerge, preserve the original scene-release filename in a stream's title even after the file itself is renamed - this catches mislabeled episodes a filename-only check can't, since the renamed filename and Jellyfin's metadata can otherwise agree with each other while both being wrong)
- Movie metadata titles that don't match the title implied by the filename's `(Year)` naming
- A series matched to the wrong TheTVDB entry (when a TheTVDB `api_key` is configured): if most of a series' local (season, episode) numbers don't correspond to any TheTVDB episode at that position in either aired order or DVD order, the match itself - not any one episode - is probably wrong (e.g. a series with the same title as a different show, matched to that other show's TheTVDB id). Checking both orderings avoids flagging a series that's simply numbered on disk in DVD order. When a Jellyfin library has more than one series sharing the exact same name (e.g. TheTVDB splitting a long-running show into a separate entry for a newer era while the old entry keeps the earlier episodes, both still titled the same in Jellyfin), local episodes are checked against the union of every same-named TheTVDB id's episodes rather than just whichever one happened to be looked up - otherwise episodes belonging to "the other half" of the split show would be flagged as unmatched. Flagged only once a series has at least 5 local episodes and at least half of them are unmatched, so a thin or partially-numbered library doesn't trigger it on noise. When this fires for a series, that series' missing-season/-episode checks fall back to internal-gap detection instead of piling on nonsense findings sourced from the wrong show's episode list. The finding then searches TheTVDB by series name for a same-named alternative (e.g. a same-titled show from a different country) and, when one explains at least 90% of the local episodes, names it directly in the finding's message as the entry to re-identify the series to in Jellyfin - checked against up to 5 search candidates per series
- Optionally (`--check-episode-order`, requires a TheTVDB `api_key` in `servers.toml`): a local TV episode's metadata title doesn't match TheTVDB's aired-order title at its season/episode position, and TheTVDB's DVD order is also available to compare against - catches series stored on disk in one ordering but labeled with the other, where the filename, season number, and episode number all still look correct. Only a title matching neither ordering is flagged as a genuine discrepancy to check by eye; a title matching DVD order instead of aired order isn't reported at all, since a series correctly organized end-to-end in DVD order is expected to disagree with aired order at every single episode. Without DVD-order data for that position, a local title that merely disagrees with aired order isn't reported on its own. A candidate title still in its original, untranslated language (no English translation on file with TheTVDB) is ignored, since there's no way to tell whether it matches or not. A filename naming a multi-episode range (e.g. `S01E05-E07`) is compared against all of those episodes' TheTVDB titles joined together rather than just the first one's

The filename-title checks tolerate cosmetic differences that don't represent a real mismatch: dot-delimited release names (`Show.S01E02.Episode.Title.mkv`), straight vs. curly quotes and dashes, roman vs. arabic numerals in a parenthetical suffix (`(I)` vs. `(1)`), `&` vs. `and`, `+` vs. `/`, a Unicode ellipsis (`…`) vs. three literal periods (`...`), and leading parenthetical text that's actually part of the title (e.g. `(Dis)Members Only`). Release-quality tags in a filename (`1080p`, `WEB-DL`, `x264`, etc.) are stripped only when they form a genuine trailing run of tags, so an ordinary title word that happens to look like one - an episode called "Spider in the Web," for example - isn't mistaken for one.

## Features

- Audits enabled movie and TV libraries from Jellyfin
- Produces CSV output for spreadsheet-style review
- Produces a static HTML dashboard with library and check drill-down pages
- Produces a combined Excel workbook (`audit_results.xlsx`) alongside the CSV/HTML reports, with one named table per server plus a `diffs` table when `--compare` is used, and conditional formatting that highlights notable cells yellow
- Report tables show a live row count next to each heading that updates as you search, and columns auto-size to their content
- Report navigation shows the audited server's name
- Supports filtering by library, finding category, and severity
- Supports auditing one server, all configured servers, or comparing two servers
- Can transfer mismatched metadata (title, overview, genres, cast, provider IDs, ratings, etc. - never images) from one server to another, either one item at a time from the comparison report or in bulk across a whole comparison run
- Uses normalized data models to keep audit logic separate from API and report code

## Requirements

- Python 3.12+
- A reachable Jellyfin server
- A Jellyfin API key for each server you want to audit - an administrator key if you plan to use metadata transfer, since that writes to the destination server

Install dependencies:

```powershell
pip install -r requirements.txt
```

## Configuration

Server configuration lives in `servers.toml`.

Example:

```toml
default_server = "main"

[servers.main]
name = "Main Jellyfin"
url = "http://your-jellyfin-host:8096"
api_key = "your_api_key_here"

[servers.backup]
name = "Backup Jellyfin"
url = "http://your-backup-host:8096"
api_key = "your_backup_api_key_here"

[tvdb]
api_key = "your_thetvdb_v4_api_key_here"
```

The optional `[tvdb]` table holds the TheTVDB v4 API key. When `api_key` is set, missing-season and missing-episode detection automatically cross-check TheTVDB's full episode list; `--check-episode-order` additionally enables the aired/DVD title-mismatch check. Omit the table, or leave `api_key` empty, to leave all TheTVDB-backed features disabled.

### Environment variables

Optional environment variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `REPORT_MEDIA_PATH_PREFIX` | Removes a path prefix from report display paths | empty |
| `MOVIES_CSV_FILENAME` | Movie CSV filename | `movies_report.csv` |
| `TV_CSV_FILENAME` | TV CSV filename | `tv_report.csv` |
| `AUDIT_CSV_FILENAME` | Per-server audit CSV filename suffix (prefixed with the server name, e.g. `MyServer_audit.csv`) | `audit.csv` |
| `AUDIT_HTML_FILENAME` | Root HTML output directory name | `audit_results` |
| `ENABLE_MOVIES` | Enable movie library auditing | `true` |
| `ENABLE_TV` | Enable TV library auditing | `true` |
| `ENGLISH_LANGUAGE_CODES` | Comma-separated language codes treated as English subtitles | `en,eng,` |
| `TVDB_CACHE_TTL_DAYS` | How many days a cached TheTVDB episode-ordering lookup stays valid before `--check-episode-order` re-fetches it | `7` |

## Usage

Run a standard audit against the default server:

```powershell
python auditor.py
```

Audit a specific configured server:

```powershell
python auditor.py --server main
```

Audit only selected libraries:

```powershell
python auditor.py --library Movies --library Shows
```

Generate only HTML or only CSV output:

```powershell
python auditor.py --html
python auditor.py --csv
```

Filter findings:

```powershell
python auditor.py --category subtitles --severity warning
```

Compare two servers:

```powershell
python auditor.py --server main --compare backup
```

Compare the first two configured servers:

```powershell
python auditor.py --compare
```

Audit every configured server:

```powershell
python auditor.py --all
```

Transfer every mismatched metadata item found by `--compare` from the base server to the compared server, previewing first and then committing:

```powershell
python auditor.py --server main --compare backup --transfer-metadata --dry-run
python auditor.py --server main --compare backup --transfer-metadata
```

### CLI options

| Option | Description |
| --- | --- |
| `--server SERVER` | Audit one configured server |
| `--compare [SERVER]` | Compare against another server, or compare the first two configured servers when used alone |
| `--all` | Audit every configured server |
| `--html` | Write HTML output |
| `--csv` | Write CSV output |
| `--library NAME` | Limit auditing to a library name; repeatable |
| `--category CATEGORY` | Filter by category: `subtitles`, `artwork`, `metadata`, `episode_order`, `video`, `audio`, `filesystem` |
| `--severity SEVERITY` | Filter by severity: `info`, `warning`, `error` |
| `--check-episode-order` | Check each TV episode's title against TheTVDB's aired-order title at its season/episode position, flagging a mismatch only when DVD-order data is also available there and the local title matches neither ordering (a title matching DVD order instead of aired order isn't flagged); requires a TheTVDB `api_key` in the `[tvdb]` table of `servers.toml`. (Missing-season/-episode detection uses TheTVDB whenever `api_key` is set, with or without this flag.) Results are cached to `tvdb_cache.json` (see `TVDB_CACHE_TTL_DAYS`) so repeat runs skip TheTVDB entirely for series with a fresh cache entry |
| `--refresh-tvdb-cache` | With `--check-episode-order`, ignore cached TheTVDB lookups and fetch fresh data for every series this run (still updates the cache) |
| `--transfer-metadata` | Transfer metadata for every mismatched item from the base `--server` to the `--compare` server; requires `--compare` (see [Synchronizing metadata between servers](#synchronizing-metadata-between-servers)) |
| `--transfer-images` | Transfer cached images (Primary, Backdrop, Thumb) for every item with an artwork difference from the base `--server` to the `--compare` server; requires `--compare` (see [Synchronizing images between servers](#synchronizing-images-between-servers)) |
| `--transfer-subtitles` | Transfer the English subtitle track for every item with a subtitle difference from the base `--server` to the `--compare` server; requires `--compare` (see [Synchronizing subtitles between servers](#synchronizing-subtitles-between-servers)) |
| `--dry-run` | With `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles`, preview planned transfers without writing anything |
| `--yes` | With `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles`, skip the batch confirmation prompt |
| `--limit N` | With `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles`, only attempt the first N items found, regardless of outcome - useful for quickly testing bulk-mode changes without waiting for a full run |
| `--verify` | With `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles`, re-audit the `--compare` server once transfers finish and write the comparison report from that post-transfer state instead of the pre-transfer snapshot; ignored with `--dry-run`, or if no item was actually transferred |

## Synchronizing metadata between servers

When `--compare` finds items whose metadata differs between the two servers (the "Mismatched Metadata" table), that metadata can be copied from the base server to the compared server - one item at a time from the report, or in bulk for the whole comparison.

**What transfers:** title, original title, overview, genres, tags, studios, cast/crew, provider IDs (IMDb/TVDB/etc.), community rating, official rating, release date, year, and episode/season number. **What never transfers:** artwork/images, file path, and any other server-managed or read-only data.

Every transfer - one-off or bulk - reads the full item from both servers, computes what would change, and refuses to write anything if the destination item's identity fields (`Id`, `Path`) would come back empty, since Jellyfin's update endpoint replaces an item's metadata wholesale rather than merging it.

### One item at a time

Each row in the comparison report's "Mismatched Metadata" table has a → button between the two servers' episode name columns. Clicking it copies a ready-made command to your clipboard:

```powershell
python transfer_metadata.py --from-server main --from-item <id> --to-server backup --to-item <id>
```

Paste and run it in a terminal. It prints the item names and a field-by-field diff, then asks for confirmation before writing (skip the prompt with `--yes`).

### In bulk, across a whole comparison

`auditor.py --transfer-metadata` walks every item in the "Mismatched Metadata" table and transfers each one, continuing past a single item's failure or rejection rather than aborting the whole batch. It asks for one confirmation covering the whole batch (skip it with `--yes` for unattended/scheduled runs), and `--dry-run` previews every planned change without writing anything.

The comparison report gains a "Transfer Results" table (Libraries page) whenever `--transfer-metadata` was used, showing each item's outcome - transferred, would transfer (`--dry-run`), unchanged, rejected, or failed - along with which fields changed.

Both the one-off command and the bulk flag append a record of every transfer to `metadata_transfer.log` in the working directory, so a scheduled/unattended run leaves an audit trail even if nobody was watching the console.

Add `--limit N` to only attempt the first N items found - handy for quickly testing a code change against a large library without waiting for a full run.

**Given this writes to a live Jellyfin server, run `--dry-run` first and review the diff before trusting it on a real library, especially unattended.**

## Synchronizing images between servers

When `--compare` finds items whose artwork presence differs between the two servers (the "Artwork Differences" table), Jellyfin's own cached images (not local poster files on disk) can be copied from the base server to the compared server for every such item in one pass:

```powershell
python auditor.py --server main --compare backup --transfer-images --dry-run
python auditor.py --server main --compare backup --transfer-images
```

A pair lands in "Artwork Differences" when Primary differs between the two servers. The bulk run only attempts `Primary` (not `Backdrop`/`Thumb`, which in practice are essentially never populated on the source server for these libraries and would just be wasted API calls); `transfer_images.py`'s standalone CLI still supports all three via `--image-type` for one-off testing. It only fills in what the destination is actually missing: an item that already has a Primary image is left alone ("already present"), one the base server has no cached image for is recorded as "no source image" rather than a failure, and one item failing doesn't stop the rest of the batch. As with `--transfer-metadata`, it asks for one confirmation covering the whole batch (skip it with `--yes`), and `--dry-run` previews without writing anything.

The comparison report gains an "Image Transfer Results" table (Artwork page) whenever `--transfer-images` was used, showing each item/image-type outcome - transferred, would transfer (`--dry-run`), already present, no source image, or failed.

`--transfer-metadata` and `--transfer-images` are independent and can be combined in the same run; both share `--dry-run`/`--yes`/`--limit`/`--verify`.

**Given this writes to a live Jellyfin server, run `--dry-run` first before trusting it on a real library, especially unattended.**

### One item at a time

`transfer_images.py` transfers a single item's image directly, without going through a comparison run - useful to isolate an item-identity or upload problem from bulk target matching:

```powershell
python transfer_images.py --from-server main --from-item <id> --to-server backup --to-item <id> --image-type Primary
```

It prints both items' names before writing (so you can confirm you're pointed at the item you think you are), and re-reads the destination item's `ImageTags` immediately after the upload to show whether Jellyfin actually recorded the new image, not just whether the HTTP request succeeded. Skip the confirmation prompt with `--yes`; `--image-type` defaults to `Primary` and also accepts `Backdrop` or `Thumb`. Transfers append to `image_transfer.log`, mirroring `metadata_transfer.log`.

## Synchronizing subtitles between servers

When `--compare` finds items whose English subtitle availability differs between the two servers (the "Subtitle Differences" table), the subtitle track can be copied from the server that has it to the one that doesn't - entirely through the Jellyfin API, not the filesystem:

```powershell
python auditor.py --server main --compare backup --transfer-subtitles --dry-run
python auditor.py --server main --compare backup --transfer-subtitles
```

This is what makes it possible to sync subtitles a plain rsync of the media directories misses: Jellyfin sometimes stores an external subtitle file inside its own internal metadata cache (e.g. `/var/lib/jellyfin/metadata/library/...`) rather than next to the video file, so a scheduled rsync of the media library never touches it even though the source server plays it fine. `--transfer-subtitles` downloads the subtitle through Jellyfin's video-streaming endpoint (which serves it regardless of where the file actually lives) and uploads it to the destination through Jellyfin's subtitle-upload endpoint, so it works the same way whether the source file sits next to the media or only in the metadata cache.

Only text-based subtitle tracks (SRT, ASS/SSA, VTT, etc.) can be transferred this way - Jellyfin transcodes any of those to SRT on the fly when streaming. Bitmap-based tracks (PGS, VobSub) can't be converted and are reported as "no source subtitle" rather than attempted. It only fills in what the destination is actually missing: an item that already has an English subtitle track is left alone ("already present"), and one item failing doesn't stop the rest of the batch. As with `--transfer-metadata`/`--transfer-images`, it asks for one confirmation covering the whole batch (skip it with `--yes`), and `--dry-run` previews without writing anything.

The comparison report gains a "Subtitle Transfer Results" table (Subtitles page) whenever `--transfer-subtitles` was used, showing each item's outcome - transferred, would transfer (`--dry-run`), already present, no source subtitle, or failed.

`--transfer-metadata`, `--transfer-images`, and `--transfer-subtitles` are independent and can be combined in the same run; all three share `--dry-run`/`--yes`/`--limit`/`--verify`.

**Given this writes to a live Jellyfin server, run `--dry-run` first before trusting it on a real library, especially unattended.**

### Verifying a transfer

By default, the comparison report written at the end of a `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles` run still reflects the `--compare` server's state from *before* those writes happened - the "Mismatched Metadata"/"Artwork Differences"/"Subtitle Differences" tables show what was different going in, with a "Transfer Results" table appended showing what was attempted. Add `--verify` to close the loop instead: once every requested transfer finishes, it re-audits the `--compare` server and rebuilds the comparison report from that fresh state, so the diff tables show what's *actually still different* after the transfer, not what was different before it ran.

```powershell
python auditor.py --server main --compare backup --transfer-metadata --transfer-images --transfer-subtitles --yes --verify
```

It also logs a one-line summary of what remains (missing media, mismatched metadata, artwork differences, subtitle differences, etc.) right after the re-audit finishes. `--verify` is ignored with `--dry-run`, since a dry run doesn't write anything for a re-audit to pick up, and it's also skipped whenever no item in the batch actually reached `transferred` status - e.g. every flagged item turned out to have no transferable field differences once fully read, or was rejected/failed - since there's nothing a re-audit would show as changed.

### One item at a time

`transfer_subtitles.py` transfers a single item's English subtitle track directly, without going through a comparison run:

```powershell
python transfer_subtitles.py --from-server main --from-item <id> --to-server backup --to-item <id>
```

It prints both items' names before writing, then re-reads the destination item's `MediaStreams` after the upload to show how many subtitle tracks it has. Skip the confirmation prompt with `--yes`. Transfers append to `subtitle_transfer.log`, mirroring `metadata_transfer.log`/`image_transfer.log`.

## Picking the right TheTVDB series when a name matches more than one

`apply_dvd_metadata.py`, `apply_episode_titles.py`, and `apply_episode_numbers.py` all look up a series by name in Jellyfin, then use whatever TheTVDB id Jellyfin has assigned to it - but that assigned id can itself be wrong, since TheTVDB sometimes has more than one series entry sharing the exact same name (e.g. a decades-old show and a from-scratch modern revival, each independently numbering their own "Season 1"). Jellyfin's automatic matching has no way to know which one actually explains a given local library, and a wrong match here wouldn't just go uncorrected - it would actively overwrite episode metadata, rename titles, or assign episode numbers using some *other* show's episode list.

All three tools guard against this the same way: they search TheTVDB by name for up to 5 same-named candidates, add the Jellyfin-assigned id itself as a candidate if it isn't already among them, and pick whichever one's aired-order (season, episode) positions best overlap the series' full local episode set - across every season, not just the one being acted on, since a wrong id can still coincidentally explain a single season while failing everywhere else. When the winner isn't the id Jellyfin had assigned, that's logged before continuing, e.g.:

```
TheTVDB id '78804' assigned in Jellyfin for 'Doctor Who' doesn't best explain its local episodes across every season - using TheTVDB id '449991' instead.
```

When Jellyfin has no id assigned at all, this can still find and use a matching TheTVDB series purely by name and local episode overlap, instead of failing outright.

## Applying TheTVDB DVD-order metadata

`--check-episode-order` only flags a season whose title disagrees between TheTVDB's aired order and DVD order at the same position - it doesn't fix it. `apply_dvd_metadata.py` fixes one series/season in place: for every episode currently in that season, it looks up TheTVDB's DVD-order episode at that same season/episode position and overwrites the episode's `Name` and `Overview` with the DVD-order values. Episode and season numbers are never touched, so this only corrects what an episode is called, not where it lives.

```powershell
python apply_dvd_metadata.py --series-name "Show Name" --season-number 2
```

Uses the same `servers.toml` (defaulting to `default_server`; override with `--server`) and `tvdb_cache.json` as the rest of the project. If the series name matches shows in more than one library, add `--library NAME` to disambiguate. It prints every episode's planned outcome - the old and new title/overview, "no DVD-order match at this position" when TheTVDB has nothing at that position, or "already matches DVD order" when there's nothing to change - then asks for one confirmation covering the whole season (skip it with `--yes`). Attempts append to `dvd_metadata_apply.log`, mirroring `metadata_transfer.log`.

Any of `Name`/`Overview` it actually changes is added to the episode's `LockedFields`, the same thing Jellyfin's own "Edit Metadata" dialog does when you change a field by hand - without this, a library with TheTVDB's internet metadata provider enabled treats those fields as provider-owned and its next scheduled/on-demand refresh silently reverts the edit back to aired-order data, even though the write itself succeeded. (`OriginalTitle` is written but never locked - Jellyfin's `LockedFields` deserializes into a fixed server-side enum that has no `OriginalTitle` member, and any unrecognized entry fails the whole update with a 400.) It also re-reads each episode immediately after writing it and reports "update did not take effect" instead of "updated" if Jellyfin still shows the old value - a successful HTTP response only means the write was accepted, not that it stuck.

### Undoing an inadvertent reordering

Before changing an episode's `Name`, the tool copies whatever `Name` currently holds into `OriginalTitle` first - this repurposes `OriginalTitle` as this tool's own undo backup, so it will overwrite any genuine original-language title already stored there. Add `--aired` to reverse the process and restore toward aired order:

```powershell
python apply_dvd_metadata.py --series-name "Show Name" --season-number 2 --aired
```

For `Name`, `--aired` prefers each episode's `OriginalTitle` backup over a fresh TheTVDB aired-order lookup, since the backup is exactly what the episode had before this tool last changed it, even if TheTVDB's own aired-order data has changed since. If an episode has no `OriginalTitle` backup (it was never touched by a DVD-order apply), `--aired` falls back to TheTVDB's aired-order title for that position instead. `Overview` has no equivalent backup field, so it's always restored from TheTVDB's aired-order data when available. An episode with neither a backup nor any TheTVDB aired-order data at that position is skipped, same as a DVD-order apply skips a position TheTVDB's DVD order doesn't cover.

## Renaming episode titles to match TheTVDB

`--check-episode-order` flags a local episode whose title matches neither TheTVDB ordering at its position - `apply_episode_titles.py` fixes that by renaming it. For every episode currently in one series/season, it looks up TheTVDB's aired-order episode at that same season/episode position (`--dvd-order` for DVD order instead) and overwrites the episode's `Name` with that title. Unlike `apply_dvd_metadata.py`, it never touches `Overview`, episode/season numbers, or images - it's scoped to titles alone - and unlike a blind overwrite, it first checks whether the episode already reads the same as TheTVDB's title using the same lenient comparison `--check-episode-order` itself uses (punctuation, articles, accents, US/UK spelling, roman-numeral part suffixes, hyphenated/compound words, and more are all treated as equivalent, not just an exact string match), so a title that already wouldn't be flagged as a mismatch is left alone rather than being rewritten to TheTVDB's exact spelling for no reason.

```powershell
python apply_episode_titles.py --series-name "Show Name" --season-number 2
python apply_episode_titles.py --series-name "Show Name" --season-number 2 --dvd-order
```

It takes the same `--server`/`--library`/`--yes`/`--debug` options as `apply_dvd_metadata.py`, uses the same `servers.toml` and `tvdb_cache.json`, and follows the same plan-then-confirm-then-apply flow: it prints every episode's planned outcome - the old and new title, "already matches aired-order title" (or DVD-order) when there's nothing to change, or "no TheTVDB aired-order match at this position" when TheTVDB has nothing there - then asks for one confirmation covering the whole season (skip it with `--yes`). Before an actual rename, the current `Name` is backed up into `OriginalTitle`, the same convention `apply_dvd_metadata.py` uses (and `apply_dvd_metadata.py --aired`'s `OriginalTitle`-preferring restore can still recover it later, the same as after a DVD-order apply). The renamed `Name` field is locked the same way the other apply tools lock what they change, and each write is re-read and reported as "update did not take effect" instead of "renamed" if Jellyfin still shows the old value - the same safeguards `apply_dvd_metadata.py` and `apply_episode_numbers.py` use. Attempts append to `episode_titles_apply.log`, mirroring `dvd_metadata_apply.log`.

## Filling in missing episode numbers

Jellyfin can't always parse an episode number out of a filename - a file with no recognizable `SxxExx` marker gets no `IndexNumber` at all, which the `missing_episode_number` audit check flags. When a series is organized as one descriptively-titled file per episode instead of `SxxExx` naming, Jellyfin falls back to using the filename itself as the episode's `Name`, so that fallback title is usually the episode's real title, just not yet tied to a number. `apply_episode_numbers.py` fixes one series/season in place: it fetches TheTVDB's aired-order episode list for that season, works out which aired-order numbers aren't already used by a numbered episode in the season, and matches each unnumbered episode to one of those by comparing its `Name` against each candidate's title (case/punctuation-insensitively, and ignoring a leading "The"/"A"/"An" if that's the only difference) - deliberately **not** by file order, since on-disk file order (e.g. alphabetical) doesn't necessarily follow aired order.

When an episode's title matches no remaining candidate exactly (or article-insensitively), the closest-scoring candidate by title similarity is offered as a fuzzy match to confirm interactively, one at a time, e.g.:

```
  Episode Title.mkv: no exact title match. Closest TheTVDB title is "Episode Title." (E07, 92% similar) - use it? [y/N]
```

Nothing below a minimum similarity threshold is offered at all. Confirming a fuzzy match also overwrites the episode's `Name` with TheTVDB's title (since a fuzzy match by definition means the two titles weren't already equivalent) and locks `Name` the same way `apply_dvd_metadata.py` locks `Name`/`Overview`, so a library with an internet metadata provider enabled doesn't silently revert it on its next refresh. An exact or article-insensitive match never touches `Name`. Declining a fuzzy match, or running with `--yes` (which skips fuzzy-match prompts entirely, not just the final batch confirmation), leaves that episode unmatched instead of guessed at.

```powershell
python apply_episode_numbers.py --series-name "Show Name" --season-number 2
```

It takes the same `--server`/`--library`/`--yes`/`--debug` options as `apply_dvd_metadata.py`, uses the same `servers.toml` and `tvdb_cache.json`, and follows the same plan-then-confirm-then-apply flow: it prints every unnumbered episode's planned outcome - the filename and the number it will be assigned, "no unused TheTVDB aired-order episode title matches this episode's name" when nothing matches, or "already numbered" when there's nothing to change - then asks for one confirmation covering the whole batch (skip it with `--yes`). `IndexNumber` is always written, and `Name` only for a confirmed fuzzy match; `Overview` and every other field are left untouched. Attempts append to `episode_numbers_apply.log`, mirroring `dvd_metadata_apply.log`. It also re-reads each episode immediately after writing it and reports "update did not take effect" instead of "numbered" if Jellyfin still shows the old value, the same safeguard `apply_dvd_metadata.py` uses.

## Output

By default, reports are written under `audit_results\`.

- `audit_results\index.html` - top-level report index
- `audit_results\<server>\index.html` - per-server HTML dashboard
- `audit_results\<server>\<ServerName>_audit.csv` - per-server CSV findings, one row per audited media item (columns: Library, Path, Series, Title, Season, Episode, Missing Subtitles, Missing Primary, Mismatched Filename Title, Mismatched Stream Title, Unknown Audio Codec, Unknown Video Codec, Mismatched TheTVDB Series, Aired/DVD Order Mismatch, Missing Episode Number, Missing Seasons, Missing Episodes), named with the server so CSVs from different servers don't collide when downloaded to the same folder. Every column is a per-item Yes/No fact except Missing Seasons/Missing Episodes, which mark a single representative episode row for the whole series/season gap they describe rather than every episode in the affected season, matching how the audit check itself is attached. The Episode column (here and in every HTML report table with one) shows a range like `5-7` instead of just `5` when the filename's own `SxxEyy-Ezz` marker says the file covers more than one episode. In the CSV specifically, a range value is written with a leading apostrophe (e.g. `'5-7`) so Excel's automatic type detection doesn't misread it as a date when the file is opened by double-clicking it - Excel's plain-text CSV import treats a leading apostrophe as "force this cell to text" and doesn't display it, while `compare_csv_files.py` strips it back off (and re-adds it to its own diff output) when reading these files
- `audit_results\comparison_results\index.html` - comparison dashboard when `--compare` is used
- `audit_results\audit_results.xlsx` - combined Excel workbook, written whenever CSV or HTML output is generated, and linked from the top-level `index.html` once it exists. It has one worksheet per audited server, named after the server, each holding a named Excel Table (same name as the server) with the exact same rows and columns as that server's own audit CSV; every non-identity column (`Missing Subtitles` through `Missing Episodes`) has conditional formatting that gives a `Yes` cell a yellow background, plus a `Problems` column with a per-row `COUNTIF` formula counting that row's `Yes` cells, and a `Totals` row directly below the table with a per-column `COUNTIF` of `Yes` cells (and a `SUM` of `Problems`) - kept out of the table's own range so table sorting/filtering can't scatter it away from the bottom of the sheet. Unlike the CSV, the Episode column is given a real text number format instead of a leading apostrophe, so a merged range like `19-20` still can't be misread as a date without the visible guard character; every column from Season onward is also wrapped and center-aligned. When `--compare` is used, a `diffs` worksheet is added with a `diffs` table built the same way `compare_csv_files.py` builds its diff CSV (identity columns show a single value unless the two servers disagree, in which case both show as `left|right`; every other column always shows `left|right`, using `-` for a side missing the row entirely) - any cell containing `|` whose left and right side actually differ gets the same yellow background
- `metadata_transfer.log` - append-only record of every metadata transfer, written next to wherever `auditor.py --transfer-metadata` or `transfer_metadata.py` was run
- `image_transfer.log` - append-only record of every image transfer, written next to wherever `auditor.py --transfer-images` or `transfer_images.py` was run
- `subtitle_transfer.log` - append-only record of every subtitle transfer, written next to wherever `auditor.py --transfer-subtitles` or `transfer_subtitles.py` was run
- `dvd_metadata_apply.log` - append-only record of every DVD-order metadata apply attempt, written next to wherever `apply_dvd_metadata.py` was run
- `episode_titles_apply.log` - append-only record of every episode-title rename attempt, written next to wherever `apply_episode_titles.py` was run
- `episode_numbers_apply.log` - append-only record of every episode-number apply attempt, written next to wherever `apply_episode_numbers.py` was run
- `audit.log` - append-only record of everything else logged during an `auditor.py` run that used at least one `--transfer-*` flag (audit progress, comparison writing, `--verify` output, errors) - the transfer-type log files above only ever contain their own transfer's history, never this. Combine multiple `--transfer-*` flags in one run and each still only writes to its own log file; nothing gets duplicated across them.
- `mismatched_tvdb_series.log` - append-only, written whenever a TheTVDB `api_key` is configured (no `--transfer-*` flag needed): for every series that actually trips the `mismatched_tvdb_series` finding, lists each local episode's (season, episode) position against TheTVDB's aired and DVD orderings and the resulting unmatched/total score - useful for checking why a series was flagged. Series that pass the check write nothing. Not shown on the console.

## Project layout

- `auditor.py` - CLI entry point and orchestration, including the bulk `--transfer-metadata`, `--transfer-images`, and `--transfer-subtitles` runs
- `transfer_metadata.py` - standalone CLI to transfer one item's metadata between two servers; also provides the plan/apply functions the bulk run uses
- `transfer_images.py` - standalone CLI to transfer one item's cached image between two servers; also provides the plan/apply functions the bulk `--transfer-images` run uses
- `transfer_subtitles.py` - standalone CLI to transfer one item's English subtitle track between two servers, entirely through the Jellyfin API; also provides the plan/apply functions the bulk `--transfer-subtitles` run uses
- `apply_dvd_metadata.py` - standalone CLI to overwrite one series/season's episode Name/Overview with TheTVDB's DVD-order values
- `apply_episode_titles.py` - standalone CLI to rename one series/season's episode titles to TheTVDB's aired- (or DVD-, with `--dvd-order`) order titles, skipping episodes that already match under `audit.titles_match()`'s lenient comparison
- `apply_episode_numbers.py` - standalone CLI to fill in missing episode numbers for one series/season from TheTVDB's aired order, with interactive fuzzy title matching for episodes with no exact title match
- `config.py` - application and server configuration
- `jellyfin.py` - Jellyfin API client (reads library/item data; also supports the item metadata, image, and subtitle read/upload calls transfers use, plus series/episode lookups by name for `apply_dvd_metadata.py`/`apply_episode_titles.py`/`apply_episode_numbers.py`)
- `models.py` - normalized data models
- `media.py` - media and filesystem helpers, including filename-based episode title parsing
- `audit.py` / `audit_types.py` - audit rules and finding types
- `reports\` - CSV and static HTML report generation
- `comparison\` - cross-server comparison report generation, including the transfer button and Transfer Results table
- `xlsx_report.py` - combined Excel workbook generation (`audit_results.xlsx`): one named-table sheet per server (with a `Problems` count column and a `Totals` row) plus an optional `diffs` sheet, with conditional formatting
- `compare_csv_files.py` - standalone CLI to diff two audit CSVs into `diffs.csv`; also provides the header/row-diffing functions `xlsx_report.py` reuses for the `diffs` sheet
- `output_layout.py` - shared output directory and site-index layout helpers
- `report_filters.py` - shared category/severity filtering for report output
- `report_theme.py` - shared dark/light theme toggle markup and script
- `tests\` - unit tests

## Development

Run the test suite:

```powershell
python -m unittest
```

## Notes

The project structure is intentionally modular so additional audits can be added without mixing Jellyfin API code, filesystem checks, and report generation logic.