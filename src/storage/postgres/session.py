from __future__ import annotations

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker


def create_postgres_engine(
    dsn: str,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_pre_ping: bool = True,
) -> Engine:
    if not dsn.strip():
        raise ValueError("PostgreSQL DSN is required")
    return create_engine(
        dsn,
        echo=echo,
        future=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=pool_pre_ping,
        hide_parameters=True,
    )


def create_postgres_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


MCP_ROLLOUT_DATABASE_ROLES = {
    "app": "maf_rollout_app_writer",
    "snapshot": "maf_rollout_snapshot_producer",
    "ci": "maf_rollout_ci_evidence_writer",
    "evaluator": "maf_rollout_gate_evaluator",
    "operator": "maf_rollout_operator",
    "validator": "maf_rollout_validator",
    "drill": "maf_rollout_drill_recorder",
}

MCP_ROLLOUT_API_OWNER = "maf_rollout_api_owner"

_MCP_ROLLOUT_OWNER_SELECT_TABLES = {
    "mcp_rollout_block_resolution",
    "mcp_rollout_deployment_activation",
    "mcp_rollout_drill_observation",
    "mcp_rollout_evidence_snapshot",
    "mcp_rollout_gate_scope",
    "mcp_rollout_instance_config",
    "mcp_rollout_metric_bucket",
    "mcp_rollout_promotion_block",
    "mcp_rollout_stage_approval",
    "mcp_shadow_audit_sample",
}
_MCP_ROLLOUT_OWNER_UPDATE_TABLES = {
    "mcp_rollout_gate_scope",
    "mcp_rollout_instance_config",
    "mcp_rollout_metric_bucket",
}
_MCP_ROLLOUT_OWNER_DELETE_TABLES = {"mcp_shadow_audit_sample"}

_MCP_ROLLOUT_EXECUTE_ALLOWLISTS = {
    "app": {
        "append_shadow_audit_sample",
        "delete_expired_shadow_audit_samples",
        "set_metric_bucket",
        "upsert_instance_config_lease",
        "upsert_metric_bucket",
    },
    "snapshot": {
        "finalize_production_evidence_snapshot",
        "prepare_production_evidence_snapshot",
    },
    "ci": {"append_ci_evidence_snapshot"},
    "evaluator": {"append_promotion_block", "ensure_gate_scope"},
    "operator": {
        "append_block_resolution",
        "append_deployment_activation",
        "append_stage_approval",
        "ensure_gate_scope",
    },
    "validator": set(),
    "drill": {"append_drill_observation"},
}

_MCP_ROLLOUT_SELECT_ALLOWLISTS = {
    "app": {
        "mcp_rollout_deployment_activation",
        "mcp_rollout_gate_scope",
        "mcp_rollout_instance_config",
        "mcp_rollout_metric_bucket",
    },
    "snapshot": {
        "mcp_rollout_drill_observation",
        "mcp_rollout_metric_bucket",
        "mcp_shadow_audit_sample",
    },
    "ci": set(),
    "evaluator": {
        "mcp_rollout_block_resolution",
        "mcp_rollout_deployment_activation",
        "mcp_rollout_evidence_snapshot",
        "mcp_rollout_gate_scope",
        "mcp_rollout_instance_config",
        "mcp_rollout_metric_bucket",
        "mcp_rollout_promotion_block",
        "mcp_rollout_stage_approval",
    },
    "operator": {
        "mcp_rollout_block_resolution",
        "mcp_rollout_deployment_activation",
        "mcp_rollout_evidence_snapshot",
        "mcp_rollout_gate_scope",
        "mcp_rollout_instance_config",
        "mcp_rollout_metric_bucket",
        "mcp_rollout_promotion_block",
        "mcp_rollout_stage_approval",
    },
    "validator": {
        "mcp_rollout_block_resolution",
        "mcp_rollout_deployment_activation",
        "mcp_rollout_evidence_snapshot",
        "mcp_rollout_gate_scope",
        "mcp_rollout_instance_config",
        "mcp_rollout_metric_bucket",
        "mcp_rollout_promotion_block",
        "mcp_rollout_stage_approval",
        "mcp_rollout_drill_observation",
        "mcp_shadow_audit_sample",
    },
    "drill": set(),
}


