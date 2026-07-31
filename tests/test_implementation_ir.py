from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from convert2aidoku.generation_context import build_generation_context
from convert2aidoku.implementation_ir import (
    ApiBaseIR,
    ImplementationIR,
    ListingEndpointIR,
    ListingImplementationIR,
    ListingRole,
    ListingSelectionIR,
    project_implementation_ir,
)
from convert2aidoku.listing_renderer import (
    render_search_listing,
    with_deterministic_search_listing,
)
from convert2aidoku.models import (
    Capability,
    GeneratedFile,
    GenerationManifest,
    RequestHeaderProfile,
    SourceFile,
    SourceFilterOption,
    SourceFilterSpec,
)
from tests.scenarios import minimal_source_ir


def _file(path: str, content: str) -> SourceFile:
    return SourceFile(
        path=path,
        content=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def _copymanga_listing_files() -> list[SourceFile]:
    return [
        _file(
            "sources/example/ApiDomainOption.java",
            """
            public enum ApiDomainOption {
                COPY7("api.copy3000.com", "api.copy3000.com"),
                HOT2("api.manga2025.com", "api.manga2025.com");
                public static final String KEY = "v2.pref.api_domain";
                private static final String DEFAULT;
                static { DEFAULT = COPY7.entryKey; }
            }
            """,
        ),
        _file(
            "sources/example/api/ApiRepo.java",
            """
            public final class ApiRepo {
                private static final int pageSize = 21;
                private final String getApiUrl() {
                    return PreferencesKt.getHttpApiDomain(preferences) + "/api/v3";
                }
                public final String comicListUrl(int page) {
                    return getApiUrl() + "/comics?limit=21&offset=" +
                        ((page - 1) * pageSize);
                }
                public final String comicRankUrl(int page) {
                    return getApiUrl() + "/ranks?type=1&limit=21&offset=" +
                        ((page - 1) * pageSize);
                }
                public final String newestPageUrl_update(int page) {
                    return getApiUrl() + "/comics?limit=21&offset=" +
                        ((page - 1) * pageSize) + "&ordering=-datetime_updated";
                }
                public final String newestPageUrl(int page) {
                    return getApiUrl() + "/update/newest?limit=21&offset=" +
                        ((page - 1) * pageSize);
                }
                public final String recommendPageUrl(int page) {
                    return getApiUrl() + "/recs?pos=3200102&limit=21&offset=" +
                        ((page - 1) * pageSize);
                }
                public final String searchUrl(int page) {
                    return getApiUrl() + "/search/comic?limit=21&offset=" +
                        ((page - 1) * pageSize);
                }
                public final String tagList() {
                    return getApiUrl() + "/theme/comic/count?limit=100";
                }
            }
            """,
        ),
        _file(
            "sources/example/CopyManga.java",
            """
            public final class CopyManga extends HttpSource {
                static {
                    int[] mapping = new int[LatestUpdateOption.values().length];
                    mapping[LatestUpdateOption.NEW_BOOKS.ordinal()] = 1;
                    mapping[LatestUpdateOption.LATEST_UPDATE.ordinal()] = 2;
                }
                protected Request popularMangaRequest(int page) {
                    return GET(ApiRepo.INSTANCE.recommendPageUrl(page));
                }
                protected Request latestUpdatesRequest(int page) {
                    int mode = latestMode();
                    if (mode == 1) {
                        return GET(ApiRepo.INSTANCE.newestPageUrl(page));
                    }
                    if (mode == 2) {
                        return GET(ApiRepo.INSTANCE.newestPageUrl_update(page));
                    }
                    throw new NoWhenBranchMatchedException();
                }
                protected MangasPage searchMangaParse(Response response) {
                    if (contains(response.url(), "/api/v3/search/comic")) {
                        Reflection.typeOf(ApiResponse.class, Reflection.typeOf(SearchResult.class));
                    }
                    if (contains(response.url(), "/api/v3/ranks")) {
                        Reflection.typeOf(ApiResponse.class, Reflection.typeOf(RankResult.class));
                    }
                    Reflection.typeOf(
                        ApiResponse.class,
                        Reflection.typeOf(ComicsListResult.class)
                    );
                    return page;
                }
                protected Request searchMangaRequest(int page, String query, FilterList filters) {
                    if (!query.isBlank()) {
                        builder = get(ApiRepo.INSTANCE.searchUrl(page)).newBuilder();
                        builder.addQueryParameter("q", query);
                        builder.addQueryParameter(
                            "q_type",
                            FilterKt.getTypeFilter()[typeIndex].getPathWord()
                        );
                    } else if (rank > 0) {
                        builder = get(ApiRepo.INSTANCE.comicRankUrl(page)).newBuilder();
                        builder.addQueryParameter(
                            "date_type",
                            FilterKt.getRankFilter()[rankIndex].getPathWord()
                        );
                        builder.addQueryParameter(
                            "audience_type",
                            FilterKt.getAudienceFilter()[audienceIndex].getPathWord()
                        );
                    } else {
                        builder = get(ApiRepo.INSTANCE.comicListUrl(page)).newBuilder();
                        if (regionIndex > 0) {
                            builder.addQueryParameter(
                                "top",
                                FilterKt.getRegionFilter()[regionIndex].getPathWord()
                            );
                        }
                        if (themeIndex > 0) {
                            builder.addQueryParameter(
                                "theme",
                                FilterKt.getThemeFilter()[themeIndex].getPathWord()
                            );
                        }
                        builder.addQueryParameter(
                            "ordering",
                            FilterKt.getSortFilter()[sortIndex].getPathWord()
                        );
                    }
                    builder.addQueryParameter("_update", "true");
                    return GET(builder.build());
                }
            }
            """,
        ),
        _file(
            "sources/example/LatestUpdateOption.java",
            """
            public final class LatestUpdateOption {
                LATEST_UPDATE("Recent", "latest_update.latest_update"),
                NEW_BOOKS("New", "latest_update.new_books");
                public static final String KEY = "v2.pref.latest_update";
                private static final String DEFAULT =
                    new LatestUpdateOption("Recent", "latest_update.latest_update").entryKey;
            }
            """,
        ),
        _file(
            "sources/example/api/ApiResponse.java",
            """
            import kotlinx.serialization.Serializable;
            @Serializable
            public final class ApiResponse<T> { private final T results; }
            """,
        ),
        _file(
            "sources/example/api/dto/ComicSummary.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class ComicSummary {
                // Serialized field names:
                // pathWord -> "path_word"
                // Fields:
                private final List<AuthorInfo> author;
                private final String cover;
                private final String name;
                private final String pathWord;
                // Source-specific behavior:
                public final SManga toSManga() {
                    SManga manga = SManga.create();
                    manga.setUrl("/comic/" + this.pathWord);
                    manga.setTitle(translate(this.name));
                    manga.setAuthor(join(this.author));
                    manga.setDescription("");
                    manga.setGenre("");
                    manga.setThumbnail_url(this.cover);
                    return manga;
                }
            }
            """,
        ),
        _file(
            "sources/example/api/dto/AuthorInfo.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class AuthorInfo { private final String name; }
            """,
        ),
        _file(
            "sources/example/api/dto/ComicsListResult.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class ComicsListResult {
                private final int limit;
                private final List<ComicSummary> list;
                private final int offset;
                private final int total;
            }
            """,
        ),
        _file(
            "sources/example/api/dto/SearchResult.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class SearchResult {
                private final int limit;
                private final List<ComicSummary> list;
                private final int offset;
                private final int total;
            }
            """,
        ),
        _file(
            "sources/example/api/dto/ListItem.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class ListItem { private final ComicSummary comic; }
            """,
        ),
        _file(
            "sources/example/api/dto/RankResult.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class RankResult {
                private final int limit;
                private final List<ListItem> list;
                private final int offset;
                private final int total;
            }
            """,
        ),
        _file(
            "sources/example/api/dto/RecommendResult.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class RecommendResult {
                private final int limit;
                private final List<ListItem> list;
                private final int offset;
                private final int total;
            }
            """,
        ),
        _file(
            "sources/example/api/dto/NewestItem.java",
            """
            public final class NewestItem { private final ComicSummary comic; }
            """,
        ),
        _file(
            "sources/example/api/dto/NewestResult.java",
            """
            public final class NewestResult {
                private final int limit;
                private final List<NewestItem> list;
                private final int offset;
                private final int total;
            }
            """,
        ),
        _file(
            "sources/example/api/dto/ChapterListResult.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class ChapterListResult {
                private final int limit;
                private final List<ChapterInfo> list;
                private final int offset;
                private final int total;
            }
            """,
        ),
    ]


