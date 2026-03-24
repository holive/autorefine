#!/usr/bin/env python3
"""
phase2/run.py -- autonomous improvement loop (data-only, no LLM calls)

claude code handles all LLM work (judging + mutation) via SKILL.md.
this script handles: init, cheap checks, scoring, keep/discard, logging, reporting.

modes:
  --init ARTIFACT --improve PATH           setup constraint hashes + baseline
  --score-before --artifact PATH           run cheap checks, write judge request
  --score-after                            read judge results, compute composite,
                                           select weakest, write mutation request
  --apply-mutation                         read mutation response, write judge
                                           request for mutated version
  --verdict                                read after-judge results, keep/discard, log
  --report PATH                            morning report from log
  --review-checkpoints PATH --improve PATH review queued checkpoints
"""

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt
from rich import box

console = Console()

RUNS_DIR = Path('runs')
ITER_STATE = RUNS_DIR / 'current_iteration.json'


# ============================================================================
# constraint hashing
# ============================================================================

def extract_constrained_sections(artifact_text: str, constraint_headings: List[str]) -> Dict[str, str]:
    """extract sections matching constraint headings from artifact text"""
    sections = {}
    for heading in constraint_headings:
        pattern = rf'^{re.escape(heading)}$.*?(?=^#+ |\Z)'
        match = re.search(pattern, artifact_text, re.MULTILINE | re.DOTALL)
        if match:
            sections[heading] = match.group(0).strip()
        else:
            console.print(f"[yellow]warning: constrained heading '{heading}' not found[/yellow]")
    return sections


def hash_sections(sections: Dict[str, str]) -> Dict[str, str]:
    """compute sha-256 hashes for each section"""
    return {h: hashlib.sha256(c.encode('utf-8')).hexdigest() for h, c in sections.items()}


def verify_constraints(artifact_path: Path, constraint_hashes_path: Path,
                       constraint_headings: List[str]) -> Tuple[bool, List[str]]:
    """verify constrained sections haven't changed"""
    if not constraint_hashes_path.exists():
        return True, []

    with open(constraint_hashes_path) as f:
        expected_hashes = json.load(f)

    artifact_text = artifact_path.read_text()
    current_sections = extract_constrained_sections(artifact_text, constraint_headings)
    current_hashes = hash_sections(current_sections)

    violations = [h for h, expected in expected_hashes.items() if current_hashes.get(h) != expected]
    return len(violations) == 0, violations


# ============================================================================
# improve.md / dimensions.md parsing
# ============================================================================

def parse_improve_md(improve_path: Path) -> Dict[str, Any]:
    """parse improve.md for rubric, constraints, and end-to-end question"""
    content = improve_path.read_text()
    sections = {}
    current_section = None
    current_content = []

    for line in content.split('\n'):
        if line.startswith('## '):
            if current_section:
                sections[current_section] = '\n'.join(current_content).strip()
            current_section = line[3:].strip()
            current_content = []
        else:
            current_content.append(line)

    if current_section:
        sections[current_section] = '\n'.join(current_content).strip()

    constraint_headings = []
    if 'Hard constraints' in sections:
        for line in sections['Hard constraints'].split('\n'):
            if line.strip().startswith('-') and '#' in line:
                match = re.search(r'"([^"]+)"', line)
                if match:
                    constraint_headings.append(match.group(1))

    return {
        'what_is_being_improved': sections.get('What is being improved', ''),
        'what_good_looks_like': sections.get('What "good" looks like', ''),
        'hard_constraints': constraint_headings,
        'end_to_end_question': sections.get('End-to-end question', '').strip(),
        'adversarial_persona': sections.get('Adversarial persona', '').strip(),
    }


def parse_dimensions_md(dimensions_path: Path) -> Dict[str, Any]:
    """parse dimensions.md for cheap checks and dimension names"""
    if not dimensions_path.exists():
        return {'cheap_checks': [], 'dimensions': []}

    content = dimensions_path.read_text()
    cheap_checks = []
    dimensions = []

    # parse cheap checks
    cheap_match = re.search(r'^## Cheap Checks\s*$(.+?)(?=^## |\Z)', content, re.MULTILINE | re.DOTALL)
    if cheap_match:
        for m in re.finditer(r'### (\w+)\n- Rule: (.+?)(?=\n### |\Z)', cheap_match.group(1), re.DOTALL):
            cheap_checks.append({'name': m.group(1).strip(), 'rule': m.group(2).strip()})

    # parse LLM judge dimensions
    judge_match = re.search(r'^## LLM Judge Dimensions\s*$(.+?)(?=^## |\Z)', content, re.MULTILINE | re.DOTALL)
    if judge_match:
        for m in re.finditer(r'^### (\w+)', judge_match.group(1), re.MULTILINE):
            dimensions.append(m.group(1))

    return {'cheap_checks': cheap_checks, 'dimensions': dimensions}


# ============================================================================
# cheap check runner
# ============================================================================

def run_cheap_checks(artifact_text: str, cheap_checks: List[Dict]) -> Dict[str, str]:
    """run deterministic cheap checks. returns {name: PASS|FAIL}"""
    results = {}
    text_lower = artifact_text.lower()

    for check in cheap_checks:
        name = check['name']
        rule = check.get('rule', check.get('description', '')).lower()

        if 'search for' in rule or 'must mention' in rule:
            # extract terms from quoted strings or OR-separated patterns
            terms = re.findall(r'"([^"]+)"', rule)
            if terms:
                results[name] = "PASS" if any(t.lower() in text_lower for t in terms) else "FAIL"
            else:
                results[name] = "PASS"

        elif 'section' in rule or 'heading' in rule:
            heading_match = re.search(r'"([^"]+)"', rule)
            if heading_match:
                results[name] = "PASS" if heading_match.group(1).lower() in text_lower else "FAIL"
            else:
                results[name] = "PASS"

        elif 'count' in rule and 'rows' in rule:
            # table row counting (e.g., risk register >= 5)
            threshold_match = re.search(r'>= (\d+)', rule)
            heading_match = re.search(r'"([^"]+)"', rule)
            if threshold_match and heading_match:
                threshold = int(threshold_match.group(1))
                heading = heading_match.group(1)
                # find section and count table rows
                section_match = re.search(
                    rf'{re.escape(heading)}.*?\n(\|.+?\n)+',
                    artifact_text, re.IGNORECASE | re.DOTALL
                )
                if section_match:
                    rows = len([l for l in section_match.group(0).split('\n')
                               if l.strip().startswith('|') and '---' not in l]) - 1  # minus header
                    results[name] = "PASS" if rows >= threshold else "FAIL"
                else:
                    results[name] = "FAIL"
            else:
                results[name] = "PASS"
        else:
            results[name] = "PASS"

    return results


