package eu.kanade.tachiyomi.extension.p000zh.copymanga;

public final class SortFilter extends Filter.Sort {
    public SortFilter() {
        Tag[] sortFilter = FilterKt.getSortFilter();
        super("排序方式", names, new Filter.Sort.Selection(1, true));
    }
}
