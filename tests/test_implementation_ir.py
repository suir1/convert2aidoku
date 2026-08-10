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
    MangaMappingIR,
    project_implementation_ir,
)
from convert2aidoku.listing_renderer import (
    render_search_listing,
    with_deterministic_search_listing,
)
from convert2aidoku.manga_detail_renderer import (
    render_manga_detail,
    with_deterministic_manga_detail,
)
from convert2aidoku.models import (
    Capability,
    GeneratedFile,
    GenerationManifest,
    ImageUrlPolicy,
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
                protected MangasPage popularMangaParse(Response response) {
                    if (contains(response.url(), "/api/v3/comics")) {
                        Reflection.typeOf(
                            ApiResponse.class,
                            Reflection.typeOf(RecommendResult.class)
                        );
                    }
                    return page;
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
                protected MangasPage latestUpdatesParse(Response response) {
                    if (contains(response.url(), "/api/v3/update/newest")) {
                        Reflection.typeOf(
                            ApiResponse.class,
                            Reflection.typeOf(NewestResult.class)
                        );
                    }
                    if (contains(response.url(), "/api/v3/comics")) {
                        Reflection.typeOf(
                            ApiResponse.class,
                            Reflection.typeOf(ComicsListResult.class)
                        );
                    }
                    return page;
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
                    } else if (rankIndex > 0 || audienceIndex > 0) {
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
                    manga.setTitle(this.name);
                    manga.setAuthor(join(this.author, value -> value.getName()));
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
                protected Request searchMangaRequest(
                    int page,
                    String query,
                    FilterList filters
                ) {
                    if (!query.isBlank()) {
                        return GET(ApiRepo.INSTANCE.searchUrl(page, query));
                    }
                    return GET(ApiRepo.INSTANCE.comicListUrl(page));
                }
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
                    manga.setAuthor(this.author);
                    manga.setGenre(join(this.typeNames));
                    return manga;
                }
            }
            """,
        ),
    ]


def _copymanga_detail_files() -> list[SourceFile]:
    return [
        _file(
            "sources/example/ApiRepo.java",
            """
            public final class ApiRepo {
                private String getApiUrl() { return domain() + "/api/v3"; }
                public String comicDetailUrl(String comicPath) {
                    return getApiUrl() + "/comic2/" + comicPath;
                }
                public String url2comicPath(String value) { return normalize(value); }
            }
            """,
        ),
        _file(
            "sources/example/CopyManga.java",
            """
            public final class CopyManga extends HttpSource {
                public Request mangaDetailsRequest(SManga manga) {
                    return GET(ApiRepo.INSTANCE.comicDetailUrl(
                        ApiRepo.INSTANCE.url2comicPath(manga.getUrl())
                    ));
                }
                protected SManga mangaDetailsParse(Response response) {
                    KType type = Reflection.typeOf(
                        ApiResponse.class,
                        Reflection.typeOf(ComicDetailResult.class)
                    );
                    return ((ComicDetailResult) ((ApiResponse) decode(type))
                        .getResults()).getComic().toSManga("1500", language());
                }
            }
            """,
        ),
        _file(
            "sources/example/api/ApiResponse.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class ApiResponse<T> { private final T results; }
            """,
        ),
        _file(
            "sources/example/api/dto/ComicDetailResult.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class ComicDetailResult {
                private final ComicDetail comic;
                private final Map<String, GroupInfo> groups;
            }
            """,
        ),
        _file(
            "sources/example/api/dto/ComicDetail.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final class ComicDetail {
                // Serialized field names:
                // pathWord -> "path_word"
                private final String pathWord;
                private final String name;
                private final String cover;
                private final String brief;
                private final List<AuthorInfo> author;
                private final Region region;
                private final List<ThemeInfo> theme;
                private final Status status;
                public SManga toSManga(String resolution, CCOption language) {
                    SManga manga = SManga.create();
                    manga.setUrl("/comic/" + this.pathWord);
                    manga.setTitle(TranslateKt.translate(this.name, language));
                    manga.setAuthor(join(this.author, value -> value.getName()));
                    manga.setDescription(TranslateKt.translate(this.brief, language));
                    manga.setGenre(TranslateKt.translate(
                        this.region.getDisplay() + ", "
                            + join(this.theme, value -> value.getName()),
                        language
                    ));
                    manga.setStatus(MangaStatusManager.INSTANCE.parseStatus(
                        this.status.getValue()
                    ));
                    manga.setThumbnail_url(ResolutionOption.INSTANCE.translate(
                        this.cover,
                        resolution
                    ));
                    return manga;
                }
            }
            """,
        ),
        _file(
            "sources/example/MangaStatusManager.java",
            """
            public final class MangaStatusManager {
                public int parseStatus(int status) {
                    if (status == 0) { return 1; }
                    return (1 > status || status >= 3) ? 0 : 2;
                }
            }
            """,
        ),
        *[
            _file(
                f"sources/example/api/dto/{name}.java",
                f"""
                // C2A compacted JADX DTO: generated constructors and value methods removed.
                public final class {name} {{ {fields} }}
                """,
            )
            for name, fields in (
                ("AuthorInfo", "private final String name;"),
                ("ThemeInfo", "private final String name;"),
                ("Region", "private final String display;"),
                ("Status", "private final int value;"),
                (
                    "GroupInfo",
                    "private final String name; private final String pathWord;",
                ),
            )
        ],
    ]


