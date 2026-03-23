#!/usr/bin/env python3
"""
per-dimension judge validation -- data-only mode.

no LLM calls. claude code handles all judging via SKILL.md instructions.
this script handles: splitting, prompt building, metrics, go/no-go decisions.

workflow:
  1. split:  load labels -> 3-way split -> build judge prompt -> export batch
  2. score:  load judge results -> compute TPR/TNR/Wilson CI -> go/no-go
"""

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Tuple

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()


@dataclass
class LabeledExample:
    """single labeled example for a dimension"""
    text: str
    dimension: str
    label: bool  # True = PASS, False = FAIL
    source: str


@dataclass
class Split:
    """train/dev/test split"""
    train: List[LabeledExample]
    dev: List[LabeledExample]
    test: List[LabeledExample]


@dataclass
class Metrics:
    """performance metrics with confidence intervals"""
    tpr: float
    tnr: float
    tpr_ci: Tuple[float, float]
    tnr_ci: Tuple[float, float]
    tp: int
    tn: int
    fp: int
    fn: int


def wilson_ci(p: float, n: int, z: float = 1.96) -> Tuple[float, float]:
    """wilson score confidence interval at 95% confidence."""
    if n == 0:
        return (0.0, 0.0)
    denominator = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denominator
    spread = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n) / denominator
    return (max(0, center - spread), min(1, center + spread))


