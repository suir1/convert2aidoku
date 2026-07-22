package eu.kanade.tachiyomi.extension.p000zh.copymanga;

import eu.kanade.tachiyomi.extension.p000zh.copymanga.api.ApiRepo;
import eu.kanade.tachiyomi.extension.p000zh.copymanga.api.dto.ContentResult;
import eu.kanade.tachiyomi.source.ConfigurableSource;
import eu.kanade.tachiyomi.source.online.HttpSource;

@Metadata(d1 = {"compiler noise"}, d2 = {"CopyManga"})
public final class CopyManga extends HttpSource implements ConfigurableSource {
    private final String baseUrl = PluginMetaData.BASE_URL;

    public Request searchMangaRequest(int page, String query, FilterList filters) {
        return GET(ApiRepo.INSTANCE.searchUrl(page));
    }

    protected MangasPage searchMangaParse(Response response) {
        return decodeFromString(response.body().string());
    }

    protected Request popularMangaRequest(int page) { return GET(ApiRepo.INSTANCE.comicListUrl(page)); }
    protected MangasPage popularMangaParse(Response response) { return decodeFromString(response.body().string()); }
    protected Request latestUpdatesRequest(int page) { return GET(ApiRepo.INSTANCE.newestPageUrl(page)); }
    protected MangasPage latestUpdatesParse(Response response) { return decodeFromString(response.body().string()); }
    protected Request mangaDetailsRequest(SManga manga) { return GET(ApiRepo.INSTANCE.comicDetailUrl(ApiRepo.INSTANCE.url2comicPath(manga.getUrl()))); }
    protected SManga mangaDetailsParse(Response response) { return decodeFromString(response.body().string()); }
    public Observable fetchChapterList(SManga manga) { return Observable.just(ApiRepo.INSTANCE.chapterListUrl(manga.getUrl())); }
    public Observable fetchPageList(SChapter chapter) { ContentResult result = decodeFromString("{}"); return result.pages(); }
    public FilterList getFilterList() { return new FilterList(); }
    public void resetThemeFilter() { ApiRepo.INSTANCE.tagList(); }
    public String getMangaUrl(SManga manga) { return manga.getUrl(); }
    public String getChapterUrl(SChapter chapter) { return chapter.getUrl(); }
    public void setupPreferenceScreen(PreferenceScreen screen) { screen.addPreference(ApiDomainOption.KEY_CUSTOM); }

    private void optionalFeatures() {
        TokenProvider.login();
        ApiRepo.INSTANCE.memberCollectUrl(1);
        ApiRepo.INSTANCE.chapterCommentUrl("id");
    }
}