def _serializable_listing_files() -> list[SourceFile]:
    return [
        _file(
            "sources/example/ApiRepo.java",
            """
            public final class ApiRepo {
                private static final String DEFAULT = "api.example.com";
                private static final int pageSize = 20;
                private final String getApiUrl() { return "/api"; }
                public final String searchUrl(int page, String query) {
                    return getApiUrl() + "/search?page=" + page + "&q=" + query;
                }
                public final String comicListUrl(int page) {
                    return getApiUrl() + "/comics?page=" + page;
                }
            }
            """,
        ),
        _file(
            "sources/example/Example.java",
            """
            public final class Example extends HttpSource {
                protected MangasPage searchMangaParse(Response response) {
                    Reflection.typeOf(ComicList.class);
                    return page;
                }
            }
            """,
        ),
        _file(
            "sources/example/ComicList.java",
            """
            import kotlinx.serialization.Serializable;
            @Serializable
            public final class ComicList {
                private final List<ComicItem> items;
                private final String next;
            }
            """,
        ),
        _file(
            "sources/example/ComicItem.java",
            """
            import kotlinx.serialization.Serializable;
            @Serializable
            public final class ComicItem {
                private final String comicId;
                private final String name;
                private final String cover;
                private final String author;
                private final List<String> typeNames;
                public final SManga toSManga() {
                    SManga manga = SManga.create();
                    manga.setUrl("/comic/" + this.comicId);
                    manga.setTitle(this.name);
                    manga.setThumbnail_url(this.cover);
                    manga.setAuthor(translate(this.author));
                    manga.setGenre(join(this.typeNames));
                    return manga;
                }
            }
            """,
        ),
    ]


