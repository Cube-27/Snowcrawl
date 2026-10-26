"""Binding: sample pages and the endpoint requests made for them bind to a request spec whose templates
reproduce every request, or to `Ambiguous` with the reason. Binder round trips: a value placed in a page URL is read
back by its binder, endpoint requests generated from a template bind back to binders that reproduce every
request, and a value written into a form body is read back by the form's decoding."""

from dataclasses import dataclass
from urllib.parse import parse_qsl, quote

from hypothesis import given
from hypothesis import strategies as st

from snowcrawl.config_ir.api_recipe.binding import (
    Ambiguous,
    CapturedRequest,
    PageData,
    Proposed,
    Sample,
    bind,
    propose,
    render_body,
    render_url,
    resolve,
    resolve_params,
    withhold_tokens,
)
from snowcrawl.config_ir.api_recipe.model import Binder, RequestSpec

_PAGE = "https://shop.example.test"
_API = "https://api.example.test"


def _sample(
    page_url: str, url: str, body: str | None = None, method: str = "GET", page_data: PageData | None = None
) -> Sample:
    return Sample(page_url, CapturedRequest(method, url, body), page_data or {})


def _spec(result: RequestSpec | Ambiguous) -> RequestSpec:
    assert isinstance(result, RequestSpec), result
    return result


# Values hold characters that need percent-encoding (space, `%`, `&`, `/`, `+`, non-ASCII) beside plain ones. Literal
# path segments are upper case, so no value meets a literal.
_value = st.text(alphabet="abcxyz0189-._~ %&/+=é", min_size=1, max_size=8)


@given(_value, st.integers(min_value=0, max_value=3))
def test_a_value_in_the_page_url_is_read_back_by_its_binder(value: str, index: int) -> None:
    segments = ["S"] * index + [quote(value, safe="")]
    page_url = "https://www.example.test/" + "/".join(segments) + "?k=" + quote(value, safe="")

    assert resolve(Binder("path", str(index)), page_url, {}) == value
    assert resolve(Binder("query", "k"), page_url, {}) == value


@dataclass(frozen=True)
class _Layout:
    """Where each parameter sits on the page (`path:<i>` or `query:<name>`) and in the endpoint URL."""

    page_binders: list[Binder]
    endpoint_template: str

    def page_url(self, values: list[str]) -> str:
        path = ["P"] * (1 + sum(binder.kind == "path" for binder in self.page_binders))
        query = []
        for binder, value in zip(self.page_binders, values, strict=True):
            if binder.kind == "path":
                path[int(binder.arg)] = quote(value, safe="")
            else:
                query.append(f"{binder.arg}={quote(value, safe='')}")
        return "https://www.example.test/" + "/".join(path) + ("?" + "&".join(query) if query else "")

    def endpoint_url(self, values: list[str]) -> str:
        return render_url(self.endpoint_template, {f"v{i}": value for i, value in enumerate(values)})


@st.composite
def _layouts(draw: st.DrawFn) -> _Layout:
    count = draw(st.integers(min_value=1, max_value=3))
    in_path = draw(st.lists(st.booleans(), min_size=count, max_size=count))
    path_indexes = iter(range(1, count + 1))
    binders = [
        Binder("path", str(next(path_indexes))) if p else Binder("query", f"q{i}") for i, p in enumerate(in_path)
    ]
    order = draw(st.permutations(range(count)))
    in_endpoint_query = draw(st.lists(st.booleans(), min_size=count, max_size=count))
    segments = ["API", "V1"] + [f"{{v{i}}}" for i in order if not in_endpoint_query[i]]
    query = [f"k{i}={{v{i}}}" for i in order if in_endpoint_query[i]] + ["fmt=JSON"]
    return _Layout(binders, "https://api.example.test/" + "/".join(segments) + "?" + "&".join(query))


@given(st.text(min_size=1, max_size=12), st.text(min_size=1, max_size=12))
def test_a_value_in_a_form_body_is_read_back_by_the_forms_decoding(first: str, second: str) -> None:
    # WHATWG URL §5.1: application/x-www-form-urlencoded parsing gives back what §5.2 serializing wrote.
    body = render_body("a={a}&b={b}&c=1", {"a": first, "b": second}, "form")

    assert parse_qsl(body, keep_blank_values=True, errors="strict") == [("a", first), ("b", second), ("c", "1")]


