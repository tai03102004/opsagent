import pytest

from opsagent.evals import load_scenarios, run_scenario

SCENARIOS = load_scenarios()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario_offline(scenario):
    result = run_scenario(scenario, llm="off")
    assert result.passed, "\n".join(result.failures)


@pytest.mark.live
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario_live(scenario):
    result = run_scenario(scenario, llm="claude")
    assert result.passed, "\n".join(result.failures)