def _copymanga_filter_specs() -> list[SourceFilterSpec]:
    return [
        SourceFilterSpec(
            source_class=f"{source_name}Filter",
            id=filter_id,
            title=filter_id,
            kind="sort" if filter_id == "sort" else "select",
            options=[SourceFilterOption(title="Default", value=value)],
            default_ascending=True if filter_id == "sort" else None,
        )
        for source_name, filter_id, value in (
            ("Type", "type", "name"),
            ("Rank", "rank", "day"),
            ("Audience", "audience", "male"),
            ("Region", "region", "japan"),
            ("FreeType", "free_type", "1"),
            ("Sort", "sort", "datetime_updated"),
        )
    ]


def test_projects_copymanga_listing_contract_without_provider() -> None:
    ir = minimal_source_ir(
        source_id="zh.copymanga",
        language="zh",
        source_format="decompiled_apk",
        main_class="CopyManga",
        capabilities=[Capability.POPULAR, Capability.LATEST],
        filter_specs=_copymanga_filter_specs(),
        files=_copymanga_listing_files(),
        request_header_profiles=[
            RequestHeaderProfile(
                name="API",
                domains=["api.copy3000.com", "api.manga2025.com", "custom"],
            )
        ],
    )

    implementation = project_implementation_ir(ir)

    assert implementation.unresolved_facts == []
    assert implementation.listing is not None
    listing = implementation.listing
    assert listing.api_base == ApiBaseIR(
        scheme="https",
        path_prefix="/api/v3",
        dynamic=True,
        setting_key="v2.pref.api_domain",
        default_host="api.copy3000.com",
        candidate_hosts=["api.copy3000.com", "api.manga2025.com"],
    )
    endpoints = {endpoint.source_method: endpoint for endpoint in listing.endpoints}
    assert set(endpoints) == {
        "comicListUrl",
        "comicRankUrl",
        "newestPageUrl",
        "newestPageUrl_update",
        "recommendPageUrl",
        "searchUrl",
    }
    assert endpoints["comicListUrl"].path == "/api/v3/comics"
    assert endpoints["comicListUrl"].role == ListingRole.BROWSE
    assert endpoints["comicListUrl"].pagination is not None
    assert endpoints["comicListUrl"].pagination.page_size == 21
    browse_parameters = {
        parameter.name: parameter for parameter in endpoints["comicListUrl"].query_parameters
    }
    assert browse_parameters["offset"].value_template == "{offset}"
    assert browse_parameters["top"].value_template == "{filter:region}"
    assert not browse_parameters["top"].required
    assert browse_parameters["theme"].value_template == "{filter:theme}"
    assert not browse_parameters["theme"].required
    assert browse_parameters["ordering"].value_template == "{filter:sort}"
    assert browse_parameters["ordering"].required
    assert browse_parameters["_update"].value_template == "true"
    search_parameters = {
        parameter.name: parameter for parameter in endpoints["searchUrl"].query_parameters
    }
    assert search_parameters["q"].value_template == "{query}"
    assert search_parameters["q_type"].value_template == "{filter:type}"
    rank_parameters = {
        parameter.name: parameter for parameter in endpoints["comicRankUrl"].query_parameters
    }
    assert rank_parameters["date_type"].value_template == "{filter:rank}"
    assert rank_parameters["audience_type"].value_template == "{filter:audience}"
    assert endpoints["searchUrl"].response_type == "SearchResult"
    assert endpoints["searchUrl"].response_evidence == "parser_path"
    assert endpoints["comicRankUrl"].response_type == "RankResult"
    assert endpoints["comicListUrl"].response_type == "ComicsListResult"
    assert listing.provider is not None
    assert listing.provider.popular_endpoint_id == "recommend_page"
    assert listing.provider.latest == ListingSelectionIR(
        default_endpoint_id="newest_page_url_update",
        setting_key="v2.pref.latest_update",
        setting_default="latest_update.latest_update",
        endpoint_ids_by_setting_value={
            "latest_update.new_books": "newest_page",
            "latest_update.latest_update": "newest_page_url_update",
        },
    )

    containers = {container.type_name: container for container in listing.containers}
    assert "ChapterListResult" not in containers
    assert containers["ComicsListResult"].envelope_path == "results"
    assert containers["ComicsListResult"].item_type == "ComicSummary"
    assert containers["RankResult"].item_wrapper_path == "comic"
    assert containers["RankResult"].manga_item_type == "ComicSummary"

    mapping = next(item for item in listing.manga_mappings if item.item_type == "ComicSummary")
    assert mapping.key_template == "/comic/{path_word}"
    assert mapping.title_path == "name"
    assert mapping.cover_path == "cover"
    assert mapping.authors_path == "author[].name"
    assert mapping.tags_path is None
    assert mapping.description_path is None
    assert {shape.name for shape in listing.data_shapes} == {
        "AuthorInfo",
        "ComicSummary",
        "ComicsListResult",
        "ListItem",
        "NewestItem",
        "NewestResult",
        "RankResult",
        "RecommendResult",
        "SearchResult",
    }


