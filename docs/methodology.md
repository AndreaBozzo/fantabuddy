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

## Availability and declared absences

The official Fantacalcio desk (`/indisponibili-serie-a`) is the primary availability
source, because it stays free and is updated in season. API injury and sidelined records
remain as a secondary signal where the paid plan has covered them.

A notice reduces the recommended price in proportion to the share of the remaining
season it covers, not by a flat penalty. The remaining matchdays come from the fixture
calendar, and the estimated return date comes from the editorial text: `metà ottobre`
resolves to the 15th, `inizio` to the 5th, `fine` to the 28th, and so on. A player out
for two of thirty-four remaining rounds loses a few per cent; a player out until January
loses close to half. A floor keeps a long absence from zeroing a player who still returns
within the season.

These conventions of reading are declared, not communicated by the clubs. Where the text
names no month, no return date is derived: the player keeps the conservative flat
treatment and the report marks the return as still to be verified. A suspension with no
stated duration costs one matchday rather than an open-ended absence.

## What the report can and cannot say

- API-Football ratings are provider ratings, not official Fantacalcio votes.
- Injury and sidelined records are monitoring signals, not medical certainty.
- Return windows are editorial estimates. The derived date is a reading convention
  applied to that estimate, and inherits its uncertainty.
- A league rule that allows replacing a long-term injury is not modeled: the price
  reflects lost matchdays, not the administrative remedy.
- FVM and quotations are market references, not automatic spending limits.
- Missing data stays missing. Historical official pages without FVM are marked as such.
- Provider anomalies remain traceable to raw payloads; curation should add an explicit
  layer rather than silently rewrite the source.

The useful question is usually “what changed, and is the evidence reliable?” rather than
“which eleven should I field?”.
