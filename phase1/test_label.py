#!/usr/bin/env python3
"""tests for 3_label.py -- focuses on non-interactive functions."""

import json
import os
import sys
import tempfile
from pathlib import Path

# add parent to path so we can import the module
sys.path.insert(0, str(Path(__file__).parent))

# minimal dimensions.md content for testing
DIMS_MD = """# Dimensions

## LLM Judge Dimensions

### problem_specificity
- Definition: must name a specific buyer role and company size
- PASS example: "engineering directors at 20-50 person startups"
- FAIL example: "many teams struggle"

### pricing_rationale
- Definition: every price must have a value justification
- PASS example: "$15/user replaces Jira ($7) + Clockify ($4)"
- FAIL example: "our pricing is competitive"
"""

# minimal artifact for testing
ARTIFACT_MD = """# Test Product

## The Problem

Many teams struggle with project management. Remote work has made it harder.

## Pricing

- Starter: $8/user/month

Our pricing is competitive and offers great value.
"""


def setup_test_dir():
    """create a temp directory with test fixtures."""
    tmpdir = tempfile.mkdtemp(prefix="autorefine_test_")
    examples_dir = Path(tmpdir) / "examples"
    examples_dir.mkdir()

    dims_path = examples_dir / "dimensions.md"
    dims_path.write_text(DIMS_MD)

    artifact_path = Path(tmpdir) / "artifact_draft.md"
    artifact_path.write_text(ARTIFACT_MD)

    return tmpdir, dims_path, artifact_path


