from pydantic import BaseModel

from cd.lib.jsonapi import (
    CollectionDocument,
    Document,
    Relationship,
    Resource,
    ResourceIdentifier,
)


class _Attrs(BaseModel):
    name: str
    seats: int


def test_document_wraps_a_single_resource():
    doc = Document[_Attrs](
        data=Resource[_Attrs](
            type="state", id="CA", attributes=_Attrs(name="California", seats=52)
        )
    )

    assert doc.model_dump() == {
        "data": {
            "type": "state",
            "id": "CA",
            "attributes": {"name": "California", "seats": 52},
            "relationships": None,
            "meta": None,
        }
    }
    # A route serving a resource with no relationships/meta uses
    # response_model_exclude_none so `"relationships": null` / `"meta":
    # null` never reach the wire (JSON:API: those members MUST be objects
    # when present).
    assert doc.model_dump(exclude_none=True) == {
        "data": {
            "type": "state",
            "id": "CA",
            "attributes": {"name": "California", "seats": 52},
        }
    }


def test_resource_carries_optional_meta():
    resource = Resource[_Attrs](
        type="bill",
        id="119-hr-2616",
        attributes=_Attrs(name="x", seats=1),
        meta={"matches": [{"via": "policy_area"}]},
    )

    dumped = resource.model_dump(exclude_none=True)
    assert dumped["meta"] == {"matches": [{"via": "policy_area"}]}
    assert "relationships" not in dumped


def test_resource_carries_relationship_linkage():
    resource = Resource[_Attrs](
        type="roll_call_vote",
        id="119-house-1-327:K000401",
        attributes=_Attrs(name="x", seats=1),
        relationships={
            "member": Relationship(
                data=ResourceIdentifier(type="member", id="K000401")
            ),
            "bill": Relationship(
                data=ResourceIdentifier(type="bill", id="119-hr-2616")
            ),
        },
    )

    dumped = resource.model_dump()
    assert dumped["relationships"]["bill"] == {
        "data": {"type": "bill", "id": "119-hr-2616"}
    }


def test_relationship_accepts_a_to_many_data_list():
    rel = Relationship(
        data=[
            ResourceIdentifier(type="roll_call", id="119-house-1-1"),
            ResourceIdentifier(type="roll_call", id="119-house-1-2"),
        ]
    )

    assert [i.id for i in rel.data] == ["119-house-1-1", "119-house-1-2"]


def test_resource_validates_relationships_from_a_raw_dict():
    resource = Resource[_Attrs].model_validate(
        {
            "type": "roll_call_vote",
            "id": "v1",
            "attributes": {"name": "x", "seats": 1},
            "relationships": {
                "bill": {"data": {"type": "bill", "id": "119-hr-2616"}}
            },
        }
    )

    assert resource.relationships["bill"].data.id == "119-hr-2616"


def test_collection_document_wraps_a_list_and_omits_meta_by_default():
    doc = CollectionDocument[_Attrs](
        data=[
            Resource[_Attrs](type="state", id="CA", attributes=_Attrs(name="California", seats=52)),
            Resource[_Attrs](type="state", id="WY", attributes=_Attrs(name="Wyoming", seats=1)),
        ]
    )

    dumped = doc.model_dump()
    assert [r["id"] for r in dumped["data"]] == ["CA", "WY"]
    assert dumped["meta"] is None


def test_collection_document_carries_an_optional_meta_object():
    doc = CollectionDocument[_Attrs](data=[], meta={"query": "voting rights"})

    assert doc.model_dump() == {"data": [], "meta": {"query": "voting rights"}}


def test_document_validates_from_a_raw_dict():
    doc = Document[_Attrs].model_validate(
        {"data": {"type": "state", "id": "TX", "attributes": {"name": "Texas", "seats": 38}}}
    )

    assert doc.data.attributes.seats == 38


def test_resource_attributes_are_type_checked():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Resource[_Attrs](type="state", id="CA", attributes={"name": "California"})


def test_generic_json_schema_names_the_concrete_attributes_model():
    schema = Document[_Attrs].model_json_schema()

    # The concrete attributes model is registered under $defs and the
    # resource's `attributes` points at it -- this is what lets FastAPI
    # render Document[MemberDetail] as a real shape in the OpenAPI doc.
    assert "_Attrs" in schema["$defs"]
    assert set(schema["$defs"]["_Attrs"]["properties"]) == {"name", "seats"}
