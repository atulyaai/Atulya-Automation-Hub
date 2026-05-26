import sys
import os
from pathlib import Path

import click

from atulya_automation_hub import __version__
from atulya_automation_hub.core import (
    Config, WorkflowEngine, Scheduler, UpdateChecker, Dashboard,
    WORKFLOWS_DIR, LOG_DIR,
)
from atulya_automation_hub.utils import (
    discover_tools, run_tool_command, check_tool_version,
    create_workflow_from_template, get_workflow_templates,
    build_tools_table,
)
from atulya_automation_hub.core import ensure_config_dirs


@click.group()
@click.version_option(__version__)
@click.pass_context
def cli(ctx):
    ctx.ensure_object(dict)
    ensure_config_dirs()
    ctx.obj["config"] = Config()
    ctx.obj["engine"] = WorkflowEngine(ctx.obj["config"])


@cli.command()
@click.pass_context
def dashboard(ctx):
    engine = ctx.obj["engine"]
    config = ctx.obj["config"]
    dash = Dashboard(config, engine)
    dash.run()


@cli.group()
def workflow():
    pass


@workflow.command()
@click.argument("file", type=click.Path(exists=True))
@click.pass_context
def run(ctx, file):
    engine = ctx.obj["engine"]
    workflow_data = engine.load_workflow(file)
    click.echo(f"Running workflow: [bold]{workflow_data.get('name', 'Unnamed')}[/]")
    result = engine.run_workflow(workflow_data, file)
    click.echo(f"\nStatus: {'[green]Completed[/]' if result.status == 'completed' else '[red]Failed[/]'}")
    click.echo(f"Duration: {result.end_time - result.start_time:.1f}s" if result.end_time and result.start_time else "")
    sys.exit(0 if result.status == "completed" else 1)


@workflow.command(name="list")
@click.pass_context
def list_workflows(ctx):
    engine = ctx.obj["engine"]
    workflows = engine.list_workflows()
    if not workflows:
        click.echo("No workflow files found in ~/.atulya/workflows/")
        return
    from rich.console import Console
    from rich.table import Table
    console = Console()
    table = Table(title="Workflows")
    table.add_column("Name", style="cyan")
    table.add_column("Schedule", style="green")
    table.add_column("Steps", style="yellow")
    for wf in workflows:
        table.add_row(wf["name"], wf["schedule"] or "manual", str(wf["steps"]))
    console.print(table)


@workflow.command()
@click.argument("name", type=click.Choice(list(get_workflow_templates().keys())))
@click.argument("output", default=str(WORKFLOWS_DIR / "new_workflow.yaml"))
@click.pass_context
def create(ctx, name, output):
    path = create_workflow_from_template(name, output)
    click.echo(f"Created workflow from template '{name}' at: {path}")
    click.echo(f"Edit it and run: atulya workflow run {path}")


@workflow.command()
@click.argument("cron_expression")
@click.argument("workflow_file", type=click.Path(exists=True))
@click.pass_context
def schedule(ctx, cron_expression, workflow_file):
    config = ctx.obj["config"]
    engine = ctx.obj["engine"]
    scheduler = Scheduler(config, engine)
    wf_name = Path(workflow_file).stem
    scheduler.add_schedule(wf_name, cron_expression, workflow_file)
    click.echo(f"Scheduled '{wf_name}' with cron: {cron_expression}")
    scheduler.start()


@workflow.command()
@click.option("--limit", default=20, help="Number of log entries")
@click.pass_context
def logs(ctx, limit):
    engine = ctx.obj["engine"]
    history = engine.get_run_history(limit)
    if not history:
        click.echo("No workflow execution history found.")
        return
    from rich.console import Console
    from rich.table import Table
    console = Console()
    table = Table(title="Workflow Execution History")
    table.add_column("Workflow", style="cyan")
    table.add_column("Started", style="green")
    table.add_column("Duration", style="yellow")
    table.add_column("Status", style="bold")
    table.add_column("Steps")
    for h in history:
        status_style = "green" if h["status"] == "completed" else "red"
        table.add_row(
            h["workflow"][:25],
            h["started"][:19],
            h["duration"],
            f"[{status_style}]{h['status']}[/]",
            f"{h['steps_ok']}/{h['steps_total']}",
        )
    console.print(table)


