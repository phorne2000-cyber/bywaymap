import io
import json
import hashlib
import secrets
import hmac
import math
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, Response, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from PIL import Image, ImageDraw
from app.qct_reader import convert_qct_to_png

APP_NAME = "hotsausage-byways"
APP_VERSION = "6.4"

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SOURCE_DIR = DATA_DIR / "sources"
BYWAY_SOURCE_DIR = SOURCE_DIR / "byways"
TRO_SOURCE_DIR = SOURCE_DIR / "tros"
MANUAL_BYWAY_DIR = BYWAY_SOURCE_DIR / "manual"
MANUAL_TRO_DIR = TRO_SOURCE_DIR / "manual"
FETCHED_DIR = SOURCE_DIR / "fetched"
DISCOVERED_DIR = DATA_DIR / "discovered"
TRACK_DIR = DATA_DIR / "tracks"
MERGED_DIR = DATA_DIR / "merged"
TILE_ROOT = DATA_DIR / "tiles"
PLAIN_TILE_DIR = TILE_ROOT / "boats"
COLOUR_TILE_DIR = TILE_ROOT / "boats-coloured"

SOURCES_PATH = DATA_DIR / "sources.json"
STATUS_PATH = DATA_DIR / "status.json"
DISCOVERY_PATH = DATA_DIR / "discovery.json"
TRACKS_PATH = DATA_DIR / "tracks.json"
USERS_PATH = DATA_DIR / "users.json"
INVITES_PATH = DATA_DIR / "invites.json"
SESSIONS_PATH = DATA_DIR / "sessions.json"
MAPS_PATH = DATA_DIR / "maps.json"
ROUTES_PATH = DATA_DIR / "routes.json"
MAP_DIR = DATA_DIR / "maps"
MAP_TILE_DIR = DATA_DIR / "map-tiles"
MAP_IMAGE_DIR = DATA_DIR / "map-images"
ROUTE_DIR = DATA_DIR / "routes"
OSM_GEOJSON_PATH = BYWAY_SOURCE_DIR / "osm-boats.geojson"
MERGED_BYWAYS_PATH = MERGED_DIR / "byways.geojson"
MERGED_TROS_PATH = MERGED_DIR / "tros.geojson"
ANALYSED_BYWAYS_PATH = MERGED_DIR / "byways-with-tro-status.geojson"
AUTHORITY_REGISTRY_PATH = Path(__file__).with_name("uk_authorities.json")

OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://bywaymap.hornesys.co.uk").rstrip("/")
UPDATE_HOUR = int(os.getenv("UPDATE_HOUR", "3"))
MIN_ZOOM = int(os.getenv("MIN_ZOOM", "7"))
MAX_ZOOM = int(os.getenv("MAX_ZOOM", "16"))
PREGENERATE_MAX_ZOOM = int(os.getenv("PREGENERATE_MAX_ZOOM", "12"))
TRO_MATCH_METRES = float(os.getenv("TRO_MATCH_METRES", "75"))

DTRO_API_BASE_URL = os.getenv("DTRO_API_BASE_URL", "").strip()
DTRO_API_KEY = os.getenv("DTRO_API_KEY", "").strip()
DTRO_API_AUTH_HEADER = os.getenv("DTRO_API_AUTH_HEADER", "Authorization").strip()
DTRO_API_AUTH_PREFIX = os.getenv("DTRO_API_AUTH_PREFIX", "Bearer").strip()

OVERPASS_QUERY = """
[out:json][timeout:900];
area["ISO3166-1"="GB"][admin_level=2]->.uk;
(
  way(area.uk)["designation"="byway_open_to_all_traffic"];
  relation(area.uk)["designation"="byway_open_to_all_traffic"];
);
out geom;
"""

app = FastAPI(title="HotSausage Byways 6.4")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS", "DELETE"],
    allow_headers=["*"],
)
update_lock = threading.Lock()
discovery_lock = threading.Lock()

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def ensure_dirs() -> None:
    for d in [DATA_DIR, SOURCE_DIR, BYWAY_SOURCE_DIR, TRO_SOURCE_DIR, MANUAL_BYWAY_DIR, MANUAL_TRO_DIR, FETCHED_DIR, DISCOVERED_DIR, TRACK_DIR, MAP_DIR, MAP_TILE_DIR, MAP_IMAGE_DIR, ROUTE_DIR, MERGED_DIR, PLAIN_TILE_DIR, COLOUR_TILE_DIR]:
        d.mkdir(parents=True, exist_ok=True)

def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)

def save_status(payload: dict[str, Any]) -> None:
    payload["updated_at"] = utc_now()
    write_json_atomic(STATUS_PATH, payload)

def load_status() -> dict[str, Any]:
    return read_json(STATUS_PATH, {"service": APP_NAME, "version": APP_VERSION, "state": "never_updated"})

def safe_id(value: str) -> str:
    s = "".join(c.lower() if c.isalnum() else "-" for c in value).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s or f"source-{int(time.time())}"

def compact_id(value: str, limit: int = 96) -> str:
    sid = safe_id(value)
    if len(sid) <= limit:
        return sid
    digest = hashlib.sha1(sid.encode("utf-8")).hexdigest()[:10]
    return f"{sid[:limit - 11].rstrip('-')}-{digest}"

def default_sources() -> dict[str, Any]:
    return {"sources": [{
        "id": "osm-boats", "name": "OpenStreetMap BOAT query", "kind": "byway",
        "type": "overpass_osm_boats", "enabled": True, "url": "",
        "notes": "OSM designation=byway_open_to_all_traffic. Not legally authoritative.",
        "created_at": utc_now(),
    }, {
        "id": "dft-dtro", "name": "DfT D-TRO API", "kind": "tro",
        "type": "dtro_api", "enabled": False, "url": "",
        "notes": "Enable after setting DTRO_API_BASE_URL and DTRO_API_KEY.",
        "created_at": utc_now(),
    }]}

def load_sources() -> dict[str, Any]:
    if not SOURCES_PATH.exists():
        payload = default_sources()
        write_json_atomic(SOURCES_PATH, payload)
        return payload
    return read_json(SOURCES_PATH, default_sources())

def save_sources(payload: dict[str, Any]) -> None:
    write_json_atomic(SOURCES_PATH, payload)

def load_authority_registry() -> dict[str, Any]:
    return read_json(AUTHORITY_REGISTRY_PATH, {"version": APP_VERSION, "authority_count": 0, "authorities": []})

def authority_to_source_record(authority: dict[str, Any]) -> dict[str, Any]:
    name = authority.get("name", "")
    return {
        "id": safe_id(f"tro-search-{name}"),
        "name": f"{name} TRO source placeholder",
        "kind": "tro",
        "type": "authority_placeholder",
        "enabled": False,
        "url": "",
        "notes": "Placeholder. Replace with GeoJSON/ArcGIS/D-TRO/council open-data URL when available.",
        "authority_id": authority.get("id"),
        "country": authority.get("country"),
        "region": authority.get("region"),
        "created_at": utc_now(),
    }

def ensure_authority_placeholders() -> dict[str, Any]:
    registry = load_authority_registry()
    payload = load_sources()
    sources = payload.get("sources", [])
    existing = {s.get("id") for s in sources}
    added = 0
    for auth in registry.get("authorities", []):
        rec = authority_to_source_record(auth)
        if rec["id"] not in existing:
            sources.append(rec)
            existing.add(rec["id"])
            added += 1
    payload["sources"] = sources
    save_sources(payload)
    return {"state": "done", "authority_count": registry.get("authority_count", len(registry.get("authorities", []))), "placeholders_added": added, "total_sources": len(sources)}

def add_source_record(name: str, kind: str, source_type: str, url: str, notes: str = "", enabled: bool = True, source_id: str = "") -> dict[str, Any]:
    source = {
        "id": source_id or safe_id(f"{kind}-{name}-{int(time.time())}"),
        "name": name,
        "kind": kind,
        "type": source_type,
        "enabled": enabled,
        "url": url.strip(),
        "notes": notes,
        "created_at": utc_now(),
    }
    payload = load_sources()
    payload["sources"] = [s for s in payload.get("sources", []) if s.get("id") != source["id"]] + [source]
    save_sources(payload)
    return source

def delete_source_id(source_id: str) -> bool:
    payload = load_sources()
    before = len(payload.get("sources", []))
    payload["sources"] = [s for s in payload.get("sources", []) if s.get("id") != source_id]
    save_sources(payload)
    return len(payload["sources"]) != before

def empty_discovery() -> dict[str, Any]:
    return {
        "service": APP_NAME,
        "version": APP_VERSION,
        "state": "never_run",
        "candidate_count": 0,
        "promoted_count": 0,
        "candidates": [],
        "last_run_at": None,
    }

def load_discovery() -> dict[str, Any]:
    return read_json(DISCOVERY_PATH, empty_discovery())

def save_discovery(payload: dict[str, Any]) -> None:
    payload["service"] = APP_NAME
    payload["version"] = APP_VERSION
    payload["candidate_count"] = len(payload.get("candidates", []))
    payload["promoted_count"] = len([c for c in payload.get("candidates", []) if c.get("promoted_source_id")])
    write_json_atomic(DISCOVERY_PATH, payload)

def candidate_path(candidate_id: str) -> Path:
    return DISCOVERED_DIR / f"{compact_id(candidate_id)}.json"

def source_keys() -> set[tuple[str, str, str]]:
    return {
        (s.get("authority_id", ""), s.get("type", ""), (s.get("url") or "").strip())
        for s in load_sources().get("sources", [])
    }

def discovery_candidate(authority: dict[str, Any], source_type: str, url: str, confidence: str, notes: str) -> dict[str, Any]:
    authority_id = authority.get("id") or safe_id(authority.get("name", "authority"))
    candidate_id = compact_id(f"{authority_id}-{source_type}-{url or 'configured'}")
    return {
        "id": candidate_id,
        "authority_id": authority_id,
        "authority_name": authority.get("name", ""),
        "country": authority.get("country", ""),
        "region": authority.get("region", ""),
        "name": f"{authority.get('name', 'Authority')} TRO source",
        "kind": "tro",
        "type": source_type,
        "url": url,
        "confidence": confidence,
        "notes": notes,
        "discovered_at": utc_now(),
        "promoted_source_id": "",
    }

def discover_sources() -> dict[str, Any]:
    if not discovery_lock.acquire(blocking=False):
        payload = load_discovery()
        payload["state"] = "already_running"
        return payload
    try:
        ensure_dirs()
        registry = load_authority_registry()
        existing = source_keys()
        candidates: list[dict[str, Any]] = []
        for authority in registry.get("authorities", []):
            authority_id = authority.get("id", "")
            known_url = (authority.get("tro_source_url") or authority.get("url") or "").strip()
            known_type = authority.get("tro_source_type") or ("dtro_api" if known_url and "dtro" in known_url.lower() else "geojson_url")
            if known_url and (authority_id, known_type, known_url) not in existing:
                candidates.append(discovery_candidate(authority, known_type, known_url, "authority_registry", "Source URL was present in the authority registry."))
            if DTRO_API_BASE_URL and (authority_id, "dtro_api", DTRO_API_BASE_URL) not in existing:
                candidates.append(discovery_candidate(authority, "dtro_api", DTRO_API_BASE_URL, "configured_dtro", "Uses the configured D-TRO API base URL with this authority attached as metadata."))
            if not known_url:
                search_url = f"https://www.google.com/search?q={authority.get('name','').replace(' ', '+')}+traffic+regulation+orders+geojson"
                candidates.append(discovery_candidate(authority, "text_only_url", search_url, "manual_review", "Discovery placeholder for manual review; promote only after replacing with a machine-readable source URL."))

        deduped = {}
        for candidate in candidates:
            deduped[candidate["id"]] = candidate
            write_json_atomic(candidate_path(candidate["id"]), candidate)

        payload = {
            "state": "ready",
            "last_run_at": utc_now(),
            "candidates": sorted(deduped.values(), key=lambda c: (c.get("confidence", ""), c.get("authority_name", ""))),
            "discovered_dir": str(DISCOVERED_DIR),
        }
        save_discovery(payload)
        return load_discovery()
    except Exception as exc:
        payload = load_discovery()
        payload.update({"state": "error", "error": str(exc), "last_run_at": utc_now()})
        save_discovery(payload)
        return payload
    finally:
        discovery_lock.release()

def promote_candidate(candidate_id: str, name: str = "", kind: str = "", source_type: str = "", url: str = "", notes: str = "") -> dict[str, Any]:
    discovery = load_discovery()
    candidates = discovery.get("candidates", [])
    candidate = next((c for c in candidates if c.get("id") == candidate_id), None)
    if not candidate:
        return {"state": "not_found", "candidate_id": candidate_id}
    if candidate.get("promoted_source_id"):
        return {"state": "already_promoted", "candidate": candidate}
    final_type = source_type or candidate.get("type", "text_only_url")
    final_url = (url or candidate.get("url") or "").strip()
    if final_type not in ("geojson_url", "arcgis_geojson_url", "dtro_api", "text_only_url"):
        return {"state": "error", "error": "unsupported source_type"}
    if final_type not in ("text_only_url", "dtro_api"):
        parsed = urlparse(final_url)
        if parsed.scheme not in ("http", "https"):
            return {"state": "error", "error": "URL must start with http or https"}
    source = add_source_record(
        name or candidate.get("name") or candidate.get("authority_name") or candidate_id,
        kind or candidate.get("kind") or "tro",
        final_type,
        final_url,
        notes or candidate.get("notes", ""),
        enabled=final_type != "text_only_url",
    )
    source["authority_id"] = candidate.get("authority_id", "")
    payload = load_sources()
    payload["sources"] = [source if s.get("id") == source["id"] else s for s in payload.get("sources", [])]
    save_sources(payload)
    candidate["promoted_source_id"] = source["id"]
    candidate["promoted_at"] = utc_now()
    write_json_atomic(candidate_path(candidate_id), candidate)
    save_discovery(discovery)
    return {"state": "promoted", "candidate": candidate, "source": source, "note": "Run /update or /dtro/sync to merge promoted data into tiles."}

def empty_tracks() -> dict[str, Any]:
    return {"service": APP_NAME, "version": APP_VERSION, "tracks": []}

def load_tracks() -> dict[str, Any]:
    return read_json(TRACKS_PATH, empty_tracks())

def save_tracks(payload: dict[str, Any]) -> None:
    payload["service"] = APP_NAME
    payload["version"] = APP_VERSION
    write_json_atomic(TRACKS_PATH, payload)

def track_geojson_path(track_id: str) -> Path:
    return TRACK_DIR / f"{compact_id(track_id)}.geojson"

