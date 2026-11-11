import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from concurrency_harness.runner import Run, digest, load_config, provenance, schedule
from concurrency_harness.cli import report
from concurrency_harness.persistent import PersistentRun


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


class SimulatedPersistentRun(PersistentRun):
    setup = SimulatedRun.setup

    def agent(self, worktree, prompt, role, task_id, attempt=1):
        self.roles.append(role)
        self.peak = max(self.peak, 1)
        active = set(self.task_map)
        if role == "implementation" or self.config.get("persistent_never_repairs"):
            active.remove("task10")  # Final contract failure is repaired by the same session.
        (worktree / "features.py").write_text(f"ACTIVE = {sorted(active)!r}\n")
        log = self.root / f"{role}-{attempt}.jsonl"
        log.write_text(json.dumps(dict(type="thread.started", thread_id="one-persistent-thread")) + "\n")
        self.capture_session(dict(stdout=str(log), command_id=len(self.roles)))
        return True


class HarnessTests(unittest.TestCase):
    def test_persistent_failed_repairs_leave_main_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, revision = self.make_source(root)
            config, tasks = load_config("benchmark/persistent.json")
            config.update(repository=str(source), revision=revision, persistent_never_repairs=True)
            run = SimulatedPersistentRun(root / "run", config, tasks, 1, 1)
            result = run.run()
            self.assertEqual(result["status"], "failed", result.get("error", result))
            self.assertEqual(result["integrated_tasks"], [])
            self.assertEqual(result["final_commit"], revision)
            self.assertEqual(result["integration_attempts"], 2)
            self.assertEqual(run.roles, ["implementation", "integration", "integration"])

    def test_persistent_full_workload_and_integration_keep_one_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, revision = self.make_source(root)
            config, tasks = load_config("benchmark/persistent.json")
            config.update(repository=str(source), revision=revision)
            run = SimulatedPersistentRun(root / "run", config, tasks, 1, 1)
            result = run.run()
            self.assertEqual(result["status"], "passed", result.get("error", result))
            self.assertEqual(result["treatment"], "persistent_workload")
            self.assertEqual(result["session_id"], "one-persistent-thread")
            self.assertEqual(run.roles, ["implementation", "integration"])
            self.assertEqual(len(result["integrated_tasks"]), 10)
            self.assertFalse(result["implementation_validation_passed"])
            self.assertFalse(result["tasks"]["task10"]["implementation_acceptance_passed"])
            events = [json.loads(line) for line in (run.root / "events.jsonl").read_text().splitlines()]
            sessions = [e for e in events if e["event"] == "session_observed"]
            self.assertEqual([e["resumed"] for e in sessions], [False, True])

    def test_persistent_command_resumes_exact_session_and_rejects_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            config, tasks = load_config("benchmark/persistent.json")
            run = PersistentRun(Path(directory) / "run", config, tasks, 1, 1)
            first = run.agent_argv(run.repo)
            self.assertNotIn("--ephemeral", first)
            self.assertNotIn("resume", first)
            log = run.root / "session.jsonl"
            log.write_text(json.dumps(dict(type="thread.started", thread_id="same-session")))
            result = dict(stdout=str(log), command_id=1)
            run.capture_session(result)
            resumed = run.agent_argv(run.repo)
            self.assertEqual(resumed[-3:], ["resume", "same-session", "-"])
            self.assertNotIn("--last", resumed)
            log.write_text(json.dumps(dict(type="thread.started", thread_id="replacement")))
            with self.assertRaisesRegex(RuntimeError, "session changed"):
                run.capture_session(result)
            log.write_text("{}\n")
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                run.capture_session(result)

    def test_reports_keep_persistent_and_fresh_one_agent_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            for treatment in ("fresh_per_task", "persistent_workload"):
                folder = Path(directory) / treatment
                folder.mkdir()
                (folder / "summary.json").write_text(json.dumps(dict(
                    treatment=treatment, agents=1, status="passed", work_seconds=10)))
            result = report(directory)
            self.assertEqual(len(result["groups"]), 2)
            self.assertEqual({g["treatment"] for g in result["groups"]}, {"fresh_per_task", "persistent_workload"})
            self.assertTrue(all(g["runs"] == 1 for g in result["groups"]))

    def test_default_interpreter_creates_a_working_venv(self):
        config, tasks = load_config("benchmark/config.json")
        self.assertEqual(config["python"], sys.executable)
        with tempfile.TemporaryDirectory() as directory:
            run = Run(Path(directory) / "run", config, tasks, 1, 1)
            run.checked([config["python"], "-m", "venv", "--without-pip", run.python.parent.parent])
            version = run.checked([run.python, "-c", "import platform; print(platform.python_version())"])
            import platform
            self.assertEqual(version, platform.python_version())

    def test_explicit_missing_interpreter_fails_before_clone(self):
        config, tasks = load_config("benchmark/config.json")
        with tempfile.TemporaryDirectory() as directory:
            config["python"] = str(Path(directory) / "missing-python")
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(config))
            explicit, _ = load_config(config_path)
            self.assertEqual(explicit["python"], config["python"])
            run = Run(Path(directory) / "run", explicit, tasks, 1, 1)
            with patch.object(run, "command") as command:
                result = run.run()
                command.assert_not_called()
            self.assertEqual(result["status"], "failed")
            self.assertIn('Set "python": null', result["error"])
            self.assertFalse(run.repo.exists())
            self.assertEqual(json.loads((run.root / "summary.json").read_text())["status"], "failed")

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

    def test_results_preserve_observed_versions_and_requested_model(self):
        config, tasks = load_config("benchmark/config.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = Run(root / "old-run", config, tasks, 3, 1)
            metadata = dict(revision="a" * 40, codex_version="codex-cli historical")
            (run.root / "config.json").write_text(json.dumps(config))
            (run.root / "metadata.json").write_text(json.dumps(metadata))
            # Older summaries lack provenance; report must recover it from artifacts.
            (run.root / "summary.json").write_text(json.dumps(dict(agents=3, status="failed")))
            result = report(root)
            recorded = result["runs"][0]["provenance"]
            self.assertEqual(recorded["baseline_commit"], "a" * 40)
            self.assertEqual(recorded["requested_baseline_commit"], config["revision"])
            self.assertEqual(recorded["model"], config["model"])
            self.assertEqual(recorded["codex_version"], "codex-cli historical")
            self.assertEqual(result["provenance"], [recorded])
            self.assertEqual(run.summary["provenance"], provenance(config))
            self.assertIsNone(run.summary["provenance"]["baseline_commit"])
            self.assertIsNone(run.summary["provenance"]["codex_version"])

    def test_report_preserves_partial_setup_observations(self):
        config, tasks = load_config("benchmark/config.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = Run(root / "partial-run", config, tasks, 1, 1)
            run.summary["provenance"]["baseline_commit"] = "b" * 40
            run.summary["status"] = "failed"
            (run.root / "summary.json").write_text(json.dumps(run.summary))
            result = report(root)
            self.assertEqual(result["runs"][0]["provenance"]["baseline_commit"], "b" * 40)
            self.assertIsNone(result["runs"][0]["provenance"]["codex_version"])


if __name__ == "__main__":
    unittest.main()
