from __future__ import annotations

from src.integrations.mysql_readonly import MySQLReadonlyAdapter, ReadonlyQueryResult

from tests.api.support import APITestCase


class RuntimeReplannerAPITest(APITestCase):
    async def test_default_runtime_replanner_splits_incomplete_multi_crop_sql_query(self) -> None:
        async def streamer(prompt: str):
            yield "汇总回答"

        corn_calls = 0

        def sql_generator(context: dict) -> str:
            table = list(context["selected_tables"])[0]
            return f"SELECT variety_name, suitable_area FROM {table}"

        def runner(sql: str) -> ReadonlyQueryResult:
            nonlocal corn_calls
            if "corn_varieties" in sql:
                corn_calls += 1
                if corn_calls == 1:
                    return ReadonlyQueryResult(columns=("variety_name", "suitable_area"), rows=(), row_count=0)
                return ReadonlyQueryResult(
                    columns=("variety_name", "suitable_area"),
                    rows=({"variety_name": "浙玉1号", "suitable_area": "浙江"},),
                    row_count=1,
                )
            if "rice_varieties" in sql:
                return ReadonlyQueryResult(
                    columns=("variety_name", "suitable_area"),
                    rows=({"variety_name": "豫稻1号", "suitable_area": "河南"},),
                    row_count=1,
                )
            return ReadonlyQueryResult(columns=("variety_name", "suitable_area"), rows=(), row_count=0)

        await self.reconfigure_runtime(
            mysql_adapter=MySQLReadonlyAdapter(runner=runner),
            sql_generator=sql_generator,
            main_agent_stream_generator=streamer,
            enable_llm_planner=False,
            enable_sql_query_llm=False,
            skill_roots=[],
        )

        response = await self.submit_message(
            content="你帮我查一下适合河南种的水稻和适合浙江中的玉米\n补充信息：route_id=审定品种库",
            capability_id=None,
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        terminal = await self.wait_for_terminal_task(task_id)
        graph_response = await self.client.get(f"/api/v1/tasks/{task_id}/graph")
        graph_response.raise_for_status()
        graph = graph_response.json()
        events = await self.runtime.storage.list_events_for_task(task_id)

        self.assertEqual(terminal["status"], "completed")
        self.assertTrue(any("runtime_query_1_1" in node["node_id"] for node in graph["nodes"]))
        self.assertTrue(any("runtime_query_1_2" in node["node_id"] for node in graph["nodes"]))
        self.assertTrue(any(event.event_type == "task.graph_updated" for event in events))
        self.assertGreaterEqual(corn_calls, 2)


if __name__ == "__main__":
    import unittest

    unittest.main()
