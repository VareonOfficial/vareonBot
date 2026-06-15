# ⚙️ Environment Configuration Guide

- 🏠 [Home](../../README.md)

This guide explains how to create your environment file before running the Vareon bot. The bot reads all its configuration — tokens, paths, database credentials — from this file at startup.

---

## 📄 File Naming

You must name your file exactly as shown below depending on your use case:

| File Name  | Purpose                                      |
| ---------- | -------------------------------------------- |
| `prod.env` | Production instance (your live, stable bot)  |
| `dev.env`  | Development instance (testing, experiments)  |

> ⚠️ **Important:** The Docker commands use these exact names to identify which environment to load. Using a different name will cause a mismatch and the container will fail to start.

Place both files inside the `main/Environments/` directory.

---

## 📋 Template

Copy the template below and fill in your own values. Each field is explained in the section below.

```dotenv
# ============================================
# TELEGRAM
# ============================================

API_ID=
API_HASH=

BOT_TOKEN=
SUPPORT_BOT_TOKEN=

# ============================================
# ADMIN
# ============================================

ADMIN_ID=

# ============================================
# PATHS
# ============================================

BASE_PATH=/var/lib/vareon

# ============================================
# LOGIN
# ============================================

LOGIN_LINK=

# ============================================
# GROUPS
# ============================================

SOS_GROUP_ID=
SUPPORT_GROUP_ID=
PRIVATE_GROUP_ID=
PRIVATE_GROUP_LINK=

# ============================================
# BROWSER AND LINK GENERATOR API
# ============================================

CDP_URL=

# ============================================
# POSTGRESQL
# ============================================

DB_USER=
DB_PASSWORD=
DB_PORT=5432

# ============================================
# COOKIE TUTORIAL VIDEO IDs
# ============================================

SPOTIFY_COOKIE_VIDEO_ID=
YT_COOKIE_VIDEO_ID=
```

---

## 🔍 Field Reference

### Telegram

| Field                | Required | Description                                                                 |
| -------------------- | -------- | --------------------------------------------------------------------------- |
| `API_ID`             | ✅ Yes   | Your Telegram API ID. Get it from [my.telegram.org](https://my.telegram.org) |
| `API_HASH`           | ✅ Yes   | Your Telegram API Hash. Same source as above                                |
| `BOT_TOKEN`          | ✅ Yes   | Your main bot token from [@BotFather](https://t.me/BotFather)              |

---

### Admin

| Field        | Required | Description                                              |
| ------------ | -------- | -------------------------------------------------------- |
| `ADMIN_ID`   | ✅ Yes   | Your personal Telegram user ID. The bot grants you admin access based on this |

> You can get your Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot).

---

### Paths

| Field       | Required | Description                                                        |
| ----------- | -------- | ------------------------------------------------------------------ |
| `BASE_PATH` | ✅ Yes   | The root directory on your server where the bot stores user files. Default is `/var/lib/vareon` — change this if your server uses a different mount or storage path |

---

### Login

| Field        | Required | Description |
| ------------ | -------- | ----------- |
| `LOGIN_LINK` | ⚠️ Depends | The URL shown to users when they need to log in |

> **Note on Login System:** By default, the bot uses a web-based login flow via `LOGIN_LINK`, which connects to an external auth page backed by the PostgreSQL database. This is entirely customizable.
>
> **Recommendation:** If you want to simplify things, you can remove the web login entirely and handle authentication directly through your existing database logic. This reduces code complexity since your DB credentials are already configured below. Modify the login logic in the bot's code to suit your preferred method.

---

### Groups

These are **optional** Telegram group/channel IDs used for specific bot features. Leave them empty if you don't need those features.

| Field                | Required | Description                                                        |
| -------------------- | -------- | ------------------------------------------------------------------ |
| `SUPPORT_BOT_TOKEN`  | ⬜ Optional   | A second bot token used for support interactions                            |
| `SOS_GROUP_ID`       | ⬜ Optional | Group where support conversations are forwarded                 |
| `SUPPORT_GROUP_ID`   | ⬜ Optional | Group where bug reports are stored                              |
| `PRIVATE_GROUP_ID`   | ✅ Yes   | Private channel used internally for file operations (upload/download via tdl) |
| `PRIVATE_GROUP_LINK` | ✅ Yes   | Invite link to the above private group                            |

> Group IDs are negative numbers starting with `-100`. You can get them by forwarding a message from the group to [@userinfobot](https://t.me/userinfobot).

---

### Browser & Link Generator API

| Field     | Required | Description |
| --------- | -------- | ----------- |
| `CDP_URL` | ⬜ Optional | URL for the Chrome DevTools Protocol endpoint, used for browser-based link generation. Leave empty if you are not using this feature |

---

### PostgreSQL

| Field         | Required | Description                                                     |
| ------------- | -------- | --------------------------------------------------------------- |
| `DB_USER`     | ✅ Yes   | PostgreSQL username                                             |
| `DB_PASSWORD` | ✅ Yes   | PostgreSQL password                                             |
| `DB_PORT`     | ✅ Yes   | PostgreSQL port. Default is `5432` — only change if your setup differs |

> The database connection is also used by the default login system. If you modify the login logic, these credentials may take on a different role depending on your implementation.

---

### Cookie Tutorial Video IDs

These are Telegram file IDs for tutorial videos shown to users inside the `/cookies` command. Both are optional.

| Field                   | Required | Description                              |
| ----------------------- | -------- | ---------------------------------------- |
| `SPOTIFY_COOKIE_VIDEO_ID` | ⬜ Optional | Telegram file ID for the Spotify cookie setup tutorial |
| `YT_COOKIE_VIDEO_ID`    | ⬜ Optional | Telegram file ID for the YouTube cookie setup tutorial |

> If left empty, the bot will simply not send a tutorial video for those commands. You can upload your own tutorial video to Telegram and use its file ID here.

---

## ✅ Checklist Before Starting

- [ ] File is named `prod.env` or `dev.env`
- [ ] File is placed inside `main/Environments/`
- [ ] `API_ID`, `API_HASH`, and `BOT_TOKEN` are filled
- [ ] `ADMIN_ID` is set to your own Telegram user ID
- [ ] `DB_USER`, `DB_PASSWORD`, and `DB_PORT` are configured
- [ ] `PRIVATE_GROUP_ID` and `PRIVATE_GROUP_LINK` are set
- [ ] `BASE_PATH` points to a valid directory on your server