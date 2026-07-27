from __future__ import annotations

from dataclasses import dataclass

from .models import Capability, SourceIR


@dataclass(frozen=True)
class LiveValidationEvidence:
    repair_context: str = ""
    setting_defaults: tuple[tuple[str, str], ...] = ()

    @property
    def setting_overrides(self) -> dict[str, str] | None:
        defaults = dict(self.setting_defaults)
        return defaults or None


_EVIDENCE = {
    "zh.mycomic": LiveValidationEvidence(
        repair_context=(
            "Independent browser-only benchmark evidence (not proof of runner connectivity): "
            "GET /comics?sort=-views returned manga entries; /comics/54348 returned details "
            "and chapters; /chapters/794527 returned 30 pages. The CLI/test runner may still "
            "receive HTTP 403 because it does not share the browser's network/session. "
            "Preserve relative keys, but make every request URL absolute."
        )
    ),
    "zh.copymanga": LiveValidationEvidence(
        repair_context=(
            "Independent 2026-07-27 public API reachability evidence from the Tachi input only: "
            "the required CopyManga headers are Accept: application/json, Origin: "
            "https://2025copy.com, Version: 2025.11.21, Region: 0, Webp: 0, platform: 1, and a "
            "browser User-Agent. GET api.mangacopy.com/api/v3/comic2/<path> and the dynamic "
            "theme endpoint returned API code 200, while mapi.copy20.com timed out on this "
            "network. A stored platform.one header returned text/html 'error'; translating it "
            "to protocol value 1 returned JSON. Keep "
            "the finite input allowlist, but prefer a currently reachable public domain as the "
            "default. Official AidokuRunner differential evidence for generated v83 loaded all "
            "seven filters across the Swift/Postcard boundary. Region, sort, and dynamic theme "
            "changed manga keys, but rank=day and audience=female produced Manga values with "
            "empty titles and key '/comic/' because /ranks returns RankResult.list of ListItem "
            "{ comic }, not direct Comic entries. The free_type filter is marked HotManga-only "
            "by the input and is expected not to change results on the default CopyManga "
            "domain. The /comic2/<path> detail endpoint is also wrapped as "
            "ApiResponse<ComicDetailResult>: deserialize the outer ApiResponse<DetailResult> "
            "and use .results before reading .comic or .groups. Deserializing the HTTP response "
            "directly into DetailResult silently produces default empty fields. Official "
            "AidokuRunner evidence for clean4 loaded the dynamic theme UI, but the filter had no "
            "effect because get_search_manga_list never read FilterValue id 'theme'. Read that "
            "same id and append &theme=<selected path_word> to the /comics request; a visible "
            "filter that does not change its request is incomplete."
        ),
        setting_defaults=(
            ("v2.pref.api_domain", "api.mangacopy.com"),
            ("api_domain", "api.mangacopy.com"),
        ),
    ),
}
_EMPTY_EVIDENCE = LiveValidationEvidence()


def live_validation_evidence(ir: SourceIR) -> LiveValidationEvidence:
    evidence = _EVIDENCE.get(ir.metadata.source_id, _EMPTY_EVIDENCE)
    if Capability.DYNAMIC_BASE_URLS in ir.capabilities or not evidence.setting_defaults:
        return evidence
    return LiveValidationEvidence(repair_context=evidence.repair_context)
