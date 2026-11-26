import hashlib

import psycopg

from apps.api.models.hashing import compute_plan_hash

conn = psycopg.connect("postgresql://opspilot@127.0.0.1:54333/opspilot")
cur = conn.cursor()

cases = [
    {
        "action": "rollback_service",
        "service": "gateway",
        "expected_version": "v1.2.3",
        "rationale_evidence_ids": ["ev-b", "ev-a"],
        "rollback_route": "git-revert",
    },
    {
        "action": "restart_service",
        "service": "orders",
        "expected_version": None,
        "rationale_evidence_ids": ["ev-1", "ev-2"],
        "rollback_route": None,
    },
    {
        "action": "rollback_service",
        "service": "orders",
        "expected_version": None,
        "rationale_evidence_ids": [],
        "rollback_route": None,
    },
]

for c in cases:
    ev_array = c["rationale_evidence_ids"]
    cur.execute(
        """
        WITH ev AS (
            SELECT COALESCE('[' || string_agg(to_json(elem)::text, ',' ORDER BY elem) || ']', '[]') as v_ev
            FROM unnest(%s::text[]) AS elem
        )
        SELECT 
            concat(
                '{"action":', to_json(%s::text),
                ',"expected_version":', CASE WHEN %s::text IS NOT NULL THEN to_json(%s::text) ELSE 'null' END,
                ',"rationale_evidence_ids":', COALESCE((SELECT v_ev FROM ev), '[]'),
                ',"rollback_route":', CASE WHEN %s::text IS NOT NULL THEN to_json(%s::text) ELSE 'null' END,
                ',"service":', CASE WHEN %s::text IS NOT NULL THEN to_json(%s::text) ELSE 'null' END,
                '}'
            );
        """,
        (
            ev_array,
            c["action"],
            c["expected_version"],
            c["expected_version"],
            c["rollback_route"],
            c["rollback_route"],
            c["service"],
            c["service"],
        ),
    )
    pg_str = cur.fetchone()[0]
    pg_hash = hashlib.sha256(pg_str.encode()).hexdigest()
    py_hash = compute_plan_hash(**c)
    assert pg_hash == py_hash, f"Mismatch!\nPG: {pg_str} -> {pg_hash}\nPY: {py_hash}"
    print(f"Match for action={c['action']}, service={c['service']}: {py_hash}")

print("ALL TEST CASES MATCHED PERFECTLY!")
