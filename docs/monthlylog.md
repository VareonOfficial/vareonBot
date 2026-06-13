# 📊 monthly_stats Table

- 🏠 [Home](../)

The `monthly_stats` table stores pre-aggregated analytics derived from the `live_logs` table.

Unlike `live_logs`, which records individual events, `monthly_stats` maintains summarized activity metrics for each user on a month-by-month basis. This significantly reduces the overhead of generating reports, dashboards, and usage statistics.

Each record contains:

| Field                | Purpose                                                     |
| -------------------- | ----------------------------------------------------------- |
| id                   | Primary key                                                 |
| vareon_id            | Internal Vareon user identifier                             |
| tg_user_id           | Telegram user ID                                            |
| year                 | Analytics year                                              |
| month                | Analytics month                                             |
| total_actions        | Total number of logged events during the month              |
| success_count        | Number of successful actions                                |
| failed_count         | Number of failed or errored actions                         |
| event_type_counts    | JSON object containing event frequency counts               |
| function_name_counts | JSON object containing function execution counts            |
| active_days          | Number of unique days with recorded activity                |
| most_active_hour     | Hour with the highest activity volume (UTC, 24-hour format) |
| most_active_weekday  | Weekday with the highest activity volume                    |
| first_activity       | Timestamp of the first recorded activity in the month       |
| last_activity        | Timestamp of the most recent recorded activity in the month |

## Sample Analytics Records

| id | vareon_id | tg_user_id | year | month | total_actions | success_count | failed_count | active_days | most_active_hour | most_active_weekday | first_activity      | last_activity       |
| -- | --------- | ---------- | ---- | ----- | ------------- | ------------- | ------------ | ----------- | ---------------- | ------------------- | ------------------- | ------------------- |
| 3  | 12345678  | 987654321  | 2026 | 6     | 28            | 26            | 2            | 4           | 18               | 2                   | 2026-06-01 08:15:22 | 2026-06-10 21:44:18 |
| 4  | 87654321  | 123456789  | 2026 | 6     | 73            | 69            | 4            | 8           | 22               | 5                   | 2026-06-02 11:42:07 | 2026-06-11 18:31:54 |

---

## event_type_counts

Stores a JSON object containing the number of occurrences of each event type recorded during the month.

Example:

```json
{
  "FILE_RECEIVED": 12,
  "DOWNLOAD_PATH_SELECTED": 12,
  "QUEUE_JOINED": 18,
  "QUEUE_WAIT_COMPLETE": 18,
  "DOWNLOAD_COMPLETE": 11
}
```

---

## function_name_counts

Stores a JSON object containing the number of times each tracked function was executed during the month.

Example:

```json
{
  "handle_file": 12,
  "run_tdl_download": 12,
  "queue_tdl_task": 24,
  "_do_download": 11
}
```

---

## Weekday Mapping

The `most_active_weekday` field follows SQLite's `strftime('%w')` format.

| Value | Day       |
| ----- | --------- |
| 0     | Sunday    |
| 1     | Monday    |
| 2     | Tuesday   |
| 3     | Wednesday |
| 4     | Thursday  |
| 5     | Friday    |
| 6     | Saturday  |

---

## Hour Format

The `most_active_hour` field uses UTC time in 24-hour format.

| Value | Time Range    |
| ----- | ------------- |
| 0     | 00:00 - 00:59 |
| 8     | 08:00 - 08:59 |
| 18    | 18:00 - 18:59 |
| 23    | 23:00 - 23:59 |

```

This section is automatically maintained by the analytics engine whenever a new record is inserted into `live_logs`.
```