def test_unknown_query_binding_is_not_guessed_from_parameter_name() -> None:
    files = _copymanga_listing_files()
    source = next(file for file in files if file.path.endswith("CopyManga.java"))
    source.content = source.content.replace(
        "FilterKt.getTypeFilter()[typeIndex].getPathWord()", "unrelatedValue"
    )
    source.sha256 = hashlib.sha256(source.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.copymanga",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
    )

    implementation = project_implementation_ir(ir)

    assert implementation.listing is not None
    search = next(
        endpoint
        for endpoint in implementation.listing.endpoints
        if endpoint.source_method == "searchUrl"
    )
    q_type = next(parameter for parameter in search.query_parameters if parameter.name == "q_type")
    assert q_type.source == "unknown"
    assert q_type.value_template == "{unrelated_value}"
    assert "listing query binding is unresolved for search.q_type" in (
        implementation.unresolved_facts
    )


def test_query_bindings_follow_filter_and_control_flow_evidence() -> None:
    files = _copymanga_listing_files()
    source = next(file for file in files if file.path.endswith("CopyManga.java"))
    source.content = source.content.replace(
        '"q_type",\n                            FilterKt.getTypeFilter()[typeIndex].getPathWord()',
        '"mode_code",\n                            categoryValue',
    ).replace('"top",', '"area_code",')
    source.sha256 = hashlib.sha256(source.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
        filter_specs=[
            SourceFilterSpec(
                source_class="CategoryFilter",
                id="category",
                title="Category",
                kind="select",
                options=[SourceFilterOption(title="All", value="")],
            )
        ],
    )

    implementation = project_implementation_ir(ir)

    assert implementation.listing is not None
    endpoints = {endpoint.source_method: endpoint for endpoint in implementation.listing.endpoints}
    search = {parameter.name: parameter for parameter in endpoints["searchUrl"].query_parameters}
    browse = {parameter.name: parameter for parameter in endpoints["comicListUrl"].query_parameters}
    assert search["mode_code"].value_template == "{filter:category}"
    assert search["mode_code"].required
    assert browse["area_code"].value_template == "{filter:region}"
    assert not browse["area_code"].required


