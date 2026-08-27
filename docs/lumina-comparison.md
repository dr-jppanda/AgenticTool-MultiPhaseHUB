# What Lumina does differently, and what we took from it

[`XiaoyuChenUofL/lumina`](https://github.com/XiaoyuChenUofL/lumina) — a local-first
PDF reader + AI assistant (FastAPI + SQLite/FTS5 + React/Vite/Tailwind, PDFium
WASM viewer). Cloned to `../lumina`. Its docs are in Chinese; quotes below are
translated.

Different product from MHT-DataHub — it is a *reading* tool with a library
attached; ours is a *catalog* with no reader. But it solves two of our problems
independently, and got several details right that we had wrong.

---

## 1. Bugs it found in our code

Three, all confirmed by measurement, all now fixed.

### `--allowedTools ""` does not disable tools

Their pitfalls table: *"Claude Code `--allowedTools` doesn't restrict tools — it
only controls whether permission is asked. To actually disable them use
`--tools`."*

The CLI's own help confirms it: `--tools <tools...>  Specify the list of
available tools… Use "" to disable all tools`. Our backend passed only
`--allowedTools ""`, so the model kept the full toolset and merely got **denied**
on each attempt — and every denial burns an agentic turn.

Measured on one extraction prompt:

| | turns | permission denials | cost |
|---|---|---|---|
| `--allowedTools ""` (ours) | 4 | 2 | $0.2257 |
| `--tools "" --permission-mode dontAsk` | 1 | 0 | $0.0489 |

**4.6× cheaper.** On a full paper the four passes went $1.58 → $0.52.

### `--max-turns` exits with an *error*, it does not stop gracefully

Their comment: *"`--max-turns 1` — the docs say plainly: on reaching the limit it
exits with an **error**… Using a 'limit' to constrain something that normally
happens means normal cases get judged as failures."* We hit this exactly: our S2
died at `--max-turns 3` because the larger schema needs more turns.

### `--resume` does not restore `--system-prompt-file`

Their git log: *"revert that `--resume` 'optimization' — it replaced the only
path that worked."*

We had built session reuse to avoid re-uploading the paper on passes 2-4. It was
silently dropping the paper. Verified directly — put a marker in the system
prompt file, resume, ask for it:

```
pass1 (--system-prompt-file):  "MARKER_ALPHA_7391"     ✓
pass2 (--resume):              "ABSENT — there's no secret fluid
                                stated in my system prompt"
pass2 (--resume + file re-passed): "Nitrogen."         ✓
```

This explains an anomaly we had noticed and shrugged at: the Kim paper produced
3 evidence spans where comparable papers produced 18-19. Passes 2-4 never saw
it. Session reuse is now removed entirely — each pass re-sends the identical
prefix and the *server-side* prompt cache serves it anyway (measured: 23.7k
written once, then read on every subsequent call).

> The general lesson, which cost us most of a day: an optimization that reduces
> token counts looks like it is working. Verify that the content still *arrives*,
> not just that the bill went down.

---

## 2. Tagging: two opposite strategies

This is the substantive design difference.

| | **MHT-DataHub** | **Lumina** |
|---|---|---|
| Vocabulary | Controlled, hand-curated (`facets.yaml`) | **None** — freely generated |
| Model's job | Pick from the list, or `propose_new` | Invent a name |
| Consistency | By construction | By convergence at write time |
| Structure | 8 independent facets, 2-3 tiers each | 1 tag axis, 2 tiers, + a collection tree |
| Per item | Unbounded | `MAX_TAGS = 3` |
| Depth | 2-3 | `MAX_DEPTH = 2`, deliberately |

Lumina's central argument is worth quoting in full, because it is the strongest
statement of the case against precision I have seen:

> *A tag's value comes **entirely** from reuse. A tag applied to exactly one
> paper groups nothing — it is just an alias for that paper. So: prefer a
> slightly broader existing tag over a more precise new one. To decide whether a
> new tag is warranted, ask only: do I expect to apply this to other papers too?*

Free generation would normally rot — *"the first paper it writes 深度学习, the
second Deep Learning, the third 神经网络… each reasonable alone, collectively a
pile of semantically overlapping tags whose retrieval value is lower than no
tags at all, because you think clicking one showed you everything."* They stop
that at the door with two layers:

1. Normalized exact match (NFKC, case, punctuation and whitespace stripped)
2. Fuzzy match (`rapidfuzz`, ≥ 88) → **reuse the existing tag**

Plus cross-language matching, so `scheduling` and `排程` don't become two tags.

**Which is right depends on the corpus.** Ours is a single, well-mapped domain
where the tier-1/tier-2 structure is genuinely known in advance, and where the
downstream consumer is an ML pipeline that needs stable categorical fields. A
controlled vocabulary is correct there. Theirs is a personal library spanning
arbitrary fields, where nobody could write the vocabulary up front. Free
generation plus convergence is correct there.

But our design has a gap their design doesn't: **the `propose_new` path has no
convergence logic at all.** Once proposals accumulate we hit precisely the drift
they solved — see "to adopt" below.

### Three ideas from their tagging worth stealing regardless

**Asymmetric thresholds.** Reuse auto-applies at 0.75; creating a *new* tag
requires 0.85.

> *The risk is asymmetric: reusing an existing tag wrongly affects only this one
> paper and is trivially fixed; creating a new tag permanently enlarges the
> shared vocabulary and affects every later judgment. Tightening the
> irreversible, wide-blast-radius side is the shape this kind of threshold
> should have.*

**Make reuse a visible fact, not an exhortation.** They inject each existing
tag's document count into the prompt — `optimal transport (4)`:

> *Seeing "optimal transport (4)" tells the model the term is alive and worth
> attaching to; given only names, every tag looks equally weightless to it.*

They also inject each tag's **description**, so the model can tell OR-scheduling
from compiler instruction scheduling.

**Incremental organization, not global re-clustering.** Per-paper tagging drifts
because each call sees one paper; so they run a separate global pass that sees
all tags at once and groups them into 3-7 parents. Critically, `split_for_organize`
sends **only tags that have no parent yet**, and lists the existing parents as
the preferred vocabulary:

> *Full recomputation is not just slow, it reshuffles groupings you already
> accepted — the same tags won't necessarily come back in the same categories.*

Our plan's outer loop said "re-run S3+S5", which would have had exactly that
defect.

---

## 3. Provenance: independent convergence

Their `backend/llm/citations.py` and our `mhtdb/docmodel.py` are near-identical
in design, arrived at separately:

- normalize ligatures / smart quotes / dashes / whitespace before matching
- tiered exact → folded → fuzzy
- a minimum quote length, because *"locating to the wrong page is worse than not
  locating"* — same reasoning as our 12-character floor
- a page index; resolve quote → page for click-to-jump

Two things they do that we didn't:

**De-hyphenation across line breaks** (`formu-\nlation` → `formulation`). We had
no handling at all. Our corpus has **352 occurrences across five papers, 204 in
one**, and every quote spanning one would fail exact match. *Fixed* — with a
guard, since blind joining would mangle `two-phase` into `twophase`, which in
this literature is unacceptable. Joining is the safer default (a missed join
breaks provenance outright; a wrong join still matches), but a list of common
compound heads keeps the real hyphens.

**`rapidfuzz.partial_ratio` for the fuzzy tier.** It is sliding-window optimal
substring matching — exactly what we hand-rolled with `difflib`, and got wrong
the first time (an oversized window made the length prefilter reject every
candidate, silently disabling the whole fallback). Ours now works and is tested;
swapping in `partial_ratio` would be simpler and faster. Not urgent.

One thing we do that they don't: **reject-if-unlocatable as a hard gate.** Our
pipeline records `resolved: false` and a test asserts no record contains an
unresolvable quote. Theirs resolves citations for navigation; a miss just means
no jump link.

---

## 4. Frontend

Theirs: React 19 + TypeScript + Vite + Tailwind v4, TanStack Query, zustand,
react-router, PDFium/WASM. Ours: one static HTML file, vanilla JS, data inlined.

Not comparable as stacks — different products, and ours is deliberately
zero-build so it opens by double-click. What *is* transferable is their
interaction reasoning, which is unusually well argued.

| Idea | Why | Applies to us? |
|---|---|---|
| **Dashed border on machine-applied tags**, solid on confirmed | *"'this is an AI guess' and 'this is confirmed' must be distinguishable at a glance, or you don't know what to trust"* | **Yes.** Our dashboard renders deterministic `derived_tags` and inferred LLM facets identically. They have very different trust levels |
| Tag **tree** in a sidebar, not a chip row | *"a dozen flat tags is visual noise — you can't see the library's structure, you can only read them one by one"* | Yes for `phenomenon`, which is hierarchical. Our sidebar is flat checkbox groups |
| Expand arrow is a **separate button** from select | *"expanding and filtering are two different intents; merged, you're forced to filter just to see what's inside"* | Yes, if we add the tree |
| Persist expand state to localStorage | *"the tree's shape is part of the user's mental map"* | Yes |
| Never hide unparented tags | *"in a library that hasn't been organized yet nothing has a parent; hiding them makes users think tags vanished"* | Yes |
| Announce cleanup | *"a tag tree silently getting shorter is the most unsettling thing"* | Yes — we silently drop counterpart records on re-triage |
| Total count comes from the caller, not `max(tag counts)` | untagged docs appear in no tag, so deriving undercounts | Already correct in ours |
| Checkboxes always visible, not hover-only | hover-only controls don't exist on touch | N/A yet |
| Provenance badge adjacent to the claim | *"credibility is a property of the title; the closer, the easier to read"* | Already convergent — our page/section chips sit next to each quote |
| Review queue must be **one row, two buttons** | *"anything needing expand, navigate, or multiple clicks turns it into an inbox nobody opens"* | Yes, when we add a queue |
| Rejections are **remembered** | *"without that memory the same word returns on the next paper and the queue never empties"* | Yes |
| `needs_review` never blocks use | *"don't make metadata confirmation a prerequisite for reading — that's a UX disaster in many reference managers"* | Yes |

**Their best engineering idea is not a UI one.** `frontend/tools/check-glass.mjs`
validates the *built* CSS, because the production minifier was deleting the
standard `backdrop-filter` declaration as "redundant" — so the app's signature
glass effect *never once worked in production*, invisible to source review, dev
mode, and unit tests alike. Their guard also only inspects the CSS file
`index.html` actually references, after an earlier version false-alarmed on
stale hashed build artifacts:

> *A false alarm is worse than no guard — once it has cried wolf a few times
> people learn to ignore it, and it will be ignored when it's finally right.*

Our analogue is thin: `test_dashboard_builds_and_inlines_its_data` checks the
built artifact, but only that the payload placeholder was substituted.

---

## 5. So is their algorithm more advanced?

In parts. Honestly, per area:

**They are ahead on**
- Tag-name convergence — normalization + fuzzy merge at write time. We have
  nothing equivalent; our controlled vocabulary makes it unnecessary *until*
  `propose_new` starts accumulating, at which point we need it.
- Confidence gating and a review queue. We apply everything the model returns,
  with no threshold and no human-in-the-loop path.
- Incremental re-organization that preserves accepted groupings.
- Per-model `min_cacheable_tokens` awareness — they *remove* the cache
  breakpoint when content is below the model's minimum (512 on Opus 5, 1024 on
  Opus 4.8, 4096 on Opus 4.6/Haiku 4.5) rather than paying for a write that can
  never be read. We always set the breakpoint.
- Request construction as a **pure function**, so cache-breakpoint placement is
  unit-assertable without an API key: *"the one place where getting it wrong
  raises no error and just quietly costs 25% more."*
- Test depth: ~590 backend tests vs our 34.

**We are ahead on**
- Numeric normalization. They have no analogue of unit conversion, CoolProp
  properties, dimensionless groups, plausibility gating, or numeric→tag binning.
  They don't need it; for a physical-science ML corpus it is the substance.
- Provenance strictness — locate-or-reject as an enforced invariant, with
  multi-page/multi-section spans, versus resolve-for-navigation.
- Reproducible tags. Ours are a deterministic function of extracted numbers, so
  a threshold change re-tags the corpus with zero model calls. Theirs are model
  output and cannot be recomputed.
- Structured output via `--json-schema`; they parse JSON out of prose with a
  three-strategy extractor. (They may predate the flag.)

**Neither has** an evaluation harness with a real gold set. Ours is a skeleton;
theirs pins regressions with unit tests but doesn't score extraction quality.
Both of us set thresholds by judgment — they say so explicitly: *"the threshold
is picked, not calibrated."*

---

## 6. Adopted / to adopt

**Done**
- `--tools ""` + `--permission-mode dontAsk` (4.6× cost reduction)
- `--max-turns` raised to 12
- Session reuse removed; system prompt re-sent every call
- De-hyphenation across line breaks, with a compound-head guard
- Tests pinning all of the above

**Next, in value order**
1. **Confidence gating + review queue.** Highest value. Currently every
   `application_target` lands in the catalog regardless of the `confidence`
   field we already collect and then ignore.
2. **Visual trust distinction in the dashboard** — deterministic `derived_tags`
   vs inferred facets vs low-confidence application targets.
3. **Port their matcher to the `propose_new` path**, with asymmetric thresholds,
   before proposals accumulate.
4. **Inject usage counts and descriptions into the vocabulary block.** Cheap,
   and directly targets our observed failure of picking tier-1 without tier-2.
5. **Incremental organize** for taxonomy curation — never reshuffle accepted
   groupings.
6. `min_cacheable_tokens` check before setting the cache breakpoint.

## 7. One caution about their prompts

> *"How you display is how the model replies. The display format is an implicit
> contract."*

They rendered existing tags as `中文 / English` on one line; the model echoed the
whole line back as a tag name, lookup failed on every one, and an organize run
silently produced zero results.

We render our vocabulary as `parent / child -> [leaf, leaf]`. Our S3 output has
already shown tier-1-only picks with `tier2: null` on papers where a tier-2 was
clearly available. Worth testing whether the composite rendering is the cause.
