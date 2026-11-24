# memebot data collector (paper trading research only)
- `collect-new-pools` runs every hour and records newly listed Solana pools to `data/pools_seen.csv`.
- `download-candles` runs once a day and downloads 1-minute candles for pools that are at least 24 hours old
  (and had at least $5,000 liquidity when first seen) into `data/candles/`. It resumes where it stopped.
Both use CoinGecko's keyless endpoint. Open the Actions tab to see runs; press "Run workflow" to start one by hand.
