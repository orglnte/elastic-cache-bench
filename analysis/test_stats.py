import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats  # noqa: E402

INF = stats.INF


@pytest.fixture(autouse=True)
def no_cap(monkeypatch):
    monkeypatch.setattr(stats, "MAX_REP", None)


def make_cell(root, name, score=True, iterations=""):
    d = root / name
    d.mkdir()
    if score:
        (d / "score.json").write_text("{}")
    (d / "iterations.log").write_text(iterations)
    return str(d)


def test_in_scope_needs_rep_suffix_and_score(tmp_path):
    assert stats.in_scope(make_cell(tmp_path, "opus55_high_kedap_sealed_apidocs_T1_r3"))
    assert not stats.in_scope(make_cell(tmp_path, "opus55_high_kedap_sealed_apidocs_T1_r4", score=False))
    assert not stats.in_scope(make_cell(tmp_path, "testagent_kedap_sealed_r1"))
    assert not stats.in_scope(make_cell(tmp_path, ".conduct"))


def test_in_scope_caps_repetitions(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "MAX_REP", 12)
    assert stats.in_scope(make_cell(tmp_path, "sonnet55_high_kedam_sealed_apidocs_T1_r12"))
    assert not stats.in_scope(make_cell(tmp_path, "sonnet55_high_kedam_sealed_apidocs_T1_r13"))


def test_load_counts_a_failed_run_as_infinite_cost(tmp_path):
    for rep, green in ((1, True), (2, False)):
        d = tmp_path / f"haiku55_high_pulumi_access_apidocs_T1_r{rep}"
        d.mkdir()
        (d / "score.json").write_text(json.dumps({
            "cell_id": d.name, "variant": "pulumi_access_apidocs", "green": green, "revoked": False,
            "iterations_to_green": 2 if green else None, "turns_total": 40, "out_tok_total": 9000}))
    runs = stats.load(str(tmp_path))[("haiku55", "pulumi", "access")]
    assert sorted(r["att"] for r in runs) == [2, INF]
    assert sorted(r["tok"] for r in runs) == [9000, INF]


def test_load_treats_a_revoked_green_as_failed(tmp_path):
    d = tmp_path / "opus55_high_kedam_sealed_apidocs_T1_r1"
    d.mkdir()
    (d / "score.json").write_text(json.dumps({
        "cell_id": d.name, "variant": "kedam_sealed_apidocs", "green": True, "revoked": True,
        "iterations_to_green": 1, "turns_total": 10, "out_tok_total": 100}))
    (run,) = stats.load(str(tmp_path))[("opus55", "kedam", "sealed")]
    assert run["green"] is False and run["att"] == INF


def test_perm_p_complete_separation_is_two_of_all_splits():
    assert stats.perm_p([1, 2], [3, 4]) == pytest.approx(2 / 6)
    assert stats.perm_p([INF, INF, INF], [1, 2, 3]) == pytest.approx(2 / 20)


def test_perm_p_identical_samples_give_one():
    assert stats.perm_p([2, 2, 3], [2, 2, 3]) == pytest.approx(1.0)


def test_perm_p_is_symmetric_and_handles_ties():
    a, b = [1, 2, 2, INF, 5], [2, 3, 3, 4, INF, INF]
    assert stats.perm_p(a, b) == pytest.approx(stats.perm_p(b, a))
    assert 0 < stats.perm_p(a, b) <= 1


def test_holm_is_monotone_and_capped():
    assert stats.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert stats.holm([0.5, 0.6]) == pytest.approx([1.0, 1.0])


def test_median_odd_even_and_infinite():
    assert stats.median([3, 1, 2]) == 2
    assert stats.median([1, 2, 3, 4]) == 2.5
    assert stats.median([1, INF, INF]) == INF


def test_cliffs_delta_signs():
    assert stats.cliffs_delta([3, 4], [1, 2]) == 1
    assert stats.cliffs_delta([1, 2], [3, 4]) == -1
    assert stats.cliffs_delta([1, 2], [1, 2]) == 0
    assert stats.cliffs_delta([INF], [5]) == 1


def test_delta_ci_of_complete_separation_is_a_point():
    assert stats.delta_ci([5, 6, 7], [1, 2, 3]) == (1.0, 1.0)


def test_cmh_one_stratum_matches_hand_computation():
    num = 0 - 10 * 10 / 20
    var = 10 * 10 * 10 * 10 / (20 * 20 * 19)
    expected = math.erfc(math.sqrt((abs(num) - 0.5) ** 2 / var / 2))
    assert stats.cmh_p([(0, 10, 10, 10)]) == pytest.approx(expected)


