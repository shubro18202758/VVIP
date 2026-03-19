"""CLI entry point for the GIS network importer."""

from __future__ import annotations

import asyncio

import click
import structlog

from corridor_importer.loader import BulkLoader
from corridor_importer.osm_extractor import OSMExtractor
from corridor_importer.schema_mapper import SchemaMapper
from corridor_importer.topology_builder import TopologyBuilder

logger = structlog.get_logger()


@click.group()
def main() -> None:
    """Corridor GIS Network Importer — load OSM road networks into PostGIS."""
    structlog.configure(
        processors=[
            structlog.dev.ConsoleRenderer(),
        ],
    )


@main.command()
@click.option(
    "--bbox",
    nargs=4,
    type=float,
    required=False,
    help="Bounding box: south north west east (lat/lon)",
)
@click.option("--place", type=str, required=False, help="Place name for geocoding (e.g. 'New Delhi, India')")
@click.option("--db-url", type=str, required=True, envvar="DATABASE_URL", help="PostgreSQL connection URL")
def import_network(
    bbox: tuple[float, float, float, float] | None,
    place: str | None,
    db_url: str,
) -> None:
    """Extract road network from OSM and load into PostGIS."""
    if bbox is None and place is None:
        raise click.UsageError("Provide either --bbox or --place")

    asyncio.run(_import_network(bbox, place, db_url))


async def _import_network(
    bbox: tuple[float, float, float, float] | None,
    place: str | None,
    db_url: str,
) -> None:
    extractor = OSMExtractor()

    if bbox:
        logger.info("extracting road network by bbox", bbox=bbox)
        nodes, edges = extractor.extract_by_bbox(*bbox)
    else:
        logger.info("extracting road network by place", place=place)
        nodes, edges = extractor.extract_by_place(place)

    mapper = SchemaMapper()
    segments = mapper.map_edges(edges)
    junctions = mapper.map_nodes(nodes)
    logger.info("schema mapping complete", segments=len(segments), junctions=len(junctions))

    builder = TopologyBuilder()
    adjacency = builder.build_adjacency(segments)
    logger.info("topology built", adjacency_count=len(adjacency))

    loader = BulkLoader(db_url)
    await loader.connect()
    try:
        await loader.load_segments(segments)
        await loader.load_junctions(junctions)
        await loader.load_adjacency(adjacency)
        logger.info("bulk load complete")
    finally:
        await loader.close()
