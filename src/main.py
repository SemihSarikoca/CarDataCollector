#!/usr/bin/env python3
"""
CAR DATA COLLECTOR BOT - Ana Giriş Noktası
Türk otomobil forumları ve teknik sitelerden veri toplama,
depolama, duplikasyon kontrolü ve Q/A üretimi pipeline'ı.

Kullanım:
    python -m src.main run              # Sürekli pipeline çalıştır
    python -m src.main single-round     # Tek tur çalıştır
    python -m src.main export-qa        # Q/A çiftlerini dışa aktar
    python -m src.main stats            # İstatistikleri göster
    python -m src.main health           # Sağlık durumu
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
    """Konfigürasyonu yükle"""
    try:
        return load_config(config_path)
    except FileNotFoundError:
        console.print(f"[red]Konfigürasyon dosyası bulunamadı: {config_path}[/red]")
        sys.exit(1)


@click.group()
@click.option("--config", "-c", default="config/settings.yaml", help="Konfigürasyon dosyası yolu")
@click.pass_context
def cli(ctx, config):
    """🚗 Car Data Collector Bot - Otomobil veri toplama sistemi"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command()
@click.pass_context
def run(ctx):
    """Sürekli pipeline çalıştır (7/24)"""
    config = get_config(ctx.obj["config_path"])
    log_config = config.get("general", {})
    setup_logging(log_config.get("log_level", "INFO"), log_config.get("log_dir", "logs"))

    console.print(Panel.fit(
        "[bold green]🚗 Car Data Collector Bot[/bold green]\n"
        "Sürekli veri toplama pipeline'ı başlatılıyor...\n"
        f"Kaynak sayısı: {len([s for s in config.get('sources', []) if s.get('enabled')])}\n"
        f"Tur arası bekleme: {config.get('general', {}).get('round_delay_seconds', 3600)}s",
        title="Pipeline Başlatılıyor"
    ))

    async def _run():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        await orchestrator.run_continuous()

    asyncio.run(_run())


@cli.command("single-round")
@click.pass_context
def single_round(ctx):
    """Tek bir pipeline turu çalıştır"""
    config = get_config(ctx.obj["config_path"])
    log_config = config.get("general", {})
    setup_logging(log_config.get("log_level", "INFO"), log_config.get("log_dir", "logs"))

    console.print("[bold]Tek tur pipeline çalıştırılıyor...[/bold]")

    async def _run():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        stats = await orchestrator.run_single_round()
        
        # Sonuçları göster
        table = Table(title="Tur Sonuçları")
        table.add_column("Metrik", style="cyan")
        table.add_column("Değer", style="green")
        table.add_row("Tur Numarası", str(stats.round_number))
        table.add_row("Toplanan İçerik", str(stats.total_items_scraped))
        table.add_row("Depolanan", str(stats.total_items_stored))
        table.add_row("Duplikatlar", str(stats.total_duplicates_found))
        table.add_row("Üretilen Q/A", str(stats.total_qa_generated))
        table.add_row("Hatalar", str(stats.errors_count))
        table.add_row("Durum", stats.status)
        console.print(table)
        
        await orchestrator._cleanup()

    asyncio.run(_run())


@cli.command("export-qa")
@click.option("--output", "-o", default="qa_output.jsonl", help="Çıktı dosyası")
@click.option("--format", "-f", "fmt", type=click.Choice(["jsonl", "json"]), default="jsonl")
@click.pass_context
def export_qa(ctx, output, fmt):
    """Q/A çiftlerini dosyaya dışa aktar"""
    config = get_config(ctx.obj["config_path"])
    setup_logging("INFO")

    async def _export():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        count = await orchestrator.qa_generator.export_all_qa(output, fmt)
        console.print(f"[green]✓ {count} Q/A çifti dışa aktarıldı: {output}[/green]")
        await orchestrator._cleanup()

    asyncio.run(_export())


