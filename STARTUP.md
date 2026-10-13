# Starting up

Ordered. Each step verifies before the next.

## 1. Secrets

```bash
cd ~/WilloMotors
cp -n .env.example .env
nano .env
chmod 600 .env
```

| variable | where from | needed? |
|---|---|---|
| `TG_TOKEN` | @BotFather → `/newbot` | yes |
| `TG_CHAT` | message your bot, then `api.telegram.org/bot<TOKEN>/getUpdates` | yes |
| `GEMINI_API_KEY` | aistudio.google.com/apikey (free) | optional, much better parsing |

Load them into your shell for the manual steps below:

```bash
set -a; source .env; set +a
```

## 2. Check the plumbing

```bash
./.venv/bin/python run.py --source sample --mode broker
```

Four leads should land on your phone. If they do: database, scoring and
Telegram all work.

## 3. Check the market source

```bash
./.venv/bin/python run.py --source autoscout24 --probe
```

One request, three cars. Expect `HTTP 200` and real listings.

## 4. Build the price baseline

```bash
./.venv/bin/python run.py --source autoscout24 --index
```

~2400 cars, roughly 10 minutes at the default delay. You get a Telegram report
at the end. The first run will say `models with a usable baseline: 0` — that is
correct, it needs 5 private comparables per model. Watch that number climb over
the following nights.

## 5. Install the services

Check `User=` and `WorkingDirectory=` in the unit files match your box first.

```bash
sudo cp deploy/willomotors-listen.service /etc/systemd/system/
sudo cp deploy/willomotors-index.service deploy/willomotors-index.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now willomotors-listen willomotors-index.timer
```

Verify:

```bash
systemctl status willomotors-listen --no-pager
systemctl list-timers willomotors-index --no-pager
journalctl -u willomotors-listen -f
```

The bot should message you "Radar is listening."

## 6. Set up your sourcing

Nothing polls for deals — tutti blocks datacenter IPs and AutoScout24 is dealer
inventory. Deals come to you:

- **tutti**: on any search results page, click *Enregistrer la recherche* and
  turn on email alerts. Free, instant, sanctioned.
- **Facebook Marketplace**: saved searches with notifications, in the app.

When something looks interesting, paste the advert text to your bot. It parses,
stores, scores against the private market median and replies with what to ask.

## Daily use

| | |
|---|---|
| Paste an advert to the bot | get a scored verdict |
| `/show` | current settings |
| `/budget 5200` | change the ceiling |
| `/range 2200 5800` | change the hunt price window |
| `/stats` | what's in the database |
| Each morning | index report arrives |

## Before deploying any change

```bash
./.venv/bin/python -m unittest discover -s tests
./.venv/bin/python run.py --version
```

Both should pass before you restart a service.

## If something breaks

```bash
journalctl -u willomotors-listen -n 50 --no-pager
journalctl -u willomotors-index -n 50 --no-pager
./.venv/bin/python run.py --source autoscout24 --probe        # is the API up?
./.venv/bin/python run.py --source manual --mode buy --explain # why no alerts?
```