@cli.group()
def tools():
    pass


@tools.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_tools(as_json):
    installed = discover_tools()
    if as_json:
        import json
        click.echo(json.dumps(installed, indent=2, default=str))
        return
    from rich.console import Console
    console = Console()
    table = build_tools_table(installed)
    console.print(table)


@tools.command()
@click.argument("tool_name")
def info(tool_name):
    from atulya_automation_hub.utils import ATULYA_TOOLS
    installed = discover_tools()
    info = ATULYA_TOOLS.get(tool_name)
    pkg_info = installed.get(tool_name, {})
    if not info:
        click.echo(f"Unknown tool: {tool_name}")
        sys.exit(1)
    click.echo(f"Package: {tool_name}")
    click.echo(f"Name: {info['name']}")
    click.echo(f"Version: {pkg_info.get('version', 'N/A')}")
    click.echo(f"Installed: {'Yes' if pkg_info.get('installed') else 'No'}")
    click.echo(f"Commands: {', '.join(info['commands'])}")


@tools.command()
def check():
    installed = discover_tools()
    from rich.console import Console
    console = Console()
    table = build_tools_table(installed)
    console.print(table)
    installed_count = sum(1 for v in installed.values() if v.get("installed"))
    console.print(f"\n[bold]{installed_count}[/] of {len(installed)} tools installed")


@cli.group()
def config():
    pass


@config.command()
@click.pass_context
def show(ctx):
    cfg = ctx.obj["config"]
    import yaml
    click.echo(yaml.dump(cfg.data, default_flow_style=False))


@config.command(name="set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def set_config(ctx, key, value):
    cfg = ctx.obj["config"]
    cfg.set(key, value)
    click.echo(f"Set {key} = {value}")


@config.command(name="init")
@click.pass_context
def init_cmd(ctx):
    cfg = ctx.obj["config"]
    result = cfg.init()
    click.echo("Hub configuration initialized:")
    import yaml
    click.echo(yaml.dump(result, default_flow_style=False))


@cli.group()
def update():
    pass


@update.command()
def check():
    checker = UpdateChecker()
    with click.progressbar(
        ["atulya-automation-hub", "atulya-office", "atulya-erp", "atulya-gst",
         "atulya-sap", "atulya-hr", "atulya-data-scrubber", "atulya-file-converter",
         "atulya-launch"],
        label="Checking for updates",
    ) as packages:
        results = []
        for pkg in packages:
            result = checker.check_tool(pkg)
            if result:
                results.append(result)
    from rich.console import Console
    from rich.table import Table
    console = Console()
    table = Table(title="Update Status")
    table.add_column("Package", style="cyan")
    table.add_column("Current", style="yellow")
    table.add_column("Latest", style="green")
    table.add_column("Status", style="bold")
    updates = 0
    for r in results:
        if r["update_available"]:
            status = "[yellow]Update Available[/]"
            updates += 1
        else:
            status = "[green]Up to date[/]"
        table.add_row(r["package"], r["current"] or "N/A", r["latest"], status)
    console.print(table)
    if updates:
        console.print(f"\n[yellow]{updates} update(s) available. Run: atulya update all[/]")


@update.command(name="all")
def update_all_cmd():
    checker = UpdateChecker()
    console = Console()
    console.print("[bold]Updating all Atulya tools...[/]")
    results = checker.update_all()
    for r in results:
        status = "[green]✓[/]" if r["success"] else "[red]✗[/]"
        console.print(f"  {status} {r['package']}")
    console.print("\n[bold green]Update complete![/]")


@cli.command()
@click.argument("tool")
@click.argument("args", nargs=-1, required=True)
def run(tool, args):
    parts = tool.split()
    tool_name = parts[0]
    cmd_args = list(parts[1:]) + list(args)
    click.echo(f"Running: {tool_name} {' '.join(cmd_args)}")
    proc = run_tool_command(tool_name, cmd_args)
    click.echo(proc.stdout)
    if proc.stderr:
        click.echo(proc.stderr, err=True)
    sys.exit(proc.returncode)


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
