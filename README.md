# TechBrief News

A self-hosted technology-news collector with company filters, Telegram alerts and a local dashboard. Runs with **Python 3.11+ and no third-party Python packages**.

Follow company announcements, technology publishers and regulator feeds in one searchable archive. New stories matching your company watchlist can be sent to your own Telegram bot.

## Start

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/).
2. Download this repository's ZIP using **Code → Download ZIP**, then extract it. Alternatively, clone it with Git.
3. Open a terminal in the extracted folder and run:

```sh
python app.py --open
```

On Windows, you can double-click **Start.cmd**, or use `py -3 app.py --open`. On systems that name Python `python3`, use `python3 app.py --open`.

Open [localhost:8765](http://localhost:8765). Leave the terminal and computer running; **Ctrl+C** stops collection. Closing the browser leaves the collector running while the terminal process remains open. If the port is occupied, choose another: `python app.py --port 8877 --open`.

Run only one collector process per project folder, including when choosing another port. Two processes sharing `data/` can send duplicate alerts.

No account or API key is needed to collect news. Telegram is optional and uses a bot you create. Software usage has no subscription fee; hosting and connectivity are your responsibility.

## Connect Telegram

1. Open [@BotFather](https://t.me/BotFather), send `/newbot`, and follow the instructions to create your own bot.
2. In TechBrief, open **Telegram**. Copy the displayed `/start …` pairing command and send it privately to your new bot.
3. Paste the bot token from BotFather and click **Connect**. Leave Chat ID blank for private pairing, or supply an explicit numeric chat ID you control.
4. Click **Send test** and check that the message arrives. Enable notifications to receive later matching headlines.

Never share or commit your bot token. A bot already used with a webhook needs an explicit chat ID or a separate bot; TechBrief does not remove another service's webhook.

For unattended use, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment before starting. Explicit environment credentials override saved credentials. `.env.example` documents the names; the app does **not** load `.env` files automatically. Do not place real secrets in version control.

## What it does

- RSS and Atom collection from curated company, publisher and regulator sources.
- Company-name and ticker association, watchlist filtering, search and CSV export.
- URL deduplication, original links and separate publication/collection timestamps.
- A persistent Telegram queue with throttling and retries, plus delivery health.
- Publisher cache/TTL handling, conditional requests and failure backoff.
- Local source-response timing records and visible feed errors.

The initial fetch of each source seeds the archive **without sending a backlog**. Later alerts require a watchlist match and a publication timestamp within 24 hours. Different publishers covering the same event remain separate stories. Tags can miss indirect references or include ambiguous mentions.

The global checking interval defaults to five minutes. Each source may refresh less often because of its own cache hints or configured floor; CNBC has a 60-minute floor and Microsoft has a 10-minute floor. There is no real-time or fastest-source guarantee. Computer downtime and finite feed inventories can leave collection gaps. Network failures do not erase saved stories. An uncertain Telegram response can occasionally cause a duplicate message.

## Sources and content rights

The catalog contains official company feeds, established technology publishers and US regulators. BBC and FTC are disabled by default; FTC's advertised feed returned HTTP 404 in the release check. You choose enabled feeds under **Settings**. Details and original discovery links are in [Sources](docs/SOURCES.md).

The MIT license covers this program's code. Publishers retain rights to their headlines, excerpts and articles; feed access is not a blanket redistribution license. This repository ships **no article database, news archive or feed snapshots**. Review each publisher's applicable terms for your use, preserve attribution and link to the original. This project is not endorsed by the listed publishers or Telegram.

## Local storage and privacy

The app listens only on `127.0.0.1`. It is designed for your own computer, without a public login system. Keep it on loopback rather than exposing it through port forwarding or a reverse proxy.

Runtime files are created in `data/`, which Git ignores. Windows saves credentials with DPAPI encryption tied to your Windows account. Linux/macOS save them as **plaintext JSON with owner-only permissions** (`0600` in a `0700` data directory); use environment variables if you prefer not to persist a token. Local administrators and malicious software running as your user remain outside this protection. See [Security](SECURITY.md).

Export CSV downloads at most 10,000 filtered rows. To preserve the entire archive, stop the collector and copy the `data/` folder to a private location. Treat this copy as sensitive: it includes credentials and recipient-specific delivery state. Restoring a delivery snapshot may cause messages sent after that snapshot to become eligible again; review notification settings before restarting.

## Development

```sh
python -m unittest discover -s tests -t . -v
```

Tests use synthetic feeds and mocked Telegram calls. They do not need real tokens, fetch live feeds or send messages. GitHub Actions is configured for Windows and Linux with Python 3.11 and 3.13. See [Contributing](CONTRIBUTING.md).

## Similar projects

RSS-to-Telegram is an established idea. [RSStT](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) has richer multi-user Telegram reading features. [Huginn](https://github.com/huginn/huginn) is a general monitoring and automation platform. [RSSHub](https://github.com/DIYgod/RSSHub) generates feeds for many sites and needs a separate sender. TechBrief focuses on a simple local tech/company-news desk. See the [comparison](docs/ALTERNATIVES.md).

## License

[MIT](LICENSE). Publisher content and third-party services have separate terms.
