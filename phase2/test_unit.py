#!/usr/bin/env python3
"""unit tests for autorefine phase 2 -- edge cases and error handling.

complements test_flow.py (happy-path integration test) by testing individual
functions in isolation with malformed inputs, missing files, and boundary cases.

run: python3 test_unit.py
"""
import json
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run import (
    compute_composite_score,
    select_weakest_dimension,
    detect_stall,
    parse_improve_md,
    parse_dimensions_md,
    run_cheap_checks,
    parse_judge_frontmatter,
    build_context_section,
    extract_constrained_sections,
    hash_sections,
    verify_constraints,
    rogan_gladen_correction,
)

PASSED = 0
FAILED = 0


def check(condition, msg):
    global PASSED, FAILED
    if condition:
        PASSED += 1
    else:
        FAILED += 1
        import traceback
        frame = traceback.extract_stack()[-2]
        print(f"  FAIL [{frame.lineno}]: {msg}")


def tmpdir():
    return Path(tempfile.mkdtemp(prefix='autorefine_unit_'))


# ============================================================================
# 1. compute_composite_score
# ============================================================================

def test_composite_score():
    print("\n--- compute_composite_score ---")

    # empty inputs
    check(compute_composite_score({}, {}) == 0.0, "empty inputs -> 0.0")

    # all pass, no weights
    r = compute_composite_score({'c1': 'PASS'}, {'d1': 'PASS', 'd2': 'PASS'})
    check(abs(r - 1.0) < 0.001, f"all PASS no weights -> 1.0, got {r}")

    # mixed with weights: cheap(1 PASS) + llm(d1 PASS w=2, d2 FAIL w=1)
    # = (1 + 2 + 0) / (1 + 2 + 1) = 0.75
    r = compute_composite_score(
        {'c1': 'PASS'},
        {'d1': 'PASS', 'd2': 'FAIL'},
        {'d1': 2.0, 'd2': 1.0}
    )
    check(abs(r - 0.75) < 0.001, f"weighted mixed -> 0.75, got {r}")

    # all fail with weights
    r = compute_composite_score(
        {'c1': 'FAIL'},
        {'d1': 'FAIL', 'd2': 'FAIL'},
        {'d1': 3.0, 'd2': 1.0}
    )
    check(r == 0.0, f"all FAIL -> 0.0, got {r}")

    # weights=None backward compat (same as equal weights)
    r1 = compute_composite_score({'c': 'PASS'}, {'d': 'FAIL'}, None)
    r2 = compute_composite_score({'c': 'PASS'}, {'d': 'FAIL'}, {})
    check(abs(r1 - r2) < 0.001, f"None weights == empty weights: {r1} vs {r2}")


# ============================================================================
# 2. select_weakest_dimension
# ============================================================================

def test_select_weakest():
    print("\n--- select_weakest_dimension ---")
    d = tmpdir()
    log_path = d / 'log.jsonl'

    # single fail
    r = select_weakest_dimension(log_path, {'a': 'FAIL', 'b': 'PASS'})
    check(r == 'a', f"single FAIL -> 'a', got {r}")

    # multiple fails, tiebreak by critique length
    r = select_weakest_dimension(
        log_path,
        {'a': 'FAIL', 'b': 'FAIL'},
        critiques={'a': 'short', 'b': 'this is a much longer critique text'}
    )
    check(r == 'b', f"tiebreak by critique length -> 'b', got {r}")

    # multiple fails, no critiques -> alphabetical
    r = select_weakest_dimension(log_path, {'z': 'FAIL', 'a': 'FAIL'})
    check(r == 'a', f"no critiques -> alphabetical 'a', got {r}")

    # all pass, no history -> alphabetical among all
    r = select_weakest_dimension(log_path, {'z': 'PASS', 'a': 'PASS'})
    check(r == 'a', f"all PASS no history -> alphabetical 'a', got {r}")

    # all pass with history -> pick most-failed
    with open(log_path, 'w') as f:
        f.write(json.dumps({'llm_scores_before': {'a': 'PASS', 'b': 'FAIL'}}) + '\n')
        f.write(json.dumps({'llm_scores_before': {'a': 'PASS', 'b': 'FAIL'}}) + '\n')
        f.write(json.dumps({'llm_scores_before': {'a': 'FAIL', 'b': 'FAIL'}}) + '\n')
    r = select_weakest_dimension(log_path, {'a': 'PASS', 'b': 'PASS'})
    check(r == 'b', f"all PASS, history -> most-failed 'b', got {r}")

    # empty scores
    r = select_weakest_dimension(log_path, {})
    check(r == 'unknown', f"empty scores -> 'unknown', got {r}")

    shutil.rmtree(d)


# ============================================================================
# 3. detect_stall
# ============================================================================