def _copymanga_filter_specs() -> list[SourceFilterSpec]:
    return [
        SourceFilterSpec(
            source_class=f"{source_name}Filter",
            id=filter_id,
            title=filter_id,
            kind="sort" if filter_id == "sort" else "select",
            options=[
                SourceFilterOption(title="Default", value=default_value),
                SourceFilterOption(title="Alternative", value=alternative),
            ],
            default_ascending=True if filter_id == "sort" else None,
        )
        for source_name, filter_id, default_value, alternative in (
            ("Type", "type", "", "name"),
            ("Rank", "rank", "", "day"),
            ("Audience", "audience", "male", "female"),
            ("Region", "region", "", "japan"),
            ("FreeType", "free_type", "", "1"),
            ("Sort", "sort", "datetime_updated", "popular"),
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
    assert endpoints["recommendPageUrl"].response_type == "RecommendResult"
    assert endpoints["recommendPageUrl"].response_evidence == "parser_call"
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
    assert listing.search_dispatch is not None
    assert listing.search_dispatch.query_endpoint_id == "search"
    assert listing.search_dispatch.default_endpoint_id == "comic_list"
    assert [
        (activation.filter_id, activation.default_value)
        for activation in listing.search_dispatch.conditional_routes[0].activate_when_any
    ] == [("rank", ""), ("audience", "male")]

    containers = {container.type_name: container for container in listing.containers}
    assert "ChapterListResult" not in containers
    assert containers["ComicsListResult"].envelope_path == "results"
    assert containers["ComicsListResult"].item_type == "ComicSummary"
    assert containers["RankResult"].item_wrapper_path == "comic"
    assert containers["RankResult"].manga_item_type == "ComicSummary"

    mapping = next(item for item in listing.manga_mappings if item.item_type == "ComicSummary")
    assert mapping.key_template == "/comic/{pathWord}"
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


def test_search_dispatch_does_not_depend_on_rank_filter_id() -> None:
    specs = [
        spec.model_copy(update={"id": "period"}) if spec.source_class == "RankFilter" else spec
        for spec in _copymanga_filter_specs()
    ]
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=_copymanga_listing_files(),
        filter_specs=specs,
    )

    implementation = project_implementation_ir(ir)
    rendered = render_search_listing(ir, implementation)

    assert implementation.listing is not None
    assert implementation.listing.search_dispatch is not None
    route = implementation.listing.search_dispatch.conditional_routes[0]
    assert [activation.filter_id for activation in route.activate_when_any] == [
        "period",
        "audience",
    ]
    assert 'selected_value(&filters, "period")' in rendered.content
    assert 'selected_value(&filters, "audience")' in rendered.content


def test_search_dispatch_uses_branch_filters_not_endpoint_parameters() -> None:
    files = _copymanga_listing_files()
    source = next(file for file in files if file.path.endswith("CopyManga.java"))
    source.content = source.content.replace(
        "rankIndex > 0 || audienceIndex > 0",
        "rankIndex > 0",
    )
    source.sha256 = hashlib.sha256(source.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
    )

    implementation = project_implementation_ir(ir)
    rendered = render_search_listing(ir, implementation)

    assert implementation.listing is not None
    assert implementation.listing.search_dispatch is not None
    route = implementation.listing.search_dispatch.conditional_routes[0]
    assert [activation.filter_id for activation in route.activate_when_any] == ["rank"]
    dispatch = rendered.content.split("pub(crate) fn get_search_manga_list", 1)[1]
    assert 'selected_value(&filters, "rank")' in dispatch
    assert 'selected_value(&filters, "audience")' not in dispatch


@pytest.mark.parametrize(
    "condition",
    ["rankIndex == 0", "rankIndex > 0 && audienceIndex > 0"],
)
def test_search_dispatch_rejects_unsupported_branch_semantics(condition: str) -> None:
    files = _copymanga_listing_files()
    source = next(file for file in files if file.path.endswith("CopyManga.java"))
    source.content = source.content.replace(
        "rankIndex > 0 || audienceIndex > 0",
        condition,
    )
    source.sha256 = hashlib.sha256(source.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
    )

    implementation = project_implementation_ir(ir)

    assert implementation.listing is not None
    assert implementation.listing.search_dispatch is None
    assert "listing search dispatch is unresolved" in implementation.unresolved_facts
    with pytest.raises(ValueError, match="no deterministic search dispatch"):
        render_search_listing(ir, implementation)


def test_search_dispatch_rejects_blank_query_branch() -> None:
    files = _copymanga_listing_files()
    source = next(file for file in files if file.path.endswith("CopyManga.java"))
    source.content = source.content.replace("!query.isBlank()", "query.isBlank()")
    source.sha256 = hashlib.sha256(source.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
    )

    implementation = project_implementation_ir(ir)

    assert implementation.listing is not None
    assert implementation.listing.search_dispatch is None
    assert "listing search dispatch is unresolved" in implementation.unresolved_facts


def test_search_dispatch_rejects_nonzero_default_index() -> None:
    specs = [
        spec.model_copy(update={"default_index": 1}) if spec.source_class == "RankFilter" else spec
        for spec in _copymanga_filter_specs()
    ]
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=_copymanga_listing_files(),
        filter_specs=specs,
    )

    implementation = project_implementation_ir(ir)

    assert implementation.listing is not None
    assert implementation.listing.search_dispatch is None
    assert "listing search dispatch is unresolved" in implementation.unresolved_facts


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


