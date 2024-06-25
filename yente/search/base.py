from abc import abstractmethod, ABC
import time
import asyncio
import warnings
from threading import Lock
from typing import cast, Any, Dict, List, AsyncGenerator, Coroutine, Tuple
from structlog.contextvars import get_contextvars
from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk
from elasticsearch.exceptions import (
    ElasticsearchWarning,
    BadRequestError,
    NotFoundError,
)
from elasticsearch.exceptions import TransportError, ConnectionError
from followthemoney import model
from followthemoney.types.date import DateType


from yente import settings
from yente.logs import get_logger
from yente.data.entity import Entity
from yente.data.dataset import Dataset
from yente.search.mapping import (
    make_entity_mapping,
    INDEX_SETTINGS,
    NAMES_FIELD,
    NAME_PHONETIC_FIELD,
    NAME_PART_FIELD,
    NAME_KEY_FIELD,
)
from yente.search.util import (
    parse_index_name,
    construct_index_name,
    index_to_dataset_version,
)
from yente.data.util import expand_dates, phonetic_names
from yente.data.util import index_name_parts, index_name_keys

warnings.filterwarnings("ignore", category=ElasticsearchWarning)

log = get_logger(__name__)
POOL: Dict[int, AsyncElasticsearch] = {}
query_semaphore = asyncio.Semaphore(settings.QUERY_CONCURRENCY)
index_lock = Lock()


def get_opaque_id() -> str:
    ctx = get_contextvars()
    return cast(str, ctx.get("trace_id"))


def get_es_connection() -> AsyncElasticsearch:
    """Get elasticsearch connection."""
    kwargs: Dict[str, Any] = dict(
        request_timeout=30,
        retry_on_timeout=True,
        max_retries=10,
    )
    if settings.ES_SNIFF:
        kwargs["sniff_on_start"] = True
        kwargs["sniffer_timeout"] = 60
        kwargs["sniff_on_connection_fail"] = True
    if settings.ES_CLOUD_ID:
        log.info("Connecting to Elastic Cloud ID", cloud_id=settings.ES_CLOUD_ID)
        kwargs["cloud_id"] = settings.ES_CLOUD_ID
    else:
        kwargs["hosts"] = [settings.ES_URL]
    if settings.ES_USERNAME and settings.ES_PASSWORD:
        auth = (settings.ES_USERNAME, settings.ES_PASSWORD)
        kwargs["basic_auth"] = auth
    if settings.ES_CA_CERT:
        kwargs["ca_certs"] = settings.ES_CA_CERT
    return AsyncElasticsearch(**kwargs)


async def get_es() -> AsyncElasticsearch:
    loop = asyncio.get_running_loop()
    loop_id = hash(loop)
    if loop_id in POOL:
        return POOL[loop_id]

    for retry in range(2, 9):
        try:
            es = get_es_connection()
            es_ = es.options(request_timeout=5)
            await es_.cluster.health(wait_for_status="yellow")
            POOL[loop_id] = es
            return POOL[loop_id]
        except (TransportError, ConnectionError) as exc:
            log.error("Cannot connect to ElasticSearch: %r" % exc)
            time.sleep(retry**2)
    raise RuntimeError("Cannot connect to ElasticSearch")


async def close_es() -> None:
    loop = asyncio.get_running_loop()
    loop_id = hash(loop)
    es = POOL.pop(loop_id, None)
    if es is not None:
        log.info("Closing elasticsearch client")
        await es.close()


