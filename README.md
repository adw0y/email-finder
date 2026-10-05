# Email Finder

A local app that finds work emails for people at a company in a given role. It chains the free tiers of nine services: when one runs out of credits, the next takes over. Results download as CSV or Excel.

## Get it

On this page, click **Code**, then **Download ZIP**, and extract it. Or `git clone` the repo.

## Start

**Windows:** extract the zip first, then double-click `Start Email Finder (Windows).bat` (it may show as "Start Email Finder (Windows)" with type "Windows Batch File"). **Mac / Linux:** `./start-mac-linux.sh`

The first run installs everything into a local `.venv`, then the app opens in your browser at `http://localhost:8501`. Paste your API keys in the sidebar and press **Save keys**. They're written to a `.env` file on your computer and nowhere else.

## The services, in fallback order

| Step | Service | Free allowance | Sign up |
|---|---|---|---|
| Find people | Serper (Google results) | 2,500 queries, one time | serper.dev |
| | Tavily | 1,000 a month | tavily.com |
| | Brave Search | $5 credit a month, card on file | api-dashboard.search.brave.com |
| | DuckDuckGo | no key, can rate-limit | built in |
| Find emails | Prospeo | 75 a month, no charge on misses | prospeo.io |
| | Hunter | 50 credits a month | hunter.io |
| | Tomba (key + secret) | 50 searches a month | tomba.io |
| Verify | Reoon | 600 a month | emailverifier.reoon.com |
| | ZeroBounce | 100 a month, business-email signup | zerobounce.net |
| | Tomba | 100 a month | same account |
| | Hunter | 0.5 credit each, shared pool | same account |
| | Local SMTP check | unlimited, needs port 25 | built in |

You don't need all of them. Any single service per step works; more keys just mean more free volume.

## How it saves credits

Default mode (**Save credits**):

1. Searches LinkedIn profiles through a search API to get names. It never opens LinkedIn itself.
2. Reads the company's website and does one web search for published addresses, to learn the email format for free.
3. If the format is still unknown, spends **one** finder credit on the first person. The address that comes back reveals the format (say `first.last`).
4. Everyone else gets their address built from that format and confirmed by a verifier, which is far cheaper than a finder credit.
5. If a verifier rejects the guesses, it spends a finder credit on that person.

**Max accuracy** mode spends a finder credit on every person instead.

## When a service runs out

- **Out of credits** (the service says so, or the local count hits its free limit): switched off until the 1st of next month and the next one takes over. The sidebar shows a red dot and a reset button.
- **Rate-limited:** waits, retries once, then skips that service for the rest of the run. It doesn't count as out of credits.
- **Bad key:** skipped for the run, with a message in the log.

Usage is counted in `usage.json`. If you also use these services on their websites, the local count won't see that, but the service's own "out of credits" reply still triggers the switch. Caps can be changed in `.env` (see `.env.example`). Brave's is set to 900 on purpose, to stay under the $5 credit since it keeps a card on file.

## Output

The Excel file has three sheets. **Results** has one row per person, colour-coded by status. **Summary** shows counts and the credits used. **Run log** records every step. A copy of each run is also saved in `exports/`.

| Status | Meaning |
|---|---|
| Verified | The mail server or a finder confirmed it. Safe to send. |
| Found, unverified | A finder returned it but nothing could confirm it. |
| Catch-all domain | The company accepts mail for any address, so no one can confirm it. The format is still usually right. |
| Best guess | Built from the format with no confirmation. Check before sending. |

## Tests

`python -m pytest -q tests` runs 11 offline tests. Every API is faked, including each one running out of credits, so the fallback logic is proven without spending anything.

## Using it responsibly

Work addresses, B2B outreach, modest volumes. Identify yourself, say why you're writing to this person, and honour opt-outs. India's DPDP Act, GDPR and CAN-SPAM all apply to cold email in different ways.
