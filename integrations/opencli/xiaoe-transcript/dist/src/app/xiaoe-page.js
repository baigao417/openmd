function isKnownXiaoeHost(hostname) {
    return hostname.endsWith(".xet.citv.cn")
        || hostname.endsWith(".xet.pomoho.com")
        || hostname === "xiaoeknow.com"
        || hostname.endsWith(".xiaoeknow.com")
        || hostname === "xiaoecloud.com"
        || hostname.endsWith(".xiaoecloud.com");
}
function hasXiaoeQueryHints(parsed) {
    return ["app_id", "product_id", "pro_id", "conduit_id"].some((key) => parsed.searchParams.has(key));
}
function isSupportedXiaoePath(pathname) {
    return /\/(?:p\/)?course\/video\//.test(pathname)
        || /\/(?:v4\/)?course\/alive\//.test(pathname);
}
export function detectXiaoePage(input) {
    if (!input.url) {
        return { supported: false, reason: "当前页面没有可用链接" };
    }
    try {
        const parsed = new URL(input.url);
        const isXiaoeHost = isKnownXiaoeHost(parsed.hostname);
        let resourceId = /[val]_[a-f0-9]+/.exec(parsed.href)?.[0];
        const encodedPage = /^\/content_page\/([A-Za-z0-9_-]+)\/?$/.exec(parsed.pathname);
        if (encodedPage) {
            try {
                const encoded = encodedPage[1].replace(/-/g, '+').replace(/_/g, '/');
                const data = JSON.parse(atob(encoded + '='.repeat((4 - encoded.length % 4) % 4)));
                if (/^[val]_[a-f0-9]+$/.test(data.resource_id || ''))
                    resourceId = data.resource_id;
            }
            catch { /* Invalid encoded links remain unsupported. */ }
        }
        const isSupportedPath = isSupportedXiaoePath(parsed.pathname) || (!!encodedPage && !!resourceId);
        if (!isSupportedPath || (!isXiaoeHost && !hasXiaoeQueryHints(parsed))) {
            return { supported: false, reason: "当前页面不是支持的小鹅通视频或直播回放页" };
        }
        return {
            supported: true,
            resourceId,
            title: input.title?.trim() || undefined
        };
    }
    catch {
        return { supported: false, reason: "当前页面链接无效" };
    }
}
