import io.github.keiyoushi.gradle.api.ContentWarning

plugins {
    alias(kei.plugins.extension)
}

keiyoushi {
    name = "Simple Source"
    versionCode = 7
    contentWarning = ContentWarning.SAFE
    source {
        lang = "en"
        baseUrl = "https://example.com"
    }
}