# ============================================================================
# composite scoring + weakest dimension
# ============================================================================

def compute_composite_score(cheap_results: Dict[str, str], llm_scores: Dict[str, str],
                            llm_weights: Optional[Dict[str, float]] = None) -> float:
    """composite = weighted_pass / weighted_total. cheap checks weight 1.0."""
    if llm_weights is None:
        llm_weights = {}
    cheap_pass = sum(1.0 for v in cheap_results.values() if v == "PASS")
    cheap_total = float(len(cheap_results))
    llm_pass = sum(llm_weights.get(d, 1.0) for d, v in llm_scores.items() if v == "PASS")
    llm_total = sum(llm_weights.get(d, 1.0) for d in llm_scores)
    total = cheap_total + llm_total
    return (cheap_pass + llm_pass) / total if total > 0 else 0.0


def select_weakest_dimension(log_path: Path, llm_scores: Dict[str, str],
                            critiques: Optional[Dict[str, str]] = None) -> str:
    """select weakest dimension, prioritizing currently-failing over historically-failing"""
    # currently-failing dimensions always take priority
    current_fails = [d for d, v in llm_scores.items() if v == "FAIL"]
    if len(current_fails) == 1:
        return current_fails[0]

    # if multiple current fails (or none), use history to break ties
    fail_counts = {}
    if log_path.exists():
        with open(log_path) as f:
            entries = [json.loads(l) for l in f if l.strip()]
        for entry in entries[-3:]:
            for dim, verdict in entry.get('llm_scores_before', {}).items():
                if verdict == "FAIL":
                    fail_counts[dim] = fail_counts.get(dim, 0) + 1

    # scope candidates to current fails if any exist
    candidates = current_fails if current_fails else list(llm_scores.keys())

    if not candidates:
        return 'unknown'

    if len(candidates) == 1:
        return candidates[0]

    # pick candidate with most historical fails
    ranked = sorted(candidates, key=lambda d: fail_counts.get(d, 0), reverse=True)
    top_count = fail_counts.get(ranked[0], 0)
    tied = [d for d in ranked if fail_counts.get(d, 0) == top_count]

    if len(tied) == 1:
        return tied[0]

    if critiques:
        return max(tied, key=lambda d: len(critiques.get(d, '')))
    return sorted(tied)[0]


# ============================================================================
# stall detection
# ============================================================================

def detect_stall(log_path: Path, threshold: int = 3) -> Dict[str, Any]:
    """detect writing ceiling via consecutive discards or score plateau"""
    result = {'consecutive_discards': 0, 'score_plateau': False, 'stalled': False}
    if not log_path.exists():
        return result

    with open(log_path) as f:
        entries = [json.loads(l) for l in f if l.strip()]

    iterations = [e for e in entries if 'composite_before' in e]
    if len(iterations) < threshold:
        return result

    recent = iterations[-threshold:]

    # consecutive discards from end
    for e in reversed(recent):
        if e.get('decision') == 'DISCARDED':
            result['consecutive_discards'] += 1
        else:
            break

    # score plateau
    scores = [e.get('composite_before', -1) for e in recent]
    result['score_plateau'] = len(set(scores)) == 1 and scores[0] >= 0

    # all-pass ceiling: all recent iterations have composite_before == 1.0 and were discarded
    all_at_ceiling = all(
        e.get('composite_before', 0) >= 1.0 and e.get('decision') == 'DISCARDED'
        for e in recent
    )
    result['all_pass_ceiling'] = all_at_ceiling

    result['stalled'] = (
        result['consecutive_discards'] >= threshold
        or result['score_plateau']
        or result['all_pass_ceiling']
    )
    return result


# ============================================================================
# judge prompt loading
# ============================================================================

def parse_judge_frontmatter(judge_path: Path) -> Dict[str, Any]:
    """parse judge prompt file for frontmatter"""
    content = judge_path.read_text()
    meta = {'dimension': judge_path.stem, 'tpr': 0.95, 'tnr': 0.95, 'weight': 1.0}

    for line in content.split('\n')[:20]:
        line = line.strip()
        if line.startswith('dimension:'):
            meta['dimension'] = line.split(':', 1)[1].strip()
        elif line.startswith('test_tpr:') or line.startswith('tpr:'):
            meta['tpr'] = float(line.split(':', 1)[1].strip())
        elif line.startswith('test_tnr:') or line.startswith('tnr:'):
            meta['tnr'] = float(line.split(':', 1)[1].strip())
        elif line.startswith('weight:'):
            meta['weight'] = float(line.split(':', 1)[1].strip())

    return meta


def load_judge_prompt(judge_path: Path) -> str:
    """load judge prompt, stripping frontmatter"""
    content = judge_path.read_text()
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            content = content[end + 3:].lstrip("\n")
    return content


# ============================================================================
# context file (ground truth)
# ============================================================================

def build_context_section(context_path: Optional[Path]) -> str:
    """build ground truth section for prompt injection"""
    if context_path and context_path.exists():
        return f"\n---\n\nGROUND TRUTH (verified facts -- use to verify claims):\n\n{context_path.read_text()}\n\n"
    return ""


# ============================================================================
# iteration state management
# ============================================================================

def save_state(state: Dict):
    """save current iteration state"""
    RUNS_DIR.mkdir(exist_ok=True)
    ITER_STATE.write_text(json.dumps(state, indent=2))


def load_state() -> Dict:
    """load current iteration state"""
    if not ITER_STATE.exists():
        return {}
    return json.loads(ITER_STATE.read_text())


