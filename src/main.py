#!/usr/bin/env python3
"""
CAR DATA COLLECTOR BOT - Entry Point
Scrapes English automotive forums and technical sites, deduplicates content,
and generates Q/A pairs for LLM fine-tuning datasets.

Usage:
    python -m src.main run              # Run continuous 7/24 pipeline
    python -m src.main single-round     # Run one pipeline round
    python -m src.main export-qa        # Export Q/A pairs to file
    python -m src.main stats            # Show statistics
    python -m src.main health           # System health check
"""

import asyncio
import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.pipeline import PipelineOrchestrator
from src.utils.helpers import load_config
from src.utils.logger import setup_logging

console = Console()


def get_config(config_path: str) -> dict:
    try:
        return load_config(config_path)
    except FileNotFoundError:
        console.print(f"[red]Config file not found: {config_path}[/red]")
        sys.exit(1)


@click.group()
@click.option("--config", "-c", default="config/settings.yaml", help="Path to config file")
@click.pass_context
def cli(ctx, config):
    """Car Data Collector Bot - English automotive data collection pipeline"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command()
@click.pass_context
def run(ctx):
    """Run the continuous 7/24 pipeline"""
    config = get_config(ctx.obj["config_path"])
    log_config = config.get("general", {})
    setup_logging(log_config.get("log_level", "INFO"), log_config.get("log_dir", "logs"))

    console.print(Panel.fit(
        "[bold green]Car Data Collector Bot[/bold green]\n"
        "Starting continuous pipeline...\n"
        f"Sources: {len([s for s in config.get('sources', []) if s.get('enabled')])}\n"
        f"Round delay: {config.get('general', {}).get('round_delay_seconds', 3600)}s",
        title="Pipeline Starting"
    ))

    async def _run():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        await orchestrator.run_continuous()

    asyncio.run(_run())


@cli.command("single-round")
@click.pass_context
def single_round(ctx):
    """Run a single pipeline round"""
    config = get_config(ctx.obj["config_path"])
    log_config = config.get("general", {})
    setup_logging(log_config.get("log_level", "INFO"), log_config.get("log_dir", "logs"))

    console.print("[bold]Running single pipeline round...[/bold]")

    async def _run():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        stats = await orchestrator.run_single_round()

        table = Table(title="Round Results")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Round Number", str(stats.round_number))
        table.add_row("Items Scraped", str(stats.total_items_scraped))
        table.add_row("Items Stored", str(stats.total_items_stored))
        table.add_row("Duplicates Found", str(stats.total_duplicates_found))
        table.add_row("Q/A Generated", str(stats.total_qa_generated))
        table.add_row("Errors", str(stats.errors_count))
        table.add_row("Status", stats.status)
        console.print(table)

        await orchestrator._cleanup()

    asyncio.run(_run())


@cli.command("export-qa")
@click.option("--output", "-o", default="qa_output.jsonl", help="Output file path")
@click.option("--format", "-f", "fmt",
              type=click.Choice(["jsonl", "json", "huggingface"]), default="jsonl",
              help="jsonl/json: flat records with metadata | huggingface: chat-format messages array")
@click.pass_context
def export_qa(ctx, output, fmt):
    """Export Q/A pairs to file (jsonl, json, or HuggingFace chat format)"""
    config = get_config(ctx.obj["config_path"])
    setup_logging("INFO")

    async def _export():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        count = await orchestrator.qa_generator.export_all_qa(output, fmt)
        console.print(f"[green]✓ {count} Q/A pairs exported ({fmt}): {output}[/green]")
        await orchestrator._cleanup()

    asyncio.run(_export())


@cli.command()
@click.pass_context
def stats(ctx):
    """Show pipeline statistics"""
    config = get_config(ctx.obj["config_path"])
    setup_logging("WARNING")

    async def _stats():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()

        db_stats = await orchestrator.db.get_stats()
        storage_stats = await orchestrator.storage.get_storage_stats()

        table = Table(title="General Statistics")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Total Documents", str(db_stats.get("total_documents", 0)))
        table.add_row("Unique Documents", str(db_stats.get("unique_documents", 0)))
        table.add_row("Duplicate Documents", str(db_stats.get("duplicate_documents", 0)))
        table.add_row("Total Q/A Pairs", str(db_stats.get("total_qa_pairs", 0)))
        table.add_row("URLs Visited", str(db_stats.get("total_urls_visited", 0)))
        table.add_row("Active Sources", str(db_stats.get("active_sources", 0)))
        console.print(table)

        disk = storage_stats.get("disk", {})
        storage_table = Table(title="Storage")
        storage_table.add_column("Metric", style="cyan")
        storage_table.add_column("Value", style="green")
        storage_table.add_row("HTML Files", str(storage_stats.get("html_files", 0)))
        storage_table.add_row("Total Size", f"{storage_stats.get('total_size_gb', 0):.3f} GB")
        storage_table.add_row("Disk Used", f"{disk.get('percent', 0)}%")
        storage_table.add_row("Disk Free", f"{disk.get('free_gb', 0):.1f} GB")
        console.print(storage_table)

        per_source = db_stats.get("per_source", {})
        if per_source:
            src_table = Table(title="Per-Source Breakdown")
            src_table.add_column("Source", style="cyan")
            src_table.add_column("Total", style="white")
            src_table.add_column("Unique", style="green")
            src_table.add_column("Q/A", style="yellow")
            for src, data in per_source.items():
                src_table.add_row(
                    src,
                    str(data.get("total", 0)),
                    str(data.get("unique", 0)),
                    str(data.get("qa_pairs", 0)),
                )
            console.print(src_table)

        await orchestrator._cleanup()

    asyncio.run(_stats())


@cli.command()
@click.pass_context
def health(ctx):
    """Check system health"""
    config = get_config(ctx.obj["config_path"])
    setup_logging("WARNING")

    async def _health():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()

        health_data = await orchestrator.get_health()
        status = "HEALTHY" if health_data.get("disk_usage_percent", 100) < 90 else "WARNING"

        table = Table(title=f"System Status: {status}")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Last Round", str(health_data.get("current_round", 0)))
        table.add_row("Total Documents", str(health_data.get("total_documents", 0)))
        table.add_row("Unique Documents", str(health_data.get("unique_documents", 0)))
        table.add_row("Q/A Pairs", str(health_data.get("total_qa_pairs", 0)))
        table.add_row("Disk Used", f"{health_data.get('disk_usage_percent', 0):.1f}%")
        table.add_row("Disk Free", f"{health_data.get('disk_free_gb', 0):.1f} GB")
        console.print(table)

        ollama_ok = await orchestrator.qa_generator.check_ollama_health()
        if ollama_ok:
            console.print("[green]✓ Ollama API reachable[/green]")
        else:
            console.print("[red]✗ Ollama API unreachable — Q/A generation disabled[/red]")

        await orchestrator._cleanup()

    asyncio.run(_health())


@cli.command("scrape-only")
@click.option("--source", "-s", default=None, help="Scrape only this source name")
@click.pass_context
def scrape_only(ctx, source):
    """Run scraping only (no Q/A generation)"""
    config = get_config(ctx.obj["config_path"])
    log_config = config.get("general", {})
    setup_logging(log_config.get("log_level", "INFO"), log_config.get("log_dir", "logs"))

    if source:
        config["sources"] = [s for s in config.get("sources", []) if s.get("name") == source]
        if not config["sources"]:
            console.print(f"[red]Source not found: {source}[/red]")
            return

    async def _scrape():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()

        round_num = orchestrator._current_round + 1
        stats = await orchestrator.run_scrape_round(round_num)

        console.print(f"[green]✓ Scraping complete:[/green]")
        console.print(f"  Scraped:  {stats['total_items']}")
        console.print(f"  Stored:   {stats['total_stored']}")
        console.print(f"  Errors:   {stats['total_errors']}")

        await orchestrator._cleanup()

    asyncio.run(_scrape())


@cli.command("generate-qa")
@click.option("--batch-size", "-b", default=10, help="Number of documents to process")
@click.pass_context
def generate_qa(ctx, batch_size):
    """Manually trigger Q/A generation"""
    config = get_config(ctx.obj["config_path"])
    config["qa_generator"]["batch_size"] = batch_size
    setup_logging("INFO")

    async def _generate():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()

        stats = await orchestrator.qa_generator.process_batch()

        console.print(f"[green]✓ Q/A generation complete:[/green]")
        console.print(f"  Documents processed: {stats.get('processed', 0)}")
        console.print(f"  Q/A pairs generated: {stats.get('qa_generated', 0)}")
        console.print(f"  Failed:              {stats.get('failed', 0)}")
        
        await orchestrator._cleanup()

    asyncio.run(_generate())


@cli.command("qa-pipeline")
@click.option("--batch-size", "-b", default=5, help="Documents per batch")
@click.pass_context
def qa_pipeline_cmd(ctx, batch_size):
    """Run Q/A generation in a loop until all pending documents are processed"""
    config = get_config(ctx.obj["config_path"])
    config.setdefault("qa_generator", {})["batch_size"] = batch_size
    log_config = config.get("general", {})
    setup_logging(log_config.get("log_level", "INFO"), log_config.get("log_dir", "logs"))

    async def _run():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()

        total_generated = 0
        batch_num = 0
        while True:
            pending = await orchestrator.db.fetchval(
                "SELECT COUNT(*) FROM scraped_data WHERE is_duplicate = false AND qa_count = 0"
            ) or 0
            if not pending:
                console.print(f"[green]✓ All pending documents processed. Total Q/A generated: {total_generated}[/green]")
                break
            batch_num += 1
            console.print(f"[dim]  Batch {batch_num}: {pending} document(s) pending…[/dim]")
            stats = await orchestrator.qa_generator.process_batch()
            generated = stats.get("qa_generated", 0)
            total_generated += generated
            console.print(f"[cyan]  Batch {batch_num}: {generated} Q/A pair(s) generated[/cyan]")
            if not generated:
                console.print("[yellow]No Q/A generated — Ollama may be unavailable or no eligible docs. Stopping.[/yellow]")
                break

        await orchestrator._cleanup()

    asyncio.run(_run())


@cli.command("scrape-loop")
@click.pass_context
def scrape_loop(ctx):
    """Run continuous scraping loop (no Q/A generation)"""
    config = get_config(ctx.obj["config_path"])
    log_config = config.get("general", {})
    setup_logging(log_config.get("log_level", "INFO"), log_config.get("log_dir", "logs"))

    console.print(Panel.fit(
        "[bold green]Scrape Loop[/bold green]\n"
        "Continuous scraping — no Q/A generation.\n"
        f"Sources: {len([s for s in config.get('sources', []) if s.get('enabled')])}\n"
        f"Round delay: {config.get('general', {}).get('round_delay_seconds', 300)}s",
        title="Scrape Loop Starting"
    ))

    async def _run():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        await orchestrator.run_scrape_only_continuous()

    asyncio.run(_run())


@cli.command("qa-loop")
@click.pass_context
def qa_loop_cmd(ctx):
    """Run continuous Q/A generation loop (no scraping)"""
    config = get_config(ctx.obj["config_path"])
    log_config = config.get("general", {})
    setup_logging(log_config.get("log_level", "INFO"), log_config.get("log_dir", "logs"))

    console.print(Panel.fit(
        "[bold green]Q/A Loop[/bold green]\n"
        "Continuous Q/A generation — processes pending documents.\n"
        f"Batch size: {config.get('qa_generator', {}).get('batch_size', 20)}",
        title="Q/A Loop Starting"
    ))

    async def _run():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        await orchestrator.run_qa_only_continuous()

    asyncio.run(_run())


@cli.command("dashboard")
@click.option("--host", default=None, help="Bind host (default from settings)")
@click.option("--port", default=None, type=int, help="Bind port (default from settings)")
@click.option("--debug", is_flag=True, help="Run Flask in debug mode")
@click.pass_context
def dashboard(ctx, host, port, debug):
    """Start the web dashboard (PostgreSQL + Redis backed)"""
    config = get_config(ctx.obj["config_path"])
    setup_logging(config.get("general", {}).get("log_level", "INFO"))

    dash_cfg = config.get("dashboard", {})
    host = host or dash_cfg.get("host", "0.0.0.0")
    port = int(port or dash_cfg.get("port", 5050))
    debug = debug or bool(dash_cfg.get("debug", False))

    from src.dashboard.app import run_dashboard

    console.print(Panel.fit(
        f"[bold green]Dashboard[/bold green]\n"
        f"URL: [cyan]http://{host}:{port}[/cyan]\n"
        f"DB: PostgreSQL  Cache: Redis",
        title="Web Dashboard"
    ))
    run_dashboard(config=config, host=host, port=port, debug=debug)


@cli.command("list-sources")
@click.pass_context
def list_sources(ctx):
    """List all configured sources"""
    config = get_config(ctx.obj["config_path"])

    table = Table(title="Configured Sources")
    table.add_column("#", style="dim")
    table.add_column("Name", style="cyan")
    table.add_column("Type", style="yellow")
    table.add_column("Status", style="green")
    table.add_column("Priority", style="white")
    table.add_column("URL", style="dim")

    for i, src in enumerate(config.get("sources", []), 1):
        status = "active" if src.get("enabled") else "disabled"
        status_style = "green" if src.get("enabled") else "red"
        table.add_row(
            str(i),
            src.get("name", ""),
            src.get("type", ""),
            f"[{status_style}]{status}[/{status_style}]",
            str(src.get("priority", "")),
            src.get("base_url", ""),
        )

    console.print(table)


if __name__ == "__main__":
    cli()
