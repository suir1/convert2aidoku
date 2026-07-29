from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from convert2aidoku.implementation_ir import (
    ApiBaseIR,
    ImplementationIR,
    ListingEndpointIR,
    ListingImplementationIR,
    ListingRole,
    project_implementation_ir,
)
from convert2aidoku.models import RequestHeaderProfile, SourceFile
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
                protected MangasPage searchMangaParse(Response response) {
                    if (contains(response.url(), "/api/v3/search/comic")) {
                        Reflection.typeOf(SearchResult.class);
                    }
                    if (contains(response.url(), "/api/v3/ranks")) {
                        Reflection.typeOf(RankResult.class);
                    }
                    Reflection.typeOf(ComicsListResult.class);
                    return page;
                }
                protected Request searchMangaRequest(int page, String query, FilterList filters) {
                    if (!query.isBlank()) {
                        builder = get(ApiRepo.INSTANCE.searchUrl(page)).newBuilder();
                        builder.addQueryParameter("q", query);
                        builder.addQueryParameter("q_type", typeValue);
                    } else if (rank > 0) {
                        builder = get(ApiRepo.INSTANCE.comicRankUrl(page)).newBuilder();
                        builder.addQueryParameter("date_type", rankValue);
                        builder.addQueryParameter("audience_type", audienceValue);
                    } else {
                        builder = get(ApiRepo.INSTANCE.comicListUrl(page)).newBuilder();
                        builder.addQueryParameter("top", regionValue);
                        builder.addQueryParameter("theme", themeValue);
                        builder.addQueryParameter("ordering", orderingValue);
                    }
                    builder.addQueryParameter("_update", "true");
                    return GET(builder.build());
                }
            }
            """,
        ),
        _file(
            "sources/example/api/ApiResponse.java",
            "public final class ApiResponse<T> { private final T results; }",
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


def test_projects_copymanga_listing_contract_without_provider() -> None:
    ir = minimal_source_ir(
        source_id="zh.copymanga",
        language="zh",
        source_format="decompiled_apk",
        main_class="CopyManga",
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
    assert browse_parameters["theme"].value_template == "{filter:theme}"
    assert not browse_parameters["theme"].required
    assert browse_parameters["_update"].value_template == "true"
    search_parameters = {
        parameter.name: parameter for parameter in endpoints["searchUrl"].query_parameters
    }
    assert search_parameters["q"].value_template == "{query}"
    assert search_parameters["q_type"].value_template == "{filter:type}"
    assert endpoints["searchUrl"].response_type == "SearchResult"
    assert endpoints["searchUrl"].response_evidence == "parser_path"
    assert endpoints["comicRankUrl"].response_type == "RankResult"
    assert endpoints["comicListUrl"].response_type == "ComicsListResult"

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
        "RankResult",
        "RecommendResult",
        "SearchResult",
    }


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
