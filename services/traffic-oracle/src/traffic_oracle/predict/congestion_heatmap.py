"""Congestion Heatmap Generator — produces spatial congestion density surfaces
for GIS visualization on the command-deck map layer.

Converts discrete segment-level congestion readings into continuous
spatial heatmap tiles consumable by MapLibre GL.
"""

from __future__ import annotations

import io
import math
import struct
import zlib

import numpy as np
import structlog

from traffic_oracle.data.cache import TrafficCache

logger = structlog.get_logger(__name__)

# Corridor segment IDs matching signal-ingress ROAD_NETWORK
_CORRIDOR_SEGMENTS = [1001, 1002, 1003, 1004, 1005]

# Meters per degree at Delhi latitude (~28.6°N)
_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(28.6))

# Congestion colormap: (threshold, R, G, B, A)
_COLORMAP = np.array(
    [
        [0.0, 34, 139, 34, 0],
        [0.2, 50, 205, 50, 100],
        [0.4, 255, 255, 0, 160],
        [0.6, 255, 165, 0, 200],
        [0.8, 255, 69, 0, 230],
        [1.0, 220, 20, 20, 255],
    ],
    dtype=np.float64,
)


def _congestion_to_rgba(congestion: np.ndarray) -> np.ndarray:
    """Map congestion values (0-1) to RGBA using piecewise-linear colormap."""
    c = np.clip(congestion, 0.0, 1.0)
    thresholds = _COLORMAP[:, 0]
    rgba = np.zeros((*c.shape, 4), dtype=np.uint8)

    for i in range(len(thresholds) - 1):
        lo, hi = thresholds[i], thresholds[i + 1]
        mask = (c >= lo) & (c <= hi)
        if not np.any(mask):
            continue
        t = np.where(mask, (c - lo) / (hi - lo), 0.0)
        for ch in range(4):
            val = _COLORMAP[i, ch + 1] + t * (_COLORMAP[i + 1, ch + 1] - _COLORMAP[i, ch + 1])
            rgba[..., ch] = np.where(mask, np.clip(val, 0, 255).astype(np.uint8), rgba[..., ch])

    return rgba


def _encode_png(rgba: np.ndarray) -> bytes:
    """Encode an (H, W, 4) uint8 RGBA array as a PNG file."""
    h, w = rgba.shape[:2]
    buf = io.BytesIO()

    # PNG signature
    buf.write(b"\x89PNG\r\n\x1a\n")

    def _write_chunk(chunk_type: bytes, data: bytes) -> None:
        buf.write(struct.pack(">I", len(data)))
        buf.write(chunk_type)
        buf.write(data)
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        buf.write(struct.pack(">I", crc))

    # IHDR: width, height, bit_depth=8, color_type=6 (RGBA)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    _write_chunk(b"IHDR", ihdr)

    # IDAT: filtered scanlines (filter type 0 = None per row)
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter byte
        raw.extend(rgba[y].tobytes())
    compressed = zlib.compress(bytes(raw), 6)
    _write_chunk(b"IDAT", compressed)

    # IEND
    _write_chunk(b"IEND", b"")

    return buf.getvalue()


class CongestionHeatmap:
    """Generates spatial congestion heatmap tiles from segment observations."""

    def __init__(self, cache: TrafficCache) -> None:
        self._cache = cache

    async def generate(
        self,
        bbox: tuple[float, float, float, float],
        resolution_m: int = 100,
    ) -> bytes:
        """Generate a congestion heatmap for the given bounding box.

        Args:
            bbox: (min_lon, min_lat, max_lon, max_lat) in WGS84
            resolution_m: Grid cell size in meters

        Returns:
            PNG image bytes of the heatmap tile
        """
        min_lon, min_lat, max_lon, max_lat = bbox

        # Fetch latest observations from cache
        points: list[tuple[float, float, float]] = []  # (lon, lat, congestion)
        for seg_id in _CORRIDOR_SEGMENTS:
            obs = await self._cache.get_latest(seg_id)
            if obs is None:
                continue
            if min_lon <= obs.lon <= max_lon and min_lat <= obs.lat <= max_lat:
                points.append((obs.lon, obs.lat, obs.congestion_idx))

        if not points:
            logger.info("congestion_heatmap.generate.no_data", bbox=bbox)
            return b""

        pts = np.array(points)
        lons, lats, congs = pts[:, 0], pts[:, 1], pts[:, 2]

        # Grid dimensions
        width_m = (max_lon - min_lon) * _M_PER_DEG_LON
        height_m = (max_lat - min_lat) * _M_PER_DEG_LAT
        nx = max(2, int(width_m / resolution_m))
        ny = max(2, int(height_m / resolution_m))

        gx = np.linspace(min_lon, max_lon, nx)
        gy = np.linspace(min_lat, max_lat, ny)
        grid_lon, grid_lat = np.meshgrid(gx, gy)

        # KDE for observation density (visibility)
        try:
            from scipy.stats import gaussian_kde

            coords = np.vstack([lons, lats])
            kde = gaussian_kde(coords, bw_method=0.05)
            grid_pts = np.vstack([grid_lon.ravel(), grid_lat.ravel()])
            density = kde(grid_pts).reshape(ny, nx)
            density = density / (density.max() + 1e-12)
        except (ImportError, np.linalg.LinAlgError):
            # Fallback: uniform density
            density = np.ones((ny, nx))

        # IDW interpolation for congestion values
        congestion_grid = np.zeros((ny, nx))
        for i in range(len(lons)):
            dx = (grid_lon - lons[i]) * _M_PER_DEG_LON
            dy = (grid_lat - lats[i]) * _M_PER_DEG_LAT
            dist = np.sqrt(dx**2 + dy**2) + 1.0  # +1m to avoid division by zero
            weight = 1.0 / dist**2
            congestion_grid += weight * congs[i]

        total_weight = np.zeros((ny, nx))
        for i in range(len(lons)):
            dx = (grid_lon - lons[i]) * _M_PER_DEG_LON
            dy = (grid_lat - lats[i]) * _M_PER_DEG_LAT
            dist = np.sqrt(dx**2 + dy**2) + 1.0
            total_weight += 1.0 / dist**2

        congestion_grid = congestion_grid / (total_weight + 1e-12)
        congestion_grid = np.clip(congestion_grid, 0.0, 1.0)

        # Blend density into alpha (observations far from any point fade out)
        blended = congestion_grid * density

        # Map to RGBA — flip vertically so lat increases upward
        rgba = _congestion_to_rgba(blended[::-1])

        png_bytes = _encode_png(rgba)

        logger.info(
            "congestion_heatmap.generate",
            bbox=bbox,
            resolution_m=resolution_m,
            points=len(points),
            grid_size=(nx, ny),
            png_bytes=len(png_bytes),
        )
        return png_bytes
