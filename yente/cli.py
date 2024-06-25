import click
import asyncio
from typing import Any
from uvicorn import Config, Server

from yente import settings
from yente.app import create_app
from yente.logs import configure_logging, get_logger
from yente.search.base import get_es
from yente.search.indexer import update_index, delta_update_catalog


log = get_logger("yente")


@click.group(help="yente API server")
def cli() -> None:
    pass


@cli.command("serve", help="Run uvicorn and serve requests")
def serve() -> None:
    app = create_app()
    server = Server(
        Config(
            app,
            host="0.0.0.0",
            port=settings.PORT,
            proxy_headers=True,
            reload=settings.DEBUG,
            # reload_dirs=[code_dir],
            # debug=settings.DEBUG,
            log_level=settings.LOG_LEVEL,
            server_header=False,
        ),
    )
    configure_logging()
    server.run()


@cli.command("reindex", help="Re-index the data if newer data is available")
@click.option("-f", "--force", is_flag=True, default=False)
def reindex(force: bool) -> None:
    configure_logging()
    asyncio.run(update_index(force=force))


@cli.command("delta-update", help="Update the index with new data only")
def delta_update() -> None:
    configure_logging()
    asyncio.run(delta_update_catalog())


async def _clear_index() -> None:
    es = await get_es()
    indices: Any = await es.cat.indices(format="json")
    for index in indices:
        index_name: str = index.get("index")
        if index_name.startswith(settings.ES_INDEX):
            log.info("Delete index", index=index_name)
            await es.indices.delete(index=index_name)
    await es.close()


@cli.command("clear-index", help="Delete everything in ElasticSearch")
def clear_index() -> None:
    configure_logging()
    asyncio.run(_clear_index())


if __name__ == "__main__":
    cli()
