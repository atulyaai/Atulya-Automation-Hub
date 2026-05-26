import os
import subprocess
import sys
import json
import time
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any, Callable

import yaml
import requests
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich import box

from atulya_automation_hub.utils import (
    discover_tools, run_tool_command, format_duration, parse_cron,
    check_tool_version, build_tools_table,
)

console = Console()

CONFIG_DIR = Path.home() / ".atulya"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
WORKFLOWS_DIR = CONFIG_DIR / "workflows"
SCHEDULE_FILE = CONFIG_DIR / "schedule.yaml"
LOG_DIR = CONFIG_DIR / "logs"


def ensure_config_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


class Config:
    def __init__(self):
        ensure_config_dirs()
        self._data = self._load()

    def _load(self) -> dict:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                return yaml.safe_load(f) or {}
        return {}

    def _save(self):
        with open(CONFIG_FILE, "w") as f:
            yaml.dump(self._data, f, default_flow_style=False, sort_keys=False)

    def get(self, key: str, default=None):
        keys = key.split(".")
        val = self._data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
        return val if val is not None else default

    def set(self, key: str, value):
        keys = key.split(".")
        target = self._data
        for k in keys[:-1]:
            target = target.setdefault(k, {})
        target[keys[-1]] = value
        self._save()

    def init(self):
        tools = discover_tools()
        self._data = {
            "hub": {
                "version": __import__("atulya_automation_hub").__version__,
                "auto_update": True,
                "log_level": "INFO",
            },
            "tools": {
                pkg: {
                    "name": info["name"],
                    "version": info.get("version"),
                    "installed": info["installed"],
                    "enabled": info["installed"],
                }
                for pkg, info in tools.items()
            },
            "paths": {
                "workflows": str(WORKFLOWS_DIR),
                "logs": str(LOG_DIR),
            },
        }
        self._save()
        return self._data

    def show(self) -> dict:
        return self._data

    @property
    def data(self):
        return self._data


class WorkflowEngine:
    def __init__(self, config: Config):
        self.config = config
        self._running = False
        self._current_run: Optional[WorkflowRun] = None

    def load_workflow(self, path: str) -> dict:
        with open(path) as f:
            return yaml.safe_load(f)

    def run_workflow(self, workflow: dict, workflow_path: str = "") -> "WorkflowRun":
        run = WorkflowRun(workflow, workflow_path)
        self._current_run = run
        run.start()
        self._current_run = None
        return run

    def list_workflows(self) -> List[Dict]:
        if not WORKFLOWS_DIR.exists():
            return []
        workflows = []
        for f in sorted(WORKFLOWS_DIR.glob("*.yaml")):
            try:
                wf = self.load_workflow(str(f))
                workflows.append({
                    "name": wf.get("name", f.stem),
                    "file": f.name,
                    "path": str(f),
                    "schedule": wf.get("schedule", ""),
                    "steps": len(wf.get("steps", [])),
                })
            except Exception:
                pass
        return workflows

    def get_run_history(self, limit: int = 20) -> List[Dict]:
        if not LOG_DIR.exists():
            return []
        logs = sorted(LOG_DIR.glob("*.log"), reverse=True)[:limit]
        history = []
        for log_file in logs:
            try:
                with open(log_file) as f:
                    lines = f.readlines()
                if lines:
                    first = json.loads(lines[0])
                    last = json.loads(lines[-1]) if len(lines) > 1 else first
                    duration = last.get("timestamp", 0) - first.get("timestamp", 0)
                    history.append({
                        "workflow": first.get("workflow", "unknown"),
                        "file": log_file.name,
                        "status": last.get("status", "unknown"),
                        "started": datetime.fromtimestamp(first.get("timestamp", 0)).isoformat() if first.get("timestamp") else "N/A",
                        "duration": format_duration(duration),
                        "steps_total": first.get("steps_total", 0),
                        "steps_ok": first.get("steps_ok", 0),
                    })
            except Exception:
                pass
        return history