class SearchProvider(ABC):
    client: Any

    @abstractmethod
    async def create(cls) -> "SearchProvider":
        pass

    @abstractmethod
    async def upsert_index(self, index: str) -> None:
        pass

    @abstractmethod
    async def clone_index(self, index: str, new_index: str) -> None:
        pass

    @abstractmethod
    async def index_exists(self, index: str) -> bool:
        pass

    @abstractmethod
    async def delete_index(self, index: str) -> None:
        pass

    @abstractmethod
    async def rollover(self, alias: str, new_index: str, prefix: str = "") -> None:
        pass

    @abstractmethod
    async def update(
        self, entities: AsyncGenerator[Dict[str, Any], None], index_name: str
    ) -> Tuple[int, int]:
        pass

    @abstractmethod
    async def count(self, index: str) -> int:
        """
        Get the number of documents in an index.
        """
        pass

    @abstractmethod
    def _delete_operation(self, doc_id: str, index: str) -> Dict[str, Any]:
        """
        Return a document delete payload that can be accepted by the concrete search provider.
        """
        pass

    @abstractmethod
    def _create_operation(
        self, entity: Dict[str, Any], doc_id: str, index: str
    ) -> Dict[str, Any]:
        """
        Return a document create payload that can be accepted by the concrete search provider.
        """
        pass

    @abstractmethod
    def _update_operation(
        self, entity: Dict[str, Any], doc_id: str, index: str
    ) -> Dict[str, Any]:
        """
        Return a document update payload that can be accepted by the concrete search provider.
        """
        pass

    @abstractmethod
    async def get_backing_indexes(self, name: str) -> List[str]:
        """
        Get all the indexes backing Yente search. In ElasticSearch this would be implemented
        with multiple indexes pointing to the alias specified by the name parameter.
        """
        pass

    def _to_operation(self, body: Dict[str, Any], index: str) -> Dict[str, Any]:
        """
        Convert an entity to a bulk operation.
        """
        try:
            entity = body.pop("entity")
            doc_id = entity.pop("id")
        except KeyError:
            raise ValueError("No entity or ID in body.\n", body)
        match body.get("op"):
            case "ADD":
                return self._create_operation(entity, doc_id, index)
            case "MOD":
                return self._update_operation(entity, doc_id, index)
            case "DEL":
                return self._delete_operation(doc_id, index)
            case _:
                raise ValueError(f"Unknown operation type: {body.get('op')}")


class ESSearchProvider(SearchProvider):

    @classmethod
    async def create(cls) -> "ESSearchProvider":
        self = cls()
        self.client = await get_es()
        return self

    async def __aenter__(self) -> "ESSearchProvider":
        self.client = await get_es()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.client.close()

    async def upsert_index(self, index: str) -> None:
        """
        Create an index if it does not exist. If it does exist, do nothing.
        """
        try:
            schemata = list(model.schemata.values())
            mapping = make_entity_mapping(schemata)
            await self.client.indices.create(
                index=index, mappings=mapping, settings=INDEX_SETTINGS
            )
        except BadRequestError:
            pass

    async def _remove_write_block(self, index: str) -> None:
        await self.client.indices.put_settings(
            index=index, settings={"index.blocks.read_only": False}
        )

    async def _add_write_block(self, index: str) -> None:
        await self.client.indices.put_settings(
            index=index, settings={"index.blocks.read_only": True}
        )

    async def clone_index(self, index: str, new_index: str) -> None:
        try:
            await self._add_write_block(index)
            await self.client.indices.clone(
                index=index,
                target=new_index,
                body={
                    "settings": {"index": {"blocks": {"read_only": False}}},
                },
            )
        finally:
            await self._remove_write_block(index)

    async def delete_index(self, index: str) -> None:
        """
        Delete a given index if it exists.
        """
        try:
            await self.client.indices.delete(index=index)
        except NotFoundError:
            pass

    async def get_backing_indexes(self, name: str) -> List[str]:
        resp = await self.client.indices.get_alias(name=name)
        return list(resp.keys())

    async def index_exists(self, index: str) -> bool:
        exists = await self.client.indices.exists(index=index)
        if exists.body:
            log.info("Index is up to date.", index=index)
            return True
        return False

    async def rollover(self, alias: str, new_index: str, prefix: str = "") -> None:
        """
        Remove all existing indices with a given prefix from the alias and add the new one.
        """
        actions = []
        actions.append({"remove": {"index": f"{prefix}*", "alias": alias}})
        actions.append({"add": {"index": new_index, "alias": alias}})
        await self.client.indices.update_aliases(actions=actions)
        return None

    async def count(self, index: str) -> int:
        resp = await self.client.count(index=index)
        return int(resp["count"])

    async def get_alias_sources(self, alias: str) -> List[str]:
        resp = await self.client.indices.get_alias(name=alias)
        return list(resp.keys())

    async def refresh(self, index: str) -> None:
        await self.client.indices.refresh(index=index)

    async def add_alias(self, index: str, alias: str) -> None:
        await self.client.indices.put_alias(index=index, name=alias)

    async def update(
        self, entities: AsyncGenerator[Dict[str, Any], None], index_name: str
    ) -> Tuple[int, int]:
        """
        Update the index with the given entities in bulk.
        Return a tuple of the number of successful and failed operations.
        """
        resp = await async_bulk(
            client=self.client,
            actions=self._entity_iterator(entities, index_name),
            chunk_size=500,
            raise_on_error=True,
            stats_only=True,
        )
        return cast(Tuple[int, int], resp)

    async def _entity_iterator(
        self, async_entities: AsyncGenerator[Dict[str, Any], None], index: str
    ) -> AsyncGenerator[Dict[str, Any], Any]:
        async for data in async_entities:
            yield self._to_operation(data, index)

    def _delete_operation(self, doc_id: str, index: str) -> Dict[str, Any]:
        return {
            "_op_type": "delete",
            "_index": index,
            "_id": doc_id,
        }

    def _update_operation(
        self, entity: Dict[str, Any], doc_id: str, index: str
    ) -> Dict[str, Any]:
        return self._create_operation(entity, doc_id, index)

    def _create_operation(
        self, entity: Dict[str, Any], doc_id: str, index: str
    ) -> Dict[str, Any]:
        return make_indexable(entity) | {
            "_op_type": "index",
            "_index": index,
            "_id": doc_id,
        }


