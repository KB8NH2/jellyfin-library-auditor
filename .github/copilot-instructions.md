# Jellyfin Library Auditor - Copilot Instructions

This project is a Python application that audits a Jellyfin media library.

## General

- Target Python 3.12+
- Follow PEP 8
- Use type hints throughout
- Use pathlib instead of os.path
- Use requests for REST API access
- Use logging instead of print(), except for CLI output
- Write Google-style docstrings
- Prefer readability to cleverness
- Keep functions reasonably small
- Avoid unnecessary third-party packages

## Architecture

- config.py contains all configuration constants.
- jellyfin.py contains all Jellyfin REST API communication.
- models.py contains data classes.
- media.py analyzes media items.
- filesystem.py performs local filesystem checks.
- reports.py generates CSV and HTML reports.
- auditor.py is the CLI entry point.

## Goals

Audit:

- English subtitles
- Posters
- Fanart
- NFO files
- Chapters
- Codecs
- Resolution
- HDR
- Duplicate media
- Missing metadata

The project should be easy to extend with additional audits.