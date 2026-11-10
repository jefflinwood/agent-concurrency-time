"""An agent is a subprocess invocation; the scheduler caps all roles together."""
from __future__ import annotations

import concurrent.futures as futures
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def provenance(config, metadata=None):
    """Keep requested settings distinct from observed checkout/tool versions."""
    metadata = metadata or {}
    return dict(repository=config.get("repository"),
                requested_baseline_commit=config.get("revision"),
                baseline_commit=metadata.get("revision"),
                model=config.get("model"),
                reasoning_effort=config.get("reasoning_effort"),
                requested_codex_version=config.get("codex_version"),
                codex_version=metadata.get("codex_version"))


def schedule(config):
    rng = random.Random(config["seed"])
    rows = []
    for trial in range(1, config["trials"] + 1):
        levels = list(config["concurrency"])
        rng.shuffle(levels)
        rows.extend({"trial": trial, "agents": n} for n in levels)
    return rows


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    if config.get("python") is None:
        config["python"] = sys.executable
    elif not isinstance(config["python"], str) or not config["python"].strip():
        raise ValueError("python must be null or an interpreter executable/path")
    for key in ("tasks", "acceptance", "constraints"):
        config[key] = str((path.parent / config[key]).resolve())
    if len(config["revision"]) != 40 or any(c not in "0123456789abcdef" for c in config["revision"]):
        raise ValueError("revision must be a full lowercase 40-character commit SHA")
    for key in ("trials", "agent_timeout_seconds", "command_timeout_seconds", "integration_attempts"):
        if not isinstance(config[key], int) or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if not config["concurrency"] or any(type(n) is not int or n < 1 for n in config["concurrency"]):
        raise ValueError("concurrency levels must be positive integers")
    tasks = json.loads(Path(config["tasks"]).read_text())
    if len(tasks) != 10 or len({t["id"] for t in tasks}) != 10:
        raise ValueError("workload must contain ten uniquely identified tasks")
    for task in tasks:
        if not task["id"].isalnum():
            raise ValueError("task IDs must be alphanumeric")
    return config, tasks


