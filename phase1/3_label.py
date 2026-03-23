#!/usr/bin/env python3
"""interactive CLI labeler for real and synthetic excerpts.

relevance filtering: keywords are extracted automatically from each dimension's
definition and PASS/FAIL examples in dimensions.md. excerpts are only shown for
a dimension if they contain at least one keyword match. no hardcoded domain terms.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

console = Console()

# words too common to be useful as relevance signals
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "must", "not", "no", "nor",
    "and", "but", "or", "if", "then", "else", "when", "where", "how",
    "what", "which", "who", "whom", "this", "that", "these", "those",
    "it", "its", "of", "in", "on", "at", "to", "for", "with", "by",
    "from", "as", "into", "through", "during", "before", "after", "above",
    "below", "between", "under", "again", "further", "once", "here",
    "there", "all", "each", "every", "both", "few", "more", "most",
    "other", "some", "such", "than", "too", "very", "just", "about",
    "also", "so", "only", "own", "same", "they", "them", "their", "he",
    "she", "his", "her", "you", "your", "we", "our", "my", "me",
    "up", "out", "any", "over", "per", "one", "two", "three",
    "example", "section", "plan", "text", "based", "without", "using",
    "make", "made", "shows", "show", "found", "appears", "appear",
    "across", "different", "sections", "rather", "treats", "either",
    "explicitly", "presented", "grounded", "addresses", "includes",
    "business", "numbers", "another", "dedicated", "prominent",
    "mentions", "needs", "still", "first", "doesn", "would", "doesn't",
    "require", "never", "where", "while", "whether", "within",
}


def parse_dimensions(dimensions_path: Path) -> List[Dict]:
    """extract dimensions with definitions, examples, and auto-generated keywords."""
    if not dimensions_path.exists():
        console.print(f"[red]error: dimensions file not found: {dimensions_path}[/red]")
        sys.exit(1)

    text = dimensions_path.read_text()
    dimensions = []
    in_judge_section = False
    current_dim = None

    for line in text.split("\n"):
        if line.strip() == "## LLM Judge Dimensions":
            in_judge_section = True
            continue
        if in_judge_section and line.startswith("## "):
            break
        if in_judge_section and line.startswith("### "):
            if current_dim:
                dimensions.append(current_dim)
            current_dim = {
                "name": line[4:].strip(),
                "definition": "",
                "pass_example": "",
                "fail_example": "",
            }
            continue
        if current_dim:
            if line.startswith("- Definition:"):
                current_dim["definition"] = line[len("- Definition:"):].strip()
            elif line.startswith("- PASS example:"):
                current_dim["pass_example"] = line[len("- PASS example:"):].strip()
            elif line.startswith("- FAIL example:"):
                current_dim["fail_example"] = line[len("- FAIL example:"):].strip()

    if current_dim:
        dimensions.append(current_dim)

    if not dimensions:
        console.print("[red]error: no dimensions found in dimensions.md[/red]")
        sys.exit(1)

    for dim in dimensions:
        dim["keywords"] = _extract_keywords(dim)

    return dimensions


def _extract_keywords(dim: Dict) -> List[str]:
    """extract keywords from dimension definition and examples only.

    strategy:
    1. quoted strings and parenthetical terms (high signal -- the author
       chose to quote them for a reason)
    2. multi-word phrases (2-3 words) that aren't stopword-only
    3. single words that are long enough (>4 chars) and not stopwords
    4. words from the dimension name itself (snake_case split)
    """
    all_text = f"{dim['definition']} {dim['pass_example']} {dim['fail_example']}"
    keywords = []

    # 1. short quoted strings (1-4 words) -- high signal terms the author highlighted
    quoted = re.findall(r'"([^"]{3,80})"', all_text)
    for term in quoted:
        if len(term.split()) <= 4:
            keywords.append(term)

    # 2. parenthetical terms (1-4 words)
    parens = re.findall(r'\(([^)]{3,60})\)', all_text)
    for term in parens:
        if len(term.split()) <= 4:
            keywords.append(term)

    # 3. dimension name parts (snake_case -> individual words)
    name_parts = dim["name"].split("_")
    for part in name_parts:
        if part and part.lower() not in _STOPWORDS and len(part) > 2:
            keywords.append(part)

    # 4. content words: non-stopwords >= 5 chars from definition
    definition_words = re.findall(r'\b[A-Za-z\-]{5,}\b', dim["definition"])
    for word in definition_words:
        if word.lower() not in _STOPWORDS:
            keywords.append(word)

    # 5. alphanumeric tokens (currency, codes, percentages) from all text
    tokens = re.findall(r'[A-Z$%€£¥]+[\d\w$%]*[\d]+[\w%]*|[\d]+[%\w]+', all_text)
    keywords.extend(tokens)

    # deduplicate preserving order, case-insensitive
    seen = set()
    unique = []
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower not in seen:
            seen.add(kw_lower)
            unique.append(kw)

    return unique


def is_relevant(excerpt: str, keywords: List[str], threshold: int = 2) -> bool:
    """check if excerpt matches enough keywords to be relevant to a dimension."""
    excerpt_lower = excerpt.lower()
    matches = 0
    for kw in keywords:
        if kw.lower() in excerpt_lower:
            matches += 1
            if matches >= threshold:
                return True
    return False


def parse_artifact_to_excerpts(artifact_path: Path) -> List[Dict]:
    """parse artifact into section-based excerpts with headings as context.

    splits on ## and ### headings, keeps tables within their section.
    returns list of dicts: {text, heading, line_start}
    """
    if not artifact_path.exists():
        console.print(f"[red]error: artifact file not found: {artifact_path}[/red]")
        sys.exit(1)

    text = artifact_path.read_text()
    lines = text.split("\n")
    excerpts = []
    current_heading = "(top)"
    current_lines = []
    current_start = 1

    def flush():
        content = "\n".join(current_lines).strip()
        if content and len(content) > 50:
            excerpts.append({
                "text": content,
                "heading": current_heading,
                "line_start": current_start,
            })

    for i, line in enumerate(lines, 1):
        if re.match(r'^#{1,3} ', line):
            flush()
            current_heading = line.strip().lstrip("#").strip()
            current_lines = [line]
            current_start = i
        else:
            current_lines.append(line)

    flush()
    return excerpts


def load_synthetic_examples(unlabeled_path: Path) -> List[Dict]:
    """load synthetic examples from unlabeled.jsonl."""
    if not unlabeled_path.exists():
        console.print(f"[red]error: unlabeled file not found: {unlabeled_path}[/red]")
        sys.exit(1)

    examples = []
    with open(unlabeled_path) as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))

    return examples


def load_existing_labels(output_path: Path) -> List[Dict]:
    """load existing labels from output file if it exists."""
    if not output_path.exists():
        return []

    labels = []
    with open(output_path) as f:
        for line in f:
            line = line.strip()
            if line:
                labels.append(json.loads(line))

    return labels


def save_label(output_path: Path, label_data: Dict):
    """append a label to the output file."""
    with open(output_path, 'a') as f:
        f.write(json.dumps(label_data) + "\n")


def label_real_excerpts(
    excerpts: List[Dict],
    dimensions: List[Dict],
    output_path: Path
):
    """interactive labeling for real excerpts with relevance filtering."""
    # group relevant dimensions per excerpt (show excerpt once, ask all dims)
    excerpt_dims: List[tuple] = []  # (excerpt, [dim1, dim2, ...])
    for exc in excerpts:
        relevant_dims = [d for d in dimensions if is_relevant(exc["text"], d["keywords"])]
        if relevant_dims:
            excerpt_dims.append((exc, relevant_dims))

    all_pairs = len(excerpts) * len(dimensions)
    total_pairs = sum(len(dims) for _, dims in excerpt_dims)

    # show filtering summary
    console.print(f"\n[bold cyan]relevance filtering[/bold cyan]")
    summary = Table(show_header=True, header_style="bold")
    summary.add_column("dimension")
    summary.add_column("relevant / total", justify="right")
    summary.add_column("keywords (sample)", style="dim")
    for dim in dimensions:
        relevant = sum(1 for _, dims in excerpt_dims if any(d["name"] == dim["name"] for d in dims))
        kw_sample = ", ".join(dim["keywords"][:6])
        if len(dim["keywords"]) > 6:
            kw_sample += f" (+{len(dim['keywords']) - 6})"
        summary.add_row(dim["name"], f"{relevant} / {len(excerpts)}", kw_sample)
    console.print(summary)
    console.print(
        f"\n[bold]{len(excerpt_dims)}[/bold] excerpts to review, "
        f"[bold]{total_pairs}[/bold] dimension judgments "
        f"(filtered from {all_pairs} total)\n"
    )

    # resume support: skip already-labeled (text, dimension) pairs
    existing = load_existing_labels(output_path)
    labeled_pairs = {(e["text"], e["dimension"]) for e in existing if "text" in e and "dimension" in e}
    pass_count = sum(1 for e in existing if e.get("human_label") == "PASS")
    fail_count = sum(1 for e in existing if e.get("human_label") == "FAIL")
    skip_count = 0
    excerpt_num = 0
    dim_labeled: Dict[str, int] = {}
    for e in existing:
        d = e.get("dimension", "")
        dim_labeled[d] = dim_labeled.get(d, 0) + 1

    if labeled_pairs:
        console.print(f"[bold cyan]resuming real excerpt labeling[/bold cyan]")
        console.print(f"  already labeled: {len(labeled_pairs)} pairs\n")
    else:
        console.print(f"[bold cyan]starting real excerpt labeling[/bold cyan]\n")
    console.print(
        "[bold]What you're doing:[/bold] you are the human judge that trains\n"
        "the LLM judges. For each excerpt, you'll see the text once, then\n"
        "judge it against each relevant dimension.\n\n"
        "[bold]Example:[/bold] if the dimension says \"numbers must be sourced\"\n"
        "and the excerpt lists R$15k, R$20k with no sources, that's a [red]FAIL[/red].\n"
        "If it says \"R$30k [ASSUMPTION]\" or cites a supplier quote, that's a [green]PASS[/green].\n\n"
        "  [green]\[p][/green] = PASS -- the text satisfies the dimension's rule\n"
        "  [red]\[f][/red] = FAIL -- the text violates or ignores the rule\n"
        "  [yellow]\[s][/yellow] = SKIP -- this excerpt has nothing to do with this dimension\n"
        "  [dim]\[q][/dim] = QUIT -- progress is saved, resume later\n\n"
        "[dim]After each label, write a brief critique (why it passes/fails).\n"
        "This teaches the LLM judges to give useful feedback, not just scores.[/dim]\n"
    )

    quit_requested = False
    for excerpt, relevant_dims in excerpt_dims:
        if quit_requested:
            break

        excerpt_num += 1

        # show the excerpt once
        console.print("━" * 60)
        console.print(
            f"[bold]EXCERPT {excerpt_num}/{len(excerpt_dims)}[/bold]  "
            f"[dim]section: {excerpt['heading']}[/dim]  "
            f"[dim]({len(relevant_dims)} dimension{'s' if len(relevant_dims) > 1 else ''})[/dim]"
        )
        console.print("━" * 60)
        console.print(Panel(excerpt["text"], border_style="blue"))

        # ask about each relevant dimension
        for dim in relevant_dims:
            dim_name = dim["name"]

            # resume: skip already-labeled pairs
            if (excerpt["text"], dim_name) in labeled_pairs:
                continue

            console.print()
            console.print(
                f"  [bold]{dim_name}[/bold]: "
                f"[dim]{dim['definition']}[/dim]"
            )

            while True:
                choice = Prompt.ask(
                    "  does this text satisfy the rule? [green]\[p]ass[/green]  [red]\[f]ail[/red]  [yellow]\[s]kip[/yellow]  [dim]\[q]uit[/dim]",
                    choices=["p", "f", "s", "q"],
                    show_choices=False
                ).lower()

                if choice == "q":
                    console.print("\n[yellow]labeling session ended by user[/yellow]")
                    quit_requested = True
                    break

                if choice == "s":
                    skip_count += 1
                    break

                if choice in ["p", "f"]:
                    label = "PASS" if choice == "p" else "FAIL"

                    critique = Prompt.ask("  [dim]why? (1-2 sentences)[/dim]", default="").strip()

                    if label == "PASS":
                        pass_count += 1
                    else:
                        fail_count += 1

                    dim_labeled[dim_name] = dim_labeled.get(dim_name, 0) + 1
                    label_data = {
                        "text": excerpt["text"],
                        "dimension": dim_name,
                        "human_label": label,
                        "critique": critique,
                        "source": "real",
                        "model_label": None,
                        "model_reason": None
                    }
                    save_label(output_path, label_data)
                    break

            if quit_requested:
                break

        if not quit_requested:
            dim_progress = "  ".join(
                f"{d['name']} {dim_labeled.get(d['name'], 0)}"
                for d in dimensions
            )
            console.print(
                f"\n[dim]{excerpt_num}/{len(excerpt_dims)} excerpts | "
                f"[green]P[/green] {pass_count}  [red]F[/red] {fail_count}  "
                f"[yellow]>>[/yellow] {skip_count}\n"
                f"  {dim_progress}[/dim]\n"
            )

    console.print("[bold green]real excerpt labeling complete![/bold green]")


def label_synthetic_examples(
    examples: List[Dict],
    dimensions: List[Dict],
    output_path: Path,
    merge_with_real: Optional[Path] = None
):
    """interactive labeling for synthetic examples."""
    all_examples = examples  # keep full list for per-dimension counting
    # resume support: skip already-labeled examples
    existing = load_existing_labels(output_path)
    already_done = len(existing)
    if already_done > 0:
        examples = examples[already_done:]
        console.print(f"\n[bold cyan]resuming synthetic example labeling[/bold cyan]")
        console.print(f"  already labeled: {already_done}, remaining: {len(examples)}\n")
    else:
        console.print(f"\n[bold cyan]starting synthetic example labeling[/bold cyan]")
        console.print(f"  examples: {len(examples)}\n")

    total = already_done + len(examples)
    current = already_done
    pass_count = sum(1 for e in existing if e.get("human_label") == "PASS")
    fail_count = sum(1 for e in existing if e.get("human_label") == "FAIL")
    skip_count = 0
    agreements = sum(1 for e in existing if e.get("human_label") == e.get("model_label"))
    disagreements = sum(1 for e in existing if e.get("human_label") and e.get("human_label") != e.get("model_label"))
    skip_tracker: Dict[str, int] = {}
    dim_lookup = {d["name"]: d for d in dimensions}

    # per-dimension progress tracking
    dim_totals: Dict[str, int] = {}
    dim_labeled: Dict[str, int] = {}
    for ex in all_examples:
        dim_totals[ex["dimension"]] = dim_totals.get(ex["dimension"], 0) + 1
        dim_labeled[ex["dimension"]] = 0
    for ex in existing:
        d = ex.get("dimension", "")
        if d in dim_labeled:
            dim_labeled[d] += 1
    console.print(
        "[bold]What you're doing:[/bold] judge each excerpt against the dimension's rule.\n"
        "Read the text, read the rule, decide: does it satisfy the rule?\n\n"
        "[bold]The model's opinion is shown AFTER you label[/bold], so it won't\n"
        "bias your judgment. Disagreements are the most valuable data.\n"
    )

    for example in examples:
        current += 1
        dimension = example["dimension"]
        text = example["text"]
        model_label = example["model_label"]
        model_reason = example["model_reason"]

        # track skips per dimension
        if dimension not in skip_tracker:
            skip_tracker[dimension] = 0

        # look up dimension definition
        dim_def = dim_lookup.get(dimension, {}).get("definition", "")

        # display example
        console.print("─" * 60)
        console.print(f"[bold]{dimension}[/bold]  [dim][{current} / {total}][/dim]")
        console.print(f"[dim]{dim_def}[/dim]")
        console.print("─" * 60)
        console.print(Panel(text, border_style="blue"))

        # get label
        while True:
            choice = Prompt.ask(
                "does this text satisfy the rule? [green]\[p]ass[/green]  [red]\[f]ail[/red]  [yellow]\[s]kip[/yellow]  [dim]\[q]uit[/dim]",
                choices=["p", "f", "s", "q"],
                show_choices=False
            ).lower()

            if choice == "q":
                console.print("\n[yellow]labeling session ended by user[/yellow]")
                return

            if choice == "s":
                skip_count += 1
                skip_tracker[dimension] += 1

                # check skip threshold
                if skip_tracker[dimension] >= 4:
                    console.print("\n[yellow]" + "─" * 60 + "[/yellow]")
                    console.print(f"[yellow][!] you've skipped {skip_tracker[dimension]} examples for '{dimension}'.[/yellow]")
                    console.print("[yellow]    this usually means the dimension definition is too vague.[/yellow]")
                    console.print("[yellow]    consider: is this dimension atomic? can you write its PASS/FAIL in one sentence?[/yellow]")
                    action = Prompt.ask(
                        "[yellow]    options:[/yellow] [cyan]\[r]efine dimension definition[/cyan]  [green]\[c]ontinue labeling[/green]  [red]\[d]rop dimension[/red]",
                        choices=["r", "c", "d"],
                        show_choices=False
                    ).lower()

                    if action == "r":
                        console.print("\n[cyan]>> open examples/dimensions.md and refine the definition, then restart labeling.[/cyan]\n")
                        return
                    elif action == "d":
                        console.print(f"\n[red]>> dropping dimension '{dimension}' - remove it from dimensions.md and restart.[/red]\n")
                        return
                    else:
                        skip_tracker[dimension] = 0  # reset counter

                    console.print("[yellow]" + "─" * 60 + "[/yellow]\n")

                break

            if choice in ["p", "f"]:
                label = "PASS" if choice == "p" else "FAIL"

                # get critique
                critique = Prompt.ask("  [dim]why? (1-2 sentences)[/dim]", default="").strip()

                # reveal model opinion after user labels
                if label == model_label:
                    agreements += 1
                    console.print(f"  [dim]model agreed: {model_label} -- {model_reason}[/dim]")
                else:
                    disagreements += 1
                    console.print(
                        f"\n  [bold yellow]<< DISAGREEMENT >>[/bold yellow] "
                        f"you said [bold]{label}[/bold], model said [bold]{model_label}[/bold]\n"
                        f"  [dim]model reason: {model_reason}[/dim]\n"
                        f"  [dim](disagreements are the most valuable training data)[/dim]"
                    )

                # update counts
                if label == "PASS":
                    pass_count += 1
                else:
                    fail_count += 1

                # save label
                dim_labeled[dimension] = dim_labeled.get(dimension, 0) + 1
                label_data = {
                    "text": text,
                    "dimension": dimension,
                    "human_label": label,
                    "critique": critique,
                    "source": "synthetic",
                    "model_label": model_label,
                    "model_reason": model_reason
                }
                save_label(output_path, label_data)
                break

        # show session stats with per-dimension progress
        total_labeled = pass_count + fail_count
        agreement_pct = int((agreements / total_labeled * 100)) if total_labeled > 0 else 0
        dim_progress = "  ".join(
            f"{d} {dim_labeled.get(d, 0)}/{dim_totals[d]}"
            for d in dim_totals
        )
        console.print(
            f"\n[dim]{current}/{total} | "
            f"[green]P[/green] {pass_count}  [red]F[/red] {fail_count}  "
            f"[yellow]>>[/yellow] {skip_count} | "
            f"agree {agreement_pct}%\n"
            f"  {dim_progress}[/dim]\n"
        )

    console.print("[bold green]synthetic example labeling complete![/bold green]")

    # merge with real excerpts if requested
    if merge_with_real and merge_with_real.exists():
        console.print(f"\n[cyan]merging with real excerpts from {merge_with_real}...[/cyan]")

        # read both files
        real_labels = load_existing_labels(merge_with_real)
        synthetic_labels = load_existing_labels(output_path)

        # determine final output path
        final_output = output_path.parent / "labels.jsonl"

        # write merged output
        with open(final_output, 'w') as f:
            for label in real_labels:
                f.write(json.dumps(label) + "\n")
            for label in synthetic_labels:
                f.write(json.dumps(label) + "\n")

        console.print(f"[green]merged {len(real_labels)} real + {len(synthetic_labels)} synthetic -> {final_output}[/green]")


def auto_accept_synthetic(examples: List[Dict], output_path: Path):
    """auto-accept model labels as human labels without interactive review."""
    existing = load_existing_labels(output_path)
    already_done = len(existing)
    if already_done >= len(examples):
        console.print(f"[green]all {len(examples)} examples already labeled[/green]")
        return
    if already_done > 0:
        examples = examples[already_done:]
        console.print(f"[cyan]skipping {already_done} already labeled, processing {len(examples)} remaining[/cyan]")

    from collections import Counter
    counts: Dict[str, Dict[str, int]] = {}
    for ex in examples:
        d = ex["dimension"]
        l = ex["model_label"]
        if d not in counts:
            counts[d] = {"PASS": 0, "FAIL": 0}
        counts[d][l] += 1

        label_data = {
            "text": ex["text"],
            "dimension": d,
            "human_label": l,
            "critique": ex.get("model_reason", ""),
            "source": "synthetic",
            "model_label": l,
            "model_reason": ex.get("model_reason", "")
        }
        save_label(output_path, label_data)

    # summary table
    table = Table(title="auto-accepted labels", show_header=True)
    table.add_column("dimension")
    table.add_column("PASS", justify="right", style="green")
    table.add_column("FAIL", justify="right", style="red")
    for d in sorted(counts):
        table.add_row(d, str(counts[d]["PASS"]), str(counts[d]["FAIL"]))
    console.print(table)
    console.print(f"\n[green]{len(examples)} labels auto-accepted to {output_path}[/green]")


def dry_run_real(excerpts: List[Dict], dimensions: List[Dict]):
    """print excerpt+dimension pairs that would be presented, then exit.

    outputs JSON array to stdout for building a --batch file.
    """
    excerpt_dims = []
    for exc in excerpts:
        relevant_dims = [d for d in dimensions if is_relevant(exc["text"], d["keywords"])]
        if relevant_dims:
            excerpt_dims.append((exc, relevant_dims))

    pairs = []
    for excerpt, relevant_dims in excerpt_dims:
        for dim in relevant_dims:
            pairs.append({
                "dimension": dim["name"],
                "heading": excerpt["heading"],
                "label": "",
                "critique": "",
            })

    # print summary table
    console.print(f"\n[bold cyan]dry run: {len(pairs)} pairs would be presented[/bold cyan]\n")
    table = Table(show_header=True, header_style="bold")
    table.add_column("excerpt heading")
    table.add_column("dimension")
    for p in pairs:
        table.add_row(p["heading"], p["dimension"])
    console.print(table)

    # output JSON to stdout for piping to a file
    import json as _json
    print(_json.dumps(pairs, indent=2))


def batch_label_real(
    excerpts: List[Dict],
    dimensions: List[Dict],
    output_path: Path,
    batch_path: Path,
):
    """non-interactive labeling from a pre-computed JSON answers file."""
    # load batch answers and build lookup
    with open(batch_path) as f:
        batch_answers = json.load(f)

    lookup = {}
    for answer in batch_answers:
        key = (answer["dimension"], answer.get("heading", ""))
        lookup[key] = answer

    # run same relevance filtering as interactive mode
    excerpt_dims = []
    for exc in excerpts:
        relevant_dims = [d for d in dimensions if is_relevant(exc["text"], d["keywords"])]
        if relevant_dims:
            excerpt_dims.append((exc, relevant_dims))

    total_pairs = sum(len(dims) for _, dims in excerpt_dims)
    console.print(f"\n[bold cyan]batch mode: {total_pairs} pairs, {len(lookup)} answers loaded[/bold cyan]\n")

    matched = 0
    skipped = 0
    pass_count = 0
    fail_count = 0

    for excerpt, relevant_dims in excerpt_dims:
        for dim in relevant_dims:
            key = (dim["name"], excerpt["heading"])
            answer = lookup.get(key)

            if answer is None:
                console.print(f"[yellow]  no answer for ({dim['name']}, {excerpt['heading']}) -- skipping[/yellow]")
                skipped += 1
                continue

            label_str = answer.get("label", "").upper()
            if label_str not in ("PASS", "FAIL"):
                console.print(f"[yellow]  invalid label '{label_str}' for ({dim['name']}, {excerpt['heading']}) -- skipping[/yellow]")
                skipped += 1
                continue

            critique = answer.get("critique", "")

            if label_str == "PASS":
                pass_count += 1
            else:
                fail_count += 1

            label_data = {
                "text": excerpt["text"],
                "dimension": dim["name"],
                "human_label": label_str,
                "critique": critique,
                "source": "real",
                "model_label": None,
                "model_reason": None,
            }
            save_label(output_path, label_data)
            matched += 1

    # summary
    table = Table(title="batch labeling summary", show_header=True)
    table.add_column("metric", style="cyan")
    table.add_column("count", justify="right")
    table.add_row("matched", str(matched))
    table.add_row("skipped (no answer)", str(skipped))
    table.add_row("PASS", str(pass_count))
    table.add_row("FAIL", str(fail_count))
    console.print(table)
    console.print(f"\n[green]{matched} labels written to {output_path}[/green]")


def write_prelabel_request(
    excerpts: List[Dict],
    dimensions: List[Dict],
    output_path: Path = Path("examples/_prelabel_request.jsonl"),
):
    """write prelabel request file for assisted labeling.

    generates one line per excerpt+dimension pair (after relevance filtering)
    for an external model to pre-label before the human reviews.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    excerpt_dims = []
    for exc in excerpts:
        relevant_dims = [d for d in dimensions if is_relevant(exc["text"], d["keywords"])]
        if relevant_dims:
            excerpt_dims.append((exc, relevant_dims))

    total_pairs = sum(len(dims) for _, dims in excerpt_dims)

    with open(output_path, 'w') as f:
        for excerpt, relevant_dims in excerpt_dims:
            for dim in relevant_dims:
                entry = {
                    "text": excerpt["text"],
                    "dimension": dim["name"],
                    "heading": excerpt["heading"],
                    "definition": dim["definition"],
                }
                f.write(json.dumps(entry) + "\n")

    console.print(f"\n[bold cyan]assist mode: wrote {total_pairs} pairs to {output_path}[/bold cyan]")
    console.print(f"\n[bold]next:[/bold] pre-label these pairs with codex or claude code.")
    console.print(f"  read {output_path}, evaluate each pair, write results to:")
    console.print(f"  examples/_prelabel_response.jsonl")
    console.print(f'  format: {{"dimension": "name", "heading": "section", "model_label": "PASS|FAIL", "model_reason": "1 sentence"}}')
    console.print(f"\n  then re-run with --assist to enter review mode.")

    return total_pairs


