# Jellyfin Library Auditor

Jellyfin Library Auditor is a Python CLI that connects to one or more Jellyfin servers, audits movie and TV libraries, and generates CSV and static HTML reports.

It is designed to highlight library gaps that are easy to miss in day-to-day use, such as missing English subtitles, missing artwork, unknown codecs, and missing numbered TV seasons or episodes. It can also compare two Jellyfin servers and generate side-by-side comparison pages.

## Current audit coverage

The current implementation checks:

- Missing configured English subtitles
- Missing local poster files
- Missing local backdrop files
- Missing Jellyfin primary images
- Missing or unknown primary video codecs
- Missing primary audio codecs
- Missing numbered TV seasons within a series
- Missing numbered TV episodes within a season
- TV episode metadata titles that don't match the title implied by the filename's `SxxExx` naming (including `SxxEyy-Ezz` multi-episode ranges)
- Movie metadata titles that don't match the title implied by the filename's `(Year)` naming

The filename-title checks tolerate cosmetic differences that don't represent a real mismatch: dot-delimited release names (`Show.S01E02.Episode.Title.mkv`), straight vs. curly quotes and dashes, roman vs. arabic numerals in a parenthetical suffix (`(I)` vs. `(1)`), `&` vs. `and`, `+` vs. `/`, a Unicode ellipsis (`…`) vs. three literal periods (`...`), and leading parenthetical text that's actually part of the title (e.g. `(Dis)Members Only`). Release-quality tags in a filename (`1080p`, `WEB-DL`, `x264`, etc.) are stripped only when they form a genuine trailing run of tags, so an ordinary title word that happens to look like one - an episode called "Spider in the Web," for example - isn't mistaken for one.

## Features

- Audits enabled movie and TV libraries from Jellyfin
- Produces CSV output for spreadsheet-style review
- Produces a static HTML dashboard with library and check drill-down pages
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
```

### Environment variables

Optional environment variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `REPORT_MEDIA_PATH_PREFIX` | Removes a path prefix from report display paths | empty |
| `MOVIES_CSV_FILENAME` | Movie CSV filename | `movies_report.csv` |
| `TV_CSV_FILENAME` | TV CSV filename | `tv_report.csv` |
| `AUDIT_CSV_FILENAME` | Per-server audit CSV filename | `audit_report.csv` |
| `AUDIT_HTML_FILENAME` | Root HTML output directory name | `audit_results` |
| `ENABLE_MOVIES` | Enable movie library auditing | `true` |
| `ENABLE_TV` | Enable TV library auditing | `true` |
| `ENGLISH_LANGUAGE_CODES` | Comma-separated language codes treated as English subtitles | `en,eng,` |

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
| `--category CATEGORY` | Filter by category: `subtitles`, `artwork`, `metadata`, `video`, `audio`, `filesystem` |
| `--severity SEVERITY` | Filter by severity: `info`, `warning`, `error` |
| `--transfer-metadata` | Transfer metadata for every mismatched item from the base `--server` to the `--compare` server; requires `--compare` (see [Synchronizing metadata between servers](#synchronizing-metadata-between-servers)) |
| `--dry-run` | With `--transfer-metadata`, preview planned transfers without writing anything |
| `--yes` | With `--transfer-metadata`, skip the batch confirmation prompt |

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

**Given this writes to a live Jellyfin server, run `--dry-run` first and review the diff before trusting it on a real library, especially unattended.**

## Output

By default, reports are written under `audit_results\`.

- `audit_results\index.html` - top-level report index
- `audit_results\<server>\index.html` - per-server HTML dashboard
- `audit_results\<server>\audit_report.csv` - per-server CSV findings
- `audit_results\comparison_results\index.html` - comparison dashboard when `--compare` is used
- `metadata_transfer.log` - append-only record of every metadata transfer, written next to wherever `auditor.py --transfer-metadata` or `transfer_metadata.py` was run

## Project layout

- `auditor.py` - CLI entry point and orchestration, including the bulk `--transfer-metadata` run
- `transfer_metadata.py` - standalone CLI to transfer one item's metadata between two servers; also provides the plan/apply functions the bulk run uses
- `config.py` - application and server configuration
- `jellyfin.py` - Jellyfin API client (reads library/item data; also supports the item metadata read/update calls transfers use)
- `models.py` - normalized data models
- `media.py` - media and filesystem helpers, including filename-based episode title parsing
- `audit.py` / `audit_types.py` - audit rules and finding types
- `reports\` - CSV and static HTML report generation
- `comparison\` - cross-server comparison report generation, including the transfer button and Transfer Results table
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