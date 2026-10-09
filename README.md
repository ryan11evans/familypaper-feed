# familypaper-feed

The shared, kid-friendly daily content for the **Family Paper: Daily Printable** iOS app: three short news stories, a fun fact, a joke, and a riddle.

- Feed URL: https://ryan11evans.github.io/familypaper-feed/feed.json
- Updated once each morning (Pacific time). Every family gets the same file; it contains no personal or family information.
- The app reads it with a plain HTTPS GET and sends nothing about its users.

## Schema (v1)
```json
{
  "schema_version": 1,
  "date": "YYYY-MM-DD",
  "news": [
    { "emoji": "🚀", "tag": "Space", "headline": "…", "body": "2–3 short sentences",
      "source_name": "NASA", "url": "https://…" }
  ],
  "fun_fact": "…",
  "joke":   { "q": "…", "a": "…" },
  "riddle": { "q": "…", "a": "…" }
}
```
`news` has 1–3 items; `url` is optional. Unknown keys are ignored by the app; `schema_version` changes only for breaking changes.

Stories are rewritten for young readers from reputable outlets (AP, NASA, MLB.com, …) and checked to be kid-safe: no politics, crime, war, disasters, or scary stories.
