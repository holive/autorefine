---
name: autorefine
description: "Autonomous improvement loop for any markdown artifact. Builds validated LLM judges through error analysis (Phase 1), then runs an overnight mutation-and-score loop with human checkpoints (Phase 2). Use when iteratively improving a document (business plan, skill, SOP) with measurable quality dimensions."
metadata:
  complements: [prompt-refiner, plan-exit-review]
  args: "[project_path] -- optional path to the target project directory. defaults to current working directory"
---

# autorefine

Autonomous improvement loop for markdown artifacts. Claude Code is the LLM
orchestrator -- scripts handle data/IO only, Claude Code does all reasoning.

## How This Skill Works

Scripts live in `~/.claude/skills/autorefine/phase1/` and `phase2/`.
Project data lives in the user's working directory.

Set `SKILL_DIR` for convenience:
```bash
SKILL_DIR=~/.claude/skills/autorefine
```

**No API key needed.** Claude Code itself is the LLM -- it generates examples,
judges excerpts, and proposes mutations directly.

---

## Where to Start

- First time? No `examples/dimensions.md` yet --> Phase 1 below
- Judges validated, ready to loop             --> Phase 2 below
- Mid-loop, need reports                      --> Reports and Checkpoints below

---

# ============================================================
# Phase 1 -- Judge Setup (run once per domain)
# ============================================================

1. **Write `improve.md`** in the project directory: fill in the 4 required fields
   (what is being improved, what good looks like, hard constraints, end-to-end
   question). Copy the template from `$SKILL_DIR/improve.md` if starting fresh.

2. **Write `artifact_draft.md`**: a genuine first attempt, however rough.

3. **Manual audit**: follow `$SKILL_DIR/phase1/1_manual_audit.md` to discover 3-6
   dimensions through error analysis. Output: `examples/dimensions.md`

4. **Label real excerpts** -- you become the human judge that trains the LLM
   judges. The script shows you sections from your draft alongside each dimension's
   definition. For each pair, you decide: does this text satisfy the rule (PASS)
   or violate/ignore it (FAIL)? Then write a brief critique explaining why. Your
   labels become the gold standard the LLM judges are validated against.
   ```bash
   python3 $SKILL_DIR/phase1/3_label.py --source real --input artifact_draft.md
   ```
   **assisted labeling (recommended):** pre-label excerpts to reduce manual
   effort. run `--assist` twice -- first generates the request file, then
   codex or claude code pre-labels, then the human reviews with Enter-to-accept:
   ```bash
   # step 1: generate prelabel request
   python3 $SKILL_DIR/phase1/3_label.py --source real --input artifact_draft.md --assist
   # step 2: pre-label with codex (if available) or claude code
   #   codex: codex exec --full-auto "read examples/_prelabel_request.jsonl and
   #          examples/dimensions.md. for each pair, evaluate the excerpt against
   #          the dimension rule. write results to examples/_prelabel_response.jsonl
   #          format: {dimension, heading, model_label: PASS|FAIL, model_reason}"
   #   claude: read the request file and write the response file directly
   # step 3: assisted review (Enter accepts, p/f overrides)
   python3 $SKILL_DIR/phase1/3_label.py --source real --input artifact_draft.md --assist
   ```
   override rate > 30% signals unreliable pre-labels -- switch to interactive mode.

   **batch mode:** for fully automated / CI runs, use `--dry-run` + `--batch`:
   ```bash
   python3 $SKILL_DIR/phase1/3_label.py --source real --input artifact_draft.md --dry-run
   python3 $SKILL_DIR/phase1/3_label.py --source real --input artifact_draft.md --batch answers.json
   ```

5. **Generate synthetic examples** -- generate excerpts matching the draft's
   style, covering each dimension with PASS and FAIL variants. Output to
   `examples/unlabeled.jsonl`.

   Format per line:
   ```json
   {"text": "...", "dimension": "dim_name", "model_label": "PASS|FAIL", "model_reason": "..."}
   ```

   Target: ~20 per dimension (10 PASS + 10 FAIL). This ensures enough data
   for validation splits (~8 per test set).

   **cross-model generation (recommended):** to reduce same-model bias (where
   claude judges text claude generated), check if `codex` CLI is available
   (`which codex`). if so, use it for synthetic generation:
   ```bash
   codex exec --full-auto \
     "read examples/dimensions.md and artifact_draft.md. generate 20 synthetic \
      excerpts for dimension '<dim>' (10 PASS + 10 FAIL). write JSONL to \
      examples/_tmp_<dim>.jsonl"
   ```
   run one call per dimension (~20 examples each) rather than one massive call
   for all dimensions. this avoids timeouts and makes validation easier.
   if codex is unavailable, claude code generates directly (note the potential
   for same-model bias in observations).

