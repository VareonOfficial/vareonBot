# 📋 live_logs Table

- 🏠 [Home](../)
- 📖 **Detailed Event Flow & Lifecycle:** [View Log Design Structure](loggingArch.md)

The `live_logs` table serves as the primary event store for the Vareon telemetry system.

Every tracked operation generates a new record within this table. These records provide a complete audit trail of user actions, command executions, file operations, system events, and background tasks.

Unlike `monthly_stats`, which stores aggregated analytics, `live_logs` preserves every event exactly as it occurred.

Each record contains:

| Field         | Purpose                                             |
| ------------- | --------------------------------------------------- |
| id            | Primary key                                         |
| vareon_id     | Internal Vareon user identifier                     |
| tg_user_id    | Telegram user ID                                    |
| event_type    | Event category                                      |
| function_name | Function responsible for generating the event       |
| task_id       | Unique operation identifier                         |
| details       | JSON object containing event-specific metadata      |
| action_status | JSON object containing execution status information |
| timestamp     | UTC timestamp of event creation                     |

## Sample Log Records

| id | vareon_id | tg_user_id | function_name    | event_type             | task_id      | details                              | action_status            | timestamp           |
| -- | --------- | ---------- | ---------------- | ---------------------- | ------------ | ------------------------------------ | ------------------------ | ------------------- |
| 42 | 12345678  | 987654321  | start_download   | DOWNLOAD_PATH_SELECTED | a1b2c3d4-e5f | {"destination_path":"/user/storage"} | {"status":"in_progress"} | 2026-06-08 16:24:31 |
| 43 | 12345678  | 987654321  | process_download | DOWNLOAD_COMPLETE      | a1b2c3d4-e5f | {"time_taken":84}                    | {"status":"success"}     | 2026-06-08 16:25:55 |

---

## action_status

The `action_status` field stores execution metadata generated automatically by the logging system.

Example:

```json
{
  "action": "download_file",
  "status": "success",
  "latency": "842ms"
}
```

Common status values:

| Status      | Meaning                                          |
| ----------- | ------------------------------------------------ |
| success     | Operation completed successfully                 |
| error       | Exception occurred during execution              |
| failed      | Operation completed but returned a failure state |
| in_progress | Operation currently running                      |

---

## details

The `details` field stores event-specific metadata.

Its structure varies depending on the event being recorded.

Example:

```json
{
  "file_name": "movie.mkv",
  "size_bytes": 104857600,
  "resolution": "1080p"
}
```

Common examples include:

* File paths
* Download identifiers
* Queue information
* Processing times
* File sizes
* Command parameters

The field is intentionally flexible to allow different modules to attach contextual information without requiring database schema changes.
