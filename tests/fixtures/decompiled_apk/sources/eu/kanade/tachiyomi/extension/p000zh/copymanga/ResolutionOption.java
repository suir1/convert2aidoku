package eu.kanade.tachiyomi.extension.p000zh.copymanga;

public final class ResolutionOption {
    private static final Regex CHAPTER_IMAGE_RESOLUTION_REGEX;

    static {
        CHAPTER_IMAGE_RESOLUTION_REGEX = new Regex("\\d+(?=x\\.(?:jpg|webp)$)");
    }
}
