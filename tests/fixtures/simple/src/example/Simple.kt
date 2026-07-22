package example

import eu.kanade.tachiyomi.network.GET
import eu.kanade.tachiyomi.source.model.FilterList
import eu.kanade.tachiyomi.source.model.MangasPage
import eu.kanade.tachiyomi.source.model.Page
import eu.kanade.tachiyomi.source.model.SChapter
import eu.kanade.tachiyomi.source.model.SManga
import eu.kanade.tachiyomi.source.online.HttpSource
import okhttp3.Request
import okhttp3.Response

abstract class Simple : HttpSource() {
    override val supportsLatest = true

    override fun headersBuilder() = super.headersBuilder()
        .add("Referer", "$baseUrl/")

    override fun popularMangaRequest(page: Int): Request = GET("$baseUrl/popular/$page")
    override fun popularMangaParse(response: Response): MangasPage = TODO()
    override fun latestUpdatesRequest(page: Int): Request = GET("$baseUrl/latest/$page")
    override fun latestUpdatesParse(response: Response): MangasPage = TODO()
    override fun searchMangaRequest(page: Int, query: String, filters: FilterList): Request = TODO()
    override fun searchMangaParse(response: Response): MangasPage = TODO()
    override fun mangaDetailsParse(response: Response): SManga = TODO()
    override fun chapterListParse(response: Response): List<SChapter> = TODO()
    override fun pageListParse(response: Response): List<Page> = TODO()
    override fun imageUrlParse(response: Response): String = TODO()
}

