"""
Dark Vessel Service - Core logic for detecting and analyzing dark vessels.
Combines SAR detections with AIS gap events.
"""
import logging
import os
import time
import json
import math
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta


def _sar_report_chunk_days() -> int:
    """Wider windows = fewer GFW /4wings/report calls (default 56d ≈ 6–7 chunks/year vs 13×30d)."""
    try:
        d = int(os.environ.get("GFW_SAR_CHUNK_DAYS", "56"))
    except ValueError:
        d = 56
    return max(14, min(d, 90))


def _sar_report_sleep_s() -> float:
    """Pause between report calls to reduce 429s; lower = faster (default 0.2s was 1.0s)."""
    try:
        s = float(os.environ.get("GFW_REPORT_SLEEP_S", "0.2"))
    except ValueError:
        s = 0.2
    return max(0.0, min(s, 2.0))


class DarkVesselService:
    """Service for dark vessel detection and analysis."""
    
    def __init__(self, gfw_client):
        """Initialize with GFW API client."""
        self.client = gfw_client
    
    def _split_date_range(self, start_date: str, end_date: str, chunk_days: int = 30) -> List[tuple]:
        """
        Split a date range into chunks of chunk_days (default 30 days).
        Returns list of (start, end) date tuples.
        """
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        chunks = []
        
        current_start = start
        while current_start < end:
            current_end = min(current_start + timedelta(days=chunk_days - 1), end)
            chunks.append((
                current_start.strftime("%Y-%m-%d"),
                current_end.strftime("%Y-%m-%d")
            ))
            current_start = current_end + timedelta(days=1)
        
        return chunks
    
    def get_dark_vessels(
        self,
        eez_ids: List[str],
        start_date: str,
        end_date: str,
        include_sar: bool = True,
        include_gaps: bool = True,
        intentional_gaps_only: bool = True,
        eez_entries: Optional[Dict[str, Any]] = None,
        use_mvt_point_fallback: bool = True,
        mvt_zoom: int = 7,
        max_mvt_tiles: int = 24,
        mvt_interval: str = "DAY",
        mvt_temporal_aggregation: bool = False,
        enable_interaction_enrichment: bool = True,
        max_interaction_cells: int = 40,
    ) -> Dict[str, Any]:
        """
        Get dark vessels: SAR detections (matched=false) + AIS gap events.

        Long ranges are split with chunk size ``GFW_SAR_CHUNK_DAYS`` (default 56) to limit GFW load.

        When the v4 SAR report returns no lat/lon rows, optional MVT harvesting fetches
        4Wings heatmap tiles (format=MVT) over each EEZ bbox and uses cell centroids as points.

        Returns combined results with vessel IDs cross-referenced.
        """
        results = {"sar_detections": [], "summary": {}}
        
        chunk_days = _sar_report_chunk_days()
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        days_diff = (end - start).days + 1

        if days_diff > chunk_days:
            logging.info(
                "Date range (%s days) exceeds chunk size (%s), splitting into chunks",
                days_diff,
                chunk_days,
            )
            date_chunks = self._split_date_range(start_date, end_date, chunk_days=chunk_days)
            logging.info("Split into %s chunks", len(date_chunks))
        else:
            date_chunks = [(start_date, end_date)]
        
        sar_summary: Dict[str, Any] = {}
        mvt_fallback_used = False
        if include_sar:
            sar = self.get_sar_presence(
                eez_ids=eez_ids,
                start_date=start_date,
                end_date=end_date,
                matched=False,
                spatial_resolution="HIGH",
                temporal_resolution="DAILY",
            )
            results["sar_detections"] = sar.get("detections", [])
            sar_summary = sar.get("summary", {})

            if (
                use_mvt_point_fallback
                and eez_entries
                and len(results["sar_detections"]) == 0
            ):
                from utils.sar_mvt_points import harvest_sar_points_from_mvt

                results["sar_detections"] = harvest_sar_points_from_mvt(
                    self.client,
                    eez_ids,
                    start_date,
                    end_date,
                    matched=False,
                    eez_entries=eez_entries,
                    zoom_level=mvt_zoom,
                    max_tiles=max_mvt_tiles,
                    interval=mvt_interval,
                    temporal_aggregation=mvt_temporal_aggregation,
                )
                mvt_fallback_used = len(results["sar_detections"]) > 0

        interaction_enriched_points = 0
        if enable_interaction_enrichment and results["sar_detections"]:
            # Speed guardrail: large MVT result sets can make interaction lookups very slow.
            # Keep an adaptive cap to preserve responsiveness for interactive map use.
            adaptive_max_cells = max_interaction_cells
            point_count = len(results["sar_detections"])
            if point_count > 5000:
                adaptive_max_cells = min(adaptive_max_cells, 25)
            elif point_count > 2000:
                adaptive_max_cells = min(adaptive_max_cells, 30)
            elif point_count > 1000:
                adaptive_max_cells = min(adaptive_max_cells, 35)
            interaction_enriched_points = self._enrich_mvt_points_with_interaction(
                sar_points=results["sar_detections"],
                start_date=start_date,
                end_date=end_date,
                max_cells=adaptive_max_cells,
                matched=False,
            )

        # Prefer v4 report total; if absent (edge case), sum MVT cell weights
        total_events = int(sar_summary.get("total_detections") or 0) if include_sar else 0
        if total_events == 0 and results["sar_detections"]:
            total_events = sum(int(d.get("detections") or 1) for d in results["sar_detections"])

        results["summary"] = {
            "total_sar_detections": total_events,
            "unique_detection_points": len(results["sar_detections"]),
            "eez_count": len(eez_ids),
            "mvt_fallback_used": mvt_fallback_used,
            "interaction_enriched_points": interaction_enriched_points,
            "geometry_source": (
                "mvt_cells"
                if mvt_fallback_used
                else ("report_json" if results["sar_detections"] else "none")
            ),
            "note": (
                "SAR totals use GFW v4 report summed weights (often no lat/lon per row). "
                + (
                    "Map markers are heatmap cell centroids from MVT tiles (approximate locations, not vessel tracks)."
                    if mvt_fallback_used
                    else "Map markers use report coordinates when present; otherwise use the heatmap tile layer."
                )
            ),
        }

        return results

    @staticmethod
    def _extract_precise_coordinates(entry: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        if not isinstance(entry, dict):
            return None
        lat = entry.get("lat") or entry.get("latitude")
        lon = entry.get("lon") or entry.get("longitude")
        if lat is not None and lon is not None:
            try:
                latf = float(lat)
                lonf = float(lon)
                if -90 <= latf <= 90 and -180 <= lonf <= 180:
                    return latf, lonf
            except (TypeError, ValueError):
                pass
        geom = entry.get("geometry")
        if isinstance(geom, dict):
            coords = geom.get("coordinates")
            if geom.get("type") == "Point" and isinstance(coords, list) and len(coords) >= 2:
                try:
                    lonf = float(coords[0])
                    latf = float(coords[1])
                    if -90 <= latf <= 90 and -180 <= lonf <= 180:
                        return latf, lonf
                except (TypeError, ValueError):
                    pass
        return None

    def _enrich_mvt_points_with_interaction(
        self,
        sar_points: List[Dict[str, Any]],
        start_date: str,
        end_date: str,
        max_cells: int = 150,
        matched: Optional[bool] = False,
    ) -> int:
        """
        Best-effort enrichment for MVT centroid points using 4Wings interaction cell details.
        """
        candidates = []
        for idx, p in enumerate(sar_points):
            if p.get("source") != "gfw_mvt_cell":
                continue
            z = p.get("tile_z")
            x = p.get("tile_x")
            y = p.get("tile_y")
            c = p.get("interaction_cell")
            if z is None or x is None or y is None or c is None:
                continue
            try:
                key = (int(z), int(x), int(y), int(c))
            except (TypeError, ValueError):
                continue
            weight = int(p.get("detections") or 1)
            candidates.append((weight, key, idx))

        if not candidates:
            return 0

        # Prioritize high-weight cells first.
        candidates.sort(key=lambda t: t[0], reverse=True)
        selected = {}
        for _w, key, idx in candidates:
            selected.setdefault(key, []).append(idx)
            if len(selected) >= max_cells:
                break

        filters = "matched='true'" if matched is True else ("matched='false'" if matched is False else None)
        date_range = f"{start_date},{end_date}"
        enriched = 0

        for (z, x, y, cell), indexes in selected.items():
            try:
                details = self.client.get_interaction_data(
                    zoom_level=z,
                    x=x,
                    y=y,
                    cells=str(cell),
                    dataset="public-global-sar-presence:latest",
                    filters=filters,
                    date_range=date_range,
                    limit=10,
                )
            except Exception as e:
                logging.debug("Interaction lookup failed for %s/%s/%s cell=%s: %s", z, x, y, cell, e)
                continue

            entries = details.get("entries") if isinstance(details, dict) else None
            if not isinstance(entries, list) or len(entries) == 0:
                continue

            precise = self._extract_precise_coordinates(entries[0])
            for idx in indexes:
                p = sar_points[idx]
                p["interaction_verified"] = True
                p["interaction_count"] = len(entries)
                p["location_source"] = "interaction"
                if precise:
                    p["latitude"], p["longitude"] = precise
                    p["location_accuracy"] = "exact"
                    p["source"] = "interaction_point_exact"
                enriched += 1

        if enriched:
            logging.info("Interaction enrichment applied to %s SAR points (%s cells queried)", enriched, len(selected))
        return enriched

    def get_sar_presence(
        self,
        eez_ids: List[str],
        start_date: str,
        end_date: str,
        matched: Optional[bool] = None,
        spatial_resolution: str = "HIGH",
        temporal_resolution: str = "DAILY",
    ) -> Dict[str, Any]:
        """
        Fetch SAR presence detections for an EEZ/date range, optionally filtering by AIS match status.

        This is a lightweight “association” view:
        - matched=True  -> SAR detections with an AIS match (cooperative signal present)
        - matched=False -> SAR detections without AIS match (non-cooperative / unmatched)
        - matched=None  -> all SAR detections (no match filter)

        Uses 4Wings report parameters required for ``public-global-sar-presence:latest`` (v4):
        ``spatial-aggregation=true`` and ``group-by=VESSEL_ID`` (applied in ``GFWApiClient.create_report``).

        v4 JSON often has ``date`` + ``detections`` but no ``lat``/``lon``; those rows contribute
        to ``summary.total_detections`` only. Legacy responses with ``lat``/``lon`` still map normally.
        """
        # Split range to avoid API limits/timeouts
        chunk_days = _sar_report_chunk_days()
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        days_diff = (end - start).days + 1
        date_chunks = (
            self._split_date_range(start_date, end_date, chunk_days=chunk_days)
            if days_diff > chunk_days
            else [(start_date, end_date)]
        )
        sleep_s = _sar_report_sleep_s()

        # Per GFW API docs for reports: use quoted string, not boolean
        filters = None
        if matched is True:
            filters = "matched='true'"
        elif matched is False:
            filters = "matched='false'"

        detections_out: List[Dict[str, Any]] = []
        total_weight_from_api = 0

        # Serialize requests to avoid 429 errors (API token not enabled for concurrent reports)
        for i, eez_id in enumerate(eez_ids):
            for chunk_idx, (chunk_start, chunk_end) in enumerate(date_chunks):
                try:
                    if (i > 0 or chunk_idx > 0) and sleep_s > 0:
                        time.sleep(sleep_s)

                    logging.info(
                        f"Fetching SAR presence for EEZ {eez_id}, chunk {chunk_start} to {chunk_end}, matched={matched}"
                    )
                    report = self.client.create_report(
                        dataset="public-global-sar-presence:latest",
                        start_date=chunk_start,
                        end_date=chunk_end,
                        filters=filters,
                        eez_id=eez_id,
                        spatial_resolution=spatial_resolution,
                        temporal_resolution=temporal_resolution,
                    )

                    if "entries" not in report or not report.get("entries"):
                        continue

                    for entry in report["entries"]:
                        dataset_key = None
                        for key in entry.keys():
                            if "sar-presence" in key.lower():
                                dataset_key = key
                                break
                        if not dataset_key or not isinstance(entry.get(dataset_key), list):
                            continue

                        for det in entry[dataset_key]:
                            try:
                                w = int(det.get("detections") or 0)
                            except (TypeError, ValueError):
                                w = 0
                            total_weight_from_api += w if w > 0 else 0

                            lat = det.get("lat")
                            lon = det.get("lon")
                            if lat is None or lon is None:
                                continue
                            detections_out.append(
                                {
                                    "latitude": lat,
                                    "longitude": lon,
                                    "date": det.get("date"),
                                    "detections": det.get("detections", 1),
                                    "matched": matched,
                                    "vessel_id": det.get("vesselId") or det.get("vessel_id"),
                                    "source": "report_point_exact",
                                    "location_accuracy": "exact",
                                }
                            )
                except Exception as e:
                    logging.warning(
                        f"Failed SAR presence for EEZ {eez_id}, chunk {chunk_start} to {chunk_end}, matched={matched}: {e}"
                    )

        geo_weight = 0
        for d in detections_out:
            try:
                geo_weight += int(d.get("detections") or 0)
            except (TypeError, ValueError):
                pass

        total_hits = total_weight_from_api if total_weight_from_api > 0 else geo_weight

        return {
            "detections": detections_out,
            "summary": {
                "points": len(detections_out),
                "total_detections": total_hits,
                "matched_filter": matched,
                "eez_count": len(eez_ids),
                "date_range": f"{start_date},{end_date}",
            },
        }
    
    def calculate_risk_score(self, vessel_id: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """
        Calculate enhanced risk score for a vessel based on:
        - Gap frequency
        - IUU status
        - Fishing intensity
        - Encounter frequency
        - Port visit patterns
        
        Returns score 0-100.
        """
        try:
            vessel = {"datasetId": "public-global-vessel-identity:latest", "vesselId": vessel_id}
            
            # Get insights for vessel
            insights = self.client.get_vessel_insights(
                vessels=[vessel],
                start_date=start_date,
                end_date=end_date,
                includes=["GAP", "VESSEL-IDENTITY-IUU-VESSEL-LIST"]
            )
            
            score = 0
            factors = {}
            
            # IUU listing increases risk (0-50 points)
            if "vesselIdentity" in insights:
                iuu_listed = insights["vesselIdentity"].get("iuuVesselList", {}).get("totalTimesListedInThePeriod", 0)
                if iuu_listed > 0:
                    iuu_score = 50  # High risk if IUU listed
                    score += iuu_score
                    factors["iuu_listed"] = True
                    factors["iuu_count"] = iuu_listed
                    factors["iuu_score"] = iuu_score
                else:
                    factors["iuu_listed"] = False
            
            # Get additional activity data for risk calculation
            try:
                # Fishing intensity (0-15 points)
                fishing_events = self.client.get_all_events(
                    datasets=["public-global-fishing-events:latest"],
                    vessels=[vessel],
                    start_date=start_date,
                    end_date=end_date
                )
                fishing_count = len(fishing_events.get("entries", [])) if isinstance(fishing_events, dict) else 0
                fishing_score = min(fishing_count * 0.5, 15)  # Max 15 points
                score += fishing_score
                factors["fishing_events"] = fishing_count
                factors["fishing_score"] = fishing_score
            except Exception as e:
                logging.warning(f"Failed to get fishing events for risk calculation: {e}")
                factors["fishing_events"] = 0
                factors["fishing_score"] = 0
            
            try:
                # Encounter frequency (0-20 points) - frequent encounters = potential transshipment
                encounters = self.client.get_all_events(
                    datasets=["public-global-encounters-events:latest"],
                    vessels=[vessel],
                    start_date=start_date,
                    end_date=end_date
                )
                encounter_count = len(encounters.get("entries", [])) if isinstance(encounters, dict) else 0
                encounter_score = min(encounter_count * 2, 20)  # Max 20 points
                score += encounter_score
                factors["encounters"] = encounter_count
                factors["encounter_score"] = encounter_score
            except Exception as e:
                logging.warning(f"Failed to get encounters for risk calculation: {e}")
                factors["encounters"] = 0
                factors["encounter_score"] = 0
            
            try:
                # Port visits (0-15 points) - many port visits could indicate suspicious activity
                port_visits = self.client.get_all_events(
                    datasets=["public-global-port-visits-events:latest"],
                    vessels=[vessel],
                    start_date=start_date,
                    end_date=end_date
                )
                port_count = len(port_visits.get("entries", [])) if isinstance(port_visits, dict) else 0
                port_score = min(port_count * 0.3, 15)  # Max 15 points
                score += port_score
                factors["port_visits"] = port_count
                factors["port_score"] = port_score
            except Exception as e:
                logging.warning(f"Failed to get port visits for risk calculation: {e}")
                factors["port_visits"] = 0
                factors["port_score"] = 0
            
            # Calculate risk level
            risk_level = "low"
            if score >= 70:
                risk_level = "high"
            elif score >= 40:
                risk_level = "medium"
            
            return {
                "vessel_id": vessel_id,
                "risk_score": min(score, 100),
                "risk_level": risk_level,
                "factors": factors,
                "insights": insights,
                "date_range": f"{start_date},{end_date}"
            }
        except Exception as e:
            logging.error(f"Error calculating risk for {vessel_id}: {e}")
            return {"vessel_id": vessel_id, "risk_score": 0, "risk_level": "unknown", "error": str(e)}
    
    def _extract_vessel_id(self, item: Dict[str, Any]) -> Optional[str]:
        """
        Extract vessel ID from a detection or gap event.
        Tries multiple possible field names and nested structures.
        
        Args:
            item: Detection or gap event dictionary
            
        Returns:
            Vessel ID string if found, None otherwise
        """
        if not isinstance(item, dict):
            return None
        
        # Try direct fields first
        vid = (item.get("vesselId") or item.get("vessel_id") or item.get("id") or
               item.get("vesselIdentifier") or item.get("vessel_identifier"))
        
        if vid:
            return str(vid)
        
        # Try nested vessel object
        vessel = item.get("vessel")
        if isinstance(vessel, dict):
            vid = (vessel.get("vesselId") or vessel.get("vessel_id") or 
                   vessel.get("id") or vessel.get("vesselIdentifier"))
            if vid:
                return str(vid)
        
        # Try nested vesselIdentity object
        vessel_identity = item.get("vesselIdentity")
        if isinstance(vessel_identity, dict):
            vid = (vessel_identity.get("id") or vessel_identity.get("vesselId") or
                   vessel_identity.get("vesselIdentifier"))
            if vid:
                return str(vid)
        
        return None

    @staticmethod
    def _first_not_none(*values):
        """
        Return the first value that is not None.

        Important: do NOT use truthiness (`or`) for numeric fields like lat/lon,
        since 0.0 is a valid coordinate but is falsy in Python.
        """
        for v in values:
            if v is not None:
                return v
        return None
    
    def _haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Calculate distance between two points using Haversine formula.
        Returns distance in kilometers.
        """
        R = 6371  # Earth's radius in kilometers
        
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return R * c
    
    def detect_proximity_clusters(self,
                                 sar_detections: List[Dict[str, Any]],
                                 max_distance_km: float = 5.0,
                                 same_date_only: bool = True) -> List[Dict[str, Any]]:
        """
        Detect clusters of SAR detections that are close to each other at the same time.
        This can indicate dark trade activity (transshipment, rendezvous, illegal transfers).
        
        Risk assessment based on established maritime security frameworks:
        - High Risk (3+ vessels): Indicates coordinated illicit activities, complex STS transfers
          (Sources: Lloyd's List Intelligence, Kpler Risk & Compliance, LSE Research)
        - Medium Risk (2 vessels): May indicate bilateral STS transfers or rendezvous
          (Sources: Lloyd's List Intelligence, Windward Maritime Intelligence)
        
        See DARK_TRADE_RISK_THRESHOLDS.md for detailed citations and rationale.
        
        Args:
            sar_detections: List of SAR detection dictionaries with lat/lon/date
            max_distance_km: Maximum distance in km to consider vessels "close" (default 5km)
                             Based on typical STS transfer distances (0.5-2nm) with buffer
            same_date_only: If True, only cluster detections on the same date (default True)
        
        Returns:
            List of cluster dictionaries, each containing:
            - center_latitude, center_longitude: Center point of cluster
            - date: Date of cluster
            - vessel_count: Number of vessels in cluster (from detections count)
            - detections: List of detection points in cluster
            - max_distance_km: Maximum distance between any two points in cluster
            - risk_indicator: "high" if 3+ vessels, "medium" if 2 vessels, "low" otherwise
        """
        if not sar_detections:
            logging.debug("No SAR detections provided for clustering")
            return []
        
        logging.info(f"Detecting proximity clusters from {len(sar_detections)} SAR detections (max_distance={max_distance_km}km, same_date_only={same_date_only})")
        
        clusters = []
        processed = set()
        
        # Group by date if same_date_only
        if same_date_only:
            detections_by_date = {}
            for det in sar_detections:
                date = det.get("date")
                # Handle both string dates and None values
                if date:
                    if date not in detections_by_date:
                        detections_by_date[date] = []
                    detections_by_date[date].append(det)
                else:
                    # If no date, use "unknown" as key
                    if "unknown" not in detections_by_date:
                        detections_by_date["unknown"] = []
                    detections_by_date["unknown"].append(det)
            
            # Process each date separately (only if 2+ detections for that date)
            for date, date_detections in detections_by_date.items():
                if len(date_detections) >= 2:  # Only process if 2+ detections for this date
                    clusters.extend(self._find_clusters_for_date(
                        date_detections, 
                        max_distance_km, 
                        date if date != "unknown" else None
                    ))
        else:
            # Process all detections together (only if 2+ total detections)
            if len(sar_detections) >= 2:
                clusters = self._find_clusters_for_date(sar_detections, max_distance_km, None)
        
        # Sort by vessel count (descending) and date
        clusters.sort(key=lambda x: (-x["vessel_count"], x.get("date", "")))
        
        logging.info(f"Found {len(clusters)} proximity clusters from {len(sar_detections)} SAR detections")
        if clusters:
            logging.info(f"Cluster summary: {sum(c['vessel_count'] for c in clusters)} total vessels in clusters")
            logging.info(f"Risk breakdown: {sum(1 for c in clusters if c['risk_indicator'] == 'high')} high, {sum(1 for c in clusters if c['risk_indicator'] == 'medium')} medium")
        
        return clusters
    
    def _find_clusters_for_date(self,
                               detections: List[Dict[str, Any]],
                               max_distance_km: float,
                               date: Optional[str]) -> List[Dict[str, Any]]:
        """
        Find clusters within a set of detections using a graph-based approach.
        Detections are clustered if they form a connected component where each detection
        is within max_distance_km of at least one other detection in the cluster.
        """
        clusters = []
        processed = set()
        
        # Build distance graph: for each detection, find all nearby detections
        # This ensures we find all connected components (clusters)
        for i, det1 in enumerate(detections):
            if i in processed:
                continue
            
            # Get coordinates for det1
            lat1 = self._first_not_none(det1.get("latitude"), det1.get("lat"))
            lon1 = self._first_not_none(det1.get("longitude"), det1.get("lon"))
            
            if lat1 is None or lon1 is None:
                continue
            
            # Start a new cluster with det1
            cluster_detections = [det1]
            cluster_indices = [i]
            
            # Use breadth-first search to find all connected detections
            # A detection is added if it's within max_distance_km of ANY detection already in cluster
            queue = [i]  # Queue of indices to check for neighbors
            visited_in_cluster = {i}  # Track what we've already checked in this cluster
            
            while queue:
                current_idx = queue.pop(0)
                current_det = detections[current_idx]
                current_lat = self._first_not_none(current_det.get("latitude"), current_det.get("lat"))
                current_lon = self._first_not_none(current_det.get("longitude"), current_det.get("lon"))
                
                if current_lat is None or current_lon is None:
                    continue
                
                # Check all other detections for proximity to current detection
                for j, det2 in enumerate(detections):
                    if j in processed or j in visited_in_cluster:
                        continue
                    
                    lat2 = self._first_not_none(det2.get("latitude"), det2.get("lat"))
                    lon2 = self._first_not_none(det2.get("longitude"), det2.get("lon"))
                    
                    if lat2 is None or lon2 is None:
                        continue
                    
                    # Check if det2 is within max_distance_km of current detection
                    distance = self._haversine_distance(current_lat, current_lon, lat2, lon2)
                    
                    if distance <= max_distance_km:
                        # Add to cluster and continue searching from this detection
                        cluster_detections.append(det2)
                        cluster_indices.append(j)
                        visited_in_cluster.add(j)
                        queue.append(j)
            
            # Only create cluster if 2+ detections found
            if len(cluster_detections) >= 2:
                # Validate all detections have coordinates (defensive check)
                valid_detections = []
                for d in cluster_detections:
                    lat = self._first_not_none(d.get("latitude"), d.get("lat"))
                    lon = self._first_not_none(d.get("longitude"), d.get("lon"))
                    if lat is not None and lon is not None:
                        valid_detections.append((lat, lon, d))
                
                # Skip cluster if we don't have at least 2 valid detections
                if len(valid_detections) < 2:
                    logging.warning(f"Skipping cluster with {len(cluster_detections)} detections but only {len(valid_detections)} have valid coordinates")
                    continue
                
                # Calculate cluster center (average of all valid points)
                total_lat = sum(lat for lat, lon, d in valid_detections)
                total_lon = sum(lon for lat, lon, d in valid_detections)
                center_lat = total_lat / len(valid_detections)
                center_lon = total_lon / len(valid_detections)
                
                # Calculate total vessel count (sum of detections counts from valid detections)
                total_vessels = sum(d.get("detections", 1) for lat, lon, d in valid_detections)
                
                # Calculate max distance within cluster (using valid detections only)
                max_dist = 0
                for i, (lat1, lon1, d1) in enumerate(valid_detections):
                    for j, (lat2, lon2, d2) in enumerate(valid_detections[i+1:], start=i+1):
                        dist = self._haversine_distance(lat1, lon1, lat2, lon2)
                        max_dist = max(max_dist, dist)
                
                # Determine risk indicator
                risk_indicator = "low"
                if total_vessels >= 3:
                    risk_indicator = "high"
                elif total_vessels >= 2:
                    risk_indicator = "medium"
                
                cluster = {
                    "center_latitude": center_lat,
                    "center_longitude": center_lon,
                    "date": date or valid_detections[0][2].get("date") if valid_detections else None,
                    "vessel_count": total_vessels,
                    "detection_count": len(valid_detections),
                    "detections": [d for lat, lon, d in valid_detections],  # Only include valid detections
                    "max_distance_km": round(max_dist, 2),
                    "risk_indicator": risk_indicator,
                    "description": f"{total_vessels} dark vessel(s) detected within {max_distance_km}km - possible transshipment/rendezvous"
                }
                
                clusters.append(cluster)
                
                # Mark all detections in cluster as processed
                processed.update(cluster_indices)
        
        return clusters
    
    def predict_routes(self,
                      sar_detections: List[Dict[str, Any]],
                      max_time_hours: float = 48.0,
                      max_distance_km: float = 100.0,
                      min_route_length: int = 2) -> List[Dict[str, Any]]:
        """
        Predict likely routes dark vessels use by connecting SAR detections temporally and spatially.
        
        Uses statistical analysis to connect SAR detections that are close in time and space.
        This method implements a temporal-spatial clustering approach based on maritime vessel
        movement patterns and SAR detection characteristics.
        
        Methodology:
        - Temporal proximity: Detections within 48 hours are considered for route connection
          (based on typical vessel speeds and SAR revisit times)
        - Spatial proximity: Detections within 100km are connected (accounts for vessel movement
          between satellite passes)
        - Confidence scoring: Based on temporal consistency, spatial continuity, and route length
        
        Research Sources:
        - Global Fishing Watch: "Public Global SAR Presence Dataset" methodology
          (https://globalfishingwatch.org/data-download/datasets/public-global-sar-presence)
        - Sentinel-1 SAR characteristics: 12-day revisit cycle, ~100km swath width
          (https://sentinel.esa.int/web/sentinel/missions/sentinel-1)
        - Maritime vessel speed analysis: Typical speeds 10-20 knots (18-37 km/h)
          (International Maritime Organization, SOLAS regulations)
        - Temporal-spatial clustering for vessel tracking:
          Kroodsma, D.A. et al. (2018). "Tracking the global footprint of fisheries."
          Science, 359(6378), 904-908.
        
        Args:
            sar_detections: List of SAR detection points
            max_time_hours: Maximum time difference (hours) to connect detections (default 48h)
            max_distance_km: Maximum distance (km) to connect detections (default 100km)
            min_route_length: Minimum number of points to form a route (default 2)
        
        Returns:
            List of route dictionaries, each containing:
            - route_id: Unique identifier for the route
            - points: List of [lat, lon, timestamp] points along the route
            - total_distance_km: Total route distance
            - duration_hours: Time span of the route
            - confidence: Confidence score (0-1) based on temporal/spatial consistency
        """
        if not sar_detections:
            logging.debug("No SAR detections provided for route prediction")
            return []
        
        logging.info(f"Predicting routes from {len(sar_detections)} SAR detections")
        
        routes = []
        
        # Helper to extract coordinates and timestamp
        def extract_point_data(item):
            """Extract lat, lon, and timestamp from detection/gap event."""
            # Extract coordinates
            lat = self._first_not_none(
                item.get("latitude"),
                item.get("lat"),
                item.get("lat_center"),
                item.get("center_lat"),
                item.get("startLat"),
                item.get("endLat"),
                item.get("centerLat"),
            )
            lon = self._first_not_none(
                item.get("longitude"),
                item.get("lon"),
                item.get("lon_center"),
                item.get("center_lon"),
                item.get("startLon"),
                item.get("endLon"),
                item.get("centerLon"),
            )
            
            # Try geometry/coordinates
            if (lat is None or lon is None) and item.get("geometry"):
                geom = item["geometry"]
                if geom.get("type") == "Point" and isinstance(geom.get("coordinates"), list):
                    lon = geom["coordinates"][0]
                    lat = geom["coordinates"][1]
            
            if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                return None
            
            # Extract timestamp/date
            timestamp = None
            date_str = item.get("date") or item.get("timestamp") or item.get("start") or item.get("end")
            if date_str:
                try:
                    # Try parsing various date formats
                    if isinstance(date_str, str):
                        if "T" in date_str:
                            timestamp = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        else:
                            timestamp = datetime.strptime(date_str, "%Y-%m-%d")
                    else:
                        timestamp = date_str
                except:
                    pass
            
            return {
                "lat": lat, "lon": lon, "timestamp": timestamp, "date": date_str,
                "vessel_id": None,
                "source": item.get("source") or "sar",
                "location_accuracy": item.get("location_accuracy") or "approx",
                "interaction_verified": bool(item.get("interaction_verified")),
                "interaction_count": int(item.get("interaction_count") or 0),
                "date_start": item.get("date_start"),
                "date_end": item.get("date_end"),
            }
        
        # Extract all points with valid coordinates from SAR detections
        all_points = []
        for det in sar_detections:
            point = extract_point_data(det)
            if point:
                all_points.append(point)
        
        if len(all_points) < min_route_length:
            logging.debug(f"Not enough valid points ({len(all_points)}) for route prediction")
            return []
        
        # Sort points by timestamp (or date if no timestamp)
        def get_sort_key(point):
            if point["timestamp"]:
                return point["timestamp"]
            elif point["date"]:
                try:
                    return datetime.strptime(point["date"], "%Y-%m-%d")
                except:
                    return datetime.min
            return datetime.min
        
        all_points.sort(key=get_sort_key)
        
        # Process SAR detections using statistical clustering (no vessel IDs available)
        if len(all_points) >= min_route_length:
            # If detections collapse to one day (common with MVT fallback), broad distance thresholds
            # can over-link into a few long spaghetti routes. Tighten distance and cap route length.
            temporal_keys = set()
            for p in all_points:
                ts = p.get("timestamp")
                ds = p.get("date")
                if isinstance(ts, datetime):
                    temporal_keys.add(ts.strftime("%Y-%m-%d"))
                elif ds:
                    temporal_keys.add(str(ds)[:10])

            low_temporal_diversity = len(temporal_keys) <= 1
            effective_max_distance = min(max_distance_km, 35.0) if low_temporal_diversity else max_distance_km
            max_points_per_route = 12 if low_temporal_diversity else 40
            if low_temporal_diversity:
                logging.info(
                    "Route prediction: low temporal diversity (%s day), using tighter max_distance_km=%s and max_points_per_route=%s",
                    max(1, len(temporal_keys)),
                    effective_max_distance,
                    max_points_per_route,
                )

            # Group SAR points by temporal proximity and connect spatially
            sar_routes = self._connect_sar_points(
                all_points,
                max_time_hours,
                effective_max_distance,
                min_route_length,
                max_points_per_route=max_points_per_route,
            )
            routes.extend(sar_routes)
        
        # Sort routes by confidence and length
        routes.sort(key=lambda r: (-r.get("confidence", 0), -len(r.get("points", []))))
        
        logging.info(f"Predicted {len(routes)} routes from {len(all_points)} detection points")
        
        return routes
    
    def _create_route_from_points(
        self,
        points: List[List],
        vessel_id: Optional[str] = None,
        point_meta: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Create a route dictionary from a list of [lat, lon, timestamp] points."""
        if len(points) < 2:
            return None
        
        # Calculate total distance
        total_distance = 0
        for i in range(len(points) - 1):
            lat1, lon1 = points[i][0], points[i][1]
            lat2, lon2 = points[i+1][0], points[i+1][1]
            total_distance += self._haversine_distance(lat1, lon1, lat2, lon2)
        
        # Calculate duration
        duration_hours = None
        if len(points) > 1:
            first_time = points[0][2]
            last_time = points[-1][2]
            if first_time and last_time:
                try:
                    if isinstance(first_time, datetime) and isinstance(last_time, datetime):
                        duration_hours = abs((last_time - first_time).total_seconds() / 3600)
                    elif isinstance(first_time, str) and isinstance(last_time, str):
                        d1 = datetime.strptime(first_time[:10], "%Y-%m-%d")
                        d2 = datetime.strptime(last_time[:10], "%Y-%m-%d")
                        duration_hours = abs((d2 - d1).total_seconds() / 3600)
                except:
                    pass
        
        # Calculate confidence based on:
        # - Number of points (more = higher confidence)
        # - Temporal consistency (closer in time = higher confidence)
        # - Spatial consistency (reasonable distances = higher confidence)
        confidence = min(len(points) / 10.0, 1.0)  # More points = higher confidence, capped at 1.0
        
        if duration_hours and duration_hours > 0:
            avg_speed_kmh = total_distance / duration_hours
            # Reasonable vessel speeds are 10-50 km/h, penalize unrealistic speeds
            if 5 <= avg_speed_kmh <= 60:
                confidence *= 1.0
            elif avg_speed_kmh < 5:
                confidence *= 0.8  # Very slow, might be drifting
            else:
                confidence *= 0.6  # Unrealistically fast
        
        exact_points = 0
        interaction_verified_points = 0
        interaction_hits = 0
        window_start = None
        window_end = None
        if point_meta:
            for pm in point_meta:
                if (pm or {}).get("location_accuracy") == "exact":
                    exact_points += 1
                if (pm or {}).get("interaction_verified"):
                    interaction_verified_points += 1
                interaction_hits += int((pm or {}).get("interaction_count") or 0)
                ds = (pm or {}).get("date_start")
                de = (pm or {}).get("date_end")
                if ds and (window_start is None or str(ds) < str(window_start)):
                    window_start = str(ds)
                if de and (window_end is None or str(de) > str(window_end)):
                    window_end = str(de)
            if interaction_verified_points > 0:
                # Reward routes that are corroborated by interaction evidence.
                confidence = min(1.0, confidence * 1.15)
            if exact_points > 0:
                # Slight confidence boost when coordinates are exact report/interaction points.
                confidence = min(1.0, confidence * 1.1)

        # Create hash from point coordinates (lat, lon) - ensure tuples for hashability
        coord_tuples = tuple((float(p[0]), float(p[1])) for p in points if len(p) >= 2)
        route_id = f"route_{len(points)}_{abs(hash(coord_tuples)) % 10000}"
        
        return {
            "route_id": route_id,
            "points": points,
            "total_distance_km": round(total_distance, 2),
            "duration_hours": round(duration_hours, 2) if duration_hours else None,
            "confidence": round(confidence, 2),
            "vessel_id": vessel_id,
            "point_count": len(points),
            "exact_point_count": exact_points,
            "interaction_verified_points": interaction_verified_points,
            "interaction_hits": interaction_hits,
            "time_window_start": window_start,
            "time_window_end": window_end,
        }
    
    def _connect_sar_points(self,
                           points: List[Dict[str, Any]],
                           max_time_hours: float,
                           max_distance_km: float,
                           min_route_length: int,
                           max_points_per_route: int = 40) -> List[Dict[str, Any]]:
        """
        Connect SAR detection points into routes using temporal and spatial proximity.
        Since SAR points don't have vessel IDs, we use statistical methods to connect them.
        """
        routes = []
        processed = set()
        
        for i, point1 in enumerate(points):
            if i in processed:
                continue
            
            # Start a new route from this point
            route_points = [[point1["lat"], point1["lon"], point1.get("timestamp") or point1.get("date")]]
            route_meta_points = [point1]
            processed.add(i)
            
            # Find next point in sequence
            current_point = point1
            found_next = True
            
            while found_next:
                found_next = False
                if len(route_points) >= max_points_per_route:
                    break
                best_next = None
                best_score = 0
                best_idx = None
                
                for j, point2 in enumerate(points):
                    if j in processed or j == i:
                        continue
                    
                    # Calculate distance
                    distance = self._haversine_distance(
                        current_point["lat"], current_point["lon"],
                        point2["lat"], point2["lon"]
                    )
                    
                    if distance > max_distance_km:
                        continue
                    
                    # Time difference between segments (hours). MVT / same-day SAR often has no
                    # ordering signal; allow 0h when temporal data is missing or same instant/day.
                    time_diff_hours = None
                    t1, t2 = current_point.get("timestamp"), point2.get("timestamp")
                    d1s, d2s = current_point.get("date"), point2.get("date")
                    if t1 and t2:
                        td = (t2 - t1).total_seconds() / 3600
                        if td < 0:
                            continue
                        time_diff_hours = td
                    elif d1s and d2s:
                        try:
                            d1 = datetime.strptime(str(d1s)[:10], "%Y-%m-%d")
                            d2 = datetime.strptime(str(d2s)[:10], "%Y-%m-%d")
                            td = (d2 - d1).total_seconds() / 3600
                            if td < 0:
                                continue
                            time_diff_hours = td
                        except Exception:
                            time_diff_hours = 0.0
                    else:
                        # One or both lack comparable dates (e.g. legacy MVT rows): spatial-only link.
                        time_diff_hours = 0.0
                    
                    if time_diff_hours is None or time_diff_hours > max_time_hours:
                        continue
                    
                    # Score based on distance and time (closer and sooner = better)
                    # Lower distance and time = higher score
                    score = 1.0 / (1.0 + distance) * 1.0 / (1.0 + time_diff_hours)
                    
                    if score > best_score:
                        best_score = score
                        best_next = point2
                        best_idx = j
                
                if best_next and best_idx is not None:
                    route_points.append([best_next["lat"], best_next["lon"], 
                                       best_next.get("timestamp") or best_next.get("date")])
                    route_meta_points.append(best_next)
                    processed.add(best_idx)
                    current_point = best_next
                    found_next = True
                else:
                    break
            
            # Create route if we have enough points
            if len(route_points) >= min_route_length:
                route = self._create_route_from_points(route_points, None, route_meta_points)
                if route:
                    routes.append(route)
        
        return routes