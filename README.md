# Leadership Media Monitor - Sprinklr refresh

This folder turns Sprinklr widget payloads into the dashboard's `data.json`.
Each dashboard section is fed by one widget. A section shows **Live** on the
dashboard once its widget returns data; the others stay **Sample**.

## One-time set-up (on the computer that owns the Sprinklr token)

1. Python 3.9 or later, plus `pip install pycountry` (used to fetch the full
   country list, past Sprinklr's 100-row limit per call).
2. Copy `.env.example` to `.env` and fill in the client ID, the secret and the
   newest refresh token. Never share `.env` or `.tokens.json`.
3. Run `python sprinklr_refresh.py token`. The new refresh token is saved to
   `.tokens.json` straight away. From now on only this script refreshes with
   this API key: stop any other script (for example the VS Code loop) that
   refreshes with the same key, or it will break this one.
4. Run `python sprinklr_refresh.py discover`. It lists the Sprinklr topic IDs
   returned under the LMN topic tag, with their names. Paste each leader's
   topic ID into `leaders.json`.
5. Run `python sprinklr_refresh.py build --dry-run` and check the totals it
   prints against the Sprinklr dashboard.

## Daily

`python sprinklr_refresh.py build` writes `../data.json`. Publishing that file
to the shared artifact updates the dashboard; the page itself does not change.
If someone else holds the token, run with `--access-token <token>`: nothing
rotates.

## Adding a widget

Paste the widget's API v2 payload into the chat with the section name, or save
it as `widgets/<file>` from `registry.json` and set its status to `received`.

| Widget file | Dashboard section | What the widget needs |
|---|---|---|
| topic_totals.json | Leader cards, share of voice | Topic; Mentions, Reach, EMV - received |
| topic_share.json | Cross-check only | Topic; Mentions - received |
| daily_topic.json | Coverage over time, card sparklines | Date (daily) and Topic; Mentions, Reach, EMV |
| sentiment_topic.json | Sentiment split; negative sentiment share on the leader cards | Topic and Sentiment; Mentions |
| country_topic.json | Market exposure | Topic and Country; Mentions, EMV |
| language_topic.json | Languages | Topic and Language; Mentions |
| mediatype_topic.json | Media value by channel | Topic and Media type, no News/Blogs filter; Mentions, EMV |
| publication_topic.json | Top publications | Topic and Publication (custom field) or Domain; Mentions, Reach |
| theme_topic.json | Themes | Topic and Theme tag (custom); Mentions |
| clippings.json | Latest clippings | Mentions stream or table: Topic, Title, Permalink, Date, Source, Country, Language, Sentiment, Reach, EMV, image URL |

## What the script checks before it writes

- All three leaders return data for every period.
- Share-of-voice widget and totals widget agree within 2% for 30 days.
- 30-day mentions per leader are printed for a manual check.
If a check fails, `data.json` is not written.

## Notes

- Time zone: Asia/Dubai. Periods end yesterday (complete days only).
- EMV is converted from USD to AED at 3.6725. Change `emv_currency` in
  `config.json` if the Sprinklr account reports EMV in AED.
- Raw responses are kept in `raw/` for audit.
- Rate limit is about 1,000 calls an hour; a full build uses about 70.

## Market counts

`breakdowns.<period>.markets` holds `names`, `share` and `counts`. `counts` has
raw mention counts per leader from the "Coverage by country, all sources"
widget, one per name in the same order, for every country the widget returns.
Sprinklr returns at most 100 countries per call and ignores `page`, so the
script asks again excluding the countries already received (by ISO code) until
a call returns fewer than 100. Country-unknown mentions stay excluded by the
widget. "Other" is the last name and is 0 when every country is listed.
