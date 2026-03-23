# Manual Failure Mode Audit

**Time budget: ~30 minutes. Do this before any code runs.**

Prerequisites:
- `improve.md` filled in (rubric + hard constraints + end-to-end question)
- `artifact_draft.md` exists (however rough)

---

## How this works

You (the domain expert) are going to read your draft and identify specific ways it
fails or could fail. This is error analysis -- the same process a QA engineer uses
to find bugs, but applied to a document.

Why you and not the LLM: you know your domain, your reader, and what actually matters.
An LLM can identify structural issues in text, but it can't know which failures will
make your specific reader say no. Your observations become the foundation for everything
that follows -- the judges, the labels, the improvement loop.

Claude can help by doing a first pass and proposing observations, but you should review,
edit, add, and remove based on your domain knowledge. The final list must reflect what
YOU think matters, not what the LLM thinks sounds important.

The audit has 6 steps. Each builds on the previous one.

---

## Step 1: Open Coding

**What to do:** Read `improve.md` and `artifact_draft.md` side by side. Write 10-15
freeform notes about specific ways the draft currently fails the rubric, or could fail
if the loop mutates it poorly.

**What "specific" means:**
- good: "The problem section says 'many SMBs struggle' -- this names a category not a person"
- good: "The R$80k variant is listed but then called NOT recommended in a footnote -- confusing"
- bad: "Lacks clarity"
- bad: "Could be more persuasive"

Each note should point to a real place in the draft (a section, a sentence, a number)
and say what's wrong with it. If you can't point to a specific place, the observation
is too vague.

**How Claude can help:** ask Claude to do a first pass ("read my rubric and draft and
list 10-15 specific failure observations"). Then review the list -- cross out what
doesn't matter for your domain, sharpen what's imprecise, and add what was missed.

Notes:

1.
2.
3.
4.
5.
6.
7.
8.
9.
10.
11.
12.
13.
14.
15.

---

## Step 2: Axial Coding

**What to do:** Group your notes from Step 1 into 4-8 failure categories. Each category
is a recurring pattern you noticed across multiple notes.

**Naming rule:** Name categories with concrete snake_case labels that describe the
specific failure. `problem_specificity` is good. `clarity` is not -- it could mean
anything.

**Example:** If notes 2, 5, and 9 all relate to "numbers that exist but aren't sourced
or are inconsistent with each other," that's a category like `financial_traceability`.

| Category name | Notes included | One-sentence definition |
|---|---|---|
| | | |
| | | |
| | | |
| | | |
| | | |
| | | |
| | | |
| | | |

---

## Step 3: Dimension Iteration

**What to do:** Re-read the draft one more time with your categories in mind. You'll
likely find that some categories overlap, some are too broad, and some miss observations
that now seem obvious.

Expect 2-3 passes before categories stabilize. This is normal -- if they stabilize
on the first pass, you probably aren't reading carefully enough.

**Pass 1 changes:**


**Pass 2 changes:**


**Pass 3 changes (if needed):**


---

## Step 4: Dimension Quality Check

**What to do:** For each candidate dimension, answer four yes/no questions. If any
answer is NO, the dimension needs work before continuing.

| Dimension | Q1: grounded in real failure? | Q2: can write PASS/FAIL now? | Q3: atomic (one thing)? | Q4: subjective (no cheap check)? | Verdict |
|---|---|---|---|---|---|
| | | | | | |
| | | | | | |
| | | | | | |
| | | | | | |
| | | | | | |
| | | | | | |

**What the questions mean:**

- **Q1 -- Grounded in real failure?** Can you point to a specific place in the draft
  where this dimension fails? If not, you're inventing a problem that doesn't exist.

- **Q2 -- Can write PASS/FAIL now?** Without thinking hard, can you write one sentence
  that would PASS and one that would FAIL this dimension? If you hesitate, the
  definition is too vague for a judge to evaluate consistently.

- **Q3 -- Atomic?** Does this dimension measure exactly one thing? If it's "the numbers
  are correct AND well-presented," that's two dimensions. Split it.

- **Q4 -- Subjective?** Could a regex, word count, or section-presence check handle
  this? If yes, it's a cheap check (free, deterministic), not an LLM judge dimension.

**Routing based on answers:**
- Q4 = NO --> mark as CHEAP CHECK (implement as code assertion in Phase 2)
- Q3 = NO --> split it into two dimensions and re-check each
- Q1 or Q2 = NO --> ground it in a specific failure or drop it

---

## Step 5: Fix-Before-Eval Triage

**What to do:** For each dimension that passed Step 4, ask: can I just fix this right
now by editing `artifact_draft.md`? Not every failure needs a judge -- some are one-time
fixes.

| Dimension | Fixable now? | If fixed, will it recur? | Keep as judge dimension? |
|---|---|---|---|
| | | | |
| | | | |
| | | | |
| | | | |
| | | | |
| | | | |

**Decision rules:**
- Fixable + won't recur --> fix it now, drop the dimension (one fewer judge to build)
- Fixable + will recur --> fix it now, keep the dimension (the judge catches regressions)
- Checkable with code --> implement as cheap check, drop from judge scope
- Subjective + recurring + not directly fixable --> keep as LLM judge dimension

**The point:** only dimensions that are subjective, recurring, and not directly fixable
need LLM judges. Every judge you DON'T build is less work to validate and maintain.

---

## Step 6: Write Output

**What to do:** Write the final dimension list to `examples/dimensions.md` using this
exact format (the Python scripts parse it):

```markdown
# Dimensions

## LLM Judge Dimensions

### dimension_name
- Definition: one sentence defining what PASS vs FAIL means
- PASS example: a concrete excerpt from the draft that passes
- FAIL example: a concrete excerpt (real or hypothetical) that fails

### another_dimension
- Definition: ...
- PASS example: ...
- FAIL example: ...

## Cheap Checks

### check_name
- Rule: concrete assertion (e.g., "word count >= 500", "section '## Problem' exists")
- Implementation: regex / word count / section presence
```

**Important:** The `### heading` names under "## LLM Judge Dimensions" become the
dimension identifiers used by every script downstream. Use snake_case, be specific.
