# Aidoku source contract used by convert2aidoku

Generate a current `no_std` Rust source using the pinned `aidoku` crate. The generated crate must
compile for `wasm32-unknown-unknown` and must not use Tokio, Reqwest, std networking, filesystem,
threads, Android APIs, or blocking OS primitives.

Required implementation:

```rust
trait Source {
    fn new() -> Self;
    fn get_search_manga_list(
        &self,
        query: Option<String>,
        page: i32,
        filters: Vec<FilterValue>,
    ) -> Result<MangaPageResult>;
    fn get_manga_update(
        &self,
        manga: Manga,
        needs_details: bool,
        needs_chapters: bool,
    ) -> Result<Manga>;
    fn get_page_list(&self, manga: Manga, chapter: Chapter) -> Result<Vec<Page>>;
}
```

All core data types and source traits above are re-exported directly at the `aidoku` crate root.
Import them as `use aidoku::{Chapter, FilterValue, ImageRequestProvider, ListingProvider, ...};`.
There is no public `aidoku::models` module and no public `aidoku::traits` module.

Mapping rules:

- `SManga.url` or stable site identifier becomes `Manga.key`; `SChapter.url` or stable chapter id
  becomes `Chapter.key`.
- Preserve canonical website links separately from stable keys. When the input provides or implies
  a manga/chapter page URL, populate `Manga.url` and `Chapter.url` with absolute URLs even when the
  corresponding keys remain relative.
- Preserve all chapter metadata exposed by the input: map the title's explicit chapter/volume
  number to `chapter_number`/`volume_number`, `date_upload` to `date_uploaded`, and `scanlator` to
  `scanlators`. Use `aidoku::imports::std::parse_date` or `parse_local_date` for source date strings
  instead of discarding them. For date-only values, normalize to a full midnight datetime (for
  example `2025-01-01 00:00:00` with `yyyy-MM-dd HH:mm:ss`) so both Aidoku and the Rust test runner
  can parse it. Do not invent metadata when the input has no corresponding value.
- When `source_ir.capabilities` contains `contextual_chapter_urls`, preserve placeholder chapter
  entries instead of dropping them. The recovered Kotlin assigns the first placeholder the next
  concrete chapter URL plus `#prev`, and later placeholders the previous concrete URL plus
  `#next`. Before fetching pages, request that adjacent chapter URL, scan its response for the
  exact `url_previous:'...'` or `url_next:'...'` value selected by the fragment, and use that
  path. If the response omits it, decrement or increment the numeric chapter segment in
  `/read/<manga>/<chapter>.html`. Finally replace the terminal `.` with `_2.` in the resolved
  reading path and fetch that URL. The fragment is local routing context and must not be sent as
  the final page request fragment.
- If the source exposes a reliable reading direction or country/type signal, populate
  `Manga.viewer` with `Viewer::RightToLeft`, `Viewer::LeftToRight`, `Viewer::Vertical`, or
  `Viewer::Webtoon`; otherwise leave it `Viewer::Unknown`.
- Keys may remain relative (for example `/comics/123` and `/chapters/456`), but every
  `Request::get`/`Request::post` must receive an absolute URL. Centralize an `absolute_url` helper
  using the source base URL before fetching details, chapters, pages, or images; never pass a
  relative `Manga.key` or `Chapter.key` directly to `Request::get`.
- Preserve recovered path separators exactly around interpolated segments. In particular, a route
  ending a placeholder with `}` followed by `/chapters` must not become `}//chapters`; duplicate
  separators change the endpoint on APIs that do not normalize paths.
- When `source_ir.relative_url_keys` is true, an `absolute_url` helper is mandatory. Use it on
  `Manga.key`, `Chapter.key`, and relative image URLs before handing the result to a request
  helper or to `Request::get`/`Request::post`.
- Treat every entry in `source_ir.chapter_page_routes` as normative behavior recovered from the
  input. Preserve its `chapter_key_template`; before requesting pages, select the matching route
  variant, apply `strip_prefix` and each ordered string replacement, then place the normalized key
  into `endpoint_template`. The `is_default` variant applies unless another condition matches.
  Do not simplify `/chapter/` and `/chapter2/` into one endpoint when the IR distinguishes them.
- `MangasPage(mangas, hasNextPage)` becomes `MangaPageResult { entries, has_next_page }`.
- Merge Tachi's detail and chapter operations in `get_manga_update`, respecting both requested
  booleans and avoiding unnecessary requests.
