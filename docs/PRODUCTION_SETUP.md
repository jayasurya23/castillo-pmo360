# Production Setup

Walkthrough for moving from `LOCAL_DEV_MODE=true` to a real production deployment.

## Prerequisites

- Azure AD tenant (Castillo's existing M365 tenant)
- PostgreSQL 14+ instance
- Hosting environment for Streamlit (internal VM, Azure App Service, or Streamlit Cloud Enterprise)

## 1. PostgreSQL setup

### Local Docker (for dev)
```bash
docker run -d --name castillo-pg \
  -e POSTGRES_USER=castillo \
  -e POSTGRES_PASSWORD=changeme \
  -e POSTGRES_DB=castillo_meetings \
  -p 5432:5432 \
  postgres:16
```

### Production
Use Azure Database for PostgreSQL Flexible Server (recommended for M365 shop). Set the connection string in `.env`:
```
DATABASE_URL=postgresql://castillo@castillo-db.postgres.database.azure.com:5432/castillo_meetings?sslmode=require
```

### Schema bootstrap
Alembic is already set up (`migrations/`) and reads the connection string
from `config.database_url()`, so `DATABASE_URL` above is all it needs — no
separate Alembic config. First run of the app still auto-creates tables via
`init_db()` and stamps the DB at the current migration head; for a fresh
Postgres instance you can instead apply the migrations directly before
first run:
```bash
DATABASE_URL=postgresql://castillo@castillo-db.postgres.database.azure.com:5432/castillo_meetings?sslmode=require \
LOCAL_DEV_MODE=false \
alembic upgrade head
```
Going forward, every schema change is a new migration — see "Database
migrations" in the README.

## 2. Azure AD app registration

1. **Azure Portal → Azure Active Directory → App registrations → New registration**
2. Name: `Castillo Meeting Tool`
3. Supported account types: Single tenant
4. Redirect URI (if using SSO): `https://meetings.castilloengineering.com/auth/callback` (Web)

### API permissions to add (Microsoft Graph)

| Permission | Type | Why |
|---|---|---|
| `Sites.ReadWrite.All` | Application | Read/write SharePoint files |
| `Mail.Send` | Application | Send meeting minutes as email |
| `User.Read` | Delegated | SSO login |
| `Files.ReadWrite.All` | Application | Backup access to file operations |

Grant admin consent after adding.

### Create a client secret

Certificates & secrets → New client secret. Copy it immediately (won't show again). Add to `.env`:
```
AZURE_TENANT_ID=<directory tenant id>
AZURE_CLIENT_ID=<application client id>
AZURE_CLIENT_SECRET=<the secret you just copied>
```

## 3. SharePoint site setup

1. Create a SharePoint site for the tool, e.g. `https://castilloengineering.sharepoint.com/sites/MeetingTool`
2. Find the site ID via Graph Explorer or the URL inspector:
   ```
   GET https://graph.microsoft.com/v1.0/sites/castilloengineering.sharepoint.com:/sites/MeetingTool
   ```
3. Get the drive ID:
   ```
   GET https://graph.microsoft.com/v1.0/sites/<site-id>/drives
   ```
4. Set in `.env`:
   ```
   SHAREPOINT_SITE_ID=<site id from step 2>
   SHAREPOINT_DRIVE_ID=<drive id from step 3>
   SHAREPOINT_ROOT_FOLDER=Castillo Meeting Tool
   ```

## 4. Switch to production mode

```env
LOCAL_DEV_MODE=false
DATABASE_URL=postgresql://...
AZURE_TENANT_ID=...
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...
SHAREPOINT_SITE_ID=...
SHAREPOINT_DRIVE_ID=...
```

The app will refuse to start if `LOCAL_DEV_MODE=false` and any of the above are missing.

## 5. Deployment options

### Option A — Internal VM
```bash
# On the VM, after cloning:
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# Use systemd to run streamlit as a service
```
Pros: Full control, no external dependencies. Cons: You maintain it.

### Option B — Azure App Service
Use Python runtime. Set startup command:
```bash
streamlit run app/main.py --server.port 8000 --server.address 0.0.0.0
```
Pros: Managed. Cons: Costs ~$50/mo for B1 tier.

### Option C — Streamlit Cloud Enterprise
Pros: Easiest. Cons: Needs Streamlit Enterprise license.

## 6. SSO integration (if Castillo wants single sign-on)

Add `streamlit-authenticator` or wrap with an OAuth reverse proxy:
```python
import streamlit_authenticator as stauth
# ... see streamlit-authenticator docs for OIDC setup
```

For Azure AD specifically, the easiest path is to put the app behind Azure Application Proxy or use Azure Front Door with auth.

## 7. Backup

PostgreSQL: nightly `pg_dump` to Azure Blob Storage.
SharePoint files: already backed up by M365 retention policies (verify with IT).
Local outputs in dev mode: not backed up — they're meant to be transient.

## Cost estimates

| Item | Monthly cost |
|---|---|
| OpenAI API (gpt-4o-mini, ~30 meetings × 5K tokens each) | ~$3-8 |
| Azure Database for PostgreSQL Flexible Server (Burstable B1ms) | ~$15 |
| Azure App Service (B1) | ~$55 |
| **Total** | **~$75-80** |

Lower if hosted on internal VM (zero hosting cost, just OpenAI + Postgres).