6. **Label synthetic examples** -- recommended: auto-accept model labels.
   ```bash
   python3 $SKILL_DIR/phase1/3_label.py --source synthetic --auto-accept --input examples/unlabeled.jsonl
   ```
   This accepts model labels as human labels and prints a summary table.
   Use interactive mode (without `--auto-accept`) only if you want to
   independently review every example.

7. **Validate each judge** -- Claude Code runs this step automatically.
   No manual commands needed. For each dimension, Claude Code will:

   a. Run the split script to create judge prompt + dev/test batches:
      ```bash
      python3 $SKILL_DIR/phase1/4_validate_judge.py --mode split --dimension <dim>
      ```
   b. Read each example in the dev batch, judge it, write results
   c. Run the score script to compute TPR/TNR
   d. If there are disagreements, show them to the user and offer to
      align labels with the judge:
      ```bash
      python3 $SKILL_DIR/phase1/4_validate_judge.py --mode flip-to-judge --dimension <dim> --split dev
      ```
   e. If dev passes (TPR >= 80%, TNR >= 80%), repeat b-d with test split
   f. Report final go/no-go per dimension

   Judges need TPR >= 80% AND TNR >= 80% on point estimates. Small sample
   sizes trigger a warning but do not block validation.

8. **Initialize constraints + baseline**:
   ```bash
   python3 $SKILL_DIR/phase2/run.py init --artifact artifact.md --improve improve.md
   ```

# ============================================================
# Phase 2 -- Autonomous Loop
# ============================================================

Claude Code orchestrates the loop. Each iteration has 4 steps:

**Step A -- Score before mutation:**
```bash
python3 $SKILL_DIR/phase2/run.py score-before \
  --artifact artifact.md --improve improve.md [--context context.md]
```
Runs cheap checks, writes `runs/iter_NNN/judge_request.jsonl`.
Optional `--context` injects ground truth facts into judge prompts.

**Step B -- Claude Code judges (via parallel agents):** the script writes
per-dimension prompt files to `runs/iter_NNN/judge_prompt_<dim>.md`. Claude
Code launches one agent per dimension in parallel. Each agent reads its prompt
file from the **project directory** (not /tmp), evaluates it, and returns a
verdict. Claude Code collects all verdicts and writes
`runs/iter_NNN/judge_response.jsonl`:
```json
{"dimension": "dim_name", "verdict": "PASS: reason"}
```

**Step C -- Score after + prepare mutation:**
```bash
python3 $SKILL_DIR/phase2/run.py score-after --improve improve.md
```
Computes composite score, selects weakest dimension, writes
`runs/iter_NNN/mutation_request.md`.

**Step D -- Mutate the artifact:** read the mutation request, produce a revised
artifact using the `---ARTIFACT START---` / `---ARTIFACT END---` delimiters,
and write to `runs/iter_NNN/mutation_response.md`.

**cross-model mutation (recommended):** if `codex` CLI is available, use it for
mutations to avoid the judge evaluating text from its own model:
```bash
codex exec --full-auto "read runs/iter_NNN/mutation_request.md. follow its \
  instructions exactly. write the revised artifact to \
  runs/iter_NNN/mutation_response.md using the delimiters."
```
if codex is unavailable, claude code generates the mutation directly.

**Step E -- Apply mutation + re-judge:**
```bash
python3 $SKILL_DIR/phase2/run.py apply-mutation \
  --artifact artifact.md
```
Applies mutation, writes a judge request for the **targeted dimension only**
(`runs/iter_NNN/judge_after_prompt_<dim>.md`). Claude Code launches one agent
to re-judge that dimension. Non-targeted dimensions carry forward their
before-scores -- this eliminates judge variance on unrelated dimensions.
Verdict collected into `runs/iter_NNN/judge_response_after.jsonl`.

**Step F -- Verdict:**
```bash
python3 $SKILL_DIR/phase2/run.py verdict --artifact artifact.md
```
Compares before/after, keeps (strictly better) or discards, logs to `runs/log.jsonl`.
Also checks for stall (3+ consecutive discards) and signals when adversarial pass
is due (every 5th iteration by default).