- Create pages with `Page { content: PageContent::url(url), ..Default::default() }`.
- Use `aidoku::imports::net::Request`; `Request::get(...)` returns a `Result`, so propagate it
  before calling `.header(...)`. After sending, use `response.get_html()` or
  `response.get_json_owned()`. The HTML selector methods return `Option` and element lists must be
  iterated with `.get(index)` (or `.first()`), after unwrapping the `Option<ElementList>` (for
  example `if let Some(list) = document.select("img") { for i in 0..list.size() { ... } }`). Only
  individual `Element` values have `.attr()` and `.text()` methods. `get_html()` returns a
  `Document`; convert it with `let document: Element = response.get_html()?.into()` before passing
  it to parser helpers that accept `&Element`.
- Use Aidoku HTML `Element` CSS selector APIs instead of Jsoup.
- Do not copy Jsoup-only selector extensions such as `:contains`, `:containsOwn`, `:eq`, or
  `:first`. They are not portable to the Aidoku test runner. Select a standards-compatible
  superset, inspect `Element::text()`, and navigate with `Element::next()`/`parent()` when the
  source locates an element by its text.
- Use `aidoku::helpers::uri` for URL encoding where possible.
- For JSON API sources, model the response envelope and DTOs with `aidoku::serde::Deserialize`,
  call `response.get_json_owned()` for plain JSON, and preserve the source's page-to-offset or
  cursor calculation exactly. Keep public unauthenticated behavior separate from optional login
  behavior; do not manufacture credentials or silently make login mandatory.
- For decompiled JSON sources, `decompiled_dto_shapes` is the deterministic field/type projection
  recovered from JADX. Preserve its generic collection shapes exactly: a Java
  `Map<String, GroupInfo>` must decode into a Rust map whose values are `GroupInfo`, never
  `String` or another scalar. Serialized names marked with `json` remain the serde field names.
- Tachi `HttpSource` requests use OkHttp, whose idempotent GET path retries transient connection
  failures. For both standard Kotlin modules and `decompiled_apk` JSON sources, preserve that
  behavior with one centralized retry: construct the same GET request again only when the first
  `.send()` returns `RequestError`, then parse the successful response once. Do not retry
  HTML/JSON parsing errors, HTTP application errors, POST requests, or authentication operations.
  A suitable generic helper has the shape
  `match self.request(url.clone())?.send() { Ok(response) => response,
  Err(_) => self.request(url)?.send()? }` followed by `response.get_json_owned()`.
- Do not call `Regex::new` inside chapter/list/detail/page parsing paths for fixed embedded-JSON
  delimiters or simple numeric labels. Those functions run per request (and often once per
  chapter), so compiling a regex there increases latency and WASM size. Prefer bounded
  `find`/slice scans for fixed delimiters and an ASCII digit/decimal scan for chapter numbers.
- A Kotlin override that starts from `super.headersBuilder()` inherits Tachi `HttpSource`'s
  default browser-like `User-Agent`. Preserve that inherited header in addition to every explicit
  `.add(...)`/`.set(...)` header; do not treat only the locally visible header calls as complete.
- Keiyoushi `KeiSource` requests likewise inherit the shared browser-like client User-Agent even
  when `configureHeaders` only lists source-specific headers. Treat every entry in
  `source_ir.shared_request_headers` as mandatory on all normal and image requests.
- When `source_ir.source_format` is `decompiled_apk`, treat the selected JADX Java as a lossy but
  behavior-bearing representation of the original Kotlin. When `feature_scope` is `public_only`,
  implement every detected public search/list/details/chapters/pages capability, but do not
  implement features explicitly listed as excluded authenticated options (login, bookcase, or
  comments). Public endpoints and headers used by those core operations remain mandatory.
- For a source explicitly classified with `encrypted_json`, AES-CBC with PKCS#5/PKCS#7 padding is
  supported through the pinned Dependency Policy crates. Preserve the exact key derivation, IV
  extraction, ciphertext encoding, and padding from the input. Use pinned `hex` or `base64` for
  decoding; never guess keys, IVs, or transformations and never execute site-provided code.
