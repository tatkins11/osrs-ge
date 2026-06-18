# Deploying the always-on terminal to a VPS

Goal: a small server that **collects prices 24/7** and serves the **dashboard
over HTTPS with a login**, reachable from anywhere (including work wifi, since
your browser only talks to *your* server — the blocked game API is fetched
server-side).

You don't need a VPN. You don't need a domain to start (we use a self-signed
certificate; the browser warns once, then it's encrypted).

---

## 1. Pick a VPS (~$6–12 / month)

All of these are **US companies with US data centers**, and the deploy kit runs
identically on any of them. Use **Ubuntu 24.04**.

**Recommended (easiest): DigitalOcean** — Basic Droplet, **2 GB / 1 vCPU ($12/mo)**.
Best beginner docs, and a one-click **"Docker" Marketplace image** that comes with
Docker preinstalled (skip the install in section 2). US regions: NYC, SF.

| Provider (US) | Plan | ~Price | Notes |
|---|---|---|---|
| **DigitalOcean** (rec.) | Basic 2 GB | $12/mo | Easiest; Docker Marketplace image |
| Vultr | Cloud Compute 2 GB | $10/mo | Many US cities |
| Linode / Akamai | 2 GB | $12/mo | Solid, US-based |
| AWS Lightsail | 2 GB | ~$10/mo | If you prefer Amazon; a little more setup |

**Cheapest:** any **1 GB** plan ($5–6/mo) also works, but the Docker build can run
out of memory — add a swap file first (section 2). The 2 GB plan avoids that.

*(Hetzner is cheaper still and does have US data centers in Ashburn & Hillsboro,
but it's a German company — mentioned only if you don't mind that.)*

Create the server and add your SSH key during creation (or use the emailed root password).

---

## 2. Connect and install Docker

```bash
ssh root@YOUR_SERVER_IP

# Install Docker + Compose plugin (SKIP if you used DigitalOcean's Docker image)
curl -fsSL https://get.docker.com | sh

# Basic firewall: allow SSH + web
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable

# ONLY on a 1 GB server: add 2 GB swap so the build doesn't run out of memory
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

---

## 3. Get the project onto the server

**Option A — GitHub (works from your work machine; GitHub isn't blocked):**
On your PC, create a *private* repo and push:
```bash
cd "OSRS GE"
git init && git add . && git commit -m "OSRS GE Terminal"
git remote add origin https://github.com/<you>/osrs-ge.git
git push -u origin main
```
Then on the VPS:
```bash
git clone https://github.com/<you>/osrs-ge.git && cd osrs-ge
```

**Option B — copy directly (from a personal machine):**
```bash
scp -r "OSRS GE" root@YOUR_SERVER_IP:/root/osrs-ge   # then: cd /root/osrs-ge
```

*(The `.gitignore` keeps the venv, demo database, and `.env` out of the repo,
so you start clean on the server.)*

---

## 4. Configure secrets

Generate a login password hash and write `.env` in one shot (change the
password and, if you like, the user/contact):

```bash
# The sed doubles every "$" so Docker Compose treats the hash as literal text,
# not as variable references.
HASH=$(docker run --rm caddy:2 caddy hash-password --plaintext 'CHOOSE-A-PASSWORD' | sed 's/\$/\$\$/g')
printf 'OSRS_GE_USER_AGENT=osrs-ge-terminal/0.1 (contact: you@example.com)\nDASH_USER=admin\nDASH_HASH=%s\n' "$HASH" > .env
echo "SITE_ADDRESS=https://$(curl -s ifconfig.me)" >> .env   # the dashboard's public address
cat .env   # DASH_HASH should be $$2a$$14$$..., SITE_ADDRESS should show your IP
```

Prefer an editor? `cp .env.example .env` then `nano .env` — but then manually
double every `$` in the hash (`$` → `$$`).

---

## 5. Launch

```bash
docker compose up -d --build        # builds the image, starts api + collector + caddy
docker compose ps                   # all three should be "running"
```

Seed history once so the analytics aren't empty on day one (~15 days of hourly
data; takes ~15–20 min for all items):
```bash
docker compose run --rm collector python -m app.backfill --timestep 1h
```

---

## 6. Open it

Browse to **`https://YOUR_SERVER_IP`** → accept the one-time "not secure"
warning (that's the self-signed cert; traffic is still encrypted) → log in with
the user/password you set.

The collector now runs forever (`restart: unless-stopped`), surviving reboots,
building your intraday history continuously.

---

## Day-to-day

```bash
docker compose logs -f collector      # watch it collecting
docker compose logs -f api            # API logs
docker compose restart                # restart everything
docker compose down                   # stop (data persists in the `data` volume)
```

Update after code changes: `git pull` (or re-`scp`), then `docker compose up -d --build`.

The DuckDB database lives in the `data` Docker volume and survives rebuilds. To
start fresh, `docker compose down -v` (deletes the volume).

---

## Later: a real domain (trusted HTTPS, no browser warning)

1. Buy a domain (~$10/yr), add an **A record** pointing to `YOUR_SERVER_IP`.
2. In `.env`, change `SITE_ADDRESS=https://YOUR_IP` to `SITE_ADDRESS=your-domain.com`.
3. `docker compose up -d` — Caddy reads `SITE_ADDRESS` and auto-fetches a free,
   trusted Let's Encrypt certificate (no Caddyfile edit needed; a bare IP instead
   gets a self-signed cert automatically). `https://your-domain.com` is now
   green-lock clean, and a real domain is also less likely to be category-filtered
   at work than a bare IP.

---

## Notes & security

- Cost: ~€4–6/mo for the VPS; the OSRS Wiki API is free.
- The login + HTTPS keep the dashboard private. Use a strong password.
- The collector makes one polite request per 5 minutes — well within the wiki's
  usage norms (with your contact in the User-Agent).
- This serves analysis only; you still place every trade yourself in-game.
