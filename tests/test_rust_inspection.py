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


def test_walks_error_tolerant_tree_and_compacts_comments() -> None:
    inspection = RustInspection.from_content(
        "fn fetch() { Request /* keep tokens separate */ :: get(url); }"
    )

    function = inspection.named("fetch")[0]

    assert any(node.type == "function_item" for node in inspection.nodes())
    assert RustInspection.compact_node(function.node) == "fnfetch(){Request::get(url);}"
