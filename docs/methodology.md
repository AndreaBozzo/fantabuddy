# Methodology and limits

## Identity before modeling

Fantacalcio and API-Football use different identifiers. Reconciliation can automatically
accept only strong matches with enough margin over the second candidate. Fuzzy matches
remain pending until reviewed; they are never promoted merely to improve coverage.

## Model gates

Season projections use walk-forward validation. A role model enters the report only when
it improves the configured baseline on held-out time. Start probability and expected
minutes use their own temporal validation. When a model misses its gate, Fantabuddy uses
the simpler baseline and reports that choice.

## Price allocation

Scores rank players; they are not auction prices. The allocator selects the expected
rosterable pool, measures distance from the replacement player by role, applies league
role budgets and caps, and reconciles the result to the exact total budget. Configuration
therefore describes the economics of a league rather than claiming a universal price.

In Classic, replacement depth and budget are calculated independently for P/D/C/A. In
Mantra, official multi-role eligibility is preserved while the market is split into the
two auction pools, POR and movement. This respects the absence of a fixed positional
roster cage, but deliberately does not optimize a recommended squad or guarantee coverage
for a chosen formation.

## What the report can and cannot say

- API-Football ratings are provider ratings, not official Fantacalcio votes.
- Injury and sidelined records are monitoring signals, not medical certainty.
- FVM and quotations are market references, not automatic spending limits.
- Missing data stays missing. Historical official pages without FVM are marked as such.
- Provider anomalies remain traceable to raw payloads; curation should add an explicit
  layer rather than silently rewrite the source.

The useful question is usually “what changed, and is the evidence reliable?” rather than
“which eleven should I field?”.
