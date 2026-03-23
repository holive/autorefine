# autorefine

A Claude Code skill for iteratively improving markdown documents.

Score a document. Mutate the weakest part. Keep it only if the score went up.
Repeat.

`/autorefine [project_path]` -- requires Python 3.11+, `rich`, and Claude Code.

---

## The problem this solves

Ask an LLM to "make this better" three times and you'll notice: it rewrites
confidently each time, but the third version isn't reliably better than the
first. Sometimes it's worse. The LLM grades its own homework, so it can't tell
the difference. You lose track of what improved and what regressed.

This isn't a prompting failure -- it's a structural one. Without separated
evaluation, targeted edits, and strict acceptance criteria, revision is a
random walk. autorefine replaces that with a loop that has those three things.

**The simpler alternative** is: write a rubric, generate 3-5 candidate
rewrites, pick the best one manually. That works for short documents and one-off
edits. autorefine is worth the setup cost when:

- the document will go through 5+ revision cycles
- quality has multiple independent dimensions (not just "is it clear?")
- you want the loop to run autonomously overnight and present results in the morning
- you need to prove the document improved on specific, measurable criteria

**Don't use autorefine for:**

- documents where quality is primarily about taste, tone, or style (the judges
  can't reliably distinguish "better" from "different")
- one-page documents with one quality dimension (just prompt and review manually)
- anything where the setup cost (~30-60 min for Phase 1) exceeds the revision
  time you'd spend anyway

---

## How it works

### Phase 1: build the judges (~30-60 min human time, one-time per domain)

**Define the domain.** Fill in `improve.md`: what is being improved, who reads
it, what good looks like in observable terms, what must never change, and one
yes/no question that captures "did the document achieve its purpose?" The
template forces specificity because vague rubrics produce vague judges.

**Write a rough draft.** `artifact_draft.md` -- however rough. The audit needs
text to fail on. A polished draft hides the failure modes you're trying to catch.

**Manual audit.** Read your draft against your rubric and list 10-15 specific
failures. Group them into 3-6 dimensions.

*Why you, not the LLM:* the LLM can find structural issues, but it can't know
which failures will make *your specific reader* say no. A pricing section that
lacks justification might be critical for a VC deck but irrelevant for an
internal SOP. The LLM can do a first pass (and the audit template suggests
this), but the final dimension list has to reflect your domain judgment.

*Why 10-15 observations and 3-6 dimensions:* fewer than 10 observations usually
means you're reading too fast -- a second pass almost always finds more. More
than 6 dimensions creates validation overhead that rarely pays off (each judge
needs ~25 labeled examples). These are heuristics from testing, not hard limits.

Each dimension passes a quality check: grounded in a real failure? Can you write
PASS/FAIL right now? Atomic (one thing)? Subjective (can't a regex or word count
handle it)? Anything deterministic becomes a cheap check -- free, instant, and
never wrong. Every LLM judge you don't build is one fewer to validate.

**Label.** You become the human judge. The script shows sections from your draft
alongside each dimension, and you decide PASS or FAIL with a brief critique.
These labels are the gold standard -- everything downstream is validated against
them.

Three labeling modes:
- **Interactive** (default): you read each excerpt and decide from scratch.
- **Assisted** (`--assist`): an external model pre-labels each pair. You review
  with Enter-to-accept for obvious cases, override only when you disagree.
  In testing, this cut labeling from ~15 min to ~3 min on a 5-dimension,
  8-section document.
- **Batch** (`--batch answers.json`): fully non-interactive. For CI and repeats.

*Why the human labels first, not the model:* if you see the model's answer before
deciding, you anchor to it. The disagreements between your judgment and the
model's are the most valuable training data. Assisted mode shows the model's
suggestion *after you've seen the text*, so the anchoring risk is lower -- but
the interactive mode is purer if you have the time.

**Synthetic examples.** Claude Code (or Codex CLI if available) generates ~20
examples per dimension (10 PASS + 10 FAIL) to cover edge cases beyond your
single draft.

*Why 20:* the validation split needs ~8 examples per test set (4 PASS + 4 FAIL)
to compute TPR/TNR with wilson confidence intervals that aren't uselessly wide.
20 per dimension, after the 15/43/42 train/dev/test split, yields ~8 test
examples. Fewer than that and the 95% CI spans [51%, 100%] -- you can't tell if
the judge is good or lucky.

*Why cross-model generation:* if Claude generates the synthetic examples and
Claude judges them, the judge is evaluating text from its own distribution.
Errors the model makes systematically (e.g., always including a specific
phrasing pattern) won't be caught. Using a different model (Codex CLI) for
generation creates distribution mismatch that the judge has to handle honestly.

**Validation.** Labeled data is split into train (few-shot examples in the
judge prompt), dev (iterate on the prompt), and test (final go/no-go).

*Why 15/43/42 and not 60/20/20:* the train set only needs 2-4 examples because
it's used for few-shot prompting, not gradient-based learning. The unusual split
maximizes dev and test sizes from a small total pool (~25 examples per
dimension). A larger train set would waste examples on few-shot slots that
don't improve judge quality.

A judge needs TPR >= 80% and TNR >= 80% to pass.

*Why 80/80:* a judge with 75% TNR will incorrectly pass ~1 in 4 failing
excerpts. Over 10 iterations, that's 2-3 false improvements entering the
artifact. At 80/80, the expected false-accept rate drops enough that the
keep/discard logic stays meaningful. 90/90 is the target; 80/80 is the floor.
Below that, the judge corrupts the loop faster than the loop improves the
artifact. This threshold came from testing -- at 75% TNR, we observed adopted
mutations that made the artifact worse according to human review.

### Phase 2: the autonomous loop (~2-5 min per iteration)

Each iteration:

**Score.** Run cheap checks (deterministic), then launch one parallel agent per
dimension. Each judge returns PASS or FAIL. The composite score is a weighted
average of all checks and judge verdicts.

*Why binary judges instead of 1-10 scores:* LLMs produce inconsistent numeric
scores -- the same text can get 7/10 and 5/10 across runs. Binary PASS/FAIL
is dramatically more reliable (our validation tests show 90-100% agreement).
Dimension weights handle importance separately without relying on the model to
produce consistent numbers.

*Why one judge per dimension:* compound judges ("evaluate clarity, accuracy, and
tone") have lower agreement rates than atomic ones. When a compound judge
disagrees with a human label, you can't tell which sub-criterion caused the
disagreement. One judge per dimension makes validation tractable.

**Mutate.** The script picks the weakest dimension (most frequent recent
failures, breaking ties by critique length then alphabetically) and builds a
mutation request targeting only that one.

*Why one dimension at a time:* a mutation that tries to fix everything tends to
regress on dimensions it wasn't targeting. In testing, multi-target mutations
had ~40% discard rate vs ~25% for single-target. The cleaner signal is worth
the slower progress.

*Why critique length as a tiebreaker:* longer critiques correlate with more
specific, actionable feedback. A critique that says "the pricing section lists
three tiers without justification and doesn't compare to competitors" gives
the mutator more to work with than "pricing is vague." This is a heuristic,
not a strong signal -- it only matters when fail frequency is tied.

**Re-judge.** Only the targeted dimension gets re-judged. Non-targeted
dimensions carry forward their before-scores.

*Why not re-judge everything:* LLM judges have variance. The same text can get
PASS on one run and FAIL on the next (~5-10% flip rate in testing). If you
re-judge all 5 dimensions, there's a meaningful chance one flips randomly,
creating a false improvement or false regression that has nothing to do with
the mutation. Re-judging only the targeted dimension isolates the signal.

**Verdict.** Strictly better = adopt. Equal or worse = discard.

*Why no lateral accepts:* allowing "different but equal" mutations lets the
artifact drift without the score catching it. Over 10 iterations, that drift
accumulates. The mutation might seem harmless in isolation, but it shifts the
text away from what the judges validated. Strict improvement is the only safe
acceptance criterion for an autonomous loop.

**Score caching.** After an adoption, before-scores are cached and reused until
the next adoption. This means a discarded mutation doesn't trigger re-judging
of the unchanged artifact.

*Why:* in testing, we observed a score regression from 1.00 to 0.86 on an
unchanged artifact because a judge flipped on `call_to_action_clarity` between
runs. Caching eliminates this class of variance entirely. Use `--force-rejudge`
if you suspect the cache is stale.

**Stall detection.** Three signals: 3+ consecutive discards, score plateau
(identical score for 3+ iterations), or all-PASS ceiling (every dimension
passing but mutations keep getting discarded).

*Why 3 consecutive discards:* 1-2 discards are normal (not every mutation
improves things). 3 in a row means the loop is likely at its ceiling for the
current dimension set. In testing, runs that stalled at 3 discards never
recovered without human intervention (new dimensions or a context change).

At the all-PASS ceiling, the system surfaces adversarial findings as candidates
for new dimensions -- because the rubric judges are satisfied but the document
may still have gaps they don't measure.

**Adversarial pass.** Every 5th iteration, a hostile-reader analysis identifies
3-5 gaps the rubric judges miss. Findings are classified as writing gaps (the
loop can fix) or data gaps (human needs to provide new information).

*Why every 5th, not continuous:* adversarial analysis costs one full LLM turn
and produces findings that feed exactly one mutation. Running it every iteration
would double the cost with diminishing returns -- most adversarial findings are
stable across 5 iterations. Running it only on stall would miss gaps that the
rubric judges will never catch.

*Why findings persist in adversarial_log.jsonl:* in the original design,
findings were one-shot (used once, then cleared). In testing, we found that
the most valuable output of a 10-iteration run was the adversarial findings --
they correctly identified gaps (team credibility, migration story) that no
rubric dimension covered. Persisting them gives the human a running list of
what the rubric misses.

**Human checkpoints.** Periodically, you review the current best version and
answer the end-to-end question from `improve.md`. Judges optimize proxy metrics.
The human checks whether the document actually achieves its purpose.

*Why this matters:* a document can pass all 5 dimensions and still fail its
purpose. In testing, the artifact reached 1.00 composite score but the
adversarial reviewer correctly noted the "solution" section was still generic
marketing language. The judges measured what they were told to measure; the
human catches what they weren't.

---

## Cost and time expectations

| Phase | Human time | Machine time | Notes |
|-------|-----------|-------------|-------|
| Phase 1 (first run) | 30-60 min | ~10 min | mostly the manual audit and labeling |
| Phase 1 (with --assist) | 15-30 min | ~10 min | pre-labels reduce labeling to ~3 min |
| Phase 2 (per iteration) | 0 (autonomous) | 2-5 min | depends on dimension count and model speed |
| Phase 2 (10 iterations) | 5-10 min checkpoints | ~30 min | 2 adversarial passes, 1-2 checkpoint reviews |

A typical run: 30 min Phase 1 setup + 30 min unattended Phase 2 + 10 min
review = ~70 min total, most of it unattended. Worth it when you'd otherwise
spend 2+ hours on manual revision cycles with uncertain quality.

This runs entirely through Claude Code -- no separate API key or billing. The
cost is whatever your Claude Code plan charges for the turns consumed.

---

## Under the hood

### Architecture

Two roles, strictly separated:

| Role | Handles | Why |
|---|---|---|
| Claude Code (LLM) | generates examples, judges text, proposes mutations | non-deterministic reasoning |
| Python scripts | splitting, scoring, logging, metrics, constraints | deterministic, testable, auditable |

No LLM calls in any script. All handoff is file-based: scripts write request
files, Claude Code processes them, scripts read response files. Files live in
the project directory (not /tmp) because subagents run in a sandbox that can't
reach outside the project tree.

*Why file-based and not direct tool returns:* files are auditable (you can read
every judge prompt and verdict after the fact), survive across Claude Code turns
(the session can resume after a disconnect), and work within the subagent
sandbox model. The tradeoff is operational visibility over convenience.

### State machine

```
initialized -> awaiting_judge_before -> awaiting_mutation -> awaiting_judge_after -> completed
                                                                                       |
                                                                                awaiting_adversarial -> completed
```

State persists in `runs/current_iteration.json` across Claude Code turns.

### Composite score

```
cheap_pass  = sum(1.0 for each passing cheap check)
llm_pass    = sum(weight[dim] for each passing LLM dimension)
cheap_total = count(cheap checks)              -- implicit weight 1.0
llm_total   = sum(weight[dim] for all LLM dimensions)
composite   = (cheap_pass + llm_pass) / (cheap_total + llm_total)
```

*Why a weighted average and not something more complex:* the score only needs to
answer "did this mutation make things better?" A weighted average is monotonic
in dimension improvements, easy to debug, and doesn't hide which dimensions
contributed. The risk of one heavy dimension dominating is real -- but that's
a feature: if you weight something 3x, it *should* dominate until it passes.

### Rogan-Gladen correction (morning reports)

Observed pass rate != true pass rate when judges have imperfect TPR/TNR. The
morning report corrects for known judge error rates:

```
theta = (p_observed + TNR - 1) / (TPR + TNR - 1)
```

*Why bother:* if a judge has 85% TNR, an observed 90% pass rate means the true
pass rate is ~82%. Without correction, the morning report would overstate
quality. The correction is small when judges are good (>90% TPR/TNR), but it
surfaces problems early when a judge is mediocre.

### Constraint hashing

At init, constrained sections (from `improve.md`) are extracted by heading
regex and SHA-256 hashed. At each `score-before`, current hashes are compared.
Any mismatch reverts the artifact to `approved.md` and aborts the iteration.

### Adversarial pass lifecycle

1. `adversarial` writes a request with the artifact, persona, and instructions
2. Claude Code (or Codex) analyzes and writes a response with objections
3. `adversarial-process` saves findings to state and appends to `runs/adversarial_log.jsonl`
4. next `score-before` carries findings forward (also invalidates score cache)
5. next `score-after` injects findings into the mutation request
6. findings clear from state after one mutation cycle, but persist in the adversarial log

### Judge validation pipeline

```
labels -> 3-way stratified split (train 15% / dev 43% / test 42%)
                                        |
                              train = few-shot examples in judge prompt
                              dev   = iterate on prompt, flip labels if needed
                              test  = final go/no-go (TPR >= 80%, TNR >= 80%)
```

Wilson score confidence intervals at 95%. Small samples (< 7) trigger a
prominent warning -- at that size, the CI is wide enough that a "100% TPR"
could really be anywhere from 51% to 100%.

### File layout

```
project/
+-- improve.md                  # domain config (user writes once)
+-- artifact.md                 # the artifact (loop edits this)
+-- artifact_draft.md           # rough draft (before Phase 1)
+-- context.md                  # optional ground truth (human maintains)
+-- examples/
|   +-- dimensions.md           # dimensions + cheap checks
|   +-- real_excerpts.jsonl     # labeled real excerpts
|   +-- _prelabel_request.jsonl # assist mode: pairs for external model
|   +-- _prelabel_response.jsonl# assist mode: pre-labels from external model
|   +-- unlabeled.jsonl         # synthetic examples
|   +-- synthetic_labels.jsonl  # labeled synthetic examples
+-- judge_prompts/
|   +-- <dim>.md                # judge prompt with frontmatter
|   +-- <dim>_dev_batch.jsonl
|   +-- <dim>_test_batch.jsonl
+-- runs/
    +-- log.jsonl               # iteration history (append-only)
    +-- adversarial_log.jsonl   # all adversarial findings (accumulates)
    +-- constraint_hashes.json  # SHA-256 of frozen sections
    +-- approved.md             # last human-approved version
    +-- best.md                 # current judge-best version
    +-- current_iteration.json  # state machine + score cache
    +-- iter_NNN/               # per-iteration request/response files
```

### CLI reference

Phase 2 (`phase2/run.py`):

| Command | Key args | Purpose |
|---|---|---|
| `init` | `--artifact`, `--improve` | hash constraints, copy baseline |
| `score-before` | `--artifact`, `--improve`, `[--context]`, `[--force-rejudge]` | cheap checks, write judge requests (uses cache if available) |
| `score-after` | `--improve` | compute score, select weakest, write mutation request |
| `apply-mutation` | `--artifact`, `[--context]` | apply mutation, write after-judge request |
| `verdict` | `--artifact`, `[--stall-threshold]`, `[--adversarial-interval]` | keep/discard, log, stall check |
| `adversarial` | `--artifact`, `--improve` | write adversarial analysis request |
| `adversarial-process` | -- | process response, save findings |
| `report` | `[--log]`, `[--judge-dir]` | morning report with Rogan-Gladen correction |
| `review-checkpoints` | `--improve` | interactive human checkpoint review |

Phase 1:

| Script | Key args | Purpose |
|---|---|---|
| `3_label.py` | `--source real\|synthetic`, `--input`, `[--auto-accept]`, `[--assist]`, `[--batch]`, `[--dry-run]` | interactive, assisted, batch, or auto labeling |
| `4_validate_judge.py` | `--mode split\|score\|flip-to-judge`, `--dimension` | split, score, align labels |

### Design decisions

| Decision | Alternative | Why this choice |
|---|---|---|
| Binary judges + weights | Graded 1-10 scoring | LLMs produce inconsistent numbers; binary is dramatically more reliable (~95% vs ~60% agreement in testing) |
| One judge per dimension | Multi-dimension judges | compound judges have lower agreement; when they disagree you can't tell which criterion failed |
| Strict keep/discard | Accept lateral moves | lateral accepts cause drift that accumulates over 10+ iterations; strict improvement is the only safe policy for autonomous loops |
| Re-judge targeted only | Re-judge all dimensions | eliminates ~5-10% random flip rate on unchanged dimensions; isolates the mutation signal |
| Scheduled adversarial | Continuous adversarial | adversarial costs a full turn and produces findings for one mutation; every-5th balances coverage vs cost |
| File-based handoff | Direct returns | auditable, survives disconnects, works in subagent sandbox; tradeoff is visibility over convenience |
| SHA-256 constraints | Diff-based | exact match on frozen sections; no false positives, no complexity |
| Human labels first | Model labels first | prevents anchoring bias; disagreements are the most valuable training data |
| Cross-model generation | Single model for everything | avoids same-model bias; in testing, Claude judging Codex-generated text caught errors Claude-on-Claude missed |
| Score caching | Re-judge every iteration | eliminates variance on unchanged artifacts; observed 14% score regression on unchanged text without caching |
| Assisted labeling | Interactive-only | cuts labeling ~80% while preserving the human override signal that makes the training data valuable |

### Portability

autorefine is currently a Claude Code skill. The Python scripts are
model-agnostic (no LLM calls), but the orchestration (SKILL.md) assumes Claude
Code as the LLM layer. Porting to another agent framework would require
rewriting the orchestration instructions, not the scripts.