- For a source explicitly classified with `triple_des_cbc`, preserve its exact 24-byte DESede key,
  8-byte IV, payload ordering, timestamp units, base64 variant, and PKCS#5/PKCS#7 padding. Use the
  pinned Dependency Policy crates. If the source creates a random alphanumeric request key,
  generate the same length and alphabet locally; do not replace it with a fixed secret or accept
  cryptographic material from the network.
  `aidoku::imports::std::current_date()` is available and returns Unix seconds; multiply by 1000
  when the Tachi source uses `System.currentTimeMillis()`. Use this live value both in the signed
  payload and the request parameter. Never substitute `0` or another fixed timestamp.
- For a source explicitly classified with `rsa_pkcs1_v15`, preserve the embedded X.509 DER public
  key and RSA/ECB/PKCS1Padding semantics with the pinned `rsa`, `rand_core`, `rand_chacha`, and
  `serde_json` crates. Seed a local ChaCha RNG from live `current_date()` plus source-local varying input; do
  not use `getrandom`, a fixed ciphertext, a private key, or network-provided executable code.
  Preserve device-key types and the exact JSON body used by anonymous-token bootstrap. Persist any
  generated stable device identifier with `defaults_get`/`defaults_set` so later signed requests
  do not silently change identity. If the input exposes manual User ID and Token preferences as a
  fallback needed for public reading, preserve those settings and prefer their non-empty values.
  At the pinned revision, `defaults_set` takes a typed `DefaultValue`; wrap stored strings as
  `aidoku::imports::defaults::DefaultValue::String(value)`.
- For a source explicitly classified with `md5_request_signing`, reproduce the exact source field
  sorting, salt placement, HTTP method casing, body inclusion, URL-encoding rules, live timestamp,
  and lowercase MD5 hex encoding with the pinned `md-5` crate. Sign the same decoded parameter
  values that are sent on the request; do not sign a differently encoded or reordered projection.
- To return a user-visible source error, use `aidoku::AidokuError::message(...)` (or `bail!`). The
  pinned `RequestError` enum has no `new` constructor.
- Preserve JSON response envelopes at every endpoint. If the Tachi input deserializes
  `ApiResponse<Payload>`, the generated Rust must deserialize `ApiResponse<Payload>` and then use
  its `results` field. Do not deserialize the raw HTTP response directly into `Payload` merely
  because all payload fields have serde defaults: that silently produces empty titles, keys,
  groups, or page data while appearing to parse successfully. This applies independently to list,
  rank, detail, chapter, page, recommendation, and dynamic-filter endpoints.
- Dynamic base URLs must come from a finite allowlist extracted from the input. Validate the
  selected defaults value against that allowlist before constructing requests; never accept an
  arbitrary URL from generated settings.
- Convert Tachi filters into `res/filters.json` and handle the resulting `FilterValue` variants.
  Aidoku filter `options` must be an array of display-name strings, never objects. Put the
  corresponding site values in a parallel `ids` string array of the same length. For example:
  `{"type":"select","id":"sort","options":["Latest","Popular"],"ids":["","popular"]}`.
  At the pinned Aidoku revision, the configured select variant is exactly
  `FilterValue::Select { id: String, value: String }`: `value` is the selected site ID from
  `ids`, not a numeric option index. Use the string directly in the request, or map it back to an
  index with `values.iter().position(...)` only when the endpoint logic genuinely needs an index.
  A helper returning a borrowed selected value must tie the return lifetime only to `filters`, for
  example
  `fn selected_value<'a>(filters: &'a [FilterValue], id: &str) -> Option<&'a str>`; cloning the
  selected `String` is also valid. `FilterValue::Sort.index` remains an `i32`, not a `usize`.
  Every ids entry must be unique. Semantically duplicate Tachi options that map to the same site
  value may be collapsed to the first label; never invent a fake site value merely to make the
  IDs unique. Preserve the recovered default explicitly. A Tachi Filter.Sort must become an
  Aidoku sort filter and Rust must handle FilterValue::Sort with id, index, and ascending; do not
  flatten it into a select filter. source_ir.filter_specs, when present, is the authoritative
  type/options/site-values/default contract.