def test_required_filter_without_contract_prevents_deterministic_ownership() -> None:
    ir = minimal_source_ir(
        source_id="zh.copymanga",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=_copymanga_listing_files(),
        filter_specs=[spec for spec in _copymanga_filter_specs() if spec.id != "sort"],
    )

    implementation = project_implementation_ir(ir)

    assert "required listing filter contract is unresolved for comic_list.ordering" in (
        implementation.unresolved_facts
    )
    with pytest.raises(ValueError, match="requires unresolved filter sort"):
        render_search_listing(ir, implementation)


def test_projects_serializable_dtos_and_string_mappings_without_directory_assumptions() -> None:
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=_serializable_listing_files(),
    )

    implementation = project_implementation_ir(ir)

    assert implementation.unresolved_facts == []
    assert implementation.listing is not None
    listing = implementation.listing
    containers = {container.type_name: container for container in listing.containers}
    assert containers["ComicList"].items_path == "items"
    assert containers["ComicList"].next_path == "next"
    mapping = next(item for item in listing.manga_mappings if item.item_type == "ComicItem")
    assert mapping.authors_path == "author"
    assert mapping.tags_path == "typeNames[]"

    rendered = render_search_listing(ir, implementation)

    assert "let authors: Vec<String> = if item.author.is_empty()" in rendered.content
    assert "Vec::from([item.author])" in rendered.content
    assert "let tags: Vec<String> = item.type_names;" in rendered.content
    assert "let has_next_page = !result.next.is_empty();" in rendered.content
    assert "let result: ComicList = get_json(url)?;" in rendered.content
    assert "ApiResponse" not in rendered.content

    without_cursor = implementation.model_copy(
        update={
            "listing": listing.model_copy(
                update={
                    "containers": [
                        container.model_copy(update={"next_path": None})
                        for container in listing.containers
                    ]
                }
            )
        }
    )
    page_sized = render_search_listing(ir, without_cursor)

    assert "let has_next_page = result.items.len() >= 20;" in page_sized.content


def test_non_string_next_field_does_not_emit_string_cursor_logic() -> None:
    files = _serializable_listing_files()
    container = next(file for file in files if file.path.endswith("ComicList.java"))
    container.content = container.content.replace("String next", "int next")
    container.sha256 = hashlib.sha256(container.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=files,
    )

    implementation = project_implementation_ir(ir)
    rendered = render_search_listing(ir, implementation)

    assert implementation.listing is not None
    comic_list = next(
        item for item in implementation.listing.containers if item.type_name == "ComicList"
    )
    assert comic_list.next_path is None
    assert "result.next.is_empty()" not in rendered.content
    assert "let has_next_page = result.items.len() >= 20;" in rendered.content