class WorkflowRun:
    def __init__(self, workflow: dict, workflow_path: str = ""):
        self.workflow = workflow
        self.name = workflow.get("name", "Unnamed")
        self.steps = workflow.get("steps", [])
        self.results: List[Dict] = []
        self.status = "pending"
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.log_path = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{self.name.replace(' ', '_')}.log"
        self.workflow_path = workflow_path

    def start(self):
        self.status = "running"
        self.start_time = time.time()
        self._log_event({"event": "start", "workflow": self.name, "timestamp": self.start_time, "steps_total": len(self.steps)})
        for i, step in enumerate(self.steps):
            step_result = self._execute_step(step, i)
            self.results.append(step_result)
            if step_result["status"] == "error":
                self.status = "failed"
                self.end_time = time.time()
                self._log_event({"event": "end", "status": "failed", "timestamp": self.end_time, "steps_ok": sum(1 for r in self.results if r["status"] == "ok")})
                return step_result
        self.status = "completed"
        self.end_time = time.time()
        self._log_event({"event": "end", "status": "completed", "timestamp": self.end_time, "steps_ok": len(self.results)})
        return step_result

    def _execute_step(self, step: dict, index: int) -> Dict:
        step_name = step.get("name", f"Step {index + 1}")
        step_type = step.get("type", "tool")
        console.print(f"  [cyan]|--[/] Running step [bold]{step_name}[/]...")
        start = time.time()
        try:
            if step_type == "tool":
                result = self._exec_tool_step(step)
            elif step_type == "python":
                result = self._exec_python_step(step)
            elif step_type == "subprocess":
                result = self._exec_subprocess_step(step)
            else:
                result = self._exec_tool_step(step)
            elapsed = time.time() - start
            if result["returncode"] == 0:
                console.print(f"  [green]  |-- OK {step_name}[/] ({format_duration(elapsed)})")
                result.update({"name": step_name, "status": "ok", "duration": elapsed, "index": index})
            else:
                console.print(f"  [red]  |-- FAIL {step_name}[/] ({format_duration(elapsed)})")
                result.update({"name": step_name, "status": "error", "duration": elapsed, "index": index})
            self._log_event({"event": "step", "step": step_name, "status": result["status"], "timestamp": time.time(), "duration": elapsed, "index": index})
            return result
        except Exception as e:
            elapsed = time.time() - start
            console.print(f"  [red]  |-- FAIL {step_name}: {e}[/]")
            err = {"name": step_name, "status": "error", "error": str(e), "duration": elapsed, "index": index, "returncode": -1}
            self._log_event({"event": "step", "step": step_name, "status": "error", "error": str(e), "timestamp": time.time(), "duration": elapsed})
            return err

    def _exec_tool_step(self, step: dict) -> Dict:
        tool = step.get("tool", "")
        command = step.get("command", "")
        if not tool or not command:
            return {"returncode": -1, "stdout": "", "stderr": "Missing tool or command", "status": "error"}
        args = command.split()
        proc = run_tool_command(tool, args)
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}

    def _exec_python_step(self, step: dict) -> Dict:
        code = step.get("code", "")
        if not code:
            return {"returncode": -1, "stdout": "", "stderr": "No inline Python code provided", "status": "error"}
        local_vars = {"step": step, "workflow": self.workflow, "results": self.results}
        try:
            exec(code, {}, local_vars)
            return {"returncode": 0, "stdout": str(local_vars.get("result", "")), "stderr": ""}
        except Exception as e:
            return {"returncode": -1, "stdout": "", "stderr": str(e)}

    def _exec_subprocess_step(self, step: dict) -> Dict:
        cmd = step.get("command", "")
        if not cmd:
            return {"returncode": -1, "stdout": "", "stderr": "No command provided", "status": "error"}
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}

    def _log_event(self, event: dict):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a") as f:
            f.write(json.dumps(event) + "\n")


