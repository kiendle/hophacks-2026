"""Read cumulative per-automation charges across every durable native batch."""
from pathlib import Path
from contextlib import closing
import sqlite3


def usage(database: Path, automation_id: str, excluding_run: str = ''):
    if not database.exists():
        return dict(spent=0., reserved=0., uncertain=0.)
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        row = conn.execute('''SELECT COALESCE(SUM(b.actual_cost_usd),0),
            COALESCE(SUM(b.reserved_usd),0),COALESCE(SUM(b.uncertain_usd),0)
            FROM route_budgets b JOIN runs r ON r.run_id=b.run_id
            WHERE json_extract(r.manifest_json,'$.automation_id')=? AND r.run_id<>?''',
            (automation_id, excluding_run)).fetchone()
        return dict(zip(('spent', 'reserved', 'uncertain'), row))