@given(_layouts(), st.data())
def test_generated_requests_bind_back_to_their_binders(layout: _Layout, data: st.DataObject) -> None:
    count, pages = len(layout.page_binders), data.draw(st.integers(min_value=2, max_value=4))
    values = data.draw(st.lists(_value, min_size=count * pages, max_size=count * pages, unique=True))
    rows = [values[page * count : (page + 1) * count] for page in range(pages)]
    samples = [Sample(layout.page_url(row), CapturedRequest("GET", layout.endpoint_url(row))) for row in rows]

    spec = bind(samples)

    assert isinstance(spec, RequestSpec), spec
    assert propose(samples) == Proposed(spec, ())
    assert set(spec.binders.values()) == set(layout.page_binders)
    for sample, row in zip(samples, rows, strict=True):
        params = resolve_params(spec, sample.page_url, {})
        assert params is not None
        assert sorted(params.values()) == sorted(row)
        assert render_url(spec.url, params) == sample.request.url


def test_a_page_path_segment_binds_an_endpoint_path_segment_and_constants_stay_literal() -> None:
    samples = [
        _sample(f"{_PAGE}/p/blue-mug", f"{_API}/v1/products/blue-mug?lang=en"),
        _sample(f"{_PAGE}/p/red-cup", f"{_API}/v1/products/red-cup?lang=en"),
    ]

    spec = _spec(bind(samples))

    assert spec.url == f"{_API}/v1/products/{{p1}}?lang=en"
    assert spec.params_from_url == {"p1": "path:1"}
    assert (spec.method, spec.headers, spec.body, spec.requires_page_fetch) == ("GET", [], None, False)


def test_a_proposal_leaves_a_varying_value_nothing_binds_as_the_first_sample_s_literal() -> None:
    samples = [
        _sample(f"{_PAGE}/p/blue-mug", f"{_API}/v1/products/blue-mug?at=1"),
        _sample(f"{_PAGE}/p/red-cup", f"{_API}/v1/products/red-cup?at=2"),
    ]

    proposed = propose(samples)

    assert isinstance(bind(samples), Ambiguous)
    assert isinstance(proposed, Proposed), proposed
    assert (proposed.spec.url, proposed.literal) == (f"{_API}/v1/products/{{p1}}?at=1", ("query value 'at'",))


def test_a_post_body_binds_string_and_number_values_in_place() -> None:
    body = '{{"query":"q1","variables":{{"slug":"{slug}","n":{n},"draft":false}}}}'
    samples = [
        _sample(f"{_PAGE}/p/a-b?n=3", f"{_API}/graphql", body.format(slug="a-b", n=3), "POST"),
        _sample(f"{_PAGE}/p/c-d?n=5", f"{_API}/graphql", body.format(slug="c-d", n=5), "post"),
    ]

    spec = _spec(bind(samples))

    assert spec.method == "POST"
    assert spec.body is not None
    assert spec.body.template == '{"query":"q1","variables":{"slug":"{p1}","n":{v1},"draft":false}}'
    assert spec.params_from_url == {"p1": "path:1", "v1": "query:n"}


# --- token-like query values (owner delegation 2026-10-17) ---

_KEY = "Zk3Q9xLm2Vb7Rt4Ws8Np1Hc6"  # 24 characters of the base64url alphabet: token-like


def test_a_constant_token_like_query_value_no_page_value_holds_is_left_out() -> None:
    samples = [
        _sample(f"{_PAGE}/c/mugs", f"{_API}/search?q=mugs&key={_KEY}&lang=en"),
        _sample(f"{_PAGE}/c/cups", f"{_API}/search?q=cups&key={_KEY}&lang=en"),
    ]

    stripped, withheld = withhold_tokens(samples)

    assert withheld == ("key",)
    assert [s.request.url for s in stripped] == [f"{_API}/search?q=mugs&lang=en", f"{_API}/search?q=cups&lang=en"]
    spec = _spec(bind(stripped))
    assert spec.url == f"{_API}/search?q={{p1}}&lang=en"
    assert _KEY not in spec.model_dump_json()
