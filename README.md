# autorefine

A Claude Code skill.

Score a markdown document. Mutate the weakest part. Keep it only if the
score went up. Repeat.

Regular prompting is a random walk -- revisions go sideways as often as
forward, the LLM grades its own homework, and you lose track after a few
passes. autorefine separates the judges from the writer, targets one
weakness per iteration, only keeps what scores higher, detects when it's
hit a ceiling, and periodically attacks the document from a hostile
reader's perspective.

Works for business plans, skills, SOPs, technical specs -- anything where
you can tell PASS from FAIL. No API key needed.

`/autorefine [project_path]` -- requires Python 3.11+, `rich`, and Claude Code.

---

## How it works

### Phase 1: build the judges

You start by filling in `improve.md`, a template that forces you to define your
artifact, your reader, what good looks like, and what must never change. The
template exists because "make it better" is too vague for a judge. You need
concrete, observable criteria a reader could point to in the text.

Then write `artifact_draft.md`, a genuine first attempt, however rough. Error
analysis needs text to fail on.

**Manual audit.** Read your draft against your rubric and identify 10-15
specific failure observations. Group them into 3-6 dimensions (recurring failure
patterns). You do this, not the LLM, because you know your domain and your
reader. The LLM can do a first pass, but the final list has to reflect what
*you* think matters.

