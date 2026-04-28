from __future__ import annotations

import unittest

from src.sql_query.schema_ddl import render_mysql_schema_ddl


class SQLQuerySchemaDDLTest(unittest.TestCase):
    def test_renders_selected_columns_primary_key_foreign_key_and_escaped_comments(self) -> None:
        schema_metadata = {
            "tables": {
                "parent_table": {
                    "description": "父表",
                    "primary_key": ["id"],
                    "columns": {
                        "id": {"sql_type": "int(11)", "description": "主键"},
                        "name": {"sql_type": "varchar(100)", "description": "名称'带引号"},
                        "hidden": {"sql_type": "text", "description": "不应渲染"},
                    },
                },
                "child_table": {
                    "description": "子表",
                    "primary_key": ["id"],
                    "columns": {
                        "id": {"sql_type": "int(11)", "description": "主键"},
                        "parent_id": {"sql_type": "int(11)", "description": "父表ID"},
                        "value": {"sql_type": "text", "description": "业务值"},
                    },
                    "foreign_keys": [
                        {"column": "parent_id", "ref_table": "parent_table", "ref_column": "id"},
                    ],
                },
            }
        }

        ddl = render_mysql_schema_ddl(
            schema_metadata,
            ["parent_table", "child_table"],
            selected_columns={
                "parent_table": ["id", "name"],
                "child_table": ["id", "parent_id", "value"],
            },
        )

        self.assertIn("CREATE TABLE `parent_table`", ddl)
        self.assertIn("`name` varchar(100) DEFAULT NULL COMMENT '名称\\'带引号'", ddl)
        self.assertIn("PRIMARY KEY (`id`)", ddl)
        self.assertIn("CONSTRAINT `fk_child_table_parent_id` FOREIGN KEY (`parent_id`) REFERENCES `parent_table` (`id`)", ddl)
        self.assertNotIn("hidden", ddl)


if __name__ == "__main__":
    unittest.main()