def test_cmh_is_symmetric_and_skips_uninformative_strata():
    strata = [(0, 12, 5, 12), (1, 8, 4, 8)]
    swapped = [(xb, nb, xa, na) for xa, na, xb, nb in strata]
    assert stats.cmh_p(strata) == pytest.approx(stats.cmh_p(swapped))
    assert stats.cmh_p([(3, 3, 4, 4), (0, 5, 0, 5)]) == 1.0


def test_cmh_equal_rates_give_one():
    assert stats.cmh_p([(2, 4, 2, 4)]) == pytest.approx(1.0)


def test_upper_bound_zero_passes_matches_closed_form():
    assert stats.upper_bound(0, 95) == pytest.approx(1 - 0.05 ** (1 / 95), abs=1e-9)


def test_upper_bound_counts_passes():
    assert 100 * stats.upper_bound(1, 100) == pytest.approx(4.66, abs=0.01)
    assert stats.upper_bound(1, 100) > stats.upper_bound(0, 100)
    assert stats.upper_bound(5, 5) == 1.0


def test_fmt_p():
    assert stats.fmt_p(0.014) == "0.014"
    assert stats.fmt_p(0.0006) == "$<10^{-3}$"
    assert stats.fmt_p(1.8e-10) == "$<10^{-9}$"


def test_fmt():
    assert stats.fmt(INF, "att") == "$\\infty$"
    assert stats.fmt(2.5, "att") == "2.5"
    assert stats.fmt(153400, "tok") == "153"
    assert stats.fmt(126.4, "turns") == "126"


def test_check_of_labels():
    assert stats.check_of({"stage_failed": "deploy"}) == stats.OTHER_STAGE
    why = "expected 2 mount episode(s) (one per spike), saw 3; other"
    assert stats.check_of({"stage_failed": "scaling", "scaling_why": why}) == "Too many mounts"
    why = "expected 2 mount episode(s) (one per spike), saw 1"
    assert stats.check_of({"scaling_why": why}) == "Too few mounts"
    assert stats.check_of({"scaling_why": "cache never released after drain"}) == "Not released"
    assert stats.check_of({"scaling_why": "something new"}) == "Other"


def test_failed_attempts_maps_each_failure_to_its_charged_arrangement(tmp_path):
    log = ("t\tITER\tfail\tattempt=1 stage=scaling\n"
           "t\tITER\tfail\tattempt=2 stage=deploy\n"
           "t\tITER\tpass\tattempt=3\n")
    cell = make_cell(tmp_path, "opus55_high_kedap_sealed_apidocs_T1_r1", iterations=log)
    (tmp_path / "opus55_high_kedap_sealed_apidocs_T1_r1" / "arrangements" / "01-a1-BBS-charged").mkdir(parents=True)
    decided = stats.failed_attempts(cell)
    assert set(decided) == {1, 2}
    assert decided[1].endswith("01-a1-BBS-charged")
    assert decided[2] is None


def test_failures_counts_stages(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "OUT_DIR", str(tmp_path / "out"))
    os.mkdir(tmp_path / "out")
    ws = tmp_path / "ws"
    ws.mkdir()
    make_cell(ws, "opus55_high_kedap_sealed_apidocs_T1_r1",
              iterations="t\tITER\tfail\tattempt=1 stage=scaling\nt\tITER\tfail\tattempt=2 stage=deploy\n")
    make_cell(ws, "opus55_high_pulumi_sealed_apidocs_T1_r1",
              iterations="t\tITER\tfail\tattempt=1 stage=functional\n")
    stats.failures(str(ws))
    out = (tmp_path / "out" / "failures.tex").read_text()
    assert "\\newcommand{\\failPall}{2}" in out
    assert "\\newcommand{\\failPdeploy}{1}" in out
    assert "\\newcommand{\\failPuother}{1}" in out
    assert "\\newcommand{\\failAllall}{3}" in out


def test_in_scope_drops_excluded_cells(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "EXCLUDED", {"g38f_high_kedap_sealed_apidocs_T1_r8"})
    assert not stats.in_scope(make_cell(tmp_path, "g38f_high_kedap_sealed_apidocs_T1_r8"))
    assert stats.in_scope(make_cell(tmp_path, "g38f_high_kedap_sealed_apidocs_T1_r7"))


def test_write_creates_a_missing_out_dir(tmp_path, monkeypatch):
    out = tmp_path / "new" / "tables"
    monkeypatch.setattr(stats, "OUT_DIR", str(out))
    stats.write("t.tex", ["a", "b"])
    assert (out / "t.tex").read_text() == "a\nb\n"