Each candidate dimension passes a quality check: is it grounded in a real
failure? Can you write PASS/FAIL right now? Is it atomic (one thing)? Is it
subjective (can't a regex handle it)? Anything a regex can check becomes a cheap
check, free, instant, deterministic. Every judge you don't build is less work to
validate and maintain.

**Labeling.** You become the human judge. The script shows you sections from
your draft alongside each dimension, and you decide PASS or FAIL with a brief
critique. Your labels become the gold standard the LLM judges are validated
against. You label first, before the model, to prevent anchoring bias.

Then Claude Code generates synthetic examples (~20 per dimension, 10 PASS + 10
FAIL) to cover edge cases beyond your single draft, and those get labeled too.

**Validation.** For each dimension, labeled data is split into train (few-shot
examples), dev (iterate on the prompt), and test (final go/no-go). Claude Code
judges the dev set; the script computes TPR and TNR. A judge needs TPR >= 80%
and TNR >= 80% to pass. Below that, the judge is wrong often enough to corrupt
the improvement loop.

After all judges pass, you initialize Phase 2. The system SHA-256 hashes any
constrained sections so mutations that touch frozen content get auto-reverted.

### Phase 2: the loop

Each iteration has 6 steps.

**Score.** Run cheap checks (deterministic assertions), then launch one parallel
agent per dimension to judge the artifact. Each judge returns PASS or FAIL. The
composite score is a weighted sum. Dimension weights (set in judge frontmatter)
let you express that some things matter more than others. Binary judges are more
reliable than graded 1-10 scores; weights handle importance separately.

If you have a `context.md` with verified facts (market sizes, financials, team
details), it gets injected into judge prompts and mutation requests so the LLM
respects ground truth. Only you can verify facts, so this is your channel for
injecting truth into an autonomous loop.

**Mutate.** The script picks the weakest dimension and builds a mutation request
targeting only that one. One targeted change produces a clean signal. Mutations
that try to fix everything at once tend to regress on dimensions they weren't
targeting.

**Re-judge.** Only the targeted dimension gets re-judged. Non-targeted
dimensions carry forward their before-scores. Without this, random noise on
other dimensions can flip the verdict for reasons that have nothing to do with
the mutation.

**Verdict.** If the composite score went up, the mutation is adopted. Equal or
worse is discarded. No lateral accepts. They let the artifact drift without
improving, and over many iterations that drift accumulates into noise.

**Stall detection.** After 3+ consecutive discards or a score plateau, the
system warns you. Without this, the loop burns tokens on mutations that will
never land.

**Adversarial pass.** Every Nth iteration (default 5), a hostile-reader
analysis looks for gaps the rubric judges miss. The adversarial persona (defined
in `improve.md`) raises 3-5 objections and classifies each as a writing gap
(fixable by the loop) or a data gap (needs human intervention). Findings feed
into the next mutation, then clear. One-shot, not permanent.

**Human checkpoints.** Periodically, you review the current best version against
the last human-approved version and answer the end-to-end question from
`improve.md`. Judges optimize proxy metrics. The human checks the actual goal.
The principle is simple: judges filter, humans decide.

---

## Under the hood

### Architecture

Two roles, strictly separated:

| Role | Handles | Why |
|---|---|---|
| Claude Code (LLM) | Generates examples, judges text, proposes mutations | Non-deterministic reasoning |
| Python scripts | Splitting, scoring, logging, metrics, constraints | Deterministic, testable, auditable |

No LLM calls in any script. All handoff is file-based: scripts write request
files, Claude Code processes them, scripts read response files. Files live in
the project directory (not /tmp) because subagents run in a sandbox that can't
reach outside the project tree.

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

### Weakest dimension selection

Tiebreaking chain when multiple dimensions fail:
1. Historical fail frequency (last 3 iterations)
2. Critique length (longer critique = more to fix)
3. Alphabetical

If all pass: same chain using history only.

### Stall detection

Two independent signals, either triggers a warning:
- Consecutive discards: N in a row (default threshold: 3)
- Score plateau: identical `composite_before` for N consecutive iterations

### Rogan-Gladen correction (morning reports)

Observed pass rate != true pass rate when judges have imperfect TPR/TNR.
The morning report corrects for known judge error rates:

```
theta = (p_observed + TNR - 1) / (TPR + TNR - 1)
```

Only applied when TPR + TNR > 1. Clamped to [0, 1].

### Constraint hashing

At init, constrained sections (from `improve.md`) are extracted by heading regex
and SHA-256 hashed. At each `score-before`, current hashes are compared. Any
mismatch reverts the artifact to `approved.md` and aborts the iteration.

### Adversarial pass lifecycle

1. `adversarial` writes a request with the artifact, persona, and instructions
2. Claude Code analyzes and writes a response with objections (scored 1-10, classified as writing or data gap)
3. `adversarial-process` saves findings to state and log
4. Next `score-before` carries findings forward
5. Next `score-after` injects findings into the mutation request
6. Findings clear after one mutation cycle (one-shot, not permanent)

### Judge validation pipeline

```
labels -> 3-way stratified split (train 15% / dev 43% / test 42%)
                                        |
                              train = few-shot examples in judge prompt
                              dev   = iterate on prompt, flip labels if needed
                              test  = final go/no-go (TPR >= 80%, TNR >= 80%)
```

Wilson score confidence intervals at 95%. Small samples (< 7) warn but don't
block. Passing judges get test metrics stamped into frontmatter.

### File layout

```
project/
+-- improve.md                  # domain config (user writes once)
+-- artifact.md                 # the artifact (loop edits this)
+-- artifact_draft.md           # rough draft (before Phase 1)
+-- context.md                  # optional ground truth (human maintains)
+-- examples/
|   +-- dimensions.md           # dimensions + cheap checks
|   +-- labels_real.jsonl       # labeled real excerpts
|   +-- unlabeled.jsonl         # synthetic examples
|   +-- synthetic_labels.jsonl  # labeled synthetic examples
+-- judge_prompts/
|   +-- <dim>.md                # judge prompt with frontmatter
|   +-- <dim>_dev_batch.jsonl
|   +-- <dim>_test_batch.jsonl
+-- runs/
    +-- log.jsonl               # iteration history (append-only)
    +-- constraint_hashes.json  # SHA-256 of frozen sections
    +-- approved.md             # last human-approved version
    +-- best.md                 # current judge-best version
    +-- current_iteration.json  # state machine
    +-- iter_NNN/               # per-iteration request/response files
```

### CLI reference

Phase 2 (`phase2/run.py`):

| Command | Key args | Purpose |
|---|---|---|
| `init` | `--artifact`, `--improve` | Hash constraints, copy baseline |
| `score-before` | `--artifact`, `--improve`, `[--context]` | Cheap checks, write judge requests |
| `score-after` | `--improve` | Compute score, select weakest, write mutation request |
| `apply-mutation` | `--artifact`, `[--context]` | Apply mutation, write after-judge request |
| `verdict` | `--artifact`, `[--stall-threshold]`, `[--adversarial-interval]` | Keep/discard, log, stall check |
| `adversarial` | `--artifact`, `--improve` | Write adversarial analysis request |
| `adversarial-process` | -- | Process response, save findings |
| `report` | `[--log]`, `[--judge-dir]` | Morning report with Rogan-Gladen correction |
| `review-checkpoints` | `--improve` | Interactive human checkpoint review |

Phase 1:

| Script | Key args | Purpose |
|---|---|---|
| `3_label.py` | `--source real\|synthetic`, `--input`, `[--auto-accept]` | Interactive or auto labeling |
| `4_validate_judge.py` | `--mode split\|score\|flip-to-judge`, `--dimension` | Split, score, align labels |

### Design decisions

| Decision | Alternative | Why this choice |
|---|---|---|
| Binary judges + weights | Graded 1-10 scoring | LLMs are better at yes/no than consistent numbers |
| One judge per dimension | Multi-dimension judges | Compound judges have lower agreement rates |
| Strict keep/discard | Accept lateral moves | Prevents noise accumulation from drift |
| Re-judge targeted only | Re-judge all dimensions | Eliminates variance on unrelated dimensions |
| Scheduled adversarial | Continuous adversarial | Cheaper; findings feed one mutation cycle |
| File-based handoff | Direct returns | Auditable, survives across turns, works in sandbox |
| SHA-256 constraints | Diff-based | Exact match, zero false positives |
| Human labels first | Model labels first | Prevents anchoring bias in training data |
