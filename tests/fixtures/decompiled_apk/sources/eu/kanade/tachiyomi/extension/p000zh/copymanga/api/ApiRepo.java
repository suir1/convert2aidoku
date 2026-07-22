package eu.kanade.tachiyomi.extension.p000zh.copymanga.api;

public final class ApiRepo {
    public static final ApiRepo INSTANCE = new ApiRepo();
    private static final Headers COPY_MANGA_HEADER = Headers.Companion.of(new String[]{
        "Accept", "application/json", "Origin", "https://2025copy.com", "Version", "2025.11.21"
    });
    private String getApiUrl() { return getApiDomain() + "/api/v3"; }
    public String searchUrl(int page) { return getApiUrl() + "/search/comic?limit=21"; }
    public String comicListUrl(int page) { return getApiUrl() + "/comics?limit=21"; }
    public String newestPageUrl(int page) { return getApiUrl() + "/update/newest?limit=21"; }
    public String tagList() { return getApiUrl() + "/theme/comic/count?limit=100"; }
    public String comicDetailUrl(String path) { return getApiUrl() + "/comic2/" + path; }
    public String chapterListUrl(String path) { return getApiUrl() + "/comic/" + path + "/chapters"; }
    public String chapterContentDetailUrl(String chapterId) { return getApiUrl() + "/comic/" + chapterId; }
    public String fixChapterId(String chapterId) {
        String normalized = removePrefix(chapterId, "/comic/");
        return ApiDomainOption.INSTANCE.isHotManga(getApiDomain())
            ? normalized
            : replace(normalized, "/chapter/", "/chapter2/");
    }
    public String memberCollectUrl(int page) { return getApiUrl() + "/member/collect/comics"; }
    public String chapterCommentUrl(String id) { return getApiUrl() + "/roasts?chapter_id=" + id; }
    public String url2comicPath(String url) { return url.substring(url.indexOf("/comic/") + 7); }
}
