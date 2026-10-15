"""Reconciliation: compare MySQL's expected graph with Neo4j and repair any drift.

python -m app.graph.reconcile          # repair, print the drift found
python -m app.graph.reconcile --check  # report only; exit 1 if there is drift
"""

import argparse
import logging
import sys

from neo4j import Driver

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import session_scope
from app.graph import snapshot
from app.graph.client import dump_graph, ensure_constraints, get_driver, write_reference, write_tenant
from app.graph.drift import Drift, diff

log = logging.getLogger(__name__)


def project_tenant(driver: Driver, tenant_id: str) -> None:
    """Re-project one tenant from MySQL. Idempotent; also removes tenants deleted in MySQL."""
    with session_scope() as session:
        graph = snapshot.tenant_graph(session, tenant_id, get_settings().graph_change_events_per_tenant)
    write_tenant(driver, tenant_id, graph)


def project_all(driver: Driver) -> None:
    with session_scope() as session:
        reference = snapshot.reference_graph(session)
        ids = snapshot.tenant_ids(session)
    write_reference(driver, reference)
    for tid in ids:
        project_tenant(driver, tid)
    # Tenants that exist only in the graph
    stale = driver.execute_query("MATCH (t:Tenant) WHERE NOT t.id IN $ids RETURN t.id AS id", ids=ids).records
    for rec in stale:
        write_tenant(driver, rec["id"], None)


def compute_drift(driver: Driver) -> Drift:
    with session_scope() as session:
        expected = snapshot.full_graph(session, get_settings().graph_change_events_per_tenant)
    return diff(expected, dump_graph(driver))


def reconcile(driver: Driver, fix: bool = True) -> Drift:
    drift = compute_drift(driver)
    if drift.total:
        log.warning("Graph drift: %s", drift.summary())
        if fix:
            project_all(driver)
            remaining = compute_drift(driver)
            log.info("After repair: %s", remaining.summary())
    else:
        log.info("Graph in sync (no drift)")
    return drift


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report only, do not repair")
    args = parser.parse_args()
    driver = get_driver()
    ensure_constraints(driver)
    drift = reconcile(driver, fix=not args.check)
    print(drift.summary())
    return 1 if (args.check and drift.total) else 0


if __name__ == "__main__":
    sys.exit(main())