def test_projects_kotlin_data_class_join_to_string_exactly() -> None:
    files = _serializable_listing_files()
    item = next(file for file in files if file.path.endswith("ComicItem.java"))
    item.content = item.content.replace(
        "private final List<String> typeNames;",
        "private final List<ThemeInfo> themes;",
    ).replace(
        "join(this.typeNames)",
        "CollectionsKt.joinToString$default(this.themes, null, null, null, 0, null, "
        "(Function1) null, 63, (Object) null)",
    )
    item.sha256 = hashlib.sha256(item.content.encode()).hexdigest()
    files.append(
        _file(
            "sources/example/ThemeInfo.java",
            """
            // C2A compacted JADX DTO: generated constructors and value methods removed.
            public final /* data */ class ThemeInfo {
                private final String name;
                private final String pathWord;
            }
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
    mapping = implementation.listing.manga_mappings[0]
    assert mapping.tags_path == "themes[]"
    assert mapping.tags_item_projection == "kotlin_data_to_string"
    assert mapping.unresolved_fields == []
    assert (
        'format!("ThemeInfo(name={}, pathWord={})", value.name, value.path_word)'
        in rendered.content
    )


def test_manga_key_projection_preserves_complete_concatenation() -> None:
    files = _serializable_listing_files()
    item = next(file for file in files if file.path.endswith("ComicItem.java"))
    item.content = item.content.replace(
        '"/comic/" + this.comicId',
        '"/comic/" + this.comicId + "/view"',
    )
    item.sha256 = hashlib.sha256(item.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=files,
    )

    implementation = project_implementation_ir(ir)

    assert implementation.listing is not None
    mapping = implementation.listing.manga_mappings[0]
    assert mapping.key_template == "/comic/{comicId}/view"


@pytest.mark.parametrize(
    "expression",
    ["buildUrl(this.comicId)", '"https://example.com/comic/" + this.comicId'],
)
def test_unsupported_manga_key_is_left_unresolved(expression: str) -> None:
    files = _serializable_listing_files()
    item = next(file for file in files if file.path.endswith("ComicItem.java"))
    item.content = item.content.replace('"/comic/" + this.comicId', expression)
    item.sha256 = hashlib.sha256(item.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=files,
    )

    implementation = project_implementation_ir(ir)

    assert "listing manga key mapping is unresolved for ComicItem" in (
        implementation.unresolved_facts
    )
    with pytest.raises(ValueError, match="unresolved fields: key"):
        render_search_listing(ir, implementation)


def test_multifield_title_is_not_reduced_to_first_field() -> None:
    files = _serializable_listing_files()
    item = next(file for file in files if file.path.endswith("ComicItem.java"))
    item.content = item.content.replace(
        "private final String name;",
        "private final String name; private final String subtitle;",
    ).replace("setTitle(this.name)", "setTitle(this.name + this.subtitle)")
    item.sha256 = hashlib.sha256(item.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=files,
    )

    implementation = project_implementation_ir(ir)

    assert "listing manga title mapping is unresolved for ComicItem" in (
        implementation.unresolved_facts
    )


@pytest.mark.parametrize(
    "statement",
    [
        "manga.setTitle(decrypt(this.name));",
        "manga.setTitle(this.name); manga.setTitle(this.cover);",
        "if (enabled) { manga.setTitle(this.name); }",
    ],
)
def test_nonidentity_or_nonunique_title_setter_is_unresolved(statement: str) -> None:
    files = _serializable_listing_files()
    item = next(file for file in files if file.path.endswith("ComicItem.java"))
    item.content = item.content.replace("manga.setTitle(this.name);", statement)
    item.sha256 = hashlib.sha256(item.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=files,
    )

    implementation = project_implementation_ir(ir)

    assert "listing manga title mapping is unresolved for ComicItem" in (
        implementation.unresolved_facts
    )


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("this.cover", "cdn(this.cover)", "cover"),
        ("join(this.typeNames)", "first(this.typeNames)", "tags"),
    ],
)
def test_unsupported_optional_projection_blocks_ownership(
    old: str,
    new: str,
    field: str,
) -> None:
    files = _serializable_listing_files()
    item = next(file for file in files if file.path.endswith("ComicItem.java"))
    item.content = item.content.replace(old, new)
    item.sha256 = hashlib.sha256(item.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=files,
    )

    implementation = project_implementation_ir(ir)

    assert f"listing manga {field} mapping is unresolved for ComicItem" in (
        implementation.unresolved_facts
    )
    with pytest.raises(ValueError, match=f"unresolved fields: {field}"):
        render_search_listing(ir, implementation)


def test_public_scope_can_project_explicitly_excluded_presentation_wrappers() -> None:
    files = _copymanga_listing_files()
    summary = next(file for file in files if file.path.endswith("ComicSummary.java"))
    summary.content = (
        summary.content.replace(
            "toSManga()",
            "toSManga(CCOption language, String resolution)",
        )
        .replace(
            "setTitle(this.name)",
            "setTitle(convert(this.name, language))",
        )
        .replace(
            "setThumbnail_url(this.cover)",
            "setThumbnail_url(resize(this.cover, resolution))",
        )
        .replace(
            "value -> value.getName()",
            "value -> convert(value.getName(), language)",
        )
    )
    summary.sha256 = hashlib.sha256(summary.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        feature_scope="public_only",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
        image_url_policy=ImageUrlPolicy(preserve_cover_urls=True),
        unsupported_features=[
            "Android ChineseUtils script conversion setting (excluded by public-only APK scope)"
        ],
    )

    implementation = project_implementation_ir(ir)

    assert implementation.listing is not None
    mapping = next(
        item for item in implementation.listing.manga_mappings if item.item_type == "ComicSummary"
    )
    assert mapping.unresolved_fields == []
    assert mapping.policy_fallback_fields == ["title", "cover", "authors"]
    assert mapping.title_path == "name"
    assert mapping.cover_path == "cover"
    assert mapping.authors_path == "author[].name"
    assert implementation.policy_fallback_facts == [
        "listing manga title uses the raw API field for ComicSummary because its presentation "
        "transform is excluded by SourceIR policy",
        "listing manga cover uses the raw API field for ComicSummary because its presentation "
        "transform is excluded by SourceIR policy",
        "listing manga authors uses the raw API field for ComicSummary because its presentation "
        "transform is excluded by SourceIR policy",
    ]
    effective = with_deterministic_search_listing(
        ir,
        GenerationManifest(
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
                            panic!("AI listing")
                        }
                    }
                    """,
                ),
            ],
        ),
    )
    assert any(
        warning.startswith("Deterministic listing intentionally uses raw API fields")
        for warning in effective.warnings
    )