def log_entry(entry: Dict):
    """append entry to log"""
    log_path = RUNS_DIR / 'log.jsonl'
    with open(log_path, 'a') as f:
        f.write(json.dumps(entry) + "\n")


# ============================================================================
# modes
# ============================================================================

def init_mode(artifact_path: Path, improve_path: Path):
    """initialize constraint hashes and baseline"""
    console.print(Panel.fit("initializing phase 2", style="bold blue"))

    improve_config = parse_improve_md(improve_path)
    artifact_text = artifact_path.read_text()

    sections = extract_constrained_sections(artifact_text, improve_config['hard_constraints'])
    hashes = hash_sections(sections)

    RUNS_DIR.mkdir(exist_ok=True)
    hashes_path = RUNS_DIR / 'constraint_hashes.json'
    hashes_path.write_text(json.dumps(hashes, indent=2))

    approved_path = RUNS_DIR / 'approved.md'
    best_path = RUNS_DIR / 'best.md'
    shutil.copy(artifact_path, approved_path)
    shutil.copy(artifact_path, best_path)

    # save initial state
    save_state({'iteration': 0, 'phase': 'initialized'})

    console.print(f"[green]constraint hashes: {hashes_path}[/green]")
    console.print(f"[green]baseline copied to {approved_path} and {best_path}[/green]")
    console.print("[bold green]initialization complete![/bold green]")


def score_before_mode(artifact_path: Path, dimensions_path: Path, judge_dir: Path,
                      improve_path: Path, context_path: Optional[Path] = None,
                      force_rejudge: bool = False):
    """run cheap checks, write judge request for claude code"""
    state = load_state()
    if force_rejudge:
        state['force_rejudge'] = True
    iteration = state.get('iteration', 0) + 1

    artifact_text = artifact_path.read_text()

    # verify constraints
    improve_config = parse_improve_md(improve_path)
    constraint_hashes_path = RUNS_DIR / 'constraint_hashes.json'
    is_valid, violations = verify_constraints(artifact_path, constraint_hashes_path,
                                              improve_config['hard_constraints'])
    if not is_valid:
        console.print(f"[bold red]constraint violation: {violations}[/bold red]")
        approved_path = RUNS_DIR / 'approved.md'
        shutil.copy(approved_path, artifact_path)
        console.print("[yellow]reverted to approved version[/yellow]")
        return

    # run cheap checks
    dims_config = parse_dimensions_md(dimensions_path)
    cheap_results = run_cheap_checks(artifact_text, dims_config['cheap_checks'])

    console.print(f"[cyan]cheap checks:[/cyan]")
    for name, result in cheap_results.items():
        color = "green" if result == "PASS" else "red"
        console.print(f"  [{color}]{name}: {result}[/{color}]")

    # write judge request for each dimension
    iter_dir = RUNS_DIR / f'iter_{iteration:03d}'
    iter_dir.mkdir(parents=True, exist_ok=True)

    judge_files = sorted(judge_dir.glob('*.md'))
    # filter out batch/results files
    judge_files = [f for f in judge_files if not any(s in f.name for s in ['_batch', '_results', '_truth', '_split'])]

    # check for cached scores (reduces judge variance between iterations)
    cached = state.get('cached_scores')
    last_decision = state.get('last_decision', '')
    force_rejudge = state.get('force_rejudge', False)
    use_cache = (
        cached is not None
        and last_decision == 'DISCARDED'
        and not force_rejudge
        and not state.get('adversarial_findings')
    )

    # build dimension weights from judge files
    dimension_weights = {}
    for jf in judge_files:
        meta = parse_judge_frontmatter(jf)
        dimension_weights[meta['dimension']] = meta.get('weight', 1.0)

    if use_cache:
        # use cached scores -- write them directly as judge_response.jsonl
        console.print(f"\n[dim]using cached scores from iteration {cached.get('iteration', '?')} (artifact unchanged since last adoption)[/dim]")
        response_path = iter_dir / 'judge_response.jsonl'
        with open(response_path, 'w') as f:
            for dim, verdict in cached.get('scores', {}).items():
                f.write(json.dumps({'dimension': dim, 'verdict': verdict}) + '\n')
        prompt_files = []
    else:
        # normal flow: write judge prompts for all dimensions
        context_section = build_context_section(context_path)

        requests = []
        for jf in judge_files:
            prompt = load_judge_prompt(jf)
            meta = parse_judge_frontmatter(jf)
            full_prompt = f"{prompt}\n{context_section}\n---\n\nARTIFACT TO EVALUATE:\n\n{artifact_text}\n\n---\n\nyour verdict (PASS or FAIL) and brief critique:"
            requests.append({
                'dimension': meta['dimension'],
                'prompt': full_prompt,
            })

        request_path = iter_dir / 'judge_request.jsonl'
        with open(request_path, 'w') as f:
            for r in requests:
                f.write(json.dumps(r) + "\n")

        # write per-dimension prompt files for agent access
        prompt_files = []
        for r in requests:
            prompt_path = iter_dir / f'judge_prompt_{r["dimension"]}.md'
            prompt_path.write_text(r['prompt'])
            prompt_files.append(prompt_path)

    # save state (carry forward adversarial_findings if present)
    new_state = {
        'iteration': iteration,
        'phase': 'awaiting_judge_before',
        'artifact_path': str(artifact_path),
        'cheap_results': cheap_results,
        'artifact_snapshot': artifact_text,
        'dimension_weights': dimension_weights,
        'context_path': str(context_path) if context_path else None,
        'used_cache': use_cache,
    }
    prev_findings = state.get('adversarial_findings', '')
    if prev_findings:
        new_state['adversarial_findings'] = prev_findings
    # preserve cached_scores through iterations
    if cached and not force_rejudge:
        new_state['cached_scores'] = cached
    save_state(new_state)

    console.print(f"\n[bold]iteration {iteration}[/bold]")
    if use_cache:
        console.print(f"[green]cached judge scores written to {iter_dir / 'judge_response.jsonl'}[/green]")
        console.print(f"\n[bold]next:[/bold] run score-after (no judging needed -- cached scores used)")
    else:
        console.print(f"[green]judge request written to {iter_dir / 'judge_request.jsonl'} ({len(judge_files)} dimensions)[/green]")
        console.print(f"\n[bold]next:[/bold] launch one agent per dimension to judge in parallel.")
        console.print(f"  each agent reads its prompt file and returns a PASS/FAIL verdict.")
        console.print(f"  prompt files:")
        for pf in prompt_files:
            console.print(f"    {pf}")
        console.print(f"\n  collect verdicts into: {iter_dir / 'judge_response.jsonl'}")
        console.print(f'  format: {{"dimension": "name", "verdict": "PASS: reason"}}')


