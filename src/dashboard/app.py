"""
Flask Dashboard Application
Real-time monitoring and data browsing for Car Data Collector Bot
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

from src.utils.logger import get_logger

logger = get_logger("dashboard")

app = Flask(__name__)
CORS(app)

# Database connection will be injected
db_pool = None
redis_client = None


def init_app(database_pool, redis):
    """Initialize app with database connections"""
    global db_pool, redis_client
    db_pool = database_pool
    redis_client = redis


# =============================================================================
# API Routes
# =============================================================================

@app.route("/")
def index():
    """Main dashboard page"""
    return render_template("index.html")


@app.route("/health")
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "car-data-collector"
    })


@app.route("/api/stats")
def get_stats():
    """Get overall statistics"""
    try:
        # This will be replaced with actual database queries
        stats = {
            "total_documents": 0,
            "unique_documents": 0,
            "total_qa_pairs": 0,
            "total_sources": 13,
            "active_sources": 0,
            "last_scrape": None,
            "scraping_status": "idle",
            "disk_usage_percent": 0,
            "disk_free_gb": 0,
        }
        
        # Get disk usage
        import psutil
        disk = psutil.disk_usage("/data")
        stats["disk_usage_percent"] = disk.percent
        stats["disk_free_gb"] = disk.free / (1024 ** 3)
        
        return jsonify(stats)
    except Exception as e:
        logger.error("stats_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources")
def get_sources():
    """Get all source statuses"""
    try:
        # Placeholder - will be replaced with database queries
        sources = [
            {
                "name": "reddit_mechanicadvice",
                "type": "forum",
                "status": "active",
                "last_scrape": None,
                "next_scrape": None,
                "total_items": 0,
                "error_count": 0,
            },
            {
                "name": "reddit_cartalk",
                "type": "forum",
                "status": "active",
                "last_scrape": None,
                "next_scrape": None,
                "total_items": 0,
                "error_count": 0,
            },
            # ... more sources
        ]
        return jsonify(sources)
    except Exception as e:
        logger.error("sources_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/data")
def get_data():
    """Get scraped data with pagination and filtering"""
    try:
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 50, type=int)
        source = request.args.get("source", None)
        category = request.args.get("category", None)
        search = request.args.get("search", None)
        
        # Placeholder response
        data = {
            "items": [],
            "total": 0,
            "page": page,
            "per_page": per_page,
            "total_pages": 0,
        }
        
        return jsonify(data)
    except Exception as e:
        logger.error("data_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/<item_id>")
def get_data_item(item_id: str):
    """Get single data item details"""
    try:
        # Placeholder
        return jsonify({"error": "Not found"}), 404
    except Exception as e:
        logger.error("data_item_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/logs")
def get_logs():
    """Get recent scrape logs"""
    try:
        limit = request.args.get("limit", 100, type=int)
        source = request.args.get("source", None)
        
        # Placeholder
        logs = []
        
        return jsonify(logs)
    except Exception as e:
        logger.error("logs_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/trends")
def get_trends():
    """Get scraping trends over time"""
    try:
        days = request.args.get("days", 7, type=int)
        
        # Placeholder - will show items scraped per day
        trends = {
            "labels": [],
            "scraped": [],
            "duplicates": [],
            "qa_generated": [],
        }
        
        # Generate sample labels
        for i in range(days, 0, -1):
            date = datetime.utcnow() - timedelta(days=i)
            trends["labels"].append(date.strftime("%Y-%m-%d"))
            trends["scraped"].append(0)
            trends["duplicates"].append(0)
            trends["qa_generated"].append(0)
        
        return jsonify(trends)
    except Exception as e:
        logger.error("trends_error", error=str(e))
        return jsonify({"error": str(e)}), 500


@app.route("/api/source-distribution")
def get_source_distribution():
    """Get document distribution by source"""
    try:
        # Placeholder
        distribution = {
            "labels": [],
            "values": [],
        }
        return jsonify(distribution)
    except Exception as e:
        logger.error("distribution_error", error=str(e))
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Error Handlers
# =============================================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500


# =============================================================================
# Run
# =============================================================================

def run_dashboard(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    """Run the Flask dashboard"""
    logger.info("dashboard_starting", host=host, port=port)
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    run_dashboard(debug=True)
