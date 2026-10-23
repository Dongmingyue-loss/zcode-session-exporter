# Changelog

## v1.0.0

Initial release.

- Read-only access to ZCode / ZCodium `session`, `message`, and `part` tables.
- Local web GUI with session search, sorting, and paged message preview.
- Readable TXT export with reasoning, tool calls, tool output, file references, model-change markers, and compaction markers.
- Raw JSONL export with message and part metadata.
- Paged export preview with previous/next/jump navigation.
- Custom User / Agent display names for readable TXT.
- Streaming export with progress, cancellation, split files, and ZIP packaging.
- Automatic backup-free read-only operation; source databases are never modified.
- Cross-platform launchers: `start.cmd` (Windows) and `start.sh` (macOS/Linux).
- JSON1 fallback for SQLite builds without JSON functions.
- Bundled synthetic demo database.
