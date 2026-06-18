# GE Terminal Export — RuneLite plugin

Streams your **live Grand Exchange offers** (buy/sell, fill progress, cancels) from
RuneLite to your GE Terminal. Completed fills auto-log as trades and feed your
Portfolio round-trips — **no manual entry**. Open orders show up live on the **Orders**
tab with fill %, real fill prices, and timelines.

## ⚠️ Read-only by design
This plugin **only observes and reports** your offers. It never places, edits, or
cancels offers — automating GE actions is against Jagex's rules and risks a ban.
Run RuneLite on your **personal** machine.

## How it works
It subscribes to RuneLite's `GrandExchangeOfferChanged` event and POSTs each update to
your server's `POST /api/ge-offers` (authenticated with a bearer token). The server
upserts the order and, when an offer finishes with a real fill, creates one trade
(buys at the real average fill price `spent/sold`; sells at the gross offer price, with
the 2% tax applied by the server).

## Requirements
- **JDK 11** (RuneLite targets Java 11) — e.g. Eclipse Temurin 11. This is the ONLY
  tool you need: a Gradle wrapper is committed, so you do **not** install Gradle.
- **Git** to clone the repo (or download the zip).
- Your server's ingest token (`OSRS_GE_INGEST_TOKEN`) from the VPS `.env`.

## Run it (developer mode — the simplest path for personal use)
```bash
cd runelite-plugin
./gradlew run          # macOS / Linux
.\gradlew run          # Windows (PowerShell)
```
The wrapper fetches the right Gradle automatically, then launches RuneLite with the
plugin loaded.
Then in the RuneLite client:
1. Open **Configuration** (wrench) → find **GE Terminal Export**.
2. Set **API URL** = `https://ge.mapletree-ge.com` and **API key** = your ingest token.
3. Make sure **Send orders** is on.
4. Log in and place/collect GE offers as normal.
5. Watch the **Orders** tab in the web app — offers appear within a second or two, and
   filled ones flow into **Portfolio**.

> `./gradlew run` (`.\gradlew run` on Windows) is the canonical RuneLite plugin dev-run.
> For a permanent in-client install you'd publish to the RuneLite **Plugin Hub** (public
> review) — not needed for personal use.

## Notes
- Each offer gets a stable id per GE slot, persisted across sessions, so reconnecting
  or a completed-but-uncollected offer never double-counts.
- If nothing shows up: check the API key matches the server, the URL has no trailing
  slash, and that `OSRS_GE_INGEST_TOKEN` is set on the server (the endpoint returns 503
  if it isn't, 401 on a bad token).
- The plugin posts asynchronously and silently — it never blocks or interrupts the game.