def assisted_label_real(
    excerpts: List[Dict],
    dimensions: List[Dict],
    output_path: Path,
    response_path: Path,
):
    """assisted interactive labeling with pre-labels from external model.

    shows model suggestion for each pair. Enter accepts, p/f overrides.
    """
    # load pre-labels and build lookup
    prelabels = {}
    with open(response_path) as f:
        for line in f:
            if line.strip():
                entry = json.loads(line)
                key = (entry["dimension"], entry.get("heading", ""))
                prelabels[key] = entry

    # relevance filtering (same as interactive)
    excerpt_dims = []
    for exc in excerpts:
        relevant_dims = [d for d in dimensions if is_relevant(exc["text"], d["keywords"])]
        if relevant_dims:
            excerpt_dims.append((exc, relevant_dims))

    total_pairs = sum(len(dims) for _, dims in excerpt_dims)
    matched_prelabels = sum(
        1 for exc, dims in excerpt_dims
        for d in dims if (d["name"], exc["heading"]) in prelabels
    )

    console.print(f"\n[bold cyan]assisted review mode[/bold cyan]")
    console.print(f"  {total_pairs} pairs to review, {matched_prelabels} have pre-labels")
    console.print(
        "\n  [green][enter][/green] accept model suggestion  "
        "[green][p]ass[/green]  [red][f]ail[/red]  "
        "[yellow][s]kip[/yellow]  [dim][q]uit[/dim]\n"
    )

    # resume support
    existing = load_existing_labels(output_path)
    labeled_pairs = {(e["text"], e["dimension"]) for e in existing if "text" in e and "dimension" in e}

    accepted = 0
    overridden = 0
    skipped = 0
    no_prelabel = 0
    quit_requested = False
    excerpt_num = 0

    for excerpt, relevant_dims in excerpt_dims:
        if quit_requested:
            break
        excerpt_num += 1

        console.print("━" * 60)
        console.print(
            f"[bold]EXCERPT {excerpt_num}/{len(excerpt_dims)}[/bold]  "
            f"[dim]section: {excerpt['heading']}[/dim]"
        )
        console.print("━" * 60)
        console.print(Panel(excerpt["text"], border_style="blue"))

        for dim in relevant_dims:
            if quit_requested:
                break
            if (excerpt["text"], dim["name"]) in labeled_pairs:
                continue

            key = (dim["name"], excerpt["heading"])
            pre = prelabels.get(key)

            console.print(f"\n  [bold]{dim['name']}[/bold]: [dim]{dim['definition']}[/dim]")

            if pre:
                ml = pre.get("model_label", "?")
                mr = pre.get("model_reason", "")
                color = "green" if ml == "PASS" else "red"
                console.print(f"  [dim]model suggestion:[/dim] [{color}]{ml}[/{color}] -- {mr}")

                choice = Prompt.ask(
                    "  [green][enter][/green] accept  [green][p]ass[/green]  [red][f]ail[/red]  [yellow][s]kip[/yellow]  [dim][q]uit[/dim]",
                    choices=["", "p", "f", "s", "q"],
                    default="",
                    show_choices=False,
                ).lower()

                if choice == "q":
                    quit_requested = True
                    break
                if choice == "s":
                    skipped += 1
                    continue
                if choice == "":
                    # accept model suggestion
                    label_data = {
                        "text": excerpt["text"],
                        "dimension": dim["name"],
                        "human_label": ml,
                        "critique": mr,
                        "source": "real",
                        "model_label": ml,
                        "model_reason": mr,
                    }
                    save_label(output_path, label_data)
                    accepted += 1
                    continue
                # override
                label = "PASS" if choice == "p" else "FAIL"
                console.print(
                    f"\n  [bold yellow]<< OVERRIDE >>[/bold yellow] "
                    f"you said [bold]{label}[/bold], model said [bold]{ml}[/bold]"
                )
                critique = Prompt.ask("  [dim]why? (1-2 sentences)[/dim]", default="").strip()
                label_data = {
                    "text": excerpt["text"],
                    "dimension": dim["name"],
                    "human_label": label,
                    "critique": critique,
                    "source": "real",
                    "model_label": ml,
                    "model_reason": mr,
                }
                save_label(output_path, label_data)
                overridden += 1
            else:
                # no pre-label -- fall back to manual
                no_prelabel += 1
                while True:
                    choice = Prompt.ask(
                        "  [green][p]ass[/green]  [red][f]ail[/red]  [yellow][s]kip[/yellow]  [dim][q]uit[/dim]",
                        choices=["p", "f", "s", "q"],
                        show_choices=False,
                    ).lower()
                    if choice == "q":
                        quit_requested = True
                        break
                    if choice == "s":
                        skipped += 1
                        break
                    if choice in ("p", "f"):
                        label = "PASS" if choice == "p" else "FAIL"
                        critique = Prompt.ask("  [dim]why?[/dim]", default="").strip()
                        label_data = {
                            "text": excerpt["text"],
                            "dimension": dim["name"],
                            "human_label": label,
                            "critique": critique,
                            "source": "real",
                            "model_label": None,
                            "model_reason": None,
                        }
                        save_label(output_path, label_data)
                        break

    # summary
    total_labeled = accepted + overridden
    override_pct = int(overridden / total_labeled * 100) if total_labeled > 0 else 0
    table = Table(title="assisted labeling summary", show_header=True)
    table.add_column("metric", style="cyan")
    table.add_column("count", justify="right")
    table.add_row("accepted (enter)", str(accepted))
    table.add_row("overridden (p/f)", str(overridden))
    table.add_row("skipped", str(skipped))
    table.add_row("no pre-label (manual)", str(no_prelabel))
    table.add_row("override rate", f"{override_pct}%")
    console.print(table)

    if override_pct > 30:
        console.print(
            "\n[yellow]override rate > 30% -- pre-labels may be unreliable.\n"
            "consider switching to full interactive mode (without --assist).[/yellow]"
        )

    console.print(f"\n[green]assisted labeling complete! {total_labeled} labels written to {output_path}[/green]")


