import logging
from typing import Any, Dict, List, Union, Optional
from followthemoney.schema import Schema
from followthemoney.proxy import EntityProxy
from followthemoney.types import registry

from yente.entity import Dataset
from yente.search.mapping import TEXT_TYPES

log = logging.getLogger(__name__)

FilterDict = Dict[str, Union[bool, str, List[str]]]


def filter_query(
    shoulds,
    dataset: Optional[Dataset] = None,
    schema: Optional[Schema] = None,
    filters: FilterDict = {},
):
    filterqs = []
    if dataset is not None:
        filterqs.append({"terms": {"datasets": dataset.source_names}})
    if schema is not None:
        schemata = schema.matchable_schemata
        schemata.add(schema)
        if not schema.matchable:
            schemata.update(schema.descendants)
        names = [s.name for s in schemata]
        filterqs.append({"terms": {"schema": names}})
    for field, values in filters.items():
        if isinstance(values, (bool, str)):
            filterqs.append({"term": {field: {"value": values}}})
            continue
        values = [v for v in values if len(v)]
        if len(values):
            filterqs.append({"terms": {field: values}})
    return {"bool": {"filter": filterqs, "should": shoulds, "minimum_should_match": 1}}


def entity_query(dataset: Dataset, entity: EntityProxy, fuzzy: bool = False):
    terms: Dict[str, List[str]] = {}
    texts: List[str] = []
    shoulds: List[Dict[str, Any]] = []
    for prop, value in entity.itervalues():
        if prop.type == registry.name:
            query = {
                "match": {
                    "names": {
                        "query": value,
                        "lenient": False,
                        # "operator": "AND",
                        "minimum_should_match": "60%",
                        # "slop": 3,
                        "fuzziness": 1 if fuzzy else 0,
                        # "boost": 3.0,
                    }
                }
            }
            shoulds.append(query)
        elif prop.type.group is not None:
            if prop.type not in TEXT_TYPES:
                field = prop.type.group
                if field not in terms:
                    terms[field] = []
                terms[field].append(value)
        if prop.type in (registry.name, registry.string, registry.address):
            if len(value) < 100:
                texts.append(value)

    for field, texts in terms.items():
        shoulds.append({"terms": {field: texts}})
    for text in texts:
        shoulds.append({"match_phrase": {"text": text}})
    return filter_query(shoulds, dataset=dataset, schema=entity.schema)


def text_query(
    dataset: Dataset,
    schema: Schema,
    query: str,
    filters: FilterDict = {},
    fuzzy: bool = False,
):

    if not len(query.strip()):
        should = {"match_all": {}}
    else:
        should = {
            "query_string": {
                "query": query,
                # "default_field": "text",
                "fields": ["names^3", "text"],
                "default_operator": "and",
                "fuzziness": 2 if fuzzy else 0,
                "lenient": fuzzy,
            }
        }
    return filter_query([should], dataset=dataset, schema=schema, filters=filters)


def prefix_query(
    dataset: Dataset,
    prefix: str,
):
    if not len(prefix.strip()):
        should = {"match_none": {}}
    else:
        should = {"match_phrase_prefix": {"names": {"query": prefix, "slop": 2}}}
    return filter_query([should], dataset=dataset)


def facet_aggregations(fields: List[str] = []) -> Dict[str, Any]:
    aggs = {}
    for field in fields:
        aggs[field] = {"terms": {"field": field, "size": 1000}}
    return aggs


def statement_query(
    dataset=Optional[Dataset], **kwargs: Optional[Union[str, bool]]
) -> Dict[str, Any]:
    # dataset: Optional[str] = None,
    # entity_id: Optional[str] = None,
    # canonical_id: Optional[str] = None,
    # prop: Optional[str] = None,
    # value: Optional[str] = None,
    # schema: Optional[str] = None,
    filters = []
    if dataset is not None:
        filters.append({"terms": {"dataset": dataset.source_names}})
    for field, value in kwargs.items():
        if value is not None:
            filters.append({"term": {field: value}})
    if not len(filters):
        return {"match_all": {}}
    return {"bool": {"filter": filters}}


def parse_sorts(sorts: List[str]) -> List[Any]:
    """Accept sorts of the form: <field>:<order>, e.g. first_seen:desc."""
    objs: List[Any] = []
    for sort in sorts:
        order = "asc"
        if ":" in sort:
            sort, order = sort.rsplit(":", 1)
        obj = {sort: {"order": order, "missing": "_last"}}
        objs.append(obj)
    objs.append("_score")
    return objs