def test_write_prelabel_request():
    """test that write_prelabel_request generates correct JSONL."""
    from importlib.machinery import SourceFileLoader
    label_mod = SourceFileLoader("label", str(Path(__file__).parent / "3_label.py")).load_module()

    tmpdir, dims_path, artifact_path = setup_test_dir()

    try:
        dimensions = label_mod.parse_dimensions(dims_path)
        excerpts = label_mod.parse_artifact_to_excerpts(artifact_path)
        output_path = Path(tmpdir) / "examples" / "_prelabel_request.jsonl"

        count = label_mod.write_prelabel_request(excerpts, dimensions, output_path)

        # verify file exists
        assert output_path.exists(), f"request file not created at {output_path}"

        # verify content
        lines = [json.loads(l) for l in output_path.read_text().strip().split("\n")]
        assert len(lines) == count, f"line count mismatch: {len(lines)} != {count}"
        assert count > 0, "no pairs generated"

        # verify structure
        for line in lines:
            assert "text" in line, "missing 'text' field"
            assert "dimension" in line, "missing 'dimension' field"
            assert "heading" in line, "missing 'heading' field"
            assert "definition" in line, "missing 'definition' field"
            assert line["dimension"] in ["problem_specificity", "pricing_rationale"], \
                f"unexpected dimension: {line['dimension']}"

        print(f"  write_prelabel_request: {count} pairs generated -- PASS")
        return True

    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_batch_label_real():
    """test that batch_label_real writes correct output from answers file."""
    from importlib.machinery import SourceFileLoader
    label_mod = SourceFileLoader("label", str(Path(__file__).parent / "3_label.py")).load_module()

    tmpdir, dims_path, artifact_path = setup_test_dir()

    try:
        dimensions = label_mod.parse_dimensions(dims_path)
        excerpts = label_mod.parse_artifact_to_excerpts(artifact_path)

        # create answers file
        answers = [
            {"dimension": "problem_specificity", "heading": "The Problem", "label": "FAIL", "critique": "too vague"},
            {"dimension": "pricing_rationale", "heading": "Pricing", "label": "FAIL", "critique": "no justification"},
        ]
        answers_path = Path(tmpdir) / "answers.json"
        answers_path.write_text(json.dumps(answers))

        output_path = Path(tmpdir) / "examples" / "test_labels.jsonl"
        label_mod.batch_label_real(excerpts, dimensions, output_path, answers_path)

        # verify output
        assert output_path.exists(), "output file not created"
        labels = [json.loads(l) for l in output_path.read_text().strip().split("\n")]

        # should have at least the 2 pairs that match
        matched = [l for l in labels if l["dimension"] in ["problem_specificity", "pricing_rationale"]]
        assert len(matched) >= 2, f"expected >= 2 matched labels, got {len(matched)}"

        for label in matched:
            assert label["human_label"] == "FAIL"
            assert label["source"] == "real"
            assert label["critique"] != ""

        print(f"  batch_label_real: {len(labels)} labels written -- PASS")
        return True

    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_dry_run_real():
    """test that dry_run_real outputs pairs without error."""
    from importlib.machinery import SourceFileLoader
    label_mod = SourceFileLoader("label", str(Path(__file__).parent / "3_label.py")).load_module()

    tmpdir, dims_path, artifact_path = setup_test_dir()

    try:
        dimensions = label_mod.parse_dimensions(dims_path)
        excerpts = label_mod.parse_artifact_to_excerpts(artifact_path)

        # dry_run prints to stdout -- just verify it doesn't crash
        label_mod.dry_run_real(excerpts, dimensions)
        print("  dry_run_real: no errors -- PASS")
        return True

    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_assisted_label_via_pexpect():
    """test assisted labeling with mock prelabels via pexpect (Enter to accept all)."""
    import pexpect

    tmpdir, dims_path, artifact_path = setup_test_dir()

    try:
        # step 1: generate prelabel request
        script = str(Path(__file__).parent / "3_label.py")
        cmd = (
            f"python3 {script} --source real --input {artifact_path} "
            f"--dimensions {dims_path} --output {tmpdir}/examples/assist_labels.jsonl --assist"
        )
        child = pexpect.spawn("/bin/bash", ["-c", cmd], encoding="utf-8", timeout=10,
                              env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"})
        child.expect(pexpect.EOF, timeout=10)
        child.close()

        request_path = Path(tmpdir) / "examples" / "_prelabel_request.jsonl"
        assert request_path.exists(), "prelabel request not created by --assist"

        # step 2: create mock prelabel response (simulating codex output)
        response_path = Path(tmpdir) / "examples" / "_prelabel_response.jsonl"
        requests = [json.loads(l) for l in request_path.read_text().strip().split("\n")]
        with open(response_path, "w") as f:
            for req in requests:
                resp = {
                    "dimension": req["dimension"],
                    "heading": req["heading"],
                    "model_label": "FAIL",
                    "model_reason": f"mock: {req['dimension']} fails in this section",
                }
                f.write(json.dumps(resp) + "\n")

        # step 3: run assisted review -- press Enter for all (accept)
        cmd2 = (
            f"python3 {script} --source real --input {artifact_path} "
            f"--dimensions {dims_path} --output {tmpdir}/examples/assist_labels.jsonl --assist"
        )
        child2 = pexpect.spawn("/bin/bash", ["-c", cmd2], encoding="utf-8", timeout=15,
                               env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"},
                               dimensions=(50, 200))

        # send Enter for each pair (accept model suggestion)
        for _ in range(len(requests)):
            try:
                child2.expect(r"accept|uit", timeout=10)
                child2.sendline("")  # Enter = accept
            except (pexpect.TIMEOUT, pexpect.EOF):
                break

        try:
            child2.expect(pexpect.EOF, timeout=10)
        except pexpect.TIMEOUT:
            pass
        child2.close()

        # verify output
        output_path = Path(tmpdir) / "examples" / "assist_labels.jsonl"
        assert output_path.exists(), "assisted labels output not created"
        labels = [json.loads(l) for l in output_path.read_text().strip().split("\n") if l.strip()]
        assert len(labels) >= 1, f"expected >= 1 labels, got {len(labels)}"

        for label in labels:
            assert label["human_label"] == "FAIL", f"expected FAIL (accepted), got {label['human_label']}"
            assert label["model_label"] == "FAIL", "model_label should be populated"
            assert label["model_reason"].startswith("mock:"), "model_reason should match mock"

        print(f"  assisted_label_via_pexpect: {len(labels)} labels accepted -- PASS")
        return True

    finally:
        import shutil
        shutil.rmtree(tmpdir)


def test_codex_mock_roundtrip():
    """test that prelabel request format matches expected codex response format.

    simulates the full codex integration: request -> mock codex response -> verify
    the response format is compatible with assisted_label_real's expectations.
    """
    from importlib.machinery import SourceFileLoader
    label_mod = SourceFileLoader("label", str(Path(__file__).parent / "3_label.py")).load_module()

    tmpdir, dims_path, artifact_path = setup_test_dir()

    try:
        dimensions = label_mod.parse_dimensions(dims_path)
        excerpts = label_mod.parse_artifact_to_excerpts(artifact_path)

        # generate request
        req_path = Path(tmpdir) / "examples" / "_prelabel_request.jsonl"
        label_mod.write_prelabel_request(excerpts, dimensions, req_path)

        requests = [json.loads(l) for l in req_path.read_text().strip().split("\n")]

        # simulate codex response -- must have: dimension, heading, model_label, model_reason
        resp_path = Path(tmpdir) / "examples" / "_prelabel_response.jsonl"
        with open(resp_path, "w") as f:
            for req in requests:
                resp = {
                    "dimension": req["dimension"],
                    "heading": req["heading"],
                    "model_label": "FAIL",
                    "model_reason": f"the text fails {req['dimension']} because it lacks specificity",
                }
                f.write(json.dumps(resp) + "\n")

        # verify response can be loaded by assisted_label_real's lookup logic
        prelabels = {}
        with open(resp_path) as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    key = (entry["dimension"], entry.get("heading", ""))
                    prelabels[key] = entry

        # verify all request pairs have matching responses
        for req in requests:
            key = (req["dimension"], req["heading"])
            assert key in prelabels, f"missing response for {key}"
            resp = prelabels[key]
            assert resp["model_label"] in ("PASS", "FAIL"), f"invalid label: {resp['model_label']}"
            assert resp["model_reason"], "empty model_reason"

        print(f"  codex_mock_roundtrip: {len(requests)} request/response pairs verified -- PASS")
        return True

    finally:
        import shutil
        shutil.rmtree(tmpdir)


if __name__ == "__main__":
    print("running 3_label.py tests...\n")
    results = []
    for test_fn in [test_write_prelabel_request, test_batch_label_real, test_dry_run_real,
                    test_assisted_label_via_pexpect, test_codex_mock_roundtrip]:
        try:
            results.append(test_fn())
        except Exception as e:
            print(f"  {test_fn.__name__}: FAIL -- {e}")
            results.append(False)

    passed = sum(1 for r in results if r)
    failed = len(results) - passed
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
