# Contributing

Bug fixes, source repairs, accessibility improvements and clearer setup instructions are welcome.

1. Fork the repository and create a branch.
2. Run `python -m unittest discover -s tests -t . -v` with Python 3.11 or newer.
3. Keep tests offline using synthetic feeds and mocked APIs. Add a focused regression for a meaningful behavior change.
4. Describe the problem, changed behavior and validation in your pull request.

Keep the default server local and dependency-free. For a new feed, include its official discovery page, permitted hosts, sensible polling floor and any applicable use restrictions. Do not add scraped article bodies, user databases, captured publisher responses, credentials or personal settings.

Frontend text from feeds must use DOM text nodes; do not insert publisher-controlled HTML. Keep request logs free of credentials and query strings. Do not test delivery with somebody else's Telegram recipient.