def test_detect_stall():
    print("\n--- detect_stall ---")
    d = tmpdir()
    log_path = d / 'log.jsonl'

    # no log file
    r = detect_stall(log_path)
    check(not r['stalled'], "no log -> not stalled")

    # fewer entries than threshold
    with open(log_path, 'w') as f:
        f.write(json.dumps({'composite_before': 0.5, 'decision': 'DISCARDED'}) + '\n')
    r = detect_stall(log_path, threshold=3)
    check(not r['stalled'], "1 entry < threshold 3 -> not stalled")

    # 3 consecutive discards
    with open(log_path, 'w') as f:
        for _ in range(3):
            f.write(json.dumps({'composite_before': 0.8, 'decision': 'DISCARDED'}) + '\n')
    r = detect_stall(log_path, threshold=3)
    check(r['stalled'], "3 consecutive discards -> stalled")
    check(r['consecutive_discards'] == 3, f"consecutive_discards should be 3, got {r['consecutive_discards']}")

    # 2 discards then 1 adopt -> not stalled (broken streak, varying scores to avoid plateau)
    with open(log_path, 'w') as f:
        f.write(json.dumps({'composite_before': 0.7, 'decision': 'DISCARDED'}) + '\n')
        f.write(json.dumps({'composite_before': 0.8, 'decision': 'ADOPTED'}) + '\n')
        f.write(json.dumps({'composite_before': 0.9, 'decision': 'DISCARDED'}) + '\n')
    r = detect_stall(log_path, threshold=3)
    check(not r['stalled'], "broken streak -> not stalled")
    check(r['consecutive_discards'] == 1, f"consecutive_discards should be 1, got {r['consecutive_discards']}")

    # score plateau
    with open(log_path, 'w') as f:
        for _ in range(3):
            f.write(json.dumps({'composite_before': 0.75, 'decision': 'ADOPTED'}) + '\n')
    r = detect_stall(log_path, threshold=3)
    check(r['score_plateau'], "same score 3x -> plateau")
    check(r['stalled'], "plateau -> stalled")

    # adversarial events should be skipped
    with open(log_path, 'w') as f:
        f.write(json.dumps({'composite_before': 0.8, 'decision': 'DISCARDED'}) + '\n')
        f.write(json.dumps({'event': 'adversarial', 'iteration': 5}) + '\n')
        f.write(json.dumps({'composite_before': 0.8, 'decision': 'DISCARDED'}) + '\n')
        f.write(json.dumps({'composite_before': 0.8, 'decision': 'DISCARDED'}) + '\n')
    r = detect_stall(log_path, threshold=3)
    check(r['stalled'], "3 discards with adversarial between -> stalled (adversarial skipped)")

    shutil.rmtree(d)


# ============================================================================
# 4. parse_improve_md
# ============================================================================

def test_parse_improve():
    print("\n--- parse_improve_md ---")
    d = tmpdir()

    # complete
    p = d / 'improve.md'
    p.write_text(
        '## What is being improved\nA thing.\n\n'
        '## What "good" looks like\nGood stuff.\n\n'
        '## Hard constraints\n- "## Frozen"\n\n'
        '## End-to-end question\nDoes it work?\n\n'
        '## Adversarial persona\nA mean reader.\n'
    )
    r = parse_improve_md(p)
    check(r['what_is_being_improved'] == 'A thing.', f"what: {r['what_is_being_improved']}")
    check(r['hard_constraints'] == ['## Frozen'], f"constraints: {r['hard_constraints']}")
    check(r['adversarial_persona'] == 'A mean reader.', f"persona: {r['adversarial_persona']}")
    check(r['end_to_end_question'] == 'Does it work?', f"e2e: {r['end_to_end_question']}")

    # missing adversarial persona
    p.write_text('## What is being improved\nA thing.\n')
    r = parse_improve_md(p)
    check(r['adversarial_persona'] == '', "missing persona -> empty string")

    # missing hard constraints
    p.write_text('## What is being improved\nA thing.\n')
    r = parse_improve_md(p)
    check(r['hard_constraints'] == [], "missing constraints -> empty list")

    # empty file
    p.write_text('')
    r = parse_improve_md(p)
    check(r['what_is_being_improved'] == '', "empty file -> empty string")
    check(r['adversarial_persona'] == '', "empty file -> empty persona")

    shutil.rmtree(d)


# ============================================================================
# 5. parse_dimensions_md
# ============================================================================

