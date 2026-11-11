"""One logical Codex session receives the full workload and performs integration."""
import json
from pathlib import Path
import time

from .runner import Run


class PersistentRun(Run):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.agents != 1:
            raise ValueError("persistent_workload requires exactly one agent")
        self.session_id = None

    def agent_argv(self, worktree):
        # Parent exec options precede resume: -C and --sandbox belong to exec.
        argv = [self.config["codex"], "exec", "--ignore-user-config", "--json",
                "--model", self.config["model"], "-c", f'model_reasoning_effort="{self.config["reasoning_effort"]}"',
                "-c", "features.multi_agent=false", "--sandbox", "workspace-write", "-C", worktree]
        if self.session_id:
            argv += ["resume", self.session_id]
        return argv + ["-"]

    def capture_session(self, result):
        ids = set()
        for line in Path(result["stdout"]).read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                ids.add(event["thread_id"])
        if len(ids) != 1:
            raise RuntimeError("Persistent invocation must emit exactly one thread.started session ID")
        observed = ids.pop()
        if self.session_id is not None and self.session_id != observed:
            raise RuntimeError(f"Persistent session changed from {self.session_id} to {observed}")
        resumed = self.session_id is not None
        self.session_id = observed
        self.summary["session_id"] = observed
        self.event("session_observed", session_id=observed, resumed=resumed,
                   command_id=result["command_id"])

    def common_prompt(self):
        return ("You are the sole agent for this entire workload, including implementation, integration, "
                "and repair. Continue only this same session for follow-up assignments. Do not spawn "
                "subagents, call other models, or resume other sessions. Edit only the current worktree. "
                "The harness owns Git commits, branch switches, and merges; do not perform those Git "
                "operations yourself. You own all code review, conflict resolution, and regression fixes. "
                "Use python -m pytest -q; dependencies are installed on PATH. Never remove or weaken tests. "
                "The repository statement that it has no automated tests is outdated.\n")

    def execute_tasks(self):
        self.work_started = time.monotonic()
        self.event("work_started", treatment="persistent_workload")
        tree = self.root / "worktrees" / "persistent"
        base = self.git("rev-parse", "main")
        self.git("worktree", "add", "-b", "persistent-implementation", str(tree), base)
        for result in self.results.values():
            result.update(status="implementation", base=base)
        prompt = self.common_prompt() + "Implement all ten assignments below in ID order. "
        prompt += "Keep working until the complete workload and your tests are finished. "
        prompt += "You have the full workload upfront and may make shared supporting changes.\n"
        prompt += "\n".join(self.task_prompt(task) for task in self.tasks)
        agent_ok = self.agent(tree, prompt, "implementation", "workload")
        # Every task receives its own immutable acceptance result, plus a combined gate.
        passed = {tid: self.validate(tree, [tid], f"{tid}-implementation") for tid in self.task_map}
        for tid, valid in passed.items():
            self.results[tid]["implementation_acceptance_passed"] = valid
        implementation_ok = self.validate(tree, list(self.task_map), "workload-implementation")
        # Unlike fresh-per-task mode, this actor may repair the whole workload during integration.
        # A failed command or timeout ends the run; do not silently replace or restart the actor.
        if agent_ok:
            self.git("add", "-A", cwd=tree)
            self.git("commit", "--allow-empty", "-m", "Implement persistent workload", cwd=tree)
            implementation = self.git("rev-parse", "HEAD", cwd=tree)
            self.summary["implementation_commit"] = implementation
            self.summary["implementation_validation_passed"] = implementation_ok
            self.git("diff", "--binary", base, implementation, cwd=tree)
            self.git("checkout", "-b", "persistent-integration", "main", cwd=tree)
            merge = self.command(["git", "merge", "--no-commit", "--no-ff", "persistent-implementation"], tree)
            conflicts = self.git("diff", "--name-only", "--diff-filter=U", cwd=tree).splitlines()
            self.event("merge_attempt", task_id="workload", parent=base, conflict_files=conflicts, **merge)
            if merge["returncode"] == 0 or conflicts:
                validation = self.validations["workload-implementation"]
                feedback = ("Implementation gate output (repair any failures during integration):\n"
                            + Path(validation["stdout"]).read_text()[-24000:]
                            + Path(validation["stderr"]).read_text()[-8000:])
                self.integrate_workload(tree, feedback)
        final_ok = self.validate(self.repo, list(self.task_map), "final-all-ten")
        if not self.integrated:
            for tid in self.task_map:
                self.results[tid]["status"] = "integration_failed" if agent_ok else "implementation_failed"
        self.summary["work_seconds"] = time.monotonic() - self.work_started
        self.summary["status"] = "passed" if final_ok and len(self.integrated) == 10 else "failed"
        self.summary["final_commit"] = self.git("rev-parse", "HEAD")

    def integrate_workload(self, tree, feedback):
        for attempt in range(1, self.config["integration_attempts"] + 1):
            self.summary["integration_attempts"] = attempt
            for result in self.results.values():
                result["status"] = "integration"
            prompt = self.common_prompt() + "The harness has committed your implementation and merged it "
            prompt += "into a candidate based on main, in the SAME worktree path. "
            prompt += "Integrate and verify all ten contracts you implemented.\n" + feedback
            agent_ok = self.agent(tree, prompt, "integration", "workload", attempt)
            self.git("add", "-A", cwd=tree)
            check = self.command(["git", "diff", "--cached", "--check"], tree)
            valid = self.validate(tree, list(self.task_map), f"workload-integration-{attempt}")
            if agent_ok and valid and check["returncode"] == 0:
                self.git("commit", "--allow-empty", "-m", "Integrate persistent workload", cwd=tree)
                head = self.git("rev-parse", "HEAD", cwd=tree)
                self.git("diff", "--binary", "persistent-implementation", head, cwd=tree)
                self.git("reset", "--hard", head)
                for tid in self.task_map:
                    self.integrated.append(tid)
                    self.results[tid].update(status="passed", integration_commit=head,
                                             integration_attempts=attempt)
                    self.event("task_integrated", task_id=tid, commit=head)
                return
            self.event("integration_rejected", task_id="workload", attempt=attempt,
                       agent_ok=agent_ok, validation_ok=valid)
            if not agent_ok:
                return
            result = self.validations[f"workload-integration-{attempt}"]
            feedback = ("The harness rejected the previous candidate. Fix these failures:\n"
                        + Path(result["stdout"]).read_text()[-24000:]
                        + Path(result["stderr"]).read_text()[-8000:]
                        + Path(check["stdout"]).read_text()[-8000:])