class Run:
    def __init__(self, root, config, tasks, agents, trial):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self.config, self.tasks = config, tasks
        self.agents, self.trial = agents, trial
        self.repo = self.root / "repository"
        self.suite = self.root / "evaluation"
        self.python = self.root / "venv/bin/python"
        self.lock = threading.Lock()
        self.sequence = 0
        self.origin = time.monotonic()
        self.integrated = []
        self.results = {t["id"]: {"status": "pending"} for t in tasks}
        self.validations = {}
        self.task_map = {t["id"]: t for t in tasks}
        self.work_started = None
        self.summary = dict(agents=agents, trial=trial, status="running", tasks=self.results,
                            provenance=provenance(config))

    def event(self, kind, **fields):
        with self.lock:
            record = dict(event=kind, utc=datetime.now(timezone.utc).isoformat(),
                          elapsed_seconds=time.monotonic() - self.origin, **fields)
            with (self.root / "events.jsonl").open("a") as stream:
                stream.write(json.dumps(record) + "\n")

    def command(self, argv, cwd=None, *, timeout=None, env=None, stdin=None):
        with self.lock:
            self.sequence += 1
            command_id = self.sequence
        prefix = self.root / f"command-{command_id:05}"
        started = time.monotonic()
        self.event("command_started", command_id=command_id, argv=list(map(str, argv)), cwd=str(cwd))
        with prefix.with_suffix(".stdout").open("wb") as out, prefix.with_suffix(".stderr").open("wb") as err:
            process = subprocess.Popen(list(map(str, argv)), cwd=cwd, env=env,
                                       stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                       stdout=out, stderr=err, start_new_session=True)
            timed_out = False
            try:
                process.communicate(None if stdin is None else stdin.encode(),
                                    timeout=timeout or self.config["command_timeout_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            except BaseException:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
        result = dict(command_id=command_id, returncode=process.returncode, timed_out=timed_out,
                      seconds=time.monotonic() - started,
                      stdout=str(prefix.with_suffix(".stdout")), stderr=str(prefix.with_suffix(".stderr")))
        self.event("command_finished", **result)
        return result

    def checked(self, argv, cwd=None, **kwargs):
        result = self.command(argv, cwd, **kwargs)
        if result["returncode"] or result["timed_out"]:
            raise RuntimeError(f"Command failed; see {result['stderr']} and {result['stdout']}")
        return Path(result["stdout"]).read_text().strip()

    def git(self, *args, cwd=None):
        return self.checked(["git", *args], cwd or self.repo)

    def setup(self):
        (self.root / "config.json").write_text(json.dumps(self.config, indent=2))
        (self.root / "tasks.json").write_text(json.dumps(self.tasks, indent=2))
        self.event("setup_started", agents=self.agents, trial=self.trial)
        interpreter = shutil.which(self.config["python"])
        if interpreter is None:
            raise RuntimeError(
                f"Configured Python interpreter {self.config['python']!r} was not found. "
                'Set "python": null to use the interpreter launching the harness, '
                'or set "python" to an installed executable/path in your config.'
            )
        self.event("python_selected", executable=interpreter)
        self.checked(["git", "clone", self.config["repository"], self.repo], self.root)
        self.git("checkout", "-B", "main", self.config["revision"])
        self.summary["provenance"]["baseline_commit"] = self.git("rev-parse", "HEAD")
        self.git("config", "user.email", "benchmark@example.invalid")
        self.git("config", "user.name", "Concurrency Benchmark")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", "/dev/null")
        self.git("config", "rerere.enabled", "false")
        with (self.repo / ".git/info/exclude").open("a") as stream:
            stream.write("\n.pytest_cache/\n.coverage\nhtmlcov/\n")
        self.checked([interpreter, "-m", "venv", self.python.parent.parent], self.root)
        shutil.copyfile(self.config["constraints"], self.root / "constraints.txt")
        self.checked([self.python, "-m", "pip", "install", "-r", "requirements-dev.txt",
                      "-c", self.root / "constraints.txt"], self.repo)
        freeze = self.checked([self.python, "-m", "pip", "freeze", "--all"], self.repo)
        (self.root / "dependencies.txt").write_text(freeze + "\n")
        shutil.copytree(self.repo / "tests", self.suite)
        shutil.copyfile(self.config["acceptance"], self.suite / "test_benchmark.py")
        self.evaluation_hashes = {str(p.relative_to(self.suite)): digest(p)
                                  for p in self.suite.rglob("*.py")}
        metadata = dict(platform=platform.platform(), machine=platform.machine(), cpu_count=os.cpu_count(),
                        harness_python=platform.python_version(),
                        codex_version=self.checked([self.config["codex"], "--version"], self.root),
                        target_python=self.checked([self.python, "--version"], self.root),
                        revision=self.git("rev-parse", "HEAD"), evaluation_sha256=self.evaluation_hashes,
                        tasks_sha256=digest(self.root / "tasks.json"),
                        harness_sha256={p.name: digest(p) for p in Path(__file__).parent.glob("*.py")})
        (self.root / "metadata.json").write_text(json.dumps(metadata, indent=2))
        self.summary["provenance"] = provenance(self.config, metadata)
        if metadata["codex_version"] != self.config["codex_version"]:
            raise RuntimeError(f"Expected {self.config['codex_version']}, got {metadata['codex_version']}")
        if not self.validate(self.repo, [], "baseline"):
            raise RuntimeError("Baseline Python tests failed")
        self.event("setup_finished")

    def validate(self, worktree, task_ids, label):
        for relative, expected in self.evaluation_hashes.items():
            if digest(self.suite / relative) != expected:
                raise RuntimeError(f"Evaluation was modified: {relative}")
        env = dict(os.environ, PYTHONPATH=str(worktree), PYTHONDONTWRITEBYTECODE="1",
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="")
        nodes = [str(p) for p in sorted(self.suite.glob("test_*.py")) if p.name != "test_benchmark.py"]
        nodes += [str(worktree / "tests")]
        nodes += [f"{self.suite / 'test_benchmark.py'}::test_{task}" for task in task_ids]
        result = self.command([self.python, "-m", "pytest", "-q", "--import-mode=importlib",
                               "-p", "no:cacheprovider", *nodes], worktree, env=env)
        self.event("validation", label=label, task_ids=task_ids, **result)
        self.validations[label] = result
        return result["returncode"] == 0 and not result["timed_out"]

    def agent(self, worktree, prompt, role, task_id, attempt=1):
        prompt_path = self.root / f"{task_id}-{role}-{attempt}.prompt.txt"
        prompt_path.write_text(prompt)
        env = dict(os.environ)
        env["PATH"] = str(self.python.parent) + os.pathsep + env.get("PATH", "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
        argv = [self.config["codex"], "exec", "--ignore-user-config", "--ephemeral", "--json",
                "--model", self.config["model"], "-c", f'model_reasoning_effort="{self.config["reasoning_effort"]}"',
                "-c", "features.multi_agent=false", "--sandbox", "workspace-write", "-C", worktree, "-"]
        def snapshot():
            paths = self.git("ls-files", "--cached", "--others", "--exclude-standard", cwd=worktree).splitlines()
            return {p: digest(worktree / p) for p in paths if (worktree / p).is_file()}
        before = snapshot()
        self.event("agent_started", role=role, task_id=task_id, attempt=attempt)
        result = self.command(argv, self.root, env=env, stdin=prompt,
                              timeout=self.config["agent_timeout_seconds"])
        self.event("agent_finished", role=role, task_id=task_id, attempt=attempt, **result)
        after = snapshot()
        self.event("agent_edits", role=role, task_id=task_id, attempt=attempt,
                   changed_files=sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p)))
        return result["returncode"] == 0 and not result["timed_out"]

    def task_prompt(self, task):
        return (f"{task['id']}: {task['title']}\nAdd a computed @property "
                f"{task['model']}.{task['field']} and expose it as '{task['field']}' "
                f"in app.api.{task['serializer']} for {task['endpoint']}.\n"
                f"Contract: {task['requirements']}\n"
                "Add focused Python tests and document the field and examples in README.md. "
                "Preserve all existing fields, routes, authentication, and behavior. "
                "No schema migrations or new dependencies are needed.\n")

    def common_prompt(self):
        return ("Work autonomously on this single assignment. Do not spawn subagents, use other models, "
                "or resume other sessions. Do not commit, merge, rebase, or switch branches; the harness "
                "owns git staging and commits. Edit only this worktree. Use python -m pytest -q "
                "for the existing suite; python and installed dependencies are on PATH. "
                "The repository's statement that it has no tests is outdated. "
                "Never remove or weaken tests or other tasks' behavior.\n")

    def implement(self, task, base):
        tid = task["id"]
        tree = self.root / "worktrees" / tid
        self.git("worktree", "add", "-b", tid, str(tree), base)
        self.results[tid]["base"] = base
        if not self.agent(tree, self.common_prompt() + self.task_prompt(task), "implementation", tid):
            return False
        if not self.validate(tree, [tid], f"{tid}-implementation"):
            return False
        self.git("add", "-A", cwd=tree)
        if not self.git("diff", "--cached", "--name-only", cwd=tree):
            self.event("empty_implementation", task_id=tid)
            return False
        self.git("commit", "-m", f"{tid}: {task['title']}", cwd=tree)
        self.results[tid]["implementation_commit"] = self.git("rev-parse", "HEAD", cwd=tree)
        self.results[tid]["changed_files"] = self.git("diff", "--name-only", base, "HEAD", cwd=tree).splitlines()
        self.git("diff", "--binary", base, "HEAD", cwd=tree)  # persisted command output is the patch
        return True

    def integrate(self, task):
        tid = task["id"]
        tree = self.root / "worktrees" / f"{tid}-integration"
        parent = self.git("rev-parse", "main")
        self.git("worktree", "add", "-b", f"integrate-{tid}", str(tree), parent)
        merge = self.command(["git", "merge", "--no-commit", "--no-ff", tid], tree)
        conflicts = self.git("diff", "--name-only", "--diff-filter=U", cwd=tree).splitlines()
        self.results[tid]["conflict_files"] = conflicts
        self.event("merge_attempt", task_id=tid, parent=parent, conflict_files=conflicts, **merge)
        if merge["returncode"] and not conflicts:
            return False
        last_failure = "First attempt."
        for attempt in range(1, self.config["integration_attempts"] + 1):
            self.results[tid]["integration_attempts"] = attempt
            # Even clean merges get a fresh integration invocation, inside the same slot budget.
            prompt = self.common_prompt() + "Integrate the task already merged into this worktree. "
            prompt += "Resolve any conflict markers, preserve all earlier integrated contracts, and repair failures.\n"
            prompt += "Current assignment:\n" + self.task_prompt(task)
            prompt += "Earlier integrated assignments:\n"
            prompt += "\n".join(self.task_prompt(self.task_map[t]) for t in self.integrated)
            prompt += "\nOn retry, the previous harness validation output follows:\n"
            prompt += last_failure
            agent_ok = self.agent(tree, prompt, "integration", tid, attempt)
            unresolved = self.git("diff", "--name-only", "--diff-filter=U", cwd=tree)
            # Resolved conflict files remain unmerged until staged by the harness.
            self.git("add", "-A", cwd=tree)
            diff_check = self.command(["git", "diff", "--cached", "--check"], tree)
            valid = self.validate(tree, self.integrated + [tid], f"{tid}-integration-{attempt}")
            if agent_ok and valid and diff_check["returncode"] == 0:
                self.git("commit", "-m", f"Integrate {tid}", cwd=tree)
                head = self.git("rev-parse", "HEAD", cwd=tree)
                self.git("diff", "--binary", tid, head, cwd=tree)
                self.results[tid]["integration_commit"] = head
                self.results[tid]["integration_changed_files"] = self.git(
                    "diff", "--name-only", tid, head, cwd=tree).splitlines()
                self.git("reset", "--hard", head)
                self.integrated.append(tid)
                self.event("task_integrated", task_id=tid, commit=head)
                return True
            self.event("integration_rejected", task_id=tid, attempt=attempt,
                       agent_ok=agent_ok, validation_ok=valid, initially_unmerged=unresolved.splitlines())
            # Full logs remain on disk; only current validation failures enter a fresh retry context.
            last = self.validations[f"{tid}-integration-{attempt}"]
            last_failure = (Path(last["stdout"]).read_text()[-24000:] + Path(last["stderr"]).read_text()[-8000:]
                            + Path(diff_check["stdout"]).read_text()[-8000:])
        return False

    def execute_tasks(self):
        pending = list(self.tasks)
        ready = []
        jobs = {}
        integration_running = False
        self.work_started = time.monotonic()
        self.event("work_started")
        with futures.ThreadPoolExecutor(max_workers=self.agents) as pool:
            while pending or ready or jobs:
                while len(jobs) < self.agents:
                    if ready and not integration_running:
                        task = ready.pop(0)
                        integration_running = True
                        role, fn, args = "integration", self.integrate, (task,)
                    elif pending:
                        task = pending.pop(0)
                        base = self.git("rev-parse", "main")
                        role, fn, args = "implementation", self.implement, (task, base)
                    else:
                        break
                    self.results[task["id"]]["status"] = role
                    self.results[task["id"]][f"{role}_dispatched_seconds"] = time.monotonic() - self.origin
                    future = pool.submit(fn, *args)
                    jobs[future] = (role, task)
                    self.event("job_dispatched", role=role, task_id=task["id"], active_jobs=len(jobs))
                done, _ = futures.wait(jobs, return_when=futures.FIRST_COMPLETED)
                for future in sorted(done, key=lambda f: jobs[f][1]["id"]):
                    role, task = jobs.pop(future)
                    try:
                        ok = future.result()
                    except Exception:
                        ok = False
                        self.event("job_exception", role=role, task_id=task["id"], traceback=traceback.format_exc())
                    if role == "integration":
                        integration_running = False
                    if ok and role == "implementation":
                        ready.append(task)
                        status = "awaiting_integration"
                    else:
                        status = "passed" if ok else f"{role}_failed"
                    self.results[task["id"]]["status"] = status
                    self.results[task["id"]][f"{role}_finished_seconds"] = time.monotonic() - self.origin
                    self.event("job_finished", role=role, task_id=task["id"], ok=ok)
        final_ok = self.validate(self.repo, list(self.task_map), "final-all-ten")
        self.summary["work_seconds"] = time.monotonic() - self.work_started
        self.summary["status"] = "passed" if final_ok and len(self.integrated) == 10 else "failed"
        self.summary["final_commit"] = self.git("rev-parse", "HEAD")

    def run(self):
        try:
            self.setup()
            self.execute_tasks()
        except KeyboardInterrupt:
            self.summary["status"] = "interrupted"
            self.event("run_interrupted")
            raise
        except Exception:
            self.summary["status"] = "failed"
            self.summary["error"] = traceback.format_exc()
            self.event("run_exception", traceback=self.summary["error"])
        finally:
            self.summary["total_seconds"] = time.monotonic() - self.origin
            self.summary["integrated_tasks"] = list(self.integrated)
            self.event("run_finished", status=self.summary["status"])
            (self.root / "summary.json").write_text(json.dumps(self.summary, indent=2))
        return self.summary
