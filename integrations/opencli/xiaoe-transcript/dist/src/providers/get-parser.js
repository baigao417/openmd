function parseJson(value) {
    try {
        return JSON.parse(value);
    }
    catch {
        return undefined;
    }
}
export function parseGetEventStream(input) {
    const chunks = input.split(/\n\n+/).map((chunk) => chunk.trim()).filter(Boolean);
    const events = [];
    for (const chunk of chunks) {
        const dataLines = chunk
            .split(/\n/)
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trim())
            .filter(Boolean);
        for (const line of dataLines) {
            const payload = parseJson(line);
            if (!payload || typeof payload !== 'object') {
                continue;
            }
            const msgType = Number(payload.msg_type);
            if (Number.isNaN(msgType)) {
                continue;
            }
            events.push({ msgType, payload: payload });
        }
    }
    return events;
}
function parseEmbeddedJson(payload) {
    const data = payload.data;
    if (!data || typeof data !== 'object') {
        return undefined;
    }
    const msg = data.msg;
    if (typeof msg !== 'string') {
        return undefined;
    }
    const parsed = parseJson(msg);
    return parsed && typeof parsed === 'object' ? parsed : undefined;
}
export function summarizeGetStream(events) {
    const summary = {
        sections: [],
        sentences: [],
        summaryText: '',
        done: false
    };
    for (const event of events) {
        if (event.msgType === -1) {
            const data = event.payload.data;
            if (data && typeof data === 'object' && typeof data.note_id === 'string') {
                summary.noteId = data.note_id;
            }
            continue;
        }
        if (event.msgType === -6) {
            const parsed = parseEmbeddedJson(event.payload);
            const sections = parsed?.section_list;
            if (Array.isArray(sections)) {
                summary.sections = sections.filter((item) => !!item && typeof item === 'object');
            }
            continue;
        }
        if (event.msgType === -7) {
            const parsed = parseEmbeddedJson(event.payload);
            const sentences = parsed?.sentence_list;
            if (Array.isArray(sentences)) {
                summary.sentences = sentences.filter((item) => !!item && typeof item === 'object');
            }
            continue;
        }
        if (event.msgType === 1) {
            const parsed = parseEmbeddedJson(event.payload);
            const content = parsed?.content;
            if (typeof content === 'string') {
                summary.summaryText += content;
            }
            continue;
        }
        if (event.msgType === 101) {
            const data = event.payload.data;
            if (data && typeof data === 'object' && data.msg === 'stop') {
                summary.done = true;
            }
        }
    }
    return summary;
}