def test_excluded_script_policy_does_not_hide_unrelated_transform() -> None:
    files = _copymanga_listing_files()
    summary = next(file for file in files if file.path.endswith("ComicSummary.java"))
    summary.content = summary.content.replace(
        "toSManga()",
        "toSManga(CCOption language)",
    ).replace(
        "setTitle(this.name)",
        "setTitle(decrypt(this.name, language))",
    )
    summary.sha256 = hashlib.sha256(summary.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        feature_scope="public_only",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
        unsupported_features=[
            "Android ChineseUtils script conversion setting (excluded by public-only APK scope)"
        ],
    )

    implementation = project_implementation_ir(ir)

    assert "listing manga title mapping is unresolved for ComicSummary" in (
        implementation.unresolved_facts
    )
    mapping = next(
        item for item in implementation.listing.manga_mappings if item.item_type == "ComicSummary"
    )
    assert mapping.policy_fallback_fields == []


def test_object_collection_child_comes_from_setter_getter() -> None:
    files = _copymanga_listing_files()
    for source in files:
        if source.path.endswith("ComicSummary.java"):
            source.content = source.content.replace("getName()", "getLabel()")
        elif source.path.endswith("AuthorInfo.java"):
            source.content = source.content.replace("String name", "String label")
        source.sha256 = hashlib.sha256(source.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
    )

    implementation = project_implementation_ir(ir)

    assert implementation.listing is not None
    mapping = next(
        item for item in implementation.listing.manga_mappings if item.item_type == "ComicSummary"
    )
    assert mapping.authors_path == "author[].label"


