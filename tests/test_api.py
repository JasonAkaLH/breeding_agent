from fastapi.testclient import TestClient

from multi_agent_framework.app import create_app


def test_healthz() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_execute_echo_workflow() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/workflows/execute",
            json={
                "agent_name": "echo",
                "messages": [{"role": "user", "content": "hello framework"}],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_name"] == "echo"
    assert payload["message"]["content"] == "echo: hello framework"
