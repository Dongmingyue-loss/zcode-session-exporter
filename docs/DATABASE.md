# Database schema

The exporter reads the ZCode / ZCodium CLI SQLite database. It does not write to it.

## Tables used

### `session`

```sql
CREATE TABLE session (
  id TEXT PRIMARY KEY,
  title TEXT,
  directory TEXT,
  time_created INTEGER,
  time_updated INTEGER,
  task_type TEXT,
  parent_id TEXT,
  slug TEXT
);
```

### `message`

```sql
CREATE TABLE message (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  time_created INTEGER,
  time_updated INTEGER,
  data TEXT,
  sequence INTEGER
);
```

`data` is JSON:

```json
{
  "role": "assistant",
  "time": {"created": 1790000000000},
  "providerID": "provider-id",
  "modelID": "model-id",
  "mode": "plan",
  "agent": "zcode-agent",
  "tokens": {"input": 1200, "output": 180, "reasoning": 60},
  "finish": "stop"
}
```

### `part`

```sql
CREATE TABLE part (
  id TEXT PRIMARY KEY,
  message_id TEXT,
  session_id TEXT,
  time_created INTEGER,
  time_updated INTEGER,
  data TEXT,
  sequence INTEGER
);
```

`data` is one of the part types below.

## Part types

### text

```json
{"type": "text", "text": "..."}
```

### reasoning

```json
{"type": "reasoning", "text": "..."}
```

### tool

```json
{
  "type": "tool",
  "tool": "Bash",
  "callID": "call_...",
  "state": {
    "status": "completed",
    "input": {"cmd": "..."},
    "output": "...",
    "error": "",
    "metadata": {}
  }
}
```

### file

```json
{
  "type": "file",
  "filename": "report.json",
  "mime": "application/json",
  "url": "D:\\path\\report.json",
  "metadata": {
    "sizeBytes": 4096,
    "preview": {"text": "..."}
  }
}
```

### timeline

```json
{
  "type": "timeline",
  "timelineType": "model_change",
  "fromModel": {"label": "provider/old"},
  "toModel": {"label": "provider/new"},
  "status": "completed"
}
```

### compaction

```json
{
  "type": "compaction",
  "trigger": "auto",
  "compactReason": "context_limit",
  "phase": "mid_turn",
  "preCompactTokenCount": 48000,
  "timelineStatus": "completed"
}
```

### step-start / step-finish

```json
{"type": "step-start"}
```

```json
{
  "type": "step-finish",
  "reason": "tool-calls",
  "tokens": {"input": 1200, "output": 180, "reasoning": 60}
}
```

## Read-only queries

The exporter opens databases with:

```text
file:<percent-encoded-path>?mode=ro
PRAGMA query_only = 1
PRAGMA busy_timeout = 30000
```

If the database is in WAL mode and cannot be opened read-only, it retries with
`mode=ro&immutable=1`. That fallback may not see rows that exist only in the
WAL file.

## Schema compatibility

The tool requires `session`, `message`, and `part` tables. If a future ZCode /
ZCodium version renames or removes them, the source is reported as unavailable
with a clear missing-table error rather than silently exporting partial data.