def parse_gpx_track(content: bytes, name: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"invalid GPX XML: {exc}") from exc

    lines: list[list[list[float]]] = []
    for segment in root.findall(".//{*}trkseg"):
        coords = []
        for point in segment.findall("{*}trkpt"):
            lat = point.get("lat")
            lon = point.get("lon")
            if lat is None or lon is None:
                continue
            try:
                coords.append([float(lon), float(lat)])
            except ValueError:
                continue
        if len(coords) >= 2:
            lines.append(coords)

    if not lines:
        route_coords = []
        for point in root.findall(".//{*}rtept"):
            lat = point.get("lat")
            lon = point.get("lon")
            if lat is None or lon is None:
                continue
            try:
                route_coords.append([float(lon), float(lat)])
            except ValueError:
                continue
        if len(route_coords) >= 2:
            lines.append(route_coords)

    if not lines:
        raise ValueError("GPX must contain at least one track or route with two points")

    features = []
    for idx, coords in enumerate(lines, start=1):
        features.append({
            "type": "Feature",
            "properties": {"name": name, "segment": idx, "source": "uploaded_gpx_track"},
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    return {"type": "FeatureCollection", "features": features}

def save_gpx_track(name: str, filename: str, content: bytes) -> dict[str, Any]:
    ensure_dirs()
    display_name = name.strip() or Path(filename or "").stem or f"GPX Track {int(time.time())}"
    track_id = compact_id(f"track-{display_name}-{int(time.time())}", 72)
    geojson = parse_gpx_track(content, display_name)
    path = track_geojson_path(track_id)
    write_json_atomic(path, geojson)
    track = {
        "id": track_id,
        "name": display_name,
        "filename": filename,
        "feature_count": len(geojson.get("features", [])),
        "point_count": sum(len(f.get("geometry", {}).get("coordinates", [])) for f in geojson.get("features", [])),
        "uploaded_at": utc_now(),
        "geojson_path": str(path),
        "traccar_tile_url": f"{PUBLIC_BASE_URL}/tiles/tracks/{track_id}/{{z}}/{{x}}/{{y}}.png",
        "style": {"colour": "purple", "line": "dotted"},
    }
    payload = load_tracks()
    payload["tracks"] = [t for t in payload.get("tracks", []) if t.get("id") != track_id] + [track]
    save_tracks(payload)
    return track

def delete_track_id(track_id: str) -> bool:
    payload = load_tracks()
    before = len(payload.get("tracks", []))
    payload["tracks"] = [t for t in payload.get("tracks", []) if t.get("id") != track_id]
    save_tracks(payload)
    path = track_geojson_path(track_id)
    if path.exists():
        path.unlink()
    return len(payload["tracks"]) != before

def load_track_features(track_id: str) -> list[dict[str, Any]]:
    payload = load_tracks()
    if not any(t.get("id") == track_id for t in payload.get("tracks", [])):
        return []
    return read_json(track_geojson_path(track_id), {"type": "FeatureCollection", "features": []}).get("features", [])

def fetch_osm_boats() -> dict[str, Any]:
    r = requests.get(OVERPASS_URL, params={"data": OVERPASS_QUERY}, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"}, timeout=1200)
    r.raise_for_status()
    osm = r.json()
    features = []
    for el in osm.get("elements", []):
        if el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        coords = [[p["lon"], p["lat"]] for p in geom if "lon" in p and "lat" in p]
        if len(coords) < 2:
            continue
        tags = el.get("tags", {})
        features.append({"type": "Feature", "properties": {
            "osm_id": el.get("id"), "name": tags.get("name",""), "designation": tags.get("designation",""),
            "highway": tags.get("highway",""), "surface": tags.get("surface",""), "source": "OpenStreetMap",
            "source_id": "osm-boats", "confidence": "osm", "tro_status": "unchecked"
        }, "geometry": {"type": "LineString", "coordinates": coords}})
    return {"type": "FeatureCollection", "features": features}

def source_fetch_path(source: dict[str, Any]) -> Path:
    return FETCHED_DIR / source.get("kind", "unknown") / f"{safe_id(source.get('id','source'))}.geojson"

def dtro_headers() -> dict[str, str]:
    headers = {"User-Agent": f"{APP_NAME}/{APP_VERSION}", "Accept": "application/json"}
    if DTRO_API_KEY:
        if DTRO_API_AUTH_PREFIX:
            headers[DTRO_API_AUTH_HEADER] = f"{DTRO_API_AUTH_PREFIX} {DTRO_API_KEY}"
        else:
            headers[DTRO_API_AUTH_HEADER] = DTRO_API_KEY
    return headers

def fetch_dtro_api(source: dict[str, Any]) -> dict[str, Any]:
    base_url = (source.get("url") or DTRO_API_BASE_URL).strip()
    if not base_url:
        raise RuntimeError("DTRO_API_BASE_URL is not configured")
    r = requests.get(base_url, headers=dtro_headers(), timeout=900)
    r.raise_for_status()
    payload = r.json()
    # If the configured API returns a FeatureCollection, use it directly.
    if payload.get("type") == "FeatureCollection":
        return payload
    # Otherwise store raw features if common key exists.
    for key in ("features", "data", "items", "results"):
        if isinstance(payload.get(key), list):
            return {"type": "FeatureCollection", "features": payload[key]}
    return {"type": "FeatureCollection", "features": []}

def dtro_configured_source() -> dict[str, Any]:
    return {
        "id": "dtro-configured-api",
        "name": "Configured D-TRO API",
        "kind": "tro",
        "type": "dtro_api",
        "enabled": True,
        "url": DTRO_API_BASE_URL,
        "notes": "Runtime D-TRO configuration source.",
    }

def test_dtro_api() -> dict[str, Any]:
    if not DTRO_API_BASE_URL:
        return {"state": "error", "error": "DTRO_API_BASE_URL is not configured"}
    started = time.time()
    try:
        r = requests.get(DTRO_API_BASE_URL, headers=dtro_headers(), timeout=30)
        sample = None
        content_type = r.headers.get("content-type", "")
        if "json" in content_type.lower():
            payload = r.json()
            if isinstance(payload, dict):
                sample = {k: payload.get(k) for k in list(payload.keys())[:5]}
        return {
            "state": "ok" if r.ok else "error",
            "status_code": r.status_code,
            "elapsed_seconds": round(time.time() - started, 2),
            "base_url": DTRO_API_BASE_URL,
            "auth_header": DTRO_API_AUTH_HEADER,
            "auth_prefix": DTRO_API_AUTH_PREFIX,
            "content_type": content_type,
            "sample": sample,
        }
    except Exception as exc:
        return {"state": "error", "error": str(exc), "base_url": DTRO_API_BASE_URL}

def sync_dtro_api() -> dict[str, Any]:
    ensure_dirs()
    source = dtro_configured_source()
    payload = fetch_dtro_api(source)
    write_json_atomic(source_fetch_path(source), payload)
    source_record = add_source_record("Configured D-TRO API", "tro", "dtro_api", DTRO_API_BASE_URL, "Synced from runtime D-TRO configuration.", enabled=True, source_id="dtro-configured-api")
    result = update_all()
    return {"state": "synced", "source": source_record, "feature_count": len(payload.get("features", [])), "update": result}

def fetch_external_sources() -> dict[str, Any]:
    stats = {"fetched": 0, "skipped": 0, "errors": []}
    for src in load_sources().get("sources", []):
        if not src.get("enabled", True):
            stats["skipped"] += 1
            continue
        stype = src.get("type")
        try:
            if stype == "overpass_osm_boats":
                write_json_atomic(OSM_GEOJSON_PATH, fetch_osm_boats())
                stats["fetched"] += 1
            elif stype in ("geojson_url", "arcgis_geojson_url"):
                url = src.get("url", "").strip()
                if not url:
                    stats["skipped"] += 1
                    continue
                r = requests.get(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}", "Accept":"application/json"}, timeout=600)
                r.raise_for_status()
                write_json_atomic(source_fetch_path(src), r.json())
                stats["fetched"] += 1
            elif stype == "dtro_api":
                write_json_atomic(source_fetch_path(src), fetch_dtro_api(src))
                stats["fetched"] += 1
            elif stype in ("manual_upload", "authority_placeholder", "text_only_url"):
                stats["skipped"] += 1
                continue
            else:
                stats["errors"].append({"source": src.get("id"), "error": f"unsupported type {stype}"})
        except Exception as exc:
            stats["errors"].append({"source": src.get("id"), "error": str(exc)})
    return stats

def feature_text(feature: dict[str, Any]) -> str:
    return json.dumps(feature.get("properties", {}), ensure_ascii=False).lower()

def is_boat_like(feature: dict[str, Any]) -> bool:
    text = feature_text(feature)
    return "byway_open_to_all_traffic" in text or "byway open to all traffic" in text or "boat" in text or "public byway" in text or " byway" in text

def is_tro_like(feature: dict[str, Any]) -> bool:
    text = feature_text(feature)
    return any(t in text for t in ["traffic regulation order","tro","temporary traffic regulation","prohibition","restriction","closure","no motor","motor vehicle","seasonal","experimental traffic","traffic order"])

def normalise_feature(feature: dict[str, Any], kind: str, source_name: str, source_id: str) -> list[dict[str, Any]]:
    geom = feature.get("geometry") or {}
    props = feature.get("properties") or {}
    lines = []
    if geom.get("type") == "LineString":
        lines = [geom.get("coordinates") or []]
    elif geom.get("type") == "MultiLineString":
        lines = geom.get("coordinates") or []
    elif geom.get("type") == "Polygon":
        rings = geom.get("coordinates") or []
        lines = rings[:1]
    elif geom.get("type") == "MultiPolygon":
        for poly in geom.get("coordinates") or []:
            if poly:
                lines.append(poly[0])
    out = []
    for coords in lines:
        clean = []
        for c in coords:
            if len(c) >= 2:
                try:
                    clean.append([float(c[0]), float(c[1])])
                except Exception:
                    pass
        if len(clean) < 2:
            continue
        if kind == "byway" and source_name != "OpenStreetMap" and not is_boat_like(feature):
            continue
        confidence = "matched_keywords" if (kind == "tro" and is_tro_like(feature)) else ("low" if kind == "tro" else "authority_or_manual")
        name = props.get("name") or props.get("NAME") or props.get("roadName") or props.get("road_name") or props.get("street") or props.get("PROW_REF") or props.get("reference") or ""
        designation = props.get("designation") or props.get("DESIGNATION") or props.get("type") or props.get("Type") or ""
        out.append({"type":"Feature","properties":{
            "name": str(name or ""), "designation": str(designation or ""), "source": source_name,
            "source_id": source_id, "source_kind": kind, "confidence": confidence, "raw_properties": props
        }, "geometry":{"type":"LineString","coordinates":clean}})
    return out

def load_geojson_features(path: Path, kind: str, source_name: str, source_id: str) -> list[dict[str, Any]]:
    geojson = read_json(path, {"type":"FeatureCollection","features":[]})
    out = []
    for feat in geojson.get("features", []):
        out.extend(normalise_feature(feat, kind, source_name, source_id))
    return out

def dedupe_features(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for f in features:
        coords = f.get("geometry", {}).get("coordinates", [])
        if not coords:
            continue
        key = tuple((round(c[0],6), round(c[1],6)) for c in coords[:3]+coords[-3:])
        props = f.get("properties", {})
        k = (key, props.get("source_id",""), props.get("name",""))
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out

def merge_sources() -> tuple[dict[str, Any], dict[str, Any]]:
    byways, tros = [], []
    byways += load_geojson_features(OSM_GEOJSON_PATH, "byway", "OpenStreetMap", "osm-boats")
    for p in sorted((FETCHED_DIR/"byway").glob("*.geojson")):
        byways += load_geojson_features(p, "byway", "external-byway-source", p.stem)
    for p in sorted(MANUAL_BYWAY_DIR.glob("*.geojson")) + sorted(MANUAL_BYWAY_DIR.glob("*.json")):
        byways += load_geojson_features(p, "byway", "manual-byway-upload", p.stem)
    for p in sorted((FETCHED_DIR/"tro").glob("*.geojson")):
        tros += load_geojson_features(p, "tro", "external-tro-source", p.stem)
    for p in sorted(MANUAL_TRO_DIR.glob("*.geojson")) + sorted(MANUAL_TRO_DIR.glob("*.json")):
        tros += load_geojson_features(p, "tro", "manual-tro-upload", p.stem)
    byways_fc = {"type":"FeatureCollection","features":dedupe_features(byways)}
    tros_fc = {"type":"FeatureCollection","features":dedupe_features(tros)}
    write_json_atomic(MERGED_BYWAYS_PATH, byways_fc)
    write_json_atomic(MERGED_TROS_PATH, tros_fc)
    return byways_fc, tros_fc

def bbox(feature):
    coords = feature.get("geometry", {}).get("coordinates", [])
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return min(xs), min(ys), max(xs), max(ys)

def bbox_intersects(a,b,buf):
    return not (a[2]+buf < b[0] or a[0]-buf > b[2] or a[3]+buf < b[1] or a[1]-buf > b[3])

def approx_xy(lon, lat, ref_lat):
    return lon*111320.0*math.cos(math.radians(ref_lat)), lat*110540.0

def point_seg_dist(p,a,b,ref_lat):
    px,py = approx_xy(p[0],p[1],ref_lat)
    ax,ay = approx_xy(a[0],a[1],ref_lat)
    bx,by = approx_xy(b[0],b[1],ref_lat)
    dx,dy=bx-ax,by-ay
    if dx==0 and dy==0:
        return math.hypot(px-ax,py-ay)
    t=max(0,min(1,((px-ax)*dx+(py-ay)*dy)/(dx*dx+dy*dy)))
    return math.hypot(px-(ax+t*dx),py-(ay+t*dy))

def line_distance(a,b):
    if not a or not b:
        return float("inf")
    lats=[p[1] for p in (a[:10]+b[:10])]
    ref=sum(lats)/len(lats)
    best=float("inf")
    for p in a:
        for i in range(len(b)-1):
            best=min(best, point_seg_dist(p,b[i],b[i+1],ref))
            if best <= 1:
                return best
    for p in b:
        for i in range(len(a)-1):
            best=min(best, point_seg_dist(p,a[i],a[i+1],ref))
            if best <= 1:
                return best
    return best

def classify_tro(tro):
    text=feature_text(tro)
    if any(t in text for t in ["closure","closed","prohibition","no motor","motor vehicle","traffic regulation order"]):
        return "restricted"
    return "possible"

def analyse_tros(byways, tros):
    tro_items=[]
    for tro in tros.get("features", []):
        try:
            tro_items.append((tro,bbox(tro)))
        except Exception:
            pass
    buf_deg=TRO_MATCH_METRES/111320.0
    analysed=[]
    for by in byways.get("features", []):
        props=by.get("properties", {}).copy()
        by_coords=by.get("geometry", {}).get("coordinates", [])
        try:
            by_bbox=bbox(by)
        except Exception:
            continue
        matches=[]
        status="clear"
        for tro,tb in tro_items:
            if not bbox_intersects(by_bbox,tb,buf_deg):
                continue
            dist=line_distance(by_coords,tro.get("geometry",{}).get("coordinates",[]))
            if dist <= TRO_MATCH_METRES:
                st=classify_tro(tro)
                if st=="restricted":
                    status="restricted"
                elif status!="restricted":
                    status="possible"
                tp=tro.get("properties", {})
                matches.append({"distance_m":round(dist,1),"status":st,"source":tp.get("source",""),"source_id":tp.get("source_id",""),"name":tp.get("name","")})
        props["tro_status"]=status
        props["tro_match_count"]=len(matches)
        props["tro_matches"]=matches[:10]
        props["tro_checked_at"]=utc_now()
        analysed.append({"type":"Feature","properties":props,"geometry":by.get("geometry")})
    fc={"type":"FeatureCollection","features":analysed}
    write_json_atomic(ANALYSED_BYWAYS_PATH, fc)
    return fc

def lonlat_to_pixel(lon,lat,z):
    lat=max(min(lat,85.05112878),-85.05112878)
    n=2**z
    x=(lon+180)/360*n*256
    r=math.radians(lat)
    y=(1-math.log(math.tan(r)+1/math.cos(r))/math.pi)/2*n*256
    return x,y

def lonlat_to_tile(lon,lat,z):
    px,py=lonlat_to_pixel(lon,lat,z)
    return int(px//256), int(py//256)

def feature_tile_bounds(f,z):
    coords=f.get("geometry",{}).get("coordinates",[])
    if not coords:
        return None
    xs=[]
    ys=[]
    for lon,lat in coords:
        tx,ty=lonlat_to_tile(lon,lat,z)
        xs.append(tx)
        ys.append(ty)
    return min(xs),min(ys),max(xs),max(ys)

def spatial_index(features,z):
    idx={}
    for f in features:
        b=feature_tile_bounds(f,z)
        if not b:
            continue
        minx,miny,maxx,maxy=b
        for x in range(max(0,minx-1),maxx+2):
            for y in range(max(0,miny-1),maxy+2):
                idx.setdefault((x,y),[]).append(f)
    return idx

def colour(f, coloured):
    p=f.get("properties",{})
    if coloured:
        st=p.get("tro_status","unknown")
        return {"restricted":(220,38,38,240),"possible":(245,158,11,240),"clear":(22,163,74,235)}.get(st,(107,114,128,230))
    src=p.get("source","")
    if src=="OpenStreetMap":
        return (215,25,28,230)
    if "manual" in src:
        return (255,140,0,235)
    return (0,120,255,235)

def render_tile(z,x,y,features,coloured):
    img=Image.new("RGBA",(256,256),(0,0,0,0))
    draw=ImageDraw.Draw(img)
    ox,oy=x*256,y*256
    width=2 if z<11 else 3 if z<15 else 5
    for f in features:
        pts=[]
        for c in f.get("geometry",{}).get("coordinates",[]):
            px,py=lonlat_to_pixel(c[0],c[1],z)
            pts.append((px-ox,py-oy))
        if len(pts)<2:
            continue
        if not any(-64<=px<=320 and -64<=py<=320 for px,py in pts):
            continue
        draw.line(pts, fill=colour(f,coloured), width=width)
        if z>=14:
            draw.line(pts, fill=(255,255,255,165), width=max(1,width//2))
    buf=io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def render_track_tile(z, x, y, features):
    img=Image.new("RGBA",(256,256),(0,0,0,0))
    draw=ImageDraw.Draw(img)
    ox,oy=x*256,y*256
    width=3 if z<14 else 5
    dash=10 if z<14 else 14
    gap=7 if z<14 else 10
    purple=(147,51,234,245)
    halo=(255,255,255,170)
    for f in features:
        coords=f.get("geometry",{}).get("coordinates",[])
        pts=[]
        for c in coords:
            px,py=lonlat_to_pixel(c[0],c[1],z)
            pts.append((px-ox,py-oy))
        if len(pts)<2:
            continue
        if not any(-64<=px<=320 and -64<=py<=320 for px,py in pts):
            continue
        for a,b in zip(pts, pts[1:]):
            draw_dotted_segment(draw, a, b, dash, gap, halo, width + 3)
            draw_dotted_segment(draw, a, b, dash, gap, purple, width)
    buf=io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def draw_dotted_segment(draw, a, b, dash, gap, fill, width):
    ax,ay=a
    bx,by=b
    dx=bx-ax
    dy=by-ay
    dist=math.hypot(dx,dy)
    if dist <= 0:
        return
    ux=dx/dist
    uy=dy/dist
    pos=0.0
    while pos < dist:
        end=min(pos+dash, dist)
        draw.line((ax+ux*pos, ay+uy*pos, ax+ux*end, ay+uy*end), fill=fill, width=width)
        pos += dash + gap

def clear_tiles():
    for d in [PLAIN_TILE_DIR, COLOUR_TILE_DIR]:
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

def pregenerate(features):
    clear_tiles()
    stats={"plain":{"total_tiles":0,"per_zoom":{}},"coloured":{"total_tiles":0,"per_zoom":{}}}
    for z in range(MIN_ZOOM, PREGENERATE_MAX_ZOOM+1):
        idx=spatial_index(features,z)
        for tile_dir, coloured, key in [(PLAIN_TILE_DIR,False,"plain"),(COLOUR_TILE_DIR,True,"coloured")]:
            count=0
            for (x,y), feats in idx.items():
                p=tile_dir/str(z)/str(x)/f"{y}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(render_tile(z,x,y,feats,coloured))
                count+=1
            stats[key]["per_zoom"][str(z)]=count
            stats[key]["total_tiles"]+=count
    return stats

def update_all():
    if not update_lock.acquire(blocking=False):
        return {"service":APP_NAME,"state":"already_running"}
    started=time.time()
    try:
        ensure_dirs()
        save_status({"service":APP_NAME,"version":APP_VERSION,"state":"updating","stage":"fetching_sources","started_at":utc_now()})
        fetch_stats=fetch_external_sources()
        save_status({"service":APP_NAME,"version":APP_VERSION,"state":"updating","stage":"merging_sources","fetch_stats":fetch_stats})
        byways,tros=merge_sources()
        save_status({"service":APP_NAME,"version":APP_VERSION,"state":"updating","stage":"matching_tros","byway_count":len(byways["features"]),"tro_count":len(tros["features"])})
        analysed=analyse_tros(byways,tros)
        features=analysed.get("features",[])
        save_status({"service":APP_NAME,"version":APP_VERSION,"state":"updating","stage":"pregenerating_tiles","feature_count":len(features)})
        tile_stats=pregenerate(features)
        counts={}
        for f in features:
            st=f.get("properties",{}).get("tro_status","unknown")
            counts[st]=counts.get(st,0)+1
        status={"service":APP_NAME,"version":APP_VERSION,"state":"ready","feature_count":len(features),"tro_feature_count":len(tros["features"]),
                "tro_status_counts":counts,"fetch_stats":fetch_stats,"authority_count":load_authority_registry().get("authority_count",0),
                "elapsed_seconds":round(time.time()-started,1),"tile_stats":tile_stats,
                "traccar_coloured_tile_url":f"{PUBLIC_BASE_URL}/tiles/boats-coloured/{{z}}/{{x}}/{{y}}.png",
                "traccar_plain_tile_url":f"{PUBLIC_BASE_URL}/tiles/boats/{{z}}/{{x}}/{{y}}.png",
                "traccar_gpx_tracks":load_tracks().get("tracks", []),
                "legend":{"green":"no imported TRO match","amber":"possible TRO match","red":"likely restriction/closure","grey":"unknown"},
                "legal_note":"Overlay aid only. Check local authority Definitive Maps and TROs."}
        save_status(status)
        return load_status()
    except Exception as exc:
        save_status({"service":APP_NAME,"version":APP_VERSION,"state":"error","error":str(exc)})
        return load_status()
    finally:
        update_lock.release()

def tile_bytes(tile_dir,z,x,y,coloured):
    if z<0 or z>22:
        return None
    p=tile_dir/str(z)/str(x)/f"{y}.png"
    if p.exists():
        return p.read_bytes()
    if z<=MAX_ZOOM:
        feats=read_json(ANALYSED_BYWAYS_PATH, {"type":"FeatureCollection","features":[]}).get("features",[])
        data=render_tile(z,x,y,feats,coloured)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return data
    img=Image.new("RGBA",(256,256),(0,0,0,0))
    buf=io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def track_tile_bytes(track_id,z,x,y):
    if z<0 or z>22:
        return None
    feats=load_track_features(track_id)
    if not feats:
        return None
    return render_track_tile(z,x,y,feats)

def png_response(data):
    return Response(content=data, media_type="image/png", headers={"Cache-Control":"public, max-age=86400","Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET, HEAD, OPTIONS","Access-Control-Allow-Headers":"*"})


HOTSAUSAGE_LOGO_DATA_URI = "data:image/svg+xml;base64,PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0idXRmLTgiPz4KPCEtLSBHZW5lcmF0b3I6IEFkb2JlIElsbHVzdHJhdG9yIDI3LjAuMCwgU1ZHIEV4cG9ydCBQbHVnLUluIC4gU1ZHIFZlcnNpb246IDYuMDAgQnVpbGQgMCkgIC0tPgo8c3ZnIHZlcnNpb249IjEuMSIgaWQ9IkxheWVyXzEiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyIgeG1sbnM6eGxpbms9Imh0dHA6Ly93d3cudzMub3JnLzE5OTkveGxpbmsiIHg9IjBweCIgeT0iMHB4IgoJIHZpZXdCb3g9IjAgMCAxMzEyIDEwODAiIHN0eWxlPSJlbmFibGUtYmFja2dyb3VuZDpuZXcgMCAwIDEzMTIgMTA4MDsiIHhtbDpzcGFjZT0icHJlc2VydmUiPgo8c3R5bGUgdHlwZT0idGV4dC9jc3MiPgoJLnN0MHtmaWxsOiNEMTBEMEQ7fQoJLnN0MXtmaWxsOiMzQzNDM0M7fQo8L3N0eWxlPgo8Zz4KCTxwYXRoIGNsYXNzPSJzdDAiIGQ9Ik02NDEuNiw3NzkuNzhjNS43NywwLDExLjQtMC4yNSwxNi45OSwwLjA5YzMuNiwwLjIyLDQuNzEsMi41MSwyLjQ5LDUuNThjLTEuNjksMi4zNC0zLjQzLDQuODItNS42OCw2LjUzCgkJYy01LjEsMy44Ni03LjM2LDguNzMtNi44NiwxNS4wNGMwLjQ4LDUuOTQtMi41MSw3Ljk1LTguMjIsOS4yOGMtNS41NSwxLjI5LTEwLjU1LDQuOTUtMTUuNzYsNy42M2MtMi4yOSwxLjE4LTQuNTEsMi40OS02Ljc2LDMuNzQKCQljLTAuNy0yLjI3LTEuNzQtNC40OS0yLjAyLTYuODFjLTAuMzUtMi45Ny0wLjA5LTYuMDEtMC4wOS04LjI4Yy03LjMxLDEuMjUtMTUuMDEsMy4wMy0yMi44MiwzLjc4Yy05LjM1LDAuOS0xOC4wOC0xLjk4LTI2LjA5LTYuNzEKCQljLTUyLjk2LTMxLjMxLTg5LjIxLTc2LjM0LTEwOS43LTEzNC4yYy0xMi42MS0zNS42MS0xNi4zMS03Mi4zLTExLjktMTA5LjY3YzQuNDQtMzcuNTksMTcuMTItNzIuMzgsMzcuNjItMTA0LjMKCQljMi40OC0zLjg2LDUuMS00LjksOS4yNy0zLjQ3YzIuNjMsMC45LDUuNDIsMS4zMyw5LjA3LDAuNjZjLTEuMDItMS4yOS0xLjkzLTIuNjgtMy4wNy0zLjg1Yy00LjktNS4wNS05Ljg2LTEwLjA1LTE0LjgyLTE1LjA2CgkJYy0xNC4yOS0xNC40My0xNC40OS0zMS4yNy03Ljk5LTQ4Ljk5YzEuOTEtNS4yMSw0Ljc3LTEwLjEyLDcuNjEtMTQuOTJjMTQuNzQtMjQuODYsMTMuODItNDIuMTUtNS4xNS02OC45MgoJCWMtNy42My0xMC43Ni0xNy4zMy0xOS40OC0yOS4xNi0yNS42M2MtMC44Ni0wLjQ1LTEuNTgtMS4xNi0zLjA3LTIuMjljMTAuNDYtNC44NywxOS42Ny0yLjE2LDI4LjQ4LDIuMDYKCQljNC40LDIuMTEsOC4yOCw1LjQxLDEyLjE4LDguNDZjNC40LDMuNDQsNi41OCw3LjcyLDYuODYsMTMuNzhjMC42NCwxMy43NSw4LjU0LDIzLjkyLDE4LjYzLDMyLjQ2YzExLDkuMzEsMTAuOTMsOS4xNCw0LjkzLDIyLjM2CgkJYy0yLjEsNC42My00LjM0LDkuNjQtNC42MiwxNC41OGMtMS4xLDE5LjU3LDEyLjMxLDI5Ljg0LDMwLjk4LDIzLjk2YzEwLjg4LTMuNDMsMjAuMTYtOS42NiwyNy41Ny0xOC4zOAoJCWM3LjM1LTguNjYsNy4xMi0xNi45OS0yLjAyLTIzLjZjLTcuNjEtNS41LTE2LjQ1LTkuMzctMjQuOTMtMTMuNTdjLTUuMTgtMi41Ni0xMC43LTQuNDUtMTYuMTItNi41MgoJCWMtMjQtOS4xNS0yOS4zMy0yOC40OS0yNS41OS01Mi4yMWMxLjE3LTcuNCw0LTE0LjkzLDcuNzEtMjEuNDZjOC44LTE1LjQ3LDExLjU0LTMxLjczLDkuMzMtNDkuMTNjLTAuMzItMi41NS0wLjI5LTUuMTUtMC40NS04LjE1CgkJYzEyLjMsNS44OCwyMS4zMSwyNC4yMSwxOS43LDM5LjM3Yy0wLjU0LDUuMTMtMS44OCwxMC4xOC0yLjc4LDE1LjI4Yy0zLjA4LDE3LjU4LTAuMDIsMzMuNDcsMTMuNTEsNDYuMDkKCQljOS44NSw5LjE5LDMyLjQyLDExLjQsNDQuMDIsNC43MWM2LjkzLTMuOTksMTAuNy0xMS45OSw4LjE1LTIxLjY3Yy0zLjMtMTIuNTQtNy43LTI0LjgzLTEyLjItMzcuMDEKCQljLTQuOTQtMTMuMzYtMTEuMDItMjYuMzEtMTUuNjktMzkuNzZjLTQuMzMtMTIuNDctMC4yMS0yMy42LDcuODYtMzMuNTRjMy4zOS00LjE3LDYuODctOC4zNCw5LjY4LTEyLjkKCQljNi4zNy0xMC4zMiw1LjA5LTIwLjc5LTMuMTUtMjkuNmMtMi45Ny0zLjE4LTYuMTYtNi4xNy04Ljk5LTkuNDdjLTYuMzktNy40Ni02Ljk5LTEzLjc3LTEuNzUtMjIuMTNjMi4yOC0zLjY0LDUuMzctNi44LDguMTgtMTAuMQoJCWM1LjQ2LTYuNDEsMTAuOTgtMTIuNzYsMTcuMDMtMTkuNzljLTIuMDUsNS4xNy0zLjc4LDkuOTctNS44NCwxNC42M2MtNi45OSwxNS44Ny01LjA1LDI0Ljk3LDcuOTgsMzYuMzYKCQljMS43MSwxLjUsMy40LDMuMSw1LjMzLDQuMjdjMTQuMjYsOC42OCwxOC45NCwyMS40MiwxNy4xLDM3LjYzYy0xLjE2LDEwLjIyLTIuODgsMjAuOTEsMi4xNywzMC41OGMzLjksNy40Niw4LjgxLDE0LjQyLDEzLjcsMjEuMwoJCWMwLjk5LDEuMzksNCwyLjE3LDUuOTEsMS45M2MxMy44Ny0xLjc3LDIzLjU3LTEyLjE1LDI0Ljg0LTI2LjE1YzEuMTktMTMuMTEtMS42My0yNS43MS02LjA5LTM3Ljc3CgkJYy00Ljk2LTEzLjQ0LTIuOTgtMjUuNTgsNC4wNy0zNy40M2MzLjA3LTUuMTYsNi4xNC0xMC4zMSw5LjIyLTE1LjQ2YzYuMjgtMTAuNSw2LjMxLTIxLjIzLDAuOTUtMzIuMDQKCQljLTIuNzYtNS41Ni01LjQyLTExLjIyLTguOC0xNi4zOWMtNC4xMi02LjMtNS4wNC0xMi44MS0yLjItMTkuNDljMy4xOS03LjUsNy4xNC0xNC42NywxMC43Ni0yMS45OWMwLjYzLDAuMTQsMS4yNSwwLjI5LDEuODgsMC40MwoJCWMwLjIzLDQuMzcsMC42MSw4Ljc0LDAuNjcsMTMuMTFjMC4xMyw4LjksMy4xNSwxNi4zNCw5LjI5LDIyLjk5YzUuMzIsNS43NSw5Ljg1LDEyLjI3LDE0LjM2LDE4LjcyCgkJYzguMDQsMTEuNSwxMS4wNSwyNC4yNiw5LjU5LDM4LjNjLTAuODYsOC4yMi0wLjgzLDE2LjU1LTAuOTMsMjQuODNjLTAuMSw4LjAzLDIuOTUsMTQuOTYsOC41LDIwLjcxYzMuMDEsMy4xMyw2LjI5LDYsOS40Nyw4Ljk2CgkJYzEyLjkxLDEyLjAxLDE1LjE5LDI0LjM4LDcuMyw0MC4yNGMtNiwxMi4wNS0xNi4xNSwyMC4zLTI2LjMsMjguNTZjLTEwLjc1LDguNzUtMjIuMDYsMTYuOTktMjksMjkuNDkKCQljLTcuODcsMTQuMTgtNy45NywyOC43NC0wLjQzLDQyLjc5YzYuOTksMTMuMDMsMTkuMjMsMTcuOCwzMy40NiwxNy4yOWMxNC4zOC0wLjUxLDIzLjUtOS4xLDI4LjI0LTIxLjc4CgkJYzYuOTItMTguNTIsNS41NS0zNi43OS01Ljg1LTUzLjM1Yy0zLjY1LTUuMy05LjE3LTkuMzItMTQuMzYtMTQuNDVjMy43OS00LjE1LDguMDYtOS4xLDEyLjYyLTEzLjc2CgkJYzE0Ljg0LTE1LjE4LDE3LjU2LTMyLjM3LDguMjMtNTEuMjZjLTIuNDgtNS4wMi0xLjQxLTguMTMsMi43NC0xMC45OWMzLjc1LTIuNTgsNy4zOC01LjM2LDExLjI2LTcuNzEKCQljMTcuODEtMTAuNzksMjguMy0yNi4yNSwzMC41Ni00Ny4xMWMwLjE2LTEuNTIsMC42NC0zLjAxLDAuOTctNC41MmMwLjY3LTAuMTYsMS4zNS0wLjMxLDIuMDItMC40N2MxLjk2LDUuNjUsNC4yNywxMS4yLDUuODEsMTYuOTYKCQljNC40MiwxNi41OCwwLjgyLDMxLjEyLTExLjY0LDQzLjI3Yy0zLjI2LDMuMTctNi40LDYuNTItOS4yMiwxMC4wN2MtOC41OSwxMC44LTkuNywyNC4zMy0xLjgzLDM1Ljc2CgkJYzQuNzUsNi45MSwxMS4wOSwxMi43NiwxNi45NCwxOC44OGMzLjE0LDMuMjgsNi43Miw2LjE2LDEwLjE1LDkuMTVjMTkuMywxNi44LDIxLjMyLDM1LjQxLDEwLjgyLDU4LjM1CgkJYy01LjMsMTEuNTgtMTMuMjMsMjEuOTMtMTkuNjksMzNjLTMuNTQsNi4wNy02Ljk1LDEyLjI5LTkuNTgsMTguNzhjLTcuNjgsMTguOTctMS43MiwzMC40MiwxNy43NSwzOC45OAoJCWMyMC40OCw5LDM1LjYyLTAuNDQsNDAuMzQtMjMuMTdjMy4xMS0xNC45NiwwLjgxLTI5LjA0LTMuNjQtNDMuMzhjLTUuODEtMTguNzQtOC41NS0zNy45Mi00LjYxLTU3LjYzCgkJYzQuNjgtMjMuNDYsMTktMzguNTgsNDAuNDgtNDcuMzdjNy41Ni0zLjA5LDE1LjY1LTQuODcsMjQuMDQtNi4xOWMtMC45OSwwLjg2LTEuODUsMi4wNC0yLjk5LDIuNTQKCQljLTE4LjIsNy45Ni0yNS44MSwyMy4zNi0yOS44OSw0MS41N2MtNS4yMiwyMy4zMywxLjU0LDQyLjc0LDE3LjgsNTkuNjljOS44OSwxMC4zMiwxOC44MSwyMS4zMywyNC4yMiwzNS4wMQoJCWMxNS40MSwzOC45OSwzLjE3LDkwLjI4LTI4LjYsMTE3Ljg1Yy0xLjY2LDEuNDQtMy4yNywyLjk2LTQuOSw0LjQ0YzAuMywwLjYsMC42LDEuMjEsMC45LDEuODFjMy43Mi0xLjUzLDcuNjMtMi43MywxMS4xMi00LjY3CgkJYzQuNTktMi41NSw3LjM3LTEuNSwxMC4xMiwyLjg0YzE5LjU5LDMwLjg4LDMyLjgzLDY0LjIxLDM3LjI4LDEwMC41NGMxMC4yMyw4My41OS0xNS4zOSwxNTUuMTEtNzUuMTYsMjEzLjkyCgkJYy0xMy43MiwxMy41LTMwLjE1LDI0LjYxLTQ2LjUsMzQuOTdjLTE1LjM0LDkuNzEtMzIuNDUsOS4wNy00OC43MiwwLjg3Yy0zLjE2LTEuNTktNS4yMS01LjQtOC40NC04LjkzCgkJYy0wLjMyLDMuMzItMC40NCw2LjEtMC44OSw4LjgyYy0xLjE2LDYuOTItMi4xMywxMy44OS0zLjg0LDIwLjY4Yy0xLjE1LDQuNTQtNC4xLDQuODEtNy4xMywxLjE3Yy0wLjkyLTEuMTEtMS45OC0yLjIxLTIuNTUtMy41CgkJYy0zLjI0LTcuMzctNy43Ny0xMi43Ny0xNi40Ni0xNC4yNWMtMS45Ni0wLjMzLTQuOTMtNS4xNy00LjQ5LTcuMzRjMS41NS03Ljc0LTAuODMtMTQuMjMtNC4wNi0yMC44NQoJCWMtMC45MS0xLjg2LTAuODctNC4xOC0xLjI2LTYuM2MxLjk4LTAuMTIsNC4wMi0wLjYsNS45My0wLjI4YzYuNCwxLjA3LDEyLjc2LDIuNDMsMTkuNSwzLjc1Yy0wLjAzLDAuMiwwLjM5LTAuNjQsMC4xNy0xLjIzCgkJYy04LjkyLTIzLjQ2LTAuODItNDcuNDcsMjAuOTUtNjAuNTVjMTUuNjItOS4zOCwyOS4zLTIwLjgzLDQwLjg3LTM0LjkxYzY2Ljc1LTgxLjIyLDI2LjAyLTIwNi42NC03NS43Mi0yMzMuMDIKCQljLTc5LjU2LTIwLjYzLTE1OS42MywyNi45LTE3OS40NSwxMDYuNTJjLTE1LjEsNjAuNjMsMTIuNDYsMTI3LjA4LDY2LjQsMTU5LjQ0YzE5LjQzLDExLjY1LDI5LjE3LDI4LjE5LDI3LjQ5LDUxCgkJQzY0Mi45OCw3NzQuNTksNjQxLjk2LDc3Ny45Myw2NDEuNiw3NzkuNzh6Ii8+Cgk8cGF0aCBjbGFzcz0ic3QxIiBkPSJNMzAuMDYsNjE2LjU0YzAtNjEuOTksMC0xMjMuOTgsMC0xODUuOTZjMC0xMi43NSwzLjU1LTE2LjMyLDE2LjEtMTYuMzJjMjQuMjUsMC4wMSw0OC41LTAuMDIsNzIuNzYsMAoJCWMxMS4zOSwwLjAxLDE1Ljg2LDQuMzksMTUuODYsMTUuNjNjMC4wMywzOC45OCwwLjE0LDc3Ljk1LTAuMTEsMTE2LjkzYy0wLjA0LDYuMTEsMS4zNiw4LjI1LDcuODcsOC4yMQoJCWM0MC0wLjI4LDgwLjAxLTAuMjQsMTIwLjAxLDBjNS45NSwwLjA0LDcuNzEtMS42Miw3LjY3LTcuNjhjLTAuMjctMzkuMTgtMC4xNC03OC4zNi0wLjExLTExNy41NQoJCWMwLjAxLTExLjY2LDMuOTUtMTUuNTMsMTUuNjgtMTUuNTNjMjQuNjctMC4wMSw0OS4zMy0wLjAxLDc0LTAuMDJjMTEuOTktMC4wMSwxNS41NiwzLjUxLDE1LjU2LDE1LjQ5CgkJYzAuMDEsMTI0LjM5LDAuMDIsMjQ4Ljc4LDAuMDMsMzczLjE3YzAsMTEuMDYtMy40MiwxNC42NS0xNC40LDE0LjY4Yy0yNS43LDAuMDYtNTEuNDEsMC4wNS03Ny4xMS0wLjAyCgkJYy0xMC4wOC0wLjAzLTEzLjcyLTMuNzMtMTMuNzMtMTQuMDFjLTAuMDUtNDguMS0wLjE1LTk2LjIsMC4xLTE0NC4yOWMwLjAzLTYuMjQtMS41NS04LjA5LTcuOTUtOC4wNGMtNDAsMC4yOS04MC4wMSwwLjI4LTEyMC4wMSwwCgkJYy02LjIxLTAuMDQtNy42LDIuMDQtNy41Nyw3Ljg0YzAuMiw0Ny42OCwwLjEzLDk1LjM3LDAuMSwxNDMuMDVjLTAuMDEsMTIuMDUtMy40MiwxNS40Ni0xNS40OCwxNS40NwoJCWMtMjUuMDgsMC4wNC01MC4xNiwwLjAzLTc1LjI0LDAuMDFjLTExLjI4LTAuMDEtMTQuMDEtMi43NC0xNC4wMS0xMy44M0MzMC4wNiw3NDEuMzQsMzAuMDYsNjc4Ljk0LDMwLjA2LDYxNi41NHoiLz4KCTxwYXRoIGNsYXNzPSJzdDEiIGQ9Ik0xMTMyLDQxNC4yNWM0Ny4yNywwLDk0LjUzLTAuMDMsMTQxLjgsMC4wMmMxMy42LDAuMDEsMTcuMTcsMy43MiwxNy4xNywxNy40MgoJCWMwLjAxLDE5LjkxLDAuMDUsMzkuODEtMC4wMiw1OS43MmMtMC4wNSwxMS45Ni00LjY0LDE2LjQxLTE2LjY5LDE2LjQxYy0yNi45NSwwLjAxLTUzLjksMC04MC44NS0wLjAxYy05LjI2LDAtOS4yOC0wLjAyLTkuMjgsOC45MgoJCWMtMC4wMSw5NS4xNy0wLjAyLDE5MC4zNC0wLjAzLDI4NS41MWMwLDExLjA4LTQuMjcsMTUuMzUtMTUuNDYsMTUuMzVjLTI0LjQ2LDAtNDguOTMsMC4wMi03My4zOSwwCgkJYy0xMS44Ni0wLjAxLTE2LjEzLTQuMjUtMTYuMTMtMTYuMWMwLTk1LjM4LTAuMDMtMTkwLjc2LDAuMTYtMjg2LjE0YzAuMDEtNi4xNC0yLjA1LTcuNjktNy44OC03LjY0CgkJYy0yNy41NywwLjI1LTU1LjE0LDAuMTItODIuNzIsMC4xYy0xMS40OCwwLTE1Ljg2LTQuMTQtMTUuOTEtMTUuNjFjLTAuMS0yMS4zNi0wLjA3LTQyLjcxLDAuMDUtNjQuMDcKCQljMC4wNS05LjU5LDQuNDctMTMuODEsMTQuMjYtMTMuODJjNDguMy0wLjAzLDk2LjYxLTAuMDIsMTQ0LjkxLTAuMDJDMTEzMiw0MTQuMjgsMTEzMiw0MTQuMjYsMTEzMiw0MTQuMjV6Ii8+Cgk8cGF0aCBjbGFzcz0ic3QxIiBkPSJNNDkwLjAyLDk3MS4xNmMwLTYuNjMtMC4wMi0xMy4yNiwwLjAxLTE5Ljg5YzAuMDEtNC4xMy0xLjM1LTYuODMtNi4xMS02LjU5Yy0zLjkzLDAuMi01LjM4LTEuOTUtNS41OS01Ljc1CgkJYy0wLjczLTEyLjg5LTAuNDUtMTMuMjcsMTIuNDMtMTMuMjVjOC40OSwwLjAxLDE2Ljk5LDAuMSwyNS40OC0wLjAzYzQuNTEtMC4wNyw2LjQ5LDEuOSw2LjI2LDYuMzZjLTAuMTIsMi4yNy0wLjA4LDQuNTYtMC4wMiw2Ljg0CgkJYzAuMSwzLjcxLTEuMjUsNi4xMS01LjM1LDUuODhjLTUuMTItMC4yOS02LjM1LDIuNjYtNi4zNCw3LjAyYzAuMDUsMTMuMDUsMC4wNiwyNi4xLTAuMDEsMzkuMTVjLTAuMDksMTUuODItNC4zMSwyOS44LTE3LjY3LDM5LjgxCgkJYy0yMC4yNywxNS4xOS02My43LDEyLjg4LTc1Ljk1LTE4LjA0Yy0yLjgyLTcuMTEtMy42NS0xNS4yNi00LjA3LTIzLjAxYy0wLjY1LTEyLjE5LTAuMjUtMjQuNDQtMC4xMy0zNi42NgoJCWMwLjA1LTQuOTYtMC43NS04LjQ1LTcuMDEtOC41M2MtMS42Ny0wLjAyLTQuMDQtMi45Mi00Ljc0LTQuOTVjLTAuODMtMi40MS0wLjE2LTUuMzItMC4xOS04LjAxYy0wLjA1LTQuMjIsMi4xMS01LjksNi4xOS01Ljg3CgkJYzEwLjU2LDAuMDgsMjEuMTMsMC4xLDMxLjY5LTAuMDJjNC41NC0wLjA1LDYuNDUsMS45Nyw2LjIsNi40Yy0wLjEzLDIuMjctMC4xMyw0LjU2LTAuMDQsNi44M2MwLjE1LDMuNzQtMS4xOSw2LjEtNS4yOSw1Ljg3CgkJYy00LjMzLTAuMjQtNi4xNSwyLjItNi4xMSw2LjA0YzAuMTcsMTUuNTItMC4yMywzMS4xLDAuOTgsNDYuNTRjMS4xMiwxNC4zOSwxMS44NSwyMi44NywyNi43MywyMy4xMgoJCWMxNS40MSwwLjI2LDI1LjI1LTcuMTcsMjcuNDktMjIuMDFjMS4zNS04Ljk1LDEuMTQtMTguMTQsMS42NC0yNy4yMkM0OTAuMzMsOTcxLjE3LDQ5MC4xNyw5NzEuMTYsNDkwLjAyLDk3MS4xNnoiLz4KCTxwYXRoIGNsYXNzPSJzdDEiIGQ9Ik0xMDcwLjgxLDEwMDUuNGMwLDguNDktMC4wOCwxNi45OSwwLjA0LDI1LjQ4YzAuMDcsNC40Ni0xLjc4LDYuNjktNi4zMSw2LjEzYy00LjM5LTAuNTQtOC43NS0xLjQtMTMuMTUtMS44NQoJCWMtMS41LTAuMTYtMy4xNSwwLjI3LTQuNjEsMC43N2MtMTcuNDgsNi4wNS0zNC44Miw1LjM1LTUxLjI4LTIuNzZjLTIxLjQtMTAuNTQtMjguMjctMjkuNzgtMjguMDQtNTIuMDcKCQljMC4yMS0xOS44NSw3LjQtMzYuODUsMjMuOTMtNDguODFjMTkuODUtMTQuMzYsNTEuODItMTMuMTMsNzEuMDEsMi4xN2MzLjk3LDMuMTYsNC44Nyw1Ljg5LDAuOSw5LjUxCgkJYy0xLjIyLDEuMTEtMi40MiwyLjMtMy4zOSwzLjYyYy0zLjExLDQuMjItNi4xNCwzLjk2LTEwLjQ0LDEuMTZjLTE3LjM2LTExLjMyLTM4LjQxLTkuMjYtNTEsNC41NwoJCWMtMTMuMjQsMTQuNTUtMTQuMTUsMzguODYtMi4wMyw1NC41YzEwLjgsMTMuOTMsMzkuNDksMTYuNzksNTIuNTMsNS4yMmMyLjc3LTIuNDUsNC4wMS0xNy4zOCwxLjI3LTE5Ljk1CgkJYy0xLjMxLTEuMjMtMy42OC0xLjc2LTUuNi0xLjg0Yy01LjM3LTAuMjQtMTAuNzctMC4xOC0xNi4xNS0wLjA3Yy0zLjY5LDAuMDgtNS4zMS0xLjc5LTUuNDYtNS4yMmMtMC4xLTIuMjcsMC00LjU2LTAuMDMtNi44MwoJCWMtMC4wNy00LjMyLDEuNzctNi40Myw2LjM1LTYuMzdjMTEuNiwwLjE2LDIzLjIsMC4yLDM0Ljc5LDAuMDFjNS4yMy0wLjA5LDYuODYsMi4yMyw2LjcyLDcuMTcKCQlDMTA3MC42Myw5ODguNDEsMTA3MC44MSw5OTYuOTEsMTA3MC44MSwxMDA1LjR6Ii8+Cgk8cGF0aCBjbGFzcz0ic3QxIiBkPSJNODI2LjY5LDkyNS40N2MzLjk0LDAsNy45MywwLjQxLDExLjgtMC4xYzYuNjEtMC44NiwxMC4wNiwxLjg0LDEyLjcxLDcuODkKCQljMTMuOTYsMzEuODUsMjguMzIsNjMuNTIsNDIuNTIsOTUuMjZjMy4wNCw2Ljc5LDEuODgsOC43OC01LjM3LDguNjhjLTE2LjI2LTAuMjQtMTIuNzQsMi42LTE5LjQ3LTEzLjA5CgkJYy0yLjQ3LTUuNzYtNS42Ni04LjA4LTEyLjExLTcuODVjLTE0LjQ5LDAuNTMtMjkuMDEsMC4zNy00My41MiwwLjEzYy00LjYtMC4wOC03LjU4LDEuMjQtOS4wMyw1LjY3Yy0wLjM4LDEuMTgtMC45MywyLjMtMS40NCwzLjQ0CgkJYy01LjQ3LDEyLjM2LTguMiwxMy43OS0yMS43OSwxMS42N2MtNS40Ny0wLjg1LTQuNzctNC4xNi0zLjE1LTcuNzhjOS42My0yMS41NCwxOS4zLTQzLjA2LDI4Ljk2LTY0LjU5CgkJYzEuODYtNC4xNSwzLjgzLTguMjYsNS42My0xMi40NGMxLjY3LTMuODgsMS4yNy02LjUtMy42Ny03Ljc4Yy04LjMyLTIuMTQtNC4wNS05LjIxLTQuNTQtMTQuMTFjLTAuNDQtNC4yOSwzLjIzLTUuMSw2LjkyLTUuMDMKCQlDODE2LjMyLDkyNS41NSw4MjEuNTEsOTI1LjQ4LDgyNi42OSw5MjUuNDd6IE04MzYuNjcsOTUxLjJjLTEuNjEsMS44Ny0yLjMyLDIuNC0yLjY0LDMuMTFjLTUuNTUsMTIuNDMtMTEuMDIsMjQuODktMTYuNTcsMzcuMzEKCQljLTIuMDEsNC41MSwwLjkxLDUuMDEsNC4xLDUuMDRjOS41MSwwLjA3LDE5LjAxLTAuMDYsMjguNTIsMC4wMmM0Ljg5LDAuMDQsNS43Ni0yLjE2LDMuOTUtNi4yNGMtMy4wMi02Ljc5LTYuMDEtMTMuNTktOS4wMi0yMC4zOQoJCUM4NDIuMzcsOTY0LjEsODM5Ljc0LDk1OC4xNSw4MzYuNjcsOTUxLjJ6Ii8+Cgk8cGF0aCBjbGFzcz0ic3QxIiBkPSJNMjY5LjI5LDEwMTYuNDZjLTcuNjcsMC0xNS4zNCwwLjE0LTIzLjAxLTAuMDVjLTQuMDUtMC4xLTYuOCwxLjE5LTguMDgsNS4yYy0wLjM3LDEuMTctMS4wOSwyLjIzLTEuNiwzLjM2CgkJYy01LjcxLDEyLjYyLTUuNzEsMTIuNjItMTkuNTQsMTIuMjRjLTcuMDMtMC4xOS04LjExLTEuNzYtNS4xOC04LjM2YzEwLjg0LTI0LjQxLDIxLjc2LTQ4Ljc4LDMyLjY4LTczLjE1CgkJYzMuMzUtNy40NiwyLjYyLTYuNTItMy42NS0xMS44NWMtMi44OC0yLjQ1LTIuNTUtOS4zMS0yLjUyLTE0LjE2YzAuMDEtMS40MiwzLjk3LTMuOTIsNi4yLTQuMDNjOS43Mi0wLjQ1LDE5LjQ5LDAuMSwyOS4yMi0wLjMxCgkJYzUuNDEtMC4yMyw4LjI3LDEuODEsMTAuNCw2LjY0YzExLjQ0LDI1Ljk0LDIzLjEsNTEuNzgsMzQuNyw3Ny42NWMyLjg4LDYuNDIsNS45MywxMi43Nyw4LjcyLDE5LjIzYzIuNjgsNi4yLDEuMjMsOC4zOS01LjYxLDguMzMKCQljLTE2LjcyLTAuMTUtMTIuNzksMi40NC0xOS4yNi0xMi40OWMtMi43Ny02LjM5LTYuMTQtOC45My0xMi45Ny04LjM4QzI4MywxMDE2Ljg4LDI3Ni4xMywxMDE2LjQ1LDI2OS4yOSwxMDE2LjQ2eiBNMjg5LjcsOTk0Ljk5CgkJYy02Ljg0LTE0LjU0LTEyLjA5LTI5LjI0LTIwLjItNDIuNzdjLTYuODYsMTMuMjctMTIuNjksMjYuMjUtMTguNDMsMzkuMjdjLTEuNTIsMy40NC0wLjAzLDUuMTIsMy41Nyw1LjEzCgkJYzEwLjE0LDAuMDEsMjAuMjksMC4wNCwzMC40My0wLjA3QzI4Ni4zNiw5OTYuNTQsMjg3LjY0LDk5NS43MSwyODkuNyw5OTQuOTl6Ii8+Cgk8cGF0aCBjbGFzcz0ic3QxIiBkPSJNMTIwMC45Nyw5MjUuNDljMTMuMDYsMCwyNi4xMi0wLjAyLDM5LjE4LDAuMDFjNy4zMSwwLjAyLDguMDEsMC43Nyw4LjA2LDguMzFjMC4wMSwxLjQ1LTAuMDcsMi45LTAuMDMsNC4zNQoJCWMwLjEzLDQuNzUtMi4wNSw2LjUtNi44NSw2LjQ0Yy0xNS45Ni0wLjItMzEuOTMsMC4xNi00Ny44OC0wLjE1Yy02LjA3LTAuMTItOC4wMywyLjIxLTcuOTYsOC4wMQoJCWMwLjI4LDIzLjU1LTIuMzEsMTguOTksMTkuMzgsMTkuNDFjOS4xMiwwLjE4LDE4LjI1LDAuMTksMjcuMzYtMC4wNWM1LjQ2LTAuMTQsOC4zNCwxLjY3LDcuNyw3LjUzYy0wLjE4LDEuNjQtMC4xNCwzLjMyLTAuMDEsNC45NwoJCWMwLjQsNC44OS0xLjksNi41OC02LjY0LDYuNTJjLTEzLjI2LTAuMTctMjYuNTQsMC4xNy0zOS44LTAuMTZjLTYuMDctMC4xNS04LjI3LDIuMjYtNy45NSw4LjA1YzAuMjMsNC4xMywwLjEyLDguMjktMC4wNCwxMi40NAoJCWMtMC4yLDUuMjUsMi4wNiw3LjQ0LDcuNDEsNy4zN2MxNS43NS0wLjE5LDMxLjUxLTAuMiw0Ny4yNiwwLjA1YzIuNTQsMC4wNCw3LjA1LDEuNTQsNy4yMiwyLjgxYzAuNjMsNC43NSwwLjAzLDkuNzItMC42NiwxNC41MwoJCWMtMC4xLDAuNjctMy4yNywxLjI2LTUuMDIsMS4yNmMtMjMuNDIsMC4wNy00Ni44NS0wLjA1LTcwLjI3LDAuMDljLTUuMTksMC4wMy02LjgtMi4xMS02Ljc3LTcuMWMwLjE2LTI1LjUsMC4wMS01MSwwLjEzLTc2LjUKCQljMC4wMi01LjMyLTAuMzEtOS41Mi03LjQtOS40Yy0xLjUxLDAuMDMtMy41NC0yLjc4LTQuNDctNC42OWMtMC43NC0xLjUxLTAuMTYtMy42Ni0wLjE2LTUuNTNjMC4wMi03Ljg5LDAuNjctOC41Nyw4LjQ0LTguNTgKCQlDMTE3NC40Myw5MjUuNDgsMTE4Ny43LDkyNS40OSwxMjAwLjk3LDkyNS40OXoiLz4KCTxwYXRoIGNsYXNzPSJzdDEiIGQ9Ik02NDkuMDUsMTA0MC43NGMtMTEuNTQtMC43OS0yMi43Ny0yLjE1LTMyLjEzLTguOTljLTUuMTktMy43OS05LjI2LTkuMjQtMTMuNDYtMTQuMjUKCQljLTIuMjgtMi43MS0xLjM3LTUuNDcsMS44Mi03LjE4YzIuNzMtMS40Nyw1LjM4LTMuNTQsOC4zLTQuMDljMi4zLTAuNDMsNi4yNiwwLjIxLDcuMjYsMS43OWM5LjE4LDE0LjQ2LDMyLjE4LDE1LjI4LDQ0LjA0LDEwLjY4CgkJYzcuNS0yLjkxLDExLTcuNjMsMTAuODctMTQuMjJjLTAuMTMtNi4xOC00LjIzLTExLjYtMTEuNzMtMTMuNTJjLTguMzktMi4xNS0xNy4wNi0zLjE4LTI1LjU1LTQuOThjLTQuNDMtMC45NC04LjgzLTIuMjItMTMuMDctMy44CgkJYy0xMi4zNi00LjYxLTE4LjktMTMuNDgtMTkuMjctMjYuODVjLTAuMzctMTMuMzQsNS42MS0yMi42OCwxNy42My0yOC4wNGMxOC4yNC04LjEzLDM2LjY0LTguMjIsNTQuNTMsMC43NgoJCWM0LjYsMi4zMSw4LjE2LDYuODIsMTEuOTMsMTAuNTljMC45NSwwLjk1LDEuNTEsMy4xNSwxLjExLDQuNDRjLTEuNjcsNS40NC0xNC4yNCw4LjU2LTE4LjY5LDQuOTIKCQljLTEwLjQ3LTguNTYtMjIuMjItMTAuNTItMzQuOTctNi4yOWMtNy4wMSwyLjMzLTExLjM4LDcuODEtMTAuOTQsMTMuNDNjMC41Niw3LjE0LDUuOTksOS43OCwxMS43MiwxMS4wOAoJCWM5LjQ3LDIuMTUsMTkuMjMsMy4wNywyOC42OCw1LjNjMTEuNzIsMi43NiwyMi43Niw3LjEsMjcuMzcsMTkuODNjNy4zNSwyMC4zMS0xLjU4LDM4Ljc2LTIyLjM5LDQ1LjE1CgkJQzY2NC42OSwxMDM4Ljc4LDY1Ni43NSwxMDM5LjM3LDY0OS4wNSwxMDQwLjc0eiIvPgoJPHBhdGggY2xhc3M9InN0MSIgZD0iTTgzLjc2LDEwNDAuNjljLTE2LjU5LTAuODgtMzEuOTMtMy44My00My4wMi0xNy41OWMtMS4wNC0xLjI5LTEuOTQtMi42OS0yLjg2LTQuMDYKCQljLTIuOTMtNC4zMi0yLjQ4LTcuNjEsMi43OS05LjYzYzIuMTEtMC44MSw0LjEtMS45Nyw2LjA3LTMuMWMzLjUtMi4wMiw1LjgtMS4xOCw4LjM3LDIuMDZjMTAuMTcsMTIuODEsMzEuNzEsMTYuODYsNDYuMjUsOS4wOAoJCWM1LjE5LTIuNzgsOC4wOS03LjA4LDguMTctMTIuOTVjMC4wNy01LjU3LTIuOTMtMTAuMDUtNy44OS0xMS41NGMtOS42My0yLjg4LTE5LjYxLTQuNTctMjkuMzktNi45NWMtNC41OS0xLjEyLTkuMTYtMi40Ni0xMy42LTQuMQoJCWMtMTIuNS00LjYxLTE4LjU5LTEzLjc3LTE4Ljg4LTI3LjA1Yy0wLjI5LTEzLjQzLDYuMTEtMjIuNDksMTguMDUtMjcuNzZjMTcuODMtNy44OCwzNS43NC03LjcsNTMuNDYsMC40NwoJCWMyLjIzLDEuMDMsNC42NCwyLjMsNi4xMyw0LjE0YzIuODQsMy41Myw1LjA4LDcuNTUsNy41NywxMS4zN2MtNC4xLDIuNi04LjAxLDUuNjQtMTIuNDEsNy41NmMtMS40MSwwLjYyLTQuMzktMS4zNi02LjE4LTIuNzUKCQljLTExLjA1LTguNjItMjMuMTMtMTAuNTItMzYuMjEtNS43N2MtNS42OSwyLjA3LTkuNCw2LjA5LTkuNTYsMTIuNDVjLTAuMTYsNi42Miw0LjM0LDkuOCw5Ljg2LDExLjA2CgkJYzEwLjA3LDIuMzEsMjAuMzcsMy42MSwzMC40Myw1Ljk2YzEwLjQxLDIuNDMsMjAuMzYsNi4wNywyNS44MSwxNi40NmMxMC4yNywxOS41NSwwLjU4LDQyLjIxLTIxLjEyLDQ4LjU1CgkJQzk4LjUzLDEwMzguNjUsOTEuMDYsMTAzOS4zNiw4My43NiwxMDQwLjY5eiIvPgo8L2c+Cjwvc3ZnPgo="
BRAND_RED = "#D10D0D"
BRAND_CHARCOAL = "#3C3C3C"

def password_hash(password: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 150000).hex()
    return f"pbkdf2_sha256${salt}${digest}"

def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, salt, digest = stored.split("$", 2)
        candidate = password_hash(password, salt).split("$", 2)[2]
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False

def default_users() -> dict[str, Any]:
    return {
        "service": APP_NAME,
        "version": APP_VERSION,
        "users": [{
            "id": "admin",
            "username": os.getenv("ADMIN_USERNAME", "admin"),
            "display_name": "Administrator",
            "role": "admin",
            "enabled": True,
            "password_hash": password_hash(os.getenv("ADMIN_PASSWORD", "changeme-now")),
            "created_at": utc_now(),
            "last_login": "",
        }],
    }

def load_users() -> dict[str, Any]:
    if not USERS_PATH.exists():
        payload = default_users()
        write_json_atomic(USERS_PATH, payload)
        return payload
    return read_json(USERS_PATH, default_users())

def save_users(payload: dict[str, Any]) -> None:
    payload["service"] = APP_NAME
    payload["version"] = APP_VERSION
    write_json_atomic(USERS_PATH, payload)

def load_invites() -> dict[str, Any]:
    return read_json(INVITES_PATH, {"service": APP_NAME, "version": APP_VERSION, "invites": []})

def save_invites(payload: dict[str, Any]) -> None:
    payload["service"] = APP_NAME
    payload["version"] = APP_VERSION
    write_json_atomic(INVITES_PATH, payload)

def load_sessions() -> dict[str, Any]:
    return read_json(SESSIONS_PATH, {"sessions": []})

def save_sessions(payload: dict[str, Any]) -> None:
    write_json_atomic(SESSIONS_PATH, payload)

def get_current_user(request: Request) -> dict[str, Any] | None:
    sid = request.cookies.get("hotsausage_session", "")
    if not sid:
        return None
    session = next((s for s in load_sessions().get("sessions", []) if s.get("id") == sid), None)
    if not session:
        return None
    return next((u for u in load_users().get("users", []) if u.get("username") == session.get("username") and u.get("enabled", True)), None)

def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("role") == "admin")

def brand_css() -> str:
    return """
<style>
:root { --hs-red:#D10D0D; --hs-charcoal:#3C3C3C; --hs-bg:#f7f7f7; --hs-card:#fff; }
body { font-family: Arial, Helvetica, sans-serif; margin:0; background:var(--hs-bg); color:#111827; }
header { background:var(--hs-charcoal); color:white; padding:16px 22px; display:flex; align-items:center; gap:16px; }
header img.logo { width:76px; height:auto; background:white; border-radius:12px; padding:6px; }
header h1 { margin:0; color:white; font-size:30px; }
header .subtitle { opacity:.9; }
main { padding:22px; max-width:1300px; margin:auto; }
section { background:var(--hs-card); border-radius:14px; padding:18px; margin-bottom:16px; box-shadow:0 1px 6px #0002; }
input,select,textarea { width:100%; padding:9px; margin:4px 0 10px; box-sizing:border-box; border:1px solid #d1d5db; border-radius:8px; }
button,.button { padding:9px 13px; border:0; border-radius:9px; background:var(--hs-red); color:white; margin:3px; cursor:pointer; text-decoration:none; display:inline-block; }
.secondary { background:var(--hs-charcoal); }
code,pre { background:#f1f5f9; padding:5px; border-radius:6px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
td,th { border-bottom:1px solid #e5e7eb; padding:7px; text-align:left; vertical-align:top; }
.nav { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
.nav a { color:white; text-decoration:none; background:#0003; padding:7px 10px; border-radius:8px; }
</style>
"""

def brand_header(title: str, subtitle: str = "") -> str:
    return f"""
<header>
  <img class="logo" src="{HOTSAUSAGE_LOGO_DATA_URI}" alt="HotSausage">
  <div>
    <h1>{title}</h1>
    <div class="subtitle">{subtitle}</div>
    <div class="nav">
      <a href="/">Dashboard</a>
      <a href="/planner">Planner</a>
      <a href="/routes">Routes</a>
      <a href="/maps">QCT Maps</a>
      <a href="/source-discovery">Discovery</a>
      <a href="/admin/users">Admin</a>
      <a href="/logout">Logout</a>
    </div>
  </div>
</header>
"""

def login_page(error: str = "") -> str:
    err = f"<p style='color:#D10D0D;font-weight:bold'>{escape(error)}</p>" if error else ""
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>HotSausage Byways Login</title>{brand_css()}</head>
<body>{brand_header("HotSausage Byways", "Self-hosted green lane mapping and route planning")}
<main style="max-width:540px"><section><h2>Login</h2>{err}
<form method="post" action="/login">
<label>Username</label><input name="username" autocomplete="username">
<label>Password</label><input name="password" type="password" autocomplete="current-password">
<button type="submit">Login</button>
</form>
<p><small>First-run default: admin / changeme-now unless set by Kubernetes secret.</small></p>
</section></main></body></html>"""

def admin_user_rows() -> str:
    rows = ""
    for u in load_users().get("users", []):
        rows += f"<tr><td>{escape(str(u.get('username','')))}</td><td>{escape(str(u.get('display_name','')))}</td><td>{escape(str(u.get('role','')))}</td><td>{escape(str(u.get('enabled', True)))}</td><td>{escape(str(u.get('last_login','')))}</td></tr>"
    return rows

def invite_rows() -> str:
    rows = ""
    for inv in load_invites().get("invites", []):
        url = f"{PUBLIC_BASE_URL}/invite/{inv.get('token','')}"
        rows += f"<tr><td><code>{escape(inv.get('token',''))}</code></td><td>{escape(inv.get('role','viewer'))}</td><td>{escape(str(inv.get('used_by','')))}</td><td style='overflow-wrap:anywhere'><code>{escape(url)}</code></td></tr>"
    return rows or "<tr><td colspan='4'>No invites yet.</td></tr>"

def create_invite(role: str = "viewer") -> dict[str, Any]:
    if role not in ("admin", "editor", "viewer"):
        role = "viewer"
    payload = load_invites()
    invite = {"token": secrets.token_urlsafe(24), "role": role, "created_at": utc_now(), "used_by": ""}
    payload.setdefault("invites", []).append(invite)
    save_invites(payload)
    return invite

def create_user_from_invite(token: str, username: str, password: str, display_name: str = "") -> dict[str, Any]:
    invites = load_invites()
    invite = next((i for i in invites.get("invites", []) if i.get("token") == token and not i.get("used_by")), None)
    if not invite:
        return {"state": "error", "error": "Invite not found or already used"}
    users = load_users()
    if any(u.get("username") == username for u in users.get("users", [])):
        return {"state": "error", "error": "Username already exists"}
    user = {
        "id": safe_id(username),
        "username": username,
        "display_name": display_name or username,
        "role": invite.get("role", "viewer"),
        "enabled": True,
        "password_hash": password_hash(password),
        "created_at": utc_now(),
        "last_login": "",
    }
    users.setdefault("users", []).append(user)
    save_users(users)
    invite["used_by"] = username
    invite["used_at"] = utc_now()
    save_invites(invites)
    return {"state": "created"}


def empty_maps() -> dict[str, Any]:
    return {"service": APP_NAME, "version": APP_VERSION, "maps": []}

def load_maps() -> dict[str, Any]:
    return read_json(MAPS_PATH, empty_maps())

def save_maps(payload: dict[str, Any]) -> None:
    payload["service"] = APP_NAME
    payload["version"] = APP_VERSION
    write_json_atomic(MAPS_PATH, payload)

def empty_routes() -> dict[str, Any]:
    return {"service": APP_NAME, "version": APP_VERSION, "routes": []}

def load_routes() -> dict[str, Any]:
    return read_json(ROUTES_PATH, empty_routes())

def save_routes(payload: dict[str, Any]) -> None:
    payload["service"] = APP_NAME
    payload["version"] = APP_VERSION
    write_json_atomic(ROUTES_PATH, payload)

def route_geojson_path(route_id: str) -> Path:
    return ROUTE_DIR / f"{compact_id(route_id)}.geojson"

def map_storage_path(map_id: str, filename: str) -> Path:
    suffix = Path(filename).suffix.lower() or ".qct"
    return MAP_DIR / f"{compact_id(map_id)}{suffix}"

def save_uploaded_qct(name: str, scale: str, filename: str, content: bytes) -> dict[str, Any]:
    ensure_dirs()
    display_name = name.strip() or Path(filename or "").stem or f"QCT Map {int(time.time())}"
    map_id = compact_id(f"qct-{display_name}-{int(time.time())}", 72)
    path = map_storage_path(map_id, filename or "map.qct")
    path.write_bytes(content)
    record = {
        "id": map_id,
        "name": display_name,
        "scale": scale or "1:25k",
        "filename": filename,
        "source_path": str(path),
        "status": "uploaded",
        "note": "QCT uploaded. Click Import/Convert to build planner background tiles.",
        "uploaded_at": utc_now(),
        "bounds": None,
    }
    payload = load_maps()
    payload["maps"] = [m for m in payload.get("maps", []) if m.get("id") != map_id] + [record]
    save_maps(payload)
    return record


def map_image_path(map_id: str) -> Path:
    return MAP_IMAGE_DIR / f"{compact_id(map_id)}.png"

def map_tile_cache_dir(map_id: str) -> Path:
    return MAP_TILE_DIR / compact_id(map_id)

def update_map_record(map_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    payload = load_maps()
    updated = None
    for m in payload.get("maps", []):
        if m.get("id") == map_id:
            m.update(updates)
            m["updated_at"] = utc_now()
            updated = m
            break
    save_maps(payload)
    return updated or {"state": "not_found", "id": map_id}

def import_qct_map(map_id: str) -> dict[str, Any]:
    payload = load_maps()
    record = next((m for m in payload.get("maps", []) if m.get("id") == map_id), None)
    if not record:
        return {"state": "error", "error": "map not found"}

    source_path = Path(record.get("source_path", ""))
    if not source_path.exists():
        return {"state": "error", "error": "QCT source file missing"}

    out_png = map_image_path(map_id)
    out_png.parent.mkdir(parents=True, exist_ok=True)

    try:
        update_map_record(map_id, {"status": "importing", "note": "QCT conversion running."})
        result = convert_qct_to_png(source_path, out_png)
        bounds = result.get("bounds")
        updates = {
            "status": "ready",
            "note": "QCT converted and ready for planner background display.",
            "image_path": str(out_png),
            "bounds": bounds,
            "image_width": result.get("image_width"),
            "image_height": result.get("image_height"),
            "qct_metadata": result.get("metadata", {}),
            "imported_at": utc_now(),
            "tile_url": f"{PUBLIC_BASE_URL}/tiles/maps/{map_id}/{{z}}/{{x}}/{{y}}.png",
        }
        updated = update_map_record(map_id, updates)
        # Clear stale tile cache for this map.
        cache = map_tile_cache_dir(map_id)
        if cache.exists():
            shutil.rmtree(cache)
        return {"state": "ready", "map": updated}
    except Exception as exc:
        updated = update_map_record(map_id, {"status": "error", "note": str(exc), "error": str(exc)})
        return {"state": "error", "error": str(exc), "map": updated}

def lonlat_tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    n = 2 ** z
    lon_w = x / n * 360.0 - 180.0
    lon_e = (x + 1) / n * 360.0 - 180.0

    def tile_y_to_lat(tile_y: int) -> float:
        merc_n = math.pi * (1 - 2 * tile_y / n)
        return math.degrees(math.atan(math.sinh(merc_n)))

    lat_n = tile_y_to_lat(y)
    lat_s = tile_y_to_lat(y + 1)
    return lon_w, lat_s, lon_e, lat_n

def render_map_tile(map_id: str, z: int, x: int, y: int) -> bytes | None:
    record = next((m for m in load_maps().get("maps", []) if m.get("id") == map_id), None)
    if not record or record.get("status") != "ready":
        return None

    image_path = Path(record.get("image_path", ""))
    bounds = record.get("bounds")
    if not image_path.exists() or not bounds:
        return None

    cache_path = map_tile_cache_dir(map_id) / str(z) / str(x) / f"{y}.png"
    if cache_path.exists():
        return cache_path.read_bytes()

    west, south, east, north = [float(v) for v in bounds]
    tw, ts, te, tn = lonlat_tile_bounds(z, x, y)

    # No intersection -> transparent tile.
    if te < west or tw > east or tn < south or ts > north:
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
        return data

    src = Image.open(image_path).convert("RGBA")
    img_w, img_h = src.size

    # Approximate linear lon/lat georeferencing from QCT outline bounds.
    def px(lon: float) -> float:
        return (lon - west) / (east - west) * img_w

    def py(lat: float) -> float:
        return (north - lat) / (north - south) * img_h

    crop_left = max(0, min(img_w, px(tw)))
    crop_right = max(0, min(img_w, px(te)))
    crop_top = max(0, min(img_h, py(tn)))
    crop_bottom = max(0, min(img_h, py(ts)))

    if crop_right <= crop_left or crop_bottom <= crop_top:
        tile = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    else:
        crop = src.crop((int(crop_left), int(crop_top), int(crop_right), int(crop_bottom)))
        tile = crop.resize((256, 256), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    tile.save(buf, format="PNG")
    data = buf.getvalue()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return data

def map_coverage_geojson() -> dict[str, Any]:
    features = []
    for m in load_maps().get("maps", []):
        bounds = m.get("bounds")
        if not bounds:
            continue
        west, south, east, north = [float(v) for v in bounds]
        features.append({
            "type": "Feature",
            "properties": {
                "id": m.get("id"),
                "name": m.get("name"),
                "scale": m.get("scale"),
                "status": m.get("status"),
                "tile_url": m.get("tile_url", ""),
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [west, south], [east, south], [east, north], [west, north], [west, south]
                ]],
            },
        })
    return {"type": "FeatureCollection", "features": features}


def delete_map_id(map_id: str) -> dict[str, Any]:
    payload = load_maps()
    target = next((m for m in payload.get("maps", []) if m.get("id") == map_id), None)
    if not target:
        return {"state": "not_found", "id": map_id}

    for key in ("source_path", "image_path"):
        raw = target.get(key, "")
        if raw:
            p = Path(raw)
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    try:
        cache = map_tile_cache_dir(map_id)
        if cache.exists():
            shutil.rmtree(cache)
    except Exception:
        pass

    payload["maps"] = [m for m in payload.get("maps", []) if m.get("id") != map_id]
    save_maps(payload)
    return {"state": "deleted", "id": map_id, "name": target.get("name", "")}

def save_planned_route(name: str, geojson: dict[str, Any], owner: str = "") -> dict[str, Any]:
    ensure_dirs()
    route_name = name.strip() or f"Planned Route {int(time.time())}"
    route_id = compact_id(f"route-{route_name}-{int(time.time())}", 72)

    features = geojson.get("features", [])
    if not features and geojson.get("type") == "Feature":
        features = [geojson]
    if not features:
        raise ValueError("route GeoJSON must contain at least one feature")

    clean_features = []
    point_count = 0
    for feature in features:
        geom = feature.get("geometry") or {}
        if geom.get("type") == "LineString":
            coords = geom.get("coordinates") or []
            if len(coords) >= 2:
                point_count += len(coords)
                clean_features.append({
                    "type": "Feature",
                    "properties": {"name": route_name, "source": "planner", **(feature.get("properties") or {})},
                    "geometry": {"type": "LineString", "coordinates": coords},
                })
        elif geom.get("type") == "MultiLineString":
            for idx, coords in enumerate(geom.get("coordinates") or [], start=1):
                if len(coords) >= 2:
                    point_count += len(coords)
                    clean_features.append({
                        "type": "Feature",
                        "properties": {"name": route_name, "segment": idx, "source": "planner"},
                        "geometry": {"type": "LineString", "coordinates": coords},
                    })

    if not clean_features:
        raise ValueError("route must contain at least one LineString with two points")

    fc = {"type": "FeatureCollection", "features": clean_features}
    path = route_geojson_path(route_id)
    write_json_atomic(path, fc)

    route = {
        "id": route_id,
        "name": route_name,
        "owner": owner,
        "feature_count": len(clean_features),
        "point_count": point_count,
        "geojson_path": str(path),
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "traccar_tile_url": f"{PUBLIC_BASE_URL}/tiles/tracks/{route_id}/{{z}}/{{x}}/{{y}}.png",
    }
    payload = load_routes()
    payload["routes"] = [r for r in payload.get("routes", []) if r.get("id") != route_id] + [route]
    save_routes(payload)
    return route

def route_to_gpx(route_id: str) -> str:
    payload = load_routes()
    route = next((r for r in payload.get("routes", []) if r.get("id") == route_id), None)
    if not route:
        raise ValueError("route not found")
    fc = read_json(route_geojson_path(route_id), {"type": "FeatureCollection", "features": []})
    name = route.get("name", route_id)
    lines = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<gpx version="1.1" creator="HotSausage Byways" xmlns="http://www.topografix.com/GPX/1/1">')
    lines.append(f'  <trk><name>{escape(str(name))}</name>')
    for feature in fc.get("features", []):
        coords = feature.get("geometry", {}).get("coordinates", [])
        if len(coords) < 2:
            continue
        lines.append("    <trkseg>")
        for lon, lat, *rest in coords:
            lines.append(f'      <trkpt lat="{float(lat):.7f}" lon="{float(lon):.7f}"></trkpt>')
        lines.append("    </trkseg>")
    lines.append("  </trk>")
    lines.append("</gpx>")
    return "\n".join(lines)

def save_route_as_track(route_id: str) -> dict[str, Any]:
    route = next((r for r in load_routes().get("routes", []) if r.get("id") == route_id), None)
    if not route:
        return {"state": "error", "error": "route not found"}
    fc = read_json(route_geojson_path(route_id), {"type": "FeatureCollection", "features": []})
    track_id = compact_id(f"track-{route.get('name','route')}-{int(time.time())}", 72)
    path = track_geojson_path(track_id)
    write_json_atomic(path, fc)
    track = {
        "id": track_id,
        "name": route.get("name", track_id),
        "filename": f"{route_id}.planned.geojson",
        "feature_count": len(fc.get("features", [])),
        "point_count": sum(len(f.get("geometry", {}).get("coordinates", [])) for f in fc.get("features", [])),
        "uploaded_at": utc_now(),
        "geojson_path": str(path),
        "traccar_tile_url": f"{PUBLIC_BASE_URL}/tiles/tracks/{track_id}/{{z}}/{{x}}/{{y}}.png",
        "style": {"colour": "purple", "line": "dotted"},
    }
    tracks_payload = load_tracks()
    tracks_payload["tracks"] = [t for t in tracks_payload.get("tracks", []) if t.get("id") != track_id] + [track]
    save_tracks(tracks_payload)
    return {"state": "saved_as_track", "track": track}

def maps_rows() -> str:
    rows = ""
    for m in load_maps().get("maps", []):
        mid = escape(str(m.get("id", "")))
        name = escape(str(m.get("name", "")))
        scale = escape(str(m.get("scale", "")))
        status = escape(str(m.get("status", "")))
        note = escape(str(m.get("note", "")))

        actions = ""
        if m.get("status") != "ready":
            actions += f"<button onclick=\"importMap('{mid}')\">Import/Convert</button> "
        if m.get("status") == "ready":
            actions += f"<a class='button' href='/planner?map_id={mid}'>Open in Planner</a> "
            actions += f"<a class='button secondary' href='/tiles/maps/{mid}/10/508/340.png' target='_blank'>Test Tile</a> "
        actions += f"<button class='secondary' onclick=\"deleteMap('{mid}')\">Delete</button>"

        rows += (
            f"<tr>"
            f"<td>{name}</td>"
            f"<td><code>{mid}</code></td>"
            f"<td>{scale}</td>"
            f"<td>{status}</td>"
            f"<td>{note}</td>"
            f"<td>{actions}</td>"
            f"</tr>"
        )
    return rows or "<tr><td colspan='6'>No QCT maps uploaded yet.</td></tr>"

def routes_rows() -> str:
    rows = ""
    for r in load_routes().get("routes", []):
        rid = escape(str(r.get("id","")))
        rows += (
            f"<tr>"
            f"<td>{escape(str(r.get('name','')))}</td>"
            f"<td><code>{rid}</code></td>"
            f"<td>{escape(str(r.get('point_count',0)))}</td>"
            f"<td>"
            f"<a href='/routes/{rid}/export-gpx'>Download GPX</a> | "
            f"<button onclick=\"saveAsTrack('{rid}')\">Add to Tracks</button>"
            f"</td>"
            f"</tr>"
        )
    return rows or "<tr><td colspan='4'>No planned routes yet.</td></tr>"

def planner_page() -> str:
    maps_payload = load_maps()
    tracks_payload = load_tracks()
    routes_payload = load_routes()

    ready_maps = [m for m in maps_payload.get("maps", []) if m.get("status") == "ready"]
    qct_options = "".join(
        f"<option value='{escape(str(m.get('id','')))}'>{escape(str(m.get('name','')))} - {escape(str(m.get('scale','')))}</option>"
        for m in ready_maps
    ) or "<option value=''>No ready QCT maps</option>"

    track_checks = "".join(
        f"<label><input type='checkbox' class='track-toggle' value='{escape(str(t.get('id','')))}'> {escape(str(t.get('name','')))}</label>"
        for t in tracks_payload.get("tracks", [])
    ) or "<p class='muted'>No uploaded GPX tracks yet.</p>"

    route_checks = "".join(
        f"<label><input type='checkbox' class='route-toggle' value='{escape(str(r.get('id','')))}'> {escape(str(r.get('name','')))}</label>"
        for r in routes_payload.get("routes", [])
    ) or "<p class='muted'>No saved planned routes yet.</p>"

    return f"""<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<title>HotSausage Planner</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css">
{brand_css()}
<style>
#layout {{ display:grid; grid-template-columns:330px minmax(0,1fr); gap:14px; }}
#map {{ height:76vh; border-radius:14px; border:1px solid #d1d5db; }}
.panel h3 {{ margin:12px 0 6px; }}
.panel label {{ display:block; margin:7px 0; cursor:pointer; }}
.panel input[type="checkbox"], .panel input[type="radio"] {{ width:auto; margin-right:8px; }}
.toolbar {{ display:flex; gap:8px; flex-wrap:wrap; }}
.legend-swatch {{ display:inline-block; width:18px; height:5px; border-radius:4px; vertical-align:middle; margin-right:7px; }}
.muted {{ color:#6b7280; font-size:13px; }}
@media (max-width:900px) {{ #layout {{ grid-template-columns:1fr; }} #map {{ height:64vh; }} }}
</style>
</head>
<body>
{brand_header("Planner", "Plan routes on QCT OS maps, BOAT/TRO overlays and GPX tracks")}
<main>
<section>
  <div class="toolbar">
    <input id="routeName" placeholder="Route name" style="max-width:360px">
    <button onclick="saveRoute()">Save Route</button>
    <button onclick="clearDrawings()" class="secondary">Clear Drawing</button>
    <a class="button secondary" href="/routes">Routes</a>
    <a class="button secondary" href="/maps">QCT Maps</a>
  </div>
</section>
<div id="layout">
<section class="panel">
  <h2>Map Layers</h2>
  <h3>Base Map</h3>
  <label><input type="radio" name="base" value="osm" checked> OpenStreetMap</label>
  <label><input type="radio" name="base" value="qct"> Selected QCT map</label>
  <label><input type="radio" name="base" value="none"> No base map</label>
  <h3>QCT / OS 1:25k</h3>
  <select id="qctSelect">{qct_options}</select>
  <button onclick="openSelectedQct()">Open Selected QCT</button>
  <p id="qctStatus" class="muted">Only maps with status ready are listed.</p>
  <h3>Byway Categories</h3>
  <label><input id="boatLayerToggle" type="checkbox" checked> BOAT / Byways</label>
  <label><input id="troLayerToggle" type="checkbox" checked> TRO colour coding</label>
  <label><input id="orpaLayerToggle" type="checkbox" disabled> ORPA / UCR <span class="muted">(next data layer)</span></label>
  <h3>Uploaded GPX Tracks</h3>
  <div id="trackToggles">{track_checks}</div>
  <h3>Saved Planned Routes</h3>
  <div id="routeToggles">{route_checks}</div>
  <h3>Legend</h3>
  <p><span class="legend-swatch" style="background:#16a34a"></span> BOAT clear</p>
  <p><span class="legend-swatch" style="background:#d97706"></span> Possible TRO</p>
  <p><span class="legend-swatch" style="background:#dc2626"></span> Restricted / closure</p>
  <p><span class="legend-swatch" style="background:#9333ea"></span> Uploaded GPX</p>
  <p><span class="legend-swatch" style="background:#D10D0D"></span> Active planned route</p>
</section>
<section><div id="map"></div></section>
</div>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>
<script>
const params = new URLSearchParams(window.location.search);
let selectedMapId = params.get('map_id') || '';
const map = L.map('map').setView([51.5, -1.5], 9);
const osm = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom:19, attribution:'OpenStreetMap'}}).addTo(map);
let qctLayer = null;
let boatOverlay = L.tileLayer('/tiles/boats-coloured/{{z}}/{{x}}/{{y}}.png', {{maxZoom:18, opacity:0.90, attribution:'HotSausage BOAT/TRO'}}).addTo(map);
const trackLayers = {{}};
const routeLayers = {{}};
const drawnItems = new L.FeatureGroup();
map.addLayer(drawnItems);
const drawControl = new L.Control.Draw({{
  draw: {{polygon:false, rectangle:false, circle:false, circlemarker:false, marker:true, polyline:{{shapeOptions:{{color:'#D10D0D', weight:7, opacity:0.95}}}}}},
  edit: {{featureGroup: drawnItems, remove:true}}
}});
map.addControl(drawControl);
map.on(L.Draw.Event.CREATED, e => drawnItems.addLayer(e.layer));
function removeBaseLayers() {{
  if (map.hasLayer(osm)) map.removeLayer(osm);
  if (qctLayer && map.hasLayer(qctLayer)) map.removeLayer(qctLayer);
}}
async function loadQct(mapId) {{
  if (!mapId) {{ document.getElementById('qctStatus').textContent = 'No QCT map selected.'; return; }}
  selectedMapId = mapId;
  const coverage = await fetch('/maps/coverage').then(r => r.json());
  const feature = (coverage.features || []).find(f => f.properties && f.properties.id === mapId);
  if (!feature) {{ document.getElementById('qctStatus').textContent = 'QCT map is not ready or has no coverage bounds.'; return; }}
  if (qctLayer && map.hasLayer(qctLayer)) map.removeLayer(qctLayer);
  qctLayer = L.tileLayer('/tiles/maps/' + encodeURIComponent(mapId) + '/{{z}}/{{x}}/{{y}}.png', {{maxZoom:18, opacity:1.0, attribution:'QCT OS 1:25k'}});
  removeBaseLayers();
  qctLayer.addTo(map);
  try {{ map.fitBounds(L.geoJSON(feature).getBounds(), {{padding:[20,20]}}); }} catch(e) {{}}
  document.querySelector('input[name="base"][value="qct"]').checked = true;
  document.getElementById('qctStatus').textContent = 'QCT map loaded.';
}}
function openSelectedQct() {{
  const id = document.getElementById('qctSelect').value;
  if (id) window.location.href = '/planner?map_id=' + encodeURIComponent(id);
}}
document.querySelectorAll('input[name="base"]').forEach(el => {{
  el.addEventListener('change', () => {{
    if (el.value === 'osm' && el.checked) {{ removeBaseLayers(); osm.addTo(map); }}
    if (el.value === 'qct' && el.checked) {{ loadQct(selectedMapId || document.getElementById('qctSelect').value); }}
    if (el.value === 'none' && el.checked) {{ removeBaseLayers(); }}
  }});
}});
document.getElementById('qctSelect').addEventListener('change', function() {{ selectedMapId = this.value; }});
document.getElementById('boatLayerToggle').addEventListener('change', function() {{ if (this.checked) boatOverlay.addTo(map); else map.removeLayer(boatOverlay); }});
document.getElementById('troLayerToggle').addEventListener('change', function() {{
  const wasOn = map.hasLayer(boatOverlay);
  if (wasOn) map.removeLayer(boatOverlay);
  boatOverlay = L.tileLayer(this.checked ? '/tiles/boats-coloured/{{z}}/{{x}}/{{y}}.png' : '/tiles/boats/{{z}}/{{x}}/{{y}}.png', {{maxZoom:18, opacity:0.90, attribution:'HotSausage BOAT/TRO'}});
  if (wasOn) boatOverlay.addTo(map);
}});
async function toggleTrack(trackId, enabled) {{
  if (!enabled) {{ if (trackLayers[trackId]) map.removeLayer(trackLayers[trackId]); return; }}
  const data = await fetch('/geojson/tracks/' + encodeURIComponent(trackId)).then(r=>r.json());
  const layer = L.geoJSON(data, {{style:{{color:'#9333ea', weight:7, opacity:0.95, dashArray:'10 8'}}}});
  trackLayers[trackId] = layer; layer.addTo(map); try {{ map.fitBounds(layer.getBounds(), {{padding:[20,20]}}); }} catch(e) {{}}
}}
async function toggleRoute(routeId, enabled) {{
  if (!enabled) {{ if (routeLayers[routeId]) map.removeLayer(routeLayers[routeId]); return; }}
  const data = await fetch('/routes/' + encodeURIComponent(routeId) + '/geojson').then(r=>r.json());
  const layer = L.geoJSON(data, {{style:{{color:'#D10D0D', weight:8, opacity:0.95}}}});
  routeLayers[routeId] = layer; layer.addTo(map); try {{ map.fitBounds(layer.getBounds(), {{padding:[20,20]}}); }} catch(e) {{}}
}}
document.querySelectorAll('.track-toggle').forEach(el => el.addEventListener('change', () => toggleTrack(el.value, el.checked)));
document.querySelectorAll('.route-toggle').forEach(el => el.addEventListener('change', () => toggleRoute(el.value, el.checked)));
function clearDrawings() {{ drawnItems.clearLayers(); }}
async function saveRoute() {{
  const fc = drawnItems.toGeoJSON();
  if (!fc.features.length) {{ alert('Draw a route first'); return; }}
  const body = new URLSearchParams();
  body.set('name', document.getElementById('routeName').value || 'Planned Route');
  body.set('geojson', JSON.stringify(fc));
  const data = await fetch('/routes/save', {{method:'POST', headers:{{'Content-Type':'application/x-www-form-urlencoded'}}, body}}).then(r=>r.json());
  if (data.state === 'saved') window.location.href = '/routes'; else alert(JSON.stringify(data, null, 2));
}}
if (selectedMapId) {{
  const sel = document.getElementById('qctSelect');
  for (const opt of sel.options) {{ if (opt.value === selectedMapId) opt.selected = true; }}
  loadQct(selectedMapId);
}}
</script>
</body>
</html>"""

def routes_page() -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>Routes</title>{brand_css()}</head><body>{brand_header("Routes", "Saved planned routes and GPX exports")}<main>
<section><a class="button" href="/planner">Open Planner</a></section>
<section><table><thead><tr><th>Name</th><th>ID</th><th>Points</th><th>Actions</th></tr></thead><tbody>{routes_rows()}</tbody></table></section>
<script>
async function saveAsTrack(id) {{
  const res = await fetch('/routes/' + encodeURIComponent(id) + '/save-track', {{method:'POST'}});
  alert(JSON.stringify(await res.json(), null, 2));
}}
</script>
</main></body></html>"""

def maps_page() -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>QCT Maps</title>{brand_css()}</head><body>{brand_header("QCT Maps", "Upload and index private OS QCT/QC3 maps")}<main>
<section><h2>Upload QCT/QC3</h2><form method="post" action="/maps/upload-qct" enctype="multipart/form-data"><label>Name</label><input name="name"><label>Scale</label><select name="scale"><option>1:25k</option><option>1:50k</option><option>Other</option></select><input name="file" type="file" accept=".qct,.qc3"><button>Upload Map</button></form></section>
<section><h2>Uploaded Maps</h2><table><thead><tr><th>Name</th><th>ID</th><th>Scale</th><th>Status</th><th>Note</th><th>Action</th></tr></thead><tbody>{maps_rows()}</tbody></table></section>
<section><a class="button secondary" href="/maps/index">Open UK Map Index</a></section>
<script>
async function importMap(id) {{
  if (!confirm('Import/convert this QCT map now? This can take several minutes.')) return;
  const res = await fetch('/maps/' + encodeURIComponent(id) + '/import', {{method:'POST'}});
  alert(JSON.stringify(await res.json(), null, 2));
  location.reload();
}}
async function deleteMap(id) {{
  if (!confirm('Delete this QCT map and its converted tiles/images?')) return;
  const res = await fetch('/maps/' + encodeURIComponent(id), {{method:'DELETE'}});
  alert(JSON.stringify(await res.json(), null, 2));
  location.reload();
}}
</script></main></body></html>"""

def maps_index_page() -> str:
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>QCT Map Index</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">{brand_css()}<style>#map{{height:75vh;border-radius:14px}}</style></head>
<body>{brand_header("UK QCT Map Index", "Click a ready 1:25k map coverage box to open it in Planner")}<main>
<section><p>Uploaded QCT maps with extracted georeference bounds appear as clickable coverage boxes. If a map is still uploaded-only, click Import/Convert in QCT Maps.</p><a class="button" href="/maps">Manage Maps</a><a class="button secondary" href="/planner">Open Planner</a></section>
<section><div id="map"></div></section>
<section><h2>Maps</h2><table><thead><tr><th>Name</th><th>ID</th><th>Scale</th><th>Status</th><th>Note</th><th>Action</th></tr></thead><tbody>{maps_rows()}</tbody></table></section>
</main><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const map=L.map('map').setView([54.5,-3],6);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:18}}).addTo(map);
fetch('/maps/coverage').then(r=>r.json()).then(data=>{{
  const layer=L.geoJSON(data,{{
    style:{{color:'#D10D0D',weight:2,fillOpacity:0.12}},
    onEachFeature:(feature, lyr)=>{{
      const p=feature.properties || {{}};
      lyr.bindPopup(`<b>${{p.name}}</b><br>${{p.scale}}<br>${{p.status}}<br><a href="/planner?map_id=${{p.id}}">Open in Planner</a>`);
      lyr.on('click',()=>{{ if(p.status==='ready') window.location.href='/planner?map_id='+encodeURIComponent(p.id); }});
    }}
  }}).addTo(map);
  try {{ map.fitBounds(layer.getBounds(), {{padding:[20,20]}}); }} catch(e) {{}}
}});
async function importMap(id) {{
  const res = await fetch('/maps/' + encodeURIComponent(id) + '/import', {{method:'POST'}});
  alert(JSON.stringify(await res.json(), null, 2));
  location.reload();
}}
</script></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_get():
    return login_page()

@app.post("/login")
def login_post(username: str = Form(...), password: str = Form(...)):
    users = load_users()
    user = next((u for u in users.get("users", []) if u.get("username") == username and u.get("enabled", True)), None)
    if not user or not verify_password(password, user.get("password_hash", "")):
        return HTMLResponse(login_page("Invalid username or password"), status_code=401)
    sid = secrets.token_urlsafe(32)
    sessions = load_sessions()
    sessions.setdefault("sessions", []).append({"id": sid, "username": username, "created_at": utc_now()})
    save_sessions(sessions)
    user["last_login"] = utc_now()
    save_users(users)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("hotsausage_session", sid, httponly=True, secure=False, samesite="lax", max_age=60*60*24*14)
    return response

@app.get("/logout")
def logout(request: Request):
    sid = request.cookies.get("hotsausage_session", "")
    sessions = load_sessions()
    sessions["sessions"] = [s for s in sessions.get("sessions", []) if s.get("id") != sid]
    save_sessions(sessions)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("hotsausage_session")
    return response

@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    user = get_current_user(request)
    if not is_admin(user):
        return RedirectResponse("/login", status_code=303)
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>HotSausage Admin</title>{brand_css()}</head><body>{brand_header("HotSausage Byways Admin", "Users, invites and access control")}<main>
<section><h2>Create Invite</h2><form method='post' action='/admin/invites/create'><label>Role</label><select name='role'><option value='viewer'>Viewer</option><option value='editor'>Editor</option><option value='admin'>Admin</option></select><button>Create Invite</button></form></section>
<section><h2>Users</h2><table><thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Enabled</th><th>Last login</th></tr></thead><tbody>{admin_user_rows()}</tbody></table></section>
<section><h2>Invites</h2><table><thead><tr><th>Token</th><th>Role</th><th>Used By</th><th>Invite Link</th></tr></thead><tbody>{invite_rows()}</tbody></table></section>
</main></body></html>"""

@app.post("/admin/invites/create")
def admin_invite_create(request: Request, role: str = Form("viewer")):
    user = get_current_user(request)
    if not is_admin(user):
        return RedirectResponse("/login", status_code=303)
    create_invite(role)
    return RedirectResponse("/admin/users", status_code=303)

@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_get(token: str):
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>Join HotSausage Byways</title>{brand_css()}</head><body>{brand_header("Join HotSausage Byways", "Create your account")}<main style='max-width:560px'><section><form method='post' action='/invite/{token}'><label>Display name</label><input name='display_name'><label>Username</label><input name='username'><label>Password</label><input name='password' type='password'><button>Create Account</button></form></section></main></body></html>"""

@app.post("/invite/{token}")
def invite_post(token: str, username: str = Form(...), password: str = Form(...), display_name: str = Form("")):
    result = create_user_from_invite(token, username, password, display_name)
    if result.get("state") != "created":
        return HTMLResponse(login_page(result.get("error", "Invite failed")), status_code=400)
    return RedirectResponse("/login", status_code=303)


@app.get("/source-discovery", response_class=HTMLResponse)
def source_discovery():
    return discovery_html()

@app.post("/discover/run")
def discover_run():
    return JSONResponse(discover_sources())

@app.get("/discover/status")
def discover_status():
    return JSONResponse(load_discovery())

@app.post("/dtro/test")
def dtro_test():
    payload = test_dtro_api()
    return JSONResponse(payload, status_code=200 if payload.get("state") == "ok" else 400)

@app.post("/dtro/sync")
def dtro_sync():
    try:
        return JSONResponse(sync_dtro_api())
    except Exception as exc:
        return JSONResponse({"state": "error", "error": str(exc)}, status_code=400)

@app.post("/source/promote")
def source_promote(candidate_id: str = Form(...), name: str = Form(""), kind: str = Form(""), source_type: str = Form(""), url: str = Form(""), notes: str = Form("")):
    result = promote_candidate(candidate_id, name=name, kind=kind, source_type=source_type, url=url, notes=notes)
    status_code = 200 if result.get("state") in ("promoted", "already_promoted") else 400
    return JSONResponse(result, status_code=status_code)


@app.get("/planner", response_class=HTMLResponse)
def planner():
    return planner_page()

@app.get("/routes", response_class=HTMLResponse)
def routes_ui():
    return routes_page()

@app.get("/routes/list")
def routes_list():
    return JSONResponse(load_routes())

@app.post("/routes/save")
def routes_save(name: str = Form(""), geojson: str = Form(...)):
    try:
        payload = json.loads(geojson)
        route = save_planned_route(name, payload)
        return JSONResponse({"state": "saved", "route": route})
    except Exception as exc:
        return JSONResponse({"state": "error", "error": str(exc)}, status_code=400)


@app.get("/routes/{route_id}/geojson")
def routes_geojson(route_id: str):
    route = next((r for r in load_routes().get("routes", []) if r.get("id") == route_id), None)
    if not route:
        return JSONResponse({"state": "error", "error": "route not found"}, status_code=404)
    return JSONResponse(read_json(route_geojson_path(route_id), {"type": "FeatureCollection", "features": []}))

@app.get("/routes/{route_id}/export-gpx")
def routes_export_gpx(route_id: str):
    try:
        gpx = route_to_gpx(route_id)
        return Response(
            content=gpx,
            media_type="application/gpx+xml",
            headers={"Content-Disposition": f'attachment; filename="{route_id}.gpx"'},
        )
    except Exception as exc:
        return JSONResponse({"state": "error", "error": str(exc)}, status_code=404)

@app.post("/routes/{route_id}/save-track")
def routes_save_track(route_id: str):
    result = save_route_as_track(route_id)
    return JSONResponse(result, status_code=200 if result.get("state") == "saved_as_track" else 404)

@app.get("/maps", response_class=HTMLResponse)
def maps_ui():
    return maps_page()

@app.get("/maps/list")
def maps_list():
    return JSONResponse(load_maps())

@app.get("/maps/index", response_class=HTMLResponse)
def maps_index():
    return maps_index_page()


@app.get("/maps/coverage")
def maps_coverage():
    return JSONResponse(map_coverage_geojson())

@app.post("/maps/{map_id}/import")
def maps_import(map_id: str):
    return JSONResponse(import_qct_map(map_id))

@app.get("/tiles/maps/{map_id}/{z}/{x}/{y}.png")
@app.head("/tiles/maps/{map_id}/{z}/{x}/{y}.png")
def map_tile(map_id: str, z: int, x: int, y: int):
    data = render_map_tile(map_id, z, x, y)
    if data is None:
        return Response(status_code=404, content=b"Map tile not found")
    return png_response(data)


@app.delete("/maps/{map_id}")
def maps_delete(map_id: str):
    return JSONResponse(delete_map_id(map_id))

@app.post("/maps/upload-qct")
async def maps_upload_qct(name: str = Form(""), scale: str = Form("1:25k"), file: UploadFile = File(...)):
    filename = file.filename or "map.qct"
    if not filename.lower().endswith((".qct", ".qc3")):
        return JSONResponse({"state": "error", "error": "file must be .qct or .qc3"}, status_code=400)
    content = await file.read()
    record = save_uploaded_qct(name, scale, filename, content)
    return RedirectResponse("/maps", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": APP_NAME, "version": APP_VERSION}

@app.get("/status")
def status():
    return JSONResponse(load_status())

@app.get("/authorities")
def authorities():
    return JSONResponse(load_authority_registry())

@app.post("/authorities/add-placeholders")
def authorities_add_placeholders():
    return JSONResponse(ensure_authority_placeholders())

@app.get("/sources")
def sources():
    return JSONResponse(load_sources())

@app.post("/sources/add")
def add_source(name: str = Form(...), kind: str = Form(...), source_type: str = Form(...), url: str = Form(""), notes: str = Form("")):
    if kind not in ("byway","tro"):
        return JSONResponse({"state":"error","error":"kind must be byway or tro"}, status_code=400)
    if source_type not in ("geojson_url","arcgis_geojson_url","dtro_api","text_only_url"):
        return JSONResponse({"state":"error","error":"unsupported source_type"}, status_code=400)
    if source_type not in ("text_only_url", "dtro_api"):
        parsed=urlparse(url)
        if parsed.scheme not in ("http","https"):
            return JSONResponse({"state":"error","error":"URL must start with http or https"}, status_code=400)
    return JSONResponse({"state":"added","source":add_source_record(name, kind, source_type, url, notes)})

@app.post("/sources/delete/{source_id}")
def delete_source(source_id: str):
    return JSONResponse({"state":"deleted" if delete_source_id(source_id) else "not_found","source_id":source_id})

@app.post("/upload-geojson")
async def upload_geojson(kind: str = Form(...), file: UploadFile = File(...)):
    if kind not in ("byway","tro"):
        return JSONResponse({"state":"error","error":"kind must be byway or tro"}, status_code=400)
    safe="".join(c for c in file.filename if c.isalnum() or c in ("-","_","."," ")).strip() or f"upload-{int(time.time())}.geojson"
    if not safe.endswith((".geojson",".json")):
        safe += ".geojson"
    content=await file.read()
    try:
        json.loads(content.decode("utf-8"))
    except Exception as exc:
        return JSONResponse({"state":"error","error":f"invalid JSON: {exc}"}, status_code=400)
    target=(MANUAL_BYWAY_DIR if kind=="byway" else MANUAL_TRO_DIR)/safe
    ensure_dirs()
    target.write_bytes(content)
    rec=add_source_record(safe, kind, "manual_upload", str(target), "Uploaded from web UI")
    return JSONResponse({"state":"uploaded","file":str(target),"source":rec,"note":"Run full update to merge it."})

@app.get("/tracks")
def tracks():
    return JSONResponse(load_tracks())

@app.post("/tracks/upload")
async def upload_track(name: str = Form(""), file: UploadFile = File(...)):
    safe_name = file.filename or "track.gpx"
    if not safe_name.lower().endswith(".gpx"):
        return JSONResponse({"state":"error","error":"file must be a .gpx track"}, status_code=400)
    content = await file.read()
    try:
        track = save_gpx_track(name, safe_name, content)
    except ValueError as exc:
        return JSONResponse({"state":"error","error":str(exc)}, status_code=400)
    return JSONResponse({"state":"uploaded","track":track,"note":"Add the Traccar tile URL as a named image overlay to display this purple dotted track."})

@app.post("/tracks/delete/{track_id}")
def delete_track(track_id: str):
    return JSONResponse({"state":"deleted" if delete_track_id(track_id) else "not_found","track_id":track_id})

@app.post("/update")
def manual_update():
    return JSONResponse(update_all())

@app.get("/geojson/byways")
def geojson_byways():
    return JSONResponse(read_json(MERGED_BYWAYS_PATH, {"type":"FeatureCollection","features":[]}))

@app.get("/geojson/tros")
def geojson_tros():
    return JSONResponse(read_json(MERGED_TROS_PATH, {"type":"FeatureCollection","features":[]}))

@app.get("/geojson/analysed")
def geojson_analysed():
    return JSONResponse(read_json(ANALYSED_BYWAYS_PATH, {"type":"FeatureCollection","features":[]}))

@app.get("/geojson/tracks/{track_id}")
def geojson_track(track_id: str):
    return JSONResponse({"type":"FeatureCollection","features":load_track_features(track_id)})

@app.get("/tiles/boats/{z}/{x}/{y}.png")
@app.head("/tiles/boats/{z}/{x}/{y}.png")
def plain_tile(z:int,x:int,y:int):
    data=tile_bytes(PLAIN_TILE_DIR,z,x,y,False)
    if data is None:
        return Response(status_code=400, content=b"Invalid zoom")
    return png_response(data)

@app.get("/tiles/boats-coloured/{z}/{x}/{y}.png")
@app.head("/tiles/boats-coloured/{z}/{x}/{y}.png")
def coloured_tile(z:int,x:int,y:int):
    data=tile_bytes(COLOUR_TILE_DIR,z,x,y,True)
    if data is None:
        return Response(status_code=400, content=b"Invalid zoom")
    return png_response(data)

@app.get("/tiles/tracks/{track_id}/{z}/{x}/{y}.png")
@app.head("/tiles/tracks/{track_id}/{z}/{x}/{y}.png")
@app.get("/tiles/ytrack/{track_id}/{z}/{x}/{y}.png")
@app.head("/tiles/ytrack/{track_id}/{z}/{x}/{y}.png")
def track_tile(track_id: str, z:int, x:int, y:int):
    data=track_tile_bytes(track_id,z,x,y)
    if data is None:
        return Response(status_code=404, content=b"Track tile not found")
    return png_response(data)

@app.on_event("startup")
def startup():
    ensure_dirs()
    load_sources()
    load_users()
    load_invites()
    load_tracks()
    scheduler=BackgroundScheduler()
    scheduler.add_job(update_all, "cron", hour=UPDATE_HOUR, minute=0)
    scheduler.start()
    if not ANALYSED_BYWAYS_PATH.exists():
        threading.Thread(target=update_all, daemon=True).start()
