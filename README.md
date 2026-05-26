# 📈 Trading Bot

A modular, production-grade algorithmic trading bot written in Python 3.10+.  
Supports Alpaca Markets (equities) and CCXT-compatible crypto exchanges out of the box.

---

## Project Structure

```
trading_bot/
├── config/
│   └── settings.yaml          # Non-sensitive configuration (committed)
├── src/
│   ├── main.py                # Bot entrypoint
│   ├── utils/
│   │   ├── helpers.py         # Env loading, settings, retry, numeric utils
│   │   └── logger.py          # Loguru setup (daily rotation, 30-day retention)
│   ├── data/
│   │   └── feed.py            # Market data abstraction
│   ├── strategies/
│   │   ├── base.py            # Abstract base strategy
│   │   └── momentum.py        # Momentum strategy implementation
│   ├── execution/
│   │   └── broker.py          # Order routing / broker adapter
│   └── risk/
│       └── manager.py         # Position sizing, stop-loss, circuit-breakers
├── tests/                     # pytest test suite
├── logs/                      # Auto-created; daily-rotated, compressed logs
├── data/                      # SQLite database and local data files
├── scripts/                   # One-off helper scripts
├── notebooks/                 # Jupyter notebooks for research
├── .env.example               # Secret variable template
├── .env                       # ← you create this (git-ignored)
├── .gitignore
└── requirements.txt
```

---

## Prerequisites (macOS)

| Tool | Minimum version | Install |
|------|----------------|---------|
| Python | 3.10 | `brew install python@3.12` |
| pip | 23+ | bundled with Python |
| Git | any | `brew install git` |
| Homebrew | any | [brew.sh](https://brew.sh) |

> **Apple Silicon (M1/M2/M3):** All dependencies support `arm64` natively.  
> If you hit a build error for a C extension, run `arch -x86_64 pip install <pkg>` as a fallback.

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/your-org/trading-bot.git
cd trading-bot
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

> Your prompt should now show `(.venv)`.  
> To deactivate at any time: `deactivate`

### 3. Upgrade pip and install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` in your editor and fill in your API keys:

```bash
# macOS default editor
open -e .env

# Or with VS Code
code .env
```

At minimum you need:
```
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
```

### 5. Review `config/settings.yaml`

The defaults use **paper trading** (`dry_run: true`, `broker.paper_trading: true`) — no real money is at risk until you explicitly change those flags.

Adjust `universe.symbols`, `strategy.active`, and `risk.*` thresholds to match your strategy.

### 6. Run the bot

```bash
python -m src.main
```

Logs are written to `logs/trading-bot.log` and also streamed to stdout.

---

## Running Tests

```bash
pytest tests/ -v --cov=src --cov-report=term-missing
```

---

## Development Workflow

### Pre-commit hooks (optional but recommended)

```bash
pre-commit install
```

This runs `black`, `isort`, `flake8`, and `mypy` on every `git commit`.

### Format & lint manually

```bash
black src/ tests/
isort src/ tests/
flake8 src/ tests/
mypy src/
```

### Jupyter notebooks

```bash
jupyter notebook notebooks/
```

---

## Logging

| File | Content |
|------|---------|
| `logs/trading-bot.log` | All log levels (rotated daily, compressed, kept 30 days) |
| `logs/trading-bot.error.log` | ERROR and above only |

Change `logging.level` in `settings.yaml` to `DEBUG` for verbose output during development.

---

## Key Configuration Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `app.dry_run` | `true` | Simulate orders without hitting the broker |
| `broker.paper_trading` | `true` | Use Alpaca paper endpoint |
| `portfolio.initial_capital` | `100000` | Starting capital in USD |
| `risk.max_position_size_pct` | `0.05` | Max 5 % of capital per position |
| `risk.stop_loss_pct` | `0.02` | Default 2 % stop-loss |
| `risk.max_daily_loss_pct` | `0.05` | Halt trading at 5 % daily loss |
| `strategy.active` | `momentum` | Strategy class to load |

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `ALPACA_API_KEY` | Yes | Alpaca API key |
| `ALPACA_SECRET_KEY` | Yes | Alpaca secret key |
| `APP_ENV` | No | `development` / `production` |
| `SLACK_BOT_TOKEN` | No | Slack notifications |
| `DB_HOST` / `DB_NAME` | No | PostgreSQL (SQLite used by default) |

---

## Safety Checklist

Before switching to live trading:

- [ ] `app.dry_run` set to `false` in `settings.yaml`
- [ ] `broker.paper_trading` set to `false`
- [ ] Live Alpaca API keys in `.env` (not paper keys)
- [ ] Risk limits reviewed and appropriate for your capital
- [ ] Back-tested strategy on at least 1 year of historical data
- [ ] Tested all notification / alerting paths
- [ ] Monitoring / uptime alerting configured

---

## License

MIT — see `LICENSE` for details.
