"""Custom recipes: an engineer's choice of the calls a site's own pages make, saved as an `operator` config that
serves every URL its patterns match (`config_ir.url_pattern`), in the selected schema.

`CustomRecipe` is the body of `POST /v1/sites/{host}/configs` and `PUT /v1/configs/{id}/recipe`; `to_ir` makes the
config it is stored as and `from_ir` reads it back. The field map is the main call's `map`, each extra call's `map`,
and the fields read from the page itself (`fields`): each field has one source. Extra calls are the config's
`recipes`, named by the `call` of the fields they fill, as a learned config's companion calls are. Credential values
are refused as in every recipe (`api_recipe.model`).
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from snowcrawl.collect.api import map_paths
from snowcrawl.config_ir.api_recipe.model import ApiRecipe
from snowcrawl.config_ir.model import (
    ExtractionConfigIR,
    FieldSteps,
    JsonPathStep,
    SentBy,
    Step,
    WarmUp,
    XPathStep,
)
from snowcrawl.config_ir.url_pattern import UrlPattern, parse
from snowcrawl.core.validation import field_error
from snowcrawl.db.models import PageKind, SchemaId
from snowcrawl.schemas.types import Name, SchemaDef, document_digest


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_serialization_defaults_required=True)


class PageField(_Model):
    """A field read from the page itself: its steps in order (the first valid value wins), on the HTTP page or, when
    only a render states it, the browser's (`browser`)."""

    steps: list[Step] = Field(min_length=1)
    needs: Literal["http", "browser"] = "http"


class CustomRecipe(_Model):
    """A custom recipe: the schema and page kind it serves, the URL patterns of one host it serves them for, the main
    call and the extra calls by name, the warm-up, the page fields, who sends the calls and the least interval between
    them (`rate_ms`)."""

    schema_id: SchemaId
    page_kind: PageKind
    patterns: list[UrlPattern] = Field(min_length=1)
    main: ApiRecipe
    calls: dict[Name, ApiRecipe] = {}
    warm_up: WarmUp | None = None
    fields: dict[Name, PageField] = {}
    sent_by: SentBy = "http"
    rate_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _one_source_per_field(self) -> Self:
        sources = [list(self.main.map), *(list(call.map) for call in self.calls.values()), list(self.fields)]
        names = [name for source in sources for name in source]
        if twice := sorted({name for name in names if names.count(name) > 1}):
            raise ValueError(f"fields with more than one source: {twice}")
        if len({parse(pattern).host for pattern in self.patterns}) != 1:
            raise ValueError("a recipe's URL patterns are of one host")
        if self.page_kind == "listing" and self.main.pagination is None:
            raise ValueError(
                "a listing recipe's main call has a pagination (`none` when its one response holds every row): its"
                " rows path selects the rows"
            )
        return self

    @property
    def host(self) -> str:
        """The site's host, as a site row writes it: an IP literal without its brackets (RFC 3986 §3.2.2)."""
        return parse(self.patterns[0]).host.removeprefix("[").removesuffix("]")

    def field_names(self) -> set[str]:
        return {*self.main.map, *(name for call in self.calls.values() for name in call.map), *self.fields}

    def to_ir(self, schema: SchemaDef, *, at: AwareDatetime, engine_version: str) -> ExtractionConfigIR:
        """The `operator` config the recipe is stored as, for `schema`. Raises `ValidationError` when the recipe is
        not a valid config (a binder a pattern does not bind, a warm-up binder without a warm-up)."""
        fields = {name: _page_steps(field) for name, field in self.fields.items()}
        for call_name, call in self.calls.items():
            fields |= {name: _call_steps(call_name, call, path) for name, path in call.map.items()}
        document = {
            "ir_version": "1",
            "site": self.host,
            "schema": {"name": schema.name, "digest": document_digest(schema)},
            "page_kind": self.page_kind,
            "fetch": {"method": "http"},
            "fields": {name: steps.model_dump(by_alias=True) for name, steps in fields.items()},
            "api_recipe": self.main.model_dump(),
            "recipes": {name: call.model_dump() for name, call in self.calls.items()},
            "provenance": {
                "origin": "operator",
                "validated_on": [],
                "learned_at": at,
                "engine_version": engine_version,
            },
            "patterns": self.patterns,
            "warm_up": None if self.warm_up is None else self.warm_up.model_dump(),
            "sent_by": self.sent_by,
            "rate_ms": self.rate_ms,
        }
        return ExtractionConfigIR.model_validate(document)

    @classmethod
    def from_ir(cls, ir: ExtractionConfigIR, schema_id: SchemaId) -> "CustomRecipe":
        """The recipe `ir` stores; raises `ValueError` for a config without URL patterns."""
        if ir.api_recipe is None or not ir.patterns:
            raise ValueError("not a custom recipe: a config without URL patterns")
        page = {name: steps for name, steps in ir.fields.items() if steps.call is None}
        return cls(
            schema_id=schema_id,
            page_kind=ir.page_kind,
            patterns=ir.patterns,
            main=ir.api_recipe,
            calls=ir.recipes,
            warm_up=ir.warm_up,
            fields={name: PageField(steps=steps.steps, needs=_page_needs(steps)) for name, steps in page.items()},
            sent_by=ir.sent_by,
            rate_ms=ir.rate_ms,
        )


def _page_steps(field: PageField) -> FieldSteps:
    return FieldSteps(steps=field.steps, status="operator", needs=field.needs)


def _page_needs(steps: FieldSteps) -> Literal["http", "browser"]:
    return "browser" if steps.needs == "browser" else "http"


def _call_steps(call_name: str, call: ApiRecipe, path: str) -> FieldSteps:
    """A field an extra call fills, as a companion call's field is: a step that reads the call's answer at its map
    path (the first JSONPath of a joined one; the call's `map` holds the whole path), or its XPath for an HTML
    answer."""
    step: Step = (
        XPathStep(xpath=path) if call.answer == "html" else JsonPathStep(jsonpath=map_paths(path)[0], source="xhr")
    )
    return FieldSteps(steps=[step], status="operator", needs="call", call=call_name)


class InvalidRecipe(ValueError):
    """The recipe does not fit its site or schema; `errors` are (dotted path in the body, reason) pairs, `""` for
    the whole body."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        super().__init__("the recipe does not fit its site or schema")
        self.errors = errors


def recipe_ir(
    recipe: CustomRecipe, host: str, schema: SchemaDef, *, at: AwareDatetime, engine_version: str
) -> ExtractionConfigIR:
    """The config `recipe` is stored as (`to_ir`) for the site `host`; raises `InvalidRecipe` when the recipe is not
    for that site, schema or page kind, names a field the schema has not, or is not a valid config."""
    errors: list[tuple[str, str]] = []
    if recipe.host != host.lower():
        errors.append(("patterns", f"the URL patterns are of host {recipe.host}, not {host.lower()}"))
    if recipe.page_kind != schema.page_kind:
        errors.append(("page_kind", f"schema {schema.name} is for {schema.page_kind} pages"))
    errors += [
        ("", f"{name}: not a field of schema {schema.name}")
        for name in sorted(recipe.field_names() - set(schema.fields))
    ]
    if errors:
        raise InvalidRecipe(errors)
    try:
        return recipe.to_ir(schema, at=at, engine_version=engine_version)
    except ValidationError as e:
        raise InvalidRecipe([field_error(error) for error in e.errors()]) from None
