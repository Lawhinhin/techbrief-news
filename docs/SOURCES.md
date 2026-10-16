# Source catalog

Sources are configured in `sources.json`. Endpoints can change; the dashboard reports individual failures. The program reads feeds and links to the originals rather than scraping full article pages.

| Source | Official discovery or homepage |
|---|---|
| CNBC Technology | [RSS directory](https://www.cnbc.com/rss-feeds/) |
| BBC Technology, disabled by default | [Feed directory](https://support.bbc.co.uk/platform/feeds/NewsFeeds.htm) |
| Apple Newsroom | [Newsroom](https://www.apple.com/newsroom/) |
| NVIDIA Newsroom | [RSS directory](https://nvidianews.nvidia.com/rss) |
| Microsoft Official Blog | [Official blog](https://blogs.microsoft.com/) |
| Google Official Blog | [The Keyword](https://blog.google/) |
| FTC Press Releases, disabled by default | [RSS directory](https://www.ftc.gov/stay-connected/rss) |
| SEC Press Releases | [RSS directory](https://www.sec.gov/files/about/secrss.shtml) |
| AMD Investor Relations | [Press releases](https://ir.amd.com/news-events/press-releases) |
| Intel Investor Relations | [Email alerts](https://www.intc.com/news-events/email-alerts) |
| Meta Investor Relations | [Email alerts and RSS](https://investor.atmeta.com/resources/email-alerts-and-rss/default.aspx) |
| Oracle Investor Relations | [RSS directory](https://www.oracle.com/corporate/investor-relations/rss/) |
| AWS News Blog | [News blog](https://aws.amazon.com/blogs/aws/) |
| Meta Newsroom | [Newsroom](https://about.fb.com/news/) |

Company associations use explicit source ownership or recognizable names/tickers in feed text. They are relevance hints and can be imperfect. Regulator press releases are agency announcements; SEC press releases are not company EDGAR filings.

On 16 October 2026, a fresh-install check fetched 12 feeds successfully. FTC's advertised endpoint returned HTTP 404, so it is disabled until verified working. BBC is also opt-in. These observations describe that check, not a guarantee of future availability.

Publisher names and original links remain visible. News content keeps its own rights and terms; this project's MIT license grants no rights to redistribute publisher articles or sell an archive. The defaults target personal local reading. Check the relevant terms before a different use.

The scheduler honors feed TTL, remaining HTTP cache lifetime, configured source floors, Retry-After and failure backoff. Five-minute checking does not promise five-minute publication latency. A newly started feed seeds its existing items without Telegram backlog. The app cannot retrieve every story missed while it was off.
