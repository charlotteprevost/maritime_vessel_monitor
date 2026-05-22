"""
SAR Detections Routes - Dark vessel detection via SAR imagery.
"""
from flask import Blueprint, request, jsonify, current_app
import logging
import traceback
from datetime import datetime, timedelta
from urllib.parse import quote
from configs.config import WINGS_API, DATASETS
from utils.api_helpers import (
    parse_filters_from_request,
    sar_filterset_to_gfw_string,
    parse_eez_ids,
    eez_entries_from_app_config,
)
from services.dark_vessel_service import DarkVesselService
from utils.ttl_cache import cache_enabled, make_cache_key, get_cached_response, set_cached_response, default_ttl_seconds

detections_bp = Blueprint("detections", __name__)



@detections_bp.route("/api/tiles/proxy/<path:tile_path>", methods=["GET"])
def proxy_tile(tile_path):
    """Proxy tile requests to GFW API with authentication."""
    from flask import Response
    import requests
    
    # Transparent 1x1 PNG for error responses
    transparent_png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xdb\x00\x00\x00\x00IEND\xaeB`\x82'
    
    try:
        client = current_app.config.get("GFW_CLIENT")
        if not client:
            logging.error("GFW_CLIENT not initialized in tile proxy")
            return Response(transparent_png, mimetype='image/png', 
                          headers={'Cache-Control': 'public, max-age=300', 
                                  'Access-Control-Allow-Origin': '*'}), 200
        
        # Reconstruct the full GFW tile URL
        # tile_path format: "heatmap/8/128/100" (Leaflet replaces {z}/{x}/{y} with actual values)
        # The path should be: /4wings/tile/heatmap/{z}/{x}/{y}
        # Ensure it starts with /4wings/tile/
        if tile_path.startswith("4wings/tile/"):
            full_path = f"/{tile_path}"
        elif tile_path.startswith("heatmap/"):
            # Path is already "heatmap/{z}/{x}/{y}", just add the prefix
            full_path = f"/4wings/tile/{tile_path}"
        else:
            # Legacy format: "heatmap8/128/100" - need to insert / after heatmap
            # This shouldn't happen with the fixed URL format, but handle it for backwards compatibility
            if tile_path.startswith("heatmap") and not tile_path.startswith("heatmap/"):
                # Insert / after "heatmap" (e.g., "heatmap8" -> "heatmap/8")
                parts = tile_path.split("/", 1)
                if len(parts) == 2:
                    full_path = f"/4wings/tile/heatmap/{parts[1]}"
                else:
                    full_path = f"/4wings/tile/{tile_path}"
            else:
                full_path = f"/4wings/tile/heatmap/{tile_path}"
        
        # Add query parameters from request
        query_string = request.query_string.decode('utf-8')
        if query_string:
            full_path += f"?{query_string}"
        
        logging.debug(f"Proxying tile request: {full_path}")
        
        # Fetch tile from GFW API with authentication
        headers = {
            "Authorization": f"Bearer {client.api_token}",
            "Accept": "image/png,image/*,*/*"
        }
        
        url = f"{client.BASE_URL}{full_path}"
        response = requests.get(url, headers=headers, timeout=30)
        
        # Handle 404 gracefully - tiles may not exist for all zoom levels/coordinates
        if response.status_code == 404:
            logging.debug(f"Tile not found (404): {full_path} - this is normal for some tiles")
            return Response(
                transparent_png,
                mimetype='image/png',
                headers={
                    'Cache-Control': 'public, max-age=300',
                    'Access-Control-Allow-Origin': '*',
                    'Content-Type': 'image/png'
                }
            )
        
        # Handle other HTTP errors
        if response.status_code >= 400:
            logging.warning(f"HTTP {response.status_code} error proxying tile: {full_path}")
            # Return transparent PNG for any HTTP error to prevent map breaking
            return Response(
                transparent_png,
                mimetype='image/png',
                headers={
                    'Cache-Control': 'public, max-age=300',
                    'Access-Control-Allow-Origin': '*',
                    'Content-Type': 'image/png'
                }
            )
        
        # Success - return the image with proper headers
        return Response(
            response.content,
            mimetype='image/png',
            headers={
                'Cache-Control': 'public, max-age=3600',
                'Access-Control-Allow-Origin': '*',
                'Content-Type': 'image/png'
            }
        )
    except requests.exceptions.RequestException as e:
        # Handle network/request errors
        logging.warning(f"Request error proxying tile {tile_path}: {e}")
        return Response(
            transparent_png,
            mimetype='image/png',
            headers={
                'Cache-Control': 'public, max-age=300',
                'Access-Control-Allow-Origin': '*',
                'Content-Type': 'image/png'
            }
        )
    except Exception as e:
        # Handle any other unexpected errors
        logging.error(f"Unexpected error proxying tile {tile_path}: {e}", exc_info=True)
        return Response(
            transparent_png,
            mimetype='image/png',
            headers={
                'Cache-Control': 'public, max-age=300',
                'Access-Control-Allow-Origin': '*',
                'Content-Type': 'image/png'
            }
        )


@detections_bp.route("/api/detections", methods=["GET"])
def get_detections():
    """Get SAR detections (dark vessels) - combines SAR + gap events."""
    try:
        # Cache: this endpoint fans out to multiple upstream calls and can be slow.
        if cache_enabled(request.args):
            key = make_cache_key(request.method, request.path, request.args)
            cached = get_cached_response(key)
            if cached:
                payload, status = cached
                return jsonify(payload), status

        eez_ids = parse_eez_ids(request.args, "eez_ids")
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        interval = request.args.get("interval", "DAY")
        temporal_aggregation = request.args.get("temporal_aggregation", "false")

        try:
            filters_obj = parse_filters_from_request(request.args)
        except Exception as e:
            return jsonify({"error": f"Invalid filters: {e}"}), 400

        if not eez_ids or not start_date or not end_date:
            return jsonify({"error": "Missing required parameters"}), 400

        # Build tile URL
        style_id = getattr(current_app.config.get("CONFIG"), "SAR_TILE_STYLE", {}).get("id", "")
        filters_str = sar_filterset_to_gfw_string(filters_obj)
        # filters[0] is an expression and must be URL-encoded when embedded in a URL string.
        # (It can contain spaces and quotes, e.g. matched='false' AND flag in ('USA')).
        filters_param = quote(filters_str, safe="")
        # WINGS_API['tile'] ends with '/heatmap', so we need to add '/' before {z}
        tile_url = (
            f"{WINGS_API['tile']}"
            f"/{{z}}/{{x}}/{{y}}?format=PNG"
            f"&temporal-aggregation={temporal_aggregation}"
            f"&interval={interval}"
            f"&datasets[0]={DATASETS['sar']}"
            f"&filters[0]={filters_param}"
            f"&date-range={start_date},{end_date}"
            f"&style={style_id}"
        )

        client = current_app.config.get("GFW_CLIENT")
        if not client:
            return jsonify({"error": "API client not initialized"}), 500

        # Use service to get dark vessels (SAR + gaps)
        # Try both intentional and all gaps to get maximum coverage
        logging.info(f"Fetching dark vessels for {len(eez_ids)} EEZ(s): {eez_ids}")
        service = DarkVesselService(client)

        use_mvt = request.args.get("mvt_points", "true").lower() == "true"
        try:
            mvt_zoom = int(request.args.get("mvt_zoom", "7"))
        except ValueError:
            mvt_zoom = 7
        mvt_zoom = max(4, min(mvt_zoom, 10))
        try:
            max_mvt_tiles = int(request.args.get("max_mvt_tiles", "24"))
        except ValueError:
            max_mvt_tiles = 24
        max_mvt_tiles = max(4, min(max_mvt_tiles, 200))
        interaction_enrichment = request.args.get("interaction_enrichment", "true").lower() == "true"
        try:
            max_interaction_cells = int(request.args.get("max_interaction_cells", "40"))
        except ValueError:
            max_interaction_cells = 40
        max_interaction_cells = max(10, min(max_interaction_cells, 500))
        ta_bool = temporal_aggregation.lower() == "true"

        dark_vessels = service.get_dark_vessels(
            eez_ids=eez_ids,
            start_date=start_date,
            end_date=end_date,
            include_sar=True,
            eez_entries=eez_entries_from_app_config(current_app.config),
            use_mvt_point_fallback=use_mvt,
            mvt_zoom=mvt_zoom,
            max_mvt_tiles=max_mvt_tiles,
            mvt_interval=interval,
            mvt_temporal_aggregation=ta_bool,
            enable_interaction_enrichment=interaction_enrichment,
            max_interaction_cells=max_interaction_cells,
        )
        logging.info(f"Dark vessels fetched: {dark_vessels.get('summary', {})}")

        # Optional extra per-EEZ summary reports (duplicate upstream work vs get_sar_presence).
        # For long ranges + multiple EEZs, skip by default or via ?include_eez_summaries=false to save N API calls.
        include_eez_summaries = request.args.get("include_eez_summaries", "true").lower() == "true"
        summaries = []
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        days_diff = (end - start).days + 1

        if include_eez_summaries:
            if days_diff > 366:
                # Chunk summaries into 365-day chunks (API max is 366)
                from datetime import timedelta

                summary_chunks = []
                current_start = start
                while current_start < end:
                    current_end = min(current_start + timedelta(days=365), end)
                    summary_chunks.append(
                        (
                            current_start.strftime("%Y-%m-%d"),
                            current_end.strftime("%Y-%m-%d"),
                        )
                    )
                    current_start = current_end + timedelta(days=1)
            else:
                summary_chunks = [(start_date, end_date)]

            for eez_id in eez_ids:
                eez_summaries = []
                for chunk_start, chunk_end in summary_chunks:
                    try:
                        # Don't use matched filter in summary - API has type issues with boolean filters
                        # We'll get all data and filter client-side if needed
                        summary_filters = None if "matched" in filters_str else filters_str
                        logging.info(f"Fetching summary for EEZ {eez_id}, chunk {chunk_start} to {chunk_end}")
                        report = client.create_report(
                            dataset=DATASETS["sar"],
                            start_date=chunk_start,
                            end_date=chunk_end,
                            filters=summary_filters,
                            eez_id=eez_id,
                        )
                        eez_summaries.append({"chunk": f"{chunk_start},{chunk_end}", "summary": report})
                        logging.info(f"Summary fetched for EEZ {eez_id}, chunk {chunk_start} to {chunk_end}")
                    except Exception as e:
                        logging.warning(
                            f"Failed summary for EEZ {eez_id}, chunk {chunk_start} to {chunk_end}: {e}"
                        )
                        eez_summaries.append({"chunk": f"{chunk_start},{chunk_end}", "error": str(e)})
                summaries.append({"eez_id": eez_id, "chunks": eez_summaries})
        else:
            logging.info("Skipping EEZ summary reports (include_eez_summaries=false)")

        # Use proxied tile URL instead of direct GFW URL (requires auth)
        # The frontend will request tiles through our backend proxy
        # tile_url format: "https://gateway.api.globalfishingwatch.org/v3/4wings/tile/heatmap{z}/{x}/{y}?..."
        # We need: "/api/tiles/proxy/heatmap{z}/{x}/{y}?..."
        # Replace the full GFW base URL with our proxy endpoint
        # Note: {z}/{x}/{y} are Leaflet placeholders that will be replaced by the frontend
        gfw_base = "https://gateway.api.globalfishingwatch.org/v3/4wings/tile/"
        proxied_tile_url = tile_url.replace(
            gfw_base,
            '/api/tiles/proxy/'
        )
        
        # Build response with base data
        response_data = {
            "tile_url": proxied_tile_url,  # Use proxied URL
            "summaries": summaries,
            "dark_vessels": dark_vessels,
            "filters": filters_obj.dict(),
            "date_range": f"{start_date},{end_date}"
        }
        
        # Option 3: Batch endpoint with feature flags - extract data once
        include_clusters = request.args.get("include_clusters", "false").lower() == "true"
        include_routes = request.args.get("include_routes", "false").lower() == "true"
        include_stats = request.args.get("include_stats", "false").lower() == "true"
        
        # Extract data once to avoid redundant lookups
        sar_detections = dark_vessels.get("sar_detections", []) if (include_clusters or include_routes) else []
        date_range_str = f"{start_date},{end_date}"
        common_params = {"eez_ids": eez_ids, "date_range": date_range_str}
        
        # Include proximity clusters if requested
        if include_clusters:
            try:
                clusters = service.detect_proximity_clusters(
                    sar_detections=sar_detections,
                    max_distance_km=float(request.args.get("max_distance_km", 5.0)),
                    same_date_only=request.args.get("same_date_only", "true").lower() == "true"
                ) if sar_detections else []
                
                clustered_count = sum(c["detection_count"] for c in clusters)
                response_data["clusters"] = {
                    "clusters": clusters,
                    "total_clusters": len(clusters),
                    "total_vessels_in_clusters": sum(c["vessel_count"] for c in clusters),
                    "high_risk_clusters": sum(1 for c in clusters if c["risk_indicator"] == "high"),
                    "medium_risk_clusters": sum(1 for c in clusters if c["risk_indicator"] == "medium"),
                    "parameters": {**common_params, "max_distance_km": float(request.args.get("max_distance_km", 5.0)), "same_date_only": request.args.get("same_date_only", "true").lower() == "true"},
                    "summary": {
                        "total_sar_detections": len(sar_detections),
                        "clustered_detections": clustered_count,
                        "clustering_rate": f"{(clustered_count / len(sar_detections) * 100):.1f}%" if sar_detections else "0%"
                    }
                } if sar_detections else {"clusters": [], "total_clusters": 0, "total_vessels_in_clusters": 0, "message": "No SAR detections found"}
            except Exception as e:
                logging.warning(f"Failed to compute clusters: {e}")
                response_data["clusters"] = {"error": str(e)}
        
        # Include predicted routes if requested
        if include_routes:
            try:
                routes = service.predict_routes(
                    sar_detections=sar_detections,
                    max_time_hours=float(request.args.get("max_time_hours", 48.0)),
                    max_distance_km=float(request.args.get("max_distance_km_route", 100.0)),
                    min_route_length=int(request.args.get("min_route_length", 2))
                )
                response_data["routes"] = {
                    "routes": routes,
                    "total_routes": len(routes),
                    "parameters": {**common_params, "max_time_hours": float(request.args.get("max_time_hours", 48.0)), "max_distance_km": float(request.args.get("max_distance_km_route", 100.0)), "min_route_length": int(request.args.get("min_route_length", 2))}
                }
            except Exception as e:
                logging.warning(f"Failed to compute routes: {e}")
                response_data["routes"] = {"error": str(e)}
        
        # Include statistics if requested
        if include_stats:
            try:
                summary = dark_vessels.get("summary", {})
                response_data["statistics"] = {
                    "statistics": {
                        "sar_detections": summary.get("total_sar_detections", 0),
                        "eez_count": len(eez_ids),
                        "date_range": date_range_str
                    },
                    "enhanced_statistics": {
                        "note": "Enhanced statistics available via /api/analytics/dark-vessels endpoint to avoid timeouts."
                    }
                }
            except Exception as e:
                logging.warning(f"Failed to compute statistics: {e}")
                response_data["statistics"] = {"error": str(e)}
        
        resp = jsonify(response_data)
        if cache_enabled(request.args):
            set_cached_response(key, response_data, 200, ttl_seconds=default_ttl_seconds())
        return resp
    except Exception as e:
        logging.error(f"Error in get_detections: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@detections_bp.route("/api/detections/proximity-clusters", methods=["GET"])
def get_proximity_clusters():
    """
    Detect clusters of dark vessels close to each other at the same time.
    This can indicate dark trade activity (transshipment, rendezvous, illegal transfers).
    
    Risk Levels (based on maritime security frameworks):
    - High Risk (3+ vessels): Coordinated illicit activities, complex STS transfers
    - Medium Risk (2 vessels): Bilateral STS transfers or rendezvous
    
    See DARK_TRADE_RISK_THRESHOLDS.md for detailed citations and rationale.
    """
    try:
        if cache_enabled(request.args):
            key = make_cache_key(request.method, request.path, request.args)
            cached = get_cached_response(key)
            if cached:
                payload, status = cached
                return jsonify(payload), status

        eez_ids = parse_eez_ids(request.args, "eez_ids")
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        max_distance_km = float(request.args.get("max_distance_km", 5.0))
        same_date_only = request.args.get("same_date_only", "true").lower() == "true"
        interaction_enrichment = request.args.get("interaction_enrichment", "true").lower() == "true"
        try:
            max_interaction_cells = int(request.args.get("max_interaction_cells", "35"))
        except ValueError:
            max_interaction_cells = 35
        max_interaction_cells = max(10, min(max_interaction_cells, 500))

        if not eez_ids or not start_date or not end_date:
            return jsonify({"error": "Missing required parameters: eez_ids, start_date, end_date"}), 400

        if max_distance_km <= 0 or max_distance_km > 50:
            return jsonify({"error": "max_distance_km must be between 0 and 50"}), 400

        client = current_app.config.get("GFW_CLIENT")
        if not client:
            return jsonify({"error": "API client not initialized"}), 500

        # Get dark vessels (SAR detections)
        service = DarkVesselService(client)
        dark_vessels = service.get_dark_vessels(
            eez_ids=eez_ids,
            start_date=start_date,
            end_date=end_date,
            include_sar=True,
            eez_entries=eez_entries_from_app_config(current_app.config),
            enable_interaction_enrichment=interaction_enrichment,
            max_interaction_cells=max_interaction_cells,
        )

        sar_detections = dark_vessels.get("sar_detections", [])
        logging.info(f"Proximity cluster request: {len(sar_detections)} SAR detections, max_distance={max_distance_km}km, same_date_only={same_date_only}")
        
        if not sar_detections:
            return jsonify({
                "clusters": [],
                "total_clusters": 0,
                "total_vessels_in_clusters": 0,
                "message": "No SAR detections found for proximity analysis"
            })

        # Detect proximity clusters
        clusters = service.detect_proximity_clusters(
            sar_detections=sar_detections,
            max_distance_km=max_distance_km,
            same_date_only=same_date_only
        )

        # Calculate summary statistics
        total_vessels_in_clusters = sum(c["vessel_count"] for c in clusters)
        high_risk_clusters = [c for c in clusters if c["risk_indicator"] == "high"]
        medium_risk_clusters = [c for c in clusters if c["risk_indicator"] == "medium"]

        out = {
            "clusters": clusters,
            "total_clusters": len(clusters),
            "total_vessels_in_clusters": total_vessels_in_clusters,
            "high_risk_clusters": len(high_risk_clusters),
            "medium_risk_clusters": len(medium_risk_clusters),
            "parameters": {
                "max_distance_km": max_distance_km,
                "same_date_only": same_date_only,
                "eez_ids": eez_ids,
                "date_range": f"{start_date},{end_date}"
            },
            "summary": {
                "total_sar_detections": len(sar_detections),
                "clustered_detections": sum(c["detection_count"] for c in clusters),
                "clustering_rate": f"{(sum(c['detection_count'] for c in clusters) / len(sar_detections) * 100):.1f}%" if sar_detections else "0%"
            }
        }
        if cache_enabled(request.args):
            set_cached_response(key, out, 200, ttl_seconds=default_ttl_seconds())
        return jsonify(out)
    except Exception as e:
        logging.error(f"Error in get_proximity_clusters: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@detections_bp.route("/api/detections/routes", methods=["GET"])
def get_predicted_routes():
    """
    Predict likely routes dark vessels use by connecting detections temporally and spatially.
    
    This endpoint is maintained for backward compatibility. For better performance,
    use /api/detections with include_routes=true parameter (Option 3: Batch endpoint).
    """
    try:
        if cache_enabled(request.args):
            key = make_cache_key(request.method, request.path, request.args)
            cached = get_cached_response(key)
            if cached:
                payload, status = cached
                return jsonify(payload), status

        eez_ids = parse_eez_ids(request.args, "eez_ids")
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        max_time_hours = float(request.args.get("max_time_hours", 48.0))
        max_distance_km = float(request.args.get("max_distance_km", 100.0))
        min_route_length = int(request.args.get("min_route_length", 2))
        interaction_enrichment = request.args.get("interaction_enrichment", "true").lower() == "true"
        try:
            max_interaction_cells = int(request.args.get("max_interaction_cells", "40"))
        except ValueError:
            max_interaction_cells = 40
        max_interaction_cells = max(10, min(max_interaction_cells, 500))

        if not eez_ids or not start_date or not end_date:
            return jsonify({"error": "Missing required parameters: eez_ids, start_date, end_date"}), 400

        if max_time_hours <= 0 or max_time_hours > 168:  # Max 1 week
            return jsonify({"error": "max_time_hours must be between 0 and 168"}), 400

        if max_distance_km <= 0 or max_distance_km > 500:
            return jsonify({"error": "max_distance_km must be between 0 and 500"}), 400

        client = current_app.config.get("GFW_CLIENT")
        if not client:
            return jsonify({"error": "API client not initialized"}), 500

        # Get dark vessels (SAR only)
        service = DarkVesselService(client)
        dark_vessels = service.get_dark_vessels(
            eez_ids=eez_ids,
            start_date=start_date,
            end_date=end_date,
            include_sar=True,
            eez_entries=eez_entries_from_app_config(current_app.config),
            enable_interaction_enrichment=interaction_enrichment,
            max_interaction_cells=max_interaction_cells,
        )

        sar_detections = dark_vessels.get("sar_detections", [])

        logging.info(f"Route prediction request: {len(sar_detections)} SAR detections")

        # Predict routes from SAR detections only
        routes = service.predict_routes(
            sar_detections=sar_detections,
            max_time_hours=max_time_hours,
            max_distance_km=max_distance_km,
            min_route_length=min_route_length
        )

        out = {
            "routes": routes,
            "total_routes": len(routes),
            "parameters": {
                "max_time_hours": max_time_hours,
                "max_distance_km": max_distance_km,
                "min_route_length": min_route_length,
                "eez_ids": eez_ids,
                "date_range": f"{start_date},{end_date}"
            }
        }
        if cache_enabled(request.args):
            set_cached_response(key, out, 200, ttl_seconds=default_ttl_seconds())
        return jsonify(out)
    except Exception as e:
        logging.error(f"Error in get_predicted_routes: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@detections_bp.route("/api/detections/sar-ais-association", methods=["GET"])
def get_sar_ais_association():
    """
    Summarize SAR presence detections by AIS match status (matched vs unmatched) for a given EEZ/date range.

    This provides a practical “cooperative vs non-cooperative” view consistent with AIS+SAR fusion literature:
    - matched=True  -> SAR detections with an AIS match available
    - matched=False -> SAR detections without AIS match (non-cooperative / unmatched)

    Note: The underlying SAR presence report does not include vessel identity; this is a count/ratio summary.
    """
    try:
        if cache_enabled(request.args):
            key = make_cache_key(request.method, request.path, request.args)
            cached = get_cached_response(key)
            if cached:
                payload, status = cached
                return jsonify(payload), status

        eez_ids = parse_eez_ids(request.args, "eez_ids")
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")

        if not eez_ids or not start_date or not end_date:
            return jsonify({"error": "Missing required parameters: eez_ids, start_date, end_date"}), 400

        client = current_app.config.get("GFW_CLIENT")
        if not client:
            return jsonify({"error": "API client not initialized"}), 500

        service = DarkVesselService(client)

        matched_resp = service.get_sar_presence(
            eez_ids=eez_ids,
            start_date=start_date,
            end_date=end_date,
            matched=True,
            spatial_resolution="HIGH",
            temporal_resolution="DAILY",
        )
        unmatched_resp = service.get_sar_presence(
            eez_ids=eez_ids,
            start_date=start_date,
            end_date=end_date,
            matched=False,
            spatial_resolution="HIGH",
            temporal_resolution="DAILY",
        )

        matched_summary = matched_resp.get("summary", {}) or {}
        unmatched_summary = unmatched_resp.get("summary", {}) or {}

        matched_points = int(matched_summary.get("points") or 0)
        unmatched_points = int(unmatched_summary.get("points") or 0)
        total_points = matched_points + unmatched_points

        matched_total = int(matched_summary.get("total_detections") or 0)
        unmatched_total = int(unmatched_summary.get("total_detections") or 0)
        total_detections = matched_total + unmatched_total

        pct_points_matched = round((matched_points / total_points) * 100.0, 2) if total_points else 0.0
        pct_detections_matched = round((matched_total / total_detections) * 100.0, 2) if total_detections else 0.0

        out = {
            "matched": matched_summary,
            "unmatched": unmatched_summary,
            "totals": {
                "points": total_points,
                "total_detections": total_detections,
                "matched_points_pct": pct_points_matched,
                "matched_detections_pct": pct_detections_matched,
            },
            "parameters": {
                "eez_ids": eez_ids,
                "date_range": f"{start_date},{end_date}",
                "spatial_resolution": "HIGH",
                "temporal_resolution": "DAILY",
            },
        }
        if cache_enabled(request.args):
            set_cached_response(key, out, 200, ttl_seconds=default_ttl_seconds())
        return jsonify(out)
    except Exception as e:
        logging.error(f"Error in get_sar_ais_association: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500
