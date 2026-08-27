# Gold set

Hand-labeled records that define "correct". Same schema as `catalog/records/*.json`;
only the fields you actually label need to be present.

Target ~30-40, stratified:

- every tier-1 phenomenon that appears in your corpus
- at least one **review/compilation** paper (must triage to `pointers`, not a record)
- at least one **mixed-mode** study (e.g. flow boiling that also reports CHF)
- at least one **microgravity** or otherwise off-nominal-gravity study
- at least one **unusual fluid** (cryogen, liquid metal, nanofluid)
- at least one paper that states **no application** (gold = `fundamental`; this is
  the case the application pass most often gets wrong by inventing one)
- at least one paper whose envelope appears **only in figures** (gold numerics null;
  tests that the model leaves them null rather than estimating from captions)

Workflow: run `--rules` first, open the record, correct it by hand, move it here.
Correcting a draft is much faster than labeling from scratch, and it surfaces
taxonomy gaps early.

Then: `python eval/run.py --tag <prompt-version>` before and after any taxonomy or
prompt change. No amendment lands without a scorecard.