- If the input fetches filter options at runtime, implement `DynamicFilters` rather than replacing
  them with a single static placeholder. Its exact method is
  `fn get_dynamic_filters(&self) -> Result<Vec<Filter>>`. Keep the same filter ID in the Rust
  query mapping and register `DynamicFilters` in `register_source!`. Every dynamic filter ID
  constructed by `get_dynamic_filters` must also be read from `FilterValue` by
  `get_search_manga_list`, and its selected site value must be sent in the actual list/search
  request. A filter that appears in the UI but does not alter a request is unimplemented.
  `Filter` does not implement `Deserialize`: never build dynamic filters by passing JSON to
  `serde_json::from_str`. Build typed filters directly, for example
  `SelectFilter { id: "theme".into(), title: Some("Theme".into()), options, ids: Some(ids),
  ..Default::default() }.into()`, and return them in a `Vec<Filter>`. Both `options` and `ids`
  must be `Vec<Cow<'static, str>>`; create their entries with `.into()` rather than collecting
  `String` values.
  Fetch dynamic options with the narrowest request the input contract permits. For GraphQL, query
  only the already-recovered option field (for example `allCategory { id name }`); never call a
  full manga-list helper from `get_dynamic_filters` or download manga entries merely to populate
  filter labels. Remove the dynamic-option field from normal list/search projections afterward.
- Keep the input source's page size and pagination semantics. For GraphQL list/search operations,
  request only fields needed to construct list entries; defer detail-only fields such as a long
  `description` to the detail query. Reusing one oversized GraphQL fragment for list, detail, and
  chapter operations is not behavior preservation when narrower projections return the same
  user-visible result.
- If the Tachi source declares filters or settings, the corresponding resource must be non-empty
  and preserve every user-visible option; an empty `[]` is an unimplemented capability.
- Aidoku settings must be top-level `group` objects whose `items` contain the actual settings.
  Select settings use parallel `titles` and `values` string arrays, not an `options` array of
  objects. Preserve the Tachi preference key and ensure its default is one of `values`.
- Read source settings with `aidoku::imports::defaults::defaults_get::<T>(key)`; never call the
  private FFI function `defaults::get`.
- Every key emitted in `res/settings.json` must be read with `defaults_get` and applied where the
  input reads that preference. A setting resource with no corresponding Rust behavior is not a
  completed conversion. Preserve bounded validation and fallbacks from the input; if the pinned
  Aidoku runtime cannot reproduce a preference (for example transport-wide rate limiting), do
  not silently expose a no-op setting—report that limitation explicitly.
- The pinned runtime supports transport-wide rate limiting through
  `aidoku::imports::net::set_rate_limit(permits, period, TimeUnit::Seconds)`. When the Kotlin
  client reads a `permits/seconds` preference before calling `rateLimit`, parse the same bounded
  positive integers in `Source::new`, fall back to the recovered default on malformed input, and
  call `set_rate_limit`. Multi-select preferences are read as
  `defaults_get::<Vec<String>>(key)` and should fall back to the recovered selected-value array.
- Preserve compatibility branches for legacy preference values found in the Tachi source. It is
  acceptable to normalize them while reading the setting when Aidoku has no reason to persist the
  obsolete value back to defaults.
- Convert image Referer or other required headers into `ImageRequestProvider`. If a header depends
  on the originating manga/chapter/page URL, construct page content with
  `PageContent::url_context(image_url, context)` and store the exact Referer in that context; a
  provider branch that reads context is incomplete when every page uses `PageContent::url`.
  Cover images have no page context, so preserve the input's site/base Referer as their fallback.
- If public requests inspect or refresh an existing cookie-jar session, expose an optional text
  setting for a user-supplied Cookie value when the pinned Aidoku API cannot inspect the jar.
  Apply a non-empty value as the `Cookie` header to both API and image requests. This represents
  the input's existing session behavior; it must not add a login flow, embed credentials, or make
  authenticated-only features part of a public-only conversion.
- Apply image URL translations only within their recovered scope. When
  source_ir.image_url_policy.preserve_cover_urls is true, copy API cover URLs to Manga.cover
  unchanged. A recovered chapter resolution pattern such as
  \d+(?=x\.(?:jpg|webp)$) may only replace the numeric segment immediately before a terminal
  x.jpg or x.webp in chapter page URLs. It must not rewrite a cover suffix such as
  .328x422.jpg.
- Add `DeepLinkHandler` when stable manga/chapter paths can be mapped without a network request,
  and register every optional trait in `register_source!`. Returned deep-link keys must use the
  exact same ID/path/absolute-URL strategy as normal list and chapter parsing; do not return a bare
  ID when normal keys contain `/comics/` or `/chapters/` paths.
- `DeepLinkHandler` uses
  `fn handle_deep_link(&self, url: String) -> Result<Option<DeepLinkResult>>`.
  Return `DeepLinkResult::Manga { key }` or
  `DeepLinkResult::Chapter { manga_key, key }` using normal source keys.
