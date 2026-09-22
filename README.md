# Onchain Alert Bot

Async **Python** crypto market alert service with **11 strategies**, Telegram delivery, SQLite persistence, and a production-oriented **DevOps** packaging layer (Docker, Compose, Kubernetes, CI, metrics).

---

## Architecture

```text
┌─────────────────┐     ┌──────────────────────────────────────┐
│  Telegram User  │◄────│  python-telegram-bot (commands)      │
└─────────────────┘     └──────────────────┬───────────────────┘
                                           │
┌─────────────────┐     ┌──────────────────▼───────────────────┐
│  /healthz       │◄────│  main.py  — strategy loops + health  │
│  /readyz        │     │                                      │
│  /metrics       │     └──────────────────┬───────────────────┘
└─────────────────┘                        │
          ┌────────────────────────────────┼────────────────────┐
          ▼                                ▼                    ▼
   ┌─────────────┐                 ┌──────────────┐     ┌────────────┐
   │  providers/ │  public RPCs    │  strategies/ │     │ analyzers/ │
   │  CEX·EVM·SOL│  + DEX APIs     │  11 scanners │     │ risk/wallet│
   └─────────────┘                 └──────┬───────┘     └────────────┘
                                          │
                                   ┌──────▼───────┐
                                   │ SQLite + TG  │
                                   │ alerts.db    │
                                   └──────────────┘
```

**Data sources (all free / public):** Binance · Bybit · MEXC (ccxt), Ethereum / BSC / Arbitrum / Solana RPCs, DEXScreener, GeckoTerminal, GoPlus, Honeypot.is, DefiLlama, Forta.

---

## Quick start

### 1. Configure secrets

```bash
cp .env.example .env
# set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

### 2. Run with Docker Compose (recommended)

```bash
docker compose up -d --build
curl http://localhost:8080/healthz
curl http://localhost:8080/metrics
```

Optional Prometheus:

```bash
docker compose --profile observability up -d
# Prometheus UI → http://localhost:9090
```

### 3. Or run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

---

## Kubernetes

```bash
# create secret (do not commit real values)
kubectl create secret generic onchain-secrets \
  --from-literal=telegram-bot-token='YOUR_TOKEN' \
  --from-literal=telegram-chat-id='YOUR_CHAT_ID'

# edit image name in deploy/k8s/deployment.yaml, then:
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
```

Manifests include non-root securityContext, liveness/readiness probes, resource requests/limits, and Prometheus scrape annotations.

---

## CI pipeline

On every push/PR to `main`:

1. **Lint** — Ruff  
2. **Test** — Pytest (health endpoints, alert formatting)  
3. **Build** — Docker Buildx with GHA cache  
4. **Scan** — Trivy (fails on HIGH/CRITICAL)

See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

---

## Telegram commands

| Command | Description |
|---|---|
| `/start` | Strategy overview |
| `/status` | Runtime status |
| `/watchlist` | Monitored symbols |
| `/risk <chain> <address>` | Token risk score |
| `/wallet <chain> <address>` | Wallet activity profile |

---

## Project layout

```text
├── main.py                 # Entrypoint, Telegram + strategy loops
├── health.py               # /healthz /readyz /metrics
├── config.py               # Env + thresholds
├── Dockerfile              # Multi-stage, non-root
├── docker-compose.yml      # Bot (+ optional Prometheus)
├── deploy/
│   ├── k8s/                # Deployment, Service, Secret example
│   └── prometheus/         # Scrape config
├── strategies/             # Market scanners
├── providers/              # CEX / EVM / Solana clients
├── analyzers/              # Risk, wallet, patterns
├── tests/                  # Pytest
└── .github/workflows/      # CI + Dependabot
```

---

## Makefile

```bash
make install-dev   # deps
make lint          # ruff
make test          # pytest
make docker-up     # compose
make compose-obs   # compose + prometheus
```

---

## Security notes

- `.env` and runtime DB are gitignored — never commit tokens.
- If a Telegram bot token was ever shared, **revoke it in BotFather** and create a new one before going public.
- See [SECURITY.md](SECURITY.md).

---

## License

MIT — see [LICENSE](LICENSE).