def score_after_mode(judge_dir: Path, improve_path: Path):
    """read judge results, compute score, select weakest, write mutation request"""
    state = load_state()
    iteration = state['iteration']
    iter_dir = RUNS_DIR / f'iter_{iteration:03d}'

    # read judge responses
    response_path = iter_dir / 'judge_response.jsonl'
    if not response_path.exists():
        console.print(f"[red]judge response not found: {response_path}[/red]")
        sys.exit(1)

    responses = []
    with open(response_path) as f:
        for line in f:
            if line.strip():
                responses.append(json.loads(line))

    # build scores + critiques
    llm_scores = {}
    critiques = {}
    for r in responses:
        verdict_text = r.get('verdict', 'FAIL')
        is_pass = verdict_text.strip().upper().startswith('PASS')
        llm_scores[r['dimension']] = "PASS" if is_pass else "FAIL"
        critiques[r['dimension']] = verdict_text

    cheap_results = state.get('cheap_results', {})
    dimension_weights = state.get('dimension_weights', {})
    composite = compute_composite_score(cheap_results, llm_scores, dimension_weights)

    console.print(f"\n[bold]iteration {iteration} -- before-mutation scores[/bold]")
    console.print(f"composite: {composite:.2f}")
    for dim, verdict in llm_scores.items():
        color = "green" if verdict == "PASS" else "red"
        console.print(f"  [{color}]{dim}: {verdict}[/{color}]")

    # select weakest dimension
    log_path = RUNS_DIR / 'log.jsonl'
    targeted = select_weakest_dimension(log_path, llm_scores, critiques)
    console.print(f"\n[cyan]targeting: {targeted}[/cyan]")
    if targeted in critiques:
        console.print(f"[dim]critique: {critiques[targeted][:200]}[/dim]")

    # write mutation request
    improve_config = parse_improve_md(improve_path)
    artifact_text = state['artifact_snapshot']

    # build optional sections
    context_path_str = state.get('context_path')
    context_path = Path(context_path_str) if context_path_str else None
    context_section = build_context_section(context_path)

    adversarial_findings = state.get('adversarial_findings', '')
    adv_section = f"\n---\n\nADVERSARIAL FINDINGS (from hostile reader analysis):\n\n{adversarial_findings}\n\n" if adversarial_findings else ""

    context_instruction = "\nground truth facts in the GROUND TRUTH section must not be contradicted." if context_section else ""
    adv_instruction = "\nconsider the adversarial findings when making improvements." if adv_section else ""

    mutation_request = f"""You are refining a document. Make ONE targeted change to improve a specific dimension.

CURRENT ARTIFACT:
{artifact_text}

---

RUBRIC (what "good" looks like):
{improve_config['what_good_looks_like']}

---

HARD CONSTRAINTS (never modify these sections):
{chr(10).join('- ' + h for h in improve_config['hard_constraints'])}
{context_section}{adv_section}
---

TARGETED DIMENSION: {targeted}

JUDGE CRITIQUE:
{critiques.get(targeted, 'no specific critique')}

---

TASK:
make one targeted change to improve "{targeted}". do not modify any hard-constrained sections.{context_instruction}{adv_instruction}

INVENTED DATA RULE:
if you invent specific information not present in the current artifact (names, numbers, dates, teams, metrics), wrap it in: [PLACEHOLDER: what real data is needed]
examples:
  - "[PLACEHOLDER: actual platform engineering lead name]"
  - "[PLACEHOLDER: real Q4 incident count from PagerDuty]"
  - "[PLACEHOLDER: confirmed sprint planning date]"
do NOT tag information already in the artifact, from GROUND TRUTH, or generic structural text.

output format (use these exact delimiters):
---ARTIFACT START---
[the full revised artifact]
---ARTIFACT END---
---RATIONALE---
[one sentence explaining what changed]"""

    mutation_path = iter_dir / 'mutation_request.md'
    mutation_path.write_text(mutation_request)

    # update state
    state.update({
        'phase': 'awaiting_mutation',
        'composite_before': composite,
        'llm_scores_before': llm_scores,
        'critiques': critiques,
        'targeted_dimension': targeted,
    })
    save_state(state)

    console.print(f"\n[green]mutation request written to {mutation_path}[/green]")
    console.print(f"\n[bold]next:[/bold] claude code should read the request, mutate the artifact, and write:")
    console.print(f"  {iter_dir / 'mutation_response.md'}")