**Adversarial pass (when signaled by verdict output):**
If verdict prints "ADVERSARIAL PASS DUE", run before the next normal iteration:
```bash
python3 $SKILL_DIR/phase2/run.py adversarial \
  --artifact artifact.md --improve improve.md
```
This writes `runs/iter_NNN/adversarial_request.md`. Claude Code reads the request,
analyzes the artifact from the hostile reader's perspective (persona defined in
improve.md), identifies 3-5 objections, and writes findings to
`runs/iter_NNN/adversarial_response.md`. Then process:
```bash
python3 $SKILL_DIR/phase2/run.py adversarial-process
```
Findings are saved to state and included in the next iteration's mutation request.

**Repeat** steps A-F for each iteration.

# ============================================================
# Reports and Checkpoints
# ============================================================

**Morning report:**
```bash
python3 $SKILL_DIR/phase2/run.py report
```

**Review queued checkpoints:**
```bash
python3 $SKILL_DIR/phase2/run.py review-checkpoints --improve improve.md
```

**Scan for invented data:**
```bash
python3 $SKILL_DIR/phase2/run.py placeholders --artifact artifact.md
```
Reports all `[PLACEHOLDER: ...]` tags in the artifact -- invented data that
needs to be replaced with real information before finalizing the document.
Mutations are instructed to tag any fabricated specifics (names, numbers, dates,
teams) with this format automatically.

---

## Project Directory Layout

```
project/
+-- improve.md                  # domain config: rubric, constraints, e2e question
+-- artifact.md                 # the thing being improved (loop edits this)
+-- artifact_draft.md           # rough draft (human writes before Phase 1)
+-- examples/
|   +-- dimensions.md           # discovered dimensions + cheap checks
|   +-- real_excerpts.jsonl      # labeled excerpts from draft
|   +-- unlabeled.jsonl         # synthetic examples awaiting labels
|   +-- synthetic_labels.jsonl  # labeled synthetic examples
+-- judge_prompts/              # one validated judge per dimension
|   +-- <dim>.md                # judge prompt with frontmatter
|   +-- <dim>_dev_batch.jsonl   # dev set for validation
|   +-- <dim>_dev_results.jsonl # claude code's dev judgments
|   +-- <dim>_test_batch.jsonl  # test set (held out)
+-- runs/
    +-- log.jsonl               # full iteration history
    +-- adversarial_log.jsonl   # all adversarial findings (accumulates)
    +-- constraint_hashes.json  # SHA-256 hashes of frozen sections
    +-- approved.md             # last human-approved version
    +-- best.md                 # current judge-best version
    +-- current_iteration.json  # iteration state + score cache
    +-- iter_NNN/               # per-iteration request/response files
```

---

## Key Concepts

- **Claude Code as orchestrator**: no API key needed. Claude Code generates
  examples, judges excerpts, and proposes mutations directly.
- **Scripts as data tools**: Python handles splitting, scoring, logging, metrics.
  No LLM calls in any script.
- **Judge as filter, human as objective**: judge score gets versions to a
  checkpoint. Human verdict decides what survives.
- **One judge per dimension**: each judge checks exactly one thing.
- **Strict keep/discard**: equal or worse = discard. No lateral accepts.
- **File-based handoff**: scripts write request files, Claude Code processes
  them, scripts read response files.
- **Project-directory only**: all handoff files must live in the project
  directory, not /tmp or other temp paths. Subagents run in a sandbox that
  restricts access outside the project tree.
- **Dimension weights**: judge frontmatter supports optional `weight: N`
  (default 1.0). Higher-weight dimensions count more in the composite score.
- **Context file**: optional `context.md` with human-maintained ground truth.
  Passed via `--context` to score-before and apply-mutation. Injected into
  judge prompts and mutation requests so the LLM respects verified facts.
- **Score caching**: after an adoption, before-scores are cached and reused
  in subsequent iterations until the next adoption. this eliminates judge
  variance on unchanged artifacts. use `--force-rejudge` to bypass the cache.
  cache is also invalidated when adversarial findings are present.
- **Stall detection**: verdict checks for consecutive discards, score
  plateaus, and all-PASS ceiling. when all dimensions pass and mutations keep
  getting discarded, suggests adding new dimensions from adversarial findings.
- **Adversarial log**: adversarial findings persist in `runs/adversarial_log.jsonl`
  across iterations. morning report shows all findings, not just the latest.
- **Cross-model generation**: using a different LLM (e.g., `codex` CLI) for
  synthetic examples, mutations, and adversarial passes reduces same-model
  bias. claude code auto-detects `codex` via `which codex` and uses it when
  available. see steps 5 and D for details.
- **Adversarial passes**: every Nth iteration (default 5), a hostile-reader
  analysis finds gaps the rubric judges miss. Findings feed into the next
  iteration's mutation request as additional context.

---

## Dependencies

Requires: `rich` (Python 3.11+)

```bash
pip install rich
```