def test_object_collection_without_getter_is_not_guessed_as_name() -> None:
    files = _copymanga_listing_files()
    summary = next(file for file in files if file.path.endswith("ComicSummary.java"))
    summary.content = summary.content.replace(
        "join(this.author, value -> value.getName())",
        "join(this.author)",
    )
    summary.sha256 = hashlib.sha256(summary.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
    )

    implementation = project_implementation_ir(ir)

    assert implementation.listing is not None
    mapping = next(
        item for item in implementation.listing.manga_mappings if item.item_type == "ComicSummary"
    )
    assert mapping.authors_path is None


def test_wrapped_object_collection_getter_is_unresolved() -> None:
    files = _copymanga_listing_files()
    summary = next(file for file in files if file.path.endswith("ComicSummary.java"))
    summary.content = summary.content.replace(
        "value -> value.getName()",
        "value -> normalize(value.getName())",
    )
    summary.sha256 = hashlib.sha256(summary.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
    )

    implementation = project_implementation_ir(ir)

    assert "listing manga authors mapping is unresolved for ComicSummary" in (
        implementation.unresolved_facts
    )


def test_scalar_object_is_not_rendered_as_string_metadata() -> None:
    files = _serializable_listing_files()
    item = next(file for file in files if file.path.endswith("ComicItem.java"))
    item.content = item.content.replace("String author", "AuthorInfo author")
    item.sha256 = hashlib.sha256(item.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=files,
    )

    implementation = project_implementation_ir(ir)

    assert implementation.listing is not None
    assert implementation.listing.manga_mappings[0].authors_path is None