def apply_mutation_mode(artifact_path: Path, dimensions_path: Path, judge_dir: Path,
                        context_path: Optional[Path] = None):
    """read mutation response, apply to artifact, write judge request for after-scoring"""
    state = load_state()
    iteration = state['iteration']
    iter_dir = RUNS_DIR / f'iter_{iteration:03d}'

    response_path = iter_dir / 'mutation_response.md'
    if not response_path.exists():
        console.print(f"[red]mutation response not found: {response_path}[/red]")
        sys.exit(1)

    response_text = response_path.read_text()

    # extract artifact using delimiters
    artifact_match = re.search(
        r'---ARTIFACT START---\s*\n(.*?)\n\s*---ARTIFACT END---',
        response_text, re.DOTALL
    )
    rationale_match = re.search(
        r'---RATIONALE---\s*\n(.+)',
        response_text, re.DOTALL
    )

    if artifact_match:
        new_artifact = artifact_match.group(1).strip()
        rationale = rationale_match.group(1).strip() if rationale_match else "mutation applied"
    else:
        console.print("[yellow]no delimiters found in mutation response, using full text[/yellow]")
        new_artifact = response_text.strip()
        rationale = "mutation applied (no delimiters)"

    console.print(f"[green]mutation applied: {rationale}[/green]")

    # write mutated artifact
    artifact_path.write_text(new_artifact)

    # run cheap checks on new version
    dims_config = parse_dimensions_md(dimensions_path)
    cheap_results_after = run_cheap_checks(new_artifact, dims_config['cheap_checks'])

    # write judge request for targeted dimension only (reduces variance on non-targeted dims)
    targeted = state.get('targeted_dimension', '')
    judge_files = sorted(judge_dir.glob('*.md'))
    judge_files = [f for f in judge_files if not any(s in f.name for s in ['_batch', '_results', '_truth', '_split'])]

    # resolve context path from arg or state
    if not context_path:
        context_path_str = state.get('context_path')
        context_path = Path(context_path_str) if context_path_str else None
    context_section = build_context_section(context_path)

    requests = []
    for jf in judge_files:
        meta = parse_judge_frontmatter(jf)
        if meta['dimension'] != targeted:
            continue
        prompt = load_judge_prompt(jf)
        full_prompt = f"{prompt}\n{context_section}\n---\n\nARTIFACT TO EVALUATE:\n\n{new_artifact}\n\n---\n\nyour verdict (PASS or FAIL) and brief critique:"
        requests.append({
            'dimension': meta['dimension'],
            'prompt': full_prompt,
        })

    request_path = iter_dir / 'judge_request_after.jsonl'
    with open(request_path, 'w') as f:
        for r in requests:
            f.write(json.dumps(r) + "\n")

    # write per-dimension prompt files for agent access
    prompt_files = []
    for r in requests:
        prompt_path = iter_dir / f'judge_after_prompt_{r["dimension"]}.md'
        prompt_path.write_text(r['prompt'])
        prompt_files.append(prompt_path)

    state.update({
        'phase': 'awaiting_judge_after',
        'mutation_rationale': rationale,
        'new_artifact': new_artifact,
        'cheap_results_after': cheap_results_after,
    })
    save_state(state)

    console.print(f"[green]after-judge request: targeted dimension only ({targeted})[/green]")
    console.print(f"\n[bold]next:[/bold] launch one agent to re-judge the targeted dimension.")
    console.print(f"  prompt file:")
    for pf in prompt_files:
        console.print(f"    {pf}")
    console.print(f"\n  collect verdict into: {iter_dir / 'judge_response_after.jsonl'}")
    console.print(f"  [dim]non-targeted dimensions carry forward their before-scores[/dim]")


def verdict_mode(artifact_path: Path, stall_threshold: int = 3, adversarial_interval: int = 5):
    """read after-judge results, compare, keep or discard, log"""
    state = load_state()
    iteration = state['iteration']
    iter_dir = RUNS_DIR / f'iter_{iteration:03d}'

    response_path = iter_dir / 'judge_response_after.jsonl'
    if not response_path.exists():
        console.print(f"[red]after-judge response not found: {response_path}[/red]")
        sys.exit(1)

    responses = []
    with open(response_path) as f:
        for line in f:
            if line.strip():
                responses.append(json.loads(line))

    # start with before-scores, then overlay re-judged dimensions
    llm_scores_after = dict(state.get('llm_scores_before', {}))
    for r in responses:
        verdict_text = r.get('verdict', 'FAIL')
        is_pass = verdict_text.strip().upper().startswith('PASS')
        llm_scores_after[r['dimension']] = "PASS" if is_pass else "FAIL"

    cheap_results_after = state.get('cheap_results_after', {})
    dimension_weights = state.get('dimension_weights', {})
    composite_after = compute_composite_score(cheap_results_after, llm_scores_after, dimension_weights)
    composite_before = state['composite_before']
    delta = composite_after - composite_before

    # keep or discard (strict: must be strictly better)
    if composite_after > composite_before:
        decision = "ADOPTED"
        best_path = RUNS_DIR / 'best.md'
        shutil.copy(artifact_path, best_path)
        console.print(f"[bold green]ADOPTED[/bold green] ({composite_before:.2f} -> {composite_after:.2f}, +{delta:.2f})")
    else:
        decision = "DISCARDED"
        # restore original
        artifact_path.write_text(state['artifact_snapshot'])
        console.print(f"[bold red]DISCARDED[/bold red] ({composite_before:.2f} -> {composite_after:.2f}, {delta:.2f})")

    # log
    entry = {
        'iteration': iteration,
        'timestamp': datetime.now().isoformat(),
        'composite_before': composite_before,
        'composite_after': composite_after,
        'targeted_dimension': state.get('targeted_dimension', ''),
        'cheap_checks': cheap_results_after,
        'llm_scores_before': state.get('llm_scores_before', {}),
        'llm_scores_after': llm_scores_after,
        'mutation_rationale': state.get('mutation_rationale', ''),
        'decision': decision,
    }
    log_entry(entry)

    # show dimension results
    for dim, verdict in llm_scores_after.items():
        before = state.get('llm_scores_before', {}).get(dim, '?')
        color = "green" if verdict == "PASS" else "red"
        console.print(f"  {dim}: {before} -> [{color}]{verdict}[/{color}]")

    # update state for next iteration (with score cache for variance reduction)
    next_state = {'iteration': iteration, 'phase': 'completed', 'last_decision': decision}
    if decision == 'ADOPTED':
        # cache the after-scores as the new baseline (artifact changed)
        # rebuild full verdict strings from judge_response_after + carried scores
        cached_verdicts = {}
        for dim, verdict in state.get('llm_scores_before', {}).items():
            cached_verdicts[dim] = f"{verdict}: carried from before-scores"
        for r in responses:
            cached_verdicts[r['dimension']] = r.get('verdict', 'FAIL')
        next_state['cached_scores'] = {
            'iteration': iteration,
            'scores': cached_verdicts,
        }
    else:
        # preserve existing cache (artifact unchanged)
        if state.get('cached_scores'):
            next_state['cached_scores'] = state['cached_scores']
    save_state(next_state)

    console.print(f"\n[dim]logged to {RUNS_DIR / 'log.jsonl'}[/dim]")

    # stall detection
    log_path = RUNS_DIR / 'log.jsonl'
    stall = detect_stall(log_path, stall_threshold)
    if stall['stalled']:
        console.print()
        if stall['consecutive_discards'] >= stall_threshold:
            console.print(Panel(
                f"possible writing ceiling: {stall['consecutive_discards']} consecutive discards\n"
                "  - add new context or run adversarial pass\n"
                "  - review judge prompts for unrealistic expectations\n"
                "  - consider stopping the loop",
                title="STALL DETECTED", border_style="yellow"
            ))
        if stall['score_plateau']:
            console.print(Panel(
                f"composite unchanged for {stall_threshold} iterations\n"
                "  - add new dimensions or increase judge difficulty\n"
                "  - run human checkpoint review",
                title="SCORE PLATEAU", border_style="yellow"
            ))

    # ceiling detection: all dimensions PASS + mutation discarded
    all_pass = all(v == "PASS" for v in llm_scores_after.values())
    if all_pass and decision == "DISCARDED":
        ceiling_lines = ["all dimensions are passing and mutation was discarded."]
        # surface adversarial findings if available
        adv_log_path = RUNS_DIR / 'adversarial_log.jsonl'
        if adv_log_path.exists():
            adv_entries = []
            with open(adv_log_path) as f:
                for line in f:
                    if line.strip():
                        adv_entries.append(json.loads(line))
            if adv_entries:
                latest = adv_entries[-1]
                ceiling_lines.append(f"\nadversarial findings (iter {latest['iteration']}) suggest gaps:")
                ceiling_lines.append(f"  {latest['summary'][:200]}")
        ceiling_lines.append("\nconsider: add new dimensions from adversarial findings, or stop the loop.")
        console.print(Panel(
            "\n".join(ceiling_lines),
            title="CEILING REACHED -- ALL DIMENSIONS PASSING",
            border_style="bright_yellow",
        ))

    # adversarial pass signal
    if adversarial_interval > 0 and iteration > 0 and iteration % adversarial_interval == 0:
        console.print(Panel(
            f"iteration {iteration} is a multiple of {adversarial_interval}\n"
            f"run: python3 $SKILL_DIR/phase2/run.py adversarial --artifact {artifact_path} --improve improve.md",
            title="ADVERSARIAL PASS DUE", border_style="cyan"
        ))


