from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

# JSON:API document/resource models for cd-api's resource endpoints
# (GET /members/{bioguide_id}, GET /members/{bioguide_id}/votes, and
# eventually /bills/search): the document envelope (`data` holding a
# resource or list of them), typed `attributes`, and `relationships`
# carrying resource *linkage* -- `{"type", "id"}` pointers, nothing more.
#
# The HTTP layer -- the `application/vnd.api+json` media type, JSON:API
# error documents, and the spec's request-side strictness -- lives in
# cd-api's own `cd.api.jsonapi`, not here. What's deliberately NOT built
# anywhere is the optional machinery: no `included` (and so no
# `?include=`), no sparse fieldsets (`?fields[type]=`), no relationship
# `links` (`self`/`related`) or their endpoints, no pagination/`sort`, no
# top-level `jsonapi` object. The one caller (cd-server) runs a fixed
# two-call merge and needs none of it. Non-resource endpoints
# (GET /version) aren't JSON:API at all.
#
# `A` is the attributes model -- each endpoint parameterises these
# generics with its own (`Document[MemberDetail]`,
# `CollectionDocument[RollCallVote]`), and FastAPI renders the concrete
# shape into the OpenAPI schema per route. `relationships` is a loose
# string-keyed bag rather than a second generic parameter -- typing it
# per resource would mean `Resource[A, R]` everywhere for little gain.

A = TypeVar("A", bound=BaseModel)


class ResourceIdentifier(BaseModel):
    """A JSON:API resource linkage object -- ``{"type": ..., "id": ...}``,
    a pointer to a resource by identity (not the resource itself)."""

    type: str
    id: str


class Relationship(BaseModel):
    """A JSON:API relationship object, linkage only.

    ``{"data": {"type", "id"}}`` for a to-one relationship,
    ``{"data": [{"type", "id"}, ...]}`` for to-many. No `links` or `meta`
    -- see the module docstring for what's left out and why.
    """

    data: ResourceIdentifier | list[ResourceIdentifier]


class Resource(BaseModel, Generic[A]):
    """A JSON:API resource object: a `type`, a string `id`, typed
    `attributes`, and optional `relationships` (linkage to other
    resources by `{type, id}`).

    Identity lives here, not in `attributes` -- an attributes model never
    repeats its own id field (a `member` resource's `attributes` has no
    `bioguide_id`). Foreign keys live in `relationships`, not
    `attributes` -- a `roll_call_vote`'s bill is
    `relationships.bill.data.id`, not an attribute.
    """

    type: str
    id: str
    attributes: A
    # Optional: a resource with no relationships omits the member
    # entirely (JSON:API: `relationships`, when present, MUST be an
    # object -- never `null`). Routes whose resources never carry
    # relationships (e.g. GET /members/{bioguide_id}) set
    # `response_model_exclude_none=True` so FastAPI drops the default
    # `None` rather than serialising `"relationships": null`.
    relationships: dict[str, Relationship] | None = None


class Document(BaseModel, Generic[A]):
    """A single-resource JSON:API document: ``{"data": <resource>}``."""

    data: Resource[A]


class CollectionDocument(BaseModel, Generic[A]):
    """A resource-collection JSON:API document:
    ``{"data": [<resource>, ...], "meta"?: {...}}``.

    `meta` is an optional free-form object for information that isn't a
    resource -- e.g. /bills/search echoing its query, or
    /members/{id}/votes listing requested bills the member never voted
    on. Kept untyped here since what belongs in it is per-endpoint.
    """

    data: list[Resource[A]]
    meta: dict[str, Any] | None = None