class Index:
    def __init__(self, client: SearchProvider, dataset_name: str, version: str) -> None:
        self.dataset = dataset_name
        self.name = construct_index_name(dataset_name, version)
        self.client = client

    async def exists(self) -> bool:
        return await self.client.index_exists(self.name)

    def upsert(self) -> Coroutine[None, None, None]:
        return self.client.upsert_index(index=self.name)

    def delete(self) -> Coroutine[None, None, None]:
        return self.client.delete_index(index=self.name)

    async def clone(self, version: str) -> "Index":
        """
        Create a copy of the index with the given name.
        """
        cloned_index = Index(self.client, self.dataset, version)
        if cloned_index.name == self.name:
            raise ValueError("Cannot clone an index to itself.")
        await self.client.clone_index(self.name, cloned_index.name)
        return cloned_index

    def make_main(self) -> Coroutine[None, None, None]:
        """
        Makes this index the base for Yente searches.
        """
        return self.client.rollover(
            settings.ENTITY_INDEX, self.name, construct_index_name(self.dataset)
        )

    def bulk_update(
        self, entity_iterator: AsyncGenerator[Dict[str, Any], None]
    ) -> Coroutine[Any, Any, Tuple[int, int | List[Any]]]:
        return self.client.update(entity_iterator, self.name)


def make_indexable(data: Dict[str, Any]) -> Dict[str, Any]:
    entity = Entity.from_dict(model, data)
    texts = entity.pop("indexText")
    doc = entity.to_full_dict(matchable=True)
    names: List[str] = doc.get(NAMES_FIELD, [])
    names.extend(entity.get("weakAlias", quiet=True))
    name_parts = index_name_parts(names)
    texts.extend(name_parts)
    doc[NAME_PART_FIELD] = name_parts
    doc[NAME_KEY_FIELD] = index_name_keys(names)
    doc[NAME_PHONETIC_FIELD] = phonetic_names(names)
    doc[DateType.group] = expand_dates(doc.pop(DateType.group, []))
    doc["text"] = texts
    del doc["id"]
    return doc


async def get_current_version(dataset: Dataset, provider: SearchProvider) -> str | None:
    """
    Return the currently indexed version of a given dataset.
    """
    sources = await provider.get_backing_indexes(settings.ENTITY_INDEX)
    if len(sources) < 1:
        raise ValueError(
            f"Expected at least one index for {settings.ENTITY_INDEX}, found 0."
        )
    versions = []
    for k in sources:
        if k.startswith(construct_index_name(dataset.name)):
            _, _, version = parse_index_name(k)
            versions.append(index_to_dataset_version(version))
    return sorted(versions, reverse=True)[0] if len(versions) > 0 else None