# ============================================================================
# adversarial analysis
# ============================================================================

def adversarial_mode(artifact_path: Path, improve_path: Path, interval: int = 5):
    """write adversarial analysis request for hostile-reader review"""
    state = load_state()
    iteration = state.get('iteration', 0)

    if iteration == 0:
        console.print("[yellow]no iterations completed yet[/yellow]")
        return

    improve_config = parse_improve_md(improve_path)
    artifact_text = artifact_path.read_text()

    persona = improve_config.get('adversarial_persona', '')
    if not persona:
        persona = "a skeptical reader who scrutinizes claims and looks for gaps"
        console.print("[yellow]no adversarial persona in improve.md, using default[/yellow]")

    iter_dir = RUNS_DIR / f'iter_{iteration:03d}'
    iter_dir.mkdir(parents=True, exist_ok=True)

    request = f"""ADVERSARIAL ANALYSIS PASS

you are analyzing this artifact from a hostile reader's perspective to find gaps
the rubric judges missed.

ADVERSARIAL PERSONA:
{persona}

---

ARTIFACT:
{artifact_text}

---

TASK:
read the artifact as the adversarial persona. find 3-5 objections this reader
would raise. for each objection:
1. state the objection clearly
2. rate 1-10 how well the current artifact addresses it (1 = not at all, 10 = fully)
3. classify the gap: writing gap (fixable with better writing) or data gap
   (requires real-world information the artifact doesn't have)

focus on gaps the normal rubric judges don't catch -- contradictions, unsupported
assumptions, logic gaps, missing critical info for the target reader.

if you reference specific data that does not exist in the artifact, wrap it in:
  [PLACEHOLDER: what real data is needed]

OUTPUT FORMAT:
for each objection:
### Objection N: [title]
- Current score: [1-10]
- Gap type: [writing gap | data gap]
- Explanation: [why this matters, what's missing]

---SUMMARY---
[2-3 sentences: highest-priority gaps to address in the next mutation]"""

    request_path = iter_dir / 'adversarial_request.md'
    request_path.write_text(request)

    save_state({
        'iteration': iteration,
        'phase': 'awaiting_adversarial',
    })

    console.print(f"\n[bold cyan]adversarial pass (iteration {iteration})[/bold cyan]")
    console.print(f"[green]request written to {request_path}[/green]")
    console.print(f"\n[bold]next:[/bold] read the request, analyze as adversarial persona, write:")
    console.print(f"  {iter_dir / 'adversarial_response.md'}")
    console.print(f"\nthen run: adversarial-process")


def adversarial_process_mode():
    """process adversarial response, save findings to state and log"""
    state = load_state()
    iteration = state.get('iteration', 0)
    iter_dir = RUNS_DIR / f'iter_{iteration:03d}'

    response_path = iter_dir / 'adversarial_response.md'
    if not response_path.exists():
        console.print(f"[red]adversarial response not found: {response_path}[/red]")
        sys.exit(1)

    findings_text = response_path.read_text()

    # extract summary if present
    summary_match = re.search(r'---SUMMARY---\s*\n(.+)', findings_text, re.DOTALL)
    summary = summary_match.group(1).strip() if summary_match else findings_text[:500]

    console.print(f"\n[bold cyan]adversarial findings (iteration {iteration}):[/bold cyan]")
    console.print(Panel(summary, border_style="cyan"))

    # log adversarial event
    entry = {
        'event': 'adversarial',
        'iteration': iteration,
        'timestamp': datetime.now().isoformat(),
        'summary': summary,
    }
    log_entry(entry)

    # persist full findings to adversarial log (accumulates across iterations)
    adv_log_path = RUNS_DIR / 'adversarial_log.jsonl'
    adv_entry = {
        'iteration': iteration,
        'timestamp': datetime.now().isoformat(),
        'summary': summary,
        'findings': findings_text,
    }
    with open(adv_log_path, 'a') as f:
        f.write(json.dumps(adv_entry) + '\n')

    # save findings to state for next mutation request
    save_state({
        'iteration': iteration,
        'phase': 'completed',
        'adversarial_findings': findings_text,
    })

    console.print(f"\n[green]adversarial findings saved to state, log, and adversarial_log.jsonl[/green]")
    console.print("[dim]next iteration's mutation request will include these findings[/dim]")