class Scheduler:
    def __init__(self, config: Config, engine: WorkflowEngine):
        self.config = config
        self.engine = engine
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._schedules = self._load_schedules()

    def _load_schedules(self) -> List[Dict]:
        if SCHEDULE_FILE.exists():
            with open(SCHEDULE_FILE) as f:
                return yaml.safe_load(f) or []
        return []

    def _save_schedules(self):
        with open(SCHEDULE_FILE, "w") as f:
            yaml.dump(self._schedules, f, default_flow_style=False, sort_keys=False)

    def add_schedule(self, workflow_name: str, cron_expr: str, workflow_path: str):
        entry = {
            "workflow": workflow_name,
            "cron": cron_expr,
            "path": workflow_path,
            "enabled": True,
            "created": datetime.now().isoformat(),
        }
        self._schedules.append(entry)
        self._save_schedules()

    def list_schedules(self) -> List[Dict]:
        return self._schedules

    def remove_schedule(self, index: int):
        if 0 <= index < len(self._schedules):
            self._schedules.pop(index)
            self._save_schedules()

    def _check_and_run(self):
        now = datetime.now()
        for entry in self._schedules:
            if not entry.get("enabled"):
                continue
            try:
                cron_parts = parse_cron(entry["cron"])
                should_run = True
                if cron_parts["minute"] and now.minute not in cron_parts["minute"]:
                    should_run = False
                if cron_parts["hour"] and now.hour not in cron_parts["hour"]:
                    should_run = False
                if cron_parts["day_of_month"] and now.day not in cron_parts["day_of_month"]:
                    should_run = False
                if cron_parts["month"] and now.month not in cron_parts["month"]:
                    should_run = False
                if cron_parts["day_of_week"] and now.weekday() not in cron_parts["day_of_week"]:
                    should_run = False
                if should_run:
                    wf_path = entry.get("path")
                    if wf_path and Path(wf_path).exists():
                        workflow = self.engine.load_workflow(wf_path)
                        console.print(f"[yellow]Scheduler[/] Running scheduled workflow: {entry['workflow']}")
                        self.engine.run_workflow(workflow, wf_path)
            except Exception as e:
                console.print(f"[red]Scheduler error for {entry['workflow']}: {e}[/]")

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        while self._running:
            self._check_and_run()
            time.sleep(30)


class UpdateChecker:
    def __init__(self):
        self.pypi_base = "https://pypi.org/pypi"

    def check_tool(self, package_name: str) -> Optional[Dict]:
        try:
            resp = requests.get(f"{self.pypi_base}/{package_name}/json", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                latest = data["info"]["version"]
                current = check_tool_version(package_name)
                return {
                    "package": package_name,
                    "current": current,
                    "latest": latest,
                    "update_available": current != latest if current else False,
                }
        except requests.RequestException:
            pass
        return None

    def check_all(self) -> List[Dict]:
        results = []
        for pkg in [
            "atulya-office", "atulya-erp", "atulya-gst", "atulya-sap",
            "atulya-hr", "atulya-data-scrubber", "atulya-file-converter",
            "atulya-launch", "atulya-automation-hub",
        ]:
            result = self.check_tool(pkg)
            if result:
                results.append(result)
        return results

    def update_all(self) -> List[Dict]:
        results = []
        for pkg in [
            "atulya-office", "atulya-erp", "atulya-gst", "atulya-sap",
            "atulya-hr", "atulya-data-scrubber", "atulya-file-converter",
            "atulya-launch", "atulya-automation-hub",
        ]:
            try:
                console.print(f"[cyan]Updating {pkg}...[/]")
                proc = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--upgrade", pkg],
                    capture_output=True, text=True,
                )
                results.append({
                    "package": pkg,
                    "success": proc.returncode == 0,
                    "output": proc.stdout[-200:] if proc.stdout else "",
                    "error": proc.stderr[-200:] if proc.stderr else "",
                })
            except Exception as e:
                results.append({"package": pkg, "success": False, "error": str(e)})
        return results