- `ListingProvider` is optional but, when used, its method is
  `fn get_manga_list(&self, listing: Listing, page: i32) -> Result<MangaPageResult>`; pass the
  supplied `Listing` (`id`, `name`, `kind`) to the site's listing endpoint rather than inventing a
  different signature.
- `ImageRequestProvider` uses
  `fn get_image_request(&self, url: String, context: Option<PageContext>) -> Result<Request>`.
- Current `Source::get_manga_update` supplies independent `needs_details` and `needs_chapters`
  flags. For GraphQL sources, provide details-only and chapters-only projections and retain a
  combined projection when both are true. Do not always download both payloads after checking only
  that at least one flag is true.
- For REST sources whose chapter traversal needs group or collection metadata from the same detail
  endpoint used by `needs_details`, fetch and decode that detail response at most once per
  `get_manga_update` call. When both flags are true, pass the decoded detail value into the chapter
  helper instead of letting the helper repeat the identical detail request. A chapters-only call
  may still fetch the detail endpoint once when it is required to discover chapter groups.
- The only valid optional trait names are `ListingProvider`, `Home`, `DynamicListings`,
  `DynamicFilters`, `DynamicSettings`, `PageImageProcessor`, `ImageRequestProvider`,
  `PageDescriptionProvider`, `AlternateCoverProvider`, `BaseUrlProvider`, `NotificationHandler`,
  `DeepLinkHandler`, `BasicLoginHandler`, `WebLoginHandler`, and `MigrationHandler`. Never put
  `Source`, `MangaProvider`, `PageProvider`, or any invented trait in `register_source!`.
- This is a `no_std` crate. Import collections from `aidoku::alloc::{String, Vec}` (and
  `aidoku::alloc::string::ToString` when needed), and deserialize with `aidoku::serde` if the
  dependency is not explicitly requested. `serde_json` is not re-exported by `aidoku`; request
  the allowlisted `serde_json` dependency when JSON parsing is needed. `Manga` uses `artists`, `authors`, `tags`, and
  `description`; it has no `genres` field. Optional text/attributes need to be unwrapped before
  assigning to non-optional fields, and `Chapter.date_uploaded` is `Option<i64>`. `Manga.tags`,
  `Manga.authors`, and `Manga.chapters` are all `Option`; build a local `Vec` and assign
  `Some(values)` rather than calling `.push()` on the option. Import the `vec!` macro explicitly
  (`use aidoku::alloc::vec;`) when using it in a `no_std` source.
- When borrowing a path from `manga.key` and later replacing or mutating the Manga, clone the path
  into an owned String first. Do not keep a `&str` borrowed from `manga.key` across assignments to
  `manga.key` or replacement of the Manga; Rust will reject that with E0506.
- Prefer small modules when parsing and URL construction are substantial.
- Do not invent endpoints, selectors, fields, or cryptography. Preserve the input source's network
  behavior exactly and report anything that cannot be represented.
- The generated smoke test calls `get_search_manga_list(None, 1, Vec::new())`. Preserve the
  original source's no-keyword listing behavior for that call (including any default sort or
  popular/latest endpoint); do not silently turn it into an empty `/comics` request when the
  Tachi source uses a default listing sort.
- In this mapping, `query: None` represents Tachi's empty query string. Run the original
  `searchMangaRequest(page, "", filters)` behavior unless the input source itself routes an empty
  query elsewhere; do not substitute the popular endpoint merely because the Aidoku query is
  `None`.
- When popular/latest capabilities are present, the smoke test calls both corresponding
  `ListingProvider` ids and requires each to return entries; implementing the trait without
  functional listings will fail live validation.
- The validator runs `cargo clippy -- -D warnings`; keep generated Rust clippy-clean (prefer
  let-chain conditions and iterator/enumerate forms over needless nested `if` blocks or indexed
  byte loops).

The tool creates Cargo metadata, source metadata, the icon, and live smoke tests. Do not generate
those files. Only return allowed Rust files plus optional filters/settings JSON.
{{DEPENDENCY_POLICY}}
When using `#[derive(Deserialize)]`, request the allowlisted `serde` dependency as well (derive
expansion needs the external `serde` crate even if the import uses `aidoku::serde`). JSON parsing
additionally requires the explicit `serde_json` request and `serde_json::from_str`.