def test_parse_dimensions():
    print("\n--- parse_dimensions_md ---")
    d = tmpdir()

    # missing file
    r = parse_dimensions_md(d / 'nonexistent.md')
    check(r['cheap_checks'] == [], "missing file -> empty cheap_checks")
    check(r['dimensions'] == [], "missing file -> empty dimensions")

    # only cheap checks
    p = d / 'dims.md'
    p.write_text(
        '## Cheap Checks\n\n'
        '### my_check\n'
        '- Rule: search for "banana"\n\n'
    )
    r = parse_dimensions_md(p)
    check(len(r['cheap_checks']) == 1, f"1 cheap check, got {len(r['cheap_checks'])}")
    check(r['dimensions'] == [], "no llm dims")

    # both
    p.write_text(
        '## Cheap Checks\n\n'
        '### check1\n'
        '- Rule: search for "x"\n\n'
        '## LLM Judge Dimensions\n\n'
        '### dim_a\n\n'
        '### dim_b\n'
    )
    r = parse_dimensions_md(p)
    check(len(r['cheap_checks']) == 1, f"1 cheap check, got {len(r['cheap_checks'])}")
    check(len(r['dimensions']) == 2, f"2 dimensions, got {len(r['dimensions'])}")

    shutil.rmtree(d)


# ============================================================================
# 6. run_cheap_checks
# ============================================================================

def test_cheap_checks():
    print("\n--- run_cheap_checks ---")

    text = (
        "# Plan\n"
        "## Risk Register\n"
        "| Risk | Impact |\n"
        "|------|--------|\n"
        "| Rain | High |\n"
        "| Fire | Low |\n"
        "| Flood | Med |\n"
    )

    # search for - match
    r = run_cheap_checks(text, [{'name': 'has_risk', 'rule': 'search for "Risk Register"'}])
    check(r['has_risk'] == 'PASS', f"search match -> PASS, got {r['has_risk']}")

    # search for - no match
    r = run_cheap_checks(text, [{'name': 'has_banana', 'rule': 'search for "banana"'}])
    check(r['has_banana'] == 'FAIL', f"search miss -> FAIL, got {r['has_banana']}")

    # section/heading rule
    r = run_cheap_checks(text, [{'name': 'has_section', 'rule': 'section heading "Risk Register"'}])
    check(r['has_section'] == 'PASS', f"section match -> PASS, got {r['has_section']}")

    # count rows - enough
    r = run_cheap_checks(text, [{'name': 'enough_rows', 'rule': 'count rows in "Risk Register" >= 3'}])
    check(r['enough_rows'] == 'PASS', f"3 rows >= 3 -> PASS, got {r['enough_rows']}")

    # count rows - too few
    r = run_cheap_checks(text, [{'name': 'too_few', 'rule': 'count rows in "Risk Register" >= 5'}])
    check(r['too_few'] == 'FAIL', f"3 rows < 5 -> FAIL, got {r['too_few']}")

    # unknown rule type -> PASS
    r = run_cheap_checks(text, [{'name': 'mystery', 'rule': 'do something weird'}])
    check(r['mystery'] == 'PASS', f"unknown rule -> PASS, got {r['mystery']}")


# ============================================================================
# 7. parse_judge_frontmatter
# ============================================================================

def test_frontmatter():
    print("\n--- parse_judge_frontmatter ---")
    d = tmpdir()

    # complete frontmatter with weight
    p = d / 'my_dim.md'
    p.write_text(
        '---\n'
        'dimension: clarity\n'
        'test_tpr: 0.90\n'
        'test_tnr: 0.85\n'
        'weight: 2.5\n'
        '---\n\n'
        'check clarity.\n'
    )
    r = parse_judge_frontmatter(p)
    check(r['dimension'] == 'clarity', f"dimension: {r['dimension']}")
    check(abs(r['tpr'] - 0.90) < 0.001, f"tpr: {r['tpr']}")
    check(abs(r['tnr'] - 0.85) < 0.001, f"tnr: {r['tnr']}")
    check(abs(r['weight'] - 2.5) < 0.001, f"weight: {r['weight']}")

    # no frontmatter -> defaults, dimension from stem
    p = d / 'bare_dim.md'
    p.write_text('just a prompt, no frontmatter.\n')
    r = parse_judge_frontmatter(p)
    check(r['dimension'] == 'bare_dim', f"stem dimension: {r['dimension']}")
    check(abs(r['tpr'] - 0.95) < 0.001, "default tpr 0.95")
    check(abs(r['tnr'] - 0.95) < 0.001, "default tnr 0.95")
    check(abs(r['weight'] - 1.0) < 0.001, "default weight 1.0")

    # missing weight field -> 1.0
    p = d / 'no_weight.md'
    p.write_text(
        '---\n'
        'dimension: foo\n'
        'test_tpr: 1.00\n'
        'test_tnr: 1.00\n'
        '---\n\n'
        'prompt\n'
    )
    r = parse_judge_frontmatter(p)
    check(abs(r['weight'] - 1.0) < 0.001, "missing weight -> 1.0")

    shutil.rmtree(d)


# ============================================================================
# 8. build_context_section
# ============================================================================

