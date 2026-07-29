from convert2aidoku.rust_inspection import RustInspection


def test_indexes_functions_calls_headers_and_routes_across_files() -> None:
    inspection = RustInspection(
        [
            (
                "fn request(&self) { Request::get(url); }\n"
                "fn fetch(&self) { self.request(); self.decode(); }\n"
            ),
            (
                'fn decode(&self) { let path = "/comics/{id}"; }\n'
                'fn get_image_request(&self) { request.header("Referer", base); }\n'
            ),
        ]
    )

    assert inspection.has_function("fetch")
    assert inspection.function_contains("request", "Request::get")
    assert inspection.function_has_header("get_image_request", "referer")
    assert inspection.calls("fetch") >= {"request", "decode"}
    assert inspection.route_literals("decode") == {"/comics/{id}"}
    assert inspection.reachable_functions("fetch") == {"fetch", "request", "decode"}


def test_function_parameter_names_ignore_fully_qualified_types() -> None:
    inspection = RustInspection.from_content(
        "fn get_manga_list(&self, listing: aidoku::Listing, page: i32) {}"
    )

    assert inspection.named("get_manga_list")[0].parameter_names == ("listing", "page")


def test_request_routes_exclude_non_network_path_manipulation() -> None:
    inspection = RustInspection.from_content(
        """
        fn update(&self, id: &str) {
            let key = id.strip_prefix("/comic/");
            self.get(format!("/comic2/{id}"));
            self.send_get_retry("/api/detail/");
        }
        """
    )

    assert inspection.route_literals("update") == {
        "/comic/",
        "/comic2/{id}",
        "/api/detail/",
    }
    assert inspection.request_route_literals("update") == {
        "/comic2/{id}",
        "/api/detail/",
    }


def test_walks_error_tolerant_tree_and_compacts_comments() -> None:
    inspection = RustInspection.from_content(
        "fn fetch() { Request /* keep tokens separate */ :: get(url); }"
    )

    function = inspection.named("fetch")[0]

    assert any(node.type == "function_item" for node in inspection.nodes())
    assert RustInspection.compact_node(function.node) == "fnfetch(){Request::get(url);}"


def test_indexes_struct_field_types() -> None:
    inspection = RustInspection.from_content(
        """
        struct ComicDetailResult {
            groups: BTreeMap<String, GroupInfo>,
        }
        struct GroupInfo { name: String }
        """
    )

    assert inspection.struct_field_type("ComicDetailResult", "groups") == (
        "BTreeMap<String, GroupInfo>"
    )
    assert inspection.struct_field_type("GroupInfo", "name") == "String"
    assert inspection.struct_field_type("ComicDetailResult", "missing") is None


def test_indexes_struct_serialized_field_names() -> None:
    inspection = RustInspection.from_content(
        """
        struct ThemeResult {
            #[serde(rename = "themeList")]
            theme_list: Vec<ThemeDetail>,
        }
        """
    )

    field = inspection.struct_field("ThemeResult", "theme_list")

    assert field is not None
    assert field.serialized_name == "themeList"