def validate_mcp_rollout_connection_role(engine: Engine, expected_role: str) -> str:
    """Fail closed unless a login owns exactly one narrow rollout role."""

    database_role = MCP_ROLLOUT_DATABASE_ROLES.get(expected_role)
    if database_role is None:
        raise ValueError("unsupported MCP rollout PostgreSQL role")
    expected_login = (engine.url.username or "").strip()
    if not expected_login:
        raise RuntimeError("MCP rollout PostgreSQL login is unavailable")
    with engine.connect() as connection:
        identity = connection.execute(
            text(
                """
                SELECT role.rolname, role.rolcanlogin, role.rolinherit,
                    role.rolsuper, role.rolbypassrls, role.rolcreatedb,
                    role.rolcreaterole, role.rolreplication,
                    CURRENT_USER AS current_role,
                    SESSION_USER AS session_role
                FROM pg_catalog.pg_roles AS role
                WHERE role.rolname = SESSION_USER
                """
            )
        ).one()
        if (
            identity.rolname != expected_login
            or identity.current_role != expected_login
            or identity.session_role != expected_login
            or not bool(identity.rolcanlogin)
            or not bool(identity.rolinherit)
            or any(
                bool(value)
                for value in (
                    identity.rolsuper,
                    identity.rolbypassrls,
                    identity.rolcreatedb,
                    identity.rolcreaterole,
                    identity.rolreplication,
                )
            )
        ):
            raise RuntimeError("MCP rollout PostgreSQL login is over-privileged")
        authority_role = connection.execute(
            text(
                """
                SELECT role.rolcanlogin, role.rolsuper, role.rolbypassrls,
                    role.rolcreatedb, role.rolcreaterole, role.rolreplication
                FROM pg_catalog.pg_roles AS role
                WHERE role.rolname = :database_role
                """
            ),
            {"database_role": database_role},
        ).one_or_none()
        if authority_role is None or bool(authority_role.rolcanlogin) or any(
            bool(value)
            for value in (
                authority_role.rolsuper,
                authority_role.rolbypassrls,
                authority_role.rolcreatedb,
                authority_role.rolcreaterole,
                authority_role.rolreplication,
            )
        ):
            raise RuntimeError("MCP rollout PostgreSQL authority role is invalid")
        memberships = {
            row.rolname
            for row in connection.execute(
                text(
                    """
                    WITH RECURSIVE inherited(role_id) AS (
                        SELECT membership.roleid
                        FROM pg_catalog.pg_auth_members AS membership
                        WHERE membership.member = (
                            SELECT oid FROM pg_catalog.pg_roles
                            WHERE rolname = SESSION_USER
                        )
                        UNION
                        SELECT membership.roleid
                        FROM pg_catalog.pg_auth_members AS membership
                        JOIN inherited
                          ON membership.member = inherited.role_id
                    )
                    SELECT role.rolname
                    FROM inherited
                    JOIN pg_catalog.pg_roles AS role
                      ON role.oid = inherited.role_id
                    """
                )
            )
        }
        if memberships != {database_role}:
            raise RuntimeError(
                "MCP rollout PostgreSQL login must inherit exactly its named role"
            )
        membership_edge = connection.execute(
            text(
                """
                SELECT membership.admin_option, membership.inherit_option,
                    membership.set_option
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS granted_role
                  ON granted_role.oid = membership.roleid
                JOIN pg_catalog.pg_roles AS login_role
                  ON login_role.oid = membership.member
                WHERE login_role.rolname = SESSION_USER
                  AND granted_role.rolname = :database_role
                """
            ),
            {"database_role": database_role},
        ).one_or_none()
        if (
            membership_edge is None
            or bool(membership_edge.admin_option)
            or not bool(membership_edge.inherit_option)
            or bool(membership_edge.set_option)
        ):
            raise RuntimeError(
                "MCP rollout PostgreSQL login membership options are unsafe"
            )
        forbidden_tables = connection.execute(
            text(
                """
                SELECT class.relname
                FROM pg_catalog.pg_class AS class
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname = 'public'
                  AND class.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND (
                      class.relowner = (
                          SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname = SESSION_USER
                      )
                      OR pg_catalog.has_table_privilege(
                          SESSION_USER, class.oid,
                          'INSERT,UPDATE,DELETE,TRUNCATE'
                      )
                  )
                """
            )
        ).all()
        if forbidden_tables:
            raise RuntimeError(
                "MCP rollout PostgreSQL login has forbidden base-table authority"
            )
        executable_functions = sorted(
            row.proname
            for row in connection.execute(
                text(
                    """
                    SELECT procedure.proname
                    FROM pg_catalog.pg_proc AS procedure
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = procedure.pronamespace
                    WHERE namespace.nspname = 'mcp_rollout_api'
                      AND pg_catalog.has_function_privilege(
                          SESSION_USER, procedure.oid, 'EXECUTE'
                      )
                    """
                )
            )
        )
        if executable_functions != sorted(
            _MCP_ROLLOUT_EXECUTE_ALLOWLISTS[expected_role]
        ):
            raise RuntimeError(
                "MCP rollout PostgreSQL login has unexpected function authority"
            )
        selectable_tables = {
            row.relname
            for row in connection.execute(
                text(
                    """
                    SELECT class.relname
                    FROM pg_catalog.pg_class AS class
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = class.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND class.relkind IN ('r', 'p', 'v', 'm', 'f')
                      AND pg_catalog.has_table_privilege(
                          SESSION_USER, class.oid, 'SELECT'
                      )
                    """
                )
            )
        }
        if selectable_tables != _MCP_ROLLOUT_SELECT_ALLOWLISTS[expected_role]:
            raise RuntimeError(
                "MCP rollout PostgreSQL login has unexpected table read authority"
            )
        owns_rollout_api = connection.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_namespace AS namespace
                    WHERE namespace.nspname = 'mcp_rollout_api'
                      AND namespace.nspowner = (
                          SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname = SESSION_USER
                      )
                ) OR EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_proc AS procedure
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = procedure.pronamespace
                    WHERE namespace.nspname = 'mcp_rollout_api'
                      AND procedure.proowner = (
                          SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname = SESSION_USER
                      )
                )
                """
            )
        )
        if bool(owns_rollout_api):
            raise RuntimeError("MCP rollout PostgreSQL login owns privileged API objects")

        owner = connection.execute(
            text(
                """
                SELECT role.oid, role.rolcanlogin, role.rolinherit, role.rolsuper,
                    role.rolbypassrls, role.rolcreatedb, role.rolcreaterole,
                    role.rolreplication,
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.pg_auth_members AS membership
                        WHERE membership.member = role.oid
                           OR membership.roleid = role.oid
                    ) AS has_membership,
                    pg_catalog.has_schema_privilege(
                        role.rolname, 'mcp_rollout_api', 'CREATE'
                    ) AS can_create_api_objects
                FROM pg_catalog.pg_roles AS role
                WHERE role.rolname = :owner_role
                """
            ),
            {"owner_role": MCP_ROLLOUT_API_OWNER},
        ).one_or_none()
        if (
            owner is None
            or bool(owner.rolcanlogin)
            or bool(owner.rolinherit)
            or any(
                bool(value)
                for value in (
                    owner.rolsuper,
                    owner.rolbypassrls,
                    owner.rolcreatedb,
                    owner.rolcreaterole,
                    owner.rolreplication,
                    owner.has_membership,
                    owner.can_create_api_objects,
                )
            )
        ):
            raise RuntimeError("MCP rollout API owner is not constrained")

        owner_table_authority = connection.execute(
            text(
                """
                SELECT class.relname,
                    pg_catalog.has_table_privilege(
                        :owner_role, class.oid, 'SELECT'
                    ) AS can_select,
                    pg_catalog.has_table_privilege(
                        :owner_role, class.oid, 'INSERT'
                    ) AS can_insert,
                    pg_catalog.has_table_privilege(
                        :owner_role, class.oid, 'UPDATE'
                    ) AS can_update,
                    pg_catalog.has_table_privilege(
                        :owner_role, class.oid, 'DELETE'
                    ) AS can_delete,
                    pg_catalog.has_table_privilege(
                        :owner_role, class.oid, 'TRUNCATE,REFERENCES,TRIGGER'
                    ) AS has_forbidden_authority
                FROM pg_catalog.pg_class AS class
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname = 'public'
                  AND class.relkind IN ('r', 'p', 'v', 'm', 'f')
                """
            ),
            {"owner_role": MCP_ROLLOUT_API_OWNER},
        ).all()
        if (
            {
                row.relname
                for row in owner_table_authority
                if bool(row.can_select)
            }
            != _MCP_ROLLOUT_OWNER_SELECT_TABLES
            or {
                row.relname
                for row in owner_table_authority
                if bool(row.can_insert)
            }
            != _MCP_ROLLOUT_OWNER_SELECT_TABLES
            or {
                row.relname
                for row in owner_table_authority
                if bool(row.can_update)
            }
            != _MCP_ROLLOUT_OWNER_UPDATE_TABLES
            or {
                row.relname
                for row in owner_table_authority
                if bool(row.can_delete)
            }
            != _MCP_ROLLOUT_OWNER_DELETE_TABLES
            or any(
                bool(row.has_forbidden_authority)
                for row in owner_table_authority
            )
        ):
            raise RuntimeError(
                "MCP rollout API owner has unexpected table authority"
            )

        api_functions = connection.execute(
            text(
                """
                SELECT procedure.proname, procedure.prosecdef,
                    procedure.proconfig, owner.rolname AS owner_role,
                    EXISTS (
                        SELECT 1
                        FROM pg_catalog.aclexplode(
                            COALESCE(
                                procedure.proacl,
                                pg_catalog.acldefault(
                                    'f'::"char", procedure.proowner
                                )
                            )
                        ) AS privilege
                        WHERE privilege.grantee = 0
                          AND privilege.privilege_type = 'EXECUTE'
                    ) AS public_can_execute
                FROM pg_catalog.pg_proc AS procedure
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = procedure.pronamespace
                JOIN pg_catalog.pg_roles AS owner
                  ON owner.oid = procedure.proowner
                WHERE namespace.nspname = 'mcp_rollout_api'
                """
            )
        ).all()
        if not api_functions or any(
            row.owner_role != MCP_ROLLOUT_API_OWNER
            or (
                row.proname != "reject_history_mutation"
                and not bool(row.prosecdef)
            )
            or "search_path=pg_catalog" not in (row.proconfig or ())
            or bool(row.public_can_execute)
            for row in api_functions
        ):
            raise RuntimeError("MCP rollout API function ownership is invalid")
        return str(identity.rolname)


MCP_LEGACY_MIGRATION_DATABASE_ROLE = "maf_mcp_legacy_migrator"
MCP_LEGACY_MIGRATION_API_OWNER = "maf_mcp_migration_api_owner"
_MCP_LEGACY_MIGRATION_EXECUTE_ALLOWLIST = {
    "apply_legacy_migration_candidate",
    "lock_legacy_migration_batch",
    "read_legacy_migration_replay_snapshot",
}
_MCP_LEGACY_MIGRATION_SELECT_ALLOWLIST: set[str] = set()
_MCP_LEGACY_MIGRATION_OWNER_TABLES = {
    "mcp_legacy_migration_record",
    "user_mcp_server",
}
_MCP_LEGACY_MIGRATION_APPLY_ARGUMENT_TYPES = (
    "text",
    "text",
    "text",
    "text",
    "text",
    "text",
    "text",
    "text",
    "jsonb",
    "boolean",
    "text",
    "bigint",
    "bigint",
    "bytea",
    "bytea",
    "integer",
    "timestamp with time zone",
    "timestamp with time zone",
    "text",
    "timestamp with time zone",
    "timestamp with time zone",
    "text",
    "text",
    "text",
    "text",
    "text",
    "text",
    "text",
    "text",
    "text",
    "text",
    "text",
    "text",
    "timestamp with time zone",
    "timestamp with time zone",
)
_MCP_LEGACY_MIGRATION_FUNCTION_CONTRACT = {
    (
        "apply_legacy_migration_candidate",
        _MCP_LEGACY_MIGRATION_APPLY_ARGUMENT_TYPES,
    ): (
        "boolean",
        True,
        MCP_LEGACY_MIGRATION_API_OWNER,
        ("search_path=pg_catalog",),
    ),
    ("lock_legacy_migration_batch", ("text[]",)): (
        "void",
        True,
        MCP_LEGACY_MIGRATION_API_OWNER,
        ("search_path=pg_catalog",),
    ),
    (
        "read_legacy_migration_replay_snapshot",
        ("text", "text", "text", "text", "text", "text"),
    ): (
        "jsonb",
        True,
        MCP_LEGACY_MIGRATION_API_OWNER,
        ("search_path=pg_catalog",),
    ),
    ("reject_legacy_migration_mutation", ()): (
        "trigger",
        False,
        MCP_LEGACY_MIGRATION_API_OWNER,
        ("search_path=pg_catalog",),
    ),
}
_MCP_LEGACY_MIGRATION_EXECUTE_SIGNATURES = {
    signature
    for signature in _MCP_LEGACY_MIGRATION_FUNCTION_CONTRACT
    if signature[0] in _MCP_LEGACY_MIGRATION_EXECUTE_ALLOWLIST
}


def validate_mcp_legacy_migration_connection_role(
    engine: Engine,
    expected_login_role: str,
) -> str:
    """Validate the dedicated legacy-migration login and definer boundary."""

    if not expected_login_role.strip():
        raise ValueError("expected MCP legacy migration PostgreSQL login is required")
    with engine.connect() as connection:
        identity = connection.execute(
            text(
                """
                SELECT role.rolname, role.rolcanlogin, role.rolinherit, role.rolsuper,
                    role.rolbypassrls, role.rolcreatedb, role.rolcreaterole,
                    role.rolreplication, CURRENT_USER AS current_role,
                    SESSION_USER AS session_role
                FROM pg_catalog.pg_roles AS role
                WHERE role.rolname = SESSION_USER
                """
            )
        ).one()
        if (
            identity.rolname != expected_login_role
            or identity.current_role != expected_login_role
            or identity.session_role != expected_login_role
        ):
            raise RuntimeError("MCP legacy migration PostgreSQL login does not match")
        if not bool(identity.rolcanlogin) or not bool(identity.rolinherit) or any(
            bool(value)
            for value in (
                identity.rolsuper,
                identity.rolbypassrls,
                identity.rolcreatedb,
                identity.rolcreaterole,
                identity.rolreplication,
            )
        ):
            raise RuntimeError(
                "MCP legacy migration PostgreSQL login is over-privileged"
            )

        memberships = {
            row.rolname
            for row in connection.execute(
                text(
                    """
                    WITH RECURSIVE inherited(role_id) AS (
                        SELECT membership.roleid
                        FROM pg_catalog.pg_auth_members AS membership
                        WHERE membership.member = (
                            SELECT oid FROM pg_catalog.pg_roles
                            WHERE rolname = SESSION_USER
                        )
                        UNION
                        SELECT membership.roleid
                        FROM pg_catalog.pg_auth_members AS membership
                        JOIN inherited ON membership.member = inherited.role_id
                    )
                    SELECT role.rolname
                    FROM inherited
                    JOIN pg_catalog.pg_roles AS role ON role.oid = inherited.role_id
                    """
                )
            )
        }
        if memberships != {MCP_LEGACY_MIGRATION_DATABASE_ROLE}:
            raise RuntimeError(
                "MCP legacy migration PostgreSQL login must inherit exactly its named role"
            )
        membership_edge = connection.execute(
            text(
                """
                SELECT membership.admin_option, membership.inherit_option,
                    membership.set_option
                FROM pg_catalog.pg_auth_members AS membership
                JOIN pg_catalog.pg_roles AS granted_role
                  ON granted_role.oid = membership.roleid
                JOIN pg_catalog.pg_roles AS login_role
                  ON login_role.oid = membership.member
                WHERE login_role.rolname = SESSION_USER
                  AND granted_role.rolname = :migration_role
                """
            ),
            {"migration_role": MCP_LEGACY_MIGRATION_DATABASE_ROLE},
        ).one_or_none()
        if (
            membership_edge is None
            or bool(membership_edge.admin_option)
            or not bool(membership_edge.inherit_option)
            or bool(membership_edge.set_option)
        ):
            raise RuntimeError(
                "MCP legacy migration PostgreSQL login membership options are unsafe"
            )
        migration_role = connection.execute(
            text(
                """
                SELECT role.rolcanlogin, role.rolinherit, role.rolsuper,
                    role.rolbypassrls,
                    role.rolcreatedb, role.rolcreaterole, role.rolreplication
                FROM pg_catalog.pg_roles AS role
                WHERE role.rolname = :migration_role
                """
            ),
            {"migration_role": MCP_LEGACY_MIGRATION_DATABASE_ROLE},
        ).one_or_none()
        if (
            migration_role is None
            or bool(migration_role.rolcanlogin)
            or bool(migration_role.rolinherit)
            or any(
                bool(value)
                for value in (
                    migration_role.rolsuper,
                    migration_role.rolbypassrls,
                    migration_role.rolcreatedb,
                    migration_role.rolcreaterole,
                    migration_role.rolreplication,
                )
            )
        ):
            raise RuntimeError("MCP legacy migration PostgreSQL role is not constrained")

        forbidden_tables = connection.execute(
            text(
                """
                SELECT class.relname
                FROM pg_catalog.pg_class AS class
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname = 'public'
                  AND class.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND (
                      class.relowner = (
                          SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname = SESSION_USER
                      )
                      OR pg_catalog.has_table_privilege(
                          SESSION_USER, class.oid,
                          'INSERT,UPDATE,DELETE,TRUNCATE'
                      )
                  )
                """
            )
        ).all()
        if forbidden_tables:
            raise RuntimeError(
                "MCP legacy migration PostgreSQL login has forbidden base-table authority"
            )

        executable_functions = {
            (row.proname, tuple(row.argument_types))
            for row in connection.execute(
                text(
                    """
                    SELECT procedure.proname,
                        ARRAY(
                            SELECT pg_catalog.format_type(argument.oid, NULL)
                            FROM pg_catalog.unnest(
                                procedure.proargtypes::pg_catalog.oid[]
                            ) WITH ORDINALITY AS argument(oid, position)
                            ORDER BY argument.position
                        ) AS argument_types
                    FROM pg_catalog.pg_proc AS procedure
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = procedure.pronamespace
                    WHERE namespace.nspname = 'mcp_migration_api'
                      AND pg_catalog.has_function_privilege(
                          SESSION_USER, procedure.oid, 'EXECUTE'
                      )
                    """
                )
            )
        }
        if executable_functions != _MCP_LEGACY_MIGRATION_EXECUTE_SIGNATURES:
            raise RuntimeError(
                "MCP legacy migration PostgreSQL login has unexpected function authority"
            )

        selectable_tables = {
            row.relname
            for row in connection.execute(
                text(
                    """
                    SELECT class.relname
                    FROM pg_catalog.pg_class AS class
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = class.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND class.relkind IN ('r', 'p', 'v', 'm', 'f')
                      AND pg_catalog.has_table_privilege(
                          SESSION_USER, class.oid, 'SELECT'
                      )
                    """
                )
            )
        }
        if selectable_tables != _MCP_LEGACY_MIGRATION_SELECT_ALLOWLIST:
            raise RuntimeError(
                "MCP legacy migration PostgreSQL login has unexpected table read authority"
            )

        owner = connection.execute(
            text(
                """
                SELECT role.oid, role.rolcanlogin, role.rolinherit, role.rolsuper,
                    role.rolbypassrls, role.rolcreatedb, role.rolcreaterole,
                    role.rolreplication,
                    EXISTS (
                        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                        WHERE membership.member = role.oid
                           OR membership.roleid = role.oid
                    ) AS has_membership
                FROM pg_catalog.pg_roles AS role
                WHERE role.rolname = :owner_role
                """
            ),
            {"owner_role": MCP_LEGACY_MIGRATION_API_OWNER},
        ).one_or_none()
        if (
            owner is None
            or bool(owner.rolcanlogin)
            or bool(owner.rolinherit)
            or any(
                bool(value)
                for value in (
                    owner.rolsuper,
                    owner.rolbypassrls,
                    owner.rolcreatedb,
                    owner.rolcreaterole,
                    owner.rolreplication,
                    owner.has_membership,
                )
            )
        ):
            raise RuntimeError("MCP legacy migration API owner is not constrained")

        owner_table_authority = connection.execute(
            text(
                """
                SELECT class.relname,
                    pg_catalog.has_table_privilege(
                        :owner_role, class.oid, 'SELECT'
                    ) AS can_select,
                    pg_catalog.has_table_privilege(
                        :owner_role, class.oid, 'INSERT'
                    ) AS can_insert,
                    pg_catalog.has_table_privilege(
                        :owner_role, class.oid, 'UPDATE,DELETE,TRUNCATE'
                    ) AS can_mutate_existing
                FROM pg_catalog.pg_class AS class
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname = 'public'
                  AND class.relkind IN ('r', 'p', 'v', 'm', 'f')
                """
            ),
            {"owner_role": MCP_LEGACY_MIGRATION_API_OWNER},
        ).all()
        expected_owner_tables = _MCP_LEGACY_MIGRATION_OWNER_TABLES
        if (
            {row.relname for row in owner_table_authority if row.can_select}
            != expected_owner_tables
            or {row.relname for row in owner_table_authority if row.can_insert}
            != expected_owner_tables
            or any(bool(row.can_mutate_existing) for row in owner_table_authority)
        ):
            raise RuntimeError(
                "MCP legacy migration API owner has unexpected table authority"
            )

        schema = connection.execute(
            text(
                """
                SELECT namespace.oid, schema_owner.rolname AS schema_owner,
                    namespace.nspowner = :owner_oid AS owns_schema,
                    pg_catalog.has_schema_privilege(
                        :owner_role, namespace.oid, 'CREATE'
                    ) AS can_create_api_objects
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_roles AS schema_owner
                  ON schema_owner.oid = namespace.nspowner
                WHERE namespace.nspname = 'mcp_migration_api'
                """
            ),
            {
                "owner_oid": owner.oid,
                "owner_role": MCP_LEGACY_MIGRATION_API_OWNER,
            },
        ).one_or_none()
        if (
            schema is None
            or bool(schema.owns_schema)
            or bool(schema.can_create_api_objects)
        ):
            raise RuntimeError("MCP legacy migration API ownership is invalid")

        schema_privileges = connection.execute(
            text(
                """
                SELECT COALESCE(grantee.rolname, 'PUBLIC') AS grantee,
                    privilege.privilege_type, privilege.is_grantable
                FROM pg_catalog.pg_namespace AS namespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        namespace.nspacl,
                        pg_catalog.acldefault('n'::"char", namespace.nspowner)
                    )
                ) AS privilege
                LEFT JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = privilege.grantee
                WHERE namespace.nspname = 'mcp_migration_api'
                """
            )
        ).all()
        schema_grants = {
            (row.grantee, row.privilege_type) for row in schema_privileges
        }
        expected_schema_grants = {
            (schema.schema_owner, "CREATE"),
            (schema.schema_owner, "USAGE"),
            (MCP_LEGACY_MIGRATION_API_OWNER, "USAGE"),
            (MCP_LEGACY_MIGRATION_DATABASE_ROLE, "USAGE"),
        }
        if schema_grants != expected_schema_grants or any(
            bool(row.is_grantable)
            for row in schema_privileges
            if row.grantee != schema.schema_owner
        ):
            raise RuntimeError("MCP legacy migration API schema ACL is invalid")

        functions = connection.execute(
            text(
                """
                SELECT procedure.oid, procedure.proname, procedure.prosecdef,
                    procedure.proconfig, function_owner.rolname AS owner_role,
                    pg_catalog.pg_get_function_result(procedure.oid) AS result_type,
                    ARRAY(
                        SELECT pg_catalog.format_type(argument.oid, NULL)
                        FROM pg_catalog.unnest(
                            procedure.proargtypes::pg_catalog.oid[]
                        ) WITH ORDINALITY AS argument(oid, position)
                        ORDER BY argument.position
                    ) AS argument_types
                FROM pg_catalog.pg_proc AS procedure
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = procedure.pronamespace
                JOIN pg_catalog.pg_roles AS function_owner
                  ON function_owner.oid = procedure.proowner
                WHERE namespace.nspname = 'mcp_migration_api'
                """
            )
        ).all()
        function_contract = {
            (row.proname, tuple(row.argument_types)): (
                row.result_type,
                bool(row.prosecdef),
                row.owner_role,
                tuple(row.proconfig or ()),
            )
            for row in functions
        }
        if function_contract != _MCP_LEGACY_MIGRATION_FUNCTION_CONTRACT:
            raise RuntimeError("MCP legacy migration API function contract is invalid")

        function_privileges = connection.execute(
            text(
                """
                SELECT procedure.proname,
                    ARRAY(
                        SELECT pg_catalog.format_type(argument.oid, NULL)
                        FROM pg_catalog.unnest(
                            procedure.proargtypes::pg_catalog.oid[]
                        ) WITH ORDINALITY AS argument(oid, position)
                        ORDER BY argument.position
                    ) AS argument_types,
                    COALESCE(grantee.rolname, 'PUBLIC') AS grantee,
                    privilege.privilege_type, privilege.is_grantable
                FROM pg_catalog.pg_proc AS procedure
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = procedure.pronamespace
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                    COALESCE(
                        procedure.proacl,
                        pg_catalog.acldefault('f'::"char", procedure.proowner)
                    )
                ) AS privilege
                LEFT JOIN pg_catalog.pg_roles AS grantee
                  ON grantee.oid = privilege.grantee
                WHERE namespace.nspname = 'mcp_migration_api'
                """
            )
        ).all()
        function_grants = {
            (
                row.proname,
                tuple(row.argument_types),
                row.grantee,
                row.privilege_type,
            )
            for row in function_privileges
        }
        expected_function_grants = {
            (
                function_name,
                argument_types,
                MCP_LEGACY_MIGRATION_API_OWNER,
                "EXECUTE",
            )
            for function_name, argument_types in (
                _MCP_LEGACY_MIGRATION_FUNCTION_CONTRACT
            )
        } | {
            (
                function_name,
                argument_types,
                MCP_LEGACY_MIGRATION_DATABASE_ROLE,
                "EXECUTE",
            )
            for function_name, argument_types in (
                _MCP_LEGACY_MIGRATION_EXECUTE_SIGNATURES
            )
        }
        if function_grants != expected_function_grants or any(
            bool(row.is_grantable)
            for row in function_privileges
            if row.grantee != MCP_LEGACY_MIGRATION_API_OWNER
        ):
            raise RuntimeError("MCP legacy migration API function ACL is invalid")

        trigger = connection.execute(
            text(
                """
                SELECT trigger.tgenabled, trigger.tgtype,
                    function_namespace.nspname AS function_schema,
                    procedure.proname AS function_name
                FROM pg_catalog.pg_trigger AS trigger
                JOIN pg_catalog.pg_proc AS procedure
                  ON procedure.oid = trigger.tgfoid
                JOIN pg_catalog.pg_namespace AS function_namespace
                  ON function_namespace.oid = procedure.pronamespace
                WHERE trigger.tgrelid =
                    'public.mcp_legacy_migration_record'::pg_catalog.regclass
                  AND trigger.tgname =
                    'mcp_legacy_migration_record_append_only'
                  AND NOT trigger.tgisinternal
                """
            )
        ).one_or_none()
        if trigger is None or tuple(trigger) != (
            "O",
            27,
            "mcp_migration_api",
            "reject_legacy_migration_mutation",
        ):
            raise RuntimeError(
                "MCP legacy migration append-only trigger is invalid"
            )
        return str(identity.rolname)
