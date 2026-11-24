# `differences/before/` — the three ARXML goldens before the compositions regeneration

Every `.arxml` file here is **synthetic**: SWCraft's own writer output over the repository's own
models, with no content from any real export.

Exact copies of `basic-4.2.2.arxml`, `composite-4.2.2.arxml` and
`empty-4.2.2.arxml` as they stood at `1458d92`, taken **before** the composition
emitters were changed and before anything was regenerated.

They exist for one reason: the goldens' change is *asserted*, not eyeballed — an
unmatched difference fails a test. A difference cannot be observed against a
baseline that exists only in the repository's history, so the baseline is
committed here and
`packages/tools/tests/test_arxml_golden_differences.py` computes the observed
difference between each file and the file of the same name in `../after/` —
the golden as the compositions regeneration froze it, committed there because a
later regeneration changed the live golden — asserting it equals the committed list
in `../../differences.json`.

Their digests are pinned by that test against the values measured before the change, so a
copy that was *regenerated* instead of copied fails rather than quietly turning
the comparison into a comparison of the change against itself:

| File | SHA-256 | Bytes |
|---|---|---|
| `basic-4.2.2.arxml` | `a6ef540773660d7a3169870114f94b616824258db24039487665d00bad5fd14c` | 21 364 |
| `composite-4.2.2.arxml` | `2b2fde13f71d4d946044b987d37407f3d401c62e26a859b86a311f295fbaec2c` | 20 732 |
| `empty-4.2.2.arxml` | `5ffcd165c2de3d0632d55e0cf4e307e785dda73a84e2ff8b6221b7f4565560ba` | 494 |

**They are never edited and never regenerated.** They are not goldens: nothing
generates them and no test compares a fresh run against them. They are a
historical record with one consumer.

`.gitignore:3` is `*.arxml` and its negations are non-recursive, so this
directory needs a negation of its own (`.gitignore:15`) — without it `git add`
skips these files with no error at all. That is asserted, not trusted.
