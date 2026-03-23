#!/usr/bin/env python3
"""end-to-end test for autorefine phase 2 loop with all features.

simulates the claude code role (writing judge responses, mutation responses,
adversarial responses) so the full state machine can be exercised without an LLM.

run: python3 test_flow.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / 'run.py'
PASSED = 0
FAILED = 0


def run_cmd(*args, cwd=None, check=True):
    """run a phase2/run.py subcommand, return CompletedProcess."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)] + list(args),
        capture_output=True, text=True, cwd=cwd
    )
    if check and result.returncode != 0:
        print(f"  COMMAND FAILED: {' '.join(args)}")
        print(f"  stdout: {result.stdout[:500]}")
        print(f"  stderr: {result.stderr[:500]}")
        raise RuntimeError(f"command failed: {' '.join(args)}")
    return result


def check(condition, msg):
    """assert with tracking."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
    else:
        FAILED += 1
        # get caller info
        import traceback
        frame = traceback.extract_stack()[-2]
        print(f"  FAIL [{frame.lineno}]: {msg}")


class TestHarness:
    def __init__(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix='autorefine_test_'))
        self.setup_fixtures()

    def setup_fixtures(self):
        """create minimal project directory with all fixture files."""
        d = self.tmpdir

        # artifact.md
        (d / 'artifact.md').write_text(
            "# Lemonade Stand Business Plan\n\n"
            "Revenue: $100/day [ASSUMPTION]\n"
            "Costs: $30/day\n"
            "Profit: $70/day\n\n"
            "## Risk Register\n\n"
            "| Risk | Mitigation |\n"
            "|------|------------|\n"
            "| Rain | Tent |\n"
            "| Competition | Lower prices |\n\n"
            "## Appendix: Notes\n\n"
            "Do not modify this section.\n"
        )

        # improve.md
        (d / 'improve.md').write_text(
            "## What is being improved\n\n"
            "A lemonade stand business plan.\n\n"
            "## What \"good\" looks like\n\n"
            "Numbers are internally consistent. Risks have mitigations.\n\n"
            "## Hard constraints\n\n"
            "- \"## Appendix: Notes\"\n\n"
            "## End-to-end question\n\n"
            "Would an investor fund this lemonade stand?\n\n"
            "## Adversarial persona\n\n"
            "A skeptical investor who has seen 100 lemonade stands fail.\n"
        )

        # context.md
        (d / 'context.md').write_text(
            "- Lemon cost: $0.50 each\n"
            "- Average cups per day: 50\n"
            "- Price per cup: $2\n"
        )

        # examples/dimensions.md
        (d / 'examples').mkdir()
        (d / 'examples' / 'dimensions.md').write_text(
            "## Cheap Checks\n\n"
            "### risk_register_exists\n"
            "- Rule: search for \"Risk Register\"\n\n"
            "## LLM Judge Dimensions\n\n"
            "### number_consistency\n\n"
            "### risk_quality\n"
        )

        # judge_prompts/
        jp = d / 'judge_prompts'
        jp.mkdir()
        (jp / 'number_consistency.md').write_text(
            "---\n"
            "dimension: number_consistency\n"
            "test_tpr: 1.00\n"
            "test_tnr: 1.00\n"
            "weight: 2.0\n"
            "---\n\n"
            "check if all numbers in the document are internally consistent.\n"
        )
        (jp / 'risk_quality.md').write_text(
            "---\n"
            "dimension: risk_quality\n"
            "test_tpr: 1.00\n"
            "test_tnr: 1.00\n"
            "weight: 1.0\n"
            "---\n\n"
            "check if the risk register has meaningful mitigations.\n"
        )

    def iter_dir(self, iteration):
        return self.tmpdir / 'runs' / f'iter_{iteration:03d}'

    def write_judge_response(self, iteration, verdicts):
        """write simulated judge_response.jsonl. verdicts = {dim: 'PASS: ...' | 'FAIL: ...'}"""
        path = self.iter_dir(iteration) / 'judge_response.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            for dim, verdict in verdicts.items():
                f.write(json.dumps({'dimension': dim, 'verdict': verdict}) + '\n')

    def write_after_judge_response(self, iteration, verdicts):
        """write simulated judge_response_after.jsonl."""
        path = self.iter_dir(iteration) / 'judge_response_after.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            for dim, verdict in verdicts.items():
                f.write(json.dumps({'dimension': dim, 'verdict': verdict}) + '\n')

    def write_mutation_response(self, iteration, artifact_text, rationale):
        """write simulated mutation_response.md."""
        path = self.iter_dir(iteration) / 'mutation_response.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---ARTIFACT START---\n{artifact_text}\n---ARTIFACT END---\n"
            f"---RATIONALE---\n{rationale}\n"
        )

    def write_adversarial_response(self, iteration, text):
        """write simulated adversarial_response.md."""
        path = self.iter_dir(iteration) / 'adversarial_response.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def load_state(self):
        return json.loads((self.tmpdir / 'runs' / 'current_iteration.json').read_text())

    def load_log(self):
        log_path = self.tmpdir / 'runs' / 'log.jsonl'
        if not log_path.exists():
            return []
        return [json.loads(l) for l in log_path.read_text().strip().split('\n') if l.strip()]

    def file_contains(self, path, text):
        if not path.exists():
            return False
        return text in path.read_text()

    def run(self, *args, check=True):
        return run_cmd(*args, cwd=self.tmpdir, check=check)

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- the artifact text used for mutations --
    @property
    def fixed_artifact(self):
        return (
            "# Lemonade Stand Business Plan\n\n"
            "Revenue: $100/day (50 cups x $2) [ASSUMPTION]\n"
            "Costs: $30/day\n"
            "Profit: $70/day\n\n"
            "## Risk Register\n\n"
            "| Risk | Mitigation |\n"
            "|------|------------|\n"
            "| Rain | Tent |\n"
            "| Competition | Lower prices |\n\n"
            "## Appendix: Notes\n\n"
            "Do not modify this section.\n"
        )

    @property
    def original_artifact(self):
        return (self.tmpdir / 'artifact.md').read_text()


def test_full_loop():
    print("setting up test harness...")
    t = TestHarness()
    print(f"  temp dir: {t.tmpdir}")

    try:
        # ================================================================
        # INIT
        # ================================================================
        print("\n--- init ---")
        t.run('init', '--artifact', 'artifact.md', '--improve', 'improve.md')

        state = t.load_state()
        check(state['phase'] == 'initialized', f"phase should be initialized, got {state['phase']}")
        check(state['iteration'] == 0, f"iteration should be 0, got {state['iteration']}")
        check((t.tmpdir / 'runs' / 'approved.md').exists(), "approved.md should exist")
        check((t.tmpdir / 'runs' / 'best.md').exists(), "best.md should exist")
        check((t.tmpdir / 'runs' / 'constraint_hashes.json').exists(), "constraint_hashes.json should exist")

        # ================================================================
        # ITERATION 1: baseline, one dim fails, context + weights
        # ================================================================
        print("\n--- iteration 1: baseline with context + weights ---")

        # step A: score-before
        t.run('score-before', '--artifact', 'artifact.md', '--improve', 'improve.md',
              '--context', 'context.md')

        state = t.load_state()
        check(state['phase'] == 'awaiting_judge_before', f"phase should be awaiting_judge_before, got {state['phase']}")
        check(state['iteration'] == 1, f"iteration should be 1, got {state['iteration']}")
        check('dimension_weights' in state, "state should have dimension_weights")
        check(state['dimension_weights'].get('number_consistency') == 2.0, "number_consistency weight should be 2.0")
        check(state['dimension_weights'].get('risk_quality') == 1.0, "risk_quality weight should be 1.0")
        check(state.get('context_path') is not None, "state should have context_path")

        # verify context injection in judge prompts
        jp_nc = t.iter_dir(1) / 'judge_prompt_number_consistency.md'
        check(t.file_contains(jp_nc, 'GROUND TRUTH'), "judge prompt should contain GROUND TRUTH")
        check(t.file_contains(jp_nc, 'Lemon cost'), "judge prompt should contain context facts")

        # step B: simulate judge responses
        t.write_judge_response(1, {
            'number_consistency': 'FAIL: revenue and cost numbers not reconciled',
            'risk_quality': 'PASS: risks have concrete mitigations',
        })

        # step C: score-after
        t.run('score-after', '--judge-dir', 'judge_prompts/', '--improve', 'improve.md')

        state = t.load_state()
        check(state['phase'] == 'awaiting_mutation', f"phase should be awaiting_mutation, got {state['phase']}")
        check(state['targeted_dimension'] == 'number_consistency', f"target should be number_consistency, got {state.get('targeted_dimension')}")

        # check weighted composite: cheap(1 pass/1 total) + llm(1.0 pass / 3.0 total) = 2.0/4.0 = 0.5
        check(abs(state['composite_before'] - 0.5) < 0.01, f"composite should be 0.50, got {state['composite_before']}")

        # verify context in mutation request
        mr = t.iter_dir(1) / 'mutation_request.md'
        check(t.file_contains(mr, 'GROUND TRUTH'), "mutation request should contain GROUND TRUTH")
        check(t.file_contains(mr, 'ground truth facts'), "mutation request should contain ground truth instruction")

        # step D: simulate mutation
        t.write_mutation_response(1, t.fixed_artifact, "added cup count math to revenue line")

        # step E: apply-mutation
        t.run('apply-mutation', '--artifact', 'artifact.md')

        state = t.load_state()
        check(state['phase'] == 'awaiting_judge_after', f"phase should be awaiting_judge_after, got {state['phase']}")

        # verify context in after-judge prompt
        ajp = t.iter_dir(1) / 'judge_after_prompt_number_consistency.md'
        check(t.file_contains(ajp, 'GROUND TRUTH'), "after-judge prompt should contain GROUND TRUTH")

        # step F: simulate after-judge + verdict
        t.write_after_judge_response(1, {
            'number_consistency': 'PASS: numbers now reconciled with cup count',
        })
        result = t.run('verdict', '--artifact', 'artifact.md', '--adversarial-interval', '5')

        state = t.load_state()
        check(state['phase'] == 'completed', f"phase should be completed, got {state['phase']}")

        log = t.load_log()
        check(len(log) == 1, f"log should have 1 entry, got {len(log)}")
        check(log[0]['decision'] == 'ADOPTED', f"decision should be ADOPTED, got {log[0].get('decision')}")
        check(abs(log[0]['composite_after'] - 1.0) < 0.01, f"composite_after should be 1.0, got {log[0].get('composite_after')}")

        # ================================================================
        # ITERATIONS 2-4: three consecutive DISCARDs (stall detection)
        # ================================================================
        for i in range(2, 5):
            print(f"\n--- iteration {i}: DISCARD (stall setup) ---")

            t.run('score-before', '--artifact', 'artifact.md', '--improve', 'improve.md',
                  '--context', 'context.md')

            t.write_judge_response(i, {
                'number_consistency': 'PASS: numbers consistent',
                'risk_quality': 'PASS: risks adequate',
            })

            t.run('score-after', '--judge-dir', 'judge_prompts/', '--improve', 'improve.md')

            # mutation doesn't change anything meaningful
            t.write_mutation_response(i, t.fixed_artifact, "minor formatting change")

            t.run('apply-mutation', '--artifact', 'artifact.md')

            # after-judge: regression on targeted dim
            targeted = t.load_state().get('targeted_dimension', 'number_consistency')
            t.write_after_judge_response(i, {
                targeted: f'FAIL: regression detected in {targeted}',
            })

            result = t.run('verdict', '--artifact', 'artifact.md',
                           '--stall-threshold', '3', '--adversarial-interval', '5')

            log = t.load_log()
            iterations_only = [e for e in log if 'composite_before' in e]
            check(iterations_only[-1]['decision'] == 'DISCARDED',
                  f"iter {i} should be DISCARDED, got {iterations_only[-1].get('decision')}")

            # stall detection check
            if i == 4:
                check('STALL DETECTED' in result.stdout or 'possible writing ceiling' in result.stdout,
                      "iteration 4 should show stall warning")
            elif i < 4:
                check('STALL DETECTED' not in result.stdout and 'possible writing ceiling' not in result.stdout,
                      f"iteration {i} should NOT show stall warning")

        # verify artifact was restored after discards
        current_artifact = (t.tmpdir / 'artifact.md').read_text()
        check('50 cups' in current_artifact, "artifact should still have the fix from iter 1")

        # ================================================================
        # ITERATION 5: ADOPTED + adversarial signal
        # ================================================================
        print("\n--- iteration 5: ADOPTED + adversarial pass ---")

        t.run('score-before', '--artifact', 'artifact.md', '--improve', 'improve.md',
              '--context', 'context.md')

        t.write_judge_response(5, {
            'number_consistency': 'PASS: numbers consistent',
            'risk_quality': 'FAIL: mitigations too vague',
        })

        t.run('score-after', '--judge-dir', 'judge_prompts/', '--improve', 'improve.md')

        # mutation adds a new risk
        mutated_5 = t.fixed_artifact.replace(
            '| Competition | Lower prices |',
            '| Competition | Lower prices |\n| Supply shortage | Multiple suppliers |'
        )
        t.write_mutation_response(5, mutated_5, "added supply shortage risk")

        t.run('apply-mutation', '--artifact', 'artifact.md')

        targeted = t.load_state().get('targeted_dimension', 'risk_quality')
        t.write_after_judge_response(5, {
            targeted: f'PASS: {targeted} improved',
        })

        result = t.run('verdict', '--artifact', 'artifact.md',
                        '--stall-threshold', '3', '--adversarial-interval', '5')

        log = t.load_log()
        last_iter = [e for e in log if 'composite_before' in e][-1]
        check(last_iter['decision'] == 'ADOPTED', f"iter 5 should be ADOPTED, got {last_iter.get('decision')}")
        check('ADVERSARIAL PASS DUE' in result.stdout, "iter 5 should signal adversarial pass due")

        # run adversarial mode
        t.run('adversarial', '--artifact', 'artifact.md', '--improve', 'improve.md')

        state = t.load_state()
        check(state['phase'] == 'awaiting_adversarial', f"phase should be awaiting_adversarial, got {state['phase']}")

        # verify adversarial request
        adv_req = t.iter_dir(5) / 'adversarial_request.md'
        check(adv_req.exists(), "adversarial_request.md should exist")
        check(t.file_contains(adv_req, '100 lemonade stands fail'),
              "adversarial request should contain persona from improve.md")
        check(t.file_contains(adv_req, 'Lemonade Stand Business Plan'),
              "adversarial request should contain artifact text")

        # simulate adversarial response
        t.write_adversarial_response(5,
            "### Objection 1: no market research\n"
            "- Current score: 3/10\n"
            "- Gap type: writing gap\n"
            "- Explanation: no data on local demand\n\n"
            "### Objection 2: supplier not named\n"
            "- Current score: 2/10\n"
            "- Gap type: data gap\n"
            "- Explanation: who sells the lemons?\n\n"
            "---SUMMARY---\n"
            "biggest gaps: no market research and unnamed supplier.\n"
        )

        t.run('adversarial-process')

        state = t.load_state()
        check(state['phase'] == 'completed', f"phase should be completed after adversarial-process, got {state['phase']}")
        check('adversarial_findings' in state and len(state['adversarial_findings']) > 0,
              "state should have adversarial_findings")

        # verify adversarial event in log
        log = t.load_log()
        adv_events = [e for e in log if e.get('event') == 'adversarial']
        check(len(adv_events) == 1, f"should have 1 adversarial event, got {len(adv_events)}")
        check('market research' in adv_events[0].get('summary', ''),
              "adversarial log should contain summary")

        # ================================================================
        # ITERATION 6: adversarial findings in mutation request
        # ================================================================
        print("\n--- iteration 6: adversarial findings injection ---")

        t.run('score-before', '--artifact', 'artifact.md', '--improve', 'improve.md',
              '--context', 'context.md')

        t.write_judge_response(6, {
            'number_consistency': 'PASS: consistent',
            'risk_quality': 'PASS: adequate',
        })

        t.run('score-after', '--judge-dir', 'judge_prompts/', '--improve', 'improve.md')

        # verify adversarial findings appear in mutation request
        mr6 = t.iter_dir(6) / 'mutation_request.md'
        check(t.file_contains(mr6, 'ADVERSARIAL FINDINGS'),
              "iter 6 mutation request should contain ADVERSARIAL FINDINGS")
        check(t.file_contains(mr6, 'market research'),
              "iter 6 mutation request should contain adversarial content")

        # complete iteration 6 normally
        t.write_mutation_response(6, t.fixed_artifact, "no change")
        t.run('apply-mutation', '--artifact', 'artifact.md')

        targeted = t.load_state().get('targeted_dimension', 'number_consistency')
        t.write_after_judge_response(6, {targeted: f'PASS: {targeted} ok'})

        t.run('verdict', '--artifact', 'artifact.md',
              '--stall-threshold', '3', '--adversarial-interval', '5')

        # ================================================================
        # ITERATION 7: verify findings are one-shot (cleared)
        # ================================================================
        print("\n--- iteration 7: verify findings cleared ---")

        t.run('score-before', '--artifact', 'artifact.md', '--improve', 'improve.md')

        t.write_judge_response(7, {
            'number_consistency': 'PASS: consistent',
            'risk_quality': 'PASS: adequate',
        })

        t.run('score-after', '--judge-dir', 'judge_prompts/', '--improve', 'improve.md')

        mr7 = t.iter_dir(7) / 'mutation_request.md'
        check(not t.file_contains(mr7, 'ADVERSARIAL FINDINGS'),
              "iter 7 mutation request should NOT contain adversarial findings (one-shot)")

        # also verify no context (we omitted --context this iteration)
        check(not t.file_contains(mr7, 'GROUND TRUTH'),
              "iter 7 mutation request should NOT contain context (--context omitted)")

        # clean up iter 7 (don't need to finish it)
        # just reset state so report works
        state = t.load_state()
        state['phase'] = 'completed'
        (t.tmpdir / 'runs' / 'current_iteration.json').write_text(json.dumps(state, indent=2))

        # ================================================================
        # REPORT
        # ================================================================
        print("\n--- report ---")

        result = t.run('report', '--log', 'runs/log.jsonl', '--judge-dir', 'judge_prompts/')

        check('adversarial' in result.stdout.lower(), "report should mention adversarial passes")
        check('ADOPTED' in result.stdout, "report should show ADOPTED decisions")
        check('DISCARDED' in result.stdout, "report should show DISCARDED decisions")

        # ================================================================
        # BACKWARD COMPAT: adversarial-interval 0 disables signal
        # ================================================================
        print("\n--- backward compat: adversarial-interval 0 ---")
        # reset to iter 5 state to test the flag
        state = t.load_state()
        state['iteration'] = 10  # multiple of 5
        state['phase'] = 'awaiting_judge_after'
        state['composite_before'] = 1.0
        state['llm_scores_before'] = {'number_consistency': 'PASS', 'risk_quality': 'PASS'}
        state['cheap_results_after'] = {'risk_register_exists': 'PASS'}
        state['dimension_weights'] = {'number_consistency': 2.0, 'risk_quality': 1.0}
        state['artifact_snapshot'] = t.fixed_artifact
        state['mutation_rationale'] = 'test'
        state['targeted_dimension'] = 'risk_quality'
        (t.tmpdir / 'runs' / 'current_iteration.json').write_text(json.dumps(state, indent=2))

        iter10 = t.tmpdir / 'runs' / 'iter_010'
        iter10.mkdir(parents=True, exist_ok=True)
        with open(iter10 / 'judge_response_after.jsonl', 'w') as f:
            f.write(json.dumps({'dimension': 'risk_quality', 'verdict': 'PASS: ok'}) + '\n')

        result = t.run('verdict', '--artifact', 'artifact.md',
                        '--adversarial-interval', '0')

        check('ADVERSARIAL PASS DUE' not in result.stdout,
              "adversarial-interval 0 should suppress adversarial signal")

    finally:
        t.cleanup()


if __name__ == '__main__':
    test_full_loop()
    print(f"\n{'='*60}")
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({PASSED} assertions)")
    else:
        print(f"FAILED: {FAILED} failures, {PASSED} passed")
        sys.exit(1)