def main():
    parser = argparse.ArgumentParser(
        description="interactive CLI labeler for real and synthetic excerpts"
    )
    parser.add_argument(
        "--source",
        required=True,
        choices=["real", "synthetic"],
        help="source type: real excerpts from draft or synthetic examples"
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="input file path (artifact_draft.md for real, examples/unlabeled.jsonl for synthetic)"
    )
    parser.add_argument(
        "--dimensions",
        type=Path,
        default=Path("examples/dimensions.md"),
        help="path to dimensions.md (default: examples/dimensions.md)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="output path (default: examples/real_excerpts.jsonl for real, examples/synthetic_labels.jsonl for synthetic; merged output goes to examples/labels.jsonl)"
    )
    parser.add_argument(
        "--auto-accept",
        action="store_true",
        help="(synthetic only) auto-accept model labels without interactive review"
    )
    parser.add_argument(
        "--batch",
        type=Path,
        default=None,
        help="(real only) path to JSON file with pre-computed labels. "
             "format: [{\"dimension\": \"...\", \"heading\": \"...\", \"label\": \"PASS|FAIL\", \"critique\": \"...\"}]. "
             "use --dry-run first to see which pairs will be presented."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="(real only) print excerpt+dimension pairs that would be presented, "
             "then exit. outputs JSON to stdout for building a --batch file."
    )
    parser.add_argument(
        "--assist",
        action="store_true",
        help="(real only) assisted labeling: first run generates prelabel request "
             "file, then an external model pre-labels, then second run enters "
             "review mode with Enter-to-accept for obvious cases."
    )

    args = parser.parse_args()

    # set default output path
    if args.output is None:
        if args.source == "real":
            args.output = Path("examples/real_excerpts.jsonl")
        else:
            args.output = Path("examples/synthetic_labels.jsonl")

    # ensure output directory exists
    args.output.parent.mkdir(parents=True, exist_ok=True)

    # parse dimensions (returns list of dicts with keywords)
    dimensions = parse_dimensions(args.dimensions)
    dim_names = [d["name"] for d in dimensions]

    if args.source == "real":
        # parse artifact into section-based excerpts
        excerpts = parse_artifact_to_excerpts(args.input)
        console.print(f"[cyan]parsed {len(excerpts)} section excerpts from {args.input}[/cyan]")

        if args.dry_run:
            dry_run_real(excerpts, dimensions)
            return

        if args.batch:
            batch_label_real(excerpts, dimensions, args.output, args.batch)
            return

        if getattr(args, 'assist', False):
            response_path = args.output.parent / "_prelabel_response.jsonl"
            if not response_path.exists():
                # step 1: generate request for external model
                write_prelabel_request(excerpts, dimensions,
                                       args.output.parent / "_prelabel_request.jsonl")
                return
            else:
                # step 2: assisted review with pre-labels
                assisted_label_real(excerpts, dimensions, args.output, response_path)
                return

        # label real excerpts interactively (with relevance filtering)
        label_real_excerpts(excerpts, dimensions, args.output)

    else:  # synthetic
        # load synthetic examples
        examples = load_synthetic_examples(args.input)
        console.print(f"[cyan]loaded {len(examples)} synthetic examples from {args.input}[/cyan]")

        # check if real_excerpts.jsonl exists for merging
        real_excerpts_path = args.output.parent / "real_excerpts.jsonl"
        merge_with = real_excerpts_path if real_excerpts_path.exists() else None

        if args.auto_accept:
            auto_accept_synthetic(examples, args.output)
        else:
            label_synthetic_examples(examples, dimensions, args.output, merge_with)


if __name__ == "__main__":
    main()