def test_mapping_field_identity_is_not_confused_with_serialized_name() -> None:
    files = _serializable_listing_files()
    item = next(file for file in files if file.path.endswith("ComicItem.java"))
    item.content = (
        item.content.replace(
            "public final class ComicItem {",
            '''public final class ComicItem {
                // Serialized field names:
                // comicId -> "slug"
                // alias -> "comicId"''',
        )
        .replace(
            "private final String comicId;",
            "private final String comicId; private final String alias;",
        )
        .replace("this.comicId", "this.alias")
    )
    item.sha256 = hashlib.sha256(item.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=files,
    )

    implementation = project_implementation_ir(ir)
    rendered = render_search_listing(ir, implementation)

    assert implementation.listing is not None
    assert implementation.listing.manga_mappings[0].key_template == "/comic/{alias}"
    assert 'format!("/comic/{}", item.comic_id)' in rendered.content
    assert 'format!("/comic/{}", item.slug)' not in rendered.content


def test_nested_mapping_field_uses_java_identity_before_serialized_name() -> None:
    files = _copymanga_listing_files()
    author = next(file for file in files if file.path.endswith("AuthorInfo.java"))
    author.content = author.content.replace(
        "public final class AuthorInfo { private final String name; }",
        """public final class AuthorInfo {
            // Serialized field names:
            // name -> "label"
            // alias -> "name"
            private final String name;
            private final String alias;
        }""",
    )
    author.sha256 = hashlib.sha256(author.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
    )

    implementation = project_implementation_ir(ir)
    rendered = render_search_listing(ir, implementation)

    assert ".map(|value| value.label)" in rendered.content
    author_struct = rendered.content.split("struct AuthorInfo {", 1)[1].split("}\n", 1)[0]
    assert "    label: String," in author_struct
    assert "    name: String," not in author_struct


def test_projects_copymanga_detail_contract_from_parser_evidence() -> None:
    ir = minimal_source_ir(
        source_id="zh.copymanga",
        source_format="decompiled_apk",
        feature_scope="public_only",
        main_class="CopyManga",
        capabilities=[Capability.DETAILS],
        files=_copymanga_detail_files(),
        image_url_policy=ImageUrlPolicy(preserve_cover_urls=True),
        unsupported_features=[
            "Android ChineseUtils script conversion setting (excluded by public-only APK scope)"
        ],
    )

    implementation = project_implementation_ir(ir)

    assert implementation.manga_detail is not None
    detail = implementation.manga_detail
    assert detail.endpoint.model_dump(mode="json") == {
        "source_method": "comicDetailUrl",
        "path": "/api/v3/comic2/{comic_path}",
        "key_parameter": "comic_path",
        "response_type": "ComicDetailResult",
        "response_evidence": "parser_path",
        "envelope_path": "results",
        "item_path": "comic",
    }
    assert detail.mapping.key_template == "/comic/{pathWord}"
    assert detail.mapping.title_path == "name"
    assert detail.mapping.cover_path == "cover"
    assert detail.mapping.authors_path == "author[].name"
    assert detail.mapping.tags_paths == ["region.display", "theme[].name"]
    assert detail.mapping.description_path == "brief"
    assert detail.mapping.status_path == "status.value"
    assert detail.mapping.status_values == {0: "ongoing", 1: "completed", 2: "completed"}
    assert detail.mapping.unresolved_fields == []
    assert detail.mapping.policy_fallback_fields == ["title", "cover", "tags", "description"]
    assert not [fact for fact in implementation.unresolved_facts if "detail" in fact]
    assert (
        len(
            [
                fact
                for fact in implementation.policy_fallback_facts
                if fact.startswith("manga detail")
            ]
        )
        == 4
    )
    rendered = render_manga_detail(ir, implementation)
    assert rendered.path == "src/c2a_manga_detail.rs"
    assert '"/api/v3/comic2/{}"' in rendered.content
    assert "        manga_path," in rendered.content
    assert "let response: DetailEnvelope" in rendered.content
    assert "let item = result.comic;" in rendered.content
    assert ".map(|value| value.name)" in rendered.content
    assert "tags.push(item.region.display);" in rendered.content
    assert "tags.extend(item.theme.into_iter().map(|value| value.name));" in rendered.content
    assert "manga.description = Some(item.brief);" in rendered.content
    assert "0 => MangaStatus::Ongoing" in rendered.content
    assert "1 => MangaStatus::Completed" in rendered.content
    effective = with_deterministic_manga_detail(
        ir,
        GenerationManifest(
            source_struct="CopyManga",
            files=[
                GeneratedFile(path="src/lib.rs", content="mod source;"),
                GeneratedFile(path="src/source.rs", content="pub struct CopyManga;"),
                GeneratedFile(path="src/c2a_listing.rs", content="fn listing() {}"),
                GeneratedFile(
                    path="src/c2a_manga_detail.rs",
                    content='compile_error!("AI must not own this file");',
                ),
            ],
        ),
    )
    effective_files = {generated.path: generated.content for generated in effective.files}
    assert "compile_error!" not in effective_files["src/c2a_manga_detail.rs"]
    assert "mod c2a_manga_detail;" in effective_files["src/lib.rs"]
    assert any(dependency.name == "serde" for dependency in effective.dependencies)
    assert any(warning.startswith("Deterministic manga detail") for warning in effective.warnings)


