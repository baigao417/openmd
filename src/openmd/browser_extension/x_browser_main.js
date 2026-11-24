// This function is serialized by chrome.scripting.executeScript into X's MAIN
// world. Keep all authentication material inside the page and return content only.
export async function openmdXReadPage(tweetId) {
  const fail = (error_code, next_action) => ({ ok: false, error_code, next_action });
  const id = String(tweetId || "");
  if (!/^\d+$/.test(id)) return fail("x_browser_invalid_input", "Pass a public X status URL and retry.");

  const hasAccountMenu = Boolean(document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]'));
  const csrf = document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith("ct0="))?.slice(4) || "";
  if (!hasAccountMenu || !csrf) return fail("x_browser_auth_required", "Sign in to X in Chrome, then retry.");

  const timeoutFetch = async (url, options, timeoutMs) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try { return await fetch(url, { ...options, signal: controller.signal }); }
    finally { clearTimeout(timer); }
  };

  try {
    const scriptUrl = [...document.scripts].map((script) => script.src).find((value) => {
      try { return /\/main\.[^/]+\.js$/.test(new URL(value).pathname); } catch { return false; }
    });
    if (!scriptUrl) return fail("x_browser_bundle_missing", "Reload the X page and retry; Openmd will continue to the next browser route.");
    const bundleResponse = await timeoutFetch(scriptUrl, { credentials: "same-origin", cache: "force-cache" }, 2500);
    if (!bundleResponse.ok) return fail("x_browser_bundle_unavailable", "Reload the X page and retry; Openmd will continue to the next browser route.");
    const bundle = await bundleResponse.text();

    let operationAt = -1;
    let cursor = 0;
    while ((cursor = bundle.indexOf("operationName", cursor)) >= 0) {
      if (bundle.slice(cursor, cursor + 100).includes("TweetDetail")) { operationAt = cursor; break; }
      cursor += 12;
    }
    if (operationAt < 0) return fail("x_browser_query_id_missing", "X changed its page API; retry later or use the next browser route.");
    const queryIdAt = bundle.lastIndexOf("queryId:", operationAt);
    const queryIdEnd = queryIdAt < 0 ? -1 : bundle.indexOf(",", queryIdAt);
    const queryId = queryIdAt < 0 || queryIdEnd < 0
      ? ""
      : bundle.slice(queryIdAt + "queryId:".length, queryIdEnd).replace(/[^A-Za-z0-9_-]/g, "");
    const featureStart = bundle.indexOf("featureSwitches:[", operationAt);
    const featureEnd = featureStart < 0 ? -1 : bundle.indexOf("]", featureStart);
    if (!queryId || featureStart < 0 || featureEnd < 0) {
      return fail("x_browser_features_missing", "X changed its page API; retry later or use the next browser route.");
    }
    const featureKeys = bundle.slice(featureStart + "featureSwitches:[".length, featureEnd)
      .split(",").map((value) => value.replace(/[^A-Za-z0-9_]/g, "")).filter(Boolean);
    const noteConsumption = featureKeys.find((key) => key.startsWith("longform_notetweets_") && key.includes("consumption"));
    const richTextRead = featureKeys.find((key) => key.startsWith("longform_notetweets_") && key.includes("rich_text_read"));
    const articleConsumption = featureKeys.find((key) => key.includes("twitter_article") && key.includes("tweet_consumption"));
    if (!noteConsumption || !richTextRead || !articleConsumption) {
      return fail("x_browser_features_missing", "X changed its page API; retry later or use the next browser route.");
    }

    const bearerMatches = [...bundle.matchAll(/Bearer\s+([A-Za-z0-9%._~-]+)/g)].map((match) => match[1]);
    const bearer = [...new Set(bearerMatches)][0] || "";
    if (!bearer) return fail("x_browser_auth_required", "Reload X while signed in, then retry.");

    const variables = {
      focalTweetId: id,
      with_rux_injections: false,
      rankingMode: "Relevance",
      includePromotedContent: true,
      withCommunity: true,
      withQuickPromoteEligibilityTweetFields: true,
      withBirdwatchNotes: true,
      withVoice: true,
    };
    const features = { [noteConsumption]: true, [richTextRead]: true, [articleConsumption]: true };
    const params = new URLSearchParams({
      variables: JSON.stringify(variables),
      features: JSON.stringify(features),
      fieldToggles: "{}",
    });
    const response = await timeoutFetch(`/i/api/graphql/${queryId}/TweetDetail?${params}`, {
      credentials: "include",
      headers: {
        authorization: `Bearer ${bearer}`,
        "x-csrf-token": csrf,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": document.documentElement.lang || "en",
        accept: "application/json",
      },
    }, 5000);
    if (response.status === 401 || response.status === 403) return fail("x_browser_auth_required", "Sign in to X in Chrome, then retry.");
    if (response.status === 404) return fail("x_not_found", "Check that the public X post still exists.");
    if (response.status === 429) return fail("x_rate_limited", "Wait briefly, then retry once.");
    if (response.status >= 500) return fail("x_browser_api_unavailable", "Retry once; Openmd will continue to the next browser route.");
    if (!response.ok) return fail("x_browser_api_failed", "Retry once; Openmd will continue to the next browser route.");

    const payload = await response.json();
    if (Array.isArray(payload.errors) && payload.errors.length) {
      const codes = payload.errors.map((error) => String(error.code || ""));
      if (codes.includes("NOT_FOUND")) return fail("x_not_found", "Check that the public X post still exists.");
      return fail("x_browser_api_changed", "X changed its page API; retry later or use the next browser route.");
    }
    const stack = [payload.data];
    const visited = new Set();
    let target = null;
    while (stack.length && !target) {
      const value = stack.pop();
      if (!value || typeof value !== "object" || visited.has(value)) continue;
      visited.add(value);
      if (value.legacy?.id_str === id || value.rest_id === id) { target = value; break; }
      if (Array.isArray(value)) stack.push(...value);
      else stack.push(...Object.values(value));
    }
    if (!target) return fail("x_browser_post_unavailable", "Check that this public post is visible to your signed-in X account.");

    const legacy = target.legacy && typeof target.legacy === "object" ? target.legacy : {};
    const note = target.note_tweet?.note_tweet_results?.result?.text;
    const text = typeof note === "string" && note.trim() ? note : String(legacy.full_text || "");
    if (!text.trim()) return fail("x_browser_text_missing", "This post has no readable text; Openmd will continue to the next browser route.");
    const user = target.core?.user_results?.result?.legacy || target.user?.legacy || {};
    const quoteResult = target.quoted_status_result?.result;
    const quote = quoteResult?.note_tweet?.note_tweet_results?.result?.text || quoteResult?.legacy?.full_text || "";
    const media = legacy.extended_entities?.media || legacy.entities?.media || [];
    return {
      ok: true,
      text,
      legacy_text: String(legacy.full_text || ""),
      title: text.split(/\r?\n/, 1)[0].slice(0, 120),
      author: String(user.name || user.screen_name || ""),
      author_handle: user.screen_name ? `@${String(user.screen_name).replace(/^@/, "")}` : "",
      created_at: String(legacy.created_at || ""),
      images: media.map((entry) => String(entry.media_url_https || "")).filter(Boolean),
      quote_text: String(quote),
    };
  } catch (error) {
    return fail(error?.name === "AbortError" ? "x_browser_timeout" : "x_browser_api_failed",
      error?.name === "AbortError" ? "The X page request exceeded its short time limit; Openmd will continue to the next browser route." : "Retry once; Openmd will continue to the next browser route.");
  }
}
