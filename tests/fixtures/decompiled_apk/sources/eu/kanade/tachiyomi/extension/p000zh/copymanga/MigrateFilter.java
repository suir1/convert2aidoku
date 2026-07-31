package eu.kanade.tachiyomi.extension.p000zh.copymanga;

import eu.kanade.tachiyomi.source.model.Filter;

public final class MigrateFilter extends Filter.Select<String> {
    public MigrateFilter() {
        Tag[] migrateFilter = FilterKt.getMigrateFilter();
        super("拷贝/热辣源站书柜 (需登入)", migrateFilter, 0, 4, null);
    }
}
