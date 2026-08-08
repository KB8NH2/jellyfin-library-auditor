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

## Features

- Audits enabled movie and TV libraries from Jellyfin
- Produces CSV output for spreadsheet-style review
- Produces a static HTML dashboard with library and check drill-down pages
- Supports filtering by library, finding category, and severity
- Supports auditing one server, all configured servers, or comparing two servers
- Uses normalized data models to keep audit logic separate from API and report code

## Requirements

- Python 3.12+
- A reachable Jellyfin server
- A Jellyfin API key for each server you want to audit

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

## Output

By default, reports are written under `audit_results\`.

- `audit_results\index.html` - top-level report index
- `audit_results\<server>\index.html` - per-server HTML dashboard
- `audit_results\<server>\audit_report.csv` - per-server CSV findings
- `audit_results\comparison_results\index.html` - comparison dashboard when `--compare` is used

## Project layout

- `auditor.py` - CLI entry point and orchestration
- `config.py` - application and server configuration
- `jellyfin.py` - Jellyfin API client
- `models.py` - normalized data models
- `media.py` - media and filesystem helpers
- `audit.py` / `audit_types.py` - audit rules and finding types
- `reports\` - CSV and static HTML report generation
- `comparison\` - cross-server comparison report generation
- `tests\` - unit tests

## Development

Run the test suite:

```powershell
python -m unittest
```

## Notes

The project structure is intentionally modular so additional audits can be added without mixing Jellyfin API code, filesystem checks, and report generation logic.