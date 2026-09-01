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
- Movie metadata titles that don't match the title implied by the filename's `(Year)` naming
- A series matched to the wrong TheTVDB entry (when a TheTVDB `api_key` is configured): if most of a series' local (season, episode) numbers don't correspond to any TheTVDB episode at that position in either aired order or DVD order, the match itself - not any one episode - is probably wrong (e.g. a series with the same title as a different show, matched to that other show's TheTVDB id). Checking both orderings avoids flagging a series that's simply numbered on disk in DVD order. Flagged only once a series has at least 5 local episodes and at least half of them are unmatched, so a thin or partially-numbered library doesn't trigger it on noise. When this fires for a series, that series' missing-season/-episode checks (and the TheTVDB title check below) fall back to internal-gap detection/are skipped instead of piling on nonsense findings sourced from the wrong show's episode list. The finding then searches TheTVDB by series name for a same-named alternative (e.g. a same-titled show from a different country) and, when one explains at least 90% of the local episodes, names it directly in the finding's message as the entry to re-identify the series to in Jellyfin - checked against up to 5 search candidates per series
- TV episode metadata titles that don't match TheTVDB's aired-order title at that season/episode position (when a TheTVDB `api_key` is configured; sourced from `tvdb_cache.json`, the same aired-order data the rest of the app already fetches/caches - see `TVDB_CACHE_TTL_DAYS` below). Unlike `--check-episode-order` below, a mismatch against aired order alone is enough to flag - DVD-order data isn't required to be available too - and it runs on every audit, not just with that flag. A local title matching DVD order instead of aired order at that position is still excused, though, same as `--check-episode-order` does, so a series correctly organized end-to-end in DVD order isn't flagged as "mismatched" at every single episode. A candidate title still in its original, untranslated language (no English translation on file with TheTVDB) is ignored, since there's no way to tell whether it matches or not - reported instead as its own separate, informational `tvdb_title_not_english` finding (`NoE` in the CSV/XLSX Mismatched TheTVDB Title column - see the CSV column list above), so that's visibly distinct from a title that was actually compared. A filename naming a multi-episode range (e.g. `S01E05-E07`) is compared against all of those episodes' TheTVDB titles joined together rather than just the first one's - if even one position in the range is untranslated, the whole range reads `NoE`
- Optionally (`--check-episode-order`, requires a TheTVDB `api_key` in `servers.toml`): a local TV episode's metadata title doesn't match TheTVDB's aired-order title at its season/episode position, and TheTVDB's DVD order is also available to compare against - catches series stored on disk in one ordering but labeled with the other, where the filename, season number, and episode number all still look correct. Only a title matching neither ordering is flagged as a genuine discrepancy to check by eye; a title matching DVD order instead of aired order isn't reported at all, since a series correctly organized end-to-end in DVD order is expected to disagree with aired order at every single episode. Without DVD-order data for that position, a local title that merely disagrees with aired order isn't reported on its own (the TheTVDB title check above still applies in that case). A candidate title still in its original, untranslated language is ignored, same as above. A filename naming a multi-episode range is compared the same way

When a Jellyfin library has more than one same-named TheTVDB id for one series (e.g. TheTVDB splitting a long-running show into a separate entry for a newer era while the old entry keeps the earlier episodes, both still titled the same in Jellyfin, or an unrelated same-named show TheTVDB's search once surfaced as a rejected candidate while looking for a better match elsewhere), local episodes are checked against every id that actually explains at least one local episode *title* - its own episode title at a local item's position, in either ordering, agreeing with that item's title - not merely every id that has *some* data at a shared (season, episode) position. Position overlap alone was tried and reverted: an unrelated same-named show using an ordinary, similarly-sized season/episode grid can cover much of the real series' position space by pure numeric coincidence without its content having anything to do with it, which let position overlap alone pick, or merge in, the wrong id far too easily. Title-matching doesn't have that problem, so every id that clears it stays in the merge - including a newer era whose local share happens to reuse (season, episode) numbers a dominant era's id also has real data at (e.g. a relaunch locally renumbered back to "Season 1" instead of continuing the original's own numbering) - and each local episode is checked against whichever qualifying id's title actually agrees with it, rather than only whichever id happened to be looked up first. Only when *no* id explains even a single local title (most commonly because every local title here is a placeholder Jellyfin never enriched with real episode metadata) does this fall back to position overlap instead, picking just the one id with the fewest local (season, episode) positions left unexplained, tied toward an id Jellyfin has actually assigned to a Series item - with no title evidence available to resolve a collision correctly, using only one id there is safer than guessing which of several to merge. A local episode with no data at all - in either ordering - under any of the checked ids is still reported in the Aired/DVD Order Mismatch results, with a distinct message saying that season/episode wasn't found in TheTVDB at all for the identified series (e.g. a season TheTVDB doesn't have for that particular entry, or one that belongs under a different same-named id entirely that didn't clear the title bar), rather than staying silent or being compared against a different id's unrelated episode.

The filename-title checks tolerate cosmetic differences that don't represent a real mismatch: dot-delimited release names (`Show.S01E02.Episode.Title.mkv`), straight vs. curly quotes and dashes, roman vs. arabic numerals in a parenthetical suffix (`(I)` vs. `(1)`) or a bare trailing multi-part disambiguator with no parentheses at all (metadata's `Those Who Rend Asunder I` vs. a filename's `Those Who Rend Asunder (1)` - tried both as the numeral being significant title text and as a disambiguator to drop entirely, since either reading matching is enough), `&` vs. `and`, `+` vs. `/`, a Unicode ellipsis (`…`) vs. three literal periods (`...`), and leading parenthetical text that's actually part of the title (e.g. `(Dis)Members Only`). Release-quality tags in a filename (`1080p`, `WEB-DL`, `x264`, etc.) are stripped only when they form a genuine trailing run of tags, so an ordinary title word that happens to look like one - an episode called "Spider in the Web," for example - isn't mistaken for one.

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
| `REPORT_MEDIA_PATH_PREFIX` | Removes a path prefix from report display paths; only consulted when a path has no `media` folder segment to trim to automatically (see Output below) | empty |
| `MOVIES_CSV_FILENAME` | Movie CSV filename | `movies_report.csv` |
| `TV_CSV_FILENAME` | TV CSV filename | `tv_report.csv` |
| `AUDIT_CSV_FILENAME` | Per-server audit CSV filename suffix (prefixed with the server name, e.g. `MyServer_audit.csv`) | `audit.csv` |
| `AUDIT_HTML_FILENAME` | Root HTML output directory name | `audit_results` |
| `ENABLE_MOVIES` | Enable movie library auditing | `true` |
| `ENABLE_TV` | Enable TV library auditing | `true` |
| `ENGLISH_LANGUAGE_CODES` | Comma-separated language codes treated as English subtitles | `en,eng,` |
| `TVDB_CACHE_TTL_DAYS` | How many days a cached TheTVDB episode-ordering lookup stays valid before a re-fetch - applies to every TheTVDB-backed check (missing seasons/episodes, mismatched TheTVDB series/title, and `--check-episode-order`), not just the last one | `7` |

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

Bare `--compare` resolves independently every run - it always means "the
first two configured servers," never "whichever server a previous run's
explicit `--compare SERVER` used." Mixing a `--server main --compare backup`
run with a later bare `--compare` run can silently pair different servers
than intended if `main`/`backup` aren't actually the first two entries in
`servers.toml`. Every run logs which two servers it resolved to (`Comparing
<base> (base) against <compare> (compare)...`) specifically so this is easy
to catch instead of looking like a report that failed to pick up a fix.

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
| `--series-name NAME` | With `--transfer-metadata`, limit the transfer to episodes of this TV series (case-insensitive match against the base server's series name; movies never match). Without it, every mismatched item - TV or movie - is transferred, as before |
| `--season-number N` | With `--series-name`, further limit the transfer to this one season number |
| `--transfer-images` | Transfer cached images (Primary, Backdrop, Thumb) for every item with an artwork difference from the base `--server` to the `--compare` server; requires `--compare` (see [Synchronizing images between servers](#synchronizing-images-between-servers)) |
| `--transfer-subtitles` | Transfer the English subtitle track for every item with a subtitle difference from the base `--server` to the `--compare` server; requires `--compare` (see [Synchronizing subtitles between servers](#synchronizing-subtitles-between-servers)) |
| `--dry-run` | With `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles`, preview planned transfers without writing anything |
| `--yes` | With `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles`, skip the batch confirmation prompt |
| `--limit N` | With `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles`, only attempt the first N items found, regardless of outcome - useful for quickly testing bulk-mode changes without waiting for a full run |
| `--verify` | With `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles`, re-audit the `--compare` server once transfers finish and write the comparison report from that post-transfer state instead of the pre-transfer snapshot; ignored with `--dry-run`, or if no item was actually transferred |

## How `--compare` matches items across two servers

Every comparison feature - the report tables, and everything `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles` act on - starts by pairing up each library's items between the two servers. Since the two servers mount the same (or mirrored) files at different absolute paths, matching can't just compare full paths; it works in two passes, each including enough of the item's own folder structure to avoid conflating two different items that happen to share a name:

1. **By filename**: items whose base filename matches, scoped to their immediate containing folder (e.g. a season folder, or a movie's own folder) - so two different episodes both named `01.mkv` in `Season 01/` and `Season 02/`, or two different movies that happen to share a filename in different movie folders, are never treated as the same file just because the folder itself differs only by mount point.
2. **By metadata** (for items whose filenames were renamed independently on one server): series name, season, episode, and title for an episode, or title for a movie - plus, in both cases, the item's own series/movie folder name, skipping past a season subfolder to the series folder itself. This is what keeps two different series (or two different movies) that happen to share a display name, but live in different folders, from being paired with each other.

The tradeoff is intentional: if the *same* item's own folder happens to be named differently between the two servers (e.g. renamed independently, not just remounted), it's reported as missing on both sides rather than matched - a false "missing" is something you can go check by eye, while a false match could mean `--transfer-metadata` overwrites one item's metadata with a completely different item's data.

## Synchronizing metadata between servers

When `--compare` finds items whose metadata differs between the two servers (the "Mismatched Metadata" table), that metadata can be copied from the base server to the compared server - one item at a time from the report, or in bulk for the whole comparison.

**What transfers:** title, original title, overview, genres, tags, studios, cast/crew, provider IDs (IMDb/TVDB/etc.), community rating, official rating, release date, year, and episode/season number. **What never transfers:** artwork/images, file path, and any other server-managed or read-only data.

Every transfer - one-off or bulk - reads the full item from both servers, computes what would change, and refuses to write anything if the destination item's identity fields (`Id`, `Path`) would come back empty, since Jellyfin's update endpoint replaces an item's metadata wholesale rather than merging it.

When a transfer actually changes `Name`, it's added to the destination item's `LockedFields`, the same convention `apply_tvdb_metadata.py`/`apply_titles_from_filename.py` use - so a library with an internet metadata provider enabled doesn't silently revert the title on its next refresh, and so a later `auditor.py` run's whole-library listing (which can otherwise keep showing an item's pre-write title for a while after a direct API write like this one - see "Applying TheTVDB metadata to episodes" above) knows to double-check it with a fresh per-item lookup instead of trusting the listing as-is.

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

Add `--series-name NAME` to limit the batch to one TV series (matched case-insensitively against the base server's series name; movies are never included when this is used), and optionally `--season-number N` (requires `--series-name`) to further limit it to one season:

```powershell
python auditor.py --server main --compare backup --transfer-metadata --series-name "Show Name" --season-number 2
```

Without `--series-name`, every mismatched item - TV or movie - is transferred, exactly as before.

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

Jellyfin's own `/Items` listing can take a while - up to roughly an hour, observed in practice - to reflect a field written moments earlier through a direct API call (as every `--transfer-*`/`apply_*.py` tool here does), even though a single-item lookup (Jellyfin's own item detail page, or `jellyfin.get_item()`) already shows the new value right away. Triggering a Jellyfin metadata refresh doesn't shorten this - it's not a metadata-provider staleness issue, and every locked field (e.g. `Name`, right after a transfer locks it) is explicitly exempt from being changed by a refresh anyway. `jellyfin.py`'s whole-library listing already re-checks a locked `Name` with a fresh per-item lookup to work around exactly this for that one field, but `--verify`'s re-audit runs immediately after the transfer, squarely inside this window, so a field the per-item recheck doesn't cover can still show its pre-transfer value in the "actually still different" table it writes. If `--verify`'s report or a follow-up `--compare` still shows an item as different right after a transfer, re-run `--compare` again later rather than assuming the transfer failed or was mismatched.

### One item at a time

`transfer_subtitles.py` transfers a single item's English subtitle track directly, without going through a comparison run:

```powershell
python transfer_subtitles.py --from-server main --from-item <id> --to-server backup --to-item <id>
```

It prints both items' names before writing, then re-reads the destination item's `MediaStreams` after the upload to show how many subtitle tracks it has. Skip the confirmation prompt with `--yes`. Transfers append to `subtitle_transfer.log`, mirroring `metadata_transfer.log`/`image_transfer.log`.

## Picking the right TheTVDB series when a name matches more than one

`apply_tvdb_metadata.py` and `apply_episode_numbers.py` both look up a series by name in Jellyfin, then use whatever TheTVDB id Jellyfin has assigned to it - but that assigned id can itself be wrong, since TheTVDB sometimes has more than one series entry sharing the exact same name (e.g. a decades-old show and a from-scratch modern revival, each independently numbering their own "Season 1"). Jellyfin's automatic matching has no way to know which one actually explains a given local library, and a wrong match here wouldn't just go uncorrected - it would actively overwrite episode metadata, rename titles, or assign episode numbers using some *other* show's episode list.

All three tools guard against this the same way: they search TheTVDB by name for up to 5 same-named candidates, add the Jellyfin-assigned id itself as a candidate if it isn't already among them, and pick whichever one's aired-order (season, episode) positions best overlap the series' full local episode set - across every season, not just the one being acted on, since a wrong id can still coincidentally explain a single season while failing everywhere else. When the winner isn't the id Jellyfin had assigned, that's logged before continuing, e.g.:

```
TheTVDB id '78804' assigned in Jellyfin for 'Doctor Who' doesn't best explain its local episodes across every season - using TheTVDB id '449991' instead.
```

When Jellyfin has no id assigned at all, this can still find and use a matching TheTVDB series purely by name and local episode overlap, instead of failing outright.

## Applying TheTVDB metadata to episodes

`--check-episode-order` only flags a season whose title disagrees between TheTVDB's aired order and DVD order at the same position - it doesn't fix it. `apply_tvdb_metadata.py` fixes one series in place, in one of three mutually exclusive modes:

- `--aired` / `--dvd`: for every episode currently in the given season, look up TheTVDB's aired-order or DVD-order episode (respectively) at that same season/episode position, via a fresh lookup each time, and overwrite the episode's `Name` and `Overview` with those values.
- `--restore`: undo a previous `--dvd` (or `--aired`) apply, preferring each episode's own `OriginalTitle` backup for `Name` - the title this tool backed up there the last time it changed `Name` - over a fresh TheTVDB lookup, since the backup is exactly what the episode had before being changed. If an episode has no backup at all, `--restore` falls back to a fresh TheTVDB aired-order lookup, the same as `--aired` would use. `Overview` has no equivalent backup field, so it's always restored from TheTVDB's aired-order data when available, regardless of where `Name` came from - because of this, `--restore` also needs a TheTVDB `api_key` and does contact TheTVDB, whenever there's actually something to restore or sync.

Exactly one of `--aired`/`--dvd`/`--restore` is required. `--season-number` is optional - every season the series has is updated in one batch if it's omitted. Episode and season numbers are never touched, so this only corrects what an episode is called and described as, not where it lives. A file spanning a multi-episode range (e.g. `Show S01E17-E18.mkv`) combines every one of those positions' `Name`s with " / " and `Overview`s with a blank line, rather than just the first position's - Jellyfin's own episode number for such a file is just the range's first episode - and is left alone entirely if TheTVDB is missing data for any position in the range; `--images` (usable with any of the three modes) still only ever replaces the Primary image with the range's first position's image, since there's no way to combine two images into one image slot. There is no local backup for the pre-change image, so `--restore --images` can only restore it when TheTVDB still reports an aired-order image at that position.

```powershell
python apply_tvdb_metadata.py --series-name "Show Name" --season-number 2 --dvd
python apply_tvdb_metadata.py --series-name "Show Name" --season-number 2 --aired
python apply_tvdb_metadata.py --series-name "Show Name" --season-number 2 --restore
```

Uses the same `servers.toml` (defaulting to `default_server`; override with `--server`) and `tvdb_cache.json` as the rest of the project. If the series name matches shows in more than one library, add `--library NAME` to disambiguate; if that still isn't specific enough (e.g. two same-named shows in the same library), add `--path PARTIAL_PATH` too, to only consider a series whose path contains that text (case-insensitive) - every `apply_*.py` tool below supports both options the same way. It prints every episode's planned outcome - the old and new title/overview, "no {aired|DVD}-order match at this position" (or, for `--restore`, "no backup title and no TheTVDB aired-order match") when TheTVDB has nothing at that position, or "already matches {aired|DVD} order" when there's nothing to change - then asks for one confirmation covering everything in scope (skip it with `--yes`). Attempts append to `tvdb_metadata_apply.log`, mirroring `metadata_transfer.log`.

Whether `Name` is even worth rewriting is decided with the same lenient `audit.titles_match()` comparison `--check-episode-order` itself uses (punctuation, articles, accents, US/UK spelling, roman-numeral part suffixes, hyphenated/compound words, and more are all treated as equivalent, not just an exact string match) in every mode, so a title that already wouldn't be flagged as a mismatch is left alone rather than being rewritten to TheTVDB's exact spelling for no reason. `Overview` has no such lenient comparison - it's prose, not a short label - so it's compared for exact equality.

`Name` is never rewritten to a title TheTVDB reports in its original, untranslated language (there's no separate flag in TheTVDB's response saying a translation is missing - see `--check-episode-order`'s "candidate title still in its original, untranslated language" note above) - writing foreign-script text into an otherwise-English library's metadata would be worse than leaving the title alone. This only affects `Name`: `Overview`/`--images` still update independently even when `Name` is left unchanged this way, since TheTVDB doesn't flag Overview's language the same way and there's no equivalent protection for it. In `--restore` mode, this only applies to the TheTVDB-fallback path (no backup present) - a backup restores `Name` without ever consulting TheTVDB, so its language is never in question there. The summary line reports how many episodes this affected ("N with no English title available (Name left unchanged)").

Any of `Name`/`Overview` it actually changes is added to the episode's `LockedFields`, the same thing Jellyfin's own "Edit Metadata" dialog does when you change a field by hand - without this, a library with TheTVDB's internet metadata provider enabled treats those fields as provider-owned and its next scheduled/on-demand refresh silently reverts the edit back to the previous data, even though the write itself succeeded. (`OriginalTitle` is written but never locked - Jellyfin's `LockedFields` deserializes into a fixed server-side enum that has no `OriginalTitle` member, and any unrecognized entry fails the whole update with a 400.) It also re-reads each episode immediately after writing it and reports "update did not take effect" instead of "updated"/"restored" if Jellyfin still shows the old value - a successful HTTP response only means the write was accepted, not that it stuck.

This same `Name` locking is why a later `auditor.py` run correctly picks up a rename made by this tool (or `apply_titles_from_filename.py`, below): Jellyfin's whole-library `/Items` listing can keep showing an item's pre-rename title for a while after a direct API write like these tools make, even though Jellyfin's own UI already shows the new one and a direct per-item lookup would too. Rather than re-checking every item's title with an extra per-item request - far too expensive across a whole library - the auditor treats a locked `Name` as the tell that an item's listed title might be stale, and only re-checks those with the same kind of direct per-item lookup, correcting it before anything is audited against it.

## Renaming titles to match the filename

`apply_titles_from_filename.py` is the filename-only sibling to `apply_tvdb_metadata.py`: instead of renaming toward TheTVDB's aired/DVD-order title, it renames a movie or a season's episodes toward the title implied by their own on-disk filename under Jellyfin's naming convention (`Show S01E02 Episode Title.mkv` for an episode, `Movie Name (Year).mkv` for a movie). It never contacts TheTVDB or any other internet metadata provider - the new title comes entirely from the filename already on disk, so it needs no TheTVDB `api_key` at all.

Exactly one target must be given: `--movie` for a single movie, or `--series-name` for a series - add `--season-number` to a series to scope it to one season instead of every season the series has.

```powershell
python apply_titles_from_filename.py --movie "Movie Name"
python apply_titles_from_filename.py --series-name "Show Name"
python apply_titles_from_filename.py --series-name "Show Name" --season-number 2
```

It takes the same `--server`/`--library`/`--path`/`--yes`/`--debug` options as the other `apply_*.py` tools, and follows the same plan-then-confirm-then-apply flow: it prints every item's planned outcome - the old and new title, "already matches its filename-implied title" when there's nothing to change, or "filename has no recognizable title marker" when the filename doesn't match Jellyfin's naming convention - then asks for one confirmation covering the whole batch (skip it with `--yes`). Whether a rename is even needed is decided with the same lenient `audit.titles_match()` comparison the sibling tools use. Before an actual rename, the current `Name` is backed up into `OriginalTitle`, the same convention (and shared implementation, in `title_backup.py`) `apply_tvdb_metadata.py` uses - including locking the renamed `Name` field so an internet metadata provider's next refresh doesn't silently revert it, and re-reading each write to report "update did not take effect" instead of "renamed"/"restored" if Jellyfin still shows the old value. Attempts append to `titles_from_filename_apply.log`, mirroring `tvdb_metadata_apply.log`.

Add `--restore` to reverse a previous rename: it sets each item's `Name` back to its own `OriginalTitle` backup, purely locally - it never inspects the filename and never contacts TheTVDB. An item with no `OriginalTitle` backup (never renamed by this tool) is left alone.

```powershell
python apply_titles_from_filename.py --movie "Movie Name" --restore
python apply_titles_from_filename.py --series-name "Show Name" --season-number 2 --restore
```

## Filling in missing episode numbers

Jellyfin can't always parse an episode number out of a filename - a file with no recognizable `SxxExx` marker gets no `IndexNumber` at all, which the `missing_episode_number` audit check flags. When a series is organized as one descriptively-titled file per episode instead of `SxxExx` naming, Jellyfin falls back to using the filename itself as the episode's `Name`, so that fallback title is usually the episode's real title, just not yet tied to a number. `apply_episode_numbers.py` fixes one series in place: for the given season (every season the series has, if `--season-number` is omitted), it fetches TheTVDB's aired-order episode list, works out which aired-order numbers aren't already used by a numbered episode in that season, and matches each unnumbered episode to one of those by comparing its `Name` against each candidate's title (case/punctuation-insensitively, and ignoring a leading "The"/"A"/"An" if that's the only difference) - deliberately **not** by file order, since on-disk file order (e.g. alphabetical) doesn't necessarily follow aired order. Matching never crosses season boundaries - each season's unnumbered episodes are only ever matched against that same season's own TheTVDB candidates.

When an episode's title matches no remaining candidate exactly (or article-insensitively), the closest-scoring candidate by title similarity is offered as a fuzzy match to confirm interactively, one at a time, e.g.:

```
  Episode Title.mkv: no exact title match. Closest TheTVDB title is "Episode Title." (E07, 92% similar) - use it? [y/N]
```

Nothing below a minimum similarity threshold is offered at all. Confirming a fuzzy match also overwrites the episode's `Name` with TheTVDB's title (since a fuzzy match by definition means the two titles weren't already equivalent) and locks `Name` the same way `apply_tvdb_metadata.py` locks `Name`/`Overview`, so a library with an internet metadata provider enabled doesn't silently revert it on its next refresh. An exact or article-insensitive match never touches `Name`. Declining a fuzzy match, or running with `--yes` (which skips fuzzy-match prompts entirely, not just the final batch confirmation), leaves that episode unmatched instead of guessed at.

```powershell
python apply_episode_numbers.py --series-name "Show Name" --season-number 2
```

It takes the same `--server`/`--library`/`--path`/`--yes`/`--debug` options as `apply_tvdb_metadata.py`, uses the same `servers.toml` and `tvdb_cache.json`, and follows the same plan-then-confirm-then-apply flow: it prints every unnumbered episode's planned outcome - the filename and the number it will be assigned, "no unused TheTVDB aired-order episode title matches this episode's name" when nothing matches, or "already numbered" when there's nothing to change - then asks for one confirmation covering everything in scope (skip it with `--yes`). `IndexNumber` is always written, and `Name` only for a confirmed fuzzy match; `Overview` and every other field are left untouched. Attempts append to `episode_numbers_apply.log`, mirroring `tvdb_metadata_apply.log`. It also re-reads each episode immediately after writing it and reports "update did not take effect" instead of "numbered" if Jellyfin still shows the old value, the same safeguard `apply_tvdb_metadata.py` uses.

## Output

By default, reports are written under `audit_results\`.

- `audit_results\index.html` - top-level report index
- `audit_results\<server>\index.html` - per-server HTML dashboard
- `audit_results\<server>\<ServerName>_audit.csv` - per-server CSV findings, one row per audited media item (columns: Library, Base Directory, Base Filename, Series, Title, Season, Episode, Missing Subtitles, Missing Primary, Mismatched Filename Title, Mismatched TheTVDB Title, Unknown Audio Codec, Unknown Video Codec, Mismatched TheTVDB Series, Aired/DVD Order Mismatch, Missing Episode Number, Missing Seasons, Missing Episodes), named with the server so CSVs from different servers don't collide when downloaded to the same folder. Every column is a per-item Yes/No fact except Missing Seasons/Missing Episodes, which mark a single representative episode row for the whole series/season gap they describe rather than every episode in the affected season, matching how the audit check itself is attached, and except Mismatched TheTVDB Title/Mismatched TheTVDB Series, which read `N/A` instead of `No` for any item (movies always; TV episodes whose series TheTVDB has no data for at all) where no TheTVDB comparison could be made in the first place - `No` is reserved for items TheTVDB was actually compared against and found to match. A series already flagged Mismatched TheTVDB Series is also `N/A` for Mismatched TheTVDB Title, since its TheTVDB data is untrustworthy and the title check never runs against it. Mismatched TheTVDB Title reads `NoE` instead, for an episode whose TheTVDB position has data but none of it in English (see `tvdb_title_not_english` below) - distinct from both `No` (a real comparison was made and passed) and `N/A` (there's no TheTVDB data at all to compare against). Aired/DVD Order Mismatch reads `NF` instead of `Yes`/`No` for an episode whose identified TheTVDB series has no data at all - in either ordering - at that season/episode position (see the "not found in TheTVDB at all" episode-order finding above) - distinct from both `No` (a real comparison was made and it matched) and `Yes` (a real comparison was made and it didn't); that finding is otherwise folded into the same Aired/DVD Order Mismatch results in the HTML report, distinguished there only by its own message text. The Episode column (here and in every HTML report table with one) shows a range like `5-7` instead of just `5` when the filename's own `SxxEyy-Ezz` marker says the file covers more than one episode. In the CSV specifically, a range value is written with a leading apostrophe (e.g. `'5-7`) so Excel's automatic type detection doesn't misread it as a date when the file is opened by double-clicking it - Excel's plain-text CSV import treats a leading apostrophe as "force this cell to text" and doesn't display it, while `compare_csv_files.py` strips it back off (and re-adds it to its own diff output) when reading these files. The item's path itself is trimmed for display, then split into two columns rather than shown whole: everything through the last folder literally named `media` is removed first, so it starts at the library folder (e.g. `TV Shows/Show/Season 01/Show.S01E09.mkv`) instead of showing each server's own mount point or drive letter (see `REPORT_MEDIA_PATH_PREFIX` below for a deployment that doesn't use a `media` folder) - Base Directory is then the one directory right below the library (e.g. `Show`, dropping the `Season 01` folder, since season is already its own column), empty when the file sits directly in the library folder with no such directory at all, and Base Filename is everything after the last `/` (e.g. `Show.S01E09.mkv`)
- `audit_results\comparison_results\index.html` - comparison dashboard when `--compare` is used
- `audit_results\audit_results.xlsx` - combined Excel workbook, written whenever CSV or HTML output is generated, and linked from the top-level `index.html` once it exists. It has one worksheet per audited server, named after the server, each holding a named Excel Table (same name as the server) with the exact same rows and columns as that server's own audit CSV; every non-identity column (`Missing Subtitles` through `Missing Episodes`) has conditional formatting that gives a `Yes` cell a yellow background, plus a `Problems` column with a per-row `COUNTIF` formula counting that row's `Yes` cells, and a `Totals` row directly below the table with a per-column `COUNTIF` of `Yes` cells (and a `SUM` of `Problems`) - kept out of the table's own range so table sorting/filtering can't scatter it away from the bottom of the sheet. Unlike the CSV, the Episode column is given a real text number format instead of a leading apostrophe, so a merged range like `19-20` still can't be misread as a date without the visible guard character. Every cell, header and data alike, is wrapped and center-aligned; every column after `Title` gets a fixed width of 11 instead of the content-fitted width the columns up through `Title` get. When `--compare` is used, a `diffs` worksheet is added with a `diffs` table built the same way `compare_csv_files.py` builds its diff CSV (identity columns show a single value unless the two servers disagree, in which case both show as `left|right`; every other column always shows `left|right`, using `-` for a side missing the row entirely) - any cell containing `|` whose left and right side actually differ gets the same yellow background
- `metadata_transfer.log` - append-only record of every metadata transfer, written next to wherever `auditor.py --transfer-metadata` or `transfer_metadata.py` was run
- `image_transfer.log` - append-only record of every image transfer, written next to wherever `auditor.py --transfer-images` or `transfer_images.py` was run
- `subtitle_transfer.log` - append-only record of every subtitle transfer, written next to wherever `auditor.py --transfer-subtitles` or `transfer_subtitles.py` was run
- `tvdb_metadata_apply.log` - append-only record of every TheTVDB metadata apply attempt, written next to wherever `apply_tvdb_metadata.py` was run
- `episode_numbers_apply.log` - append-only record of every episode-number apply attempt, written next to wherever `apply_episode_numbers.py` was run
- `titles_from_filename_apply.log` - append-only record of every filename-based title rename/restore attempt, written next to wherever `apply_titles_from_filename.py` was run
- `audit.log` - append-only record of everything else logged during an `auditor.py` run (audit progress, comparison writing, `--verify` output, errors) - the transfer-type log files above only ever contain their own transfer's history, never this. Written on every run, not just ones using a `--transfer-*` flag. Combine multiple `--transfer-*` flags in one run and each still only writes to its own log file; nothing gets duplicated across them.
- `mismatched_tvdb_series.log` - append-only, written whenever a TheTVDB `api_key` is configured (no `--transfer-*` flag needed): for every series that actually trips the `mismatched_tvdb_series` finding, lists each local episode's (season, episode) position against TheTVDB's aired and DVD orderings and the resulting unmatched/total score - useful for checking why a series was flagged. Series that pass the check write nothing. Not shown on the console.

Every CLI in this project - `auditor.py` and every standalone `apply_*.py`/`transfer_*.py` tool - logs the exact command line it was invoked with (program name plus every argument, shell-quoted) as the first line of its run, both to the console and to its own log file above. This is the one thing `auditor.py`'s `audit.log` always contains even for a plain run with no `--transfer-*` flag, since a persistent record of what was actually run is useful on its own regardless of what else that run did.

`auditor.py`'s end-of-run summary also reports, per audited server, how many Jellyfin API requests that run made (`Jellyfin API calls: N`) - and, once across the whole run rather than per server, how many TheTVDB API requests it made (`TheTVDB API calls: N`, only shown when a TheTVDB `api_key` is configured). Retried requests count individually, since each retry is a real network round trip; a request answered from the local TheTVDB cache (`tvdb_cache.json`) does not count at all, since it never reached the network. A server touched by more than one phase of the run - audited, then written to by `--transfer-metadata`/`--transfer-images`/`--transfer-subtitles`, then re-audited by `--verify` - reports the total across all of them, not just the initial audit.

## Project layout

- `auditor.py` - CLI entry point and orchestration, including the bulk `--transfer-metadata`, `--transfer-images`, and `--transfer-subtitles` runs
- `transfer_metadata.py` - standalone CLI to transfer one item's metadata between two servers; also provides the plan/apply functions the bulk run uses, plus `changed_fields()`/`rejected_reason()`/`lock_changed_fields()`, shared by the three `apply_*.py` tools below (via `title_backup.py` for the two rename tools) for their own plan-building
- `transfer_images.py` - standalone CLI to transfer one item's cached image between two servers; also provides the plan/apply functions the bulk `--transfer-images` run uses
- `transfer_subtitles.py` - standalone CLI to transfer one item's English subtitle track between two servers, entirely through the Jellyfin API; also provides the plan/apply functions the bulk `--transfer-subtitles` run uses
- `tvdb_series_resolution.py` - shared TheTVDB series-id resolution (`resolve_series_tvdb_id()`) used by all three TheTVDB-backed `apply_*.py` tools, so a series matched to the wrong TheTVDB entry doesn't silently corrupt data from some other show
- `title_backup.py` - shared Name/OriginalTitle rename-and-backup logic (`build_title_merged_item_dto()`/`build_title_restore_merged_item_dto()`, plus the LockedFields safety handling behind them) used by `apply_titles_from_filename.py`
- `apply_tvdb_metadata.py` - standalone CLI to apply TheTVDB's aired-order (`--aired`) or DVD-order (`--dvd`) episode Name/Overview values, or restore toward aired order preferring each episode's own `OriginalTitle` backup (`--restore`); skips episodes that already match under `audit.titles_match()`'s lenient comparison, and optionally (`--images`) replaces the Primary image too
- `apply_episode_numbers.py` - standalone CLI to fill in missing episode numbers for one series/season from TheTVDB's aired order, with interactive fuzzy title matching for episodes with no exact title match
- `apply_titles_from_filename.py` - standalone CLI to rename a single movie (`--movie`) or one series/season's episodes (`--series-name`/`--season-number`) to the title their own on-disk filename implies; never contacts TheTVDB. `--restore` reverses a rename locally from each item's own `OriginalTitle` backup
- `config.py` - application and server configuration
- `jellyfin.py` - Jellyfin API client (reads library/item data; also supports the item metadata, image, and subtitle read/upload calls transfers use, plus series/episode lookups by name for `apply_tvdb_metadata.py`/`apply_episode_numbers.py`/`apply_titles_from_filename.py`, and movie lookups by name for `apply_titles_from_filename.py`). Its whole-library listing also re-checks a couple of fields directly per-item when the listing's own value can't be trusted: an episode's `IndexNumber` when the listing shows it missing, and an item's `Name` when `LockedFields` shows it locked - see "Applying TheTVDB metadata to episodes" above for why a locked `Name` needs this.
- `models.py` - normalized data models
- `results.py` - structured per-library/per-server audit result types (`LibraryAuditResult`, `AuditServerResult`) shared across the auditor, reports, and comparison code
- `media.py` - media and filesystem helpers, including filename-based episode title parsing
- `audit.py` / `audit_types.py` - audit rules and finding types
- `search_tvdb_cache.py` - standalone CLI to search `tvdb_cache.json` by series name and print its cached aired-/DVD-order episode positions, for manually checking why a series was (or wasn't) flagged by `mismatched_tvdb_series`, without any network calls
- `reports\` - CSV and static HTML report generation
- `comparison\` - cross-server comparison report generation, including the transfer button and Transfer Results table
- `xlsx_report.py` - combined Excel workbook generation (`audit_results.xlsx`): one named-table sheet per server (with a `Problems` count column and a `Totals` row) plus an optional `diffs` sheet, with conditional formatting
- `compare_csv_files.py` - standalone CLI to diff two audit CSVs into `diffs.csv`; also provides the header/row-diffing functions `xlsx_report.py` reuses for the `diffs` sheet
- `output_layout.py` - shared output directory and site-index layout helpers
- `report_filters.py` - shared category/severity filtering for report output
- `report_theme.py` - shared dark/light theme toggle markup and script
- `tests\` - unit tests, one `test_<module>.py` file per corresponding project module (e.g. `test_audit_rules.py` for `audit.py`, `test_apply_tvdb_metadata.py` for `apply_tvdb_metadata.py`), plus `helpers.py` for fixture builders (`_make_item`, `_make_library`, etc.) shared across them

## Development

Run the test suite:

```powershell
python -m unittest
```

## Notes

The project structure is intentionally modular so additional audits can be added without mixing Jellyfin API code, filesystem checks, and report generation logic.