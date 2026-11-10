"""Verify the public acceptance suite against a clean baseline and an oracle.

This is a harness self-check, never a benchmark trial or an agent result.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from concurrency_harness.runner import load_config


REFERENCE_MODELS = '''
# Reference properties used only by the harness self-check.
from decimal import Decimal as _D
Product.stock_value = property(lambda p: {
    "cases": round(float(_D(p.cases_in_stock) * p.wholesale_price_per_case), 2),
    "bags": round(float(_D(p.bags_in_stock) * p.retail_price_per_bag), 2),
    "total": round(float(_D(p.cases_in_stock) * p.wholesale_price_per_case + _D(p.bags_in_stock) * p.retail_price_per_bag), 2)})
Product.needs_restock = property(lambda p: bool(p.active and (p.cases_in_stock < 5 or p.bags_in_stock < 10)))
GreenCoffeeLot.remaining_value_usd = property(lambda l: float(round(l.weight_kg_remaining * l.cost_per_kg_usd, 2)))
RoastBatch.weight_loss_pct = property(lambda b: None if not b.green_weight_kg or b.roasted_weight_kg is None else round(float((b.green_weight_kg-b.roasted_weight_kg)/b.green_weight_kg*100),1))
def _coverage(o):
    ordered = sum(i.quantity for i in o.items)
    allocated = sum(s.cases or 0 for s in o.shipments if s.method == "delivery_truck")
    return dict(ordered_quantity=ordered, allocated_quantity=allocated, unallocated_quantity=max(ordered-allocated,0))
Order.shipment_coverage = property(_coverage)
Order.days_overdue = property(lambda o: max((date.today()-o.due_date).days,0) if o.due_date and o.status not in ("delivered","cancelled") else 0)
Customer.delivered_revenue = property(lambda c: float(round(sum((i.unit_price*i.quantity for o in c.orders if o.status == "delivered" for i in o.items), _D(0)),2)))
Customer.active_subscription_quantity = property(lambda c: sum(s.quantity for s in c.subscriptions if s.status == "active"))
Shipment.capacity_utilization_pct = property(lambda s: round(s.cases/s.trucking_service.capacity_cases*100,1) if s.method == "delivery_truck" and s.trucking_service and s.trucking_service.capacity_cases > 0 and s.cases is not None else None)
Subscription.monthly_quantity = property(lambda s: round(s.quantity*{"weekly":52/12,"biweekly":26/12,"monthly":1}.get(s.frequency,0),2) if s.status == "active" else 0.0)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default="https://github.com/jefflinwood/ridgeline.git")
    args = parser.parse_args()
    config, tasks = load_config("benchmark/config.json")
    with tempfile.TemporaryDirectory(prefix="workload-check-") as directory:
        root = Path(directory)
        repo, suite = root / "repository", root / "evaluation"
        subprocess.run(["git", "clone", args.repository, str(repo)], check=True)
        subprocess.run(["git", "checkout", "--detach", config["revision"]], cwd=repo, check=True)
        shutil.copytree(repo / "tests", suite)
        shutil.copyfile(config["acceptance"], suite / "test_benchmark.py")
        import os
        env = dict(os.environ, PYTHONPATH=str(repo), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
        base = [sys.executable, "-m", "pytest", "-q", "--import-mode=importlib", "-p", "no:cacheprovider"]
        baseline = [str(p) for p in suite.glob("test_*.py") if p.name != "test_benchmark.py"]
        subprocess.run(base + baseline, cwd=repo, env=env, check=True)
        negative = subprocess.run(base + [str(suite / "test_benchmark.py"), f"--junitxml={root / 'negative.xml'}"], cwd=repo, env=env, capture_output=True)
        tree = ET.parse(root / "negative.xml")
        cases = list(tree.iter("testcase"))
        assert negative.returncode == 1 and cases and all(c.find("failure") is not None for c in cases), negative.stdout.decode()
        assert {c.attrib["name"].split("[")[0] for c in cases} == {f"test_{t['id']}" for t in tasks}
        with (repo / "app/models.py").open("a") as stream:
            stream.write(REFERENCE_MODELS)
        with (repo / "app/api.py").open("a") as stream:
            stream.write('\n\ndef _benchmark_field(serializer, field):\n    def wrapped(obj, *args, **kwargs):\n        result = serializer(obj, *args, **kwargs)\n        result[field] = getattr(obj, field)\n        return result\n    return wrapped\n')
            for task in tasks:
                stream.write(f"{task['serializer']} = _benchmark_field({task['serializer']}, {task['field']!r})\n")
        subprocess.run(base + [str(suite)], cwd=repo, env=env, check=True)
        print(f"Baseline passed; all {len(cases)} acceptance cases rejected the baseline and passed the reference implementation.")


if __name__ == "__main__":
    main()