def test_context_section():
    print("\n--- build_context_section ---")
    d = tmpdir()

    # None -> empty
    check(build_context_section(None) == '', "None -> empty")

    # nonexistent -> empty (no crash)
    check(build_context_section(d / 'nope.md') == '', "nonexistent -> empty")

    # valid file
    p = d / 'ctx.md'
    p.write_text('fact: lemons are yellow\n')
    r = build_context_section(p)
    check('GROUND TRUTH' in r, "valid file -> has GROUND TRUTH")
    check('lemons are yellow' in r, "valid file -> has content")

    shutil.rmtree(d)


# ============================================================================
# 9. constraint handling
# ============================================================================

def test_constraints():
    print("\n--- constraint handling ---")
    d = tmpdir()

    artifact = (
        "# Plan\n\nSome content.\n\n"
        "## Frozen Section\n\nDo not change.\n"
    )
    headings = ['## Frozen Section']

    # extract + hash
    sections = extract_constrained_sections(artifact, headings)
    check('## Frozen Section' in sections, "section extracted")
    hashes = hash_sections(sections)
    check(len(hashes) == 1, "1 hash computed")

    # no violation
    artifact_path = d / 'artifact.md'
    artifact_path.write_text(artifact)
    hashes_path = d / 'hashes.json'
    hashes_path.write_text(json.dumps(hashes))
    ok, violations = verify_constraints(artifact_path, hashes_path, headings)
    check(ok, "unmodified -> no violation")
    check(violations == [], "no violations list empty")

    # violation (modified section)
    artifact_path.write_text(
        "# Plan\n\nSome content.\n\n"
        "## Frozen Section\n\nI CHANGED THIS.\n"
    )
    ok, violations = verify_constraints(artifact_path, hashes_path, headings)
    check(not ok, "modified -> violation detected")
    check(len(violations) == 1, f"1 violation, got {len(violations)}")

    # missing hashes file -> no violation (graceful)
    ok, violations = verify_constraints(artifact_path, d / 'nope.json', headings)
    check(ok, "missing hashes file -> no violation")

    shutil.rmtree(d)


# ============================================================================
# 10. mutation response parsing
# ============================================================================

def test_mutation_parsing():
    print("\n--- mutation response parsing ---")
    import re

    # with delimiters
    text = (
        "---ARTIFACT START---\nnew content here\n---ARTIFACT END---\n"
        "---RATIONALE---\nfixed the thing\n"
    )
    m = re.search(r'---ARTIFACT START---\s*\n(.*?)\n\s*---ARTIFACT END---', text, re.DOTALL)
    check(m is not None, "delimiters found")
    check(m.group(1).strip() == 'new content here', f"artifact: {m.group(1).strip()}")
    rm = re.search(r'---RATIONALE---\s*\n(.+)', text, re.DOTALL)
    check(rm.group(1).strip() == 'fixed the thing', "rationale extracted")

    # without delimiters -> full text
    text2 = "just some text without any delimiters"
    m2 = re.search(r'---ARTIFACT START---\s*\n(.*?)\n\s*---ARTIFACT END---', text2, re.DOTALL)
    check(m2 is None, "no delimiters -> no match")

    # missing rationale
    text3 = "---ARTIFACT START---\ncontent\n---ARTIFACT END---\n"
    rm3 = re.search(r'---RATIONALE---\s*\n(.+)', text3, re.DOTALL)
    check(rm3 is None, "no rationale section -> None")


# ============================================================================
# 11. rogan_gladen_correction
# ============================================================================

def test_rogan_gladen():
    print("\n--- rogan_gladen_correction ---")

    # perfect judge (tpr=1, tnr=1) -> no correction
    r = rogan_gladen_correction(0.8, 1.0, 1.0)
    check(abs(r - 0.8) < 0.001, f"perfect judge -> same: {r}")

    # degenerate judge (tpr + tnr <= 1) -> return observed
    r = rogan_gladen_correction(0.6, 0.5, 0.4)
    check(abs(r - 0.6) < 0.001, f"degenerate judge -> observed: {r}")

    # correction clamps to [0, 1]
    r = rogan_gladen_correction(0.0, 0.8, 0.8)
    check(r >= 0.0, f"clamped >= 0: {r}")
    r = rogan_gladen_correction(1.0, 0.8, 0.8)
    check(r <= 1.0, f"clamped <= 1: {r}")


# ============================================================================
# run all
# ============================================================================

if __name__ == '__main__':
    test_composite_score()
    test_select_weakest()
    test_detect_stall()
    test_parse_improve()
    test_parse_dimensions()
    test_cheap_checks()
    test_frontmatter()
    test_context_section()
    test_constraints()
    test_mutation_parsing()
    test_rogan_gladen()

    print(f"\n{'='*60}")
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({PASSED} assertions)")
    else:
        print(f"FAILED: {FAILED} failures, {PASSED} passed")
        sys.exit(1)
