import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from concurrency_harness.runner import Run, digest, load_config, schedule
from concurrency_harness.cli import report


class SimulatedRun(Run):
    """Real Git, subprocess tests, and scheduler; deterministic stand-in agents."""
    def setup(self):
        self.checked(["git", "clone", self.config["repository"], self.repo], self.root)
        self.git("checkout", "-B", "main", self.config["revision"])
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", "/dev/null")
        self.git("config", "rerere.enabled", "false")
        self.python = Path(sys.executable)
        self.suite.mkdir()
        self.suite.joinpath("test_benchmark.py").write_text("\n".join(
            f"def test_task{i:02}():\n    from features import ACTIVE\n    assert 'task{i:02}' in ACTIVE\n" for i in range(1, 11)))
        self.evaluation_hashes = {"test_benchmark.py": digest(self.suite / "test_benchmark.py")}
        self.active = self.peak = 0
        self.roles = []
        assert self.validate(self.repo, [], "baseline")

    def agent(self, worktree, prompt, role, task_id, attempt=1):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.roles.append(role)
        try:
            time.sleep(0.02)
            if task_id == self.config.get("fail_task"):
                return False
            if role == "implementation":
                namespace = {}
                exec((worktree / "features.py").read_text(), namespace)
                active = set(namespace["ACTIVE"]) | {task_id}
            else:
                active = set(self.integrated) | {task_id}
                if self.config.get("reject_first_integration") == task_id and attempt == 1:
                    active.discard(task_id)
            (worktree / "features.py").write_text(f"ACTIVE = {sorted(active)!r}\n")
            return True
        finally:
            with self.lock:
                self.active -= 1


class HarnessTests(unittest.TestCase):
    def make_source(self, root):
        source = root / "source"
        source.mkdir()
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=source, stderr=subprocess.DEVNULL).decode().strip()
        git("init")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        git("config", "commit.gpgsign", "false")
        (source / "features.py").write_text("ACTIVE = []\n")
        # Use a set in implementations while keeping repr output portable.
        (source / "features.py").write_text("ACTIVE = set()\n")
        (source / "tests").mkdir()
        (source / "tests/test_base.py").write_text("def test_baseline():\n    assert True\n")
        git("add", ".")
        git("commit", "-m", "Baseline")
        return source, git("rev-parse", "HEAD")

    def test_matrix_reproducible_and_balanced(self):
        config, _ = load_config("benchmark/config.json")
        first = schedule(config)
        self.assertEqual(first, schedule(config))
        self.assertEqual(len(first), 12)
        for trial in range(1, 4):
            self.assertEqual(sorted(r["agents"] for r in first if r["trial"] == trial), [1, 3, 5, 10])

    def test_real_git_scheduler_and_conflict_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, revision = self.make_source(root)
            config, tasks = load_config("benchmark/config.json")
            config.update(repository=str(source), revision=revision)
            for agents in (1, 3):
                run = SimulatedRun(root / f"run-{agents}", config, tasks, agents, 1)
                result = run.run()
                self.assertEqual(result["status"], "passed", result.get("error", result))
                self.assertLessEqual(run.peak, agents)
                self.assertEqual(run.roles.count("implementation"), 10)
                self.assertEqual(run.roles.count("integration"), 10)
                self.assertEqual(len(result["integrated_tasks"]), 10)
                events = [json.loads(line) for line in (run.root / "events.jsonl").read_text().splitlines()]
                self.assertLessEqual(max(e["active_jobs"] for e in events if e["event"] == "job_dispatched"), agents)
                if agents == 3:
                    self.assertTrue(any(e.get("conflict_files") for e in events))

    def test_failure_retained_and_other_tasks_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, revision = self.make_source(root)
            config, tasks = load_config("benchmark/config.json")
            config.update(repository=str(source), revision=revision, fail_task="task02")
            run = SimulatedRun(root / "run", config, tasks, 3, 1)
            result = run.run()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["tasks"]["task02"]["status"], "implementation_failed")
            self.assertEqual(len(result["integrated_tasks"]), 9)

    def test_setup_failure_written_and_timeout_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            config, tasks = load_config("benchmark/config.json")
            config["repository"] = str(Path(directory) / "missing")
            run = Run(Path(directory) / "run", config, tasks, 1, 1)
            result = run.run()
            self.assertEqual(result["status"], "failed")
            self.assertTrue((run.root / "summary.json").exists())
            timeout = run.command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
            self.assertTrue(timeout["timed_out"])
            self.assertNotEqual(timeout["returncode"], 0)

    def test_integration_retry_rechecks_contract_and_uses_fresh_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, revision = self.make_source(root)
            config, tasks = load_config("benchmark/config.json")
            config.update(repository=str(source), revision=revision, reject_first_integration="task01")
            run = SimulatedRun(root / "run", config, tasks, 3, 1)
            result = run.run()
            self.assertEqual(result["status"], "passed", result.get("error", result))
            self.assertEqual(result["tasks"]["task01"]["integration_attempts"], 2)
            self.assertEqual(run.roles.count("integration"), 11)
            self.assertLessEqual(run.peak, 3)
            self.assertIn("task01-integration-1", run.validations)
            self.assertNotEqual(run.validations["task01-integration-1"]["returncode"], 0)

    def test_codex_command_is_fresh_for_every_role(self):
        with tempfile.TemporaryDirectory() as directory:
            config, tasks = load_config("benchmark/config.json")
            run = Run(Path(directory) / "run", config, tasks, 3, 1)
            tree = run.root / "tree"
            tree.mkdir()
            log = run.root / "fake.stdout"
            log.write_text("")
            result = dict(returncode=0, timed_out=False, stdout=str(log), stderr=str(log), seconds=0)
            with patch.object(run, "git", return_value=""), patch.object(run, "command", return_value=result) as command:
                for role in ("implementation", "integration"):
                    self.assertTrue(run.agent(tree, "assignment", role, "task01"))
                self.assertEqual(command.call_count, 2)
                for call in command.call_args_list:
                    argv = call.args[0]
                    self.assertIn("--ephemeral", argv)
                    self.assertIn("--ignore-user-config", argv)
                    self.assertIn("features.multi_agent=false", argv)
                    self.assertIn(config["model"], argv)
                    self.assertNotIn("resume", argv)

    def test_report_exposes_failed_trials(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, status in enumerate(("passed", "failed")):
                path = Path(directory) / str(index)
                path.mkdir()
                (path / "summary.json").write_text(json.dumps(dict(agents=3, status=status, work_seconds=10)))
            result = report(directory)
            self.assertEqual(result["groups"][0]["failures"], 1)
            self.assertEqual(len(result["runs"]), 2)


if __name__ == "__main__":
    unittest.main()
