from multi_agent_framework.bootstrap import build_registry


def test_registry_contains_echo_agent() -> None:
    registry = build_registry()
    agents = registry.list_agents()

    assert len(agents) == 1
    assert agents[0].name == "echo"