class Dashboard:
    def __init__(self, config: Config, engine: WorkflowEngine):
        self.config = config
        self.engine = engine
        self._refresh_interval = 5
        self._running = False

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )
        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right"),
        )
        layout["left"].split_column(
            Layout(name="system_info"),
            Layout(name="tools_status"),
        )
        layout["right"].split_column(
            Layout(name="recent_workflows"),
            Layout(name="scheduled_tasks"),
        )
        return layout

    def _render_header(self) -> Panel:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = Text(f" Atulya Automation Hub  v{__import__('atulya_automation_hub').__version__}", style="bold cyan")
        text.append(f"  |  {now}", style="yellow")
        return Panel(text, box=box.HEAVY_EDGE)

    def _render_system_info(self) -> Panel:
        import platform
        table = Table(box=box.SIMPLE, show_header=False)
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("OS", f"{platform.system()} {platform.release()}")
        table.add_row("Python", sys.version.split()[0])
        table.add_row("Host", platform.node())
        table.add_row("Config", str(CONFIG_FILE))
        table.add_row("Workflows", str(WORKFLOWS_DIR))
        return Panel(table, title="System Info", box=box.ROUNDED)

    def _render_tools_status(self) -> Panel:
        tools = discover_tools()
        table = build_tools_table(tools)
        return Panel(table, title="Tools Status", box=box.ROUNDED)

    def _render_recent_workflows(self) -> Panel:
        history = self.engine.get_run_history(limit=10)
        table = Table(box=box.SIMPLE, show_header=True)
        table.add_column("Workflow", style="cyan")
        table.add_column("Status", style="bold")
        table.add_column("Duration", style="yellow")
        table.add_column("Steps")
        for h in history:
            status_style = "green" if h["status"] == "completed" else "red" if h["status"] == "failed" else "yellow"
            table.add_row(
                h["workflow"][:30],
                f"[{status_style}]{h['status']}[/]",
                h["duration"],
                f"{h['steps_ok']}/{h['steps_total']}",
            )
        return Panel(table, title="Recent Workflows", box=box.ROUNDED)

    def _render_scheduled_tasks(self) -> Panel:
        scheduler = Scheduler(self.config, self.engine)
        schedules = scheduler.list_schedules()
        table = Table(box=box.SIMPLE, show_header=True)
        table.add_column("Workflow", style="cyan")
        table.add_column("Schedule", style="green")
        table.add_column("Status", style="bold")
        for s in schedules:
            status = "[green]Active[/]" if s.get("enabled") else "[red]Disabled[/]"
            table.add_row(s.get("workflow", "N/A"), s.get("cron", ""), status)
        if not schedules:
            table.add_row("[dim]No scheduled tasks[/]", "", "")
        return Panel(table, title="Scheduled Tasks", box=box.ROUNDED)

    def _render_footer(self) -> Panel:
        text = Text(" [F5] Refresh  [Q] Quit  [W] Run Workflow  [C] Config", style="bold white on blue")
        return Panel(text, box=box.HEAVY_EDGE)

    def run(self):
        self._running = True
        layout = self._build_layout()
        with Live(layout, refresh_per_second=1, screen=True) as live:
            while self._running:
                try:
                    layout["header"].update(self._render_header())
                    layout["system_info"].update(self._render_system_info())
                    layout["tools_status"].update(self._render_tools_status())
                    layout["recent_workflows"].update(self._render_recent_workflows())
                    layout["scheduled_tasks"].update(self._render_scheduled_tasks())
                    layout["footer"].update(self._render_footer())
                    time.sleep(self._refresh_interval)
                except KeyboardInterrupt:
                    break

    def stop(self):
        self._running = False
