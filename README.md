## 🏗️ Vareon Deployment Hub
---

### 📂 1. Environment Configuration

Before launching your services, you must define your secrets and configurations. The project uses a directory-based environment structure to keep production and development data isolated.

* **Action:** Create your environment files in the `main/Environments/` directory.
* **Production:** Create a file named `prod.env`.
* **Development:** Create a file named `dev.env` for testing new features or secondary bots.

> [!TIP]
> **Check the Docs:** For a detailed breakdown of required variables and formatting, refer to the **README.md** located inside the `main/Environments/` folder.

---

### 🚀 2. Production Deployment

Use this command to launch the stable version of your application. This uses the production environment variables and assigns a unique project name to prevent container collisions.

**Run Production:**

```bash
ENV_FILE_PATH=main/Environments/prod.env docker compose -p vareon-prod up --build -d && docker compose -p vareon-prod logs -f

```
**Stop Development:**

```bash
ENV_FILE_PATH=main/Environments/prod.env docker compose -p vareon-prod down

```

---

### 🛠️ 3. Development & Testing

If you need to test changes on a different bot instance without affecting the production service, you can run a parallel stack. By changing the `-p` (project name) flag and the `ENV_FILE_PATH`, Docker treats this as a completely separate entity.

**Run Development:**

```bash
ENV_FILE_PATH=main/Environments/dev.env docker compose -p vareon-dev up --build -d && docker compose -p vareon-dev logs -f

```
**Stop Development:**

```bash
ENV_FILE_PATH=main/Environments/dev.env docker compose -p vareon-dev down

```

---

**Key Advantage:** This method allows you to run both **Production** and **Development** versions of your code simultaneously on the same server without them interfering with each other's logs or containers.