def test_response_envelope_is_projected_from_generic_decode_evidence() -> None:
    files = _serializable_listing_files()
    source = next(file for file in files if file.path.endswith("Example.java"))
    source.content = source.content.replace(
        "Reflection.typeOf(ComicList.class)",
        "Reflection.typeOf(Envelope.class, Reflection.typeOf(ComicList.class))",
    )
    source.sha256 = hashlib.sha256(source.content.encode()).hexdigest()
    files.append(
        _file(
            "sources/example/Envelope.java",
            """
            import kotlinx.serialization.Serializable;
            @Serializable
            public final class Envelope<T> { private final T payload; }
            """,
        )
    )
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=files,
    )

    implementation = project_implementation_ir(ir)
    rendered = render_search_listing(ir, implementation)

    assert implementation.listing is not None
    comic_list = next(
        item for item in implementation.listing.containers if item.type_name == "ComicList"
    )
    assert comic_list.envelope_path == "payload"
    assert '#[serde(rename = "payload")]' in rendered.content
    assert "let response: SearchEnvelope = get_json(url)?;" in rendered.content


def test_kotlin_projection_keeps_unresolved_slot_explicit() -> None:
    implementation = project_implementation_ir(minimal_source_ir())

    assert implementation.listing is None
    assert implementation.unresolved_facts == [
        "deterministic listing projection is not implemented for Kotlin modules"
    ]


def test_implementation_ir_rejects_duplicate_endpoint_ids() -> None:
    endpoint = ListingEndpointIR(
        id="browse",
        role=ListingRole.BROWSE,
        source_method="listUrl",
        path="/api/comics",
    )

    with pytest.raises(ValidationError, match="endpoint ids must be unique"):
        ImplementationIR(
            source_id="en.example",
            listing=ListingImplementationIR(
                api_base=ApiBaseIR(),
                endpoints=[endpoint, endpoint.model_copy()],
            ),
        )


def test_deterministic_search_listing_renderer_uses_only_projected_contract() -> None:
    ir = minimal_source_ir(
        source_id="zh.copymanga",
        language="zh",
        source_format="decompiled_apk",
        main_class="CopyManga",
        capabilities=[Capability.POPULAR, Capability.LATEST],
        filter_specs=_copymanga_filter_specs(),
        files=_copymanga_listing_files(),
        request_header_profiles=[
            RequestHeaderProfile(
                name="API",
                domains=["api.copy3000.com", "api.manga2025.com", "custom"],
                headers={
                    "Accept": "application/json",
                    "Version": "1",
                    "X-Test": "<tag>\b",
                },
            )
        ],
    )

    rendered = render_search_listing(ir)

    assert rendered.path == "src/c2a_listing.rs"
    assert "fn search_url(" in rendered.content
    assert 'api_url("/api/v3/search/comic")' in rendered.content
    assert 'push_query(&mut url, "q", query);' in rendered.content
    assert "fn comic_rank_url(" in rendered.content
    assert 'selected_value(&filters, "rank")' in rendered.content
    assert "fn comic_list_url(" in rendered.content
    assert "struct SearchResult" in rendered.content
    assert "struct RankResult" in rendered.content
    assert "struct SearchEnvelope" in rendered.content
    assert '#[serde(rename = "results")]' in rendered.content
    assert "let response: SearchEnvelope = get_json(url)?;" in rendered.content
    assert "let result = response.value;" in rendered.content
    assert "fn manga_from_comic_summary" in rendered.content
    assert "let has_next_page = result.offset + result.limit < result.total;" in rendered.content
    assert 'request = request.header("Version", "1");' in rendered.content
    assert 'request = request.header("X-Test", "<tag>\\u{8}");' in rendered.content
    assert "pub(crate) fn get_search_manga_list(" in rendered.content
    assert "pub(crate) fn get_manga_list(" in rendered.content
    assert '"popular" => fetch_recommend_page' in rendered.content
    assert 'defaults_get::<String>("v2.pref.latest_update")' in rendered.content
    assert '"latest_update.new_books" => fetch_newest_page' in rendered.content
    assert "_ => fetch_newest_page_url_update" in rendered.content