@cli.command()
@click.pass_context
def stats(ctx):
    """İstatistikleri göster"""
    config = get_config(ctx.obj["config_path"])
    setup_logging("WARNING")

    async def _stats():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        
        db_stats = await orchestrator.db.get_stats()
        storage_stats = await orchestrator.storage.get_storage_stats()
        
        # Genel istatistikler
        table = Table(title="📊 Genel İstatistikler")
        table.add_column("Metrik", style="cyan")
        table.add_column("Değer", style="green")
        table.add_row("Toplam Döküman", str(db_stats.get("total_documents", 0)))
        table.add_row("Benzersiz Döküman", str(db_stats.get("unique_documents", 0)))
        table.add_row("Duplikat Döküman", str(db_stats.get("duplicate_documents", 0)))
        table.add_row("Toplam Q/A Çifti", str(db_stats.get("total_qa_pairs", 0)))
        table.add_row("Ziyaret Edilen URL", str(db_stats.get("total_urls_visited", 0)))
        table.add_row("Aktif Kaynak", str(db_stats.get("active_sources", 0)))
        console.print(table)

        # Depolama
        disk = storage_stats.get("disk", {})
        storage_table = Table(title="💾 Depolama")
        storage_table.add_column("Metrik", style="cyan")
        storage_table.add_column("Değer", style="green")
        storage_table.add_row("HTML Dosyası", str(storage_stats.get("html_files", 0)))
        storage_table.add_row("Toplam Boyut", f"{storage_stats.get('total_size_gb', 0):.3f} GB")
        storage_table.add_row("Disk Kullanım", f"{disk.get('percent', 0)}%")
        storage_table.add_row("Disk Boş", f"{disk.get('free_gb', 0):.1f} GB")
        console.print(storage_table)

        # Kaynak başına
        per_source = db_stats.get("per_source", {})
        if per_source:
            src_table = Table(title="📋 Kaynak Detayları")
            src_table.add_column("Kaynak", style="cyan")
            src_table.add_column("Toplam", style="white")
            src_table.add_column("Benzersiz", style="green")
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
    """Sistem sağlık durumunu kontrol et"""
    config = get_config(ctx.obj["config_path"])
    setup_logging("WARNING")

    async def _health():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        
        health_data = await orchestrator.get_health()
        
        status = "🟢 SAĞLIKLI" if health_data.get("disk_usage_percent", 100) < 90 else "🔴 DİKKAT"
        
        table = Table(title=f"Sistem Durumu: {status}")
        table.add_column("Metrik", style="cyan")
        table.add_column("Değer", style="green")
        table.add_row("Son Tur", str(health_data.get("current_round", 0)))
        table.add_row("Toplam Döküman", str(health_data.get("total_documents", 0)))
        table.add_row("Benzersiz Döküman", str(health_data.get("unique_documents", 0)))
        table.add_row("Q/A Çifti", str(health_data.get("total_qa_pairs", 0)))
        table.add_row("Disk Kullanım", f"{health_data.get('disk_usage_percent', 0):.1f}%")
        table.add_row("Disk Boş", f"{health_data.get('disk_free_gb', 0):.1f} GB")
        console.print(table)

        # Ollama kontrolü
        ollama_ok = await orchestrator.qa_generator.check_ollama_health()
        if ollama_ok:
            console.print("[green]✓ Ollama API erişilebilir[/green]")
        else:
            console.print("[red]✗ Ollama API erişilemiyor - Q/A üretimi devre dışı[/red]")

        await orchestrator._cleanup()

    asyncio.run(_health())


@cli.command("scrape-only")
@click.option("--source", "-s", default=None, help="Sadece belirli bir kaynağı scrape et")
@click.pass_context
def scrape_only(ctx, source):
    """Sadece scraping çalıştır (Q/A üretimi olmadan)"""
    config = get_config(ctx.obj["config_path"])
    log_config = config.get("general", {})
    setup_logging(log_config.get("log_level", "INFO"), log_config.get("log_dir", "logs"))

    if source:
        # Belirli kaynağı filtrele
        config["sources"] = [s for s in config.get("sources", []) if s.get("name") == source]
        if not config["sources"]:
            console.print(f"[red]Kaynak bulunamadı: {source}[/red]")
            return

    async def _scrape():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        
        round_num = orchestrator._current_round + 1
        stats = await orchestrator.run_scrape_round(round_num)
        
        console.print(f"[green]✓ Scraping tamamlandı:[/green]")
        console.print(f"  Toplanan: {stats['total_items']}")
        console.print(f"  Depolanan: {stats['total_stored']}")
        console.print(f"  Hatalar: {stats['total_errors']}")
        
        await orchestrator._cleanup()

    asyncio.run(_scrape())


@cli.command("generate-qa")
@click.option("--batch-size", "-b", default=10, help="İşlenecek döküman sayısı")
@click.pass_context
def generate_qa(ctx, batch_size):
    """Q/A üretimini manuel çalıştır"""
    config = get_config(ctx.obj["config_path"])
    config["qa_generator"]["batch_size"] = batch_size
    setup_logging("INFO")

    async def _generate():
        orchestrator = PipelineOrchestrator(config)
        await orchestrator.initialize()
        
        stats = await orchestrator.qa_generator.process_batch()
        
        console.print(f"[green]✓ Q/A üretimi tamamlandı:[/green]")
        console.print(f"  İşlenen döküman: {stats.get('processed', 0)}")
        console.print(f"  Üretilen Q/A: {stats.get('qa_generated', 0)}")
        console.print(f"  Başarısız: {stats.get('failed', 0)}")
        
        await orchestrator._cleanup()

    asyncio.run(_generate())


@cli.command("list-sources")
@click.pass_context
def list_sources(ctx):
    """Tanımlı kaynakları listele"""
    config = get_config(ctx.obj["config_path"])

    table = Table(title="📋 Tanımlı Kaynaklar")
    table.add_column("#", style="dim")
    table.add_column("İsim", style="cyan")
    table.add_column("Tür", style="yellow")
    table.add_column("Durum", style="green")
    table.add_column("Öncelik", style="white")
    table.add_column("URL", style="dim")

    for i, src in enumerate(config.get("sources", []), 1):
        status = "✓ Aktif" if src.get("enabled") else "✗ Pasif"
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
