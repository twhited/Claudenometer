# ChronoConnect

MCP server that gives Claude direct read/write access to your [Cronometer](https://cronometer.com) diary.  Say "log 2 scrambled eggs and a coffee" in Claude and the entries appear in Cronometer immediately.

## Tools exposed to Claude

| Tool | What it does |
|------|-------------|
| `search_food` | Search Cronometer's food database |
| `get_food_details` | Get serving size options for a specific food |
| `add_food_entry` | Log a food serving to the diary |
| `get_daily_nutrition` | Get macro totals (calories, protein, carbs, fat, fiber) |
| `get_food_log` | List all logged entries for a day |
| `refresh_connection` | Re-fetch the GWT hash and re-authenticate (survives Cronometer redeploys) |

---

## Quick start — local (stdio)

```bash
git clone https://github.com/twhited/ChronoConnect
cd ChronoConnect
pip install -e .
cp .env.example .env
# Edit .env — fill in CRONOMETER_EMAIL and CRONOMETER_PASSWORD
```

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "ChronoConnect": {
      "command": "chronoconnect",
      "env": {
        "CRONOMETER_EMAIL": "your@email.com",
        "CRONOMETER_PASSWORD": "yourpassword",
        "TRANSPORT": "stdio"
      }
    }
  }
}
```

Restart Claude Desktop.  You should see ChronoConnect listed under connected tools.

---

## Remote deployment (Docker)

Useful for always-on access from Claude on the web or mobile.

### 1. Set up the server

```bash
git clone https://github.com/twhited/ChronoConnect
cd ChronoConnect
cp .env.example .env
```

Edit `.env`:
```
CRONOMETER_EMAIL=your@email.com
CRONOMETER_PASSWORD=yourpassword
TRANSPORT=sse
PORT=8000
# Generate a strong key: python -c "import secrets; print(secrets.token_urlsafe(32))"
API_KEY=your-strong-random-key-here
```

```bash
docker compose up -d
```

### 2. Expose to the internet (pick one)

**Option A — Port forwarding:** Forward port 8000 on your router to the server.

**Option B — Cloudflare Tunnel (recommended, no open ports):**
```bash
# Install cloudflared, then:
cloudflared tunnel --url http://localhost:8000
# Copy the https://xxx.trycloudflare.com URL
```

### 3. Configure Claude

In your Claude config, add the remote SSE server:
```json
{
  "mcpServers": {
    "ChronoConnect": {
      "url": "https://your-domain-or-ip:8000/sse",
      "headers": {
        "Authorization": "Bearer your-strong-random-key-here"
      }
    }
  }
}
```

### NAS setup (Synology / QNAP)

Use **Container Manager** (Synology) or **Container Station** (QNAP):
1. Upload the repo to a shared folder
2. Create a new container from the local `Dockerfile`
3. Set environment variables from `.env` in the container settings
4. Map host port 8000 → container port 8000
5. Enable auto-restart

---

## How Cronometer's API works

Cronometer has no public API.  It uses **GWT-RPC** (Google Web Toolkit) — a binary-ish protocol where the client and server exchange pipe-delimited payloads over HTTPS.

ChronoConnect reverse-engineers this protocol.  The main fragility is a **permutation hash** baked into each Cronometer JS build.  When Cronometer redeploys their frontend, the hash changes.

ChronoConnect handles this automatically:
- **On every startup**, it fetches `cronometer.nocache.js` and extracts the current hash.
- **At any time**, you can say "refresh the Cronometer connection" and Claude will call `refresh_connection` without restarting the server.

---

## Security

- **Credentials** are stored only in your local `.env` file, which is `.gitignore`d and never committed.
- **The SSE endpoint** requires an `Authorization: Bearer <API_KEY>` header.  Without the key, every request returns 401.
- **Docker** reads credentials from `.env` at runtime; they are not baked into the image.
