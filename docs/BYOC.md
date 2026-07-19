# BYOC — Bring Your Own Cloud

Rosa is macOS-first (iMessage bridge, Full Disk Access), but you can
also run her on any Linux VM that has Docker and expose her via Slack.
This document walks through three popular hosters.

**Trade-offs vs the macOS install:**

- ✓ No Mac needed, no Full Disk Access dance
- ✓ 24/7 without a running Mac
- ✓ Data still under YOUR control (your VM, your disk)
- ✗ iMessage bridge is unavailable — set `main_channel: slack`
- ✗ You pay for the VM (~€5–15/mo) instead of using your Mac's spare cycles

Rosa never phones home. The privacy story is identical whether she runs
on a Mac or a self-hosted VM.

---

## Prerequisites (all platforms)

1. **Docker + Compose v2** on the target VM.
2. **A Slack workspace** with an app you control (see the wizard's
   Slack step for setup — it takes ~5 min).
3. **An Anthropic API key** ($5–15/mo usage).
4. **Optional but recommended: Tailscale** to reach the wizard without
   exposing port 8765 to the internet.

---

## Recipe 1 — fly.io (~€3/mo, 5 min setup)

Fly gives you a free-tier VM that's just enough for Rosa if you set the
Ollama models to `phi3:mini` (2GB RAM is tight for Llama-3.1-8B).

```bash
# 1. Install fly CLI
brew install flyctl                      # macOS
# or: curl -L https://fly.io/install.sh | sh

# 2. Login
fly auth login

# 3. Clone Rosa
git clone https://github.com/hendrikgeerts/meet-rosa.git ~/rosa-fly
cd ~/rosa-fly

# 4. Launch (creates fly.toml)
fly launch --no-deploy --name rosa-yourname --region ams

# 5. Set up a persistent volume
fly volumes create rosa_data --size 10 --region ams

# 6. Add volume + env to fly.toml
cat >> fly.toml <<EOF
[mounts]
  source = "rosa_data"
  destination = "/data"

[env]
  ROSA_HOME = "/data"
  OLLAMA_HOST = "http://ollama.internal:11434"   # deploy Ollama as separate app
EOF

# 7. Deploy
fly deploy

# 8. Reach the wizard via fly proxy (loopback tunnel)
fly proxy 8765:8765 -a rosa-yourname
# Now open http://localhost:8765/ in your browser locally.
```

Ollama on fly is a separate app; see
[fly-apps/ollama-open-webui](https://github.com/fly-apps/ollama-open-webui)
for a battle-tested compose. For MVP, just install Ollama on the same
container by extending the Dockerfile.

---

## Recipe 2 — DigitalOcean droplet (~€6/mo, 10 min setup)

For serious use, a droplet with 4GB RAM comfortably runs Llama-3.1-8B.

```bash
# On your Mac:
doctl compute droplet create rosa-server \
  --size s-2vcpu-4gb \
  --image docker-20-04 \
  --region ams3 \
  --ssh-keys $(doctl compute ssh-key list --format ID --no-header)

# SSH in
ssh root@<droplet-ip>

# Clone + start
git clone https://github.com/hendrikgeerts/meet-rosa.git /opt/rosa
cd /opt/rosa
docker compose up -d

# Pull Ollama models (one-time, ~5GB)
docker exec rosa-ollama ollama pull llama3.1:8b-instruct-q4_K_M
docker exec rosa-ollama ollama pull phi3:mini
docker exec rosa-ollama ollama pull nomic-embed-text

# Wizard: DO NOT expose port 8765 publicly. SSH tunnel from your Mac:
# (in a new terminal on your Mac)
ssh -L 8765:localhost:8765 root@<droplet-ip>

# Then browse http://localhost:8765/ on your Mac.
```

**Firewall reminder:** port 8765 should stay bound to loopback. The
docker-compose.yml already publishes it as `127.0.0.1:8765` — do NOT
change that to `0.0.0.0`.

---

## Recipe 3 — Hetzner Cloud (~€4/mo, 10 min setup)

Cheapest option in Europe with 4GB RAM.

```bash
# Same as DigitalOcean, but with hcloud:
hcloud server create --type cpx21 --image docker-ce --location fsn1 \
  --ssh-key <your-key-id> --name rosa-server

# Rest identical:
ssh root@<server-ip>
git clone https://github.com/hendrikgeerts/meet-rosa.git /opt/rosa
cd /opt/rosa
docker compose up -d
# Pull Ollama models…
# SSH tunnel to wizard…
```

**Hetzner note:** their `cpx11` (2GB RAM) works only with `phi3:mini`.
`cpx21` (4GB RAM) is comfortable for the default Llama-3.1-8B.

---

## After the wizard

Once the wizard's confirm step is finished, Rosa is running. Because
this is a headless install:

- **`main_channel`** is set to `slack` (recommended for BYOC — see wizard).
- **iMessage step**: skip it. You won't get iMessages on a Linux VM.
- **Google OAuth**: complete it if you want Gmail + Calendar
  (recommended). The callback URL that the wizard shows is
  `http://127.0.0.1:8765/oauth/google/callback` — this is why the SSH
  tunnel is essential: Google's redirect happens *in your Mac's
  browser*, which is talking to your local tunnel that forwards to
  the VM.

## Upgrading a BYOC install

```bash
ssh root@<server>
cd /opt/rosa
git pull
docker compose build && docker compose up -d
```

`rosa update` (from inside the container) also works but doesn't
rebuild the image; use compose from the host for image-level updates.

## Backup

Snapshot the `/data` volume from within the container:

```bash
docker exec rosa rosa backup --out /data/rosa-backup.tar.gz
docker cp rosa:/data/rosa-backup.tar.gz ./
```

Or use your hoster's block-storage snapshot feature on the `rosa-home`
volume.

## Uninstall

```bash
docker compose down -v          # stops + removes volumes
```

Wipes everything. `rm -rf /opt/rosa` removes the code too.
