# `type:gender`

The audience a product is meant for, as one of four codes: `male`, `female`, `unisex`, `kids`.

| File | Language | Source | Content |
|---|---|---|---|
| `en.json` | `en` | schema.org 30.1 `suggestedGender` ("for example male, female, or unisex"; `GenderType` members `Male`, `Female`), in the vocabulary file `backend/data/manifest.json` pins; Google Merchant Center product data specification, `gender` (`male`, `female`, `unisex`, Merchant Center Help article 6324479) and `age_group` (`kids`, 5-13 years, article 6324463), checked 2026-10-28 | The four codes, each its own one phrase, nothing else. |

The codes are the standards' values, which Merchant Center takes in English whatever the feed's language, so the enum
normalizer reads a code in any page language (`vocab.code_entries`). Merchant Center's other age groups (`newborn`,
`infant`, `toddler`, `adult`) name other ages than `kids`, so they state no code. A page word the sources do not list
("Men's", "Women") states nothing, and the field stays unpublished (owner decision 2026-10-28).

Any other phrase or language is added only under the C10 acceptance rule: an e2e improvement
with no precision drop, shown in the PR's evaluation report. Record each addition here, with its
justification and the PR.
