package example

import eu.kanade.tachiyomi.source.online.HttpSource
import java.security.spec.AlgorithmParameterSpec
import javax.crypto.Cipher
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec

abstract class EncryptedApi : HttpSource() {
    override val baseUrl = "https://api.example.com"

    private val API_DOMAINS = arrayOf("api.example.com", "api2.example.com")

    override fun searchMangaParse(response: Response): MangasPage =
        response.parseAs<ApiResponse>().toMangasPage()

    override fun mangaDetailsParse(response: Response): SManga =
        response.parseAs<ApiResponse>().toManga()

    override fun chapterListParse(response: Response): List<SChapter> =
        response.parseAs<ApiResponse>().toChapters()

    override fun pageListParse(response: Response): List<Page> =
        decrypt(response.body.string()).parseAs<ApiResponse>().toPages()

    private fun decrypt(value: String): String {
        val iv: AlgorithmParameterSpec = IvParameterSpec(value.take(16).toByteArray())
        val key = SecretKeySpec("0123456789abcdef".toByteArray(), "AES")
        return Cipher.getInstance("AES/CBC/PKCS5Padding").run {
            init(Cipher.DECRYPT_MODE, key, iv)
            doFinal(value.drop(16).decodeHex()).decodeToString()
        }
    }
}
