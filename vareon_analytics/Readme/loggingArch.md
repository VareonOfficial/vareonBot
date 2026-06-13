# 📊 System Logging & Event Lifecycle

- 🏠 [Home](../)
- 📊 [live_logs Table](logging.md)

This document maps out how our backend logging system tracks operations. Every user command triggers structured state transitions, capturing specific parameters (`Inputs`) to trace successes, failures, and cancellations.

---

## 🗺️ Visual Flowchart

```mermaid
graph LR
    %% Live-log for different functions
    A[For /link] --> A1[LINK RECEIVED<br>Input: url, link_type]
    
    A1 --> |If direct link| A2[PATH_SELECTED<br>Input: path, file_name, size_bytes, resume_supported<br>DOWNLOAD_STARTED<br>Input: download_id]
    A1 --> |If YouTube link| A3[DOWNLOAD_STARTED<br>Input: fmt_id, resolution<br>FILE_INFO<br>Input: path, file_name, size_bytes]
    A2 --> A5
    A2 --> A4
    A3 --> A4
    A4[DOWNLOAD_COMPLETE<br>Input: time_taken, canceled]
    A5[DOWNLOAD_PAUSED<br>pause: True, resume: False<br>DOWNLOAD_RESUMED<br>pause: False, resume: True]
    
    %% Clean Side-Branches for Fallbacks
    A2 -.->|On Error| A6[DOWNLOAD_ERROR<br>Input: time_taken, error]
    A3 -.->|On Error| A6

    %% File Section
    B[For /files] --> B1[FILE_RECEIVED<br>Input: file_name, file_size, file_type, forwarded_link<br>DOWNLOAD_PATH_SELECTED<br>Input: destination_path]
    B1 --> B2[QUEUE_JOINED<br>Input: kind,queue_position<br>QUEUE_WAIT_COMPLETE<br>Input: kind, queue_wait_seconds]
    B2 --> B3[DOWNLOAD_COMPLETE<br>Input: destination_path, time_taken]
    
    %% Clean Side-Branch for File Fallback
    B2 -.->|On Error| B4[DOWNLOAD_FAILED<br>Input: time_taken, exit_code, download_started]

    %% Music Section
    C[For /music] --> C1[MUSIC_INFO<br>Input: path, url, type, num_tracks]
    C1 --> C2[DOWNLOAD_COMPLETE<br>Input: time_taken]
    C1 -.->|On Error| C3[DOWNLOAD_ERROR<br>Input: time_taken, tracks_completed]

    %% Myfiles Section
    D[For /myfiles] --> D1[NEW_FOLDER<br>Input: name]
    D --> D2[RENAME_FOLDER<br>Input: old_name, new_name]
    D --> D3[MOVE_FOLDER<br>Input: old_location, new_location]
    D --> D4[DELETE_FOLDER<br>Input: name, path]
    D --> D5[UPLOAD_STARTED<br>Input: name, path, size<br>UPLOAD_COMPLETED<br>Input: time_taken]
    D --> D6[GET_LINK<br>Input: url, name, path, size]
    D --> D7[COMPRESS_STARTED<br>Input: format, zip_name<br>COMPRESS_COMPLETED<br>Input: time_taken]
    D --> D8[EXTRACT<br>Input: path, time_taken<br>EXTRACT_TO_FOLDER<br>Input: path, time_taken]
    %% Multi-Selection
    D --> D0[MULTIPLE_SELECTION]
    D0 --> D9[MULTI-DELETE<br>Input: items_selected]
    D0 --> D10[MULTI-MOVE<br>Input: new_location, items_selected]
    D0 --> D11[MULTI-COMPRESS<br>Input: format, zip_name, items_selected<br>MULTI-COMPRESS_COMPLETED<br>Input: time_taken]
    D0 --> D12[MULTI-EXTRACT_SEPARATELY<br>Input: items_selected, time_taken<br>MULTI-EXTRACT_SINGLE_FOLDER<br>Input: new_name, time_taken]

    D1 --> D13[PROCESS_CANCELED<br>Input: process_name]
    D2 --> D13
    D4 --> D13
    D5 --> D13
    D6 --> D13
    D7 --> D13
    D8 --> D13
    D9 --> D13
    D10 --> D13
    D11 --> D13
    D12 --> D13

    %% Cookies Section
    E[For /cookies] --> E1[YT_COOKIE<br>Input: set, remove]
    E --> E2[SPOTIFY_COOKIE<br>Input: set, remove]

    %% Settings Section
    F[For /settings] --> F1[DEFAULT_DOWNLOAD_DIR<br>Input: ON/OFF, path]
    F --> F2[BOT_UPDATES<br>Input: ON/OFF]

    %% Report Section
    G[For /report] --> G1[BUG_REPORTED<br>Input: bug_id]
```

---