def test_effective_manifest_owns_listing_module_and_source_delegation() -> None:
    ir = minimal_source_ir(
        source_id="zh.copymanga",
        language="zh",
        source_format="decompiled_apk",
        main_class="CopyManga",
        capabilities=[Capability.POPULAR, Capability.LATEST],
        filter_specs=_copymanga_filter_specs(),
        files=_copymanga_listing_files(),
    )
    manifest = GenerationManifest(
        source_struct="CopyManga",
        files=[
            GeneratedFile(path="src/lib.rs", content="mod source;"),
            GeneratedFile(
                path="src/source.rs",
                content="""
                pub struct CopyManga;
                impl Source for CopyManga {
                    fn get_search_manga_list(
                        &self,
                        query: Option<String>,
                        page: i32,
                        filters: Vec<FilterValue>,
                    ) -> Result<MangaPageResult> {
                        panic!("provider-owned listing")
                    }
                }
                impl aidoku::ListingProvider for CopyManga {
                    fn get_manga_list(
                        &self,
                        listing: aidoku::Listing,
                        page: i32,
                    ) -> aidoku::Result<aidoku::MangaPageResult> {
                        panic!("provider-owned popular/latest")
                    }
                }
                """,
            ),
            GeneratedFile(
                path="src/c2a_listing.rs",
                content='compile_error!("provider must not own this file");',
            ),
        ],
    )

    effective = with_deterministic_search_listing(ir, manifest)
    files = {generated.path: generated.content for generated in effective.files}

    assert "provider-owned listing" not in files["src/source.rs"]
    assert (
        "crate::c2a_listing::get_search_manga_list(query, page, filters)" in files["src/source.rs"]
    )
    assert "compile_error!" not in files["src/c2a_listing.rs"]
    assert "impl aidoku::ListingProvider for CopyManga" in files["src/source.rs"]
    assert "crate::c2a_listing::get_manga_list(listing, page)" in files["src/source.rs"]
    assert "provider-owned popular/latest" not in files["src/source.rs"]
    assert "get_manga_list(aidoku, aidoku)" not in files["src/source.rs"]
    assert "ListingProvider" in effective.implemented_traits
    assert "ListingProvider" in files["src/lib.rs"]
    assert "mod c2a_listing;" in files["src/lib.rs"]
    assert any(
        dependency.name == "serde" and "derive" in dependency.features
        for dependency in effective.dependencies
    )


def test_generation_context_omits_tool_owned_listing_evidence() -> None:
    ir = minimal_source_ir(
        source_id="zh.copymanga",
        language="zh",
        source_format="decompiled_apk",
        main_class="CopyManga",
        capabilities=[Capability.POPULAR, Capability.LATEST],
        filter_specs=_copymanga_filter_specs(),
        files=_copymanga_listing_files(),
    )

    context = build_generation_context(ir).as_payload()
    evidence = {item["path"]: item["content"] for item in context["source_evidence"]}

    api_repo = evidence["sources/example/api/ApiRepo.java"]
    assert "comicListUrl" not in api_repo
    assert "comicRankUrl" not in api_repo
    assert "searchUrl" not in api_repo
    assert "recommendPageUrl" not in api_repo
    assert "newestPageUrl" not in api_repo
    source = evidence["sources/example/CopyManga.java"]
    assert "searchMangaRequest" not in source
    assert "popularMangaRequest" not in source
    assert "latestUpdatesRequest" not in source
    omitted = {item["path"]: item["reason"] for item in context["omitted_source_files"]}
    assert omitted["sources/example/api/dto/ComicSummary.java"] == (
        "represented_in_deterministic_search_listing"
    )
    assert context["context_stats"]["deterministic_search_listing_dto_shapes"] >= 5