# ============================================================================
# morning report
# ============================================================================

def rogan_gladen_correction(p_obs: float, tpr: float, tnr: float) -> float:
    """apply rogan-gladen correction to observed pass rate"""
    if tpr + tnr <= 1.0:
        return p_obs
    theta = (p_obs + tnr - 1) / (tpr + tnr - 1)
    return max(0.0, min(1.0, theta))


def generate_report(log_path: Path, judge_dir: Optional[Path]):
    """generate morning report"""
    if not log_path.exists():
        console.print("[red]log not found[/red]")
        return

    console.print("\n" + "=" * 80)
    console.print(Panel.fit("MORNING REPORT", style="bold blue"))
    console.print("=" * 80 + "\n")

    entries = []
    with open(log_path) as f:
        entries = [json.loads(l) for l in f if l.strip()]

    iterations = [e for e in entries if 'composite_before' in e]
    adversarials = [e for e in entries if e.get('event') == 'adversarial']

    if not iterations:
        console.print("[yellow]no iterations found[/yellow]")
        return

    # summary table
    table = Table(box=box.SIMPLE)
    table.add_column("iter", style="cyan")
    table.add_column("composite", style="magenta")
    table.add_column("dimension", style="yellow")
    table.add_column("decision", style="green")
    table.add_column("rationale", style="dim")

    for e in iterations[-20:]:
        decision = e.get('decision', '?')
        dc = "green" if decision == "ADOPTED" else "red" if decision == "DISCARDED" else "yellow"
        table.add_row(
            str(e.get('iteration', '?')),
            f"{e.get('composite_after', 0):.2f}",
            e.get('targeted_dimension', '?')[:20],
            f"[{dc}]{decision}[/{dc}]",
            e.get('mutation_rationale', '')[:50]
        )
    console.print(table)

    # score progression
    console.print("\n[bold]score progression:[/bold]\n")
    for e in iterations[-30:]:
        score = e.get('composite_after', 0)
        bar = '█' * int(score * 50)
        console.print(f"  {e.get('iteration', '?'):3} | {bar} {score:.2f}")

    # per-dimension corrected pass rates
    if judge_dir and judge_dir.exists():
        console.print("\n[bold]per-dimension corrected pass rates (rogan-gladen):[/bold]\n")

        judge_meta = {}
        for jf in sorted(judge_dir.glob('*.md')):
            if any(s in jf.name for s in ['_batch', '_results', '_truth', '_split']):
                continue
            meta = parse_judge_frontmatter(jf)
            judge_meta[meta['dimension']] = meta

        dim_stats = {}
        for e in iterations:
            for dim, v in e.get('llm_scores_after', {}).items():
                dim_stats.setdefault(dim, {'pass': 0, 'total': 0})
                dim_stats[dim]['total'] += 1
                if v == 'PASS':
                    dim_stats[dim]['pass'] += 1

        dt = Table(box=box.SIMPLE)
        dt.add_column("dimension"); dt.add_column("observed"); dt.add_column("corrected")
        dt.add_column("tpr", style="dim"); dt.add_column("tnr", style="dim")

        for dim, stats in sorted(dim_stats.items()):
            if stats['total'] == 0:
                continue
            p_obs = stats['pass'] / stats['total']
            meta = judge_meta.get(dim, {'tpr': 0.95, 'tnr': 0.95})
            theta = rogan_gladen_correction(p_obs, meta['tpr'], meta['tnr'])
            dt.add_row(dim, f"{p_obs:.2f}", f"{theta:.2f}", f"{meta['tpr']:.2f}", f"{meta['tnr']:.2f}")
        console.print(dt)

    # adversarial events (prefer full log if available, fallback to log.jsonl summaries)
    adv_log_path = RUNS_DIR / 'adversarial_log.jsonl'
    if adv_log_path.exists():
        adv_entries = []
        with open(adv_log_path) as f:
            for line in f:
                if line.strip():
                    adv_entries.append(json.loads(line))
        if adv_entries:
            console.print(f"\n[bold]adversarial passes ({len(adv_entries)} total):[/bold]")
            for adv in adv_entries:
                console.print(f"  iter {adv.get('iteration', '?')}: {adv.get('summary', 'no summary')[:120]}")
    elif adversarials:
        console.print("\n[bold]adversarial passes:[/bold]")
        for adv in adversarials:
            console.print(f"  iter {adv.get('iteration', '?')}: {adv.get('summary', 'no summary')[:120]}")

    # stats
    adopted = sum(1 for e in iterations if e.get('decision') == 'ADOPTED')
    discarded = sum(1 for e in iterations if e.get('decision') == 'DISCARDED')
    console.print(f"\n[bold]totals:[/bold] {len(iterations)} iterations, {adopted} adopted, {discarded} discarded, {len(adversarials)} adversarial")


# ============================================================================
# checkpoint review
# ============================================================================

def review_checkpoints(log_path: Path, improve_path: Path):
    """review queued checkpoints from unattended run"""
    improve_config = parse_improve_md(improve_path)
    console.print(Panel.fit("reviewing checkpoints", style="bold yellow"))

    if not log_path.exists():
        console.print("[red]log not found[/red]")
        return

    entries = []
    with open(log_path) as f:
        entries = [json.loads(l) for l in f if l.strip()]

    checkpoints = [e for e in entries if e.get('event') == 'CHECKPOINT'
                   and e.get('verdicts', {}).get('quality') == 'pending']

    if not checkpoints:
        console.print("[yellow]no pending checkpoints[/yellow]")
        return

    approved_path = RUNS_DIR / 'approved.md'
    best_path = RUNS_DIR / 'best.md'

    for ckpt in checkpoints:
        iteration = ckpt['iteration']
        console.print(f"\n[bold]checkpoint at iteration {iteration}[/bold]")

        approved_text = approved_path.read_text()
        best_text = best_path.read_text()

        console.print("[bold]APPROVED:[/bold]")
        console.print(Panel(approved_text[:500] + "...", border_style="green"))
        console.print("[bold]BEST:[/bold]")
        console.print(Panel(best_text[:500] + "...", border_style="blue"))

        quality = Prompt.ask("better, worse, or same?", choices=["b", "w", "s"], default="s")
        ete = Prompt.ask(f"E2E: {improve_config['end_to_end_question']}", choices=["y", "n"], default="n")

        if quality == "b" and ete == "y":
            shutil.copy(best_path, approved_path)
            console.print("[bold green]approved updated[/bold green]")
        elif quality in ["w", "s"]:
            shutil.copy(approved_path, best_path)
            console.print("[yellow]reverted to approved[/yellow]")