def test_unknown_detail_transform_stays_unresolved() -> None:
    files = _copymanga_detail_files()
    detail = next(file for file in files if file.path.endswith("ComicDetail.java"))
    detail.content = detail.content.replace(
        "TranslateKt.translate(this.name, language)",
        "decrypt(this.name, language)",
    )
    detail.sha256 = hashlib.sha256(detail.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        feature_scope="public_only",
        capabilities=[Capability.DETAILS],
        files=files,
        image_url_policy=ImageUrlPolicy(preserve_cover_urls=True),
        unsupported_features=[
            "Android ChineseUtils script conversion setting (excluded by public-only APK scope)"
        ],
    )

    implementation = project_implementation_ir(ir)

    assert implementation.manga_detail is not None
    assert implementation.manga_detail.mapping.title_path is None
    assert implementation.manga_detail.mapping.policy_fallback_fields == [
        "cover",
        "tags",
        "description",
    ]
    assert "manga detail title mapping is unresolved for ComicDetail" in (
        implementation.unresolved_facts
    )


def test_ambiguous_detail_url_helper_stays_unresolved() -> None:
    files = _copymanga_detail_files()
    files.append(
        _file(
            "sources/example/MirrorApi.java",
            """
            public final class MirrorApi {
                public String comicDetailUrl(String comicPath) {
                    return getApiUrl() + "/mirror/" + comicPath;
                }
            }
            """,
        )
    )
    ir = minimal_source_ir(
        source_format="decompiled_apk",
        capabilities=[Capability.DETAILS],
        files=files,
    )

    implementation = project_implementation_ir(ir)

    assert implementation.manga_detail is None
    assert "manga detail request or response contract is unresolved" in (
        implementation.unresolved_facts
    )


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


def test_markerless_default_response_does_not_depend_on_dto_name() -> None:
    files = _copymanga_listing_files()
    for source in files:
        source.content = source.content.replace("ComicsListResult", "BrowsePayload")
        source.sha256 = hashlib.sha256(source.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
    )

    implementation = project_implementation_ir(ir)
    rendered = render_search_listing(ir, implementation)

    assert implementation.listing is not None
    browse = next(
        endpoint
        for endpoint in implementation.listing.endpoints
        if endpoint.source_method == "comicListUrl"
    )
    assert browse.response_type == "BrowsePayload"
    assert browse.response_evidence == "parser_default"
    assert "let result = response.value;" in rendered.content


