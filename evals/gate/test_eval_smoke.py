import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("run_eval", Path(__file__).with_name("run_eval.py"))
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)


def test_gate_eval_thresholds():
    res = run_eval.evaluate(use_ai=False, fixes=True)
    assert res["recall"] >= 0.9, res
    assert res["false_positive_rate"] <= 0.1, res
    assert res["fix_verification_rate"] == 1.0, res
