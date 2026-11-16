import { OpenCliPage } from "../opencli/client.js";
const GET_SUBJECT_HOME_URL = "https://www.biji.com/subject";
const GET_TOPIC_MANAGER_URL = "https://knowledge-api.trytalks.com/v1/web/topic/list/manager?is_selection=0&page=1&size=50";
const NETWORK_INTERCEPTOR_JS = `(function(){if(window.__opencli_net)return;window.__opencli_net=[];var M=300,B=2500000,F=window.fetch;window.fetch=async function(){var r=await F.apply(this,arguments);try{var ct=r.headers.get('content-type')||'';if(ct.includes('json')||ct.includes('text/event-stream')||ct.includes('text')){var c=r.clone(),t=await c.text();if(window.__opencli_net.length<M){var b=t;if(ct.includes('application/json')&&t.length<=B){try{b=JSON.parse(t)}catch(e){}}window.__opencli_net.push({url:r.url||(arguments[0]&&arguments[0].url)||String(arguments[0]),method:(arguments[1]&&arguments[1].method)||'GET',status:r.status,ct:ct,body:b,size:t.length})}}}catch(e){}return r};var X=XMLHttpRequest.prototype,O=X.open,S=X.send;X.open=function(m,u){this._om=m;this._ou=u;return O.apply(this,arguments)};X.send=function(){var x=this;x.addEventListener('load',function(){try{var ct=x.getResponseHeader('content-type')||'';if((ct.includes('json')||ct.includes('text/event-stream')||ct.includes('text'))&&window.__opencli_net.length<M){var t=x.responseText,b=t;if(ct.includes('application/json')&&t.length<=B){try{b=JSON.parse(t)}catch(e){}}window.__opencli_net.push({url:x._ou,method:x._om||'GET',status:x.status,ct:ct,body:b,size:t?t.length:0})}}catch(e){}});return S.apply(this,arguments)}})()`;
const ROWS_READY_SCRIPT = `(() => document.querySelectorAll('tbody tr').length > 0)()`;
const SUBJECT_CARD_READY_SCRIPT = `(() => document.querySelectorAll('.subject-card').length > 0)()`;
function createDefaultDependencies() {
    return {
        createPage: () => new OpenCliPage(`get-subscription:${Date.now()}`)
    };
}
function normalizeText(value) {
    return typeof value === "string" ? value.trim() : "";
}
function normalizeIsoDate(value) {
    if (!value) {
        return undefined;
    }
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString();
}
export function normalizeGetSubscriptionList(payload) {
    const list = payload && typeof payload === "object" && "c" in payload
        ? payload.c?.list
        : undefined;
    if (!Array.isArray(list)) {
        return [];
    }
    const subscriptions = [];
    for (const item of list) {
        const creatorId = normalizeText(item.id_alias);
        const creatorName = normalizeText(item.name);
        if (!creatorId || !creatorName) {
            continue;
        }
        subscriptions.push({
            creatorId,
            creatorName,
            subjectUrl: `${GET_SUBJECT_HOME_URL}/${creatorId}/DEFAULT`,
            statsInfo: normalizeText(item.extend_data?.stats_info) || undefined,
            rootDirectoryId: typeof item.root_dir?.id === "number" ? item.root_dir.id : undefined
        });
    }
    return subscriptions;
}
export function inferCreatorNameFromSubjectUrl(subjectUrl) {
    if (!subjectUrl) {
        return undefined;
    }
    try {
        const parsed = new URL(subjectUrl);
        const followName = parsed.searchParams.get("followName")?.trim();
        return followName ? decodeURIComponent(followName) : undefined;
    }
    catch {
        return undefined;
    }
}
export function parseGetTopicTimestamp(rawValue, now = new Date()) {
    const match = rawValue.match(/^(\d{2})月(\d{2})日\s+(\d{2}):(\d{2})$/);
    if (!match) {
        return undefined;
    }
    const month = Number.parseInt(match[1], 10);
    const day = Number.parseInt(match[2], 10);
    const hours = Number.parseInt(match[3], 10);
    const minutes = Number.parseInt(match[4], 10);
    const baseYear = now.getUTCFullYear();
    const candidate = new Date(Date.UTC(baseYear, month - 1, day, hours, minutes, 0, 0));
    if (candidate.getTime() - now.getTime() > 24 * 60 * 60 * 1000) {
        candidate.setUTCFullYear(baseYear - 1);
    }
    return candidate;
}
export function pickTranscriptTextFromDetail(payload, domFallback) {
    const detail = payload && typeof payload === "object" && "c" in payload
        ? payload.c
        : undefined;
    const mediaText = normalizeText(detail?.post_media_text);
    if (mediaText) {
        return { content: mediaText, contentSource: "media_text" };
    }
    const summary = normalizeText(detail?.post_summary);
    if (summary) {
        return { content: summary, contentSource: "summary" };
    }
    const cleanedSummary = normalizeText(detail?.post_cleaned_summary).replace(/<br\s*\/?>/gi, "\n");
    if (cleanedSummary) {
        return { content: cleanedSummary, contentSource: "cleaned_summary" };
    }
    return {
        content: domFallback.trim(),
        contentSource: "dom"
    };
}
export function extractTranscriptTextFromWebPage(input) {
    const paragraphs = input.paragraphs
        .map((item) => item.trim())
        .filter(Boolean);
    return {
        content: paragraphs.join("\n\n"),
        contentSource: "linked_web",
        sourceUrl: input.sourceUrl?.trim() || undefined
    };
}
async function waitForCondition(page, script, timeoutMs, intervalMs = 200) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        const done = await page.evaluate(script).catch(() => false);
        if (done) {
            return;
        }
        await page.evaluate(`new Promise(resolve => setTimeout(resolve, ${intervalMs}))`).catch(() => undefined);
    }
    throw new Error("Get page did not reach the expected state before timeout");
}
async function fetchTopicManagerPayload(page) {
    return await page.evaluate(`(async () => {
    const response = await fetch(${JSON.stringify(GET_TOPIC_MANAGER_URL)}, {
      credentials: "include"
    });
    return await response.json();
  })()`);
}
async function readSubjectCards(page) {
    return await page.evaluate(`(() => Array.from(document.querySelectorAll('.subject-card')).map((card) => {
    const name = (card.querySelector('.subject-title')?.textContent || "").trim();
    const statsInfo = (card.querySelector('.subject-meta')?.textContent || "").trim();
    return {
      creatorName: name,
      statsInfo
    };
  }))()`);
}
async function readSubjectRows(page) {
    return await page.evaluate(`(() => Array.from(document.querySelectorAll('tbody tr')).map((row) => {
    const cells = Array.from(row.querySelectorAll('td'));
    return {
      title: (cells[0]?.innerText || "").trim(),
      noteType: (cells[2]?.innerText || "").trim(),
      createdAtText: (cells[3]?.innerText || "").trim()
    };
  }))()`);
}
async function readFollowRows(page) {
    return await page.evaluate(`(() => Array.from(document.querySelectorAll('tbody tr')).map((row) => {
    const cells = Array.from(row.querySelectorAll('td'));
    return {
      name: (cells[0]?.innerText || "").trim(),
      status: (cells[1]?.innerText || "").trim(),
      type: (cells[2]?.innerText || "").trim()
    };
  }))()`);
}
async function readPaginationState(page) {
    return await page.evaluate(`(() => {
    const active = Number.parseInt(document.querySelector('.n-pagination-item--active')?.textContent?.trim() || "1", 10);
    const buttons = Array.from(document.querySelectorAll('.n-pagination-item--button'));
    const nextButton = buttons[buttons.length - 1];
    return {
      activePage: Number.isFinite(active) ? active : 1,
      hasNext: nextButton instanceof HTMLElement && !nextButton.className.includes('n-pagination-item--disabled')
    };
  })()`);
}
async function openFollowTab(page) {
    await waitForCondition(page, `(() => !!document.querySelector('[data-name="follow"]'))()`, 10_000);
    const clicked = await page.evaluate(`(() => {
    const tab = document.querySelector('[data-name="follow"]');
    if (tab instanceof HTMLElement) {
      tab.click();
      return true;
    }
    return false;
  })()`);
    if (!clicked) {
        throw new Error("Unable to open the Get follow tab");
    }
    await waitForCondition(page, `(() => location.pathname.includes('/subject/') && location.search.includes('page=') && document.querySelectorAll('tbody tr').length > 0)()`, 15_000);
}
async function clickFollowRow(page, rowIndex) {
    const clicked = await page.evaluate(`((index) => {
    const row = document.querySelectorAll('tbody tr')[index];
    if (row instanceof HTMLElement) {
      row.click();
      return true;
    }
    return false;
  })(${rowIndex})`);
    if (!clicked) {
        throw new Error(`Unable to open Get follow row ${rowIndex}`);
    }
}
async function waitForExportableSubjectPage(page) {
    await waitForCondition(page, `(() => location.pathname.includes('/subject/') && location.search.includes('followId=') && document.querySelectorAll('tbody tr').length > 0)()`, 15_000);
}
async function clickSubjectCard(page, creatorName) {
    const clicked = await page.evaluate(`(() => {
    const target = Array.from(document.querySelectorAll('.subject-card')).find((card) => {
      const text = (card.textContent || "").trim();
      return text.includes(${JSON.stringify(creatorName)});
    });
    if (target instanceof HTMLElement) {
      target.click();
      return true;
    }
    return false;
  })()`);
    if (!clicked) {
        throw new Error(`Unable to open Get subscription card for ${creatorName}`);
    }
}
async function clickSubjectCardByIndex(page, cardIndex) {
    const clicked = await page.evaluate(`((index) => {
    const card = document.querySelectorAll('.subject-card')[index];
    if (card instanceof HTMLElement) {
      card.click();
      return true;
    }
    return false;
  })(${cardIndex})`);
    if (!clicked) {
        throw new Error(`Unable to open Get subscription card ${cardIndex}`);
    }
}
async function waitForSubjectDetail(page) {
    await waitForCondition(page, `(() => location.pathname.startsWith('/subject/') && document.title.includes('｜Get笔记'))()`, 15_000);
}
async function openSubscriptionSubject(page, input) {
    if (input.subjectUrl) {
        await page.goto(input.subjectUrl);
        try {
            await waitForCondition(page, ROWS_READY_SCRIPT, 5_000);
            return undefined;
        }
        catch {
            const fallbackCreatorId = input.subjectUrl.match(/\/subject\/([^/?]+)/)?.[1];
            if (!fallbackCreatorId) {
                throw new Error("Get subject URL did not expose exportable rows");
            }
            input = {
                creatorId: fallbackCreatorId,
                creatorName: input.creatorName
            };
        }
    }
    if (!input.creatorId) {
        throw new Error("Get export requires subjectUrl or creatorId");
    }
    await page.goto(GET_SUBJECT_HOME_URL);
    await waitForCondition(page, SUBJECT_CARD_READY_SCRIPT, 15_000);
    const cards = await readSubjectCards(page);
    const preferredIndexes = input.creatorName
        ? cards.map((card, index) => ({ card, index })).filter(({ card }) => card.creatorName === input.creatorName).map(({ index }) => index)
        : [];
    const allIndexes = [...preferredIndexes, ...cards.map((_, index) => index).filter((index) => !preferredIndexes.includes(index))];
    for (const cardIndex of allIndexes) {
        const card = cards[cardIndex];
        await clickSubjectCardByIndex(page, cardIndex);
        await waitForSubjectDetail(page);
        const currentUrl = await page.evaluate("location.href");
        const creatorId = currentUrl.match(/\/subject\/([^/?]+)/)?.[1];
        if (creatorId === input.creatorId) {
            await openFollowTab(page);
            const rows = await readFollowRows(page);
            const targetIndex = rows.findIndex((row) => row.name === card.creatorName);
            const rowIndex = targetIndex >= 0 ? targetIndex : 0;
            await clickFollowRow(page, rowIndex);
            await waitForExportableSubjectPage(page);
            return {
                creatorId,
                creatorName: card.creatorName,
                subjectUrl: await page.evaluate("location.href"),
                statsInfo: card.statsInfo
            };
        }
        await page.evaluate("history.back()");
        await waitForCondition(page, SUBJECT_CARD_READY_SCRIPT, 15_000);
    }
    throw new Error(`Get subscription ${input.creatorId} was not found`);
}
async function clickRow(page, rowIndex) {
    const clicked = await page.evaluate(`((index) => {
    const row = document.querySelectorAll('tbody tr')[index];
    if (row instanceof HTMLElement) {
      row.click();
      return true;
    }
    return false;
  })(${rowIndex})`);
    if (!clicked) {
        throw new Error(`Unable to click Get note row ${rowIndex}`);
    }
}
async function waitForPostPage(page) {
    await waitForCondition(page, `(() => location.pathname.includes('/post/'))()`, 15_000);
}
async function getPostDetailResponseCount(page) {
    return await page.evaluate(`(() => (window.__opencli_net || []).filter((entry) => String(entry.url || "").includes('/v1/web/topic/post/detail')).length)()`).catch(() => 0);
}
async function readLatestPostDetail(page) {
    return await page.evaluate(`(() => {
    const matches = (window.__opencli_net || []).filter((entry) => String(entry.url || "").includes('/v1/web/topic/post/detail'));
    return matches[matches.length - 1]?.body;
  })()`);
}
async function readPostDomFallback(page) {
    return await page.evaluate(`(() => ({
    title: (document.querySelector('.note-title')?.textContent || "").trim(),
    createdAtText: ((document.querySelector('.note-update-time')?.textContent || "").trim()).replace(/^创建于：/, "").trim(),
    content: (document.querySelector('.note-content')?.textContent || document.body.innerText || "").trim(),
    detailUrl: location.href
  }))()`);
}
async function waitForWebTranscriptPage(page) {
    await waitForCondition(page, `(() => location.pathname.endsWith('/web')
      && ((document.querySelector('.web-title')?.textContent || '').trim().length > 0)
      && (document.querySelectorAll('.web-html p').length > 0 || ((document.querySelector('.web-html')?.textContent || '').trim().length > 0)))()`, 15_000);
}
async function readWebTranscriptPage(page) {
    return await page.evaluate(`(() => ({
    title: (document.querySelector('.web-title')?.textContent || "").trim(),
    sourceUrl: (document.querySelector('.web-source-url span')?.textContent || "").trim() || undefined,
    paragraphs: (() => {
      const explicit = Array.from(document.querySelectorAll('.web-html p'))
        .map((node) => (node.textContent || "").trim())
        .filter(Boolean);
      if (explicit.length > 0) {
        return explicit;
      }
      const container = document.querySelector('.web-html');
      const title = (document.querySelector('.web-title')?.textContent || "").trim();
      const source = (document.querySelector('.web-source-url')?.textContent || "").trim();
      const raw = (container?.textContent || "").trim();
      const cleaned = raw
        .replace(title, "")
        .replace(source, "")
        .trim();
      return cleaned ? cleaned.split(/\\n{2,}/).map((item) => item.trim()).filter(Boolean) : [];
    })()
  }))()`);
}
async function waitForNewPostDetail(page, previousCount) {
    const deadline = Date.now() + 8_000;
    while (Date.now() < deadline) {
        const count = await getPostDetailResponseCount(page);
        if (count > previousCount) {
            return await readLatestPostDetail(page);
        }
        await page.evaluate("new Promise(resolve => setTimeout(resolve, 200))").catch(() => undefined);
    }
    return undefined;
}
async function goBackToSubject(page) {
    await page.evaluate("history.back()");
    await waitForCondition(page, ROWS_READY_SCRIPT, 15_000);
}
async function returnToSubjectPage(page) {
    for (let attempt = 0; attempt < 3; attempt += 1) {
        const ready = await page.evaluate(`(() => location.pathname.includes('/subject/') && document.querySelectorAll('tbody tr').length > 0)()`).catch(() => false);
        if (ready) {
            return;
        }
        await page.evaluate("history.back()").catch(() => undefined);
        try {
            await waitForCondition(page, `(() => location.pathname.includes('/subject/') && document.querySelectorAll('tbody tr').length > 0)()`, 6_000);
            return;
        }
        catch {
            // try another history step when still inside nested post/web pages
        }
    }
    throw new Error("Unable to return to the Get subject page");
}
async function goToNextPage(page, currentPage) {
    const clicked = await page.evaluate(`(() => {
    const buttons = Array.from(document.querySelectorAll('.n-pagination-item--button'));
    const nextButton = buttons[buttons.length - 1];
    if (nextButton instanceof HTMLElement && !nextButton.className.includes('n-pagination-item--disabled')) {
      nextButton.click();
      return true;
    }
    return false;
  })()`);
    if (!clicked) {
        throw new Error("Unable to navigate to the next Get subject page");
    }
    await waitForCondition(page, `(() => Number.parseInt(document.querySelector('.n-pagination-item--active')?.textContent?.trim() || "0", 10) === ${currentPage + 1})()`, 15_000);
}
export async function listGetSubscriptions(overrides) {
    const deps = {
        ...createDefaultDependencies(),
        ...overrides
    };
    const page = deps.createPage();
    try {
        await page.goto(GET_SUBJECT_HOME_URL);
        await waitForCondition(page, SUBJECT_CARD_READY_SCRIPT, 15_000);
        const cards = await readSubjectCards(page);
        const subscriptions = [];
        for (const [index, card] of cards.entries()) {
            await clickSubjectCardByIndex(page, index);
            await waitForSubjectDetail(page);
            const currentUrl = await page.evaluate("location.href");
            const creatorId = currentUrl.match(/\/subject\/([^/?]+)/)?.[1] || "";
            await openFollowTab(page);
            const followRows = await readFollowRows(page);
            const targetIndex = followRows.findIndex((row) => row.name === card.creatorName);
            const rowIndex = targetIndex >= 0 ? targetIndex : 0;
            await clickFollowRow(page, rowIndex);
            await waitForExportableSubjectPage(page);
            const exportableUrl = await page.evaluate("location.href");
            subscriptions.push({
                creatorId,
                creatorName: card.creatorName,
                statsInfo: card.statsInfo || undefined,
                subjectUrl: exportableUrl
            });
            await page.goto(GET_SUBJECT_HOME_URL);
            await waitForCondition(page, SUBJECT_CARD_READY_SCRIPT, 15_000);
        }
        return subscriptions;
    }
    finally {
        await page.closeWindow();
    }
}
export async function collectGetTranscriptNotes(input, overrides) {
    const deps = {
        ...createDefaultDependencies(),
        ...overrides
    };
    const page = deps.createPage();
    try {
        const openedSubscription = await openSubscriptionSubject(page, input);
        await page.evaluate(NETWORK_INTERCEPTOR_JS).catch(() => undefined);
        const subjectUrl = await page.evaluate("location.href");
        const creatorId = input.creatorId
            || openedSubscription?.creatorId
            || subjectUrl.match(/\/subject\/([^/?]+)/)?.[1]
            || "";
        const creatorName = openedSubscription?.creatorName
            || inferCreatorNameFromSubjectUrl(subjectUrl)
            || inferCreatorNameFromSubjectUrl(input.subjectUrl)
            || input.creatorName
            || await page.evaluate("(document.title || '').replace(/｜Get笔记$/, '').trim()");
        const startBoundary = input.startDate ? new Date(`${input.startDate}T00:00:00.000Z`) : undefined;
        const endBoundary = input.endDate ? new Date(`${input.endDate}T23:59:59.999Z`) : undefined;
        const items = [];
        let hasNext = true;
        while (hasNext) {
            const pageState = await readPaginationState(page);
            const rows = await readSubjectRows(page);
            let shouldStop = false;
            for (const [rowIndex, row] of rows.entries()) {
                const rowDate = parseGetTopicTimestamp(row.createdAtText);
                if (rowDate && endBoundary && rowDate > endBoundary) {
                    continue;
                }
                if (rowDate && startBoundary && rowDate < startBoundary) {
                    shouldStop = true;
                    break;
                }
                const previousDetailCount = await getPostDetailResponseCount(page);
                try {
                    await clickRow(page, rowIndex);
                    await waitForPostPage(page);
                    const detailPayload = await waitForNewPostDetail(page, previousDetailCount);
                    const domFallback = await readPostDomFallback(page);
                    let picked = pickTranscriptTextFromDetail(detailPayload, domFallback.content);
                    let sourceUrl;
                    try {
                        await page.goto(`${domFallback.detailUrl}/web`);
                        await waitForWebTranscriptPage(page);
                        const webTranscript = await readWebTranscriptPage(page);
                        const extracted = extractTranscriptTextFromWebPage(webTranscript);
                        if (extracted.content.trim()) {
                            picked = {
                                content: extracted.content,
                                contentSource: extracted.contentSource
                            };
                            sourceUrl = extracted.sourceUrl;
                        }
                    }
                    catch {
                        // Keep the summary/dom fallback when the linked web transcript page is unavailable.
                    }
                    const detailBody = detailPayload && typeof detailPayload === "object" && "c" in detailPayload
                        ? detailPayload.c
                        : undefined;
                    const unixSeconds = typeof detailBody?.post_create_time === "number"
                        ? detailBody.post_create_time
                        : typeof detailBody?.post_update_time === "number"
                            ? detailBody.post_update_time
                            : undefined;
                    const publishedAt = unixSeconds
                        ? new Date(unixSeconds * 1000).toISOString()
                        : normalizeIsoDate(parseGetTopicTimestamp(row.createdAtText)?.toISOString())
                            || normalizeIsoDate(parseGetTopicTimestamp(domFallback.createdAtText)?.toISOString());
                    items.push({
                        noteId: normalizeText(detailBody?.post_id_str) || domFallback.detailUrl.match(/\/post\/[^/]+\/(\d+)/)?.[1],
                        title: normalizeText(detailBody?.post_title) || domFallback.title || row.title,
                        publishedAt,
                        detailUrl: domFallback.detailUrl,
                        sourceUrl: sourceUrl || normalizeText(detailBody?.post_url) || undefined,
                        content: picked.content,
                        contentSource: picked.contentSource,
                        pageNumber: pageState.activePage,
                        rowIndex
                    });
                }
                catch (error) {
                    items.push({
                        title: row.title,
                        publishedAt: normalizeIsoDate(parseGetTopicTimestamp(row.createdAtText)?.toISOString()),
                        error: error instanceof Error ? error.message : String(error),
                        pageNumber: pageState.activePage,
                        rowIndex
                    });
                }
                finally {
                    const currentUrl = await page.evaluate("location.href").catch(() => subjectUrl);
                    if (currentUrl.includes("/post/")) {
                        await returnToSubjectPage(page).catch(() => undefined);
                    }
                }
            }
            if (shouldStop) {
                break;
            }
            hasNext = (await readPaginationState(page)).hasNext;
            if (hasNext) {
                await goToNextPage(page, pageState.activePage);
            }
        }
        return {
            creatorId,
            creatorName,
            subjectUrl,
            items
        };
    }
    finally {
        await page.closeWindow();
    }
}
