# The unknown-key oracle's own elements

`test_unknown_key_oracle.py` plants an undeclared key into every mapping of a corpus and holds
`rule:model.unknown-key` to the committed editor schemas. Its corpus is `examples/basic`, the 51.x
fixture trees and the editor schemas' own valid elements (`fixtures/editor_schema/valid`); this tree
holds one element of each kind none of those reaches:

- `OracleSwc`: an init event, an operation event, a write and a call, and a port's `measures`, a
  map the rule must stay silent in;
- `OracleComposition`: a composition port and a delegation;
- `OracleTemperature_C`: a conversion's limits and their bounds;
- `OracleSpeed_Linear`: a linear method's limits and their bounds.

Each file is loaded on its own through its layer's adapter; the tree is not a consistent model, and
no test runs `check-model` over it: the references name elements this tree does not hold.