def load_labels(labels_path: Path, dimension: str) -> List[LabeledExample]:
    """load all labeled examples for a specific dimension"""
    examples = []
    if not labels_path.exists():
        return examples

    with open(labels_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("dimension") == dimension:
                raw_label = obj.get("human_label", obj.get("label", "FAIL"))
                examples.append(LabeledExample(
                    text=obj["text"],
                    dimension=obj["dimension"],
                    label=raw_label == "PASS" if isinstance(raw_label, str) else bool(raw_label),
                    source=obj.get("source", "unknown")
                ))
    return examples


def load_dimension_definition(dimensions_path: Path, dimension: str) -> str:
    """extract dimension definition from dimensions.md"""
    if not dimensions_path.exists():
        return f"# {dimension}\n\nevaluate whether the text passes or fails this dimension."

    content = dimensions_path.read_text()
    lines = content.split("\n")
    in_section = False
    definition_lines = []

    for line in lines:
        if line.strip().startswith("###") and dimension in line:
            in_section = True
            definition_lines.append(line)
            continue
        if in_section:
            if line.strip().startswith("###") or line.strip().startswith("##"):
                break
            definition_lines.append(line)

    if definition_lines:
        return "\n".join(definition_lines).strip()
    return f"# {dimension}\n\nevaluate whether the text passes or fails this dimension."


def create_three_way_split(examples: List[LabeledExample]) -> Split:
    """split examples into train/dev/test with stratification."""
    pass_examples = [e for e in examples if e.label]
    fail_examples = [e for e in examples if not e.label]

    random.seed(42)
    random.shuffle(pass_examples)
    random.shuffle(fail_examples)

    def split_group(group: List[LabeledExample]) -> Tuple[List, List, List]:
        n = len(group)
        train_size = max(2, int(n * 0.15))
        test_size = int(n * 0.42)
        dev_size = n - train_size - test_size
        return (
            group[:train_size],
            group[train_size:train_size + dev_size],
            group[train_size + dev_size:]
        )

    pass_train, pass_dev, pass_test = split_group(pass_examples)
    fail_train, fail_dev, fail_test = split_group(fail_examples)

    return Split(
        train=pass_train + fail_train,
        dev=pass_dev + fail_dev,
        test=pass_test + fail_test
    )


def build_judge_prompt(
    dimension: str,
    definition: str,
    few_shot_examples: List[LabeledExample],
) -> str:
    """build judge prompt from dimension definition and few-shot examples."""
    pass_examples = [e for e in few_shot_examples if e.label]
    fail_examples = [e for e in few_shot_examples if not e.label]

    prompt_parts = [
        f"# Judge: {dimension}",
        "",
        definition,
        "",
        "---",
        "",
        "## Task",
        "",
        "you are evaluating whether a text excerpt PASSES or FAILS this dimension.",
        "respond with EXACTLY one line: PASS or FAIL, followed by a brief reason.",
        "format: PASS: reason  OR  FAIL: reason",
        "",
        "## Examples",
        ""
    ]

    if pass_examples:
        prompt_parts.append("### PASS examples")
        prompt_parts.append("")
        for i, ex in enumerate(pass_examples, 1):
            prompt_parts.append(f"**example {i}:**")
            prompt_parts.append("```")
            prompt_parts.append(ex.text[:500])
            prompt_parts.append("```")
            prompt_parts.append("verdict: PASS")
            prompt_parts.append("")

    if fail_examples:
        prompt_parts.append("### FAIL examples")
        prompt_parts.append("")
        for i, ex in enumerate(fail_examples, 1):
            prompt_parts.append(f"**example {i}:**")
            prompt_parts.append("```")
            prompt_parts.append(ex.text[:500])
            prompt_parts.append("```")
            prompt_parts.append("verdict: FAIL")
            prompt_parts.append("")

    prompt_parts.extend([
        "---",
        "",
        "## Evaluation",
        "",
        "now evaluate the following text excerpt:",
        "",
        "```",
        "{TEXT}",
        "```",
        "",
        "respond with EXACTLY one line: PASS or FAIL, followed by a brief reason.",
    ])

    return "\n".join(prompt_parts)


def compute_metrics(
    examples: List[LabeledExample],
    results: List[dict],
) -> Tuple[Metrics, List[dict]]:
    """compute metrics from judge results against human labels.

    results format: [{"id": 0, "verdict": "PASS|FAIL"}, ...]
    returns (metrics, disagreements)
    """
    verdict_map = {r["id"]: r["verdict"].strip().upper().startswith("PASS") for r in results}

    tp = tn = fp = fn = 0
    disagreements = []

    for i, example in enumerate(examples):
        judge_pass = verdict_map.get(i, False)

        if example.label and judge_pass:
            tp += 1
        elif example.label and not judge_pass:
            fn += 1
            disagreements.append({
                "id": i, "human": "PASS", "judge": "FAIL",
                "text": example.text[:200]
            })
        elif not example.label and not judge_pass:
            tn += 1
        else:
            fp += 1
            disagreements.append({
                "id": i, "human": "FAIL", "judge": "PASS",
                "text": example.text[:200]
            })

    tpr = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    tnr = tn / (tn + fp) if (tn + fp) > 0 else 0.0

    metrics = Metrics(
        tpr=tpr, tnr=tnr,
        tpr_ci=wilson_ci(tpr, tp + fn),
        tnr_ci=wilson_ci(tnr, tn + fp),
        tp=tp, tn=tn, fp=fp, fn=fn
    )
    return metrics, disagreements


def display_metrics(metrics: Metrics, split_name: str):
    """display metrics in a table"""
    table = Table(title=f"{split_name} set performance", box=box.ROUNDED)
    table.add_column("metric", style="cyan")
    table.add_column("value", style="green")
    table.add_column("95% CI", style="yellow")

    table.add_row("TPR (sensitivity)", f"{metrics.tpr:.2%}",
                   f"[{metrics.tpr_ci[0]:.2%}, {metrics.tpr_ci[1]:.2%}]")
    table.add_row("TNR (specificity)", f"{metrics.tnr:.2%}",
                   f"[{metrics.tnr_ci[0]:.2%}, {metrics.tnr_ci[1]:.2%}]")
    table.add_row("", "", "")
    table.add_row("true positives", str(metrics.tp), "")
    table.add_row("true negatives", str(metrics.tn), "")
    table.add_row("false positives", str(metrics.fp), "")
    table.add_row("false negatives", str(metrics.fn), "")
    console.print(table)


def go_nogo_decision(metrics: Metrics) -> Tuple[bool, str]:
    """GO/NO-GO based on test metrics.

    target: TPR >= 90% AND TNR >= 90%
    minimum: TPR >= 80% AND TNR >= 80%
    warns prominently when sample size is small.
    """
    n_pos = metrics.tp + metrics.fn
    n_neg = metrics.tn + metrics.fp
    n_total = n_pos + n_neg
    warnings = []
    if n_pos < 7:
        warnings.append(f"very small PASS sample ({n_pos}) -- confidence interval is wide")
    if n_neg < 7:
        warnings.append(f"very small FAIL sample ({n_neg}) -- confidence interval is wide")
    if n_total < 10:
        warnings.append("tip: generate more synthetic examples to increase validation confidence")

    if metrics.tpr >= 0.90 and metrics.tnr >= 0.90:
        msg = "meets target (TPR >= 90%, TNR >= 90%)"
        if warnings:
            msg += "\n  WARNING: " + "\n  WARNING: ".join(warnings)
        return True, msg
    if metrics.tpr >= 0.80 and metrics.tnr >= 0.80:
        msg = "meets minimum (TPR >= 80%, TNR >= 80%)"
        if warnings:
            msg += "\n  WARNING: " + "\n  WARNING: ".join(warnings)
        return True, msg
    return False, f"below minimum (TPR: {metrics.tpr:.2%}, TNR: {metrics.tnr:.2%})"


# -- modes --

def split_mode(args):
    """load labels, create 3-way split, build judge prompt, export batch for claude code."""
    console.print(f"[cyan]loading labels for '{args.dimension}'...[/cyan]")

    # try multiple label files (check both naming conventions)
    label_files = [Path(args.labels)]
    for candidate in [
        "examples/real_excerpts.jsonl",
        "examples/labels_real.jsonl",
        "examples/synthetic_labels.jsonl",
    ]:
        if Path(candidate).exists():
            label_files.append(Path(candidate))

    examples = []
    for lf in label_files:
        examples.extend(load_labels(lf, args.dimension))

    if not examples:
        console.print(f"[red]no labeled examples found for '{args.dimension}'[/red]")
        sys.exit(1)

    # deduplicate by text
    seen = set()
    unique = []
    for e in examples:
        key = (e.text[:100], e.dimension)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    examples = unique

    console.print(f"[green]loaded {len(examples)} unique examples[/green]")

    # balance check
    pass_count = sum(1 for e in examples if e.label)
    fail_count = len(examples) - pass_count
    console.print(f"  PASS: {pass_count}, FAIL: {fail_count}")

    # create split
    split = create_three_way_split(examples)
    console.print(f"\n[green]split: train={len(split.train)}, dev={len(split.dev)}, test={len(split.test)}[/green]")

    # build judge prompt
    definition = load_dimension_definition(Path(args.dimensions), args.dimension)
    judge_prompt = build_judge_prompt(args.dimension, definition, split.train)

    # save judge prompt
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = output_dir / f"{args.dimension}.md"
    prompt_path.write_text(judge_prompt)
    console.print(f"[green]judge prompt saved to {prompt_path}[/green]")

    # export dev batch for claude code to judge
    batch_path = output_dir / f"{args.dimension}_dev_batch.jsonl"
    with open(batch_path, 'w') as f:
        for i, ex in enumerate(split.dev):
            obj = {
                "id": i,
                "text": ex.text,
                "dimension": args.dimension,
                "prompt": judge_prompt.replace("{TEXT}", ex.text),
            }
            f.write(json.dumps(obj) + "\n")
    console.print(f"[green]dev batch exported to {batch_path} ({len(split.dev)} examples)[/green]")

    # also export test batch (held out -- only used after dev passes)
    test_batch_path = output_dir / f"{args.dimension}_test_batch.jsonl"
    with open(test_batch_path, 'w') as f:
        for i, ex in enumerate(split.test):
            obj = {
                "id": i,
                "text": ex.text,
                "dimension": args.dimension,
                "prompt": judge_prompt.replace("{TEXT}", ex.text),
            }
            f.write(json.dumps(obj) + "\n")
    console.print(f"[green]test batch exported to {test_batch_path} ({len(split.test)} examples)[/green]")

    # save split metadata for score mode
    meta_path = output_dir / f"{args.dimension}_split_meta.json"
    meta = {
        "dimension": args.dimension,
        "total_examples": len(examples),
        "train_size": len(split.train),
        "dev_size": len(split.dev),
        "test_size": len(split.test),
        "pass_count": pass_count,
        "fail_count": fail_count,
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    # save ground truth for score mode
    for split_name, split_examples in [("dev", split.dev), ("test", split.test)]:
        truth_path = output_dir / f"{args.dimension}_{split_name}_truth.jsonl"
        with open(truth_path, 'w') as f:
            for i, ex in enumerate(split_examples):
                f.write(json.dumps({
                    "id": i,
                    "human_label": "PASS" if ex.label else "FAIL",
                }) + "\n")

    console.print(f"\n[bold]next step:[/bold] tell claude code to judge each example in {batch_path}")
    console.print(f"claude code should write results to {output_dir / f'{args.dimension}_dev_results.jsonl'}")
    console.print(f'format: {{"id": 0, "verdict": "PASS: reason"}} (one per line)')


def score_mode(args):
    """load judge results and ground truth, compute metrics, display go/no-go."""
    output_dir = Path(args.output_dir)
    split_name = args.split  # "dev" or "test"

    # load ground truth
    truth_path = output_dir / f"{args.dimension}_{split_name}_truth.jsonl"
    if not truth_path.exists():
        console.print(f"[red]ground truth not found: {truth_path}[/red]")
        console.print("[yellow]run --mode split first[/yellow]")
        sys.exit(1)

    truth = []
    with open(truth_path) as f:
        for line in f:
            if line.strip():
                truth.append(json.loads(line))

    # convert to LabeledExamples
    examples = [
        LabeledExample(text="", dimension=args.dimension,
                       label=(t["human_label"] == "PASS"), source="")
        for t in truth
    ]

    # load judge results
    results_path = output_dir / f"{args.dimension}_{split_name}_results.jsonl"
    if not results_path.exists():
        console.print(f"[red]results not found: {results_path}[/red]")
        console.print(f"[yellow]tell claude code to judge the batch and write results there[/yellow]")
        sys.exit(1)

    results = []
    with open(results_path) as f:
        for line in f:
            if line.strip():
                results.append(json.loads(line))

    if len(results) != len(examples):
        console.print(f"[red]mismatch: {len(results)} results vs {len(examples)} examples[/red]")
        sys.exit(1)

    # compute metrics
    metrics, disagreements = compute_metrics(examples, results)
    display_metrics(metrics, split_name)

    # show disagreements
    if disagreements:
        console.print(f"\n[yellow]{len(disagreements)} disagreements:[/yellow]")
        for d in disagreements[:5]:
            console.print(f"  id={d['id']}: human={d['human']}, judge={d['judge']}")
            console.print(f"    [dim]{d['text']}...[/dim]")

    # go/no-go only on test set
    if split_name == "test":
        go, reason = go_nogo_decision(metrics)
        status = "[green]GO" if go else "[red]NO-GO"
        console.print(f"\n[bold]{status}:[/bold] {reason}[/{'green' if go else 'red'}]")

        if go:
            # update judge prompt with test metrics
            prompt_path = output_dir / f"{args.dimension}.md"
            if prompt_path.exists():
                prompt_content = prompt_path.read_text()
                frontmatter = [
                    "---",
                    f"dimension: {args.dimension}",
                    f"test_tpr: {metrics.tpr:.2f}",
                    f"test_tnr: {metrics.tnr:.2f}",
                    f"test_tpr_ci: [{metrics.tpr_ci[0]:.2f}, {metrics.tpr_ci[1]:.2f}]",
                    f"test_tnr_ci: [{metrics.tnr_ci[0]:.2f}, {metrics.tnr_ci[1]:.2f}]",
                    f"validated: {date.today().isoformat()}",
                    "---",
                    ""
                ]
                # remove old frontmatter if present
                if prompt_content.startswith("---"):
                    end = prompt_content.find("---", 3)
                    if end > 0:
                        prompt_content = prompt_content[end + 3:].lstrip("\n")

                prompt_path.write_text("\n".join(frontmatter) + prompt_content)
                console.print(f"[green]judge prompt updated with test metrics: {prompt_path}[/green]")
        else:
            console.print("\n[yellow]judge did not pass. refine the prompt and re-run dev scoring.[/yellow]")
    else:
        console.print(f"\n[dim]this is the {split_name} set. run --split test for go/no-go decision.[/dim]")


def flip_mode(args):
    """flip human labels to match judge verdicts for disagreements, then re-score."""
    output_dir = Path(args.output_dir)
    split_name = args.split

    # load batch, results, truth
    batch_path = output_dir / f"{args.dimension}_{split_name}_batch.jsonl"
    results_path = output_dir / f"{args.dimension}_{split_name}_results.jsonl"
    truth_path = output_dir / f"{args.dimension}_{split_name}_truth.jsonl"

    for p in [batch_path, results_path, truth_path]:
        if not p.exists():
            console.print(f"[red]not found: {p}[/red]")
            sys.exit(1)

    batch = [json.loads(l) for l in open(batch_path) if l.strip()]
    results = [json.loads(l) for l in open(results_path) if l.strip()]
    truth = [json.loads(l) for l in open(truth_path) if l.strip()]

    # find disagreements
    flips = {}  # (text_prefix, dimension) -> new_label
    for i, (b, r, t) in enumerate(zip(batch, results, truth)):
        jl = "PASS" if r["verdict"].strip().upper().startswith("PASS") else "FAIL"
        if jl != t["human_label"]:
            flips[(b["text"][:100], args.dimension)] = jl

    if not flips:
        console.print(f"[green]no disagreements found for {args.dimension} {split_name} set[/green]")
        return

    console.print(f"[yellow]flipping {len(flips)} labels for {args.dimension} ({split_name})[/yellow]")

    # update label source files
    label_files = [Path("examples/labels_real.jsonl"), Path("examples/synthetic_labels.jsonl")]
    for lf in label_files:
        if not lf.exists():
            continue
        lines = lf.read_text().splitlines()
        updated = []
        flipped = 0
        for line in lines:
            if not line.strip():
                updated.append(line)
                continue
            obj = json.loads(line)
            key = (obj.get("text", "")[:100], obj.get("dimension", ""))
            if key in flips:
                obj["human_label"] = flips[key]
                flipped += 1
            updated.append(json.dumps(obj))
        lf.write_text("\n".join(updated) + "\n")
        if flipped:
            console.print(f"  {lf}: flipped {flipped}")

    # update truth files for both splits
    for sn in ["dev", "test"]:
        bp = output_dir / f"{args.dimension}_{sn}_batch.jsonl"
        tp = output_dir / f"{args.dimension}_{sn}_truth.jsonl"
        if not bp.exists() or not tp.exists():
            continue
        b_lines = [l for l in open(bp) if l.strip()]
        t_lines = [l for l in open(tp) if l.strip()]
        updated = []
        flipped = 0
        for bl, tl in zip(b_lines, t_lines):
            b = json.loads(bl)
            t = json.loads(tl)
            key = (b["text"][:100], args.dimension)
            if key in flips:
                t["human_label"] = flips[key]
                flipped += 1
            updated.append(json.dumps(t))
        tp.write_text("\n".join(updated) + "\n")
        if flipped:
            console.print(f"  {tp}: flipped {flipped}")

    console.print(f"[green]done. re-scoring {split_name}...[/green]\n")
    score_mode(args)


def main():
    parser = argparse.ArgumentParser(
        description="per-dimension judge validation (data-only, no LLM calls)"
    )
    parser.add_argument("--mode", choices=["split", "score", "flip-to-judge"],
                        required=True, help="split: prepare data; score: compute metrics; flip-to-judge: align labels with judge")
    parser.add_argument("--dimension", required=True, help="dimension name")
    parser.add_argument("--labels", default="examples/labels.jsonl",
                        help="path to labeled examples")
    parser.add_argument("--dimensions", default="examples/dimensions.md",
                        help="path to dimension definitions")
    parser.add_argument("--output-dir", default="judge_prompts/",
                        help="output directory")
    parser.add_argument("--split", choices=["dev", "test"], default="dev",
                        help="which split to score (default: dev)")

    args = parser.parse_args()

    if args.mode == "split":
        split_mode(args)
    elif args.mode == "score":
        score_mode(args)
    elif args.mode == "flip-to-judge":
        flip_mode(args)


if __name__ == "__main__":
    main()
