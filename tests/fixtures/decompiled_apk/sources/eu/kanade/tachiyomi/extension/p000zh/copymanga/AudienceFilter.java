package eu.kanade.tachiyomi.extension.p000zh.copymanga;

public final class AudienceFilter extends Filter.Select<String> {
    public AudienceFilter() {
        Tag[] audienceFilter = FilterKt.getAudienceFilter();
        super("受众类型 (仅限拷贝)", names, 0, 4, null);
    }
}
