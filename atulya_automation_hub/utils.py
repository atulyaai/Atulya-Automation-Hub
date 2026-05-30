import importlib.metadata
import importlib.util
import subprocess
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

from rich.table import Table
from rich.console import Console

ATULYA_TOOLS = {
    "atulya-office": {"name": "Office Automation", "commands": ["excel", "word", "outlook", "pdf"]},
    "atulya-erp": {"name": "ERP Integration", "commands": ["sync", "inventory", "orders", "invoice"]},
    "atulya-gst": {"name": "GST Filing", "commands": ["gstr1", "gstr3b", "itc", "eway"]},
    "atulya-sap": {"name": "SAP Connector", "commands": ["extract", "upload", "sync", "report"]},
    "atulya-hr": {"name": "HR Management", "commands": ["payroll", "attendance", "leaves", "reports"]},
    "atulya-data-scrubber": {"name": "Data Scrubber", "commands": ["clean", "dedup", "validate", "transform"]},
    "atulya-file-converter": {"name": "File Converter", "commands": ["csv", "json", "xml", "pdf", "image"]},
    "atulya-launch": {"name": "App Launcher", "commands": ["open", "run", "schedule", "shortcut"]},
}

SAMPLE_WORKFLOWS = {
    "gst-monthly": {
        "name": "Monthly GST Filing",
        "description": "Generate and file GSTR-1, GSTR-3B, reconcile ITC",
        "schedule": "0 0 1 * *",
        "steps": [
            {"name": "Generate GSTR-1", "tool": "gst", "command": "gstr1 generate --sales last-month-sales.xlsx"},
            {"name": "File GSTR-3B", "tool": "gst", "command": "gstr3b file --period last-month"},
            {"name": "Reconcile ITC", "tool": "gst", "command": "itc reconcile --purchase purchase-register.xlsx"},
            {"name": "Send summary", "tool": "office", "command": "outlook send --to accounts@company.com --subject 'GST Monthly Summary'"},
        ],
    },
    "payroll-monthly": {
        "name": "Monthly Payroll Processing",
        "description": "Process attendance, calculate payroll, generate payslips",
        "schedule": "0 6 25 * *",
        "steps": [
            {"name": "Fetch attendance", "tool": "hr", "command": "attendance pull --month last-month"},
            {"name": "Process payroll", "tool": "hr", "command": "payroll process --month last-month"},
            {"name": "Generate payslips", "tool": "hr", "command": "payroll payslips --month last-month"},
            {"name": "Email payslips", "tool": "office", "command": "outlook send-bulk --template payslip --recipients employees.csv"},
        ],
    },
    "office-backup": {
        "name": "Office Data Backup",
        "description": "Backup Outlook emails, Excel files, and documents",
        "schedule": "0 2 * * 0",
        "steps": [
            {"name": "Backup emails", "tool": "office", "command": "outlook export --folder inbox --format pst"},
            {"name": "Archive Excel files", "tool": "file-converter", "command": "csv archive --source C:\data\excel --dest D:\backups"},
            {"name": "Compress documents", "tool": "file-converter", "command": "pdf compress --source C:\data\docs --quality high"},
        ],
    },
}


def discover_tools() -> Dict[str, Dict]:
    installed = {}
    for pkg_name, info in ATULYA_TOOLS.items():
        spec = importlib.util.find_spec(pkg_name.replace("-", "_"))
        if spec is not None:
            try:
                ver = importlib.metadata.version(pkg_name)
            except importlib.metadata.PackageNotFoundError:
                ver = "unknown"
            installed[pkg_name] = {**info, "version": ver, "installed": True}
        else:
            installed[pkg_name] = {**info, "version": None, "installed": False}
    return installed


def run_tool_command(tool_name: str, args: List[str]) -> subprocess.CompletedProcess:
    cmd = [tool_name] + args
    return subprocess.run(cmd, capture_output=True, text=True)


def check_tool_version(tool_name: str) -> Optional[str]:
    try:
        return importlib.metadata.version(tool_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def parse_cron(expression: str) -> Dict[str, List[int]]:
    fields = expression.strip().split()
    if len(fields) != 5:
        raise ValueError("Cron expression must have 5 fields")
    names = ["minute", "hour", "day_of_month", "month", "day_of_week"]
    upper_bounds = {"minute": 59, "hour": 23, "day_of_month": 31, "month": 12, "day_of_week": 6}
    result = {}
    for name, field in zip(names, fields):
        if field == "*":
            result[name] = []
        else:
            parts = []
            for part in field.split(","):
                if "/" in part:
                    base, step = part.split("/")
                    if base == "*":
                        base_val = 0
                    else:
                        base_val = int(base)
                    parts.extend(range(base_val, upper_bounds[name] + 1, int(step)))
                elif "-" in part:
                    start, end = part.split("-")
                    parts.extend(range(int(start), int(end) + 1))
                else:
                    parts.append(int(part))
            result[name] = parts
    return result


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.0f}m {seconds % 60:.0f}s"
    elif seconds < 86400:
        h = seconds / 3600
        return f"{h:.0f}h {(seconds % 3600) / 60:.0f}m"
    else:
        d = seconds / 86400
        return f"{d:.0f}d {(seconds % 86400) / 3600:.0f}h"


def get_workflow_templates() -> Dict:
    return SAMPLE_WORKFLOWS


def create_workflow_from_template(name: str, output_path: str) -> str:
    import yaml
    template = SAMPLE_WORKFLOWS.get(name)
    if template is None:
        raise ValueError(f"Unknown template: {name}. Available: {list(SAMPLE_WORKFLOWS.keys())}")
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(template, f, default_flow_style=False, sort_keys=False)
    return str(path)


def build_tools_table(installed: Dict[str, Dict]) -> Table:
    table = Table(title="Atulya Tools")
    table.add_column("Package", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Version", style="yellow")
    table.add_column("Status", style="bold")
    for pkg, info in installed.items():
        status = "[green]Installed[/]" if info["installed"] else "[red]Not Found[/]"
        ver = info["version"] or "[red]N/A[/]"
        table.add_row(pkg, info["name"], str(ver), status)
    return table