def placeholders_mode(artifact_path: Path):
    """scan artifact for [PLACEHOLDER: ...] tags and report"""
    if not artifact_path.exists():
        console.print(f"[red]artifact not found: {artifact_path}[/red]")
        sys.exit(1)

    text = artifact_path.read_text()
    pattern = r'\[PLACEHOLDER:\s*([^\]]+)\]'
    matches = []

    for line_num, line in enumerate(text.split('\n'), start=1):
        for m in re.finditer(pattern, line):
            matches.append({
                'line': line_num,
                'description': m.group(1).strip(),
                'context': line.strip()
            })

    if not matches:
        console.print("[green]no placeholders found in artifact[/green]")
        return

    console.print(f"\n[bold yellow]{len(matches)} placeholder(s) need real data[/bold yellow]\n")

    table = Table(box=box.ROUNDED)
    table.add_column("line", style="cyan", width=6)
    table.add_column("needs", style="yellow")
    table.add_column("context", style="dim", max_width=60)

    for m in matches:
        ctx = m['context'][:80] + "..." if len(m['context']) > 80 else m['context']
        table.add_row(str(m['line']), m['description'], ctx)

    console.print(table)


# ============================================================================
# cli
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='phase 2 improvement loop (data-only, no LLM calls)',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    sub = parser.add_subparsers(dest='command', help='available commands')

    # init
    p_init = sub.add_parser('init', help='initialize constraint hashes + baseline')
    p_init.add_argument('--artifact', required=True, help='path to artifact.md')
    p_init.add_argument('--improve', required=True, help='path to improve.md')

    # score-before
    p_sb = sub.add_parser('score-before', help='run cheap checks, write judge request')
    p_sb.add_argument('--artifact', required=True)
    p_sb.add_argument('--dimensions', default='examples/dimensions.md')
    p_sb.add_argument('--judge-dir', default='judge_prompts/')
    p_sb.add_argument('--improve', required=True)
    p_sb.add_argument('--context', help='optional path to context.md (ground truth facts)')
    p_sb.add_argument('--force-rejudge', action='store_true',
                      help='ignore cached scores and re-judge all dimensions')

    # score-after
    p_sa = sub.add_parser('score-after', help='read judge results, select weakest, write mutation request')
    p_sa.add_argument('--judge-dir', default='judge_prompts/')
    p_sa.add_argument('--improve', required=True)

    # apply-mutation
    p_am = sub.add_parser('apply-mutation', help='apply mutation, write after-judge request')
    p_am.add_argument('--artifact', required=True)
    p_am.add_argument('--dimensions', default='examples/dimensions.md')
    p_am.add_argument('--judge-dir', default='judge_prompts/')
    p_am.add_argument('--context', help='optional path to context.md (ground truth facts)')

    # verdict
    p_v = sub.add_parser('verdict', help='compare before/after, keep or discard')
    p_v.add_argument('--artifact', required=True)
    p_v.add_argument('--stall-threshold', type=int, default=3, help='consecutive discards before warning (default: 3)')
    p_v.add_argument('--adversarial-interval', type=int, default=5, help='adversarial pass every N iterations (default: 5, 0 to disable)')

    # report
    p_r = sub.add_parser('report', help='generate morning report')
    p_r.add_argument('--log', default='runs/log.jsonl')
    p_r.add_argument('--judge-dir', default='judge_prompts/')

    # adversarial
    p_adv = sub.add_parser('adversarial', help='write adversarial analysis request')
    p_adv.add_argument('--artifact', required=True)
    p_adv.add_argument('--improve', required=True)
    p_adv.add_argument('--interval', type=int, default=5, help='adversarial pass interval (default: 5)')

    # adversarial-process
    p_adv_proc = sub.add_parser('adversarial-process', help='process adversarial response')

    # review-checkpoints
    p_rc = sub.add_parser('review-checkpoints', help='review queued checkpoints')
    p_rc.add_argument('--log', default='runs/log.jsonl')
    p_rc.add_argument('--improve', required=True)

    # placeholders
    p_ph = sub.add_parser('placeholders', help='scan artifact for [PLACEHOLDER: ...] tags')
    p_ph.add_argument('--artifact', required=True, help='path to artifact.md')

    args = parser.parse_args()

    if args.command == 'init':
        init_mode(Path(args.artifact), Path(args.improve))
    elif args.command == 'score-before':
        ctx = Path(args.context) if getattr(args, 'context', None) else None
        score_before_mode(Path(args.artifact), Path(args.dimensions),
                         Path(args.judge_dir), Path(args.improve), ctx,
                         getattr(args, 'force_rejudge', False))
    elif args.command == 'score-after':
        score_after_mode(Path(args.judge_dir), Path(args.improve))
    elif args.command == 'apply-mutation':
        ctx = Path(args.context) if getattr(args, 'context', None) else None
        apply_mutation_mode(Path(args.artifact), Path(args.dimensions), Path(args.judge_dir), ctx)
    elif args.command == 'verdict':
        verdict_mode(Path(args.artifact), args.stall_threshold, args.adversarial_interval)
    elif args.command == 'adversarial':
        adversarial_mode(Path(args.artifact), Path(args.improve), args.interval)
    elif args.command == 'adversarial-process':
        adversarial_process_mode()
    elif args.command == 'report':
        generate_report(Path(args.log), Path(args.judge_dir))
    elif args.command == 'review-checkpoints':
        review_checkpoints(Path(args.log), Path(args.improve))
    elif args.command == 'placeholders':
        placeholders_mode(Path(args.artifact))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
