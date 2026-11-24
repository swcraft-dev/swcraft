# `differences/after/` — the three ARXML goldens as the compositions regeneration froze them

Every `.arxml` file here is **synthetic**: SWCraft's own writer output over the repository's own
models, with no content from any real export.

Exact copies of `basic-4.2.2.arxml`, `composite-4.2.2.arxml` and
`empty-4.2.2.arxml` as they stood after the compositions regeneration (contract
`4.0.0`) and before the base and implementation types regeneration (contract
`5.0.0`) re-froze the live goldens.

They exist for one reason. `packages/tools/tests/test_arxml_golden_differences.py`
asserts that the compositions difference — `../before/` against the goldens
that regeneration produced — equals the committed list in `../../differences.json`.
The live goldens changed again later on purpose (the writer stopped inventing a
`{name}_impl` implementation type beside each base type), so they carry two
regenerations' differences, and the compositions difference can only be
observed against the bytes the compositions regeneration left. Those bytes are
committed here, unchanged, so the compositions list stays exactly what it was.

Their digests are pinned by that test and equal the "After" digests the golden
README's compositions section records:

| File | SHA-256 | Bytes |
|---|---|---|
| `basic-4.2.2.arxml` | `0a2b711432c87657514c4cfca71022917216da439ba4bda609a3e828c07d623c` | 21 322 |
| `composite-4.2.2.arxml` | `5564f9866c202e50dfafeb6b10b51f18730e1d898f5cf47db9638bbdc4b8cb4a` | 20 690 |
| `empty-4.2.2.arxml` | `5ffcd165c2de3d0632d55e0cf4e307e785dda73a84e2ff8b6221b7f4565560ba` | 494 |

**They are never edited and never regenerated.** They are not goldens: nothing
generates them and no test compares a fresh run against them. They are a
historical record with one consumer.

`.gitignore:3` is `*.arxml` and its negations are non-recursive, so this
directory needs a negation of its own — without it `git add` skips these files
with no error at all. That is asserted, not trusted.
