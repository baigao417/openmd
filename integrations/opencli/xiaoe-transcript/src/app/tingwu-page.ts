export interface TingwuPageInfo {
  supported: boolean;
  reason?: string;
  title?: string;
}

export function detectTingwuTranscriptPage(input: { url?: string; title?: string }): TingwuPageInfo {
  if (!input.url) {
    return { supported: false, reason: "当前页面没有可用链接" };
  }

  try {
    const parsed = new URL(input.url);
    const isTingwuHost = parsed.hostname === "tingwu.aliyun.com";
    const looksLikeTranscriptPath = /\/(?:doc\/)?transcripts\//.test(parsed.pathname);
    if (!isTingwuHost || !looksLikeTranscriptPath) {
      return { supported: false, reason: "当前页面不是支持的听悟逐字稿页面" };
    }
    return {
      supported: true,
      title: input.title?.trim() || undefined
    };
  } catch {
    return { supported: false, reason: "当前页面链接无效" };
  }
}