def test_listing_intent_uses_request_calls_before_helper_names() -> None:
    files = _copymanga_listing_files()
    replacements = {
        "searchUrl": "lookupEndpoint",
        "comicRankUrl": "conditionalEndpoint",
        "comicListUrl": "defaultEndpoint",
        "/search/comic": "/lookup",
        "/ranks": "/scoreboard",
        '"/comics?': '"/catalog?',
    }
    for source in files:
        for old, new in replacements.items():
            source.content = source.content.replace(old, new)
        source.sha256 = hashlib.sha256(source.content.encode()).hexdigest()
    ir = minimal_source_ir(
        source_id="zh.example",
        source_format="decompiled_apk",
        main_class="CopyManga",
        files=files,
        filter_specs=_copymanga_filter_specs(),
    )

    implementation = project_implementation_ir(ir)
    rendered = render_search_listing(ir, implementation)

    assert implementation.listing is not None
    endpoints = {endpoint.source_method: endpoint for endpoint in implementation.listing.endpoints}
    assert endpoints["lookupEndpoint"].role == ListingRole.SEARCH
    assert endpoints["conditionalEndpoint"].role == ListingRole.RANK
    assert endpoints["defaultEndpoint"].role == ListingRole.BROWSE
    assert implementation.listing.search_dispatch is not None
    assert implementation.listing.search_dispatch.query_endpoint_id == "lookup_endpoint"
    assert implementation.listing.search_dispatch.default_endpoint_id == "default_endpoint"
    assert 'api_url("/api/v3/lookup")' in rendered.content
    assert 'api_url("/api/v3/catalog")' in rendered.content


def test_name_only_response_match_cannot_authorize_deterministic_ownership() -> None:
    ir = minimal_source_ir(
        source_id="en.example",
        source_format="decompiled_apk",
        main_class="Example",
        files=_serializable_listing_files(),
    )
    implementation = project_implementation_ir(ir)
    assert implementation.listing is not None
    listing = implementation.listing
    endpoints = [
        endpoint.model_copy(update={"response_evidence": "name_match"})
        if endpoint.id == listing.search_dispatch.default_endpoint_id
        else endpoint
        for endpoint in listing.endpoints
    ]
    low_confidence = implementation.model_copy(
        update={"listing": listing.model_copy(update={"endpoints": endpoints})}
    )

    with pytest.raises(ValueError, match="requires parser evidence"):
        render_search_listing(ir, low_confidence)


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


def test_manga_mapping_rejects_ambiguous_or_missing_policy_fallbacks() -> None:
    with pytest.raises(ValidationError, match="cannot be unresolved and policy fallbacks"):
        MangaMappingIR(
            item_type="Comic",
            title_path="name",
            unresolved_fields=["title"],
            policy_fallback_fields=["title"],
        )
    with pytest.raises(ValidationError, match="require a recovered projection"):
        MangaMappingIR(
            item_type="Comic",
            policy_fallback_fields=["cover"],
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
    assert 'value != ""' in rendered.content
    assert 'selected_value(&filters, "audience")' in rendered.content
    assert 'value != "male"' in rendered.content
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
            GeneratedFile(
                path="src/api.rs",
                content="""
                impl ListingProvider for CopyManga {
                    fn get_manga_list(
                        &self,
                        listing: Listing,
                        page: i32,
                    ) -> Result<MangaPageResult> {
                        panic!("provider-owned duplicate")
                    }
                }
                """,
            ),
            GeneratedFile(
                path="res/settings.json",
                content="""[
                    {
                        "type": "group",
                        "title": "Settings",
                        "items": [
                            {
                                "type": "select",
                                "key": "v2.pref.platform",
                                "title": "Platform",
                                "values": ["platform.none", "platform.one", "platform.two"],
                                "titles": ["None", "1", "2"],
                                "default": "platform.one"
                            }
                        ]
                    }
                ]""",
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
    assert "provider-owned duplicate" not in files["src/api.rs"]
    assert "impl ListingProvider for CopyManga" not in files["src/api.rs"]
    assert "get_manga_list(aidoku, aidoku)" not in files["src/source.rs"]
    assert "ListingProvider" in effective.implemented_traits
    assert "ListingProvider" in files["src/lib.rs"]
    assert "mod c2a_listing;" in files["src/lib.rs"]
    assert 'defaults_get::<String>("v2.pref.platform")' in files["src/c2a_listing.rs"]
    assert 'Some("platform.none") => None' in files["src/c2a_listing.rs"]
    assert 'Some("platform.one") => Some("1")' in files["src/c2a_listing.rs"]
    assert 'request = request.header("platform", platform);' in files["src/c2a_listing.rs"]
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
